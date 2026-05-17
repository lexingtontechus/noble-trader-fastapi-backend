"""
Multi-asset correlation regime + drawdown-optimised portfolio endpoints.

  POST /correlation/detect   — detect portfolio-level correlation regime
  POST /optimise             — solve drawdown-controlled max-Sharpe portfolio
  POST /optimise/full        — correlation detect + optimise in one request
  POST /optimise/masaniello  — two-stage: portfolio optimisation + per-asset Masaniello sizing
"""

from __future__ import annotations

import numpy as np
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Optional

from ..core.regime_engine import RegimeHMM
from ..core.correlation_regime import (
    CorrelationRegimeDetector, CorrelationRegimeConfig,
    CorrelationSnapshot, AssetRegime,
)
from ..core.portfolio_optimiser import DrawdownOptimiser, OptimiserConfig, OptimisationResult
from ..core.position_sizer import SizingConfig
from ..services.masaniello_bridge import MasanielloBridge
from ..models.schemas import (
    PortfolioMasanielloRequest, PortfolioMasanielloResponse, AssetMasanielloResult,
)
from ..auth.jwt_auth import get_authed_user, TokenData

router = APIRouter(tags=["Multi-Asset"])


# ── Request / Response schemas ─────────────────────────────────────────────────

class MultiAssetPayload(BaseModel):
    symbols:        list[str]          = Field(..., min_length=2, max_length=50)
    returns_matrix: list[list[float]]  = Field(
        ...,
        description="(n_bars × n_assets) matrix of log-returns, outer list = bars.",
    )

    def to_numpy(self) -> np.ndarray:
        arr = np.array(self.returns_matrix, dtype=float)
        if arr.shape[1] != len(self.symbols):
            raise ValueError(
                f"returns_matrix columns ({arr.shape[1]}) ≠ symbols length ({len(self.symbols)})"
            )
        return arr


class CorrDetectRequest(MultiAssetPayload):
    window:       int   = Field(default=60,  ge=20, le=252)
    ewma_span:    int   = Field(default=20,  ge=5,  le=60)
    n_hmm_states: int   = Field(default=4,   ge=2,  le=6)


class CorrRegimeResponse(BaseModel):
    symbols:                 list[str]
    n_assets:                int
    corr_regime:             str
    corr_confidence:         float
    corr_risk_multiplier:    float
    blended_risk_multiplier: float
    mean_abs_correlation:    float
    correlation_matrix:      list[list[float]]
    corr_probs:              dict[str, float]
    n_bars_fitted:           int
    asset_regimes: list[dict]


class OptimiseRequest(MultiAssetPayload):
    max_dd_limit:     float = Field(default=0.20, ge=0.01, le=1.0)
    max_weight:       float = Field(default=0.40, ge=0.05, le=1.0)
    risk_free_rate:   float = Field(default=0.04, ge=0.0,  le=0.20)
    corr_regime:      Optional[str] = Field(default=None)
    kelly_fractions:  Optional[list[float]] = Field(default=None)
    use_asset_regimes: bool = Field(default=True)


class OptimiseResponse(BaseModel):
    symbols:               list[str]
    weights:               list[float]
    regime_adj_weights:    list[float]
    expected_return:       float
    expected_vol:          float
    sharpe_ratio:          float
    expected_max_drawdown: float
    dd_constraint_met:     bool
    regime_exposure:       float
    per_asset_bounds:      list[list[float]]
    converged:             bool
    solver_message:        str


class FullOptimiseRequest(OptimiseRequest):
    """Runs correlation detection + optimisation in one call."""
    corr_window:   int = Field(default=60, ge=20, le=252)
    corr_ewma:     int = Field(default=20, ge=5,  le=60)


class FullOptimiseResponse(BaseModel):
    correlation: CorrRegimeResponse
    optimisation: OptimiseResponse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fit_asset_snaps(returns_matrix: np.ndarray, symbols: list[str]):
    """Fit per-asset HMMs and return list of RegimeSnapshot."""
    # Reconstruct pseudo-prices from returns
    snaps = []
    for j in range(returns_matrix.shape[1]):
        prices = 100.0 * np.cumprod(1 + returns_matrix[:, j])
        try:
            model = RegimeHMM()
            model.fit(prices)
            snaps.append(model.predict(prices))
        except Exception:
            snaps.append(None)
    return snaps


