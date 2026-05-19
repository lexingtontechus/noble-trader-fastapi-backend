"""
Renko Pipeline — orchestrates the full HFT pipeline from tick to trade.

Pipeline flow:
    Tick → Brick Engine → Swing Classifier → Pattern Detector →
    Signal Filter → Risk Manager → Executor → Trade Journal

The pipeline is event-driven: each tick is processed synchronously
through all stages. This ensures deterministic ordering and makes
backtesting identical to live trading.

Integration with Noble Trader Agent:
    - HMM regime detection feeds into Signal Filter (regime gate)
    - Kelly sizing from Trade Journal stats feeds into Signal Filter
    - Risk metrics feed back into the existing RiskEngine
    - Strategy Evolution can optimise pipeline config parameters
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Generator, List, Optional

from renko.brick_engine import RenkoBrickEngine, Brick, BrickDirection
from renko.swing_classifier import SwingClassifier, ClassifiedBrick, SwingLabel
from renko.pattern_detector import PatternDetector, PatternSignal, SignalDirection
from renko.signal_filter import SignalFilter, FilteredSignal
from renko.risk_manager import RiskManager, RiskParams, ActivePosition, TradeResult, PositionStatus
from renko.executor import AlpacaExecutor, OrderResult
from renko.trade_journal import TradeJournal, TradeRecord
from renko.config import RenkoConfig

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """Immutable snapshot of the pipeline's current state."""
    brick_count: int = 0
    last_brick_direction: Optional[str] = None
    last_swing_label: Optional[str] = None
    bull_run_count: int = 0
    bear_run_count: int = 0
    active_position: bool = False
    session_trades: int = 0
    session_pnl_bricks: float = 0.0
    total_trades: int = 0
    total_pnl_bricks: float = 0.0

    def to_dict(self) -> dict:
        return {
            "brick_count": self.brick_count,
            "last_brick_direction": self.last_brick_direction,
            "last_swing_label": self.last_swing_label,
            "bull_run_count": self.bull_run_count,
            "bear_run_count": self.bear_run_count,
            "active_position": self.active_position,
            "session_trades": self.session_trades,
            "session_pnl_bricks": self.session_pnl_bricks,
            "total_trades": self.total_trades,
            "total_pnl_bricks": self.total_pnl_bricks,
        }


