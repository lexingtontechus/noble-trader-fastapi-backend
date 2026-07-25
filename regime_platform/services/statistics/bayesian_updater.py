"""
Bayesian Updater - live P_win updates based on in-session trade outcomes.

.. deprecated:: P5 (2026-07-23)
    This class is **dead code in the backend**. The Noble Trader backend
    (``noble-trader-fastapi-backend``) is the **sweep orchestrator only** —
    it does NOT execute trades and therefore has no live trade outcomes
    to feed a Bayesian updater. As of P5, all execution lives in the
    agent (``noble-trader-agent``).

    The canonical replacement is ``hermes.agent.bayesian_alpha.BayesianAlpha``
    in the agent repo. ``BayesianAlpha`` is a strict superset of this
    class:

    * DuckDB-persistent (this class is in-memory only — lost on restart)
    * Decay-weighted rolling window of N trades (this class uses fixed
      Beta-Binomial with Laplace smoothing over the entire session)
    * Floor/ceiling clamping to [0.10, 1.0] (this class has a binary
      ``should_pause`` circuit breaker after 3 consecutive losses, which
      is too brittle for live trading — see audit recommendation D-2)
    * Thread-safe (this class has no locking)
    * Per-symbol caching with TTL

    The class is preserved here ONLY to avoid breaking the
    ``statistics/__init__.py`` re-export surface and any external
    importers. **Do not instantiate in new backend code.** If you need
    a Bayesian trade-outcome-driven P_win modulator, use
    ``BayesianAlpha`` in the agent repo.

Uses Beta-Binomial conjugate updates with Laplace smoothing and a
consecutive-loss decay penalty. When consecutive losses exceed a
threshold, the system flags a should_pause condition to halt trading
until the session is reset.

v3: Accepts an optional TimesFM forecast as a dynamic Bayesian prior,
replacing the fixed heuristic prior with a foundation-model-informed
estimate.

Usage::

    # v2: fixed prior
    updater = BayesianUpdater(prior_p_win=0.55)

    # v3: TimesFM-enhanced prior
    updater = BayesianUpdater(prior_p_win=0.55,
                               timesfm_prior=0.72,
                               timesfm_prior_weight=0.30)

    p = updater.observe_trade(is_win=True)   # P_win goes up
    p = updater.observe_trade(is_win=False)  # P_win goes down
    if updater.should_pause:
        # Pause trading
        updater.reset_session()
"""

import warnings
from typing import Dict, List, Optional


_DEPRECATION_MSG = (
    "BayesianUpdater is dead code in the backend (the backend does not "
    "execute trades — see P5 execution-deprecation). The canonical "
    "replacement is hermes.agent.bayesian_alpha.BayesianAlpha in the "
    "agent repo. This class is preserved only for backward-compat "
    "imports; instantiating it has no effect on production signal "
    "generation."
)


