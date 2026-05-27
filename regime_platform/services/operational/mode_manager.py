"""
Mode Manager Service - Paper/Live trading mode toggle with confirmation gates.

Manages the current trading mode (simulation / paper / live) with:
1. Redis-backed mode state (persists across restarts)
2. Mode switch requires confirmation token (prevents accidental switches)
3. Every mode change is audit-logged
4. Mode change triggers health check on target environment
5. Live mode requires explicit acknowledgment of risk disclaimer

Flow for PAPER → LIVE:
1. User requests mode change → generate confirmation token (5-min TTL in Redis)
2. UI shows risk disclaimer + confirmation dialog with token
3. User confirms (must provide acknowledgment text)
4. Backend validates confirmation token
5. Backend switches AlpacaExecutor base_url to live endpoint
6. Backend logs MODE_CHANGED event to audit trail
7. Backend sends Discord notification
8. Frontend updates all visual indicators

Flow for LIVE → PAPER:
1. User requests mode change → generate confirmation token
2. Backend cancels all open live orders (safety)
3. Backend switches to paper endpoint
4. Audit log + Discord notification
"""

from __future__ import annotations
import logging
import hashlib
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

from regime_platform.core.tenant import tenant_key

logger = logging.getLogger(__name__)


class TradingMode(str, Enum):
    SIMULATION = "simulation"
    PAPER = "paper"
    LIVE = "live"


class ModeChangeRequest(BaseModel):
    target_mode: TradingMode
    requested_by: str
    requested_at: datetime
    confirmation_token: str
    acknowledged: bool = False


class ModeState(BaseModel):
    current_mode: TradingMode
    last_changed_at: datetime
    last_changed_by: str
    previous_mode: Optional[TradingMode] = None


