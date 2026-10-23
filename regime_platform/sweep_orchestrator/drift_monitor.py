"""
Drift monitor — intraday calibration fast path (2026-08-13 plan, D)
══════════════════════════════════════════════════════════════════════

The nightly 00:30 lookback and the 4h intraday correction passes recompute
bias on a schedule. This module detects bias drift BETWEEN those passes:
on each light sweep, a per-symbol rolling realized win rate (last
``EOD_DRIFT_WINDOW_HOURS`` hours) is compared against the mean predicted
p_win. When the bias exceeds ``EOD_DRIFT_THRESHOLD`` with at least
``EOD_DRIFT_MIN_SAMPLE`` records, an immediate ``bayesian_penalty`` action is
written to ``nt_eod_actions`` — the sweep's enforcement
(``calibration_enforcement.apply_calibration_adjustment``) picks it up on the
next tick, bypassing the 4h cadence.

Guards:
  * Min sample (``EOD_DRIFT_MIN_SAMPLE``) — a couple of records must not fire.
  * Fresh-action skip — if a penalty action written within
    ``EOD_ACTION_FRESH_HOURS`` already exists, do nothing (no 5-min churn).
  * Overconfidence-only — ``bias > +threshold`` is the risk direction (larger
    positions on inflated p_win). Underconfidence is conservative (smaller
    positions) and is handled by the scheduled passes.

This is one of THREE calibration-trust checks in the platform, each
tuned for a different cadence — see ``eod_correction.py``'s module-level
comment (above ``OVERCONFIDENT_BIAS``) for the full tiering table and
why they're deliberately not merged into one. ``EOD_DRIFT_THRESHOLD``
(default 0.15) is the fast/tick-level tier; this module's own default is
intentionally narrower than the 4h raw-bias band (0.30) and wider than
the 24h enforced-bias band (0.10) — see that table before changing any
of the three.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from regime_platform.core.settings import settings

log = logging.getLogger("regime.sweep.drift_monitor")


async def check_drift(symbol: str) -> dict[str, Any] | None:
    """Write an immediate penalty action when a symbol has drifted overconfident.

    Returns the written action row (dict) or ``None`` (no drift / disabled /
    fresh action already present / insufficient sample). Best-effort: never
    raises — failures are logged and swallowed so a monitor hiccup cannot
    break the sweep tick.
    """
    from regime_platform.core.feature_flags import get_eod_drift_monitor_enabled

    if not get_eod_drift_monitor_enabled():
        return None

    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            _insert_row,
            fetch_calibration_bias,
            fetch_calibration_state,
            upsert_calibration_state,
        )
        from regime_platform.sweep_orchestrator.calibration_enforcement import (
            _action_is_fresh,
            _settings_number,
        )

        window_h = float(getattr(settings, "EOD_DRIFT_WINDOW_HOURS", 48) or 48)
        min_n = int(getattr(settings, "EOD_DRIFT_MIN_SAMPLE", 20) or 20)
        # Registry-tunable (Layer 1, 2026-08-23) — see
        # worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.2.1.
        threshold = _settings_number("EOD_DRIFT_THRESHOLD", 0.15)

        # A fresh penalty already exists → the scheduled passes own this
        # symbol; do not churn actions every 5 minutes.
        #
        # 2026-09-09 (worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16):
        # reads nt_symbol_calibration_state (the single current-state
        # row) instead of reconstructing "the latest enforceable action"
        # from nt_eod_actions via a PENALTY_ACTIONS filter. That filter
        # was itself a patch for a real incident (an audit-only
        # regime_shift_alert row masking a still-fresh penalty
        # underneath it, live-confirmed for USDCAD) — reading the single
        # state row instead makes that whole failure mode structurally
        # impossible, since there is nothing else in that table for a
        # different writer's row to be mistaken for.
        current_state = await fetch_calibration_state(symbol)
        is_active = bool(current_state and current_state.get("action") not in (None, "none"))
        if is_active and _action_is_fresh({"ts": current_state.get("computed_at")}):
            return None

        bias_info = await fetch_calibration_bias(
            symbol,
            window_days=max(1, int(round(window_h / 24.0))),
            min_records=min_n,
        )
        if not bias_info or bias_info.get("bias") is None:
            return None

        bias = float(bias_info["bias"])
        if bias <= threshold:
            return None

        row: dict[str, Any] = {
            "symbol": symbol,
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": "bayesian_penalty",
            "bias": round(bias, 4),
            "detail": {
                "n": bias_info.get("n"),
                "win_rate": bias_info.get("win_rate"),
                "avg_pred": bias_info.get("avg_pred"),
                "source": "drift_monitor",
                "window_hours": window_h,
            },
        }
        ok = await _insert_row("nt_eod_actions", row)
        if ok:
            log.info(
                "drift monitor %s: OVERCONFIDENT bias=%.4f → bayesian_penalty (n=%s)",
                symbol, bias, bias_info.get("n"),
            )
            # Current-state write (2026-09-09) — through the single
            # writer (supabase_client.upsert_calibration_state), same as
            # every other verdict-computing pass. The audit row above is
            # unchanged/unaffected.
            await upsert_calibration_state(
                symbol, action="bayesian_penalty", bias=round(bias, 4),
                status="OVERCONFIDENT", n_records=bias_info.get("n"),
                source="drift_monitor", detail=row["detail"],
            )
            return row
        log.warning("drift monitor %s: nt_eod_actions insert failed", symbol)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("drift monitor %s failed: %s", symbol, exc)
    return None
