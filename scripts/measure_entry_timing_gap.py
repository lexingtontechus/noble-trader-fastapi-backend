#!/usr/bin/env python3
"""measure_entry_timing_gap.py — entry-timing edge search: where (if anywhere) is mu > 0?

The Posterior Barrier Model prices brackets; it does not pick bars. Today the
15-min clock picks the bar, the HMM regime label picks direction
(orchestrator.py:4073: "short" if "bear" in regime_label), and the EV engine
only prices the entry it is GIVEN — the live system has no vocabulary for
"which bar within this state". Edge can therefore only exist as a CONDITIONAL
mu != 0, and the per-signal realized selection gap

    g_i = 1[outcome == 'tp_hit'] - p_w_null

IS the empirical cell-level mu in win-probability space: how much the realized
win rate of the entries the clock actually took exceeds the random-walk null's
first-touch TP probability for the SAME bracket. Negative gap = adverse entry
timing — the platform's measured problem (~-30pt at tight buckets). This
harness searches the covariate conditioning under which mu > 0, at the SAME
promotion bar Phase E applies to drift candidates
(scripts/measure_pbm_drift_auc.py): walk-forward, measured or nothing.

Promotion bar (pre-registered, deliberately hard):

    discovery half (earlier 50% of rows by ts): mean gap > 0 AND n >= min_cell_n
    confirmation half (later 50%): mean gap > 0 AND bootstrap 95% CI lower
                                   bound > 0 AND n >= min_cell_n  ->  PROMOTE
    anything else -> STAY-OFF.

Candidate family (small and PRE-REGISTERED): every single-dimension condition
over {symbol, side, UTC 4-hour bucket, vol tier, regime class, side-vs-regime
alignment, qualified} plus four 2-way conjunction families (side x regime
class, side x hour bucket, side x alignment, symbol x side). No 3-way. The
multiple-testing posture is honest because the bar is CONFIRMATION ON
HELD-OUT DATA, not significance on the discovery half: discovery only
nominates; only the later half can promote.

Data: era-clean resolved rows of nt_signal_sim joined OFFLINE to the PBM
shadow ledger nt_pbm_shadow on (symbol, ts, signal) — one ledger row per
emitted signal; sim rows without a ledger row still count (null recomputed
from their own geometry). p_w_null prefers the ledger's stamped null and
falls back to barrier.barrier_null_from_prices on the sim row exactly as the
drift harness prices it (T = EXPIRY_BARS, sigma = posterior.pbm_sigma_atr());
rows with neither are dropped and counted. The ledger's own `gap` column is
posterior - null (a calibration object, NOT realized) — g_i here is always
recomputed from outcome x p_w_null. Era discipline: rows before the
nt_calibration_cutover marker (bracket_reachability._fetch_cutover_at) are
excluded; missing marker -> fail-open, and the report SAYS SO.

Offline and read-only: fetches over Supabase REST (supabase_client._url/
_headers, 1000/page offset pagination), writes one markdown report. No live
wiring, no writes, no flags flipped. Never imports orchestrator/heavy_sweep/
eod_resolver.

Honest expected outcome: given the measured record, MOST predicates land
STAY-OFF — and that is the system working, not failing. A fence cannot
create timing skill; this harness's job is to make the absence of edge
visible and cheap to re-verify, and to surface a mu > 0 cell if one ever
actually forms. A PROMOTE verdict is the rare event that earns the next step
(a shadow qualifier stamp — a separate change, never this harness).

Run:
    PYTHONPATH="$PWD" python3 scripts/measure_entry_timing_gap.py              # live (needs creds)
    PYTHONPATH="$PWD" python3 scripts/measure_entry_timing_gap.py --synthetic  # offline self-test
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

# .env.local first (same pattern as scripts/replay_bands_era_clean.py) so the
# regime_platform settings module sees SUPABASE_URL/KEY at import time.
_ENV_FILE = ROOT / ".env.local"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip().strip('"').strip("'"))

from regime_platform.core.settings import settings  # noqa: E402
from regime_platform.sweep_orchestrator.barrier import barrier_null_from_prices  # noqa: E402
from regime_platform.sweep_orchestrator.bracket_reachability import (  # noqa: E402
    _fetch_cutover_at,
)
from regime_platform.sweep_orchestrator.posterior import pbm_sigma_atr  # noqa: E402
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS  # noqa: E402

logger = logging.getLogger("measure_entry_timing_gap")

REPORT_PATH = ROOT / "worklog" / "kairos" / "reports" / "20261014_entry_timing_gap.md"
PAGE = 1000
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_BOOTSTRAP = 200
DEFAULT_MIN_CELL_N = 20
DEFAULT_SEED = 13

NT_SIGNAL_SIM_COLUMNS = (
    "signal_id,symbol,ts,signal,entry_price,stop_loss,take_profit,atr,regime,"
    "outcome,exit_price,p_win_dir,p_clef_dir,p_clef_wait,bracket_vol_tier,qualified"
)
NT_PBM_SHADOW_COLUMNS = (
    "symbol,ts,signal,tp_atr,sl_atr,t_bars,sigma,p_w,p_l,p_e,p_w_null,gap,"
    "vol_tier,bracket_bucket,deadline_class,era,outcome"
)

OUTCOMES = ("tp_hit", "sl_hit", "expired")
HOUR_BUCKET_LABELS = tuple(f"{h:02d}-{h + 3:02d}" for h in range(0, 24, 4))

# The pre-registered search family (production): single-dimension conditions
# over these covariates + the four 2-way conjunction families below it.
SINGLE_DIMS = ("symbol", "side", "hour_bucket", "vol_tier", "regime_class", "alignment", "qualified")
PREREGISTERED_PAIRS = (
    ("side", "regime_class"),
    ("side", "hour_bucket"),
    ("side", "alignment"),
    ("symbol", "side"),
)
# Synthetic-verification only: the injected truth cell is a 3-way, so the
# verification family is extended with (symbol x side x hour bucket) to make
# it enumerable. Production runs never enumerate 3-ways.
SYNTH_TRIPLE = ("symbol", "side", "hour_bucket")

SYNTH_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "US30USD", "BTCUSD", "XAUEUR")
SYNTH_REGIME_LABELS = ("bull_trend", "bear_trend", "range_neutral")
SYNTH_VOL_TIERS = ("low", "mid", "high")
SYNTH_INJECTED: tuple[tuple[str, str], ...] = (
    ("symbol", "XAUUSD"),
    ("side", "sell"),
    ("hour_bucket", "08-11"),
)
SYNTH_INJECTED_GAP = 0.18
SYNTH_N_ROWS = 6000
SYNTH_MAX_FALSE_PROMOTES = 3


# ─── Small pure helpers ─────────────────────────────────────────────────────


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


# ─── Covariates (row-native only) ───────────────────────────────────────────


def hour_bucket_of(ts: datetime) -> str:
    """UTC 4-hour block label: 00-03, 04-07, 08-11, 12-15, 16-19, 20-23."""
    return HOUR_BUCKET_LABELS[ts.hour // 4]


def regime_class_of(label: Any) -> str:
    """bull/bear/neutral from the sim regime label — mirrors the live direction
    rule (orchestrator.py:4073: 'short' iff 'bear' in the label)."""
    text = str(label or "").lower()
    if "bull" in text:
        return "bull"
    if "bear" in text:
        return "bear"
    return "neutral"


def alignment_of(side: str, regime_class: str) -> str:
    """Side vs regime: with-trend, counter-trend, or neutral-regime."""
    if regime_class == "neutral":
        return "neutral-regime"
    if (side == "buy" and regime_class == "bull") or (side == "sell" and regime_class == "bear"):
        return "with-trend"
    return "counter-trend"


def attach_covariates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the ``_cov`` covariate dict to every unified row."""
    out: list[dict[str, Any]] = []
    for r in rows:
        side = str(r.get("signal") or "").lower()
        rc = regime_class_of(r.get("regime"))
        row = dict(r)
        row["_cov"] = {
            "symbol": str(r.get("symbol") or "?"),
            "side": side,
            "hour_bucket": hour_bucket_of(r["_ts"]),
            "vol_tier": str(r.get("vol_tier") or "unknown"),
            "regime_class": rc,
            "alignment": alignment_of(side, rc),
            "qualified": "true" if r.get("qualified") else "false",
        }
        out.append(row)
    return out


