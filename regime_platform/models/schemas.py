"""Pydantic I/O models for the API."""

from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal


# ─── Shared ───────────────────────────────────────────────────────────────────

class PricePayload(BaseModel):
    prices: list[float] = Field(..., min_length=81, description="Ordered price series (OHLCV close preferred). Min 81 bars.")
    symbol: str = Field(default="UNKNOWN", description="Ticker symbol for labelling.")

    @field_validator("prices")
    @classmethod
    def no_non_positive(cls, v):
        if any(p <= 0 for p in v):
            raise ValueError("All prices must be positive.")
        return v


# ─── Regime ───────────────────────────────────────────────────────────────────

class RegimeRequest(PricePayload):
    pass


class RegimeResponse(BaseModel):
    symbol: str
    vol_probs: dict[str, float]     # {"low": x, "med_low": x, "med_high": x, "high": z}
    trend_probs: dict[str, float]   # {"strong_bear": x, "bear": x, "bull": x, "strong_bull": x}
    vol_state: str
    trend_state: str
    regime_label: str
    confidence: float
    risk_multiplier: float
    n_bars_fitted: int


# ─── Position Sizing ──────────────────────────────────────────────────────────

class SizeRequest(PricePayload):
    returns: Optional[list[float]] = Field(
        default=None,
        description="Use explicit returns instead of computing from prices.",
    )
    kelly_fraction: float = Field(default=0.5, ge=0.1, le=1.0)
    target_vol: float    = Field(default=0.15, gt=0, le=1.0, description="Annualised target vol for scaling.")
    use_regime: bool     = Field(default=True)


class SizeResponse(BaseModel):
    symbol: str
    full_kelly_f: float
    fractional_f: float
    vol_scaled_f: float
    regime_gated_f: float
    recommended_f: float
    kelly_edge: float
    sharpe_ratio: float
    regime_label: str
    regime_multiplier: float
    fraction_type: str
    notes: list[str]


# ─── Risk Analysis ────────────────────────────────────────────────────────────

class RiskRequest(SizeRequest):
    base_risk_limit: float = Field(default=0.02, gt=0, le=0.5, description="Max daily loss fraction at 1× leverage.")


class RiskResponse(BaseModel):
    symbol: str
    # Position
    recommended_f: float
    # VaR / CVaR
    var_95: float
    var_99: float
    cvar_95: float
    cvar_99: float
    # Portfolio stats
    max_drawdown: float
    annual_vol: float
    annual_return: float
    calmar_ratio: float
    sortino_ratio: float
    # Regime
    regime_label: str
    regime_max_loss: float
    # Trade guidance
    suggested_stop: float
    suggested_tp: float
    risk_budget_used: float
    # Kelly notes
    notes: list[str]


# ─── Full Pipeline ────────────────────────────────────────────────────────────

class FullAnalysisRequest(PricePayload):
    kelly_fraction: float = Field(default=0.5, ge=0.1, le=1.0)
    target_vol: float     = Field(default=0.15, gt=0, le=1.0)
    base_risk_limit: float = Field(default=0.02, gt=0, le=0.5)


class FullAnalysisResponse(BaseModel):
    symbol: str
    regime: RegimeResponse
    sizing: SizeResponse
    risk: RiskResponse


# ─── Observation Builder ──────────────────────────────────────────────────────

class ObservationRequest(PricePayload):
    """Request model for building a 24-feature observation vector."""
    high: list[float] = Field(
        ..., min_length=81,
        description="Ordered high-price series (same length as prices).",
    )
    low: list[float] = Field(
        ..., min_length=81,
        description="Ordered low-price series (same length as prices).",
    )
    window: int          = Field(default=200, ge=81, description="Rolling window length for feature computation.")
    refit_every: int     = Field(default=50, ge=1, description="HMM refit interval (bars).")
    n_hmm_states: int    = Field(default=4, ge=2, le=8, description="Number of HMM hidden states.")
    recommended_f: float = Field(default=0.0, ge=0.0, le=1.0, description="Kelly fraction for Masaniello feature (idx 22).")

    @field_validator("high", "low")
    @classmethod
    def no_non_positive_hl(cls, v):
        if any(p <= 0 for p in v):
            raise ValueError("All high/low prices must be positive.")
        return v


class ObservationResponse(BaseModel):
    """24-feature observation vector with Markov regime features."""
    symbol: str
    observation_vector: list[float] = Field(
        ..., min_length=24, max_length=24,
        description="Full 24-feature observation vector.",
    )
    feature_labels: list[str] = Field(
        ..., min_length=24, max_length=24,
        description="Human-readable labels for each feature index.",
    )
    vol_probs: list[float]      = Field(..., description="4-state volatility posteriors [low, med_low, med_high, high].")
    trend_probs: list[float]    = Field(..., description="4-state trend posteriors [strong_bear, bear, bull, strong_bull].")
    regime_label: str           = Field(..., description="Combined regime label, e.g. 'low_vol_bull'.")
    confidence: float           = Field(..., description="HMM confidence scalar.")
    bar_index: int              = Field(..., description="Bar index within the window.")
    is_markov_uniform: bool     = Field(..., description="True if Markov features (14-19) appear suspiciously uniform.")
    n_bars_fitted: int          = Field(..., description="Number of bars used for the HMM fit.")


