"""Risk analysis endpoints."""

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from ..models.schemas import (
    RiskRequest, RiskResponse, RiskAnalyzeRequest, RiskAnalyzeResponse,
    StressTestResult, RiskDashboardResponse,
)
from ..core.regime_engine import RegimeHMM
from ..core.position_sizer import PositionSizer, PositionRequest
from ..core.risk_manager import RiskManager
from ..core.exceptions import raise_http_exception
from ..auth.jwt_auth import get_authed_user, TokenData
from ..core.runtime_config import runtime_config
from ..middleware.rate_limit import limiter, RATE_LIMIT_COMPUTE, RATE_LIMIT_DEFAULT
import numpy as np

router   = APIRouter(prefix="/risk", tags=["Risk Analysis"])
sizer    = PositionSizer()
riskman  = RiskManager()


@router.post("/analyse", response_model=RiskResponse, summary="Full risk analysis: VaR, CVaR, drawdown, stop/TP suggestions")
@limiter.limit(RATE_LIMIT_COMPUTE)
async def risk_analyse(request: Request, req: RiskRequest, user: TokenData = Depends(get_authed_user)):
    """
    Computes comprehensive risk metrics including parametric + historical VaR/CVaR,
    max drawdown, Sortino & Calmar ratios, and regime-adjusted stop-loss/take-profit.
    """
    try:
        prices  = np.array(req.prices, dtype=float)
        returns = np.diff(np.log(prices)) if req.returns is None else np.array(req.returns, dtype=float)

        regime = None
        if req.use_regime:
            model = await RegimeHMM.from_runtime_config()
            model.fit(prices)
            regime = model.predict(prices)

        # Resolve runtime-configurable defaults for risk params
        base_risk_limit = req.base_risk_limit or await runtime_config.get_float("risk.base_risk_limit", default=0.02)

        size_result = await sizer.size_async(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=regime,
        ))

        risk = await riskman.analyse_async(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=regime,
            base_risk_limit=base_risk_limit,
        )

        return RiskResponse(
            symbol=req.symbol,
            recommended_f=size_result.recommended_f,
            var_95=risk.var_95,
            var_99=risk.var_99,
            cvar_95=risk.cvar_95,
            cvar_99=risk.cvar_99,
            max_drawdown=risk.max_drawdown,
            annual_vol=risk.annual_vol,
            annual_return=risk.annual_return,
            calmar_ratio=risk.calmar_ratio,
            sortino_ratio=risk.sortino_ratio,
            regime_label=risk.regime_label,
            regime_max_loss=risk.regime_max_loss,
            suggested_stop=risk.suggested_stop,
            suggested_tp=risk.suggested_tp,
            risk_budget_used=risk.risk_budget_used,
            notes=size_result.notes,
        )
    except Exception as e:
        raise_http_exception(e, "risk analysis")


