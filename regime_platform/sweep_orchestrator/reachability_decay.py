"""
reachability_decay.py — is the cell's failure non-stationarity, and does recency fix it?
═══════════════════════════════════════════════════════════════════════════════════════

**Where this comes from.** `cell_conditioning.py` measured the reachability cell at
in-sample AUC 0.7256 but out-of-fold 0.4994 — real signal that does not transfer
across time. That admits two very different explanations:

  (a) **Non-stationarity.** The cell's rate is a real property, but it *moves* — so
      a rate estimated from all history is an average over regimes that no longer
      apply. Fix: estimate it from a trailing window.
  (b) **Overfitting.** The cell has no real signal; the in-sample number is
      memorisation of noise. Fix: none — the cell is not the model.

This module separates them, and it does so without touching live signal generation.

**Test 1 — does recency recover transfer?** Re-run the walk-forward, but fit each
fold's cell rates on a **trailing window** of the most recent ``W`` days rather than
on all history. Sweep ``W``. If some ``W`` transfers (out-of-fold AUC clears the
bar) where ``W = all`` does not, the failure is (a) and we learn the horizon. If no
``W`` transfers, it is (b).

**Test 2 — is the rate actually unstable?** Measure the cell rate's *own* stability,
independent of any model: split the history into eras and ask how much a cell's rate
moves between them. A stable rate that still fails to transfer points at (b) from a
different direction (the rate is stable but uninformative); a rate that swings wildly
is direct evidence of (a).

Both are needed. Test 1 says whether the fix works; test 2 says whether the diagnosis
is right. A window that "helps" while rates are perfectly stable would be a
coincidence, not a mechanism.

**Pure functions only** — no I/O, no clock. Rows must carry a numeric ``_ts``
(seconds); the CLI sets it.
"""

from __future__ import annotations

import statistics
from typing import Any, Callable, Sequence

from regime_platform.sweep_orchestrator.calibration_decomp import auc, decompose
from regime_platform.sweep_orchestrator.cell_conditioning import (
    DEFAULT_ALPHA,
    MIN_CELL_N,
    SPECS,
    fit,
    predict,
)

DAY = 86_400.0


# ─── Trailing-window splits ────────────────────────────────────────────────


def trailing_splits(
    rows: Sequence[dict[str, Any]],
    n_folds: int = 5,
    warmup_frac: float = 0.5,
    window_days: float | None = None,
) -> list[tuple[list[dict], list[dict]]]:
    """``(train, test)`` where train is the **trailing window** before each test block.

    ``rows`` must be sorted by ``_ts``. Test blocks tile the post-warmup range
    exactly as in ``cell_conditioning.walk_forward_splits``; the only difference is
    that ``train`` is restricted to ``[test_start − W days, test_start)``.

    ``window_days=None`` reproduces the expanding window (all history before the
    block) — the baseline that measured 0.4994.
    """
    n = len(rows)
    start = int(n * warmup_frac)
    if n - start < n_folds:
        return []
    block = (n - start) // n_folds
    if block < 1:
        return []
    out = []
    for i in range(n_folds):
        lo = start + i * block
        hi = start + (i + 1) * block if i < n_folds - 1 else n
        test = list(rows[lo:hi])
        if window_days is None:
            train = list(rows[:lo])
        else:
            cutoff = rows[lo]["_ts"] - window_days * DAY
            train = [r for r in rows[:lo] if r["_ts"] >= cutoff]
        out.append((train, test))
    return out