def _snap_to_dict(ar: AssetRegime) -> dict:
    return {
        "symbol":          ar.symbol,
        "regime_label":    ar.regime_label,
        "vol_state":       ar.vol_state,
        "trend_state":     ar.trend_state,
        "confidence":      ar.confidence,
        "risk_multiplier": ar.risk_multiplier,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post(
    "/correlation/detect",
    response_model=CorrRegimeResponse,
    summary="Detect portfolio-level correlation regime across multiple assets",
)
async def detect_correlation_regime(
    req: CorrDetectRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Fits a 4-state Gaussian HMM on rolling correlation features across all assets.

    Returns the current correlation regime:
    - `low_corr`  — assets moving independently (healthy diversification)
    - `mid_corr`  — moderate comovement
    - `high_corr` — elevated stress, correlations rising
    - `crisis`    — crash regime, diversification collapsed (mean |ρ| > 0.75)

    Also returns per-asset regime breakdowns and a `blended_risk_multiplier`
    that combines per-asset regimes with the portfolio correlation multiplier.
    """
    try:
        arr     = req.to_numpy()
        config  = CorrelationRegimeConfig(
            window=req.window,
            ewma_span=req.ewma_span,
            n_hmm_states=req.n_hmm_states,
        )
        detector = CorrelationRegimeDetector(config)
        snaps    = _fit_asset_snaps(arr, req.symbols)
        snap     = detector.detect(arr, req.symbols, snaps)

        return CorrRegimeResponse(
            symbols=snap.symbols,
            n_assets=snap.n_assets,
            corr_regime=snap.corr_regime,
            corr_confidence=snap.corr_confidence,
            corr_risk_multiplier=snap.corr_risk_multiplier,
            blended_risk_multiplier=snap.blended_risk_multiplier,
            mean_abs_correlation=snap.mean_abs_correlation,
            correlation_matrix=snap.correlation_matrix,
            corr_probs=snap.corr_probs,
            n_bars_fitted=snap.n_bars_fitted,
            asset_regimes=[_snap_to_dict(ar) for ar in snap.asset_regimes],
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/optimise",
    response_model=OptimiseResponse,
    summary="Drawdown-controlled regime-aware portfolio optimisation",
)
async def optimise_portfolio(
    req: OptimiseRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Solves a regime-adjusted maximum Sharpe portfolio subject to a drawdown constraint.

    Three regime layers applied:
    1. **Regime-adjusted μ** — per-asset expected returns scaled by risk multiplier
    2. **Regime weight bounds** — tighter upper bounds in high-risk regimes
    3. **Correlation exposure** — gross portfolio exposure reduced in crisis regimes

    The drawdown constraint uses a Cornish-Fisher CVaR proxy. Set `max_dd_limit`
    to your maximum acceptable expected drawdown (e.g. 0.20 = 20%).
    """
    try:
        arr    = req.to_numpy()
        config = OptimiserConfig(
            max_dd_limit=req.max_dd_limit,
            max_weight=req.max_weight,
            risk_free_rate=req.risk_free_rate,
        )
        snaps = _fit_asset_snaps(arr, req.symbols) if req.use_asset_regimes else None
        opt   = DrawdownOptimiser(config)
        res   = opt.optimise(
            returns_matrix=arr,
            symbols=req.symbols,
            asset_snaps=snaps,
            corr_regime=req.corr_regime,
            kelly_fractions=req.kelly_fractions,
        )

        return OptimiseResponse(
            symbols=res.symbols,
            weights=res.weights,
            regime_adj_weights=res.regime_adj_weights,
            expected_return=res.expected_return,
            expected_vol=res.expected_vol,
            sharpe_ratio=res.sharpe_ratio,
            expected_max_drawdown=res.expected_max_drawdown,
            dd_constraint_met=res.dd_constraint_met,
            regime_exposure=res.regime_exposure,
            per_asset_bounds=[list(b) for b in res.per_asset_bounds],
            converged=res.converged,
            solver_message=res.solver_message,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/optimise/full",
    response_model=FullOptimiseResponse,
    summary="One-shot: correlation regime detection + portfolio optimisation",
)
async def full_optimise(
    req: FullOptimiseRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Runs the complete multi-asset pipeline in one request:
    1. Fit per-asset HMMs → per-asset RegimeSnapshots
    2. Detect correlation regime → corr_regime label + blended multiplier
    3. Solve drawdown-controlled max-Sharpe with both regime layers applied
    """
    try:
        arr     = req.to_numpy()
        config  = CorrelationRegimeConfig(window=req.corr_window, ewma_span=req.corr_ewma)
        snaps   = _fit_asset_snaps(arr, req.symbols)

        detector = CorrelationRegimeDetector(config)
        corr_snap = detector.detect(arr, req.symbols, snaps)

        opt_config = OptimiserConfig(
            max_dd_limit=req.max_dd_limit,
            max_weight=req.max_weight,
            risk_free_rate=req.risk_free_rate,
        )
        opt = DrawdownOptimiser(opt_config)
        res = opt.optimise(
            returns_matrix=arr,
            symbols=req.symbols,
            asset_snaps=snaps,
            corr_regime=corr_snap.corr_regime,
        )

        return FullOptimiseResponse(
            correlation=CorrRegimeResponse(
                symbols=corr_snap.symbols,
                n_assets=corr_snap.n_assets,
                corr_regime=corr_snap.corr_regime,
                corr_confidence=corr_snap.corr_confidence,
                corr_risk_multiplier=corr_snap.corr_risk_multiplier,
                blended_risk_multiplier=corr_snap.blended_risk_multiplier,
                mean_abs_correlation=corr_snap.mean_abs_correlation,
                correlation_matrix=corr_snap.correlation_matrix,
                corr_probs=corr_snap.corr_probs,
                n_bars_fitted=corr_snap.n_bars_fitted,
                asset_regimes=[_snap_to_dict(ar) for ar in corr_snap.asset_regimes],
            ),
            optimisation=OptimiseResponse(
                symbols=res.symbols,
                weights=res.weights,
                regime_adj_weights=res.regime_adj_weights,
                expected_return=res.expected_return,
                expected_vol=res.expected_vol,
                sharpe_ratio=res.sharpe_ratio,
                expected_max_drawdown=res.expected_max_drawdown,
                dd_constraint_met=res.dd_constraint_met,
                regime_exposure=res.regime_exposure,
                per_asset_bounds=[list(b) for b in res.per_asset_bounds],
                converged=res.converged,
                solver_message=res.solver_message,
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/optimise/masaniello",
    response_model=PortfolioMasanielloResponse,
    summary="Two-stage pipeline: portfolio optimisation + per-asset Masaniello sizing",
)
async def optimise_with_masaniello(
    req: PortfolioMasanielloRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Full two-stage pipeline in one request:

    **Stage 1** — DrawdownOptimiser:
    Solves a regime-adjusted max-Sharpe portfolio with drawdown constraint.
    Produces per-asset portfolio weights (capital allocation).

    **Stage 2** — Dynamic Masaniello Sizer:
    Sizes each asset's position within its allocated capital using:
      f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i

    Where:
    - M_i = urgency (wins needed / trades remaining)
    - Q_i = quality (prob × regime × confidence)
    - DD_i = drawdown scaling (floor 0.25)
    - V_i = volatility adjustment (ATR-based)

    The optimiser says "what to hold"; Masaniello says "how much".
    """
    try:
        arr = np.array(req.returns_matrix, dtype=float)
        if arr.shape[1] != len(req.symbols):
            raise ValueError(
                f"returns_matrix columns ({arr.shape[1]}) ≠ symbols length ({len(req.symbols)})"
            )

        # Build configs
        opt_config = OptimiserConfig(
            max_dd_limit=req.max_dd_limit,
            max_weight=req.max_weight,
        )
        sizing_config = SizingConfig(
            base_risk=req.base_risk,
            min_rr=req.min_rr,
            batch_size=req.batch_size,
            target_wins=req.target_wins,
            use_kelly_overlay=req.use_kelly_overlay,
            kelly_fraction=req.kelly_fraction,
        )

        # Run the two-stage pipeline
        bridge = MasanielloBridge(opt_config, sizing_config)
        result = bridge.optimise_and_size(
            returns_matrix=arr,
            symbols=req.symbols,
            equity=req.equity,
            stop_distances=req.stop_distances,
            p_wins=req.p_wins,
            reward_risks=req.reward_risks,
            atr_baselines=req.atr_baselines,
            atr_currents=req.atr_currents,
            point_values=req.point_values or None,
            directions=req.directions or None,
        )

        # Build response
        asset_results = []
        for a in result.asset_sizes:
            asset_results.append(AssetMasanielloResult(
                symbol=a.symbol,
                portfolio_weight=a.portfolio_weight,
                capital_allocation=a.capital_allocation,
                allowed=a.allowed,
                risk_fraction=a.risk_fraction,
                risk_amount=a.risk_amount,
                contracts=a.contracts,
                masaniello_factor=a.masaniello_factor,
                quality_factor=a.quality_factor,
                drawdown_factor=a.drawdown_factor,
                volatility_factor=a.volatility_factor,
                expected_edge=a.expected_edge,
                reason=a.reason,
                regime_label=a.regime_label,
            ))

        opt = result.optimisation
        return PortfolioMasanielloResponse(
            symbols=opt.symbols,
            weights=opt.weights,
            regime_adj_weights=opt.regime_adj_weights,
            expected_return=opt.expected_return,
            expected_vol=opt.expected_vol,
            sharpe_ratio=opt.sharpe_ratio,
            expected_max_drawdown=opt.expected_max_drawdown,
            regime_exposure=opt.regime_exposure,
            asset_sizes=asset_results,
            total_risk_amount=result.total_risk_amount,
            total_contracts=result.total_contracts,
            corr_regime=result.corr_regime or "",
            corr_confidence=result.corr_confidence,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
