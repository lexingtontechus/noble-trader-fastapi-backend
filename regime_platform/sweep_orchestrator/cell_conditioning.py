"""
cell_conditioning.py — does conditioning the reachability cell improve AUC?
═══════════════════════════════════════════════════════════════════════════════

Scope: `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` §3.3.

**The thesis under test.** ``P(reach TP before SL)`` is dominated by the geometry
cell ``(symbol, vol_tier, distance/ATR)`` — so the reachability cell is the model
and the direction signals are at most *conditioning terms* on it.

**The falsifiable question.** Does conditioning the cell on a direction signal
(regime trend, TimesFM, the live ``p_win`` blend, the trade's own side) improve
**out-of-fold AUC** over the cell alone?

**The trap this module is built to avoid.** Adding conditioning *always* improves
in-sample fit: a finer partition has more cells and more freedom to memorise. The
only honest test is out-of-fold, so:

  * every cell rate is estimated from **training rows only**;
  * the evaluation fold is scored with those frozen rates;
  * folds are **time-ordered** (expanding window) — a random split leaks the
    future into the past, which is the failure mode the repo's own quant audit
    flags (R4 look-ahead, and the kairos walk-forward discipline).

**Backoff, because finer cells are sparser.** Each model is a *ladder* of key
ladders, coarsest-last. A row's probability is taken from the finest level that
has enough support; otherwise it falls back. Each level is Beta-shrunk toward its
parent level's rate, so a cell with 3 observations cannot claim certainty:

    p = (wins + α·parent) / (n + α)          α = shrinkage strength

This is standard hierarchical smoothing (the same shape as ``p_win_bracket``'s
``β(2,2)`` prior, generalized to a ladder).

**Pure functions only** — no I/O, no clock. The CLI fetches.
"""

from __future__ import annotations

import statistics
from typing import Any, Callable, Sequence

from regime_platform.sweep_orchestrator.calibration_decomp import auc, decompose

# Shrinkage strength at every level. Higher = more pull toward the parent.
DEFAULT_ALPHA = 10.0

# A level is only used if it has at least this many training rows; otherwise the
# ladder drops to the next coarser level. Prevents a 1-row cell from winning.
MIN_CELL_N = 20


# ─── Row keys ──────────────────────────────────────────────────────────────


