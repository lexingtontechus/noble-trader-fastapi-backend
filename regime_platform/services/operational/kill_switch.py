"""
Kill Switch Service - Three-tier emergency halt system.

Tiers:
1. GLOBAL_HALT - Stops ALL trading across all users, all symbols
2. USER_HALT   - Stops trading for a specific user
3. SYMBOL_HALT - Stops trading for a specific symbol

State is stored in Supabase (`nt_system_config`) for cross-process
persistence:
- noble:halt:global         -> JSON HaltRecord or None
- noble:halt:user:{user_id} -> JSON HaltRecord or None
- noble:halt:symbol:{sym}   -> JSON HaltRecord or None

In-memory cache for zero-latency checks in the hot path.

v7.4: DEACTIVATED - Background refresh (pub/sub + polling) disabled.
      The Redis pub/sub listener was causing repeated crashes on Render
      when Upstash closed idle connections, creating a crash loop that
      impacted service availability.

      Current state:
      - start_refresh() is a no-op - no background tasks
      - stop_refresh() is a no-op - nothing to stop
      - is_halted() returns False (cache is always empty) - trading always allowed
      - activate_halt()/deactivate_halt() still write to Supabase for future use
      - KillSwitchGateMiddleware is fail-open, so deactivation = trading allowed

2026-08-25: Redis retired — on-demand reads/writes now go to Supabase
`nt_system_config` instead (same fail-open semantics: a read/write
failure never raises, it just leaves the in-memory cache as the only
source of truth for this process). This is a single-process deployment,
so the in-memory cache alone already covers same-process checks; the
Supabase persistence is for surviving a restart, not cross-process
sharing.
"""

from __future__ import annotations
import json
import logging
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


class HaltLevel(str, Enum):
    GLOBAL_HALT = "global_halt"
    USER_HALT = "user_halt"
    SYMBOL_HALT = "symbol_halt"


class HaltReason(str, Enum):
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"
    MAX_DRAWDOWN = "max_drawdown"
    DATA_FEED_ERROR = "data_feed_error"
    COMPLIANCE = "compliance"
    RECONCILIATION_FAILURE = "reconciliation_failure"


class HaltRecord(BaseModel):
    level: HaltLevel
    reason: HaltReason
    triggered_by: str           # user_id or "system"
    triggered_at: datetime
    scope: Optional[str] = None  # user_id or symbol
    notes: Optional[str] = None


