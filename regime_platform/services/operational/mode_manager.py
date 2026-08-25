"""
Mode Manager Service - Paper/Live trading mode toggle with confirmation gates.

Manages the current trading mode (simulation / paper / live) with:
1. Supabase-backed mode state (persists across restarts)
2. Mode switch requires confirmation token (prevents accidental switches)
3. Every mode change is audit-logged
4. Mode change triggers health check on target environment
5. Live mode requires explicit acknowledgment of risk disclaimer

Flow for PAPER -> LIVE:
1. User requests mode change -> generate confirmation token (5-min TTL,
   expiry checked manually against `requested_at` since Supabase has no
   native key TTL the way Redis did)
2. UI shows risk disclaimer + confirmation dialog with token
3. User confirms (must provide acknowledgment text)
4. Backend validates confirmation token
5. Backend switches AlpacaExecutor base_url to live endpoint
6. Backend logs MODE_CHANGED event to audit trail
7. Backend sends Discord notification
8. Frontend updates all visual indicators

Flow for LIVE -> PAPER:
1. User requests mode change -> generate confirmation token
2. Backend cancels all open live orders (safety)
3. Backend switches to paper endpoint
4. Audit log + Discord notification

2026-08-25: Redis retired. `request_mode_change`/`confirm_mode_change`
used to store the confirmation token in Redis with no fallback — when
Redis was unavailable, `request_mode_change` would silently produce a
token that could never be confirmed, and `confirm_mode_change` raised a
hard RuntimeError. The token is now stored via `system_config`
(Supabase `nt_system_config`), the same durable store the mode state
itself already used as its authoritative L2 — paper<->live switching no
longer depends on Redis being up at all.
"""

from __future__ import annotations
import logging
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

