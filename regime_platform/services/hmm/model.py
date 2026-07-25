"""
Gaussian HMM model — multivariate Baum-Welch fit + 3-state emit for p_markov.

This module replaces the discrete MarkovChain as the source of p_markov.
It fits a 5-state Gaussian HMM (via hmmlearn) on the 9-feature vector from
FeatureBuffer, then maps the 5 states to {UP, DOWN, FLAT} for the EV engine's
existing 3x3 transition matrix contract.

Design:
  - 5 states (hardcoded): calm_trend / choppy_range / high_vol_breakout /
    liquidity_drained / regime_transition
  - 9-feature emission (see feature_buffer.py)
  - Refit every sweep tick (5min) from the rolling 300-bar window
  - State → {UP, DOWN, FLAT} mapping based on per-state mean log_return
  - Output: (transition_matrix_3x3, current_state) — drop-in for the old
    _build_markov_chain() return shape

Bootstrap:
  - On startup, /api/price/batch returns ~300 OHLCV bars per symbol
  - log_return + 5 TA indicators computed locally from OHLCV
  - 3 microstructure features are 0.0 until live /api/quote data arrives
  - HMM fits immediately on the bootstrap data — no cold-start gap
"""

from __future__ import annotations

import logging
import math
import pickle
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from regime_platform.services.hmm.feature_buffer import (
    FEATURE_NAMES,
    N_FEATURES,
    FeatureBuffer,
)

logger = logging.getLogger("regime.hmm.model")

# ─── Constants ───────────────────────────────────────────────────────────────

# HMM state count — hardcoded per proposal. If a future backtest shows 3 or 7
# is better, edit this constant.
N_STATES: int = 5

# State labels (for logging / diagnostics only — the HMM is unlabeled).
STATE_LABELS: list[str] = [
    "calm_trend",
    "choppy_range",
    "high_vol_breakout",
    "liquidity_drained",
    "regime_transition",
]

# Log-return threshold for mapping HMM states to {UP, DOWN, FLAT}.
# If a state's mean log_return > threshold → UP; < -threshold → DOWN; else FLAT.
# The threshold is relative to the overall log_return std across the window.
STATE_MAP_THRESHOLD_STDS: float = 0.25  # 0.25 std from zero → directional

# 3-state labels (matching the old MarkovChain.STATES).
STATES_3: list[str] = ["UP", "DOWN", "FLAT"]

# Minimum bars required before the HMM will fit.
MIN_FIT_BARS: int = 30

# Model persistence path — crash recovery only. On normal startup, the
# bootstrap from /api/price/batch repopulates the buffer and the HMM refits.
MODEL_PATH: str = "/data/hmm_model.pkl"


# ─── HMM wrapper ────────────────────────────────────────────────────────────


