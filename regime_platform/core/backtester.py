"""
Backtesting Engine
══════════════════
Walk-forward backtest with regime-aware strategy signals, Kelly sizing, and risk checks.

Pipeline per bar:
  1. Fit HMM on trailing window → RegimeSnapshot
  2. Generate strategy signal (long/short/flat) based on regime + trend
  3. Compute position size via Kelly + regime gating
  4. Apply risk check (VaR, max drawdown, regime limits)
  5. Execute trade, track PnL and equity curve
  6. Compute 30+ performance metrics at the end
"""

from __future__ import annotations

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime

from .regime_engine import RegimeHMM, RegimeSnapshot
from .position_sizer import PositionSizer, PositionRequest
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for a walk-forward backtest."""
    window: int = 200                 # trailing window for HMM fit
    refit_every: int = 50             # refit HMM every N bars
    n_hmm_states: int = 4             # HMM states (2-4)
    kelly_fraction: float = 0.5       # fractional Kelly
    target_vol: float = 0.15          # annualised target vol
    base_risk_limit: float = 0.02     # max daily loss fraction
    initial_equity: float = 100000.0  # starting capital
    commission_bps: float = 5.0       # commission in basis points
    slippage_bps: float = 2.0         # slippage in basis points
    max_position_pct: float = 0.25    # max position as fraction of equity
    risk_check: bool = True           # enable VaR/CVaR risk gates
    regime_gate: bool = True          # block trades in high-vol bear regimes


@dataclass
class BacktestTrade:
    """Single trade record in the backtest."""
    bar: int
    date_idx: int
    signal: str                    # "long", "short", "flat", "exit_long", "exit_short"
    regime_label: str
    regime_multiplier: float
    position_size: float           # fraction of equity
    entry_price: float
    exit_price: Optional[float] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    commission: float = 0.0       # Commission only (separate from slippage)
    slippage_cost: float = 0.0    # Slippage only (separate from commission)
    total_cost: float = 0.0       # commission + slippage_cost
    hold_bars: int = 0
    exit_reason: str = ""
    entry_date: Optional[str] = None   # ISO date string if dates provided
    exit_date: Optional[str] = None    # ISO date string if dates provided


@dataclass
class BacktestResult:
    """Comprehensive backtest result with 30+ metrics."""
    # Summary
    symbol: str
    n_bars: int
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    
    # Returns
    total_return: float
    annual_return: float
    buy_hold_return: float
    excess_return: float
    monthly_return_avg: float
    
    # Risk
    max_drawdown: float
    max_drawdown_duration: int
    avg_drawdown: float
    var_95: float
    cvar_95: float
    annual_vol: float
    downside_vol: float
    
    # Ratios
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    information_ratio: float
    
    # Trade stats
    avg_win_pct: float
    avg_loss_pct: float
    avg_win_loss_ratio: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    avg_hold_bars: float
    profit_factor: float

    # Regime stats
    regime_distribution: dict       # {label: count}
    trades_by_regime: dict          # {label: {wins, losses, avg_pnl}}

    # Equity curve
    equity_curve: list              # [equity at each bar]
    drawdown_curve: list            # [drawdown at each bar]
    trade_log: list                 # [BacktestTrade dicts]

    # Aggregate transaction costs (after non-default fields)
    total_commission: float = 0.0       # Total commission across all trades
    total_slippage: float = 0.0         # Total slippage cost across all trades
    total_cost: float = 0.0             # total_commission + total_slippage
    gross_return: float = 0.0           # Return before transaction costs
    cost_drag_pct: float = 0.0          # total_cost as % of gross P&L
    avg_cost_per_trade: float = 0.0     # Average total cost per trade

    # Config snapshot (for persistence / comparison)
    config_used: dict = field(default_factory=dict)


class Backtester:
    """
    Walk-forward backtester with regime-aware strategy.
    
    Strategy logic:
      - Low vol + bull/strong_bull → long
      - High vol + bear/strong_bear → short (if enabled)
      - All other regimes → flat / exit
      - Risk gate: block new trades if CVaR exceeds base_risk_limit
    
    Supports n-state HMM (2, 3, or 4 states) with auto-mapped labels.
    """
    
    def run(
        self,
        prices: np.ndarray,
        symbol: str = "UNKNOWN",
        config: Optional[BacktestConfig] = None,
        dates: Optional[list[str]] = None,
    ) -> BacktestResult:
        cfg = config or BacktestConfig()
        prices = np.asarray(prices, dtype=float)
        n = len(prices)
        
        if n < cfg.window + 20:
            raise ValueError(
                f"Need at least {cfg.window + 20} bars for backtest. Got {n}."
            )
        
        # Validate dates if provided
        if dates is not None and len(dates) != n:
            raise ValueError(
                f"dates length ({len(dates)}) must match prices length ({n})."
            )
        
        # Get n-state label maps for signal generation
        n_states = cfg.n_hmm_states
        vol_labels = RegimeHMM.VOL_LABELS_BY_N.get(n_states, RegimeHMM.VOL_LABELS_BY_N[4])
        trend_labels = RegimeHMM.TREND_LABELS_BY_N.get(n_states, RegimeHMM.TREND_LABELS_BY_N[4])
        
        equity = cfg.initial_equity
        equity_curve = [equity]
        drawdown_curve = [0.0]
        peak_equity = equity
        position = 0.0          # fraction of equity in position
        position_dir = 0        # 0 = flat, 1 = long, -1 = short
        entry_price = 0.0
        entry_equity = 0.0      # equity at trade entry (for pnl_pct)
        entry_bar = 0
        trades: List[BacktestTrade] = []
        regime_counts = {}
        trades_by_regime = {}
        
        hmm = RegimeHMM(n_components=cfg.n_hmm_states)
        sizer = PositionSizer()
        riskman = RiskManager()
        last_fit_bar = -(cfg.refit_every + 1)  # force first fit
        snap = None
        
        daily_returns = []
        consec_wins = 0
        consec_losses = 0
        max_consec_wins = 0
        max_consec_losses = 0
        buy_hold_start = prices[0]
        
        for bar_idx in range(cfg.window, n):
            # Use prices up to (but not including) current bar for HMM fit
            # to avoid look-ahead bias. Use bar_idx as the execution price.
            window_prices = prices[bar_idx - cfg.window : bar_idx]
            current_price = prices[bar_idx]
            
            # Refit HMM periodically
            if bar_idx - last_fit_bar >= cfg.refit_every:
                try:
                    hmm = RegimeHMM(n_components=cfg.n_hmm_states)
                    hmm.fit(window_prices)
                    # Predict on the full window including current bar for signal
                    snap = hmm.predict(prices[bar_idx - cfg.window : bar_idx + 1])
                    last_fit_bar = bar_idx
                except Exception as exc:
                    logger.warning("HMM refit failed at bar %d: %s", bar_idx, exc)
                    snap = None
            
            # If no snap, try predict with existing model
            if snap is None and hmm.fitted:
                try:
                    snap = hmm.predict(prices[bar_idx - cfg.window : bar_idx + 1])
                except Exception as exc:
                    logger.debug("HMM predict failed at bar %d: %s", bar_idx, exc)
                    snap = None
            
            # Track regime distribution
            if snap:
                label = snap.regime_label
                regime_counts[label] = regime_counts.get(label, 0) + 1
            
            # Generate strategy signal (n-state aware)
            signal = self._generate_signal(snap, cfg, vol_labels, trend_labels)
            
            # Calculate PnL for existing position
            if position_dir != 0 and bar_idx > 0:
                prev_price = prices[bar_idx - 1]
                if prev_price > 0:
                    if position_dir == 1:
                        daily_ret = (current_price - prev_price) / prev_price
                    else:
                        daily_ret = (prev_price - current_price) / prev_price
                    
                    # Apply position size
                    position_pnl = equity * position * daily_ret
                    equity += position_pnl
            
            daily_returns.append((equity - equity_curve[-1]) / max(equity_curve[-1], 1.0))
            
            # Update peak and drawdown
            peak_equity = max(peak_equity, equity)
            dd = (equity - peak_equity) / peak_equity if peak_equity > 0 else 0.0
            equity_curve.append(equity)
            drawdown_curve.append(dd)
            
            # Check if we need to exit
            if position_dir != 0:
                # Exit conditions
                should_exit = False
                exit_reason = ""
                
                if signal == "flat" or (position_dir == 1 and signal == "short") or (position_dir == -1 and signal == "long"):
                    should_exit = True
                    exit_reason = "signal_reversal"
                elif snap and cfg.regime_gate:
                    if position_dir == 1 and snap.risk_multiplier < 0.3:
                        should_exit = True
                        exit_reason = "regime_exit"
                    elif position_dir == -1 and snap.vol_state in vol_labels[:len(vol_labels)//2] and snap.trend_state in trend_labels[len(trend_labels)//2:]:
                        should_exit = True
                        exit_reason = "regime_exit"
                
                # Risk check: stop loss if drawdown exceeds 5× base_risk_limit
                max_dd_threshold = -(cfg.base_risk_limit * 5)
                if cfg.risk_check and dd < max_dd_threshold:
                    should_exit = True
                    exit_reason = "max_drawdown_stop"
                
                if should_exit:
                    # Close position
                    commission = abs(position * equity) * cfg.commission_bps / 10000
                    slippage = abs(position * equity) * cfg.slippage_bps / 10000
                    total_cost = commission + slippage
                    equity -= total_cost
                    
                    trade_pnl = 0.0  # default
                    if trades and trades[-1].exit_price is None:
                        trades[-1].exit_price = current_price
                        # PnL = exit equity contribution - entry equity contribution
                        if entry_price > 0:
                            price_change_pct = (current_price - entry_price) / entry_price * position_dir
                            trades[-1].pnl = entry_equity * position * price_change_pct - total_cost
                            trades[-1].pnl_pct = price_change_pct * position  # percentage return on position
                        trades[-1].commission = commission
                        trades[-1].slippage_cost = slippage
                        trades[-1].total_cost = total_cost
                        trades[-1].hold_bars = bar_idx - entry_bar
                        trades[-1].exit_reason = exit_reason
                        if dates is not None:
                            trades[-1].exit_date = dates[bar_idx] if bar_idx < len(dates) else None
                        trade_pnl = trades[-1].pnl
                    
                    if trade_pnl > 0:
                        consec_wins += 1
                        consec_losses = 0
                        max_consec_wins = max(max_consec_wins, consec_wins)
                    elif trade_pnl < 0:
                        consec_losses += 1
                        consec_wins = 0
                        max_consec_losses = max(max_consec_losses, consec_losses)
                    
                    # Track by regime
                    regime_label = snap.regime_label if snap else "unknown"
                    if regime_label not in trades_by_regime:
                        trades_by_regime[regime_label] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                    if trade_pnl > 0:
                        trades_by_regime[regime_label]["wins"] += 1
                    else:
                        trades_by_regime[regime_label]["losses"] += 1
                    trades_by_regime[regime_label]["total_pnl"] += trade_pnl
                    
                    position = 0.0
                    position_dir = 0
                    entry_price = 0.0
                    entry_equity = 0.0
            
            # Enter new position
            if position_dir == 0 and signal in ("long", "short"):
                # Compute position size via Kelly
                size_result = None
                if snap and len(window_prices) > 20:
                    try:
                        returns = np.diff(np.log(window_prices))
                        size_result = sizer.size(PositionRequest(
                            returns=returns.tolist(),
                            kelly_fraction=cfg.kelly_fraction,
                            target_vol=cfg.target_vol,
                            regime=snap,
                        ))
                    except Exception:
                        size_result = None
                
                pos_size = size_result.recommended_f if size_result else 0.1
                pos_size = min(pos_size, cfg.max_position_pct)
                
                # Risk gate
                if cfg.risk_check and len(daily_returns) > 20:
                    recent_returns = np.array(daily_returns[-60:])
                    var_95 = float(-np.percentile(recent_returns, 5))
                    if var_95 * pos_size > cfg.base_risk_limit:
                        pos_size *= 0.5  # halve position size
                
                if snap and cfg.regime_gate and snap.risk_multiplier < 0.3:
                    pos_size = 0.0  # block trade
                
                if pos_size > 0.01:
                    position = pos_size
                    position_dir = 1 if signal == "long" else -1
                    entry_price = current_price
                    entry_equity = equity
                    entry_bar = bar_idx
                    
                    commission = position * equity * cfg.commission_bps / 10000
                    slippage = position * equity * cfg.slippage_bps / 10000
                    total_cost = commission + slippage
                    equity -= total_cost
                    
                    trade = BacktestTrade(
                        bar=bar_idx,
                        date_idx=bar_idx,
                        signal=signal,
                        regime_label=snap.regime_label if snap else "unknown",
                        regime_multiplier=snap.risk_multiplier if snap else 1.0,
                        position_size=position,
                        entry_price=entry_price,
                        commission=commission,
                        slippage_cost=slippage,
                        total_cost=total_cost,
                        entry_date=dates[bar_idx] if dates is not None and bar_idx < len(dates) else None,
                    )
                    trades.append(trade)
        
        # Close any open position at end
        if position_dir != 0 and trades:
            exit_price = prices[-1]
            trades[-1].exit_price = exit_price
            if entry_price > 0:
                price_change_pct = (exit_price - entry_price) / entry_price * position_dir
                trades[-1].pnl = entry_equity * position * price_change_pct
                trades[-1].pnl_pct = price_change_pct * position
            trades[-1].hold_bars = len(prices) - 1 - entry_bar
            trades[-1].exit_reason = "end_of_data"
            if dates is not None:
                trades[-1].exit_date = dates[-1] if len(dates) > 0 else None
            equity += trades[-1].pnl
            equity_curve.append(equity)
        
        # Calculate metrics
        return self._compute_metrics(
            equity_curve, drawdown_curve, daily_returns, trades,
            trades_by_regime, regime_counts, prices, symbol, cfg
        )
    
    def _generate_signal(
        self,
        snap: Optional[RegimeSnapshot],
        cfg: BacktestConfig,
        vol_labels: list[str],
        trend_labels: list[str],
    ) -> str:
        """Generate a trading signal based on regime state (n-state aware)."""
        if snap is None:
            return "flat"
        
        vol = snap.vol_state
        trend = snap.trend_state
        
        # "Low" half of vol labels + "bull" half of trend labels → long
        low_vol_labels = vol_labels[:len(vol_labels) // 2]      # e.g. ["low", "med_low"] for 4-state
        bull_trend_labels = trend_labels[len(trend_labels) // 2:]  # e.g. ["bull", "strong_bull"] for 4-state
        
        # "High" half of vol labels + "bear" half of trend labels → short
        high_vol_labels = vol_labels[len(vol_labels) // 2:]     # e.g. ["med_high", "high"] for 4-state
        bear_trend_labels = trend_labels[:len(trend_labels) // 2]  # e.g. ["strong_bear", "bear"] for 4-state
        
        # Long conditions
        if vol in low_vol_labels and trend in bull_trend_labels:
            return "long"
        
        # Short conditions (only in high vol bear regimes)
        if vol in high_vol_labels and trend in bear_trend_labels:
            return "short"
        
        return "flat"
    
    def _compute_metrics(
        self,
        equity_curve: list,
        drawdown_curve: list,
        daily_returns: list,
        trades: list,
        trades_by_regime: dict,
        regime_counts: dict,
        prices: np.ndarray,
        symbol: str,
        cfg: BacktestConfig,
    ) -> BacktestResult:
        """Compute 30+ backtest performance metrics."""
        eq = np.array(equity_curve)
        dd = np.array(drawdown_curve)
        rets = np.array(daily_returns) if daily_returns else np.array([0.0])
        
        # Basic stats
        n_trades = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        n_wins = len(wins)
        n_losses = len(losses)
        win_rate = n_wins / n_trades if n_trades > 0 else 0.0
        
        # Returns
        total_return = (eq[-1] / cfg.initial_equity - 1.0) if len(eq) > 0 else 0.0
        n_days = len(daily_returns) if daily_returns else 1
        annual_return = (1 + total_return) ** (252 / max(n_days, 1)) - 1 if total_return > -1 else -1.0
        buy_hold_return = float(prices[-1] / prices[0] - 1) if len(prices) > 1 and prices[0] > 0 else 0.0
        excess_return = total_return - buy_hold_return
        monthly_return_avg = total_return / max(n_days / 21, 1)
        
        # Risk
        max_dd = float(np.min(dd)) if len(dd) > 0 else 0.0
        
        # Max DD duration
        max_dd_duration = 0
        current_dd_duration = 0
        for d in dd:
            if d < 0:
                current_dd_duration += 1
                max_dd_duration = max(max_dd_duration, current_dd_duration)
            else:
                current_dd_duration = 0
        
        avg_dd = float(np.mean(dd[dd < 0])) if np.any(dd < 0) else 0.0
        
        var_95 = float(-np.percentile(rets, 5)) if len(rets) > 10 else 0.0
        cvar_95_tail = rets[rets <= -var_95] if len(rets) > 10 else np.array([0.0])
        cvar_95 = float(-np.mean(cvar_95_tail)) if len(cvar_95_tail) > 0 else var_95
        
        annual_vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0.0
        neg_rets = rets[rets < 0]
        downside_vol = float(np.std(neg_rets, ddof=1) * np.sqrt(252)) if len(neg_rets) > 1 else annual_vol
        
        # Ratios
        sharpe = float(annual_return / annual_vol) if annual_vol > 0 else 0.0
        sortino = float(annual_return / downside_vol) if downside_vol > 0 else 0.0
        calmar = float(annual_return / abs(max_dd)) if max_dd != 0 else 0.0
        information = float(excess_return / annual_vol) if annual_vol > 0 else 0.0
        
        # Trade stats — now using pnl_pct (fixed from always-0 bug)
        win_pcts = [t.pnl_pct for t in wins] if wins else [0.0]
        loss_pcts = [abs(t.pnl_pct) for t in losses] if losses else [0.0]
        avg_win_pct = float(np.mean(win_pcts))
        avg_loss_pct = float(np.mean(loss_pcts))
        avg_win_loss = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else 999.0
        
        avg_hold = float(np.mean([t.hold_bars for t in trades])) if trades else 0.0
        
        gross_profit = sum(t.pnl for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.001
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0
        
        # Compute max consecutive from trades
        consec_wins = 0
        consec_losses = 0
        max_cw = 0
        max_cl = 0
        for t in trades:
            if t.pnl > 0:
                consec_wins += 1
                consec_losses = 0
                max_cw = max(max_cw, consec_wins)
            elif t.pnl < 0:
                consec_losses += 1
                consec_wins = 0
                max_cl = max(max_cl, consec_losses)
        
        # Compute avg pnl by regime
        for label, data in trades_by_regime.items():
            total_trades_r = data["wins"] + data["losses"]
            data["avg_pnl"] = data["total_pnl"] / total_trades_r if total_trades_r > 0 else 0.0

        # ── Aggregate transaction costs ──────────────────────────────
        total_commission = sum(t.commission for t in trades) if trades else 0.0
        total_slippage = sum(t.slippage_cost for t in trades) if trades else 0.0
        total_cost_agg = sum(t.total_cost for t in trades) if trades else 0.0
        # Gross return = net return + cost drag (approximation)
        # gross_return represents the return the strategy would have achieved
        # without any transaction costs, expressed as a fraction of initial equity.
        initial_equity = cfg.initial_equity
        gross_pnl_dollars = sum(t.pnl for t in trades) + total_cost_agg if trades else 0.0
        gross_return = total_return + (total_cost_agg / initial_equity)
        cost_drag_pct = (total_cost_agg / abs(gross_pnl_dollars) * 100) if gross_pnl_dollars != 0 else 0.0
        avg_cost_per_trade = total_cost_agg / n_trades if n_trades > 0 else 0.0

        return BacktestResult(
            symbol=symbol,
            n_bars=len(prices),
            n_trades=n_trades,
            n_wins=n_wins,
            n_losses=n_losses,
            win_rate=round(win_rate, 4),
            total_return=round(total_return, 6),
            annual_return=round(annual_return, 6),
            buy_hold_return=round(buy_hold_return, 6),
            excess_return=round(excess_return, 6),
            monthly_return_avg=round(monthly_return_avg, 6),
            max_drawdown=round(max_dd, 6),
            max_drawdown_duration=max_dd_duration,
            avg_drawdown=round(avg_dd, 6),
            var_95=round(var_95, 6),
            cvar_95=round(cvar_95, 6),
            annual_vol=round(annual_vol, 6),
            downside_vol=round(downside_vol, 6),
            sharpe_ratio=round(sharpe, 4),
            sortino_ratio=round(sortino, 4),
            calmar_ratio=round(calmar, 4),
            information_ratio=round(information, 4),
            avg_win_pct=round(avg_win_pct, 6),
            avg_loss_pct=round(avg_loss_pct, 6),
            avg_win_loss_ratio=round(avg_win_loss, 4),
            max_consecutive_wins=max_cw,
            max_consecutive_losses=max_cl,
            avg_hold_bars=round(avg_hold, 1),
            profit_factor=round(profit_factor, 4),
            total_commission=round(total_commission, 4),
            total_slippage=round(total_slippage, 4),
            total_cost=round(total_cost_agg, 4),
            gross_return=round(gross_return, 6),
            cost_drag_pct=round(cost_drag_pct, 2),
            avg_cost_per_trade=round(avg_cost_per_trade, 4),
            regime_distribution=regime_counts,
            trades_by_regime=trades_by_regime,
            equity_curve=[round(x, 2) for x in equity_curve],
            drawdown_curve=[round(x, 6) for x in drawdown_curve],
            trade_log=[{
                "bar": t.bar,
                "date_idx": t.date_idx,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "signal": t.signal,
                "regime_label": t.regime_label,
                "regime_multiplier": round(t.regime_multiplier, 4),
                "position_size": round(t.position_size, 4),
                "entry_price": round(t.entry_price, 4),
                "exit_price": round(t.exit_price, 4) if t.exit_price is not None else None,
                "pnl": round(t.pnl, 4),
                "pnl_pct": round(t.pnl_pct, 6),
                "commission": round(t.commission, 4),
                "slippage_cost": round(t.slippage_cost, 4),
                "total_cost": round(t.total_cost, 4),
                "hold_bars": t.hold_bars,
                "exit_reason": t.exit_reason,
            } for t in trades],
            config_used={
                "window": cfg.window,
                "refit_every": cfg.refit_every,
                "n_hmm_states": cfg.n_hmm_states,
                "kelly_fraction": cfg.kelly_fraction,
                "target_vol": cfg.target_vol,
                "base_risk_limit": cfg.base_risk_limit,
                "initial_equity": cfg.initial_equity,
                "commission_bps": cfg.commission_bps,
                "slippage_bps": cfg.slippage_bps,
                "max_position_pct": cfg.max_position_pct,
                "risk_check": cfg.risk_check,
                "regime_gate": cfg.regime_gate,
            },
        )
