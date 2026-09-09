"""
Meta-Regime Model — GaussianHMM over the signal-generation stream
════════════════════════════════════════════════════════════════

Worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §3-5.

``regime_platform/core/regime_engine.py``'s ``RegimeHMM`` already is a
``hmm.GaussianHMM`` (via ``hmmlearn``), fit fresh every sweep tick on raw
PRICE-derived features (rolling-window return statistics) to answer "what's
the market regime." This module points the same class of tool at a
different layer: the model's OWN output stream (regime-label stability,
p_win trajectory) to answer a different question — "is my own signal-
generation process currently coherent, or currently noisy" — a meta-state,
not a market regime.

Why this is the right tool: an HMM models a sequence of CORRELATED
observations by inferring a small number of hidden states and the
transition dynamics between them. That is exactly the structure a 5/15-
minute signal stream has (long same-state runs punctuated by real
transitions) — treating each tick as independent (as qualification and,
until 2026-09-08, calibration-bias counting both did) is the wrong lens;
a sequential model is the right one. Confirmed with a real production
example: XAUUSD 2026-09-07 11:00-16:00 showed brick-level price direction
holding one unbroken trend the whole window while the regime-HMM's own
directional call flip-flopped four times — the meta-state this module
computes is meant to catch exactly that kind of self-inconsistency.

**Long-history fit, short-window read** (§4-5 of the worklog): fitting a
fresh HMM on a single short block (e.g. 4 hours / ~16-48 ticks) hits the
same sparse-transition problem episode-counting exposed one layer down —
a short block might contain zero genuine state changes, nowhere near
enough to fit a transition matrix. This model is meant to be fit on a
long, continuous history (weeks, via ``signal_stream_cache.py``'s local
lookback), using every tick as a sequential observation, and read at
inference time for "what state is this symbol in right now."

**Aggregation window is event-driven, not a fixed duration** (§5, user's
explicit correction — "it's not a fixed 4hr block but a time period for
aggregation" / "how would the system know to maintain/change this"):
``summarize_since_last_transition`` reports "how long has the current
state held" from the fitted state SEQUENCE itself — it has no duration
parameter to tune or drift out of date, because the boundary is defined
by the data (the last real transition), not a wall clock.

Reuses ``RegimeHMM``'s standardization/degenerate-fit-repair static
methods directly (``_fit_scale``/``_standardize``/``_sanitize_model``,
``_STANDARDIZED_MIN_COVAR``) rather than re-deriving them — same
precedent this whole design leans on for ``min_bars``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
from hmmlearn import hmm

from regime_platform.core.regime_engine import RegimeHMM

logger = logging.getLogger("regime.sweep.meta_regime_model")

STATE_COHERENT = "coherent"
STATE_NOISY = "noisy"

# Mirrors RegimeHMM.fit()'s own empirical minimum: max(50, 4 * n_components * 5).
# n_components=2 here (coherent/noisy) -> max(50, 40) = 50.
MIN_TICKS_TO_FIT = 50


@dataclass
class MetaRegimeFit:
    """A fitted meta-regime model for one symbol."""
    model: hmm.GaussianHMM
    scale: tuple[np.ndarray, np.ndarray]
    # Maps hmmlearn's raw component index -> STATE_COHERENT/STATE_NOISY,
    # decided post-fit by ranking components (see _label_components).
    state_by_component: dict[int, str]
    n_ticks_fit: int


# v2 (2026-09-09): window for the rolling confidence-volatility feature
# (column 3, below). Short enough to localize a brief spike to roughly
# where it happened, long enough to have more than one sample to take a
# standard deviation over. Not tuned against a grid — a small, defensible
# starting point (a spike lasting 1 tick surrounded by calm ticks should
# stand out clearly at this window size); revisit with real shadow-mode
# data once this ships past `off`.
CONFIDENCE_VOLATILITY_WINDOW = 4


def _build_features(history: list[dict[str, Any]]) -> np.ndarray:
    """Turn a symbol's tick history (oldest-first, as returned by
    ``signal_stream_cache.fetch_history``) into a feature matrix.

    Columns:
      0. ``confidence_deviation`` = ``abs(p_win - 0.5)`` — how far from a
         coin-flip the model's own stated confidence is. Missing/None
         p_win treated as 0.5 (no deviation — the neutral assumption).
      1. ``regime_conf`` — the underlying RegimeHMM's own posterior
         confidence for this tick. Missing/None treated as 0.0.
      2. ``label_persistence`` — 1.0 if this tick's ``regime_label``
         equals the PREVIOUS tick's, else 0.0. The first tick (no
         predecessor) gets 1.0 (neutral — no evidence of instability yet).
      3. ``regime_conf_volatility`` (v2, 2026-09-09) — rolling standard
         deviation of column 1 (``regime_conf``, NOT column 0) over the
         trailing ``CONFIDENCE_VOLATILITY_WINDOW`` ticks (including this
         one). Added specifically because v1's real-data validation
         (worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
         §9) found label_persistence's clean binary 0/1 separation
         dominated the Gaussian fit over the noisier, more continuous
         columns — so a confidence SPIKE with no accompanying label
         change went undetected. This is deliberately keyed off
         ``regime_conf``, not ``confidence_deviation``/p_win: the exact
         XAUUSD 2026-09-07 13:01 case that motivated this fix was a
         ``regime_conf`` spike (0.477→0.896 in one tick) while p_win
         barely moved (0.546→0.562) and the label held
         "med_low_vol_strong_bear" throughout — volatility on the wrong
         column would have missed the very case this exists to catch. A
         local volatility spike is a clean, near-binary signal (elevated
         during and briefly after a spike, ~0 in a calm stretch) — giving
         the fit a SECOND separable axis instead of relying on
         label_persistence alone. Fewer than 2 ticks of history available
         for the window (i.e. the very first tick) get 0.0 (no evidence
         of volatility yet, same "neutral" convention as
         label_persistence's first-tick handling).

    This is deliberately a small, cheap feature set, not the full
    brick-direction-persistence design in the worklog — brick data is not
    yet trustworthy for this purpose (see worklog §7, Finding 1:
    fabricated timestamps, now fixed 2026-09-09 but not yet wired into
    this feature set) and needs its own validation before it's a safe
    input here. Extending this feature set further is expected, not a
    redesign.
    """
    n = len(history)
    features = np.zeros((n, 4), dtype=float)
    prev_label = None
    for i, row in enumerate(history):
        p_win = row.get("p_win")
        features[i, 0] = abs(float(p_win) - 0.5) if p_win is not None else 0.0
        regime_conf = row.get("regime_conf")
        features[i, 1] = float(regime_conf) if regime_conf is not None else 0.0
        label = row.get("regime_label")
        features[i, 2] = 1.0 if (prev_label is None or label == prev_label) else 0.0
        prev_label = label

        window_start = max(0, i - CONFIDENCE_VOLATILITY_WINDOW + 1)
        window = features[window_start:i + 1, 1]  # regime_conf, not confidence_deviation
        features[i, 3] = float(np.std(window)) if len(window) >= 2 else 0.0
    return features


def _label_components(
    model: hmm.GaussianHMM, scale: tuple[np.ndarray, np.ndarray],
) -> dict[int, str]:
    """Decide which raw hmmlearn component index is STATE_COHERENT vs
    STATE_NOISY. Component identity/order is not stable across fits (same
    caveat as RegimeHMM) — this re-derives the mapping fresh every fit
    rather than assuming an index convention.

    Ranks components by mean (regime_conf + label_persistence -
    regime_conf_volatility) in ORIGINAL units (destandardized) — higher
    combined score means the component represents ticks where confidence
    was high, the label was holding steady, AND confidence wasn't
    whipsawing tick-to-tick, which is the definition of "coherent" this
    module uses. ``regime_conf_volatility`` (v2, column 3) is SUBTRACTED —
    it's a noisiness measure, so a high value should pull a component
    toward STATE_NOISY, not away from it.
    """
    mu, sigma = scale
    # means_ is in standardized space; destandardize the same way
    # RegimeHMM._destandardize_means does.
    means_original = model.means_ * sigma + mu
    scores = means_original[:, 1] + means_original[:, 2] - means_original[:, 3]
    order = np.argsort(scores)  # ascending: lowest score first
    return {
        int(order[0]): STATE_NOISY,
        int(order[-1]): STATE_COHERENT,
    }


def fit(history: list[dict[str, Any]]) -> Optional[MetaRegimeFit]:
    """Fit a 2-state (coherent/noisy) Gaussian HMM on a symbol's tick
    history. Returns ``None`` if there isn't enough history yet
    (``len(history) < MIN_TICKS_TO_FIT``) rather than fitting on data too
    thin to trust — mirrors ``RegimeHMM.fit()``'s own minimum-bars floor,
    not an arbitrarily different number.
    """
    if len(history) < MIN_TICKS_TO_FIT:
        logger.debug(
            "meta_regime_model: only %d ticks, need >= %d — skipping fit",
            len(history), MIN_TICKS_TO_FIT,
        )
        return None

    features = _build_features(history)
    scale = RegimeHMM._fit_scale(features)
    features_std = RegimeHMM._standardize(features, scale)

    model = hmm.GaussianHMM(
        n_components=2,
        covariance_type="diag",
        n_iter=100,
        random_state=42,
        min_covar=RegimeHMM._STANDARDIZED_MIN_COVAR,
    )
    model.fit(features_std)
    RegimeHMM._sanitize_model(model)

    state_by_component = _label_components(model, scale)
    return MetaRegimeFit(
        model=model, scale=scale,
        state_by_component=state_by_component,
        n_ticks_fit=len(history),
    )


def predict_latest(fitted: MetaRegimeFit, history: list[dict[str, Any]]) -> dict[str, Any]:
    """Posterior state probabilities for the MOST RECENT tick in
    ``history`` (same feature construction as ``fit``, standardized with
    the FIT's own stored scale — never re-fit a fresh scale at predict
    time, that would make predictions incomparable across calls).

    Returns ``{"coherent_prob": float, "noisy_prob": float, "state": str}``
    — ``state`` is whichever has higher posterior probability.
    """
    features = _build_features(history)
    features_std = RegimeHMM._standardize(features, fitted.scale)
    posteriors = fitted.model.predict_proba(features_std)[-1]

    probs = {STATE_COHERENT: 0.0, STATE_NOISY: 0.0}
    for component_idx, state in fitted.state_by_component.items():
        probs[state] += float(posteriors[component_idx])

    state = STATE_COHERENT if probs[STATE_COHERENT] >= probs[STATE_NOISY] else STATE_NOISY
    return {
        "coherent_prob": round(probs[STATE_COHERENT], 4),
        "noisy_prob": round(probs[STATE_NOISY], 4),
        "state": state,
    }


def summarize_since_last_transition(
    fitted: MetaRegimeFit, history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Event-driven aggregation summary — "how long has the CURRENT state
    held, and what happened during it" — instead of a fixed wall-clock
    block. This is the direct answer to "it's not a fixed 4hr block but a
    time period for aggregation... how would the system know to maintain/
    change this": there is no duration to tune, because the boundary is
    the last real state transition in the fitted Viterbi sequence, not a
    clock. Self-adjusting by construction — a fast-moving symbol
    transitions often and gets short, frequent summaries; a slow one gets
    long, infrequent ones, with no parameter deciding which.

    Returns ``{"current_state": str, "ticks_in_current_state": int,
    "since_ts": float, "n_transitions_total": int}``. Returns a
    zero/empty-shaped result (not ``None``) on an empty history, so
    callers can treat "no data yet" uniformly rather than special-casing.
    """
    if not history:
        return {
            "current_state": None, "ticks_in_current_state": 0,
            "since_ts": None, "n_transitions_total": 0,
        }

    features = _build_features(history)
    features_std = RegimeHMM._standardize(features, fitted.scale)
    viterbi_components = fitted.model.predict(features_std)
    states = [fitted.state_by_component.get(int(c), STATE_NOISY) for c in viterbi_components]

    current_state = states[-1]
    ticks_in_current_state = 1
    for s in reversed(states[:-1]):
        if s == current_state:
            ticks_in_current_state += 1
        else:
            break
    since_index = len(states) - ticks_in_current_state
    since_ts = history[since_index].get("ts")

    n_transitions_total = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])

    return {
        "current_state": current_state,
        "ticks_in_current_state": ticks_in_current_state,
        "since_ts": since_ts,
        "n_transitions_total": n_transitions_total,
    }


