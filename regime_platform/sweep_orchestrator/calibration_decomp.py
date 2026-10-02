"""
calibration_decomp.py — Murphy decomposition + the two-label calibration test.
═══════════════════════════════════════════════════════════════════════════════

**Why this module exists.** A previous pass scored the EV blend's ``p_win``
against ``outcome = tp_hit`` and concluded it was "anti-predictive" because it
lost to a constant base-rate prediction. That conclusion was an artifact of the
reference line, not a property of the model.

The blend's ``p_win`` is a **directional confidence** — it answers *"will price
move in the signal's direction?"* (its sources are ``p_regime`` = P(bull|bear)
from the HMM and ``p_timesfm`` = P(next bar up)). The settlement label is a
**bracket / first-passage** event — *"does price reach +TP before −SL within
48h?"*. Those are different random variables, which the repo's own quant audit
already flags as the core "category error" (E1).

**The reference-line error, precisely.** A constant forecast ``p ≡ ō`` (the base
rate) has, by construction:

    REL = 0        (every bin's mean forecast equals its observed frequency)
    RES = 0        (outcome frequency does not vary across bins — there is one bin)

so its Brier score is *exactly* the uncertainty term:

    Brier(constant) = UNC = ō(1 − ō)

UNC is a property of the **event**, not of any model. Beating it requires
``RES > REL`` — discrimination in excess of miscalibration. Failing to beat it
therefore means one of two very different things:

    (a) the model has no resolution for THIS event, or
    (b) the model is well-calibrated for a DIFFERENT event.

Distinguishing (a) from (b) is the entire point of this module. The Murphy
decomposition separates them; scoring against the right label settles it.

**Murphy (1973) decomposition**, over ``K`` bins with ``n_k`` forecasts,
bin-mean forecast ``p̄_k``, bin observed frequency ``ō_k``, and base rate ``ō``:

    REL = (1/N) Σ_k n_k (p̄_k − ō_k)²      miscalibration. 0 = perfect. LOWER better.
    RES = (1/N) Σ_k n_k (ō_k − ō)²        discrimination. HIGHER better.
    UNC = ō (1 − ō)                       event difficulty. Model-independent.

    Brier = REL − RES + UNC

Exact when stratifying on distinct forecast values; with bins it is the standard
binned approximation, and this module reports the residual rather than hiding it
(see ``decompose``'s ``residual``).

**The two labels.**

  ``directional``  ``drift_{1h,4h,24h}_pct > 0`` — direction-relative post-entry
                   price drift, "computed from the post-entry candle path
                   regardless of eventual outcome" (migration 121). This is the
                   event ``p_win`` actually forecasts. ~95% coverage.
  ``bracket``      ``outcome == 'tp_hit'`` — the settlement event. What the EV
                   formula and the qualification gates need. ~49% of light
                   signals resolve (the rest expire).

**Pure functions only** — no I/O, no clock.
"""

from __future__ import annotations

import statistics
from typing import Any, Sequence

# ─── Reference lines ───────────────────────────────────────────────────────


def uncertainty(base_rate: float) -> float:
    """``UNC = ō(1−ō)`` — the Brier of a constant forecast at the base rate.

    Model-independent: it is the score you get by saying "this event happens
    ō of the time" to every signal, and it is the number to beat.
    """
    return base_rate * (1.0 - base_rate)


def brier_skill_score(brier: float | None, base_rate: float | None) -> float | None:
    """``BSS = 1 − Brier/UNC``. 0 = no better than the constant; >0 = skill.

    The standard normalized skill score. Reported alongside the raw Brier
    because a Brier of 0.26 sounds fine until you learn the constant scores 0.20.
    """
    if brier is None or base_rate is None:
        return None
    u = uncertainty(base_rate)
    if u <= 0:
        return None
    return 1.0 - brier / u


# ─── Murphy decomposition ──────────────────────────────────────────────────


def decompose(
    pairs: Sequence[tuple[float, bool]],
    n_bins: int = 10,
) -> dict[str, Any]:
    """``(predicted, realised)`` -> ``{rel, res, unc, brier, residual, ...}``.

    All three terms are the standard per-sample-normalised Murphy definitions,
    so ``Brier = REL − RES + UNC`` holds to within binning error. The
    ``residual`` key is the actual gap and is reported, not assumed away — with
    coarse bins on a continuous forecast it is non-zero, and a reader deserves
    to see how big it is before trusting the split.
    """
    if not pairs:
        return {"n": 0}
    n = len(pairs)
    obs = [1.0 if y else 0.0 for _, y in pairs]
    base = statistics.fmean(obs)

    rel = res = 0.0
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        idx = [j for j, (p, _) in enumerate(pairs)
               if lo <= p < hi or (i == n_bins - 1 and p >= hi)]
        if not idx:
            continue
        nk = len(idx)
        p_bar = statistics.fmean([pairs[j][0] for j in idx])
        o_bar = statistics.fmean([obs[j] for j in idx])
        rel += nk * (p_bar - o_bar) ** 2
        res += nk * (o_bar - base) ** 2
        bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": nk,
                     "mean_pred": round(p_bar, 4), "realised": round(o_bar, 4)})
    rel /= n
    res /= n
    unc = uncertainty(base)
    brier = statistics.fmean([(p - o) ** 2 for p, o in zip([p for p, _ in pairs], obs)])
    bss = brier_skill_score(brier, base)

    return {
        "n": n,
        "base_rate": round(base, 4),
        "brier": round(brier, 4),
        "rel": round(rel, 4),
        "res": round(res, 4),
        "unc": round(unc, 4),
        "residual": round(brier - (rel - res + unc), 4),
        # None on a single-class sample (base_rate 0 or 1 -> UNC 0 -> undefined)
        "bss": round(bss, 4) if bss is not None else None,
        "bins": bins,
    }