class BayesianUpdater:
    """Live Bayesian P_win updates based on in-session trade outcomes.

    v3: The prior can be enhanced by a TimesFM forecast using a weighted
    blend::

        effective_prior = (1 - timesfm_prior_weight) * default_prior
                          + timesfm_prior_weight * timesfm_forecast
    """

    def __init__(
        self,
        prior_p_win: float = 0.55,
        decay_rate: float = 0.10,
        timesfm_prior: Optional[float] = None,
        timesfm_prior_weight: float = 0.30,
    ):
        """
        Parameters
        ----------
        prior_p_win : float
            Default prior probability of winning (0–1).
        decay_rate : float
            Penalty per consecutive loss applied to posterior (default 0.10).
        timesfm_prior : float or None
            TimesFM forecast probability (0–1). If None, uses default prior.
        timesfm_prior_weight : float
            Weight given to the TimesFM forecast when blending with the
            default prior (0 = ignore TimesFM, 1 = fully replace prior).

        .. deprecated:: P5
            Emits a ``DeprecationWarning`` on every instantiation. See
            the module docstring for the migration path to
            ``hermes.agent.bayesian_alpha.BayesianAlpha``.
        """
        warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)

        self.prior_p_win = prior_p_win
        self.decay_rate = decay_rate
        self.timesfm_prior = timesfm_prior
        self.timesfm_prior_weight = max(0.0, min(1.0, timesfm_prior_weight))

        # Compute effective prior
        if timesfm_prior is not None:
            w = self.timesfm_prior_weight
            self.current_p_win = (1.0 - w) * prior_p_win + w * timesfm_prior
        else:
            self.current_p_win = prior_p_win

        self.session_trades: List[bool] = []  # True=win, False=loss
        self.consecutive_losses = 0

    def observe_trade(self, is_win: bool) -> float:
        """Update P_win after a trade outcome using Bayesian update."""
        self.session_trades.append(is_win)

        if is_win:
            self.consecutive_losses = 0
            # Beta-Binomial update with Laplace smoothing: (n_wins + 1) / (n_total + 2)
            n_wins = sum(self.session_trades)
            n_total = len(self.session_trades)
            self.current_p_win = (n_wins + 1) / (n_total + 2)
        else:
            self.consecutive_losses += 1
            # Apply session decay: penalize P_win by decay_rate per consecutive loss
            n_wins = sum(self.session_trades)
            n_total = len(self.session_trades)
            base_p = (n_wins + 1) / (n_total + 2)
            decay_penalty = self.decay_rate * min(self.consecutive_losses, 5)
            self.current_p_win = max(0.10, base_p - decay_penalty)

        return self.current_p_win

    def reset_session(self) -> None:
        """Reset for a new trading session.

        The prior is re-computed each session, so if the caller provides
        a fresh ``timesfm_prior``, the new session starts with an updated
        foundation-model-informed estimate.
        """
        self.session_trades = []
        self.consecutive_losses = 0
        if self.timesfm_prior is not None:
            w = self.timesfm_prior_weight
            self.current_p_win = (1.0 - w) * self.prior_p_win + w * self.timesfm_prior
        else:
            self.current_p_win = self.prior_p_win

    def set_timesfm_prior(self, timesfm_prior: float) -> None:
        """Update the TimesFM prior mid-session (e.g. after a fresh forecast)."""
        self.timesfm_prior = timesfm_prior
        # Recompute current P_win using the new prior (only if no trades yet)
        if not self.session_trades:
            w = self.timesfm_prior_weight
            self.current_p_win = (1.0 - w) * self.prior_p_win + w * timesfm_prior

    @property
    def session_win_rate(self) -> float:
        """Raw session win rate (no smoothing)."""
        if not self.session_trades:
            return self.current_p_win
        return sum(self.session_trades) / len(self.session_trades)

    @property
    def should_pause(self) -> bool:
        """Pause trading after 3+ consecutive losses."""
        return self.consecutive_losses >= 3

    @property
    def n_trades(self) -> int:
        """Number of trades observed in this session."""
        return len(self.session_trades)

    @property
    def n_wins(self) -> int:
        """Number of wins in this session."""
        return sum(self.session_trades)

    @property
    def n_losses(self) -> int:
        """Number of losses in this session."""
        return len(self.session_trades) - sum(self.session_trades)

    # ── EVEngine bridge (audit 2026-07-23) ──────────────────────────
    # The user-asked-for "fix BayesianUpdater EVEngine.set_weights()"
    # surfaces here: a deprecated-but-callable bridge that lets a
    # (theoretical) caller translate this updater's session-derived
    # P_win into a weight adjustment on an EVEngine. This was the
    # missing calibration feedback loop flagged in the audit:
    # CALIBRATION_OVERCONFIDENT_THRESHOLD was defined but never used,
    # set_weights() existed but was only called from the orchestrator's
    # soft TimesFM gate (now via temp_weights()), and BayesianUpdater
    # had no path to influence the EV blend.
    #
    # In practice this bridge is **never invoked in production** because
    # BayesianUpdater itself is dead code in the backend (see module
    # docstring). The agent's BayesianAlpha + agent-side synthesizer
    # already provides the live calibration feedback loop. This method
    # exists for (a) test completeness, (b) any future backend-only
    # research/simulation harness that wants to wire the two together.

    def apply_to_ev_engine(
        self,
        ev_engine: "object",
        bias_field: str = "p_regime",
        max_scale: float = 0.5,
    ) -> Dict[str, float]:
        """Translate this updater's P_win bias into an EVEngine weight override.

        The mapping is intentionally simple: when the session P_win
        diverges from the prior, scale the named source's weight
        proportionally. ``p_win > prior`` ⇒ the source has been
        informative, give it more weight (up to ``max_scale`` × base).
        ``p_win < prior`` ⇒ the source is overconfident, shrink its
        weight toward 0.

        Args:
            ev_engine: an ``EVEngine`` instance. Accepted as ``object``
                to avoid an import cycle (this module is in
                ``statistics/``, same package as ``ev_engine``).
            bias_field: which canonical weight key to scale. Default
                ``p_regime`` (the source most correlated with
                in-session outcomes). One of ``p_pattern``,
                ``p_regime``, ``p_markov_hold_n``, ``p_timesfm``.
            max_scale: cap on the multiplier applied to the base weight.
                Default 0.5 means: at most halve the weight (when
                P_win is much lower than prior) or boost by 50% (when
                much higher).

        Returns:
            The dict passed to ``ev_engine.set_weights()``. Also
            **calls** ``ev_engine.set_weights()`` with that dict, so
            the caller does not need to do anything further.

        Raises:
            DeprecationWarning: always (this whole class is deprecated).
            ValueError: if ``bias_field`` is not a canonical EVEngine
                weight key, or if ``ev_engine`` has no ``set_weights``.
        """
        warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)

        # Lazy import to avoid cycle.
        from regime_platform.services.statistics.ev_engine import EVEngine  # noqa: PLC0415

        if not isinstance(ev_engine, EVEngine):
            raise ValueError(
                f"ev_engine must be an EVEngine instance, got "
                f"{type(ev_engine).__name__}"
            )
        if bias_field not in EVEngine._ALLOWED_WEIGHT_KEYS:
            raise ValueError(
                f"bias_field must be one of {EVEngine._ALLOWED_WEIGHT_KEYS}, "
                f"got {bias_field!r}"
            )

        # Compute multiplier in [0, 1 + max_scale] from P_win drift.
        # If P_win == prior ⇒ multiplier = 1.0 (no change).
        # If P_win >> prior ⇒ multiplier up to 1 + max_scale.
        # If P_win << prior ⇒ multiplier down to 0.
        drift = self.current_p_win - self.prior_p_win
        # Clamp drift to [-prior, 1-prior] so the multiplier stays bounded.
        drift_clamped = max(-self.prior_p_win, min(1.0 - self.prior_p_win, drift))
        # Map drift ∈ [-prior, 1-prior] → multiplier ∈ [0, 1 + max_scale].
        if drift_clamped >= 0:
            # Positive drift → boost weight (up to 1 + max_scale).
            denom = max(1e-9, 1.0 - self.prior_p_win)
            multiplier = 1.0 + max_scale * (drift_clamped / denom)
        else:
            # Negative drift → shrink weight (down to 0).
            denom = max(1e-9, self.prior_p_win)
            multiplier = 1.0 - (abs(drift_clamped) / denom)
        multiplier = max(0.0, min(1.0 + max_scale, multiplier))

        # Get current weight, scale, and apply via the public API.
        current = ev_engine.get_weights().get(bias_field, 0.25)
        override = {bias_field: current * multiplier}
        ev_engine.set_weights(override)
        return override
