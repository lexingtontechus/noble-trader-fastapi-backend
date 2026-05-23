"""Observation builder endpoints — 24-feature and 24+-feature stateful observation vectors."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import ObservationRequest, ObservationResponse, ObservationV2Request, ObservationV2Response
from ..core.obs_builder import InferenceObservationBuilder, F
from ..core.tda_engine import TDAEngine
from ..auth.jwt_auth import get_authed_user, TokenData
import numpy as np

router = APIRouter(prefix="/observation", tags=["Observation Builder"])

# Feature labels in index order (single source of truth, mirrors F class)
FEATURE_LABELS = [
    "log_return_1bar",       # 0
    "log_return_3bar",       # 1
    "log_return_10bar",      # 2
    "normalised_atr",        # 3
    "rolling_vol_20",        # 4
    "ema_distance",          # 5
    "hhll_score",            # 6
    "vol_regime_percentile", # 7
    "atr_vs_baseline",       # 8
    "vol_trend_slope",       # 9
    "regime_prob_0",         # 10
    "regime_prob_1",         # 11
    "regime_prob_2",         # 12
    "regime_prob_3",         # 13
    "vol_prob_low",          # 14
    "vol_prob_medium",       # 15
    "vol_prob_high",         # 16
    "trend_prob_down",       # 17
    "trend_prob_neutral",    # 18
    "trend_prob_up",         # 19
    "regime_quality",        # 20
    "state_confidence",      # 21
    "masaniello_pressure",   # 22
    "drawdown_factor",       # 23
]


@router.post(
    "/build",
    response_model=ObservationResponse,
    summary="Build a 24-feature observation vector from price series",
)
async def build_observation(req: ObservationRequest, user: TokenData = Depends(get_authed_user)):
    """
    Constructs the full 24-feature observation vector used by the RL/ML policy
    layer. Features 14–19 are **Markov features**: live HMM posterior
    probabilities from a fitted RegimeHMM — never defaulted to uniform priors.

    Requires `prices`, `high`, and `low` series (min 81 bars each).

    Workflow:
    1. Seed the `InferenceObservationBuilder` with the price window
    2. Call `build()` to compute the full 24-dim vector
    3. Return the vector with labels, regime info, and quality flags
    """
    try:
        prices = np.array(req.prices, dtype=float)
        high   = np.array(req.high,   dtype=float)
        low    = np.array(req.low,    dtype=float)

        # Instantiate and seed the builder
        builder = InferenceObservationBuilder(
            window=req.window,
            refit_every=req.refit_every,
            n_hmm_states=req.n_hmm_states,
            recommended_f=req.recommended_f,
        )
        builder.seed(prices, high, low)

        # Build the full observation
        obs = builder.build(prices, high, low)

        return ObservationResponse(
            symbol=req.symbol,
            observation_vector=obs.vector.tolist(),
            feature_labels=FEATURE_LABELS,
            vol_probs=obs.vol_probs,
            trend_probs=obs.trend_probs,
            regime_label=obs.regime_label,
            confidence=obs.confidence,
            bar_index=obs.bar_index,
            is_markov_uniform=obs.is_markov_uniform(),
            n_bars_fitted=len(prices),
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))


# TDA feature labels for the extended vector
TDA_FEATURE_LABELS = [
    "tda_n_components",
    "tda_n_loops",
    "tda_max_persistence_0d",
    "tda_mean_persistence_0d",
    "tda_max_persistence_1d",
    "tda_mean_persistence_1d",
    "tda_entropy_0d",
    "tda_entropy_1d",
    "tda_anomaly_score",
    "tda_regime_change_probability",
]


@router.post(
    "/build-v2",
    response_model=ObservationV2Response,
    summary="Build enhanced 24+ feature observation vector with optional TDA features",
)
async def build_observation_v2(req: ObservationV2Request, user: TokenData = Depends(get_authed_user)):
    """
    Enhanced observation builder (v2):
    1. Builds the standard 24-feature observation vector (same as /build)
    2. Optionally appends 10 TDA (Topological Data Analysis) features:
       - n_components, n_loops
       - max/mean persistence (0d, 1d)
       - persistence entropy (0d, 1d)
       - anomaly score, regime change probability
    3. When include_tda=True, the vector extends to 34 features (24 + 10)

    TDA features capture topological structure of the price series:
    - Connected components (0-dim persistence) → market fragmentation
    - Loops (1-dim persistence) → cyclical patterns
    - Anomaly score → topology deviation from baseline
    - Regime change probability → early warning of regime transitions
    """
    try:
        prices = np.array(req.prices, dtype=float)
        high   = np.array(req.high,   dtype=float)
        low    = np.array(req.low,    dtype=float)

        # Build standard 24-feature observation
        builder = InferenceObservationBuilder(
            window=req.window,
            refit_every=req.refit_every,
            n_hmm_states=req.n_hmm_states,
            recommended_f=req.recommended_f,
        )
        builder.seed(prices, high, low)
        obs = builder.build(prices, high, low)

        # Start with the 24-feature vector
        vector = obs.vector.tolist()
        labels = list(FEATURE_LABELS)

        # Optionally append TDA features
        tda_anomaly = None
        tda_regime_change = None

        if req.include_tda:
            try:
                tda_engine = TDAEngine(
                    embedding_dim=req.embedding_dim,
                    embedding_delay=req.embedding_delay,
                )
                tda_result = tda_engine.extract_features(prices, symbol=req.symbol)
                vector.extend(tda_result.feature_vector)
                labels.extend(TDA_FEATURE_LABELS)
                tda_anomaly = tda_result.anomaly_score
                tda_regime_change = tda_result.regime_change_probability
            except Exception:
                # TDA failed — append zeros
                vector.extend([0.0] * 10)
                labels.extend(TDA_FEATURE_LABELS)
                tda_anomaly = 0.0
                tda_regime_change = 0.0

        return ObservationV2Response(
            symbol=req.symbol,
            observation_vector=[round(x, 6) for x in vector],
            feature_labels=labels,
            vol_probs=obs.vol_probs,
            trend_probs=obs.trend_probs,
            regime_label=obs.regime_label,
            confidence=obs.confidence,
            bar_index=obs.bar_index,
            is_markov_uniform=obs.is_markov_uniform(),
            n_bars_fitted=len(prices),
            tda_included=req.include_tda,
            tda_anomaly_score=tda_anomaly,
            tda_regime_change_prob=tda_regime_change,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
