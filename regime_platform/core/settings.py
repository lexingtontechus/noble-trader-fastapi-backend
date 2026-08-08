"""
Centralized Pydantic Settings Configuration
════════════════════════════════════════════

Replaces all scattered os.getenv() calls with a single, validated,
type-safe Settings class powered by pydantic-settings.

Usage:
    from regime_platform.core.settings import settings

    secret = settings.JWT_SECRET_KEY
    keys = settings.api_keys_set

Environment variables are loaded from a .env file (if present) and
can be overridden by actual environment variables at runtime.
"""

from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings

log = logging.getLogger("regime.settings")


class Settings(BaseSettings):
    """
    Centralized application settings.

    All values can be overridden via environment variables or a .env file.
    Grouped into logical sections for clarity.
    """

    model_config = {
        "env_file": [".env", ".env.local"],
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # ── Server ──────────────────────────────────────────────────────────────
    PORT: int = 8000

    # ── Auth ────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINS: int = 60
    API_KEYS: str = ""
    AUTH_ENABLED: bool = True
    AUTH_USERS: str = ""

    # ── Clerk ───────────────────────────────────────────────────────────────
    CLERK_PUBLISHABLE_KEY: str = ""
    CLERK_SECRET_KEY: str = ""
    CLERK_JWKS_URL: str = ""

    # ── Noble Trader Multi-Tenant Architecture ─────────────────────────────
    # P5 (2026-07-20): noble-trader-fastapi-backend is the SWEEP ORCHESTRATOR.
    # It computes signals and publishes them to Redis. It is NOT a trade
    # executor anymore — execution lives in the hermes agent (noble-trader-agent).
    # The flags below enforce that contract and wire in multi-tenant routing.
    #
    # NOBLE_TRADER_DISABLE_EXECUTION (default True):
    #   When True, all execution codepaths (renko/executor.py, operational/*,
    #   /live/toggle, /reconcile/*, /executor/status, /sse/pnl) raise
    #   ExecutionDisabledError. Set to False only for emergency rollback.
    NOBLE_TRADER_DISABLE_EXECUTION: bool = True

    # NOBLE_TRADER_DISABLE_L2_FEED (default True, audit HIGH #11, 2026-07-23):
    #   When True, the L2FeedManager is NOT started in main.py lifespan.
    #   The L2 imbalance value it computes (regime_platform/services/
    #   statistics/l2_imbalance.py) is NOT consumed by any live path —
    #   the v5 EV engine removed external_signal (compute_ev() no longer
    #   accepts it), and the replacement microstructure source is the
    #   proxy's SSE stream (HIGH #9 wired the agent-side consumer).
    #   Keeping the L2 WebSocket adapters running wastes one WS
    #   connection per symbol + CPU on snapshot processing.
    #
    #   Set to False ONLY for research/simulation harnesses that want
    #   to push L2 snapshots to a downstream consumer (e.g. a future
    #   `/api/ta/{symbol}/indicators?source=l2` endpoint). The
    #   L2FeedManager code is preserved (not deleted) for that path.
    NOBLE_TRADER_DISABLE_L2_FEED: bool = True

    # NOBLE_TRADER_STREAM_SPLIT_ENABLED (default True):
    #   When True, the StreamSplitter background task republishes signals from
    #   `signal.raw.noble_trader` to per-plan streams `signals:signal_scout`
    #   and `signals:precision_pro` based on nt_symbol.plan_ids membership.
    #   The raw stream is preserved — proxy + agent can still subscribe to it.
    NOBLE_TRADER_STREAM_SPLIT_ENABLED: bool = True

    # Plan UUIDs — resolved at startup from nobletradingapp's `plans` table.
    # Used by StreamSplitter and Discord plan-routing to bucket signals.
    # If left empty, plan routing is skipped (signals go to both streams).
    NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID: str = ""
    NOBLE_TRADER_PLAN_PRECISION_PRO_ID: str = ""

    # ── Alpaca (DATA FEED ONLY — execution deprecated) ─────────────────────
    # ALPACA_API_KEY/SECRET are still used as READ-ONLY data credentials by
    # sweep_orchestrator/price_fetcher.py and adapters/l2_adapters.py.
    # They MUST NOT be used for order placement — that path is gated behind
    # NOBLE_TRADER_DISABLE_EXECUTION above.
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", os.getenv("ALPACA_APIKEY", ""))
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", os.getenv("ALPACA_SECRETKEY", ""))
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_LIVE_BASE_URL: str = "https://api.alpaca.markets"
    ALPACA_PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_RATE_LIMIT: int = 200        # Max Alpaca API requests per period
    ALPACA_RATE_PERIOD: int = 60         # Rate limit period in seconds

    # ── Binance ─────────────────────────────────────────────────────────────
    BINANCE_TESTNET: bool = False

    # ── Hyperliquid ──────────────────────────────────────────────────────────
    HL_API_URL: str = "https://api.hyperliquid.xyz"
    HL_WS_URL: str = "wss://api.hyperliquid.xyz/ws"

    # ── TradingView (RapidAPI) ──────────────────────────────────────────────
    # TradingView API via RapidAPI provides OHLCV candle data for crypto,
    # forex, stocks, commodities, and more. Requires a RapidAPI key.
    # Sign up at https://rapidapi.com/ and subscribe to the TradingView API.
    # Plan: Ultra (100k requests/month)
    TRADINGVIEW_API_KEY: str = os.getenv("TRADINGVIEW_API_KEY", "")
    TRADINGVIEW_API_HOST: str = os.getenv("TRADINGVIEW_API_HOST", "tradingview-data1.p.rapidapi.com")
    TRADINGVIEW_BASE_URL: str = os.getenv("TRADINGVIEW_BASE_URL", "https://tradingview-data1.p.rapidapi.com")

    # ── Interactive Brokers ─────────────────────────────────────────────────
    IB_HOST: str = "127.0.0.1"
    IB_PORT: int = 7497
    IB_CLIENT_ID: int = 1

    # ── Discord ─────────────────────────────────────────────────────────────
    DISCORD_WEBHOOK_SIGNALS: str = ""
    DISCORD_WEBHOOK_EXECUTIONS: str = ""
    DISCORD_WEBHOOK_STATUS: str = ""
    # Debug webhook — used by scripts/debug_signal_subscriber.py to forward
    # ALL signals (qualified AND rejected) to a debug Discord channel.
    # Must also set DEBUG_SIGNALS=1 on the orchestrator to publish to the
    # "signal.debug.noble_trader" Redis channel.
    DISCORD_WEBHOOK_DEBUG: str = ""
    # ── Per-asset-class signal webhooks (optional) ───────────────────────
    # If set, trade signals are routed to these channels by asset class
    # instead of all going to DISCORD_WEBHOOK_SIGNALS. This splits signals
    # across channels so traders only see signals for their asset class:
    #   crypto   → #crypto-signals    (BTC, ETH, SOL, etc.)
    #   equities → #equities-signals  (SPY, QQQ, etc.)
    #   metals   → #metals-signals    (Gold, Silver, etc.)
    # Leave empty to use the default DISCORD_WEBHOOK_SIGNALS for all.
    DISCORD_WEBHOOK_CRYPTO: str = ""
    DISCORD_WEBHOOK_EQUITIES: str = ""
    DISCORD_WEBHOOK_METALS: str = ""
    DISCORD_WEBHOOK_FOREX: str = ""
    # ── Per-plan signal webhooks (P5 — multi-tenant routing) ─────────────
    # When set, signals are routed to the plan-specific Discord channel
    # based on nt_symbol.plan_ids membership. This is the canonical
    # 4-channel Discord layout for Noble Trader's multi-tenant model:
    #   1. #signal-scout       ← Signal Scout ($79/mo) subscriber signals
    #   2. #precision-pro      ← Precision Pro ($199/mo) subscriber signals
    #   3. #system-status      ← System health + regime updates (DISCORD_WEBHOOK_STATUS)
    #   4. #trade-executions   ← Hermes agent execution alerts (DISCORD_WEBHOOK_EXECUTIONS)
    # If both plan webhooks are empty, signals fall back to per-asset-class
    # routing, then to DISCORD_WEBHOOK_SIGNALS.
    DISCORD_WEBHOOK_SIGNAL_SCOUT: str = ""
    DISCORD_WEBHOOK_SIGNAL_PRECISION: str = ""

    # ── Telegram ────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # ── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = ""
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""
    
    # ── Signal Aggregator Configuration ───────────────────────────────────────
    EXTERNAL_SIGNAL_WEBHOOK_URL: str = ""  # Webhook URL for external signal forwarding
    
    @property
    def redis_url_effective(self) -> str:
        """Return REDIS_URL — the single source of truth for the Redis connection.

        The legacy UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN fallback has
        been removed deliberately: a missing or misconfigured REDIS_URL must
        surface clearly (no Redis configured) instead of silently connecting to
        a stale Upstash instance. If REDIS_URL is empty, callers that require
        Redis (stream_splitter, redis_pool, signal_aggregator, webhook service)
        will raise/log "no Redis configured" rather than hang on a dead host.

        Set REDIS_URL in the deployment environment (e.g. the new Railway Redis)
        and ensure it is present on every fresh instance — env vars are
        per-instance and are NOT carried over when a Studio is re-created.
        """
        return self.REDIS_URL

    # ── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_COMPUTE: str = "10/minute"
    RATE_LIMIT_WRITE: str = "20/minute"

    # ── Supabase ─────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # ── Supabase Realtime broadcast (Talaria dashboard) ─────────────────────
    # NOBLE_REALTIME_BROADCAST=1 (default) enables fire-and-forget broadcast
    # publishing of qualified signals (realtime:signals) and paper portfolio
    # events (realtime:paper). Set to "0" to disable publishing entirely.
    NOBLE_REALTIME_BROADCAST: str = "1"

    # NOBLE_TRADER_REDIS_PUBLISH_ENABLED (default True):
    #   When False, the sweep skips ALL Redis writes for qualified signals
    #   (PUBLISH + XADD signal.raw.noble_trader, plus the signal.debug
    #   channel) — i.e. the proxy/agent feed is off. The sweep itself,
    #   Supabase persistence, signal-sim capture, and the Supabase Realtime
    #   broadcast all continue unaffected. Use when retiring the proxy /
    #   Redis delivery layer (Talaria replaces the agent).
    NOBLE_TRADER_REDIS_PUBLISH_ENABLED: bool = True

    # ── MCP ─────────────────────────────────────────────────────────────────
    MCP_AUTH_ENABLED: bool = True
    MCP_ISSUER: str = ""

    # ── Computed properties ─────────────────────────────────────────────────

    @property
    def api_keys_set(self) -> set[str]:
        """Split API_KEYS by comma, filter empty strings, return as set."""
        return set(filter(None, self.API_KEYS.split(",")))

    @property
    def auth_users_dict(self) -> dict[str, dict]:
        """Parse AUTH_USERS format "user:pass:role,user2:pass2:role2" into dict."""
        users: dict[str, dict] = {}
        for entry in self.AUTH_USERS.split(","):
            parts = entry.strip().split(":")
            if len(parts) == 3:
                username, password, role = parts
                users[username] = {"password": password, "role": role}
        return users

    @property
    def clerk_auth_enabled(self) -> bool:
        """True if CLERK_SECRET_KEY is configured."""
        return bool(self.CLERK_SECRET_KEY)

    @property
    def discord_configured(self) -> bool:
        """True if any Discord webhook URL is configured."""
        return bool(
            self.DISCORD_WEBHOOK_SIGNALS
            or self.DISCORD_WEBHOOK_EXECUTIONS
            or self.DISCORD_WEBHOOK_STATUS
        )

    @property
    def telegram_configured(self) -> bool:
        """True if Telegram bot token and chat ID are configured."""
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)
    
    @property
    def signal_aggregator_configured(self) -> bool:
        """True if signal aggregator services are configured."""
        return bool(self.REDIS_URL or self.UPSTASH_REDIS_REST_URL)
    
    @property
    def external_webhook_configured(self) -> bool:
        """True if external webhook URL is configured."""
        return bool(self.EXTERNAL_SIGNAL_WEBHOOK_URL)

    # ── Validation ──────────────────────────────────────────────────────────

    def model_post_init(self, __context: object) -> None:
        """Validate configuration and warn about missing settings."""
        # Authentication warnings
        if self.AUTH_ENABLED and not self.JWT_SECRET_KEY:
            log.warning(
                "JWT_SECRET_KEY is not set. Authentication will reject all JWT tokens. "
                "Set JWT_SECRET_KEY env var or set AUTH_ENABLED=false for development."
            )
        
        # Redis configuration warnings
        if not self.REDIS_URL and not self.UPSTASH_REDIS_REST_URL:
            log.warning("No Redis URL configured. Signal aggregator services will be disabled.")
        
        # External webhook warning
        if self.EXTERNAL_SIGNAL_WEBHOOK_URL and not self.REDIS_URL and not self.UPSTASH_REDIS_REST_URL:
            log.warning("External webhook URL configured but no Redis connection. Webhook service will be disabled.")
            
        # Telegram configuration warning
        if self.TELEGRAM_BOT_TOKEN and not self.TELEGRAM_CHAT_ID:
            log.warning("Telegram bot token configured but no chat ID. Telegram signals will be disabled.")
        elif self.TELEGRAM_CHAT_ID and not self.TELEGRAM_BOT_TOKEN:
            log.warning("Telegram chat ID configured but no bot token. Telegram signals will be disabled.")


# ── Singleton ────────────────────────────────────────────────────────────────

settings = Settings()