def cell_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(symbol, vol_tier, distance/ATR bucket)`` — the reachability cell.

    Reuses ``bracket_reachability``'s own bucketing and vol-tier extraction so
    this test cannot drift from what the live table means. Returns ``None`` when
    ATR / prices are missing (the row cannot be placed in a cell).
    """
    from regime_platform.sweep_orchestrator.bracket_reachability import (
        _bucket_for,
        _vol_tier,
    )

    atr = row.get("atr")
    entry = row.get("entry_price")
    tp = row.get("take_profit")
    sym = row.get("symbol")
    if not sym or not atr or entry is None or tp is None:
        return None
    try:
        atr = float(atr)
        ratio = abs(float(tp) - float(entry)) / atr
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    bucket = _bucket_for(ratio)
    if bucket is None:
        return None
    return (str(sym), _vol_tier(row.get("regime")), bucket)


def trend_state(regime_label: str | None) -> str:
    """The trend half of ``f"{vol}_vol_{trend}"`` — ``med_low_vol_strong_bear``
    -> ``strong_bear``. ``unknown`` when unparseable."""
    if not regime_label or "_vol_" not in regime_label:
        return "unknown"
    return regime_label.split("_vol_", 1)[1] or "unknown"


def p_win_band(row: dict[str, Any]) -> str:
    """Coarse band of the live ``p_win`` blend — a conditioning term.

    Coarse on purpose: the question is whether the *blend carries information
    about the bracket beyond geometry*, not whether a fine version of it does.
    Bands chosen to separate the overconfident top end from the calibrated middle.
    """
    p = row.get("p_win")
    if p is None:
        return "unknown"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "unknown"
    for lo, label in ((0.7, "hi"), (0.6, "mid_hi"), (0.5, "mid"), (0.4, "mid_lo")):
        if p >= lo:
            return label
    return "lo"


# ─── Model specs: a ladder of key extractors, finest first ─────────────────

# Each spec is (name, [level_fn, ...]) where level_fn(row) -> hashable key.
# The LAST level is always the global key (a constant) so the ladder always
# terminates.

def _lvl(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
    return fn


SPECS: dict[str, list[Callable[[dict], Any]]] = {
    # M0 — the cell alone. The baseline every other model must beat.
    "cell": [
        lambda r: cell_key(r),
        lambda r: (cell_key(r) or ("?",))[:2] if cell_key(r) else None,   # symbol, vol_tier
        lambda r: (cell_key(r) or ("?",))[0] if cell_key(r) else None,    # symbol
        lambda r: "global",
    ],
    # M1 — cell + the trade's own side.
    "cell+dir": [
        lambda r: (cell_key(r), r.get("signal")) if cell_key(r) else None,
        lambda r: cell_key(r),
        lambda r: (cell_key(r) or ("?",))[0] if cell_key(r) else None,
        lambda r: "global",
    ],
    # M2 — cell + the regime trend state.
    "cell+trend": [
        lambda r: (cell_key(r), trend_state(r.get("regime"))) if cell_key(r) else None,
        lambda r: cell_key(r),
        lambda r: (cell_key(r) or ("?",))[0] if cell_key(r) else None,
        lambda r: "global",
    ],
    # M3 — cell + the live p_win blend (the thing being challenged).
    "cell+p_win": [
        lambda r: (cell_key(r), p_win_band(r)) if cell_key(r) else None,
        lambda r: cell_key(r),
        lambda r: (cell_key(r) or ("?",))[0] if cell_key(r) else None,
        lambda r: "global",
    ],
    # M4 — cell + both direction terms (the fullest direction conditioning).
    "cell+dir+trend": [
        lambda r: (cell_key(r), r.get("signal"), trend_state(r.get("regime")))
        if cell_key(r) else None,
        lambda r: cell_key(r),
        lambda r: (cell_key(r) or ("?",))[0] if cell_key(r) else None,
        lambda r: "global",
    ],
}


# ─── Fit / predict ─────────────────────────────────────────────────────────


def fit(rows: Sequence[dict[str, Any]], levels: Sequence[Callable[[dict], Any]],
        alpha: float = DEFAULT_ALPHA, min_n: int = MIN_CELL_N) -> list[dict[Any, tuple[float, int]]]:
    """Count wins/n per level, **coarsest level first** so each finer level can
    be shrunk toward its parent.

    Returns one ``{key: (wins, n)}`` dict per level, in the same order as
    ``levels`` (finest first). The parent of level ``i`` is level ``i+1``.
    """
    tables: list[dict[Any, tuple[float, int]]] = []
    for fn in levels:
        t: dict[Any, tuple[float, int]] = {}
        for r in rows:
            k = fn(r)
            if k is None:
                continue
            y = 1.0 if r["realised"] else 0.0
            w, n = t.get(k, (0.0, 0))
            t[k] = (w + y, n + 1)
        tables.append(t)
    return tables


def _rate(tables, levels, row, alpha, min_n) -> float | None:
    """Walk the ladder from coarsest to finest, shrinking each level toward the
    parent's rate. The finest level with ``n >= min_n`` supplies the answer."""
    # coarsest -> finest
    parent_rate: float | None = None
    out: float | None = None
    for i in range(len(levels) - 1, -1, -1):
        k = levels[i](row)
        if k is None:
            continue
        w, n = tables[i].get(k, (0.0, 0))
        if parent_rate is None:
            # global level: Beta(1,1) prior = 0.5 when the global cell is empty
            parent_rate = (w + 1.0) / (n + 2.0) if n > 0 else 0.5
            if i == 0:
                out = parent_rate
            continue
        rate = (w + alpha * parent_rate) / (n + alpha) if (n + alpha) > 0 else parent_rate
        parent_rate = rate
        if i == 0:
            out = rate
        elif n >= min_n:
            out = rate
    return out