@router.post(
    "/analyze",
    response_model=RiskAnalyzeResponse,
    summary="Enhanced risk analysis with VaR/CVaR, stress tests, and regime context",
)
@limiter.limit(RATE_LIMIT_COMPUTE)
async def risk_analyze(request: Request, req: RiskAnalyzeRequest, user: TokenData = Depends(get_authed_user)):
    """
    Enhanced risk analysis (US spelling endpoint):
    1. Full VaR/CVaR computation (parametric + historical)
    2. Regime-aware risk multiplier and max loss
    3. Stress test scenarios:
       - 2008-style crash (-30% in 2 weeks)
       - Flash crash (-10% in 1 day)
       - Volatility spike (3x vol for 1 week)
       - Interest rate shock (+2% rates)
       - Liquidity crisis (wider spreads, worse execution)
    4. Stop-loss and take-profit guidance
    """
    try:
        prices  = np.array(req.prices, dtype=float)
        returns = np.diff(np.log(prices)) if req.returns is None else np.array(req.returns, dtype=float)

        regime = None
        if req.use_regime:
            model = await RegimeHMM.from_runtime_config(n_components=req.n_hmm_states)
            model.fit(prices)
            regime = model.predict(prices)

        # Resolve runtime-configurable defaults for risk params
        base_risk_limit = req.base_risk_limit or await runtime_config.get_float("risk.base_risk_limit", default=0.02)

        size_result = await sizer.size_async(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=regime,
        ))

        risk = await riskman.analyse_async(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=regime,
            base_risk_limit=base_risk_limit,
        )

        # Stress tests - params resolved from runtime_config
        stress_tests = []
        stress_min_returns = await runtime_config.get_int("risk.stress_min_returns", default=20)
        if req.stress_scenarios and len(returns) > stress_min_returns:
            mu = float(np.mean(returns))
            sigma = float(np.std(returns, ddof=1))

            # Resolve stress test parameters from runtime config
            crash_mean = await runtime_config.get_float("risk.stress_crash_mean", default=-0.02)
            crash_std = await runtime_config.get_float("risk.stress_crash_std", default=0.04)
            crash_seed = await runtime_config.get_int("risk.stress_crash_seed", default=42)
            flash_drop = await runtime_config.get_float("risk.stress_flash_drop", default=-0.10)
            vol_multiplier = await runtime_config.get_int("risk.stress_vol_multiplier", default=3)
            vol_spike_bars = await runtime_config.get_int("risk.stress_vol_spike_bars", default=20)
            rate_shock_bps = await runtime_config.get_float("risk.stress_rate_shock_bps", default=0.002)
            liquidity_shift = await runtime_config.get_float("risk.stress_liquidity_shift", default=0.001)

            # 2008 crash scenario
            crash_returns = returns.copy()
            crash_factor = np.random.default_rng(crash_seed).normal(crash_mean, crash_std, len(returns))
            crash_returns_stressed = crash_returns + crash_factor
            crash_var = float(-np.percentile(crash_returns_stressed, 5))
            crash_max_dd = float(np.min(np.cumsum(crash_returns_stressed)))
            stress_tests.append(StressTestResult(
                scenario="2008_crash",
                description=f"2008-style crash: sustained {crash_mean*100:.1f}%/day with vol {crash_std*100:.1f}%",
                simulated_return=float(np.sum(crash_returns_stressed)),
                simulated_var_95=round(crash_var, 6),
                simulated_max_dd=round(crash_max_dd, 6),
            ))

            # Flash crash scenario
            flash_returns = returns.copy()
            flash_day = len(flash_returns) // 2
            flash_returns[flash_day] = flash_drop
            flash_var = float(-np.percentile(flash_returns, 5))
            flash_dd_series = np.cumsum(flash_returns)
            flash_max_dd = float(np.min(flash_dd_series))
            stress_tests.append(StressTestResult(
                scenario="flash_crash",
                description=f"Flash crash: {flash_drop*100:.0f}% single-day drop",
                simulated_return=float(np.sum(flash_returns)),
                simulated_var_95=round(flash_var, 6),
                simulated_max_dd=round(flash_max_dd, 6),
            ))

            # Volatility spike scenario
            vol_spike_returns = returns.copy()
            spike_bars = min(vol_spike_bars, len(vol_spike_returns))
            vol_spike_returns[-spike_bars:] = np.random.default_rng(crash_seed + 1).normal(
                mu, sigma * vol_multiplier, spike_bars
            )
            vol_var = float(-np.percentile(vol_spike_returns, 5))
            vol_dd = float(np.min(np.cumsum(vol_spike_returns)))
            stress_tests.append(StressTestResult(
                scenario="volatility_spike",
                description=f"Volatility spike: {vol_multiplier}x normal vol for {spike_bars} bars",
                simulated_return=float(np.sum(vol_spike_returns)),
                simulated_var_95=round(vol_var, 6),
                simulated_max_dd=round(vol_dd, 6),
            ))

            # Rate shock scenario
            rate_returns = returns.copy()
            rate_returns -= rate_shock_bps
            rate_var = float(-np.percentile(rate_returns, 5))
            rate_dd = float(np.min(np.cumsum(rate_returns)))
            stress_tests.append(StressTestResult(
                scenario="rate_shock",
                description=f"Interest rate shock: {rate_shock_bps*10_000:.0f}bps/day shift",
                simulated_return=float(np.sum(rate_returns)),
                simulated_var_95=round(rate_var, 6),
                simulated_max_dd=round(rate_dd, 6),
            ))

            # Resolve extended stress test parameters from runtime config
            stress_liquidity_downside_mult = await runtime_config.get_float("risk.stress_liquidity_downside_mult", default=1.5)
            stress_liquidity_upside_mult = await runtime_config.get_float("risk.stress_liquidity_upside_mult", default=0.8)

            # Liquidity crisis scenario
            liq_returns = returns.copy()
            liq_returns -= liquidity_shift  # worse execution
            liq_returns = np.where(liq_returns < 0, liq_returns * stress_liquidity_downside_mult, liq_returns * stress_liquidity_upside_mult)  # asymmetric slippage
            liq_var = float(-np.percentile(liq_returns, 5))
            liq_dd = float(np.min(np.cumsum(liq_returns)))
            stress_tests.append(StressTestResult(
                scenario="liquidity_crisis",
                description=f"Liquidity crisis: {liquidity_shift*10_000:.0f}bps shift + asymmetric slippage",
                simulated_return=float(np.sum(liq_returns)),
                simulated_var_95=round(liq_var, 6),
                simulated_max_dd=round(liq_dd, 6),
            ))

        return RiskAnalyzeResponse(
            symbol=req.symbol,
            recommended_f=size_result.recommended_f,
            var_95=risk.var_95,
            var_99=risk.var_99,
            cvar_95=risk.cvar_95,
            cvar_99=risk.cvar_99,
            max_drawdown=risk.max_drawdown,
            annual_vol=risk.annual_vol,
            annual_return=risk.annual_return,
            calmar_ratio=risk.calmar_ratio,
            sortino_ratio=risk.sortino_ratio,
            regime_label=risk.regime_label,
            risk_multiplier=regime.risk_multiplier if regime else 1.0,
            regime_max_loss=risk.regime_max_loss,
            suggested_stop=risk.suggested_stop,
            suggested_tp=risk.suggested_tp,
            risk_budget_used=risk.risk_budget_used,
            stress_tests=stress_tests,
            notes=size_result.notes,
        )
    except Exception as e:
        raise_http_exception(e, "risk analysis")


