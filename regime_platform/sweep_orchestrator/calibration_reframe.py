"""Calibration + reachability reframe — the fused-p_win decomposition.

══════════════════════════════════════════════════════════════════════════
2026-10-11. worklog/20261011_CALIBRATION_REACHABILITY_REFRAME.md.
Roadmap: calibration_reachability_reframe.

THE DEFECT
──────────
The platform prices every signal with ONE fused number:

    p_win  =  the blend (regime + markov + timesfm) — a direction read.

It is then scored by calibration against the realized win rate. Two problems,
both confirmed live (era-clean, 2026-10-11):

  1. FUSION. The fused p_win mashes together TWO independent probabilities:
       p_direction      — was the regime/trend/vol read honest (given the
                          bracket resolved at all)?
       p_reachability   — was the bracket winnable given geometry + expiry?
     A +0.53 average bias (AUDUSD +0.63 … AAPL +0.21, NO symbol calibrated)
     cannot tell you which parent is broken, so the shrink-toward-0.5 penalty
     fixes neither: it moves 0.68 → 0.59 when the truth is 0.05.

  2. WRONG POPULATION. Calibration scores only RESOLVED signals (win/loss)
     and deliberately EXCLUDES expiry. But the all-in book is 12.6% TP /
     76.6% SL / 10.9% expired. So a cell can read "calibrated" on its
     resolved subset while the all-in book is a loss machine — the
     "calibrated but large losses" the user observed. Calibration measures a
     different population than the one that loses money.

THE REFRAME
───────────
  * Split the fused number: p_win = p_direction × p_reachability (clamped).
  * Score calibration on the ALL-IN population with THREE outcomes
    (tp / sl / expired), not two.
  * A cell is CALIBRATED only when the published p_win matches the all-in
    reachability-conditioned base rate for that cell — not the resolved-only
    rate, and not the direction-only blend.

This module is PURE: given cell counts (tp/sl/expired), the published p_win,
and the geometry, it returns the decomposition + a 3-outcome calibration
verdict. No I/O, no state — so the E2E/replay tests pin it deterministically.

SHADOW / FAIL-OPEN. This module computes; it does NOT change live EV/Kelly or
the enforcement path on its own. It returns verdicts the sweep can stamp for
measurement; the behavioral swap (p_win → p_reach, retire the shrink penalty)
is a separate gated flip, one cohort at a time, after the shadow data proves
bias → 0.
══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# A cell is only adjudicated once it has this many all-in episodes.
DEFAULT_MIN_N = 20

# |bias| within this band (on the all-in base rate) = calibrated.
CALIBRATED_BAND = 0.05

# no_skill_line = SL / (TP + SL): the bracket's coin-flip resolved win rate.
# Imported lazily to avoid a cycle (kairos_quant imports nothing from here).


@dataclass(frozen=True)
class CellCalibration:
    """3-outcome, all-in calibration verdict for one cell."""

    # all-in population
    n: int
    tp: int
    sl: int
    expired: int
    tp_share: float          # tp / n           (all-in win rate)
    sl_share: float          # sl / n
    expired_share: float     # expired / n
    resolved_n: int          # tp + sl
    resolved_tp_share: float  # tp / (tp + sl)  (the OLD calibration's WR)

    # decomposition
    p_reachability: float    # ≈ all-in tp share (what the bracket delivers)
    p_direction: float       # resolved_tp_share (direction skill, given resolve)
    p_win_fused_rebuilt: float  # p_direction × p_reachability
    published_p_win: float   # what the blend published
    bias_all_in: float       # published_p_win − tp_share (the honest bias)
    bias_resolved_only: float   # published_p_win − resolved_tp_share (old)

    # verdict
    status: str              # CALIBRATED | OVERCONFIDENT | UNDERCONFIDENT | THIN
    claimed_edge: Optional[float]   # p_win − no_skill_line (vs geometry coin-flip)
    no_skill_line: Optional[float]

    def as_log(self) -> str:
        ce = f"{self.claimed_edge:+.3f}" if self.claimed_edge is not None else "n/a"
        return (
            f"n={self.n} all-in tp/sl/exp={self.tp_share:.2f}/"
            f"{self.sl_share:.2f}/{self.expired_share:.2f} "
            f"resolved_wr={self.resolved_tp_share:.2f} "
            f"p_dir={self.p_direction:.2f} p_reach={self.p_reachability:.2f} "
            f"pub={self.published_p_win:.2f} bias_allin={self.bias_all_in:+.3f} "
            f"bias_resolved={self.bias_resolved_only:+.3f} "
            f"no_skill={self.no_skill_line} claimed_edge={ce} → {self.status}"
        )


def _clamp(p: float) -> float:
    return min(max(float(p), 0.0), 1.0)


def cell_calibration(
    *,
    tp: int,
    sl: int,
    expired: int,
    published_p_win: float,
    tp_distance: Optional[float] = None,
    sl_distance: Optional[float] = None,
    min_n: int = DEFAULT_MIN_N,
) -> CellCalibration:
    """All-in 3-outcome calibration verdict for one cell.

    tp/sl/expired are the cell's all-in episode counts (NOT resolved-only).
    published_p_win is the fused blend the signal published for this cell.

    The verdict is OVERCONFIDENT when published_p_win exceeds the ALL-IN
    reachability base rate (tp/n) by more than the band — this is the number
    that actually loses money, not the resolved-only rate. claimed_edge uses
    the geometry's own coin-flip line (no_skill = SL/(TP+SL)) so a signal is
    judged against what the bracket geometry alone would deliver.
    """
    n = int(tp) + int(sl) + int(expired)
    tp_s = int(tp)
    sl_s = int(sl)
    exp_s = int(expired)
    resolved = tp_s + sl_s

    tp_share = (tp_s / n) if n else 0.0
    sl_share = (sl_s / n) if n else 0.0
    exp_share = (exp_s / n) if n else 0.0
    resolved_tp_share = (tp_s / resolved) if resolved else 0.0

    # Decomposition: reachability ≈ what the bracket delivers (all-in tp);
    # direction ≈ skill given the bracket resolved at all (resolved tp share).
    p_reachability = tp_share
    p_direction = resolved_tp_share
    p_win_fused_rebuilt = _clamp(p_direction * p_reachability)

    pub = float(published_p_win)
    bias_all_in = round(pub - tp_share, 4)
    bias_resolved_only = round(pub - resolved_tp_share, 4)

    ns_line = None
    claimed = None
    if tp_distance is not None and sl_distance is not None:
        try:
            from regime_platform.sweep_orchestrator.kairos_quant import no_skill_line
            ns_line = no_skill_line(float(tp_distance), float(sl_distance))
            if ns_line is not None:
                claimed = round(pub - ns_line, 4)
        except Exception:  # noqa: BLE001
            ns_line = None

    if n < min_n:
        status = "THIN"
    elif abs(bias_all_in) <= CALIBRATED_BAND:
        status = "CALIBRATED"
    elif bias_all_in > 0:
        status = "OVERCONFIDENT"
    else:
        status = "UNDERCONFIDENT"

    return CellCalibration(
        n=n, tp=tp_s, sl=sl_s, expired=exp_s,
        tp_share=round(tp_share, 4), sl_share=round(sl_share, 4),
        expired_share=round(exp_share, 4),
        resolved_n=resolved, resolved_tp_share=round(resolved_tp_share, 4),
        p_reachability=round(p_reachability, 4),
        p_direction=round(p_direction, 4),
        p_win_fused_rebuilt=round(p_win_fused_rebuilt, 4),
        published_p_win=round(pub, 4),
        bias_all_in=bias_all_in,
        bias_resolved_only=bias_resolved_only,
        status=status,
        claimed_edge=claimed,
        no_skill_line=round(ns_line, 4) if ns_line is not None else None,
    )


def reframe_reachability_row(row: dict[str, Any]) -> CellCalibration:
    """Adjudicate one live ``nt_bracket_reachability`` row (all-in shares).

    The reachability table already stores all-in shares (pct_hit_tp /
    pct_hit_sl / pct_expired), so reconstruct counts from n × share and pass
    the published p_win (avg_p_win for the cell, or a supplied value). Used by
    the replay/backfill to verdict the whole era-clean book in one pass.
    """
    n = int(row.get("n") or 0)
    tp_share = float(row.get("pct_hit_tp") or 0.0)
    sl_share = float(row.get("pct_hit_sl") or 0.0)
    exp_share = float(row.get("pct_expired") or 0.0)
    # Reconstruct integer-ish counts; keep them as floats but the model wants
    # counts — round to nearest while preserving n.
    tp = round(n * tp_share)
    sl = round(n * sl_share)
    expired = max(n - tp - sl, 0)
    pub = float(row.get("avg_p_win") or row.get("published_p_win") or 0.0)
    return cell_calibration(
        tp=int(tp), sl=int(sl), expired=int(expired),
        published_p_win=pub,
        tp_distance=row.get("tp_distance_atr"),
        sl_distance=row.get("sl_distance_atr"),
        min_n=int(row.get("min_n") or DEFAULT_MIN_N),
    )
