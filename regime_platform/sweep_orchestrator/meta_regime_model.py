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

    This is deliberately a small, cheap feature set for a first cut, not
    the full brick-direction-persistence design in the worklog — brick
    data is not yet trustworthy for this purpose (see worklog §7, Finding
    1: fabricated timestamps) and needs its own fix before it's a safe
    input here. Extending this feature set once that lands is expected,
    not a redesign.
    """
    n = len(history)
    features = np.zeros((n, 3), dtype=float)
    prev_label = None
    for i, row in enumerate(history):
        p_win = row.get("p_win")
        features[i, 0] = abs(float(p_win) - 0.5) if p_win is not None else 0.0
        regime_conf = row.get("regime_conf")
        features[i, 1] = float(regime_conf) if regime_conf is not None else 0.0
        label = row.get("regime_label")
        features[i, 2] = 1.0 if (prev_label is None or label == prev_label) else 0.0
        prev_label = label
    return features


def _label_components(
    model: hmm.GaussianHMM, scale: tuple[np.ndarray, np.ndarray],
) -> dict[int, str]:
    """Decide which raw hmmlearn component index is STATE_COHERENT vs
    STATE_NOISY. Component identity/order is not stable across fits (same
    caveat as RegimeHMM) — this re-derives the mapping fresh every fit
    rather than assuming an index convention.

    Ranks components by their mean (regime_conf + label_persistence) in
    ORIGINAL units (destandardized) — higher combined score means the
    component represents ticks where confidence was high AND the label
    was holding steady, which is the definition of "coherent" this module
    uses.
    """
    mu, sigma = scale
    # means_ is in standardized space; destandardize columns 1 (regime_conf)
    # and 2 (label_persistence) the same way RegimeHMM._destandardize_means does.
    means_original = model.means_ * sigma + mu
    scores = means_original[:, 1] + means_original[:, 2]
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