# ─── Discrimination ────────────────────────────────────────────────────────


def auc(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """Rank-based AUC (Mann–Whitney U), tie-corrected.

    Discrimination, independent of calibration: the probability that a randomly
    chosen positive event got a higher forecast than a randomly chosen negative
    one. 0.5 = no better than chance ordering.

    Computed from average ranks so ties (the platform stores 4-decimal values,
    so ties are common) are handled correctly rather than arbitrarily broken.
    """
    pos = [p for p, y in pairs if y]
    neg = [p for p, y in pairs if not y]
    if not pos or not neg:
        return None
    vals = sorted((p, y) for p, y in pairs)
    # average rank for tied values
    ranks: dict[float, float] = {}
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1][0] == vals[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0        # 1-indexed
        ranks[vals[i][0]] = avg
        i = j + 1
    rank_sum_pos = sum(ranks[p] for p in pos)
    n_p, n_n = len(pos), len(neg)
    u = rank_sum_pos - n_p * (n_p + 1) / 2.0
    return u / (n_p * n_n)


# ─── Reliability ───────────────────────────────────────────────────────────


def reliability(
    pairs: Sequence[tuple[float, bool]],
    n_bins: int = 10,
) -> list[dict[str, Any]]:
    """Per-bin ``{bin, n, mean_pred, realised, gap}``.

    ``gap = realised − mean_pred``: **negative = overconfident**, positive =
    underconfident. Empty bins omitted (never reported as 0 — that reads as
    "perfectly calibrated at zero observations").
    """
    if not pairs:
        return []
    out = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        idx = [(p, y) for p, y in pairs
               if lo <= p < hi or (i == n_bins - 1 and p >= hi)]
        if not idx:
            continue
        mp = statistics.fmean([p for p, _ in idx])
        ro = statistics.fmean([1.0 if y else 0.0 for _, y in idx])
        out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx),
                    "mean_pred": round(mp, 4), "realised": round(ro, 4),
                    "gap": round(ro - mp, 4)})
    return out


# ─── The headline comparison ───────────────────────────────────────────────


def score_candidate(
    name: str,
    pairs: Sequence[tuple[float, bool]],
    *,
    target: str,
    n_bins: int = 10,
) -> dict[str, Any]:
    """One candidate scored on one target: decomposition + AUC + reliability."""
    d = decompose(pairs, n_bins)
    if not d.get("n"):
        return {"name": name, "target": target, "n": 0}
    return {
        "name": name,
        "target": target,
        "n": d["n"],
        "base_rate": d["base_rate"],
        "brier": d["brier"],
        "rel": d["rel"],
        "res": d["res"],
        "unc": d["unc"],
        "residual": d["residual"],
        "bss": d["bss"],
        "auc": round(auc(pairs), 4) if auc(pairs) is not None else None,
        "bins": d["bins"],
    }


def verdict(c: dict[str, Any]) -> str:
    """One-line mechanical read of a scored candidate.

    Deliberately two independent axes — **calibration** (REL vs RES) and
    **discrimination** (AUC) — because a forecast can be perfectly calibrated
    and useless (a constant is), or sharp and badly calibrated.
    """
    if not c.get("n"):
        return "no data"
    bss = c.get("bss")
    a = c.get("auc")
    parts = []
    if bss is None:
        parts.append("BSS n/a")
    elif bss > 0.10:
        parts.append(f"beats the constant (BSS {bss:+.3f})")
    elif bss > 0:
        parts.append(f"marginally beats the constant (BSS {bss:+.3f})")
    else:
        parts.append(f"no better than a constant (BSS {bss:+.3f})")
    if a is None:
        parts.append("AUC n/a")
    elif a >= 0.60:
        parts.append(f"strong ordering (AUC {a:.3f})")
    elif a >= 0.55:
        parts.append(f"real ordering (AUC {a:.3f})")
    elif a >= 0.52:
        parts.append(f"weak ordering (AUC {a:.3f})")
    else:
        parts.append(f"no ordering (AUC {a:.3f})")
    return "; ".join(parts)
