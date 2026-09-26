"""
kairos_rank_score_eval.py — walk-forward composite rank score + shadow ΔD gate.

Method: worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md §3.4 / §4
Output: worklog/kairos/reports/20260926_rank_score_eval.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_rank_score_eval.py
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
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.kairos import regime_cell  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_quant import era_of  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import (  # noqa: E402
    DEFAULT_RANK_W,
    auc_within,
    delta_d,
    delta_d_bootstrap,
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
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260926_rank_score_eval.md"
VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")
GATE_Q = 0.60  # shadow gate: top 40% of trailing score quantile — see report


def fetch(params: dict) -> list[dict]:
    rows: list[dict] = []
    off = 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H, params={**params, "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def split_cell(regime):
    if not regime:
        return None, None
    cell = regime_cell(regime)
    if cell and "|" in cell:
        vol, d = cell.split("|", 1)
        return vol, d
    return None, None


def main() -> None:
    now = datetime.now(timezone.utc)
    cols = (
        "signal_id,symbol,signal,ts,outcome,p_win,regime,tp_distance_atr,sl_distance_atr,"
        "tp_bricks,sl_bricks,entry_price,take_profit,stop_loss,gate_outcome,qualified"
    )
    print("fetch…")
    raw = fetch({
        "select": cols,
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": "gte.2026-08-23T00:00:00Z",
        "order": "ts.asc",
    })
    print(f"  {len(raw)} rows")

    recs = []
    for r in raw:
        ts = parse_ts(r.get("ts"))
        vol, d = split_cell(r.get("regime"))
        tp_a, sl_a = num(r.get("tp_distance_atr")), num(r.get("sl_distance_atr"))
        tp_b, sl_b = num(r.get("tp_bricks")), num(r.get("sl_bricks"))
        dist_tp = tp_a if tp_a and tp_a > 0 else tp_b
        dist_sl = sl_a if sl_a and sl_a > 0 else sl_b
        recs.append({
            "symbol": r.get("symbol"),
            "ts": ts,
            "outcome": r.get("outcome"),
            "vol": vol,
            "dir": d,
            "regime": r.get("regime"),
            "p_win": num(r.get("p_win")),
            "dist_tp": dist_tp,
            "dist_sl": dist_sl,
            "era": era_of(ts),
            "qualified": r.get("qualified") is True,
        })

    by_sym: dict[str, list[dict]] = defaultdict(list)
    # episode dedupe: one decision per symbol × direction × hour (G2 unit)
    seen_ep: set[tuple] = set()
    for x in recs:
        if not x["ts"]:
            continue
        key = (x["symbol"], x.get("dir"), x["ts"].strftime("%Y%m%d%H"))
        if key in seen_ep:
            continue
        seen_ep.add(key)
        by_sym[x["symbol"]].append(x)
    for v in by_sym.values():
        v.sort(key=lambda x: x["ts"])
    print(f"  episodes after dedupe: {sum(len(v) for v in by_sym.values())}")

    # event time / type
    for sym, rows in by_sym.items():
        prev_vol = prev_dir = None
        last_change = None
        for x in rows:
            vol, d = x["vol"], x["dir"]
            if prev_vol is not None and (vol, d) != (prev_vol, prev_dir):
                x["ttype"] = transition_type(prev_vol, prev_dir, vol, d, VOL_ORDER)
                last_change = x["ts"]
                x["tau_h"] = 0.0
            elif last_change is not None:
                x["ttype"] = None
                x["tau_h"] = (x["ts"] - last_change).total_seconds() / 3600.0
            else:
                x["ttype"] = None
                x["tau_h"] = None
            x["ewin"] = event_window(x.get("tau_h"))
            prev_vol, prev_dir = vol, d

    # walk-forward rank score on decided rows (chronological per symbol stream)
    decided_all = []
    # trailing state
    recent_outcomes: dict[str, list[bool]] = defaultdict(list)  # chronological TP flags
    cell_hits: dict[tuple, list[float]] = defaultdict(list)
    global_hits: list[float] = []
    # score history for quantile gate
    score_hist: list[float] = []

    scored = []
    for sym, rows in by_sym.items():
        # reset per-symbol trailing using only earlier rows in THIS symbol
        local_recent: list[bool] = []
        for x in rows:
            # score at decision time uses only prior decided in this symbol
            if x["outcome"] in ("tp_hit", "sl_hit") and x["dist_tp"] and x["dist_sl"]:
                tp_first = x["outcome"] == "tp_hit"
                yb = 1.0 if tp_first else 0.0
                ya = y_accuracy(tp_first, x["dist_tp"], x["dist_sl"])
                cell = (sym, x["regime"])
                p_cell = shrink_cell_mean(
                    cell_hits.get(cell, []), statistics.fmean(global_hits) if global_hits else 0.3, k=10.0,
                )
                rec_tp = None
                if local_recent:
                    wsum = tsum = 0.0
                    for i, tp in enumerate(local_recent):
                        age = float(len(local_recent) - i)
                        w = 0.5 ** (age / 3.0)
                        wsum += w
                        tsum += w * (1.0 if tp else 0.0)
                    rec_tp = tsum / wsum if wsum > 0 else None
                s = rank_score(
                    event_window=x.get("ewin"),
                    transition=x.get("ttype"),
                    recent_tp_rate=rec_tp,
                    cell_p=p_cell,
                    weights=DEFAULT_RANK_W,
                )
                row = {
                    **x,
                    "y_bin": yb,
                    "y_acc": ya,
                    "score": s,
                    "cell_p": p_cell,
                    "recent_tp": rec_tp,
                    "p_win_clip": min(1.0, max(0.0, x["p_win"])) if x["p_win"] is not None else None,
                }
                scored.append(row)
                decided_all.append(row)
                # update trailing AFTER scoring (no lookahead)
                local_recent.append(tp_first)
                cell_hits[cell].append(yb)
                global_hits.append(yb)
                score_hist.append(s)

    lines = ["# Walk-forward rank score + shadow ΔD gate", ""]
    lines.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · weights {DEFAULT_RANK_W} · gate quantile ≥ {GATE_Q}")
    lines.append(f"Decided rows scored walk-forward: **{len(scored)}**")
    lines.append("")

    def eval_block(rows: list[dict], label: str) -> None:
        lines.append(f"## {label}")
        lines.append("")
        if len(rows) < 30:
            lines.append(f"n={len(rows)} — too small.")
            lines.append("")
            return
        # drop rows without score/p_win
        rows = [x for x in rows if x.get("score") is not None]
        ya = [x["y_acc"] for x in rows if x["y_acc"] is not None]
        yb = [x["y_bin"] for x in rows]
        lines.append(f"n={len(rows)} · mean y_acc={statistics.fmean(ya):.3f} · TP rate={statistics.fmean(yb):.3f}")
        # IC / AUC
        for name, key in (("rank_score", "score"), ("p_win", "p_win_clip"), ("cell_p", "cell_p")):
            xs = [x.get(key) for x in rows]
            ok = [(a, b) for a, b in zip(xs, [x["y_acc"] for x in rows]) if a is not None and b is not None]
            ic = rank_ic([a for a, _ in ok], [b for _, b in ok]) if len(ok) >= 20 else None
            pairs = [(x["y_bin"] > 0.5, x.get(key)) for x in rows if x.get(key) is not None]
            auc = auc_within([p for p, _ in pairs], [s for _, s in pairs]) if pairs else None
            lines.append(f"- **{name}**: Spearman(y_acc)={ic if ic is None else round(ic, 3)} · AUC(y_bin)={auc if auc is None else round(auc, 3)}")
        # Brier: map score/p_win/cell_p to a P(TP) proxy
        # For ranking we also report hit@top-tercile of score vs p_win
        def tercile_hit(key: str) -> str:
            pairs = [(x.get(key), x["y_bin"]) for x in rows if x.get(key) is not None and x["y_acc"] is not None]
            if len(pairs) < 60:
                return "—"
            pairs.sort(key=lambda t: t[0])
            k = len(pairs) // 3
            top = pairs[-k:]
            bot = pairs[:k]
            return (
                f"top {statistics.fmean([y for _, y in top]):.3f} vs bot {statistics.fmean([y for _, y in bot]):.3f} "
                f"(Δ {statistics.fmean([y for _, y in top]) - statistics.fmean([y for _, y in bot]):+.3f})"
            )
        lines.append(f"- TP-rate top vs bot tercile of **rank_score**: {tercile_hit('score')}")
        lines.append(f"- TP-rate top vs bot tercile of **p_win**: {tercile_hit('p_win_clip')}")
        lines.append(f"- TP-rate top vs bot tercile of **cell_p**: {tercile_hit('cell_p')}")
        # shadow gate on rank_score quantile (computed within this block)
        vals = sorted(x["score"] for x in rows)
        if len(vals) >= 20:
            thr = vals[int(GATE_Q * (len(vals) - 1))]
            gated = [x for x in rows if shadow_entry_gate(x["score"], thr)]
            rest = [x for x in rows if not shadow_entry_gate(x["score"], thr)]
            dd, lo, hi = delta_d_bootstrap([x["y_acc"] for x in gated], [x["y_acc"] for x in rest], 400)
            lines.append("")
            lines.append(f"### Shadow gate (score ≥ {thr:.3f} ≈ P{int(GATE_Q*100)})")
            lines.append(f"- gated n={len(gated)} · rest n={len(rest)}")
            lines.append(f"- mean y_acc gated={statistics.fmean([x['y_acc'] for x in gated if x['y_acc'] is not None]):.3f} · rest={statistics.fmean([x['y_acc'] for x in rest if x['y_acc'] is not None]):.3f}")
            if dd is not None:
                if lo is not None:
                    lines.append(f"- **ΔD = {dd:+.3f} [{lo:+.3f}, {hi:+.3f}]**")
                    if lo > 0:
                        lines.append("- **R3 signal:** ΔD > 0 with CI — candidate for a real gate change (still require post-ATR confirm).")
                    elif hi < 0:
                        lines.append("- ΔD < 0 — score orders the wrong way at this threshold.")
                    else:
                        lines.append("- ΔD CI spans 0 — shadow only.")
                else:
                    lines.append(f"- ΔD = {dd:+.3f} (CI unavailable)")
        lines.append("")

    eval_block(scored, "1. All eras (walk-forward)")
    eval_block([x for x in scored if x["era"] in ("atr_to_resolver", "post_resolver", "debounce_to_atr")],
               "2. Post-debounce (debounce_to_atr + ATR eras)")
    eval_block([x for x in scored if x["era"] in ("atr_to_resolver", "post_resolver")],
               "3. Post-ATR only")
    # qualified only
    eval_block([x for x in scored if x.get("qualified")], "4. Qualified only (all eras)")

    lines.append("## Notes")
    lines.append("")
    lines.append("- Scores use only trailing per-symbol decided outcomes and cell means (no lookahead).")
    lines.append("- `cell_p` is shrunk empirical P(TP|decided); `rank_score` mixes freshness, type, recent_tp, cell_p.")
    lines.append("- Selection metric is **ΔD = mean(y_acc|gated) − mean(y_acc|rest)** (worklog §4).")
    lines.append("- Mixed-era blocks are discovery; promote only on post-ATR confirmation.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
