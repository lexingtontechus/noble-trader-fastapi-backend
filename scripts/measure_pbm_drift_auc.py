#!/usr/bin/env python3
"""measure_pbm_drift_auc.py — Phase E harness: drift-layer AUC, measured or nothing.

Decides whether ANY directional candidate may enter the Posterior Barrier Model
(PBM) as a drift term ``mu`` on the analytic barrier null
(`worklog/20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md` §5 Phase E;
proposal `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` §6).

Promotion bar (pre-registered, deliberately hard):

    walk-forward barrier-orientation AUC > 0.5   AND   bootstrap 95% CI
    lower bound > 0.5   AND   above the null's own walk-forward AUC,
    era-clean.  (The third clause is the pre-registration's "promote only
    above null": a candidate that cannot beat the geometry baseline
    carries no timing information beyond geometry.)

"Walk-forward" = each symbol's era-clean decided rows split chronologically
50/50, scored on the LATER half only. The scorers are fixed scores (nothing is
fit), so the honest out-of-sample read is simply evaluation on the later half.
The analytic null itself is scored as a candidate too — it encodes bracket
geometry; anything that cannot beat IT has no timing information.

Scorers (fixed, no fitting):
    p_win_dir    directional logit blend                     label tp_hit
    p_clef_dir   LLM direction-agreement stamp               label tp_hit
    null_p_win   barrier_null_from_prices(...).p_win         label tp_hit
                 (the analytic null — geometry only)
    p_clef_wait  LLM abstention stamp — the ONE measured-     label sl_hit
                 positive LLM output (AUC 0.61 vs sl_hit)     (its published
                                                             skill claim)

Also measures the selection gap per (symbol, side) cell — realized win rate
minus the null's p_win — the entry-timing thermometer (measured TP rates sit
BELOW the random-walk null: 13-15% vs ~37.5% first-touch at shipped bands).

Era discipline: rows whose ``ts`` predates the ``nt_calibration_cutover``
marker (read via ``bracket_reachability._fetch_cutover_at`` — the same marker
reachability, the bias loop and ``atr_reopt`` use) are EXCLUDED, exactly like
``scripts/replay_bands_era_clean.py``. Fail-open to all rows if the marker is
missing — and the report SAYS SO.

Offline and read-only: fetches resolved ``nt_signal_sim`` rows over Supabase
REST (``supabase_client._url/_headers``, 1000/page offset pagination), writes
one markdown report. No live wiring, no writes, no flags flipped.

Run:
    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/measure_pbm_drift_auc.py
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

logger = logging.getLogger("measure_pbm_drift_auc")

REPORT_PATH = ROOT / "worklog" / "kairos" / "reports" / "20261014_pbm_drift_auc.md"
PAGE = 1000
DEFAULT_LOOKBACK_DAYS = 60
DEFAULT_BOOTSTRAP = 200
DEFAULT_MIN_CELL_N = 20
DEFAULT_SEED = 13

NT_SIGNAL_SIM_COLUMNS = (
    "signal_id,symbol,ts,signal,entry_price,stop_loss,take_profit,atr,regime,"
    "outcome,exit_price,p_win_dir,p_clef_dir,p_clef_wait,bracket_vol_tier,qualified"
)

# (row key, display name, target outcome, description)
SCORERS: tuple[tuple[str, str, str, str], ...] = (
    ("p_win_dir", "p_win_dir", "tp_hit", "directional logit blend"),
    ("p_clef_dir", "p_clef_dir", "tp_hit", "LLM direction-agreement stamp"),
    ("_null", "null_p_win", "tp_hit", "analytic null p_win (geometry only)"),
    ("p_clef_wait", "p_clef_wait", "sl_hit", "LLM abstention (published skill claim)"),
)


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


# ─── AUC (Mann-Whitney rank statistic — hand-rolled, no sklearn) ────────────


def _average_ranks(x: np.ndarray) -> np.ndarray:
    """Ranks 1..n with ties assigned the average (mid-)rank."""
    n = x.size
    if n == 0:
        return np.empty(0, dtype=np.float64)
    order = np.argsort(x, kind="mergesort")
    sx = x[order]
    ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sx[j + 1] == sx[i]:
            j += 1
        ranks[order[i: j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranks


def auc_mw(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """Mann-Whitney AUC = P(score_pos > score_neg) + 0.5 * P(score_pos = score_neg).

    Ties handled via mid-ranks. None when either class is empty (AUC
    undefined) or the inputs are empty/mismatched. Callers pre-filter
    non-finite scores.
    """
    s = np.asarray(scores, dtype=np.float64)
    y = np.asarray(labels, dtype=np.int64)
    if s.size == 0 or s.size != y.size:
        return None
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return None
    ranks = _average_ranks(s)
    u1 = float(ranks[y == 1].sum()) - n1 * (n1 + 1) / 2.0
    return u1 / float(n1 * n0)


def bootstrap_auc_ci(
    scores: np.ndarray,
    labels: np.ndarray,
    n_boot: int,
    seed: int,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI (2.5 / 97.5) on the pooled AUC — row-level.

    Simple by design (per Phase E spec); resamples that land with one class
    empty are skipped. Seeded → deterministic across runs.
    """
    n = scores.size
    if n == 0 or n_boot <= 0:
        return None
    rng = np.random.default_rng(seed)
    vals: list[float] = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        a = auc_mw(scores[idx], labels[idx])
        if a is not None:
            vals.append(a)
    if len(vals) < max(10, n_boot // 2):
        return None
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return (float(lo), float(hi))


# ─── Row preparation (pure — testable offline) ──────────────────────────────


def null_p_win(row: dict[str, Any], sigma: float) -> float | None:
    """The analytic null's p_win for one row's SHIPPED bracket geometry.

    ``barrier_null_from_prices`` with the row's own entry/TP/SL/ATR and
    direction, T = EXPIRY_BARS (48), sigma per the harness setting (1.0 =
    one ATR of per-bar log standard deviation, the proposal's construction).
    """
    entry = _to_float(row.get("entry_price"))
    tp = _to_float(row.get("take_profit"))
    sl = _to_float(row.get("stop_loss"))
    atr = _to_float(row.get("atr"))
    if entry is None or tp is None or sl is None or atr is None or atr <= 0:
        return None
    p = barrier_null_from_prices(
        entry, tp, sl,
        atr=atr,
        t_bars=EXPIRY_BARS,
        sigma=sigma,
        direction=str(row.get("signal") or "buy"),
    ).p_win
    return p if math.isfinite(p) else None


def prepare_rows(raw: list[dict[str, Any]], sigma: float) -> tuple[list[dict[str, Any]], int]:
    """Normalize fetched rows into prepared dicts with all scorer fields.

    Returns (prepared, n_unparsable_ts). ``_ts`` is an aware UTC datetime;
    scorer fields are float-or-None so coverage is countable per scorer.
    """
    prep: list[dict[str, Any]] = []
    bad_ts = 0
    for r in raw:
        ts = _parse_ts(r.get("ts"))
        if ts is None:
            bad_ts += 1
            continue
        prep.append({
            "symbol": str(r.get("symbol") or "?"),
            "signal": str(r.get("signal") or "").lower(),
            "outcome": r.get("outcome"),
            "qualified": bool(r.get("qualified")),
            "bracket_vol_tier": r.get("bracket_vol_tier"),
            "_ts": ts,
            "p_win_dir": _to_float(r.get("p_win_dir")),
            "p_clef_dir": _to_float(r.get("p_clef_dir")),
            "p_clef_wait": _to_float(r.get("p_clef_wait")),
            "_null": null_p_win(r, sigma),
        })
    return prep, bad_ts


def era_filter(
    prep: list[dict[str, Any]], cutover_dt: datetime | None
) -> tuple[list[dict[str, Any]], int]:
    """Era-clean: drop rows whose ts predates the resolver-basis cutover.

    Missing marker (``cutover_dt is None``) → fail-open to all rows; the
    report states this explicitly.
    """
    if cutover_dt is None:
        return list(prep), 0
    clean = [r for r in prep if r["_ts"] >= cutover_dt]
    return clean, len(prep) - len(clean)


def walk_forward_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per symbol, the chronologically LATER half of its rows (50/50 split).

    Fixed scorers → nothing is fit → the honest out-of-sample read is simply
    "evaluate on the later half". ONE split, shared by every scorer, so the
    cross-scorer comparison stays apples-to-apples.
    """
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_sym[r["symbol"]].append(r)
    out: list[dict[str, Any]] = []
    for sym in sorted(by_sym):
        rs = sorted(by_sym[sym], key=lambda r: r["_ts"])
        out.extend(rs[len(rs) // 2:])
    return out


# ─── Scorer evaluation ──────────────────────────────────────────────────────


def scorer_stats(
    decided: list[dict[str, Any]],
    wf_rows: list[dict[str, Any]],
    key: str,
    target: str,
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    """AUC (all rows / walk-forward rows) + bootstrap CI for one fixed scorer."""
    usable_all = [r for r in decided if r.get(key) is not None]
    usable_wf = [r for r in wf_rows if r.get(key) is not None]
    labels_all = [1 if r["outcome"] == target else 0 for r in usable_all]
    labels_wf = [1 if r["outcome"] == target else 0 for r in usable_wf]
    auc_all = auc_mw([r[key] for r in usable_all], labels_all)
    auc_wf = auc_mw([r[key] for r in usable_wf], labels_wf)
    ci = None
    if usable_wf:
        s = np.asarray([r[key] for r in usable_wf], dtype=np.float64)
        y = np.asarray(labels_wf, dtype=np.int64)
        ci = bootstrap_auc_ci(s, y, n_boot, seed)
    return {
        "key": key,
        "target": target,
        "n": len(usable_all),
        "n_wf": len(usable_wf),
        "auc_all": auc_all,
        "auc_wf": auc_wf,
        "ci": ci,
    }


def verdict_for(stat: dict[str, Any], null_wf: float | None) -> tuple[str, str]:
    """The pre-registered promotion bar, mechanically applied. No discretion.

    Drift candidate: WF AUC > 0.5 AND CI lower bound > 0.5 AND above the
    null's own WF AUC (the pre-registration's "promote only above null" —
    phases doc §5 Phase E; a candidate that cannot beat the geometry
    baseline carries no timing information). The null scorer is exempt
    from the third clause (it IS the baseline) and never promotes: its
    row reads BASELINE with the mechanical detail. ``null_wf`` None (null
    itself unscoreable) leaves the third clause vacuous — reported as such
    via the null's own BASELINE/no-rows verdict.
    """
    is_null = stat["key"] == "_null"
    if stat["n_wf"] == 0 or stat["auc_wf"] is None:
        return ("BASELINE" if is_null else "STAY-OFF", "no scorable walk-forward rows")
    reasons: list[str] = []
    if stat["auc_wf"] <= 0.5:
        reasons.append(f"WF AUC {_fmt(stat['auc_wf'], '.3f')} <= 0.5")
    if stat["ci"] is None or stat["ci"][0] <= 0.5:
        lo = stat["ci"][0] if stat["ci"] else float("nan")
        reasons.append(f"CI lower bound {_fmt(lo, '.3f')} <= 0.5")
    if not is_null and null_wf is not None and stat["auc_wf"] <= null_wf:
        reasons.append(
            f"WF AUC {_fmt(stat['auc_wf'], '.3f')} <= null WF AUC "
            f"{_fmt(null_wf, '.3f')} (pre-registered: above the null)"
        )
    if reasons:
        return ("BASELINE" if is_null else "STAY-OFF", "; ".join(reasons))
    if is_null:
        return ("BASELINE", "cleared the 0.5 bar (geometry baseline, not a candidate)")
    return ("PROMOTE", "WF AUC > 0.5, CI lower bound > 0.5, above the null")


# ─── Selection gap (entry-timing thermometer) ───────────────────────────────


def selection_gap(
    decided: list[dict[str, Any]], min_cell_n: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Per (symbol, side) cell: realized win rate minus the null's mean p_win.

    In-sample by construction — a thermometer (what the entry layer actually
    delivered vs the driftless price of the same brackets), not a forecast.
    Returns (all cells, cells with n >= min_cell_n, pooled).
    """
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in decided:
        if r["_null"] is not None:
            cells[(r["symbol"], r["signal"])].append(r)

    def _cell(sym: str, side: str, rs: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(rs)
        wins = sum(1 for r in rs if r["outcome"] == "tp_hit")
        wr = wins / n if n else 0.0
        null_mean = sum(r["_null"] for r in rs) / n if n else 0.0
        return {
            "cell": f"{sym}/{side}",
            "n": n,
            "wr": wr,
            "null": null_mean,
            "gap": wr - null_mean,
        }

    all_cells = [_cell(sym, side, rs) for (sym, side), rs in sorted(cells.items())]
    big = [c for c in all_cells if c["n"] >= min_cell_n]
    pooled = None
    scored = [r for r in decided if r["_null"] is not None]
    if scored:
        pooled = _cell("*POOLED*", "*", scored)
    return all_cells, big, pooled


# ─── Measurement (pure pipeline over prepared rows) ─────────────────────────


def measure(
    prep: list[dict[str, Any]],
    *,
    sigma: float,
    n_boot: int,
    seed: int,
    min_cell_n: int,
    cutover_iso: str | None,
    fetched: int,
    bad_ts: int,
    since: datetime,
    symbols: list[str] | None,
    limit: int | None,
) -> dict[str, Any]:
    """Run the whole Phase E measurement on prepared rows → report context."""
    cutover_dt = _parse_ts(cutover_iso) if cutover_iso else None
    era_clean, n_pre = era_filter(prep, cutover_dt)
    decided = [r for r in era_clean if r["outcome"] in ("tp_hit", "sl_hit")]
    expired = [r for r in era_clean if r["outcome"] == "expired"]
    wf = walk_forward_rows(decided)

    stats: dict[str, dict[str, Any]] = {}
    for key, _name, target, _desc in SCORERS:
        stats[key] = scorer_stats(decided, wf, key, target, n_boot, seed)
    null_wf = stats["_null"]["auc_wf"]
    for stat in stats.values():
        if stat["auc_wf"] is not None and null_wf is not None:
            stat["delta_null"] = stat["auc_wf"] - null_wf
        else:
            stat["delta_null"] = None
        stat["verdict"], stat["reason"] = verdict_for(stat, null_wf)

    dec_by_sym: Counter[str] = Counter(r["symbol"] for r in decided)
    wf_by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in wf:
        wf_by_sym[r["symbol"]].append(r)
    per_symbol: list[dict[str, Any]] = []
    for sym in sorted(set(dec_by_sym) | set(wf_by_sym)):
        rows_sym = wf_by_sym.get(sym, [])
        row: dict[str, Any] = {
            "symbol": sym, "n_dec": dec_by_sym.get(sym, 0), "n_wf": len(rows_sym),
        }
        for key, name, target, _desc in SCORERS:
            usable = [r for r in rows_sym if r.get(key) is not None]
            row[name] = auc_mw(
                [r[key] for r in usable],
                [1 if r["outcome"] == target else 0 for r in usable],
            )
        per_symbol.append(row)

    all_cells, big_cells, pooled_gap = selection_gap(decided, min_cell_n)
    tiers = Counter(str(r.get("bracket_vol_tier") or "null") for r in decided)

    return {
        "since": since,
        "sigma": sigma,
        "symbols": symbols,
        "limit": limit,
        "cutover_iso": cutover_iso,
        "cutover_dt": cutover_dt,
        "fail_open": cutover_dt is None,
        "fetched": fetched,
        "bad_ts": bad_ts,
        "pre_cutover_dropped": n_pre,
        "era_clean": len(era_clean),
        "decided": len(decided),
        "tp": sum(1 for r in decided if r["outcome"] == "tp_hit"),
        "sl": sum(1 for r in decided if r["outcome"] == "sl_hit"),
        "expired": len(expired),
        "qualified": sum(1 for r in decided if r["qualified"]),
        "tier_mix": ", ".join(f"{k}: {v}" for k, v in sorted(tiers.items())) or "—",
        "stats": stats,
        "per_symbol": per_symbol,
        "all_cells": all_cells,
        "big_cells": big_cells,
        "top_cells": sorted(big_cells, key=lambda c: -c["gap"])[:10],
        "bottom_cells": sorted(big_cells, key=lambda c: c["gap"])[:10],
        "pooled_gap": pooled_gap,
        "min_cell_n": min_cell_n,
        "n_boot": n_boot,
        "seed": seed,
    }


# ─── Report rendering ───────────────────────────────────────────────────────


def render_report(ctx: dict[str, Any], now: datetime) -> str:
    """The markdown deliverable: bar statement, tables, verdicts, caveats."""
    s = ctx["stats"]
    L: list[str] = []
    L.append("# PBM drift-layer AUC — Phase E measurement (measured or nothing)")
    L.append("")
    L.append(
        f"Generated {now:%Y-%m-%d %H:%M} UTC · since {ctx['since']:%Y-%m-%d} · "
        f"sigma {ctx['sigma']:g} ATR/bar · T = {EXPIRY_BARS} bars · "
        f"bootstrap {ctx['n_boot']} resamples (seed {ctx['seed']})"
    )
    sym_note = f" · symbols {', '.join(ctx['symbols'])}" if ctx["symbols"] else ""
    lim_note = f" · row cap {ctx['limit']}" if ctx["limit"] else ""
    L.append(f"Population: resolved `nt_signal_sim` rows{sym_note}{lim_note}.")
    L.append("")

    L.append("## Promotion bar (pre-registered)")
    L.append("")
    L.append(
        "A directional candidate may enter the PBM as a drift term **μ** on the "
        "barrier model only if:"
    )
    L.append("")
    L.append(
        "1. its **walk-forward barrier-orientation AUC > 0.5** — each symbol's "
        "era-clean decided rows split chronologically 50/50, scored on the later "
        "half only (fixed scores, nothing fit), and"
    )
    L.append(
        "2. the **bootstrap 95% CI lower bound on that walk-forward AUC > 0.5** "
        f"({ctx['n_boot']} row-level resamples), and"
    )
    L.append(
        "3. its walk-forward AUC is **above the null's own walk-forward AUC** — "
        "the geometry baseline. A candidate that cannot beat the null carries no "
        "timing information beyond geometry."
    )
    L.append("")
    L.append(
        "The analytic null is scored on the same bar too: it encodes bracket "
        "GEOMETRY, and its row reads BASELINE (it is the reference, never a "
        "candidate). "
        "**Expected honest outcome: everything stays off** (directional blend "
        "AUC 0.499 on ~74k resolved signals; `p_clef_dir` 0.58 = chance)."
    )
    L.append("")

    L.append("## Era discipline")
    L.append("")
    if ctx["fail_open"]:
        L.append(
            "> ⚠️ **FAIL-OPEN: the `nt_calibration_cutover` marker was missing or "
            "unreadable — NO era filter was applied, ALL fetched rows are "
            "included (pre-cutover outcomes may be present).** Treat every AUC "
            "below as discovery, never promotion evidence."
        )
    else:
        L.append(
            f"- cutover marker `nt_calibration_cutover`: **{ctx['cutover_iso']}** — "
            "the same marker reachability, the bias loop and atr_reopt exclude on."
        )
    L.append(f"- rows dropped as pre-cutover: **{ctx['pre_cutover_dropped']}**")
    L.append(f"- era-clean rows: **{ctx['era_clean']}** of {ctx['fetched']} fetched "
             f"({ctx['bad_ts']} unreadable ts skipped)")
    L.append("")

    L.append("## Population")
    L.append("")
    L.append(
        f"- era-clean decided (tp_hit/sl_hit): **{ctx['decided']}** "
        f"(tp {ctx['tp']} / sl {ctx['sl']} · qualified {ctx['qualified']} / "
        f"not {ctx['decided'] - ctx['qualified']})"
    )
    L.append(
        f"- expired (resolved but orientation-undefined — excluded from AUC, "
        f"counted here): **{ctx['expired']}**"
    )
    L.append(f"- vol-tier mix (decided): {ctx['tier_mix']}")
    L.append("")

    L.append("## Per-scorer AUC (barrier orientation)")
    L.append("")
    L.append(
        "| scorer | target | n | AUC all-era | AUC walk-fwd | WF n | 95% CI (WF) | "
        "Δ vs null (WF) | verdict |"
    )
    L.append("|---|---|---:|---:|---:|---:|---|---:|---|")
    for key, name, target, desc in SCORERS:
        st = s[key]
        ci = (
            f"[{_fmt(st['ci'][0], '.3f')}, {_fmt(st['ci'][1], '.3f')}]"
            if st["ci"] else "—"
        )
        L.append(
            f"| **{name}** ({desc}) | {target} | {st['n']} | {_fmt(st['auc_all'], '.3f')} | "
            f"**{_fmt(st['auc_wf'], '.3f')}** | {st['n_wf']} | {ci} | "
            f"{_fmt(st['delta_null'], '.3f', plus=True)} | **{st['verdict']}** |"
        )
    L.append("")
    L.append(
        "*`null_p_win` is the geometry baseline, not a drift candidate — its AUC "
        "measures how much bracket geometry alone explains. `p_clef_wait` targets "
        "the inverted label (sl_hit): higher wait ≈ higher P(adverse) — a "
        "suppressor claim, judged against the same bar.*"
    )
    L.append("")

    L.append("## Per-symbol walk-forward AUC")
    L.append("")
    L.append(
        "| symbol | n decided | n WF | p_win_dir | p_clef_dir | null_p_win | p_clef_wait |"
    )
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for row in ctx["per_symbol"]:
        cells = " | ".join(_fmt(row[name], ".3f") for _k, name, _t, _d in SCORERS)
        L.append(f"| {row['symbol']} | {row['n_dec']} | {row['n_wf']} | {cells} |")
    L.append(
        "*— = AUC undefined for that scorer (coverage gap, or the symbol's "
        "walk-forward half is single-class). Rows with n WF < 30 are noise-grade: "
        "read extremes there as artifacts, not evidence.*"
    )
    L.append("")

    L.append("## Selection gap — entry-timing thermometer")
    L.append("")
    pooled = ctx["pooled_gap"]
    if pooled:
        L.append(
            f"- **POOLED gap: {_fmt(pooled['gap'], '.4f', plus=True)}** — realized "
            f"win rate {_fmt(pooled['wr'], '.4f')} vs null p_win "
            f"{_fmt(pooled['null'], '.4f')} over n={pooled['n']} decided rows. "
            "Negative = entries resolve worse than the driftless price of the "
            "same brackets (adverse timing)."
        )
    else:
        L.append("- POOLED gap: — (no decided rows with usable geometry)")
    L.append(f"- cells require n ≥ {ctx['min_cell_n']} decided rows to be listed.")
    L.append("")
    L.append("### Best 10 cells by gap (closest to / above the null)")
    L.append("")
    L.append("| cell | n | realized WR | null p_win | gap |")
    L.append("|---|---:|---:|---:|---:|")
    for c in ctx["top_cells"]:
        L.append(
            f"| {c['cell']} | {c['n']} | {_fmt(c['wr'], '.3f')} | "
            f"{_fmt(c['null'], '.3f')} | {_fmt(c['gap'], '.4f', plus=True)} |"
        )
    if not ctx["top_cells"]:
        L.append("| — | 0 | — | — | — |")
    L.append("")
    L.append("### Worst 10 cells by gap (most adverse entry timing)")
    L.append("")
    L.append("| cell | n | realized WR | null p_win | gap |")
    L.append("|---|---:|---:|---:|---:|")
    for c in ctx["bottom_cells"]:
        L.append(
            f"| {c['cell']} | {c['n']} | {_fmt(c['wr'], '.3f')} | "
            f"{_fmt(c['null'], '.3f')} | {_fmt(c['gap'], '.4f', plus=True)} |"
        )
    if not ctx["bottom_cells"]:
        L.append("| — | 0 | — | — | — |")
    L.append("")

    L.append("## Verdict (mechanically applied bar — no discretion)")
    L.append("")
    promoted = []
    for key, name, _target, _desc in SCORERS:
        st = s[key]
        L.append(f"- **{name}**: **{st['verdict']}** — {st['reason']}.")
        if st["verdict"] == "PROMOTE" and key != "_null":
            promoted.append(name)
    L.append("")
    if promoted:
        L.append(
            f"> **PROMOTE: {', '.join(promoted)}** cleared the bar. Before any "
            "live wiring, re-run on a fresh window and get operator sign-off "
            "(Phase E wiring is a separate change — this harness never flips a flag)."
        )
    else:
        L.append(
            "> **STAY-OFF across the board. μ stays pinned to 0 — the stable "
            "resting state, by design, not a failure.** No directional candidate "
            "is admitted to the PBM as drift on this evidence."
        )
    L.append("")

    L.append("## Notes / honesty")
    L.append("")
    L.append(
        "- Scorers are FIXED (nothing is fit), so walk-forward = evaluation on "
        "each symbol's later half; there is no train/test leakage to guard."
    )
    L.append(
        "- AUC is the hand-rolled Mann-Whitney rank statistic (mid-ranks for "
        "ties); no sklearn dependency."
    )
    L.append(
        "- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals "
        "correlate rows, so CIs are optimistic (n inflated). The bar demands the "
        "CI lower bound clear 0.5 regardless — and no episode dedupe is applied "
        "here, matching the platform's own 74k-row directional measurement."
    )
    if ctx["fail_open"]:
        L.append(
            "- ERA FILTER UNAVAILABLE this run (marker missing) — see the "
            "warning above; numbers are discovery-grade only."
        )
    L.append(
        f"- sigma = {ctx['sigma']:g} ATR/bar (default from "
        "`posterior.pbm_sigma_atr()`; 1.0 = one ATR per bar of log standard "
        "deviation). The null is a model (no jumps/gaps, constant σ) — the "
        "empirical posterior layer exists to absorb its systematic deviation."
    )
    L.append(
        "- The selection gap is in-sample by construction: a thermometer of what "
        "the entry layer delivered vs the driftless price of the same brackets, "
        "not a forecast."
    )
    L.append("")
    return "\n".join(L) + "\n"


# ─── Supabase fetch (the only I/O) ──────────────────────────────────────────


async def fetch_rows(
    since: datetime, symbols: list[str] | None, limit: int | None
) -> list[dict[str, Any]]:
    """Resolved nt_signal_sim rows since `since`, paginated 1000/page."""
    rows: list[dict[str, Any]] = []
    off = 0
    base: dict[str, str] = {
        "select": NT_SIGNAL_SIM_COLUMNS,
        "outcome": "in.(tp_hit,sl_hit,expired)",
        "ts": f"gte.{since.isoformat().replace('+00:00', 'Z')}",
        "order": "ts.asc",
        "limit": str(PAGE),
    }
    if symbols:
        base["symbol"] = f"in.({','.join(symbols)})"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = dict(base)
            params["offset"] = str(off)
            resp = await client.get(
                _url("nt_signal_sim"), headers=_headers(), params=params
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(
                    f"unexpected nt_signal_sim response shape: {type(batch).__name__}"
                )
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


# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Phase E: measure walk-forward barrier-orientation AUC of every "
        "directional candidate vs the analytic null (era-clean). Read-only.",
    )
    ap.add_argument(
        "--since", default=None, metavar="ISO",
        help=f"ISO date/datetime UTC (default: {DEFAULT_LOOKBACK_DAYS} days back)",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="optional cap on fetched rows"
    )
    ap.add_argument(
        "--symbols", default=None, metavar="A,B",
        help="optional comma-separated symbol whitelist",
    )
    ap.add_argument(
        "--sigma", type=float, default=None,
        help="per-bar sigma in ATR units (default: posterior.pbm_sigma_atr(), i.e. 1.0)",
    )
    ap.add_argument(
        "--min-cell-n", type=int, default=DEFAULT_MIN_CELL_N,
        help="min decided rows per (symbol, side) cell for the gap tables",
    )
    ap.add_argument(
        "--bootstrap", type=int, default=DEFAULT_BOOTSTRAP,
        help="bootstrap resamples for the walk-forward AUC CI",
    )
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap RNG seed")
    ap.add_argument("--out", default=None, help="report path override")
    return ap.parse_args(argv)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args()
    sigma = float(args.sigma) if args.sigma is not None else float(pbm_sigma_atr())
    since = _parse_since(args.since)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else None
    )

    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        raise SystemExit(
            "SUPABASE_URL/SUPABASE_KEY not configured — nothing to measure. "
            "(Offline verification: py_compile + the synthetic AUC self-test "
            "require no network.)"
        )

    cutover_iso = await _fetch_cutover_at()
    logger.info("cutover marker: %s", cutover_iso or "MISSING (fail-open)")
    raw = await fetch_rows(since, symbols, args.limit)
    logger.info("fetched %d resolved rows since %s", len(raw), since.isoformat())

    prep, bad_ts = prepare_rows(raw, sigma)
    ctx = measure(
        prep,
        sigma=sigma,
        n_boot=args.bootstrap,
        seed=args.seed,
        min_cell_n=args.min_cell_n,
        cutover_iso=cutover_iso,
        fetched=len(raw),
        bad_ts=bad_ts,
        since=since,
        symbols=symbols,
        limit=args.limit,
    )

    now = datetime.now(timezone.utc)
    text = render_report(ctx, now)
    out_path = Path(args.out) if args.out else REPORT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    for key, name, _target, _desc in SCORERS:
        st = ctx["stats"][key]
        logger.info(
            "scorer %-12s n=%-6d WF AUC %s  -> %s",
            name, st["n"], _fmt(st["auc_wf"], ".3f"), st["verdict"],
        )
    pooled = ctx["pooled_gap"]
    if pooled:
        logger.info(
            "pooled selection gap %s (WR %.3f vs null %.3f, n=%d)",
            _fmt(pooled["gap"], ".4f", plus=True), pooled["wr"], pooled["null"], pooled["n"],
        )
    logger.info("wrote %s", out_path)


if __name__ == "__main__":
    asyncio.run(main())
