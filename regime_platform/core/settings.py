"""
Centralized Pydantic Settings Configuration
════════════════════════════════════════════

Replaces all scattered os.getenv() calls with a single, validated,
type-safe Settings class powered by pydantic-settings.

Usage:
    from regime_platform.core.settings import settings

    secret = settings.JWT_SECRET_KEY
    origins = settings.cors_origins_list
    keys = settings.api_keys_set

Environment variables are loaded from a .env file (if present) and
can be overridden by actual environment variables at runtime.
"""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings

log = logging.getLogger("regime.settings")


class Settings(BaseSettings):
    """
    Centralized application settings.

    All values can be overridden via environment variables or a .env file.
    Grouped into logical sections for clarity.
    """

    model_config = {
        "env_file": ".env",
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

    # ── Alpaca ──────────────────────────────────────────────────────────────
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_BASE_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_LIVE_BASE_URL: str = "https://api.alpaca.markets"
    ALPACA_PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"

    # ── Binance ─────────────────────────────────────────────────────────────
    BINANCE_TESTNET: bool = False

    # ── Interactive Brokers ─────────────────────────────────────────────────
    IB_HOST: str = "127.0.0.1"
    IB_PORT: int = 7497
    IB_CLIENT_ID: int = 1

    # ── Discord ─────────────────────────────────────────────────────────────
    DISCORD_WEBHOOK_SIGNALS: str = ""
    DISCORD_WEBHOOK_EXECUTIONS: str = ""
    DISCORD_WEBHOOK_STATUS: str = ""

    # ── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = ""

    # ── CORS ────────────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"

    # ── Rate Limiting ───────────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_COMPUTE: str = "10/minute"
    RATE_LIMIT_WRITE: str = "20/minute"

    # ── Supabase ─────────────────────────────────────────────────────────────
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # ── MCP ─────────────────────────────────────────────────────────────────
    MCP_AUTH_ENABLED: bool = True
    MCP_ISSUER: str = ""

    # ── Computed properties ─────────────────────────────────────────────────

    @property
    def cors_origins_list(self) -> list[str]:
        """Split CORS_ORIGINS by comma, strip whitespace, filter empty strings."""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

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

    # ── Validation ──────────────────────────────────────────────────────────

    def model_post_init(self, __context: object) -> None:
        """Warn if JWT_SECRET_KEY is empty while auth is enabled."""
        if self.AUTH_ENABLED and not self.JWT_SECRET_KEY:
            log.warning(
                "JWT_SECRET_KEY is not set. Authentication will reject all JWT tokens. "
                "Set JWT_SECRET_KEY env var or set AUTH_ENABLED=false for development."
            )


# ── Singleton ────────────────────────────────────────────────────────────────

settings = Settings()
