"""
Reconciliation Service - Fill verification and position reconciliation.

Compares internal order expectations against Alpaca's reported fills:
1. IMMEDIATE - After order submission, verify fill status
2. PERIODIC  - Reconcile all orders from last 24 hours

Thresholds (configurable):
- SLIPPAGE_WARNING: > 50 bps -> log warning
- SLIPPAGE_CRITICAL: > 200 bps -> trigger risk alert + Discord
- PARTIAL_FILL_TIMEOUT: 5 min -> suggest cancel or convert to market
- MISSED_FILL_TIMEOUT: 10 min -> escalate

Also reconciles position counts: our DB vs Alpaca's reported positions.
"""

from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ReconciliationStatus(str, Enum):
    MATCHED = "MATCHED"
    PARTIAL_FILL = "PARTIAL_FILL"
    MISSED_FILL = "MISSED_FILL"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    PENDING = "PENDING"
    ERROR = "ERROR"


class ReconciliationResult(BaseModel):
    order_id: str
    symbol: Optional[str] = None
    direction: Optional[str] = None
    expected_price: Optional[float] = None
    actual_fill_price: Optional[float] = None
    expected_quantity: Optional[float] = None
    actual_filled_quantity: Optional[float] = None
    slippage_bps: Optional[float] = None
    status: ReconciliationStatus
    discrepancy_notes: Optional[str] = None
    reconciled_at: datetime = None


class PositionReconciliationResult(BaseModel):
    our_positions: int
    alpaca_positions: int
    matched: bool
    discrepancies: list[dict] = []