def predict(tables, levels, rows, alpha=DEFAULT_ALPHA, min_n=MIN_CELL_N) -> list[tuple[float, bool]]:
    """``(predicted, realised)`` pairs for every placeable row."""
    out: list[tuple[float, bool]] = []
    for r in rows:
        p = _rate(tables, levels, r, alpha, min_n)
        if p is None:
            continue
        out.append((min(max(p, 0.0), 1.0), bool(r["realised"])))
    return out


# ─── Walk-forward ──────────────────────────────────────────────────────────


def walk_forward_splits(
    rows: Sequence[dict[str, Any]],
    n_folds: int = 5,
    warmup_frac: float = 0.5,
) -> list[tuple[list[dict], list[dict]]]:
    """Expanding-window, time-ordered ``(train, test)`` splits.

    ``rows`` must already be sorted by timestamp. The first ``warmup_frac`` is the
    initial training window; the remainder is cut into ``n_folds`` test blocks,
    each trained on everything strictly before it. No row is ever used to predict
    itself, and no future row informs a past prediction.
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
        out.append((list(rows[:lo]), list(rows[lo:hi])))
    return out


def evaluate(
    rows: Sequence[dict[str, Any]],
    levels: Sequence[Callable[[dict], Any]],
    *,
    n_folds: int = 5,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = MIN_CELL_N,
    warmup_frac: float = 0.5,
) -> dict[str, Any]:
    """Fit on each training window, score the held-out block, pool the results.

    Pooling is over the concatenated out-of-fold predictions — every row is
    predicted exactly once, by a model that never saw it.
    """
    splits = walk_forward_splits(rows, n_folds, warmup_frac)
    if not splits:
        return {"n": 0}
    pooled: list[tuple[float, bool]] = []
    per_fold = []
    for train, test in splits:
        tables = fit(train, levels, alpha, min_n)
        pairs = predict(tables, levels, test, alpha, min_n)
        if pairs:
            pooled.extend(pairs)
            per_fold.append({
                "n": len(pairs),
                "auc": round(auc(pairs), 4) if auc(pairs) is not None else None,
            })
    if not pooled:
        return {"n": 0}
    d = decompose(pooled, n_bins=10)
    return {
        "n": len(pooled),
        "auc": round(auc(pooled), 4) if auc(pooled) is not None else None,
        "brier": d["brier"],
        "rel": d["rel"],
        "res": d["res"],
        "bss": d["bss"],
        "base_rate": d["base_rate"],
        "per_fold": per_fold,
    }


def evaluate_in_sample(
    rows: Sequence[dict[str, Any]],
    levels: Sequence[Callable[[dict], Any]],
    *,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = MIN_CELL_N,
) -> dict[str, Any]:
    """Fit and score on the SAME rows — the **ceiling**, not a result.

    Reported only to separate two very different failures:

      * in-sample high, out-of-fold ≈ 0.5  -> the cell HAS signal but it does not
        transfer across time (regime drift / non-stationarity).
      * in-sample ≈ 0.5 as well            -> the cell has no signal at all.

    Never quote this as performance. It is an upper bound a model reaches only by
    memorising.

    **Caller must pass the same evaluation population** the out-of-fold number was
    pooled over (see ``compare``), or the two are not comparable: ``evaluate``
    scores only the post-warmup rows, so a ceiling computed over *all* rows would
    be measuring a different set.
    """
    if not rows:
        return {"n": 0}
    tables = fit(rows, levels, alpha, min_n)
    pairs = predict(tables, levels, rows, alpha, min_n)
    if not pairs:
        return {"n": 0}
    d = decompose(pairs, n_bins=10)
    return {
        "n": len(pairs),
        "auc": round(auc(pairs), 4) if auc(pairs) is not None else None,
        "brier": d["brier"],
        "bss": d["bss"],
    }


def compare(
    rows: Sequence[dict[str, Any]],
    *,
    n_folds: int = 5,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = MIN_CELL_N,
    warmup_frac: float = 0.5,
    with_ceiling: bool = True,
) -> list[dict[str, Any]]:
    """Every spec evaluated on the same walk-forward splits, sorted by AUC.

    Same rows, same folds, same shrinkage — so a ranking between specs is a
    ranking of the *conditioning*, not of the setup. Each result also carries an
    ``in_sample_auc`` ceiling so the reader can tell "no signal" from "no
    transferable signal".
    """
    out = []
    for name, levels in SPECS.items():
        r = evaluate(rows, levels, n_folds=n_folds, alpha=alpha,
                     min_n=min_n, warmup_frac=warmup_frac)
        if with_ceiling:
            # Same evaluation population as the pooled out-of-fold number: the
            # post-warmup rows that `evaluate` actually scored. Fitting the
            # ceiling on ALL rows would compare two different sets.
            start = int(len(rows) * warmup_frac)
            ceil = evaluate_in_sample(rows[start:], levels, alpha=alpha, min_n=min_n)
            r["in_sample_auc"] = ceil.get("auc")
            r["in_sample_bss"] = ceil.get("bss")
        out.append({"name": name, **r})
    return sorted(out, key=lambda d: (d.get("auc") or 0.0), reverse=True)


def transfer_verdict(r: dict[str, Any], min_ceiling: float = 0.55) -> str:
    """Did the cell's signal survive time, or was there none to begin with?

    The distinction the out-of-fold number alone cannot make.
    """
    oof = r.get("auc")
    ins = r.get("in_sample_auc")
    if oof is None or ins is None:
        return "no data"
    if ins < min_ceiling:
        return (f"NO SIGNAL — even in-sample AUC {ins:.4f} is at chance, so there is "
                f"nothing to transfer")
    if oof < min_ceiling:
        return (f"NO TRANSFER — in-sample {ins:.4f} but out-of-fold {oof:.4f}: the "
                f"cell memorises the training window and does not survive time")
    return f"TRANSFERS — in-sample {ins:.4f}, out-of-fold {oof:.4f}"


def fold_stats(per_fold: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Mean/spread of the per-fold AUCs.

    Needed because a **pooled** AUC can show a gain that is entirely fold noise.
    With 5 folds on ~19k rows the per-fold spread here is ~0.05–0.07 AUC, so any
    gain smaller than that spread is not evidence — it is the same data seen
    through a different cut. Reported so the verdict can say so.
    """
    aucs = [f["auc"] for f in per_fold if f.get("auc") is not None]
    if not aucs:
        return {}
    return {
        "mean": round(statistics.fmean(aucs), 4),
        "sd": round(statistics.stdev(aucs), 4) if len(aucs) > 1 else 0.0,
        "min": round(min(aucs), 4),
        "max": round(max(aucs), 4),
        "n_folds": len(aucs),
        "n_above_half": sum(1 for a in aucs if a > 0.5),
    }


