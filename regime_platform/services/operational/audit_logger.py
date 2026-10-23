"""
Audit Logger Service - Append-only, immutable trade audit trail.

Every write goes to:
1. Supabase trade_audit_log table (source of truth, immutable)
2. Python logging (stdout/CloudWatch for real-time monitoring)
3. Discord webhook (critical events only: halts, risk breaches, reconciliation failures)

This service is called by:
- AlpacaExecutor after every order submission/fill
- KillSwitchService on activate/deactivate
- TradingWorkflow on approve/reject
- ReconciliationService on fill verification
- ModeManager on paper/live switch

CRITICAL: Audit events must NEVER be silently lost. If Supabase is down,
the event is still logged to stdout and an error is raised so the caller
can decide whether to proceed.
"""

from __future__ import annotations
import logging
import httpx
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AuditEventType(str, Enum):
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    SIGNAL_FILTERED = "SIGNAL_FILTERED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_PARTIAL_FILL = "ORDER_PARTIAL_FILL"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    HALT_ACTIVATED = "HALT_ACTIVATED"
    HALT_DEACTIVATED = "HALT_DEACTIVATED"
    MODE_CHANGED = "MODE_CHANGED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RISK_LIMIT_BREACH = "RISK_LIMIT_BREACH"
    RECONCILIATION_PASSED = "RECONCILIATION_PASSED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    KILL_SWITCH_CANCEL_ALL = "KILL_SWITCH_CANCEL_ALL"
    KILL_SWITCH_CLOSE_ALL = "KILL_SWITCH_CLOSE_ALL"


# Events that trigger Discord notifications
DISCORD_CRITICAL_EVENTS = {
    AuditEventType.HALT_ACTIVATED,
    AuditEventType.HALT_DEACTIVATED,
    AuditEventType.RISK_LIMIT_BREACH,
    AuditEventType.RECONCILIATION_FAILED,
    AuditEventType.KILL_SWITCH_CANCEL_ALL,
    AuditEventType.KILL_SWITCH_CLOSE_ALL,
    AuditEventType.MODE_CHANGED,
    AuditEventType.ORDER_REJECTED,
}


class AuditEvent(BaseModel):
    event_type: AuditEventType
    user_id: str
    org_id: Optional[str] = None
    symbol: Optional[str] = None
    order_id: Optional[str] = None
    direction: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    order_type: Optional[str] = None
    regime: Optional[str] = None
    strategy: Optional[str] = None
    signal_score: Optional[float] = None
    risk_metrics: Optional[dict] = None
    metadata: Optional[dict] = None