# ─── Regime V2 (configurable 2-4 states) ────────────────────────────────────

class RegimeV2Request(PricePayload):
    """Request for regime detection with configurable HMM states."""
    n_states: int = Field(default=4, ge=2, le=4, description="Number of HMM states per dimension (2-4).")
    n_iter: int   = Field(default=100, ge=10, le=500, description="Maximum EM iterations.")


class RegimeV2Response(BaseModel):
    """Enhanced regime detection with auto-labeled states."""
    symbol: str
    n_states: int
    vol_probs: dict[str, float]
    trend_probs: dict[str, float]
    vol_state: str
    trend_state: str
    regime_label: str
    confidence: float
    risk_multiplier: float
    n_bars_fitted: int
    transition_entropy: float = Field(..., description="Shannon entropy of the transition matrix (low = persistent regimes).")
    regime_stability: float   = Field(..., description="Fraction of last 20 bars in the same regime [0,1].")


# ─── Strategy Signal ──────────────────────────────────────────────────────────

class StrategySignalRequest(PricePayload):
    """Request for strategy signal + position sizing."""
    kelly_fraction: float     = Field(default=0.5, ge=0.1, le=1.0)
    target_vol: float         = Field(default=0.15, gt=0, le=1.0)
    use_regime: bool          = Field(default=True)
    base_risk_limit: float    = Field(default=0.02, gt=0, le=0.5)
    n_hmm_states: int         = Field(default=4, ge=2, le=4)


class StrategySignalResponse(BaseModel):
    """Strategy signal with regime context and position sizing."""
    symbol: str
    signal: str                = Field(..., description="Trading signal: 'long', 'short', or 'flat'.")
    signal_strength: float     = Field(..., description="Signal strength [0, 1].")
    confidence: float          = Field(..., description="Regime confidence [0, 1].")
    # Regime context
    regime_label: str
    vol_state: str
    trend_state: str
    risk_multiplier: float
    # Position sizing
    recommended_f: float
    full_kelly_f: float
    fractional_f: float
    vol_scaled_f: float
    regime_gated_f: float
    sharpe_ratio: float
    # Risk guidance
    suggested_stop: float
    suggested_tp: float
    var_95: float
    cvar_95: float
    # Notes
    notes: list[str]


# ─── Risk Analyze (enhanced, US spelling) ────────────────────────────────────

class RiskAnalyzeRequest(PricePayload):
    """Enhanced risk analysis request with stress test support."""
    returns: Optional[list[float]] = Field(default=None, description="Explicit returns (optional).")
    kelly_fraction: float     = Field(default=0.5, ge=0.1, le=1.0)
    target_vol: float         = Field(default=0.15, gt=0, le=1.0)
    use_regime: bool          = Field(default=True)
    base_risk_limit: float    = Field(default=0.02, gt=0, le=0.5)
    n_hmm_states: int         = Field(default=4, ge=2, le=4)
    stress_scenarios: bool    = Field(default=True, description="Include stress test scenarios.")


class StressTestResult(BaseModel):
    """Result of a single stress test scenario."""
    scenario: str
    description: str
    simulated_return: float
    simulated_var_95: float
    simulated_max_dd: float


class RiskAnalyzeResponse(BaseModel):
    """Enhanced risk analysis with VaR/CVaR, stress tests, and regime context."""
    symbol: str
    # Position
    recommended_f: float
    # VaR / CVaR
    var_95: float
    var_99: float
    cvar_95: float
    cvar_99: float
    # Portfolio stats
    max_drawdown: float
    annual_vol: float
    annual_return: float
    calmar_ratio: float
    sortino_ratio: float
    # Regime
    regime_label: str
    risk_multiplier: float
    regime_max_loss: float
    # Trade guidance
    suggested_stop: float
    suggested_tp: float
    risk_budget_used: float
    # Stress tests
    stress_tests: list[StressTestResult] = Field(default_factory=list, description="Stress test scenarios.")
    # Notes
    notes: list[str]


# ─── Backtest ──────────────────────────────────────────────────────────────────

