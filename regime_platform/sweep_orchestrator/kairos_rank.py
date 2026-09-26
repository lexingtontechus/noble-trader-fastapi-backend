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
from typing import Any, Iterable, Sequence

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


# ─── Composite rank score (worklog §8.5 / production score) ──────────────
# Weights are the R1 event-time / type contrasts, frozen 2026-09-26 before
# walk-forward scoring. Not re-fit per day (overfit risk).

FRESHNESS_W: dict[str, float] = {
    "fresh": 1.00,
    "warm": 0.55,
    "stale": 0.20,
    "pre": 0.40,
    "deep": 0.00,
}
TYPE_W: dict[str, float] = {
    "dir_flip": 1.00,
    "both": 0.65,
    "none": 0.35,
    "vol_expand": 0.25,
    "vol_contract": 0.25,
}
DEFAULT_RANK_W = {
    "freshness": 0.35,
    "type": 0.25,
    "recent_tp": 0.25,
    "cell_p": 0.15,
}


def freshness_score(window: str | None) -> float:
    """0–1 information freshness from the event-time window (R1)."""
    if not window:
        return 0.35
    return float(FRESHNESS_W.get(window, 0.35))


def type_score(ttype: str | None) -> float:
    """0–1 transition-type prior (R1: dir_flip only non-negative)."""
    if not ttype:
        return 0.35
    return float(TYPE_W.get(ttype, 0.35))


def clip01(x: float | None, default: float = 0.5) -> float:
    if x is None:
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(0.0, min(1.0, v))


def rank_score(
    *,
    event_window: str | None,
    transition: str | None,
    recent_tp_rate: float | None,
    cell_p: float | None,
    weights: dict[str, float] | None = None,
) -> float:
    """Composite 0–1 ranking score (higher = more likely to beat no-skill).

    Inputs must be known at signal time. `cell_p` is a shrunk empirical
    P(TP|decided) for the cell (not the live p_win blend).
    """
    w = dict(DEFAULT_RANK_W)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    total = sum(w.values()) or 1.0
    parts = {
        "freshness": freshness_score(event_window),
        "type": type_score(transition),
        "recent_tp": clip01(recent_tp_rate, 0.35),
        "cell_p": clip01(cell_p, 0.35),
    }
    return sum(w[k] * parts[k] for k in w) / total


def shadow_entry_gate(score: float, threshold: float = 0.55) -> bool:
    """Would-publish rule for shadow stamping only (never live-enforced here)."""
    try:
        return float(score) >= float(threshold)
    except (TypeError, ValueError):
        return False


def delta_d(y_acc_gated: Sequence[float | None], y_acc_rest: Sequence[float | None]) -> float | None:
    """ΔD = mean(y_acc | gated) − mean(y_acc | rest). Selection metric (worklog §4)."""
    g = [float(v) for v in y_acc_gated if v is not None]
    r = [float(v) for v in y_acc_rest if v is not None]
    if not g or not r:
        return None
    return statistics.fmean(g) - statistics.fmean(r)


def delta_d_bootstrap(
    y_acc_gated: Sequence[float | None],
    y_acc_rest: Sequence[float | None],
    n_boot: int = 500,
    seed: int = 20260926,
) -> tuple[float | None, float | None, float | None]:
    """(ΔD, lo, hi) with a simple iid bootstrap on the two arms."""
    point = delta_d(y_acc_gated, y_acc_rest)
    if point is None:
        return (None, None, None)
    g = [float(v) for v in y_acc_gated if v is not None]
    r = [float(v) for v in y_acc_rest if v is not None]
    if len(g) < 5 or len(r) < 5:
        return (point, None, None)
    try:
        import numpy as np
    except ImportError:
        return (point, None, None)
    rng = np.random.default_rng(seed)
    stats_: list[float] = []
    for _ in range(n_boot):
        a = rng.choice(g, size=len(g), replace=True)
        b = rng.choice(r, size=len(r), replace=True)
        stats_.append(float(a.mean() - b.mean()))
    return (point, float(np.percentile(stats_, 2.5)), float(np.percentile(stats_, 97.5)))


# ─── Live shadow tracker (migration 172) ─────────────────────────────────


