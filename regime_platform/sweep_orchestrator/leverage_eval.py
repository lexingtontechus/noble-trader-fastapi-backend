"""
leverage_eval.py — Track A: the calibration + leverage effect of withdrawing
p_markov_hold_n from the EV blend.
═══════════════════════════════════════════════════════════════════════════════

Scope: `docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md` §3 Track A.

**The question.** Withdrawing `p_markov_hold_n` was NOT leverage-neutral. The
legacy HMM's `p_markov` values cluster low, so removing the source from the logit
pool *raises* `p_win`, which raises `ev_per_dollar`, which raises `ev_scale`, which
multiplies Kelly. This module measures how much.

**The algebra (exact, not approximated).** The engine's logit pool is

    L_on  = Σ_available w_i · logit(p_i)          (weights renormalised over
                                                   the sources present)

Withdrawing one source of weight `w_m` and probability `p_m` renormalises the
rest by `1/(1−w_m)`, so

    L_off = (L_on − w_m · logit(p_m)) / (1 − w_m)          ← exact identity

Verified against `EVEngine.compute_ev` itself to 1e-12 (see the tests). No
re-fitting, no simulation — the counterfactual is a closed form on data we
already store.

**Inputs (all already persisted, no new capture).**

  ``nt_signal_sim``    ``p_win``, ``predicted_p_win_raw`` (the PRE-enforcement
                       blend output — i.e. ``L_on``), ``outcome``, ``tp_bricks``,
                       ``sl_bricks``, ``brick_size``, ``symbol``, ``ts``
  ``nt_sweep_result``  ``p_timesfm``, ``markov_p_up``, ``markov_p_dn``,
                       ``sweep_timestamp`` — joined on nearest (symbol, ts)

**What ``p_m`` is (and is not).** The engine consumed
``p_markov_hold_n = T^N[current][target]``. What is *stored* is the single-step
``T[current][UP]`` / ``T[current][DOWN]``. This module therefore uses

    p_m = markov_p_up  (buy)  |  markov_p_dn  (sell)

— the probability mass the legacy model put on the trade's own direction. That is
a **proxy** for the T^N value, and it is the conservative choice: the stored
single-step value is *lower* than any multi-step hold probability, so it
understates the withdrawal's effect rather than overstating it. Flagged in the
report; the direction of the finding does not depend on it.

**Precision (be honest about it).** The identity is exact in exact arithmetic.
The value it is applied to — ``predicted_p_win_raw`` in the DB, and
``EVFullResult.p_win`` — is **rounded to 4 decimal places** at write time. That
rounding propagates to roughly ``1e-4 / (p(1−p)(1−w_m))`` in logit space, i.e.
~1e-3 worst case at p≈0.5. The effects this module measures are ~0.15 in p_win,
two to three orders of magnitude larger, so the rounding does not change any
conclusion — but the reconstruction is *not* bit-exact and the tests assert the
algebra exactly while asserting the engine only within storage rounding.

**Pure functions only** — no I/O, no clock. The CLI fetches and joins.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

# The engine's logit clamp — logit() must match it or the reconstruction drifts
# on degenerate inputs (the legacy model produced exactly those).
from regime_platform.services.statistics.ev_engine import (
    P_WIN_WEIGHTS_FULL,
    _inv_logit,
    _logit,
)

# ─── Weights ───────────────────────────────────────────────────────────────


def markov_weight() -> float:
    """The withdrawn source's weight in the logit pool, as it was applied.

    Read from ``P_WIN_WEIGHTS_FULL`` (5:4:5) and renormalised over the three
    sources that were present live — ``p_historical`` was already retired and
    renormalised out, so the applied weight is ``4/14``, not ``4/14`` of a
    different denominator.
    """
    w = P_WIN_WEIGHTS_FULL
    total = sum(w.values())
    return float(w["p_markov_hold_n"]) / float(total)


# ─── The counterfactual ────────────────────────────────────────────────────


def logit_without_source(logit_on: float, p_m: float, w_m: float) -> float:
    """``L_off`` from ``L_on`` — the exact withdrawal identity.

    ``L_on`` is the blend's logit *with* the source (i.e.
    ``logit(predicted_p_win_raw)``), ``p_m`` the withdrawn source's probability,
    ``w_m`` its applied weight. Returns the logit the blend would have produced
    with the source absent.

    Exact in exact arithmetic. Applied to the platform's stored values it
    inherits their 4-decimal rounding (see the module docstring's precision
    note) — fine here, because the effects measured are ~1e-1.
    """
    if not (0.0 < w_m < 1.0):
        return logit_on
    return (logit_on - w_m * _logit(p_m)) / (1.0 - w_m)


def p_win_without_source(p_win_on: float, p_m: float, w_m: float) -> float:
    """The same, returning a probability."""
    return _inv_logit(logit_without_source(_logit(p_win_on), p_m, w_m))


# Worst-case sensitivity of the reconstruction to the stored 4-decimal rounding,
# evaluated at p≈0.5 (where dlogit/dp is smallest, so the error is largest).
# Used by the tests to size their tolerance honestly rather than pick one.
_STORAGE_ROUNDING_TOL: float = 2e-3


# ─── Leverage ──────────────────────────────────────────────────────────────


def ev_scale(p_win: float, profit: float, stake: float) -> float:
    """``EVEngine.compute_ev_scale``'s formula, reproduced on stored geometry.

    ``ev = p·profit − (1−p)·stake``; ``ev_per_dollar = ev/stake``;
    ``ev_scale = min(1, ev_per_dollar/0.5)`` when positive, else 0.

    Kept as a local pure function so the leverage replay needs no engine
    instance — the formula is three lines and is asserted against the engine in
    the tests, so drift is caught.
    """
    if stake <= 0:
        return 0.0
    ev = p_win * profit - (1.0 - p_win) * stake
    ev_per_dollar = ev / stake
    if ev_per_dollar > 0:
        return min(1.0, ev_per_dollar / 0.50)
    return 0.0


# ─── Calibration ───────────────────────────────────────────────────────────


def brier(p: float | None, realised: bool) -> float | None:
    """``(p − y)²`` with ``y ∈ {0,1}``. The platform's existing convention
    (``eod_resolver.calibration_brier``). ``None`` when ``p`` is missing — never
    imputed, because a fabricated 0.5 would flatter the score."""
    if p is None:
        return None
    y = 1.0 if realised else 0.0
    return (float(p) - y) ** 2


def reliability_table(
    pairs: Sequence[tuple[float, bool]],
    n_bins: int = 10,
) -> list[dict[str, Any]]:
    """``(predicted, realised)`` -> per-bin counts and rates.

    The **shape** diagnostic: Brier alone cannot distinguish "wrong level" from
    "wrong slope". A source whose removal merely shifted the level moves every
    bin the same way; one that broke calibration shows up as a slope change
    between the low and high bins.

    Empty bins are omitted (never reported as 0 — that would read as "perfectly
    calibrated at 0 observations").
    """
    if not pairs:
        return []
    out: list[dict[str, Any]] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        vals = [p for p, _ in pairs if (lo <= p < hi or (i == n_bins - 1 and p >= hi))]
        if not vals:
            continue
        realised = [y for p, y in pairs if (lo <= p < hi or (i == n_bins - 1 and p >= hi))]
        out.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": len(vals),
            "mean_pred": round(statistics.fmean(vals), 4),
            "realised": round(statistics.fmean([1.0 if y else 0.0 for y in realised]), 4),
        })
    return out


# ─── The row-level result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class LeverageRow:
    """One signal, replayed with the withdrawn source present and absent."""

    symbol: str
    p_win_on: float          # predicted_p_win_raw (pre-enforcement blend output)
    p_win_off: float         # counterfactual
    p_m: float               # the withdrawn source's value (proxy — see module doc)
    realised: bool
    scale_on: float
    scale_off: float

    @property
    def d_p_win(self) -> float:
        return self.p_win_off - self.p_win_on

    @property
    def d_scale(self) -> float:
        return self.scale_off - self.scale_on

    @property
    def zero_to_size(self) -> bool:
        """Was unsized, becomes sized — the risk case."""
        return self.scale_on == 0.0 and self.scale_off > 0.0

    @property
    def to_full_size(self) -> bool:
        """Was partially sized, becomes fully sized."""
        return self.scale_on < 1.0 and self.scale_off >= 1.0


def build_rows(joined: Sequence[dict[str, Any]]) -> list[LeverageRow]:
    """``{p_win_on, p_m, realised, tp_bricks, sl_bricks, brick_size, symbol}``
    -> replayed rows. Rows missing any required field are skipped, not imputed."""
    w_m = markov_weight()
    out: list[LeverageRow] = []
    for r in joined:
        try:
            p_on = float(r["p_win_on"])
            p_m = float(r["p_m"])
            tp = float(r["tp_bricks"])
            sl = float(r["sl_bricks"])
            brick = float(r["brick_size"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 < p_on < 1.0) or not (0.0 <= p_m <= 1.0):
            continue
        profit, stake = tp * brick, sl * brick
        if profit <= 0 or stake <= 0:
            continue
        p_off = p_win_without_source(p_on, p_m, w_m)
        out.append(LeverageRow(
            symbol=str(r.get("symbol") or "?"),
            p_win_on=p_on,
            p_win_off=p_off,
            p_m=p_m,
            realised=bool(r["realised"]),
            scale_on=ev_scale(p_on, profit, stake),
            scale_off=ev_scale(p_off, profit, stake),
        ))
    return out


def constant_brier(realised: Sequence[bool]) -> float | None:
    """Brier of the **constant** prediction ``p = base rate`` for every row.

    The reference line that matters: if a single constant beats the blend, the
    blend is adding negative information about the outcome, and no amount of
    re-weighting the sources will fix that. Cheap, exact, and impossible to
    argue with — it is one number.
    """
    vals = [1.0 if y else 0.0 for y in realised]
    if not vals:
        return None
    p = statistics.fmean(vals)
    return statistics.fmean([(p - v) ** 2 for v in vals])


# ─── Side-by-side candidate comparison ─────────────────────────────────────
#
# Which probability is best calibrated for the SAME event? Each candidate is
# scored on its OWN available rows, and against a constant baseline computed on
# those SAME rows — that is the only apples-to-apples read, because a candidate
# with lower coverage may be covering an easier or harder sub-population.
#
# `event` is not decoration: a column that estimates a DIFFERENT event cannot be
# ranked against the others on this target, and saying so is the difference
# between a comparison and a category error.

# The settlement event this module scores against: did the trade reach TP.
TARGET_EVENT = "P(TP before SL within deadline)"

CANDIDATES: tuple[dict[str, str], ...] = (
    {"key": "p_win", "col": "p_win", "event": TARGET_EVENT,
     "note": "as published (post-enforcement)"},
    {"key": "p_win_raw", "col": "p_win_raw", "event": TARGET_EVENT,
     "note": "pre-enforcement blend output"},
    {"key": "p_win_bracket", "col": "p_win_bracket", "event": TARGET_EVENT,
     "note": "p_resolve × Beta-shrunk P(tp|resolve) — reachability-conditioned"},
    {"key": "p_win_dir", "col": "p_win_dir", "event": TARGET_EVENT,
     "note": "direction blend"},
    {"key": "p_win_resolve", "col": "p_win_resolve", "event": "P(TP | resolved)",
     "note": "conditional — different event, shown for reference"},
    {"key": "p_resolve", "col": "p_resolve", "event": "P(resolved before deadline)",
     "note": "different event, shown for reference"},
)


def compare_candidates(
    rows: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, str]] = CANDIDATES,
) -> list[dict[str, Any]]:
    """Brier + coverage + constant-edge for each candidate, on its own rows.

    ``rows`` need ``realised`` (bool) plus the candidate columns. A candidate's
    ``edge`` is ``brier_constant − brier``: **positive means it beats a constant
    prediction on its own turf**. Negative means it is anti-predictive, and the
    magnitude is how much worse than saying nothing.
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        col = c["col"]
        pairs = [(r.get(col), bool(r["realised"])) for r in rows]
        have = [(float(p), y) for p, y in pairs
                if p is not None and _is_prob(p)]
        if not have:
            out.append({**c, "n": 0, "coverage": None, "brier": None,
                        "base_rate": None, "brier_constant": None, "edge": None})
            continue
        realised = [y for _, y in have]
        b = statistics.fmean([(p - (1.0 if y else 0.0)) ** 2 for p, y in have])
        bc = constant_brier(realised)
        out.append({
            **c,
            "n": len(have),
            "coverage": round(len(have) / len(rows), 4) if rows else None,
            "brier": round(b, 4),
            "base_rate": round(statistics.fmean([1.0 if y else 0.0 for y in realised]), 4),
            "brier_constant": round(bc, 4) if bc is not None else None,
            "edge": round(bc - b, 4) if bc is not None else None,
        })
    return out


def common_subset(
    rows: Sequence[dict[str, Any]],
    keys: Sequence[str],
    candidates: Sequence[dict[str, str]] = CANDIDATES,
) -> list[dict[str, Any]]:
    """Re-score the named candidates on the rows where ALL of them are present.

    Removes the coverage confound: every candidate sees the identical population,
    so a ranking between them is a ranking of the predictions, not of the rows
    each happened to cover.
    """
    cols = [c["col"] for c in candidates if c["key"] in keys]
    if not cols:
        return []
    subset = [
        r for r in rows
        if all(r.get(c) is not None and _is_prob(r.get(c)) for c in cols)
    ]
    if not subset:
        return []
    return compare_candidates(
        subset, [c for c in candidates if c["key"] in keys],
    )


def _is_prob(v: Any) -> bool:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return False
    return 0.0 <= x <= 1.0


def summarize(rows: Sequence[LeverageRow]) -> dict[str, Any]:
    """The Track-A numbers. Means/medians and the transition counts that matter."""
    if not rows:
        return {"n": 0}
    d_pw = [r.d_p_win for r in rows]
    d_sc = [r.d_scale for r in rows]
    b_on = [brier(r.p_win_on, r.realised) for r in rows]
    b_off = [brier(r.p_win_off, r.realised) for r in rows]
    realised = [r.realised for r in rows]
    b_const = constant_brier(realised)
    return {
        "n": len(rows),
        "mean_p_m": round(statistics.fmean([r.p_m for r in rows]), 4),
        "median_p_m": round(statistics.median([r.p_m for r in rows]), 4),
        # calibration
        "brier_on": round(statistics.fmean(b_on), 4),
        "brier_off": round(statistics.fmean(b_off), 4),
        "brier_delta": round(statistics.fmean(b_off) - statistics.fmean(b_on), 4),
        "brier_constant": round(b_const, 4) if b_const is not None else None,
        "base_rate": round(statistics.fmean([1.0 if r.realised else 0.0 for r in rows]), 4),
        # level shift
        "mean_p_win_on": round(statistics.fmean([r.p_win_on for r in rows]), 4),
        "mean_p_win_off": round(statistics.fmean([r.p_win_off for r in rows]), 4),
        "mean_d_p_win": round(statistics.fmean(d_pw), 4),
        "median_d_p_win": round(statistics.median(d_pw), 4),
        "max_d_p_win": round(max(d_pw), 4),
        # leverage
        "mean_scale_on": round(statistics.fmean([r.scale_on for r in rows]), 4),
        "mean_scale_off": round(statistics.fmean([r.scale_off for r in rows]), 4),
        "mean_d_scale": round(statistics.fmean(d_sc), 4),
        "median_d_scale": round(statistics.median(d_sc), 4),
        "n_zero_to_size": sum(1 for r in rows if r.zero_to_size),
        "pct_zero_to_size": round(100.0 * sum(1 for r in rows if r.zero_to_size) / len(rows), 2),
        "n_to_full_size": sum(1 for r in rows if r.to_full_size),
        "pct_to_full_size": round(100.0 * sum(1 for r in rows if r.to_full_size) / len(rows), 2),
        "n_unsized_on": sum(1 for r in rows if r.scale_on == 0.0),
        "n_unsized_off": sum(1 for r in rows if r.scale_off == 0.0),
    }


def by_symbol(rows: Sequence[LeverageRow]) -> list[dict[str, Any]]:
    """Per-symbol rollup, largest leverage effect first."""
    groups: dict[str, list[LeverageRow]] = {}
    for r in rows:
        groups.setdefault(r.symbol, []).append(r)
    out = []
    for sym, rs in groups.items():
        s = summarize(rs)
        out.append({
            "symbol": sym, "n": s["n"],
            "mean_p_m": s["mean_p_m"],
            "brier_on": s["brier_on"], "brier_off": s["brier_off"],
            "brier_delta": s["brier_delta"],
            "mean_d_p_win": s["mean_d_p_win"],
            "mean_scale_on": s["mean_scale_on"], "mean_scale_off": s["mean_scale_off"],
            "mean_d_scale": s["mean_d_scale"],
            "pct_zero_to_size": s["pct_zero_to_size"],
        })
    return sorted(out, key=lambda d: d["mean_d_scale"], reverse=True)


def leverage_verdict(s: dict[str, Any]) -> dict[str, Any]:
    """Mechanical read of the Track-A result.

    Two independent questions, deliberately NOT collapsed into one score:

      * **Calibration** — did Brier improve? (``brier_delta`` < 0 = better)
      * **Risk** — did sizing rise? (``mean_d_scale`` > 0 = larger positions)

    The dangerous quadrant is *worse calibration + higher size*. The benign one
    is *better calibration + higher size* (the source was dragging p_win down and
    removing it corrected that). Reported separately so neither hides the other.
    """
    if not s.get("n"):
        return {"read": "no data"}
    brier_better = s["brier_delta"] < 0
    size_up = s["mean_d_scale"] > 0
    if brier_better and size_up:
        read = "BENIGN — calibration improved and size rose (the source was depressing p_win)"
    elif brier_better and not size_up:
        read = "CLEAN — calibration improved, size flat or down"
    elif not brier_better and size_up:
        read = "RISK — calibration got WORSE while size rose; revert pending a replacement"
    else:
        read = "INERT — calibration worse but size flat/down"
    return {
        "read": read,
        "brier_delta": s["brier_delta"],
        "mean_d_scale": s["mean_d_scale"],
        "pct_zero_to_size": s["pct_zero_to_size"],
        "calibration_better": brier_better,
        "size_rose": size_up,
    }
