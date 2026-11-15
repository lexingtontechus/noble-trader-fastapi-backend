"""claims.py — the CRT claims sibling (CRT-2, kind='crt_option').

The CRT v5 CONT paper engine's prediction ledger on ``nt_signal_claims``
(migration 221 widened the CHECK lists), following the claims-layer contract
with one CRT-specific divergence, stated up front:

- MINT: one claim per paper FILL, at the moment the position opens (C3
  close). ``claimed_probs`` carries the option's PRE-REGISTERED numbers as
  of the frozen C2-close decision — the walk-forward OOS EV/SD/t/n, the
  decay/CUSUM state, the rolling stats — plus the implied win probability
  (fixed-R inversion) and the variable-Kelly SHADOW (migration 174's
  discipline: recorded, never used for sizing — the engine stays
  EA-verbatim confidence sizing).
- RESOLVE: CRT's OWN close pass patches the claim — the single-resolver
  contract extended, not bent: the labeler's ``walk_barriers`` produced
  the position's verdict (the same walk the paper row's PATCH carries);
  the claim echoes it, never recomputes it. The platform EOD machinery
  never touches CRT claims (its hooks pop nt_signal_sim-keyed claims;
  CRT signal_ids are the paper rows' self-minted UUIDs). The brier fields
  reuse ``claims_resolver.brier_fields`` — the one scoring-kernel source.

Claim row contract (migration 218 columns + 221's widened values):

- ``signal_id`` = the paper position row's signal_id (the ledger sibling
  key — ``unique (signal_id, kind)`` holds one claim per position);
- ``kind='crt_option'`` / ``source='crt_walkforward'`` — the artifact's
  own numbers, no house overlay;
- ``ts`` = the paper row's open_ts (C3 close — fill-knowledge moment);
- ``deadline_ts`` = ts + hold x tf_seconds (the option's own barrier
  horizon; ``horizon_h`` the same in hours);
- ``bracket_ref`` = the live fill/SL/TP + option geometry (read-only
  stamp — claims never set levels);
- ``claimed_probs`` = the pre-registration + implied p_win + kelly shadow;
- resolution PATCH guarded by ``status=eq.open`` (a claim voided by the
  EOD ``void_stale_claims`` safety net — open > 14d — is never
  re-touched: claims are immutable once written except
  status/outcome/resolved_at).

Everything fails OPEN: an unconfigured Supabase, a missing migration, or
a transient error logs and returns False/None/[] — a lost stamp is a lost
stamp (the platform claims contract); the paper book remains the durable
record. All DB access is lazy-imported (the crt/paper.py convention).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("crt.claims")

CLAIMS_TABLE = "nt_signal_claims"
CRT_CLAIM_KIND = "crt_option"
CRT_CLAIM_SOURCE = "crt_walkforward"

# migration 218's columns CRT sets (claim_id/server defaults, fan/arm/
# calib_level are never set by CRT; space is always 'price').
CRT_CLAIM_COLUMNS = (
    "signal_id", "symbol", "ts", "deadline_ts", "kind", "source", "space",
    "horizon_h", "bracket_ref", "claimed_probs", "status", "created_at",
)

SESS_NAMES = {0: "OUT", 1: "NY", 2: "ASIA"}
MODEL_NAMES = {0: "STOP", 1: "C3C", 2: "CONT"}

# the settlement-vocabulary map (claims_resolver.brier_fields reads
# "tp_hit"/"sl_hit"/"expired"; CRT label outcomes are 1/-1/0).
RESULT_NAMES = {1: "tp_hit", -1: "sl_hit", 0: "expired"}

_CLAIM_SELECT = ("signal_id,symbol,ts,kind,source,status,claimed_probs,"
                 "bracket_ref,outcome")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ── pure kernels (offline-testable) ─────────────────────────────────────────


def implied_p_win(ev_r: float, r: float) -> float | None:
    """Fixed-R inversion: EV = p*(r+1) - 1  ->  p = (EV+1)/(R+1).

    The honest CRT claim: the walk-forward OOS EV *is* the pre-registered
    promise; its implied win probability (clamped to [0,1]) is what the
    leaderboard's brier discipline scores against reality. No p_resolve
    is claimed — the CONT model can time-exit and the OOS stats carry no
    resolve-rate.
    """
    if r <= 0 or ev_r is None:
        return None
    try:
        p = (float(ev_r) + 1.0) / (float(r) + 1.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(1.0, max(0.0, p))


def kelly_shadow(p_win: float | None, r: float) -> dict[str, Any]:
    """Variable binary Kelly f* from the implied p_win — SHADOW ONLY.

    The migration-174 discipline (variable_kelly precedent): computed and
    recorded for later measurement, NEVER a sizing input — the engine
    stays EA-verbatim confidence sizing (0.25%→0.50% on OOS t). f* = p −
    (1−p)/r; negative f* means the evidence says no-trade at any size.
    """
    if p_win is None or r <= 0:
        return {}
    p = float(p_win)
    f = p - (1.0 - p) / float(r)
    return {
        "p_win": round(p, 6),
        "f_binary": round(f, 6),
        "f_half": round(f / 2.0, 6),
        "note": "shadow only — never a sizing input (engine stays EA-verbatim)",
    }


def build_option_claim(
    *, symbol: str, tf: str, option: Any, decision: Any, label: Any,
    position_row: dict[str, Any], artifact_runid: str | None = None,
    open_ts_epoch: float, cash_risk: float,
    source: str = CRT_CLAIM_SOURCE,
) -> dict[str, Any]:
    """One nt_signal_claims row for a gated, filled CRT setup (pure).

    ``decision.monitor`` is the freeze-time (C2-close) monitor snapshot the
    gate chain consumed — the claim stamps the inputs, not a re-derivation
    (no lookahead, no hindsight). ``position_row`` is the paper row the
    claim is the ledger sibling of (signal_id shared).
    """
    p_win = implied_p_win(option.base_ev, option.r)
    monitor = dict(getattr(decision, "monitor", None) or {})
    claimed_probs: dict[str, Any] = {}
    if p_win is not None:
        claimed_probs["p_win_bracket"] = round(p_win, 6)
        claimed_probs["p_win_given_resolve"] = round(p_win, 6)
    # the pre-registration provenance (frozen at C2 close, evidence only)
    claimed_probs.update({
        "oos_ev": round(float(option.base_ev), 6),
        "oos_sd": round(float(option.base_sd), 6),
        "oos_t": round(float(option.oos_t), 6),
        "oos_n": int(option.oos_n),
        "decay_state": monitor.get("state", option.state_name()),
        "s_cusum": round(float(monitor.get("s_cusum", option.s_cusum)), 6),
        "live_n": int(monitor.get("live_n", option.live_n)),
        "live_ev": round(float(monitor.get("live_ev", option.live_ev)), 6),
        "roll_ev": round(float(monitor.get("roll_ev", option.roll_ev)), 6),
        "rl_n": int(monitor.get("rl_n", option.rl_n)),
        "rl_ev": round(float(monitor.get("rl_ev", option.rl_ev)), 6),
        "risk_pct": round(float(decision.risk_pct), 6),
        "gate_reason": str(decision.reason),
        "kelly_shadow": kelly_shadow(p_win, option.r),
    })
    if artifact_runid:
        claimed_probs["artifact_runid"] = str(artifact_runid)
    horizon_s = float(option.hold) * float(option.tf_seconds)
    bracket_ref = {
        "entry": float(label.fill_price),
        "sl": float(label.sl),
        "tp": float(label.tp),
        "direction": "buy" if label.trade_buy else "sell",
        "r": float(option.r),
        "hold": int(option.hold),
        "tf": str(tf),
        "session": SESS_NAMES.get(option.sess, str(option.sess)),
        "setup_dir": str(option.dir),
        "model": MODEL_NAMES.get(option.model, str(option.model)),
        "entry_ref": float(label.entry_ref),
        "risk": float(label.risk),
        "cash_risk": round(float(cash_risk), 6),
        "size_units": float(position_row.get("size_units") or 0.0),
        "venue": str(position_row.get("venue") or ""),
        "frozen_at": float(decision.frozen_at),
    }
    return {
        "signal_id": str(position_row.get("signal_id")),
        "symbol": str(symbol).upper(),
        "ts": position_row.get("open_ts") or _iso(open_ts_epoch),
        "deadline_ts": _iso(open_ts_epoch + horizon_s),
        "kind": CRT_CLAIM_KIND,
        "source": str(source),
        "space": "price",
        "horizon_h": round(horizon_s / 3600.0, 6),
        "bracket_ref": bracket_ref,
        "claimed_probs": claimed_probs,
        "status": "open",
        "created_at": _iso(open_ts_epoch),
    }


def build_claim_outcome(label: Any, *, cash_risk: float,
                        close_ts_epoch: float) -> dict[str, Any]:
    """The outcome JSONB for one CRT claim — pure, from the verdict only.

    ``label`` is the SAME walk result the paper row's PATCH carries (the
    single-resolver contract); ``brier_*`` fields are added at PATCH time
    from the claim's OWN frozen claimed_probs (see resolve_position_claim).
    """
    result = RESULT_NAMES.get(int(label.outcome), "expired")
    outcome: dict[str, Any] = {
        "result": result,
        "exit_price": None if label.exit_price is None else float(label.exit_price),
        "hold_bars": int(label.held_bars),
        "exit_ts": _iso(close_ts_epoch),
        "r": round(float(label.r), 4),
    }
    if cash_risk:
        outcome["realized_pnl"] = round(float(label.r) * float(cash_risk), 6)
    return outcome


# ── evidence aggregation (the /crt/claims view + CRT-3's gate source) ───────


def _r_of(row: dict[str, Any]) -> float | None:
    try:
        r = (row.get("outcome") or {}).get("r")
        return None if r is None else float(r)
    except (TypeError, ValueError):
        return None


def _result_of(row: dict[str, Any]) -> str:
    return str((row.get("outcome") or {}).get("result") or "expired")


def wilson_interval(prop: float, n: int, z: float = 1.959963984540054) -> list[float] | None:
    """Wilson score interval (claims_leaderboard.wilson_interval math —
    kept here as the same formula so the CRT aggregate is kernel-complete
    offline; the leaderboard's own module stays the platform authority)."""
    if n <= 0:
        return None
    p = min(1.0, max(0.0, float(prop)))
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return [round(max(0.0, center - half), 6), round(min(1.0, center + half), 6)]


def aggregate_evidence(rows: list[dict[str, Any]], *,
                       lcb_z: float = 1.28) -> dict[str, Any]:
    """Pooled evidence over resolved CRT claims — the CRT-3 gate source.

    EV is mean R (the labels carry the cost model — after-costs by
    construction), t = mean/(sd/sqrt(n)) (n>1, sd>0), LCB = mean −
    z·sd/sqrt(n) at the optimizer's own z=1.28. Win rate is CLOSED-only
    (tp+sl), Wilson-intervaled. n counts every resolved claim (expired
    rows carry r — time-exit MTM — and count in EV: the PBM discipline,
    expired is data, not an exclusion).
    """
    rs = [r for r in (_r_of(row) for row in rows or []) if r is not None]
    n_total = len(rows or [])
    n_tp = sum(1 for row in rows or [] if _result_of(row) == "tp_hit")
    n_sl = sum(1 for row in rows or [] if _result_of(row) == "sl_hit")
    n_exp = sum(1 for row in rows or [] if _result_of(row) == "expired")
    ev = sum(rs) / len(rs) if rs else 0.0
    sd = (math.sqrt(sum((r - ev) ** 2 for r in rs) / (len(rs) - 1))
          if len(rs) > 1 else 0.0)
    t_stat = (ev / (sd / math.sqrt(len(rs)))
              if len(rs) > 1 and sd > 0 else 0.0)
    lcb = ev - float(lcb_z) * sd / math.sqrt(len(rs)) if len(rs) > 1 else 0.0
    closed = n_tp + n_sl
    win_rate = (n_tp / closed) if closed else None
    return {
        "n": n_total, "n_tp": n_tp, "n_sl": n_sl, "n_expired": n_exp,
        "n_r": len(rs),
        "win_rate_closed": round(win_rate, 4) if win_rate is not None else None,
        "win_rate_wilson95": wilson_interval(win_rate, closed) if closed else None,
        "ev_r": round(ev, 6), "sd_r": round(sd, 6),
        "t_stat": round(t_stat, 4), "lcb_r": round(lcb, 6),
        "lcb_z": float(lcb_z),
    }


def aggregate_by_option(rows: list[dict[str, Any]], *,
                        lcb_z: float = 1.28) -> list[dict[str, Any]]:
    """Per-option cells (symbol, tf, session, setup_dir) from bracket_ref.

    The per-option view of the same pooled math — the leaderboard's cell
    discipline, CRT-scoped: n per verdict recorded (the thin-H1-window
    honesty note: pooled fallback is a floor, not proof).
    """
    cells: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in rows or []:
        ref = row.get("bracket_ref") or {}
        key = (str(row.get("symbol") or ""), str(ref.get("tf") or ""),
               str(ref.get("session") or ""), str(ref.get("setup_dir") or ""))
        cells.setdefault(key, []).append(row)
    out = []
    for key in sorted(cells):
        symbol, tf, session, sdir = key
        out.append({
            "symbol": symbol, "tf": tf, "session": session, "setup_dir": sdir,
            **aggregate_evidence(cells[key], lcb_z=lcb_z),
        })
    return out


def claims_view(rows: list[dict[str, Any]], *, lcb_z: float = 1.28) -> dict[str, Any]:
    """The /crt/claims response body (pure)."""
    return {
        "kind": CRT_CLAIM_KIND,
        "generated_at": _iso(datetime.now(timezone.utc).timestamp()),
        "pooled": aggregate_evidence(rows, lcb_z=lcb_z),
        "per_option": aggregate_by_option(rows, lcb_z=lcb_z),
        "n_rows": len(rows or []),
    }


# ── I/O (lazy imports; fail-open by contract) ───────────────────────────────


async def mint_option_claim(row: dict[str, Any]) -> bool:
    """INSERT one CRT option claim. Best-effort: True on success."""
    payload = {k: v for k, v in row.items()
               if k in CRT_CLAIM_COLUMNS and v is not None}
    if not payload.get("signal_id"):
        return False
    try:
        from regime_platform.sweep_orchestrator.supabase_client import _insert_row
        return await _insert_row(CLAIMS_TABLE, payload)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.claims: mint failed: %s", exc)
        return False


async def fetch_resolved_claims(limit: int = 2000) -> list[dict[str, Any]]:
    """Resolved CRT option claims, newest first (the evidence pool)."""
    try:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_all_rows
        return await fetch_all_rows(
            CLAIMS_TABLE, _CLAIM_SELECT,
            filters={"kind": f"eq.{CRT_CLAIM_KIND}", "status": "eq.resolved"},
            order="ts.desc", max_rows=limit,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.claims: fetch resolved failed: %s", exc)
        return []


async def fetch_claim_by_signal(signal_id: str) -> dict[str, Any] | None:
    """The open crt_option claim for one paper position (None if absent)."""
    if not signal_id:
        return None
    try:
        from regime_platform.sweep_orchestrator.supabase_client import fetch_all_rows
        rows = await fetch_all_rows(
            CLAIMS_TABLE, "claim_id,signal_id,status,claimed_probs",
            filters={"signal_id": f"eq.{signal_id}",
                     "kind": f"eq.{CRT_CLAIM_KIND}"},
            max_rows=2,
        )
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.claims: fetch by signal failed: %s", exc)
        return None


async def resolve_position_claim(signal_id: str, outcome: dict[str, Any]) -> bool:
    """PATCH the position's claim to resolved — the CRT close pass.

    Single-resolver contract: ``outcome`` was built from the SAME walk
    verdict the paper row's PATCH carried. Brier fields are added here from
    the claim's OWN frozen claimed_probs (one bounded GET; fail-open — a
    claim we cannot fetch resolves without brier, never blocks the paper
    patch). The PATCH is guarded by ``status=eq.open``: a claim already
    void (the EOD 14d safety net) or resolved is never re-touched — claims
    are immutable once written except status/outcome/resolved_at.
    """
    if not signal_id:
        return False
    try:
        from regime_platform.core.settings import settings
        if not settings.SUPABASE_URL:
            return False
        claim = await fetch_claim_by_signal(signal_id)
        if claim is None:
            logger.debug("crt.claims: no claim for %s — nothing to resolve",
                         str(signal_id)[:8])
            return False
        if str(claim.get("status")) != "open":
            logger.info("crt.claims: claim %s already %s — not re-touched",
                        str(signal_id)[:8], claim.get("status"))
            return False
        result = str(outcome.get("result") or "expired")
        try:
            from regime_platform.sweep_orchestrator.claims_resolver import (
                brier_fields,
            )
            outcome = {**outcome, **brier_fields(claim.get("claimed_probs"), result)}
        except Exception:  # noqa: BLE001 - kernel import failed: resolve without brier
            logger.debug("crt.claims: brier kernel unavailable — no brier fields")
        now_iso = datetime.now(timezone.utc).isoformat()
        import httpx
        url = f"{settings.SUPABASE_URL}/rest/v1/{CLAIMS_TABLE}"
        headers = {
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                url, headers=headers,
                params={"signal_id": f"eq.{signal_id}",
                        "kind": f"eq.{CRT_CLAIM_KIND}",
                        "status": "eq.open"},
                json={"status": "resolved", "outcome": outcome,
                      "resolved_at": now_iso},
            )
            if not resp.is_success:
                logger.error("crt.claims: resolve PATCH HTTP %d — %s",
                             resp.status_code, resp.text[:200])
                return False
            return True
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.error("crt.claims: resolve failed: %s", exc)
        return False
