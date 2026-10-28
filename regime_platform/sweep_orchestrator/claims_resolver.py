"""
Claims resolver — Phase A scoring hook (eod_resolver integration).

Worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4 Phase A + migration 218.

The SINGLE-RESOLVER contract, taken to its strongest form: claims are scored
inside ``eod_resolver._resolve_one`` / ``_resolve_one_bars`` (the PBM-hook
precedent) so each claim receives:

  * the SAME verdict its signal row received (``resolved`` — produced by
    ``settlement_contract``; there is no second resolution rule here), and
  * the SAME post-entry bar window the signal was resolved on (``closes`` /
    the closes extracted from the OHLC bars) — not even a second bar fetch.

One resolver, one bar window, two ledgers in lockstep by construction.

Per-claim outcome (JSONB):
  result, exit_price, hold_bars, exit_ts          — the settlement verdict
  coverage50 / coverage80 / coverage90            — central-band empirical
                                                    coverage of the fan vs
                                                    realized hourly closes
  pinball, crps                                   — mean pinball over the 7
                                                    levels × scored horizons;
                                                    CRPS = 2 × pinball (the
                                                    exact integral identity
                                                    for a quantile fan)
  brier_resolve                                   — (p_resolve − resolved)²
                                                    over ALL outcome classes
                                                    (expired counts as
                                                    unresolved — the honest
                                                    read of the claim)
  brier_win                                       — (p_win_given_resolve −
                                                    win)², tp/sl rows only
  n_scored_h                                      — hourly closes actually
                                                    scored (honest when a
                                                    late fetch shortens the
                                                    window)

Index discipline (the ``_ensure_reach_index`` precedent): the open-claims
ledger is fetched ONCE per resolve run and read synchronously by the hooks.
Fail-open everywhere: flag off, table missing (migration not yet applied),
or a fetch error ⇒ empty index ⇒ every hook is a no-op. ``void_stale_claims``
(EOD lookback Part) bounds the open pool for claims orphaned by repeated
scoring failures.

Pure functions first (``_pinball`` / ``fan_metrics`` / ``brier_fields`` /
``score_claim_outcome`` are stdlib-only and offline-testable); I/O is
confined to the module's tail, mirroring settlement_contract.py's rule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.claims import (
    FAN_BANDS,
    FAN_QUANTILES,
    NT_SIGNAL_CLAIMS_COLUMNS,
    _claims_on,
)
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.claims_resolver")

_CLAIMS_PATCH_COLUMNS: frozenset[str] = frozenset({
    "status", "outcome", "resolved_at",
})

# Run-scoped open-claims index, keyed by signal_id (the _REACH_INDEX
# precedent: primed once per resolve run by ensure_claims_index, read
# synchronously by the _resolve_one hooks, popped per signal as it scores).
_CLAIMS_INDEX: dict[str, list[dict[str, Any]]] = {}


# ─── Pure scoring kernels (stdlib only; offline-testable) ────────────────────


def _pinball(q: float, q_value: float, realized: float) -> float:
    """Pinball (quantile) loss for one level: (y−ŷ)·q if y≥ŷ else (ŷ−y)·(1−q)."""
    d = realized - q_value
    return d * q if d >= 0.0 else -d * (1.0 - q)


def fan_metrics(
    fan: list[dict[str, Any]] | None,
    realized_closes: list[float] | None,
) -> dict[str, Any]:
    """Coverage / pinball / CRPS of a registered fan vs realized closes.

    ``realized_closes[h-1]`` is the close of walk-bar h — the same 1-based
    convention the fan rows carry (and the same bars the settlement walk
    inspected). Fan rows beyond the available realized window are skipped
    and ``n_scored_h`` reports what was actually scored. Coverage counts a
    central band hit when the realized close falls in [lo, hi] of that row.
    """
    out: dict[str, Any] = {}
    if not fan or not realized_closes:
        return out
    n = 0
    hits = {name: 0 for name, _lo, _hi in FAN_BANDS}
    pin_sum = 0.0
    pin_n = 0
    for row in fan:
        if not isinstance(row, dict):
            continue
        h = row.get("h")
        try:
            h = int(h)
        except (TypeError, ValueError):
            continue
        if h < 1 or h > len(realized_closes):
            continue
        try:
            r = float(realized_closes[h - 1])
        except (TypeError, ValueError):
            continue
        n += 1
        for name, lo_key, hi_key in FAN_BANDS:
            lo, hi = row.get(lo_key), row.get(hi_key)
            if lo is not None and hi is not None and float(lo) <= r <= float(hi):
                hits[name] += 1
        for q_name, q in FAN_QUANTILES:
            v = row.get(q_name)
            if v is not None:
                pin_sum += _pinball(q, float(v), r)
                pin_n += 1
    if n <= 0:
        return out
    out["n_scored_h"] = n
    for name, _lo, _hi in FAN_BANDS:
        out[name] = round(hits[name] / n, 4)
    if pin_n > 0:
        pin = pin_sum / pin_n
        out["pinball"] = round(pin, 6)
        # CRPS = 2·∫₀¹ pinball_q dq; for a discrete quantile fan the mean
        # pinball over a uniform level grid is the standard approximation.
        out["crps"] = round(2.0 * pin, 6)
    return out


def brier_fields(claimed_probs: dict[str, Any] | None, result: str) -> dict[str, Any]:
    """Per-claim Brier scores from the frozen claimed probabilities.

    ``brier_resolve`` scores p_resolve against resolution-within-window
    (tp/sl = resolved, expired = not) over ALL outcome classes — expired is
    data, not an exclusion (the PBM discipline). ``brier_win`` scores
    p_win_given_resolve against the win indicator on tp/sl rows only.
    """
    out: dict[str, Any] = {}
    if not isinstance(claimed_probs, dict):
        return out
    resolved_flag = 1.0 if result in ("tp_hit", "sl_hit") else 0.0
    p_resolve = claimed_probs.get("p_resolve")
    if p_resolve is not None:
        out["brier_resolve"] = round((float(p_resolve) - resolved_flag) ** 2, 6)
    p_win = claimed_probs.get("p_win_given_resolve")
    if p_win is not None and result in ("tp_hit", "sl_hit"):
        out["brier_win"] = round((float(p_win) - (1.0 if result == "tp_hit" else 0.0)) ** 2, 6)
    return out


def _shift_iso_hours(ts_iso: Any, hours: float) -> str | None:
    try:
        dt = datetime.fromisoformat(str(ts_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(hours=hours)).isoformat()
    except (TypeError, ValueError):
        return None


def score_claim_outcome(
    claim: dict[str, Any],
    resolved: dict[str, Any],
    realized_closes: list[float] | None,
) -> dict[str, Any]:
    """The outcome JSONB for one claim — pure, from frozen fields only.

    ``resolved`` is the verdict dict the settlement contract produced for
    the signal row (outcome/exit_price/hold_bars/mfe/mae); the claim echoes
    it, never recomputes it.
    """
    result = str(resolved.get("outcome") or "expired")
    hold = resolved.get("hold_bars")
    try:
        hold_i = int(hold) if hold is not None else None
    except (TypeError, ValueError):
        hold_i = None
    outcome: dict[str, Any] = {
        "result": result,
        "exit_price": resolved.get("exit_price"),
        "hold_bars": hold_i,
        "exit_ts": _shift_iso_hours(claim.get("ts"), hold_i) if hold_i is not None else None,
    }
    outcome.update(fan_metrics(claim.get("fan"), realized_closes))
    outcome.update(brier_fields(claim.get("claimed_probs"), result))
    return outcome


def bars_to_closes(bars: list[dict[str, Any]] | None) -> list[float]:
    """Extract closes from OHLC bars (both ``close`` and ``c`` key aliases)."""
    out: list[float] = []
    if not bars:
        return out
    for b in bars:
        if isinstance(b, dict):
            c = b.get("close", b.get("c"))
            if c is None:
                continue
            try:
                out.append(float(c))
            except (TypeError, ValueError):
                continue
    return out


# ─── I/O tail (fail-open; never raises into the resolver) ────────────────────


async def ensure_claims_index() -> int:
    """Prime the run-scoped open-claims index (once per resolve run).

    Fail-open: flag off / Supabase unconfigured / table missing / fetch
    error ⇒ empty index ⇒ every hook this run is a no-op (claims stay open
    and are re-primed next run).
    """
    global _CLAIMS_INDEX
    if not _claims_on() or not settings.SUPABASE_URL:
        _CLAIMS_INDEX = {}
        return 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_signal_claims"),
                headers=_headers(),
                params={
                    "select": "*",
                    "status": "eq.open",
                    "order": "ts.asc",
                    "limit": "5000",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        idx: dict[str, list[dict[str, Any]]] = {}
        if isinstance(rows, list):
            for r in rows:
                sid = str(r.get("signal_id") or "")
                if sid:
                    idx.setdefault(sid, []).append(r)
        _CLAIMS_INDEX = idx
        return len(idx)
    except Exception as exc:
        logger.warning("claims_resolver: open-claims index fetch failed: %s", exc)
        _CLAIMS_INDEX = {}
        return 0


async def _patch_claim(claim_id: Any, patch: dict[str, Any]) -> bool:
    """PATCH one nt_signal_claims row by claim_id. True on success."""
    if not settings.SUPABASE_URL or claim_id is None:
        return False
    body = {k: v for k, v in patch.items() if k in _CLAIMS_PATCH_COLUMNS}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                _url("nt_signal_claims"),
                headers=_headers(),
                params={"claim_id": f"eq.{claim_id}"},
                json=body,
            )
            if not resp.is_success:
                logger.error(
                    "claims_resolver: PATCH claim %s HTTP %d — %s",
                    claim_id, resp.status_code, resp.text[:300],
                )
                return False
            return True
    except Exception as exc:
        logger.error(
            "claims_resolver: PATCH claim %s failed: %s: %r",
            claim_id, type(exc).__name__, exc,
        )
        return False


async def score_claims_for_signal(
    sig: dict[str, Any],
    resolved: dict[str, Any],
    realized_closes: list[float] | None,
) -> int:
    """Score every open claim of one just-resolved signal. Never raises.

    Called from the eod_resolver hooks AFTER the signal row's terminal
    PATCH landed — the claim inherits that verdict verbatim. Pops the
    signal's claims from the run-scoped index (score-once per run; a failed
    PATCH leaves the row open for the next run's re-prime).
    """
    if not _claims_on():
        return 0
    sid = str(sig.get("signal_id") or "")
    if not sid:
        return 0
    claims = _CLAIMS_INDEX.pop(sid, None)
    if not claims:
        return 0
    scored = 0
    for claim in claims:
        if not isinstance(claim, dict) or claim.get("status") != "open":
            continue
        try:
            outcome = score_claim_outcome(claim, resolved, realized_closes)
        except Exception as exc:
            logger.debug(
                "claims_resolver: score math failed for claim %s: %s",
                claim.get("claim_id"), exc,
            )
            continue
        ok = await _patch_claim(claim.get("claim_id"), {
            "status": "resolved",
            "outcome": outcome,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        })
        if ok:
            scored += 1
    if scored:
        logger.debug(
            "claims_resolver: %s %s → %s — %d claim(s) scored",
            sig.get("symbol"), sid[:8], resolved.get("outcome"), scored,
        )
    return scored


async def void_stale_claims(max_age_days: int = 14) -> int:
    """Void open claims older than ``max_age_days`` (bulk PATCH, one call).

    The EOD-lookback hygiene pass: claims orphaned by repeated scoring
    failures (or by signal rows lost before their resolution) must not
    accumulate in the open pool forever. A voided claim carries
    ``outcome = {result: "void", reason: "stale_open"}`` — it is excluded
    from scoring aggregates, never silently deleted.
    """
    if not _claims_on() or not settings.SUPABASE_URL:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                _url("nt_signal_claims"),
                headers={**_headers(), "Prefer": "return=representation"},
                params={"status": "eq.open", "ts": f"lt.{cutoff}"},
                json={
                    "status": "void",
                    "outcome": {"result": "void", "reason": "stale_open"},
                },
            )
            if not resp.is_success:
                logger.error(
                    "claims_resolver: void sweep HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
                return 0
            rows = resp.json()
            return len(rows) if isinstance(rows, list) else 0
    except Exception as exc:
        logger.warning("claims_resolver: void sweep failed: %s", exc)
        return 0


# Re-export for the resolver's schema-lag discipline (whitelist shared with
# the registration module — one schema, two writers).
__all__ = [
    "NT_SIGNAL_CLAIMS_COLUMNS", "ensure_claims_index", "score_claims_for_signal",
    "void_stale_claims", "fan_metrics", "brier_fields", "score_claim_outcome",
    "bars_to_closes",
]
