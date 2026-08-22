"""
EV Engine v5 — 4-source logit-pooled P_win with T^N Markov hold probability.

Sources (default weights, override via ``set_weights()``):
    P_pattern       0.30   — historical pattern win-rate (from agent DuckDB)
    P_regime        0.25   — HMM regime posterior
    P_markov_hold_N 0.20   — T^N multi-step hold probability (N = bars to TP)
    P_timesfm       0.25   — TimesFM foundation model forecast

When any source is unavailable, the engine renormalises the remaining weights
to sum to 1.0 and blends only the available sources — it does NOT silently
substitute 0.5 (which would dilute signal). This is the correct logit-pooling
behaviour for missing sources.

v5 changes from v4:
    - 4-source blend (was 3-source; v4 had p_ext_signal which is deprecated)
    - Markov moved BACK into P_win blend as P_markov_hold_N (v4 had it as a
      sizing multiplier — that double-counted directionality with HMM only
      when Markov was single-step; with T^N multi-step hold it measures a
      genuinely different question: "what is the probability the price
      reaches the TP brick count within N bars?")
    - ``external_signal`` parameter removed (microstructure deprecated from EV)
    - ``p_pattern`` parameter added (source from agent DuckDB, optional)
    - ``transition_matrix`` + ``current_state`` + ``n_steps`` drive T^N
    - ``markov_multiplier`` kept on ``EVFullResult`` for backward
      compatibility but always 1.0 in v5 (no-op) — sizing is driven by
      ``ev_scale`` and downstream Kelly multipliers
"""

import logging
import math
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from dataclasses import dataclass

# ── Log-odds helpers ─────────────────────────────────────────────────
# Log-odds pooling respects probability bounds and prevents overconfidence
# when independent sources agree.

_LOGIT_CLAMP = 0.01  # L1 fix (2026-08-21): was 1e-15. A degenerate HMM produces
# p_markov_hold_n=0.0 → logit(0) = -34.5, which dominates the 4-source blend
# and crushes p_win to ~0. With clamp=0.01, logit(0.01) = -4.6 — still negative
# but allows p_regime and p_timesfm to countervail. Verified: AUDUSD p_win
# rises from ~0 to ~0.47. This does NOT mask degenerate models — the signal
# still fails qualification (p_win < 0.55) but the EOD correction loop can now
# engage and write bayesian_penalty actions.


def _logit(p: float) -> float:
    """Logit function: ln(p / (1-p)). Clamped for numerical stability."""
    p = max(_LOGIT_CLAMP, min(1.0 - _LOGIT_CLAMP, p))
    return math.log(p / (1.0 - p))


def _inv_logit(x: float) -> float:
    """Inverse logit (logistic sigmoid): 1 / (1 + exp(-x))."""
    if x > 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ── EV Engine v5 defaults ────────────────────────────────────────────
# 4-source blend (full).
# Markov hold probability is now blended into P_win (no longer a sizing
# multiplier) because the T^N multi-step question — "does price reach TP
# within N bars?" — is genuinely different from the HMM regime question.
P_WIN_WEIGHTS_FULL = {
    "p_pattern": 0.30,
    "p_regime": 0.25,
    "p_markov_hold_n": 0.20,
    "p_timesfm": 0.25,
}

# Renormalised 3-source fallback when TimesFM is unavailable.
P_WIN_WEIGHTS_NO_TIMESFM = {
    "p_pattern": 0.40,   # 0.30 / 0.75
    "p_regime": 0.3333,  # 0.25 / 0.75
    "p_markov_hold_n": 0.2667,  # 0.20 / 0.75
}

# Renormalised 3-source fallback when no pattern stats (server-side sweep
# without agent DuckDB access).
P_WIN_WEIGHTS_NO_PATTERN = {
    "p_regime": 0.3125,        # 0.25 / 0.80
    "p_markov_hold_n": 0.2500, # 0.20 / 0.80
    "p_timesfm": 0.3125,       # 0.25 / 0.80
}

# Renormalised 2-source fallback when both TimesFM and pattern are missing.
P_WIN_WEIGHTS_MINIMAL = {
    "p_regime": 0.5556,        # 0.25 / 0.45
    "p_markov_hold_n": 0.4444, # 0.20 / 0.45
}

# Regime shift meta-signal thresholds
TIMESFM_CONFIDENCE_MIN = 0.50
CALIBRATION_OVERCONFIDENT_THRESHOLD = 0.05