class KillSwitchService:
    """Supabase-backed kill switch with in-memory cache for hot-path checks.

    v7.4: DEACTIVATED - All background refresh tasks disabled.
    See module docstring for reactivation options.
    """

    GLOBAL_BASE_KEY = "noble:halt:global"
    USER_BASE_KEY = "noble:halt:user"
    SYMBOL_BASE_KEY = "noble:halt:symbol"
    PUBSUB_CHANNEL = "noble:halt:updates"

    def __init__(self):
        self._cache: dict[str, Optional[HaltRecord]] = {}
        self._running = False

    # ── Disabled background tasks ─────────────────────────────────────────

    async def start_refresh(self):
        """DEACTIVATED - No background tasks started.

        Previous versions started a Redis pub/sub listener and a 60s
        fallback polling loop. Both have been disabled because the pub/sub
        listener caused repeated Render crashes when Upstash closed idle
        connections, creating a crash loop that impacted service availability.

        The kill switch cache remains empty, so is_halted() always returns
        False and trading is never blocked. This is safe because:
        1. KillSwitchGateMiddleware is fail-open (allows requests on error)
        2. No halt records exist in cache = trading allowed
        3. Emergency actions (cancel-all, close-all) still work on-demand
        """
        self._running = True
        logger.warning(
            "KillSwitch: DEACTIVATED - no background refresh tasks. "
            "Kill switch will not enforce halts until reactivated. "
            "See kill_switch.py module docstring for reactivation options."
        )

    async def stop_refresh(self):
        """DEACTIVATED - Nothing to stop."""
        self._running = False
        logger.info("KillSwitch: Stopped (was already deactivated)")

    # ── On-demand Supabase read ──────────────────────────────────────────
    # The v7.4 deactivation killed the BACKGROUND pub/sub refresh (crash
    # loop on Render). The safe reactivation per the module docstring is
    # option #2: read ON DEMAND on every check — no background tasks, no
    # persistent connection to maintain. Cache is still consulted first
    # (sub-millisecond hot path); when it is empty we fall through to
    # Supabase. Fail-open on read errors: the kill switch is a safety
    # net, not a primary control.

    async def _stored_get_record(self, key: str) -> Optional[HaltRecord]:
        """Read one halt key from Supabase and return a HaltRecord (or None)."""
        raw = await get_config(key)
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            return HaltRecord.model_validate(data)
        except Exception as exc:
            logger.warning("KillSwitch: invalid halt record at %s: %s", key, exc)
            return None

    async def is_halted(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> bool:
        """
        Check if trading is halted. O(1) from in-memory cache, falling
        through to an on-demand Supabase read when the cache is empty (so
        a halt activated in ANOTHER process is honored without background
        refresh).

        Checks in order: global > user > symbol.
        If ANY tier is halted, returns True.
        """
        # Global halt
        global_key = tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        if self._cache.get(global_key) is not None:
            return True
        global_record = await self._stored_get_record(global_key)
        if global_record is not None:
            self._cache[global_key] = global_record
            return True

        # User halt
        if user_id:
            user_key = tenant_key(f"{self.USER_BASE_KEY}:{user_id}", org_id=org_id, user_id=user_id if not org_id else None)
            if self._cache.get(user_key) is not None:
                return True
            user_record = await self._stored_get_record(user_key)
            if user_record is not None:
                self._cache[user_key] = user_record
                return True

        # Symbol halt
        if symbol:
            sym_key = tenant_key(f"{self.SYMBOL_BASE_KEY}:{symbol.upper()}", org_id=org_id)
            if self._cache.get(sym_key) is not None:
                return True
            sym_record = await self._stored_get_record(sym_key)
            if sym_record is not None:
                self._cache[sym_key] = sym_record
                return True

        return False

    async def get_active_halt(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> Optional[HaltRecord]:
        """Get the most specific active halt record (for error messages).

        Mirrors ``is_halted``: cache first, then on-demand Redis (so a
        cross-process halt is surfaced in the 403 response).
        """
        # Check in priority order: global > user > symbol
        global_key = tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        global_halt = self._cache.get(global_key) or await self._stored_get_record(global_key)
        if global_halt:
            self._cache[global_key] = global_halt
            return global_halt

        if user_id:
            user_key = tenant_key(f"{self.USER_BASE_KEY}:{user_id}", org_id=org_id, user_id=user_id if not org_id else None)
            user_halt = self._cache.get(user_key) or await self._stored_get_record(user_key)
            if user_halt:
                self._cache[user_key] = user_halt
                return user_halt

        if symbol:
            sym_key = tenant_key(f"{self.SYMBOL_BASE_KEY}:{symbol.upper()}", org_id=org_id)
            sym_halt = self._cache.get(sym_key) or await self._stored_get_record(sym_key)
            if sym_halt:
                self._cache[sym_key] = sym_halt
                return sym_halt

        return None

    # ── Activate / Deactivate (writes to Redis + cache) ───────────────────

    async def activate_halt(
        self,
        level: HaltLevel,
        reason: HaltReason,
        triggered_by: str,
        scope: Optional[str] = None,
        notes: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> HaltRecord:
        """Activate a halt at the specified level.

        Writes to Supabase for persistence and updates in-memory cache.
        Note: Without background refresh, this only affects the current
        process. Other processes won't see the halt until reactivation.
        """
        record = HaltRecord(
            level=level,
            reason=reason,
            triggered_by=triggered_by,
            triggered_at=datetime.now(timezone.utc),
            scope=scope,
            notes=notes,
        )

        key = self._get_key(level, scope, org_id=org_id)
        record_json = record.model_dump_json()

        # Write to Supabase
        if await set_config(key, record_json, category="kill_switch"):
            logger.warning(f"KillSwitch: HALT ACTIVATED - {level.value} scope={scope} reason={reason.value} by={triggered_by} org={org_id}")
        else:
            logger.error("KillSwitch: Failed to write halt to Supabase")

        # Update in-memory cache immediately
        self._cache[key] = record

        return record

    async def deactivate_halt(
        self,
        level: HaltLevel,
        scope: Optional[str] = None,
        deactivated_by: str = "unknown",
        org_id: Optional[str] = None,
    ) -> bool:
        """Deactivate a halt at the specified level."""
        key = self._get_key(level, scope, org_id=org_id)

        # Remove from Supabase
        if await delete_config(key):
            logger.info(f"KillSwitch: HALT DEACTIVATED - {level.value} scope={scope} by={deactivated_by} org={org_id}")
        else:
            logger.error("KillSwitch: Failed to remove halt from Supabase")

        # Update in-memory cache
        self._cache[key] = None

        return True

    async def get_all_active_halts(self) -> list[HaltRecord]:
        """Get all active halt records."""
        return [v for v in self._cache.values() if v is not None]

    # ── Emergency actions ─────────────────────────────────────────────────

    async def cancel_all_orders(self, user_id: str) -> dict:
        """
        Cancel all open orders for a user via ExecutorFactory.
        Returns dict with count of cancelled orders.

        .. deprecated:: P5 (2026-07-20)
            Execution is disabled on noble server. This method now returns
            a no-op result with `cancelled_count=0` and a `deprecated=True`
            flag when the execution gate is active. The hermes agent owns
            order cancellation now.
        """
        # P5: Short-circuit if execution is disabled.
        from regime_platform.services.execution_deprecation import execution_enabled
        if not execution_enabled():
            logger.warning(
                "KillSwitch.cancel_all_orders: execution disabled on noble server. "
                "Use hermes agent (noble-trader-agent) for order cancellation. "
                "user_id=%s", user_id,
            )
            return {"cancelled_count": 0, "order_ids": [], "deprecated": True}

        cancelled = []
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()
            if api:
                orders = api.get_orders(status="open")
                for order in orders:
                    try:
                        api.cancel_order(order.id)
                        cancelled.append(order.id)
                        logger.info(f"KillSwitch: Cancelled order {order.id}")
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to cancel order {order.id}: {e}")
            else:
                logger.warning("KillSwitch: No Alpaca API client - cancel_all_orders is no-op (simulation mode)")
        except Exception as e:
            logger.error(f"KillSwitch: cancel_all_orders failed: {e}")

        return {"cancelled_count": len(cancelled), "order_ids": cancelled}

    async def close_all_positions(self, user_id: str) -> dict:
        """
        Close all open positions for a user via ExecutorFactory.
        Returns dict with count of closed positions.

        .. deprecated:: P5 (2026-07-20)
            Execution is disabled on noble server. This method now returns
            a no-op result with `closed_count=0` and a `deprecated=True`
            flag when the execution gate is active. The hermes agent owns
            position management now.
        """
        # P5: Short-circuit if execution is disabled.
        from regime_platform.services.execution_deprecation import execution_enabled
        if not execution_enabled():
            logger.warning(
                "KillSwitch.close_all_positions: execution disabled on noble server. "
                "Use hermes agent (noble-trader-agent) for position management. "
                "user_id=%s", user_id,
            )
            return {"closed_count": 0, "symbols": [], "deprecated": True}

        closed = []
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()
            if api:
                positions = api.list_positions()
                for pos in positions:
                    try:
                        api.close_position(pos.symbol)
                        closed.append(pos.symbol)
                        logger.info(f"KillSwitch: Closed position {pos.symbol}")
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to close position {pos.symbol}: {e}")
            else:
                logger.warning("KillSwitch: No Alpaca API client - close_all_positions is no-op (simulation mode)")
        except Exception as e:
            logger.error(f"KillSwitch: close_all_positions failed: {e}")

        return {"closed_count": len(closed), "symbols": closed}

    # ── Helpers ───────────────────────────────────────────────────────────

    def _get_key(self, level: HaltLevel, scope: Optional[str] = None, org_id: Optional[str] = None) -> str:
        if level == HaltLevel.GLOBAL_HALT:
            return tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        elif level == HaltLevel.USER_HALT:
            return tenant_key(f"{self.USER_BASE_KEY}:{scope}", org_id=org_id, user_id=scope if not org_id else None)
        elif level == HaltLevel.SYMBOL_HALT:
            return tenant_key(f"{self.SYMBOL_BASE_KEY}:{scope.upper()}", org_id=org_id)
        return tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)


# Module-level singleton
kill_switch = KillSwitchService()