async def record_tick_and_update_shadow(
    symbol: str,
    ts: float,
    direction: str,
    regime_label: Optional[str],
    regime_conf: Optional[float],
    p_win: Optional[float],
    kelly_f: Optional[float],
) -> None:
    """Single per-tick entry point wiring the whole pipeline together:
    append this tick to the local lookback cache, and — if
    ``signal_stream_meta_model_mode`` is not ``off`` and enough history
    exists — fit, predict, and persist the current read to
    ``nt_signal_stream_meta_shadow`` (migration 147).

    This is the piece that was missing entirely before 2026-09-09
    (worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §11,
    "what happened to all the shadow mode tracking?") — the model and the
    cache existed, but nothing called either of them from the live sweep,
    so no shadow data could ever have accumulated regardless of how long
    the mode stayed off. Fits fresh every call rather than caching a
    fitted model across ticks — deliberately simple: at the local cache's
    realistic size (thousands of rows, not millions), a 2-state GaussianHMM
    fit is sub-second, so a refit-interval/caching scheme would be
    premature optimization until proven otherwise by real shadow-mode
    load.

    Best-effort throughout — every failure is caught and logged, never
    raised into the sweep tick, matching every other fire-and-forget
    capture in this codebase (``log_signal_sim``, ``log_renko_bricks``, …).
    """
    from regime_platform.core.feature_flags import get_signal_stream_meta_model_mode
    from regime_platform.sweep_orchestrator.signal_stream_cache import (
        append_tick,
        fetch_history,
    )

    try:
        await append_tick({
            "symbol": symbol, "ts": ts, "direction": direction,
            "regime_label": regime_label, "regime_conf": regime_conf,
            "p_win": p_win, "kelly_f": kelly_f,
        })
    except Exception as exc:
        logger.warning("record_tick_and_update_shadow: append_tick failed for %s: %s", symbol, exc)

    if get_signal_stream_meta_model_mode() == "off":
        return

    try:
        history = await fetch_history(symbol)
        fitted = fit(history)
        if fitted is None:
            return
        prediction = predict_latest(fitted, history)
        summary = summarize_since_last_transition(fitted, history)
        result = {
            "state": prediction["state"],
            "coherent_prob": prediction["coherent_prob"],
            "noisy_prob": prediction["noisy_prob"],
            "ticks_in_current_state": summary["ticks_in_current_state"],
            "since_ts": summary["since_ts"],
            "n_transitions_total": summary["n_transitions_total"],
            "n_ticks_fit": fitted.n_ticks_fit,
        }
        from regime_platform.sweep_orchestrator.supabase_client import (
            record_signal_stream_meta_shadow,
        )
        await record_signal_stream_meta_shadow(symbol, result)
    except Exception as exc:
        logger.warning("record_tick_and_update_shadow: shadow update failed for %s: %s", symbol, exc)
