"""
Markov Chain - tracks discrete price state transitions and computes transition probabilities.

Used by the EV Engine to compute P_markov, the probability of a favorable state
transition given the current price state.

States: UP, DOWN, FLAT (based on price change relative to brick_size)
Rolling window ensures the transition matrix adapts to recent market dynamics.
"""

from typing import Dict, List, Tuple


class MarkovChain:
    """Tracks discrete price state transitions and computes transition probabilities."""
    
    STATES = ["UP", "DOWN", "FLAT"]
    
    def __init__(self, brick_size: float, window: int = 200):
        self.brick_size = brick_size
        self.window = window
        self.counts: Dict[str, Dict[str, int]] = {
            s: {t: 0 for t in self.STATES} for s in self.STATES
        }
        self.transitions: List[Tuple[str, str]] = []
    
    def detect_state(self, prev_price: float, curr_price: float) -> str:
        """Classify price change into UP/DOWN/FLAT based on brick_size."""
        delta = curr_price - prev_price
        if delta > self.brick_size:
            return "UP"
        elif delta < -self.brick_size:
            return "DOWN"
        else:
            return "FLAT"
    
    def observe(self, from_state: str, to_state: str) -> None:
        """Record a state transition."""
        self.counts[from_state][to_state] += 1
        self.transitions.append((from_state, to_state))
        # Rolling window: trim old observations
        if len(self.transitions) > self.window:
            old_from, old_to = self.transitions.pop(0)
            self.counts[old_from][old_to] = max(0, self.counts[old_from][old_to] - 1)
    
    def get_transition_matrix(self) -> Dict[str, Dict[str, float]]:
        """Compute transition probabilities from observation counts."""
        T = {}
        for from_state in self.STATES:
            total = sum(self.counts[from_state].values())
            T[from_state] = {}
            for to_state in self.STATES:
                if total > 0:
                    T[from_state][to_state] = round(
                        self.counts[from_state][to_state] / total, 4
                    )
                else:
                    T[from_state][to_state] = 1.0 / len(self.STATES)  # Uniform prior
        return T
    
    def fit_from_prices(self, prices: List[float]) -> Dict[str, Dict[str, float]]:
        """Build transition matrix from a price series."""
        if len(prices) < 3:
            return self.get_transition_matrix()
        
        for i in range(1, len(prices)):
            from_state = self.detect_state(prices[i - 1], prices[i])
            to_state = (
                self.detect_state(prices[i], prices[i + 1])
                if i + 1 < len(prices)
                else "FLAT"
            )
            self.observe(from_state, to_state)
        return self.get_transition_matrix()
    
    def get_transition_prob(
        self,
        from_state: str,
        to_state: str = None,
        direction: str = None,
    ) -> float:
        """
        Get P(to_state | from_state), or P(direction | from_state) for long/short.
        
        When direction is provided:
          - "long"  -> target = "UP"
          - "short" -> target = "DOWN"
        """
        T = self.get_transition_matrix()
        if direction:
            target = "UP" if direction == "long" else "DOWN"
            return T.get(from_state, {}).get(target, 1.0 / 3)
        if to_state:
            return T.get(from_state, {}).get(to_state, 1.0 / 3)
        return 1.0 / 3
    
    @property
    def current_state(self) -> str:
        """Get the most recent observed state."""
        if not self.transitions:
            return "FLAT"
        return self.transitions[-1][1]
    
    @property
    def n_observations(self) -> int:
        """Number of transitions observed (within the rolling window)."""
        return len(self.transitions)
