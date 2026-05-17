"""
Trade Journal + Stats Engine — records every trade and computes
performance metrics for strategy evaluation and Kelly sizing.

Tracks:
  - Every signal, filter decision, and order result
  - P&L in both bricks and dollars
  - Win rate, profit factor, Sharpe, max drawdown
  - Per-pattern-type breakdowns
  - Rolling statistics for adaptive Kelly sizing
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from collections import deque

from renko.pattern_detector import PatternType, SignalDirection
from renko.risk_manager import PositionStatus, TradeResult
from renko.signal_filter import FilteredSignal


# ── Data types ─────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """Complete record of a single trade lifecycle."""

    trade_id: str
    symbol: str
    direction: SignalDirection
    pattern_type: PatternType
    entry_price: float
    exit_price: float
    entry_ts: float
    exit_ts: float
    pnl_bricks: float
    pnl_dollars: float
    status: PositionStatus
    bricks_held: int
    signal_strength: float
    regime: Optional[str] = None
    regime_aligned: bool = False
    kelly_fraction: float = 0.0
    velocity: float = 0.0
    label_sequence: List[str] = field(default_factory=list)
    filters_passed: List[str] = field(default_factory=list)

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

    def __init__(self, max_records: int = 10_000):
        self.max_records = max_records
        self.trades: Deque[TradeRecord] = deque(maxlen=max_records)
        self._trade_counter: int = 0

    # ── Public API ────────────────────────────────────────────────

    def record(self, trade: TradeRecord) -> None:
        """Record a completed trade."""
        self.trades.append(trade)
        self._trade_counter += 1

    def create_record(
        self,
        symbol: str,
        filtered: FilteredSignal,
        result: TradeResult,
        regime: Optional[str] = None,
    ) -> TradeRecord:
        """Factory method to create a TradeRecord from pipeline data."""
        self._trade_counter += 1
        signal = filtered.original_signal

        return TradeRecord(
            trade_id=f"R{self._trade_counter:06d}",
            symbol=symbol,
            direction=result.direction,
            pattern_type=signal.pattern_type,
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

        # Sharpe estimate (simplified)
        if len(trades) > 1:
            import numpy as np
            returns = np.array([t.pnl_bricks for t in trades])
            mean_r = returns.mean()
            std_r = returns.std()
            sharpe = mean_r / std_r if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        stats = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades),
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
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