class RankScoreTracker:
    """Trailing decided outcomes + regime bookkeeping for the shadow stamp.

    ``score()`` uses only state from *before* the current tick (no lookahead).
    ``observe_outcome()`` is called later when TP/SL is known (resolver /
    kairos-daily). ``note_regime()`` feeds event-time (τ, transition type)
    from the live label stream.

    Restart-safe via to_dict/from_dict (regime_state_store).
    """

    def __init__(self, max_recent: int = 200) -> None:
        self.max_recent = int(max_recent)
        self._recent: dict[str, list[tuple[float, bool]]] = {}
        self._cell: dict[tuple[str, str], list[float]] = {}
        self._last_cell: dict[str, str] = {}
        self._last_change_ts: dict[str, float] = {}
        self._last_ttype: dict[str, str] = {}

    def note_regime(self, symbol: str, cell: str | None, now_ts: float) -> tuple[str | None, float | None]:
        """Record the current regime cell. Returns (transition_type, tau_h)."""
        if not symbol or not cell:
            return None, None
        prev = self._last_cell.get(symbol)
        now_ts = float(now_ts)
        if prev is None:
            self._last_cell[symbol] = cell
            self._last_change_ts[symbol] = now_ts
            return None, 0.0
        if prev != cell:
            prev_vol, prev_dir = (prev.split("|", 1) + [None])[:2]
            new_vol, new_dir = (cell.split("|", 1) + [None])[:2]
            ttype = transition_type(prev_vol, prev_dir, new_vol, new_dir)
            self._last_cell[symbol] = cell
            self._last_change_ts[symbol] = now_ts
            if ttype:
                self._last_ttype[symbol] = ttype
            return ttype, 0.0
        t0 = self._last_change_ts.get(symbol, now_ts)
        return self._last_ttype.get(symbol), (now_ts - t0) / 3600.0

    def observe_outcome(self, symbol: str, cell: str | None, tp_first: bool, ts: float) -> None:
        """Ingest a decided outcome (TP/SL). Expired outcomes are not passed in."""
        if not symbol:
            return
        ts = float(ts)
        rec = self._recent.setdefault(symbol, [])
        rec.append((ts, bool(tp_first)))
        if len(rec) > self.max_recent:
            del rec[: len(rec) - self.max_recent]
        if cell:
            self._cell.setdefault((symbol, cell), []).append(1.0 if tp_first else 0.0)

    def _recent_tp(self, symbol: str, now_ts: float, half_life_h: float = 3.0) -> float | None:
        rec = self._recent.get(symbol) or []
        wsum = tsum = 0.0
        for ts, tp in rec:
            age_h = max(0.0, (float(now_ts) - ts) / 3600.0)
            w = 0.5 ** (age_h / max(half_life_h, 1e-9))
            wsum += w
            tsum += w * (1.0 if tp else 0.0)
        if wsum <= 0:
            return None
        return tsum / wsum

    def _cell_p(self, symbol: str, cell: str | None, k: float = 10.0) -> float | None:
        if not cell:
            return None
        vals = self._cell.get((symbol, cell)) or []
        if not vals:
            # no observations in this cell yet — do not invent a prior
            return None
        all_vals = [v for key, vs in self._cell.items() if key[0] == symbol for v in vs]
        if not all_vals:
            all_vals = [v for vs in self._cell.values() for v in vs]
        global_mean = statistics.fmean(all_vals) if all_vals else 0.3
        return shrink_cell_mean(vals, global_mean, k=k)

    def score(
        self,
        symbol: str,
        cell: str | None,
        *,
        transition: str | None = None,
        tau_h: float | None = None,
        now_ts: float | None = None,
        weights: dict[str, float] | None = None,
    ) -> float | None:
        """Composite rank score from trailing state. None if no trailing evidence."""
        if not symbol:
            return None
        now_ts = float(now_ts if now_ts is not None else 0.0)
        if tau_h is None and symbol in self._last_change_ts and now_ts > 0:
            tau_h = (now_ts - self._last_change_ts[symbol]) / 3600.0
        if transition is None:
            transition = self._last_ttype.get(symbol)
        ewin = event_window(tau_h)
        rec = self._recent_tp(symbol, now_ts) if now_ts > 0 else None
        cell_p = self._cell_p(symbol, cell)
        if rec is None and cell_p is None:
            return None
        return rank_score(
            event_window=ewin,
            transition=transition,
            recent_tp_rate=rec,
            cell_p=cell_p,
            weights=weights,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent": {s: [[t, bool(tp)] for t, tp in v] for s, v in self._recent.items()},
            "cell": {f"{s}|{c}": list(vs) for (s, c), vs in self._cell.items()},
            "last_cell": dict(self._last_cell),
            "last_change_ts": dict(self._last_change_ts),
            "last_ttype": dict(self._last_ttype),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RankScoreTracker":
        obj = cls()
        d = d or {}
        for s, rows in (d.get("recent") or {}).items():
            try:
                obj._recent[str(s)] = [(float(t), bool(tp)) for t, tp in rows]
            except (TypeError, ValueError):
                continue
        for key, vs in (d.get("cell") or {}).items():
            if "|" not in str(key):
                continue
            sym, cell = str(key).split("|", 1)
            try:
                obj._cell[(sym, cell)] = [float(v) for v in vs]
            except (TypeError, ValueError):
                continue
        obj._last_cell = {str(k): str(v) for k, v in (d.get("last_cell") or {}).items()}
        obj._last_ttype = {str(k): str(v) for k, v in (d.get("last_ttype") or {}).items()}
        try:
            obj._last_change_ts = {str(k): float(v) for k, v in (d.get("last_change_ts") or {}).items()}
        except (TypeError, ValueError):
            obj._last_change_ts = {}
        return obj


def ingest_decided_rows(tracker: RankScoreTracker, rows: Sequence[dict[str, Any]]) -> int:
    """Feed decided TP/SL rows into the tracker (kairos-daily).

    Each row needs: symbol, regime, outcome, ts (or exit_ts). Returns n ingested.
    """
    from regime_platform.sweep_orchestrator.kairos import regime_cell

    n = 0
    for r in rows or []:
        outcome = r.get("outcome")
        if outcome not in ("tp_hit", "sl_hit"):
            continue
        sym = r.get("symbol")
        if not sym:
            continue
        cell = regime_cell(r.get("regime")) or r.get("regime")
        ts = r.get("exit_ts") or r.get("ts") or r.get("resolved_at")
        try:
            # PostgREST may return ISO strings
            if isinstance(ts, str):
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            ts_f = float(ts)
        except (TypeError, ValueError):
            continue
        tracker.observe_outcome(sym, str(cell) if cell else None, outcome == "tp_hit", ts_f)
        n += 1
    return n
