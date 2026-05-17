"""Position sizing endpoints."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import (
    SizeRequest, SizeResponse,
    MasanielloSizeRequest, MasanielloSizeResponse,
    MasanielloBatchRequest, MasanielloBatchResponse,
    PortfolioMasanielloRequest, PortfolioMasanielloResponse,
)
from ..core.regime_engine import RegimeHMM
from ..core.position_sizer import (
    PositionSizer, PositionRequest,
    DynamicMasanielloSizer, SizingConfig, TradeContext,
)
from ..auth.jwt_auth import get_authed_user, TokenData
import numpy as np

router = APIRouter(prefix="/size", tags=["Position Sizing"])
sizer  = PositionSizer()


@router.post("/kelly", response_model=SizeResponse, summary="Regime-aware Kelly position sizing")
async def kelly_size(req: SizeRequest):
    """
    Computes fractional Kelly position size, optionally scaled by:
    - Current realised volatility vs target volatility
    - Dynamic regime risk multiplier from HMM
    """
    try:
        prices  = np.array(req.prices, dtype=float)
        returns = np.diff(np.log(prices)) if req.returns is None else np.array(req.returns, dtype=float)

        regime = None
        if req.use_regime:
            model = RegimeHMM()
            model.fit(prices)
            regime = model.predict(prices)

        result = sizer.size(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=req.kelly_fraction,
            target_vol=req.target_vol,
            regime=regime,
        ))

        return SizeResponse(
            symbol=req.symbol,
            full_kelly_f=result.full_kelly_f,
            fractional_f=result.fractional_f,
            vol_scaled_f=result.vol_scaled_f,
            regime_gated_f=result.regime_gated_f,
            recommended_f=result.recommended_f,
            kelly_edge=result.kelly_edge,
            sharpe_ratio=result.sharpe_ratio,
            regime_label=result.regime_label,
            regime_multiplier=result.regime_multiplier,
            fraction_type=result.fraction_type,
            notes=result.notes,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/masaniello",
    response_model=MasanielloSizeResponse,
    summary="Dynamic Masaniello position sizing (Path B)",
)
async def masaniello_size(
    req: MasanielloSizeRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Computes position size using the Dynamic Masaniello formula:
    
        f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
    
    Where:
    - M_i = Masaniello progress factor (urgency: wins needed / trades left)
    - Q_i = Model quality (prob × regime × confidence)
    - DD_i = Drawdown scaling (never below 0.25)
    - V_i = Volatility adjustment (ATR-based)
    
    Supports optional Kelly overlay cap and regime auto-detection from prices.
    """
    try:
        # Build config from request overrides
        config = SizingConfig(
            base_risk=req.base_risk,
            min_risk=req.min_risk,
            max_risk=req.max_risk,
            min_prob=req.min_prob,
            min_rr=req.min_rr,
            use_kelly_overlay=req.use_kelly_overlay,
            kelly_fraction=req.kelly_fraction,
            batch_size=req.batch_size,
            target_wins=req.target_wins,
        )
        
        # Detect regime if prices provided and use_regime=True
        regime_quality = 1.0
        state_confidence = 0.75
        regime_label = "no_regime"
        
        if req.use_regime and req.prices:
            model = RegimeHMM()
            model.fit(np.array(req.prices, dtype=float))
            snap = model.predict(np.array(req.prices, dtype=float))
            regime_quality = snap.risk_multiplier
            state_confidence = snap.confidence
            regime_label = snap.regime_label
        
        # Create sizer with fresh batch state
        sizer_m = DynamicMasanielloSizer.create(config, equity=req.equity)
        
        # Build trade context
        ctx = TradeContext(
            equity=req.equity,
            stop_distance_price=req.stop_distance_price,
            point_value=req.point_value,
            p_win=req.p_win,
            reward_risk=req.reward_risk,
            regime_quality=regime_quality,
            state_confidence=state_confidence,
            current_drawdown=req.current_drawdown,
            atr_baseline=req.atr_baseline,
            atr_current=req.atr_current,
            wins_so_far=req.wins_so_far,
            losses_so_far=req.losses_so_far,
            trade_index=req.trade_index,
            batch_size=req.batch_size,
            target_wins=req.target_wins,
            direction=req.direction,
            symbol=req.symbol,
            regime_label=regime_label,
        )
        
        result = sizer_m.size_trade(ctx)
        
        return MasanielloSizeResponse(
            allowed=result.allowed,
            risk_fraction=result.risk_fraction,
            risk_amount=result.risk_amount,
            units=result.units,
            contracts=result.contracts,
            masaniello_factor=result.masaniello_factor,
            quality_factor=result.quality_factor,
            drawdown_factor=result.drawdown_factor,
            volatility_factor=result.volatility_factor,
            expected_edge=result.expected_edge,
            kelly_cap=result.kelly_cap,
            reason=result.reason,
            regime_label=regime_label,
            regime_quality=regime_quality,
            batch_num=sizer_m.batch.batch_num,
            trade_index=req.trade_index,
            wins_so_far=req.wins_so_far,
            losses_so_far=req.losses_so_far,
            drawdown=req.current_drawdown,
            symbol=req.symbol,
            direction=req.direction,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/masaniello/batch",
    response_model=MasanielloBatchResponse,
    summary="Batch Dynamic Masaniello sizing for a sequence of trades",
)
async def masaniello_batch(
    req: MasanielloBatchRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Sizes an entire batch of trades sequentially, with batch state
    (wins, losses, drawdown) flowing between trades automatically.
    
    Optionally provide outcomes and PnLs to update batch state as
    the batch is sized.
    """
    try:
        if not (len(req.stop_distances) == len(req.p_wins) == len(req.reward_risks) == len(req.atr_currents)):
            raise ValueError("stop_distances, p_wins, reward_risks, atr_currents must all be same length.")
        
        # Detect regime if prices provided
        regime_quality = 1.0
        state_confidence = 0.75
        regime_label = "no_regime"
        
        if req.prices:
            model = RegimeHMM()
            model.fit(np.array(req.prices, dtype=float))
            snap = model.predict(np.array(req.prices, dtype=float))
            regime_quality = snap.risk_multiplier
            state_confidence = snap.confidence
            regime_label = snap.regime_label
        
        config = SizingConfig(
            base_risk=req.base_risk,
            min_rr=req.min_rr,
            batch_size=req.batch_size,
            target_wins=req.target_wins,
            use_kelly_overlay=req.use_kelly_overlay,
            kelly_fraction=req.kelly_fraction,
        )
        sizer_m = DynamicMasanielloSizer.create(config, equity=req.equity)
        
        # Build contexts
        contexts = []
        for i in range(len(req.stop_distances)):
            ctx = TradeContext(
                equity=req.equity,
                stop_distance_price=req.stop_distances[i],
                point_value=req.point_value,
                p_win=req.p_wins[i],
                reward_risk=req.reward_risks[i],
                regime_quality=regime_quality,
                state_confidence=state_confidence,
                current_drawdown=0.0,  # injected by run_batch
                atr_baseline=req.atr_baseline,
                atr_current=req.atr_currents[i],
                wins_so_far=0,  # injected by run_batch
                losses_so_far=0,
                trade_index=1,  # injected by run_batch
                batch_size=req.batch_size,
                target_wins=req.target_wins,
                direction=req.direction,
                symbol=req.symbol,
                regime_label=regime_label,
            )
            contexts.append(ctx)
        
        results, batch = sizer_m.run_batch(
            contexts,
            outcomes=req.outcomes,
            pnls=req.pnls,
        )
        
        response_results = []
        for r in results:
            response_results.append(MasanielloSizeResponse(
                allowed=r.allowed,
                risk_fraction=r.risk_fraction,
                risk_amount=r.risk_amount,
                units=r.units,
                contracts=r.contracts,
                masaniello_factor=r.masaniello_factor,
                quality_factor=r.quality_factor,
                drawdown_factor=r.drawdown_factor,
                volatility_factor=r.volatility_factor,
                expected_edge=r.expected_edge,
                kelly_cap=r.kelly_cap,
                reason=r.reason,
                regime_label=regime_label,
                symbol=req.symbol,
                direction=req.direction,
            ))
        
        return MasanielloBatchResponse(
            results=response_results,
            batch_summary={
                "batch_num": batch.batch_num,
                "wins": batch.wins,
                "losses": batch.losses,
                "pnl": round(batch.pnl, 2),
                "drawdown": round(batch.drawdown, 4),
                "trade_index": batch.trade_index,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post(
    "/masaniello/portfolio",
    response_model=PortfolioMasanielloResponse,
    summary="Portfolio-optimised Masaniello sizing across multiple assets",
)
async def masaniello_portfolio(
    req: PortfolioMasanielloRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Two-stage pipeline for multi-asset position sizing:
    
    Stage 1: DrawdownOptimiser → regime-adjusted portfolio weights
    Stage 2: DynamicMasanielloSizer → per-asset contract sizes
    
    Each asset's capital allocation comes from the optimiser, and
    the Masaniello formula sizes within that allocation.
    """
    try:
        from ..core.portfolio_optimiser import DrawdownOptimiser, OptimiserConfig
        from ..core.correlation_regime import CorrelationRegimeDetector, CorrelationRegimeConfig
        from ..models.schemas import AssetMasanielloResult
        
        n = len(req.symbols)
        returns_matrix = np.array(req.returns_matrix, dtype=float)
        
        # Validate dimensions
        if returns_matrix.ndim != 2 or returns_matrix.shape[1] != n:
            raise ValueError(
                f"returns_matrix shape {returns_matrix.shape} doesn't match {n} symbols."
            )
        
        point_values = req.point_values if req.point_values else [1.0] * n
        directions = req.directions if req.directions else ["long"] * n
        
        # ── Auto-detect per-asset regimes ────────────────────────────────
        asset_snaps = []
        for j in range(n):
            try:
                prices = 100.0 * np.cumprod(1 + returns_matrix[:, j])
                model = RegimeHMM()
                model.fit(prices)
                asset_snaps.append(model.predict(prices))
            except Exception:
                asset_snaps.append(None)
        
        # ── Auto-detect correlation regime ───────────────────────────────
        corr_regime = "mid_corr"
        corr_confidence = 0.5
        try:
            corr_config = CorrelationRegimeConfig()
            detector = CorrelationRegimeDetector(corr_config)
            corr_snap = detector.detect(returns_matrix, req.symbols, asset_snaps)
            corr_regime = corr_snap.corr_regime
            corr_confidence = corr_snap.corr_confidence
        except Exception:
            pass
        
        # ── Stage 1: Portfolio Optimisation ──────────────────────────────
        opt_config = OptimiserConfig(
            max_dd_limit=req.max_dd_limit,
            max_weight=req.max_weight,
        )
        opt = DrawdownOptimiser(opt_config)
        opt_result = opt.optimise(
            returns_matrix=returns_matrix,
            symbols=req.symbols,
            asset_snaps=asset_snaps,
            corr_regime=corr_regime,
        )
        
        weights = opt_result.regime_adj_weights
        
        # ── Stage 2: Per-Asset Masaniello Sizing ────────────────────────
        sizing_config = SizingConfig(
            base_risk=req.base_risk,
            min_rr=req.min_rr,
            batch_size=req.batch_size,
            target_wins=req.target_wins,
            use_kelly_overlay=req.use_kelly_overlay,
            kelly_fraction=req.kelly_fraction,
        )
        
        asset_sizes = []
        total_risk = 0.0
        total_contracts = 0
        
        for j in range(n):
            capital_alloc = weights[j] * req.equity
            snap = asset_snaps[j] if j < len(asset_snaps) else None
            
            regime_quality = snap.risk_multiplier if snap else 1.0
            state_confidence = snap.confidence if snap else 0.75
            regime_label = snap.regime_label if snap else "no_regime"
            
            sizer_m = DynamicMasanielloSizer.create(sizing_config, equity=capital_alloc)
            
            ctx = TradeContext(
                equity=capital_alloc,
                stop_distance_price=req.stop_distances[j],
                point_value=point_values[j],
                p_win=float(np.clip(req.p_wins[j], 0.0, 1.0)),
                reward_risk=float(max(0.0, req.reward_risks[j])),
                regime_quality=regime_quality,
                state_confidence=state_confidence,
                current_drawdown=0.0,
                atr_baseline=req.atr_baselines[j],
                atr_current=req.atr_currents[j],
                wins_so_far=0,
                losses_so_far=0,
                trade_index=1,
                batch_size=req.batch_size,
                target_wins=req.target_wins,
                direction=directions[j],
                symbol=req.symbols[j],
                regime_label=regime_label,
            )
            
            result = sizer_m.size_trade(ctx)
            
            asset_sizes.append(AssetMasanielloResult(
                symbol=req.symbols[j],
                portfolio_weight=weights[j],
                capital_allocation=capital_alloc,
                allowed=result.allowed,
                risk_fraction=result.risk_fraction,
                risk_amount=result.risk_amount,
                contracts=result.contracts,
                masaniello_factor=result.masaniello_factor,
                quality_factor=result.quality_factor,
                drawdown_factor=result.drawdown_factor,
                volatility_factor=result.volatility_factor,
                expected_edge=result.expected_edge,
                reason=result.reason,
                regime_label=regime_label,
            ))
            
            total_risk += result.risk_amount
            total_contracts += result.contracts
        
        return PortfolioMasanielloResponse(
            symbols=req.symbols,
            weights=opt_result.weights,
            regime_adj_weights=opt_result.regime_adj_weights,
            expected_return=opt_result.expected_return,
            expected_vol=opt_result.expected_vol,
            sharpe_ratio=opt_result.sharpe_ratio,
            expected_max_drawdown=opt_result.expected_max_drawdown,
            regime_exposure=opt_result.regime_exposure,
            asset_sizes=asset_sizes,
            total_risk_amount=round(total_risk, 2),
            total_contracts=total_contracts,
            corr_regime=corr_regime,
            corr_confidence=corr_confidence,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
