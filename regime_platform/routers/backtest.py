"""Backtest endpoints — walk-forward backtesting with regime-aware strategy."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import BacktestRequest, BacktestResponse
from ..core.backtester import Backtester, BacktestConfig
from ..auth.jwt_auth import get_authed_user, TokenData
import numpy as np

router = APIRouter(prefix="/backtest", tags=["Backtesting"])
backtester = Backtester()


@router.post(
    "/run",
    response_model=BacktestResponse,
    summary="Run walk-forward backtest with regime-aware strategy (30+ metrics)",
)
async def run_backtest(req: BacktestRequest, user: TokenData = Depends(get_authed_user)):
    """
    Walk-forward backtest pipeline:
    1. Rolling HMM fit on trailing window
    2. Strategy signal generation based on regime
    3. Regime-gated Kelly position sizing
    4. Risk check gates (VaR, max drawdown)
    5. Trade execution with commission/slippage
    6. Comprehensive performance metrics (30+)

    Returns:
    - Full equity curve and drawdown curve
    - Return metrics (total, annual, excess over buy-and-hold)
    - Risk metrics (VaR, CVaR, max drawdown, vol)
    - Ratio metrics (Sharpe, Sortino, Calmar, information)
    - Trade statistics (win rate, profit factor, consecutive wins/losses)
    - Regime distribution and per-regime trade breakdown
    - Full trade log
    """
    try:
        prices = np.array(req.prices, dtype=float)
        config = BacktestConfig(
            window=req.window,
            refit_every=req.refit_every,
            n_hmm_states=req.n_hmm_states,
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            base_risk_limit=req.base_risk_limit,
            initial_equity=req.initial_equity,
            commission_bps=req.commission_bps,
            slippage_bps=req.slippage_bps,
            max_position_pct=req.max_position_pct,
            risk_check=req.risk_check,
            regime_gate=req.regime_gate,
        )
        result = backtester.run(prices, symbol=req.symbol, config=config)

        return BacktestResponse(
            symbol=result.symbol,
            n_bars=result.n_bars,
            n_trades=result.n_trades,
            n_wins=result.n_wins,
            n_losses=result.n_losses,
            win_rate=result.win_rate,
            total_return=result.total_return,
            annual_return=result.annual_return,
            buy_hold_return=result.buy_hold_return,
            excess_return=result.excess_return,
            monthly_return_avg=result.monthly_return_avg,
            max_drawdown=result.max_drawdown,
            max_drawdown_duration=result.max_drawdown_duration,
            avg_drawdown=result.avg_drawdown,
            var_95=result.var_95,
            cvar_95=result.cvar_95,
            annual_vol=result.annual_vol,
            downside_vol=result.downside_vol,
            sharpe_ratio=result.sharpe_ratio,
            sortino_ratio=result.sortino_ratio,
            calmar_ratio=result.calmar_ratio,
            information_ratio=result.information_ratio,
            avg_win_pct=result.avg_win_pct,
            avg_loss_pct=result.avg_loss_pct,
            avg_win_loss_ratio=result.avg_win_loss_ratio,
            max_consecutive_wins=result.max_consecutive_wins,
            max_consecutive_losses=result.max_consecutive_losses,
            avg_hold_bars=result.avg_hold_bars,
            profit_factor=result.profit_factor,
            regime_distribution=result.regime_distribution,
            trades_by_regime=result.trades_by_regime,
            equity_curve=result.equity_curve,
            drawdown_curve=result.drawdown_curve,
            trade_log=result.trade_log,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