class HmmModel:
    """Multivariate Gaussian HMM with 3-state emit contract.

    Lifecycle:
      1. Construct: HmmModel(buffer)
      2. Bootstrap:  await model.bootstrap_from_ohlcv(symbols, ohlcv_map)
      3. Each tick:  model.fit(symbol)  → refits HMM from buffer
                     matrix, state = model.emit_markov(symbol, direction)
      4. On restart: load pickled model (if buffer is empty) or refit from
                     bootstrap (normal path)

    Thread-safety: fit() and emit_markov() are guarded by a per-symbol lock.
    The _models dict (fitted HMM per symbol) is guarded by a global lock.
    """

    def __init__(self, buffer: FeatureBuffer) -> None:
        self._buffer = buffer
        # Per-symbol fitted HMM + state mapping
        self._models: dict[str, _FittedModel] = {}
        self._global_lock = threading.Lock()

    # ─── Bootstrap ───────────────────────────────────────────────────────

    def bootstrap_from_ohlcv(
        self,
        symbol: str,
        closes: list[float],
    ) -> int:
        """Bootstrap the feature buffer from OHLCV close prices.

        Computes log_return + 5 TA indicators locally from closes and pushes
        ~N-1 feature vectors to the buffer (the first bar has no log_return).
        Microstructure features (spread, imbalance, volume_persistence) are
        0.0 — they'll be filled by live /api/quote polls.

        Returns the number of feature vectors pushed.
        """
        if len(closes) < 2:
            logger.warning(
                "hmm.bootstrap: %s — only %d closes, need >=2",
                symbol, len(closes),
            )
            return 0

        closes_arr = np.array(closes, dtype=float)
        log_returns = _compute_log_returns(closes_arr)
        ta_features = _compute_ta_indicators_local(closes_arr)

        # Align: log_returns has len = N-1. TA indicators also have len = N-1
        # (they're computed from the same closes, with the first bar dropped
        # since it has no log_return). Pad TA arrays to match.
        n = len(log_returns)

        pushed = 0
        for i in range(n):
            vector = np.zeros(N_FEATURES, dtype=float)
            vector[0] = log_returns[i]  # log_return
            # Features 1-3 (microstructure) = 0.0 — not available in bootstrap
            if i < len(ta_features["adx"]):
                vector[4] = ta_features["adx"][i] or 0.0
                vector[5] = ta_features["ao"][i] or 0.0
                vector[6] = ta_features["bb_power"][i] or 0.0
                vector[7] = ta_features["macd"][i] or 0.0
                vector[8] = ta_features["rsi"][i] or 0.0
            self._buffer.push(symbol, vector)
            pushed += 1

        logger.info(
            "hmm.bootstrap: %s — pushed %d vectors (closes=%d)",
            symbol, pushed, len(closes),
        )
        return pushed

    # ─── Fit ─────────────────────────────────────────────────────────────

    def fit(self, symbol: str) -> bool:
        """Fit (or refit) the HMM for a symbol from the feature buffer.

        Returns True if the model was fitted, False if insufficient data.
        """
        mat = self._buffer.matrix(symbol)
        if mat is None or len(mat) < MIN_FIT_BARS:
            return False

        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            logger.error(
                "hmm.fit: hmmlearn not installed — cannot fit HMM for %s",
                symbol,
            )
            return False

        try:
            # Replace any NaN/inf with 0 (defensive — TA indicators can
            # produce NaN during warmup periods).
            mat = np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0)

            model = GaussianHMM(
                n_components=N_STATES,
                covariance_type="diag",  # diagonal cov — fast + robust on 9 features
                n_iter=100,
                random_state=42,
                verbose=False,
            )
            model.fit(mat)

            # Build the 5→3 state mapping based on per-state mean log_return
            # (feature index 0).
            state_means_lr = model.means_[:, 0]  # (N_STATES,)
            lr_std = float(np.std(mat[:, 0])) if mat.shape[0] > 1 else 1.0
            if lr_std < 1e-8:
                lr_std = 1e-8  # avoid div-by-zero in flat markets
            threshold = STATE_MAP_THRESHOLD_STDS * lr_std

            state_map = {}  # 5-state index → "UP" / "DOWN" / "FLAT"
            for i in range(N_STATES):
                if state_means_lr[i] > threshold:
                    state_map[i] = "UP"
                elif state_means_lr[i] < -threshold:
                    state_map[i] = "DOWN"
                else:
                    state_map[i] = "FLAT"

            # Build the 3x3 aggregate transition matrix
            T5 = model.transmat_  # (N_STATES, N_STATES)
            T3 = _aggregate_transition(T5, state_map)

            fitted = _FittedModel(
                model=model,
                state_map=state_map,
                T3=T3,
            )

            with self._global_lock:
                self._models[symbol] = fitted

            return True

        except Exception as exc:
            logger.warning(
                "hmm.fit: %s — fit failed (%s: %s), model unchanged",
                symbol, type(exc).__name__, exc,
            )
            return False

    # ─── Emit ────────────────────────────────────────────────────────────

    def emit_markov(
        self,
        symbol: str,
    ) -> tuple[dict[str, dict[str, float]] | None, str]:
        """Emit the 3-state transition matrix + current state for the EV engine.

        This is a drop-in replacement for the old MarkovChain.get_transition_matrix()
        + .current_state contract.

        Returns:
            (transition_matrix, current_state) where:
              - transition_matrix: 3x3 dict-of-dicts keyed by {UP, DOWN, FLAT}
                (or None if HMM not fitted for this symbol)
              - current_state: "UP" / "DOWN" / "FLAT" (or "FLAT" if not fitted)

        The EV engine's compute_p_markov_hold_n() reads T^N[current][target]
        from this matrix. The orchestrator reads p_markov_single_step from
        T[current][target] where target depends on direction.
        """
        with self._global_lock:
            fitted = self._models.get(symbol)

        if fitted is None:
            return None, "FLAT"

        # Predict the current state from the latest feature vector
        latest = self._buffer.latest(symbol)
        if latest is None:
            return None, "FLAT"

        try:
            latest_clean = np.nan_to_num(latest, nan=0.0, posinf=0.0, neginf=0.0)
            # hmmlearn predict expects 2D: (1, n_features)
            state_5 = fitted.model.predict(latest_clean.reshape(1, -1))[0]
            current_state = fitted.state_map.get(state_5, "FLAT")
        except Exception as exc:
            logger.warning(
                "hmm.emit: %s — predict failed (%s: %s), using FLAT",
                symbol, type(exc).__name__, exc,
            )
            current_state = "FLAT"

        return fitted.T3, current_state

    # ─── Persistence ────────────────────────────────────────────────────

    def save(self, path: str = MODEL_PATH) -> None:
        """Persist all fitted models to disk (crash recovery only)."""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._global_lock:
                data = {
                    sym: {
                        "means_": f.model.means_,
                        "covars_": f.model.covars_,
                        "transmat_": f.model.transmat_,
                        "startprob_": f.model.startprob_,
                        "state_map": f.state_map,
                        "T3": f.T3,
                    }
                    for sym, f in self._models.items()
                }
            with open(p, "wb") as fh:
                pickle.dump(data, fh)
            logger.info("hmm.save: persisted %d models to %s", len(data), path)
        except Exception as exc:
            logger.warning("hmm.save: failed (%s: %s)", type(exc).__name__, exc)

    def load(self, path: str = MODEL_PATH) -> int:
        """Load fitted models from disk. Returns number of models loaded."""
        try:
            p = Path(path)
            if not p.exists():
                return 0
            from hmmlearn.hmm import GaussianHMM

            with open(p, "rb") as fh:
                data = pickle.load(fh)

            loaded = 0
            for sym, d in data.items():
                model = GaussianHMM(
                    n_components=N_STATES,
                    covariance_type="diag",
                    n_iter=100,
                    random_state=42,
                )
                model.means_ = d["means_"]
                model.covars_ = d["covars_"]
                model.transmat_ = d["transmat_"]
                model.startprob_ = d["startprob_"]
                fitted = _FittedModel(
                    model=model,
                    state_map=d["state_map"],
                    T3=d["T3"],
                )
                with self._global_lock:
                    self._models[sym] = fitted
                loaded += 1

            logger.info("hmm.load: loaded %d models from %s", loaded, path)
            return loaded
        except Exception as exc:
            logger.warning("hmm.load: failed (%s: %s)", type(exc).__name__, exc)
            return 0

    # ─── Diagnostics ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return a diagnostics dict for admin/health endpoints."""
        with self._global_lock:
            return {
                "n_models": len(self._models),
                "symbols": list(self._models.keys()),
                "buffer_symbols": self._buffer.symbols(),
                "n_states": N_STATES,
                "n_features": N_FEATURES,
            }


