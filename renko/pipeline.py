"""
Renko Pipeline - orchestrates the full HFT pipeline from tick to trade.

Pipeline flow:
    Tick -> Brick Engine -> Swing Classifier -> Pattern Detector ->
    Signal Filter -> Risk Manager -> Executor -> Trade Journal

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
            # quality_floor is an alias for regime_floor - use whichever is set
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
        
        # Regime shift tracking
        self._prev_regime: Optional[str] = None
        self._regime_shift: Optional[str] = None  # "bull_bear" or "bear_bull"

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
    
    def _update_regime_shift(self, new_regime: Optional[str]) -> Optional[str]:
        """
        Update regime shift tracking when regime changes.
        
        Args:
            new_regime: The new regime (bull/bear/neutral)
            
        Returns:
            Regime shift string if changed, None otherwise
        """
        if self._current_regime == new_regime:
            return None
            
        # Detect regime shift
        regime_shift = None
        if self._current_regime is not None and new_regime is not None:
            if "bull" in self._current_regime.lower() and "bear" in new_regime.lower():
                regime_shift = "bull_bear"
            elif "bear" in self._current_regime.lower() and "bull" in new_regime.lower():
                regime_shift = "bear_bull"
        
        # Update tracking
        self._prev_regime = self._current_regime
        self._current_regime = new_regime
        self._regime_shift = regime_shift
        
        return regime_shift

    @property
    def readiness(self) -> Dict[str, Any]:
        """Pipeline readiness assessment for MCP agents.

        Returns a dict indicating whether the pipeline has enough data
        for signal generation and risk management. An MCP agent should
        check readiness before making trading decisions.

        Levels:
          - ``cold``: brick_count == 0, pipeline needs warmup
          - ``warming``: brick_count < 50, signals unreliable
          - ``warm``: brick_count >= 50, signals active but limited history
          - ``hot``: brick_count >= 200, full pattern detection + journal
          - ``live_ready``: hot + no active position, ready for new entries
        """
        bc = self.brick_engine.brick_count
        has_position = self.risk_manager.has_position

        if bc == 0:
            level = "cold"
            can_trade = False
            reason = "Pipeline is empty - call POST /renko/warmup first"
        elif bc < 50:
            level = "warming"
            can_trade = False
            reason = f"Insufficient bricks ({bc}/50) for reliable signals"
        elif bc < 200:
            level = "warm"
            can_trade = True
            reason = f"Signals active ({bc} bricks) but limited journal history"
        else:
            if has_position:
                level = "hot"
                can_trade = False  # Already in a position
                reason = f"Hot pipeline ({bc} bricks) but position already open"
            else:
                level = "live_ready"
                can_trade = True
                reason = f"Fully ready ({bc} bricks, no open position)"

        return {
            "level": level,
            "can_trade": can_trade,
            "brick_count": bc,
            "has_position": has_position,
            "reason": reason,
            "sizing_mode": self.config.sizing_mode.value,
            "regime": self._current_regime,
            "equity": self._current_equity,
        }

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

        Includes: config, brick history, classified history, trade journal,
        regime, equity, filter stats, **and active position state**.

        v3 format adds ``active_position``, ``signal_filter_state``, and
        ``snapshot_version`` so that ``from_snapshot()`` can fully hydrate
        the pipeline - including restoring an open position with trailing
        stop state - without requiring a warmup or losing trades.

        This is critical for MCP agents: an orphaned position after restart
        is a risk disaster. The agent must know its exact state on recovery.
        """
        # Serialize active position (including trailing stop state)
        pos_data = None
        if self.risk_manager.has_position:
            pos = self.risk_manager.active_position
            pos_data = {
                "entry_price": pos.entry_price,
                "direction": pos.direction.value,
                "brick_size": pos.brick_size,
                "entry_ts": pos.entry_ts,
                "entry_brick_idx": pos.entry_brick_idx,
                "qty": pos.qty,
                "sl_price": pos.sl_price,
                "tp_price": pos.tp_price,
                "peak_profit_bricks": pos.peak_profit_bricks,
                "current_sl_price": pos.current_sl_price,
                "bricks_open": pos.bricks_open,
                "partial_closed": pos.partial_closed,
                "remaining_qty": pos.remaining_qty,
                "params": {
                    "sl_bricks": pos.params.sl_bricks,
                    "tp_bricks": pos.params.tp_bricks,
                    "trailing_stop": pos.params.trailing_stop,
                    "trail_after_bricks": pos.params.trail_after_bricks,
                    "trail_distance_bricks": pos.params.trail_distance_bricks,
                    "time_stop_bricks": pos.params.time_stop_bricks,
                    "partial_exit_pct": pos.params.partial_exit_pct,
                    "partial_exit_bricks": pos.params.partial_exit_bricks,
                    "slippage_bps": pos.params.slippage_bps,
                    "oco_priority": pos.params.oco_priority,
                },
            }

        # Signal filter state (warmup mode, session counters)
        sf = self.signal_filter
        signal_filter_state = {
            "warmup_mode": sf._warmup_mode,
            "session_trades_taken": sf._stats.trades_taken,
            "session_daily_pnl_bricks": sf._stats.daily_pnl_bricks,
            "consecutive_losses": sf._stats.consecutive_losses,
            "last_trade_ts": sf._stats.last_trade_ts,
            # Masaniello batch state (if applicable)
            "masaniello_batch_wins": getattr(sf, '_masaniello_batch_wins', 0),
            "masaniello_batch_losses": getattr(sf, '_masaniello_batch_losses', 0),
            "masaniello_trade_index": getattr(sf, '_masaniello_trade_index', 0),
            "masaniello_initial_equity": getattr(sf, '_masaniello_initial_equity', 0.0),
            "masaniello_peak_equity": getattr(sf, '_masaniello_peak_equity', 0.0),
            "masaniello_pnl": getattr(sf, '_masaniello_pnl', 0.0),
        }

        return {
            "snapshot_version": 3,
            "config": self.config.to_dict(),
            "brick_count": self.brick_engine.brick_count,
            "current_direction": self.brick_engine.current_direction.value if self.brick_engine.current_direction else None,
            "current_price": self.brick_engine.last_brick.close_price if self.brick_engine.last_brick else None,
            "regime": self._current_regime,
            "equity": self._current_equity,
            "regime_quality": self._current_regime_quality,
            "regime_confidence": self._current_regime_confidence,
            "current_drawdown": self._current_drawdown,
            "brick_velocity": self._brick_velocity,
            "session_stats": self.signal_filter.session_stats,
            "journal_stats": self.journal.get_stats(),
            "snapshot_ts": time.time(),
            # Full history arrays - enable instant restore without warmup
            "bricks": [b.to_dict() for b in self.brick_engine.history],
            "classified": [cb.to_dict() for cb in self.swing_classifier.classified_history],
            "trades": [t.to_dict() for t in self.journal.trades],
            # v3: Active position + signal filter state (MCP agent safety)
            "active_position": pos_data,
            "signal_filter_state": signal_filter_state,
            # v3.1: Pending signal context (trade journal continuity)
            # When a position is open, _last_filtered provides the context
            # needed to create a complete journal entry when the position closes.
            # Without it, restored pipelines lose trade attribution on close.
            "last_filtered": self._last_filtered.to_dict() if self._last_filtered else None,
            "last_signal": self._last_signal.to_dict() if self._last_signal else None,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> 'RenkoPipeline':
        """
        Restore a pipeline from a persisted snapshot.

        v3 snapshots additionally restore:
          - ``active_position`` - open position with trailing stop state
          - ``signal_filter_state`` - session counters, warmup mode, Masaniello batch

        When the snapshot includes ``bricks``, ``classified``, and ``trades``
        arrays (v2+ format), the pipeline is fully hydrated - the brick engine
        has its complete history, the swing classifier has classified context,
        and the journal has all trade records.  This eliminates the need for
        a warmup cycle on restart.

        Falls back gracefully when only v1 stats are available (no arrays).
        """
        config_data = data.get("config", {})
        config = RenkoConfig(**{k: v for k, v in config_data.items() if k in RenkoConfig.__dataclass_fields__})
        pipeline = cls(config)
        pipeline._current_regime = data.get("regime")
        pipeline._current_equity = data.get("equity", 100000.0)
        pipeline._brick_velocity = data.get("brick_velocity", 0.0)
        pipeline._current_regime_quality = data.get("regime_quality", 1.0)
        pipeline._current_regime_confidence = data.get("regime_confidence", 0.75)
        pipeline._current_drawdown = data.get("current_drawdown", 0.0)

        # ── Hydrate brick engine history (v2+ snapshots) ──
        bricks_data = data.get("bricks")
        if bricks_data and isinstance(bricks_data, list):
            for bd in bricks_data:
                brick = Brick(
                    index=bd["index"],
                    direction=BrickDirection(bd["direction"]),
                    open_price=bd["open_price"],
                    close_price=bd["close_price"],
                    high=bd["high"],
                    low=bd["low"],
                    timestamp=bd["timestamp"],
                    brick_size=bd["brick_size"],
                )
                pipeline.brick_engine.history.append(brick)
            # Rebuild engine internal state from last brick so new ticks
            # continue correctly (direction, reference levels, brick_count).
            if pipeline.brick_engine.history:
                last = pipeline.brick_engine.history[-1]
                pipeline.brick_engine._state.direction = last.direction
                pipeline.brick_engine._state.last_brick_high = last.high
                pipeline.brick_engine._state.last_brick_low = last.low
                pipeline.brick_engine._state.brick_count = last.index + 1
                pipeline.brick_engine._initialized = True
                logger.info(
                    "Hydrated brick engine with %d bricks (last direction=%s, price=%.2f)",
                    len(bricks_data), last.direction.value, last.close_price,
                )

        # ── Hydrate swing classifier classified history (v2+ snapshots) ──
        classified_data = data.get("classified")
        if classified_data and isinstance(classified_data, list):
            from renko.swing_classifier import SwingLabel, SwingPoint
            for cd in classified_data:
                brick_d = cd.get("brick", {})
                brick = Brick(
                    index=brick_d["index"],
                    direction=BrickDirection(brick_d["direction"]),
                    open_price=brick_d["open_price"],
                    close_price=brick_d["close_price"],
                    high=brick_d["high"],
                    low=brick_d["low"],
                    timestamp=brick_d["timestamp"],
                    brick_size=brick_d["brick_size"],
                )
                label = SwingLabel(cd.get("label", "HH"))
                sp_data = cd.get("swing_point")
                swing_point = None
                if sp_data:
                    swing_point = SwingPoint(
                        swing_type=SwingPoint.SwingType(sp_data.get("swing_type", "swing_high")),
                        brick_index=sp_data["brick_index"],
                        price=sp_data["price"],
                        timestamp=sp_data["timestamp"],
                        label=SwingLabel(sp_data.get("label", "HH")),
                    )
                cb = ClassifiedBrick(brick=brick, label=label, swing_point=swing_point)
                pipeline.swing_classifier.classified_history.append(cb)
            logger.info("Hydrated swing classifier with %d classified bricks", len(classified_data))

        # ── Hydrate trade journal (v2+ snapshots) ──
        trades_data = data.get("trades")
        if trades_data and isinstance(trades_data, list):
            for td in trades_data:
                record = TradeRecord(
                    trade_id=td["trade_id"],
                    symbol=td["symbol"],
                    direction=SignalDirection(td["direction"]),
                    pattern_type=PatternType(td["pattern_type"]),
                    qty=td.get("qty", 1.0),
                    entry_price=td["entry_price"],
                    exit_price=td["exit_price"],
                    entry_ts=td["entry_ts"],
                    exit_ts=td["exit_ts"],
                    pnl_bricks=td["pnl_bricks"],
                    pnl_dollars=td.get("pnl_dollars", 0.0),
                    status=PositionStatus(td["status"]),
                    bricks_held=td["bricks_held"],
                    signal_strength=td.get("signal_strength", 0.5),
                    regime=td.get("regime"),
                    regime_aligned=td.get("regime_aligned", False),
                    kelly_fraction=td.get("kelly_fraction", 0.0),
                    velocity=td.get("velocity", 0.0),
                    label_sequence=td.get("label_sequence", []),
                    filters_passed=td.get("filters_passed", []),
                    commission=td.get("commission", 0.0),
                    slippage_cost=td.get("slippage_cost", 0.0),
                    spread_cost=td.get("spread_cost", 0.0),
                    total_cost=td.get("total_cost", 0.0),
                    impact_cost=td.get("impact_cost", 0.0),
                    borrow_cost=td.get("borrow_cost", 0.0),
                    margin_cost=td.get("margin_cost", 0.0),
                    dividend_cost=td.get("dividend_cost", 0.0),
                    total_financing_cost=td.get("total_financing_cost", 0.0),
                    fill_probability=td.get("fill_probability", 1.0),
                    missed_fill=td.get("missed_fill", False),
                )
                pipeline.journal.trades.append(record)
            # Sync the counter so new trade IDs don't collide
            pipeline.journal._trade_counter = len(trades_data)
            logger.info("Hydrated trade journal with %d trade records", len(trades_data))

        # ── Hydrate active position (v3 snapshots - MCP agent safety) ──
        pos_data = data.get("active_position")
        if pos_data and isinstance(pos_data, dict):
            from renko.risk_manager import ActivePosition, RiskParams
            pos_params = RiskParams(
                sl_bricks=pos_data.get("params", {}).get("sl_bricks", 3),
                tp_bricks=pos_data.get("params", {}).get("tp_bricks", 5),
                trailing_stop=pos_data.get("params", {}).get("trailing_stop", True),
                trail_after_bricks=pos_data.get("params", {}).get("trail_after_bricks", 3),
                trail_distance_bricks=pos_data.get("params", {}).get("trail_distance_bricks", 2),
                time_stop_bricks=pos_data.get("params", {}).get("time_stop_bricks", 10),
                partial_exit_pct=pos_data.get("params", {}).get("partial_exit_pct", 0.0),
                partial_exit_bricks=pos_data.get("params", {}).get("partial_exit_bricks", 3),
                slippage_bps=pos_data.get("params", {}).get("slippage_bps", 2.0),
                oco_priority=pos_data.get("params", {}).get("oco_priority", "sl_first"),
            )
            pos = ActivePosition(
                entry_price=pos_data["entry_price"],
                direction=SignalDirection(pos_data["direction"]),
                brick_size=pos_data["brick_size"],
                entry_ts=pos_data["entry_ts"],
                entry_brick_idx=pos_data["entry_brick_idx"],
                qty=pos_data.get("qty", 1.0),
                params=pos_params,
            )
            # Restore dynamic trailing stop state
            pos.peak_profit_bricks = pos_data.get("peak_profit_bricks", 0.0)
            pos.current_sl_price = pos_data.get("current_sl_price", pos.sl_price)
            pos.bricks_open = pos_data.get("bricks_open", 0)
            pos.partial_closed = pos_data.get("partial_closed", False)
            pos.remaining_qty = pos_data.get("remaining_qty", 1.0)
            pipeline.risk_manager.active_position = pos
            logger.warning(
                "Hydrated ACTIVE POSITION from snapshot: %s at %.2f, "
                "SL=%.2f, TP=%.2f, trail_sl=%.2f, bricks_open=%d - "
                "agent MUST verify position with broker",
                pos.direction.value, pos.entry_price,
                pos.sl_price, pos.tp_price, pos.current_sl_price,
                pos.bricks_open,
            )

        # ── Hydrate signal filter state (v3 snapshots) ──
        sf_data = data.get("signal_filter_state")
        if sf_data and isinstance(sf_data, dict):
            sf = pipeline.signal_filter
            sf._warmup_mode = sf_data.get("warmup_mode", False)
            sf._stats.trades_taken = sf_data.get("session_trades_taken", 0)
            sf._stats.daily_pnl_bricks = sf_data.get("session_daily_pnl_bricks", 0.0)
            sf._stats.consecutive_losses = sf_data.get("consecutive_losses", 0)
            sf._stats.last_trade_ts = sf_data.get("last_trade_ts", 0.0)
            # Masaniello batch state
            if hasattr(sf, '_masaniello_batch_wins'):
                sf._masaniello_batch_wins = sf_data.get("masaniello_batch_wins", 0)
                sf._masaniello_batch_losses = sf_data.get("masaniello_batch_losses", 0)
                sf._masaniello_trade_index = sf_data.get("masaniello_trade_index", 0)
                sf._masaniello_initial_equity = sf_data.get("masaniello_initial_equity", 0.0)
                sf._masaniello_peak_equity = sf_data.get("masaniello_peak_equity", 0.0)
                sf._masaniello_pnl = sf_data.get("masaniello_pnl", 0.0)
            logger.info(
                "Hydrated signal filter: warmup=%s, session_trades=%d, consecutive_losses=%d",
                sf._warmup_mode, sf._stats.trades_taken, sf._stats.consecutive_losses,
            )

        # ── Hydrate pending signal context (v3.1 - trade journal continuity) ──
        last_filtered_data = data.get("last_filtered")
        if last_filtered_data and isinstance(last_filtered_data, dict):
            try:
                from renko.pattern_detector import PatternSignal, SignalDirection
                sig_data = last_filtered_data.get("signal", {})
                original_signal = PatternSignal(
                    direction=SignalDirection(sig_data.get("direction", "long")),
                    price=sig_data.get("price", 0.0),
                    pattern_type=sig_data.get("pattern_type", "unknown"),
                    timestamp=sig_data.get("timestamp", 0.0),
                    strength=sig_data.get("strength", 0.5),
                    brick_index=sig_data.get("brick_index", 0),
                )
                pipeline._last_filtered = FilteredSignal(
                    original_signal=original_signal,
                    passed_filters=last_filtered_data.get("passed_filters", []),
                    regime_aligned=last_filtered_data.get("regime_aligned", False),
                    kelly_fraction=last_filtered_data.get("kelly_fraction", 0.0),
                    position_size_usd=last_filtered_data.get("position_size_usd", 0.0),
                    brick_size=last_filtered_data.get("brick_size", 0.0),
                    sizing_mode=last_filtered_data.get("sizing_mode", "kelly"),
                    masaniello_factor=last_filtered_data.get("masaniello_factor", 0.0),
                    quality_factor=last_filtered_data.get("quality_factor", 0.0),
                    drawdown_factor=last_filtered_data.get("drawdown_factor", 0.0),
                    volatility_factor=last_filtered_data.get("volatility_factor", 0.0),
                    risk_fraction=last_filtered_data.get("risk_fraction", 0.0),
                    contracts=last_filtered_data.get("contracts", 0),
                )
                logger.info("Hydrated last_filtered signal for journal continuity")
            except Exception as e:
                logger.debug(f"Could not hydrate last_filtered: {e}")

        last_signal_data = data.get("last_signal")
        if last_signal_data and isinstance(last_signal_data, dict):
            try:
                from renko.pattern_detector import PatternSignal, SignalDirection
                pipeline._last_signal = PatternSignal(
                    direction=SignalDirection(last_signal_data.get("direction", "long")),
                    price=last_signal_data.get("price", 0.0),
                    pattern_type=last_signal_data.get("pattern_type", "unknown"),
                    timestamp=last_signal_data.get("timestamp", 0.0),
                    strength=last_signal_data.get("strength", 0.5),
                    brick_index=last_signal_data.get("brick_index", 0),
                )
                logger.debug("Hydrated last_signal for context continuity")
            except Exception as e:
                logger.debug(f"Could not hydrate last_signal: {e}")

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
        
        # Data hash (Phase 5 - data lineage)
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
            result["deflated_sharpe"] = {"dsr": 1.0, "n_trials": 1, "interpretation": "Single trial - no multiple testing adjustment needed"}
        
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
        """Handle a closed trade - update journal with full cost modeling."""
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
