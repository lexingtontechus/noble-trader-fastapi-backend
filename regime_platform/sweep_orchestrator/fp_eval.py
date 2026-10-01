"""
fp_eval.py — Phase 2 offline evaluation for the first-passage programme.
═══════════════════════════════════════════════════════════════════════════════

Phase 2 of `worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md`.

Answers the pre-registered question: **does first-passage beat the shrunk
base-rate control, and does the regime-transition overlay add anything on top
of the bare level?**

Three arms, scored on the SAME decided population:

======================  =========================================================
arm                     what it is
======================  =========================================================
``first_passage``       ``walk()``-derived P(TP before SL) for the signal's own
                        recorded bracket (``first_passage.py``)
``base_rate``           the shrunk per-symbol P(TP|decided) — a trivial model
                        with 100% coverage and no fitting. **This is the bar**
                        (scope §2.2: it beat the HMM outright, 0.5898 vs 0.5251)
``incumbent``           the withdrawn legacy HMM's directional call, scored on
                        its own non-FLAT calls (the source ``p_markov_hold_n``
                        used to feed)
======================  =========================================================

Plus the **overlay** (D5) measured as an *increment on top of each arm*, never
alone — bar B5 is the real gate, because a source can be individually accurate
and still add nothing to a correlated blend.

Metrics (pre-registered, scope §5):
  * accuracy + **skill vs base rate** (the number that killed the HMM)
  * coverage
  * **ΔD** = mean(y_acc | gated) − mean(y_acc | rest), y_acc = 1{TP} − SL/(TP+SL)
  * era halves (B4), symbol-day cluster count (B7), episode unit (B6)

**Look-ahead discipline (risk R1).** A path is scored only when its full
48-bar window has closed *before* the evaluation cutoff, and the trailing
base-rate / cell statistics that a gate would use are built strictly from
*earlier* decided rows in the same symbol. Rows are time-ordered throughout.

Pure functions only — no I/O. The CLI (`scripts/kairos_fp_eval.py`) fetches,
feeds this module, and writes the report.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable, Sequence

import numpy as np

from regime_platform.sweep_orchestrator.kairos_quant import (
    accuracy_vs_no_skill,
    no_skill_line,
    symbol_day_clusters,
)
from regime_platform.sweep_orchestrator.kairos_rank import (
    event_window,
    freshness_score,
    transition_type,
    type_score,
)

# ─── Frozen evaluation constants ───────────────────────────────────────────
# Declared before any scoring. Same discipline as rank_score / FP_OVERLAY_W.

VOL_ORDER: tuple[str, ...] = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")

# Shrinkage strength for the base-rate control (pseudo-counts). k=20 matches the
# RM-1 evaluation's control arm exactly, so the 0.5898 bar is reproducible.
BASE_RATE_K: float = 20.0

# Gate quantile for the ΔD comparison — top 40% by score, matching the Kairos
# rank_score shadow gate so the two are directly comparable.
GATE_Q: float = 0.60


# ─── Row preparation ───────────────────────────────────────────────────────


def num(v: Any) -> float | None:
    """Finite float or None. Never raises."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def build_records(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise raw ``nt_signal_sim`` rows into evaluation records.

    Keeps only decided (tp_hit / sl_hit) rows with a usable bracket, and
    preserves the fields every arm needs. ``dist_tp`` / ``dist_sl`` fall back
    from the ATR distances to the brick counts (the same fallback the
    rank_score eval uses) so older rows without ATR geometry are not silently
    dropped from the population.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        outcome = r.get("outcome")
        if outcome not in ("tp_hit", "sl_hit"):
            continue
        tp_a, sl_a = num(r.get("tp_distance_atr")), num(r.get("sl_distance_atr"))
        tp_b, sl_b = num(r.get("tp_bricks")), num(r.get("sl_bricks"))
        dist_tp = tp_a if tp_a and tp_a > 0 else tp_b
        dist_sl = sl_a if sl_a and sl_a > 0 else sl_b
        if not dist_tp or not dist_sl or dist_tp <= 0 or dist_sl <= 0:
            continue
        ns = no_skill_line(dist_tp, dist_sl)
        if ns is None:
            continue
        tp_first = outcome == "tp_hit"
        out.append({
            "signal_id": r.get("signal_id"),
            "symbol": r.get("symbol"),
            "signal": r.get("signal"),
            "ts": r.get("ts"),
            "regime": r.get("regime"),
            "outcome": outcome,
            "tp_first": tp_first,
            "y_bin": 1.0 if tp_first else 0.0,
            "y_acc": accuracy_vs_no_skill(tp_first, dist_tp, dist_sl),
            "dist_tp": dist_tp,
            "dist_sl": dist_sl,
            # Carried for path reconstruction — the CLI needs these to rebuild
            # the ATR-scaled window; without them every row would be skipped.
            "entry_price": num(r.get("entry_price")),
            "atr": num(r.get("atr")),
            "legacy_state": r.get("legacy_markov_state"),
        })
    return out