def evaluate_trailing(
    rows: Sequence[dict[str, Any]],
    levels: Sequence[Callable[[dict], Any]],
    *,
    window_days: float | None = None,
    n_folds: int = 5,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = MIN_CELL_N,
    warmup_frac: float = 0.5,
) -> dict[str, Any]:
    """Walk-forward with trailing-window fitting. Pools out-of-fold predictions."""
    splits = trailing_splits(rows, n_folds, warmup_frac, window_days)
    if not splits:
        return {"n": 0, "window_days": window_days}
    pooled: list[tuple[float, bool]] = []
    per_fold = []
    train_sizes = []
    for train, test in splits:
        train_sizes.append(len(train))
        if not train:
            continue
        tables = fit(train, levels, alpha, min_n)
        pairs = predict(tables, levels, test, alpha, min_n)
        if pairs:
            pooled.extend(pairs)
            per_fold.append({
                "n": len(pairs),
                "auc": round(auc(pairs), 4) if auc(pairs) is not None else None,
            })
    if not pooled:
        return {"n": 0, "window_days": window_days}
    d = decompose(pooled, n_bins=10)
    return {
        "n": len(pooled),
        "window_days": window_days,
        "auc": round(auc(pooled), 4) if auc(pooled) is not None else None,
        "brier": d["brier"],
        "bss": d["bss"],
        "base_rate": d["base_rate"],
        "mean_train_n": round(statistics.fmean(train_sizes), 1) if train_sizes else 0,
        "per_fold": per_fold,
    }


def window_sweep(
    rows: Sequence[dict[str, Any]],
    levels: Sequence[Callable[[dict], Any]],
    windows: Sequence[float | None] = (7.0, 14.0, 30.0, 60.0, None),
    *,
    n_folds: int = 5,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = MIN_CELL_N,
    warmup_frac: float = 0.5,
) -> list[dict[str, Any]]:
    """Out-of-fold AUC for each trailing-window length, plus the expanding baseline.

    Same folds, same shrinkage, same population — only the training *lookback*
    changes. So a difference between rows is attributable to recency alone.
    """
    out = []
    for w in windows:
        r = evaluate_trailing(rows, levels, window_days=w, n_folds=n_folds,
                              alpha=alpha, min_n=min_n, warmup_frac=warmup_frac)
        out.append(r)
    return out


