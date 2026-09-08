"""
Expiry-rate threshold alert — priority #3, worklog
20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.

A signal that neither hits TP nor SL within the 48h deadline (``EXPIRY_BARS``,
``eod_resolver.py``) is deliberately excluded from win/loss/PnL — a timeout
is neither a win nor a loss, and counting it as a loss would systematically
understate the model's p_win (see ``eod_resolver.py``'s own docstring). But
a persistently high expiry rate is itself a real signal-quality problem —
the strategy is picking brackets/deadlines that mostly time out before
resolving either way — and it needs its own visibility instead of silently
vanishing from the win/loss stats.

Reads ``v_symbol_signal_scorecard_7d`` (migration 138/140) once per day
(called from ``orchestrator._run_eod_lookback``, right after the resolver
batch that produces fresh numbers) and fires a Discord risk alert
(``notifications.discord.send_risk_alert``, reusing the existing
#system-status plumbing) for any symbol whose 7-day ``expired_pct`` exceeds
``config/feature_flags.yaml``'s ``expiry_rate_alert.threshold_pct``, gated
by ``nt_expiry_rate_alerts`` (migration 141) so a persisting condition
doesn't re-alert every day.

Best-effort throughout: every failure is caught and logged, never raised
into the eod-lookback tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from regime_platform.core.feature_flags import get_expiry_rate_alert_config

logger = logging.getLogger("regime.sweep.expiry_rate_alert")


def _is_cooldown_active(last_alert: dict[str, Any] | None, cooldown_hours: float) -> bool:
    """True if ``last_alert`` exists and is younger than ``cooldown_hours``."""
    if not last_alert or not last_alert.get("last_alert_at"):
        return False
    try:
        ts_raw = str(last_alert["last_alert_at"]).replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        return age_h < float(cooldown_hours)
    except (ValueError, TypeError):
        return False


async def check_expiry_rate_alerts() -> dict[str, Any]:
    """Check the 7-day scorecard for symbols over the expiry-rate
    threshold and fire (cooldown-gated) Discord risk alerts.

    Returns a summary dict ``{enabled, checked, alerted}`` for the
    caller's log line. Never raises.
    """
    cfg = get_expiry_rate_alert_config()
    if not cfg["enabled"]:
        return {"enabled": False, "checked": 0, "alerted": []}

    threshold_pct = cfg["threshold_pct"]
    min_resolved = cfg["min_resolved"]
    cooldown_hours = cfg["cooldown_hours"]

    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_expiry_rate_alert_state,
        fetch_symbol_scorecard_7d,
        record_expiry_rate_alert,
    )

    rows = await fetch_symbol_scorecard_7d()
    checked = 0
    alerted: list[str] = []

    for row in rows:
        symbol = row.get("symbol")
        resolved = row.get("resolved") or 0
        expired_pct_raw = row.get("expired_pct")
        if not symbol or resolved < min_resolved or expired_pct_raw is None:
            continue
        checked += 1
        try:
            expired_pct = float(expired_pct_raw)
        except (TypeError, ValueError):
            continue
        if expired_pct < threshold_pct:
            continue

        last_alert = await fetch_expiry_rate_alert_state(symbol)
        if _is_cooldown_active(last_alert, cooldown_hours):
            continue

        try:
            from notifications.discord import discord_notifier

            await discord_notifier.send_risk_alert(
                symbol=symbol,
                risk_type="expiry_rate",
                message=(
                    f"{symbol}: {expired_pct:.1f}% of the last 7 days' resolved "
                    f"qualified signals expired (neither TP nor SL hit within "
                    f"the 48h deadline), out of {resolved} resolved — exceeds "
                    f"the {threshold_pct:.0f}% threshold."
                ),
                data={
                    "expired_pct": f"{expired_pct:.1f}%",
                    "resolved_7d": resolved,
                    "threshold_pct": f"{threshold_pct:.0f}%",
                },
            )
        except Exception as exc:
            logger.warning(
                "[expiry_rate_alert] %s: Discord send failed: %s", symbol, exc,
            )
            continue

        alerted.append(symbol)
        logger.warning(
            "[expiry_rate_alert] %s: %.1f%% expired (n=%d resolved, "
            "threshold %.0f%%) — alert sent",
            symbol, expired_pct, resolved, threshold_pct,
        )
        await record_expiry_rate_alert(symbol, expired_pct, resolved)

    return {"enabled": True, "checked": checked, "alerted": alerted}
