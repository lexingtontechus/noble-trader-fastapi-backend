#!/usr/bin/env python3
"""measure_pbm_calibration.py — Phase B calibration audit (proper scoring, measured or nothing).

The missing instrument. The Phase E harness (`measure_pbm_drift_auc.py`) scores
barrier-orientation **AUC** — a RANK statistic. AUC is invariant to any monotone
recalibration of the scores, so a layer that predicts 0.60 when reality is 0.30
scores exactly the same AUC as one that predicts 0.30. Overconfidence is
invisible to it. This harness measures what AUC cannot: **Brier, log-loss, and
the reliability curve** of each probability layer, era-clean, off the
`nt_pbm_shadow` prediction ledger.

Layers scored on ONE joined population (see "Join" below):

    posterior        nt_pbm_shadow.p_w                PBM Dirichlet posterior mean
    table            nt_signal_sim.p_win_bracket      reachability lookup — the INCUMBENT
    raw_directional  nt_signal_sim.predicted_p_win_raw  EV-engine blend, PRE-bracket
    null             nt_pbm_shadow.p_w_null           analytic barrier null (geometry only)

`table` is the layer Phase B replaces; `raw_directional` is the directional model
the EV engine prices on before the bracket substitution. NOTE: in the live path
`nt_signal_sim.p_win_dir` is a COPY of `p_win_bracket` (`orchestrator.py:5650`
`_p_win_dir = _p_win_bracket`), so the genuine directional series is
`predicted_p_win_raw` (captured at `orchestrator.py:4624`, before the bracket
swap at `:4631`) — this harness says so in the report rather than scoring two
identical columns.

Metrics (binary, target y = 1[tp_hit], the event every layer's p_W names):
    Brier        mean((p - y)^2)                — lower is better
    BSS          Brier skill vs climatology      — 1 - BS/BS_ref, >0 beats base rate
    log-loss     -mean(y ln p + (1-y) ln(1-p))  — lower is better
    ECE / MCE    expected / max calibration error over equal-width bins
    bias         mean(p) - mean(y)              — sign = over/under-confidence
    reliability  per-decile mean(p) vs realized — the curve itself

Also a three-outcome read (tp / sl / expired) for the layers that carry the full
triple (posterior native, null recomputed, table reconstructed via `p_resolve`),
so expiry survivorship is scored, not dropped.

Era discipline: `nt_pbm_shadow.era = 'live'` AND `nt_signal_sim.contaminated =
false` AND `ts >= nt_calibration_cutover` (read via
`bracket_reachability._fetch_cutover_at` — the one marker reachability, the bias
loop, atr_reopt and the Phase E harness all use). Fail-open to all rows if the
marker is missing, and the report SAYS SO.

Join: `nt_pbm_shadow` does NOT store the outcome (migration 211: "backfilled by
the acceptance harness") and its `ts` is the sweep wall-clock
(`orchestrator.py:4292` `datetime.now(...)`), ~2.5s off the `nt_signal_sim` row
it describes. So the harness joins on `(symbol, signal)` + NEAREST `ts` within
`--join-tol` seconds (default 60), and reports the match rate and |dt| quantiles
so the join is auditable, never assumed.

Offline and read-only: fetches over Supabase REST (1000/page), writes one
markdown report. No live wiring, no writes, no flags flipped.

Verdict: a POOLED, coverage-gated flip bar (NO-GO / PASS-FLIP / PASS-POINT /
FAIL) — it refuses a verdict below ``--min-n`` scored AND paired rows rather
than reading one off a handful of rows, and its PASS reason carries an
absolute-skill clause: a pass against a table that is itself worse than
climatology is a RELATIVE pass, and the report says so.

Run:
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/measure_pbm_calibration.py
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/measure_pbm_calibration.py --selftest

Offline verification needs no network: ``--selftest`` exercises every pure stage
(scoring, triples, join, bootstrap, verdict branches, renderer) on synthetic data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# .env.local first (same pattern as the sibling harnesses) so the
# regime_platform settings module sees SUPABASE_URL/KEY at import time.
_ENV_FILE = ROOT / ".env.local"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

from regime_platform.core.settings import settings  # noqa: E402
from regime_platform.sweep_orchestrator.barrier import barrier_null  # noqa: E402
from regime_platform.sweep_orchestrator.bracket_reachability import (  # noqa: E402
    _fetch_cutover_at,
)
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url  # noqa: E402

logger = logging.getLogger("measure_pbm_calibration")

REPORT_PATH = ROOT / "worklog" / "kairos" / "reports" / "20261016_pbm_calibration.md"
PAGE = 1000
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_JOIN_TOL = 60.0
DEFAULT_BINS = 10
DEFAULT_BOOTSTRAP = 400
DEFAULT_SEED = 13
DEFAULT_MIN_N = 500
DEFAULT_MIN_SYMBOL_N = 30
_EPS = 1e-6

SHADOW_COLUMNS = (
    "symbol,ts,signal,source,tp_atr,sl_atr,t_bars,sigma,"
    "p_w,p_l,p_e,p_w_null,gap,n_eff,ev_atr,ev_lb_atr,ev_lb_pass,"
    "bracket_bucket,vol_tier,deadline_class,era,outcome"
)
SIM_COLUMNS = (
    "symbol,ts,signal,outcome,p_win,p_win_dir,p_win_bracket,p_resolve,"
    "p_win_resolve,predicted_p_win_raw,qualified,contaminated,regime"
)

# (key, display, extractor(x=shadow row, s=sim row) -> p_W | None)
Layer = tuple[str, str, Callable[[dict[str, Any], dict[str, Any]], float | None]]
LAYERS: tuple[Layer, ...] = (
    ("posterior", "PBM posterior p_W", lambda x, s: _f(x.get("p_w"))),
    ("table", "reachability table (incumbent)", lambda x, s: _f(s.get("p_win_bracket"))),
    (
        "raw_directional",
        "raw directional blend (pre-bracket)",
        lambda x, s: _f(s.get("predicted_p_win_raw")),
    ),
    ("null", "analytic barrier null", lambda x, s: _f(x.get("p_w_null"))),
)

DECIDED = ("tp_hit", "sl_hit")
ALL_OUTCOMES = ("tp_hit", "sl_hit", "expired")


# ─── Small pure helpers ─────────────────────────────────────────────────────


def _f(raw: Any) -> float | None:
    """Finite float in [0, 1], else None (missing / null / NaN collapse)."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return min(1.0, max(0.0, v))


def _finite(raw: Any) -> float | None:
    """Finite float with NO [0,1] clamp — for geometry (tp_atr / sl_atr / sigma).

    ``_f`` clamps to [0,1] because it parses PROBABILITIES; feeding it a
    distance silently rewrites a 6.4-ATR bracket to 1.0 and prices the null at
    a fictitious symmetric bracket (its p_E collapses to 0). Geometry goes
    through here instead.
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fmt(value: float | None, spec: str = ".4f", plus: bool = False) -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), ("+" if plus else "") + spec)
    except (TypeError, ValueError):
        return str(value)


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_since(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise SystemExit(f"--since is not an ISO date/datetime: {raw!r} ({exc})") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


# ─── Proper scoring rules (pure — no sklearn) ───────────────────────────────


def brier(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """mean((p - y)^2) — the bounded proper score. Lower is better."""
    if not scores:
        return None
    return float(np.mean((np.asarray(scores, dtype=np.float64) - np.asarray(labels, dtype=np.float64)) ** 2))


def log_loss(scores: Sequence[float], labels: Sequence[int], eps: float = _EPS) -> float | None:
    """-mean(y ln p + (1-y) ln(1-p)), p clipped to [eps, 1-eps]. Lower is better."""
    if not scores:
        return None
    p = np.clip(np.asarray(scores, dtype=np.float64), eps, 1.0 - eps)
    y = np.asarray(labels, dtype=np.float64)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def climatology_brier(labels: Sequence[int]) -> float | None:
    """Brier of the constant base-rate predictor — the skill reference."""
    if not labels:
        return None
    base = float(np.mean(np.asarray(labels, dtype=np.float64)))
    return base * (1.0 - base)


def brier_skill_score(bs: float | None, bs_ref: float | None) -> float | None:
    """1 - BS/BS_ref. >0 beats climatology, 0 ties it, <0 is worse."""
    if bs is None or bs_ref is None or bs_ref <= 0:
        return None
    return 1.0 - bs / bs_ref


def reliability_curve(scores: Sequence[float], labels: Sequence[int], n_bins: int) -> list[dict[str, Any]]:
    """Equal-width bins over [0,1]: per-bin n, mean prediction, realized rate, gap.

    ``gap = mean_pred - realized``: positive = over-confident in that bin.
    """
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, y in zip(scores, labels):
        idx = min(n_bins - 1, max(0, int(p * n_bins)))
        buckets[idx].append((float(p), int(y)))
    rows: list[dict[str, Any]] = []
    for i, b in enumerate(buckets):
        if not b:
            rows.append(
                {"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "mean_pred": None, "realized": None, "gap": None}
            )
            continue
        mp = sum(p for p, _ in b) / len(b)
        mr = sum(y for _, y in b) / len(b)
        rows.append(
            {"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": len(b), "mean_pred": mp, "realized": mr, "gap": mp - mr}
        )
    return rows


def ece(curve: list[dict[str, Any]]) -> float | None:
    """Expected calibration error — n-weighted mean |gap| over populated bins."""
    tot = sum(r["n"] for r in curve)
    if tot == 0:
        return None
    return sum(r["n"] * abs(r["gap"]) for r in curve if r["n"]) / tot


def mce(curve: list[dict[str, Any]]) -> float | None:
    """Maximum calibration error — worst populated bin's |gap|."""
    gaps = [abs(r["gap"]) for r in curve if r["n"]]
    return max(gaps) if gaps else None


