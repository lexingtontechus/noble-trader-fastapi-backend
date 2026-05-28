"""Pydantic I/O models for the API."""

from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal


# ─── Shared ───────────────────────────────────────────────────────────────────

class PricePayload(BaseModel):
    prices: list[float] = Field(..., min_length=81, max_length=5000, description="Ordered price series (OHLCV close preferred). Min 81, max 5000 bars.")
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
    # EV Metrics
    entry_ev: float            = Field(default=0.0, description="Expected Value of the trade based on historical pattern stats.")
    expectancy_ratio: float    = Field(default=0.0, description="RR ratio for the trade (Profit/Loss).")
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
    dates: Optional[list[str]] = Field(
        default=None,
        description="ISO date strings matching prices length. Enables date labels in trade log.",
    )
    save: bool = Field(default=True, description="Persist result to database for later retrieval.")


class BacktestResponse(BaseModel):
    """Walk-forward backtest result with 30+ metrics."""
    id: Optional[str] = Field(default=None, description="Database ID if result was saved.")
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
    config_used: dict = Field(default_factory=dict, description="Config snapshot used for this backtest.")
    created_at: Optional[str] = Field(default=None, description="ISO timestamp when result was saved.")
    cached: bool = Field(default=False, description="True if result was served from Redis cache (1h TTL).")


class BacktestSummary(BaseModel):
    """Lightweight backtest summary for list views."""
    id: str
    symbol: str
    n_trades: int
    win_rate: float
    total_return: float
    annual_return: float
    sharpe_ratio: float
    max_drawdown: float
    profit_factor: float
    n_hmm_states: int
    created_at: str


class BacktestHistoryResponse(BaseModel):
    """Paginated list of saved backtest summaries."""
    results: list[BacktestSummary]
    total: int
    offset: int
    limit: int


class BacktestCompareRequest(BaseModel):
    """Request to compare two or more saved backtests."""
    ids: list[str] = Field(..., min_length=2, max_length=10, description="Backtest IDs to compare.")


class BacktestCompareResponse(BaseModel):
    """Side-by-side comparison of backtest results."""
    comparisons: list[BacktestResponse]
    diff: dict = Field(default_factory=dict, description="Key metric differences between first and best result.")


class BacktestOptimizeRequest(PricePayload):
    """Parameter sweep request: run backtests across a grid of parameter values."""
    param_grid: dict[str, list] = Field(
        ...,
        description="Map of parameter name to list of values to sweep. "
        "Supported keys: window, refit_every, n_hmm_states, kelly_fraction, "
        "target_vol, base_risk_limit, commission_bps, slippage_bps, "
        "max_position_pct, risk_check, regime_gate.",
    )
    # Fixed params (applied to every run)
    window: int               = Field(default=200, ge=81)
    refit_every: int          = Field(default=50, ge=10)
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
    save: bool                = Field(default=False, description="Save each result (default false for sweeps).")


class BacktestOptimizeResponse(BaseModel):
    """Grid search results: one summary per parameter combination."""
    results: list[dict] = Field(
        ...,
        description="List of {params, total_return, sharpe_ratio, max_drawdown, "
        "profit_factor, win_rate, n_trades} for each combination.",
    )
    best_by_sharpe: Optional[dict] = Field(default=None, description="Params with highest Sharpe ratio.")
    best_by_return: Optional[dict] = Field(default=None, description="Params with highest total return.")
    n_combinations: int = Field(..., description="Total parameter combinations tested.")
    # Phase 6: Statistical Rigor fields
    deflated_sharpe_result: Optional[dict] = Field(default=None, description="Deflated Sharpe Ratio result (Phase 6A).")
    multiple_testing_results: Optional[dict] = Field(default=None, description="Multiple testing correction results (Phase 6C).")
    significance_test_results: Optional[dict] = Field(default=None, description="Strategy significance test results (Phase 6D).")


class BacktestExportRequest(BaseModel):
    """Request to export a saved backtest result as CSV or JSON."""
    id: str = Field(..., description="Backtest result ID to export.")
    format: Literal["csv", "json"] = Field(default="json", description="Export format.")
    sections: list[Literal["summary", "equity_curve", "drawdown_curve", "trade_log", "all"]] = Field(
        default=["all"],
        description="Which sections to include in the export.",
    )


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


# ─── Dynamic Masaniello Position Sizing ──────────────────────────────────────