# ─── p_w_null resolution (ledger stamp preferred, analytic null fallback) ───


def null_p_win(sim: dict[str, Any], sigma: float) -> float | None:
    """The analytic null's p_win for one sim row's SHIPPED bracket geometry —
    the same construction the Phase E drift harness uses."""
    entry = _to_float(sim.get("entry_price"))
    tp = _to_float(sim.get("take_profit"))
    sl = _to_float(sim.get("stop_loss"))
    atr = _to_float(sim.get("atr"))
    if entry is None or tp is None or sl is None or atr is None or atr <= 0:
        return None
    p = barrier_null_from_prices(
        entry, tp, sl,
        atr=atr,
        t_bars=EXPIRY_BARS,
        sigma=sigma,
        direction=str(sim.get("signal") or "buy"),
    ).p_win
    return p if math.isfinite(p) else None


def _resolve_p_null(
    led: dict[str, Any] | None, sim: dict[str, Any] | None, sigma: float
) -> tuple[float | None, str | None]:
    """Ledger p_w_null if usable; else the analytic null from sim geometry."""
    if led is not None:
        p = _to_float(led.get("p_w_null"))
        if p is not None and 0.0 <= p <= 1.0:
            return p, "ledger"
    if sim is not None:
        p = null_p_win(sim, sigma)
        if p is not None:
            return p, "recomputed"
    return None, None


def _resolve_vol_tier(led: dict[str, Any] | None, sim: dict[str, Any] | None) -> str:
    if led is not None and led.get("vol_tier"):
        return str(led["vol_tier"])
    if sim is not None and sim.get("bracket_vol_tier"):
        return str(sim["bracket_vol_tier"])
    return "unknown"