def split_cell(regime: str | None) -> tuple[str | None, str | None]:
    """``'med_high_vol_strong_bull'`` -> ``('med_high_vol', 'bull')``.

    Delegates to ``kairos.regime_cell`` — the frozen, shared definition the live
    stamps and every offline Kairos panel use. Hand-rolling this is how the
    first cut of this module silently produced ``None`` for every row (the raw
    label has no ``|``); the transition programme would have measured nothing.
    """
    from regime_platform.sweep_orchestrator.kairos import regime_cell

    cell = regime_cell(regime)
    if not cell or "|" not in cell:
        return None, None
    vol, d = cell.split("|", 1)
    return vol or None, d or None


def annotate_event_time(records: Sequence[dict[str, Any]]) -> None:
    """Attach τ (hours since the symbol's last regime-cell change) and type.

    Mutates in place. Walks each symbol's records in time order and records a
    transition only when the ``(vol, dir)`` cell actually changes — the same
    rule ``kairos_rank_transition`` uses, so τ here and τ there mean the same
    thing. First observation of a symbol has no prior cell, so τ is None
    (unknown freshness), never 0.
    """
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in records:
        if x.get("symbol"):
            by_sym[x["symbol"]].append(x)

    for rows in by_sym.values():
        rows.sort(key=lambda x: str(x.get("ts") or ""))
        prev_vol = prev_dir = None
        last_change_ts = None
        for x in rows:
            vol, d = split_cell(x.get("regime"))
            x["vol"], x["dir"] = vol, d
            changed = prev_vol is not None and (vol, d) != (prev_vol, prev_dir)
            if changed:
                x["ttype"] = transition_type(prev_vol, prev_dir, vol, d, VOL_ORDER)
                last_change_ts = x.get("ts")
                x["tau_h"] = 0.0
            else:
                x["ttype"] = None
                x["tau_h"] = _hours_between(last_change_ts, x.get("ts"))
            x["ewin"] = event_window(x.get("tau_h"))
            prev_vol, prev_dir = vol, d