class BacktestRequest(PricePayload):
    """Walk-forward backtest request."""
    window: int               = Field(default=200, ge=81, description="Trailing window for HMM fit.")
    refit_every: int          = Field(default=50, ge=10, description="Refit HMM every N bars.")
    n_hmm_states: int         = Field(default=4, ge=2, le=4)
    kelly_fraction: float     = Field(default=0.5, ge=0.1, le=1.0)
    target_vol: float         = Field(default=0.15, gt=0, le=1.0)
    base_risk_limit: float    = Field(default=0.02, gt=0, le=0.5)
    initial_equity: float     = Field(default=100000.0, gt=0)
    commission_bps: float     = Field(default=5.0, ge=0, le=50.0)
    slippage_bps: float       = Field(default=2.0, ge=0, le=50.0)
    max_position_pct: float   = Field(default=0.25, gt=0, le=1.0)
    risk_check: bool          = Field(default=True)
    regime_gate: bool         = Field(default=True)


class BacktestResponse(BaseModel):
    """Walk-forward backtest result with 30+ metrics."""
    symbol: str
    n_bars: int
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    total_return: float
    annual_return: float
    buy_hold_return: float
    excess_return: float
    monthly_return_avg: float
    max_drawdown: float
    max_drawdown_duration: int
    avg_drawdown: float
    var_95: float
    cvar_95: float
    annual_vol: float
    downside_vol: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    information_ratio: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_win_loss_ratio: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    avg_hold_bars: float
    profit_factor: float
    regime_distribution: dict
    trades_by_regime: dict
    equity_curve: list[float]
    drawdown_curve: list[float]
    trade_log: list[dict]


# ─── TDA Features ──────────────────────────────────────────────────────────────

class TDARequest(PricePayload):
    """Topological Data Analysis feature extraction request."""
    embedding_dim: int       = Field(default=3, ge=2, le=10, description="Takens embedding dimension.")
    embedding_delay: int     = Field(default=1, ge=1, le=5, description="Time delay for embedding.")
    max_filtration: float    = Field(default=2.0, gt=0, le=10.0, description="Maximum filtration value (std units).")
    n_filtration_steps: int  = Field(default=20, ge=5, le=100, description="Filtration resolution.")
    anomaly_threshold: float = Field(default=1.5, gt=0, le=5.0, description="Std deviations for anomaly detection.")
    baseline_prices: Optional[list[float]] = Field(
        default=None,
        description="Optional baseline price series for anomaly comparison.",
    )


class TDAResponse(BaseModel):
    """TDA feature extraction result."""
    symbol: str
    n_bars: int
    n_components: int
    n_loops: int
    max_persistence_0d: float
    mean_persistence_0d: float
    max_persistence_1d: float
    mean_persistence_1d: float
    persistence_entropy_0d: float
    persistence_entropy_1d: float
    total_persistence_entropy: float
    anomaly_score: float
    regime_change_probability: float
    feature_vector: list[float] = Field(..., description="10-dim TDA feature vector for ML pipeline.")
    feature_labels: list[str]
    betti_0: list[int]      = Field(..., description="Betti-0 numbers at each filtration step.")
    betti_1: list[int]      = Field(..., description="Betti-1 numbers at each filtration step.")


# ─── Observation V2 ────────────────────────────────────────────────────────────

class ObservationV2Request(PricePayload):
    """Enhanced 24+ feature observation vector builder."""
    high: list[float] = Field(
        ..., min_length=81,
        description="Ordered high-price series (same length as prices).",
    )
    low: list[float] = Field(
        ..., min_length=81,
        description="Ordered low-price series (same length as prices).",
    )
    window: int          = Field(default=200, ge=81, description="Rolling window length.")
    refit_every: int     = Field(default=50, ge=1, description="HMM refit interval.")
    n_hmm_states: int    = Field(default=4, ge=2, le=8)
    recommended_f: float = Field(default=0.0, ge=0.0, le=1.0)
    include_tda: bool    = Field(default=False, description="Include TDA features in the vector (extends to 34 features).")
    embedding_dim: int   = Field(default=3, ge=2, le=10)
    embedding_delay: int = Field(default=1, ge=1, le=5)

    @field_validator("high", "low")
    @classmethod
    def no_non_positive_hl(cls, v):
        if any(p <= 0 for p in v):
            raise ValueError("All high/low prices must be positive.")
        return v


class ObservationV2Response(BaseModel):
    """Enhanced observation vector with optional TDA features."""
    symbol: str
    observation_vector: list[float]
    feature_labels: list[str]
    vol_probs: list[float]
    trend_probs: list[float]
    regime_label: str
    confidence: float
    bar_index: int
    is_markov_uniform: bool
    n_bars_fitted: int
    tda_included: bool = Field(default=False, description="Whether TDA features were appended.")
    tda_anomaly_score: Optional[float] = Field(default=None, description="TDA anomaly score if included.")
    tda_regime_change_prob: Optional[float] = Field(default=None, description="TDA regime change probability if included.")