class MasanielloSizeRequest(BaseModel):
    """Request for Dynamic Masaniello position sizing (Path B)."""
    equity: float = Field(..., gt=0, description="Current account equity in $.")
    stop_distance_price: float = Field(..., gt=0, description="Stop distance in price units.")
    point_value: float = Field(default=1.0, gt=0, description="$ value per 1 price unit (1.0 for stocks/FX).")
    p_win: float = Field(..., ge=0.0, le=1.0, description="Estimated win probability.")
    reward_risk: float = Field(..., gt=0, description="Expected reward/risk ratio (R multiple).")
    direction: Literal["long", "short"] = Field(default="long")
    symbol: str = Field(default="UNKNOWN")
    # ATR values for volatility factor
    atr_baseline: float = Field(..., gt=0, description="Historical ATR reference.")
    atr_current: float = Field(..., gt=0, description="Current ATR value.")
    # Optional: provide prices to auto-detect regime
    prices: Optional[list[float]] = Field(default=None, min_length=81, description="Price series for auto regime detection.")
    # Batch state (optional - if not provided, fresh batch is assumed)
    wins_so_far: int = Field(default=0, ge=0)
    losses_so_far: int = Field(default=0, ge=0)
    trade_index: int = Field(default=1, ge=1)
    current_drawdown: float = Field(default=0.0, ge=0.0, le=1.0)
    # Sizing config overrides
    base_risk: float = Field(default=0.005, gt=0, le=0.05, description="Base risk fraction β (default 0.5%).")
    min_risk: float = Field(default=0.0025, ge=0.0001, le=0.01)
    max_risk: float = Field(default=0.010, ge=0.001, le=0.05)
    min_prob: float = Field(default=0.50, ge=0.0, le=1.0)
    min_rr: float = Field(default=1.50, ge=0.5, le=10.0, description="Min reward/risk (lowered from 2.50 default).")
    use_regime: bool = Field(default=True, description="Auto-detect regime from prices if True.")
    use_kelly_overlay: bool = Field(default=False)
    kelly_fraction: float = Field(default=0.25, ge=0.01, le=1.0)
    batch_size: int = Field(default=5, ge=2, le=50)
    target_wins: int = Field(default=3, ge=1, le=25)


class MasanielloSizeResponse(BaseModel):
    """Response from Dynamic Masaniello position sizing."""
    allowed: bool
    risk_fraction: float
    risk_amount: float
    units: float
    contracts: int
    # Factors (for inspection/logging)
    masaniello_factor: float = Field(description="M_i: urgency factor (wins needed / trades left).")
    quality_factor: float = Field(description="Q_i: model quality composite (prob × regime × confidence).")
    drawdown_factor: float = Field(description="DD_i: drawdown scaling factor.")
    volatility_factor: float = Field(description="V_i: volatility adjustment factor.")
    expected_edge: float = Field(description="E = p·R - (1-p).")
    kelly_cap: Optional[float] = Field(default=None, description="Fractional Kelly cap if overlay active.")
    reason: str
    # Regime context
    regime_label: str = ""
    regime_quality: float = 0.0
    # Batch state
    batch_num: int = 1
    trade_index: int = 1
    wins_so_far: int = 0
    losses_so_far: int = 0
    drawdown: float = 0.0
    # Symbol/direction
    symbol: str = ""
    direction: str = "long"


class MasanielloBatchRequest(BaseModel):
    """Request to size an entire batch of trades sequentially."""
    # Base context (shared fields)
    equity: float = Field(..., gt=0)
    point_value: float = Field(default=1.0, gt=0)
    direction: Literal["long", "short"] = Field(default="long")
    symbol: str = Field(default="UNKNOWN")
    atr_baseline: float = Field(..., gt=0)
    # Per-trade details (must be same length)
    stop_distances: list[float] = Field(..., min_length=1, max_length=50, description="Stop distance per trade.")
    p_wins: list[float] = Field(..., min_length=1, max_length=50, description="Win probability per trade.")
    reward_risks: list[float] = Field(..., min_length=1, max_length=50, description="R multiple per trade.")
    atr_currents: list[float] = Field(..., min_length=1, max_length=50, description="Current ATR per trade.")
    # Outcomes (optional - if provided, updates batch state)
    outcomes: Optional[list[bool]] = Field(default=None, description="Win/loss outcomes (True=win, False=loss).")
    pnls: Optional[list[float]] = Field(default=None, description="PnL per trade.")
    # Config
    base_risk: float = Field(default=0.005, gt=0, le=0.05)
    min_rr: float = Field(default=1.50, ge=0.5, le=10.0)
    batch_size: int = Field(default=5, ge=2, le=50)
    target_wins: int = Field(default=3, ge=1, le=25)
    use_kelly_overlay: bool = Field(default=False)
    kelly_fraction: float = Field(default=0.25, ge=0.01, le=1.0)
    # Regime
    prices: Optional[list[float]] = Field(default=None, min_length=81, description="For auto regime detection.")


