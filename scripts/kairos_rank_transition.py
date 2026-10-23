"""
kairos_rank_transition.py — live ranking IC + regime-transition event study.

Read-only. Method: worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md
Output: worklog/kairos/reports/20260926_rank_transition.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_rank_transition.py
"""
from __future__ import annotations

import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.kairos import regime_cell  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_quant import era_of  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import (  # noqa: E402
    auc_within,
    cluster_agree,
    event_window,
    rank_ic,
    shrink_cell_mean,
    surprise_from_counts,
    transition_type,
    y_accuracy,
)

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260926_rank_transition.md"

VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")


def fetch(table: str, params: dict) -> list[dict]:
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{U}/rest/v1/{table}", headers=H, params={**params, "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def split_cell(regime: str | None) -> tuple[str | None, str | None]:
    """HMM label → (vol_tier, direction). Accepts raw labels or 'vol|dir'."""
    if not regime:
        return None, None
    cell = regime_cell(regime)
    if cell and "|" in cell:
        vol, d = cell.split("|", 1)
        return vol, d
    if "|" in str(regime):
        vol, d = str(regime).split("|", 1)
        return vol, d
    return None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    cols = (
        "signal_id,symbol,signal,ts,exit_ts,outcome,qualified,p_win,p_win_dir,p_win_bracket,"
        "predicted_p_win_raw,p_resolve,p_win_resolve,regime,regime_conf,raw_p_regime,"
        "efficiency_24h,clv_1h,rel_volume_1h,atr_ratio_run,tp_distance_atr,sl_distance_atr,"
        "geometry_risk_hat,tp_bricks,sl_bricks,entry_price,take_profit,stop_loss,gate_outcome"
    )
    print("fetch…")
    # discovery window: last 45 days of buy/sell with outcomes (decided focus)
    raw = fetch("nt_signal_sim", {
        "select": cols,
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": "gte.2026-08-15T00:00:00Z",
        "order": "ts.asc",
    })
    print(f"  {len(raw)} rows")

    recs = []
    for r in raw:
        ts = parse_ts(r.get("ts"))
        outcome = r.get("outcome")
        vol, d = split_cell(r.get("regime"))
        tp_b, sl_b = num(r.get("tp_bricks")), num(r.get("sl_bricks"))
        tp_a, sl_a = num(r.get("tp_distance_atr")), num(r.get("sl_distance_atr"))
        # prefer ATR distances for no-skill (matches live settle scale)
        dist_tp = tp_a if tp_a and tp_a > 0 else tp_b
        dist_sl = sl_a if sl_a and sl_a > 0 else sl_b
        recs.append({
            "symbol": r.get("symbol"),
            "ts": ts,
            "outcome": outcome,
            "qualified": r.get("qualified") is True,
            "gate_outcome": r.get("gate_outcome"),
            "regime": r.get("regime"),
            "vol": vol,
            "dir": d,
            "dist_tp": dist_tp,
            "dist_sl": dist_sl,
            "era": era_of(ts),
            "p_win": num(r.get("p_win")),
            "p_win_dir": num(r.get("p_win_dir")),
            "p_win_bracket": num(r.get("p_win_bracket")),
            "predicted_p_win_raw": num(r.get("predicted_p_win_raw")),
            "p_resolve": num(r.get("p_resolve")),
            "regime_conf": num(r.get("regime_conf")),
            "raw_p_regime": num(r.get("raw_p_regime")),
            "efficiency_24h": num(r.get("efficiency_24h")),
            "clv_1h": num(r.get("clv_1h")),
            "rel_volume_1h": num(r.get("rel_volume_1h")),
            "atr_ratio_run": num(r.get("atr_ratio_run")),
            "geometry_risk_hat": num(r.get("geometry_risk_hat")),
        })

    # sort per symbol for event / recent-rate
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for x in recs:
        by_sym[x["symbol"]].append(x)
    for v in by_sym.values():
        v.sort(key=lambda x: x["ts"] or now)

    # transitions + event time
    transition_counts: dict[tuple[str, str], int] = defaultdict(int)
    for sym, rows in by_sym.items():
        prev_vol = prev_dir = None
        last_change = None
        for x in rows:
            vol, d = x["vol"], x["dir"]
            if prev_vol is not None and (vol, d) != (prev_vol, prev_dir):
                ttype = transition_type(prev_vol, prev_dir, vol, d, VOL_ORDER)
                x["ttype"] = ttype
                x["prev_cell"] = f"{prev_vol}|{prev_dir}"
                x["new_cell"] = f"{vol}|{d}"
                transition_counts[(f"{prev_vol}|{prev_dir}", f"{vol}|{d}")] += 1
                last_change = x["ts"]
                x["tau_h"] = 0.0
            elif last_change is not None and x["ts"] is not None:
                x["ttype"] = None
                x["tau_h"] = (x["ts"] - last_change).total_seconds() / 3600.0
            else:
                x["ttype"] = None
                x["tau_h"] = None
            x["ewin"] = event_window(x.get("tau_h"))
            prev_vol, prev_dir = vol, d

    # surprise + cluster agree on flips
    for x in recs:
        if x.get("prev_cell") and x.get("new_cell"):
            x["surprise"] = surprise_from_counts(transition_counts, x["prev_cell"], x["new_cell"])
        else:
            x["surprise"] = None
    flips = [
        (x["symbol"], x.get("dir"), (x["ts"].timestamp() / 3600.0 if x["ts"] else None))
        for x in recs if x.get("ttype") and x.get("ttype") != "none"
    ]
    # crude cluster: first 3 letters (metal/gold family) — better: pair_clusters from corr
    cluster_of = {}
    for s in by_sym:
        if s.startswith("XAU") or s.startswith("XAG"):
            cluster_of[s] = "gold"
        elif s in ("US30USD", "US500"):
            cluster_of[s] = "us_idx"
        elif s in ("EURUSD", "GBPUSD", "USDCHF", "USDCAD", "USDJPY", "AUDUSD"):
            cluster_of[s] = "fx"
        else:
            cluster_of[s] = s
    agree_map = cluster_agree([(a, b, c) for a, b, c in flips if c is not None], cluster_of, 2.0)
    for x in recs:
        x["agree"] = agree_map.get(x["symbol"]) if x.get("ttype") else None

    # decided + targets
    decided = []
    for x in recs:
        if x["outcome"] not in ("tp_hit", "sl_hit"):
            continue
        tp_first = x["outcome"] == "tp_hit"
        yb = 1.0 if tp_first else 0.0
        ya = y_accuracy(tp_first, x["dist_tp"], x["dist_sl"])
        x["y_bin"] = yb
        x["y_acc"] = ya
        decided.append(x)

    lines = ["# Ranking IC + regime-transition event study", ""]
    lines.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · read-only · `kairos_rank`")
    lines.append(f"Rows {len(recs)} · decided {len(decided)} · transitions logged {sum(1 for x in recs if x.get('ttype') and x['ttype']!='none')}")
    lines.append("")

    # ── Section 1: event-time T1 ─────────────────────────────────────────
    lines.append("## 1. Event time (T1: fresh vs deep)")
    lines.append("")
    lines.append("| window | n decided | mean y_acc | mean y_bin | mean y_acc |")
    lines.append("|---|---:|---:|---:|---:|")
    by_win: dict[str, list[dict]] = defaultdict(list)
    for x in decided:
        if x.get("ewin"):
            by_win[x["ewin"]].append(x)
    for w in ("pre", "fresh", "warm", "stale", "deep"):
        rows = by_win.get(w, [])
        if not rows:
            lines.append(f"| {w} | 0 | — | — | — |")
            continue
        ya = [x["y_acc"] for x in rows if x["y_acc"] is not None]
        yb = [x["y_bin"] for x in rows]
        lines.append(
            f"| {w} | {len(rows)} | {statistics.fmean(ya) if ya else float('nan'):.3f} | "
            f"{statistics.fmean(yb):.3f} | {statistics.fmean(ya) if ya else float('nan'):.3f} |"
        )
    # contrast fresh - deep
    fr = [x["y_acc"] for x in by_win.get("fresh", []) if x["y_acc"] is not None]
    dp = [x["y_acc"] for x in by_win.get("deep", []) if x["y_acc"] is not None]
    if fr and dp:
        dlt = statistics.fmean(fr) - statistics.fmean(dp)
        rng = np.random.default_rng(20260926)
        boots = []
        for _ in range(500):
            a = rng.choice(fr, size=len(fr), replace=True)
            b = rng.choice(dp, size=len(dp), replace=True)
            boots.append(float(np.mean(a) - np.mean(b)))
        lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
        lines.append("")
        lines.append(f"**T1 contrast (fresh − deep) y_acc = {dlt:+.3f} [{lo:+.3f}, {hi:+.3f}]** · n_fresh={len(fr)} n_deep={len(dp)}")
        if lo > 0:
            lines.append("→ T1 **supported** (fresh beats deep).")
        elif hi < 0:
            lines.append("→ T1 **reversed** (deep beats fresh).")
        else:
            lines.append("→ T1 **not supported** at this n.")
    else:
        lines.append("")
        lines.append("**T1 contrast:** insufficient decided in fresh/deep.")
    lines.append("")

    # type split
    lines.append("## 2. Transition type (H3)")
    lines.append("")
    lines.append("| type | n decided | mean y_acc | mean y_bin |")
    lines.append("|---|---:|---:|---:|")
    by_type: dict[str, list[dict]] = defaultdict(list)
    for x in decided:
        if x.get("ttype") and x["ttype"] != "none" and x.get("ewin") in ("fresh", "warm"):
            by_type[x["ttype"]].append(x)
    # also include decided near transitions (tau < 12h)
    by_type2: dict[str, list[dict]] = defaultdict(list)
    for x in decided:
        if x.get("ttype") and x["ttype"] != "none":
            by_type2[x["ttype"]].append(x)
        elif x.get("tau_h") is not None and x["tau_h"] < 12 and x.get("ttype") is None:
            pass
    for t, rows in sorted(by_type2.items()):
        ya = [x["y_acc"] for x in rows if x["y_acc"] is not None]
        yb = [x["y_bin"] for x in rows]
        lines.append(f"| {t} | {len(rows)} | {statistics.fmean(ya) if ya else float('nan'):.3f} | {statistics.fmean(yb):.3f} |")
    lines.append("")

    # surprise
    lines.append("## 3. Surprise (H4) — Spearman(surprise, y_acc) on decided with surprise")
    lines.append("")
    pairs = [(x["surprise"], x["y_acc"]) for x in decided if x.get("surprise") is not None and x.get("y_acc") is not None]
    if len(pairs) >= 5:
        ic = rank_ic([p[0] for p in pairs], [p[1] for p in pairs])
        lines.append(f"n={len(pairs)} · rank IC = {ic if ic is None else round(ic, 3)}")
    else:
        lines.append(f"n={len(pairs)} — too few transitions with decided outcomes for H4.")
    lines.append("")

    # ── Section 4: ranking ───────────────────────────────────────────────
    lines.append("## 4. Ranking inventory vs y_acc / y_bin (within-cell)")
    lines.append("")
    feats = [
        "p_win", "p_win_dir", "p_win_bracket", "predicted_p_win_raw", "p_resolve",
        "regime_conf", "raw_p_regime", "efficiency_24h", "clv_1h", "rel_volume_1h",
        "atr_ratio_run", "geometry_risk_hat",
    ]
    lines.append("| feature | n | Spearman(y_acc) pooled | mean within-cell IC | cells n≥30 | AUC(y_bin) pooled |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for f in feats:
        rows = [x for x in decided if x.get(f) is not None and x.get("y_acc") is not None]
        if len(rows) < 20:
            lines.append(f"| {f} | {len(rows)} | — | — | — | — |")
            continue
        ic_p = rank_ic([x[f] for x in rows], [x["y_acc"] for x in rows])
        # within-cell
        cells: dict[tuple, list[dict]] = defaultdict(list)
        for x in rows:
            cells[(x["symbol"], x["regime"])].append(x)
        ics = []
        n_big = 0
        for _c, xs in cells.items():
            if len(xs) < 30:
                continue
            n_big += 1
            ic = rank_ic([x[f] for x in xs], [x["y_acc"] for x in xs])
            if ic is not None:
                ics.append(ic)
        auc = auc_within([x["y_bin"] > 0.5 for x in rows], [x[f] for x in rows])
        lines.append(
            f"| {f} | {len(rows)} | {ic_p if ic_p is None else round(ic_p, 3)} | "
            f"{statistics.fmean(ics) if ics else float('nan'):.3f} | {n_big} | "
            f"{auc if auc is None else round(auc, 3)} |"
        )
    lines.append("")
    lines.append("Pooled AUC mixes symbols; treat as descriptive only (triage: withdrawn as mix artefact). Within-cell IC is the ranking bar.")
    lines.append("")

    # cell-shrink baseline vs p_win
    lines.append("## 5. Cell-shrink baseline vs p_win (walk-forward-ish)")
    lines.append("")
    # chronological split 70/30 per symbol
    hit_shrink = hit_pwin = n_scored = 0
    brier_shrink = brier_pwin = []
    # use decided sorted by ts
    decided_sorted = sorted([x for x in decided if x["ts"]], key=lambda x: x["ts"])
    # trailing estimates per (symbol, regime)
    hist: dict[tuple, list[float]] = defaultdict(list)
    global_hits: list[float] = []
    for x in decided_sorted:
        cell = (x["symbol"], x["regime"])
        if len(hist[cell]) >= 5 and global_hits:
            p_cell = shrink_cell_mean(hist[cell], statistics.fmean(global_hits), k=10.0)
            p_win = x.get("p_win")
            if p_cell is not None and p_win is not None:
                y = x["y_bin"]
                brier_shrink.append((p_cell - y) ** 2)
                brier_pwin.append((min(1, max(0, p_win)) - y) ** 2)
                n_scored += 1
                if (p_cell >= 0.5) == (y >= 0.5):
                    hit_shrink += 1
                if (p_win >= 0.5) == (y >= 0.5):
                    hit_pwin += 1
        hist[cell].append(x["y_bin"])
        global_hits.append(x["y_bin"])
    if n_scored:
        lines.append(f"Scored decisions (prior n_cell≥5): **{n_scored}**")
        lines.append(f"- cell-shrink Brier {statistics.fmean(brier_shrink):.3f} · p_win Brier {statistics.fmean(brier_pwin):.3f} · Δ(p_win − shrink) {statistics.fmean(brier_pwin)-statistics.fmean(brier_shrink):+.3f}")
        lines.append(f"- hit-rate@0.5: shrink {hit_shrink/n_scored:.3f} · p_win {hit_pwin/n_scored:.3f}")
        if statistics.fmean(brier_pwin) > statistics.fmean(brier_shrink) + 0.01:
            lines.append("- **p_win is worse than a shrunk cell hit-rate** on Brier → ranking/calibration should start from cell rates (worklog §3.4).")
    else:
        lines.append("Not enough decided history for walk-forward cell shrink.")
    lines.append("")

    # era note
    lines.append("## 6. Era mix (triage D4)")
    lines.append("")
    lines.append("| era | decided |")
    lines.append("|---|---:|")
    for e in ("pre_debounce", "debounce_to_atr", "atr_to_resolver", "post_resolver"):
        n = sum(1 for x in decided if x["era"] == e)
        lines.append(f"| {e} | {n} |")
    lines.append("")
    lines.append("Event-time and ranking results are **mixed-era** until post-resolver volume grows; treat as discovery (R1), not promote evidence.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
