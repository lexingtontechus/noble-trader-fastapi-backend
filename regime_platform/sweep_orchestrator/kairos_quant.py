"""
kairos_quant.py — binding scorecard maths and era/cluster helpers.

Implements worklog/kairos/20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md
(D2, D4, D5, D6, D8) and V3 scope §15. Pure functions only: no I/O, no
Supabase, no trading side effects. Research scripts and live stamps share
these so era splits and no-skill maths cannot drift.

Era bounds (D4) are structural breaks in the data-generating process:
  pre_debounce     raw regime labels before the 3-tick debouncer (09-22)
  debounce_to_atr  debounced labels, pre hybrid/dynamic ATR (09-25)
  atr_to_resolver  ATR flip live, resolver still head-of-line (09-26)
  post_resolver    full-scan resolver + ATR live — the only clean era for
                   promoting calibration levers
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

# Structural breaks, UTC. Inclusive lower bounds; exclusive upper.
ERA_PRE_DEBOUNCE_END = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
ERA_ATR_START = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)
ERA_RESOLVER_START = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)

ERA_ORDER: tuple[str, ...] = (
    "pre_debounce",
    "debounce_to_atr",
    "atr_to_resolver",
    "post_resolver",
)

# Sweep ATR (close-to-close SMA14) is the live Kairos scale. P1 used OHLC ATR.
ATR_SCALE_LIVE = "sweep_cc_sma14"
ATR_SCALE_P1 = "ohlc_tr"


def era_of(ts: datetime | None) -> str:
    """Map a timestamp to one of ERA_ORDER. Naive timestamps are treated as UTC."""
    if ts is None:
        return "pre_debounce"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    if ts < ERA_PRE_DEBOUNCE_END:
        return "pre_debounce"
    if ts < ERA_ATR_START:
        return "debounce_to_atr"
    if ts < ERA_RESOLVER_START:
        return "atr_to_resolver"
    return "post_resolver"


def split_eras(timestamps: Iterable[datetime | None]) -> dict[str, list[int]]:
    """Indices of timestamps by era (stable order within each era)."""
    out: dict[str, list[int]] = {e: [] for e in ERA_ORDER}
    for i, ts in enumerate(timestamps):
        out[era_of(ts)].append(i)
    return out


def no_skill_line(tp_distance: float, sl_distance: float) -> float | None:
    """P(TP first | decided) under a coin-flip direction for that bracket.

    SL / (TP + SL). Both distances must be > 0. This is the only legal
    normaliser when comparing symbols, regimes, or geometries.
    """
    try:
        tp, sl = float(tp_distance), float(sl_distance)
    except (TypeError, ValueError):
        return None
    if not (tp > 0 and sl > 0):
        return None
    denom = tp + sl
    if denom <= 0:
        return None
    return sl / denom


def calibration_error(p_win: float, tp_first: bool) -> float | None:
    """p_win − 1{TP first}; positive = overconfident. Decided signals only."""
    try:
        p = float(p_win)
    except (TypeError, ValueError):
        return None
    return p - (1.0 if tp_first else 0.0)


def accuracy_vs_no_skill(tp_first: bool, tp_distance: float, sl_distance: float) -> float | None:
    """1{TP} − SL/(TP+SL). Positive = beat the bracket's coin-flip line."""
    ns = no_skill_line(tp_distance, sl_distance)
    if ns is None:
        return None
    return (1.0 if tp_first else 0.0) - ns


def claimed_edge(p_win: float, tp_distance: float, sl_distance: float) -> float | None:
    """p_win − no_skill: the stated edge against the bracket geometry."""
    ns = no_skill_line(tp_distance, sl_distance)
    if ns is None:
        return None
    try:
        return float(p_win) - ns
    except (TypeError, ValueError):
        return None


def symbol_day_clusters(ts_and_symbol: Sequence[tuple[datetime | None, str]]) -> int:
    """ESS proxy: distinct (symbol, UTC date) clusters. D5 reports this next to n."""
    seen: set[tuple[str, str]] = set()
    for ts, symbol in ts_and_symbol:
        if ts is None or not symbol:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        day = ts.astimezone(timezone.utc).date().isoformat()
        seen.add((symbol, day))
    return len(seen)


@dataclass(frozen=True)
class ClusterRisk:
    """D8 shadow: open risk already committed in a correlated cluster (Σ only)."""

    cluster: str
    open_kelly: float
    cap_mult: float = 1.5

    @property
    def cap(self) -> float:
        return self.cap_mult

    def allows(self, new_kelly: float, single_symbol_max: float = 1.0) -> bool:
        """Would-reject rule for a shadow cluster cap (never live-enforced here)."""
        if new_kelly < 0:
            return False
        return (self.open_kelly + new_kelly) <= self.cap * max(single_symbol_max, 0.0)


def pair_clusters(
    symbols: Sequence[str],
    corr: Sequence[Sequence[float]],
    threshold: float = 0.7,
) -> dict[str, str]:
    """Map symbol → cluster id from a correlation matrix (|ρ| ≥ threshold).

    Single-linkage on |ρ|. For risk budgets only; not a trading signal.
    """
    n = len(symbols)
    if n == 0:
        return {}
    if len(corr) != n or any(len(row) != n for row in corr):
        raise ValueError("corr must be n x n matching symbols")
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            try:
                rho = abs(float(corr[i][j]))
            except (TypeError, ValueError):
                continue
            if rho >= threshold:
                union(i, j)

    labels: dict[str, str] = {}
    for i, sym in enumerate(symbols):
        labels[sym] = f"c{find(i)}"
    return labels