@dataclass
class EVResult:
    p_win: float
    ev: float
    rr: float
    allowed: bool
    reason: Optional[str] = None


@dataclass
class EVFullResult:
    """Full EV computation result with 4-source P_win breakdown.

    v5: ``p_markov`` is the T^N multi-step hold probability (not the v4
    single-step persistence). ``markov_multiplier`` is kept for backward
    compatibility but always 1.0 in v5 (no-op).
    """
    p_win: float
    p_regime: float
    p_pattern: float
    p_markov: float               # T^N multi-step hold probability
    p_timesfm: float = 0.0
    ev: float = 0.0
    ev_per_dollar: float = 0.0
    rr: float = 0.0
    profit: float = 0.0
    stake: float = 0.0
    allowed: bool = False
    aggression: str = "aggressive"
    ev_scale: float = 0.0
    markov_multiplier: float = 1.0  # Deprecated in v5; kept for backward compat
    sources_used: Tuple[str, ...] = ()
    weights_used: Dict[str, float] = None
    reason: Optional[str] = None


# ── T^N multi-step hold probability ──────────────────────────────────
# Given a 3x3 transition matrix T over states {UP, DOWN, FLAT}, the
# N-step transition matrix T^N gives P(state at step N | state at step 0).
# For a long trade with TP at brick count N, we want the probability of
# being in UP state at any step ≤ N — approximated by P(UP at step N).
# This is a first-passage approximation: it slightly underestimates the
# true hit probability (which would sum over all paths that touch UP
# at any step 1..N), but is numerically stable and avoids combinatorial
# explosion. For sizing purposes the approximation is conservative.

_STATES = ("UP", "DOWN", "FLAT")


def _matrix_to_dict(T_list: List[List[float]]) -> Dict[str, Dict[str, float]]:
    """Convert a 3x3 list-of-lists matrix to a dict-of-dicts keyed by state."""
    return {
        _STATES[i]: {_STATES[j]: float(T_list[i][j]) for j in range(3)}
        for i in range(3)
    }


def _dict_to_matrix(T_dict: Dict[str, Dict[str, float]]) -> Optional[List[List[float]]]:
    """Convert a dict-of-dicts transition matrix to a 3x3 list-of-lists.

    Returns ``None`` if the dict is missing any of the 9 required entries.
    """
    if not T_dict:
        return None
    try:
        M = [[0.0] * 3 for _ in range(3)]
        for i, s_from in enumerate(_STATES):
            row = T_dict.get(s_from, {})
            for j, s_to in enumerate(_STATES):
                M[i][j] = float(row.get(s_to, 0.0))
        return M
    except Exception:
        return None