class MasanielloBatchResponse(BaseModel):
    """Response from batch Masaniello sizing."""
    results: list[MasanielloSizeResponse]
    batch_summary: dict = Field(description="Batch statistics: total wins, losses, PnL, drawdown.")


class PortfolioMasanielloRequest(BaseModel):
    """Request for portfolio-optimised Masaniello sizing across multiple assets.
    
    Two-stage pipeline:
      Stage 1: DrawdownOptimiser -> portfolio weights
      Stage 2: DynamicMasanielloSizer -> per-asset position sizes
    """
    symbols: list[str] = Field(..., min_length=2, max_length=50)
    returns_matrix: list[list[float]] = Field(..., description="(n_bars × n_assets) matrix of log-returns.")
    equity: float = Field(..., gt=0, description="Total account equity.")
    # Per-asset trade context
    stop_distances: list[float] = Field(..., description="Stop distance per asset.")
    point_values: list[float] = Field(default=None, description="$ per 1 price unit per asset (null = all 1.0).")
    p_wins: list[float] = Field(..., description="Win probability per asset.")
    reward_risks: list[float] = Field(..., description="R multiple per asset.")
    atr_baselines: list[float] = Field(..., description="Historical ATR per asset.")
    atr_currents: list[float] = Field(..., description="Current ATR per asset.")
    directions: Optional[list[str]] = Field(default=None, description="Direction per asset (default all 'long').")
    # Optimiser config
    max_dd_limit: float = Field(default=0.20, ge=0.01, le=1.0)
    max_weight: float = Field(default=0.40, ge=0.05, le=1.0)
    # Masaniello config
    base_risk: float = Field(default=0.005, gt=0, le=0.05)
    min_rr: float = Field(default=1.50, ge=0.5, le=10.0)
    batch_size: int = Field(default=5, ge=2, le=50)
    target_wins: int = Field(default=3, ge=1, le=25)
    use_kelly_overlay: bool = Field(default=False)
    kelly_fraction: float = Field(default=0.25, ge=0.01, le=1.0)


class AssetMasanielloResult(BaseModel):
    """Masaniello sizing result for a single asset within a portfolio."""
    symbol: str
    # Optimizer output
    portfolio_weight: float
    capital_allocation: float = Field(description="portfolio_weight × equity in $.")
    # Masaniello output
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


class PortfolioMasanielloResponse(BaseModel):
    """Full two-stage pipeline response: optimizer weights + per-asset Masaniello sizing."""
    # Stage 1: Portfolio optimisation
    symbols: list[str]
    weights: list[float]
    regime_adj_weights: list[float]
    expected_return: float
    expected_vol: float
    sharpe_ratio: float
    expected_max_drawdown: float
    regime_exposure: float
    # Stage 2: Per-asset Masaniello sizing
    asset_sizes: list[AssetMasanielloResult]
    total_risk_amount: float = Field(description="Sum of risk_amount across all assets.")
    total_contracts: int = Field(description="Sum of contracts across all assets.")
    # Correlation regime
    corr_regime: str = ""
    corr_confidence: float = 0.0


# ─── P&L Dashboard ────────────────────────────────────────────────────────────

class RiskDashboardResponse(BaseModel):
    """Live portfolio risk metrics dashboard."""
    total_return: float = 0
    total_return_pct: float = 0
    annual_return_pct: float = 0
    daily_return_avg: float = 0
    annual_vol: float = 0
    downside_vol: float = 0
    sharpe_ratio: float = 0
    sortino_ratio: float = 0
    calmar_ratio: float = 0
    max_drawdown: float = 0
    max_drawdown_pct: float = 0
    current_drawdown: float = 0
    current_drawdown_pct: float = 0
    avg_drawdown: float = 0
    var_95: float = 0
    var_99: float = 0
    cvar_95: float = 0
    cvar_99: float = 0
    win_days: int = 0
    loss_days: int = 0
    win_rate: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    profit_factor: float = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    n_data_points: int = 0
    period_days: int = 0
    start_equity: float = 0
    end_equity: float = 0
    computed_at: str = ""
