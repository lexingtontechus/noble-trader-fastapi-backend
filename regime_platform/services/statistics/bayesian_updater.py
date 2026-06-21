"""
Bayesian Updater - live P_win updates based on in-session trade outcomes.

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

from typing import List, Optional


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
        """
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
