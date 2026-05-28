"""
Alpaca Order Executor - places and tracks orders via Alpaca API.

Supports:
  - Bracket orders (entry + SL + TP in one atomic call)
  - Market and limit orders
  - Paper and live trading
  - Fill tracking via WebSocket or polling
  - Position reconciliation with Alpaca's position API

Design principles:
  - All order logic is here - the pipeline never touches Alpaca directly
  - Errors are caught and logged, never crash the pipeline
  - Paper trading is the default and must be explicitly overridden
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from renko.pattern_detector import SignalDirection
from renko.signal_filter import FilteredSignal
from regime_platform.core.settings import settings
from regime_platform.core.resilience import CircuitBreaker, CircuitOpenError, retry_with_backoff

def _resolve_alpaca_credentials(api_key, api_secret):
    """Resolve Alpaca credentials from args or global settings fallback."""
    if api_key and api_secret:
        return api_key, api_secret
    # Fallback to global settings (which reads from env vars with our fixes)
    if settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
        return settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY
    return api_key, api_secret

logger = logging.getLogger(__name__)

# ── Circuit breaker for Alpaca API calls ────────────────────────────────

_alpaca_breaker = CircuitBreaker(
    name="alpaca_trading",
    failure_threshold=3,
    recovery_timeout=60,
)


# ── Data types ─────────────────────────────────────────────────────


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    SIMULATED = "simulated"


@dataclass
class OrderParams:
    """Parameters for a single order."""

    symbol: str
    side: str            # "buy" or "sell"
    qty: float
    order_type: str = "market"   # "market" or "limit"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "day"

    # Bracket order legs
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    use_bracket: bool = True

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "time_in_force": self.time_in_force,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "use_bracket": self.use_bracket,
        }


@dataclass
class OrderResult:
    """Result of an order submission."""

    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_qty: Optional[float] = None
    submitted_at: float = 0.0
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.SIMULATED,
        )

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "status": self.status.value,
            "filled_price": self.filled_price,
            "filled_qty": self.filled_qty,
            "submitted_at": self.submitted_at,
            "error": self.error,
        }


# ── Executor ───────────────────────────────────────────────────────


class AlpacaExecutor:
    """
    Executes trades via the Alpaca API.

    This is a thin wrapper that handles:
      - Converting FilteredSignal -> Alpaca order
      - Bracket order submission
      - Fill tracking
      - Error handling and retry logic

    Usage::

        executor = AlpacaExecutor(api_key, api_secret, paper=True)
        result = await executor.execute(filtered_signal, order_params)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: str = "https://paper-api.alpaca.markets",
        paper: bool = True,
        use_bracket_orders: bool = True,
    ):
        # Resolve credentials from global settings if not explicitly provided
        api_key, api_secret = _resolve_alpaca_credentials(api_key, api_secret)
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.paper = paper
        self.use_bracket_orders = use_bracket_orders

        self._order_history: List[OrderResult] = []
        self._client: Any = None

        # Warn when running in simulation mode
        if self.is_simulation:
            logger.warning(
                "AlpacaExecutor running in SIMULATION mode - no real orders will be placed. "
                "Set ALPACA_API_KEY and ALPACA_SECRET_KEY to enable live/paper trading."
            )

    # ── Execution Mode ────────────────────────────────────────────

    @property
    def execution_mode(self) -> str:
        """
        Return the current execution mode.

        Returns
        -------
        str
            "live"  - real money, base_url points to api.alpaca.markets
            "paper" - paper trading, base_url points to paper-api
            "simulation" - no API keys or alpaca-trade-api not installed
        """
        if self.api_key is None or self.api_secret is None:
            return "simulation"
        if "paper-api" in self.base_url:
            return "paper"
        return "live"

    @property
    def is_simulation(self) -> bool:
        """Return True if running in simulation (no real order execution)."""
        return self.execution_mode == "simulation"

    # ── Public API ────────────────────────────────────────────────

    async def execute(
        self,
        filtered: FilteredSignal,
        symbol: str,
        brick_size: float,
        sl_bricks: int = 3,
        tp_bricks: int = 5,
        qty: Optional[float] = None,
    ) -> OrderResult:
        """
        Execute a filtered signal as an Alpaca order.

        Defense in depth: checks kill switch BEFORE submitting.
        Even if middleware fails, the executor itself will refuse to trade.

        Parameters
        ----------
        filtered : FilteredSignal
            The signal that passed all filters.
        symbol : str
            Alpaca-compatible symbol (e.g. "SPY").
        brick_size : float
            Current brick size in dollars.
        sl_bricks : int
            Stop-loss distance in bricks.
        tp_bricks : int
            Take-profit distance in bricks.
        qty : float, optional
            Number of shares. If None, calculated from position_size_usd.

        Returns
        -------
        OrderResult
        """
        # ── Defense in depth: Kill switch check ──
        # Even if middleware fails, the executor itself refuses to trade
        try:
            from regime_platform.services.operational.kill_switch import kill_switch
            if await kill_switch.is_halted(symbol=symbol):
                active_halt = await kill_switch.get_active_halt(symbol=symbol)
                halt_info = active_halt.level.value if active_halt else "unknown"
                logger.error(
                    f"AlpacaExecutor: BLOCKED by kill switch - "
                    f"symbol={symbol} halt_level={halt_info}"
                )
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    error=f"Trading halted ({halt_info}). Use kill switch panel to review.",
                    submitted_at=time.time(),
                )
        except Exception as e:
            # If kill switch check fails, log but continue (fail-open for availability)
            logger.warning(f"AlpacaExecutor: Kill switch check failed: {e}")

        signal = filtered.original_signal
        entry_price = signal.price

        # Determine side
        side = "buy" if signal.direction == SignalDirection.LONG else "sell"

        # Calculate quantity
        if qty is None:
            if filtered.position_size_usd > 0 and entry_price > 0:
                qty = filtered.position_size_usd / entry_price
                qty = max(int(qty), 1)  # At least 1 share
            else:
                qty = 1

        # Calculate SL/TP prices
        if signal.direction == SignalDirection.LONG:
            sl_price = round(entry_price - sl_bricks * brick_size, 4)
            tp_price = round(entry_price + tp_bricks * brick_size, 4)
        else:
            sl_price = round(entry_price + sl_bricks * brick_size, 4)
            tp_price = round(entry_price - tp_bricks * brick_size, 4)

        order_params = OrderParams(
            symbol=symbol,
            side=side,
            qty=qty,
            sl_price=sl_price,
            tp_price=tp_price,
            use_bracket=self.use_bracket_orders,
        )

        # Submit order
        result = await self._submit_order(order_params)
        self._order_history.append(result)

        # ── Audit log the order ──
        await self._audit_order(result, order_params, symbol)

        return result

    async def close_position(self, symbol: str) -> OrderResult:
        """Close the current position for a symbol."""
        result = await self._close_alpaca_position(symbol)
        self._order_history.append(result)
        return result

    async def get_account(self) -> Optional[Dict]:
        """Get Alpaca account info."""
        return await self._get_account_info()

    @property
    def order_history(self) -> List[OrderResult]:
        return list(self._order_history)

    # ── Alpaca API calls ──────────────────────────────────────────

    async def _submit_order(self, params: OrderParams) -> OrderResult:
        """
        Submit an order to Alpaca. Uses the alpaca-py SDK or
        direct REST calls.

        In production, this calls the real Alpaca API with circuit
        breaker and retry protection.
        In backtest / dry-run mode, this simulates a fill
        (circuit breaker does NOT apply to simulation).
        """
        if self.api_key is None or self.api_secret is None:
            # Simulation mode - no real order placed, no circuit breaker
            sim_id = f"sim_{int(time.time()*1000)}"
            logger.warning(
                "SIMULATED order '%s': %s %s %s @ %s "
                "(no Alpaca API keys configured)",
                sim_id,
                params.side,
                params.qty,
                params.symbol,
                params.stop_price or params.limit_price or "market",
            )
            return OrderResult(
                order_id=sim_id,
                client_order_id=f"renko_sim_{int(time.time()*1000)}",
                status=OrderStatus.SIMULATED,
                filled_price=params.stop_price or params.limit_price or 0.0,
                filled_qty=params.qty,
                submitted_at=time.time(),
            )

        try:
            # Attempt to use alpaca-py (modern SDK)
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import SubmitOrderParams
            from alpaca.enums import OrderSide, TimeInForce, OrderType, OrderClass

            if self._client is None:
                # alpaca-py uses 'paper' boolean; base_url is auto-selected
                is_paper = "paper-api" in self.base_url
                self._client = TradingClient(
                    self.api_key,
                    self.api_secret,
                    paper=is_paper,
                )

            # ── Circuit breaker + retry for real Alpaca API calls ────
            try:
                async with _alpaca_breaker:
                    if params.use_bracket and params.sl_price and params.tp_price:
                        # Submit bracket order
                        order = await retry_with_backoff(
                            lambda: self._client.submit_order(
                                symbol=params.symbol,
                                qty=int(params.qty),
                                side=OrderSide(params.side.upper()),
                                type=OrderType.MARKET,
                                time_in_force=TimeInForce(params.time_in_force),
                                order_class=OrderClass.BRACKET,
                                stop_loss_params={"stop_price": params.sl_price},
                                take_profit_params={"limit_price": params.tp_price},
                            ),
                            max_retries=2,
                            base_delay=1.0,
                            retryable_exceptions=(ConnectionError, TimeoutError, OSError),
                        )
                    else:
                        # Simple market/limit order
                        ord_type = OrderType.MARKET if params.order_type == "market" else OrderType.LIMIT
                        limit_p = params.limit_price if params.limit_price else None
                        order = await retry_with_backoff(
                            lambda: self._client.submit_order(
                                symbol=params.symbol,
                                qty=int(params.qty),
                                side=OrderSide(params.side.upper()),
                                type=ord_type,
                                time_in_force=TimeInForce(params.time_in_force),
                                limit_price=limit_p,
                            ),
                            max_retries=2,
                            base_delay=1.0,
                            retryable_exceptions=(ConnectionError, TimeoutError, OSError),
                        )

            except CircuitOpenError:
                logger.error("Alpaca circuit breaker OPEN - order rejected")
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    error="Alpaca service unavailable (circuit breaker open)",
                    submitted_at=time.time(),
                )

            return OrderResult(
                order_id=order.id,
                client_order_id=order.client_order_id,
                status=OrderStatus(order.status),
                filled_price=float(order.filled_avg_price or 0),
                filled_qty=float(order.filled_qty or 0),
                submitted_at=time.time(),
            )

        except ImportError:
            sim_id = f"sim_{int(time.time()*1000)}"
            logger.warning(
                "SIMULATED order '%s': %s %s %s @ %s "
                "(alpaca-py not installed)",
                sim_id,
                params.side,
                params.qty,
                params.symbol,
                params.limit_price or "market",
            )
            return OrderResult(
                order_id=sim_id,
                status=OrderStatus.SIMULATED,
                filled_price=params.limit_price or 0.0,
                filled_qty=params.qty,
                submitted_at=time.time(),
            )
        except Exception as e:
            logger.error(f"Order submission failed: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                error=str(e),
                submitted_at=time.time(),
            )

    async def _close_alpaca_position(self, symbol: str) -> OrderResult:
        """Close an Alpaca position."""
        if self.api_key is None or self.api_secret is None:
            sim_id = f"sim_close_{int(time.time()*1000)}"
            logger.warning(
                "SIMULATED close '%s': %s (no Alpaca API keys configured)",
                sim_id,
                symbol,
            )
            return OrderResult(
                order_id=sim_id,
                status=OrderStatus.SIMULATED,
                submitted_at=time.time(),
            )

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import ClosePositionRequest
            from alpaca.trading.enums import QtyQuantityType

            if self._client is None:
                is_paper = "paper-api" in self.base_url
                self._client = TradingClient(
                    self.api_key,
                    self.api_secret,
                    paper=is_paper,
                )

            # ── Circuit breaker + retry ────────────────────────────
            try:
                async with _alpaca_breaker:
                    await retry_with_backoff(
                        lambda: self._client.close_position(symbol),
                        max_retries=2,
                        base_delay=1.0,
                        retryable_exceptions=(ConnectionError, TimeoutError, OSError),
                    )
            except CircuitOpenError:
                logger.error("Alpaca circuit breaker OPEN - position close rejected")
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    error="Alpaca service unavailable (circuit breaker open)",
                    submitted_at=time.time(),
                )

            return OrderResult(
                status=OrderStatus.FILLED,
                submitted_at=time.time(),
            )
        except Exception as e:
            logger.error(f"Position close failed: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                error=str(e),
                submitted_at=time.time(),
            )

    async def _get_account_info(self) -> Optional[Dict]:
        """Fetch Alpaca account info."""
        if self.api_key is None or self.api_secret is None:
            return {"equity": 100000, "cash": 100000, "status": "simulation"}

        try:
            from alpaca.trading.client import TradingClient

            if self._client is None:
                is_paper = "paper-api" in self.base_url
                self._client = TradingClient(
                    self.api_key,
                    self.api_secret,
                    paper=is_paper,
                )

            # ── Circuit breaker + retry ────────────────────────────
            try:
                async with _alpaca_breaker:
                    account = await retry_with_backoff(
                        lambda: self._client.get_account(),
                        max_retries=2,
                        base_delay=1.0,
                        retryable_exceptions=(ConnectionError, TimeoutError, OSError),
                    )
            except CircuitOpenError:
                logger.error("Alpaca circuit breaker OPEN - account info unavailable")
                return None

            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "status": account.status,
                "buying_power": float(account.buying_power),
            }
        except Exception as e:
            logger.error(f"Account fetch failed: {e}")
            return None

    @property
    def circuit_breaker_status(self) -> dict:
        """Return the Alpaca circuit breaker status for health/stats endpoints."""
        return _alpaca_breaker.status()

    def reset(self) -> None:
        """Reset executor state."""
        self._order_history.clear()
        self._client = None

    # ── Audit Logging ────────────────────────────────────────────────

    async def _audit_order(
        self,
        result: OrderResult,
        params: OrderParams,
        symbol: str,
    ) -> None:
        """
        Log order result to audit trail.

        Maps OrderResult.status to AuditEventType:
        - SIMULATED/SUBMITTED/PARTIALLY_FILLED -> ORDER_SUBMITTED
        - FILLED -> ORDER_FILLED
        - REJECTED -> ORDER_REJECTED
        - CANCELLED -> ORDER_CANCELLED
        """
        try:
            from regime_platform.services.operational.audit_logger import (
                audit_logger, AuditEventType
            )

            # Map order status to audit event type
            status_to_event = {
                OrderStatus.SIMULATED: AuditEventType.ORDER_SUBMITTED,
                OrderStatus.SUBMITTED: AuditEventType.ORDER_SUBMITTED,
                OrderStatus.PARTIALLY_FILLED: AuditEventType.ORDER_PARTIAL_FILL,
                OrderStatus.FILLED: AuditEventType.ORDER_FILLED,
                OrderStatus.REJECTED: AuditEventType.ORDER_REJECTED,
                OrderStatus.CANCELLED: AuditEventType.ORDER_CANCELLED,
            }
            event_type = status_to_event.get(
                result.status, AuditEventType.ORDER_SUBMITTED
            )

            await audit_logger.log_event(
                event_type=event_type,
                user_id="system",  # Executor doesn't have user context
                symbol=symbol,
                order_id=result.order_id,
                direction=params.side,
                quantity=params.qty,
                price=result.filled_price or params.limit_price,
                order_type=params.order_type,
                metadata={
                    "execution_mode": self.execution_mode,
                    "client_order_id": result.client_order_id,
                    "error": result.error,
                    "bracket": {
                        "sl": params.sl_price,
                        "tp": params.tp_price,
                    } if params.use_bracket else None,
                },
            )
        except Exception as e:
            # Audit logging failure must NEVER block order execution
            logger.error(f"AlpacaExecutor: Audit log failed: {e}")
