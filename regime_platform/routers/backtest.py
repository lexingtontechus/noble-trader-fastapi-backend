"""Backtest endpoints — walk-forward backtesting with regime-aware strategy."""

from __future__ import annotations

import asyncio
import logging
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from typing import Optional

from ..auth import get_authed_user, TokenData
from ..core.exceptions import raise_http_exception
from ..core.backtester import Backtester, BacktestConfig
from ..models.schemas import (
    BacktestRequest,
    BacktestResponse,
    BacktestSummary,
    BacktestHistoryResponse,
    BacktestCompareRequest,
    BacktestCompareResponse,
)
from ..services.backtest_store import (
    save_backtest_result,
    get_backtest_history,
    get_backtest_detail,
    delete_backtest as db_delete_backtest,
)
from ..middleware.rate_limit import limiter, RATE_LIMIT_COMPUTE

import numpy as np

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["Backtesting"])
backtester = Backtester()


def _result_to_response(result, record_id: Optional[str] = None, created_at: Optional[str] = None) -> BacktestResponse:
    """Convert BacktestResult to BacktestResponse Pydantic model."""
    return BacktestResponse(
        id=record_id,
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
        config_used=result.config_used,
        created_at=created_at,
    )


@router.post(
    "/run",
    response_model=BacktestResponse,
    summary="Run walk-forward backtest with regime-aware strategy (30+ metrics)",
)
@limiter.limit(RATE_LIMIT_COMPUTE)
async def run_backtest(
    request: Request,
    req: BacktestRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Walk-forward backtest pipeline:
    1. Rolling HMM fit on trailing window
    2. Strategy signal generation based on regime
    3. Regime-gated Kelly position sizing
    4. Risk check gates (VaR, max drawdown)
    5. Trade execution with commission/slippage
    6. Comprehensive performance metrics (30+)

    Results are persisted to the database by default (set save=false to skip).
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
        # Offload CPU-bound backtest to thread pool to avoid blocking event loop
        result = await asyncio.to_thread(
            backtester.run,
            prices,
            req.symbol,
            config,
            req.dates,
        )

        # Persist to database
        record_id = None
        created_at = None
        if req.save:
            result_dict = {
                "n_bars": result.n_bars,
                "n_trades": result.n_trades,
                "n_wins": result.n_wins,
                "n_losses": result.n_losses,
                "win_rate": result.win_rate,
                "total_return": result.total_return,
                "annual_return": result.annual_return,
                "buy_hold_return": result.buy_hold_return,
                "excess_return": result.excess_return,
                "monthly_return_avg": result.monthly_return_avg,
                "max_drawdown": result.max_drawdown,
                "max_drawdown_duration": result.max_drawdown_duration,
                "avg_drawdown": result.avg_drawdown,
                "var_95": result.var_95,
                "cvar_95": result.cvar_95,
                "annual_vol": result.annual_vol,
                "downside_vol": result.downside_vol,
                "sharpe_ratio": result.sharpe_ratio,
                "sortino_ratio": result.sortino_ratio,
                "calmar_ratio": result.calmar_ratio,
                "information_ratio": result.information_ratio,
                "avg_win_pct": result.avg_win_pct,
                "avg_loss_pct": result.avg_loss_pct,
                "avg_win_loss_ratio": result.avg_win_loss_ratio,
                "max_consecutive_wins": result.max_consecutive_wins,
                "max_consecutive_losses": result.max_consecutive_losses,
                "avg_hold_bars": result.avg_hold_bars,
                "profit_factor": result.profit_factor,
                "regime_distribution": result.regime_distribution,
                "trades_by_regime": result.trades_by_regime,
                "equity_curve": result.equity_curve,
                "drawdown_curve": result.drawdown_curve,
                "trade_log": result.trade_log,
            }
            record_id, created_at = await save_backtest_result(
                user_id=user.sub,
                result_dict=result_dict,
                config_used=result.config_used,
                symbol=req.symbol,
            )

        return _result_to_response(result, record_id, created_at)

    except Exception as e:
        raise_http_exception(e, "backtest")


@router.get(
    "/history",
    response_model=BacktestHistoryResponse,
    summary="List saved backtest results for the authenticated user",
)
async def list_backtest_history(
    offset: int = Query(default=0, ge=0, description="Pagination offset."),
    limit: int = Query(default=20, ge=1, le=100, description="Page size."),
    user: TokenData = Depends(get_authed_user),
):
    """
    Returns a paginated list of the user's saved backtest results,
    ordered by most recent first. Only summary metrics are included
    (equity curves and trade logs are omitted for performance).
    """
    try:
        data = await get_backtest_history(user_id=user.sub, offset=offset, limit=limit)
        summaries = [
            BacktestSummary(
                id=row["id"],
                symbol=row["symbol"],
                n_trades=row["n_trades"],
                win_rate=row["win_rate"],
                total_return=row["total_return"],
                annual_return=row["annual_return"],
                sharpe_ratio=row["sharpe_ratio"],
                max_drawdown=row["max_drawdown"],
                profit_factor=row["profit_factor"],
                n_hmm_states=row["n_hmm_states"],
                created_at=row["created_at"],
            )
            for row in data["results"]
        ]
        return BacktestHistoryResponse(
            results=summaries,
            total=data["total"],
            offset=data["offset"],
            limit=data["limit"],
        )
    except Exception as e:
        raise_http_exception(e, "backtest_history")


@router.get(
    "/{backtest_id}",
    response_model=BacktestResponse,
    summary="Get a saved backtest result by ID",
)
async def get_backtest_by_id(
    backtest_id: str,
    user: TokenData = Depends(get_authed_user),
):
    """
    Retrieve the full backtest result (including equity curve, trade log,
    and all metrics) for a previously saved backtest.
    Scoped to the authenticated user.
    """
    try:
        row = await get_backtest_detail(backtest_id=backtest_id, user_id=user.sub)
        if row is None:
            raise HTTPException(status_code=404, detail="Backtest result not found.")
        # Reconstruct BacktestResponse from the stored row
        return BacktestResponse(
            id=row.get("id"),
            symbol=row.get("symbol", "UNKNOWN"),
            n_bars=row.get("n_bars", 0),
            n_trades=row.get("n_trades", 0),
            n_wins=row.get("n_wins", 0),
            n_losses=row.get("n_losses", 0),
            win_rate=row.get("win_rate", 0.0),
            total_return=row.get("total_return", 0.0),
            annual_return=row.get("annual_return", 0.0),
            buy_hold_return=row.get("buy_hold_return", 0.0),
            excess_return=row.get("excess_return", 0.0),
            monthly_return_avg=row.get("monthly_return_avg", 0.0),
            max_drawdown=row.get("max_drawdown", 0.0),
            max_drawdown_duration=row.get("max_drawdown_duration", 0),
            avg_drawdown=row.get("avg_drawdown", 0.0),
            var_95=row.get("var_95", 0.0),
            cvar_95=row.get("cvar_95", 0.0),
            annual_vol=row.get("annual_vol", 0.0),
            downside_vol=row.get("downside_vol", 0.0),
            sharpe_ratio=row.get("sharpe_ratio", 0.0),
            sortino_ratio=row.get("sortino_ratio", 0.0),
            calmar_ratio=row.get("calmar_ratio", 0.0),
            information_ratio=row.get("information_ratio", 0.0),
            avg_win_pct=row.get("avg_win_pct", 0.0),
            avg_loss_pct=row.get("avg_loss_pct", 0.0),
            avg_win_loss_ratio=row.get("avg_win_loss_ratio", 0.0),
            max_consecutive_wins=row.get("max_consecutive_wins", 0),
            max_consecutive_losses=row.get("max_consecutive_losses", 0),
            avg_hold_bars=row.get("avg_hold_bars", 0.0),
            profit_factor=row.get("profit_factor", 0.0),
            regime_distribution=row.get("regime_distribution", {}),
            trades_by_regime=row.get("trades_by_regime", {}),
            equity_curve=row.get("equity_curve", []),
            drawdown_curve=row.get("drawdown_curve", []),
            trade_log=row.get("trade_log", []),
            config_used=row.get("config_used", {}),
            created_at=row.get("created_at"),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise_http_exception(e, "backtest_detail")


@router.post(
    "/compare",
    response_model=BacktestCompareResponse,
    summary="Compare two or more saved backtest results",
)
async def compare_backtests(
    req: BacktestCompareRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Side-by-side comparison of saved backtest results.
    Returns full results for each ID plus a diff summary highlighting
    the key metric differences between the first and best result.
    """
    try:
        comparisons = []
        for bid in req.ids:
            row = await get_backtest_detail(backtest_id=bid, user_id=user.sub)
            if row is None:
                raise HTTPException(status_code=404, detail=f"Backtest {bid} not found.")
            comparisons.append(BacktestResponse(
                id=row.get("id"),
                symbol=row.get("symbol", "UNKNOWN"),
                n_bars=row.get("n_bars", 0),
                n_trades=row.get("n_trades", 0),
                n_wins=row.get("n_wins", 0),
                n_losses=row.get("n_losses", 0),
                win_rate=row.get("win_rate", 0.0),
                total_return=row.get("total_return", 0.0),
                annual_return=row.get("annual_return", 0.0),
                buy_hold_return=row.get("buy_hold_return", 0.0),
                excess_return=row.get("excess_return", 0.0),
                monthly_return_avg=row.get("monthly_return_avg", 0.0),
                max_drawdown=row.get("max_drawdown", 0.0),
                max_drawdown_duration=row.get("max_drawdown_duration", 0),
                avg_drawdown=row.get("avg_drawdown", 0.0),
                var_95=row.get("var_95", 0.0),
                cvar_95=row.get("cvar_95", 0.0),
                annual_vol=row.get("annual_vol", 0.0),
                downside_vol=row.get("downside_vol", 0.0),
                sharpe_ratio=row.get("sharpe_ratio", 0.0),
                sortino_ratio=row.get("sortino_ratio", 0.0),
                calmar_ratio=row.get("calmar_ratio", 0.0),
                information_ratio=row.get("information_ratio", 0.0),
                avg_win_pct=row.get("avg_win_pct", 0.0),
                avg_loss_pct=row.get("avg_loss_pct", 0.0),
                avg_win_loss_ratio=row.get("avg_win_loss_ratio", 0.0),
                max_consecutive_wins=row.get("max_consecutive_wins", 0),
                max_consecutive_losses=row.get("max_consecutive_losses", 0),
                avg_hold_bars=row.get("avg_hold_bars", 0.0),
                profit_factor=row.get("profit_factor", 0.0),
                regime_distribution=row.get("regime_distribution", {}),
                trades_by_regime=row.get("trades_by_regime", {}),
                equity_curve=row.get("equity_curve", []),
                drawdown_curve=row.get("drawdown_curve", []),
                trade_log=row.get("trade_log", []),
                config_used=row.get("config_used", {}),
                created_at=row.get("created_at"),
            ))

        # Compute diff: first vs best (by Sharpe ratio)
        diff = {}
        if len(comparisons) >= 2:
            first = comparisons[0]
            best = max(comparisons, key=lambda c: c.sharpe_ratio)
            diff_keys = [
                "total_return", "annual_return", "sharpe_ratio", "sortino_ratio",
                "calmar_ratio", "max_drawdown", "profit_factor", "win_rate",
                "avg_hold_bars", "n_trades",
            ]
            for key in diff_keys:
                f_val = getattr(first, key, 0)
                b_val = getattr(best, key, 0)
                diff[key] = {
                    "first": f_val,
                    "best": b_val,
                    "delta": round(b_val - f_val, 6) if isinstance(f_val, (int, float)) else None,
                }

        return BacktestCompareResponse(comparisons=comparisons, diff=diff)

    except HTTPException:
        raise
    except Exception as e:
        raise_http_exception(e, "backtest_compare")


@router.delete(
    "/{backtest_id}",
    summary="Delete a saved backtest result",
    status_code=204,
)
async def delete_backtest_result(
    backtest_id: str,
    user: TokenData = Depends(get_authed_user),
):
    """
    Delete a saved backtest result by ID.
    Scoped to the authenticated user.
    """
    try:
        deleted = await db_delete_backtest(backtest_id=backtest_id, user_id=user.sub)
        if not deleted:
            raise HTTPException(status_code=404, detail="Backtest result not found.")
    except HTTPException:
        raise
    except Exception as e:
        raise_http_exception(e, "backtest_delete")
