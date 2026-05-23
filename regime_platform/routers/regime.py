"""Regime detection endpoints."""

from fastapi import APIRouter, HTTPException, Depends
from ..models.schemas import RegimeRequest, RegimeResponse, RegimeV2Request, RegimeV2Response
from ..core.regime_engine import RegimeHMM
from ..auth.jwt_auth import get_authed_user, TokenData
import numpy as np

router = APIRouter(prefix="/regime", tags=["Regime Detection"])


@limiter.limit(RATE_LIMIT_COMPUTE)
@router.post("/detect", response_model=RegimeResponse, summary="Detect market regime from price series")
async def detect_regime(req: RegimeRequest, user: TokenData = Depends(get_authed_user)):
    """
    Fits dual Gaussian HMMs (volatility + trend) on the supplied price series
    and returns posterior regime probabilities plus a risk multiplier.
    """
    try:
        prices = np.array(req.prices, dtype=float)
        model  = RegimeHMM()
        model.fit(prices)
        snap   = model.predict(prices)

        return RegimeResponse(
            symbol=req.symbol,
            vol_probs=dict(zip(RegimeHMM.VOL_LABELS, snap.vol_probs)),
            trend_probs=dict(zip(RegimeHMM.TREND_LABELS, snap.trend_probs)),
            vol_state=snap.vol_state,
            trend_state=snap.trend_state,
            regime_label=snap.regime_label,
            confidence=snap.confidence,
            risk_multiplier=snap.risk_multiplier,
            n_bars_fitted=snap.n_bars_fitted,
        )
    except Exception as e:
        raise_http_exception(e, "regime detection")


@limiter.limit(RATE_LIMIT_COMPUTE)
@router.post(
    "/detect-v2",
    response_model=RegimeV2Response,
    summary="Enhanced regime detection with configurable 2-4 states, auto-labeled",
)
async def detect_regime_v2(req: RegimeV2Request, user: TokenData = Depends(get_authed_user)):
    """
    Enhanced HMM regime detection (v2):
    - Configurable 2-4 states per dimension (volatility + trend)
    - Auto-labeled states sorted by emission means
    - Transition matrix entropy (measures regime persistence)
    - Regime stability metric (fraction of recent bars in same regime)

    With 2 states: vol={low, high}, trend={bear, bull} → 4 combined regimes
    With 3 states: vol={low, med, high}, trend={bear, neutral, bull} → 9 combined regimes
    With 4 states: vol={low, med_low, med_high, high}, trend={strong_bear, bear, bull, strong_bull} → 16 combined regimes
    """
    try:
        prices = np.array(req.prices, dtype=float)
        model = RegimeHMM(n_components=req.n_states, n_iter=req.n_iter)
        model.fit(prices)
        snap = model.predict(prices)

        # Compute transition matrix entropy (Shannon entropy of vol transition matrix)
        vol_trans = model._vol_model.transmat_
        trend_trans = model._trend_model.transmat_

        def matrix_entropy(T):
            """Shannon entropy of a transition matrix (row-averaged)."""
            entropies = []
            for row in T:
                p = row[row > 0]
                entropies.append(-float(np.sum(p * np.log2(p))))
            return float(np.mean(entropies))

        trans_entropy = round((matrix_entropy(vol_trans) + matrix_entropy(trend_trans)) / 2.0, 4)

        # Compute regime stability (fraction of last 20 decoded bars in the dominant regime)
        returns = np.diff(np.log(prices))
        vol_feats = model._vol_features(returns)
        trend_feats = model._trend_features(returns)
        vol_states = model._vol_model.decode(vol_feats)[1]
        trend_states = model._trend_model.decode(trend_feats)[1]

        recent_n = min(20, len(vol_states))
        vol_recent = vol_states[-recent_n:]
        trend_recent = trend_states[-recent_n:]

        # Map decoded states to labels
        vol_label_recent = [model._vol_label_map.get(int(s), "unknown") for s in vol_recent]
        trend_label_recent = [model._trend_label_map.get(int(s), "unknown") for s in trend_recent]

        combined_recent = [f"{v}_vol_{t}" for v, t in zip(vol_label_recent, trend_label_recent)]
        dominant_count = max(set(combined_recent), key=combined_recent.count) if combined_recent else ""
        stability = round(combined_recent.count(dominant_count) / max(len(combined_recent), 1), 4)

        # Build appropriate label maps for n_states
        if req.n_states == 2:
            vol_labels = ["low", "high"]
            trend_labels = ["bear", "bull"]
        elif req.n_states == 3:
            vol_labels = ["low", "medium", "high"]
            trend_labels = ["bear", "neutral", "bull"]
        else:
            vol_labels = RegimeHMM.VOL_LABELS  # 4-state
            trend_labels = RegimeHMM.TREND_LABELS

        # Pad or trim probs to match label count
        vol_probs_list = snap.vol_probs
        trend_probs_list = snap.trend_probs

        # If we have fewer labels than probs, aggregate; if more, pad
        vol_probs_dict = {}
        if len(vol_labels) <= len(vol_probs_list):
            # Aggregate extra states
            for i, label in enumerate(vol_labels[:-1]):
                vol_probs_dict[label] = vol_probs_list[i]
            vol_probs_dict[vol_labels[-1]] = sum(vol_probs_list[len(vol_labels)-1:])
        else:
            for i, label in enumerate(vol_labels):
                vol_probs_dict[label] = vol_probs_list[i] if i < len(vol_probs_list) else 0.0

        trend_probs_dict = {}
        if len(trend_labels) <= len(trend_probs_list):
            for i, label in enumerate(trend_labels[:-1]):
                trend_probs_dict[label] = trend_probs_list[i]
            trend_probs_dict[trend_labels[-1]] = sum(trend_probs_list[len(trend_labels)-1:])
        else:
            for i, label in enumerate(trend_labels):
                trend_probs_dict[label] = trend_probs_list[i] if i < len(trend_probs_list) else 0.0

        return RegimeV2Response(
            symbol=req.symbol,
            n_states=req.n_states,
            vol_probs=vol_probs_dict,
            trend_probs=trend_probs_dict,
            vol_state=snap.vol_state,
            trend_state=snap.trend_state,
            regime_label=snap.regime_label,
            confidence=snap.confidence,
            risk_multiplier=snap.risk_multiplier,
            n_bars_fitted=snap.n_bars_fitted,
            transition_entropy=trans_entropy,
            regime_stability=stability,
        )
    except Exception as e:
        raise_http_exception(e, "regime detection v2")
