"""
Regime Detection Engine — Hidden Markov Model via hmmlearn.
Detects volatility (Low / Medium-Low / Medium-High / High) and
trend (Strong-Bear / Bear / Bull / Strong-Bull) regimes.

Upgraded from 3 → 4 states per dimension for finer-grained risk segmentation.
The 4×4 grid yields 16 distinct regime labels, allowing the position sizer
to react more precisely to transitional market conditions.
"""

from __future__ import annotations
import numpy as np
from hmmlearn import hmm
from dataclasses import dataclass
from typing import Optional
import warnings
warnings.filterwarnings("ignore")


# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class RegimeSnapshot:
    """Point-in-time regime state for one symbol."""
    vol_probs:   list[float]   # [P(low), P(med_low), P(med_high), P(high)]
    trend_probs: list[float]   # [P(strong_bear), P(bear), P(bull), P(strong_bull)]
    vol_state:   str           # dominant volatility label
    trend_state: str           # dominant trend label
    regime_label: str          # combined label e.g. "low_vol_bull"
    confidence:  float         # max(vol) * max(trend)
    n_bars_fitted: int

    @property
    def risk_multiplier(self) -> float:
        """
        Risk scaling factor [0.10 – 1.75] across the regime grid.
        Supports 2, 3, and 4-state labels via centralized scale maps.

        Vol scale:
          low       → 1.50 (2-state) / 1.75 (4-state)
          med       → 1.00 (3-state)
          med_low   → 1.20 (4-state)
          med_high  → 0.70 (4-state)
          high      → 0.40 (2-state) / 0.35 (4-state)

        Trend scale:
          strong_bull → 1.00 (4-state)
          bull        → 0.88 (4-state) / 1.00 (2/3-state)
          neutral     → 0.80 (3-state)
          bear        → 0.50 (2-state) / 0.60 (4-state)
          strong_bear → 0.40 (4-state)

        Combined = clip(vol_scale × trend_scale, 0.10, 1.75)
        """
        vol_scale = RegimeHMM.VOL_RISK_SCALE.get(self.vol_state, 1.0)
        trend_scale = RegimeHMM.TREND_RISK_SCALE.get(self.trend_state, 1.0)
        return round(max(0.10, min(1.75, vol_scale * trend_scale)), 4)


# ─── HMM Wrapper ─────────────────────────────────────────────────────────────

