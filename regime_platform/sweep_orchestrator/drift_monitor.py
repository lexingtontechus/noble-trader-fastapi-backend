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
    if not settings.EOD_DRIFT_MONITOR_ENABLED:
        return None

    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            _insert_row,
            fetch_calibration_bias,
        )
        from regime_platform.sweep_orchestrator.calibration_enforcement import (
            PENALTY_ACTIONS,
            _action_is_fresh,
            fetch_latest_eod_action,
        )

        window_h = float(getattr(settings, "EOD_DRIFT_WINDOW_HOURS", 48) or 48)
        min_n = int(getattr(settings, "EOD_DRIFT_MIN_SAMPLE", 20) or 20)
        threshold = float(getattr(settings, "EOD_DRIFT_THRESHOLD", 0.15) or 0.15)

        # A fresh penalty already exists → the scheduled passes own this
        # symbol; do not churn actions every 5 minutes.
        latest = await fetch_latest_eod_action(symbol)
        if (
            latest
            and latest.get("action") in PENALTY_ACTIONS
            and _action_is_fresh(latest)
        ):
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
            return row
        log.warning("drift monitor %s: nt_eod_actions insert failed", symbol)
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        log.warning("drift monitor %s failed: %s", symbol, exc)
    return None
