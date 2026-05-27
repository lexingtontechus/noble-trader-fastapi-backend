"""
Masaniello Bridge Service
═════════════════════════

Connects the DrawdownOptimiser (portfolio weights) to the
DynamicMasanielloSizer (per-asset position sizing).

Two-stage pipeline:
  Stage 1: DrawdownOptimiser -> regime-adjusted portfolio weights
  Stage 2: DynamicMasanielloSizer -> per-asset contract sizes

Usage
─────
  bridge = MasanielloBridge()
  result = bridge.optimise_and_size(
      returns_matrix=returns,
      symbols=["SPY", "QQQ", "GLD", "TLT"],
      equity=100_000,
      stop_distances=[2.0, 3.0, 1.5, 1.0],
      p_wins=[0.60, 0.55, 0.58, 0.52],
      reward_risks=[2.5, 2.0, 2.0, 1.5],
      atr_baselines=[10.0, 15.0, 8.0, 5.0],
      atr_currents=[10.0, 15.0, 8.0, 5.0],
  )
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional

from ..core.portfolio_optimiser import DrawdownOptimiser, OptimiserConfig, OptimisationResult
from ..core.position_sizer import DynamicMasanielloSizer, SizingConfig, TradeContext, PositionSizeResult
from ..core.regime_engine import RegimeHMM, RegimeSnapshot
from ..core.correlation_regime import CorrelationRegimeDetector, CorrelationRegimeConfig


@dataclass
class AssetSizingResult:
    """Per-asset result from the two-stage pipeline."""
    symbol: str
    portfolio_weight: float
    capital_allocation: float
    allowed: bool
    risk_fraction: float
    risk_amount: float
    contracts: int
    masaniello_factor: float
    quality_factor: float
    drawdown_factor: float
    volatility_factor: float
    expected_edge: float
    reason: str
    regime_label: str = ""


@dataclass
class BridgeResult:
    """Full two-stage pipeline result."""
    # Stage 1: Portfolio optimisation
    optimisation: OptimisationResult
    # Stage 2: Per-asset Masaniello sizing
    asset_sizes: List[AssetSizingResult]
    total_risk_amount: float
    total_contracts: int
    # Correlation regime
    corr_regime: str = ""
    corr_confidence: float = 0.0


class MasanielloBridge:
    """
    Two-stage pipeline: Portfolio Optimiser -> Per-Asset Masaniello Sizing.
    
    Stage 1 solves for optimal portfolio weights using regime-adjusted
    expected returns and drawdown constraints.
    
    Stage 2 sizes each asset's position within its capital allocation
    using the Dynamic Masaniello formula, which adapts to:
    - Batch progress (M_i urgency factor)
    - Regime quality (Q_i quality factor)
    - Drawdown level (DD_i scaling)
    - Volatility regime (V_i adjustment)
    """
    
    def __init__(
        self,
        optimiser_config: Optional[OptimiserConfig] = None,
        sizing_config: Optional[SizingConfig] = None,
    ):
        self.optimiser_config = optimiser_config or OptimiserConfig()
        self.sizing_config = sizing_config or SizingConfig()
    
    def optimise_and_size(
        self,
        returns_matrix: np.ndarray,
        symbols: List[str],
        equity: float,
        stop_distances: List[float],
        p_wins: List[float],
        reward_risks: List[float],
        atr_baselines: List[float],
        atr_currents: List[float],
        point_values: Optional[List[float]] = None,
        directions: Optional[List[str]] = None,
        asset_snaps: Optional[List[Optional[RegimeSnapshot]]] = None,
        corr_regime: Optional[str] = None,
        kelly_fractions: Optional[List[float]] = None,
    ) -> BridgeResult:
        """
        Run the full two-stage pipeline.
        
        Parameters
        ──────────
        returns_matrix : (n_bars, n_assets) log-returns
        symbols        : Asset labels
        equity         : Total account equity
        stop_distances : Per-asset stop distance in price units
        p_wins         : Per-asset estimated win probability
        reward_risks   : Per-asset expected R multiple
        atr_baselines  : Per-asset historical ATR
        atr_currents   : Per-asset current ATR
        point_values   : Per-asset $ per 1 price unit (default all 1.0)
        directions     : Per-asset "long" or "short" (default all "long")
        asset_snaps    : Per-asset regime snapshots (auto-detected if None)
        corr_regime    : Correlation regime label (auto-detected if None)
        kelly_fractions: Per-asset Kelly fractions for optimizer weight bounds
        """
        n = len(symbols)
        if point_values is None:
            point_values = [1.0] * n
        if directions is None:
            directions = ["long"] * n
        
        # ── Auto-detect regimes if not provided ──────────────────────────
        if asset_snaps is None:
            asset_snaps = []
            for j in range(n):
                try:
                    prices = 100.0 * np.cumprod(1 + returns_matrix[:, j])
                    model = RegimeHMM()
                    model.fit(prices)
                    asset_snaps.append(model.predict(prices))
                except Exception:
                    asset_snaps.append(None)
        
        # ── Auto-detect correlation regime if not provided ───────────────
        corr_confidence = 0.0
        if corr_regime is None and asset_snaps:
            try:
                config = CorrelationRegimeConfig()
                detector = CorrelationRegimeDetector(config)
                corr_snap = detector.detect(returns_matrix, symbols, asset_snaps)
                corr_regime = corr_snap.corr_regime
                corr_confidence = corr_snap.corr_confidence
            except Exception:
                corr_regime = "mid_corr"
                corr_confidence = 0.5
        elif corr_regime is not None:
            corr_confidence = 0.0
        
        # ── Stage 1: Portfolio Optimisation ──────────────────────────────
        opt = DrawdownOptimiser(self.optimiser_config)
        opt_result = opt.optimise(
            returns_matrix=returns_matrix,
            symbols=symbols,
            asset_snaps=asset_snaps,
            corr_regime=corr_regime,
            kelly_fractions=kelly_fractions,
        )
        
        # Use regime-adjusted weights for capital allocation
        weights = opt_result.regime_adj_weights
        
        # ── Stage 2: Per-Asset Masaniello Sizing ────────────────────────
        asset_sizes: List[AssetSizingResult] = []
        total_risk = 0.0
        total_contracts = 0
        
        for j in range(n):
            # Capital allocated to this asset
            capital_alloc = weights[j] * equity
            snap = asset_snaps[j] if j < len(asset_snaps) else None
            
            # Build Masaniello config per asset
            sizer = DynamicMasanielloSizer.create(self.sizing_config, equity=capital_alloc)
            
            # Extract regime from snapshot
            regime_quality = snap.risk_multiplier if snap else 1.0
            state_confidence = snap.confidence if snap else 0.75
            regime_label = snap.regime_label if snap else "no_regime"
            
            ctx = TradeContext(
                equity=capital_alloc,  # Size within allocated capital
                stop_distance_price=stop_distances[j],
                point_value=point_values[j],
                p_win=float(np.clip(p_wins[j], 0.0, 1.0)),
                reward_risk=float(max(0.0, reward_risks[j])),
                regime_quality=regime_quality,
                state_confidence=state_confidence,
                current_drawdown=0.0,
                atr_baseline=atr_baselines[j],
                atr_current=atr_currents[j],
                wins_so_far=0,
                losses_so_far=0,
                trade_index=1,
                batch_size=self.sizing_config.batch_size,
                target_wins=self.sizing_config.target_wins,
                direction=directions[j],
                symbol=symbols[j],
                regime_label=regime_label,
            )
            
            result = sizer.size_trade(ctx)
            
            asset_sizes.append(AssetSizingResult(
                symbol=symbols[j],
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
        
        return BridgeResult(
            optimisation=opt_result,
            asset_sizes=asset_sizes,
            total_risk_amount=round(total_risk, 2),
            total_contracts=total_contracts,
            corr_regime=corr_regime or "",
            corr_confidence=corr_confidence,
        )