# ─── Internal dataclass ──────────────────────────────────────────────────────


@dataclass
class _FittedModel:
    """Container for a fitted HMM + its 5→3 state mapping + 3x3 T matrix."""

    model: Any  # hmmlearn.hmm.GaussianHMM
    state_map: dict[int, str]  # 5-state index → "UP"/"DOWN"/"FLAT"
    T3: dict[str, dict[str, float]]  # 3x3 transition matrix


# ─── TA indicator computation (local, for bootstrap) ────────────────────────


def _compute_log_returns(closes: np.ndarray) -> np.ndarray:
    """ln(close[i] / close[i-1]) — returns array of len N-1."""
    returns = np.diff(np.log(closes[closes > 0]))
    return returns


def _compute_ta_indicators_local(closes: np.ndarray) -> dict[str, np.ndarray]:
    """Compute 5 TA indicators locally from close prices.

    Used during bootstrap only — live TA data comes from /api/ta/indicators.

    All indicators are computed on the N-1 log-return-aligned series (the
    first bar is dropped since it has no log_return). Returns arrays of
    length N-1 (or shorter if the indicator has a warmup period — padded
    with 0.0 to length N-1).
    """
    n = len(closes)
    if n < 2:
        return {
            "adx": np.array([]),
            "ao": np.array([]),
            "bb_power": np.array([]),
            "macd": np.array([]),
            "rsi": np.array([]),
        }

    # All indicators computed on the full close series, then truncated to
    # N-1 (matching log_returns length).
    rsi = _compute_rsi(closes, period=14)
    macd_line = _compute_macd(closes)
    ao = _compute_ao(closes)
    bb_power = _compute_bb_power(closes, period=20)
    adx = _compute_adx_close_only(closes, period=14)

    # Truncate to N-1 (drop first element to align with log_returns)
    def _truncate(arr: np.ndarray) -> np.ndarray:
        if len(arr) >= n - 1:
            return arr[-(n - 1):]
        # Pad with zeros at the start if shorter (warmup period)
        pad = np.zeros(n - 1 - len(arr))
        return np.concatenate([pad, arr])

    return {
        "adx": _truncate(adx),
        "ao": _truncate(ao),
        "bb_power": _truncate(bb_power),
        "macd": _truncate(macd_line),
        "rsi": _truncate(rsi),
    }


