#!/usr/bin/env python3
"""measure_pbm_calibration.py — Phase B audit: is the posterior's price CALIBRATED?

The Phase B flip bar (worklog/20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md §4):

    Phase A ledger log-score >= table lookup, era-clean
    (flip pbm_ev_mode: "off" -> "shadow" -> "live" only on this evidence)

This is the calibration instrument the drift AUC harness deliberately is not:
AUC is invariant under any monotone transform of a score, so it CANNOT see
overconfidence — a badly miscalibrated but well-ranked scorer scores 0.9 AUC.
Proper scoring rules (log-score, Brier) plus reliability curves measure the
thing the flip actually needs: are the NUMBERS on scale.

What it scores, per resolved signal (the nt_pbm_shadow ledger joins offline
on (symbol, ts, signal) — exactly what migration 211 designed this harness
to do, including backfilling the stamp's outcome column when asked):

    pbm    the posterior triple (p_w, p_l, p_e) stamped at emit time
    null   the analytic barrier triple recomputed from the stamp's own
           geometry (tp_atr, sl_atr, t_bars, sigma) — physics, not memory
    table  the live EV input today: p_win_bracket (all-in TP share) +
           p_resolve from nt_signal_sim — implied triple
           (p_win_bracket, p_resolve - p_win_bracket, 1 - p_resolve)
    dir    the raw directional p_win (the overconfidence exhibit)

Two populations, both era-clean (cutover marker, same as every consumer):

    full triple   ALL resolved stamps incl. expired — log p(outcome) under
                  each triple. The ledger's own format; the PRIMARY bar.
    conditional   decided rows only (tp_hit/sl_hit) — q = P(tp | resolve):
                  pbm p_w/(p_w+p_l), table p_win_bracket/p_resolve, dir
                  p_win_dir. Brier + reliability curves live here.

Paired comparison: per-row log-score differences (pbm - table, pbm - null,
table - null) on the COMMON scoreable population, row-level percentile
bootstrap CI (seeded, deterministic). Mechanical verdict, no discretion:

    PASS-FLIP    mean Δ(pbm − table) full-triple ≥ 0  AND  bootstrap 95%
                 CI lower bound ≥ 0  (clearly ≥ the table lookup)
    PASS-POINT   mean Δ ≥ 0 but CI LB < 0 — point estimate clears, the
                 confidence does not: keep collecting, do not flip
    FAIL         mean Δ < 0 — the table lookup is the better-calibrated
                 price; the posterior is not ready to replace it
    NO-GO        coverage gates unmet (era fail-open, ledger depth, paired
                 depth) — named reason; numbers are discovery-grade only

Coverage floors: ≥ 500 joined resolved stamps AND ≥ 500 paired rows for a
verdict (the verdict is POOLED — one number, one gate; per-symbol rows are
diagnostics, not votes).

Read-only by default. ``--backfill`` exercises migration 211's designated
job for this harness: PATCH the stamp's NULL outcome column from the joined
resolved row (idempotent — non-null outcomes are never touched).

Run:
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/measure_pbm_calibration.py
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/measure_pbm_calibration.py --selftest

Offline verification needs no network: ``--selftest`` exercises every pure
stage (scoring, pairing, bootstrap, reliability, verdict branches, renderer)
on synthetic data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# .env.local first (same pattern as scripts/measure_pbm_drift_auc.py) so the
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
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS  # noqa: E402

logger = logging.getLogger("measure_pbm_calibration")

REPORT_PATH = ROOT / "worklog" / "kairos" / "reports" / "20261016_pbm_calibration.md"
PAGE = 1000
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_BOOTSTRAP = 200
DEFAULT_MIN_N = 500
DEFAULT_SEED = 13
DEFAULT_MIN_SYMBOL_N = 100

NT_SHADOW_COLUMNS = (
    "id,symbol,ts,signal,source,tp_atr,sl_atr,t_bars,sigma,"
    "p_w,p_l,p_e,p_w_null,gap,n_eff,ev_atr,ev_lb_atr,ev_lb_pass,"
    "bracket_bucket,vol_tier,deadline_class,era,outcome,created_at"
)
NT_SIM_COLUMNS = "symbol,ts,signal,outcome,p_win_bracket,p_resolve,p_win_dir"

OUTCOMES = ("tp_hit", "sl_hit", "expired")
DECIDED = ("tp_hit", "sl_hit")
EPS = 1e-6  # log-score clip: log(1e-6) ~ -13.8 — a wrong certainty, priced

# Reliability bin edges (0.1 wide).
_BIN_EDGES = tuple(round(0.1 * i, 2) for i in range(11))


# ─── Small pure helpers (mirrors measure_pbm_drift_auc.py) ──────────────────


def _fmt(value: float | None, spec: str = ".4f", plus: bool = False) -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), ("+" if plus else "") + spec)
    except (TypeError, ValueError):
        return str(value)


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an ISO timestamp (Z or +00:00) to an aware UTC datetime."""
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _to_float(raw: Any) -> float | None:
    """Finite float or None (missing / null / NaN all collapse to None)."""
    if raw is None:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_since(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise SystemExit(f"--since is not an ISO date/datetime: {raw!r} ({exc})") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # PostgREST: Z suffix, no microseconds (known repo pitfall).
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _key(symbol: Any, ts: Any, signal: Any) -> tuple[str, datetime, str] | None:
    """The join key migration 211 designed: (symbol, ts, signal).

    ts is floored to whole seconds — the stamp and the sim row carry the
    same emit-time string, but any microsecond formatting drift between
    the two tables would otherwise break the join (15-min cadence makes
    second-granularity collision-free).
    """
    dt = _parse_ts(ts)
    if dt is None or not symbol or signal is None:
        return None
    dt = dt.replace(microsecond=0)
    side = "sell" if str(signal).lower().startswith("s") else "buy"
    return (str(symbol).upper(), dt, side)


# ─── Scoring math (pure — the heart of the audit) ──────────────────────────


def log_score_triple(p_w: float | None, p_l: float | None, p_e: float | None,
                     outcome: str) -> tuple[float | None, bool]:
    """log p(outcome) under one triple. Clipped at EPS; returns (score, clipped).

    None inputs (unscoreable) return (None, False) — the caller counts them.
    """
    if p_w is None or p_l is None or p_e is None:
        return None, False
    if outcome not in OUTCOMES:
        return None, False
    p = {"tp_hit": p_w, "sl_hit": p_l, "expired": p_e}[outcome]
    if not math.isfinite(p):
        return None, False
    if p < EPS:
        return math.log(EPS), True
    return math.log(p), False


def conditional_q(p_w: float | None, p_l: float | None,
                  p_e: float | None = None) -> float | None:
    """q = P(tp | resolve) for a triple — the decided-rows probability.

    Accepts the third component (p_e) for triple-unpacking call sites; it is
    deliberately ignored (the conditional is a function of the resolve split).
    """
    if p_w is None or p_l is None:
        return None
    denom = p_w + p_l
    if denom <= 0 or not math.isfinite(denom):
        return None
    return p_w / denom


def table_triple(p_win_bracket: float | None,
                 p_resolve: float | None) -> tuple[float | None, float | None, float | None]:
    """The live EV input's implied triple, exactly as apply_bracket_to_ev prices it.

    p_win_bracket (all-in TP share) is P(tp first); p_resolve is P(resolve).
    Unscored p_resolve fails open to 0 expire — the live contract — so a
    missing p_resolve yields the binary triple (p, 1-p, 0).
    """
    if p_win_bracket is None:
        return None, None, None
    p = float(p_win_bracket)
    if not (0.0 <= p <= 1.0):
        return None, None, None
    if p_resolve is None:
        return p, 1.0 - p, 0.0
    r = float(p_resolve)
    if not (0.0 <= r <= 1.0) or r < p:
        return None, None, None
    return p, r - p, 1.0 - r


def table_conditional_q(p_win_bracket: float | None,
                        p_resolve: float | None) -> float | None:
    """q = P(tp | resolve) for the table lookup (fail-open resolve = binary)."""
    tri = table_triple(p_win_bracket, p_resolve)
    return conditional_q(tri[0], tri[1])


def null_triple(tp_atr: float | None, sl_atr: float | None,
                t_bars: int | None, sigma: float | None) -> tuple[float, float, float]:
    """Recompute the analytic triple from the STAMP's own geometry.

    Degenerate geometry returns the expired-for-sure null — barrier.py's own
    honest answer for a bracket that cannot resolve.
    """
    try:
        t = int(t_bars) if t_bars else EXPIRY_BARS
    except (TypeError, ValueError):
        t = EXPIRY_BARS
    sig = sigma if (sigma is not None and math.isfinite(sigma) and sigma > 0) else 1.0
    a = tp_atr if (tp_atr is not None and math.isfinite(tp_atr)) else 0.0
    b = sl_atr if (sl_atr is not None and math.isfinite(sl_atr)) else 0.0
    n = barrier_null(max(a, 0.0), max(b, 0.0), t_bars=t, sigma=sig)
    return (n.p_win, n.p_loss, n.p_expire)


def brier(q: float, y: int) -> float:
    return (q - y) ** 2


def mean_log_score(scores: Sequence[float]) -> float | None:
    vals = [s for s in scores if s is not None]
    return sum(vals) / len(vals) if vals else None


def paired_bootstrap_ci(deltas: Sequence[float], n_boot: int,
                        seed: int) -> tuple[float, float] | None:
    """Percentile bootstrap CI (2.5/97.5) on the MEAN of paired per-row deltas.

    Row-level (correlated 15-min rows → optimistic CI — stated in the report;
    the bar asks the CI to clear regardless). Seeded → deterministic.
    """
    arr = np.asarray([d for d in deltas if d is not None], dtype=np.float64)
    if arr.size == 0 or n_boot <= 0:
        return None
    rng = np.random.default_rng(seed)
    vals: list[float] = []
    for _ in range(n_boot):
        idx = rng.integers(0, arr.size, size=arr.size)
        vals.append(float(arr[idx].mean()))
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return (float(lo), float(hi))


def reliability_bins(pairs: Sequence[tuple[float, int]]) -> list[dict[str, Any]]:
    """10 equal bins of predicted q: n, mean predicted, realized frequency."""
    bins: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for q, y in pairs:
        b = min(9, max(0, int(math.floor(q * 10.0))))
        bins[b].append((q, y))
    out: list[dict[str, Any]] = []
    for b in range(10):
        rows = bins.get(b, [])
        n = len(rows)
        out.append({
            "bin": f"{_BIN_EDGES[b]:.1f}–{_BIN_EDGES[b + 1]:.1f}",
            "n": n,
            "pred": (sum(q for q, _ in rows) / n) if n else None,
            "real": (sum(y for _, y in rows) / n) if n else None,
        })
    return out


# ─── The mechanical verdict (pre-registered bar — no discretion) ────────────


def verdict_for(ctx: dict[str, Any]) -> tuple[str, str]:
    """Phase B flip bar, mechanically applied.

    PASS-FLIP requires the posterior's full-triple log-score to be clearly
    ≥ the table lookup's: point estimate ≥ 0 AND bootstrap 95% CI lower
    bound ≥ 0, era-clean, on ≥ 500 joined-resolved + ≥ 500 paired rows.
    """
    gates: list[str] = []
    if ctx["fail_open"]:
        gates.append("era marker unavailable (fail-open)")
    if ctx["n_joined_resolved"] < ctx["min_n"]:
        gates.append(
            f"ledger depth {ctx['n_joined_resolved']} < {ctx['min_n']} joined resolved stamps"
        )
    paired = ctx["paired_full"]
    if paired["n"] < ctx["min_n"]:
        gates.append(f"paired depth {paired['n']} < {ctx['min_n']} (pbm ∧ table scoreable)")
    if gates:
        return ("NO-GO", "; ".join(gates) + " — keep collecting, discovery-grade only")

    d_mean = paired["mean"]
    ci = paired["ci"]
    ci_lb = ci[0] if ci else None
    if d_mean is None:
        return ("NO-GO", "no paired rows — discovery-grade only")
    if d_mean < 0:
        return ("FAIL", f"mean Δ(pbm − table) {d_mean:+.4f} < 0 — the table lookup prices better")
    if ci_lb is not None and ci_lb >= 0:
        v = "PASS-FLIP"
        r = (f"mean Δ {d_mean:+.4f} ≥ 0 AND CI lower bound {ci_lb:+.4f} ≥ 0 "
             f"(n={paired['n']}, era-clean)")
    else:
        v = "PASS-POINT"
        r = (f"mean Δ {d_mean:+.4f} ≥ 0 but CI lower bound "
             f"{_fmt(ci_lb, '.4f', plus=True)} < 0 — point estimate clears, "
             "confidence does not: keep collecting, do not flip")
    # Cross-check clause: the conditional read must not contradict the bar.
    pc = ctx["paired_cond"]
    if pc and pc["mean"] is not None and pc["n"] >= ctx["min_n"] and (
        (pc["mean"] < 0) != (d_mean < 0)
    ):
        r += (f" — SPLIT: conditional Δ reads {pc['mean']:+.4f} (opposite sign); "
              "the full-triple bar stands, but investigate before flipping")
    return (v, r)


# ─── Row preparation (pure — testable offline) ──────────────────────────────


def prepare_stamps(stamps: list[dict[str, Any]]) -> tuple[dict[Any, dict[str, Any]], dict[str, int]]:
    """Dedupe stamps onto the join key (latest created_at wins)."""
    keyed: dict[tuple[str, datetime, str], dict[str, Any]] = {}
    stats = {"fetched": len(stamps), "bad_key": 0, "dupes": 0}
    for s in sorted(stamps, key=lambda r: str(r.get("created_at") or "")):
        k = _key(s.get("symbol"), s.get("ts"), s.get("signal"))
        if k is None:
            stats["bad_key"] += 1
            continue
        if k in keyed:
            stats["dupes"] += 1
        keyed[k] = s  # later created_at overwrites — deterministic order above
    return keyed, stats


def join_records(
    stamps_by_key: dict[tuple[str, datetime, str], dict[str, Any]],
    sim_rows: list[dict[str, Any]],
    cutover_iso: str | None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join stamps to resolved sim rows on (symbol, ts, signal), era-clean.

    The joined record carries every scorer input side by side. Stamps are
    written only after the PBM shadow flip, but the cutover gate is enforced
    anyway — era-clean by construction, not by assumption.
    """
    stats = {
        "sim_rows": len(sim_rows), "sim_bad_key": 0, "sim_no_stamp": 0,
        "stamps_era_dropped": 0, "joined": 0, "joined_resolved": 0,
        "stamp_outcome_backfilled": 0, "stamp_outcome_mismatch": 0,
        "null_p_w_mismatch": 0, "sim_key_collision": 0, "pending": 0,
    }
    cut = str(cutover_iso).replace("Z", "+00:00") if cutover_iso else None
    sim_by_key: dict[tuple[str, datetime, str], dict[str, Any]] = {}
    for r in sim_rows:
        k = _key(r.get("symbol"), r.get("ts"), r.get("signal"))
        if k is None:
            stats["sim_bad_key"] += 1
            continue
        if k in sim_by_key:
            stats["sim_key_collision"] += 1
        sim_by_key[k] = r

    records: list[dict[str, Any]] = []
    matched_sim: set[tuple[str, datetime, str]] = set()
    for k, s in stamps_by_key.items():
        if cut is not None and k[1].isoformat().replace("+00:00", "") < cut.replace("+00:00", ""):
            stats["stamps_era_dropped"] += 1
            continue
        sim = sim_by_key.get(k)
        if sim is None:
            # Emitted but unresolved (or resolved outside the fetch window):
            # a pending prediction — counted, never scored.
            stats["pending"] += 1
            continue
        matched_sim.add(k)

        stats["joined"] += 1
        outcome_sim = str(sim.get("outcome"))
        stamp_outcome = s.get("outcome")
        if stamp_outcome is not None and outcome_sim is not None and str(stamp_outcome) != outcome_sim:
            stats["stamp_outcome_mismatch"] += 1
        if stamp_outcome is None and outcome_sim is not None:
            stats["stamp_outcome_backfilled"] += 1
        if outcome_sim not in OUTCOMES:
            continue
        stats["joined_resolved"] += 1

        t_bars = s.get("t_bars")
        sigma = _to_float(s.get("sigma"))
        n_w, n_l, n_e = null_triple(_to_float(s.get("tp_atr")), _to_float(s.get("sl_atr")),
                                    t_bars if t_bars else None, sigma)
        p_w_null_stamped = _to_float(s.get("p_w_null"))
        if p_w_null_stamped is not None and abs(p_w_null_stamped - n_w) > 1e-4:
            stats["null_p_w_mismatch"] += 1
        tw, tl, te = table_triple(_to_float(sim.get("p_win_bracket")), _to_float(sim.get("p_resolve")))
        records.append({
            "symbol": k[0], "ts": k[1], "side": k[2], "source": s.get("source"),
            "outcome": outcome_sim,
            "pbm": (_to_float(s.get("p_w")), _to_float(s.get("p_l")), _to_float(s.get("p_e"))),
            "null": (n_w, n_l, n_e),
            "table": (tw, tl, te),
            "q_dir": _to_float(sim.get("p_win_dir")),
            "n_eff": _to_float(s.get("n_eff")),
            "ev_lb_pass": s.get("ev_lb_pass"),
        })
    stats["sim_no_stamp"] = len(sim_by_key) - len(matched_sim)
    return records, stats


# ─── Measurement (pure) ─────────────────────────────────────────────────────


def _scorer_cols(rec: dict[str, Any]) -> dict[str, tuple[float | None, float | None, float | None]]:
    return {"pbm": rec["pbm"], "null": rec["null"], "table": rec["table"]}


def measure(records: list[dict[str, Any]], *, n_boot: int, seed: int,
            min_n: int, min_symbol_n: int, cutover_iso: str | None,
            stats: dict[str, int], since: datetime,
            symbols: list[str] | None, limit: int | None,
            backfilled: dict[str, int] | None) -> dict[str, Any]:
    """All scoring, pairing, binning, per-symbol summaries, and the verdict."""
    clipped: dict[str, int] = {"pbm": 0, "null": 0, "table": 0}
    full_scores: dict[str, list[float]] = {"pbm": [], "null": [], "table": []}
    full_n: dict[str, int] = {"pbm": 0, "null": 0, "table": 0}
    cond_pairs: dict[str, list[tuple[float, int]]] = {
        "pbm": [], "null": [], "table": [], "dir": [],
    }
    paired_full_rows: list[dict[str, Any]] = []
    paired_cond_rows: list[dict[str, Any]] = []
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for rec in records:
        by_symbol[rec["symbol"]].append(rec)
        outcome = rec["outcome"]
        cols = _scorer_cols(rec)
        row_full: dict[str, float | None] = {}
        for name, tri in cols.items():
            sc, was_clipped = log_score_triple(tri[0], tri[1], tri[2], outcome)
            row_full[name] = sc
            if sc is not None:
                full_scores[name].append(sc)
                full_n[name] += 1
            if was_clipped:
                clipped[name] += 1
        # The paired population: BOTH pbm and table scoreable on this row.
        if row_full["pbm"] is not None and row_full["table"] is not None:
            paired_full_rows.append(
                {"symbol": rec["symbol"], "d_pbm_table": row_full["pbm"] - row_full["table"],
                 "d_pbm_null": row_full["pbm"] - row_full["null"] if row_full["null"] is not None else None,
                 "d_table_null": row_full["table"] - row_full["null"] if row_full["null"] is not None else None,
                 "outcome": outcome})
        if outcome in DECIDED:
            y = 1 if outcome == "tp_hit" else 0
            q_pbm = conditional_q(*rec["pbm"])
            q_null = conditional_q(*rec["null"])
            q_tab = conditional_q(*rec["table"])
            q_dir = rec["q_dir"]
            qs = {"pbm": q_pbm, "null": q_null, "table": q_tab, "dir": q_dir}
            for name, q in qs.items():
                if q is not None:
                    cond_pairs[name].append((q, y))
            if q_pbm is not None and q_tab is not None:
                paired_cond_rows.append(
                    {"symbol": rec["symbol"], "d": q_pbm - q_tab, "y": y})

    def _pair(rows: Sequence[dict[str, Any]], field: str) -> dict[str, Any]:
        vals = [r[field] for r in rows if r.get(field) is not None]
        mean = (sum(vals) / len(vals)) if vals else None
        ci = paired_bootstrap_ci(vals, n_boot, seed) if vals else None
        return {"n": len(vals), "mean": mean, "ci": ci}

    paired_full = _pair(paired_full_rows, "d_pbm_table")
    paired_cond = _pair(paired_cond_rows, "d")
    d_pbm_null = _pair(paired_full_rows, "d_pbm_null")
    d_table_null = _pair(paired_full_rows, "d_table_null")

    scorer_rows: dict[str, dict[str, Any]] = {}
    for name in ("pbm", "null", "table"):
        ls = mean_log_score(full_scores[name])
        qs = cond_pairs.get(name, [])
        b = sum(brier(q, y) for q, y in qs) / len(qs) if qs else None
        scorer_rows[name] = {
            "n_full": full_n[name], "log_score": ls, "n_cond": len(qs),
            "brier": b, "clipped": clipped[name],
            "q_mean": (sum(q for q, _ in qs) / len(qs)) if qs else None,
        }
    qs = cond_pairs.get("dir", [])
    scorer_rows["dir"] = {
        "n_full": 0, "log_score": None, "n_cond": len(qs),
        "brier": (sum(brier(q, y) for q, y in qs) / len(qs)) if qs else None,
        "clipped": 0,
        "q_mean": (sum(q for q, _ in qs) / len(qs)) if qs else None,
    }

    realized_wr = None
    if records:
        decided = [r for r in records if r["outcome"] in DECIDED]
        realized_wr = (sum(1 for r in decided if r["outcome"] == "tp_hit") / len(decided)
                       if decided else None)

    per_symbol = []
    for sym in sorted(by_symbol):
        rs = by_symbol[sym]
        pf = [r["d_pbm_table"] for r in paired_full_rows if r["symbol"] == sym
              and r.get("d_pbm_table") is not None]
        mean = (sum(pf) / len(pf)) if pf else None
        ci = paired_bootstrap_ci(pf, n_boot, seed) if len(pf) >= min_symbol_n else None
        decided = [r for r in rs if r["outcome"] in DECIDED]
        wr = (sum(1 for r in decided if r["outcome"] == "tp_hit") / len(decided)
              if decided else None)
        qmean = None
        qs = [conditional_q(*r["pbm"]) for r in decided]
        qs = [q for q in qs if q is not None]
        if qs:
            qmean = sum(qs) / len(qs)
        per_symbol.append({
            "symbol": sym, "n": len(rs), "n_decided": len(decided),
            "d_mean": mean, "ci": ci, "wr": wr, "pbm_q_mean": qmean,
        })

    ctx = {
        "since": since, "symbols": symbols, "limit": limit,
        "cutover_iso": cutover_iso,
        "fail_open": cutover_iso is None,
        "stats": stats, "clipped": clipped,
        "n_records": len(records),
        "n_joined_resolved": stats["joined_resolved"],
        "scorers": scorer_rows,
        "paired_full": paired_full, "paired_cond": paired_cond,
        "d_pbm_null": d_pbm_null, "d_table_null": d_table_null,
        "reliability": {name: reliability_bins(pairs)
                        for name, pairs in cond_pairs.items()},
        "per_symbol": per_symbol, "realized_wr": realized_wr,
        "min_n": min_n, "min_symbol_n": min_symbol_n,
        "n_boot": n_boot, "seed": seed,
        "backfilled": backfilled or {},
    }
    ctx["verdict"], ctx["verdict_reason"] = verdict_for(ctx)
    return ctx


# ─── Report rendering ───────────────────────────────────────────────────────


def _scorer_label(name: str) -> str:
    return {
        "pbm": "**pbm** posterior triple (nt_pbm_shadow stamp)",
        "null": "**null** analytic barrier (recomputed from stamp geometry)",
        "table": "**table** live EV input (p_win_bracket + p_resolve)",
        "dir": "**dir** raw directional p_win (overconfidence exhibit)",
    }[name]


def render_report(ctx: dict[str, Any], now: datetime) -> str:
    s = ctx["stats"]
    L: list[str] = []
    L.append("# PBM calibration audit — Phase B flip bar (measured or nothing)")
    L.append("")
    L.append(
        f"Generated {now.strftime('%Y-%m-%d %H:%M')} UTC · since "
        f"{ctx['since'].isoformat().replace('+00:00', 'Z')} · "
        f"bootstrap {ctx['n_boot']} resamples (seed {ctx['seed']})"
    )
    L.append("Population: nt_pbm_shadow stamps joined to resolved nt_signal_sim rows on "
             "(symbol, ts, signal) — migration 211's designated acceptance replay.")
    L.append("")
    L.append("## The flip bar (pre-registered)")
    L.append("")
    L.append(
        "`pbm_ev_mode` may flip `off → shadow → live` only when the **Phase A ledger "
        "log-score ≥ table lookup, era-clean** (worklog 20261014_PBM_COMPLETE_BUILD_"
        "PHASES_A_E.md §4). Proper scoring rules measure what AUC cannot: AUC is "
        "monotone-invariant, structurally blind to overconfidence. PASS-FLIP requires "
        "mean Δ(pbm − table) full-triple ≥ 0 AND bootstrap 95% CI lower bound ≥ 0 "
        f"on ≥ {ctx['min_n']} joined-resolved and ≥ {ctx['min_n']} paired rows; "
        "PASS-POINT = point estimate clears but the CI does not (keep collecting)."
    )
    L.append("")
    L.append("## Era discipline & coverage")
    L.append("")
    L.append(f"- cutover marker `nt_calibration_cutover`: **{ctx['cutover_iso'] or 'MISSING (fail-open)'}**")
    L.append(f"- stamps fetched: **{s['fetched']}** (bad keys {s['bad_key']}, dupes {s['dupes']})")
    L.append(f"- stamps dropped as pre-cutover: **{s['stamps_era_dropped']}**")
    L.append(f"- resolved sim rows fetched: **{s['sim_rows']}** (bad keys {s['sim_bad_key']}, "
             f"key collisions {s['sim_key_collision']})")
    L.append(f"- stamps still pending (no resolved sim row): **{s.get('pending', 0)}**")
    L.append(f"- joined: **{s['joined']}** · joined & resolved: **{s['joined_resolved']}**")
    L.append(f"- stamp outcome already backfilled vs NULL: mismatched "
             f"**{s['stamp_outcome_mismatch']}** (stamped ≠ resolved), "
             f"backfillable **{s['stamp_outcome_backfilled']}**")
    L.append(f"- recomputed null vs stamped p_w_null mismatches (>1e-4): **{s['null_p_w_mismatch']}**")
    if ctx["fail_open"]:
        L.append("- **ERA FILTER UNAVAILABLE this run — numbers are discovery-grade only.**")
    if ctx["backfilled"]:
        parts = [f"{k}: {v}" for k, v in sorted(ctx["backfilled"].items()) if v]
        L.append(f"- backfill PATCHed this run: {', '.join(parts) if parts else 'none needed'}"
                 f" (idempotent — only NULL outcomes written)")
    L.append("")
    L.append("## Full-triple log-score (all resolved stamps incl. expired — the PRIMARY)")
    L.append("")
    L.append("| scorer | n | mean log-score | clipped | notes |")
    L.append("|---|---:|---:|---:|---|")
    for name in ("pbm", "null", "table"):
        st = ctx["scorers"][name]
        L.append(f"| {_scorer_label(name)} | {st['n_full']} | {_fmt(st['log_score'], '.4f')} "
                 f"| {st['clipped']} | log p(outcome) under the triple |")
    L.append("")
    L.append("## Conditional calibration (decided rows: q = P(tp \\| resolve))")
    L.append("")
    L.append(f"Realized win rate on decided rows: **{_fmt(ctx['realized_wr'], '.4f')}**")
    L.append("")
    L.append("| scorer | n | mean q | Brier |")
    L.append("|---|---:|---:|---:|")
    for name in ("pbm", "null", "table", "dir"):
        st = ctx["scorers"][name]
        L.append(f"| {_scorer_label(name)} | {st['n_cond']} | {_fmt(st['q_mean'], '.4f')} "
                 f"| {_fmt(st['brier'], '.4f')} |")
    L.append("")
    L.append("## Paired log-score differences (common scoreable rows)")
    L.append("")
    L.append("| comparison | population | n paired | mean Δ | 95% CI |")
    L.append("|---|---|---:|---:|---|")
    for label, key, pop in (
        ("pbm − table", "paired_full", "full triple"),
        ("pbm − table", "paired_cond", "conditional"),
        ("pbm − null", "d_pbm_null", "full triple"),
        ("table − null", "d_table_null", "full triple"),
    ):
        p = ctx[key]
        ci = p["ci"]
        L.append(f"| {label} | {pop} | {p['n']} | {_fmt(p['mean'], '.4f', plus=True)} "
                 f"| [{_fmt(ci[0] if ci else None, '.4f', plus=True)}, "
                 f"{_fmt(ci[1] if ci else None, '.4f', plus=True)}] |")
    L.append("")
    L.append("## Reliability curves (decided rows, 0.1 bins)")
    L.append("")
    for name in ("pbm", "table", "dir"):
        L.append(f"### {name}")
        L.append("")
        L.append("| q bin | n | mean predicted | realized frequency |")
        L.append("|---|---:|---:|---:|")
        for b in ctx["reliability"][name]:
            L.append(f"| {b['bin']} | {b['n']} | {_fmt(b['pred'], '.3f')} | {_fmt(b['real'], '.3f')} |")
        L.append("")
    L.append("## Per-symbol Δ(pbm − table), full triple")
    L.append("")
    header = (
        f"| symbol | n resolved | n decided | mean Δ | "
        f"95% CI (n≥{ctx['min_symbol_n']}) | realized WR | pbm mean q |"
    )
    L.append(header)
    L.append("|---|---:|---:|---:|---|---:|---:|")
    for row in ctx["per_symbol"]:
        ci = row["ci"]
        L.append(f"| {row['symbol']} | {row['n']} | {row['n_decided']} "
                 f"| {_fmt(row['d_mean'], '.4f', plus=True)} "
                 f"| [{_fmt(ci[0] if ci else None, '.4f', plus=True)}, "
                 f"{_fmt(ci[1] if ci else None, '.4f', plus=True)}] "
                 f"| {_fmt(row['wr'], '.3f')} | {_fmt(row['pbm_q_mean'], '.3f')} |")
    L.append("")
    L.append("## Verdict (mechanically applied bar — no discretion)")
    L.append("")
    L.append(f"- **{ctx['verdict']}** — {ctx['verdict_reason']}.")
    L.append("")
    if ctx["verdict"] == "PASS-FLIP":
        L.append("> The posterior's price is clearly ≥ the table lookup on era-clean "
                 "resolved evidence. Next: flip `pbm_ev_mode: \"off\" → \"shadow\"`, "
                 "observe the substitution logs, and re-run this audit on a fresh "
                 "window before considering `\"live\"`. This harness never flips a flag.")
    elif ctx["verdict"] == "PASS-POINT":
        L.append("> Point estimate clears the bar but the confidence interval does not. "
                 "**Keep collecting ledger depth; do not flip.**")
    elif ctx["verdict"] == "FAIL":
        L.append("> The table lookup is the better-calibrated price on this evidence. "
                 "The posterior is not ready to replace it — keep `pbm_ev_mode: \"off\"` "
                 "and investigate the per-symbol table before any retry.")
    else:
        L.append("> Coverage gates unmet — the ledger is younger than the bar. "
                 "This is the expected state for the first few days after `pbm_mode: shadow`; "
                 "re-run weekly alongside the drift AUC harness.")
    L.append("")
    L.append("## Notes / honesty")
    L.append("")
    L.append("- Log-scores clip probabilities at 1e-6 (a wrong certainty costs −13.8, not −∞); "
             "clipped counts are reported per scorer.")
    L.append("- The table scorer mirrors the LIVE fail-open contract exactly: unscored "
             "`p_resolve` prices a binary triple `(p, 1−p, 0)` — the audit scores what "
             "production prices, not an idealized table.")
    L.append("- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate "
             "rows, so CIs are optimistic (n inflated). The bar demands the CI lower bound "
             "clear 0 regardless — no episode dedupe, matching the platform's own "
             "measurement conventions.")
    L.append("- The verdict is POOLED (one number, one gate). Per-symbol rows are "
             f"diagnostics; their CIs print only at n ≥ {ctx['min_symbol_n']}.")
    L.append("- Scoring is evaluation of FIXED stamps (nothing is fit): there is no "
             "train/test leakage to guard. The posterior was written at emit time and "
             "cannot see the outcome it is scored against.")
    L.append("- `dir` is scored to exhibit the raw model's calibration (the overconfidence "
             "the calibration-enforcement layer masks); it is not a Phase B candidate.")
    L.append("")
    return "\n".join(L) + "\n"


# ─── Supabase fetch (the only I/O) ──────────────────────────────────────────


async def _fetch_table(table: str, columns: str, since: datetime,
                       symbols: list[str] | None, limit: int | None,
                       outcome_filter: bool) -> list[dict[str, Any]]:
    """Generic paginated PostgREST fetch (mirrors the drift harness)."""
    rows: list[dict[str, Any]] = []
    off = 0
    base: dict[str, str] = {
        "select": columns,
        "ts": f"gte.{since.isoformat().replace('+00:00', 'Z')}",
        "order": "ts.asc",
        "limit": str(PAGE),
    }
    if outcome_filter:
        base["outcome"] = "in.(tp_hit,sl_hit,expired)"
    if symbols:
        base["symbol"] = f"in.({','.join(symbols)})"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = dict(base)
            params["offset"] = str(off)
            resp = await client.get(_url(table), headers=_headers(), params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(
                    f"unexpected {table} response shape: {type(batch).__name__}")
            rows.extend(batch)
            if len(batch) < PAGE:
                break
            off += len(batch)
            if limit is not None and len(rows) >= limit:
                rows = rows[:limit]
                break
            if off > 500_000:  # belt-and-braces pagination guard
                logger.warning("pagination guard hit at offset %d on %s — stopping", off, table)
                break
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
    return rows


async def backfill_outcomes(pairs: list[tuple[str, str]], chunk: int = 40) -> dict[str, int]:
    """PATCH NULL stamp outcomes from joined resolved rows (idempotent).

    ``pairs`` = (stamp id, resolved outcome). Only rows whose stamp outcome
    IS NULL are sent; the caller filters. Migration 211 designates this
    harness as the backfiller — the resolver never writes here.
    """
    counts: dict[str, int] = defaultdict(int)
    by_outcome: dict[str, list[str]] = defaultdict(list)
    for sid, outcome in pairs:
        by_outcome[outcome].append(sid)
    async with httpx.AsyncClient(timeout=15.0) as client:
        for outcome, ids in by_outcome.items():
            for i in range(0, len(ids), chunk):
                batch = ids[i:i + chunk]
                params = {
                    "id": f"in.({','.join(batch)})",
                    "outcome": "is.null",
                }
                resp = await client.patch(
                    _url("nt_pbm_shadow"),
                    headers={**_headers(), "Content-Type": "application/json"},
                    params=params,
                    json={"outcome": outcome},
                )
                resp.raise_for_status()
                counts[outcome] += len(batch)
    return dict(counts)


# ─── Offline self-test (--selftest: pure, no network) ───────────────────────


def _selftest() -> int:  # pragma: no cover — exercised via CLI
    rng = np.random.default_rng(7)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    print("selftest: scoring primitives")
    # log_score_triple basics
    sc, cl = log_score_triple(0.3, 0.5, 0.2, "tp_hit")
    check("log_score exact", sc is not None and abs(sc - math.log(0.3)) < 1e-12 and not cl)
    sc, cl = log_score_triple(0.0, 1.0, 0.0, "tp_hit")
    check("clip at EPS", sc is not None and abs(sc - math.log(EPS)) < 1e-9 and cl)
    check("unscoreable → None", log_score_triple(None, 0.5, 0.5, "tp_hit")[0] is None)
    check("bad outcome → None", log_score_triple(0.3, 0.5, 0.2, "weird")[0] is None)
    # conditional_q + table_triple semantics
    check("conditional_q", abs((conditional_q(0.2, 0.3) or 0) - 0.4) < 1e-12)
    check("conditional_q degenerate", conditional_q(0.0, 0.0) is None)
    tt = table_triple(0.25, 0.7)
    check("table_triple all_in",
          tt[0] == 0.25 and abs(tt[1] - 0.45) < 1e-12 and abs(tt[2] - 0.30) < 1e-12,
          f"tt={tt}")
    check("table_triple fail-open binary", table_triple(0.25, None) == (0.25, 0.75, 0.0))
    check("table_triple invalid", all(v is None for v in table_triple(0.8, 0.5)))
    check("table_triple unscored", all(v is None for v in table_triple(None, 0.7)))
    check("table_conditional_q", abs((table_conditional_q(0.25, 0.7) or 0) - 0.25 / 0.7) < 1e-12)

    print("selftest: calibration ordering (the property AUC cannot see)")
    # Two scorers with IDENTICAL ranking, different calibration.
    qs = np.linspace(0.05, 0.95, 400)
    y = (rng.random(qs.size) < qs).astype(int)  # outcomes drawn from TRUE q
    good = [math.log(max(q, EPS)) if yy else math.log(max(1 - q, EPS)) for q, yy in zip(qs, y)]
    over = [math.log(max(min(q * 0.6 + 0.35, 1 - EPS), EPS)) if yy
            else math.log(max(1 - (q * 0.6 + 0.35), EPS)) for q, yy in zip(qs, y)]
    check("calibrated beats shrunk-overconfident on log-score",
          sum(good) / len(good) > sum(over) / len(over))
    b_good = np.mean([(q - yy) ** 2 for q, yy in zip(qs, y)])
    b_over = np.mean([((q * 0.6 + 0.35) - yy) ** 2 for q, yy in zip(qs, y)])
    check("Brier agrees with log-score ordering", b_good < b_over)

    print("selftest: paired bootstrap")
    deltas = np.asarray(good) - np.asarray(over)
    ci = paired_bootstrap_ci(list(deltas), 200, 13)
    check("CI excludes 0 in the right direction", ci is not None and ci[0] > 0,
          f"ci={ci}")
    ci0 = paired_bootstrap_ci([0.0] * 300, 200, 13)
    check("CI brackets 0 on identical scorers", ci0 is not None and ci0[0] <= 0 <= ci0[1],
          f"ci={ci0}")

    print("selftest: reliability bins")
    pairs = [(0.04, 0), (0.06, 1), (0.95, 1), (0.96, 1), (0.55, 0), (0.45, 1)]
    rb = reliability_bins(pairs)
    b0, b4, b5, b9 = rb[0], rb[4], rb[5], rb[9]
    check("bin membership", b0["n"] == 2 and b9["n"] == 2 and b4["n"] == 1 and b5["n"] == 1)
    check("bin stats", abs(b9["real"] - 1.0) < 1e-12 and abs(b0["pred"] - 0.05) < 1e-9)

    print("selftest: verdict branches")
    def mk_ctx(mean: float, ci: tuple[float, float] | None, n: int = 900,
               cond_mean: float | None = None, fail_open: bool = False,
               paired_n: int | None = None) -> dict[str, Any]:
        return {
            "fail_open": fail_open, "n_joined_resolved": n, "min_n": 500,
            "paired_full": {"n": paired_n if paired_n is not None else n,
                            "mean": mean, "ci": ci},
            "paired_cond": {"n": 600, "mean": cond_mean, "ci": None},
        }
    v, _ = verdict_for(mk_ctx(0.05, (0.02, 0.08)))
    check("PASS-FLIP branch", v == "PASS-FLIP")
    v, _ = verdict_for(mk_ctx(0.05, (-0.02, 0.08)))
    check("PASS-POINT branch", v == "PASS-POINT")
    v, _ = verdict_for(mk_ctx(-0.05, (-0.08, -0.02)))
    check("FAIL branch", v == "FAIL")
    v, _ = verdict_for(mk_ctx(0.05, (0.02, 0.08), n=300))
    check("NO-GO depth branch", v == "NO-GO")
    v, _ = verdict_for(mk_ctx(0.05, (0.02, 0.08), fail_open=True))
    check("NO-GO era branch", v == "NO-GO")
    v, r = verdict_for(mk_ctx(0.05, (0.02, 0.08), cond_mean=-0.03))
    check("SPLIT cross-check flagged", v == "PASS-FLIP" and "SPLIT" in r)

    print("selftest: join + full pipeline on synthetic stamps")
    base_ts = datetime(2026, 10, 14, tzinfo=timezone.utc)
    stamps, sims = [], []
    for i in range(600):
        ts = base_ts + timedelta(minutes=15 * i)
        outcome = ("tp_hit", "sl_hit", "expired")[i % 3]
        # Posterior: well-calibrated flat (0.34, 0.33, 0.33). Table: the SAME
        # ranking but overconfident toward tp — (0.44, 0.33, 0.23), constant
        # per row (no oracle leak: probabilities never see the outcome).
        stamps.append({
            "id": f"row-{i}", "symbol": "TESTUSD", "ts": ts.isoformat(),
            "signal": "buy" if i % 2 else "sell", "source": "light",
            "tp_atr": 4.0, "sl_atr": 4.0, "t_bars": 48, "sigma": 1.0,
            "p_w": 0.34, "p_l": 0.33, "p_e": 0.33, "p_w_null": None,
            "gap": 0.0, "n_eff": 30.0, "ev_atr": 0.0, "ev_lb_atr": 0.0,
            "ev_lb_pass": True, "bracket_bucket": "2-4", "vol_tier": "med",
            "deadline_class": "standard", "era": "live", "outcome": None,
            "created_at": ts.isoformat(),
        })
        sims.append({
            "symbol": "TESTUSD", "ts": ts.isoformat(),
            "signal": "buy" if i % 2 else "sell", "outcome": outcome,
            "p_win_bracket": 0.44, "p_resolve": 0.77, "p_win_dir": 0.55,
        })
    keyed, kstats = prepare_stamps(stamps)
    check("stamp keying", len(keyed) == 600 and kstats["bad_key"] == 0)
    records, jstats = join_records(keyed, sims, "2026-10-02T00:00:00Z")
    jstats.update(kstats)
    check("join completeness", len(records) == 600 and jstats["joined_resolved"] == 600,
          f"stats={jstats}")
    check("backfillable count", jstats["stamp_outcome_backfilled"] == 600)
    ctx = measure(records, n_boot=200, seed=13, min_n=500, min_symbol_n=100,
                  cutover_iso="2026-10-02T00:00:00Z", stats=jstats,
                  since=base_ts, symbols=None, limit=None, backfilled=None)
    # table's triple deliberately overconfident → pbm should win on log-score.
    check("synthetic pbm beats overconfident table",
          ctx["paired_full"]["mean"] is not None and ctx["paired_full"]["mean"] > 0,
          f"mean={ctx['paired_full']['mean']}")
    check("synthetic verdict PASS-FLIP", ctx["verdict"] == "PASS-FLIP",
          f"verdict={ctx['verdict']} reason={ctx['verdict_reason']}")
    check("reliability tables populated",
          sum(b["n"] for b in ctx["reliability"]["pbm"]) == 400)
    text = render_report(ctx, datetime(2026, 10, 16, 20, 51, tzinfo=timezone.utc))
    check("renderer emits every section",
          all(sec in text for sec in (
              "## The flip bar", "## Full-triple log-score", "## Paired log-score",
              "## Reliability curves", "## Per-symbol", "## Verdict", "## Notes / honesty")))
    out = Path("/tmp/pbm_calibration_selftest.md")
    out.write_text(text, encoding="utf-8")
    print(f"  rendered synthetic report → {out}")

    if failures:
        print(f"selftest: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("selftest: all checks passed")
    return 0


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Phase B audit: is the posterior's price calibrated vs the live "
        "table lookup? (log-score / Brier / reliability, era-clean, read-only)",
    )
    ap.add_argument("--since", default=None, metavar="ISO",
                    help=f"ISO date/datetime UTC (default: {DEFAULT_LOOKBACK_DAYS} days back)")
    ap.add_argument("--limit", type=int, default=None, help="optional cap on fetched rows")
    ap.add_argument("--symbols", default=None, metavar="A,B",
                    help="optional comma-separated symbol whitelist")
    ap.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP,
                    help="bootstrap resamples for the paired Δ CIs")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap RNG seed")
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N,
                    help="joined-resolved + paired rows required for a verdict")
    ap.add_argument("--min-symbol-n", type=int, default=DEFAULT_MIN_SYMBOL_N,
                    help="per-symbol rows before a per-symbol CI prints")
    ap.add_argument("--backfill", action="store_true",
                    help="PATCH NULL stamp outcomes from joined resolved rows "
                         "(idempotent; migration 211's designated backfiller)")
    ap.add_argument("--out", default=None, help="report path override")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline synthetic self-test (no network, no DB)")
    return ap.parse_args(argv)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()

    if args.selftest:
        raise SystemExit(_selftest())

    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        raise SystemExit(
            "SUPABASE_URL/SUPABASE_KEY not configured — nothing to measure. "
            "(Offline verification: --selftest requires no network.)"
        )

    since = _parse_since(args.since)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else None
    )

    cutover_iso = await _fetch_cutover_at()
    logger.info("cutover marker: %s", cutover_iso or "MISSING (fail-open)")

    stamps = await _fetch_table("nt_pbm_shadow", NT_SHADOW_COLUMNS, since, symbols,
                                args.limit, outcome_filter=False)
    logger.info("fetched %d nt_pbm_shadow stamps since %s", len(stamps), since.isoformat())
    sim_rows = await _fetch_table("nt_signal_sim", NT_SIM_COLUMNS, since, symbols,
                                  args.limit, outcome_filter=True)
    logger.info("fetched %d resolved nt_signal_sim rows since %s",
                len(sim_rows), since.isoformat())

    keyed, kstats = prepare_stamps(stamps)
    records, jstats = join_records(keyed, sim_rows, cutover_iso)
    jstats.update(kstats)
    logger.info("joined %d resolved stamps (pending %d, no-stamp sim rows %d)",
                jstats["joined_resolved"], jstats.get("pending", 0), jstats["sim_no_stamp"])

    backfilled: dict[str, int] | None = None
    if args.backfill:
        pairs: list[tuple[str, str]] = []
        sim_by_key: dict[tuple[str, datetime, str], dict[str, Any]] = {}
        for r in sim_rows:
            k = _key(r.get("symbol"), r.get("ts"), r.get("signal"))
            if k is not None:
                sim_by_key[k] = r
        for k, s in keyed.items():
            sim = sim_by_key.get(k)
            if sim and s.get("outcome") is None and str(sim.get("outcome")) in OUTCOMES:
                pairs.append((str(s["id"]), str(sim["outcome"])))
        if pairs:
            backfilled = await backfill_outcomes(pairs)
            logger.info("backfilled %d stamp outcomes: %s",
                        sum(backfilled.values()), backfilled)
        else:
            backfilled = {}

    ctx = measure(
        records,
        n_boot=args.bootstrap,
        seed=args.seed,
        min_n=args.min_n,
        min_symbol_n=args.min_symbol_n,
        cutover_iso=cutover_iso,
        stats=jstats,
        since=since,
        symbols=symbols,
        limit=args.limit,
        backfilled=backfilled,
    )

    now = datetime.now(timezone.utc)
    text = render_report(ctx, now)
    out_path = Path(args.out) if args.out else REPORT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    pf = ctx["paired_full"]
    logger.info("paired Δ(pbm − table) full-triple: n=%d mean=%s ci=%s",
                pf["n"], _fmt(pf["mean"], ".4f", plus=True), pf["ci"])
    logger.info("VERDICT %s — %s", ctx["verdict"], ctx["verdict_reason"])
    logger.info("wrote %s", out_path)


if __name__ == "__main__":
    asyncio.run(main())
