"""
Trade Journal + Stats Engine - records every trade and computes
performance metrics for strategy evaluation and Kelly sizing.

Tracks:
  - Every signal, filter decision, and order result
  - P&L in both bricks and dollars
  - Win rate, profit factor, Sharpe, max drawdown
  - Per-pattern-type breakdowns
  - Rolling statistics for adaptive Kelly sizing
  - Calibration records (v3) — predicted P_win vs actual outcome
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from collections import deque

from renko.pattern_detector import PatternType, SignalDirection
from renko.risk_manager import PositionStatus, TradeResult
from renko.signal_filter import FilteredSignal

log = logging.getLogger(__name__)


# ── Fire-and-forget task tracking ─────────────────────────────────────
# When we schedule a calibration write via ``loop.create_task()``, we
# must keep a reference to the Task or Python's garbage collector may
# collect it mid-execution (this is explicitly called out in the
# asyncio docs). We stash pending tasks here and discard them via a
# done-callback once they complete.
_PENDING_CALIBRATION_TASKS: set = set()


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
    p_ext_signal: Optional[float] = None # External signal vote breakdown
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
            "p_ext_signal": self.p_ext_signal,
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
        """Record a completed trade and fire calibration log.

        This method is **synchronous** but schedules an **async** fire-and-
        forget task to write the calibration record to Supabase. The
        previous implementation called ``asyncio.create_task()`` directly,
        which raises ``RuntimeError: no running event loop`` if invoked
        from outside an async context (e.g. from a backtest thread, or
        from any sync code path). That exception was silently swallowed
        by callers, so calibration records were never written.

        The fix handles three cases:
          1. Running event loop available → schedule task normally
          2. No running loop, but loop is set on this thread → use
             ``run_coroutine_threadsafe`` (cross-thread safe)
          3. No loop at all → log a warning (can't schedule)
        """
        self.trades.append(trade)
        self._trade_counter += 1
        # Fire-and-forget calibration record to Supabase
        self._schedule_calibration_write(trade)

    def _schedule_calibration_write(self, trade: TradeRecord) -> None:
        """Schedule the async calibration write, robust to sync contexts.

        Avoids the ``RuntimeWarning: coroutine was never awaited`` that
        would occur if we called ``asyncio.create_task(coro)`` and let it
        raise — the partially-constructed Task leaves the coro dangling.
        Instead, check for a running loop FIRST, and only construct the
        coro in the branch we're actually going to use.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop in this thread.
            # Spawn a daemon thread that runs a fresh loop just for this
            # write. Heavier than create_task but guarantees the write
            # actually happens when called from sync code.
            import threading
            thread = threading.Thread(
                target=_run_calibration_in_new_loop,
                args=(trade,),
                name=f"calib-{trade.symbol}",
                daemon=True,
            )
            thread.start()
            return

        # We have a running loop — schedule the coro as a task.
        # NOTE: create_task() returns a Task that we should keep a
        # reference to, otherwise Python's GC may collect it mid-run
        # (CPython docs explicitly warn about this for fire-and-forget
        # tasks). We stash refs in a module-level set.
        task = loop.create_task(_log_calibration_record(trade))
        _PENDING_CALIBRATION_TASKS.add(task)
        task.add_done_callback(_PENDING_CALIBRATION_TASKS.discard)

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
        p_ext_signal = prediction_data.get("p_ext_signal") if prediction_data else None
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
            p_ext_signal=p_ext_signal,
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


# ─── Calibration Record (fire-and-forget) ──────────────────────────────


# DB schema constraint: aggression must be one of these values.
# Anything else → HTTP 400 check-constraint violation.
_NT_CALIBRATION_VALID_AGRESSIONS = frozenset({"passive", "mid", "aggressive"})


def _normalize_aggression(value: str) -> str:
    """Map any aggression string to a DB-valid value.

    The DB has ``CHECK (aggression IN ('passive', 'mid', 'aggressive'))``.
    If the EV engine ever emits a value outside this set (e.g. ``moderate``,
    ``conservative``), the insert would fail. Map unknowns to ``mid``.
    """
    if value in _NT_CALIBRATION_VALID_AGRESSIONS:
        return value
    v = (value or "").lower()
    if "passive" in v or "conservative" in v or "low" in v:
        return "passive"
    if "aggress" in v or "high" in v:
        return "aggressive"
    # Everything else (including None, "", "moderate", "medium") → mid
    return "mid"


def _run_calibration_in_new_loop(trade: "TradeRecord") -> None:
    """Run the async calibration write in a fresh event loop.

    Used as a thread target when no event loop is available in the
    calling thread. The thread exits as soon as the write completes
    (or fails — errors are logged inside ``_log_calibration_record``).
    """
    try:
        asyncio.run(_log_calibration_record(trade))
    except Exception as exc:
        # Don't let the worker thread crash silently — log so we can
        # diagnose. Use a print as well because logging may not be
        # fully configured in worker threads.
        log.warning(
            "calibration write thread failed for %s: %s",
            getattr(trade, "symbol", "?"), exc,
        )


async def _log_calibration_record(trade: TradeRecord) -> None:
    """
    Fire-and-forget: log a calibration record to Supabase.

    Called from ``TradeJournal.record()`` on every closed trade.
    Failures are logged at WARNING level (not DEBUG) so they're visible
    in production — the previous implementation silently dropped errors
    by logging at DEBUG, which made the "no calibration records" bug
    invisible.
    """
    try:
        from regime_platform.core.settings import settings

        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            log.debug(
                "calibration write skipped for %s — SUPABASE_URL/KEY not set",
                trade.symbol,
            )
            return

        import httpx

        # Build the row. NOTE on column names:
        #   - Migration 100 (initial) used 'p_imbalance'
        #   - Migration 024 renamed it to 'p_ext_signal'
        # If migration 024 hasn't been applied on the target Supabase,
        # the column is still 'p_imbalance' and writing 'p_ext_signal'
        # will fail with PGRST204. We try 'p_ext_signal' first; if that
        # returns PGRST204, we retry once with 'p_imbalance'.
        row = {
            "id": str(uuid.uuid4()),
            "symbol": trade.symbol,
            "predicted_p_win": trade.predicted_p_win,
            "outcome": "win" if trade.is_win else "loss",
            # ── Normalize aggression to a DB-valid value ─────────────
            # The DB has CHECK (aggression IN ('passive', 'mid', 'aggressive')).
            # The EV engine emits these values directly, but if any other
            # value leaks in (e.g. from a future code path), the insert
            # would fail. Normalize defensively.
            "aggression": _normalize_aggression(trade.aggression),
            "actual_pnl": trade.pnl_dollars,
            "error_source": trade.status.value if hasattr(trade.status, "value") else str(trade.status),
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "p_regime": trade.p_regime,
            "p_ext_signal": trade.p_ext_signal,  # renamed from p_imbalance in migration 024
            "p_markov": trade.p_markov,
            "p_timesfm": trade.p_timesfm,
            "entered_at": datetime.fromtimestamp(trade.entry_ts, tz=timezone.utc).isoformat(),
            "closed_at": datetime.fromtimestamp(trade.exit_ts, tz=timezone.utc).isoformat(),
            # venue column (migration 031) — exchange where the trade was executed
            "venue": getattr(trade, "venue", None),
        }

        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
            "Content-Type": "application/json",
            # Prefer: return=minimal tells PostgREST to NOT return the
            # inserted row in the response body, which is faster and
            # avoids issues with computed/virtual columns.
            "Prefer": "return=minimal",
        }

        url = f"{settings.SUPABASE_URL}/rest/v1/nt_calibration_records"

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=row)

            # ── Handle schema drift: p_ext_signal vs p_imbalance ──────
            # If the column doesn't exist (migration 024 not applied),
            # retry once with the old column name.
            if resp.status_code == 400 and "p_ext_signal" in resp.text:
                log.warning(
                    "calibration write for %s: 'p_ext_signal' column not found "
                    "— retrying with legacy 'p_imbalance' name. Apply migration "
                    "024 to fix.",
                    trade.symbol,
                )
                row["p_imbalance"] = row.pop("p_ext_signal")
                resp = await client.post(url, headers=headers, json=row)

            if resp.status_code not in (200, 201, 204):
                # ── Log at WARNING so it's visible in production ───────
                # The previous implementation logged at DEBUG, which made
                # calibration failures invisible by default.
                log.warning(
                    "calibration record HTTP %s for %s: %s",
                    resp.status_code, trade.symbol, resp.text[:200],
                )
            else:
                log.debug(
                    "calibration record OK for %s (predicted=%.3f, outcome=%s)",
                    trade.symbol, trade.predicted_p_win,
                    "win" if trade.is_win else "loss",
                )

    except Exception as exc:
        # Same reasoning — WARNING, not DEBUG.
        log.warning(
            "calibration record failed (non-blocking) for %s: %s",
            getattr(trade, "symbol", "?"), exc,
        )
