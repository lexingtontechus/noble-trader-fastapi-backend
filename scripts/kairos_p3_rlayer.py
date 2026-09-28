"""
kairos_p3_rlayer.py — P3 R-Layer: rank_score + timesfm_gap walk-forward eval.

Scope: worklog/kairos/20260927_P0_P4_PROGRAMME.md §5
Gate: ΔD CI > 0 vs current rank_score on episode unit; research-only.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_p3_rlayer.py
"""
from __future__ import annotations

import math
import os
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.kairos import regime_cell  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import (  # noqa: E402
    event_window,
    rank_ic,
    rank_score,
    shadow_entry_gate,
    shrink_cell_mean,
    transition_type,
    y_accuracy,
)

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260928_p3_rlayer.md"
VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")
MIN_CELL_N = 20


def fetch(table: str, params: dict) -> list[dict]:
    def page(off: int) -> tuple[list[dict], str | None]:
        with httpx.Client(timeout=120) as c:
            r = c.get(
                f"{U}/rest/v1/{table}",
                headers={**H, "Prefer": "count=exact"},
                params={**params, "limit": "1000", "offset": str(off)},
            )
            r.raise_for_status()
            return r.json(), r.headers.get("content-range")

    head, cr = page(0)
    total = None
    if cr and "/" in cr:
        try:
            total = int(cr.split("/")[-1])
        except ValueError:
            total = None
    if not total or total <= 1000:
        return head
    rows = list(head)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(page, o) for o in range(1000, total, 1000)]
        for fut in as_completed(futs):
            b, _ = fut.result()
            rows.extend(b)
    return rows


def parse_ts(s) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def split_cell(regime: str | None) -> tuple[str | None, str | None]:
    if not regime:
        return None, None
    cell = regime_cell(regime)
    src = cell if cell and "|" in cell else str(regime)
    if "|" in src:
        vol, d = src.split("|", 1)
        return vol, d
    return None, None


def delta_d_bootstrap(y_a: list[float], y_b: list[float], n_boot: int = 400, seed: int = 20260928) -> tuple[float | None, float | None, float | None]:
    """Mean(y_a) − mean(y_b) with bootstrap CI."""
    if not y_a or not y_b:
        return None, None, None
    import random

    d = statistics.fmean(y_a) - statistics.fmean(y_b)
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        a = [y_a[rng.randrange(len(y_a))] for _ in range(len(y_a))]
        b = [y_b[rng.randrange(len(y_b))] for _ in range(len(y_b))]
        boots.append(statistics.fmean(a) - statistics.fmean(b))
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot) - 1]
    return d, lo, hi