def bootstrap_brier_diff(
    scores_a: Sequence[float],
    scores_b: Sequence[float],
    labels: Sequence[int],
    n_boot: int,
    seed: int,
) -> tuple[float, float] | None:
    """Percentile CI on (Brier_a - Brier_b), row-level resamples.

    Negative CI upper bound ⇒ layer A is significantly better calibrated
    (lower Brier) than B on this population. Row-level by design (matches the
    Phase E harness's own caveat: overlapping same-symbol signals inflate n).
    """
    n = len(labels)
    if n == 0 or n_boot <= 0:
        return None
    a = np.asarray(scores_a, dtype=np.float64)
    b = np.asarray(scores_b, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    rng = np.random.default_rng(seed)
    vals: list[float] = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        vals.append(float(np.mean((a[idx] - y[idx]) ** 2) - np.mean((b[idx] - y[idx]) ** 2)))
    if len(vals) < max(10, n_boot // 2):
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return (float(lo), float(hi))


# ─── Three-outcome scoring ──────────────────────────────────────────────────


def multiclass_brier(triples: Sequence[Sequence[float]], onehots: Sequence[Sequence[float]]) -> float | None:
    """mean over rows of sum_c (p_c - y_c)^2 — the multi-class Brier."""
    if not triples:
        return None
    p = np.asarray(triples, dtype=np.float64)
    y = np.asarray(onehots, dtype=np.float64)
    return float(np.mean(np.sum((p - y) ** 2, axis=1)))


def multiclass_log_loss(
    triples: Sequence[Sequence[float]], onehots: Sequence[Sequence[float]], eps: float = _EPS
) -> float | None:
    """-mean ln p_actual — the multi-class log score (lower is better)."""
    if not triples:
        return None
    p = np.clip(np.asarray(triples, dtype=np.float64), eps, 1.0)
    y = np.asarray(onehots, dtype=np.float64)
    return float(-np.mean(np.sum(y * np.log(p), axis=1)))


def _renorm(triple: tuple[float, float, float]) -> tuple[float, float, float]:
    """Clip negatives to 0 and renormalise to a probability triple."""
    vals = [max(0.0, float(v)) for v in triple]
    s = sum(vals)
    if s <= 0:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    return (vals[0] / s, vals[1] / s, vals[2] / s)


def posterior_triple(x: dict[str, Any]) -> tuple[float, float, float] | None:
    pw, pl, pe = _f(x.get("p_w")), _f(x.get("p_l")), _f(x.get("p_e"))
    if pw is None or pl is None or pe is None:
        return None
    return _renorm((pw, pl, pe))


def null_triple(x: dict[str, Any]) -> tuple[float, float, float] | None:
    """Recompute the analytic null triple from the shadow's stored geometry.

    Geometry goes through ``_finite`` (no [0,1] clamp) — see its docstring.
    """
    tp, sl = _finite(x.get("tp_atr")), _finite(x.get("sl_atr"))
    t = x.get("t_bars")
    sig = _finite(x.get("sigma"))
    if tp is None or sl is None or not t or sig is None or tp <= 0 or sl <= 0:
        return None
    try:
        n = barrier_null(tp, sl, t_bars=int(t), sigma=float(sig))
    except Exception:
        return None
    return _renorm((n.p_win, n.p_loss, n.p_expire))


def table_triple(x: dict[str, Any], s: dict[str, Any]) -> tuple[float, float, float] | None:
    """Reconstruct the table's three outcomes from (p_win_bracket, p_resolve)."""
    pw = _f(s.get("p_win_bracket"))
    pr = _f(s.get("p_resolve"))
    if pw is None or pr is None:
        return None
    pe = 1.0 - pr
    pl = pr - pw
    return _renorm((pw, pl, pe))


# ─── Join ───────────────────────────────────────────────────────────────────


def join_ledger(
    shadow: list[dict[str, Any]],
    sim: list[dict[str, Any]],
    tol_seconds: float,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Join shadow→sim on (symbol, signal) + NEAREST ts within tol_seconds.

    Returns (pairs, diagnostics). Diagnostics carry the match rate and |dt|
    quantiles so the join is auditable rather than assumed.
    """
    sim_by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in sim:
        sim_by[(str(r.get("symbol")), str(r.get("signal")))].append(r)
    for key in sim_by:
        sim_by[key].sort(key=lambda r: _parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    deltas: list[float] = []
    no_candidate = 0
    out_of_tol = 0
    for x in shadow:
        cands = sim_by.get((str(x.get("symbol")), str(x.get("signal"))), [])
        if not cands:
            no_candidate += 1
            continue
        xt = _parse_ts(x.get("ts"))
        if xt is None:
            no_candidate += 1
            continue
        best = min(
            cands,
            key=lambda r: abs(((_parse_ts(r.get("ts")) or xt) - xt).total_seconds()),
        )
        bt = _parse_ts(best.get("ts"))
        d = abs((bt - xt).total_seconds()) if bt else float("inf")
        if d <= tol_seconds:
            pairs.append((x, best))
            deltas.append(d)
        else:
            out_of_tol += 1
    deltas.sort()
    diag = {
        "shadow_n": len(shadow),
        "sim_n": len(sim),
        "joined": len(pairs),
        "match_rate": len(pairs) / len(shadow) if shadow else 0.0,
        "no_candidate": no_candidate,
        "out_of_tol": out_of_tol,
        "dt_median": deltas[len(deltas) // 2] if deltas else None,
        "dt_p90": deltas[int(len(deltas) * 0.9)] if deltas else None,
        "dt_max": deltas[-1] if deltas else None,
        "tol": tol_seconds,
    }
    return pairs, diag


# ─── Measurement ────────────────────────────────────────────────────────────


def _layer_series(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], layer: Layer
) -> tuple[list[float], list[int], list[dict[str, Any]]]:
    """(scores, labels, kept pairs) for one layer over decided-or-all rows.

    ``kept`` preserves the row so per-bucket splits can reuse it.
    """
    _key, _disp, fn = layer
    scores, labels, kept = [], [], []
    for x, s in pairs:
        p = fn(x, s)
        if p is None:
            continue
        y = 1 if s.get("outcome") == "tp_hit" else 0
        scores.append(p)
        labels.append(y)
        kept.append({"x": x, "s": s, "p": p, "y": y})
    return scores, labels, kept


def score_layer(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    layer: Layer,
    n_bins: int,
) -> dict[str, Any]:
    """Full binary scorecard for one layer over the given population."""
    scores, labels, kept = _layer_series(pairs, layer)
    n = len(scores)
    if n == 0:
        return {"key": layer[0], "name": layer[1], "n": 0}
    curve = reliability_curve(scores, labels, n_bins)
    bs = brier(scores, labels)
    ref = climatology_brier(labels)
    return {
        "key": layer[0],
        "name": layer[1],
        "n": n,
        "mean_pred": float(np.mean(scores)),
        "realized": float(np.mean(labels)),
        "bias": float(np.mean(scores)) - float(np.mean(labels)),
        "brier": bs,
        "brier_ref": ref,
        "bss": brier_skill_score(bs, ref),
        "log_loss": log_loss(scores, labels),
        "ece": ece(curve),
        "mce": mce(curve),
        "curve": curve,
        "scores": scores,
        "labels": labels,
        "kept": kept,
    }


def score_three_outcome(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """Multi-class Brier + log-loss per triple-bearing layer (all era-clean rows)."""
    builders: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[float, float, float] | None]] = {
        "posterior": lambda x, s: posterior_triple(x),
        "table": lambda x, s: table_triple(x, s),
        "null": lambda x, s: null_triple(x),
    }
    out: dict[str, dict[str, Any]] = {}
    for name, build in builders.items():
        triples, onehots = [], []
        for x, s in pairs:
            tri = build(x, s)
            if tri is None:
                continue
            oc = s.get("outcome")
            if oc not in ALL_OUTCOMES:
                continue
            one = (1.0, 0.0, 0.0) if oc == "tp_hit" else (0.0, 1.0, 0.0) if oc == "sl_hit" else (0.0, 0.0, 1.0)
            triples.append(tri)
            onehots.append(one)
        if not triples:
            out[name] = {"n": 0}
            continue
        out[name] = {
            "n": len(triples),
            "brier": multiclass_brier(triples, onehots),
            "log_loss": multiclass_log_loss(triples, onehots),
            "mean_p_e": float(np.mean([t[2] for t in triples])),
        }
    return out


def conditional_q(triple: Sequence[float] | None) -> float | None:
    """q = P(tp | resolve) — the expiry-free directional probability."""
    if triple is None:
        return None
    p_w, p_l = float(triple[0]), float(triple[1])
    denom = p_w + p_l
    if denom <= 0 or not math.isfinite(denom):
        return None
    return p_w / denom


def _layer_triple(x: dict[str, Any], s: dict[str, Any], key: str) -> tuple[float, float, float] | None:
    if key == "posterior":
        return posterior_triple(x)
    if key == "table":
        return table_triple(x, s)
    if key == "null":
        return null_triple(x)
    return None


def _cond_scorecard(qs: Sequence[float], ys: Sequence[int], eps: float = _EPS) -> dict[str, Any]:
    """Brier / log-loss / bias for a set of conditional probabilities."""
    n = len(qs)
    if n == 0:
        return {"n": 0}
    q = np.asarray(qs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    c = np.clip(q, eps, 1.0 - eps)
    return {
        "n": n,
        "mean_q": float(q.mean()),
        "realized": float(y.mean()),
        "bias": float(q.mean() - y.mean()),
        "brier": float(np.mean((q - y) ** 2)),
        "log_loss": float(-np.mean(y * np.log(c) + (1.0 - y) * np.log(1.0 - c))),
        "degenerate": int(np.sum((q <= 0.0) | (q >= 1.0))),
    }


def score_conditional(pairs: list[tuple[dict[str, Any], dict[str, Any]]], *, n_boot: int, seed: int) -> dict[str, Any]:
    """The expiry-free conditional read: q = P(tp | resolve) vs the realized outcome.

    This is the DURABLE half of the Phase B bar. On a young window with zero
    realized expiries the full-triple log-score is dominated by the
    resolve-mass term (see ``log_score_decomposition``); the conditional read
    is the part that does not depend on the expiry LEVEL.

    It also reports the DEGENERATE tail — rows where a layer emits q == 0 or
    q == 1 exactly. A hard 0/1 on a binary event is a "never say never"
    defect, and under a log score the 1e-6 clip turns it into a −13.8 penalty
    that can manufacture a large, spurious advantage for the other layer. The
    ``ex_degenerate`` block removes those rows so any advantage can be
    attributed honestly rather than to the clip.
    """
    q_by: dict[str, list[float | None]] = {lay[0]: [] for lay in LAYERS}
    ys: list[int] = []
    for x, s in pairs:
        if s.get("outcome") not in DECIDED:
            continue
        ys.append(1 if s.get("outcome") == "tp_hit" else 0)
        for lay in LAYERS:
            q_by[lay[0]].append(conditional_q(_layer_triple(x, s, lay[0])))

    cards: dict[str, dict[str, Any]] = {}
    for lay in LAYERS:
        z = [(q, y) for q, y in zip(q_by[lay[0]], ys) if q is not None]
        cards[lay[0]] = _cond_scorecard([q for q, _ in z], [y for _, y in z])

    def _paired(mask: Callable[[float, float], bool] | None) -> dict[str, Any]:
        z = [
            (qp, qt, y)
            for qp, qt, y in zip(q_by["posterior"], q_by["table"], ys)
            if qp is not None and qt is not None and (mask is None or mask(qp, qt))
        ]
        if not z:
            return {"n": 0, "delta_brier": None, "delta_log_loss": None, "ci": None}
        pa = [a for a, _, _ in z]
        pb = [b for _, b, _ in z]
        yl = [y for _, _, y in z]
        ca = _cond_scorecard(pa, yl)
        cb = _cond_scorecard(pb, yl)
        return {
            "n": len(z),
            "delta_brier": (ca["brier"] or 0.0) - (cb["brier"] or 0.0),
            "delta_log_loss": (ca["log_loss"] or 0.0) - (cb["log_loss"] or 0.0),
            "ci": bootstrap_brier_diff(pa, pb, yl, n_boot, seed),
            "posterior": ca,
            "table": cb,
        }

    degenerate = lambda qp, qt: (qp > 0.0 and qp < 1.0) and (qt > 0.0 and qt < 1.0)  # noqa: E731
    return {
        "layers": cards,
        "paired": _paired(None),
        "paired_ex_degenerate": _paired(degenerate),
    }


def log_score_decomposition(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], *, n_boot: int, seed: int
) -> dict[str, Any]:
    """Split the full-triple log-score delta into its two EXACT parts.

    Per row, ``log p(outcome) = log q + log(p_W + p_L)`` exactly (q is the
    conditional P(tp|resolve)). So the paired mean delta decomposes as

        Δlog p(outcome)  =  Δlog q  +  Δlog(resolve mass)

    The second term is the EXPIRY term. On a window where nothing expired it
    is what the table's implied resolve mass costs it, and it is not
    directional skill. Reporting the split stops a young window's expiry
    asymmetry from reading as an edge.

    ``lq`` is defined as ``lf - lr`` rather than an independently clipped
    ``log q``, so the split is EXACT by construction. That matters on
    degenerate rows: a hard ``p_W = 0`` makes an independently-clipped
    ``log q`` arbitrary, and the two sides would stop summing.
    """
    lq_p, lq_t, lr_p, lr_t, lf_p, lf_t = [], [], [], [], [], []
    for x, s in pairs:
        if s.get("outcome") not in DECIDED:
            continue
        tri_p = posterior_triple(x)
        tri_t = table_triple(x, s)
        if tri_p is None or tri_t is None:
            continue
        y = 1 if s.get("outcome") == "tp_hit" else 0
        for tri, lq, lr, lf in ((tri_p, lq_p, lr_p, lf_p), (tri_t, lq_t, lr_t, lf_t)):
            rmass = tri[0] + tri[1]
            f = math.log(max(tri[0] if y == 1 else tri[1], _EPS))
            r = math.log(max(rmass, _EPS))
            lf.append(f)
            lr.append(r)
            lq.append(f - r)

    if not lf_p:
        return {"n": 0}

    def _ci(a: Sequence[float], b: Sequence[float]) -> tuple[float, float] | None:
        d = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
        n = d.size
        if n == 0 or n_boot <= 0:
            return None
        rng = np.random.default_rng(seed)
        vals = [float(d[rng.integers(0, n, size=n)].mean()) for _ in range(n_boot)]
        lo, hi = np.percentile(vals, [2.5, 97.5])
        return (float(lo), float(hi))

    return {
        "n": len(lf_p),
        "full": float(np.mean(lf_p) - np.mean(lf_t)),
        "cond": float(np.mean(lq_p) - np.mean(lq_t)),
        "resolve": float(np.mean(lr_p) - np.mean(lr_t)),
        "full_ci": _ci(lf_p, lf_t),
        "cond_ci": _ci(lq_p, lq_t),
        "resolve_ci": _ci(lr_p, lr_t),
        "implied_resolve_posterior": float(np.exp(np.mean(lr_p))),
        "implied_resolve_table": float(np.exp(np.mean(lr_t))),
    }


def split_scores(scorecards: dict[str, dict[str, Any]], field: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Per-value scorecard split on a shadow row field (bracket_bucket / vol_tier)."""
    groups: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for key, sc in scorecards.items():
        for row in sc.get("kept", []):
            val = str(row["x"].get(field) or "null")
            groups[val][key].append(row)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for val, by_layer in groups.items():
        out[val] = {}
        for key, rows in by_layer.items():
            scores = [r["p"] for r in rows]
            labels = [r["y"] for r in rows]
            bs = brier(scores, labels)
            ref = climatology_brier(labels)
            out[val][key] = {
                "n": len(rows),
                "mean_pred": float(np.mean(scores)),
                "realized": float(np.mean(labels)),
                "bias": float(np.mean(scores)) - float(np.mean(labels)),
                "brier": bs,
                "bss": brier_skill_score(bs, ref),
                "ece": ece(reliability_curve(scores, labels, DEFAULT_BINS)),
            }
    return out


def score_per_symbol(
    scorecards: dict[str, dict[str, Any]],
    *,
    min_symbol_n: int,
    n_boot: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Per-symbol scorecard per layer + the paired ΔBrier(posterior − table).

    Ported from the sibling harness's per-symbol Δ table (its most actionable
    section), expressed in this harness's currency: per-symbol Brier / bias /
    BSS / ECE per layer, plus the paired ΔBrier with a CI that prints only at
    n >= min_symbol_n — thin symbols are noise, not votes.
    """
    by_sym: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for key, sc in scorecards.items():
        for row in sc.get("kept", []):
            by_sym[str(row["x"].get("symbol") or "?")][key].append(row)

    def _rk(row: dict[str, Any]) -> tuple[Any, Any]:
        return (row["x"].get("symbol"), row["x"].get("ts"))

    out: list[dict[str, Any]] = []
    for sym in sorted(by_sym):
        layers = by_sym[sym]
        rec: dict[str, Any] = {"symbol": sym, "n": max(len(v) for v in layers.values())}
        for lay in LAYERS:
            rows = layers.get(lay[0], [])
            if not rows:
                continue
            scores = [r["p"] for r in rows]
            labels = [r["y"] for r in rows]
            bs = brier(scores, labels)
            rec[lay[0]] = {
                "n": len(rows),
                "mean_pred": float(np.mean(scores)),
                "realized": float(np.mean(labels)),
                "bias": float(np.mean(scores)) - float(np.mean(labels)),
                "brier": bs,
                "bss": brier_skill_score(bs, climatology_brier(labels)),
                "ece": ece(reliability_curve(scores, labels, DEFAULT_BINS)),
            }
        # paired ΔBrier(posterior − table) on the rows carrying both.
        # Tuple order matters: (posterior, table, y) so the mean is
        # Brier_posterior − Brier_table — the SAME sign convention as the
        # pooled sig["brier_delta"] (negative ⇒ posterior better). Swapping
        # these two makes the per-symbol column read the exact opposite of the
        # pooled verdict.
        post = {_rk(r): r for r in layers.get("posterior", [])}
        paired = [(post[_rk(r)]["p"], r["p"], r["y"]) for r in layers.get("table", []) if _rk(r) in post]
        rec["delta"] = None
        if paired:
            pa = [p for p, _, _ in paired]
            pb = [q for _, q, _ in paired]
            yl = [y for _, _, y in paired]
            rec["delta"] = {
                "n": len(paired),
                "mean": (brier(pa, yl) or 0.0) - (brier(pb, yl) or 0.0),
                "ci": bootstrap_brier_diff(pa, pb, yl, n_boot, seed) if len(paired) >= min_symbol_n else None,
            }
        out.append(rec)
    return out


def measure(
    pairs: list[dict[str, Any]],
    *,
    diag: dict[str, Any],
    cutover_iso: str | None,
    n_bins: int,
    n_boot: int,
    seed: int,
    since: datetime,
    min_n: int = DEFAULT_MIN_N,
    min_symbol_n: int = DEFAULT_MIN_SYMBOL_N,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Run the whole audit on the joined, era-clean population."""
    cutover_dt = _parse_ts(cutover_iso) if cutover_iso else None

    era_clean = [
        (x, s)
        for x, s in pairs
        if str(x.get("era") or "live") == "live"
        and not bool(s.get("contaminated"))
        and (cutover_dt is None or (_parse_ts(x.get("ts")) or cutover_dt) >= cutover_dt)
    ]
    n_pre = len(pairs) - len(era_clean)

    all_rows = [(x, s) for x, s in era_clean if s.get("outcome") in ALL_OUTCOMES]
    decided = [(x, s) for x, s in era_clean if s.get("outcome") in DECIDED]
    expired = [(x, s) for x, s in era_clean if s.get("outcome") == "expired"]

    sc_all = {lay[0]: score_layer(all_rows, lay, n_bins) for lay in LAYERS}
    sc_dec = {lay[0]: score_layer(decided, lay, n_bins) for lay in LAYERS}
    three = score_three_outcome(all_rows)

    # Significance: posterior vs the incumbent table, on the all-rows population.
    sig: dict[str, Any] = {}
    a, b = sc_all.get("posterior", {}), sc_all.get("table", {})
    if a.get("scores") and b.get("scores"):
        # align on the rows where BOTH layers have a value
        common = [
            (x, s) for x, s in all_rows if _f(x.get("p_w")) is not None and _f(s.get("p_win_bracket")) is not None
        ]
        pa = [_f(x.get("p_w")) for x, _ in common]
        pb = [_f(s.get("p_win_bracket")) for _, s in common]
        yl = [1 if s.get("outcome") == "tp_hit" else 0 for _, s in common]
        sig["n_common"] = len(common)
        sig["brier_posterior"] = brier(pa, yl)
        sig["brier_table"] = brier(pb, yl)
        sig["diff_ci"] = bootstrap_brier_diff(pa, pb, yl, n_boot, seed)
        if sig["brier_posterior"] is not None and sig["brier_table"] is not None:
            sig["brier_delta"] = sig["brier_posterior"] - sig["brier_table"]

    return {
        "since": since,
        "symbols": symbols,
        "diag": diag,
        "cutover_iso": cutover_iso,
        "fail_open": cutover_dt is None,
        "pre_cutover_dropped": n_pre,
        "era_clean": len(era_clean),
        "scored": len(all_rows),
        "decided": len(decided),
        "expired": len(expired),
        "tp": sum(1 for _, s in all_rows if s.get("outcome") == "tp_hit"),
        "sl": sum(1 for _, s in all_rows if s.get("outcome") == "sl_hit"),
        "qualified": sum(1 for _, s in all_rows if s.get("qualified")),
        "bucket_mix": Counter(str(x.get("bracket_bucket") or "null") for x, _ in all_rows),
        "tier_mix": Counter(str(x.get("vol_tier") or "null") for x, _ in all_rows),
        "sc_all": sc_all,
        "sc_dec": sc_dec,
        "three": three,
        "sig": sig,
        "by_bucket": split_scores(sc_all, "bracket_bucket"),
        "by_tier": split_scores(sc_all, "vol_tier"),
        "per_symbol": score_per_symbol(sc_all, min_symbol_n=min_symbol_n, n_boot=n_boot, seed=seed),
        "conditional": score_conditional(decided, n_boot=n_boot, seed=seed),
        "decomp": log_score_decomposition(decided, n_boot=n_boot, seed=seed),
        "n_bins": n_bins,
        "n_boot": n_boot,
        "seed": seed,
        "min_n": min_n,
        "min_symbol_n": min_symbol_n,
    }


# ─── Verdict (mechanically applied) ─────────────────────────────────────────


def pooled_verdict(ctx: dict[str, Any]) -> tuple[str, str]:
    """The flip bar, POOLED and coverage-gated (ported from the sibling harness).

    The sibling harness's best piece of discipline: refuse a verdict below a
    coverage floor rather than reading one off a handful of rows. PASS-FLIP needs
    ΔBrier(posterior − table) clearly NEGATIVE (lower Brier is better): point
    estimate < 0 AND bootstrap 95% CI upper < 0, era-clean, on >= min_n scored
    AND >= min_n paired rows.

    The absolute-skill clause is THIS harness's addition: a pass against a table
    that is itself worse than climatology is a RELATIVE pass only, and the reason
    string says so rather than letting PASS-FLIP read as "calibrated".
    """
    gates: list[str] = []
    if ctx["fail_open"]:
        gates.append("era marker unavailable (fail-open)")
    if ctx["scored"] < ctx["min_n"]:
        gates.append(f"ledger depth {ctx['scored']} < {ctx['min_n']} scored rows")
    paired_n = int(ctx["sig"].get("n_common") or 0)
    if paired_n < ctx["min_n"]:
        gates.append(f"paired depth {paired_n} < {ctx['min_n']} (posterior ∧ table scoreable)")
    if gates:
        return ("NO-GO", "; ".join(gates) + " — keep collecting, discovery-grade only")

    d = ctx["sig"].get("brier_delta")
    ci = ctx["sig"].get("diff_ci")
    if d is None:
        return ("NO-GO", "no paired rows — discovery-grade only")
    if d >= 0:
        return ("FAIL", f"ΔBrier(posterior − table) {d:+.4f} ≥ 0 — the table lookup prices better")
    if ci is not None and ci[1] < 0:
        v, reason = "PASS-FLIP", (f"ΔBrier {d:+.4f} < 0 AND CI upper bound {ci[1]:+.4f} < 0 (n={paired_n}, era-clean)")
    else:
        v, reason = (
            "PASS-POINT",
            (
                f"ΔBrier {d:+.4f} < 0 but CI upper bound {_fmt(ci[1] if ci else None, '.4f', plus=True)} ≥ 0 "
                "— point estimate clears, confidence does not: keep collecting, do not flip"
            ),
        )
    bss = ctx["sc_all"].get("posterior", {}).get("bss")
    if bss is not None and bss < 0:
        reason += (
            f" — RELATIVE ONLY: posterior BSS {bss:+.4f} < 0, i.e. still worse than the constant "
            "base-rate predictor; a table that is itself below climatology is the wrong benchmark to pass"
        )
    return (v, reason)


def verdict(ctx: dict[str, Any]) -> list[dict[str, str]]:
    """Apply the pre-registered bar to each layer vs the incumbent table.

    Bar (phases doc §5, Phase A acceptance): "posterior reliability ≥ table
    lookup on the resolved book". Operationalised on proper scores: the
    posterior must (a) have a LOWER Brier than the table and (b) a smaller
    |bias| — and the Brier difference's bootstrap CI must not straddle zero in
    the wrong direction. No discretion; the numbers decide.
    """
    table = ctx["sc_all"].get("table", {})
    out: list[dict[str, str]] = []
    for lay in LAYERS:
        key, name, _ = lay
        sc = ctx["sc_all"].get(key, {})
        if not sc.get("n"):
            out.append({"layer": name, "verdict": "NO-DATA", "reason": "no scorable rows"})
            continue
        if key == "table":
            out.append(
                {
                    "layer": name,
                    "verdict": "INCUMBENT",
                    "reason": f"Brier {_fmt(sc['brier'], '.4f')}, |bias| {_fmt(abs(sc['bias']), '.4f')}",
                }
            )
            continue
        reasons: list[str] = []
        beats_brier = table.get("brier") is not None and sc.get("brier") is not None and sc["brier"] < table["brier"]
        better_bias = (
            table.get("bias") is not None and sc.get("bias") is not None and abs(sc["bias"]) < abs(table["bias"])
        )
        ci = ctx["sig"].get("diff_ci") if key == "posterior" else None
        reasons.append(
            f"Brier {_fmt(sc['brier'], '.4f')} vs table {_fmt(table.get('brier'), '.4f')}"
            + (" ✓" if beats_brier else " ✗")
        )
        reasons.append(
            f"|bias| {_fmt(abs(sc['bias']), '.4f')} vs table {_fmt(abs(table.get('bias') or 0.0), '.4f')}"
            + (" ✓" if better_bias else " ✗")
        )
        if ci is not None:
            reasons.append(f"ΔBrier 95% CI [{_fmt(ci[0], '.4f')}, {_fmt(ci[1], '.4f')}]")
        ok = beats_brier and better_bias and (ci is None or ci[1] < 0)
        out.append(
            {
                "layer": name,
                "verdict": "BEATS-TABLE" if ok else "STAYS-BEHIND",
                "reason": "; ".join(reasons),
            }
        )
    return out


# ─── Report rendering ───────────────────────────────────────────────────────


def _scorecard_table(sc: dict[str, dict[str, Any]]) -> list[str]:
    L = [
        "| layer | n | mean pred | realized | bias | Brier | BSS | log-loss | ECE | MCE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for lay in LAYERS:
        s = sc.get(lay[0], {})
        if not s.get("n"):
            L.append(f"| **{lay[1]}** | 0 | — | — | — | — | — | — | — | — |")
            continue
        L.append(
            f"| **{s['name']}** | {s['n']} | {_fmt(s['mean_pred'])} | {_fmt(s['realized'])} | "
            f"{_fmt(s['bias'], '.4f', plus=True)} | **{_fmt(s['brier'], '.4f')}** | "
            f"{_fmt(s['bss'], '.4f', plus=True)} | {_fmt(s['log_loss'], '.4f')} | "
            f"{_fmt(s['ece'], '.4f')} | {_fmt(s['mce'], '.4f')} |"
        )
    return L


def _curve_table(sc: dict[str, Any]) -> list[str]:
    L = ["| bin | n | mean pred | realized | gap (over-conf) |", "|---|---:|---:|---:|---:|"]
    for r in sc.get("curve", []):
        if not r["n"]:
            continue
        L.append(
            f"| [{r['lo']:.1f},{r['hi']:.1f}) | {r['n']} | {_fmt(r['mean_pred'], '.3f')} | "
            f"{_fmt(r['realized'], '.3f')} | {_fmt(r['gap'], '.3f', plus=True)} |"
        )
    if len(L) == 2:
        L.append("| — | 0 | — | — | — |")
    return L


def render_report(ctx: dict[str, Any], now: datetime) -> str:
    d = ctx["diag"]
    L: list[str] = []
    L.append("# PBM Phase B calibration audit — log-score / Brier / reliability")
    L.append("")
    L.append(
        f"Generated {now:%Y-%m-%d %H:%M} UTC · since {ctx['since']:%Y-%m-%d} · "
        f"join tol {d['tol']:g}s · bins {ctx['n_bins']} · bootstrap {ctx['n_boot']} (seed {ctx['seed']})"
    )
    L.append("")
    L.append(
        "The missing instrument. `measure_pbm_drift_auc.py` scores barrier-orientation "
        "**AUC** — a rank statistic, invariant to any monotone recalibration. A layer "
        "predicting 0.60 when reality is 0.30 scores the same AUC as one predicting 0.30. "
        "**AUC cannot see overconfidence.** This harness measures Brier, log-loss and the "
        "reliability curve of each probability layer, era-clean, off the `nt_pbm_shadow` ledger."
    )
    L.append("")

    L.append("## The bar (pre-registered)")
    L.append("")
    L.append(
        'Phases doc §5, **Phase A acceptance**: *"posterior reliability ≥ table lookup on '
        'the resolved book"*. Operationalised on proper scores: the posterior must show a '
        "**lower Brier** than the incumbent table AND a **smaller |bias|**, with the "
        "posterior−table Brier difference's bootstrap 95% CI **not straddling zero from "
        "above** (CI upper < 0). No discretion — the numbers decide."
    )
    L.append("")

    L.append("## Era discipline")
    L.append("")
    if ctx["fail_open"]:
        L.append(
            "> ⚠️ **FAIL-OPEN: `nt_calibration_cutover` missing/unreadable — no era filter "
            "applied. Treat every number below as discovery, never promotion evidence.**"
        )
    else:
        L.append(
            f"- cutover marker `nt_calibration_cutover`: **{ctx['cutover_iso']}** (the one marker "
            "reachability, the bias loop, atr_reopt and the Phase E harness all exclude on)."
        )
    L.append(
        "- filters applied: `nt_pbm_shadow.era='live'` **AND** `nt_signal_sim.contaminated=false` "
        "**AND** `ts >= cutover` — the two are not redundant (see the skill's era-clean standard)."
    )
    L.append(f"- rows dropped pre-cutover / dirty: **{ctx['pre_cutover_dropped']}**")
    L.append("")

    L.append("## Population & join")
    L.append("")
    if ctx.get("symbols"):
        L.append(f"- **symbol filter**: {', '.join(ctx['symbols'])} ({len(ctx['symbols'])} of the active book)")
    L.append(
        f"- `nt_pbm_shadow` rows fetched: **{d['shadow_n']}** · joined to `nt_signal_sim`: "
        f"**{d['joined']}** ({100 * d['match_rate']:.1f}%) · unmatched (no candidate) {d['no_candidate']}, "
        f"out-of-tol {d['out_of_tol']}."
    )
    L.append(
        f"- join = `(symbol, signal)` + NEAREST `ts` within **{d['tol']:g}s** — the shadow `ts` is the "
        "sweep wall-clock (`orchestrator.py:4292`), not the sim row's ts, so an exact "
        "`(symbol, ts, signal)` join fails."
    )
    if d["dt_median"] is not None:
        L.append(f"- |Δt| median **{d['dt_median']:.2f}s** · p90 {d['dt_p90']:.2f}s · max {d['dt_max']:.2f}s")
    L.append(
        f"- era-clean joined: **{ctx['era_clean']}** · scored (all outcomes) **{ctx['scored']}** "
        f"· decided (tp/sl) **{ctx['decided']}**"
    )
    L.append(
        f"- outcome mix: tp **{ctx['tp']}** / sl **{ctx['sl']}** / expired **{ctx['expired']}** "
        f"· realized P(tp_hit) **{ctx['tp'] / ctx['scored']:.4f}** over the scored population"
    )
    L.append(f"- bucket mix: {', '.join(f'{k}: {v}' for k, v in sorted(ctx['bucket_mix'].items()))}")
    L.append(f"- vol-tier mix: {', '.join(f'{k}: {v}' for k, v in sorted(ctx['tier_mix'].items()))}")
    L.append(
        f"- coverage floor for a verdict: **{ctx['min_n']}** scored AND paired rows — have "
        f"**{ctx['scored']}** scored / **{int(ctx['sig'].get('n_common') or 0)}** paired."
    )
    L.append("")

    L.append("## Headline — binary P(TP first), all era-clean rows (y = 1[tp_hit])")
    L.append("")
    L.append(
        "*Every layer's `p_W` names P(tp_hit); expired rows enter as y=0 (survivorship-honest, "
        "not dropped). BSS is the Brier skill vs the constant base-rate predictor — >0 beats "
        "climatology, 0 ties it, <0 is worse. ECE/MCE are over equal-width bins.*"
    )
    L.append("")
    L.extend(_scorecard_table(ctx["sc_all"]))
    L.append("")
    if ctx["expired"] > 0:
        L.append("**Decided-only variant (tp vs sl — the Phase E harness's population):**")
        L.append("")
        L.extend(_scorecard_table(ctx["sc_dec"]))
    else:
        L.append(
            "*Decided-only variant omitted: **0 expired rows** in this window, so the decided "
            "population is identical to the all-rows population — the 48-bar deadline has not "
            "matured yet, so no expiry mass exists to separate them.*"
        )
    L.append("")

    L.append("## Reliability curves (deciles)")
    L.append("")
    for lay in LAYERS:
        sc = ctx["sc_all"].get(lay[0], {})
        if not sc.get("n"):
            continue
        L.append(f"### {sc['name']}")
        L.append("")
        L.extend(_curve_table(sc))
        L.append("")

    L.append("## Three-outcome scoring (tp / sl / expired) — all era-clean rows")
    L.append("")
    L.append(
        "*Posterior carries the native triple; null is recomputed from the shadow's stored "
        "geometry (`barrier_null(tp_atr, sl_atr, t_bars, sigma)`); table is reconstructed from "
        "`(p_win_bracket, p_resolve)` as p_E = 1 − p_resolve, p_L = p_resolve − p_win. "
        "`raw_directional` stores no resolve probability, so it is scored binary-only — stated, not faked.*"
    )
    L.append("")
    L.append("| layer | n | multi-class Brier | multi-class log-loss | mean p_E |")
    L.append("|---|---:|---:|---:|---:|")
    for name in ("posterior", "table", "null"):
        t = ctx["three"].get(name, {})
        if not t.get("n"):
            L.append(f"| {name} | 0 | — | — | — |")
            continue
        L.append(
            f"| **{name}** | {t['n']} | {_fmt(t['brier'], '.4f')} | {_fmt(t['log_loss'], '.4f')} | "
            f"{_fmt(t['mean_p_e'], '.4f')} |"
        )
    L.append("")
    if ctx["expired"] == 0:
        L.append(
            "> ⚠️ **No expiry mass in this window** — every scored row resolved tp/sl, so the "
            "three-outcome score is effectively two-outcome here. The posterior's mean p_E "
            f"({_fmt(ctx['three'].get('posterior', {}).get('mean_p_e'), '.4f')}) is a PREDICTION "
            "the window cannot yet validate; re-run once the 48-bar deadline matures."
        )
        L.append("")

    L.append("## Conditional read — q = P(tp | resolve) (the expiry-free half)")
    L.append("")
    L.append(
        "*The part of the bar that does not depend on the expiry LEVEL. On a young window with "
        "zero realized expiries the full-triple score is dominated by the resolve-mass term below, "
        "so this is the durable read. `degenerate q∈{0,1}` counts hard 0/1 probabilities — under a "
        "log score the 1e-6 clip turns each into a −13.8 penalty that can manufacture an advantage.*"
    )
    L.append("")
    c = ctx["conditional"]
    L.append("| layer | n | mean q | realized | bias | Brier | log-loss | degenerate q∈{0,1} |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for lay in LAYERS:
        s = c["layers"].get(lay[0], {})
        if not s.get("n"):
            continue
        L.append(
            f"| {lay[1]} | {s['n']} | {_fmt(s['mean_q'])} | {_fmt(s['realized'])} | "
            f"{_fmt(s['bias'], '.4f', plus=True)} | {_fmt(s['brier'], '.4f')} | "
            f"{_fmt(s['log_loss'], '.4f')} | {s['degenerate']} |"
        )
    L.append("")
    p = c.get("paired", {})
    pe = c.get("paired_ex_degenerate", {})
    if p.get("n"):
        ci = p.get("ci")
        verdict_txt = (
            "CI upper < 0 ⇒ significantly better." if ci and ci[1] < 0 else "CI straddles 0 ⇒ **NOT significant**."
        )
        L.append(
            f"- **ΔBrier (posterior − table)** on {p['n']} paired rows: "
            f"**{_fmt(p['delta_brier'], '.4f', plus=True)}**"
            + (f", 95% CI [{_fmt(ci[0], '.4f', plus=True)}, {_fmt(ci[1], '.4f', plus=True)}]" if ci else "")
            + f" — {verdict_txt}"
        )
    if pe.get("n"):
        cie = pe.get("ci")
        L.append(
            f"- **ΔBrier excluding degenerate q** on {pe['n']} rows: "
            f"**{_fmt(pe['delta_brier'], '.4f', plus=True)}**"
            + (f", 95% CI [{_fmt(cie[0], '.4f', plus=True)}, {_fmt(cie[1], '.4f', plus=True)}]" if cie else "")
            + " — removes the 1e-6-clip artifact so any advantage is attributable."
        )
        L.append(
            f"- **Δlog-loss excluding degenerate q** on {pe['n']} rows: "
            f"**{_fmt(pe['delta_log_loss'], '.4f', plus=True)}** — the same removal applied to the log "
            "score; if this is ~0 while the all-rows Δlog-loss is large, the gap WAS the clip."
        )
    L.append("")

    L.append("## Log-score decomposition — how much of the delta is the expiry term")
    L.append("")
    d = ctx.get("decomp", {})
    if d.get("n"):
        L.append("`log p(outcome) = log q + log(p_W + p_L)` exactly, so the paired delta splits into:")
        L.append("")
        L.append("| term | mean Δ (posterior − table) | 95% CI |")
        L.append("|---|---:|---|")
        for label, key, cik in (
            ("full  log p(outcome)", "full", "full_ci"),
            ("conditional  log q", "cond", "cond_ci"),
            ("resolve mass  log(p_W+p_L)", "resolve", "resolve_ci"),
        ):
            ci = d.get(cik)
            L.append(
                f"| {label} | {_fmt(d[key], '.4f', plus=True)} | "
                f"[{_fmt(ci[0] if ci else None, '.4f', plus=True)}, "
                f"{_fmt(ci[1] if ci else None, '.4f', plus=True)}] |"
            )
        L.append("")
        L.append(
            f"- implied resolve mass: posterior **{_fmt(d['implied_resolve_posterior'], '.4f')}** vs "
            f"table **{_fmt(d['implied_resolve_table'], '.4f')}** — realized **1.0000** "
            f"({ctx['expired']} expired in this window)."
        )
        if ctx["expired"] == 0:
            L.append(
                f"- ⚠️ the resolve-mass term is **{_fmt(d['resolve'], '.4f', plus=True)}** of the "
                f"**{_fmt(d['full'], '.4f', plus=True)}** full delta on a **zero-expiry window** — it is "
                "not directional skill and its sign is not durable until expiries land."
            )
    L.append("")

    L.append("## Significance — posterior vs incumbent table (aligned rows)")
    L.append("")
    sig = ctx["sig"]
    if sig.get("n_common"):
        L.append(f"- common rows: **{sig['n_common']}**")
        L.append(
            f"- Brier posterior **{_fmt(sig['brier_posterior'], '.4f')}** vs table "
            f"**{_fmt(sig['brier_table'], '.4f')}**"
        )
        ci = sig.get("diff_ci")
        if ci:
            L.append(
                f"- ΔBrier (posterior − table) 95% CI **[{_fmt(ci[0], '.4f')}, {_fmt(ci[1], '.4f')}]** — "
                + (
                    "upper < 0 ⇒ posterior significantly better calibrated."
                    if ci[1] < 0
                    else "straddles/includes 0 ⇒ not separable on this population."
                )
            )
        else:
            L.append("- ΔBrier CI: — (bootstrap unusable)")
    else:
        L.append("- — no rows where both layers carry a value.")
    L.append("")

    L.append("## Per-bucket split (bracket_bucket)")
    L.append("")
    L.append("| bucket | layer | n | mean pred | realized | bias | Brier | BSS | ECE |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for bucket in sorted(ctx["by_bucket"]):
        for lay in LAYERS:
            s = ctx["by_bucket"][bucket].get(lay[0], {})
            if not s.get("n"):
                continue
            L.append(
                f"| {bucket} | {lay[1]} | {s['n']} | {_fmt(s['mean_pred'], '.3f')} | "
                f"{_fmt(s['realized'], '.3f')} | {_fmt(s['bias'], '.3f', plus=True)} | "
                f"{_fmt(s['brier'], '.4f')} | {_fmt(s['bss'], '.3f', plus=True)} | {_fmt(s['ece'], '.3f')} |"
            )
    L.append("")

    L.append("## Per-vol-tier split (vol_tier)")
    L.append("")
    L.append("| vol_tier | layer | n | mean pred | realized | bias | Brier | BSS | ECE |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for tier in sorted(ctx["by_tier"]):
        for lay in LAYERS:
            s = ctx["by_tier"][tier].get(lay[0], {})
            if not s.get("n"):
                continue
            L.append(
                f"| {tier} | {lay[1]} | {s['n']} | {_fmt(s['mean_pred'], '.3f')} | "
                f"{_fmt(s['realized'], '.3f')} | {_fmt(s['bias'], '.3f', plus=True)} | "
                f"{_fmt(s['brier'], '.4f')} | {_fmt(s['bss'], '.3f', plus=True)} | {_fmt(s['ece'], '.3f')} |"
            )
    L.append("")

    L.append("## Per-symbol (all era-clean rows)")
    L.append("")
    L.append(
        "| symbol | n | n paired | posterior Brier | table Brier | ΔBrier (post−table) | 95% CI "
        "| posterior bias | posterior BSS |"
    )
    L.append("|---|---:|---:|---:|---:|---:|---|---:|---:|")
    for rec in ctx["per_symbol"]:
        post = rec.get("posterior") or {}
        tab = rec.get("table") or {}
        dl = rec.get("delta")
        ci = dl["ci"] if dl else None
        L.append(
            f"| {rec['symbol']} | {rec['n']} | {dl['n'] if dl else 0} | "
            f"{_fmt(post.get('brier'), '.4f')} | "
            f"{_fmt(tab.get('brier'), '.4f')} | {_fmt(dl['mean'] if dl else None, '.4f', plus=True)} | "
            f"[{_fmt(ci[0] if ci else None, '.4f', plus=True)}, "
            f"{_fmt(ci[1] if ci else None, '.4f', plus=True)}] | "
            f"{_fmt(post.get('bias'), '.3f', plus=True)} | {_fmt(post.get('bss'), '.3f', plus=True)} |"
        )
    L.append(
        "*ΔBrier < 0 ⇒ the posterior is better calibrated on that symbol. `n paired` is the rows "
        f"carrying BOTH layers; a CI prints only at n paired ≥ {ctx['min_symbol_n']} — thin symbols "
        "are diagnostics, not votes.*"
    )
    L.append("")

    L.append("## Verdict (mechanically applied bar — no discretion)")
    L.append("")
    pv, pr = pooled_verdict(ctx)
    L.append(f"### Pooled flip bar (coverage-gated) — **{pv}**")
    L.append("")
    L.append(f"- {pr}.")
    L.append("")
    if pv == "PASS-FLIP":
        L.append(
            "> The posterior's price is clearly better calibrated than the table lookup on era-clean "
            "resolved evidence. Before any live wiring: re-run on a fresh window, read the "
            "absolute-skill clause above, and get operator sign-off. This harness never flips a flag."
        )
    elif pv == "PASS-POINT":
        L.append(
            "> Point estimate clears the bar but the confidence interval does not. **Keep collecting "
            "ledger depth; do not flip.**"
        )
    elif pv == "FAIL":
        L.append(
            "> The table lookup is the better-calibrated price on this evidence. Keep the incumbent "
            "source and investigate the per-symbol table before any retry."
        )
    else:
        L.append(
            "> Coverage gates unmet — the ledger is younger than the bar. Expected state in the first "
            "days after `pbm_mode: shadow`; re-run weekly alongside the drift AUC harness."
        )
    L.append("")
    L.append("### Per-layer scorecard (vs the incumbent table)")
    L.append("")
    for row in verdict(ctx):
        L.append(f"- **{row['layer']}**: **{row['verdict']}** — {row['reason']}.")
    L.append("")

    L.append("## Notes / honesty")
    L.append("")
    L.append(
        "- **`nt_signal_sim.p_win_dir` is NOT the raw directional layer.** `orchestrator.py:5650` "
        "sets `_p_win_dir = _p_win_bracket`, so `p_win_dir == p_win_bracket == p_win` on every live "
        "row (verified: 1181/1181 comparable rows). The genuine directional series is "
        "`predicted_p_win_raw` (`orchestrator.py:4624`, captured before the bracket swap at `:4631`). "
        'Scoring `p_win_dir` as "directional" would silently score the table twice.'
    )
    L.append(
        "- **`nt_pbm_shadow.outcome` is NULL for every row** (migration 211: backfilled by the "
        "acceptance harness; the resolver writes the cell posterior, not this column). The join to "
        "`nt_signal_sim` is therefore the outcome source — this harness stays read-only and does not "
        "backfill."
    )
    L.append(
        "- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs "
        "are optimistic (n inflated). The bar demands the CI clear zero regardless."
    )
    L.append(
        "- **Brier/log-loss are proper scores**: they reward honesty, not just ranking. A layer can "
        "win on AUC and lose on Brier — that divergence IS the overconfidence signal this instrument "
        "exists to surface."
    )
    L.append(
        "- Expired rows are scored (as y=0 for tp_hit) rather than dropped — the survivorship the "
        "Phase A P0 list closes. Dropping them would flatter every layer by removing the outcomes "
        "no layer predicted."
    )
    L.append("")
    return "\n".join(L) + "\n"


# ─── Fetch (the only I/O) ───────────────────────────────────────────────────


async def _fetch(client: httpx.AsyncClient, table: str, columns: str, **extra: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    off = 0
    while True:
        params = {"select": columns, "order": "ts.asc", "limit": str(PAGE), "offset": str(off), **extra}
        resp = await client.get(_url(table), headers=_headers(), params=params)
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected {table} response shape: {type(batch).__name__}")
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        off += len(batch)
        if off > 200_000:
            logger.warning("pagination guard hit at offset %d — stopping", off)
            break
    return rows


# ─── Offline self-test (--selftest: pure, no network) ───────────────────────


def _selftest() -> int:
    """Offline synthetic self-test — no network, no DB.

    Ported in spirit from the sibling harness's ``--selftest`` (its best idea):
    every pure stage exercised on data whose right answer is known in advance, so
    the harness is verifiable without touching the ledger. Includes a regression
    test for the geometry-clamp bug this selftest originally found (``null_triple``
    routing tp_atr through the probability clamp).
    """
    rng = np.random.default_rng(7)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    def num(x: float | None, default: float = 0.0) -> float:
        """None-safe float (avoids the falsy-zero trap in ``or`` defaults)."""
        return default if x is None else float(x)

    print("selftest: scoring primitives")
    check("brier exact", abs(num(brier([0.25, 0.75], [1, 1])) - 0.3125) < 1e-12)
    check("brier empty → None", brier([], []) is None)
    check("log_loss exact", abs(num(log_loss([0.5], [1])) - math.log(2)) < 1e-12)
    check("log_loss clips at EPS", abs(num(log_loss([0.0], [1])) + math.log(_EPS)) < 1e-9)
    check("climatology = base(1-base)", abs(num(climatology_brier([1, 1, 0, 0])) - 0.25) < 1e-12)
    check("bss ties at 0", abs(num(brier_skill_score(0.25, 0.25))) < 1e-12)
    check("bss sign flips", num(brier_skill_score(0.2, 0.25)) > 0 > num(brier_skill_score(0.3, 0.25)))

    print("selftest: reliability / ECE / MCE")
    perfect = reliability_curve([0.05, 0.05, 0.95, 0.95], [0, 0, 1, 1], 10)
    check("curve bins populated", perfect[0]["n"] == 2 and perfect[9]["n"] == 2)
    check("ece ~0 when calibrated", num(ece(perfect), 1.0) < 0.06, f"ece={num(ece(perfect)):.4f}")
    bad = reliability_curve([0.95, 0.95], [0, 0], 10)
    check("mce catches a dead bin", abs(num(mce(bad)) - 0.95) < 1e-9)
    check("ece empty → None", ece([]) is None)

    print("selftest: the property AUC cannot see")
    qs = np.linspace(0.05, 0.95, 400)
    y = (rng.random(qs.size) < qs).astype(int)
    over = np.clip(qs * 0.6 + 0.35, 0, 1)  # identical ranking, worse calibration
    check("ranking identical ⇒ AUC blind", bool(np.array_equal(np.argsort(qs), np.argsort(over))))
    check("Brier separates them", num(brier(list(qs), list(y)), 1.0) < num(brier(list(over), list(y))))
    check("log-loss agrees", num(log_loss(list(qs), list(y)), 1.0) < num(log_loss(list(over), list(y))))
    check(
        "bias is smaller on the calibrated scorer",
        abs(float(np.mean(qs)) - float(np.mean(y))) < abs(float(np.mean(over)) - float(np.mean(y))),
    )

    print("selftest: paired bootstrap")
    ci = bootstrap_brier_diff(list(qs), list(over), list(y), 200, 13)
    check("CI upper < 0 when a is better", ci is not None and ci[1] < 0, f"ci={ci}")
    ci0 = bootstrap_brier_diff([0.3] * 200, [0.3] * 200, [1, 0] * 100, 200, 13)
    check("CI brackets 0 on identical scorers", ci0 is not None and ci0[0] <= 0 <= ci0[1])

    print("selftest: triples")
    check("_renorm clips + normalises", abs(sum(_renorm((-0.2, 0.5, 0.7))) - 1.0) < 1e-12)
    check("_renorm degenerate → uniform", _renorm((0.0, 0.0, 0.0)) == (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0))
    sym = null_triple({"tp_atr": 4.0, "sl_atr": 4.0, "t_bars": 48, "sigma": 1.0})
    check("null symmetric at a==b", sym is not None and abs(sym[0] - sym[1]) < 1e-9)
    check("null normalised", sym is not None and abs(sum(sym) - 1.0) < 1e-9)
    # REGRESSION: geometry must NOT be clamped to [0,1] (the bug this found)
    wide = null_triple({"tp_atr": 6.4, "sl_atr": 6.4, "t_bars": 48, "sigma": 1.0})
    narrow = null_triple({"tp_atr": 1.0, "sl_atr": 1.0, "t_bars": 48, "sigma": 1.0})
    wide_pe = wide[2] if wide else float("nan")
    narrow_pe = narrow[2] if narrow else float("nan")
    check(
        "geometry NOT clamped to [0,1]",
        wide is not None and narrow is not None and wide_pe > 1e-6,
        f"wide p_E={wide_pe:.4f}",
    )
    check("wider bracket ⇒ more expire", wide_pe > narrow_pe, f"{wide_pe:.4f} > {narrow_pe:.4f}")
    check("null degenerate → None", null_triple({"tp_atr": 0, "sl_atr": 0}) is None)
    tb = table_triple({}, {"p_win_bracket": 0.25, "p_resolve": 0.70})
    check("table triple reconstructs", tb is not None and abs(tb[0] - 0.25) < 1e-9 and abs(tb[2] - 0.30) < 1e-9)
    check("table triple missing fields → None", table_triple({}, {}) is None)
    post = posterior_triple({"p_w": 0.3, "p_l": 0.5, "p_e": 0.2})
    check("posterior triple normalises", post is not None and abs(sum(post) - 1.0) < 1e-12)

    print("selftest: multiclass")
    check("multiclass brier perfect → 0", abs(num(multiclass_brier([(1.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)]))) < 1e-12)
    check(
        "multiclass log-loss perfect → ~0",
        num(multiclass_log_loss([(1.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)]), 1.0) < 1e-6,
    )

    print("selftest: conditional read + log-score decomposition")
    check("conditional_q basic", abs((conditional_q((0.2, 0.3, 0.5)) or 0) - 0.4) < 1e-12)
    check("conditional_q degenerate → None", conditional_q((0.0, 0.0, 1.0)) is None)
    check("conditional_q None → None", conditional_q(None) is None)
    # exact identity: log p(outcome) = log q + log(resolve mass)
    tri = _renorm((0.3, 0.5, 0.2))
    rmass = tri[0] + tri[1]
    lhs = math.log(tri[0])
    rhs = math.log(tri[0] / rmass) + math.log(rmass)
    check("log-score identity holds exactly", abs(lhs - rhs) < 1e-12)
    synth = [
        (
            {
                "symbol": "T",
                "ts": f"t{i}",
                "p_w": 0.30,
                "p_l": 0.60,
                "p_e": 0.10,
                "tp_atr": 4.0,
                "sl_atr": 4.0,
                "t_bars": 48,
                "sigma": 1.0,
            },
            {
                "symbol": "T",
                "ts": f"t{i}",
                "signal": "buy",
                "outcome": ("tp_hit", "sl_hit")[i % 2],
                "p_win_bracket": 0.30 if i % 3 else 0.0,
                "p_resolve": 0.90,
                "predicted_p_win_raw": 0.6,
                "contaminated": False,
                "qualified": True,
            },
        )
        for i in range(60)
    ]
    cond = score_conditional(synth, n_boot=100, seed=13)
    check("conditional scores every layer", cond["layers"]["posterior"]["n"] == 60)
    check(
        "degenerate q counted",
        cond["layers"]["table"]["degenerate"] == 20,
        f"deg={cond['layers']['table']['degenerate']}",
    )
    check("ex-degenerate block is smaller", cond["paired_ex_degenerate"]["n"] < cond["paired"]["n"])
    dec = log_score_decomposition(synth, n_boot=100, seed=13)
    check(
        "decomposition identity: full == cond + resolve",
        dec["n"] == 60 and abs(dec["full"] - (dec["cond"] + dec["resolve"])) < 1e-9,
        f"{dec['full']:+.4f} vs {dec['cond'] + dec['resolve']:+.4f}",
    )

    print("selftest: join (nearest-ts — the thing that broke the sibling harness)")
    base = datetime(2026, 10, 14, tzinfo=timezone.utc)
    shadow, sim = [], []
    for i in range(50):
        st = base + timedelta(minutes=15 * i)
        shadow.append(
            {
                "symbol": "T",
                "ts": st.isoformat(),
                "signal": "buy",
                "era": "live",
                "p_w": 0.3,
                "p_l": 0.5,
                "p_e": 0.2,
                "p_w_null": 0.25,
                "tp_atr": 4.0,
                "sl_atr": 4.0,
                "t_bars": 48,
                "sigma": 1.0,
                "bracket_bucket": "2-4",
                "vol_tier": "med",
                "qualified": True,
            }
        )
        # the sim row is ALWAYS ~2.55s LATER — the real-world offset, exactly
        sim.append(
            {
                "symbol": "T",
                "ts": (st + timedelta(seconds=2.55)).isoformat(),
                "signal": "buy",
                "outcome": ("tp_hit", "sl_hit")[i % 2],
                "p_win_bracket": 0.25,
                "predicted_p_win_raw": 0.6,
                "contaminated": False,
                "qualified": True,
            }
        )
    pairs, diag = join_ledger(shadow, sim, 60.0)
    check("nearest-ts join bridges the 2.55s offset", len(pairs) == 50, f"joined={len(pairs)}")
    pairs_tight, _ = join_ledger(shadow, sim, 1.0)
    check("tolerance is honoured (1s finds none)", len(pairs_tight) == 0)

    print("selftest: verdict branches")

    def mk_ctx(
        scored: int = 900,
        paired: int = 900,
        d: float = -0.02,
        ci: tuple[float, float] | None = (-0.03, -0.01),
        bss: float = 0.01,
        fail_open: bool = False,
    ) -> dict[str, Any]:
        return {
            "fail_open": fail_open,
            "scored": scored,
            "min_n": 500,
            "sig": {"n_common": paired, "brier_delta": d, "diff_ci": ci},
            "sc_all": {"posterior": {"bss": bss}},
        }

    check("PASS-FLIP branch", pooled_verdict(mk_ctx())[0] == "PASS-FLIP")
    check("PASS-POINT branch", pooled_verdict(mk_ctx(ci=(-0.03, 0.01)))[0] == "PASS-POINT")
    check("FAIL branch", pooled_verdict(mk_ctx(d=0.02, ci=(-0.01, 0.05)))[0] == "FAIL")
    check("NO-GO scored-depth branch", pooled_verdict(mk_ctx(scored=100))[0] == "NO-GO")
    check("NO-GO paired-depth branch", pooled_verdict(mk_ctx(paired=100))[0] == "NO-GO")
    check("NO-GO era branch", pooled_verdict(mk_ctx(fail_open=True))[0] == "NO-GO")
    pv, pr = pooled_verdict(mk_ctx(bss=-0.08))
    check("relative-only clause fires", pv == "PASS-FLIP" and "RELATIVE ONLY" in pr)

    print("selftest: full pipeline + renderer")
    ctx = measure(
        pairs,
        diag=diag,
        cutover_iso="2026-10-02T00:00:00Z",
        n_bins=10,
        n_boot=100,
        seed=13,
        since=base,
        min_n=10,
        min_symbol_n=5,
    )
    check("measure scores the synthetic population", ctx["scored"] == 50, f"scored={ctx['scored']}")
    check("per-symbol populated", len(ctx["per_symbol"]) == 1)
    # SIGN CONVENTION: per-symbol ΔBrier must agree with the pooled ΔBrier.
    # (Regression guard: a swapped tuple in score_per_symbol makes the
    # per-symbol column read the exact opposite of the pooled verdict.)
    sym0 = ctx["per_symbol"][0].get("delta")
    pooled_d = ctx["sig"].get("brier_delta")
    check(
        "per-symbol ΔBrier sign matches pooled",
        sym0 is not None and pooled_d is not None and (sym0["mean"] < 0) == (pooled_d < 0),
        f"sym={sym0['mean']:+.4f} pooled={pooled_d:+.4f}" if sym0 and pooled_d is not None else "—",
    )
    check(
        "per-symbol ΔBrier reproduces pooled when one symbol",
        sym0 is not None and pooled_d is not None and abs(sym0["mean"] - pooled_d) < 1e-12,
    )
    text = render_report(ctx, datetime(2026, 10, 16, tzinfo=timezone.utc))
    for sec in (
        "## The bar",
        "## Era discipline",
        "## Population & join",
        "## Headline",
        "## Reliability curves",
        "## Three-outcome",
        "## Significance",
        "## Per-bucket",
        "## Per-vol-tier",
        "## Per-symbol",
        "## Verdict",
        "## Notes / honesty",
    ):
        check(f"renderer section {sec!r}", sec in text)

    if failures:
        print(f"selftest: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("selftest: all checks passed")
    return 0


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Phase B calibration audit: Brier / log-loss / reliability of the PBM "
        "posterior vs the incumbent reachability table vs the raw directional blend, "
        "era-clean, off the nt_pbm_shadow ledger. Read-only.",
    )
    ap.add_argument(
        "--since",
        default=None,
        metavar="ISO",
        help=f"ISO date/datetime UTC (default: {DEFAULT_LOOKBACK_DAYS} days back)",
    )
    ap.add_argument(
        "--join-tol",
        type=float,
        default=DEFAULT_JOIN_TOL,
        help=f"seconds tolerance for the shadow→sim ts join (default {DEFAULT_JOIN_TOL:g})",
    )
    ap.add_argument("--bins", type=int, default=DEFAULT_BINS, help="reliability bins (default 10)")
    ap.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP, help="bootstrap resamples")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap RNG seed")
    ap.add_argument(
        "--symbols",
        default=None,
        metavar="A,B",
        help="optional comma-separated symbol whitelist (filters the fetch)",
    )
    ap.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N,
        help=f"scored + paired rows required for a pooled verdict (default {DEFAULT_MIN_N})",
    )
    ap.add_argument(
        "--min-symbol-n",
        type=int,
        default=DEFAULT_MIN_SYMBOL_N,
        help=f"per-symbol rows before a per-symbol CI prints (default {DEFAULT_MIN_SYMBOL_N})",
    )
    ap.add_argument("--out", default=None, help="report path override")
    ap.add_argument("--json", default=None, help="optional path to dump the context as JSON")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="run the offline synthetic self-test (no network, no DB)",
    )
    return ap.parse_args(argv)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()

    if args.selftest:
        raise SystemExit(_selftest())

    since = _parse_since(args.since)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    sym_filter = {"symbol": f"in.({','.join(symbols)})"} if symbols else {}

    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        raise SystemExit(
            "SUPABASE_URL/SUPABASE_KEY not configured — nothing to measure. "
            "(Offline verification: py_compile + the pure-math self-tests require no network.)"
        )

    cutover_iso = await _fetch_cutover_at()
    logger.info("cutover marker: %s", cutover_iso or "MISSING (fail-open)")

    async with httpx.AsyncClient(timeout=60.0) as client:
        shadow = await _fetch(client, "nt_pbm_shadow", SHADOW_COLUMNS, **sym_filter)
        logger.info("fetched %d nt_pbm_shadow rows%s", len(shadow), f" for {len(symbols)} symbols" if symbols else "")
        if not shadow:
            raise SystemExit("nt_pbm_shadow is empty — nothing to audit.")
        sim_lo = _parse_ts(shadow[0].get("ts")) or since
        sim_lo_iso = sim_lo.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        sim = await _fetch(client, "nt_signal_sim", SIM_COLUMNS, ts=f"gte.{sim_lo_iso}", **sym_filter)
        logger.info("fetched %d nt_signal_sim rows since %s", len(sim), sim_lo_iso)

    pairs, diag = join_ledger(shadow, sim, args.join_tol)
    logger.info(
        "joined %d/%d (%.1f%%) within %gs", diag["joined"], diag["shadow_n"], 100 * diag["match_rate"], args.join_tol
    )

    ctx = measure(
        pairs,
        diag=diag,
        cutover_iso=cutover_iso,
        n_bins=args.bins,
        n_boot=args.bootstrap,
        seed=args.seed,
        since=since,
        min_n=args.min_n,
        min_symbol_n=args.min_symbol_n,
        symbols=symbols,
    )

    now = datetime.now(timezone.utc)
    text = render_report(ctx, now)
    out_path = Path(args.out) if args.out else REPORT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    if args.json:
        slim = {k: v for k, v in ctx.items() if k not in ("sc_all", "sc_dec")}
        slim["sc_all"] = {
            k: {kk: vv for kk, vv in v.items() if kk not in ("scores", "labels", "kept")}
            for k, v in ctx["sc_all"].items()
        }
        slim["sc_dec"] = {
            k: {kk: vv for kk, vv in v.items() if kk not in ("scores", "labels", "kept")}
            for k, v in ctx["sc_dec"].items()
        }
        Path(args.json).write_text(json_dumps(slim), encoding="utf-8")
        logger.info("wrote %s", args.json)

    for lay in LAYERS:
        sc = ctx["sc_all"].get(lay[0], {})
        if not sc.get("n"):
            logger.info("layer %-16s no data", lay[0])
            continue
        logger.info(
            "layer %-16s n=%-5d mean=%.4f realized=%.4f bias=%+.4f Brier=%.4f BSS=%+.4f ECE=%.4f",
            lay[0],
            sc["n"],
            sc["mean_pred"],
            sc["realized"],
            sc["bias"],
            sc["brier"],
            sc["bss"] or 0.0,
            sc["ece"] or 0.0,
        )
    pv, pr = pooled_verdict(ctx)
    logger.info("POOLED VERDICT %s — %s", pv, pr)
    logger.info("wrote %s", out_path)


def json_dumps(obj: Any) -> str:
    import json

    def default(o: Any) -> Any:
        if isinstance(o, Counter):
            return dict(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        return str(o)

    return json.dumps(obj, indent=2, default=default)


if __name__ == "__main__":
    asyncio.run(main())