def best_window(results: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """The window with the highest out-of-fold AUC (``None`` window = expanding)."""
    scored = [r for r in results if r.get("auc") is not None]
    if not scored:
        return None
    return max(scored, key=lambda r: r["auc"])


def recency_verdict(results: Sequence[dict[str, Any]], bar: float = 0.55) -> str:
    """Did any window make the cell transfer?

    Compares the best trailing window against the expanding baseline, because the
    question is whether *recency* adds — not whether some window is better by luck.
    """
    expanding = next((r for r in results if r.get("window_days") is None), None)
    trailing = [r for r in results if r.get("window_days") is not None
                and r.get("auc") is not None]
    if not trailing:
        return "no data"
    best = max(trailing, key=lambda r: r["auc"])
    base = expanding.get("auc") if expanding else None
    if best["auc"] >= bar:
        delta = f" vs expanding {base:.4f}" if base is not None else ""
        return (f"RECENCY HELPS — best trailing window {best['window_days']:.0f}d reaches "
                f"AUC {best['auc']:.4f}{delta}, clearing the {bar} bar")
    delta = f" (expanding {base:.4f})" if base is not None else ""
    return (f"RECENCY DOES NOT FIX IT — best trailing window "
            f"{best['window_days']:.0f}d reaches only AUC {best['auc']:.4f}{delta}; "
            f"no window clears the {bar} bar")


# ─── Rate stability (the mechanism) ────────────────────────────────────────


def era_rates(
    rows: Sequence[dict[str, Any]],
    n_eras: int = 4,
    min_n: int = MIN_CELL_N,
) -> tuple[list[dict[Any, float]], list[dict[Any, int]]]:
    """Per-era cell rates, on equal-*time* chunks of the (sorted) history.

    Equal time, not equal count: the question is whether a *calendar* period's rate
    predicts the next period's, so the chunks must be comparable in duration.
    """
    n = len(rows)
    if n_eras < 2 or n < n_eras * 2:
        return [], []
    size = n // n_eras
    rates: list[dict[Any, float]] = []
    counts: list[dict[Any, int]] = []
    for e in range(n_eras):
        lo = e * size
        hi = (e + 1) * size if e < n_eras - 1 else n
        agg: dict[Any, list[int]] = {}
        for r in rows[lo:hi]:
            k = r.get("_cell")
            if k is None:
                continue
            a = agg.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += 1 if r["realised"] else 0
        rates.append({k: w / c for k, (c, w) in agg.items() if c >= min_n})
        counts.append({k: c for k, (c, _) in agg.items() if c >= min_n})
    return rates, counts


def stability(rates: Sequence[dict[Any, float]], counts: Sequence[dict[Any, int]]) -> dict[str, Any]:
    """How much does a cell's rate move between consecutive eras?

    The direct test of non-stationarity. Reports:

      * ``mean_abs_drift`` — average |rate(e+1) − rate(e)| over cells present in
        both eras with adequate n. This is the headline number: how far a cell's
        rate typically travels from one era to the next.
      * ``mean_range`` — average (max − min) across all eras a cell appears in.
      * ``sign_flip_frac`` — share of consecutive pairs where the rate crosses 0.5,
        i.e. a cell that flips from majority-win to majority-loss.
      * ``split_half_corr`` — Pearson correlation of each cell's rate in the first
        half vs the second half. High = the rate is a stable property; low = it is
        not, and no amount of windowing makes it one.
    """
    drifts: list[float] = []
    for a, b in zip(rates, rates[1:]):
        for k in set(a) & set(b):
            drifts.append(abs(b[k] - a[k]))

    ranges: list[float] = []
    for k in {k for r in rates for k in r}:
        vals = [r[k] for r in rates if k in r]
        if len(vals) >= 2:
            ranges.append(max(vals) - min(vals))

    flips = 0
    pairs = 0
    for a, b in zip(rates, rates[1:]):
        for k in set(a) & set(b):
            pairs += 1
            if (a[k] - 0.5) * (b[k] - 0.5) < 0:
                flips += 1

    # split-half correlation over cells present in both halves
    half = len(rates) // 2
    first: dict[Any, list[float]] = {}
    second: dict[Any, list[float]] = {}
    for i, r in enumerate(rates):
        tgt = first if i < half else second
        for k, v in r.items():
            tgt.setdefault(k, []).append(v)
    xs, ys = [], []
    for k in set(first) & set(second):
        xs.append(statistics.fmean(first[k]))
        ys.append(statistics.fmean(second[k]))
    corr = None
    if len(xs) >= 3:
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = sum((x - mx) ** 2 for x in xs) ** 0.5
        dy = sum((y - my) ** 2 for y in ys) ** 0.5
        if dx > 0 and dy > 0:
            corr = num / (dx * dy)

    return {
        "n_cells": len({k for r in rates for k in r}),
        "mean_abs_drift": round(statistics.fmean(drifts), 4) if drifts else None,
        "mean_range": round(statistics.fmean(ranges), 4) if ranges else None,
        "sign_flip_frac": round(flips / pairs, 4) if pairs else None,
        "split_half_corr": round(corr, 4) if corr is not None else None,
        "n_consecutive_pairs": pairs,
    }


def stability_verdict(s: dict[str, Any], drift_bar: float = 0.10) -> str:
    """Mechanical read: is the cell rate a stable property?"""
    drift = s.get("mean_abs_drift")
    corr = s.get("split_half_corr")
    if drift is None:
        return "no data"
    if drift >= drift_bar:
        return (f"UNSTABLE — a cell's rate moves {drift:.3f} on average between eras "
                f"(bar {drift_bar}), split-half corr {corr}. The rate is not a fixed "
                f"property; any all-history estimate averages over regimes that no "
                f"longer apply.")
    return (f"STABLE — a cell's rate moves only {drift:.3f} between eras "
            f"(bar {drift_bar}), split-half corr {corr}. The rate is a real property; "
            f"if it still fails to transfer, the problem is elsewhere.")