from regime_platform.core.tenant import tenant_key
from regime_platform.services.operational.system_config import (
    get_config,
    set_config,
    delete_config,
)

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
    """Supabase-backed trading mode manager with confirmation gates."""

    MODE_BASE_KEY = "noble:trading:mode"
    TOKEN_BASE_KEY = "noble:trading:mode_token"
    TOKEN_TTL = 300  # 5 minutes

    # Required acknowledgment text for live mode activation
    LIVE_ACKNOWLEDGMENT = "I UNDERSTAND THE RISKS"

    def __init__(self):
        self._cached_state: Optional[ModeState] = None

    async def close(self):
        """No-op — kept for interface parity with the other operational
        services main.py's shutdown block calls alongside this one."""
        return None

    async def get_current_mode(self, org_id: Optional[str] = None) -> TradingMode:
        """Get current trading mode. Defaults to PAPER if not set.

        Reads from Supabase nt_system_config first (durable source of
        truth), then falls back to Alpaca config inference.
        """
        # Check in-memory cache first (O(1), zero latency)
        if self._cached_state:
            return self._cached_state.current_mode

        # Try Supabase nt_system_config (durable, authoritative)
        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        data = await get_config(mode_key)
        if data:
            try:
                state = ModeState.model_validate_json(data)
                self._cached_state = state
                return state.current_mode
            except Exception as e:
                logger.error(f"ModeManager: Failed to parse mode state from Supabase: {e}")

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

        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        data = await get_config(mode_key)
        if data:
            try:
                state = ModeState.model_validate_json(data)
                self._cached_state = state
                return state
            except Exception as e:
                logger.error(f"ModeManager: Failed to parse mode state from Supabase: {e}")

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
        token_data = ModeChangeRequest(
            target_mode=target_mode,
            requested_by=requested_by,
            requested_at=datetime.now(timezone.utc),
            confirmation_token=token,
        )

        # Store token in Supabase with manually-checked expiry (no native
        # TTL like Redis had — confirm_mode_change checks requested_at).
        token_key = tenant_key(f"{self.TOKEN_BASE_KEY}:{token}", org_id=org_id)
        stored = await set_config(token_key, token_data.model_dump_json(), category="mode_token")
        if not stored:
            logger.error("ModeManager: Failed to store confirmation token")
            raise RuntimeError("Failed to create mode change request")

        logger.info(
            f"ModeManager: Mode change requested "
            f"{current.value} -> {target_mode.value} by {requested_by} "
            f"(token expires in {self.TOKEN_TTL}s)"
        )

        return token_data

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
        # Validate token exists in Supabase and hasn't expired. There's no
        # native key TTL here (unlike Redis), so expiry is checked against
        # the stored requested_at timestamp.
        token_key = tenant_key(f"{self.TOKEN_BASE_KEY}:{confirmation_token}", org_id=org_id)
        token_data = await get_config(token_key)
        if not token_data:
            raise ValueError("Invalid or expired confirmation token")

        try:
            request = ModeChangeRequest.model_validate_json(token_data)
        except Exception as e:
            logger.error(f"ModeManager: Token validation failed: {e}")
            raise RuntimeError("Token validation failed")

        age_seconds = (datetime.now(timezone.utc) - request.requested_at).total_seconds()
        if age_seconds > self.TOKEN_TTL:
            await delete_config(token_key)
            raise ValueError("Invalid or expired confirmation token")

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

        # Persist new state to Supabase (durable, authoritative)
        mode_key = tenant_key(self.MODE_BASE_KEY, org_id=org_id)
        if await set_config(mode_key, new_state.model_dump_json()):
            logger.info("ModeManager: Mode state written to Supabase nt_system_config")
        else:
            logger.warning("ModeManager: Supabase mode-state write failed")

        # Delete the used token
        await delete_config(token_key)

        # Update in-memory cache
        self._cached_state = new_state

        # Audit log the mode change
        await self._audit_mode_change(current, request.target_mode, acknowledged_by, org_id=org_id)

        logger.warning(
            f"ModeManager: MODE CHANGED {current.value} -> {request.target_mode.value} "
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
            from alpaca.trading.client import TradingClient

            if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
                return {"ok": False, "error": "No Alpaca API keys configured"}

            target_url = (
                settings.ALPACA_LIVE_BASE_URL
                if target_mode == TradingMode.LIVE
                else settings.ALPACA_PAPER_BASE_URL
            )

            # Temporary client for health check (does not affect factory)
            temp_client = TradingClient(
                settings.ALPACA_API_KEY,
                settings.ALPACA_SECRET_KEY,
                paper=target_mode == TradingMode.PAPER,
                url=target_url,
            )

            account = temp_client.get_account()
            # alpaca-trade-api 3.x may use account_status or account.status
            status = getattr(account, 'account_status', getattr(account, 'status', 'UNKNOWN'))
            return {
                "ok": True,
                "account_status": status,
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
            }
        except ImportError:
            return {"ok": False, "error": "alpaca-py not installed"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def _switch_executor_mode(self, target_mode: TradingMode):
        """
        Switch the AlpacaExecutor to the target mode by updating base_url.

        .. deprecated:: P5 (2026-07-20)
            Mode switching is now a no-op when the execution gate is active.
            The noble server has no executor to switch. The hermes agent
            handles paper/live mode via its own env vars.

        PAPER: https://paper-api.alpaca.markets
        LIVE:  https://api.alpaca.markets

        Uses ExecutorFactory to ensure all services get the updated executor.
        """
        # P5: Short-circuit if execution is disabled.
        from regime_platform.services.execution_deprecation import execution_enabled
        if not execution_enabled():
            logger.warning(
                "ModeManager._switch_executor_mode: execution disabled — "
                "no-op (target=%s). The hermes agent owns paper/live mode.",
                target_mode.value,
            )
            return

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
                f"**TRADING MODE CHANGED**: {from_mode.value} -> {to_mode.value} "
                f"by {changed_by}"
            )
        except Exception:
            pass


# Module-level singleton
mode_manager = ModeManager()
