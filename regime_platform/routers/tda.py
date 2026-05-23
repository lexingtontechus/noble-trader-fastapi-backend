"""TDA (Topological Data Analysis) endpoints — persistent homology feature extraction."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import TDARequest, TDAResponse
from ..core.tda_engine import TDAEngine
from ..auth.jwt_auth import get_authed_user, TokenData
import numpy as np

router = APIRouter(prefix="/tda", tags=["TDA Features"])
tda_engine = TDAEngine()


@router.post(
    "/features",
    response_model=TDAResponse,
    summary="Extract TDA features from price series using persistent homology",
)
async def extract_tda_features(req: TDARequest, user: TokenData = Depends(get_authed_user)):
    """
    Topological Data Analysis feature extraction:
    1. Compute log-returns from price series
    2. Create Takens time-delay embedding (point cloud)
    3. Compute distance matrix
    4. Compute persistent homology (0-dim: connected components, 1-dim: loops)
    5. Compute Betti numbers at each filtration step
    6. Compute persistence entropy (topological complexity)
    7. Compute anomaly score (deviation from baseline topology)
    8. Estimate regime change probability

    Returns:
    - Persistence statistics (max/mean persistence for 0d and 1d)
    - Betti numbers (connected components and loops at each filtration level)
    - Persistence entropy (0d, 1d, total)
    - Anomaly score (topology deviation)
    - Regime change probability [0, 1]
    - 10-dim feature vector for ML pipeline
    """
    try:
        prices = np.array(req.prices, dtype=float)
        baseline = np.array(req.baseline_prices, dtype=float) if req.baseline_prices else None

        engine = TDAEngine(
            embedding_dim=req.embedding_dim,
            embedding_delay=req.embedding_delay,
            max_filtration=req.max_filtration,
            n_filtration_steps=req.n_filtration_steps,
            anomaly_threshold=req.anomaly_threshold,
        )

        result = engine.extract_features(prices, symbol=req.symbol, baseline_prices=baseline)

        return TDAResponse(
            symbol=result.symbol,
            n_bars=result.n_bars,
            n_components=result.n_components,
            n_loops=result.n_loops,
            max_persistence_0d=result.max_persistence_0d,
            mean_persistence_0d=result.mean_persistence_0d,
            max_persistence_1d=result.max_persistence_1d,
            mean_persistence_1d=result.mean_persistence_1d,
            persistence_entropy_0d=result.persistence_entropy_0d,
            persistence_entropy_1d=result.persistence_entropy_1d,
            total_persistence_entropy=result.total_persistence_entropy,
            anomaly_score=result.anomaly_score,
            regime_change_probability=result.regime_change_probability,
            feature_vector=result.feature_vector,
            feature_labels=result.feature_labels,
            betti_0=result.betti_0,
            betti_1=result.betti_1,
        )
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