def verdict(baseline: dict[str, Any], candidate: dict[str, Any],
            min_gain: float = 0.02) -> str:
    """Mechanical read of one candidate vs the cell-alone baseline.

    Two bars, both required for "ADDS":

      1. **Pooled gain** ≥ ``min_gain`` — the headline improvement.
      2. **Stability** — the gain must exceed the *fold-to-fold spread* of the
         baseline. A +0.03 gain when folds swing ±0.07 is not a finding; it is
         one lucky cut. This is the guard against promoting noise.
    """
    a0, a1 = baseline.get("auc"), candidate.get("auc")
    if a0 is None or a1 is None:
        return "no data"
    gain = a1 - a0

    # fold-noise guard
    fs = fold_stats(baseline.get("per_fold") or [])
    sd = fs.get("sd") or 0.0
    unstable = sd > 0 and gain < sd

    if gain >= min_gain and not unstable:
        return (f"ADDS — AUC {a0:.4f} → {a1:.4f} ({gain:+.4f}), "
                f"clears the {min_gain} bar and the ±{sd:.4f} fold spread")
    if gain >= min_gain and unstable:
        return (f"WITHIN NOISE — AUC {a0:.4f} → {a1:.4f} ({gain:+.4f}) clears the "
                f"{min_gain} bar but is smaller than the ±{sd:.4f} fold spread")
    if gain > 0:
        return f"MARGINAL — AUC {a0:.4f} → {a1:.4f} ({gain:+.4f}), below the {min_gain} bar"
    return f"NO GAIN — AUC {a0:.4f} → {a1:.4f} ({gain:+.4f})"
