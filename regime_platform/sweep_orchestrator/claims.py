"""
Signal claims layer — Phase A registration.

Worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4 Phase A + migration 218.

One ``nt_signal_claims`` bracket claim per QUALIFIED signal, registered at
qualification time alongside the paper-book open and the broadcast (the
acted-on book — the same population whose brackets the frontend sees). The
claim pre-registers, frozen at write time:

  * the live bracket (ENTRY/SL/TP + direction) — evidence, never an input
    to level selection; geometry is untouched,
  * a PRICE-space quantile fan (q05..q95, hourly steps h = 1..48 where h is
    the 1-based walk-bar of the SAME 48-bar window the settlement contract
    walks). Phase A source: the house ATR normal-approximation
    (``entry·exp(z·σ·√h)``, σ = ATR/entry × pbm_sigma_atr) because NO live
    multi-horizon quantile source exists today (TimesFM live = horizon-1
    q10/q90 3-point stamp; vol_backtest arms are research-only). Honest
    provenance label: ``source = "atr_normal"`` — one value beyond the
    design doc's three, for the same reason the doc demands provenance,
  * ``bracket_ref`` with ``tp_q``/``sl_q`` — read-only fan-space positions
    of the bracket levels at the claim's reference horizon,
  * ``claimed_probs`` — the probabilities already computed at qualification
    (p_resolve / p_win_given_resolve / p_win_bracket from the bracket
    table; π_win/π_loss/π_expire from the barrier null).

Scoring happens in the EOD resolver (claims_resolver.py hook) through the
SAME settlement contract verdict the signal row receives — no second
resolver, no gate/EV/geometry/p_win/MT5 changes (Phase A non-goals).

Write pattern: ``posterior.record_pbm_shadow`` — fail-open httpx POST,
module-owned column whitelist, fire-and-forget dispatch, never raises.
Flag: ``claims_mode`` (off|shadow, default shadow).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.settlement_contract import EXPIRY_BARS
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.claims")

# ─── Column whitelist (module-owned; one service, one schema) ────────────────

NT_SIGNAL_CLAIMS_COLUMNS: frozenset[str] = frozenset({
    "claim_id", "signal_id", "symbol", "ts", "deadline_ts",
    "kind", "source", "space", "horizon_h", "fan", "bracket_ref",
    "claimed_probs", "status", "outcome", "resolved_at", "created_at",
})

# ─── Fan grid: the 7 lab levels, price space, hourly to the expiry window ────

FAN_QUANTILES: tuple[tuple[str, float], ...] = (
    ("q05", 0.05), ("q10", 0.10), ("q25", 0.25), ("q50", 0.50),
    ("q75", 0.75), ("q90", 0.90), ("q95", 0.95),
)
_Z = {name: NormalDist().inv_cdf(q) for name, q in FAN_QUANTILES}

# Central bands scored at resolution (claims_resolver.py): nominal level ->
# (lo key, hi key).
FAN_BANDS: tuple[tuple[str, str, str], ...] = (
    ("coverage50", "q25", "q75"),
    ("coverage80", "q10", "q90"),
    ("coverage90", "q05", "q95"),
)

# Default reference horizon for the tp_q/sl_q translation when the deadline
# tier stamped nothing (heavy path / cold reachability): 24h — mid-window,
# documented as a stamp convention, not a forecast.
DEFAULT_REF_H = 24


# ─── Flags (lazy import; defaults live in feature_flags._DEFAULTS) ──────────


def _flag(name: str, default: Any) -> Any:
    try:
        from regime_platform.core.feature_flags import _load
        return _load().get(name, default)
    except Exception:
        return default


def claims_mode() -> str:
    """``off`` | ``shadow`` — claims ledger registration + scoring."""
    return str(_flag("claims_mode", "shadow")).lower()


def _sigma_atr() -> float:
    """Per-bar log-vol in ATR units (same flag the PBM anchor reads)."""
    try:
        return max(float(_flag("pbm_sigma_atr", 1.0)), 1e-6)
    except (TypeError, ValueError):
        return 1.0


def _claims_on() -> bool:
    return claims_mode() != "off"


# ─── Fan construction (pure; stdlib only) ────────────────────────────────────


def build_atr_normal_fan(
    entry_price: float,
    atr: float,
    *,
    horizon_h: int = EXPIRY_BARS,
    sigma_atr: float = 1.0,
) -> list[dict[str, Any]] | None:
    """Price-space normal-approximation fan over the settlement window.

    ``entry_price·exp(z_q · σ · √h)`` with σ = ATR/entry × ``sigma_atr``
    (the house barrier convention: 1 ATR per bar in log space, barrier.py).
    Log-space stays INSIDE this function — the boundary sees price only
    (design §1 units contract; the lab's fan-axis bug class). ``h`` is the
    1-based walk-bar of the same ``horizon_h``-bar window the settlement
    contract walks. Returns None when geometry is unusable (never invents).
    """
    try:
        e, a = float(entry_price), float(atr)
    except (TypeError, ValueError):
        return None
    if not (e > 0 and a > 0):
        return None
    sigma = (a / e) * max(float(sigma_atr), 1e-6)
    fan: list[dict[str, Any]] = []
    for h in range(1, max(int(horizon_h), 1) + 1):
        row: dict[str, Any] = {"h": h}
        scale = sigma * math.sqrt(h)
        for name, _q in FAN_QUANTILES:
            row[name] = round(e * math.exp(_Z[name] * scale), 6)
        fan.append(row)
    return fan


def fan_quantile_position(
    fan: list[dict[str, Any]] | None,
    price: float,
    h_ref: int,
) -> float | None:
    """Where ``price`` sits in the fan's h_ref marginal, as a quantile.

    Linear interpolation between the 7 (value, q) knots, extrapolated along
    the outermost segment when the level is outside [q05, q95] and clamped
    to [0.001, 0.999]. Direction-agnostic: the position within the
    distribution, NOT a win probability (a buy's SL sits at a low quantile,
    its TP at a high one; a sell's are mirrored). Returns None when the fan
    or the reference row is unusable.
    """
    if not fan or price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    rows = [r for r in fan if isinstance(r, dict) and r.get("h") is not None]
    if not rows:
        return None
    h_want = max(int(h_ref), 1)
    row = min(rows, key=lambda r: abs(float(r["h"]) - h_want))
    knots = sorted(
        (float(row[name]), q) for name, q in FAN_QUANTILES
        if row.get(name) is not None
    )
    if len(knots) < 2:
        return None
    if p <= knots[0][0]:
        # extrapolate below along the first segment
        (v0, q0), (v1, q1) = knots[0], knots[1]
        if v1 == v0:
            return q0
        out = q0 - (v0 - p) / (v1 - v0) * (q1 - q0)
    elif p >= knots[-1][0]:
        (v0, q0), (v1, q1) = knots[-2], knots[-1]
        if v1 == v0:
            return q0
        out = q1 + (p - v1) / (v1 - v0) * (q1 - q0)
    else:
        for (v0, q0), (v1, q1) in zip(knots, knots[1:]):
            if v0 <= p <= v1:
                out = q0 + (p - v0) / (v1 - v0) * (q1 - q0) if v1 != v0 else q0
                break
        else:  # pragma: no cover — sorted knots + bracketing above is exhaustive
            return None
    return round(min(max(out, 0.001), 0.999), 4)


def build_bracket_ref(
    *,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    direction: str,
    fan: list[dict[str, Any]] | None,
    h_ref: int,
) -> dict[str, Any]:
    """The live bracket + its read-only fan-space positions.

    ``tp_q``/``sl_q`` are stamps ("TP at the fan's q92"), computed at the
    reference horizon — never inputs to level selection (design §1).
    """
    ref: dict[str, Any] = {
        "entry": round(float(entry_price), 6),
        "sl": round(float(stop_loss), 6),
        "tp": round(float(take_profit), 6),
        "direction": str(direction or "buy").lower(),
        "h_ref": int(h_ref),
    }
    if fan:
        ref["tp_q"] = fan_quantile_position(fan, float(take_profit), h_ref)
        ref["sl_q"] = fan_quantile_position(fan, float(stop_loss), h_ref)
    return ref


# ─── Claim registration (the write; posterior.record_pbm_shadow pattern) ────


def _shift_iso_hours(ts_iso: str, hours: float) -> str | None:
    try:
        dt = datetime.fromisoformat(str(ts_iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt + timedelta(hours=hours)).isoformat()
    except (TypeError, ValueError):
        return None


async def record_bracket_claim(
    *,
    signal_id: str,
    symbol: str,
    ts_iso: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    atr: float | None,
    source: str | None,
    p_resolve: float | None = None,
    p_win_given_resolve: float | None = None,
    p_win_bracket: float | None = None,
    expected_resolution_hours: float | None = None,
    expiry_bars: int = EXPIRY_BARS,
) -> dict[str, Any] | None:
    """Register the mandatory bracket claim for one qualified signal.

    Builds the fan (ATR normal-approx, price space), the bracket_ref with
    fan-space positions, and the claimed_probs (bracket-table probabilities
    + analytic barrier null), then POSTs one ``nt_signal_claims`` row.
    Returns the row dict so callers can log it; returns None (and never
    raises) when the flag is off, Supabase is unconfigured, the geometry is
    unusable, or the insert fails — a lost stamp is a lost stamp, exactly
    like the PBM ledger.
    """
    if not _claims_on() or not settings.SUPABASE_URL:
        return None
    try:
        entry, sl, tp = float(entry_price), float(stop_loss), float(take_profit)
    except (TypeError, ValueError):
        return None
    if not (entry > 0 and sl > 0 and tp > 0) or entry in (sl, tp):
        return None

    horizon_h = int(expiry_bars)
    fan = build_atr_normal_fan(entry, atr, horizon_h=horizon_h, sigma_atr=_sigma_atr())

    try:
        h_ref = int(round(float(expected_resolution_hours))) if expected_resolution_hours else DEFAULT_REF_H
    except (TypeError, ValueError):
        h_ref = DEFAULT_REF_H
    h_ref = min(max(h_ref, 1), horizon_h)

    bracket_ref = build_bracket_ref(
        entry_price=entry, stop_loss=sl, take_profit=tp,
        direction=direction, fan=fan, h_ref=h_ref,
    )

    # π_win / π_loss / π_expire at the bracket's own geometry — the same
    # closed-form double-barrier null the PBM anchor uses (pure, cheap).
    pi_win = pi_loss = pi_expire = None
    if atr and float(atr) > 0:
        try:
            from regime_platform.sweep_orchestrator.barrier import (
                barrier_null_from_prices,
            )
            _null = barrier_null_from_prices(
                entry, tp, sl, atr=float(atr), t_bars=horizon_h,
                sigma=_sigma_atr(), direction=direction or "buy",
            )
            pi_win = round(float(_null.p_win), 6)
            pi_loss = round(float(_null.p_loss), 6)
            pi_expire = round(float(_null.p_expire), 6)
        except Exception as exc:
            logger.debug("claims %s: barrier null unavailable: %s", symbol, exc)

    claimed_probs: dict[str, Any] = {}
    if p_resolve is not None:
        claimed_probs["p_resolve"] = round(float(p_resolve), 6)
    if p_win_given_resolve is not None:
        claimed_probs["p_win_given_resolve"] = round(float(p_win_given_resolve), 6)
    if p_win_bracket is not None:
        claimed_probs["p_win_bracket"] = round(float(p_win_bracket), 6)
    claimed_probs["pi_win"] = pi_win
    claimed_probs["pi_loss"] = pi_loss
    claimed_probs["pi_expire"] = pi_expire

    row: dict[str, Any] = {
        "signal_id": str(signal_id),
        "symbol": str(symbol),
        "ts": str(ts_iso),
        "deadline_ts": _shift_iso_hours(ts_iso, horizon_h),
        "kind": "bracket",
        "source": "atr_normal" if fan else "manual",
        "space": "price",
        "horizon_h": float(horizon_h),
        "fan": fan,
        "bracket_ref": bracket_ref,
        "claimed_probs": claimed_probs,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    row = {k: v for k, v in row.items() if k in NT_SIGNAL_CLAIMS_COLUMNS and v is not None}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_signal_claims"),
                headers={**_headers(), "Content-Type": "application/json"},
                json=[row],
            )
            if not resp.is_success:
                logger.debug(
                    "claims stamp %s HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
                return None
        return row
    except Exception as exc:
        logger.debug("claims stamp %s failed: %s", symbol, exc)
        return None