class RegimeHMM:
    """
    Dual HMM: separate n-state Gaussian HMMs for volatility and trend regimes.

    Supports 2, 3, or 4 states per dimension.

    States per dimension (4-state default)
    ─────────────────────
    Volatility  : low | med_low | med_high | high
    Trend       : strong_bear | bear | bull | strong_bull

    Both models use posterior state probabilities (forward-backward algorithm),
    not just Viterbi hard assignments, so confidence scores are smooth and
    differentiable. States are auto-labelled by sorting HMM component means —
    no manual mapping required.
    """

    # Centralized label maps for n-state support
    VOL_LABELS_BY_N: dict[int, list[str]] = {
        2: ["low", "high"],
        3: ["low", "med", "high"],
        4: ["low", "med_low", "med_high", "high"],
    }
    TREND_LABELS_BY_N: dict[int, list[str]] = {
        2: ["bear", "bull"],
        3: ["bear", "neutral", "bull"],
        4: ["strong_bear", "bear", "bull", "strong_bull"],
    }

    # Backward-compatible class-level defaults (4-state)
    VOL_LABELS   = ["low", "med_low", "med_high", "high"]
    TREND_LABELS = ["strong_bear", "bear", "bull", "strong_bull"]

    # Risk scale maps for all n-state variants
    VOL_RISK_SCALE: dict[str, float] = {
        # 2-state
        "low": 1.50, "high": 0.40,
        # 3-state
        "med": 1.00,
        # 4-state
        "med_low": 1.20, "med_high": 0.70,
    }
    TREND_RISK_SCALE: dict[str, float] = {
        # 2-state
        "bear": 0.50, "bull": 1.00,
        # 3-state
        "neutral": 0.80,
        # 4-state
        "strong_bear": 0.40, "strong_bull": 1.00,
    }

    def __init__(
        self,
        n_components: int = 4,
        n_iter: int = 100,
        covariance_type: str = "diag",
        vol_window_long: int = 20,
        vol_window_short: int = 5,
        trend_window_short: int = 10,
        trend_window_long: int = 30,
        random_state: int = 42,
    ):
        if n_components not in (2, 3, 4):
            raise ValueError(f"n_components must be 2, 3, or 4. Got {n_components}.")
        self.n_components    = n_components
        self.n_iter          = n_iter
        self.covariance_type = covariance_type
        self._vol_model:    Optional[hmm.GaussianHMM] = None
        self._trend_model:  Optional[hmm.GaussianHMM] = None
        self._vol_label_map:   dict[int, str] = {}
        self._trend_label_map: dict[int, str] = {}
        self.fitted = False

        # Active label sets for current n_components
        self._vol_labels   = self.VOL_LABELS_BY_N[n_components]
        self._trend_labels = self.TREND_LABELS_BY_N[n_components]

        # Configurable feature engineering windows
        self.vol_window_long  = vol_window_long
        self.vol_window_short = vol_window_short
        self.trend_window_short = trend_window_short
        self.trend_window_long  = trend_window_long
        self.random_state = random_state

    @classmethod
    async def from_runtime_config(cls, **overrides) -> "RegimeHMM":
        """Build a RegimeHMM with values from the runtime configuration system."""
        from regime_platform.core.runtime_config import runtime_config

        defaults = {"n_components": 4, "n_iter": 100, "covariance_type": "diag",
                     "vol_window_long": 20, "vol_window_short": 5,
                     "trend_window_short": 10, "trend_window_long": 30,
                     "random_state": 42}

        key_map = {
            "n_iter": ("regime.hmm_n_iter", "get_int"),
            "vol_window_long": ("regime.vol_window_long", "get_int"),
            "vol_window_short": ("regime.vol_window_short", "get_int"),
            "trend_window_short": ("regime.trend_window_short", "get_int"),
            "trend_window_long": ("regime.trend_window_long", "get_int"),
            "random_state": ("regime.hmm_random_state", "get_int"),
        }

        kwargs = dict(defaults)
        for field_name, (config_key, getter_name) in key_map.items():
            if field_name in overrides:
                kwargs[field_name] = overrides[field_name]
                continue
            getter = getattr(runtime_config, getter_name)
            kwargs[field_name] = await getter(config_key, default=defaults[field_name])

        # Pass through any overrides for non-configurable params
        for k in ("n_components", "covariance_type"):
            if k in overrides:
                kwargs[k] = overrides[k]

        return cls(**kwargs)

    # ── Fitting ──────────────────────────────────────────────────────────────

    def fit(self, prices: np.ndarray) -> "RegimeHMM":
        """
        Fit both HMMs on a price series (1-D numpy array).
        Requires at least 4 × n_components bars for stable EM convergence
        (minimum 80 at the default n_components=4; enforced at 80).
        """
        min_bars = max(50, 4 * self.n_components * 5)   # 80 for n=4
        if len(prices) < min_bars:
            raise ValueError(
                f"Need at least {min_bars} price bars for {self.n_components}-state HMM. "
                f"Got {len(prices)}."
            )

        returns        = np.diff(np.log(prices))
        vol_features   = self._vol_features(returns, self.vol_window_long, self.vol_window_short)
        trend_features = self._trend_features(returns, self.trend_window_short, self.trend_window_long)

        self._vol_model   = self._build_hmm()
        self._trend_model = self._build_hmm()

        self._vol_model.fit(vol_features)
        self._trend_model.fit(trend_features)

        # Sanitize degenerate parameters before predict() is called
        self._sanitize_model(self._vol_model)
        self._sanitize_model(self._trend_model)

        self._vol_label_map   = self._label_states(self._vol_model, self._vol_labels)
        self._trend_label_map = self._label_states(self._trend_model, self._trend_labels)
        self.fitted = True
        return self

    # ── Inference ────────────────────────────────────────────────────────────

    def predict(self, prices: np.ndarray) -> RegimeSnapshot:
        """Return current regime snapshot from the most recent price window."""
        if not self.fitted:
            raise RuntimeError("Call .fit() before .predict()")

        returns        = np.diff(np.log(prices))
        vol_features   = self._vol_features(returns, self.vol_window_long, self.vol_window_short)
        trend_features = self._trend_features(returns, self.trend_window_short, self.trend_window_long)

        vol_posteriors   = self._vol_model.predict_proba(vol_features)[-1]
        trend_posteriors = self._trend_model.predict_proba(trend_features)[-1]

        vol_probs   = self._reorder(vol_posteriors,   self._vol_label_map,   self._vol_labels)
        trend_probs = self._reorder(trend_posteriors, self._trend_label_map, self._trend_labels)

        # NaN fallback: if posteriors are degenerate (all NaN), use uniform
        if not np.all(np.isfinite(vol_probs)):
            n = len(self._vol_labels)
            vol_probs = [1.0 / n] * n
        if not np.all(np.isfinite(trend_probs)):
            n = len(self._trend_labels)
            trend_probs = [1.0 / n] * n

        # Re-normalise (floating point drift)
        vs = sum(vol_probs)
        if vs > 0:
            vol_probs = [p / vs for p in vol_probs]
        ts = sum(trend_probs)
        if ts > 0:
            trend_probs = [p / ts for p in trend_probs]

        vol_state   = self._vol_labels[int(np.argmax(vol_probs))]
        trend_state = self._trend_labels[int(np.argmax(trend_probs))]

        return RegimeSnapshot(
            vol_probs=vol_probs,
            trend_probs=trend_probs,
            vol_state=vol_state,
            trend_state=trend_state,
            regime_label=f"{vol_state}_vol_{trend_state}",
            confidence=round(float(np.max(vol_probs) * np.max(trend_probs)), 4),
            n_bars_fitted=len(prices),
        )

    # ── Feature Engineering ──────────────────────────────────────────────────

    @staticmethod
    def _vol_features(returns: np.ndarray, window_long: int = 20, window_short: int = 5) -> np.ndarray:
        rv_long  = np.array([
            np.std(returns[max(0, i - window_long):i + 1])
            for i in range(len(returns))
        ])
        rv_short = np.array([
            np.std(returns[max(0, i - window_short):i + 1])
            for i in range(len(returns))
        ])
        return np.column_stack([rv_long, rv_short, np.abs(returns)])

    @staticmethod
    def _trend_features(returns: np.ndarray, window_short: int = 10, window_long: int = 30) -> np.ndarray:
        cum_short = np.array([
            np.sum(returns[max(0, i - window_short):i + 1])
            for i in range(len(returns))
        ])
        cum_long = np.array([
            np.sum(returns[max(0, i - window_long):i + 1])
            for i in range(len(returns))
        ])
        return np.column_stack([returns, cum_short, cum_long])

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_hmm(self) -> hmm.GaussianHMM:
        return hmm.GaussianHMM(
            n_components=self.n_components,
            covariance_type=self.covariance_type,
            n_iter=self.n_iter,
            random_state=self.random_state,
        )

    @staticmethod
    def _sanitize_model(model: hmm.GaussianHMM) -> None:
        """
        Fix degenerate HMM parameters that cause predict_proba() to return NaN.

        When EM collapses some states (all observations assigned to a few states),
        the other states get zero rows in transmat_ and NaN/zero in startprob_.
        This is common with strongly-structured synthetic data or short windows.

        Fixes applied in-place:
        - NaN/zero startprob_ → uniform
        - NaN/zero transmat_ rows → uniform rows
        - NaN means_ → zero
        - NaN/zero covars_ → identity-like (1e-3 diagonal)
        """
        n = model.n_components
        # startprob_
        sp = model.startprob_
        if not np.all(np.isfinite(sp)) or sp.sum() < 1e-8:
            model.startprob_ = np.full(n, 1.0 / n)
        else:
            model.startprob_ = sp / sp.sum()

        # transmat_
        for i in range(n):
            row = model.transmat_[i]
            if not np.all(np.isfinite(row)) or row.sum() < 1e-8:
                model.transmat_[i] = np.full(n, 1.0 / n)
            else:
                model.transmat_[i] = row / row.sum()

        # means_
        if not np.all(np.isfinite(model.means_)):
            model.means_ = np.where(np.isfinite(model.means_), model.means_, 0.0)

        # covars_ — write to private _covars_ to bypass shape-validation setter
        # Shape: "diag" → (n_components, n_features), "full" → (n, nf, nf)
        if hasattr(model, "_covars_") and model._covars_ is not None:
            bad = ~np.isfinite(model._covars_) | (model._covars_ <= 0)
            if bad.any():
                model._covars_ = np.where(bad, 1e-3, model._covars_)

    @staticmethod
    def _label_states(model: hmm.GaussianHMM, labels: list[str]) -> dict[int, str]:
        """
        Sort HMM component means to auto-label states.
        Uses feature 0 for vol (ascending → low to high),
        feature 2 for trend (ascending → bear to bull).
        Works for any n_components (2, 3, or 4).
        """
        n = min(model.n_components, len(labels))
        feature_idx = 2 if len(labels) > 2 and labels[0] in ("strong_bear", "bear", "neutral") else 0
        means  = model.means_[:, feature_idx]
        order  = np.argsort(means)
        return {int(order[i]): labels[i] for i in range(n)}

    @staticmethod
    def _reorder(
        posteriors:    np.ndarray,
        label_map:     dict[int, str],
        target_labels: list[str],
    ) -> list[float]:
        """Re-order raw HMM posteriors into a fixed semantic order."""
        prob_map = {label_map[i]: float(posteriors[i]) for i in range(len(posteriors))}
        return [prob_map[lbl] for lbl in target_labels]
