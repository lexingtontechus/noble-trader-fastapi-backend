"""
Regime Detection Engine - Hidden Markov Model via hmmlearn.
Detects volatility (Low / Medium-Low / Medium-High / High) and
trend (Strong-Bear / Bear / Bull / Strong-Bull) regimes.

Upgraded from 3 -> 4 states per dimension for finer-grained risk segmentation.
The 4×4 grid yields 16 distinct regime labels, allowing the position sizer
to react more precisely to transitional market conditions.

2026-08-23 stability fixes (worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md
R1 — AAPL/US500 root-cause deep-dive):

  1. ~~Data-relative ``min_covar`` floor (``_data_min_covar``)~~ — REMOVED
     2026-08-24, superseded by fix #3 below. Originally: a fresh
     ``RegimeHMM`` is fit from scratch every ~5-minute sweep tick (no
     persistence between fits). In a calm/low-volatility market,
     hmmlearn's EM can fit an artificially tight covariance to one
     component, and a tight covariance makes ``predict_proba``'s
     forward-backward posterior swing to near-0/near-1 for a negligible
     wiggle in the input — this is the direct cause of the OVERCONFIDENCE
     symptom (e.g. US500 pinned at regime_conf=1.000 for hours). The fix
     floored ``min_covar`` at a fraction of the fitting window's own
     feature std — but that computed ONE value from the LARGEST-std
     feature column and applied it uniformly across all columns, which
     were never on the same scale to begin with. Non-binding for the
     smaller-scale columns, it let EM shrink their variance further than
     intended — reproducing the same overconfidence on a different
     dimension. Standardizing every feature to std=1 (fix #3) makes this
     entire per-window computation unnecessary — a single fixed floor
     works identically well for every dimension once they're all on the
     same scale, which is a strictly better fix, not a parallel one; see
     ``_STANDARDIZED_MIN_COVAR``.
  2. Label continuity across refits (``_label_states``' ``previous_means``
     param): because every fit is independent, ``_label_states`` used to
     re-sort that fit's own component means and assign labels by rank
     order every single time, with no memory of the previous fit's
     mapping. When two components' means are nearly tied — exactly the
     calm-market condition above — a tiny window shift can swap their
     rank order and silently relabel e.g. "bull" as "strong_bull" between
     two 5-minutes-apart ticks (confirmed live for AAPL). Fix: match each
     newly-fit component to whichever *previous* fit's labeled mean it's
     closest to, falling back to fresh rank-sort only when no previous
     mapping exists (a symbol's first fit). Callers (orchestrator.py) are
     responsible for persisting ``trend_label_means``/``vol_label_means``
     per symbol between ticks and passing them into the next ``fit()``.
     Still live, unaffected by fix #3 (means_ are converted back to
     original units immediately after each fit — see
     ``_destandardize_means`` — so this comparison keeps working on the
     same units it always has).

2026-08-24 fix #3 — feature standardization (root-cause fix for
"Model is not converging", not just the overconfidence symptom fix #1
targeted). Confirmed live: the trend HMM logged "did not converge cleanly"
on ~99% of ticks for 13/14 symbols. Root-cause research (real-data tested,
not theoretical) found the cause was unscaled features — e.g. trend model
returns std ~0.0004 vs cum_long std ~0.003 (~8x spread) sharing one
min_covar floor and one EM optimization with no per-dimension scale
awareness. Z-scoring ``vol_features``/``trend_features`` (per fitting
window, per column) before fitting resolved convergence in every real
symbol tested (EURUSD, XAUUSD, BTCUSD, AAPL) — dropping collinear features
and multi-restart fitting were tried first and did NOT reliably fix it
(see ``scripts/diagnose_hmm_convergence.py``). Implementation:
``_fit_scale``/``_standardize`` compute and apply per-fit (mu, sigma);
``_destandardize_means`` converts ``means_`` back to original units
immediately after fitting so every downstream consumer (label assignment,
continuity matching, ``vol_label_means``/``trend_label_means``) is
unaffected and unchanged.

All three fixes stabilize ``RegimeSnapshot.regime_label``/``confidence``
directly (not just ``p_regime``), which also reduces spurious
``regime_shift`` detections (Layer 2) caused by the same relabeling
artifact.
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
    # R4 (2026-08-23, worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md):
    # whether the HMM fit that produced this snapshot's trend/vol posteriors
    # converged cleanly (see RegimeHMM._fit_converged). Default True so every
    # existing call site that constructs a RegimeSnapshot directly (not via
    # RegimeHMM.predict()) — gpu_engine.py, simulator.py, heavy_sweep.py's
    # neutral fallback, tests — keeps working unchanged; they don't have a
    # real hmmlearn monitor_ to check, so "assume converged" (fail open,
    # same posture as p_timesfm=None not blocking trading) is the safe
    # default rather than fail closed.
    trend_converged: bool = True
    vol_converged: bool = True

    @property
    def risk_multiplier(self) -> float:
        """
        Risk scaling factor [0.10 – 1.75] across the regime grid.
        Supports 2, 3, and 4-state labels via centralized scale maps.

        Vol scale:
          low       -> 1.50 (2-state) / 1.75 (4-state)
          med       -> 1.00 (3-state)
          med_low   -> 1.20 (4-state)
          med_high  -> 0.70 (4-state)
          high      -> 0.40 (2-state) / 0.35 (4-state)

        Trend scale:
          strong_bull -> 1.00 (4-state)
          bull        -> 0.88 (4-state) / 1.00 (2/3-state)
          neutral     -> 0.80 (3-state)
          bear        -> 0.50 (2-state) / 0.60 (4-state)
          strong_bear -> 0.40 (4-state)

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
    differentiable. States are auto-labelled by sorting HMM component means -
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
        # Per-fit standardization stats (mu, sigma), set by fit(), consumed
        # by predict() to standardize query features identically — see
        # module docstring fix #3.
        self._vol_scale:   Optional[tuple[np.ndarray, np.ndarray]] = None
        self._trend_scale: Optional[tuple[np.ndarray, np.ndarray]] = None
        # Real Viterbi-occupancy share per trend component over the fit
        # window (2026-08-28 root-cause classifier — see
        # _classify_trend_components). {component_idx: share in [0,1]}.
        self._trend_occupancy: dict[int, float] = {}
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

    def fit(
        self,
        prices: np.ndarray,
        prev_vol_means: Optional[dict[str, float]] = None,
        prev_trend_means: Optional[dict[str, float]] = None,
    ) -> "RegimeHMM":
        """
        Fit both HMMs on a price series (1-D numpy array).
        Requires at least 4 × n_components bars for stable EM convergence
        (minimum 80 at the default n_components=4; enforced at 80).

        Args:
            prices: 1-D price series.
            prev_vol_means: Optional ``{label: mean}`` from the PREVIOUS
                fit's volatility model (this symbol's ``vol_label_means``
                after its last ``fit()``). When given, new components are
                matched to the closest previous label instead of being
                re-sorted from scratch — see the module docstring's R1
                fix #2. ``None`` (default) preserves the original
                rank-sort behavior (a symbol's first fit).
            prev_trend_means: Same, for the trend model.
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

        # Standardize before fitting (fix #3, see module docstring). Store
        # this fit's (mu, sigma) so predict() can standardize query features
        # identically, and so means_ can be converted back to original units
        # immediately after fitting (below) — everything downstream of that
        # point (_label_states, vol_label_means/trend_label_means, cross-tick
        # continuity matching) keeps operating on original-unit values,
        # unchanged.
        self._vol_scale   = self._fit_scale(vol_features)
        self._trend_scale = self._fit_scale(trend_features)
        vol_features_std   = self._standardize(vol_features, self._vol_scale)
        trend_features_std = self._standardize(trend_features, self._trend_scale)

        self._vol_model   = self._build_hmm()
        self._trend_model = self._build_hmm()

        self._vol_model.fit(vol_features_std)
        self._trend_model.fit(trend_features_std)

        # Convert means_ back to original units now, before anything else
        # reads them.
        self._destandardize_means(self._vol_model, self._vol_scale)
        self._destandardize_means(self._trend_model, self._trend_scale)

        # Sanitize degenerate parameters before predict() is called
        self._sanitize_model(self._vol_model)
        self._sanitize_model(self._trend_model)

        # Real per-component occupancy over this fit window (2026-08-28
        # root-cause classifier) — how many bars the Viterbi path
        # actually assigns to each component, not just its fitted mean.
        # A component can be numerically valid (no NaN, nothing
        # _sanitize_model would touch) yet fit on almost no real data —
        # confirmed live: AAPL's trend model assigned 199 of 199 bars to
        # a single component, leaving the other 3 components' "means"
        # as artifacts of near-zero effective weight. Computed once here
        # (cheap — reuses the already-fit model and already-standardized
        # features) and cached for _classify_trend_components() to use
        # on every call, rather than re-decoding each time.
        try:
            trend_viterbi = self._trend_model.predict(trend_features_std)
            n_bars = len(trend_viterbi)
            self._trend_occupancy = {
                i: float(np.sum(trend_viterbi == i)) / n_bars if n_bars else 0.0
                for i in range(self._trend_model.n_components)
            }
        except Exception:
            self._trend_occupancy = {}

        self._vol_label_map   = self._label_states(self._vol_model, self._vol_labels, prev_vol_means)
        self._trend_label_map = self._label_states(self._trend_model, self._trend_labels, prev_trend_means)
        self.fitted = True
        return self

    # ── Label-mean introspection (for cross-tick label continuity) ────────
    # Callers persist these per-symbol and pass them back into the NEXT
    # fit() call as prev_vol_means/prev_trend_means (R1 fix #2).

    @property
    def vol_label_means(self) -> dict[str, float]:
        """``{label: fitted component mean}`` for the volatility model."""
        if not self.fitted or self._vol_model is None:
            return {}
        means = self._vol_model.means_[:, 0]
        return {lbl: float(means[i]) for i, lbl in self._vol_label_map.items()}

    @property
    def trend_label_means(self) -> dict[str, float]:
        """``{label: fitted component mean}`` for the trend model."""
        if not self.fitted or self._trend_model is None:
            return {}
        feature_idx = 2 if len(self._trend_labels) > 2 else 0
        means = self._trend_model.means_[:, feature_idx]
        return {lbl: float(means[i]) for i, lbl in self._trend_label_map.items()}

    # ── Inference ────────────────────────────────────────────────────────────

    def predict(self, prices: np.ndarray) -> RegimeSnapshot:
        """Return current regime snapshot from the most recent price window."""
        if not self.fitted:
            raise RuntimeError("Call .fit() before .predict()")

        returns        = np.diff(np.log(prices))
        vol_features   = self._vol_features(returns, self.vol_window_long, self.vol_window_short)
        trend_features = self._trend_features(returns, self.trend_window_short, self.trend_window_long)

        # Standardize with THIS instance's fit-time (mu, sigma) — the model
        # was fit on standardized features (fix #3), so query features must
        # go through the identical transform.
        vol_features_std   = self._standardize(vol_features, self._vol_scale)
        trend_features_std = self._standardize(trend_features, self._trend_scale)

        vol_posteriors   = self._vol_model.predict_proba(vol_features_std)[-1]
        trend_posteriors = self._trend_model.predict_proba(trend_features_std)[-1]

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
            trend_converged=self._fit_converged(self._trend_model),
            vol_converged=self._fit_converged(self._vol_model),
        )

    # ── 3-state transition matrix derivation (Part 1 Change B, 2026-08-25) ──
    # Derives a 3x3 {UP, DOWN, FLAT} transition matrix + current_state from
    # the ALREADY-FITTED trend model above — replacing
    # regime_platform/services/hmm/model.py's separate 5-state HmmModel
    # (fit on 9 unscaled features; confirmed live 2026-08-25 to produce
    # "startprob_ must sum to 1 (got nan)" and "state collapse detected"
    # warnings across most symbols — the same numerical-instability class
    # Change A already root-caused and fixed here via feature
    # standardization). No new fitting, no new instability: this reuses
    # the trend model's own transmat_ (already sanitized/finite via
    # ``_sanitize_model``) and label map.

    # {strong_bull, bull} -> UP, {strong_bear, bear} -> DOWN. The trend
    # model has no neutral state, so FLAT is never assigned a component —
    # its row in T3 falls through to the uniform-prior handling below
    # (mirrors _aggregate_transition's existing "no state maps to this
    # label" fallback in the legacy model), and current_state will
    # therefore only ever resolve to UP or DOWN in practice. Also covers
    # the 2/3-state label sets ("bear"/"bull", "bear"/"neutral"/"bull") —
    # "neutral" and any unmapped label fall through to FLAT the same way.
    _TREND_TO_3STATE: dict[str, str] = {
        "strong_bull": "UP", "bull": "UP",
        "strong_bear": "DOWN", "bear": "DOWN",
    }

    def _classify_trend_components(self) -> dict[int, str]:
        """
        Root-cause classifier (2026-08-28, worklog
        20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md), replacing the
        two separate Tier 1/Tier 2 patches that used to gate the
        rank-assigned label with a single-feature magnitude check. That
        wasn't enough — real-data analysis across the live symbol
        universe found two distinct problems the old gate never caught:

          A. ``_label_states`` ranks/labels using ONLY feature index 2
             (the 30-bar cumulative return) — but each component was fit
             on THREE features (1-bar return, 10-bar cumulative, 30-bar
             cumulative) that are only weakly correlated with each other
             in real data (avg corr(feat0, feat2) = 0.185 across the
             live universe). A component's short-term behavior can
             flatly contradict the label its 30-bar figure implies. 9 of
             14 live symbols had at least one such internally-
             contradictory labeled component.
          B. Some fits are near-total single-component collapse (e.g.
             AAPL: 100% of the fit window's bars Viterbi-assigned to one
             component) — the other components' means are fit on
             ~0 real data and are numerically valid but meaningless,
             regardless of magnitude. ``_sanitize_model`` doesn't catch
             this (nothing is NaN or zero); it only catches hard
             numerical breakage.

        This classifies each trend component directly from the model's
        own fitted parameters — occupancy and all 3 raw feature means —
        instead of trusting the rank-assigned label at all. A component
        is "UP"/"DOWN" only if:
          1. Its real Viterbi occupancy (``self._trend_occupancy``,
             computed once at fit time) clears
             ``markov_min_occupancy`` (fixes B).
          2. All 3 of its fitted features agree in sign (fixes A).
          3. The consistent-direction magnitude (feature 2) clears
             ``markov_flat_gate_threshold_stds`` standard deviations of
             that feature's own fit-time noise scale.
        Otherwise: "FLAT". Any number of the 4 components (0 to 4) can
        end up FLAT in a given fit — this is what lets a genuinely flat
        market resolve toward genuine neutrality instead of the old
        scheme's forced, always-confident directional read.

        Returns ``{component_idx: "UP"|"DOWN"|"FLAT"}`` for every
        fitted trend component.
        """
        if not self.fitted or self._trend_model is None:
            raise RuntimeError(
                "Call .fit() before ._classify_trend_components()"
            )

        from regime_platform.core.feature_flags import (
            get_markov_flat_gate_threshold_stds,
            get_markov_min_occupancy,
        )

        min_occupancy = get_markov_min_occupancy()
        sigma = 1.0
        if self._trend_scale is not None:
            sigma = float(self._trend_scale[1][2]) or 1.0
        threshold = get_markov_flat_gate_threshold_stds() * sigma

        means = self._trend_model.means_  # (n, 3), original units
        n = self._trend_model.n_components
        result: dict[int, str] = {}
        for i in range(n):
            if self._trend_occupancy.get(i, 0.0) < min_occupancy:
                result[i] = "FLAT"
                continue
            signs = np.sign(means[i])
            if not (signs[0] == signs[1] == signs[2]) or signs[2] == 0:
                result[i] = "FLAT"
                continue
            mag = float(means[i, 2])
            if mag > threshold:
                result[i] = "UP"
            elif mag < -threshold:
                result[i] = "DOWN"
            else:
                result[i] = "FLAT"
        return result

    def trend_transition_matrix_3state(
        self, trend_state: str,
    ) -> tuple[dict[str, dict[str, float]], str]:
        """
        Derive a 3x3 {UP, DOWN, FLAT} transition matrix from the fitted
        trend HMM's own ``transmat_`` — a drop-in replacement for
        ``HmmModel.emit_markov()``'s return shape, for use as an
        alternative source of ``p_markov_hold_n`` (see
        ``settings.MARKOV_SOURCE_MODE``). Component classification is
        ``_classify_trend_components()`` (see its docstring) — direct
        from the model's own fitted parameters, not the rank-assigned
        label.

        Args:
            trend_state: The current tick's dominant trend label (e.g.
                ``regime.trend_state`` from this instance's own
                ``predict()`` call) — used only to look up which
                component produced it, so "current state" reflects that
                SAME component's real classification.

        Returns:
            (T3, current_state) — same shape as
            ``HmmModel.emit_markov()``.
        """
        if not self.fitted or self._trend_model is None:
            raise RuntimeError(
                "Call .fit() before .trend_transition_matrix_3state()"
            )

        component_to_3state = self._classify_trend_components()

        n = self._trend_model.n_components
        transmat = self._trend_model.transmat_
        states3 = ("UP", "DOWN", "FLAT")
        T3: dict[str, dict[str, float]] = {s: {t: 0.0 for t in states3} for s in states3}
        for i in range(n):
            from_3 = component_to_3state.get(i, "FLAT")
            for j in range(n):
                to_3 = component_to_3state.get(j, "FLAT")
                T3[from_3][to_3] += float(transmat[i][j])

        for s in states3:
            row_sum = sum(T3[s].values())
            if row_sum > 0:
                for t in states3:
                    T3[s][t] = round(T3[s][t] / row_sum, 6)
            else:
                # No component maps to this label (e.g. FLAT) — uniform
                # prior, same fallback the legacy model's
                # _aggregate_transition uses for an empty row.
                for t in states3:
                    T3[s][t] = 1.0 / 3.0

        # current_state: find the component that produced `trend_state`
        # and use ITS real classification, rather than mapping the label
        # directly — a direct label mapping would bypass the classifier
        # for the one reading that matters most (it's what feeds the EV
        # blend's current-row lookup).
        current_idx = next(
            (i for i, lbl in self._trend_label_map.items() if lbl == trend_state),
            None,
        )
        current_state = component_to_3state.get(current_idx, "FLAT") if current_idx is not None else "FLAT"
        return T3, current_state

    def gated_trend_probs(self, trend_probs: dict[str, float]) -> dict[str, float]:
        """
        Redistributes probability mass for any trend component that
        ``_classify_trend_components()`` (see its docstring for the
        root-cause classifier this and ``trend_transition_matrix_3state``
        share) doesn't classify as directional, into an explicit
        ``"neutral"`` key — the OTHER consumer of that classification,
        since ``trend_probs`` feeds ``p_regime`` directly into the live
        EV blend via ``ev_engine.EVEngine.compute_p_regime``.

        Deliberately does NOT touch ``ev_engine.py``:
        ``compute_p_regime`` already handles a ``"neutral"`` key
        correctly (``p = p_bullish + 0.5 * p_neutral``) — that's the
        existing mechanism for the 3-state label variant
        (``["bear","neutral","bull"]``), simply never populated by the
        4-state config this system runs. Returning a dict with a
        ``"neutral"`` key alongside the (possibly fewer than 4)
        directional keys is a strict superset of what
        ``compute_p_regime`` already expects — no changes needed there.

        Args:
            trend_probs: ``{label: probability}`` for this instance's
                own ``_trend_labels`` (e.g. the output of
                ``dict(zip(RegimeHMM.TREND_LABELS, regime.trend_probs))``,
                post any EMA smoothing the caller applies — gate whatever
                distribution is about to be used, not the raw per-tick
                posterior).

        Returns:
            A new dict, same shape plus ``"neutral"``. Probabilities for
            gated-out labels are folded into ``"neutral"`` rather than
            dropped, so the total probability mass is unchanged.
        """
        if not self.fitted or self._trend_model is None:
            raise RuntimeError("Call .fit() before .gated_trend_probs()")

        component_to_3state = self._classify_trend_components()

        gated: dict[str, float] = {}
        neutral_mass = 0.0
        for i, label in self._trend_label_map.items():
            p = float(trend_probs.get(label, 0.0))
            implied = self._TREND_TO_3STATE.get(label, "FLAT")
            real = component_to_3state.get(i, "FLAT")
            # Keep mass under its rank-assigned label only if the
            # classifier's own direct read of the component agrees with
            # what that label implies. A component the classifier calls
            # FLAT is uncertain; one it calls UP/DOWN but which
            # contradicts its rank label (e.g. rank-labeled "bull" but
            # actually DOWN) is evidence the label itself is wrong, not a
            # reason to keep counting it as bullish — either way, the
            # mass is untrustworthy under the original label and goes to
            # neutral rather than being silently misattributed.
            if real != "FLAT" and real == implied:
                gated[label] = gated.get(label, 0.0) + p
            else:
                neutral_mass += p
        gated["neutral"] = neutral_mass
        return gated

    # ── Convergence diagnostic (R4) ─────────────────────────────────────

    @staticmethod
    def _fit_converged(model: Optional[hmm.GaussianHMM]) -> bool:
        """Whether an already-fit model's EM run was numerically clean.

        NOT the same as hmmlearn's own ``model.monitor_.converged`` —
        that property is True if EITHER the log-likelihood improvement
        fell below ``tol`` OR ``n_iter`` was simply exhausted, so it can
        read True merely because the fit ran out of iterations, not
        because it actually stabilized. This checks the FULL per-iteration
        log-likelihood history (hmmlearn already records it, unbounded,
        in ``monitor_.history`` — nothing new to compute) for ANY
        consecutive decrease, using the SAME precision tolerance
        hmmlearn's own ``ConvergenceMonitor.report()`` uses internally
        for its "Model is not converging" warning — so this flag is
        True if and only if that warning did NOT fire during this fit.
        """
        monitor = getattr(model, "monitor_", None)
        history = list(getattr(monitor, "history", None) or [])
        if len(history) < 2:
            return True  # nothing to compare against — can't disprove convergence
        precision = np.finfo(float).eps ** 0.5
        return all(
            (history[i + 1] - history[i]) >= -precision
            for i in range(len(history) - 1)
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
            min_covar=self._STANDARDIZED_MIN_COVAR,
        )

    # Fixed min_covar in standardized-feature space (fix #3, see module
    # docstring). SUPERSEDES the old data-relative floor
    # (``_data_min_covar``, R1 fix #1, removed 2026-08-24): that floor
    # computed a per-symbol, per-window covariance floor from the LARGEST
    # per-column feature std, then applied that ONE value uniformly across
    # all feature dimensions — but the dimensions were never on the same
    # scale to begin with (e.g. trend model: returns std ~0.0004 vs cum_long
    # std ~0.003, an 8x spread; the Markov module's separate 9-feature model
    # was far worse, up to 18,000x). A floor sized for the largest-std
    # column is non-binding for the smaller ones, letting EM shrink their
    # variance further than intended — reproducing the exact overconfidence
    # the floor was meant to prevent, just on a different dimension, and
    # confirmed (real-data tested) to be a contributor to the "Model is not
    # converging" instability. Standardizing every feature to std=1 before
    # fitting (see ``_fit_scale``/``_standardize`` below) makes ONE fixed
    # floor value meaningful for every dimension, every symbol, every
    # window — no per-window computation needed.
    _STANDARDIZED_MIN_COVAR: float = 0.01

    @staticmethod
    def _fit_scale(features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Per-column (mu, sigma) for standardizing ``features``.

        A zero-variance column (e.g. a degenerate/constant window) would
        divide by zero — sigma is floored at 1.0 for those columns so
        standardization becomes a no-op (mean-centering only) rather than
        producing inf/NaN.
        """
        mu = np.mean(features, axis=0)
        sigma = np.std(features, axis=0)
        sigma = np.where(sigma > 0, sigma, 1.0)
        return mu, sigma

    @staticmethod
    def _standardize(features: np.ndarray, scale: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
        mu, sigma = scale
        return (features - mu) / sigma

    @staticmethod
    def _destandardize_means(model: hmm.GaussianHMM, scale: tuple[np.ndarray, np.ndarray]) -> None:
        """Convert a just-fit model's ``means_`` from standardized back to
        original feature units, in place, using the SAME (mu, sigma) the
        features were standardized with. Must run before anything reads
        ``means_`` (label assignment, continuity matching, introspection
        properties) — those all expect original-unit values, unchanged
        from before fix #3."""
        mu, sigma = scale
        model.means_ = model.means_ * sigma + mu

    @staticmethod
    def _sanitize_model(model: hmm.GaussianHMM) -> None:
        """
        Fix degenerate HMM parameters that cause predict_proba() to return NaN.

        When EM collapses some states (all observations assigned to a few states),
        the other states get zero rows in transmat_ and NaN/zero in startprob_.
        This is common with strongly-structured synthetic data or short windows.

        Fixes applied in-place:
        - NaN/zero startprob_ -> uniform
        - NaN/zero transmat_ rows -> uniform rows
        - NaN means_ -> zero
        - NaN/zero covars_ -> identity-like (1e-3 diagonal)
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

        # covars_ - write to private _covars_ to bypass shape-validation setter
        # Shape: "diag" -> (n_components, n_features), "full" -> (n, nf, nf)
        if hasattr(model, "_covars_") and model._covars_ is not None:
            bad = ~np.isfinite(model._covars_) | (model._covars_ <= 0)
            if bad.any():
                model._covars_ = np.where(bad, 1e-3, model._covars_)

    @staticmethod
    def _label_states(
        model: hmm.GaussianHMM,
        labels: list[str],
        previous_means: Optional[dict[str, float]] = None,
    ) -> dict[int, str]:
        """
        Label HMM components either by continuity with a previous fit, or
        (when no previous fit exists) by sorting component means.

        Uses feature 0 for vol (ascending -> low to high),
        feature 2 for trend (ascending -> bear to bull).
        Works for any n_components (2, 3, or 4).

        Args:
            previous_means: Optional ``{label: mean}`` from the PREVIOUS
                fit (this symbol's ``vol_label_means``/``trend_label_means``
                after its last fit). When given, each label is assigned to
                whichever of THIS fit's components has the closest mean to
                its previous value (greedy nearest-match, closest pair
                first) instead of blind rank-sort — R1 fix #2
                (worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md).
                Every fit is independent (refit from scratch each tick), so
                without this, two near-tied component means can swap rank
                order between ticks and silently relabel e.g. "bull" as
                "strong_bull" with no underlying price move. ``None``
                (a symbol's first fit) falls back to the original
                rank-sort behavior.
        """
        n = min(model.n_components, len(labels))
        feature_idx = 2 if len(labels) > 2 and labels[0] in ("strong_bear", "bear", "neutral") else 0
        means  = model.means_[:, feature_idx]

        if previous_means:
            remaining = list(range(n))
            label_map: dict[int, str] = {}
            # Closest (label, component) pair first, so a genuinely
            # ambiguous case doesn't steal the best match away from an
            # unambiguous one processed later.
            candidates = [
                (abs(float(means[i]) - target), lbl, i)
                for lbl, target in previous_means.items()
                if lbl in labels[:n]
                for i in remaining
            ]
            candidates.sort(key=lambda c: c[0])
            assigned_labels: set[str] = set()
            for _, lbl, i in candidates:
                if lbl in assigned_labels or i not in remaining:
                    continue
                label_map[i] = lbl
                assigned_labels.add(lbl)
                remaining.remove(i)
                if not remaining:
                    break
            # Any components with no previous-label match (new symbol
            # mid-warm-up, or a label set change) fall back to rank-sort
            # among themselves, preserving relative order.
            if remaining:
                leftover_labels = [l for l in labels[:n] if l not in assigned_labels]
                order = sorted(remaining, key=lambda i: means[i])
                for i, lbl in zip(order, leftover_labels):
                    label_map[i] = lbl
            return label_map

        order = np.argsort(means)
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
