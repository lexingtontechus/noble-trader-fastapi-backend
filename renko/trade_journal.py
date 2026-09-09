"""
Trade Journal + Stats Engine - records every trade and computes
performance metrics for strategy evaluation and Kelly sizing.

.. deprecated:: P5 (2026-07-23), fully deprecated 2026-09-09
    The Noble Trader backend (``noble-trader-fastapi-backend``) is the
    SWEEP ORCHESTRATOR ONLY — it does NOT execute trades (see P5
    execution-deprecation contract in
    ``regime_platform/services/execution_deprecation.py``). Live trade
    execution AND live trade journaling both live in the agent
    (``noble-trader-agent``), which writes its own ``trade_journal``
    DuckDB table (see ``hermes/db/schema.sql:153`` +
    ``hermes/agent/learning.py:336``) — that is the canonical trade
    journal. Production calibration records
    (``nt_calibration_records``) are written by
    ``regime_platform/sweep_orchestrator/eod_resolver.py`` against real
    resolved signals, not by this module.

    ``TradeJournal.record()`` originally also scheduled an async
    fire-and-forget write to ``nt_calibration_records`` on every closed
    trade, gated behind ``assert_execution_enabled()`` so it no-op'd
    (with a WARNING log) whenever the backend's execution gate was
    closed — which is unconditionally, always, on every deployment of
    this backend, forever, per the P5 architecture. That write path,
    its gate check, and the two WARNING log lines it produced on every
    single simulated trade close were removed entirely 2026-09-09 (not
    just silenced) — worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md: a
    permanently-true condition doesn't need a runtime gate re-checked
    and logged on every call, it needs the dead branch deleted.
    ``record()`` now only appends to the in-memory deque for stats
    (win rate, profit factor, Sharpe, etc.) — the actual reason every
    caller (heavy_sweep.py's grid-search backtests, this repo's own
    replay tests) uses this class.

    The module is NOT deleted because the ``TradeRecord`` dataclass +
    ``TradeJournal.get_stats()`` are used by
    ``tests/test_execution_modeling.py``,
    ``tests/test_statistical_rigor.py``, and ``heavy_sweep.py``'s grid
    search for backtest replay/scoring, and removing the module would
    break ``renko/__init__.py:18``'s re-export and the renko pipeline
    import chain.

Tracks:
  - Every signal, filter decision, and order result
  - P&L in both bricks and dollars
  - Win rate, profit factor, Sharpe, max drawdown
  - Per-pattern-type breakdowns
  - Rolling statistics for adaptive Kelly sizing
  - Calibration records (v3) — predicted P_win vs actual outcome
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from collections import deque

from renko.pattern_detector import PatternType, SignalDirection
from renko.risk_manager import PositionStatus, TradeResult
from renko.signal_filter import FilteredSignal

# Sentinel cap for profit_factor when there are no losing trades (2026-08-24).
# gross_profit / gross_loss is undefined at gross_loss == 0 — the previous
# behavior returned literal float("inf"), which Postgres/JSON reject
# ("Out of range float values are not JSON compliant"), silently dropping
# the entire nt_sweep_result row for that tick (confirmed live: a US500
# emergency sweep with win_rate=1.0 lost its telemetry row this way).
# A 0-loss window is exactly the "absurd outlier, likely overfit/small
# sample" case heavy_sweep.py's own MAX_SHARPE already exists to flag for a
# different metric — same rationale, a finite cap here instead of a
# rejection filter, since get_stats() reports rather than picks combos.
_PROFIT_FACTOR_CAP = 999.0


# ── Data types ─────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """Complete record of a single trade lifecycle."""

    trade_id: str
    symbol: str
    direction: SignalDirection
    pattern_type: PatternType
    qty: float = 1.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_ts: float = 0.0
    exit_ts: float = 0.0
    pnl_bricks: float = 0.0
    pnl_dollars: float = 0.0
    status: PositionStatus = PositionStatus.CLOSED_MANUAL
    bricks_held: int = 0
    signal_strength: float = 0.0
    regime: Optional[str] = None
    regime_aligned: bool = False
    kelly_fraction: float = 0.0
    velocity: float = 0.0
    label_sequence: List[str] = field(default_factory=list)
    filters_passed: List[str] = field(default_factory=list)

    # Cost tracking (Phase 2)
    commission: float = 0.0              # Commission cost in dollars
    slippage_cost: float = 0.0           # Slippage cost in dollars
    spread_cost: float = 0.0            # Spread cost in dollars
    total_cost: float = 0.0             # commission + slippage_cost + spread_cost

    # Execution modeling (Phase 7)
    impact_cost: float = 0.0            # Market impact cost in dollars
    borrow_cost: float = 0.0            # Short borrow cost in dollars
    margin_cost: float = 0.0            # Margin financing cost in dollars
    dividend_cost: float = 0.0          # Dividend cost for shorts in dollars
    total_financing_cost: float = 0.0   # borrow + margin + dividend costs
    fill_probability: float = 1.0       # Fill probability for this trade's entry
    missed_fill: bool = False           # Whether this trade was a missed fill

    # Calibration fields (v3)
    predicted_p_win: float = 0.5        # Blended P_win at entry for calibration
    aggression: str = "mid"             # passive | mid | aggressive
    p_regime: Optional[float] = None    # P_regime breakdown for calibration
    p_pattern: Optional[float] = None   # P_pattern breakdown (historical win-rate; v5 rename of v4 p_ext_signal)
    p_markov: Optional[float] = None    # P_markov breakdown
    p_timesfm: Optional[float] = None   # TimesFM component

    @property
    def is_win(self) -> bool:
        return self.pnl_bricks > 0

    @property
    def duration_seconds(self) -> float:
        return self.exit_ts - self.entry_ts

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "pattern_type": self.pattern_type.value,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "pnl_bricks": self.pnl_bricks,
            "pnl_dollars": self.pnl_dollars,
            "status": self.status.value,
            "bricks_held": self.bricks_held,
            "signal_strength": self.signal_strength,
            "regime": self.regime,
            "regime_aligned": self.regime_aligned,
            "kelly_fraction": self.kelly_fraction,
            "velocity": self.velocity,
            "label_sequence": self.label_sequence,
            "filters_passed": self.filters_passed,
            "commission": self.commission,
            "slippage_cost": self.slippage_cost,
            "spread_cost": self.spread_cost,
            "total_cost": self.total_cost,
            "impact_cost": self.impact_cost,
            "borrow_cost": self.borrow_cost,
            "margin_cost": self.margin_cost,
            "dividend_cost": self.dividend_cost,
            "total_financing_cost": self.total_financing_cost,
            "fill_probability": self.fill_probability,
            "missed_fill": self.missed_fill,
            "predicted_p_win": self.predicted_p_win,
            "aggression": self.aggression,
            "p_regime": self.p_regime,
            "p_pattern": self.p_pattern,
            "p_markov": self.p_markov,
            "p_timesfm": self.p_timesfm,
        }


# ── Journal ────────────────────────────────────────────────────────


class TradeJournal:
    """
    Records trades and computes rolling statistics.

    Usage::

        journal = TradeJournal()
        journal.record(trade_record)
        stats = journal.get_stats()
    """
    """
    Records trades and computes rolling statistics.

    Usage::

        journal = TradeJournal()
        journal.record(trade_record)
        stats = journal.get_stats()
    """

    def __init__(self, max_records: int = 10_000):
        self.max_records = max_records
        self.trades: Deque[TradeRecord] = deque(maxlen=max_records)
        self._trade_counter: int = 0

    # ── Public API ────────────────────────────────────────────────

    def record(self, trade: TradeRecord) -> None:
        """Record a completed trade into the in-memory stats deque.

        No live write of any kind happens here — see module docstring
        ("fully deprecated 2026-09-09") for why: this backend never
        executes trades or journals live, on any deployment, ever, so
        there was nothing left to gate.
        """
        self.trades.append(trade)
        self._trade_counter += 1

    def create_record(
        self,
        symbol: str,
        filtered: FilteredSignal,
        result: TradeResult,
        regime: Optional[str] = None,
        prediction_data: Optional[Dict[str, float]] = None,
    ) -> TradeRecord:
        """Factory method to create a TradeRecord from pipeline data."""
        self._trade_counter += 1
        signal = filtered.original_signal

        # Use prediction data if provided, otherwise use defaults
        p_win = prediction_data.get("predicted_p_win", 0.5) if prediction_data else 0.5
        p_regime = prediction_data.get("p_regime") if prediction_data else None
        p_pattern = prediction_data.get("p_pattern") if prediction_data else None
        p_markov = prediction_data.get("p_markov") if prediction_data else None
        p_timesfm = prediction_data.get("p_timesfm") if prediction_data else None

        return TradeRecord(
            trade_id=f"R{self._trade_counter:06d}",
            symbol=symbol,
            direction=result.direction,
            pattern_type=signal.pattern_type,
            qty=result.qty if hasattr(result, 'qty') else 1.0,
            entry_price=result.entry_price,
            exit_price=result.exit_price,
            entry_ts=result.entry_ts,
            exit_ts=result.exit_ts,
            pnl_bricks=result.pnl_bricks,
            pnl_dollars=result.pnl_dollars,
            status=result.status,
            bricks_held=result.bricks_held,
            signal_strength=signal.strength,
            regime=regime,
            regime_aligned=filtered.regime_aligned,
            kelly_fraction=filtered.kelly_fraction,
            velocity=signal.velocity,
            label_sequence=signal.label_sequence,
            filters_passed=filtered.passed_filters,
            predicted_p_win=p_win,
            aggression=filtered.aggression if hasattr(filtered, 'aggression') else "mid",
            p_regime=p_regime,
            p_pattern=p_pattern,
            p_markov=p_markov,
            p_timesfm=p_timesfm,
        )

    def get_stats(self, last_n: Optional[int] = None) -> Dict:
        """
        Compute comprehensive trading statistics.

        Parameters
        ----------
        last_n : int, optional
            Only use the last N trades. None = all.

        Returns
        -------
        dict with keys: total_trades, win_rate, profit_factor,
        avg_pnl_bricks, sharpe_estimate, max_drawdown_bricks,
        by_pattern, by_direction, by_regime, rolling_win_rate
        """
        trades = list(self.trades)
        if last_n is not None:
            trades = trades[-last_n:]

        if not trades:
            return {"total_trades": 0}

        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]

        total_pnl = sum(t.pnl_bricks for t in trades)
        gross_profit = sum(t.pnl_bricks for t in wins)
        gross_loss = abs(sum(t.pnl_bricks for t in losses))

        # Cumulative P&L for drawdown
        cum_pnl = []
        running = 0.0
        for t in trades:
            running += t.pnl_bricks
            cum_pnl.append(running)

        max_dd = 0.0
        peak = 0.0
        for val in cum_pnl:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd

        # Sharpe estimate — using ddof=1 (sample standard deviation)
        # Previously used ddof=0 (population std) which inflates Sharpe
        # for small samples. ddof=1 is the correct choice for sample data
        # and matches industry standard (pandas, quantlib, etc.)
        if len(trades) > 1:
            import numpy as np
            returns = np.array([t.pnl_bricks for t in trades])
            mean_r = returns.mean()
            std_r = returns.std(ddof=1)  # Sample std — correct for backtested trade samples
            sharpe = mean_r / std_r if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        stats = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades),
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else _PROFIT_FACTOR_CAP,
            "total_pnl_bricks": round(total_pnl, 4),
            "avg_pnl_bricks": round(total_pnl / len(trades), 4),
            "avg_win_bricks": round(gross_profit / len(wins), 4) if wins else 0.0,
            "avg_loss_bricks": round(gross_loss / len(losses), 4) if losses else 0.0,
            "max_drawdown_bricks": round(max_dd, 4),
            "sharpe_estimate": round(sharpe, 4),
            "avg_bricks_held": round(sum(t.bricks_held for t in trades) / len(trades), 2),
            "avg_duration_seconds": round(sum(t.duration_seconds for t in trades) / len(trades), 2),
        }

        # Per-pattern breakdown
        by_pattern: Dict[str, Dict] = {}
        for pt in PatternType:
            pt_trades = [t for t in trades if t.pattern_type == pt]
            if pt_trades:
                pt_wins = [t for t in pt_trades if t.is_win]
                by_pattern[pt.value] = {
                    "count": len(pt_trades),
                    "win_rate": len(pt_wins) / len(pt_trades),
                    "avg_pnl_bricks": round(sum(t.pnl_bricks for t in pt_trades) / len(pt_trades), 4),
                }
        stats["by_pattern"] = by_pattern

        # Per-direction breakdown
        longs = [t for t in trades if t.direction == SignalDirection.LONG]
        shorts = [t for t in trades if t.direction == SignalDirection.SHORT]
        stats["by_direction"] = {
            "long": {
                "count": len(longs),
                "win_rate": len([t for t in longs if t.is_win]) / len(longs) if longs else 0.0,
                "avg_pnl": round(sum(t.pnl_bricks for t in longs) / len(longs), 4) if longs else 0.0,
            },
            "short": {
                "count": len(shorts),
                "win_rate": len([t for t in shorts if t.is_win]) / len(shorts) if shorts else 0.0,
                "avg_pnl": round(sum(t.pnl_bricks for t in shorts) / len(shorts), 4) if shorts else 0.0,
            },
        }

        # Per-regime breakdown
        by_regime: Dict[str, Dict] = {}
        for t in trades:
            r = t.regime or "unknown"
            if r not in by_regime:
                by_regime[r] = {"count": 0, "wins": 0, "pnl": 0.0}
            by_regime[r]["count"] += 1
            if t.is_win:
                by_regime[r]["wins"] += 1
            by_regime[r]["pnl"] += t.pnl_bricks
        for r in by_regime:
            c = by_regime[r]["count"]
            by_regime[r]["win_rate"] = by_regime[r]["wins"] / c if c > 0 else 0.0
            by_regime[r]["avg_pnl"] = round(by_regime[r]["pnl"] / c, 4) if c > 0 else 0.0
        stats["by_regime"] = by_regime

        # Rolling win rate (last 20 trades)
        if len(trades) >= 20:
            last_20 = trades[-20:]
            stats["rolling_win_rate_20"] = len([t for t in last_20 if t.is_win]) / 20
        else:
            stats["rolling_win_rate_20"] = stats["win_rate"]

        # Kelly input estimates
        stats["kelly_inputs"] = {
            "win_rate": stats["win_rate"],
            "avg_win": stats["avg_win_bricks"],
            "avg_loss": stats["avg_loss_bricks"],
            "implied_kelly": self._calc_kelly(
                stats["win_rate"], stats["avg_win_bricks"], stats["avg_loss_bricks"]
            ),
        }

        # Cost summary (Phase 2 + 7)
        total_commission = sum(t.commission for t in trades)
        total_slippage = sum(t.slippage_cost for t in trades)
        total_spread = sum(t.spread_cost for t in trades)
        total_impact = sum(t.impact_cost for t in trades)
        total_borrow = sum(t.borrow_cost for t in trades)
        total_margin = sum(t.margin_cost for t in trades)
        total_dividend = sum(t.dividend_cost for t in trades)
        total_financing = sum(t.total_financing_cost for t in trades)
        total_all_costs = sum(t.total_cost + t.impact_cost + t.total_financing_cost for t in trades)

        stats["cost_summary"] = {
            "commission": round(total_commission, 2),
            "slippage": round(total_slippage, 2),
            "spread": round(total_spread, 2),
            "market_impact": round(total_impact, 2),
            "borrow": round(total_borrow, 2),
            "margin": round(total_margin, 2),
            "dividend": round(total_dividend, 2),
            "total_financing": round(total_financing, 2),
            "total_all_costs": round(total_all_costs, 2),
            "cost_per_trade": round(total_all_costs / len(trades), 2) if trades else 0.0,
            "total_cost_pct_of_pnl": round(total_all_costs / abs(total_pnl) * 100, 2) if abs(total_pnl) > 0 else 0.0,
        }

        # Fill statistics (Phase 7)
        fill_probs = [t.fill_probability for t in trades]
        missed_fills = sum(1 for t in trades if t.missed_fill)
        stats["fill_stats"] = {
            "avg_fill_probability": round(sum(fill_probs) / len(fill_probs), 4) if fill_probs else 1.0,
            "missed_fills": missed_fills,
            "missed_fill_rate": round(missed_fills / len(trades), 4) if trades else 0.0,
        }

        # Dollar-denominated metrics (Phase 4A)
        initial_capital = 100_000.0  # Default; can be overridden
        total_pnl_dollars = sum(t.pnl_dollars for t in trades)
        net_pnl_dollars = total_pnl_dollars - total_all_costs
        stats["dollar_stats"] = {
            "initial_capital": initial_capital,
            "gross_pnl_dollars": round(total_pnl_dollars, 2),
            "total_costs_dollars": round(total_all_costs, 2),
            "net_pnl_dollars": round(net_pnl_dollars, 2),
            "net_return_pct": round(net_pnl_dollars / initial_capital * 100, 4),
        }

        return stats

    def get_kelly_estimate(self, last_n: int = 50) -> float:
        """
        Get Kelly fraction estimate from recent trade history.

        Uses half-Kelly by default for safety.
        """
        stats = self.get_stats(last_n=last_n)
        if "kelly_inputs" not in stats:
            return 0.05  # Conservative default

        kelly = stats["kelly_inputs"]["implied_kelly"]
        return max(min(kelly * 0.5, 0.20), 0.01)  # Half-Kelly, capped [1%, 20%]

    @staticmethod
    def _calc_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
        """Calculate full Kelly fraction: f = p - q/b where b = avg_win/avg_loss."""
        if avg_loss <= 0 or avg_win <= 0:
            return 0.05
        b = avg_win / avg_loss
        q = 1.0 - win_rate
        kelly = win_rate - q / b
        return max(kelly, 0.0)

    def reset(self) -> None:
        """Reset journal state."""
        self.trades.clear()
        self._trade_counter = 0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