def build_rows(
    sim_rows: list[dict[str, Any]], ledger_rows: list[dict[str, Any]], sigma: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Offline join of the two tables on (symbol, ts, signal) -> unified rows.

    Population = resolved sim rows (primary) UNION resolved ledger-only rows
    (outcome backfilled on the ledger). Unresolved rows are excluded and
    counted; rows with no usable p_w_null either way are dropped and counted.
    """
    counts: dict[str, Any] = {
        "fetched_sim": len(sim_rows), "fetched_ledger": len(ledger_rows),
        "bad_ts": 0, "ledger_dups": 0, "joined": 0, "sim_only": 0, "ledger_only": 0,
        "unresolved": 0, "dropped_no_null": 0, "basis_ledger": 0, "basis_recomputed": 0,
        "pre_cutover_dropped": 0, "era_clean": 0,
    }

    def bump(key: str) -> None:
        counts[key] = int(counts[key]) + 1

    ledger_by_key: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    for r in ledger_rows:
        ts = _parse_ts(r.get("ts"))
        if ts is None:
            bump("bad_ts")
            continue
        key = (str(r.get("symbol") or "?"), str(r.get("signal") or "").lower(), ts)
        if key in ledger_by_key:
            bump("ledger_dups")
        ledger_by_key[key] = r

    rows: list[dict[str, Any]] = []
    used_keys: set[tuple[str, str, datetime]] = set()

    def _emit(symbol: str, side: str, ts: datetime, outcome: str, p_null: float, basis: str,
              led: dict[str, Any] | None, sim: dict[str, Any] | None) -> None:
        rows.append({
            "symbol": symbol,
            "signal": side,
            "_ts": ts,
            "outcome": outcome,
            "p_w_null": p_null,
            "basis": basis,
            "vol_tier": _resolve_vol_tier(led, sim),
            "regime": sim.get("regime") if sim is not None else None,
            "qualified": bool(sim.get("qualified")) if sim is not None else False,
        })
        bump(f"basis_{basis}")

    for r in sim_rows:
        ts = _parse_ts(r.get("ts"))
        if ts is None:
            bump("bad_ts")
            continue
        symbol = str(r.get("symbol") or "?")
        side = str(r.get("signal") or "").lower()
        key = (symbol, side, ts)
        led = ledger_by_key.get(key)
        if led is not None:
            bump("joined")
            used_keys.add(key)
        else:
            bump("sim_only")
        outcome = r.get("outcome")
        if outcome not in OUTCOMES and led is not None:
            outcome = led.get("outcome")  # ledger backfill when the sim outcome is absent
        if outcome not in OUTCOMES:
            bump("unresolved")
            continue
        p_null, basis = _resolve_p_null(led, r, sigma)
        if p_null is None:
            bump("dropped_no_null")
            continue
        _emit(symbol, side, ts, outcome, p_null, basis, led, r)

    for key, led in ledger_by_key.items():
        if key in used_keys:
            continue
        outcome = led.get("outcome")
        if outcome not in OUTCOMES:
            bump("unresolved")
            continue
        p_null, basis = _resolve_p_null(led, None, sigma)  # no sim geometry to recompute from
        if p_null is None:
            bump("dropped_no_null")
            continue
        bump("ledger_only")
        _emit(key[0], key[1], key[2], outcome, p_null, basis, led, None)

    n_led = int(counts["basis_ledger"])
    n_rec = int(counts["basis_recomputed"])
    counts["basis"] = "ledger" if n_rec == 0 else ("recomputed" if n_led == 0 else "mixed")
    counts["resolved"] = len(rows)
    return rows, counts


def era_filter(
    rows: list[dict[str, Any]], cutover_dt: datetime | None
) -> tuple[list[dict[str, Any]], int]:
    """Era-clean: drop rows whose ts predates the resolver-basis cutover.

    Missing marker (``cutover_dt is None``) -> fail-open to all rows; the
    report states this explicitly.
    """
    if cutover_dt is None:
        return list(rows), 0
    clean = [r for r in rows if r["_ts"] >= cutover_dt]
    return clean, len(rows) - len(clean)


# ─── Predicate machinery (the pre-registered family) ────────────────────────

Predicate = tuple[tuple[str, str], ...]


def predicate_label(pred: Predicate) -> str:
    return " & ".join(f"{d}={v}" for d, v in pred)


def enumerate_predicates(rows: list[dict[str, Any]], include_triples: bool) -> list[Predicate]:
    """All single-dimension conditions + the pre-registered 2-way conjunctions
    over the OBSERVED value space (``include_triples`` = synthetic verification
    only). Empty combinations simply never clear discovery nomination."""
    vals: dict[str, set[str]] = {d: set() for d in SINGLE_DIMS}
    for r in rows:
        for d, v in r["_cov"].items():
            vals[d].add(v)
    preds: list[Predicate] = []
    for dim in SINGLE_DIMS:
        preds.extend(((dim, v),) for v in sorted(vals[dim]))
    for d1, d2 in PREREGISTERED_PAIRS:
        for v1 in sorted(vals[d1]):
            for v2 in sorted(vals[d2]):
                preds.append(((d1, v1), (d2, v2)))
    if include_triples:
        d1, d2, d3 = SYNTH_TRIPLE
        for v1 in sorted(vals[d1]):
            for v2 in sorted(vals[d2]):
                for v3 in sorted(vals[d3]):
                    preds.append(((d1, v1), (d2, v2), (d3, v3)))
    return preds


# ─── Statistics ─────────────────────────────────────────────────────────────


def bootstrap_mean_ci(
    values: np.ndarray, n_boot: int, seed: int
) -> tuple[float, float] | None:
    """Percentile bootstrap CI (2.5 / 97.5) for the mean of ONE predicate's
    confirmation-half gap rows. Fresh seeded RNG per predicate -> deterministic
    regardless of enumeration order."""
    n = int(values.size)
    if n == 0 or n_boot <= 0:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = values[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def verdict_for(
    n_c: int, c_gap: float | None, ci: tuple[float, float] | None, min_cell_n: int
) -> tuple[str, str]:
    """The pre-registered promotion bar, mechanically applied. No discretion."""
    if n_c == 0:
        return ("STAY-OFF", "no confirmation-half rows")
    if n_c < min_cell_n:
        return ("STAY-OFF", f"confirmation n {n_c} < {min_cell_n}")
    if c_gap is None or c_gap <= 0.0:
        return ("STAY-OFF", f"confirmation mean gap {_fmt(c_gap, '.4f', plus=True)} <= 0")
    if ci is None or ci[0] <= 0.0:
        lo = ci[0] if ci else float("nan")
        return ("STAY-OFF", f"CI lower bound {_fmt(lo, '.4f', plus=True)} <= 0")
    return ("PROMOTE", "confirmation mean gap > 0 with CI lower bound > 0")


def cell_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """n, realized TP rate, mean p_w_null, mean realized gap for a row set."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "tp_rate": None, "null_mean": None, "gap": None}
    tp = sum(1 for r in rows if r["outcome"] == "tp_hit")
    tp_rate = tp / n
    null_mean = sum(float(r["p_w_null"]) for r in rows) / n
    return {"n": n, "tp_rate": tp_rate, "null_mean": null_mean, "gap": tp_rate - null_mean}


def unconditional_cells(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The thermometer's home: per (symbol, side) realized gap, unconditional."""
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cells[(str(r["symbol"]), str(r["signal"]))].append(r)
    table = []
    for (sym, side), rs in sorted(cells.items()):
        st = cell_stats(rs)
        st["cell"] = f"{sym}/{side}"
        table.append(st)
    return table, cell_stats(rows)


def run_search(
    rows: list[dict[str, Any]],
    *,
    min_cell_n: int,
    n_boot: int,
    seed: int,
    include_triples: bool = False,
) -> dict[str, Any]:
    """The honest walk-forward search over the pre-registered family.

    Rows are split chronologically 50/50 by ts (global split, one split shared
    by every predicate). Discovery nominates; confirmation — and only
    confirmation — promotes.
    """
    ordered = sorted(rows, key=lambda r: (r["_ts"], r["symbol"], r["signal"]))
    split = len(ordered) // 2
    gap = np.asarray(
        [(1.0 if r["outcome"] == "tp_hit" else 0.0) - float(r["p_w_null"]) for r in ordered],
        dtype=np.float64,
    )
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, r in enumerate(ordered):
        for d, v in r["_cov"].items():
            groups[(d, v)].append(i)
    group_sets = {k: frozenset(v) for k, v in groups.items()}

    def pred_rows(pred: Predicate) -> list[int]:
        sets: list[frozenset[int]] = []
        for cond in pred:
            s = group_sets.get(cond)
            if s is None:
                return []
            sets.append(s)
        return sorted(sets[0].intersection(*sets[1:]))

    preds = enumerate_predicates(ordered, include_triples)
    discovery: dict[str, dict[str, Any]] = {}
    selected: list[Predicate] = []
    for pred in preds:
        d_idx = [i for i in pred_rows(pred) if i < split]
        d_n = len(d_idx)
        d_gap = float(gap[d_idx].mean()) if d_n else None
        is_sel = d_n >= min_cell_n and d_gap is not None and d_gap > 0.0
        discovery[predicate_label(pred)] = {"d_n": d_n, "d_gap": d_gap, "selected": is_sel}
        if is_sel:
            selected.append(pred)

    results: list[dict[str, Any]] = []
    for pred in selected:
        c_idx = [i for i in pred_rows(pred) if i >= split]
        d = discovery[predicate_label(pred)]
        n_c = len(c_idx)
        c_gap = float(gap[c_idx].mean()) if n_c else None
        ci = bootstrap_mean_ci(gap[c_idx], n_boot, seed) if n_c else None
        verdict, reason = verdict_for(n_c, c_gap, ci, min_cell_n)
        results.append({
            "label": predicate_label(pred),
            "d_n": d["d_n"],
            "d_gap": d["d_gap"],
            "c_n": n_c,
            "c_gap": c_gap,
            "ci": ci,
            "verdict": verdict,
            "reason": reason,
        })
    results.sort(
        key=lambda r: (r["verdict"] != "PROMOTE", -(r["c_gap"] if r["c_gap"] is not None else float("-inf")))
    )
    return {
        "n_rows": len(ordered),
        "n_discovery": split,
        "n_confirmation": len(ordered) - split,
        "n_candidates": len(preds),
        "n_selected": len(selected),
        "selected_labels": {predicate_label(p) for p in selected},
        "discovery": discovery,
        "results": results,
        "promoted": [r["label"] for r in results if r["verdict"] == "PROMOTE"],
    }


# ─── Report rendering ───────────────────────────────────────────────────────


def unconditional_table_lines(table: list[dict[str, Any]], pooled: dict[str, Any]) -> list[str]:
    L = ["| cell | n | realized TP rate | mean p_w_null | mean gap |", "|---|---:|---:|---:|---:|"]
    for c in table:
        L.append(
            f"| {c['cell']} | {c['n']} | {_fmt(c['tp_rate'], '.3f')} | "
            f"{_fmt(c['null_mean'], '.3f')} | {_fmt(c['gap'], '.4f', plus=True)} |"
        )
    if pooled and pooled["n"]:
        L.append(
            f"| *POOLED* | {pooled['n']} | {_fmt(pooled['tp_rate'], '.3f')} | "
            f"{_fmt(pooled['null_mean'], '.3f')} | {_fmt(pooled['gap'], '.4f', plus=True)} |"
        )
    return L


def search_table_lines(results: list[dict[str, Any]]) -> list[str]:
    L = [
        "| predicate | disc n | disc gap | conf n | conf gap | conf 95% CI | verdict |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for r in results:
        ci = (
            f"[{_fmt(r['ci'][0], '.4f', plus=True)}, {_fmt(r['ci'][1], '.4f', plus=True)}]"
            if r["ci"]
            else "—"
        )
        L.append(
            f"| `{r['label']}` | {r['d_n']} | {_fmt(r['d_gap'], '.4f', plus=True)} | "
            f"{r['c_n']} | {_fmt(r['c_gap'], '.4f', plus=True)} | {ci} | **{r['verdict']}** |"
        )
    if not results:
        L.append("| — | 0 | — | 0 | — | — | STAY-OFF |")
    return L


def render_report(ctx: dict[str, Any], now: datetime) -> str:
    """The markdown deliverable: thermometer first, then the honest search."""
    c = ctx["counts"]
    s = ctx["search"]
    L: list[str] = []
    L.append("# Entry-timing selection gap — conditioning search (measured or nothing)")
    L.append("")
    L.append(
        f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {ctx['lookback_days']} days "
        f"(since {ctx['since']:%Y-%m-%d}) · bootstrap {ctx['n_boot']} resamples "
        f"(seed {ctx['seed']}) · min cell n {ctx['min_cell_n']}"
    )
    L.append("")
    L.append("## The quantity")
    L.append("")
    L.append(
        "Per-signal REALIZED selection gap: `g_i = 1[outcome == 'tp_hit'] - p_w_null` — how much "
        "the realized win rate of the entries the clock actually took exceeds the random-walk "
        "null's first-touch TP probability for the SAME bracket. Negative gap = adverse entry "
        "timing. This is the empirical cell-level **mu** in win-probability space: edge can only "
        "exist as conditional mu != 0, and this harness searches for the conditioning under "
        "which mu > 0 — the Phase E promotion bar applied at the entry layer."
    )
    L.append("")
    L.append(
        "*The ledger's own `gap` column is posterior − null (a calibration object, NOT realized); "
        "g_i here is recomputed from outcome and p_w_null.*"
    )
    L.append("")
    L.append("## Population & era discipline")
    L.append("")
    if ctx["synthetic"]:
        L.append(
            f"- SYNTHETIC MODE: {c['era_clean']} in-memory rows over "
            f"{ctx['lookback_days']} days — no network, no cutover marker, no era filter "
            "(fail-open by construction)."
        )
        L.append(
            f"- world: {len(SYNTH_SYMBOLS)} symbols × 2 sides × 6 UTC hour buckets, "
            "p_w_null ~ U(0.25, 0.45), regime labels mixed bull/bear/neutral, outcomes "
            "Bernoulli at p_tp = p_w_null everywhere EXCEPT the injected cell "
            f"`{ctx.get('injected_label', predicate_label(SYNTH_INJECTED))}` where "
            f"p_tp = p_w_null + {SYNTH_INJECTED_GAP:g}."
        )
    elif ctx["fail_open"]:
        L.append(
            "> ⚠️ **FAIL-OPEN: the `nt_calibration_cutover` marker was missing or unreadable — "
            "NO era filter was applied, ALL fetched rows are included (pre-cutover outcomes "
            "may be present).** Treat every cell below as discovery, never promotion evidence."
        )
    else:
        L.append(
            f"- cutover marker `nt_calibration_cutover`: **{ctx['cutover_iso']}** — the same "
            "marker reachability, the bias loop and atr_reopt exclude on."
        )
    if not ctx["synthetic"]:
        L.append(f"- rows dropped as pre-cutover: **{c['pre_cutover_dropped']}**")
        L.append(
            f"- fetched: `nt_signal_sim` {c['fetched_sim']} resolved rows · `nt_pbm_shadow` "
            f"{c['fetched_ledger']} rows ({c['bad_ts']} unreadable ts, {c['ledger_dups']} "
            "duplicate ledger keys)"
        )
        L.append(
            f"- offline join on (symbol, ts, signal): **{c['joined']} matched** · "
            f"{c['sim_only']} sim-only (null recomputed from sim geometry) · "
            f"{c['ledger_only']} ledger-only resolved (backfilled outcome)"
        )
        L.append(
            f"- excluded: **{c['unresolved']}** unresolved (outcome NULL in both) · "
            f"**{c['dropped_no_null']}** with no usable p_w_null (ledger null missing AND sim "
            "geometry incomplete)"
        )
        L.append(
            f"- p_w_null basis: **{c['basis']}** (ledger {c['basis_ledger']} / recomputed "
            f"{c['basis_recomputed']}) · era-clean resolved rows: **{c['era_clean']}**"
        )
    L.append("")
    L.append("## Section 1 — the thermometer: unconditional realized gap per symbol × side")
    L.append("")
    pooled = ctx["pooled"]
    if pooled and pooled["n"]:
        L.append(
            f"- **POOLED gap: {_fmt(pooled['gap'], '.4f', plus=True)}** — realized TP rate "
            f"{_fmt(pooled['tp_rate'], '.4f')} vs mean p_w_null {_fmt(pooled['null_mean'], '.4f')} "
            f"over n={pooled['n']} resolved rows."
        )
    else:
        L.append("- POOLED gap: — (no resolved rows with a usable null)")
    L.append(
        "- in-sample by construction: a thermometer of what the entry layer delivered vs the "
        "driftless price of the same brackets, not a forecast."
    )
    L.append("")
    L.extend(unconditional_table_lines(ctx["table"], pooled))
    L.append("")
    L.append("## The search — pre-registered family, honest walk-forward")
    L.append("")
    L.append(
        "- family: ALL single-dimension conditions over symbol / side / UTC 4h bucket / vol "
        "tier / regime class / side-vs-regime alignment / qualified, PLUS the pre-registered "
        "2-way conjunctions (side×regime class, side×hour bucket, side×alignment, symbol×side). "
        "**No 3-way.** The family is small and fixed before looking — the multiple-testing "
        "posture is honest because the bar is confirmation on held-out data, not significance "
        "on the discovery half."
    )
    if ctx["synthetic"]:
        L.append(
            "- SYNTHETIC verification extends the family with the 3-way (symbol × side × hour "
            "bucket) conjunctions so the injected truth cell is enumerable; production runs "
            "never enumerate 3-ways."
        )
    L.append(
        f"- walk-forward: rows split chronologically 50/50 by ts. DISCOVERY half (earlier, "
        f"n={s['n_discovery']}) nominates: mean gap > 0 AND n ≥ {ctx['min_cell_n']}. "
        f"CONFIRMATION half (later, n={s['n_confirmation']}) is the only thing that can "
        f"promote: mean gap > 0 AND bootstrap 95% CI lower bound > 0 AND n ≥ {ctx['min_cell_n']}."
    )
    L.append(
        f"- candidates enumerated: **{s['n_candidates']}** · discovery-nominated: "
        f"**{s['n_selected']}** · promoted: **{len(s['promoted'])}**"
    )
    L.append("")
    L.extend(search_table_lines(s["results"]))
    if not s["results"]:
        L.append("")
        L.append("(no predicate cleared discovery nomination on this window)")
    L.append("")
    L.append("## Verdict (mechanically applied bar — no discretion)")
    L.append("")
    if s["promoted"]:
        for r in s["results"]:
            if r["verdict"] != "PROMOTE":
                continue
            L.append(
                f"- **PROMOTE `{r['label']}`** — confirmation mean gap "
                f"{_fmt(r['c_gap'], '.4f', plus=True)} (n={r['c_n']}), CI "
                f"[{_fmt(r['ci'][0], '.4f', plus=True)}, {_fmt(r['ci'][1], '.4f', plus=True)}]."
            )
        L.append("")
        L.append(
            "> Before any live wiring (a shadow qualifier stamp, then a trigger — a separate "
            "change, never this harness), re-run on a fresh window and get operator sign-off."
        )
    else:
        L.append("> **STAY-OFF across the board — the stable resting state, by design.**")
    L.append("")
    L.append("## Honest expected outcome")
    L.append("")
    L.append(
        "Given the measured record (realized gap ~−30pt at tight buckets; directional AUC "
        "0.499 on the resolved book), the honest expectation is that MOST predicates land "
        "STAY-OFF — and that is the system working, not failing. No predicate here manufactures "
        "edge: a fence cannot create timing skill. The clock picked the bar, the regime label "
        "picked direction, and the EV engine priced whatever it was given; if no conditioning "
        "beats the null on held-out data, the entry layer has no timing information to harvest "
        "and the honest price of every bracket remains the null. A PROMOTE verdict is the rare "
        "event that earns the next step."
    )
    L.append("")
    L.append("## Notes / honesty")
    L.append("")
    L.append(
        "- The bootstrap is ROW-LEVEL on each predicate's confirmation-half rows: overlapping "
        "same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar "
        "demands the lower bound clear 0 regardless."
    )
    L.append(
        "- Nothing is fit anywhere — predicates are fixed conditions, so walk-forward means "
        "evaluation, not training; the discovery half nominates, the later half decides."
    )
    L.append(
        "- regime class parses nt_signal_sim.regime ('bull'/'bear' substring, else 'neutral') "
        "mirroring the live direction rule; alignment is side-vs-regime (with-trend / "
        "counter-trend / neutral-regime). Ledger-only rows have no sim regime and fall to "
        "'neutral'."
    )
    L.append(
        "- hour buckets are UTC 4h blocks (00-03 … 20-23) from ts; vol tier prefers the "
        "ledger's stamp and falls back to the sim row's bracket_vol_tier (then 'unknown')."
    )
    L.append(
        "- the p_w_null fallback recomputes the analytic null from the sim row's own shipped "
        f"geometry (T = {EXPIRY_BARS} bars, sigma = {ctx['sigma']:g} ATR/bar via "
        "posterior.pbm_sigma_atr()) exactly as the Phase E drift harness does."
    )
    if ctx["fail_open"] and not ctx["synthetic"]:
        L.append("- ERA FILTER UNAVAILABLE this run (marker missing) — see the warning above.")
    L.append("")
    return "\n".join(L) + "\n"


# ─── Supabase fetch (the only I/O) ──────────────────────────────────────────


async def fetch_table(
    table: str,
    columns: str,
    since: datetime,
    extra_filters: dict[str, str],
    limit: int | None,
) -> list[dict[str, Any]]:
    """Rows of one Supabase table since `since`, 1000/page offset pagination."""
    rows: list[dict[str, Any]] = []
    off = 0
    base: dict[str, str] = {
        "select": columns,
        "ts": f"gte.{since.isoformat().replace('+00:00', 'Z')}",
        "order": "ts.asc",
        "limit": str(PAGE),
    }
    base.update(extra_filters)
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = dict(base)
            params["offset"] = str(off)
            resp = await client.get(_url(table), headers=_headers(), params=params)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"unexpected {table} response shape: {type(batch).__name__}")
            rows.extend(batch)
            if len(batch) < PAGE:
                break
            off += len(batch)
            if limit is not None and len(rows) >= limit:
                rows = rows[:limit]
                break
            if off > 200_000:  # belt-and-braces pagination guard
                logger.warning("pagination guard hit at offset %d — stopping", off)
                break
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
    return rows


# ─── Synthetic verification world (no network at all) ──────────────────────


def synthetic_rows(seed: int, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """~6000 in-memory rows: a NULL world everywhere except ONE injected cell.

    6 symbols × 2 sides × 6 UTC hour buckets (balanced cell schedule), ts
    uniform over the window, p_w_null ~ U(0.25, 0.45), regime labels mixed
    bull/bear/neutral. Null-world rows draw outcomes at p_tp = p_w_null
    exactly (E[gap] = 0 for every null predicate). The injected cell
    (SYNTH_INJECTED) draws at p_tp = clip(p_w_null + 0.18, 0, 1). Non-tp
    outcomes split sl_hit/expired 80/20; expired contributes 0 to the gap
    indicator — same formula as production. Draw order (ts, p_w_null,
    covariates, outcome uniforms) is part of the fixture: seeded, so the
    verification world is identical on every run.
    """
    rng = np.random.default_rng(seed)
    cells = [
        (sym, side, hour)
        for sym in sorted(SYNTH_SYMBOLS)
        for side in ("buy", "sell")
        for hour in HOUR_BUCKET_LABELS
    ]
    per_cell, extra = divmod(SYNTH_N_ROWS, len(cells))
    schedule: list[int] = []
    for i in range(len(cells)):
        schedule.extend([i] * (per_cell + (1 if i < extra else 0)))
    n = len(schedule)
    start = datetime(2026, 10, 14, tzinfo=timezone.utc) - timedelta(days=lookback_days)
    days = rng.integers(0, lookback_days, size=n)
    secs = rng.uniform(0.0, 4.0 * 3600.0, size=n)
    p_null = rng.uniform(0.25, 0.45, size=n)
    regime = rng.integers(0, len(SYNTH_REGIME_LABELS), size=n)
    tier = rng.integers(0, len(SYNTH_VOL_TIERS), size=n)
    qual = rng.random(n) < 0.6
    u = rng.random(n)
    injected_cell = (SYNTH_INJECTED[0][1], SYNTH_INJECTED[1][1], SYNTH_INJECTED[2][1])
    rows: list[dict[str, Any]] = []
    for i, ci in enumerate(schedule):
        sym, side, hour = cells[ci]
        hi = HOUR_BUCKET_LABELS.index(hour)
        ts = start + timedelta(days=int(days[i]), hours=hi * 4, seconds=float(secs[i]))
        p = float(p_null[i])
        p_tp = min(1.0, p + SYNTH_INJECTED_GAP) if (sym, side, hour) == injected_cell else p
        uu = float(u[i])
        if uu < p_tp:
            outcome = "tp_hit"
        elif uu < p_tp + 0.8 * (1.0 - p_tp):
            outcome = "sl_hit"
        else:
            outcome = "expired"
        rows.append({
            "symbol": sym,
            "signal": side,
            "_ts": ts,
            "outcome": outcome,
            "p_w_null": p,
            "basis": "synthetic",
            "vol_tier": SYNTH_VOL_TIERS[int(tier[i])],
            "regime": SYNTH_REGIME_LABELS[int(regime[i])],
            "qualified": bool(qual[i]),
        })
    return rows


def run_synthetic(args: argparse.Namespace) -> None:
    """Verification: the pipeline must find, confirm and promote the ONE true
    predicate, and tolerate at most SYNTH_MAX_FALSE_PROMOTES false promotes
    (a few at ~2.5% per predicate across ~100 candidates is honest statistics,
    not a bug; determinism comes from the seed)."""
    rows = synthetic_rows(args.seed, lookback_days=DEFAULT_LOOKBACK_DAYS)
    cov_rows = attach_covariates(rows)
    search = run_search(
        cov_rows, min_cell_n=args.min_cell_n, n_boot=args.bootstrap, seed=args.seed,
        include_triples=True,
    )
    injected_label = predicate_label(SYNTH_INJECTED)
    false = [r for r in search["results"] if r["verdict"] == "PROMOTE" and r["label"] != injected_label]

    print(
        f"SYNTHETIC MODE: {search['n_rows']} rows over {DEFAULT_LOOKBACK_DAYS} days · "
        f"discovery {search['n_discovery']} / confirmation {search['n_confirmation']} · "
        f"{search['n_candidates']} candidates · {search['n_selected']} discovery-nominated"
    )
    print("Verdict table (discovery-nominated predicates, evaluated on the confirmation half):")
    for line in search_table_lines(search["results"]):
        print(line)
    print(f"false promotes (promoted predicates other than the injected one): {len(false)}")

    inj_disc = search["discovery"].get(injected_label, {})
    inj_res = next((r for r in search["results"] if r["label"] == injected_label), None)
    problems: list[str] = []
    if not inj_disc.get("selected"):
        problems.append(
            f"injected predicate {injected_label!r} NOT discovered on the discovery half "
            f"(n={inj_disc.get('d_n')}, mean gap={inj_disc.get('d_gap')})"
        )
    if inj_res is None or inj_res["verdict"] != "PROMOTE":
        got = inj_res["verdict"] if inj_res else "not evaluated (not nominated)"
        problems.append(
            f"injected predicate {injected_label!r} not PROMOTED on the confirmation half "
            f"(verdict: {got})"
        )
    if len(false) > SYNTH_MAX_FALSE_PROMOTES:
        problems.append(
            f"false promotes {len(false)} > {SYNTH_MAX_FALSE_PROMOTES} "
            f"({', '.join(r['label'] for r in false)})"
        )
    if problems:
        for p in problems:
            print(f"SYNTHETIC MODE: FAIL — {p}")
        raise SystemExit("SYNTHETIC MODE: FAIL — see messages above")
    print(
        f"SYNTHETIC MODE: PASS — injected predicate {injected_label!r} discovered on the "
        f"discovery half and PROMOTED on the confirmation half; {len(false)} false promotes "
        f"(≤ {SYNTH_MAX_FALSE_PROMOTES})"
    )

    if args.out:  # only when --out is explicitly passed
        counts: dict[str, int | str] = {
            "fetched_sim": len(rows), "fetched_ledger": 0, "bad_ts": 0, "ledger_dups": 0,
            "joined": 0, "sim_only": len(rows), "ledger_only": 0, "unresolved": 0,
            "dropped_no_null": 0, "resolved": len(rows), "pre_cutover_dropped": 0,
            "era_clean": len(rows), "basis_ledger": 0, "basis_recomputed": 0, "basis": "synthetic",
        }
        table, pooled = unconditional_cells(cov_rows)
        ctx = {
            "synthetic": True, "since": datetime(2026, 8, 15, tzinfo=timezone.utc),
            "lookback_days": DEFAULT_LOOKBACK_DAYS, "min_cell_n": args.min_cell_n,
            "n_boot": args.bootstrap, "seed": args.seed, "sigma": 1.0,
            "counts": counts, "cutover_iso": None, "fail_open": True,
            "table": table, "pooled": pooled, "search": search,
            "injected_label": injected_label,
        }
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_report(ctx, datetime.now(timezone.utc)), encoding="utf-8")
        print(f"wrote {out_path}")


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Entry-timing edge search: walk-forward realized selection-gap "
        "(outcome - p_w_null) predicate search over the nt_signal_sim x nt_pbm_shadow "
        "offline join, era-clean, read-only.",
    )
    ap.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"fetch window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    ap.add_argument(
        "--min-cell-n", type=int, default=DEFAULT_MIN_CELL_N,
        help="min rows per predicate per half for nomination / promotion",
    )
    ap.add_argument(
        "--bootstrap", type=int, default=DEFAULT_BOOTSTRAP,
        help="bootstrap resamples for the confirmation-half gap CI",
    )
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap/data RNG seed")
    ap.add_argument(
        "--out", default=None,
        help=f"report path override (default: {REPORT_PATH.relative_to(ROOT)})",
    )
    ap.add_argument(
        "--synthetic", action="store_true",
        help="in-memory verification mode — NO network, no report file unless --out is passed",
    )
    ap.add_argument(
        "--limit-rows", type=int, default=None, help="optional cap on fetched rows per table"
    )
    return ap.parse_args(argv)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()

    if args.synthetic:
        run_synthetic(args)
        return

    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        raise SystemExit(
            "SUPABASE_URL/SUPABASE_KEY not configured — nothing to measure. "
            "(Offline verification: py_compile + `--synthetic` self-test need no network.)"
        )

    sigma = float(pbm_sigma_atr())
    since = (datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).replace(microsecond=0)
    cutover_iso = await _fetch_cutover_at()
    logger.info("cutover marker: %s", cutover_iso or "MISSING (fail-open)")

    sim_rows = await fetch_table(
        "nt_signal_sim", NT_SIGNAL_SIM_COLUMNS, since,
        {"outcome": "in.(tp_hit,sl_hit,expired)"}, args.limit_rows,
    )
    ledger_rows = await fetch_table(
        "nt_pbm_shadow", NT_PBM_SHADOW_COLUMNS, since, {}, args.limit_rows
    )
    logger.info(
        "fetched %d sim rows / %d ledger rows since %s", len(sim_rows), len(ledger_rows),
        since.isoformat(),
    )

    joined_rows, counts = build_rows(sim_rows, ledger_rows, sigma)
    cutover_dt = _parse_ts(cutover_iso) if cutover_iso else None
    era_rows, n_pre = era_filter(joined_rows, cutover_dt)
    counts["pre_cutover_dropped"] = n_pre
    counts["era_clean"] = len(era_rows)
    cov_rows = attach_covariates(era_rows)

    table, pooled = unconditional_cells(cov_rows)
    search = run_search(
        cov_rows, min_cell_n=args.min_cell_n, n_boot=args.bootstrap, seed=args.seed
    )
    ctx = {
        "synthetic": False, "since": since, "lookback_days": args.lookback_days,
        "min_cell_n": args.min_cell_n, "n_boot": args.bootstrap, "seed": args.seed,
        "sigma": sigma, "counts": counts, "cutover_iso": cutover_iso,
        "fail_open": cutover_dt is None, "table": table, "pooled": pooled, "search": search,
    }

    now = datetime.now(timezone.utc)
    text = render_report(ctx, now)
    out_path = Path(args.out) if args.out else REPORT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    logger.info(
        "population: %d era-clean resolved rows (joined %d / sim-only %d / ledger-only %d; "
        "basis %s; unresolved %d dropped-no-null %d pre-cutover %d)",
        counts["era_clean"], counts["joined"], counts["sim_only"], counts["ledger_only"],
        counts["basis"], counts["unresolved"], counts["dropped_no_null"], n_pre,
    )
    if pooled and pooled["n"]:
        logger.info(
            "pooled realized gap %s (TP rate %.3f vs null %.3f, n=%d)",
            _fmt(pooled["gap"], ".4f", plus=True), pooled["tp_rate"], pooled["null_mean"],
            pooled["n"],
        )
    logger.info(
        "search: %d candidates -> %d nominated -> %d promoted",
        search["n_candidates"], search["n_selected"], len(search["promoted"]),
    )
    for label in search["promoted"]:
        logger.info("PROMOTE %s", label)
    logger.info("wrote %s", out_path)


if __name__ == "__main__":
    asyncio.run(main())