# ─── Live Risk Dashboard ─────────────────────────────────────────────────────

@router.get(
    "/dashboard",
    response_model=RiskDashboardResponse,
    summary="Live portfolio risk metrics dashboard from equity curve",
)
@limiter.limit(RATE_LIMIT_DEFAULT)
async def risk_dashboard(
    request: Request,
    period: str = Query(default="1M", description="Lookback period: 1W, 1M, 3M, 6M, 1A, all"),
    user: TokenData = Depends(get_authed_user),
):
    """
    Compute live portfolio risk metrics from the equity curve.

    Returns a comprehensive risk dashboard with:
      - Return metrics: total return, annual return, daily average
      - Risk metrics: annual vol, downside vol, Sharpe, Sortino, Calmar
      - Drawdown: max drawdown, current drawdown, average drawdown
      - VaR/CVaR: 95% and 99% confidence levels (historical)
      - Win/Loss: win rate, avg win/loss, profit factor, max streaks

    Uses the same credential resolution as AlpacaStreamManager.
    Results are cached for 30 seconds per user.
    """
    from ..services.pnl_service import pnl_service

    metrics = await pnl_service.get_risk_dashboard(
        user_id=user.sub,
        period=period,
        org_id=user.org_id,
    )

    return RiskDashboardResponse(
        total_return=metrics.total_return,
        total_return_pct=metrics.total_return_pct,
        annual_return_pct=metrics.annual_return_pct,
        daily_return_avg=metrics.daily_return_avg,
        annual_vol=metrics.annual_vol,
        downside_vol=metrics.downside_vol,
        sharpe_ratio=metrics.sharpe_ratio,
        sortino_ratio=metrics.sortino_ratio,
        calmar_ratio=metrics.calmar_ratio,
        max_drawdown=metrics.max_drawdown,
        max_drawdown_pct=metrics.max_drawdown_pct,
        current_drawdown=metrics.current_drawdown,
        current_drawdown_pct=metrics.current_drawdown_pct,
        avg_drawdown=metrics.avg_drawdown,
        var_95=metrics.var_95,
        var_99=metrics.var_99,
        cvar_95=metrics.cvar_95,
        cvar_99=metrics.cvar_99,
        win_days=metrics.win_days,
        loss_days=metrics.loss_days,
        win_rate=metrics.win_rate,
        avg_win=metrics.avg_win,
        avg_loss=metrics.avg_loss,
        profit_factor=metrics.profit_factor,
        max_consecutive_wins=metrics.max_consecutive_wins,
        max_consecutive_losses=metrics.max_consecutive_losses,
        n_data_points=metrics.n_data_points,
        period_days=metrics.period_days,
        start_equity=metrics.start_equity,
        end_equity=metrics.end_equity,
        computed_at=metrics.computed_at,
    )
