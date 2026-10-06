"""
Discord Webhook Notifier - sends rich embed notifications to Discord channels.

Design:
  - Three webhooks: SIGNALS, EXECUTIONS, STATUS (one per Discord channel)
  - Rich Discord embeds with color coding by alert type and severity
  - Rate-limited: respects Discord's 5 requests / 2 seconds per webhook
  - Non-blocking: all sends are fire-and-forget; failures are logged, never crash
  - Graceful degradation: if webhook URLs are not configured, silently skips

Rate limiting:
  Discord allows ~5 webhook messages per 2 seconds per channel.
  We use a simple sliding-window counter per webhook URL.

Environment variables:
  DISCORD_WEBHOOK_SIGNALS     -> #trade-signals channel
  DISCORD_WEBHOOK_EXECUTIONS  -> #trade-executions channel
  DISCORD_WEBHOOK_STATUS      -> #system-status channel
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from regime_platform.core.resilience import retry_with_backoff
from regime_platform.core.settings import settings

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────


class AlertChannel(str, Enum):
    SIGNALS = "signals"
    EXECUTIONS = "executions"
    STATUS = "status"


class AlertLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Discord embed color palette (decimal integers for Discord API)
EMBED_COLORS = {
    AlertLevel.INFO: 0x3498DB,       # Blue
    AlertLevel.SUCCESS: 0x2ECC71,    # Green
    AlertLevel.WARNING: 0xF39C12,    # Orange/Amber
    AlertLevel.ERROR: 0xE74C3C,      # Red
    AlertLevel.CRITICAL: 0x9B59B6,   # Purple
}

# Direction colors for signal embeds
DIRECTION_COLORS = {
    "LONG": 0x2ECC71,    # Green
    "BUY": 0x2ECC71,     # Green
    "SHORT": 0xE74C3C,   # Red
    "SELL": 0xE74C3C,    # Red
}

# Alert type emojis
ALERT_EMOJI = {
    AlertChannel.SIGNALS: "📊",
    AlertChannel.EXECUTIONS: "💰",
    AlertChannel.STATUS: "🔧",
}

LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.SUCCESS: "✅",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.ERROR: "🚨",
    AlertLevel.CRITICAL: "🔴",
}


# ── Full-value price formatting (2026-08-05) ────────────────────────────
# Discord signal embeds MUST deliver the SAME full value that is stored in
# Supabase nt_sweep_result (entry_price / stop_loss / take_profit). The
# previous hardcoded ``f"${price:.2f}"`` truncated 3-5 decimal prices to
# 2dp — e.g. AUDUSD SL 0.697 rendered as "$0.70" (identical to entry) and
# XAUUSD TP 4032.16 rendered as "$432.16" when the leading digit was lost
# in the payload. Format at 6dp then strip trailing zeros (keeping at
# least 2dp for readability), so 4030.325 → "$4030.325", 0.697 → "$0.697",
# 163.7225 → "$163.7225", 4036.32 → "$4036.32".

def _fmt_price(value: Optional[float]) -> str:
    """Format a price preserving its full stored precision.

    Returns a currency string WITHOUT the ``$`` prefix (callers add it) —
    e.g. ``"4030.325"`` / ``"0.697"`` / ``"4036.32"``. ``None`` → ``""``.
    """
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    # 6dp keeps the full value; rstrip trailing zeros, keep >= 2dp.
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if "." not in s:
        return f"{v:.2f}"
    frac = s.split(".", 1)[1]
    if len(frac) < 2:
        return f"{v:.2f}"
    return s


def _fmt_price_usd(value: Optional[float]) -> str:
    """Full-value price with a ``$`` prefix (``None`` → empty string)."""
    s = _fmt_price(value)
    return f"${s}" if s else ""


# ── Rate Limiter ─────────────────────────────────────────────────────────


@dataclass
class RateLimiter:
    """Simple sliding-window rate limiter per webhook URL.

    Discord allows ~5 messages per 2 seconds per webhook.
    We track timestamps of recent sends and block if over limit.
    """
    max_per_window: int = 5
    window_seconds: float = 2.0
    _timestamps: Dict[str, List[float]] = field(default_factory=dict)

    def can_send(self, url: str) -> bool:
        """Check if we can send to this URL without exceeding rate limit."""
        now = time.monotonic()
        timestamps = self._timestamps.get(url, [])

        # Remove timestamps outside the window
        cutoff = now - self.window_seconds
        timestamps = [ts for ts in timestamps if ts > cutoff]
        self._timestamps[url] = timestamps

        return len(timestamps) < self.max_per_window

    def record_send(self, url: str) -> None:
        """Record that a send occurred for rate limiting."""
        now = time.monotonic()
        if url not in self._timestamps:
            self._timestamps[url] = []
        self._timestamps[url].append(now)


# ── Discord Notifier ─────────────────────────────────────────────────────


class DiscordNotifier:
    """
    Sends rich embed notifications to Discord via webhooks.

    All methods are async and non-blocking. Failures are logged but never
    raise exceptions or crash the calling code.

    Usage::

        notifier = DiscordNotifier()

        # Auto-loaded from env vars
        await notifier.send_signal(symbol="SPY", direction="LONG", ...)

        # Check if configured
        if notifier.is_configured:
            ...
    """

    def __init__(self):
        # Load webhook URLs from centralized settings
        self._webhooks: Dict[AlertChannel, Optional[str]] = {
            AlertChannel.SIGNALS: settings.DISCORD_WEBHOOK_SIGNALS or None,
            AlertChannel.EXECUTIONS: settings.DISCORD_WEBHOOK_EXECUTIONS or None,
            AlertChannel.STATUS: settings.DISCORD_WEBHOOK_STATUS or None,
        }
        # ── Per-asset-class signal webhooks (optional) ───────────────────
        # If set, signals are routed to these channels by asset class
        # instead of all going to #trade-signals. Falls back to the
        # default SIGNALS webhook if not set.
        #   DISCORD_WEBHOOK_CRYPTO    → crypto signals (BTC, ETH, SOL, etc.)
        #   DISCORD_WEBHOOK_EQUITIES  → stock/ETF signals (SPY, QQQ, etc.)
        #   DISCORD_WEBHOOK_METALS    → metals signals (Gold, Silver, etc.)
        self._venue_webhooks: Dict[str, Optional[str]] = {
            "crypto": getattr(settings, "DISCORD_WEBHOOK_CRYPTO", "") or None,
            "equities": getattr(settings, "DISCORD_WEBHOOK_EQUITIES", "") or None,
            "stocks": getattr(settings, "DISCORD_WEBHOOK_EQUITIES", "") or None,  # alias
            "metals": getattr(settings, "DISCORD_WEBHOOK_METALS", "") or None,
            "forex": getattr(settings, "DISCORD_WEBHOOK_FOREX", "") or None,
        }
        # ── Per-plan signal webhooks (P5 — multi-tenant routing) ─────────
        # When set, signals are routed to the plan-specific Discord channel
        # based on nt_symbol.plan_ids membership. Falls back to per-asset-class
        # routing, then to the default SIGNALS webhook.
        #   DISCORD_WEBHOOK_SIGNAL_SCOUT   → #signal-scout (Signal Scout $79/mo)
        #   DISCORD_WEBHOOK_SIGNAL_PRECISION → #precision-pro (Precision Pro $199/mo)
        self._plan_webhooks: Dict[str, Optional[str]] = {
            # Keyed by plan_id (UUID string) → webhook URL
            # Populated lazily from settings.NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID
            # and settings.NOBLE_TRADER_PLAN_PRECISION_PRO_ID at first lookup.
        }
        self._plan_webhooks_initialized = False
        self._rate_limiter = RateLimiter()
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """Check if at least one webhook is configured."""
        return any(self._webhooks.values())

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client. Call on shutdown."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _resolve_signal_webhook(
        self,
        asset_class: Optional[str] = None,
        plan_ids: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Resolve which webhook URL to use for a trade signal.

        Priority (P5):
          1. Per-plan webhook (if configured for any plan in `plan_ids`)
          2. Per-asset-class webhook (if configured for this asset class)
          3. Default SIGNALS webhook (fallback)

        Asset class routing lets you split signals across channels:
          crypto   → #crypto-signals
          equities → #equities-signals
          metals   → #metals-signals

        Per-plan routing (P5) lets you split signals by subscription tier:
          Signal Scout subscribers   → #signal-scout
          Precision Pro subscribers  → #precision-pro

        When a symbol belongs to BOTH plans (backfilled in P2 migration),
        the FIRST configured plan webhook wins. This is intentional — it
        lets operators choose a primary channel by ordering the plan
        webhook env vars in their preferred priority.
        """
        # ── 1. Plan routing (P5) ──────────────────────────────────────
        if plan_ids:
            self._init_plan_webhooks_if_needed()
            for plan_id, webhook_url in self._plan_webhooks.items():
                if plan_id in plan_ids and webhook_url:
                    return webhook_url

        # ── 2. Asset-class routing (existing) ────────────────────────
        if asset_class:
            ac = asset_class.lower().strip()
            # Try exact match first (crypto, equities, metals, forex)
            if ac in self._venue_webhooks and self._venue_webhooks[ac]:
                return self._venue_webhooks[ac]
            # Handle common variants
            if ac in ("stock", "stocks", "etf", "equity"):
                if self._venue_webhooks.get("equities"):
                    return self._venue_webhooks["equities"]
            elif ac in ("metal", "metals", "commodity", "commodities"):
                if self._venue_webhooks.get("metals"):
                    return self._venue_webhooks["metals"]

        # ── 3. Default fallback ───────────────────────────────────────
        return self._webhooks.get(AlertChannel.SIGNALS)

    def _init_plan_webhooks_if_needed(self) -> None:
        """Lazily populate the plan_id → webhook URL map from settings.

        Done lazily so test fixtures can set env vars BEFORE the map is
        built — same pattern as the proxy's settings module.
        """
        if self._plan_webhooks_initialized:
            return

        scout_id = settings.NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID
        precision_id = settings.NOBLE_TRADER_PLAN_PRECISION_PRO_ID

        if scout_id:
            self._plan_webhooks[scout_id] = (
                settings.DISCORD_WEBHOOK_SIGNAL_SCOUT or None
            )
        if precision_id:
            self._plan_webhooks[precision_id] = (
                settings.DISCORD_WEBHOOK_SIGNAL_PRECISION or None
            )

        self._plan_webhooks_initialized = True
        logger.debug(
            "Discord plan webhooks initialized: %d plan(s) mapped",
            len(self._plan_webhooks),
        )

    # ── Core send method ──────────────────────────────────────────────

    async def _send_webhook(
        self,
        channel: AlertChannel,
        embed: Dict[str, Any],
        content: Optional[str] = None,
        url_override: Optional[str] = None,
    ) -> bool:
        """
        Send a Discord webhook message with a rich embed.

        Parameters
        ----------
        channel : AlertChannel
            The channel to send to (used for rate-limit labeling and
            as the default URL source).
        embed : dict
            Discord embed payload.
        content : str, optional
            Text content to include before the embed.
        url_override : str, optional
            If provided, use this webhook URL instead of the channel's
            default. Used for per-asset-class routing (e.g., crypto
            signals go to the crypto webhook, not the default SIGNALS
            webhook).

        Returns True if sent successfully, False otherwise.
        Never raises - all errors are caught and logged.
        """
        url = url_override or self._webhooks.get(channel)
        if not url:
            return False

        # Rate limit check
        if not self._rate_limiter.can_send(url):
            label = url_override or channel.value
            logger.warning(f"Discord rate limit reached for {label}, dropping message")
            return False

        payload: Dict[str, Any] = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]

        try:
            client = self._get_client()

            async def _do_post():
                return await client.post(url, json=payload)

            try:
                response = await retry_with_backoff(
                    _do_post,
                    max_retries=2,
                    base_delay=1.0,
                    retryable_exceptions=(
                        httpx.ConnectError,
                        httpx.TimeoutException,
                        ConnectionError,
                        TimeoutError,
                        OSError,
                    ),
                )
            except (httpx.ConnectError, httpx.TimeoutException, ConnectionError, TimeoutError, OSError) as e:
                logger.error(f"Discord send error for {channel.value} after retries: {e}")
                return False

            if response.status_code == 204 or response.status_code == 200:
                self._rate_limiter.record_send(url)
                return True
            elif response.status_code == 429:
                # Rate limited by Discord - extract retry-after
                retry_after = response.headers.get("Retry-After", "2")
                logger.warning(
                    f"Discord 429 rate limited on {channel.value}, "
                    f"retry after {retry_after}s"
                )
                return False
            else:
                logger.error(
                    f"Discord webhook failed: {response.status_code} "
                    f"for {channel.value}: {response.text[:200]}"
                )
                return False

        except Exception as e:
            logger.error(f"Discord send error for {channel.value}: {e}")
            return False

    # ── Build Discord embed ───────────────────────────────────────────

    @staticmethod
    def _build_embed(
        title: str,
        description: str,
        color: int = 0x3498DB,
        fields: Optional[List[Dict[str, str]]] = None,
        footer: Optional[str] = None,
        emoji: str = "",
    ) -> Dict[str, Any]:
        """Build a Discord embed payload."""
        embed: Dict[str, Any] = {
            "title": f"{emoji} {title}" if emoji else title,
            "description": description,
            "color": color,
            "timestamp": _utc_now_iso(),
        }

        if fields:
            embed["fields"] = [
                {
                    "name": f["name"],
                    "value": str(f["value"]),
                    "inline": f.get("inline", True),
                }
                for f in fields
            ]

        if footer:
            embed["footer"] = {"text": footer}

        return embed

    # ── Public API: Signal Alerts ─────────────────────────────────────

    async def send_signal(
        self,
        symbol: str,
        direction: str,
        pattern: str,
        price: float,
        confidence: Optional[float] = None,
        velocity: Optional[float] = None,
        brick_size: Optional[float] = None,
        brick_count: Optional[int] = None,
        regime: Optional[str] = None,
        kelly_fraction: Optional[float] = None,
        sharpe_ratio: Optional[float] = None,
        win_rate: Optional[float] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        sl_bricks: Optional[int] = None,
        tp_bricks: Optional[int] = None,
        prev_regime: Optional[str] = None,
        regime_shift: Optional[str] = None,
        asset_class: Optional[str] = None,
        venue: Optional[str] = None,
        plan_ids: Optional[list[str]] = None,
        rank: Optional[float] = None,
        state: Optional[str] = None,
        ev_r: Optional[float] = None,
        expected_resolution_hours: Optional[float] = None,
        expected_resolution_days: Optional[float] = None,
        resolution_confidence: Optional[str] = None,
        soft_deadline_hours: Optional[float] = None,
        hard_deadline_hours: Optional[float] = None,
        p_clef_wait: Optional[float] = None,
    ) -> bool:
        """
        Send a trading signal alert to #trade-signals (or per-plan /
        per-asset-class channel if configured).

        This is called when the Renko pattern detector produces a signal
        that passes all filter gates. The embed is color-coded by direction
        (green for LONG, red for SHORT).

        Routing (P5 priority):
          1. If `plan_ids` is provided AND a per-plan webhook is configured
             for any of those plan_ids (DISCORD_WEBHOOK_SIGNAL_SCOUT or
             DISCORD_WEBHOOK_SIGNAL_PRECISION), route there.
          2. Else if DISCORD_WEBHOOK_CRYPTO/EQUITIES/METALS is configured,
             route to the per-asset-class channel.
          3. Else fall back to the default #trade-signals channel
             (DISCORD_WEBHOOK_SIGNALS).

        Parameters
        ----------
        symbol : str
            Trading symbol (e.g., "SPY")
        direction : str
            Signal direction ("LONG", "SHORT", "BUY", "SELL", "HOLD")
        pattern : str
            Pattern type (bull_run, bear_run, reversal, double_top, etc.)
        price : float
            Signal price (entry price)
        confidence : float, optional
            Signal confidence 0.0-1.0
        velocity : float, optional
            Brick velocity (bricks/minute)
        brick_size : float, optional
            Current brick size in dollars
        brick_count : int, optional
            Current brick count in the pipeline
        regime : str, optional
            Current HMM regime label
        kelly_fraction : float, optional
            Kelly criterion fraction for position sizing (aggression)
        sl_price : float, optional
            Stop loss price
        tp_price : float, optional
            Take profit price
        sl_bricks : int, optional
            Stop loss in bricks
        tp_bricks : int, optional
            Take profit in bricks
        prev_regime : str, optional
            Previous HMM regime label
        regime_shift : str, optional
            Regime shift direction (None, "bull_bear", "bear_bull")
        sharpe_ratio : float, optional
            Sharpe ratio for strategy performance
        win_rate : float, optional
            Win rate (0.0-1.0) for strategy
        plan_ids : list[str], optional (P5)
            List of plan_id UUIDs the symbol belongs to. Used to route
            the signal to a per-plan Discord channel. If None or empty,
            plan routing is skipped and the next-priority routing applies.
        """
        dir_emoji = "🟢" if direction.upper() in ("LONG", "BUY") else "🔴"
        color = DIRECTION_COLORS.get(direction.upper(), 0x3498DB)

        # Price levels formatting — FULL VALUE (2026-08-05). The previous
        # ``f"${price:.2f}"`` truncated entry/stop/tp to 2dp. Use the
        # full-precision formatter so the embed delivers the SAME values
        # stored in nt_sweep_result (e.g. 4030.325 → "$4030.325",
        # 0.697 → "$0.697").
        price_fields = []
        price_fields.append({"name": "Entry Price", "value": _fmt_price_usd(price), "inline": True})

        if sl_price is not None:
            price_fields.append({"name": "Stop Loss", "value": _fmt_price_usd(sl_price), "inline": True})
        if tp_price is not None:
            price_fields.append({"name": "Take Profit", "value": _fmt_price_usd(tp_price), "inline": True})
        
        # Risk levels in bricks
        risk_fields = []
        if sl_bricks is not None and tp_bricks is not None:
            risk_fields.append({
                "name": "Risk Levels", 
                "value": f"SL: {sl_bricks} bricks | TP: {tp_bricks} bricks", 
                "inline": True
            })
        
        # Signal aggression
        aggression_fields = []
        if kelly_fraction is not None:
            aggression_emoji = "🔥" if kelly_fraction > 0.25 else "⚡" if kelly_fraction > 0.15 else "🎯"
            aggression_fields.append({
                "name": "Aggression", 
                "value": f"{aggression_emoji} Kelly: {kelly_fraction:.2%}", 
                "inline": True
            })
        
        # Performance metrics
        perf_fields = []
        if sharpe_ratio is not None:
            sharpe_emoji = "🚀" if sharpe_ratio > 2.0 else "📈" if sharpe_ratio > 1.0 else "📊"
            perf_fields.append({
                "name": "Sharpe Ratio", 
                "value": f"{sharpe_emoji} {sharpe_ratio:.2f}", 
                "inline": True
            })
        if win_rate is not None:
            win_rate_pct = win_rate * 100
            win_rate_emoji = "🏆" if win_rate_pct > 60 else "✅" if win_rate_pct > 50 else "⚠️"
            perf_fields.append({
                "name": "Win Rate", 
                "value": f"{win_rate_emoji} {win_rate_pct:.1f}%", 
                "inline": True
            })
        
        # Regime info
        regime_fields = []
        if regime is not None:
            regime_color = "🟢" if "bull" in regime.lower() else "🔴" if "bear" in regime.lower() else "🟡"
            regime_fields.append({"name": "Current Regime", "value": f"{regime_color} {regime}", "inline": True})
        if prev_regime is not None and regime is not None:
            prev_regime_color = "🟢" if "bull" in prev_regime.lower() else "🔴" if "bear" in prev_regime.lower() else "🟡"
            regime_fields.append({"name": "Previous Regime", "value": f"{prev_regime_color} {prev_regime}", "inline": True})
        if regime_shift is not None:
            shift_emoji = {"bull_bear": "📉", "bear_bull": "📈"}.get(regime_shift, "🔄")
            regime_fields.append({"name": "Regime Shift", "value": f"{shift_emoji} {regime_shift.replace('_', ' → ')}", "inline": True})

        # RANK board (20260928) — opportunity priority, not win probability
        rank_fields = []
        if rank is not None:
            rank_fields.append({
                "name": "RANK",
                "value": f"**{float(rank):.2f}**",
                "inline": True,
            })
        if state:
            state_up = str(state).upper()
            state_emoji = {
                "FRESH": "🟢", "WARM": "🟡", "STALE": "🟠", "DEEP": "⚪", "PRE": "🔵",
            }.get(state_up, "•")
            rank_fields.append({
                "name": "STATE",
                "value": f"{state_emoji} {state_up}",
                "inline": True,
            })
        if ev_r is not None:
            rank_fields.append({
                "name": "EV",
                "value": f"{float(ev_r):+.2f}R",
                "inline": True,
            })
        # Dynamic deadline tier (20261005_REACHABILITY_REDESIGN)
        # Descriptive, not predictive — always shows confidence alongside estimate.
        if expected_resolution_hours is not None:
            conf_str = f" ({resolution_confidence})" if resolution_confidence else ""
            rank_fields.append({
                "name": "Est. Resolution",
                "value": f"~{expected_resolution_hours:.0f}h{conf_str}",
                "inline": True,
            })
        if soft_deadline_hours is not None and hard_deadline_hours is not None:
            rank_fields.append({
                "name": "Deadline Range",
                "value": f"{soft_deadline_hours:.0f}h – {hard_deadline_hours:.0f}h",
                "inline": True,
            })
        if p_clef_wait is not None:
            wait_pct = p_clef_wait * 100
            wait_emoji = "⏸️" if p_clef_wait > 0.5 else "▶️"
            rank_fields.append({
                "name": "Model Confidence",
                "value": f"{wait_emoji} {wait_pct:.0f}% wait",
                "inline": True,
            })

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Direction", "value": f"{dir_emoji} {direction}", "inline": True},
            {"name": "Pattern", "value": pattern, "inline": True},
        ] + rank_fields + price_fields + risk_fields + aggression_fields + perf_fields + regime_fields

        # ── Venue + asset class info ──────────────────────────────────
        # Shows the exchange/venue and asset class separately for clarity.
        # Format: "Exchange" field = venue (e.g., "Coinbase", "Hyperliquid")
        #         "Asset Class" field = crypto/forex/stocks/commodities
        # Previously these were combined as "₿ Crypto / TradingView" which
        # was misleading — TradingView is the data source, not the exchange.
        if venue is not None:
            fields.append({
                "name": "Exchange",
                "value": venue,
                "inline": True,
            })
        if asset_class is not None:
            ac_emoji = {
                "crypto": "₿",
                "equities": "📈",
                "stocks": "📈",
                "metals": "🥇",
                "forex": "💱",
                "commodities": "🥇",
            }.get(asset_class.lower(), "📊")
            fields.append({
                "name": "Asset Class",
                "value": f"{ac_emoji} {asset_class.title()}",
                "inline": True,
            })

        if confidence is not None:
            fields.append({
                "name": "Confidence",
                "value": f"{confidence:.0%}",
                "inline": True,
            })
        if velocity is not None:
            fields.append({
                "name": "Velocity",
                "value": f"{velocity:.2f} bricks/min",
                "inline": True,
            })
        if brick_size is not None:
            fields.append({
                "name": "Brick Size",
                "value": f"${brick_size:.2f}",
                "inline": True,
            })
        if regime is not None:
            fields.append({
                "name": "Regime",
                "value": regime,
                "inline": True,
            })
        # NOTE: Kelly fraction is already shown in the "Aggression" field
        # above (built when kelly_fraction is not None). The previous
        # implementation also appended a duplicate "Kelly %" field here,
        # producing two Kelly entries in the embed — removed.

        # ── Build title with exchange/venue ────────────────────────────
        # Format: "Signal: BTCUSD SELL (Coinbase)"
        # The exchange comes from the venue param (already extracted from
        # the symbol config by _derive_venue() in the orchestrator).
        # For TradingView symbols, venue is the exchange (e.g., "Coinbase",
        # "OANDA"). For HL/Alpaca, venue is the platform name.
        if venue:
            title = f"Signal: {symbol} {direction} ({venue})"
        else:
            title = f"Signal: {symbol} {direction}"

        embed = self._build_embed(
            title=title,
            description=f"**{pattern}** pattern detected at {_fmt_price_usd(price)}",
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Renko HFT Pipeline",
            emoji=ALERT_EMOJI[AlertChannel.SIGNALS],
        )

        # ── Route to per-plan or per-asset-class webhook if configured ─
        # P5 priority:
        #   1. Per-plan webhook (DISCORD_WEBHOOK_SIGNAL_SCOUT / _PRECISION)
        #      — selected when `plan_ids` contains a configured plan UUID.
        #   2. Per-asset-class webhook (DISCORD_WEBHOOK_CRYPTO/EQUITIES/METALS)
        #   3. Default SIGNALS webhook (fallback)
        webhook_url = self._resolve_signal_webhook(asset_class, plan_ids=plan_ids)
        return await self._send_webhook(
            AlertChannel.SIGNALS,
            embed,
            url_override=webhook_url,
        )

    # ── Public API: Execution Alerts ──────────────────────────────────

    async def send_execution(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_id: Optional[str] = None,
        order_type: str = "market",
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        pnl_bricks: Optional[float] = None,
        pnl_dollars: Optional[float] = None,
        status: str = "filled",
    ) -> bool:
        """
        Send a trade execution alert to #trade-executions.

        Called when an Alpaca order is submitted, filled, or when SL/TP
        is hit. The embed is color-coded by PnL (green for profit, red
        for loss, blue for entry).

        Parameters
        ----------
        symbol : str
            Trading symbol
        side : str
            "buy" or "sell"
        qty : float
            Number of shares
        price : float, optional
            Execution price
        order_id : str, optional
            Alpaca order ID
        order_type : str
            "market", "limit", "bracket"
        sl_price : float, optional
            Stop-loss price
        tp_price : float, optional
            Take-profit price
        pnl_bricks : float, optional
            P&L in brick units (for close alerts)
        pnl_dollars : float, optional
            P&L in dollars (for close alerts)
        status : str
            "filled", "submitted", "rejected", "cancelled"
        """
        # Determine color based on context
        if pnl_bricks is not None:
            color = 0x2ECC71 if pnl_bricks >= 0 else 0xE74C3C
        elif status == "rejected":
            color = 0xE74C3C
        else:
            color = DIRECTION_COLORS.get(side.upper(), 0x3498DB)

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Side", "value": side.upper(), "inline": True},
            {"name": "Qty", "value": str(int(qty)), "inline": True},
            {"name": "Status", "value": status.upper(), "inline": True},
        ]

        if price is not None:
            fields.append({
                "name": "Price", "value": _fmt_price_usd(price), "inline": True,
            })
        if order_id is not None:
            fields.append({
                "name": "Order ID", "value": f"`{order_id[:16]}…`", "inline": True,
            })
        if sl_price is not None:
            fields.append({
                "name": "Stop-Loss", "value": _fmt_price_usd(sl_price), "inline": True,
            })
        if tp_price is not None:
            fields.append({
                "name": "Take-Profit", "value": _fmt_price_usd(tp_price), "inline": True,
            })
        if pnl_bricks is not None:
            pnl_emoji = "📈" if pnl_bricks >= 0 else "📉"
            fields.append({
                "name": f"{pnl_emoji} P&L",
                "value": f"{pnl_bricks:+.2f} bricks",
                "inline": True,
            })
        if pnl_dollars is not None:
            fields.append({
                "name": "P&L $",
                "value": f"${pnl_dollars:+.2f}",
                "inline": True,
            })

        # Build title and description based on context
        if pnl_bricks is not None:
            title = f"Close: {symbol} {side.upper()}"
            result = "PROFIT" if pnl_bricks >= 0 else "LOSS"
            description = f"Position closed with **{result}**: {pnl_bricks:+.2f} bricks (${pnl_dollars:+.2f})" if pnl_dollars else f"Position closed: {pnl_bricks:+.2f} bricks"
        elif status == "rejected":
            title = f"Rejected: {symbol} {side.upper()}"
            description = f"Order for {int(qty)} shares of {symbol} was rejected"
        else:
            title = f"Entry: {symbol} {side.upper()}"
            description = f"Bracket order: {int(qty)} shares at {'${price:.2f}' if price else 'market'}"

        embed = self._build_embed(
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Alpaca Executor",
            emoji=ALERT_EMOJI[AlertChannel.EXECUTIONS],
        )

        return await self._send_webhook(AlertChannel.EXECUTIONS, embed)

    # ── Public API: System Status ─────────────────────────────────────

    async def send_status(
        self,
        message: str,
        symbol: Optional[str] = None,
        level: AlertLevel = AlertLevel.INFO,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a system status alert to #system-status.

        Used for warmup completions, pipeline resets, errors, health
        checks, and any non-trade operational notifications.

        Parameters
        ----------
        message : str
            Human-readable status message
        symbol : str, optional
            Associated symbol (if any)
        level : AlertLevel
            Severity level (determines embed color)
        data : dict, optional
            Additional structured data to show as fields
        """
        color = EMBED_COLORS.get(level, 0x3498DB)
        emoji = LEVEL_EMOJI.get(level, "ℹ️")

        fields = []
        if symbol:
            fields.append({"name": "Symbol", "value": symbol, "inline": True})

        fields.append({"name": "Level", "value": f"{emoji} {level.value}", "inline": True})

        if data:
            for key, value in list(data.items())[:8]:  # Max 8 fields for readability
                fields.append({
                    "name": key.replace("_", " ").title(),
                    "value": str(value),
                    "inline": True,
                })

        embed = self._build_embed(
            title=f"System: {level.value.upper()}",
            description=message,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Pipeline Status",
            emoji=ALERT_EMOJI[AlertChannel.STATUS],
        )

        return await self._send_webhook(AlertChannel.STATUS, embed)

    # ── Public API: Risk Alert ────────────────────────────────────────

    async def send_risk_alert(
        self,
        symbol: str,
        risk_type: str,
        message: str,
        level: AlertLevel = AlertLevel.WARNING,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a risk management alert.

        Routes to #system-status with elevated color coding.
        Used for daily loss limits, consecutive losses, cooldown triggers.

        Parameters
        ----------
        symbol : str
            Trading symbol
        risk_type : str
            "daily_loss_limit", "consecutive_losses", "cooldown", "regime_mismatch"
        message : str
            Human-readable risk alert message
        level : AlertLevel
            Severity (WARNING or ERROR typically)
        data : dict, optional
            Additional risk metrics
        """
        color = EMBED_COLORS.get(level, 0xF39C12)
        emoji = "⚠️" if level == AlertLevel.WARNING else "🚨"

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Risk Type", "value": risk_type, "inline": True},
        ]

        if data:
            for key, value in list(data.items())[:6]:
                fields.append({
                    "name": key.replace("_", " ").title(),
                    "value": str(value),
                    "inline": True,
                })

        embed = self._build_embed(
            title=f"Risk Alert: {risk_type}",
            description=message,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Risk Manager",
            emoji=emoji,
        )

        return await self._send_webhook(AlertChannel.STATUS, embed)

    # ── Convenience: Send to all channels ─────────────────────────────

    async def send_trade_lifecycle(
        self,
        symbol: str,
        direction: str,
        pattern: str,
        price: float,
        qty: float,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        confidence: Optional[float] = None,
        order_id: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> Dict[str, bool]:
        """
        Send both a signal alert AND an execution alert in one call.

        This is the primary method to call when a filtered signal
        leads to an actual trade execution. It sends:
          1. Signal embed to #trade-signals
          2. Execution embed to #trade-executions

        Returns dict with {channel_name: success_bool}
        """
        results = {}

        # 1. Signal alert
        results["signals"] = await self.send_signal(
            symbol=symbol,
            direction=direction,
            pattern=pattern,
            price=price,
            confidence=confidence,
            regime=regime,
        )

        # 2. Execution alert
        results["executions"] = await self.send_execution(
            symbol=symbol,
            side="buy" if direction.upper() in ("LONG", "BUY") else "sell",
            qty=qty,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=order_id,
            status="submitted",
        )

        return results


# ── Helper ───────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Singleton ────────────────────────────────────────────────────────────

# Global singleton instance - import and use across the backend
discord_notifier = DiscordNotifier()
