"""
kairos_rank.py — ranking scores and regime-transition event helpers.

Pure functions for worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md.
No I/O. Shared by scripts/kairos_rank_transition.py and live stamps if needed.

Frozen targets (worklog §3.1):
  y_acc = 1{TP first} − SL/(TP+SL)     accuracy vs no-skill
  y_bin = 1{TP first}                  raw hit on decided signals
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from regime_platform.sweep_orchestrator.kairos_quant import no_skill_line

# Event-time windows relative to a transition at T0 (hours).
EVENT_WINDOWS: tuple[tuple[str, float, float], ...] = (
    ("pre", -6.0, 0.0),
    ("fresh", 0.0, 3.0),
    ("warm", 3.0, 12.0),
    ("stale", 12.0, 24.0),
    ("deep", 24.0, math.inf),
)

VOL_ORDER: tuple[str, ...] = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")


def event_window(hours_since_transition: float | None) -> str | None:
    """Label τ = hours since the regime-cell change. None if τ is None."""
    if hours_since_transition is None:
        return None
    try:
        tau = float(hours_since_transition)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(tau):
        return None
    for name, lo, hi in EVENT_WINDOWS:
        if lo <= tau < hi:
            return name
    return None


def transition_type(
    prev_vol: str | None,
    prev_dir: str | None,
    new_vol: str | None,
    new_dir: str | None,
    vol_order: Sequence[str] = ("low_vol", "med_low_vol", "med_high_vol", "high_vol"),
) -> str | None:
    """Classify a regime-cell change. None if either side is incomplete or nothing changed.

    Returns one of: dir_flip | vol_expand | vol_contract | both | none
    """
    if not prev_vol or not prev_dir or not new_vol or not new_dir:
        return None
    if prev_vol == new_vol and prev_dir == new_dir:
        return "none"
    dir_changed = prev_dir != new_dir
    try:
        i0, i1 = vol_order.index(prev_vol), vol_order.index(new_vol)
    except ValueError:
        # unknown tier strings — still classify direction
        vol_changed = prev_vol != new_vol
        vol_dir = "expand" if vol_changed else "hold"
    else:
        if i1 > i0:
            vol_dir = "expand"
        elif i1 < i0:
            vol_dir = "contract"
        else:
            vol_dir = "hold"
    if dir_changed and vol_dir != "hold":
        return "both"
    if dir_changed:
        return "dir_flip"
    if vol_dir == "expand":
        return "vol_expand"
    if vol_dir == "contract":
        return "vol_contract"
    return "none"


def surprise_from_counts(
    counts: dict[tuple[str, str], int],
    src: str,
    dst: str,
    prior: float = 1.0,
) -> float:
    """−log P(dst|src) from transition counts. Higher = rarer transition.

    Laplace prior on the row. Returns 0.0 if src unknown.
    """
    if not src or not dst:
        return 0.0
    row_total = sum(v for (s, _d), v in counts.items() if s == src)
    n_sd = counts.get((src, dst), 0)
    # add prior to every observed destination in the row (simple Laplace)
    n_dst = len({d for (s, d) in counts if s == src}) or 1
    denom = row_total + prior * n_dst
    if denom <= 0:
        return 0.0
    p = (n_sd + prior) / denom
    return -math.log(max(p, 1e-12))


def y_accuracy(tp_first: bool, tp_distance: float | None, sl_distance: float | None) -> float | None:
    """1{TP} − SL/(TP+SL). None if geometry missing."""
    ns = no_skill_line(tp_distance, sl_distance) if tp_distance and sl_distance else None
    if ns is None:
        return None
    return (1.0 if tp_first else 0.0) - ns


def shrink_cell_mean(
    values: Sequence[float],
    global_mean: float,
    k: float = 10.0,
) -> float | None:
    """James-Stein style shrink: (n*mean + k*global) / (n+k). Empty → global."""
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return float(global_mean)
    n = len(vals)
    mu = statistics.fmean(vals)
    return (n * mu + k * float(global_mean)) / (n + k)


def rank_ic(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation. None if n < 3 or either side constant."""
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None

    def ranks(v: Sequence[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for t in range(i, j + 1):
                r[order[t]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return num / (denx * deny)


def auc_within(labels: Sequence[bool], scores: Sequence[float]) -> float | None:
    """AUC of scores for binary labels (Mann-Whitney). None if degenerate."""
    pairs = [(bool(y), float(s)) for y, s in zip(labels, scores) if s is not None]
    pos = [s for y, s in pairs if y]
    neg = [s for y, s in pairs if not y]
    if not pos or not neg:
        return None
    # rank-based AUC (fast); ties get mid-rank
    combined = [(s, 1) for s in pos] + [(s, 0) for s in neg]
    combined.sort(key=lambda t: t[0])
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            ranks[t] = avg
        i = j + 1
    sum_pos = sum(ranks[k] for k, (_s, lab) in enumerate(combined) if lab == 1)
    n_pos, n_neg = len(pos), len(neg)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def recent_tp_rate(
    outcomes: Sequence[bool | None],
    half_life_h: float = 3.0,
    now_index: int | None = None,
) -> float | None:
    """Decay-weighted TP rate over prior decided outcomes (None = skip expired).

    outcomes[i] is True=TP, False=SL, None=not decided, in chronological order.
    Index distance is used as a proxy for hours if ages are not supplied.
    """
    decided = [(i, bool(o)) for i, o in enumerate(outcomes) if o is not None]
    if not decided:
        return None
    end = len(outcomes) if now_index is None else now_index
    prior = [(i, tp) for i, tp in decided if i < end]
    if not prior:
        return None
    wsum = 0.0
    tsum = 0.0
    for i, tp in prior:
        # treat each index as 1 hour back from end
        age_h = float(end - 1 - i)
        w = 0.5 ** (max(age_h, 0.0) / max(half_life_h, 1e-9))
        wsum += w
        tsum += w * (1.0 if tp else 0.0)
    if wsum <= 0:
        return None
    return tsum / wsum


def cluster_agree(
    flips: Sequence[tuple[str, str | None, float]],
    cluster_of: dict[str, str],
    window_h: float = 2.0,
) -> dict[str, float]:
    """Share of cluster-mates flipping to the same direction within ±window_h.

    flips: (symbol, new_dir, epoch_hours). Returns symbol → agreement share
    (0 if the symbol has no cluster-mates).
    """
    by_cluster: dict[str, list[tuple[str, str, float]]] = {}
    for sym, d, t in flips:
        if not sym or not d or t is None:
            continue
        c = cluster_of.get(sym, sym)
        by_cluster.setdefault(c, []).append((sym, d, float(t)))
    out: dict[str, float] = {}
    for c, items in by_cluster.items():
        for sym, d, t in items:
            mates = [x for x in items if x[0] != sym and abs(x[2] - t) <= window_h]
            if not mates:
                out[sym] = 0.0
                continue
            same = sum(1 for _s, dd, _t in mates if dd == d)
            out[sym] = same / len(mates)
    return out


@dataclass(frozen=True)
class RankRow:
    feature: str
    n: int
    rank_ic_y_acc: float | None
    auc_y_bin: float | None
    cell: str | None = None