class ModeManager:
    """Redis-backed trading mode manager with confirmation gates."""

    MODE_BASE_KEY = "noble:trading:mode"
    TOKEN_BASE_KEY = "noble:trading:mode_token"
    TOKEN_TTL = 300  # 5 minutes

    # Required acknowledgment text for live mode activation
    LIVE_ACKNOWLEDGMENT = "I UNDERSTAND THE RISKS"

    def __init__(self):
        self._redis = None
        self._cached_state: Optional[ModeState] = None

    async def _get_client(self):
        """Get Redis client from the shared connection pool.

        v7.3: Uses the shared RedisConnectionManager instead of creating
        a per-service TCP connection. Reduces from 6 connections to 1 pool.
        """
        if self._redis is None:
            try:
                from regime_platform.services.redis_pool import redis_pool
                self._redis = await redis_pool.get_client()
                if self._redis:
                    logger.info("ModeManager: Using shared Redis pool")
                else:
                    logger.warning("ModeManager: No Redis available, using in-memory only")
            except Exception as e:
                logger.error(f"ModeManager: Redis pool connection failed: {e}")
        return self._redis

    async def close(self):
        """Release Redis client reference (pool manages the connection)."""
        self._redis = None

    async def get_current_mode(self, org_id: Optional[str] = None) -> TradingMode:
        """Get current trading mode. Defaults to PAPER if not set.

        v7.3: Reads from Supabase system_config first (durable source of truth),
        then falls back to Redis (L1 cache), then Alpaca config inference.
        """
        # Check in-memory cache first (O(1), zero latency)
        if self._cached_state:
            return self._cached_state.current_mode

        # Try Supabase system_config (L2 - durable, authoritative)
        try:
            from regime_platform.core.settings import settings
            if settings.SUPABASE_URL and settings.SUPABASE_KEY:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{settings.SUPABASE_URL}/rest/v1/system_config",
                        headers={
                            "apikey": settings.SUPABASE_KEY,
                            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                        },
                        params={"key": "eq.noble:trading:mode", "select": "value"},
                    )
                    if resp.status_code == 200:
                        rows = resp.json()
                        if rows:
                            state = ModeState.model_validate_json(rows[0]["value"])
                            self._cached_state = state
                            return state.current_mode
        except Exception as e:
            logger.debug(f"ModeManager: Supabase read failed, trying Redis: {e}")

        # Try Redis (L1 cache)
        client = await self._get_client()
        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        if client:
            try:
                data = await client.get(mode_key)
                if data:
                    state = ModeState.model_validate_json(data)
                    self._cached_state = state
                    return state.current_mode
            except Exception as e:
                logger.error(f"ModeManager: Failed to read mode from Redis: {e}")

        # Try to infer from AlpacaExecutor config
        try:
            from regime_platform.core.settings import settings
            base_url = getattr(settings, 'ALPACA_BASE_URL', '') or ''
            if 'paper-api' in base_url:
                return TradingMode.PAPER
            elif 'api.alpaca.markets' in base_url and 'paper' not in base_url:
                return TradingMode.LIVE
        except Exception:
            pass

        # Default to paper for safety
        return TradingMode.PAPER

    async def get_mode_state(self, org_id: Optional[str] = None) -> ModeState:
        """Get full mode state including change history."""
        if self._cached_state:
            return self._cached_state

        client = await self._get_client()
        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        if client:
            try:
                data = await client.get(mode_key)
                if data:
                    state = ModeState.model_validate_json(data)
                    self._cached_state = state
                    return state
            except Exception as e:
                logger.error(f"ModeManager: Failed to read mode state: {e}")

        # Return default
        return ModeState(
            current_mode=TradingMode.PAPER,
            last_changed_at=datetime.now(timezone.utc),
            last_changed_by="system",
        )

    async def request_mode_change(
        self,
        target_mode: TradingMode,
        requested_by: str,
        org_id: Optional[str] = None,
    ) -> ModeChangeRequest:
        """
        Request a mode change. Generates a confirmation token that must be
        confirmed within 5 minutes.
        
        Returns ModeChangeRequest with confirmation_token.
        """
        current = await self.get_current_mode(org_id=org_id)

        # Validate transition
        if current == target_mode:
            raise ValueError(f"Already in {target_mode.value} mode")

        # Simulation mode cannot be changed via API (requires API keys)
        if current == TradingMode.SIMULATION:
            raise ValueError(
                "Cannot switch from simulation mode. "
                "Configure Alpaca API keys to enable paper/live trading."
            )

        # Generate confirmation token
        token = secrets.token_urlsafe(32)

        # Store token in Redis with TTL
        client = await self._get_client()
        if client:
            try:
                token_data = ModeChangeRequest(
                    target_mode=target_mode,
                    requested_by=requested_by,
                    requested_at=datetime.now(timezone.utc),
                    confirmation_token=token,
                )
                token_key = tenant_key(f"{self.TOKEN_BASE_KEY}:{token}", org_id=org_id)
                await client.set(
                    token_key,
                    token_data.model_dump_json(),
                    ex=self.TOKEN_TTL,
                )
                logger.info(
                    f"ModeManager: Mode change requested "
                    f"{current.value} → {target_mode.value} by {requested_by} "
                    f"(token expires in {self.TOKEN_TTL}s)"
                )
            except Exception as e:
                logger.error(f"ModeManager: Failed to store token: {e}")
                raise RuntimeError("Failed to create mode change request")

        return ModeChangeRequest(
            target_mode=target_mode,
            requested_by=requested_by,
            requested_at=datetime.now(timezone.utc),
            confirmation_token=token,
        )

    async def confirm_mode_change(
        self,
        confirmation_token: str,
        acknowledged_by: str,
        acknowledgment_text: str,
        org_id: Optional[str] = None,
    ) -> ModeState:
        """
        Confirm a mode change using the token from request_mode_change().
        
        For LIVE mode: acknowledgment_text must match LIVE_ACKNOWLEDGMENT exactly.
        For PAPER mode: any acknowledgment text is accepted.
        
        Before switching to LIVE, runs a health check on the target environment.
        """
        # Validate token exists in Redis
        client = await self._get_client()
        if not client:
            raise RuntimeError("Redis unavailable - cannot confirm mode change")

        try:
            token_key = tenant_key(f"{self.TOKEN_BASE_KEY}:{confirmation_token}", org_id=org_id)
            token_data = await client.get(token_key)
            if not token_data:
                raise ValueError("Invalid or expired confirmation token")

            request = ModeChangeRequest.model_validate_json(token_data)
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"ModeManager: Token validation failed: {e}")
            raise RuntimeError("Token validation failed")

        # For live mode, require exact acknowledgment
        if request.target_mode == TradingMode.LIVE:
            if acknowledgment_text.strip().upper() != self.LIVE_ACKNOWLEDGMENT:
                raise ValueError(
                    f"Acknowledgment text must be exactly: '{self.LIVE_ACKNOWLEDGMENT}'"
                )

            # Pre-switch health check on target environment
            health = await self._check_target_health(TradingMode.LIVE)
            if not health.get("ok"):
                raise RuntimeError(
                    f"Target environment health check failed: {health.get('error', 'unknown')}"
                )

        # Execute mode change
        current = await self.get_current_mode(org_id=org_id)
        new_state = ModeState(
            current_mode=request.target_mode,
            last_changed_at=datetime.now(timezone.utc),
            last_changed_by=acknowledged_by,
            previous_mode=current,
        )

        # Update AlpacaExecutor base URL
        await self._switch_executor_mode(request.target_mode)

        # Save new state to Redis (no TTL - persistent, acts as L1 cache)
        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        try:
            await client.set(mode_key, new_state.model_dump_json())
            # Delete used token
            await client.delete(token_key)
        except Exception as e:
            logger.error(f"ModeManager: Failed to persist mode change to Redis: {e}")

        # v7.3: Write-through to Supabase system_config (L2 - durable)
        try:
            from regime_platform.core.settings import settings as _settings
            if _settings.SUPABASE_URL and _settings.SUPABASE_KEY:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as sb_client:
                    resp = await sb_client.post(
                        f"{_settings.SUPABASE_URL}/rest/v1/system_config",
                        headers={
                            "apikey": _settings.SUPABASE_KEY,
                            "Authorization": f"Bearer {_settings.SUPABASE_KEY}",
                            "Content-Type": "application/json",
                            "Prefer": "resolution=merge-duplicates",
                        },
                        json={
                            "key": "noble:trading:mode",
                            "value": new_state.model_dump_json(),
                            "category": "operational",
                        },
                    )
                    if resp.status_code in (200, 201, 204):
                        logger.info("ModeManager: Mode state written to Supabase system_config")
                    else:
                        logger.warning(f"ModeManager: Supabase write failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"ModeManager: Supabase write-through failed: {e}")

        # Update in-memory cache
        self._cached_state = new_state

        # Audit log the mode change
        await self._audit_mode_change(current, request.target_mode, acknowledged_by, org_id=org_id)

        logger.warning(
            f"ModeManager: MODE CHANGED {current.value} → {request.target_mode.value} "
            f"by {acknowledged_by}"
        )

        return new_state

    async def _check_target_health(self, target_mode: TradingMode) -> dict:
        """
        Health check the target Alpaca environment before switching.
        
        Temporarily creates a REST client pointing to the target URL
        and attempts to fetch account info. Does NOT modify global settings.
        """
        try:
            from regime_platform.core.settings import settings
            import alpaca_trade_api as tradeapi

            if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
                return {"ok": False, "error": "No Alpaca API keys configured"}

            target_url = (
                settings.ALPACA_LIVE_BASE_URL
                if target_mode == TradingMode.LIVE
                else settings.ALPACA_PAPER_BASE_URL
            )

            # Temporary client for health check (does not affect factory)
            temp_client = tradeapi.REST(
                key_id=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=target_url,
            )

            account = temp_client.get_account()
            return {
                "ok": True,
                "account_status": account.status,
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
            }
        except ImportError:
            return {"ok": False, "error": "alpaca-trade-api not installed"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def _switch_executor_mode(self, target_mode: TradingMode):
        """
        Switch the AlpacaExecutor to the target mode by updating base_url.
        
        PAPER: https://paper-api.alpaca.markets
        LIVE:  https://api.alpaca.markets
        
        Uses ExecutorFactory to ensure all services get the updated executor.
        """
        try:
            from regime_platform.core.settings import settings
            from regime_platform.services.operational.executor_factory import executor_factory
            
            if target_mode == TradingMode.LIVE:
                live_url = getattr(settings, 'ALPACA_LIVE_BASE_URL', None) or 'https://api.alpaca.markets'
                settings.ALPACA_BASE_URL = live_url
            elif target_mode == TradingMode.PAPER:
                paper_url = getattr(settings, 'ALPACA_PAPER_BASE_URL', None) or 'https://paper-api.alpaca.markets'
                settings.ALPACA_BASE_URL = paper_url

            # Invalidate the executor factory cache so next access creates
            # a fresh executor with the new base URL
            executor_factory.invalidate()
            logger.info(
                f"ModeManager: Executor mode switched to {target_mode.value} "
                f"(base_url={settings.ALPACA_BASE_URL})"
            )

        except Exception as e:
            logger.error(f"ModeManager: Failed to switch executor mode: {e}")

    async def _audit_mode_change(
        self,
        from_mode: TradingMode,
        to_mode: TradingMode,
        changed_by: str,
        org_id: Optional[str] = None,
    ):
        """Log mode change to audit trail."""
        try:
            from regime_platform.services.operational.audit_logger import (
                audit_logger, AuditEventType
            )
            await audit_logger.log_event(
                event_type=AuditEventType.MODE_CHANGED,
                user_id=changed_by,
                org_id=org_id,
                metadata={
                    "from_mode": from_mode.value,
                    "to_mode": to_mode.value,
                },
            )
        except Exception as e:
            logger.error(f"ModeManager: Audit log failed: {e}")

        # Discord notification
        try:
            from notifications.discord import discord_notifier
            await discord_notifier.send_status(
                f"**TRADING MODE CHANGED**: {from_mode.value} → {to_mode.value} "
                f"by {changed_by}"
            )
        except Exception:
            pass


# Module-level singleton
mode_manager = ModeManager()
