"""
common_factor.py — is the cell rate a cross-sectional property, or a market-wide one?
════════════════════════════════════════════════════════════════════════════════════

**Where this comes from.** `reachability_decay.py` found the cell rate moves a lot
between eras (mean |drift| 0.20) yet no trailing window recovers transfer. Those two
facts together are hard to explain with "each cell has its own drifting property",
but they fall out naturally if cell rates are **dominated by a shared, market-wide
factor**:

  * all cells load on the same driver -> they move together (high drift), and
  * the *ordering* between cells barely changes -> per-cell rates carry little
    ordering information, and
  * re-fitting on a recent window just re-estimates the same shared factor noisily.

This module tests that directly, by decomposing the cell-rate matrix.

**The decomposition.** Arrange rates as a matrix ``R[cell, window]`` (the rate of
each reachability cell in each time window, where the window has enough rows). The
standard two-way additive decomposition is

    R[c, w] = μ + a[c] + b[w] + e[c, w]

    μ       grand mean
    a[c]    cell effect        — the CROSS-SECTIONAL part (does this cell win more?)
    b[w]    window effect      — the COMMON factor   (does this period win more?)
    e[c,w]  residual           — cell×time interaction + noise

The variance shares answer the question:

  * ``share_time`` large  -> the rate is mostly a market-wide phenomenon.
  * ``share_cell`` large  -> the cell is a genuine cross-sectional property.
  * ``share_resid`` large -> neither; the rate is mostly noise.

**The predictive test.** Shares alone do not settle it — a big ``a[c]`` that is
unstable is still useless. So also compare two out-of-fold predictions:

  * **raw**      — rank by the cell's training rate (what the current model does)
  * **residual** — rank by ``a[c]`` alone, i.e. the cell's rate *after* removing the
                   training window's common factor

If residual beats raw, the common factor was hurting and a cross-sectional edge
exists. If both sit at 0.5, there is no cross-sectional edge — only a time-series one.

**Pure functions only** — no I/O, no clock.
"""

from __future__ import annotations

import statistics
from typing import Any, Sequence

from regime_platform.sweep_orchestrator.calibration_decomp import auc

DEFAULT_MIN_N = 20


# ─── Building the rate matrix ──────────────────────────────────────────────


