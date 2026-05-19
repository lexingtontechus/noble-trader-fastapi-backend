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
                # Phase 7: Advanced execution modeling
                enable_market_impact=self.config.enable_market_impact,
                avg_daily_volume=self.config.avg_daily_volume,
                impact_gamma=self.config.impact_gamma,
                impact_eta=self.config.impact_eta,
                enable_fill_probability=self.config.enable_fill_probability,
                enable_borrow_costs=self.config.enable_borrow_costs,
                borrow_rate_annual=self.config.borrow_rate_annual,
                hard_to_borrow=self.config.hard_to_borrow,
                htb_premium_rate=self.config.htb_premium_rate,
                enable_margin_costs=self.config.enable_margin_costs,
                margin_rate_annual=self.config.margin_rate_annual,
                margin_requirement=self.config.margin_requirement,
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
            "journal": self.journal.get_stats(initial_capital=self.config.initial_capital),
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

    # ── Walk-Forward Out-of-Sample Validation (Phase 4B) ─────────────

    def walk_forward(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
        train_window: int = 500,
        test_window: int = 250,
        min_trades_for_stats: int = 5,
    ) -> Dict[str, Any]:
        """
        Walk-forward out-of-sample validation.

        Splits the price series into rolling train/test windows.
        For each window, runs the backtest on the full window but reports
        separate in-sample and out-of-sample statistics. This validates
        whether the strategy parameters are robust or just overfitted
        to the full dataset.

        Parameters
        ----------
        prices : list[float]
            Historical price series.
        timestamps : list[float], optional
            Matching timestamps.
        regimes : list[str], optional
            Pre-computed regime labels aligned with prices.
        train_window : int
            Number of ticks for the in-sample (training) window.
        test_window : int
            Number of ticks for the out-of-sample (test) window.
        min_trades_for_stats : int
            Minimum trades required for a window to be included in stats.

        Returns
        -------
        dict with keys:
          - windows: list of per-window results (in-sample + out-of-sample stats)
          - aggregate: combined OOS stats across all windows
          - is_oos_ratio: out-of-sample / in-sample return ratio
        """
        self.reset()
        n = len(prices)
        windows = []
        all_oos_trades = []

        step = test_window
        start = 0

        while start + train_window + test_window <= n:
            # In-sample: [start, start+train_window)
            is_start = start
            is_end = start + train_window
            # Out-of-sample: [is_end, is_end+test_window)
            oos_start = is_end
            oos_end = min(oos_start + test_window, n)

            # Run in-sample
            is_pipeline = RenkoPipeline(self.config)
            is_result = is_pipeline.backtest(
                prices[is_start:is_end],
                timestamps[is_start:is_end] if timestamps else None,
                regimes[is_start:is_end] if regimes else None,
            )
            is_stats = is_result.get("stats", {}).get("journal", {})

            # Run out-of-sample
            oos_pipeline = RenkoPipeline(self.config)
            oos_result = oos_pipeline.backtest(
                prices[oos_start:oos_end],
                timestamps[oos_start:oos_end] if timestamps else None,
                regimes[oos_start:oos_end] if regimes else None,
            )
            oos_stats = oos_result.get("stats", {}).get("journal", {})
            oos_trades = oos_result.get("trades", [])

            is_trades_count = is_stats.get("total_trades", 0)
            oos_trades_count = oos_stats.get("total_trades", 0)

            window_data = {
                "window": len(windows) + 1,
                "is_range": [is_start, is_end],
                "oos_range": [oos_start, oos_end],
                "is_stats": is_stats,
                "oos_stats": oos_stats,
                "is_total_pnl_bricks": is_stats.get("total_pnl_bricks", 0.0),
                "oos_total_pnl_bricks": oos_stats.get("total_pnl_bricks", 0.0),
                "is_sharpe": is_stats.get("sharpe_estimate", 0.0),
                "oos_sharpe": oos_stats.get("sharpe_estimate", 0.0),
                "is_trades": is_trades_count,
                "oos_trades": oos_trades_count,
            }

            # Only include windows with enough trades for meaningful stats
            if oos_trades_count >= min_trades_for_stats:
                all_oos_trades.extend(oos_trades)
                windows.append(window_data)
            elif is_trades_count >= min_trades_for_stats:
                windows.append(window_data)

            start += step

        # Aggregate OOS stats
        aggregate = {
            "total_windows": len(windows),
            "total_oos_trades": len(all_oos_trades),
            "avg_oos_pnl_bricks": 0.0,
            "avg_oos_sharpe": 0.0,
            "avg_oos_win_rate": 0.0,
            "degradation_ratio": 0.0,
        }

        if windows:
            avg_oos_pnl = sum(w["oos_total_pnl_bricks"] for w in windows) / len(windows)
            avg_is_pnl = sum(w["is_total_pnl_bricks"] for w in windows) / len(windows)
            avg_oos_sharpe = sum(w["oos_sharpe"] for w in windows) / len(windows)
            avg_oos_wr = sum(w["oos_stats"].get("win_rate", 0.0) for w in windows) / len(windows)

            aggregate["avg_oos_pnl_bricks"] = round(avg_oos_pnl, 4)
            aggregate["avg_is_pnl_bricks"] = round(avg_is_pnl, 4)
            aggregate["avg_oos_sharpe"] = round(avg_oos_sharpe, 4)
            aggregate["avg_oos_win_rate"] = round(avg_oos_wr, 4)

            # Degradation ratio: OOS/IS — below 0.5 means severe overfitting
            if avg_is_pnl != 0:
                aggregate["degradation_ratio"] = round(avg_oos_pnl / avg_is_pnl, 4)
            elif avg_oos_pnl > 0:
                aggregate["degradation_ratio"] = float("inf")

        return {
            "windows": windows,
            "aggregate": aggregate,
            "config_used": self.config.to_dict(),
            "total_ticks": n,
        }

    # ── Monte Carlo Permutation (Phase 4C) ──────────────────────────

    def monte_carlo_permute(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
        regimes: Optional[List[str]] = None,
        n_simulations: int = 1000,
        confidence_levels: tuple = (5, 25, 50, 75, 95),
    ) -> Dict[str, Any]:
        """
        Monte Carlo simulation by permuting trade order.

        Shuffles the trade P&L sequence N times and computes the equity
        curve for each permutation. This answers: "Could I have gotten
        these results by luck?" If the 5th-percentile equity curve is
        still profitable, the strategy is robust regardless of trade timing.

        Parameters
        ----------
        prices : list[float]
            Historical price series.
        timestamps : list[float], optional
            Matching timestamps.
        regimes : list[str], optional
            Pre-computed regime labels aligned with prices.
        n_simulations : int
            Number of random trade-order permutations (default 1000).
        confidence_levels : tuple
            Percentile levels to compute (default 5th, 25th, 50th, 75th, 95th).

        Returns
        -------
        dict with keys:
          - original: original backtest stats
          - confidence_bands: dict of percentile → equity curve
          - p_profitable: probability that a random permutation is profitable
          - p_beat_original: probability that a random permutation beats original
          - simulation_count: number of simulations run
        """
        import numpy as np

        # Run original backtest
        self.reset()
        original_result = self.backtest(prices, timestamps, regimes)
        original_stats = original_result.get("stats", {}).get("journal", {})
        trades = list(self.journal.trades)

        if len(trades) < 3:
            return {
                "original": original_stats,
                "confidence_bands": {},
                "dollar_bands": {},
                "original_total_pnl_bricks": round(sum(t.pnl_bricks for t in trades), 4),
                "original_total_pnl_dollars": round(sum(t.pnl_dollars for t in trades), 2),
                "p_profitable": 0.0,
                "p_beat_original": 0.0,
                "p5_final_pnl": 0.0,
                "p95_final_pnl": 0.0,
                "mean_final_pnl": 0.0,
                "simulation_count": 0,
                "n_trades": len(trades),
                "error": "Need at least 3 trades for Monte Carlo simulation",
                "config_used": self.config.to_dict(),
            }

        # Extract trade P&Ls in bricks and dollars
        trade_pnls_bricks = [t.pnl_bricks for t in trades]
        trade_pnls_dollars = [t.pnl_dollars for t in trades]
        original_total_pnl = sum(trade_pnls_bricks)
        original_total_pnl_dollars = sum(trade_pnls_dollars)

        # Run N permutations
        n_trades = len(trades)
        final_pnls_bricks = np.zeros(n_simulations)
        final_pnls_dollars = np.zeros(n_simulations)
        equity_curves = np.zeros((n_simulations, n_trades))

        rng = np.random.default_rng()

        for i in range(n_simulations):
            perm = rng.permutation(trade_pnls_bricks)
            cumsum = np.cumsum(perm)
            equity_curves[i] = cumsum
            final_pnls_bricks[i] = cumsum[-1]

            # Also track dollar P&L
            perm_dollars = trade_pnls_dollars.copy()
            rng.shuffle(perm_dollars)
            final_pnls_dollars[i] = sum(perm_dollars)

        # Compute confidence bands at each trade step
        confidence_bands = {}
        for level in confidence_levels:
            band = np.percentile(equity_curves, level, axis=0)
            confidence_bands[f"p{level}"] = [round(float(v), 4) for v in band]

        # Also include the original equity curve
        original_equity = list(np.cumsum(trade_pnls_bricks))
        confidence_bands["original"] = [round(float(v), 4) for v in original_equity]

        # Probability that a random permutation is profitable
        p_profitable = float(np.mean(final_pnls_bricks > 0))

        # Probability that a random permutation beats the original
        p_beat_original = float(np.mean(final_pnls_bricks >= original_total_pnl))

        # Dollar-denominated confidence stats
        dollar_bands = {}
        for level in confidence_levels:
            dollar_bands[f"p{level}_dollars"] = round(float(np.percentile(final_pnls_dollars, level)), 2)

        return {
            "original": original_stats,
            "confidence_bands": confidence_bands,
            "dollar_bands": dollar_bands,
            "original_total_pnl_bricks": round(original_total_pnl, 4),
            "original_total_pnl_dollars": round(original_total_pnl_dollars, 2),
            "p_profitable": round(p_profitable, 4),
            "p_beat_original": round(p_beat_original, 4),
            "p5_final_pnl": round(float(np.percentile(final_pnls_bricks, 5)), 4),
            "p95_final_pnl": round(float(np.percentile(final_pnls_bricks, 95)), 4),
            "mean_final_pnl": round(float(np.mean(final_pnls_bricks)), 4),
            "simulation_count": n_simulations,
            "n_trades": n_trades,
            "config_used": self.config.to_dict(),
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
