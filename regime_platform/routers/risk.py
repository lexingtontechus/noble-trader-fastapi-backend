"""Risk analysis endpoints."""

from fastapi import APIRouter, HTTPException
from ..models.schemas import RiskRequest, RiskResponse, RiskAnalyzeRequest, RiskAnalyzeResponse, StressTestResult
from ..core.regime_engine import RegimeHMM
from ..core.position_sizer import PositionSizer, PositionRequest
from ..core.risk_manager import RiskManager
import numpy as np

router   = APIRouter(prefix="/risk", tags=["Risk Analysis"])
sizer    = PositionSizer()
riskman  = RiskManager()


@router.post("/analyse", response_model=RiskResponse, summary="Full risk analysis: VaR, CVaR, drawdown, stop/TP suggestions")
async def risk_analyse(req: RiskRequest):
    """
    Computes comprehensive risk metrics including parametric + historical VaR/CVaR,
    max drawdown, Sortino & Calmar ratios, and regime-adjusted stop-loss/take-profit.
    """
    try:
        prices  = np.array(req.prices, dtype=float)
        returns = np.diff(np.log(prices)) if req.returns is None else np.array(req.returns, dtype=float)

        regime = None
        if req.use_regime:
            model = RegimeHMM()
            model.fit(prices)
            regime = model.predict(prices)

        size_result = sizer.size(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=regime,
        ))

        risk = riskman.analyse(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=regime,
            base_risk_limit=req.base_risk_limit,
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
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/analyze",
    response_model=RiskAnalyzeResponse,
    summary="Enhanced risk analysis with VaR/CVaR, stress tests, and regime context",
)
async def risk_analyze(req: RiskAnalyzeRequest):
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
            model = RegimeHMM(n_components=req.n_hmm_states)
            model.fit(prices)
            regime = model.predict(prices)

        size_result = sizer.size(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=regime,
        ))

        risk = riskman.analyse(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=regime,
            base_risk_limit=req.base_risk_limit,
        )

        # Stress tests
        stress_tests = []
        if req.stress_scenarios and len(returns) > 20:
            mu = float(np.mean(returns))
            sigma = float(np.std(returns, ddof=1))

            # 2008 crash scenario
            crash_returns = returns.copy()
            crash_factor = np.random.default_rng(42).normal(-0.02, 0.04, len(returns))
            crash_returns_stressed = crash_returns + crash_factor
            crash_var = float(-np.percentile(crash_returns_stressed, 5))
            crash_max_dd = float(np.min(np.cumsum(crash_returns_stressed)))
            stress_tests.append(StressTestResult(
                scenario="2008_crash",
                description="2008-style crash: sustained -2%/day with elevated vol",
                simulated_return=float(np.sum(crash_returns_stressed)),
                simulated_var_95=round(crash_var, 6),
                simulated_max_dd=round(crash_max_dd, 6),
            ))

            # Flash crash scenario
            flash_returns = returns.copy()
            flash_day = len(flash_returns) // 2
            flash_returns[flash_day] = -0.10  # -10% in single day
            flash_var = float(-np.percentile(flash_returns, 5))
            flash_dd_series = np.cumsum(flash_returns)
            flash_max_dd = float(np.min(flash_dd_series))
            stress_tests.append(StressTestResult(
                scenario="flash_crash",
                description="Flash crash: -10% single-day drop",
                simulated_return=float(np.sum(flash_returns)),
                simulated_var_95=round(flash_var, 6),
                simulated_max_dd=round(flash_max_dd, 6),
            ))

            # Volatility spike scenario
            vol_spike_returns = returns.copy()
            vol_spike_returns[-20:] = np.random.default_rng(43).normal(mu, sigma * 3, 20)
            vol_var = float(-np.percentile(vol_spike_returns, 5))
            vol_dd = float(np.min(np.cumsum(vol_spike_returns)))
            stress_tests.append(StressTestResult(
                scenario="volatility_spike",
                description="Volatility spike: 3x normal vol for 20 bars",
                simulated_return=float(np.sum(vol_spike_returns)),
                simulated_var_95=round(vol_var, 6),
                simulated_max_dd=round(vol_dd, 6),
            ))

            # Rate shock scenario
            rate_returns = returns.copy()
            rate_returns -= 0.002  # -20bp/day for rate shock
            rate_var = float(-np.percentile(rate_returns, 5))
            rate_dd = float(np.min(np.cumsum(rate_returns)))
            stress_tests.append(StressTestResult(
                scenario="rate_shock",
                description="Interest rate shock: +2% rates reducing equity returns",
                simulated_return=float(np.sum(rate_returns)),
                simulated_var_95=round(rate_var, 6),
                simulated_max_dd=round(rate_dd, 6),
            ))

            # Liquidity crisis scenario
            liq_returns = returns.copy()
            liq_returns -= 0.001  # worse execution
            liq_returns = np.where(liq_returns < 0, liq_returns * 1.5, liq_returns * 0.8)  # asymmetric slippage
            liq_var = float(-np.percentile(liq_returns, 5))
            liq_dd = float(np.min(np.cumsum(liq_returns)))
            stress_tests.append(StressTestResult(
                scenario="liquidity_crisis",
                description="Liquidity crisis: wider spreads and asymmetric slippage",
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
        raise HTTPException(status_code=422, detail=str(e))
