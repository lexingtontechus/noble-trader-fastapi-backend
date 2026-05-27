"""Strategy signal endpoints - combines regime detection, Kelly sizing, and risk guidance."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import StrategySignalRequest, StrategySignalResponse
from ..core.regime_engine import RegimeHMM
from ..core.position_sizer import PositionSizer, PositionRequest
from ..core.risk_manager import RiskManager
from ..auth.jwt_auth import get_authed_user, TokenData
from ..core.exceptions import raise_http_exception
import numpy as np

router = APIRouter(prefix="/strategy", tags=["Strategy Signals"])
sizer = PositionSizer()
riskman = RiskManager()


@router.post(
    "/signal",
    response_model=StrategySignalResponse,
    summary="Generate strategy signal with regime context and position sizing",
)
async def strategy_signal(req: StrategySignalRequest, user: TokenData = Depends(get_authed_user)):
    """
    Full strategy signal pipeline:
    1. Fit HMM regime model → detect current regime
    2. Generate trading signal based on regime (long/short/flat)
    3. Compute regime-gated Kelly position size
    4. Compute risk metrics and guidance (stop/TP, VaR/CVaR)
    5. Return combined signal with all context
    """
    try:
        prices = np.array(req.prices, dtype=float)
        returns = np.diff(np.log(prices)) if req.returns is None else np.array(req.returns, dtype=float)

        # 1. Regime detection
        snap = None
        if req.use_regime:
            model = RegimeHMM(n_components=req.n_hmm_states)
            model.fit(prices)
            snap = model.predict(prices)

        # 2. Generate signal
        if snap is None:
            signal = "flat"
            signal_strength = 0.0
            regime_label = "no_regime"
            vol_state = "unknown"
            trend_state = "unknown"
            risk_multiplier = 1.0
            confidence = 0.0
        else:
            regime_label = snap.regime_label
            vol_state = snap.vol_state
            trend_state = snap.trend_state
            risk_multiplier = snap.risk_multiplier
            confidence = snap.confidence

            # Signal logic based on regime
            if vol_state in ("low", "med_low") and trend_state in ("bull", "strong_bull"):
                signal = "long"
                signal_strength = confidence * risk_multiplier
            elif vol_state in ("high", "med_high") and trend_state in ("bear", "strong_bear"):
                signal = "short"
                signal_strength = confidence * risk_multiplier * 0.5  # reduced short strength
            else:
                signal = "flat"
                signal_strength = confidence * 0.3  # low strength for neutral signals

        # 3. Position sizing
        size_result = sizer.size(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=snap,
        ))

        # 4. Risk analysis
        risk = riskman.analyse(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=snap,
            base_risk_limit=req.base_risk_limit,
        )

        # 5. Adjust signal based on risk
        notes = list(size_result.notes)
        if risk.risk_budget_used > 1.0:
            notes.append("Risk budget exceeded - consider reducing position size.")
        if signal == "long" and risk.regime_max_loss > req.base_risk_limit:
            notes.append("Regime risk exceeds base limit - signal weakened.")
            signal_strength *= 0.5
        if signal != "flat" and size_result.recommended_f < 0.02:
            notes.append("Very low Kelly fraction - signal may not be actionable.")
            signal = "flat"
            signal_strength *= 0.1

        return StrategySignalResponse(
            symbol=req.symbol,
            signal=signal,
            signal_strength=round(min(signal_strength, 1.0), 4),
            confidence=round(confidence, 4),
            regime_label=regime_label,
            vol_state=vol_state,
            trend_state=trend_state,
            risk_multiplier=round(risk_multiplier, 4),
            recommended_f=size_result.recommended_f,
            full_kelly_f=size_result.full_kelly_f,
            fractional_f=size_result.fractional_f,
            vol_scaled_f=size_result.vol_scaled_f,
            regime_gated_f=size_result.regime_gated_f,
            sharpe_ratio=size_result.sharpe_ratio,
            suggested_stop=risk.suggested_stop,
            suggested_tp=risk.suggested_tp,
            var_95=risk.var_95,
            cvar_95=risk.cvar_95,
            notes=notes,
        )
    except Exception as e:
        raise_http_exception(e, "strategy signal")