def build_matrix(
    rows: Sequence[dict[str, Any]],
    *,
    window_seconds: float = 3 * 86_400.0,
    min_n: int = DEFAULT_MIN_N,
) -> dict[str, Any]:
    """``R[cell, window]`` from timestamped rows.

    ``rows`` need ``_ts`` (seconds), ``_cell`` (hashable key), ``realised`` (bool).
    A (cell, window) entry exists only when the window holds ``>= min_n`` rows for
    that cell — a 3-row window rate is noise, and including it would inflate the
    residual share with sampling variance rather than signal.
    """
    if not rows:
        return {"cells": [], "windows": [], "rates": {}, "counts": {}}
    t0 = min(r["_ts"] for r in rows)
    agg: dict[tuple, list[int]] = {}
    for r in rows:
        c = r.get("_cell")
        if c is None:
            continue
        w = int((r["_ts"] - t0) // window_seconds)
        a = agg.setdefault((c, w), [0, 0])
        a[0] += 1
        a[1] += 1 if r["realised"] else 0
    rates: dict[tuple, float] = {}
    counts: dict[tuple, int] = {}
    for (c, w), (n, wins) in agg.items():
        counts[(c, w)] = n
        if n >= min_n:
            rates[(c, w)] = wins / n
    cells = sorted({c for c, _ in rates}, key=lambda x: str(x))
    windows = sorted({w for _, w in rates})
    return {"cells": cells, "windows": windows, "rates": rates, "counts": counts}


def decompose_matrix(m: dict[str, Any]) -> dict[str, Any]:
    """Two-way additive decomposition of ``R[cell, window]``.

    Uses the **unweighted** cell/window means (each observed (cell, window) pair
    counts once). Weighting by n would let a few very large cells dominate the
    common factor, which is the opposite of what "market-wide" should mean here —
    the factor should be what a *typical* cell did in that window.
    """
    rates: dict[tuple, float] = m.get("rates") or {}
    if not rates:
        return {"n_obs": 0}
    vals = list(rates.values())
    mu = statistics.fmean(vals)

    cell_mean: dict[Any, list[float]] = {}
    win_mean: dict[int, list[float]] = {}
    for (c, w), v in rates.items():
        cell_mean.setdefault(c, []).append(v)
        win_mean.setdefault(w, []).append(v)

    a = {c: statistics.fmean(vs) - mu for c, vs in cell_mean.items()}
    b = {w: statistics.fmean(vs) - mu for w, vs in win_mean.items()}

    resid = []
    for (c, w), v in rates.items():
        resid.append(v - (mu + a[c] + b[w]))

    n = len(vals)
    var_total = statistics.fmean([(v - mu) ** 2 for v in vals])
    var_cell = statistics.fmean([a[c] ** 2 for (c, w) in rates])
    var_time = statistics.fmean([b[w] ** 2 for (c, w) in rates])
    var_resid = statistics.fmean([e ** 2 for e in resid])

    # The shares below are of the TOTAL sum of the three component variances, not of
    # var_total. On an UNBALANCED matrix (cells observed in different numbers of
    # windows) the two-way decomposition is not orthogonal: var_cell + var_time +
    # var_resid != var_total, and normalising by var_total produces shares that sum
    # to more than 1. Normalising by their own sum keeps them interpretable as
    # "which component carries the most of the explained variation".
    denom = var_cell + var_time + var_resid

    def share(x: float) -> float | None:
        return round(x / denom, 4) if denom > 0 else None

    return {
        "n_obs": n,
        "n_cells": len(cell_mean),
        "n_windows": len(win_mean),
        "grand_mean": round(mu, 4),
        "var_total": round(var_total, 5),
        "var_cell": round(var_cell, 5),
        "var_time": round(var_time, 5),
        "var_resid": round(var_resid, 5),
        "var_components_sum": round(denom, 5),
        # non-orthogonality residual: how far the component sum is from the total
        "orthogonality_gap": round(denom - var_total, 5),
        "share_cell": share(var_cell),
        "share_time": share(var_time),
        "share_resid": share(var_resid),
        "cell_effects": {k: round(v, 4) for k, v in sorted(
            a.items(), key=lambda kv: -kv[1])},
        "window_effects": {k: round(v, 4) for k, v in sorted(b.items())},
    }


def factor_verdict(d: dict[str, Any], dominant: float = 0.5) -> str:
    """Which component dominates the cell-rate variance?"""
    if not d.get("n_obs"):
        return "no data"
    sc, st, sr = d.get("share_cell"), d.get("share_time"), d.get("share_resid")
    if sc is None:
        return "no variance to decompose"
    parts = f"cell {sc:.0%}, time {st:.0%}, residual {sr:.0%}"
    if st >= dominant:
        return (f"COMMON FACTOR DOMINATES — {parts}. The rate is mostly a "
                f"market-wide phenomenon; a per-cell rate mostly re-expresses the "
                f"period, not the cell.")
    if sc >= dominant:
        return (f"CROSS-SECTIONAL — {parts}. The cell is a genuine property; a "
                f"per-cell rate carries real ordering information.")
    if sr >= dominant:
        return (f"MOSTLY NOISE — {parts}. Neither the cell nor the period explains "
                f"the rate; it is dominated by sampling variation.")
    return f"MIXED — {parts}. No single component dominates."


# ─── The predictive test ───────────────────────────────────────────────────


def residual_scores(
    m: dict[str, Any],
    train_windows: Sequence[int],
    *,
    min_n: int = DEFAULT_MIN_N,
) -> dict[Any, float]:
    """Cell rate with **each window's common factor removed**, then averaged.

    ``mean_w (R[c,w] − b[w])`` — not ``mu + a[c]``.

    That distinction is the whole point. ``mu + a[c]`` reduces to the plain cell
    mean (the raw score), because ``a[c]`` is *defined* as that mean minus the
    grand mean — so the two rankings would be identical by construction and the
    test would be vacuous. De-factoring **per window first, then averaging**
    corrects for *which periods the cell happened to appear in*: a cell seen only
    in good windows has an inflated raw rate, and this removes that. On perfectly
    balanced data the two coincide; the real matrix is badly unbalanced (162 cells
    over 12 windows), which is exactly when it matters.
    """
    rates = m.get("rates") or {}
    tw = set(train_windows)
    sub = {k: v for k, v in rates.items() if k[1] in tw}
    if not sub:
        return {}
    mu = statistics.fmean(sub.values())
    win_mean: dict[int, list[float]] = {}
    for (c, w), v in sub.items():
        win_mean.setdefault(w, []).append(v)
    b = {w: statistics.fmean(vs) - mu for w, vs in win_mean.items()}
    # per-cell average of the de-factored rates, level restored to the grand mean
    acc: dict[Any, list[float]] = {}
    for (c, w), v in sub.items():
        acc.setdefault(c, []).append(v - b[w])
    return {c: statistics.fmean(vs) for c, vs in acc.items()}


def raw_scores(m: dict[str, Any], train_windows: Sequence[int]) -> dict[Any, float]:
    """The cell's raw training rate — what the current model uses."""
    rates = m.get("rates") or {}
    tw = set(train_windows)
    acc: dict[Any, list[float]] = {}
    for (c, w), v in rates.items():
        if w in tw:
            acc.setdefault(c, []).append(v)
    return {c: statistics.fmean(vs) for c, vs in acc.items()}


def score_matrix(
    m: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    scores: dict[Any, float],
    windows: Sequence[int],
) -> list[tuple[float, bool]]:
    """``(score, realised)`` pairs for rows falling in the given windows."""
    ws = set(windows)
    t0 = min(r["_ts"] for r in rows) if rows else 0.0
    out = []
    for r in rows:
        c = r.get("_cell")
        if c is None or c not in scores:
            continue
        w = int((r["_ts"] - t0) // (m.get("_window_seconds") or 3 * 86_400.0))
        if w in ws:
            out.append((scores[c], bool(r["realised"])))
    return out


def compare_predictions(
    rows: Sequence[dict[str, Any]],
    *,
    window_seconds: float = 3 * 86_400.0,
    min_n: int = DEFAULT_MIN_N,
    n_folds: int = 4,
    warmup_frac: float = 0.5,
) -> dict[str, Any]:
    """Out-of-fold AUC of raw vs factor-removed cell scores.

    Both are ranked on the *same* rows and the *same* fold structure, so a
    difference is attributable to the factor removal alone.
    """
    m = build_matrix(rows, window_seconds=window_seconds, min_n=min_n)
    m["_window_seconds"] = window_seconds
    wins = m["windows"]
    if len(wins) < 4:
        return {"n_windows": len(wins), "raw": None, "residual": None}
    start = int(len(wins) * warmup_frac)
    blocks = []
    remaining = wins[start:]
    if not remaining:
        return {"n_windows": len(wins), "raw": None, "residual": None}
    size = max(1, len(remaining) // n_folds)
    for i in range(0, len(remaining), size):
        blocks.append(remaining[i:i + size])

    raw_pairs, res_pairs = [], []
    for blk in blocks:
        train = [w for w in wins if w < min(blk)]
        if not train:
            continue
        raw_pairs.extend(score_matrix(m, rows, raw_scores(m, train), blk))
        res_pairs.extend(score_matrix(m, rows, residual_scores(m, train), blk))

    ra = auc(raw_pairs) if raw_pairs else None
    re_ = auc(res_pairs) if res_pairs else None
    return {
        "n_windows": len(wins),
        "n_train_windows_first_fold": None,
        "raw": round(ra, 4) if ra is not None else None,
        "residual": round(re_, 4) if re_ is not None else None,
        "n_raw": len(raw_pairs),
        "n_residual": len(res_pairs),
        "raw_auc": round(ra, 4) if ra is not None else None,
        "residual_auc": round(re_, 4) if re_ is not None else None,
    }


def predictive_verdict(r: dict[str, Any], bar: float = 0.55) -> str:
    """Does removing the common factor reveal a cross-sectional edge?"""
    ra, re_ = r.get("raw_auc"), r.get("residual_auc")
    if ra is None or re_ is None:
        return "no data"
    if re_ >= bar and re_ > ra:
        return (f"RESIDUAL WINS — raw {ra:.4f} vs factor-removed {re_:.4f}: the common "
                f"factor was masking a real cross-sectional edge")
    if max(ra, re_) < bar:
        return (f"NO CROSS-SECTIONAL EDGE — raw {ra:.4f}, factor-removed {re_:.4f}; "
                f"neither clears {bar}. Ranking cells adds nothing once the period is "
                f"accounted for, so any edge here is time-series, not cross-sectional")
    return f"RAW WINS — raw {ra:.4f} vs factor-removed {re_:.4f}"
