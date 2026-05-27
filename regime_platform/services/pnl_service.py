"""
PnlService - Intraday P&L analytics, risk dashboard, CSV export, alert thresholds.

Provides:
  • Intraday P&L time-bucketed from Alpaca portfolio history (5Min/15Min/1Hour/1Day)
  • Live portfolio risk metrics (Sharpe, Sortino, Max DD, VaR, CVaR from equity curve)
  • Historical P&L retrieval (daily equity curve with P&L derivation)
  • CSV export of P&L data (positions + trades + equity curve)
  • P&L alert threshold management (Supabase-persisted with in-memory cache)

Architecture:
  Uses the same credential resolution chain as AlpacaStreamManager:
    1. Supabase credentials table (per-user, encrypted)
    2. Global ALPACA_API_KEY / ALPACA_SECRET_KEY env vars (fallback)

  All Alpaca REST calls go through httpx with proper auth headers.

  Alert thresholds are persisted in Supabase (pnl_alert_thresholds table)
  with an in-memory cache for fast evaluation in the SSE snapshot loop.
  On startup, thresholds are lazy-loaded per user on first access.
  Writes go to Supabase first, then update the cache.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from ..core.settings import settings

log = logging.getLogger("regime.pnl_service")


# ─── Data Models ─────────────────────────────────────────────────────────────

@dataclass
class IntradayBucket:
    """A single time bucket in an intraday P&L series."""
    timestamp: float
    date_str: str
    equity: float
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class RiskDashboardMetrics:
    """Live portfolio risk metrics computed from equity curve."""
    # Return metrics
    total_return: float = 0.0
    total_return_pct: float = 0.0
    annual_return_pct: float = 0.0
    daily_return_avg: float = 0.0

    # Risk metrics
    annual_vol: float = 0.0
    downside_vol: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    # Drawdown metrics
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    current_drawdown: float = 0.0
    current_drawdown_pct: float = 0.0
    avg_drawdown: float = 0.0

    # VaR / CVaR
    var_95: float = 0.0
    var_99: float = 0.0
    cvar_95: float = 0.0
    cvar_99: float = 0.0

    # Win/Loss stats
    win_days: int = 0
    loss_days: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    # Metadata
    n_data_points: int = 0
    period_days: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0
    computed_at: str = ""


@dataclass
class AlertThreshold:
    """A P&L alert threshold configuration."""
    id: str
    user_id: str
    metric: str           # day_pnl, unrealized_pnl, drawdown_pct, var_breach, equity_change_pct
    operator: str         # lt, gt, lte, gte, crosses_below, crosses_above
    value: float
    severity: str = "warning"  # info, warning, critical
    enabled: bool = True
    cooldown_minutes: int = 15  # Minimum time between repeated alerts
    created_at: float = 0.0
    last_triggered: float = 0.0


@dataclass
class AlertThresholdCreate:
    """Request model for creating an alert threshold."""
    metric: str
    operator: str
    value: float
    severity: str = "warning"
    enabled: bool = True
    cooldown_minutes: int = 15


# ─── Credential Resolution (reuses pattern from alpaca_stream.py) ────────────

async def _resolve_credentials(user_id: str, org_id: str = None) -> Optional[tuple[str, str, str]]:
    """Resolve Alpaca credentials for a user (optionally org-scoped). Returns (api_key, secret_key, base_url) or None."""
    from .alpaca_stream import resolve_alpaca_credentials
    return await resolve_alpaca_credentials(user_id, org_id=org_id)


# ─── Alpaca REST Helpers ─────────────────────────────────────────────────────

async def _alpaca_get(user_id: str, path: str, params: dict = None, org_id: str = None) -> Optional[dict]:
    """Make an authenticated, rate-throttled GET request to the Alpaca REST API."""
    import httpx
    from ..middleware.alpaca_throttle import get_alpaca_throttle

    creds = await _resolve_credentials(user_id, org_id=org_id)
    if not creds:
        return None

    api_key, secret_key, base_url = creds
    url = f"{base_url}{path}"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }

    # Acquire throttle token before the API call
    throttle = get_alpaca_throttle()
    acquired = await throttle.acquire(timeout=15.0)
    if not acquired:
        log.warning(f"Alpaca rate throttle: could not acquire token for GET {path}")
        return None

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers, params=params or {})
        if resp.status_code == 200:
            return resp.json()
        log.debug(f"Alpaca GET {path} returned {resp.status_code}: {resp.text[:200]}")
        return None


# ─── Supabase Helpers ────────────────────────────────────────────────────────

def _supabase_headers() -> dict[str, str]:
    """Build headers for Supabase PostgREST API calls."""
    return {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _supabase_alerts_url() -> str:
    """Build the Supabase URL for the alert thresholds table."""
    return f"{settings.SUPABASE_URL}/rest/v1/pnl_alert_thresholds"


def _supabase_configured() -> bool:
    """Check if Supabase is configured for persistence."""
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


def _row_to_threshold(row: dict) -> AlertThreshold:
    """Convert a Supabase row dict to an AlertThreshold dataclass."""
    created_at = row.get("created_at", "")
    last_triggered = row.get("last_triggered")

    # Parse ISO timestamp to epoch float
    created_at_ts = 0.0
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            created_at_ts = dt.timestamp()
        except (ValueError, TypeError):
            created_at_ts = time.time()

    last_triggered_ts = 0.0
    if last_triggered:
        try:
            dt = datetime.fromisoformat(last_triggered.replace("Z", "+00:00"))
            last_triggered_ts = dt.timestamp()
        except (ValueError, TypeError):
            last_triggered_ts = 0.0

    return AlertThreshold(
        id=row.get("id", str(uuid.uuid4())),
        user_id=row.get("user_id", ""),
        metric=row.get("metric", ""),
        operator=row.get("operator", ""),
        value=float(row.get("value", 0)),
        severity=row.get("severity", "warning"),
        enabled=bool(row.get("enabled", True)),
        cooldown_minutes=int(row.get("cooldown_minutes", 15)),
        created_at=created_at_ts,
        last_triggered=last_triggered_ts,
    )


# ─── Discord Helper ─────────────────────────────────────────────────────────

async def _push_pnl_alert_discord(alert: dict) -> None:
    """
    Push a triggered P&L alert to Discord via the STATUS webhook.
    Non-blocking - failures are logged but never crash.
    """
    try:
        from notifications.discord import discord_notifier, AlertLevel
        severity_map = {
            "info": AlertLevel.INFO,
            "warning": AlertLevel.WARNING,
            "critical": AlertLevel.CRITICAL,
        }
        level = severity_map.get(alert.get("severity", "warning"), AlertLevel.WARNING)

        await discord_notifier.send_status(
            message=alert.get("message", "P&L Alert triggered"),
            level=level,
            data={
                "Metric": alert.get("metric", ""),
                "Condition": f"{alert.get('operator', '')} {alert.get('threshold_value', '')}",
                "Current Value": f"{alert.get('current_value', 0):.2f}",
                "Severity": alert.get("severity", "warning"),
            },
        )
    except Exception as e:
        log.debug(f"Discord P&L alert push failed: {e}")


async def _push_pnl_alert_telegram(alert: dict) -> None:
    """
    Push a triggered P&L alert to Telegram via the Bot API.
    Non-blocking - failures are logged but never crash.
    """
    try:
        from notifications.telegram import telegram_notifier
        await telegram_notifier.send_pnl_alert(
            metric=alert.get("metric", ""),
            operator=alert.get("operator", ""),
            threshold_value=float(alert.get("threshold_value", 0)),
            current_value=float(alert.get("current_value", 0)),
            severity=alert.get("severity", "warning"),
            message=alert.get("message", ""),
        )
    except Exception as e:
        log.debug(f"Telegram P&L alert push failed: {e}")


# ─── PnlService ──────────────────────────────────────────────────────────────

class PnlService:
    """
    Service for intraday P&L analytics, risk dashboard metrics,
    CSV export, and P&L alert threshold management.

    Alert thresholds are persisted in Supabase (pnl_alert_thresholds table)
    with an in-memory cache for fast SSE evaluation. On startup, thresholds
    are lazy-loaded per user on first access. If Supabase is not configured,
    falls back to in-memory only (pre-restart behavior).
    """

    def __init__(self):
        # In-memory alert cache: user_id -> list[AlertThreshold]
        # Populated from Supabase on first access per user, or used as
        # standalone storage if Supabase is not configured.
        self._alerts: dict[str, list[AlertThreshold]] = {}

        # Track which users have been loaded from Supabase
        self._alerts_loaded: set[str] = set()

        # Cache for risk metrics: user_id -> (timestamp, RiskDashboardMetrics)
        self._risk_cache: dict[str, tuple[float, RiskDashboardMetrics]] = {}
        self._risk_cache_ttl = 30.0  # seconds

        # Previous metric values per user for true cross detection:
        # user_id -> { metric_name: float }
        self._prev_values: dict[str, dict[str, float]] = {}

    # ─── Intraday P&L ────────────────────────────────────────────────────────

    async def get_intraday_pnl(
        self,
        user_id: str,
        timeframe: str = "15Min",
        period: str = "1D",
        org_id: str = None,
    ) -> list[IntradayBucket]:
        """
        Fetch intraday P&L from Alpaca portfolio history with fine-grained timeframes.

        Timeframes: 5Min, 15Min, 1Hour, 1Day
        Periods: 1D, 1W, 1M, 3M, 6M, 1A, all

        Returns a list of IntradayBucket objects with equity and P&L per bucket.
        """
        # Map frontend-friendly timeframe to Alpaca format
        tf_map = {
            "5m": "5Min",
            "15m": "15Min",
            "1h": "1Hour",
            "1d": "1Day",
            "5Min": "5Min",
            "15Min": "15Min",
            "1Hour": "1Hour",
            "1Day": "1Day",
        }
        alpaca_tf = tf_map.get(timeframe, "15Min")

        # Map period to Alpaca format
        period_map = {
            "1D": "1D",
            "1W": "1W",
            "1M": "1M",
            "3M": "3M",
            "6M": "6M",
            "1A": "1A",
            "all": "all",
        }
        alpaca_period = period_map.get(period, "1D")

        data = await _alpaca_get(
            user_id,
            "/v2/account/portfolio/history",
            params={"period": alpaca_period, "timeframe": alpaca_tf},
            org_id=org_id,
        )

        if not data or not data.get("timestamp") or not data.get("equity"):
            return []

        timestamps = data["timestamp"]
        equities = data["equity"]
        profit_losses = data.get("profit_loss", [])

        if not timestamps:
            return []

        start_equity = equities[0] if equities else 0
        buckets: list[IntradayBucket] = []

        for i, ts in enumerate(timestamps):
            eq = float(equities[i]) if i < len(equities) else 0
            pnl = float(profit_losses[i]) if i < len(profit_losses) else (eq - start_equity)
            pnl_pct = ((eq - start_equity) / start_equity * 100) if start_equity > 0 else 0

            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            # Format date string based on timeframe
            if alpaca_tf in ("5Min", "15Min"):
                date_str = dt.strftime("%H:%M")
            elif alpaca_tf == "1Hour":
                date_str = dt.strftime("%H:%M")
            else:
                date_str = dt.strftime("%b %d")

            buckets.append(IntradayBucket(
                timestamp=ts,
                date_str=date_str,
                equity=eq,
                pnl=pnl,
                pnl_pct=pnl_pct,
            ))

        return buckets

    # ─── Historical P&L (daily) ──────────────────────────────────────────────

    async def get_historical_pnl(
        self,
        user_id: str,
        period: str = "1M",
        org_id: str = None,
    ) -> dict:
        """
        Fetch daily equity curve with P&L derivation.

        Returns {
            "timestamps": [...],
            "equity": [...],
            "pnl": [...],
            "pnl_pct": [...],
            "cumulative_pnl": [...],
            "drawdown": [...],
            "drawdown_pct": [...],
            "dates": [...],
            "start_equity": float,
            "end_equity": float,
        }
        """
        period_map = {
            "1W": "1W",
            "1M": "1M",
            "3M": "3M",
            "6M": "6M",
            "1A": "1A",
            "all": "all",
        }
        alpaca_period = period_map.get(period, "1M")

        data = await _alpaca_get(
            user_id,
            "/v2/account/portfolio/history",
            params={"period": alpaca_period, "timeframe": "1Day"},
            org_id=org_id,
        )

        if not data or not data.get("timestamp") or not data.get("equity"):
            return {
                "timestamps": [],
                "equity": [],
                "pnl": [],
                "pnl_pct": [],
                "cumulative_pnl": [],
                "drawdown": [],
                "drawdown_pct": [],
                "dates": [],
                "start_equity": 0,
                "end_equity": 0,
            }

        timestamps = data["timestamp"]
        equities = [float(e) for e in data["equity"]]
        profit_losses = [float(pl) if pl is not None else 0 for pl in data.get("profit_loss", [])]

        start_equity = equities[0] if equities else 0
        end_equity = equities[-1] if equities else 0

        # Compute cumulative P&L and drawdown
        running_max = 0.0
        cumulative_pnl = []
        drawdown = []
        drawdown_pct = []
        dates = []
        daily_pnl = []
        daily_pnl_pct = []

        for i, eq in enumerate(equities):
            if i < len(profit_losses):
                daily_pl = profit_losses[i]
            else:
                daily_pl = eq - (equities[i - 1] if i > 0 else start_equity)

            daily_pnl.append(daily_pl)
            daily_pnl_pct.append((daily_pl / (equities[i - 1] if i > 0 else start_equity) * 100) if (equities[i - 1] if i > 0 else start_equity) > 0 else 0)

            cum_pl = eq - start_equity
            cumulative_pnl.append(cum_pl)

            running_max = max(running_max, eq)
            dd = eq - running_max
            dd_pct = (dd / running_max * 100) if running_max > 0 else 0
            drawdown.append(dd)
            drawdown_pct.append(dd_pct)

            dt = datetime.fromtimestamp(timestamps[i], tz=timezone.utc)
            dates.append(dt.strftime("%Y-%m-%d"))

        return {
            "timestamps": timestamps,
            "equity": equities,
            "pnl": daily_pnl,
            "pnl_pct": daily_pnl_pct,
            "cumulative_pnl": cumulative_pnl,
            "drawdown": drawdown,
            "drawdown_pct": drawdown_pct,
            "dates": dates,
            "start_equity": start_equity,
            "end_equity": end_equity,
        }

    # ─── Risk Dashboard ──────────────────────────────────────────────────────

    async def get_risk_dashboard(
        self,
        user_id: str,
        period: str = "1M",
        org_id: str = None,
    ) -> RiskDashboardMetrics:
        """
        Compute live portfolio risk metrics from the equity curve.

        Includes: Sharpe, Sortino, Max DD, VaR, CVaR, win rate, profit factor,
        max consecutive wins/losses, and current drawdown.
        """
        # Check cache
        cached = self._risk_cache.get(user_id)
        if cached and (time.time() - cached[0]) < self._risk_cache_ttl:
            return cached[1]

        hist = await self.get_historical_pnl(user_id, period, org_id=org_id)

        equities = hist.get("equity", [])
        daily_pnl = hist.get("pnl", [])

        if len(daily_pnl) < 2:
            # Not enough data - return zeros
            return RiskDashboardMetrics(computed_at=datetime.now(timezone.utc).isoformat())

        pnl_array = np.array(daily_pnl, dtype=float)
        equity_array = np.array(equities, dtype=float)
        start_eq = equity_array[0] if len(equity_array) > 0 else 1
        end_eq = equity_array[-1] if len(equity_array) > 0 else 1

        # Return metrics
        total_return = end_eq - start_eq
        total_return_pct = (total_return / start_eq * 100) if start_eq > 0 else 0
        n_days = len(daily_pnl)
        annual_factor = 252 / max(n_days, 1)
        annual_return_pct = total_return_pct * annual_factor

        # Daily returns
        returns = np.diff(np.log(equity_array)) if len(equity_array) > 1 else np.array([0.0])

        # Volatility
        annual_vol = float(np.std(returns, ddof=1) * np.sqrt(252)) if len(returns) > 1 else 0.0

        # Downside deviation (only negative returns)
        neg_returns = returns[returns < 0]
        downside_vol = float(np.std(neg_returns, ddof=1) * np.sqrt(252)) if len(neg_returns) > 1 else 0.0

        # Sharpe ratio (annualized)
        daily_return_avg = float(np.mean(returns)) if len(returns) > 0 else 0.0
        sharpe = (daily_return_avg * 252) / annual_vol if annual_vol > 0 else 0.0

        # Sortino ratio
        sortino = (daily_return_avg * 252) / downside_vol if downside_vol > 0 else 0.0

        # Drawdown calculation
        running_max = np.maximum.accumulate(equity_array)
        drawdowns = equity_array - running_max
        drawdown_pcts = drawdowns / running_max * 100

        max_dd = float(np.min(drawdowns))
        max_dd_pct = float(np.min(drawdown_pcts))
        current_dd = float(drawdowns[-1]) if len(drawdowns) > 0 else 0
        current_dd_pct = float(drawdown_pcts[-1]) if len(drawdown_pcts) > 0 else 0
        avg_dd = float(np.mean(drawdowns[drawdowns < 0])) if np.any(drawdowns < 0) else 0

        # Calmar ratio
        calmar = annual_return_pct / abs(max_dd_pct) if abs(max_dd_pct) > 0.01 else 0.0

        # VaR and CVaR (historical)
        if len(pnl_array) >= 10:
            var_95 = float(-np.percentile(pnl_array, 5))
            var_99 = float(-np.percentile(pnl_array, 1))
            cvar_95 = float(-np.mean(pnl_array[pnl_array <= np.percentile(pnl_array, 5)]))
            cvar_99 = float(-np.mean(pnl_array[pnl_array <= np.percentile(pnl_array, 1)]))
        else:
            var_95 = var_99 = cvar_95 = cvar_99 = 0.0

        # Win/Loss stats
        win_days = int(np.sum(pnl_array > 0))
        loss_days = int(np.sum(pnl_array < 0))
        win_rate = win_days / n_days if n_days > 0 else 0
        avg_win = float(np.mean(pnl_array[pnl_array > 0])) if win_days > 0 else 0
        avg_loss = float(np.mean(np.abs(pnl_array[pnl_array < 0]))) if loss_days > 0 else 0
        profit_factor = (avg_win * win_days) / (avg_loss * loss_days) if loss_days > 0 and avg_loss > 0 else 0

        # Max consecutive wins/losses
        max_consec_wins = 0
        max_consec_losses = 0
        current_wins = 0
        current_losses = 0
        for pl in pnl_array:
            if pl > 0:
                current_wins += 1
                current_losses = 0
                max_consec_wins = max(max_consec_wins, current_wins)
            elif pl < 0:
                current_losses += 1
                current_wins = 0
                max_consec_losses = max(max_consec_losses, current_losses)
            else:
                current_wins = 0
                current_losses = 0

        metrics = RiskDashboardMetrics(
            total_return=round(total_return, 2),
            total_return_pct=round(total_return_pct, 4),
            annual_return_pct=round(annual_return_pct, 4),
            daily_return_avg=round(daily_return_avg, 6),
            annual_vol=round(annual_vol, 4),
            downside_vol=round(downside_vol, 4),
            sharpe_ratio=round(sharpe, 4),
            sortino_ratio=round(sortino, 4),
            calmar_ratio=round(calmar, 4),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd_pct, 4),
            current_drawdown=round(current_dd, 2),
            current_drawdown_pct=round(current_dd_pct, 4),
            avg_drawdown=round(avg_dd, 2),
            var_95=round(var_95, 2),
            var_99=round(var_99, 2),
            cvar_95=round(cvar_95, 2),
            cvar_99=round(cvar_99, 2),
            win_days=win_days,
            loss_days=loss_days,
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            profit_factor=round(profit_factor, 4),
            max_consecutive_wins=max_consec_wins,
            max_consecutive_losses=max_consec_losses,
            n_data_points=n_days,
            period_days=n_days,
            start_equity=round(start_eq, 2),
            end_equity=round(end_eq, 2),
            computed_at=datetime.now(timezone.utc).isoformat(),
        )

        # Cache result
        self._risk_cache[user_id] = (time.time(), metrics)

        return metrics

    # ─── CSV Export ───────────────────────────────────────────────────────────

    async def export_pnl_csv(
        self,
        user_id: str,
        period: str = "1M",
        sections: list[str] = None,
        org_id: str = None,
    ) -> str:
        """
        Export P&L data as CSV.

        Sections: "positions", "trades", "equity_curve", "risk_metrics", "all"
        Default: ["all"]
        """
        if sections is None:
            sections = ["all"]

        if "all" in sections:
            sections = ["positions", "trades", "equity_curve", "risk_metrics"]

        output = io.StringIO()
        writer = csv.writer(output)

        # ── Positions section ──
        if "positions" in sections:
            positions_data = await _alpaca_get(user_id, "/v2/positions", org_id=org_id)
            if positions_data:
                writer.writerow(["=== POSITIONS ==="])
                if isinstance(positions_data, list) and positions_data:
                    headers = list(positions_data[0].keys())
                    writer.writerow(headers)
                    for pos in positions_data:
                        writer.writerow([pos.get(h, "") for h in headers])
                writer.writerow([])

        # ── Trades section ──
        if "trades" in sections:
            activities = await _alpaca_get(
                user_id,
                "/v2/account/activities/FILL",
                params={"direction": "desc", "page_size": "500"},
                org_id=org_id,
            )
            if activities:
                writer.writerow(["=== TRADES (FILL activities) ==="])
                if isinstance(activities, list) and activities:
                    headers = list(activities[0].keys())
                    writer.writerow(headers)
                    for act in activities:
                        writer.writerow([act.get(h, "") for h in headers])
                writer.writerow([])

        # ── Equity Curve section ──
        if "equity_curve" in sections:
            hist = await self.get_historical_pnl(user_id, period, org_id=org_id)
            if hist.get("timestamps"):
                writer.writerow(["=== EQUITY CURVE ==="])
                writer.writerow(["date", "equity", "daily_pnl", "daily_pnl_pct", "cumulative_pnl", "drawdown", "drawdown_pct"])
                for i in range(len(hist["timestamps"])):
                    writer.writerow([
                        hist["dates"][i] if i < len(hist["dates"]) else "",
                        round(hist["equity"][i], 2) if i < len(hist["equity"]) else "",
                        round(hist["pnl"][i], 2) if i < len(hist["pnl"]) else "",
                        round(hist["pnl_pct"][i], 4) if i < len(hist["pnl_pct"]) else "",
                        round(hist["cumulative_pnl"][i], 2) if i < len(hist["cumulative_pnl"]) else "",
                        round(hist["drawdown"][i], 2) if i < len(hist["drawdown"]) else "",
                        round(hist["drawdown_pct"][i], 4) if i < len(hist["drawdown_pct"]) else "",
                    ])
                writer.writerow([])

        # ── Risk Metrics section ──
        if "risk_metrics" in sections:
            risk = await self.get_risk_dashboard(user_id, period, org_id=org_id)
            writer.writerow(["=== RISK METRICS ==="])
            writer.writerow(["metric", "value"])
            for attr in [
                "total_return", "total_return_pct", "annual_return_pct",
                "annual_vol", "downside_vol", "sharpe_ratio", "sortino_ratio", "calmar_ratio",
                "max_drawdown", "max_drawdown_pct", "current_drawdown", "current_drawdown_pct",
                "var_95", "var_99", "cvar_95", "cvar_99",
                "win_days", "loss_days", "win_rate", "profit_factor",
                "max_consecutive_wins", "max_consecutive_losses",
                "start_equity", "end_equity", "computed_at",
            ]:
                writer.writerow([attr, getattr(risk, attr, "")])
            writer.writerow([])

        return output.getvalue()

    # ─── Alert Thresholds (Supabase-persisted + in-memory cache) ─────────────

    async def _ensure_alerts_loaded(self, user_id: str) -> None:
        """
        Lazy-load alert thresholds from Supabase on first access per user.
        If Supabase is not configured, the in-memory cache stands alone.
        """
        if user_id in self._alerts_loaded:
            return

        self._alerts_loaded.add(user_id)

        if not _supabase_configured():
            return  # Use in-memory only

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _supabase_alerts_url(),
                    headers=_supabase_headers(),
                    params={
                        "user_id": f"eq.{user_id}",
                        "order": "created_at.asc",
                    },
                )
                if resp.status_code == 200:
                    rows = resp.json()
                    self._alerts[user_id] = [_row_to_threshold(r) for r in rows]
                    log.info(f"Loaded {len(rows)} alert thresholds from Supabase for user {user_id}")
                else:
                    log.debug(f"Supabase alert load failed for user {user_id}: {resp.status_code}")
        except Exception as e:
            log.warning(f"Failed to load alert thresholds from Supabase for user {user_id}: {e}")

    async def list_alert_thresholds(self, user_id: str) -> list[AlertThreshold]:
        """Get all alert thresholds for a user (from cache, lazy-loaded from Supabase)."""
        await self._ensure_alerts_loaded(user_id)
        return self._alerts.get(user_id, [])

    async def create_alert_threshold(
        self, user_id: str, req: AlertThresholdCreate
    ) -> AlertThreshold:
        """Create a new P&L alert threshold for a user (persisted to Supabase)."""
        # Validate metric
        valid_metrics = {"day_pnl", "unrealized_pnl", "drawdown_pct", "var_breach", "equity_change_pct"}
        if req.metric not in valid_metrics:
            raise ValueError(f"Invalid metric '{req.metric}'. Must be one of: {valid_metrics}")

        # Validate operator
        valid_operators = {"lt", "gt", "lte", "gte", "crosses_below", "crosses_above"}
        if req.operator not in valid_operators:
            raise ValueError(f"Invalid operator '{req.operator}'. Must be one of: {valid_operators}")

        # Validate severity
        valid_severities = {"info", "warning", "critical"}
        if req.severity not in valid_severities:
            raise ValueError(f"Invalid severity '{req.severity}'. Must be one of: {valid_severities}")

        threshold_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        threshold = AlertThreshold(
            id=threshold_id,
            user_id=user_id,
            metric=req.metric,
            operator=req.operator,
            value=req.value,
            severity=req.severity,
            enabled=req.enabled,
            cooldown_minutes=req.cooldown_minutes,
            created_at=time.time(),
            last_triggered=0,
        )

        # Persist to Supabase first
        if _supabase_configured():
            try:
                import httpx
                row = {
                    "id": threshold_id,
                    "user_id": user_id,
                    "metric": req.metric,
                    "operator": req.operator,
                    "value": req.value,
                    "severity": req.severity,
                    "enabled": req.enabled,
                    "cooldown_minutes": req.cooldown_minutes,
                    "created_at": now_iso,
                    "last_triggered": None,
                }
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        _supabase_alerts_url(),
                        headers=_supabase_headers(),
                        json=row,
                    )
                    if resp.status_code in (200, 201):
                        log.info(f"Persisted P&L alert threshold {threshold_id} to Supabase for user {user_id}")
                    else:
                        log.warning(f"Supabase persist failed for alert {threshold_id}: {resp.status_code} {resp.text[:200]}")
            except Exception as e:
                log.warning(f"Supabase persist error for alert {threshold_id}: {e}")

        # Update in-memory cache
        if user_id not in self._alerts:
            self._alerts[user_id] = []
        self._alerts[user_id].append(threshold)
        self._alerts_loaded.add(user_id)

        log.info(f"Created P&L alert threshold for user {user_id}: {req.metric} {req.operator} {req.value} ({req.severity})")
        return threshold

    async def delete_alert_threshold(self, user_id: str, threshold_id: str) -> bool:
        """Delete an alert threshold (from Supabase and cache). Returns True if found."""
        # Delete from Supabase first
        if _supabase_configured():
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.delete(
                        _supabase_alerts_url(),
                        headers=_supabase_headers(),
                        params={
                            "id": f"eq.{threshold_id}",
                            "user_id": f"eq.{user_id}",
                        },
                    )
                    if resp.status_code in (200, 204):
                        log.info(f"Deleted P&L alert threshold {threshold_id} from Supabase for user {user_id}")
                    else:
                        log.warning(f"Supabase delete failed for alert {threshold_id}: {resp.status_code}")
            except Exception as e:
                log.warning(f"Supabase delete error for alert {threshold_id}: {e}")

        # Remove from in-memory cache
        thresholds = self._alerts.get(user_id, [])
        for i, t in enumerate(thresholds):
            if t.id == threshold_id:
                thresholds.pop(i)
                log.info(f"Deleted P&L alert threshold {threshold_id} for user {user_id}")
                return True
        return False

    async def _update_last_triggered(self, threshold_id: str, user_id: str) -> None:
        """Update last_triggered timestamp in Supabase for cooldown tracking."""
        if not _supabase_configured():
            return

        try:
            import httpx
            now_iso = datetime.now(timezone.utc).isoformat()
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.patch(
                    _supabase_alerts_url(),
                    headers=_supabase_headers(),
                    params={
                        "id": f"eq.{threshold_id}",
                        "user_id": f"eq.{user_id}",
                    },
                    json={"last_triggered": now_iso},
                )
                if resp.status_code not in (200, 204):
                    log.debug(f"Supabase last_triggered update failed for {threshold_id}: {resp.status_code}")
        except Exception as e:
            log.debug(f"Supabase last_triggered update error for {threshold_id}: {e}")

    async def check_alerts(
        self,
        user_id: str,
        current_values: dict[str, float],
    ) -> list[dict]:
        """
        Check all alert thresholds against current values.
        Returns a list of triggered alerts.

        current_values: {
            "day_pnl": float,
            "unrealized_pnl": float,
            "drawdown_pct": float,
            "var_breach": float,   # e.g., current VaR as % of equity
            "equity_change_pct": float,
        }

        Cross detection:
          "crosses_below" triggers only when the previous value was >= threshold
          AND the current value is < threshold (a genuine downward crossing).
          "crosses_above" triggers only when the previous value was <= threshold
          AND the current value is > threshold (a genuine upward crossing).
          If no previous value exists, cross operators fall through (no trigger).
        """
        await self._ensure_alerts_loaded(user_id)
        thresholds = self._alerts.get(user_id, [])
        triggered = []
        now = time.time()

        # Load previous values for this user
        prev = self._prev_values.get(user_id, {})

        for t in thresholds:
            if not t.enabled:
                continue

            # Check cooldown
            if t.last_triggered and (now - t.last_triggered) < (t.cooldown_minutes * 60):
                continue

            current_val = current_values.get(t.metric)
            if current_val is None:
                continue

            # Get previous value for cross detection
            prev_val = prev.get(t.metric)

            # Evaluate condition
            is_triggered = False
            if t.operator == "lt":
                is_triggered = current_val < t.value
            elif t.operator == "gt":
                is_triggered = current_val > t.value
            elif t.operator == "lte":
                is_triggered = current_val <= t.value
            elif t.operator == "gte":
                is_triggered = current_val >= t.value
            elif t.operator == "crosses_below":
                # True cross: previous was >= threshold, current is < threshold
                if prev_val is not None:
                    is_triggered = prev_val >= t.value and current_val < t.value
                # If no previous value, don't trigger (first observation can't "cross")
            elif t.operator == "crosses_above":
                # True cross: previous was <= threshold, current is > threshold
                if prev_val is not None:
                    is_triggered = prev_val <= t.value and current_val > t.value

            if is_triggered:
                t.last_triggered = now
                alert_dict = {
                    "id": t.id,
                    "metric": t.metric,
                    "operator": t.operator,
                    "threshold_value": t.value,
                    "current_value": current_val,
                    "severity": t.severity,
                    "message": f"P&L Alert: {t.metric} ({current_val:.2f}) {t.operator} {t.value}",
                    "triggered_at": now,
                }
                triggered.append(alert_dict)

                # Update last_triggered in Supabase (fire-and-forget)
                await self._update_last_triggered(t.id, user_id)

                # Push to Discord (fire-and-forget)
                await _push_pnl_alert_discord(alert_dict)

                # Push to Telegram (fire-and-forget)
                await _push_pnl_alert_telegram(alert_dict)

        # Store current values as previous for next evaluation
        self._prev_values[user_id] = dict(current_values)

        return triggered

    # ─── SSE Alert Broadcasting ───────────────────────────────────────────────

    async def evaluate_and_broadcast(
        self,
        user_id: str,
        account: dict,
        positions: list[dict],
    ) -> list[dict]:
        """
        Evaluate alert thresholds against current portfolio state.
        Returns list of triggered alerts for SSE broadcast.

        This is called from the AlpacaStreamManager snapshot loop (every 5s).
        """
        equity = float(account.get("equity", 0))
        last_equity = float(account.get("last_equity", equity))
        day_pnl = equity - last_equity

        # Compute unrealized P&L
        unrealized_pnl = sum(float(p.get("unrealized_pl", 0)) for p in positions)

        # Compute current drawdown
        # Simple approach: use day P&L relative to last equity
        drawdown_pct = (day_pnl / last_equity * 100) if last_equity > 0 else 0

        # Equity change pct
        equity_change_pct = drawdown_pct

        # VaR breach: compare actual daily loss against historical VaR 95%
        # If the cached risk metrics are available, use the real VaR_95 value;
        # otherwise fall back to a simple heuristic (2 * |drawdown_pct|).
        cached = self._risk_cache.get(user_id)
        if cached and (time.time() - cached[0]) < self._risk_cache_ttl * 6:  # Use a wider window for VaR ref
            risk_metrics = cached[1]
            # VaR is expressed as a positive dollar amount; convert to % of equity
            if equity > 0 and risk_metrics.var_95 > 0:
                var_pct = (risk_metrics.var_95 / equity) * 100
                # Breach magnitude: how many times the loss exceeds VaR
                # Positive = breach (loss > VaR), zero = within VaR
                loss_pct = abs(drawdown_pct) if drawdown_pct < 0 else 0
                var_breach = max(0, loss_pct - var_pct)
            else:
                var_breach = abs(drawdown_pct)
        else:
            # Fallback: simplified metric when no risk cache available
            var_breach = abs(drawdown_pct)

        current_values = {
            "day_pnl": day_pnl,
            "unrealized_pnl": unrealized_pnl,
            "drawdown_pct": drawdown_pct,
            "var_breach": var_breach,
            "equity_change_pct": equity_change_pct,
        }

        return await self.check_alerts(user_id, current_values)


# ─── Singleton ────────────────────────────────────────────────────────────────

pnl_service = PnlService()
