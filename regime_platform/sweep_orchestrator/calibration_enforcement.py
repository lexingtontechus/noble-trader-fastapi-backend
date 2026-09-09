"""
Calibration Enforcement — live consumer of the EOD self-correction loop
═══════════════════════════════════════════════════════════════════════

The EOD correction pass (``eod_correction.run_eod_correction``, daily 00:30
UTC) is an AUDIT + RECOMMENDATION pass: for every symbol whose calibration
bias exceeds the threshold it writes ``nt_eod_actions`` rows:

  • |bias| >  0.10  → OVERCONFIDENT   (predicted win rate > realized)
  • |bias| < -0.10  → UNDERCONFIDENT  (predicted win rate < realized)

…with three recommendation actions each:
  ``bayesian_penalty``      shrink p_win toward 0.5 before qualification
  ``kelly_reduction``       reduce Kelly sizing by 50 %
  ``emergency_heavy_sweep`` re-run the heavy sweep with fresh params

This module is the missing CONSUMER side. Before every qualification
decision the sweep asks ``apply_calibration_adjustment``, which reads the
LATEST ``nt_eod_actions`` row for the symbol and returns the live correction
to apply:

  - p_win'  = (p_win * n + 0.5 * 10) / (n + 10)   (Bayesian shrink → 0.5)
  - kelly'  = kelly * 0.5                          (when a penalty is active)

Enforcement is controlled by ``settings.EOD_CALIBRATION_ENFORCE``
(env ``EOD_CALIBRATION_ENFORCE``, default ``1`` = enforce). When disabled
the module returns no-op (recommendations are still logged by the EOD pass;
they just aren't applied live).

NOTE: this module deliberately does NOT re-derive bias from calibration
records — it reads the EOD's own recommendation rows, so the audit trail
(nt_eod_actions) and the enforcement are guaranteed to agree. If the EOD
pass has never run for a symbol (no rows), the module returns no-op.

Design rules (mirror eod_correction.py):
  • Best-effort only — every failure is caught and logged, never raised
    into the sweep tick.
  • Supabase reads use the existing httpx helpers in supabase_client.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

# One source of truth (2026-09-09, worklog
# 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16): these
# constants and _settings_number were previously copy-pasted verbatim
# into this module ("mirror, don't share"), kept in sync only by a unit
# test — real shared imports instead, since eod_correction.py doesn't
# import anything from this module (no circularity) and IS the module
# that actually writes the nt_eod_actions rows these values
# classify/shrink. bayesian_adjusted_p_win is deliberately NOT imported
# here — found, while consolidating, to have a real behavioral
# difference at n_records=0 (this module returns raw p_win unshrunk in
# that case, "no evidence to shrink on"; eod_correction.py's version
# shrinks fully to 0.5 via the same formula) — a considered live-
# enforcement safety choice, not accidental drift, so it's kept as its
# own local definition below rather than silently unified.
from regime_platform.sweep_orchestrator.eod_correction import (
    OVERCONFIDENT_BIAS,
    UNDERCONFIDENT_BIAS,
    BAYES_PRIOR_COUNT,
    BAYES_PRIOR_PWIN,
    KELLY_REDUCTION,
    _settings_number,
)

log = logging.getLogger("regime.sweep.calibration_enforcement")

# How old an EOD action may be before it stops being enforced. The EOD
# correction now runs every 4h (EOD_CORRECTION_INTERVAL_HOURS), so the
# freshness window is 8h (2026-08-13) — a stale penalty ages out in a single
# intraday tick instead of lingering 26h. `settings.EOD_ACTION_FRESH_HOURS`
# is authoritative when set; this constant is the fallback (a test asserts
# the two stay in sync).
ACTION_FRESH_WINDOW_HOURS = 8

# Penalty actions that activate enforcement (see eod_correction module doc).
# "stagnation_kill_switch" (2026-08-22 audit fix #3c) is the Tier 2 hard
# block written by the generalized stagnation breaker — see
# apply_calibration_adjustment()'s handling below and
# worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §4 fix #3c.
PENALTY_ACTIONS = (
    "bayesian_penalty", "kelly_reduction", "emergency_heavy_sweep",
    "stagnation_kill_switch",
)

# Recommended actions that should NOT downgrade sizing (audit-only).
AUDIT_ONLY_ACTIONS = ("parameter_decay_check",)


def bayesian_adjusted_p_win(p_win: float, n_records: int) -> float:
    """Shrink ``p_win`` toward 0.5 with a Bayesian prior.

    p_win' = (p_win * n + 0.5 * prior_count) / (n + prior_count)

    With few calibration records the prior dominates (shrink hard); with
    many records the observed p_win dominates (shrink less).
    ``prior_count`` (BAYES_PRIOR_COUNT) is registry-tunable (Layer 1); the
    0.5 anchor (BAYES_PRIOR_PWIN) is not — see §2.5.2.1.

    NOT the same function as ``eod_correction.bayesian_adjusted_p_win``
    despite the identical name/formula — this one deliberately returns
    ``p_win`` UNCHANGED at ``n_records=0`` ("no evidence to shrink on")
    rather than shrinking fully to 0.5, since this is the live
    enforcement path and ``n_records=0`` can occur here for a mundane
    reason (an ``nt_eod_actions`` row missing its ``n``/``count`` field)
    rather than meaning "confirmed zero calibration evidence." See this
    module's import comment (top of file) — found via the 2026-09-09
    duplicated-constants consolidation, kept as a deliberate divergence
    rather than silently unified.
    """
    n = max(int(n_records or 0), 0)
    if n == 0:
        # No records — no evidence to shrink on; keep the raw p_win.
        return float(p_win)
    prior_count = _settings_number("BAYES_PRIOR_COUNT", BAYES_PRIOR_COUNT)
    return (float(p_win) * n + BAYES_PRIOR_PWIN * prior_count) / (
        n + prior_count
    )


# NOTE (2026-09-09): ``fetch_historical_p_win()`` — the ``p_historical``
# EV-blend source (2026-08-23, worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md
# R3) — was removed here. It read ``v_eod_calibration_bias_latest`` and fed
# a Bayesian-shrunk realized win rate directly into ``compute_ev()``'s
# logit-pooled blend, independently of (and stacked on top of) this same
# module's ``apply_calibration_adjustment()`` below, which reads the EOD
# correction loop's own ``nt_eod_actions`` recommendation and applies a
# separate, hard, post-hoc Bayesian shrink to the ALREADY-blended p_win.
# Both mechanisms were answering "is this symbol's confidence trustworthy
# right now" off the same underlying calibration-bias evidence, computed
# two different ways — see worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md.
# Consolidated to ``apply_calibration_adjustment`` (the hard, post-hoc
# mechanism) as the sole calibration-correction path; the EV blend's
# ``p_historical`` source is simply never fed a value by production
# callers anymore (compute_ev()'s existing missing-source renormalization
# handles that gracefully — no blend-formula change needed).


async def fetch_latest_eod_action(
    symbol: str, actions: Optional[tuple[str, ...]] = None,
) -> Optional[dict[str, Any]]:
    """Fetch the most recent ``nt_eod_actions`` row for ``symbol``.

    Returns the row dict (or ``None`` on failure / no rows).

    ``actions``, when given, restricts the query to those action types
    server-side (``action=in.(...)``) — pass ``PENALTY_ACTIONS`` to get the
    latest ENFORCEABLE row specifically, ignoring audit-only rows written
    by other subsystems in between. This matters as of 2026-08-23: Layer
    2's ``regime_shift_alert`` (and the older ``parameter_decay_check``)
    write to the SAME table, and without this filter, whichever one of
    those happens to be the true latest row silently defeats BOTH real
    callers of this function — ``apply_calibration_adjustment`` (reads
    "no penalty active" and skips enforcement entirely) and
    ``drift_monitor.check_drift`` (its "don't churn" freshness check reads
    "no fresh penalty" and re-fires even though one exists underneath).
    Confirmed live: USDCAD had 9 consecutive ``regime_shift_alert`` rows
    with zero penalty actions interspersed across a 2-hour span, during
    which any active penalty for it was invisible to both call sites. Both
    real callers pass ``actions=PENALTY_ACTIONS``; ``actions=None``
    (default) preserves the original "latest row of any type" behavior for
    any future caller that genuinely wants that.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        log.debug("Supabase not configured — no EOD action for %s", symbol)
        return None
    url = _url("nt_eod_actions")
    params: dict[str, str] = {
        "select": "action,bias,ts,detail",
        "symbol": f"eq.{symbol}",
        "order": "ts.desc",
        "limit": "1",
    }
    if actions:
        params["action"] = f"in.({','.join(actions)})"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("fetch_latest_eod_action %s failed: %s", symbol, exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    return data[0]


def _action_is_fresh(action: Optional[dict[str, Any]]) -> bool:
    """An action is enforceable if it exists and is within the fresh window."""
    if not action:
        return False
    from datetime import datetime, timezone

    ts_raw = action.get("ts")
    if not ts_raw:
        return True  # no ts — assume fresh rather than enforcing nothing
    try:
        if isinstance(ts_raw, str):
            # ISO 8601 with optional Z suffix.
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        else:
            ts = ts_raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        fresh_hours = getattr(settings, "EOD_ACTION_FRESH_HOURS", None)
        if not isinstance(fresh_hours, (int, float)) or fresh_hours <= 0:
            fresh_hours = ACTION_FRESH_WINDOW_HOURS
        return age_h <= float(fresh_hours)
    except (ValueError, TypeError):
        return True  # unparseable — assume fresh


async def apply_calibration_adjustment(
    symbol: str,
    *,
    p_win: float,
    kelly_f: float,
    venue: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the calibration correction for one symbol.

    Reads the latest ``nt_eod_actions`` row. When a penalty is active (and
    fresh), returns adjusted ``p_win`` (Bayesian shrink toward 0.5) and
    ``kelly_f`` (× 0.5), plus the status/bias for stamping. Otherwise
    returns the inputs unchanged.

    Returns:
        {
          "applied": bool,          # True when a penalty was enforced
          "status": str | None,     # OVERCONFIDENT / UNDERCONFIDENT / None
          "bias": float | None,
          "p_win": float,           # possibly adjusted
          "kelly_f": float,         # possibly adjusted
          "reason": str,
        }

    ``venue`` is accepted for API symmetry with fetch_calibration_bias but
    the EOD actions table is keyed by symbol only (no venue column).
    """
    noop = {
        "applied": False,
        "status": None,
        "bias": None,
        "p_win": float(p_win),
        "kelly_f": float(kelly_f),
        "reason": "enforcement_disabled",
    }
    if not settings.EOD_CALIBRATION_ENFORCE:
        return noop

    try:
        # actions=PENALTY_ACTIONS (2026-08-24 fix): fetch the latest
        # ENFORCEABLE row specifically, not the latest row of any type.
        # Without this, an audit-only row (regime_shift_alert,
        # parameter_decay_check) written after a still-fresh penalty would
        # be seen here instead, silently no-op'ing enforcement for a
        # symbol that has an active, fresh penalty underneath it — see
        # fetch_latest_eod_action's docstring for the live-confirmed case.
        action = await fetch_latest_eod_action(symbol, actions=PENALTY_ACTIONS)
    except Exception as exc:
        log.warning("calibration adjustment %s failed: %s", symbol, exc)
        return {**noop, "reason": f"fetch_error:{type(exc).__name__}"}

    if not action:
        return {**noop, "reason": "no_eod_action"}

    action_name = action.get("action")

    if not _action_is_fresh(action):
        return {
            **noop,
            "status": None,
            "bias": _num(action.get("bias")),
            "reason": "action_stale",
        }

    # Penalty active → apply Bayesian shrink + Kelly reduction.
    # The EOD row's detail carries the n used to compute the penalty.
    # Accept dict (current writer) OR legacy double-encoded JSON string
    # (pre-H2 rows) so enforcement works on both.
    _raw_detail = action.get("detail")
    detail = {}
    if isinstance(_raw_detail, dict):
        detail = _raw_detail
    elif isinstance(_raw_detail, str):
        try:
            _parsed = json.loads(_raw_detail)
            if isinstance(_parsed, dict):
                detail = _parsed
        except Exception:
            detail = {}
    n_records = _num(detail.get("n")) or _num(action.get("count")) or 0
    bias = _num(action.get("bias"))

    if action_name == "stagnation_kill_switch":
        # Tier 2 hard block (fix #3c, 2026-08-22 audit): the symbol has been
        # stuck in the same over/underconfident status for
        # settings.STAGNATION_TIER2_CYCLES+ consecutive 4h correction
        # cycles — a parameter re-sweep provably isn't fixing it (see
        # worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2). Force
        # kelly_f to 0.0 unconditionally so the symbol cannot qualify
        # (kelly_min_ok always fails at 0.0) — no bayesian-shrink math,
        # just a hard stop until the stagnation streak resets (either the
        # bias returns to CALIBRATED, or STAGNATION_RESET_CYCLES elapses
        # and eod_correction.py automatically forces a fresh attempt).
        adjusted_p_win = 0.5
        adjusted_kelly = 0.0
        status = "STAGNATION_KILL_SWITCH"
        log.warning(
            "[calibration:enforce] %s: STAGNATION KILL SWITCH (bias=%s, "
            "streak=%s) → kelly forced to 0.0",
            symbol, bias, detail.get("stagnation_streak"),
        )
        return {
            "applied": True,
            "status": status,
            "bias": bias,
            "p_win": adjusted_p_win,
            "kelly_f": adjusted_kelly,
            "reason": f"penalty:{action_name}",
        }

    # Tier 1 (fix #3c) writes a deeper kelly_reduction_override in detail
    # when the symbol has been stagnant for 2+ cycles; otherwise falls back
    # to the standard KELLY_REDUCTION (0.5).
    kelly_reduction = _num(detail.get("kelly_reduction_override")) or _settings_number(
        "KELLY_REDUCTION", KELLY_REDUCTION
    )

    adjusted_p_win = bayesian_adjusted_p_win(p_win, n_records)
    adjusted_kelly = float(kelly_f) * kelly_reduction
    status = "OVERCONFIDENT" if (bias is not None and bias > OVERCONFIDENT_BIAS) else (
        "UNDERCONFIDENT" if (bias is not None and bias < UNDERCONFIDENT_BIAS) else "PENALTY"
    )
    log.info(
        "[calibration:enforce] %s: %s bias=%s n=%s → p_win %.4f→%.4f, kelly %.6f→%.6f "
        "(kelly_reduction=%.2f)",
        symbol, status, bias, n_records, p_win, adjusted_p_win, kelly_f, adjusted_kelly,
        kelly_reduction,
    )
    return {
        "applied": True,
        "status": status,
        "bias": bias,
        "p_win": adjusted_p_win,
        "kelly_f": adjusted_kelly,
        "reason": f"penalty:{action_name}",
    }


def _num(v: Any) -> Optional[float]:
    """Best-effort numeric coercion (None-safe)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