def _compute_rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI via Wilder's smoothing."""
    if len(closes) < period + 1:
        return np.array([])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Wilder's smoothing
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    rsi_vals = np.zeros(len(deltas))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_vals[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_vals[i] = 100.0 - (100.0 / (1.0 + rs))

    # First `period` values are 0 (warmup)
    return rsi_vals


def _compute_macd(closes: np.ndarray) -> np.ndarray:
    """MACD line = EMA12 - EMA26."""
    if len(closes) < 26:
        return np.array([])

    def _ema(data: np.ndarray, span: int) -> np.ndarray:
        alpha = 2.0 / (span + 1)
        out = np.zeros(len(data))
        out[0] = data[0]
        for i in range(1, len(data)):
            out[i] = alpha * data[i] + (1 - alpha) * out[i - 1]
        return out

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = ema12 - ema26
    return macd


def _compute_ao(closes: np.ndarray) -> np.ndarray:
    """Awesome Oscillator = SMA5 - SMA34 (on median price, approximated with close)."""
    if len(closes) < 34:
        return np.array([])

    def _sma(data: np.ndarray, period: int) -> np.ndarray:
        out = np.zeros(len(data))
        for i in range(period - 1, len(data)):
            out[i] = np.mean(data[i - period + 1 : i + 1])
        return out

    sma5 = _sma(closes, 5)
    sma34 = _sma(closes, 34)
    return sma5 - sma34


def _compute_bb_power(closes: np.ndarray, period: int = 20) -> np.ndarray:
    """Bollinger Band Power = (close - SMA) / (2 * stddev).

    Values > 1 → price above upper band; < -1 → below lower band.
    """
    if len(closes) < period:
        return np.array([])

    out = np.zeros(len(closes))
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        sma = np.mean(window)
        std = np.std(window, ddof=0)
        if std > 1e-8:
            out[i] = (closes[i] - sma) / (2 * std)
    return out


def _compute_adx_close_only(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """ADX approximated from close-only data (no high/low available).

    Uses close-to-close range as a proxy for true range. This is a rough
    approximation — live /api/ta data will replace these values as the
    buffer rolls.
    """
    if len(closes) < period * 2:
        return np.array([])

    # Approximate true range as |close[i] - close[i-1]|
    tr = np.abs(np.diff(closes))
    tr = np.concatenate([[0], tr])  # align with closes

    # Directional movement (close-only approximation)
    dm_plus = np.where(np.diff(closes) > 0, np.diff(closes), 0.0)
    dm_minus = np.where(np.diff(closes) < 0, -np.diff(closes), 0.0)
    dm_plus = np.concatenate([[0], dm_plus])
    dm_minus = np.concatenate([[0], dm_minus])

    # Wilder's smoothing
    atr = np.zeros(len(closes))
    tr_ema = np.mean(tr[1 : period + 1])
    atr[period] = tr_ema
    for i in range(period + 1, len(closes)):
        tr_ema = (tr_ema * (period - 1) + tr[i]) / period
        atr[i] = tr_ema

    plus_di = np.zeros(len(closes))
    minus_di = np.zeros(len(closes))
    dm_plus_ema = np.mean(dm_plus[1 : period + 1])
    dm_minus_ema = np.mean(dm_minus[1 : period + 1])
    for i in range(period, len(closes)):
        if i > period:
            dm_plus_ema = (dm_plus_ema * (period - 1) + dm_plus[i]) / period
            dm_minus_ema = (dm_minus_ema * (period - 1) + dm_minus[i]) / period
        if atr[i] > 0:
            plus_di[i] = 100 * dm_plus_ema / atr[i]
            minus_di[i] = 100 * dm_minus_ema / atr[i]

    dx = np.zeros(len(closes))
    for i in range(period * 2, len(closes)):
        di_sum = plus_di[i] + minus_di[i]
        if di_sum > 0:
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    # ADX = Wilder's smoothing of DX
    adx = np.zeros(len(closes))
    if len(dx) > period * 2:
        adx_start = period * 2
        adx_ema = np.mean(dx[adx_start : adx_start + period])
        adx[adx_start + period - 1] = adx_ema
        for i in range(adx_start + period, len(closes)):
            adx_ema = (adx_ema * (period - 1) + dx[i]) / period
            adx[i] = adx_ema

    return adx


# ─── 5→3 state aggregation ──────────────────────────────────────────────────


def _aggregate_transition(
    T5: np.ndarray,
    state_map: dict[int, str],
) -> dict[str, dict[str, float]]:
    """Aggregate the 5x5 HMM transition matrix into a 3x3 {UP,DOWN,FLAT} matrix.

    For each pair (5-state i, 5-state j), T5[i][j] is added to
    T3[state_map[i]][state_map[j]]. Rows are then renormalized to sum to 1.
    """
    # Initialize 3x3 with zeros
    T3: dict[str, dict[str, float]] = {s: {t: 0.0 for t in STATES_3} for s in STATES_3}

    for i in range(N_STATES):
        from_3 = state_map.get(i, "FLAT")
        for j in range(N_STATES):
            to_3 = state_map.get(j, "FLAT")
            T3[from_3][to_3] += float(T5[i][j])

    # Renormalize rows
    for s in STATES_3:
        row_sum = sum(T3[s].values())
        if row_sum > 0:
            for t in STATES_3:
                T3[s][t] = round(T3[s][t] / row_sum, 6)
        else:
            # Uniform prior if row is all zeros (no 5-state mapped to this 3-state)
            for t in STATES_3:
                T3[s][t] = 1.0 / 3.0

    return T3
