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

    NOT used for live enforcement decisions anymore (2026-09-09, worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16, item
    #6): ``apply_calibration_adjustment`` and ``drift_monitor.check_drift``
    both read ``nt_symbol_calibration_state`` (the single current-state
    row) instead — see that function's docstring for why "reconstruct
    current state from the latest matching audit-log row" was itself the
    problem, not just the specific ``regime_shift_alert``-interference bug
    described below. Kept as a general read helper against the permanent
    ``nt_eod_actions`` audit log (multiple writers, multiple readers, by
    design) for any observability/debugging use that genuinely wants "the
    latest logged event of any/some type," not "what's currently
    enforceable."

    Returns the row dict (or ``None`` on failure / no rows).

    ``actions``, when given, restricts the query to those action types
    server-side (``action=in.(...)``) — historically ``PENALTY_ACTIONS``
    was passed here to get the latest ENFORCEABLE row specifically,
    ignoring audit-only rows written by other subsystems in between. This
    mattered as of 2026-08-23: Layer 2's ``regime_shift_alert`` (and the
    older ``parameter_decay_check``) write to the SAME table, and without
    this filter, whichever one of those happens to be the true latest row
    silently defeated BOTH real callers of this function at the time.
    Confirmed live: USDCAD had 9 consecutive ``regime_shift_alert`` rows
    with zero penalty actions interspersed across a 2-hour span, during
    which any active penalty for it was invisible to both call sites —
    the incident that motivated moving both callers off this
    reconstruction entirely rather than adding a third special case.
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

    Reads ``symbol``'s single current-state row from
    ``nt_symbol_calibration_state`` (2026-09-09, worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16, item
    #6 — the nt_eod_actions state/history split). When a penalty is
    active (and fresh), returns adjusted ``p_win`` (Bayesian shrink
    toward 0.5) and ``kelly_f`` (× 0.5), plus the status/bias for
    stamping. Otherwise returns the inputs unchanged.

    This no longer reads ``nt_eod_actions`` at all — that table remains
    the permanent, multi-writer audit log (eod_correction
    nightly/intraday, drift_monitor, regime_shift_alert all still write
    to it, unchanged), but reconstructing "what's currently enforceable"
    from its latest matching row was the exact fragility that caused two
    confirmed incidents (see ``fetch_latest_eod_action``'s docstring,
    kept below for its own remaining callers/history). The single
    current-state row structurally cannot have that problem: there is
    nothing else in that table for a different writer's row to be
    mistaken for.

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
    the calibration-state table is keyed by symbol only (no venue column).
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
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_calibration_state,
        )
        state = await fetch_calibration_state(symbol)
    except Exception as exc:
        log.warning("calibration adjustment %s failed: %s", symbol, exc)
        return {**noop, "reason": f"fetch_error:{type(exc).__name__}"}

    if not state or state.get("action") in (None, "none"):
        return {**noop, "reason": "no_eod_action"}

    action_name = state.get("action")

    if not _action_is_fresh({"ts": state.get("computed_at")}):
        return {
            **noop,
            "status": None,
            "bias": _num(state.get("bias")),
            "reason": "action_stale",
        }

    detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
    n_records = _num(state.get("n_records")) or 0
    bias = _num(state.get("bias"))

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

    # Tier 1 (fix #3c) writes a deeper kelly_reduction_override (its own
    # column on nt_symbol_calibration_state now, mirrored into detail for
    # audit) when the symbol has been stagnant for 2+ cycles; otherwise
    # falls back to the standard KELLY_REDUCTION (0.5).
    kelly_reduction = _num(state.get("kelly_reduction_override")) or _settings_number(
        "KELLY_REDUCTION", KELLY_REDUCTION
    )

    status = state.get("status") or "PENALTY"
    # Batch D (4.6): underconfidence is already conservative — do NOT apply
    # the same Kelly cut as overconfidence. Keep p_win Bayesian shrink (it
    # pulls toward 0.5, which is directionally correct when the model is
    # too low) but leave sizing untouched.
    if str(status).upper() == "UNDERCONFIDENT":
        adjusted_p_win = bayesian_adjusted_p_win(p_win, n_records)
        adjusted_kelly = float(kelly_f)
        log.info(
            "[calibration:enforce] %s: UNDERCONFIDENT bias=%s n=%s → p_win %.4f→%.4f, "
            "kelly unchanged %.6f (no underconfidence cut)",
            symbol, bias, n_records, p_win, adjusted_p_win, adjusted_kelly,
        )
        return {
            "applied": True,
            "status": status,
            "bias": bias,
            "p_win": adjusted_p_win,
            "kelly_f": adjusted_kelly,
            "reason": "underconfidence_no_kelly_cut",
        }

    adjusted_p_win = bayesian_adjusted_p_win(p_win, n_records)
    adjusted_kelly = float(kelly_f) * kelly_reduction
    # status: read directly from the state row (2026-09-09) — it was
    # already classified via eod_correction._classify_bias/drift_monitor
    # at write time using the real canonical thresholds; re-deriving a
    # third time here from bias vs OVERCONFIDENT_BIAS/UNDERCONFIDENT_BIAS
    # was itself a small instance of the "one arbiter" problem this
    # session's review found (worklog SS16). "PENALTY" fallback preserved
    # for the defensive case where an older/malformed row has no status.
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