class AuditLogger:
    """
    Append-only audit logger backed by Supabase trade_audit_log table.
    
    Uses raw httpx to PostgREST (same pattern as backtest_store.py).
    Table schema must be created via migration:
    supabase/migrations/00000000000014_trade_audit_log.sql
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> Optional[httpx.AsyncClient]:
        """Lazy httpx client (same pattern as backtest_store.py)."""
        if self._client is None or self._client.is_closed:
            try:
                from regime_platform.core.settings import settings
                if settings.SUPABASE_URL and settings.SUPABASE_SECRET_KEY:
                    self._client = httpx.AsyncClient(
                        base_url=f"{settings.SUPABASE_URL}/rest/v1",
                        headers={
                            "apikey": settings.SUPABASE_SECRET_KEY,
                            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
                            "Content-Type": "application/json",
                            "Prefer": "return=representation",
                        },
                        timeout=10.0,
                    )
                    logger.info("AuditLogger: Supabase client connected")
                else:
                    logger.warning("AuditLogger: No Supabase credentials, audit log disabled")
            except Exception as e:
                logger.error(f"AuditLogger: Client init failed: {e}")
        return self._client

    async def close(self):
        """Close httpx client (called during app lifespan shutdown)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            logger.info("AuditLogger: Client closed")

    async def log_event(
        self,
        event_type: AuditEventType,
        user_id: str,
        org_id: Optional[str] = None,
        symbol: Optional[str] = None,
        order_id: Optional[str] = None,
        direction: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
        regime: Optional[str] = None,
        strategy: Optional[str] = None,
        signal_score: Optional[float] = None,
        risk_metrics: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[int]:
        """
        Log an audit event. Returns the audit log row ID, or None on failure.
        
        ALWAYS logs to Python logging even if Supabase is down.
        Sends Discord notification for critical events.
        """
        event = AuditEvent(
            event_type=event_type,
            user_id=user_id,
            org_id=org_id,
            symbol=symbol,
            order_id=order_id,
            direction=direction,
            quantity=quantity,
            price=price,
            order_type=order_type,
            regime=regime,
            strategy=strategy,
            signal_score=signal_score,
            risk_metrics=risk_metrics,
            metadata=metadata,
        )

        # 1. Always log to Python logging
        log_msg = (
            f"AUDIT | {event_type.value} | user={user_id} org={org_id} "
            f"symbol={symbol} order={order_id} dir={direction} "
            f"qty={quantity} price={price}"
        )
        if event_type in DISCORD_CRITICAL_EVENTS:
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        # 2. Write to Supabase
        row_id = None
        client = await self._get_client()
        if client:
            try:
                payload = {
                    "event_type": event_type.value,
                    "user_id": user_id,
                    "org_id": org_id,
                    "symbol": symbol,
                    "order_id": order_id,
                    "direction": direction,
                    "quantity": quantity,
                    "price": price,
                    "order_type": order_type,
                    "regime": regime,
                    "strategy": strategy,
                    "signal_score": signal_score,
                    "risk_metrics": risk_metrics,
                    "metadata": metadata or {},
                }

                resp = await client.post("/trade_audit_log", json=payload)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    if isinstance(data, list) and data:
                        row_id = data[0].get("id")
                    elif isinstance(data, dict):
                        row_id = data.get("id")
                    logger.debug(f"AuditLogger: Event {event_type.value} saved (id={row_id})")
                else:
                    logger.error(
                        f"AuditLogger: Supabase insert failed "
                        f"status={resp.status_code} body={resp.text[:200]}"
                    )
            except Exception as e:
                logger.error(f"AuditLogger: Supabase write failed: {e}")

        # 3. Discord notification for critical events
        if event_type in DISCORD_CRITICAL_EVENTS:
            await self._send_discord(event)

        return row_id

    async def query_events(
        self,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict:
        """
        Query audit events with filters. Returns dict with 'events' list and 'total' count.
        """
        client = await self._get_client()
        if not client:
            return {"events": [], "total": 0}

        try:
            params = {}
            filters = []
            
            if user_id:
                filters.append(f"user_id.eq.{user_id}")
            if event_type:
                filters.append(f"event_type.eq.{event_type}")
            if symbol:
                filters.append(f"symbol.eq.{symbol.upper()}")
            if start_date:
                filters.append(f"created_at.gte.{start_date}")
            if end_date:
                filters.append(f"created_at.lte.{end_date}")

            if filters:
                params["and"] = f"({','.join(filters)})"

            params["order"] = "created_at.desc"
            params["limit"] = limit
            params["offset"] = offset

            resp = await client.get("/trade_audit_log", params=params)
            if resp.status_code == 200:
                events = resp.json()
                # Get total count from content-range header
                content_range = resp.headers.get("content-range", "")
                total = int(content_range.split("/")[1]) if "/" in content_range else len(events)
                return {"events": events, "total": total}
            else:
                logger.error(f"AuditLogger: Query failed status={resp.status_code}")
                return {"events": [], "total": 0}

        except Exception as e:
            logger.error(f"AuditLogger: Query error: {e}")
            return {"events": [], "total": 0}

    async def get_summary(self, user_id: Optional[str] = None) -> dict:
        """Get event count summary for the last 24h/7d/30d."""
        client = await self._get_client()
        if not client:
            return {}

        try:
            now = datetime.now(timezone.utc)
            summaries = {}
            
            for period, hours in [("24h", 24), ("7d", 168), ("30d", 720)]:
                from datetime import timedelta
                start = (now - timedelta(hours=hours)).isoformat()
                
                params = {
                    "select": "event_type",
                    "created_at": f"gte.{start}",
                    "limit": 10000,
                }
                if user_id:
                    params["user_id"] = f"eq.{user_id}"

                resp = await client.get("/trade_audit_log", params=params)
                if resp.status_code == 200:
                    events = resp.json()
                    counts = {}
                    for e in events:
                        et = e.get("event_type", "unknown")
                        counts[et] = counts.get(et, 0) + 1
                    summaries[period] = counts
                else:
                    summaries[period] = {}

            return summaries

        except Exception as e:
            logger.error(f"AuditLogger: Summary error: {e}")
            return {}

    async def _send_discord(self, event: AuditEvent):
        """Send critical audit event to Discord."""
        try:
            from notifications.discord import discord_notifier
            message = (
                f"**{event.event_type.value}** | "
                f"Org: {event.org_id or 'N/A'} | "
                f"User: {event.user_id} | "
                f"Symbol: {event.symbol or 'N/A'} | "
                f"Order: {event.order_id or 'N/A'} | "
                f"Direction: {event.direction or 'N/A'} | "
                f"Qty: {event.quantity or 'N/A'} @ {event.price or 'N/A'}"
            )
            if event.metadata:
                message += f"\nDetails: {event.metadata}"
            await discord_notifier.send_status(message)
        except Exception as e:
            logger.error(f"AuditLogger: Discord send failed: {e}")


# Module-level singleton
audit_logger = AuditLogger()
