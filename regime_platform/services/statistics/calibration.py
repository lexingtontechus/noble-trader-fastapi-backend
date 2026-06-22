"""
Calibration Tracker — P_win Quality Monitoring (v4)
══════════════════════════════════════════════════════

Tracks predicted P_win vs. actual outcomes in decile bins.
Enables reliability diagrams, Brier score computation, and
early detection of overconfidence / underconfidence.

Usage
─────
    tracker = CalibrationTracker()

    # After each trade closes:
    tracker.record(p_win=0.72, outcome=True)

    # Periodically:
    report = tracker.report()
    print(report.brier_score)
    print(report.reliability_diagram)

Integration
───────────
Called by the Renko pipeline's trade journal when a trade closes.
Data is also persisted to ``nt_calibration_log`` via the
``POST /calibration/log`` endpoint for cross-session analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import mean
from typing import List, Optional

log = logging.getLogger("regime.calibration")


# ─── Data structures ─────────────────────────────────────────────────


@dataclass
class CalibrationBin:
    """A single decile bin tracking predictions and outcomes."""
    lower: float
    upper: float
    predictions: List[float] = field(default_factory=list)
    outcomes: List[bool] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.outcomes)

    @property
    def accuracy(self) -> float:
        """Observed win rate within this bin."""
        if not self.outcomes:
            return 0.5
        return sum(self.outcomes) / len(self.outcomes)

    @property
    def midpoint(self) -> float:
        """Midpoint of the bin range for plotting."""
        return (self.lower + self.upper) / 2.0

    @property
    def brier(self) -> float:
        """Mean squared error of predictions in this bin."""
        if not self.predictions:
            return 0.0
        return mean((p - o) ** 2 for p, o in zip(self.predictions, self.outcomes))


@dataclass
class CalibrationReport:
    """Summary of calibration quality across all bins."""
    n_total: int = 0
    brier_score: float = 0.0
    brier_skill_score: float = 0.0  # relative to naive 0.5 predictor
    reliability_diagram: List[dict] = field(default_factory=list)
    max_bias: float = 0.0
    is_overconfident: bool = False
    is_underconfident: bool = False

    @property
    def is_reliable(self) -> bool:
        """True if Brier skill score > 0 and |bias| < 0.05."""
        return self.brier_skill_score > 0 and abs(self.max_bias) < 0.05


# ─── Tracker ──────────────────────────────────────────────────────────


class CalibrationTracker:
    """
    Tracks P_win calibration across configurable decile bins.

    Thread-safe when each pipeline has its own instance.
    For cross-session persistence, use the ``POST /calibration/log`` API.

    Parameters
    ----------
    n_bins : int
        Number of equally-sized bins (default 10 → 10% deciles).
    """

    def __init__(self, n_bins: int = 10):
        if n_bins < 2 or n_bins > 100:
            raise ValueError(f"n_bins must be between 2 and 100, got {n_bins}")
        self.n_bins = n_bins
        self.bins: List[CalibrationBin] = [
            CalibrationBin(lower=i / n_bins, upper=(i + 1) / n_bins)
            for i in range(n_bins)
        ]

    def record(self, p_win: float, outcome: bool) -> None:
        """
        Record a single prediction-outcome pair.

        Parameters
        ----------
        p_win : float
            Predicted win probability (blended P_win at entry). Clamped to [0, 1].
        outcome : bool
            True if the trade was a winner (pnl > 0).
        """
        p_win = max(0.0, min(1.0, p_win))
        bin_idx = min(int(p_win * self.n_bins), self.n_bins - 1)
        self.bins[bin_idx].predictions.append(p_win)
        self.bins[bin_idx].outcomes.append(outcome)

    def report(self) -> CalibrationReport:
        """Compute calibration quality metrics across all bins."""
        all_predictions: List[float] = []
        all_outcomes: List[bool] = []

        diagram: List[dict] = []
        max_bias = 0.0

        for b in self.bins:
            if b.count == 0:
                continue
            all_predictions.extend(b.predictions)
            all_outcomes.extend(b.outcomes)
            bias = b.midpoint - b.accuracy
            max_bias = max(max_bias, abs(bias))
            diagram.append({
                "bin": f"{b.lower:.1f}-{b.upper:.1f}",
                "midpoint": round(b.midpoint, 3),
                "accuracy": round(b.accuracy, 3),
                "count": b.count,
                "bias": round(bias, 3),
            })

        n_total = len(all_outcomes)
        if n_total == 0:
            return CalibrationReport()

        # Brier score
        brier = mean(
            (p - o) ** 2
            for p, o in zip(all_predictions, all_outcomes)
        )

        # Brier skill score relative to naive 0.5 predictor
        naive_brier = mean(
            (0.5 - o) ** 2 for o in all_outcomes
        )
        skill = 1.0 - (brier / naive_brier) if naive_brier > 0 else 0.0

        return CalibrationReport(
            n_total=n_total,
            brier_score=round(brier, 4),
            brier_skill_score=round(skill, 4),
            reliability_diagram=diagram,
            max_bias=round(max_bias, 4),
            is_overconfident=max_bias > 0.05,
            is_underconfident=max_bias < -0.05,
        )

    def reset(self) -> None:
        """Clear all recorded predictions and outcomes."""
        for b in self.bins:
            b.predictions.clear()
            b.outcomes.clear()

    @property
    def n_total(self) -> int:
        return sum(b.count for b in self.bins)
