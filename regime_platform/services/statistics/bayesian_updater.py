"""
Bayesian Updater - live P_win updates based on in-session trade outcomes.

Uses Beta-Binomial conjugate updates with Laplace smoothing and a
consecutive-loss decay penalty. When consecutive losses exceed a
threshold, the system flags a should_pause condition to halt trading
until the session is reset.

Usage::

    updater = BayesianUpdater(prior_p_win=0.55)
    p = updater.observe_trade(is_win=True)   # P_win goes up
    p = updater.observe_trade(is_win=False)  # P_win goes down
    if updater.should_pause:
        # Pause trading
        updater.reset_session()
"""

from typing import List


class BayesianUpdater:
    """Live Bayesian P_win updates based on in-session trade outcomes."""
    
    def __init__(self, prior_p_win: float = 0.55, decay_rate: float = 0.10):
        self.prior_p_win = prior_p_win
        self.current_p_win = prior_p_win
        self.decay_rate = decay_rate  # Penalty per consecutive loss
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
        """Reset for a new trading session."""
        self.session_trades = []
        self.consecutive_losses = 0
        self.current_p_win = self.prior_p_win
    
    @property
    def session_win_rate(self) -> float:
        """Raw session win rate (no smoothing)."""
        if not self.session_trades:
            return self.prior_p_win
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