class ReconciliationService:
    """
    Fill reconciliation service with configurable thresholds.
    
    Stores reconciliation results in Redis for fast access
    and logs critical discrepancies to audit trail + Discord.
    """

    # Thresholds (can be overridden via constructor)
    SLIPPAGE_WARNING_BPS = 50      # > 50 bps -> warning
    SLIPPAGE_CRITICAL_BPS = 200    # > 200 bps -> critical alert
    PARTIAL_FILL_TIMEOUT_S = 300   # 5 minutes
    MISSED_FILL_TIMEOUT_S = 600    # 10 minutes

    RESULTS_KEY = "noble:reconciliation:results"
    LAST_RUN_KEY = "noble:reconciliation:last_run"
    RESULTS_TTL_SECONDS = 86400  # v7.3: 24h TTL for results (was no TTL)

    def __init__(
        self,
        slippage_warning_bps: int = 50,
        slippage_critical_bps: int = 200,
    ):
        self._redis = None
        self.SLIPPAGE_WARNING_BPS = slippage_warning_bps
        self.SLIPPAGE_CRITICAL_BPS = slippage_critical_bps
        self._last_results: list[ReconciliationResult] = []
        self._last_position_reconciliation: Optional[PositionReconciliationResult] = None

    async def _get_client(self):
        """Get Redis client from the shared connection pool.

        v7.3: Uses the shared RedisConnectionManager instead of creating
        a per-service TCP connection.
        """
        if self._redis is None:
            try:
                from regime_platform.services.redis_pool import redis_pool
                self._redis = await redis_pool.get_client()
                if self._redis:
                    logger.info("Reconciler: Using shared Redis pool")
            except Exception as e:
                logger.error(f"Reconciler: Redis pool connection failed: {e}")
        return self._redis

    async def close(self):
        """Release Redis client reference (pool manages the connection)."""
        self._redis = None

    @property
    def is_simulation(self) -> bool:
        """Check if we're in simulation mode (no real Alpaca API)."""
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            return executor_factory.is_simulation
        except Exception:
            return True

    def _calculate_slippage_bps(self, expected: float, actual: float) -> float:
        """Calculate slippage in basis points."""
        if expected == 0:
            return 0.0
        return ((actual - expected) / expected) * 10000

    async def reconcile_order(
        self,
        order_id: str,
        expected_price: Optional[float] = None,
        expected_quantity: Optional[float] = None,
        direction: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> ReconciliationResult:
        """
        Reconcile a single order by checking its fill status against expectations.
        """
        result = ReconciliationResult(
            order_id=order_id,
            symbol=symbol,
            direction=direction,
            expected_price=expected_price,
            expected_quantity=expected_quantity,
            status=ReconciliationStatus.PENDING,
            reconciled_at=datetime.now(timezone.utc),
        )

        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()

            if not api:
                result.status = ReconciliationStatus.ERROR
                result.discrepancy_notes = "Alpaca API not available (simulation mode)"
                return result

            # Get order from Alpaca
            try:
                order = await api.get_order_by_client_id(order_id)
            except Exception:
                try:
                    order = await api.get_order(order_id)
                except Exception as e:
                    result.status = ReconciliationStatus.ERROR
                    result.discrepancy_notes = f"Order not found: {str(e)[:100]}"
                    return result

            # Extract fill info
            filled_qty = float(order.filled_qty or 0)
            filled_price = float(order.filled_avg_price or 0)
            order_qty = float(order.qty or 0)
            order_status = order.status

            result.actual_filled_quantity = filled_qty
            result.actual_fill_price = filled_price
            result.symbol = result.symbol or order.symbol
            result.direction = result.direction or order.side

            # Check fill status
            if order_status == "filled":
                # Full fill - check price deviation
                if expected_price and filled_price:
                    result.slippage_bps = self._calculate_slippage_bps(
                        expected_price, filled_price
                    )

                    if abs(result.slippage_bps) > self.SLIPPAGE_CRITICAL_BPS:
                        result.status = ReconciliationStatus.PRICE_DEVIATION
                        result.discrepancy_notes = (
                            f"CRITICAL slippage: {result.slippage_bps:.1f} bps "
                            f"(expected ${expected_price:.2f}, filled ${filled_price:.2f})"
                        )
                        await self._handle_critical_discrepancy(result)
                    elif abs(result.slippage_bps) > self.SLIPPAGE_WARNING_BPS:
                        result.status = ReconciliationStatus.PRICE_DEVIATION
                        result.discrepancy_notes = (
                            f"Warning slippage: {result.slippage_bps:.1f} bps "
                            f"(expected ${expected_price:.2f}, filled ${filled_price:.2f})"
                        )
                    else:
                        result.status = ReconciliationStatus.MATCHED

                else:
                    result.status = ReconciliationStatus.MATCHED

            elif order_status == "partially_filled":
                result.status = ReconciliationStatus.PARTIAL_FILL
                result.discrepancy_notes = (
                    f"Partial fill: {filled_qty}/{order_qty} shares "
                    f"at ${filled_price:.2f}"
                )

            elif order_status in ("rejected", "canceled", "expired"):
                result.status = ReconciliationStatus.MISSED_FILL
                result.discrepancy_notes = (
                    f"Order {order_status}: expected {expected_quantity} @ ${expected_price}"
                )

            else:
                # Still pending/new
                result.status = ReconciliationStatus.PENDING
                result.discrepancy_notes = f"Order status: {order_status}"

        except Exception as e:
            result.status = ReconciliationStatus.ERROR
            result.discrepancy_notes = f"Reconciliation error: {str(e)[:200]}"
            logger.error(f"Reconciler: Error reconciling order {order_id}: {e}")

        # Save result
        self._last_results.append(result)
        if len(self._last_results) > 1000:
            self._last_results = self._last_results[-500:]

        # Audit log non-matched results
        if result.status not in (ReconciliationStatus.MATCHED, ReconciliationStatus.PENDING):
            await self._audit_reconciliation(result)

        return result

    async def reconcile_all_recent(self) -> list[ReconciliationResult]:
        """
        Reconcile all orders from the last 24 hours.
        This is called by the periodic reconciliation scheduler.
        """
        results = []
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()

            if not api:
                logger.warning("Reconciler: Alpaca API not available")
                return results

            # Get recent orders (params vary by alpaca-py version, so fetch unfiltered)
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            try:
                orders = await api.get_orders(
                    after=since.isoformat(),
                    limit=500,
                    direction="desc",
                )
            except TypeError:
                # Fallback for alpaca-py versions that don't support these params
                orders = await api.get_orders()

            for order in orders:
                result = await self.reconcile_order(
                    order_id=order.id,
                    symbol=order.symbol,
                    direction=order.side,
                    expected_quantity=float(order.qty or 0),
                )
                results.append(result)

            # Update last run timestamp (Redis + Supabase write-through)
            now_iso = datetime.now(timezone.utc).isoformat()
            client = await self._get_client()
            if client:
                try:
                    await client.set(self.LAST_RUN_KEY, now_iso)
                except Exception:
                    pass

            # v7.3: Also persist last_run to Supabase nt_system_config (durable)
            try:
                from regime_platform.core.settings import settings as _settings
                if _settings.SUPABASE_URL and _settings.SUPABASE_KEY:
                    import httpx
                    async with httpx.AsyncClient(timeout=5.0) as sb_client:
                        resp = await sb_client.post(
                            f"{_settings.SUPABASE_URL}/rest/v1/nt_system_config",
                            headers={
                                "apikey": _settings.SUPABASE_KEY,
                                "Authorization": f"Bearer {_settings.SUPABASE_KEY}",
                                "Content-Type": "application/json",
                                "Prefer": "resolution=merge-duplicates",
                            },
                            json={
                                "key": "noble:reconciliation:last_run",
                                "value": now_iso,
                                "category": "operational",
                            },
                        )
                        if resp.status_code not in (200, 201, 204):
                            logger.debug(f"Reconciler: Supabase last_run write failed: {resp.status_code}")
            except Exception as e:
                logger.debug(f"Reconciler: Supabase write-through failed: {e}")

            self._last_results = results
            logger.info(f"Reconciler: Reconciled {len(results)} orders")

        except Exception as e:
            logger.error(f"Reconciler: reconcile_all_recent failed: {e}")

        return results

    async def reconcile_positions(self, user_id: str = "default") -> PositionReconciliationResult:
        """
        Compare our DB positions against Alpaca's reported positions.
        """
        result = PositionReconciliationResult(
            our_positions=0,
            alpaca_positions=0,
            matched=True,
            discrepancies=[],
        )

        try:
            # Get Alpaca positions
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()

            alpaca_symbols = set()
            if api:
                positions = await api.list_positions()
                for pos in positions:
                    alpaca_symbols.add(pos.symbol)
                    result.alpaca_positions += 1

            # Get our DB positions (via Supabase)
            our_symbols = set()
            try:
                import httpx
                from regime_platform.core.settings import settings
                if settings.SUPABASE_URL and settings.SUPABASE_KEY:
                    async with httpx.AsyncClient(
                        base_url=f"{settings.SUPABASE_URL}/rest/v1",
                        headers={
                            "apikey": settings.SUPABASE_KEY,
                            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                        },
                        timeout=10.0,
                    ) as client:
                        resp = await client.get(
                            "/tradeRecommendation",
                            params={
                                "user_id": f"eq.{user_id}",
                                "status": "eq.executing",
                                "select": "symbol",
                            },
                        )
                        if resp.status_code == 200:
                            for rec in resp.json():
                                our_symbols.add(rec.get("symbol", "").upper())
                                result.our_positions += 1
            except Exception as e:
                logger.error(f"Reconciler: Failed to get DB positions: {e}")

            # Compare
            missing_in_alpaca = our_symbols - alpaca_symbols
            missing_in_db = alpaca_symbols - our_symbols

            if missing_in_alpaca:
                result.matched = False
                result.discrepancies.append({
                    "type": "position_in_db_not_in_alpaca",
                    "symbols": list(missing_in_alpaca),
                })

            if missing_in_db:
                result.matched = False
                result.discrepancies.append({
                    "type": "position_in_alpaca_not_in_db",
                    "symbols": list(missing_in_db),
                })

            self._last_position_reconciliation = result

        except Exception as e:
            logger.error(f"Reconciler: Position reconciliation failed: {e}")
            result.matched = False
            result.discrepancies.append({"type": "error", "message": str(e)[:200]})

        return result

    async def get_last_results(self) -> list[dict]:
        """Get last reconciliation results."""
        return [r.model_dump(mode="json") for r in self._last_results[-50:]]

    async def get_status(self) -> dict:
        """Get reconciliation service status."""
        last_run = None
        client = await self._get_client()
        if client:
            try:
                last_run = await client.get(self.LAST_RUN_KEY)
            except Exception:
                pass

        # Count results by status
        status_counts = {}
        for r in self._last_results:
            s = r.status.value
            status_counts[s] = status_counts.get(s, 0) + 1

        return {
            "last_run": last_run,
            "total_reconciled": len(self._last_results),
            "status_counts": status_counts,
            "position_reconciliation": (
                self._last_position_reconciliation.model_dump(mode="json")
                if self._last_position_reconciliation
                else None
            ),
        }

    async def _handle_critical_discrepancy(self, result: ReconciliationResult):
        """Handle critical slippage - alert + audit + Discord."""
        logger.warning(
            f"Reconciler: CRITICAL SLIPPAGE - order {result.order_id} "
            f"{result.slippage_bps:.1f} bps on {result.symbol}"
        )

        # Discord alert
        try:
            from notifications.discord import discord_notifier
            await discord_notifier.send_status(
                f"**CRITICAL SLIPPAGE**: Order {result.order_id} on {result.symbol} - "
                f"{result.slippage_bps:.1f} bps slippage "
                f"(expected ${result.expected_price}, filled ${result.actual_fill_price})"
            )
        except Exception:
            pass

    async def _audit_reconciliation(self, result: ReconciliationResult):
        """Log reconciliation result to audit trail."""
        try:
            from regime_platform.services.operational.audit_logger import (
                audit_logger, AuditEventType
            )

            event_type = (
                AuditEventType.RECONCILIATION_FAILED
                if result.status in (
                    ReconciliationStatus.MISSED_FILL,
                    ReconciliationStatus.PRICE_DEVIATION,
                    ReconciliationStatus.ERROR,
                )
                else AuditEventType.RECONCILIATION_PASSED
            )

            await audit_logger.log_event(
                event_type=event_type,
                user_id="system",
                symbol=result.symbol,
                order_id=result.order_id,
                direction=result.direction,
                price=result.actual_fill_price,
                metadata={
                    "status": result.status.value,
                    "expected_price": result.expected_price,
                    "actual_price": result.actual_fill_price,
                    "slippage_bps": result.slippage_bps,
                    "notes": result.discrepancy_notes,
                },
            )
        except Exception as e:
            logger.error(f"Reconciler: Audit log failed: {e}")


# Module-level singleton
reconciler = ReconciliationService()