def _matmul(A: List[List[float]], B: List[List[float]]) -> List[List[float]]:
    """3x3 matrix multiply."""
    return [
        [sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def _matrix_power(M: List[List[float]], n: int) -> List[List[float]]:
    """Raise a 3x3 matrix to the n-th power (n >= 0)."""
    # Identity
    result = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    base = [row[:] for row in M]
    while n > 0:
        if n & 1:
            result = _matmul(result, base)
        base = _matmul(base, base)
        n >>= 1
    return result


def _markov_hold_probability(
    transition_matrix: Optional[Dict[str, Dict[str, float]]],
    current_state: str,
    direction: str,
    n_steps: int,
) -> float:
    """Compute P(reach target state by step N | current state).

    Uses T^N — the N-step transition matrix — and reads the entry
    [current_state][target_state]. ``target_state`` is "UP" for long,
    "DOWN" for short.

    Conservative first-passage approximation: returns P(target at step N),
    not P(target at any step 1..N). The latter requires the cumulative
    sum of first-passage probabilities which is O(N^2) to compute and
    only marginally higher (~5-10%) for typical transition matrices.

    Args:
        transition_matrix: 3x3 dict-of-dicts keyed by {UP, DOWN, FLAT}.
        current_state: One of "UP", "DOWN", "FLAT".
        direction: "long" (target=UP) or "short" (target=DOWN).
        n_steps: Number of bars to TP (brick count). Must be >= 1.

    Returns:
        Probability in [0, 1]. Returns 0.5 (neutral) when:
          - transition_matrix is None/empty
          - current_state is unknown
          - n_steps < 1
          - matrix is malformed
    """
    if not transition_matrix or not current_state or n_steps < 1:
        return 0.5

    M = _dict_to_matrix(transition_matrix)
    if M is None:
        return 0.5

    try:
        # Validate row-stochastic (each row sums to ~1.0).
        # Degenerate rows (all zeros from 5→3 aggregation) indicate that
        # no 5-state HMM component maps to this 3-state label. For the
        # current_state's row, this means an absorbing state — P(reach
        # target in N steps) = 0.0. For other rows, set uniform prior
        # so T^N computation can proceed normally.
        current_idx = _STATES.index(current_state.upper()) if current_state.upper() in _STATES else 0
        for i in range(3):
            row_sum = sum(M[i])
            if row_sum <= 0:
                if i == current_idx:
                    # Absorbing state: cannot reach UP or DOWN from here
                    return 0.0
                else:
                    # Other degenerate row — use uniform prior
                    M[i] = [1.0 / 3.0] * 3
            elif abs(row_sum - 1.0) > 0.01:
                # Renormalise defensively
                M[i] = [v / row_sum for v in M[i]]

        # Compute T^N
        T_N = _matrix_power(M, n_steps)

        # Read entry [current_state][target_state]
        try:
            i = _STATES.index(current_state.upper())
        except ValueError:
            return 0.5
        target = "UP" if direction == "long" else "DOWN"
        try:
            j = _STATES.index(target)
        except ValueError:
            return 0.5

        p = float(T_N[i][j])
        # Clamp to [0, 1]
        return max(0.0, min(1.0, p))
    except Exception:
        return 0.5


class EVEngine:
    """
    Mathematical edge validation layer.
    Computes Expected Value (EV) based on 4-source logit-pooled P_win:
    pattern, regime, markov hold (T^N), and TimesFM.

    v5 Changes from v4:
      - 4-source blend: pattern + regime + markov_hold_N + timesfm
      - Markov moved back INTO P_win blend (T^N multi-step is a different
        question than HMM regime)
      - ``external_signal`` parameter removed (microstructure deprecated)
      - ``p_pattern`` parameter added (from agent DuckDB, optional)
      - ``n_steps`` parameter added (bars to TP, drives T^N)
      - Missing sources are renormalised out, not substituted with 0.5
      - ``markov_multiplier`` kept on result for backward compat (always 1.0)
    """

    def __init__(self, min_ev: float = 0.0, min_rr: float = 2.0):
        self.min_ev = min_ev
        self.min_rr = min_rr
        # Mutable weights — can be overridden at runtime
        self._full_weights: Dict[str, float] = dict(P_WIN_WEIGHTS_FULL)

    # ── Runtime weight override ─────────────────────────────────────

    # Canonical key set — used by set_weights() / temp_weights() to
    # validate keys and reject typos / stale v4 keys like ``p_ext_signal``.
    _ALLOWED_WEIGHT_KEYS: Tuple[str, ...] = (
        "p_pattern",
        "p_regime",
        "p_markov_hold_n",
        "p_timesfm",
    )

    def set_weights(self, weights: Dict[str, float]) -> None:
        """Override the 4-source P_win weights at runtime.

        Accepts a dict with any subset of the canonical keys:
        ``p_pattern``, ``p_regime``, ``p_markov_hold_n``, ``p_timesfm``.
        Missing keys keep their current values. Weights are NOT
        renormalised here — the engine renormalises at blend time based
        on which sources are actually available.

        v5.1 (audit 2026-07-23): the orchestrator's "soft TimesFM gate"
        previously reached directly into ``self._ev_engine._full_weights``
        to do a temporary override (orchestrator.py:1209-1228 pre-fix).
        That bypassed all validation and was exception-fragile. Callers
        should now use either ``set_weights()`` for permanent overrides
        or ``temp_weights()`` for a guaranteed-restored temporary
        override. Direct access to ``_full_weights`` from outside the
        class is deprecated and will be removed in v6.

        Raises:
            ValueError: if ``weights`` contains a key not in
                ``_ALLOWED_WEIGHT_KEYS``, or any value is not a finite
                number in [0, 1].
        """
        self._apply_weight_overrides(weights)

    def get_weights(self) -> Dict[str, float]:
        """Return the current 4-source P_win weights (pre-renormalisation)."""
        return dict(self._full_weights)

    def restore_default_weights(self) -> None:
        """Reset all 4-source weights to their module-level defaults.

        Useful after a long-running ``set_weights()`` override when the
        caller wants the engine back to its pristine state without
        reconstructing the object.
        """
        self._full_weights = dict(P_WIN_WEIGHTS_FULL)

    @contextmanager
    def temp_weights(self, overrides: Dict[str, float]) -> Iterator[Dict[str, float]]:
        """Context manager: temporarily override weights, restore on exit.

        Use this for any short-lived weight adjustment inside a single
        ``compute_ev()`` call (e.g. the orchestrator's soft TimesFM
        gate). The original weights are guaranteed to be restored even
        if the body raises — the restore happens in the ``finally``
        clause of the generator, so it survives exceptions, returns,
        and early breaks.

        Example::

            with ev_engine.temp_weights({"p_timesfm": 0.10}):
                result = ev_engine.compute_ev(...)
            # weights are back to their pre-``with`` values here

        Args:
            overrides: same shape as ``set_weights()`` — any subset of
                the canonical keys. Validation is identical to
                ``set_weights()`` and raises ``ValueError`` BEFORE the
                ``with`` block body runs, so callers know the failure
                was in their override, not in the compute_ev call.

        Yields:
            The transient weights dict that will be in effect inside
            the ``with`` block. Mutating the yielded dict has no effect
            on the engine — make changes via ``set_weights()`` instead.
        """
        saved = dict(self._full_weights)
        self._apply_weight_overrides(overrides)
        try:
            yield dict(self._full_weights)
        finally:
            # Restore — never raise out of finally unless the restore
            # itself fails (which it can't, since ``saved`` is a known
            # good dict).
            self._full_weights = saved

    def _apply_weight_overrides(self, overrides: Dict[str, float]) -> None:
        """Validate and apply a partial weight override in place.

        Shared by ``set_weights()`` and ``temp_weights()``. Validates
        keys (must be in ``_ALLOWED_WEIGHT_KEYS``) and values (must be
        finite floats in [0, 1]) before mutating ``_full_weights``.
        """
        if not isinstance(overrides, dict):
            raise TypeError(
                f"weights must be a dict, got {type(overrides).__name__}"
            )
        for key, value in overrides.items():
            if key not in self._ALLOWED_WEIGHT_KEYS:
                raise ValueError(
                    f"Unknown weight key {key!r}. "
                    f"Allowed keys: {self._ALLOWED_WEIGHT_KEYS}. "
                    f"Note: 'p_ext_signal' was removed in v5 — use 'p_pattern' "
                    f"for the historical-pattern source."
                )
            try:
                v = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Weight for {key!r} must be a number, got {value!r}"
                ) from exc
            if not math.isfinite(v) or v < 0.0 or v > 1.0:
                raise ValueError(
                    f"Weight for {key!r} must be a finite number in [0, 1], got {v}"
                )
            self._full_weights[key] = v

    # ── P_regime from HMM posteriors ────────────────────────────────
    # Cap p_regime to prevent logit(1.0)=34.5 from dominating the
    # logit-pooled blend. When the HMM posterior collapses to a single
    # state (conf=1.0), logit(1.0) overwhelms all other components
    # regardless of their values. Capping at 0.97 reduces the logit
    # contribution from 34.5 to 3.5, allowing other sources to countervail.
    # See worklog/20260818_EV_ENGINE_DEEP_ANALYSIS.md for details.
    _P_REGIME_CAP: float = 0.97

    @staticmethod
    def compute_p_regime(trend_probs: Dict[str, float], direction: str) -> float:
        """
        Compute P_regime from HMM trend posteriors.

        P_regime(long)  = P(bullish states) + 0.5 * P(neutral)
        P_regime(short) = P(bearish states) + 0.5 * P(neutral)

        Handles 2-state, 3-state, and 4-state HMM label variants:
          2: bear, bull
          3: bear, neutral, bull
          4: strong_bear, bear, bull, strong_bull

        The result is capped at ``_P_REGIME_CAP`` (0.97) to prevent the
        logit-pooled blend from being dominated by a single overconfident
        regime component. See the class docstring and worklog for rationale.
        """
        if not trend_probs:
            return 0.50

        bullish_keys = {"bull", "strong_bull"}
        bearish_keys = {"bear", "strong_bear"}
        neutral_keys = {"neutral"}

        p_bullish = sum(v for k, v in trend_probs.items() if k in bullish_keys)
        p_bearish = sum(v for k, v in trend_probs.items() if k in bearish_keys)
        p_neutral = sum(v for k, v in trend_probs.items() if k in neutral_keys)

        if direction == "long":
            p = p_bullish + 0.5 * p_neutral
        elif direction == "short":
            p = p_bearish + 0.5 * p_neutral
        else:
            p = 0.50

        # Cap to prevent logit(1.0)=34.5 from dominating the blend.
        # When HMM posteriors collapse to a single state, p_regime hits
        # 1.0 — even with a small weight, this overwhelms all other
        # components in the logit-pool. Capping at 0.97 keeps the signal
        # informative without allowing overconfidence to hijack the blend.
        return min(EVEngine._P_REGIME_CAP, p)

    # ── P_pattern from agent DuckDB (optional) ──────────────────────

    @staticmethod
    def compute_p_pattern(
        p_pattern: Optional[float],
        direction: str,
    ) -> Optional[float]:
        """Pass through the pattern win-rate from agent DuckDB.

        Returns ``None`` when no pattern stats are available — the blend
        will renormalise around the missing source. Returns the value
        unchanged otherwise (it is already a P(win) for the trade
        direction).

        Args:
            p_pattern: Historical win-rate for this pattern+regime+direction,
                       in [0, 1]. None when not available.
            direction: "long" or "short" — used to flip the probability
                       when the pattern stats are stored as bullish-only.
                       Currently a pass-through (caller is responsible for
                       selecting the correct pattern row).
        """
        if p_pattern is None:
            return None
        try:
            p = float(p_pattern)
            return max(0.0, min(1.0, p))
        except (TypeError, ValueError):
            return None

    # ── P_markov_hold_N — T^N multi-step hold probability ───────────

    @staticmethod
    def compute_p_markov_hold_n(
        transition_matrix: Optional[Dict[str, Dict[str, float]]],
        current_state: str,
        direction: str,
        n_steps: int,
    ) -> Optional[float]:
        """Compute P(reach TP within N bars | current state) via T^N.

        Returns ``None`` when no transition matrix is available — the
        blend will renormalise around the missing source. Returns 0.5
        (neutral) when the matrix is malformed or current_state is
        unknown — these are NOT missing-source cases, they are
        computation failures, so we substitute a neutral value rather
        than dropping the source.
        """
        if not transition_matrix or not current_state:
            return None
        return _markov_hold_probability(
            transition_matrix, current_state, direction, n_steps
        )

    # ── Aggression Dispatcher ──────────────────────────────────────

    @staticmethod
    def compute_aggression(ev_per_dollar: float) -> str:
        """Map ev_per_dollar to aggression level."""
        if ev_per_dollar > 0.40:
            return "passive"
        elif ev_per_dollar > 0.15:
            return "mid"
        else:
            return "aggressive"

    @staticmethod
    def compute_ev_scale(ev_per_dollar: float) -> float:
        """Compute EV-scaled Kelly factor."""
        if ev_per_dollar > 0:
            return min(1.0, ev_per_dollar / 0.50)
        return 0.0

    # ── Core: compute_ev (4-source blended P_win, v5) ───────────────

    def compute_ev(
        self,
        trend_probs: Optional[Dict[str, float]] = None,
        direction: str = "long",
        p_pattern: Optional[float] = None,
        transition_matrix: Optional[Dict[str, Dict[str, float]]] = None,
        current_state: str = "FLAT",
        tp_bricks: int = 5,
        sl_bricks: int = 3,
        brick_size: float = 0.50,
        p_timesfm: Optional[float] = None,
        n_steps: Optional[int] = None,
    ) -> EVFullResult:
        """Full EV computation with 4-source logit-pooled P_win (v5).

        The N for T^N is derived from ``tp_bricks`` (1 brick per bar —
        brick velocity model) unless explicitly overridden via ``n_steps``.

        Weights are renormalised at blend time based on which sources
        are actually available. Missing sources are NOT substituted
        with 0.5 — they are dropped from the blend and the remaining
        weights rescaled to sum to 1.0. This prevents a missing source
        from diluting the signal toward 0.5.

        Args:
            trend_probs: HMM regime posterior dict. Drives P_regime.
            direction: "long" or "short".
            p_pattern: Historical win-rate from agent DuckDB. None if
                       not available (server-side sweep without agent
                       access). Drives P_pattern.
            transition_matrix: 3x3 dict-of-dicts keyed by {UP, DOWN, FLAT}.
                               Drives P_markov_hold_N via T^N.
            current_state: One of "UP", "DOWN", "FLAT".
            tp_bricks: Take-profit brick count. Also used as N for T^N
                       unless ``n_steps`` is provided.
            sl_bricks: Stop-loss brick count.
            brick_size: Dollar size of one Renko brick.
            p_timesfm: TimesFM P(up next bar) forecast. None if
                       unavailable.
            n_steps: Optional override for T^N. Defaults to ``tp_bricks``
                     (1 brick per bar — brick velocity model).
        """
        # Step 1: Compute individual P_win sources
        p_regime = self.compute_p_regime(trend_probs or {}, direction)
        p_pattern_computed = self.compute_p_pattern(p_pattern, direction)

        # N for T^N — default to tp_bricks (brick velocity: 1 brick = 1 bar)
        N = int(n_steps) if n_steps is not None else int(tp_bricks)
        p_markov_hold_n = self.compute_p_markov_hold_n(
            transition_matrix, current_state, direction, N
        )

        p_timesfm_val = p_timesfm  # may be None

        # Step 2: Build the available-source map and renormalise weights
        # Missing sources are dropped; remaining weights rescaled to sum
        # to 1.0. This prevents dilution toward 0.5 when a source is
        # genuinely unavailable.
        available: Dict[str, float] = {}
        if p_pattern_computed is not None:
            available["p_pattern"] = p_pattern_computed
        available["p_regime"] = p_regime  # always available (defaults to 0.5)
        if p_markov_hold_n is not None:
            available["p_markov_hold_n"] = p_markov_hold_n
        if p_timesfm_val is not None:
            available["p_timesfm"] = p_timesfm_val

        # Renormalise weights over available sources only
        raw_weights = {
            k: self._full_weights.get(k, 0.0)
            for k in available
        }
        total_w = sum(raw_weights.values())
        if total_w > 0:
            weights_used = {k: v / total_w for k, v in raw_weights.items()}
        else:
            # All weights zero — fallback to uniform
            weights_used = {k: 1.0 / len(available) for k in available}

        # Step 3: Logit-pool the available sources
        # logit(P_win) = Σ w_i * logit(p_i)
        if not available:
            p_win = 0.5
        else:
            logit_sum = sum(
                weights_used[k] * _logit(p)
                for k, p in available.items()
            )
            p_win = _inv_logit(logit_sum)

        # Step 4: EV computation
        # profit/stake in dollars — brick_size * brick_count
        profit = float(tp_bricks) * float(brick_size)
        stake = float(sl_bricks) * float(brick_size)
        ev = (p_win * profit) - ((1.0 - p_win) * stake)
        ev_per_dollar = ev / stake if stake > 0 else 0.0
        rr = profit / stake if stake > 0 else 0.0

        # Step 5: Aggression dispatch
        # Markov no longer drives a sizing multiplier in v5 (it's in the
        # blend). ev_scale is purely Kelly scaling.
        aggression = self.compute_aggression(ev_per_dollar)
        ev_scale = self.compute_ev_scale(ev_per_dollar)

        # Step 6: Allow/deny gate
        allowed = (ev > self.min_ev) and (rr >= self.min_rr)
        reason = None
        if not allowed:
            reason = (
                f"EV=${ev:.2f}, RR={rr:.2f}, ev/$={ev_per_dollar:.3f} "
                f"(Min EV={self.min_ev}, Min RR={self.min_rr})"
            )

        return EVFullResult(
            p_win=round(p_win, 4),
            p_regime=round(p_regime, 4),
            p_pattern=round(p_pattern_computed, 4) if p_pattern_computed is not None else 0.0,
            p_markov=round(p_markov_hold_n, 4) if p_markov_hold_n is not None else 0.0,
            p_timesfm=round(p_timesfm_val, 4) if p_timesfm_val is not None else 0.0,
            ev=round(ev, 4),
            ev_per_dollar=round(ev_per_dollar, 4),
            rr=round(rr, 4),
            profit=round(profit, 4),
            stake=round(stake, 4),
            allowed=allowed,
            aggression=aggression,
            ev_scale=round(ev_scale, 4),
            markov_multiplier=1.0,  # Deprecated in v5; kept for backward compat
            sources_used=tuple(available.keys()),
            weights_used={k: round(v, 4) for k, v in weights_used.items()},
            reason=reason,
        )

    # ── Post-hoc recompute (audit 2026-08-22) ───────────────────────
    # Calibration enforcement (calibration_enforcement.apply_calibration_adjustment)
    # shrinks p_win toward 0.5 AFTER compute_ev() has already run, and the
    # orchestrator mutates EVFullResult.p_win in place. Without this method,
    # ev/ev_per_dollar/rr/allowed/ev_scale/aggression silently stayed computed
    # from the PRE-enforcement p_win — an internal-consistency bug that also
    # corrupted the aggression (execution-style) decision and all EV-based
    # telemetry downstream. See worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md
    # §1 "New finding — stale derived fields after calibration enforcement".

    def recompute_ev_fields(
        self, p_win: float, profit: float, stake: float
    ) -> Dict[str, Any]:
        """Recompute ev/ev_per_dollar/rr/allowed/ev_scale/aggression for a
        p_win that changed after ``compute_ev()`` already ran.

        ``profit``/``stake`` are unchanged by a post-hoc p_win adjustment
        (they're purely brick_size * brick_count), so they're passed through
        from the original ``EVFullResult`` rather than recomputed. ``rr``
        (``profit / stake``) is therefore also unchanged in value, but is
        returned for callers that want to overwrite the field consistently.

        Callers should overwrite ``EVFullResult.{ev, ev_per_dollar, rr,
        allowed, aggression, ev_scale}`` with this method's return values
        after mutating ``EVFullResult.p_win`` — never mutate ``p_win`` alone.
        """
        ev = (p_win * profit) - ((1.0 - p_win) * stake)
        ev_per_dollar = ev / stake if stake > 0 else 0.0
        rr = profit / stake if stake > 0 else 0.0
        allowed = (ev > self.min_ev) and (rr >= self.min_rr)
        aggression = self.compute_aggression(ev_per_dollar)
        ev_scale = self.compute_ev_scale(ev_per_dollar)
        return {
            "ev": round(ev, 4),
            "ev_per_dollar": round(ev_per_dollar, 4),
            "rr": round(rr, 4),
            "allowed": allowed,
            "aggression": aggression,
            "ev_scale": round(ev_scale, 4),
        }

    # ── v5: validate_signal / validate_signal_sync ──────────────────
    # Both methods are DEPRECATED in v5. They are kept only for backward
    # compatibility with renko/signal_filter.py:239 (validate_signal_sync)
    # and regime_platform/routers/strategy.py:76 (validate_signal).
    #
    # The v4 implementation queried nt_pattern_stats — a table that is
    # always empty (no server-side trade journal; see EV-REWORK-DESIGN-v2
    # §3.7). The v5 implementation uses the local HMM trend_probs as the
    # P_regime source and falls back to p_win=0.55 when unavailable.
    #
    # The canonical v5 path is compute_ev() — callers should migrate to
    # that interface. These wrappers exist to avoid breaking RenkoPipeline
    # / SignalFilter which still call them synchronously inside the bar
    # processing loop.

    async def validate_signal(self, db_session, regime: str, pattern: str,
                                expected_profit: float, expected_loss: float) -> EVResult:
        """v5: validate a signal using local HMM regime as P_win source.

        DEPRECATED — callers should use ``compute_ev()`` with ``trend_probs``
        for the full 4-source logit-pooled P_win. This async wrapper is
        kept only for ``regime_platform/routers/strategy.py:76`` which
        still calls it. The ``db_session`` parameter is ignored in v5
        (the ``nt_pattern_stats`` table is deprecated; pattern stats live
        in the agent's DuckDB, not the server).
        """
        try:
            # No DB lookup in v5 — pattern stats are agent-side only.
            # Use a conservative default; the caller (strategy router)
            # overrides this with real P_regime via trend_probs when
            # available (see validate_signal_sync below).
            p_win = 0.55

            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0

            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"

            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)

        except Exception as e:
            logging.error(f"EVEngine error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Calculation_Error")

    def validate_signal_sync(self, db_session, regime: str, pattern: str,
                            expected_profit: float, expected_loss: float,
                            trend_probs: Optional[Dict[str, float]] = None,
                            direction: Optional[str] = None) -> EVResult:
        """v5: synchronous EV validation for use in RenkoPipeline / SignalFilter.

        DEPRECATED — callers should use ``compute_ev()`` for the full
        4-source logit pool. This sync wrapper is kept because
        ``renko/signal_filter.py:239`` calls it inside the bar processing
        loop (which is synchronous). The v5 implementation uses real
        P_regime from HMM posteriors when available; otherwise falls back
        to p_win=0.55 (backward compatible). The ``db_session`` and
        ``pattern`` parameters are ignored in v5.
        """
        try:
            if trend_probs is not None and direction is not None:
                # Use real P_regime from HMM posteriors
                p_regime = self.compute_p_regime(trend_probs, direction)
                # In v5 the full 4-source blend happens in compute_ev().
                # This sync path uses P_regime alone because the other 3
                # sources (pattern stats, Markov T^N, TimesFM) are not
                # available in the synchronous bar-processing context.
                p_win = p_regime
            else:
                # Backward compatible: conservative default
                p_win = 0.55

            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0

            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"

            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)

        except Exception as e:
            logging.error(f"EVEngine sync error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Sync_Calculation_Error")

    # ── v3: Regime Shift Meta-Signal ──────────────────────────────

    @staticmethod
    def _shift_direction(prev_regime: str, current_regime: str) -> str:
        """
        Determine the direction of a regime shift.

        Returns ``"bullish"``, ``"bearish"``, or ``"neutral"`` based on
        the presence of bull/bear labels in the regime strings.
        """
        bull_markers = {"bull", "strong_bull"}
        bear_markers = {"bear", "strong_bear"}

        prev_bull = any(m in prev_regime for m in bull_markers)
        prev_bear = any(m in prev_regime for m in bear_markers)
        curr_bull = any(m in current_regime for m in bull_markers)
        curr_bear = any(m in current_regime for m in bear_markers)

        if not prev_bull and curr_bull:
            return "bullish"
        if not prev_bear and curr_bear:
            return "bearish"
        if prev_bull and not curr_bull:
            return "bearish"
        if prev_bear and not curr_bear:
            return "bullish"
        return "neutral"

    @staticmethod
    def _shift_magnitude(prev_probs: Dict[str, float],
                         curr_probs: Dict[str, float]) -> float:
        """Total variation distance between two regime distributions."""
        keys = set(prev_probs) | set(curr_probs)
        return 0.5 * sum(
            abs(curr_probs.get(k, 0.0) - prev_probs.get(k, 0.0))
            for k in keys
        )

    def detect_regime_shift(self, prev: Dict[str, Any],
                            curr: Dict[str, Any]) -> Dict[str, Any]:
        """Detect regime shifts and compute a meta-signal.

        Returns a dict with keys:
          - ``shifted``: bool
          - ``direction``: "bullish" | "bearish" | "neutral"
          - ``magnitude``: float in [0, 1]
          - ``confidence``: float in [0, 1] — confidence in the shift
          - ``prev_label``: str
          - ``curr_label``: str
        """
        prev_label = prev.get("regime_label", "unknown")
        curr_label = curr.get("regime_label", "unknown")
        prev_probs = prev.get("trend_probs", {})
        curr_probs = curr.get("trend_probs", {})

        shifted = (prev_label != curr_label) and prev_label != "unknown"
        direction = self._shift_direction(prev_label, curr_label) if shifted else "neutral"
        magnitude = self._shift_magnitude(prev_probs, curr_probs) if shifted else 0.0

        # Confidence scales with magnitude — small shifts are noisy
        confidence = min(1.0, magnitude * 2.0) if shifted else 0.0

        return {
            "shifted": shifted,
            "direction": direction,
            "magnitude": round(magnitude, 4),
            "confidence": round(confidence, 4),
            "prev_label": prev_label,
            "curr_label": curr_label,
        }

    # ── v3: Compute Tail Risk (TimesFM-driven) ──────────────────────

    def compute_tail_risk(self, p_timesfm: Optional[float]) -> Dict[str, Any]:
        """Compute tail-risk adjustment based on TimesFM confidence.

        Returns a dict with:
          - ``action``: "skip" | "reduce" | "normal"
          - ``multiplier``: float in [0, 1] — sizing multiplier
          - ``reason``: str

        ``p_timesfm`` close to 0 or 1 indicates high-confidence forecast
        (extreme). When ``p_timesfm`` is None (TimesFM unavailable),
        returns "normal" with multiplier 1.0 — the absence of TimesFM
        should not block trading (it just removes one source from the
        blend).

        .. deprecated::
            Called only by the deprecated Supabase Edge Function path.
            The Python orchestrator does not call this method.
        """
        if p_timesfm is None:
            return {"action": "normal", "multiplier": 1.0, "reason": "no_timesfm"}

        is_extreme = (p_timesfm < 0.20) or (p_timesfm > 0.80)
        if is_extreme:
            return {
                "action": "skip",
                "multiplier": 0.0,
                "reason": f"timesfm_extreme_{ 'low' if p_timesfm < 0.20 else 'high'}",
            }
        if p_timesfm < 0.30 or p_timesfm > 0.70:
            return {
                "action": "reduce",
                "multiplier": 0.5,
                "reason": "timesfm_moderate_extreme",
            }
        return {"action": "normal", "multiplier": 1.0, "reason": "normal"}