def _hours_between(start: Any, end: Any) -> float | None:
    """Hours between two ISO timestamps. None if either is missing/unparseable."""
    from datetime import datetime, timezone

    def parse(s: Any) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    a, b = parse(start), parse(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def dedupe_episodes(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One decision per (symbol, direction, hour) — the G2 episode unit (B6).

    15-minute sweeps repeat the same call four times an hour; without this the
    AUC/IC numbers inflate from overlapping hours (already documented in the
    ranking scope §9.2). Keeps the FIRST row in each episode.
    """
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for x in sorted(records, key=lambda r: str(r.get("ts") or "")):
        key = (
            x.get("symbol"),
            x.get("signal"),
            str(x.get("ts") or "")[:13],   # YYYY-MM-DDTHH
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


# ─── The arms ──────────────────────────────────────────────────────────────


def geometry_bucket(dist_tp: float, dist_sl: float) -> tuple[str, str]:
    """Coarse (TP, SL) bucket in ATR units — the conditioning for the
    first-passage arm.

    Quantised coarsely on purpose: the point is to separate *materially*
    different brackets, not to memorise individual geometries. Boundaries are
    round ATR multiples because that is the vocabulary the platform's geometry
    work already uses (TP 1–4 / SL 2–4 commodity, TP 8–14 / SL 6–12 equity, …).
    """
    def band(x: float, edges: Sequence[float]) -> str:
        # `<=` so a value exactly on a boundary lands in the LOWER band
        # (1.0 ATR SL is "<1", not "<2.5") — matches how the geometry work
        # reads its own round-number clamps.
        for e in edges:
            if x <= e:
                return f"<={e:g}"
        return f">{edges[-1]:g}"

    return band(dist_tp, (2.0, 5.0, 9.0)), band(dist_sl, (1.0, 2.5, 5.0))


def first_passage_arm(
    records: Sequence[dict[str, Any]],
    paths_by_id: dict[str, np.ndarray],
    *,
    k: float = BASE_RATE_K,
    n_paths_per_signal: int = 1,
) -> dict[str, Any]:
    """Arm 1: **walk-forward conditional first-passage rate**.

    Each signal contributes ONE 48-bar path, so a single signal's first-touch
    is a binary outcome — not a probability. Scoring a signal on its own path
    would be look-ahead (risk R1), so the arm is built as a *trailing
    conditional rate*, exactly the way ``rank_score`` builds ``cell_p``:

      bucket_i = (symbol, direction, geometry_bucket(dist_tp, dist_sl))
      label_j  = did path_j touch TP before SL   (from ``walk``'s rule)
      p_i      = shrink( labels of j < i in the same bucket, k, population )

    Only strictly-earlier signals in the same symbol feed signal ``i``, so the
    estimate is walk-forward. ``n_paths_per_signal`` records how many paths
    stand behind each label (1 here) — kept explicit so a future multi-path
    variant is a visible change, not a silent one.

    **What this adds over the base-rate arm:** that one conditions on *symbol*
    only; this conditions on *symbol × bracket geometry*, which is the
    question the platform actually trades. If geometry carries no information,
    this arm collapses onto the base rate — and that is the honest finding.

    Returns ``{signal_id: p}`` plus the diagnostics the report needs.
    """
    from regime_platform.sweep_orchestrator.first_passage import first_passage

    # Time-ordered per symbol so "prior" is well-defined.
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in records:
        if x.get("symbol"):
            by_sym[x["symbol"]].append(x)

    probs: dict[str, float] = {}
    expiry: dict[str, float] = {}
    labels: dict[str, int] = {}
    buckets: dict[str, tuple] = {}
    skipped = 0
    no_path = 0

    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: str(x.get("ts") or ""))
        # trailing label store per bucket — only ever appended AFTER scoring
        hist: dict[tuple, list[int]] = defaultdict(list)
        all_labels: list[int] = []
        for x in rows:
            sid = x.get("signal_id")
            bucket = (sym, x.get("signal"), *geometry_bucket(x["dist_tp"], x["dist_sl"]))

            # score FIRST (uses only history), then append this row's label.
            # Both levels are shrunk toward neutral: the bucket rate toward the
            # running population, and the population itself toward 0.5 — so a
            # cold start cannot produce a confident 0.0/1.0 from two labels.
            prior = hist.get(bucket, [])
            pop_raw = (sum(all_labels) / len(all_labels)) if all_labels else 0.5
            pop = (sum(all_labels) + k * 0.5) / (len(all_labels) + k)
            if prior:
                p = (sum(prior) + k * pop_raw) / (len(prior) + k)
            else:
                p = pop          # no evidence in this bucket yet -> shrunk population
            if sid:
                probs[sid] = p
                buckets[sid] = bucket

            # label from this signal's own path (the walk rule)
            paths = paths_by_id.get(sid) if sid else None
            if paths is None:
                no_path += 1
                continue
            read = first_passage(
                paths, x["dist_tp"], x["dist_sl"], min_paths=n_paths_per_signal,
            )
            if read is None:
                skipped += 1
                continue
            lab = 1 if read.p_tp_first >= 0.5 else 0
            labels[sid] = lab
            expiry[sid] = read.expiry_share
            hist[bucket].append(lab)
            all_labels.append(lab)

    return {
        "p": probs,
        "labels": labels,
        "buckets": buckets,
        "expiry": expiry,
        "skipped": skipped,
        "no_path": no_path,
    }


def base_rate_arm(records: Sequence[dict[str, Any]], *, k: float = BASE_RATE_K) -> dict[str, Any]:
    """Arm 2 (the bar): shrunk per-symbol P(TP | decided).

    Shrinks each symbol's empirical TP share toward the population share with
    ``k`` pseudo-counts. Uses ALL decided rows for the symbol — the control
    model sees everything, which is exactly why beating it is meaningful.

    Also returns the population majority-direction accuracy, so the report can
    show both trivial baselines the HMM failed to clear.
    """
    by_sym_tp: dict[str, list[float]] = defaultdict(list)
    for x in records:
        by_sym_tp[x["symbol"]].append(x["y_bin"])
    all_tp = [v for vals in by_sym_tp.values() for v in vals]
    pop = statistics.fmean(all_tp) if all_tp else 0.5

    probs: dict[str, float] = {}
    for sym, vals in by_sym_tp.items():
        n = len(vals)
        probs[sym] = (sum(vals) + k * pop) / (n + k)
    return {"p": probs, "population_tp": pop, "n_symbols": len(by_sym_tp)}


def incumbent_arm(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Arm 3: the legacy HMM's directional call, scored on its own non-FLAT calls.

    Mirrors ``hmm_shadow_accuracy.compute_tier1_accuracy`` so the number here
    and the weekly diagnostic agree. Accuracy is computed only where the model
    actually called a direction (FLAT is "no answer", not a wrong answer).
    """
    calls = correct = 0
    for x in records:
        st = x.get("legacy_state")
        if not st or st == "FLAT":
            continue
        realized = _realized_direction(x.get("signal"), x.get("outcome"))
        if realized is None:
            continue
        calls += 1
        if st == realized:
            correct += 1
    return {"n_calls": calls, "n_correct": correct,
            "accuracy": (correct / calls) if calls else None}


def _realized_direction(signal: Any, outcome: Any) -> str | None:
    """Which way price actually went, in the markov UP/DOWN vocabulary."""
    if signal not in ("buy", "sell") or outcome not in ("tp_hit", "sl_hit"):
        return None
    hit = outcome == "tp_hit"
    if signal == "buy":
        return "UP" if hit else "DOWN"
    return "DOWN" if hit else "UP"


# ─── Gate + ΔD ─────────────────────────────────────────────────────────────


def gate_threshold(scores: Iterable[float], q: float = GATE_Q) -> float | None:
    """Score quantile used as the gate threshold (top ``1-q`` fraction passes)."""
    vals = sorted(v for v in scores if v is not None and math.isfinite(v))
    if len(vals) < 20:
        return None
    return vals[min(len(vals) - 1, int(q * (len(vals) - 1)))]


def delta_d_clustered(
    gated: Sequence[dict[str, Any]],
    rest: Sequence[dict[str, Any]],
    *,
    n_boot: int = 500,
    seed: int = 20261001,
) -> dict[str, Any]:
    """ΔD with a **symbol-day clustered** bootstrap (B7).

    Resamples whole (symbol, day) clusters rather than individual rows, because
    overlapping signals on one symbol-day are one bet, not many — the naive iid
    bootstrap used by the ranking eval understates the interval (its own §9.2
    flags this).
    """
    def clusters(rows: Sequence[dict[str, Any]]) -> dict[tuple, list[float]]:
        out: dict[tuple, list[float]] = defaultdict(list)
        for x in rows:
            ya = x.get("y_acc")
            if ya is None:
                continue
            ts = str(x.get("ts") or "")
            out[(x.get("symbol"), ts[:10])].append(float(ya))
        return out

    g = [v for vals in clusters(gated).values() for v in vals]
    r = [v for vals in clusters(rest).values() for v in vals]
    if not g or not r:
        return {"dd": None, "lo": None, "hi": None, "n_gated": len(g), "n_rest": len(r)}

    point = statistics.fmean(g) - statistics.fmean(r)
    g_c, r_c = clusters(gated), clusters(rest)
    g_keys, r_keys = list(g_c), list(r_c)
    if len(g_keys) < 5 or len(r_keys) < 5:
        return {"dd": point, "lo": None, "hi": None,
                "n_gated": len(g), "n_rest": len(r),
                "clusters_gated": len(g_keys), "clusters_rest": len(r_keys)}

    rng = np.random.default_rng(seed)
    stats_: list[float] = []
    for _ in range(n_boot):
        gs = rng.choice(len(g_keys), size=len(g_keys), replace=True)
        rs = rng.choice(len(r_keys), size=len(r_keys), replace=True)
        gv = [v for i in gs for v in g_c[g_keys[i]]]
        rv = [v for i in rs for v in r_c[r_keys[i]]]
        if gv and rv:
            stats_.append(statistics.fmean(gv) - statistics.fmean(rv))
    if not stats_:
        return {"dd": point, "lo": None, "hi": None,
                "n_gated": len(g), "n_rest": len(r), 
                "clusters_gated": len(g_keys), "clusters_rest": len(r_keys)}
    return {
        "dd": point,
        "lo": float(np.percentile(stats_, 2.5)),
        "hi": float(np.percentile(stats_, 97.5)),
        "n_gated": len(g),
        "n_rest": len(r),
        "clusters_gated": len(g_keys),
        "clusters_rest": len(r_keys),
    }


# ─── Scoring the arms ──────────────────────────────────────────────────────


def score_arm(
    records: Sequence[dict[str, Any]],
    arm: str,
    probs: dict[str, float] | None,
    *,
    k: float = BASE_RATE_K,
) -> dict[str, Any]:
    """Accuracy + skill-vs-base-rate + coverage for one arm.

    ``probs`` is ``{signal_id: p}`` for first-passage; the base-rate arm is
    keyed by symbol and handled by the caller passing per-symbol values.

    The base rate used for ``skill`` is computed **within this population** —
    the symbol's own majority TP/SL direction — so accuracy and skill are
    always measured against the same rows.
    """
    per_sym_tp: dict[str, list[float]] = defaultdict(list)
    for x in records:
        per_sym_tp[x["symbol"]].append(x["y_bin"])
    base = {s: (max(sum(v), len(v) - sum(v)) / len(v)) for s, v in per_sym_tp.items() if v}

    n_calls = n_correct = 0
    per_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    for x in records:
        p = None
        if arm == "first_passage":
            p = (probs or {}).get(x.get("signal_id"))
        elif arm == "base_rate":
            p = (probs or {}).get(x.get("symbol"))
        if p is None:
            continue
        # A probability arm "calls" when it leans past neutral — the same
        # non-FLAT-is-an-opinion rule the incumbent arm uses, so coverage and
        # accuracy are comparable across arms.
        if p == 0.5:
            continue
        called_tp = p > 0.5
        n_calls += 1
        if called_tp == x["tp_first"]:
            n_correct += 1
        per_symbol[x["symbol"]]["n"] += 1
        if called_tp == x["tp_first"]:
            per_symbol[x["symbol"]]["correct"] += 1

    accuracy = (n_correct / n_calls) if n_calls else None
    base_rate = (
        statistics.fmean([base[x["symbol"]] for x in records if x["symbol"] in base])
        if base else None
    )
    skills = []
    for sym, d in per_symbol.items():
        if d["n"] and sym in base:
            skills.append(d["correct"] / d["n"] - base[sym])
    return {
        "arm": arm,
        "n_calls": n_calls,
        "n_population": len(records),
        "coverage": (n_calls / len(records)) if records else None,
        "accuracy": accuracy,
        "base_rate": base_rate,
        "skill": (accuracy - base_rate) if (accuracy is not None and base_rate is not None) else None,
        "per_symbol_skill": skills,
        "positive_symbols": sum(1 for s in skills if s > 0),
        "n_symbols": len(skills),
    }


def apply_overlay(
    records: Sequence[dict[str, Any]],
    arm: str,
    probs: dict[str, float],
    *,
    carry_floor: float = 0.25,
) -> dict[str, Any]:
    """D5: damp each arm's probability by the regime-transition carry.

    Returns the overlay probabilities keyed the same way as ``probs``. Measured
    as an INCREMENT on top of the arm (bar B5) — the report compares
    ``arm`` vs ``arm+overlay`` on identical rows, so the difference is the
    overlay's contribution and nothing else.

    The carry uses the Kairos-proven primitives verbatim (freshness, type;
    surprise stays rejected). ``carry_floor`` matches ``FP_OVERLAY_W['floor']``.
    """
    out: dict[str, float] = {}
    for x in records:
        key = x.get("signal_id") if arm == "first_passage" else x.get("symbol")
        p = probs.get(key)
        if p is None:
            continue
        fresh = freshness_score(x.get("ewin"))
        typ = type_score(x.get("ttype"))
        combined = math.sqrt(max(0.0, fresh) * max(0.0, typ))
        carry = carry_floor + (1.0 - carry_floor) * combined
        out[key] = 0.5 + carry * (p - 0.5)
    return out


def evaluate_block(
    records: Sequence[dict[str, Any]],
    paths_by_id: dict[str, np.ndarray],
    label: str,
) -> dict[str, Any]:
    """Run all arms + the overlay increment on one population.

    Returns a structured result the report writer renders. Never raises on
    thin data — arms with no usable rows report ``None`` rather than a
    fabricated number.
    """
    fp = first_passage_arm(records, paths_by_id)
    br = base_rate_arm(records)
    inc = incumbent_arm(records)

    arms = {
        "first_passage": score_arm(records, "first_passage", fp["p"]),
        "base_rate": score_arm(records, "base_rate", br["p"]),
    }
    overlay = {
        "first_passage": score_arm(
            records, "first_passage", apply_overlay(records, "first_passage", fp["p"]),
        ),
        "base_rate": score_arm(
            records, "base_rate", apply_overlay(records, "base_rate", br["p"]),
        ),
    }

    # ΔD: gate on each arm's own score quantile, compare against the rest.
    # The overlaid score is evaluated as its own arm so the overlay's
    # contribution is isolated (B5) — shrinking toward 0.5 cannot move a
    # sign-based accuracy, only the ranking, so ΔD is the only metric that
    # can detect it.
    dds: dict[str, Any] = {}
    for arm_name, probs in (
        ("first_passage", fp["p"]),
        ("base_rate", br["p"]),
        ("first_passage_overlay", apply_overlay(records, "first_passage", fp["p"])),
        ("base_rate_overlay", apply_overlay(records, "base_rate", br["p"])),
    ):
        keyed_by_symbol = arm_name.startswith("base_rate")
        scored = []
        for x in records:
            key = x.get("symbol") if keyed_by_symbol else x.get("signal_id")
            p = probs.get(key)
            if p is not None:
                scored.append({**x, "_score": p})
        thr = gate_threshold([x["_score"] for x in scored])
        if thr is None:
            dds[arm_name] = {"dd": None}
            continue
        gated = [x for x in scored if x["_score"] >= thr]
        rest = [x for x in scored if x["_score"] < thr]
        dds[arm_name] = delta_d_clustered(gated, rest)

    # Honest-baseline check: how much of the arm's ΔD is just "TP happened
    # more often in the trailing window"? Measured by gating on a trailing
    # base rate that knows NOTHING about the bracket. If the first-passage
    # arm's ΔD is not materially above this, geometry added nothing.
    trailing_base = trailing_base_rate_arm(records)
    scored_tb = [{**x, "_score": trailing_base[x["signal_id"]]}
                 for x in records if x.get("signal_id") in trailing_base]
    thr_tb = gate_threshold([x["_score"] for x in scored_tb])
    if thr_tb is not None:
        dds["trailing_base_rate"] = delta_d_clustered(
            [x for x in scored_tb if x["_score"] >= thr_tb],
            [x for x in scored_tb if x["_score"] < thr_tb],
        )
    else:
        dds["trailing_base_rate"] = {"dd": None}

    return {
        "label": label,
        "n_population": len(records),
        "fp": fp,
        "br": br,
        "inc": inc,
        "arms": arms,
        "overlay": overlay,
        "dd": dds,
    }


def trailing_base_rate_arm(records: Sequence[dict[str, Any]]) -> dict[str, float]:
    """Honest-baseline control: trailing per-symbol TP rate, bracket-blind.

    Walk-forward, same no-lookahead rule as the first-passage arm, but it
    conditions on **nothing about the geometry**. Its ΔD is the floor that
    any geometry-aware arm has to clear — if first-passage does not beat
    this, the bracket conditioning bought nothing and the arm is just a
    momentum read.
    """
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in records:
        if x.get("symbol"):
            by_sym[x["symbol"]].append(x)

    out: dict[str, float] = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: str(x.get("ts") or ""))
        seen: list[int] = []
        for x in rows:
            sid = x.get("signal_id")
            if sid:
                out[sid] = (sum(seen) / len(seen)) if seen else 0.5
            seen.append(int(x["y_bin"]))
    return out


# ─── Bar verdict (mechanical, not hand-waved) ─────────────────────────────


def verdict(blocks: Sequence[dict[str, Any]], arm: str = "first_passage") -> dict[str, Any]:
    """Evaluate the pre-registered bar B1–B5 against the arm.

    Computed, never asserted by hand — the whole point of freezing the bar in
    the scope was that the answer should not depend on who reads the report.
    B6/B7 are methodological (enforced by construction) and are reported as
    satisfied-by-design rather than re-tested here.
    """
    results: dict[str, Any] = {}
    # Use the all-eras block (first) for B1–B3; the halves for B4.
    all_eras = blocks[0] if blocks else {}
    a = (all_eras.get("arms") or {}).get(arm) or {}
    br = (all_eras.get("arms") or {}).get("base_rate") or {}

    skill = a.get("skill")
    results["B1_beat_base_rate"] = {
        "pass": bool(skill is not None and skill > 0),
        "detail": f"skill {_f(skill)} vs base rate {_f(a.get('base_rate'))}",
    }
    cov = a.get("coverage")
    results["B2_coverage"] = {
        "pass": bool(cov is not None and cov >= 0.30),
        "detail": f"coverage {_f(cov)} (need >= 0.30)",
    }
    pos, tot = a.get("positive_symbols") or 0, a.get("n_symbols") or 0
    results["B3_consistency"] = {
        "pass": bool(tot and pos / tot >= 2 / 3),
        "detail": f"positive skill on {pos}/{tot} symbols (need >= 2/3)",
    }
    # B4: both halves must clear the honest floor (trailing_base_rate ΔD)
    halves = [b for b in blocks if b.get("label", "").endswith("half")]
    b4 = []
    for h in halves:
        fp_dd = ((h.get("dd") or {}).get(arm) or {}).get("dd")
        tb_dd = ((h.get("dd") or {}).get("trailing_base_rate") or {}).get("dd")
        b4.append(fp_dd is not None and tb_dd is not None and fp_dd >= tb_dd)
    results["B4_era_halves"] = {
        "pass": bool(b4) and all(b4),
        "detail": f"clears the bracket-blind floor in {sum(b4)}/{len(b4)} halves",
    }
    # B5: the overlay's own increment. Measured on **ΔD of the overlaid score**,
    # NOT on sign-accuracy: shrinking a probability toward 0.5 preserves its
    # sign, so an accuracy metric is structurally blind to the overlay (it can
    # only ever report exactly 0.0000). What the overlay actually changes is the
    # *ranking* — a fresh∧dir_flip signal keeps more of its level, so it sorts
    # above a deep∧vol_contract one at the same bare probability. ΔD is the
    # metric that can see that.
    b5_bare = ((all_eras.get("dd") or {}).get(arm) or {}).get("dd")
    b5_over = ((all_eras.get("dd") or {}).get(f"{arm}_overlay") or {}).get("dd")
    delta_dd = None
    if b5_bare is not None and b5_over is not None:
        delta_dd = b5_over - b5_bare
    results["B5_overlay_increment"] = {
        "pass": bool(delta_dd is not None and delta_dd > 0),
        "detail": f"overlay ΔD {_f(b5_over)} vs bare {_f(b5_bare)} (Δ {_f(delta_dd)})",
    }
    results["_summary"] = {
        "passed": sum(1 for k, v in results.items() if k.startswith("B") and v.get("pass")),
        "total": 5,
    }
    return results


def _f(v: Any, spec: str = ".4f") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+{spec}}"
    except (TypeError, ValueError):
        return str(v)