def main() -> None:
    now = datetime.now(timezone.utc)
    print("fetch signals…", flush=True)
    raw = fetch(
        "nt_signal_sim",
        {
            "select": "symbol,ts,outcome,signal,regime,tp_distance_atr,sl_distance_atr,tp_bricks,sl_bricks",
            "source": "eq.sweep-light",
            "signal": "in.(buy,sell)",
            "ts": "gte.2026-08-15T00:00:00Z",
            "order": "ts.asc",
        },
    )
    print(f"  {len(raw)} rows", flush=True)
    print("fetch sweep p_timesfm/p_win…", flush=True)
    sweep = fetch(
        "nt_sweep_result",
        {
            "select": "symbol,sweep_timestamp,p_timesfm,p_win",
            "source": "eq.sweep-light",
            "sweep_timestamp": "gte.2026-08-15T00:00:00Z",
            "order": "sweep_timestamp.asc",
        },
    )
    print(f"  {len(sweep)} sweep", flush=True)

    by_sym_sweep: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for s in sweep:
        ts = parse_ts(s.get("sweep_timestamp"))
        if ts is None or not s.get("symbol"):
            continue
        by_sym_sweep[s["symbol"]].append((ts.timestamp(), s))
    for v in by_sym_sweep.values():
        v.sort(key=lambda t: t[0])

    def nearest(symbol: str, ts: datetime) -> dict | None:
        arr = by_sym_sweep.get(symbol)
        if not arr:
            return None
        t = ts.timestamp()
        lo, hi = 0, len(arr) - 1
        best, best_d = None, None
        while lo <= hi:
            mid = (lo + hi) // 2
            d = abs(arr[mid][0] - t)
            if best_d is None or d < best_d:
                best_d, best = d, arr[mid][1]
            if arr[mid][0] < t:
                lo = mid + 1
            else:
                hi = mid - 1
        return best if best_d is not None and best_d <= 1200 else None

    recs = []
    for r in raw:
        if r.get("outcome") not in ("tp_hit", "sl_hit"):
            continue
        ts = parse_ts(r.get("ts"))
        if ts is None:
            continue
        vol, d = split_cell(r.get("regime"))
        tp_a, sl_a = num(r.get("tp_distance_atr")), num(r.get("sl_distance_atr"))
        tp_b, sl_b = num(r.get("tp_bricks")), num(r.get("sl_bricks"))
        dist_tp = tp_a if tp_a and tp_a > 0 else tp_b
        dist_sl = sl_a if sl_a and sl_a > 0 else sl_b
        ya = y_accuracy(r.get("outcome") == "tp_hit", dist_tp, dist_sl)
        if ya is None:
            continue
        sw = nearest(r.get("symbol"), ts) or {}
        p_tf, p_win = num(sw.get("p_timesfm")), num(sw.get("p_win"))
        recs.append({
            "symbol": r.get("symbol"),
            "ts": ts,
            "regime": r.get("regime"),
            "vol": vol,
            "dir": d,
            "y_acc": ya,
            "y_bin": 1.0 if r.get("outcome") == "tp_hit" else 0.0,
            "p_timesfm": p_tf,
            "p_win": p_win,
            "timesfm_gap": (p_tf - p_win) if (p_tf is not None and p_win is not None) else None,
        })

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for x in recs:
        by_sym[x["symbol"]].append(x)
    for rows in by_sym.values():
        rows.sort(key=lambda x: x["ts"] or now)
        prev = None
        last_change = last_type = None
        recent: list[tuple[float, bool]] = []
        for x in rows:
            cur = (x["vol"], x["dir"])
            if prev is not None and cur != prev:
                last_type = transition_type(prev[0], prev[1], cur[0], cur[1], VOL_ORDER)
                last_change = x["ts"]
                x["tau_h"] = 0.0
            elif last_change and x["ts"]:
                x["tau_h"] = (x["ts"] - last_change).total_seconds() / 3600.0
            else:
                x["tau_h"] = None
            x["ttype"] = last_type if last_type not in (None, "none") else None
            x["ewin"] = event_window(x.get("tau_h"))
            # trailing recent_tp 3h
            t = x["ts"].timestamp() if x["ts"] else None
            if t is not None:
                recent = [(tt, tp) for tt, tp in recent if t - tt <= 3 * 3600]
                if x["y_bin"] is not None:
                    recent.append((t, bool(x["y_bin"])))
                x["recent_tp"] = (
                    sum(1 for _, tp in recent if tp) / len(recent) if recent else None
                )
            else:
                x["recent_tp"] = None
            prev = cur

    cell_out: dict[tuple, list[float]] = defaultdict(list)
    for x in recs:
        cell_out[(x["symbol"], x.get("regime"))].append(x["y_bin"])
    for x in recs:
        x["cell_p"] = shrink_cell_mean(cell_out.get((x["symbol"], x.get("regime"))) or [], 0.35, k=10)

    for x in recs:
        x["score_base"] = rank_score(
            event_window=x.get("ewin"), transition=x.get("ttype"),
            recent_tp_rate=x.get("recent_tp"), cell_p=x.get("cell_p"),
        )
        x["score_p3"] = rank_score(
            event_window=x.get("ewin"), transition=x.get("ttype"),
            recent_tp_rate=x.get("recent_tp"), cell_p=x.get("cell_p"),
            timesfm_gap=x.get("timesfm_gap"),
        )

    decided = [x for x in recs if x.get("timesfm_gap") is not None]
    n = len(decided)
    print(f"  decided with gap n={n}", flush=True)

    lines = [
        "# P3 — R-Layer (rank_score + timesfm_gap)",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · episode unit · n={n}",
        "",
        "P3 feature set: freshness + type + recent_tp + cell_p + **timesfm_gap** (P1.2).",
        "Quantiles optional / not used.",
        "",
    ]

    # IC
    for label, key in (("base rank_score", "score_base"), ("P3 rank_score", "score_p3"), ("timesfm_gap alone", "timesfm_gap")):
        ic = rank_ic([x[key] for x in decided], [x["y_acc"] for x in decided])
        cells: dict[tuple, list[dict]] = defaultdict(list)
        for x in decided:
            cells[(x["symbol"], x.get("regime"))].append(x)
        ics = []
        for _c, xs in cells.items():
            if len(xs) < MIN_CELL_N:
                continue
            c = rank_ic([x[key] for x in xs], [x["y_acc"] for x in xs])
            if c is not None:
                ics.append(c)
        wic = statistics.fmean(ics) if ics else None
        lines.append(
            f"- **{label}**: pooled IC={ic if ic is None else round(ic, 3)} · "
            f"within-cell={wic if wic is None else round(wic, 3)} (cells {len(ics)})"
        )
    lines.append("")

    # ΔD at P60 gate
    lines.append("## ΔD at trailing-P60 gate (y_acc)")
    lines.append("")
    for label, key in (("base", "score_base"), ("P3", "score_p3")):
        scores = [x[key] for x in decided]
        thr = statistics.quantiles(scores, n=10)[5] if len(scores) >= 20 else 0.55
        gated = [x["y_acc"] for x in decided if shadow_entry_gate(x[key], thr)]
        rest = [x["y_acc"] for x in decided if not shadow_entry_gate(x[key], thr)]
        d, lo, hi = delta_d_bootstrap(gated, rest)
        lines.append(
            f"- **{label}** thr≈{thr:.3f} · n_gated={len(gated)} n_rest={len(rest)} · "
            f"ΔD={d if d is None else f'{d:+.3f}'} [{lo if lo is None else f'{lo:+.3f}'}, {hi if hi is None else f'{hi:+.3f}'}]"
        )
    lines.append("")

    # half-split
    lines.append("## Chronological half-split (pooled IC)")
    lines.append("")
    rows = sorted(decided, key=lambda x: x["ts"] or now)
    mid = len(rows) // 2
    for lab, part in (("h1", rows[:mid]), ("h2", rows[mid:])):
        ic_b = rank_ic([x["score_base"] for x in part], [x["y_acc"] for x in part])
        ic_p = rank_ic([x["score_p3"] for x in part], [x["y_acc"] for x in part])
        lines.append(f"- **{lab}** base={ic_b if ic_b is None else round(ic_b, 3)} · P3={ic_p if ic_p is None else round(ic_p, 3)}")
    lines.append("")
    lines.append("Research-only. No live gate / Kelly change.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