class RenkoPipeline:
    """
    End-to-end Renko HFT pipeline.

    Usage::

        config = RenkoConfig(symbol="SPY", brick_size=0.50)
        pipeline = RenkoPipeline(config)

        # Process a tick
        result = pipeline.process_tick(price=450.25)

        # Get state
        state = pipeline.get_state()

        # Backtest
        results = pipeline.backtest(prices, timestamps)
    """

    def __init__(
        self,
        config: Optional[RenkoConfig] = None,
        on_signal: Optional[Callable[[FilteredSignal], None]] = None,
        on_trade: Optional[Callable[[TradeResult], None]] = None,
        on_brick: Optional[Callable[[Brick], None]] = None,
    ):
        self.config = config or RenkoConfig()
        self._on_signal = on_signal
        self._on_trade = on_trade
        self._on_brick = on_brick

        # Initialize pipeline components
        self.brick_engine = RenkoBrickEngine(
            brick_size=self.config.get_brick_size(),
            reversal_bricks=self.config.reversal_bricks,
        )
        self.swing_classifier = SwingClassifier(
            swing_lookback=self.config.swing_lookback,
            min_swing_distance_bricks=self.config.min_swing_distance_bricks,
        )
        self.pattern_detector = PatternDetector(
            bull_trigger_n=self.config.bull_trigger_n,
            bear_trigger_n=self.config.bear_trigger_n,
            velocity_threshold=self.config.velocity_threshold,
            double_top_bricks=self.config.double_top_bricks,
            consolidation_max_mix=self.config.consolidation_max_mix,
        )
        self.signal_filter = SignalFilter(
            session_start=self.config.session_start,
            session_end=self.config.session_end,
            skip_lunch=self.config.skip_lunch,
            lunch_start=self.config.lunch_start,
            lunch_end=self.config.lunch_end,
            max_trades_per_session=self.config.max_trades_per_session,
            max_daily_loss_bricks=self.config.max_daily_loss_bricks,
            max_consecutive_losses=self.config.max_consecutive_losses,
            cooldown_seconds=self.config.cooldown_seconds,
            min_brick_velocity=self.config.min_brick_velocity,
            regime_gate=self.config.regime_gate,
            timezone=self.config.timezone,
        )
        self.risk_manager = RiskManager(
            params=RiskParams(
                sl_bricks=self.config.sl_bricks,
                tp_bricks=self.config.tp_bricks,
                trailing_stop=self.config.trailing_stop,
                trail_after_bricks=self.config.trail_after_bricks,
                trail_distance_bricks=self.config.trail_distance_bricks,
                time_stop_bricks=self.config.time_stop_bricks,
                partial_exit_pct=self.config.partial_exit_pct,
                partial_exit_bricks=self.config.partial_exit_bricks,
                slippage_bps=self.config.slippage_bps,
                commission_bps=self.config.commission_bps,
                spread_bps=self.config.spread_bps,
                oco_priority=self.config.oco_priority,
            )
        )
        self.executor = AlpacaExecutor(
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
            base_url=self.config.base_url,
            paper=self.config.paper_trading,
            use_bracket_orders=self.config.use_bracket_orders,
        )
        self.journal = TradeJournal()

        # External state (set by caller)
        self._current_regime: Optional[str] = None
        self._current_equity: float = 100000.0

        # Internal tracking
        self._last_signal: Optional[PatternSignal] = None
        self._last_filtered: Optional[FilteredSignal] = None
        self._pending_signal: Optional[FilteredSignal] = None
        self._brick_velocity: float = 0.0

    # ── Public API ────────────────────────────────────────────────

    def process_tick(self, price: float, ts: Optional[float] = None) -> Dict[str, Any]:
        """
        Process a single tick through the entire pipeline.

        Returns a dict with any events that occurred:
          - bricks_created: list of new bricks
          - classified: list of classified bricks
          - signal: pattern signal (if any)
          - filtered: filtered signal (if any)
          - trade_result: trade close result (if any)
          - order_result: order submission result (if any)
        """
        if ts is None:
            ts = time.time()

        result: Dict[str, Any] = {
            "bricks_created": [],
            "classified": [],
            "signal": None,
            "filtered": None,
            "trade_result": None,
            "order_result": None,
        }

        # Stage 1: Brick Engine
        new_bricks = self.brick_engine.process_tick(price, ts)
        if not new_bricks:
            return result

        result["bricks_created"] = [b.to_dict() for b in new_bricks]

        for brick in new_bricks:
            if self._on_brick:
                self._on_brick(brick)

            # Stage 2: Swing Classifier
            classified = self.swing_classifier.classify(brick)
            result["classified"].append(classified.to_dict())

            # Update brick velocity
            self._brick_velocity = self.swing_classifier.get_brick_velocity()

            # Stage 2.5: Risk Manager check (on every brick)
            if self.risk_manager.has_position:
                trade_result = self.risk_manager.evaluate(brick)
                if trade_result is not None:
                    result["trade_result"] = trade_result.to_dict()
                    self._handle_trade_close(trade_result)
                    continue  # Position closed, skip pattern detection

            # Stage 3: Pattern Detector
            signal = self.pattern_detector.detect(classified, self._brick_velocity)
            if signal is None:
                continue

            result["signal"] = signal.to_dict()
            self._last_signal = signal

            # Stage 4: Signal Filter
            kelly = self.journal.get_kelly_estimate(last_n=50)
            filtered = self.signal_filter.evaluate(
                signal=signal,
                regime=self._current_regime,
                brick_velocity=self._brick_velocity,
                equity=self._current_equity,
                brick_size=self.config.get_brick_size(),
                kelly_fraction=kelly,
            )

            if filtered is None:
                continue  # Signal rejected by filters

            result["filtered"] = filtered.to_dict()
            self._last_filtered = filtered
            if self._on_signal:
                self._on_signal(filtered)

            # Stage 5: Open position (if none active)
            if not self.risk_manager.has_position:
                self._open_position(filtered, brick)

        return result

    def set_regime(self, regime: str) -> None:
        """Set the current HMM regime (called externally)."""
        self._current_regime = regime

    def set_equity(self, equity: float) -> None:
        """Set the current account equity (called externally)."""
        self._current_equity = equity

    def get_state(self) -> PipelineState:
        """Get a snapshot of the pipeline's current state."""
        session = self.signal_filter.session_stats
        journal_stats = self.journal.get_stats()

        last_classified = self.swing_classifier.last_classified

        return PipelineState(
            brick_count=self.brick_engine.brick_count,
            last_brick_direction=(
                self.brick_engine.current_direction.value
                if self.brick_engine.current_direction else None
            ),
            last_swing_label=(
                last_classified.label.value if last_classified else None
            ),
            bull_run_count=getattr(self.pattern_detector, '_bull_run_count', 0),
            bear_run_count=getattr(self.pattern_detector, '_bear_run_count', 0),
            active_position=self.risk_manager.has_position,
            session_trades=session.get("trades_taken", 0),
            session_pnl_bricks=session.get("daily_pnl_bricks", 0.0),
            total_trades=journal_stats.get("total_trades", 0),
            total_pnl_bricks=journal_stats.get("total_pnl_bricks", 0.0),
        )

    def get_stats(self) -> Dict:
        """Get comprehensive pipeline statistics."""
        return {
            "config": self.config.to_dict(),
            "state": self.get_state().to_dict(),
            "session": self.signal_filter.session_stats,
            "journal": self.journal.get_stats(),
            "filter_rejections": len(self.signal_filter.rejected_signals),
        }

    def reset(self) -> None:
        """Reset all pipeline components."""
        self.brick_engine.reset()
        self.swing_classifier.reset()
        self.pattern_detector.reset()
        self.signal_filter.reset_session()
        self.risk_manager.reset()
        self.executor.reset()
        self.journal.reset()
        self._current_regime = None
        self._current_equity = 100000.0
        self._last_signal = None
        self._last_filtered = None
        self._pending_signal = None
        self._brick_velocity = 0.0

    # ── Backtest ──────────────────────────────────────────────────

    def backtest(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
    ) -> Dict:
        """
        Run a full backtest on historical prices.

        Parameters
        ----------
        prices : list[float]
            Historical price series.
        timestamps : list[float], optional
            Matching timestamps.
        regimes : list[str], optional
            Pre-computed regime labels aligned with prices.

        Returns
        -------
        dict with keys: stats, trades, bricks
        """
        self.reset()
        all_results = []

        for i, price in enumerate(prices):
            ts = timestamps[i] if timestamps else float(i)
            if regimes and i < len(regimes):
                self.set_regime(regimes[i])

            tick_result = self.process_tick(price, ts)
            all_results.append(tick_result)

        stats = self.get_stats()
        total_bricks = self.brick_engine.brick_count

        return {
            "stats": stats,
            "total_bricks": total_bricks,
            "total_ticks": len(prices),
            "trades": [t.to_dict() for t in self.journal.trades],
        }

    # ── Chunked Backtest (SSE streaming) ────────────────────────────

    def backtest_chunked(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
        chunk_size: int = 150,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Run a backtest on historical prices, yielding intermediate results
        after each chunk for SSE streaming.

        This is the same as ``backtest()`` but yields progress events
        after every ``chunk_size`` ticks so the frontend can render
        partial equity curves and running statistics.

        Yields
        ------
        dict
            Progress events with keys:
              - type: "progress" | "complete"
              - chunk / total_chunks (progress events only)
              - percent: 0-100 completion
              - trades_so_far: list of closed trades so far
              - stats_so_far: current journal stats
              - bricks_so_far: bricks processed so far
              - ticks_so_far: ticks processed so far

        The final event has type "complete" and contains the full result
        (identical to what ``backtest()`` returns).
        """
        self.reset()
        n_chunks = max(1, math.ceil(len(prices) / chunk_size))

        for chunk_idx in range(n_chunks):
            start = chunk_idx * chunk_size
            end = min(start + chunk_size, len(prices))

            for i in range(start, end):
                ts = timestamps[i] if timestamps else float(i)
                if regimes and i < len(regimes):
                    self.set_regime(regimes[i])
                self.process_tick(prices[i], ts)

            # Yield intermediate progress
            yield {
                "type": "progress",
                "chunk": chunk_idx + 1,
                "total_chunks": n_chunks,
                "percent": round(((chunk_idx + 1) / n_chunks) * 100),
                "trades_so_far": [t.to_dict() for t in self.journal.trades],
                "stats_so_far": self.journal.get_stats(),
                "bricks_so_far": self.brick_engine.brick_count,
                "ticks_so_far": end,
            }

        # Final event — identical shape to backtest() return value
        yield {
            "type": "complete",
            "stats": self.get_stats(),
            "total_bricks": self.brick_engine.brick_count,
            "total_ticks": len(prices),
            "trades": [t.to_dict() for t in self.journal.trades],
        }

    # ── Internal ──────────────────────────────────────────────────

    def _open_position(self, filtered: FilteredSignal, brick: Brick) -> None:
        """Open a new position from a filtered signal."""
        try:
            self.risk_manager.open_position(
                entry_price=filtered.original_signal.price,
                direction=filtered.original_signal.direction,
                brick_size=filtered.brick_size,
                entry_ts=brick.timestamp,
                entry_brick_idx=brick.index,
                qty=1.0,  # Will be refined by executor
            )
            # In live mode, also submit the order
            # (async — would need to be scheduled differently in production)
            logger.info(
                f"Position opened: {filtered.original_signal.direction.value} "
                f"at {filtered.original_signal.price:.2f}"
            )
        except RuntimeError as e:
            logger.warning(f"Cannot open position: {e}")

    def _handle_trade_close(self, result: TradeResult) -> None:
        """Handle a closed trade — update journal and filter stats."""
        self.signal_filter.record_trade_result(result.pnl_bricks)

        # Create journal record if we have filtered signal context
        if self._last_filtered is not None:
            record = self.journal.create_record(
                symbol=self.config.symbol,
                filtered=self._last_filtered,
                result=result,
                regime=self._current_regime,
            )
            self.journal.record(record)

        if self._on_trade:
            self._on_trade(result)

        logger.info(
            f"Trade closed: {result.status.value} PnL={result.pnl_bricks:.2f} bricks "
            f"(${result.pnl_dollars:.2f})"
        )
