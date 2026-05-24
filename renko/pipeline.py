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
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from renko.brick_engine import RenkoBrickEngine, Brick, BrickDirection
from renko.swing_classifier import SwingClassifier, ClassifiedBrick, SwingLabel
from renko.pattern_detector import PatternDetector, PatternSignal, SignalDirection
from renko.signal_filter import SignalFilter, FilteredSignal
from renko.risk_manager import RiskManager, RiskParams, ActivePosition, TradeResult, PositionStatus
from renko.executor import AlpacaExecutor, OrderResult
from renko.trade_journal import TradeJournal, TradeRecord
from renko.config import RenkoConfig

import hashlib
import numpy as np
from regime_platform.services.execution.market_impact import compute_market_impact
from regime_platform.services.execution.fill_probability import compute_fill_probability
from regime_platform.services.execution.borrow_financing import compute_total_financing_costs
from regime_platform.services.statistics.deflated_sharpe import deflated_sharpe_ratio
from regime_platform.services.statistics.bootstrap_ci import compute_all_bootstrap_cis
from regime_platform.services.statistics.multiple_testing import apply_all_corrections
from regime_platform.services.statistics.significance_tests import run_significance_tests

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
        # Build Masaniello sizer if sizing_mode=masaniello
        masaniello_sizer = None
        if self.config.sizing_mode.value == "masaniello":
            from regime_platform.core.position_sizer import DynamicMasanielloSizer, SizingConfig
            # quality_floor is an alias for regime_floor — use whichever is set
            regime_floor = self.config.masaniello_regime_floor
            if hasattr(self.config, 'masaniello_quality_floor') and self.config.masaniello_quality_floor != regime_floor:
                regime_floor = self.config.masaniello_quality_floor
            mcfg = SizingConfig(
                base_risk=self.config.masaniello_base_risk,
                min_risk=self.config.masaniello_min_risk,
                max_risk=self.config.masaniello_max_risk,
                min_prob=self.config.masaniello_min_prob,
                min_rr=self.config.masaniello_min_rr,
                max_drawdown=self.config.masaniello_max_drawdown,
                regime_floor=regime_floor,
                use_kelly_overlay=self.config.masaniello_use_kelly_overlay,
                batch_size=self.config.masaniello_batch_size,
                target_wins=self.config.masaniello_target_wins,
            )
            masaniello_sizer = DynamicMasanielloSizer(mcfg)

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
            sizing_mode=self.config.sizing_mode.value,
            masaniello_sizing_config=masaniello_sizer,
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
                oco_priority=self.config.oco_priority,
            ),
            slippage_bps=self.config.slippage_bps,
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
        self._current_regime_quality: float = 1.0   # From RegimeSnapshot.risk_multiplier
        self._current_regime_confidence: float = 0.75  # From RegimeSnapshot.confidence
        self._current_drawdown: float = 0.0  # Current batch drawdown fraction

        # Internal tracking
        self._last_signal: Optional[PatternSignal] = None
        self._last_filtered: Optional[FilteredSignal] = None
        self._pending_signal: Optional[FilteredSignal] = None
        self._pending_execution: Optional[dict] = None   # Signal info for router to execute
        self._pending_close: Optional[dict] = None        # Close info for router to execute
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
                regime_quality=self._current_regime_quality,
                regime_confidence=self._current_regime_confidence,
                current_drawdown=self._current_drawdown,
                atr_baseline=self.config.brick_size * 20,  # Approx ATR
                atr_current=self.config.brick_size * 20,
                default_win_rate=self.config.default_win_rate,
                sl_bricks=self.config.sl_bricks,
                tp_bricks=self.config.tp_bricks,
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

    def set_regime_quality(self, quality: float, confidence: float = 0.75) -> None:
        """Set the current regime quality and confidence (from RegimeSnapshot)."""
        self._current_regime_quality = quality
        self._current_regime_confidence = confidence

    def set_equity(self, equity: float) -> None:
        """Set the current account equity (called externally)."""
        self._current_equity = equity

    def set_drawdown(self, drawdown: float) -> None:
        """Set the current batch drawdown fraction (called externally)."""
        self._current_drawdown = drawdown

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
            "sizing_mode": self.config.sizing_mode.value,
        }

    def snapshot(self) -> dict:
        """
        Capture a serializable snapshot of the pipeline state for persistence.
        Includes: config, brick history, classified history, signal history,
        trade journal, regime, equity, and filter stats.
        """
        return {
            "config": self.config.to_dict(),
            "brick_count": self.brick_engine.brick_count,
            "current_direction": self.brick_engine.current_direction.value if self.brick_engine.current_direction else None,
            "current_price": self.brick_engine.last_brick.close_price if self.brick_engine.last_brick else None,
            "regime": self._current_regime,
            "equity": self._current_equity,
            "brick_velocity": self._brick_velocity,
            "session_stats": self.signal_filter.session_stats,
            "journal_stats": self.journal.get_stats(),
            "snapshot_ts": time.time(),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> 'RenkoPipeline':
        """
        Restore a pipeline from a persisted snapshot.
        Note: Full brick/trade history is NOT restored (only stats).
        The pipeline will need re-warmup for full brick history,
        but session stats and regime state are preserved.
        """
        config_data = data.get("config", {})
        config = RenkoConfig(**{k: v for k, v in config_data.items() if k in RenkoConfig.__dataclass_fields__})
        pipeline = cls(config)
        pipeline._current_regime = data.get("regime")
        pipeline._current_equity = data.get("equity", 100000.0)
        pipeline._brick_velocity = data.get("brick_velocity", 0.0)
        return pipeline

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
        self._pending_execution = None
        self._pending_close = None
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

        # Enable warmup mode for backtest (bypasses session/cooldown filters)
        self.signal_filter.set_warmup_mode(True)

        for i, price in enumerate(prices):
            ts = timestamps[i] if timestamps else float(i)
            if regimes and i < len(regimes):
                self.set_regime(regimes[i])

            tick_result = self.process_tick(price, ts)
            all_results.append(tick_result)

        # Disable warmup mode after backtest
        self.signal_filter.set_warmup_mode(False)

        stats = self.get_stats()
        total_bricks = self.brick_engine.brick_count

        return {
            "stats": stats,
            "total_bricks": total_bricks,
            "total_ticks": len(prices),
            "trades": [t.to_dict() for t in self.journal.trades],
        }

    def backtest_with_analytics(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
        n_trials: int = 1,
        confidence_level: float = 0.95,
    ) -> Dict:
        """
        Run a full backtest with statistical rigor and execution modeling analytics.
        
        Extends the basic backtest with:
          - Data hash for reproducibility (Phase 5)
          - Bootstrap confidence intervals (Phase 6)
          - Deflated Sharpe Ratio (Phase 6)
          - Full cost breakdown (Phase 2 + 7)
        """
        result = self.backtest(prices, timestamps, regimes)
        
        # Data hash (Phase 5 — data lineage)
        price_bytes = ",".join(f"{p:.6f}" for p in prices).encode()
        data_hash = hashlib.sha256(price_bytes).hexdigest()[:16]
        result["data_hash"] = data_hash
        
        # Get journal stats (now includes cost summary, fill stats, dollar stats)
        stats = self.journal.get_stats()
        result["stats"]["journal"] = stats
        
        # Bootstrap confidence intervals (Phase 6)
        if len(self.journal.trades) >= 5:
            trade_pnls = np.array([t.pnl_bricks for t in self.journal.trades])
            bootstrap = compute_all_bootstrap_cis(trade_pnls, confidence_level)
            result["bootstrap_ci"] = bootstrap
        else:
            result["bootstrap_ci"] = {"error": "Insufficient trades for bootstrap CI"}
        
        # Deflated Sharpe Ratio (Phase 6)
        sharpe = stats.get("sharpe_estimate", 0.0)
        if n_trials > 1 and sharpe > 0:
            trade_pnls = np.array([t.pnl_bricks for t in self.journal.trades])
            dsr = deflated_sharpe_ratio(
                observed_sharpe=sharpe,
                n_trials=n_trials,
                sample_length=len(self.journal.trades),
                returns=trade_pnls,
            )
            result["deflated_sharpe"] = dsr
        else:
            result["deflated_sharpe"] = {"dsr": 1.0, "n_trials": 1, "interpretation": "Single trial — no multiple testing adjustment needed"}
        
        return result

    def backtest_chunked(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
        chunk_size: int = 150,
    ):
        """
        Generator that yields intermediate results every chunk_size ticks.
        Used for SSE streaming (Phase 1).
        """
        self.reset()
        total_ticks = len(prices)

        # Enable warmup mode for chunked backtest (bypasses session/cooldown filters)
        self.signal_filter.set_warmup_mode(True)

        for i, price in enumerate(prices):
            ts = timestamps[i] if timestamps else float(i)
            if regimes and i < len(regimes):
                self.set_regime(regimes[i])
            
            self.process_tick(price, ts)
            
            # Yield intermediate results every chunk_size ticks
            if (i + 1) % chunk_size == 0 or i == total_ticks - 1:
                chunk_num = (i + 1) // chunk_size + (1 if (i + 1) % chunk_size > 0 else 0)
                total_chunks = max((total_ticks + chunk_size - 1) // chunk_size, 1)
                
                yield {
                    "type": "progress" if i < total_ticks - 1 else "complete",
                    "chunk": chunk_num,
                    "total_chunks": total_chunks,
                    "ticks_processed": i + 1,
                    "total_ticks": total_ticks,
                    "trades_so_far": [t.to_dict() for t in list(self.journal.trades)[-10:]],
                    "stats_so_far": self.journal.get_stats(),
                }

        # Disable warmup mode after chunked backtest
        self.signal_filter.set_warmup_mode(False)

    # ── Pending Execution Interface (called by router) ─────────────

    def consume_pending_execution(self) -> Optional[dict]:
        """Get and clear the pending execution payload. Called by router after process_tick()."""
        pending = self._pending_execution
        self._pending_execution = None
        return pending

    def consume_pending_close(self) -> Optional[dict]:
        """Get and clear the pending close payload. Called by router after process_tick()."""
        pending = self._pending_close
        self._pending_close = None
        return pending

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
            # Store pending execution info for the router to pick up
            self._pending_execution = {
                "filtered": filtered,
                "brick_size": self.config.get_brick_size(),
                "sl_bricks": self.config.sl_bricks,
                "tp_bricks": self.config.tp_bricks,
                "symbol": self.config.symbol,
            }
            logger.info(
                f"Position opened: {filtered.original_signal.direction.value} "
                f"at {filtered.original_signal.price:.2f}"
            )
        except RuntimeError as e:
            logger.warning(f"Cannot open position: {e}")

    def _handle_trade_close(self, result: TradeResult) -> None:
        """Handle a closed trade — update journal with full cost modeling."""
        self.signal_filter.record_trade_result(result.pnl_bricks)

        # Create journal record if we have filtered signal context
        if self._last_filtered is not None:
            record = self.journal.create_record(
                symbol=self.config.symbol,
                filtered=self._last_filtered,
                result=result,
                regime=self._current_regime,
            )
            
            # ── Phase 2: Transaction cost modeling ──
            entry_value = record.entry_price  # per share
            exit_value = record.exit_price
            
            # Commission: commission_bps of notional
            notional = entry_value * record.qty
            record.commission = notional * self.config.commission_bps / 10000
            
            # Slippage cost: difference between ideal and actual fill
            if record.direction.value == "LONG":
                ideal_entry = entry_value / (1 + self.config.slippage_bps / 10000)
                ideal_exit = exit_value / (1 - self.config.slippage_bps / 20000)  # TP limit
                if "SL" in record.status.value:
                    ideal_exit = exit_value / (1 + self.config.slippage_bps / 10000)  # SL market
            else:
                ideal_entry = entry_value / (1 - self.config.slippage_bps / 10000)
                ideal_exit = exit_value / (1 + self.config.slippage_bps / 20000)
                if "SL" in record.status.value:
                    ideal_exit = exit_value / (1 - self.config.slippage_bps / 10000)
            record.slippage_cost = abs(exit_value - ideal_exit) * record.qty + abs(entry_value - ideal_entry) * record.qty
            
            # Spread cost: half-spread on entry and exit
            record.spread_cost = notional * self.config.spread_bps / 10000 / 2 + exit_value * record.qty * self.config.spread_bps / 10000 / 2
            
            # Total basic cost
            record.total_cost = record.commission + record.slippage_cost + record.spread_cost
            
            # ── Phase 7: Market impact ──
            if self.config.market_impact_mode == "almgren_chriss":
                impact = compute_market_impact(
                    order_shares=record.qty,
                    adv_shares=self.config.adv_shares,
                    sigma_daily=0.02,  # Default; should come from data
                    avg_price=record.entry_price,
                )
                record.impact_cost = impact["impact_cost_dollars"]
            
            # ── Phase 7: Borrow & financing costs ──
            if record.direction.value == "SHORT" or self.config.margin_rate_bps > 0:
                holding_days = max(record.bricks_held / 252, 1/252)  # Approximate
                position_notional = entry_value * record.qty
                
                financing = compute_total_financing_costs(
                    position_notional=position_notional,
                    holding_days=holding_days,
                    direction="short" if record.direction.value == "SHORT" else "long",
                    is_hard_to_borrow=self.config.is_hard_to_borrow,
                    borrow_rate_bps=self.config.borrow_rate_bps,
                    margin_rate_bps=self.config.margin_rate_bps,
                    dividend_yield_bps=self.config.dividend_yield_bps,
                )
                record.borrow_cost = financing["components"].get("borrow", {}).get("total_borrow_cost", 0.0)
                record.margin_cost = financing["components"].get("margin", {}).get("total_margin_cost", 0.0)
                record.dividend_cost = financing["components"].get("dividend", {}).get("total_dividend_cost", 0.0)
                record.total_financing_cost = financing["total_financing_cost"]
            
            # ── Phase 7: Fill probability ──
            if self.config.fill_probability_mode == "realistic":
                # Check if entry was a limit order (TP) or market order
                fill_prob = compute_fill_probability(
                    order_type="limit",
                    distance_from_mid_pct=abs(record.entry_price - (record.entry_price)) * 0.01,
                    volatility_daily=0.02,
                )
                record.fill_probability = fill_prob["fill_probability"]
                
                # Probabilistic missed fill (for TP limit orders)
                import random
                if random.random() > record.fill_probability:
                    record.missed_fill = True
            
            # Adjust P&L for costs
            total_costs = record.total_cost + record.impact_cost + record.total_financing_cost
            record.pnl_dollars = record.pnl_dollars - total_costs
            
            self.journal.record(record)

        # Store pending close info for the router to pick up
        self._pending_close = {
            "symbol": self.config.symbol,
            "trade_result": result,
        }

        if self._on_trade:
            self._on_trade(result)

        logger.info(
            f"Trade closed: {result.status.value} PnL={result.pnl_bricks:.2f} bricks "
            f"(${result.pnl_dollars:.2f})"
        )
