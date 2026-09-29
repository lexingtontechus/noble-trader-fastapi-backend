"""
kairos_atr_decision_read.py — dynamic ATR promoted-vs-control + OOS geometry band check.

Roadmap: dynamic_atr_mode_flip (48-72h light outcomes) + geometry_replay_wide_tp_tight_sl
(re-run on 09-25+). Read-only.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_atr_decision_read.py
"""
from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.tiered_atr import should_promote_atr_candidates  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260929_atr_decision_read.md"
# Hybrid promote live 2026-09-25; first live heavy 09-26 17:06 UTC
WIN_START = "2026-09-26T00:00:00Z"


def fetch(table: str, params: dict) -> list[dict]:
    """PostgREST caps limit at 1000 — page by offset."""
    rows: list[dict] = []
    off = 0
    while True:
        r = httpx.get(
            f"{U}/rest/v1/{table}",
            headers=H,
            params={**params, "limit": "1000", "offset": str(off)},
            timeout=90,
        )
        r.raise_for_status()
        b = r.json()
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    return rows


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    out = {"n": n}
    for key in ("tp_distance_atr", "sl_distance_atr", "pnl_bricks", "pnl_dollars", "mae", "mfe"):
        vals = [x[key] for x in rows if x.get(key) is not None]
        out[f"avg_{key}"] = round(sum(vals) / len(vals), 4) if vals else None
    outs = [x.get("outcome") for x in rows]
    out["tp_hit"] = outs.count("tp_hit")
    out["sl_hit"] = outs.count("sl_hit")
    out["expired"] = outs.count("expired")
    out["pct_expired"] = round(out["expired"] / n, 3) if n else None
    out["win_rate_decided"] = (
        round(out["tp_hit"] / (out["tp_hit"] + out["sl_hit"]), 3)
        if (out["tp_hit"] + out["sl_hit"])
        else None
    )
    return out


def main() -> None:
    now = datetime.now(timezone.utc)
    lines = [
        "# ATR decision read — promoted vs control + OOS geometry band",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · window from {WIN_START}",
        "",
        "## 0. Promote list (heavy ATR evidence)",
        "",
    ]

    shadow = fetch("nt_heavy_sweep_atr_anchored_shadow", {"select": "*", "order": "updated_at.desc", "limit": "50"})
    reach = fetch(
        "nt_bracket_reachability",
        {"select": "symbol,distance_atr_bucket,n_stored,pct_expired", "signal": "eq.all", "limit": "500"},
    )
    lag = {}
    for x in reach:
        if int(x.get("n_stored") or 0) < 3:
            continue
        lag[(x["symbol"], x["distance_atr_bucket"])] = x.get("pct_expired")

    promoted, control, table_rows = [], [], []
    for x in shadow:
        sym = x.get("symbol")
        bb, wb = x.get("last_baseline_bucket"), x.get("last_widened_bucket")
        lb, lw = lag.get((sym, bb)), lag.get((sym, wb))
        prom = should_promote_atr_candidates(
            selection_changed=bool(x.get("n_changed")),
            baseline_bucket=bb,
            widened_bucket=wb,
            pct_expired_by_bucket={bb: lb, wb: lw} if bb or wb else {},
        )
        table_rows.append((sym, x.get("n_compared"), x.get("n_changed"), bb, wb, lb, lw, prom))
        (promoted if prom else control).append(sym)

    lines.append("| symbol | n_cmp | chg | base | wide | lagB | lagW | promote |")
    lines.append("|---|---:|---:|---|---|---:|---:|---|")
    for row in table_rows:
        sym, nc, chg, bb, wb, lb, lw, prom = row
        lines.append(
            f"| {sym} | {nc} | {chg} | {bb} | {wb} | "
            f"{lb if lb is not None else '—'} | {lw if lw is not None else '—'} | {prom} |"
        )
    lines.append("")
    lines.append(f"**Promoted:** {', '.join(promoted) or '(none)'}")
    lines.append(f"**Control:** {', '.join(control) or '(none)'}")
    lines.append("")

    # Light outcomes since hybrid promote
    light = fetch(
        "nt_signal_sim",
        {
            "select": "symbol,ts,outcome,tp_distance_atr,sl_distance_atr,pnl_bricks,pnl_dollars,gate_outcome",
            "source": "eq.sweep-light",
            "ts": f"gte.{WIN_START}",
            "order": "ts.asc",
        },
    )
    # only decided for path metrics
    decided = [x for x in light if x.get("outcome") in ("tp_hit", "sl_hit", "expired")]
    print(f"decided light since promote: {len(decided)}", flush=True)

    lines.append("## 1. Dynamic ATR — light outcomes after hybrid promote")
    lines.append("")
    for label, syms in (("PROMOTED", set(promoted)), ("CONTROL", set(control))):
        rows = [x for x in decided if x.get("symbol") in syms]
        s = summarize(rows)
        lines.append(f"### {label} (n={s['n']})")
        lines.append("")
        if not s["n"]:
            lines.append("No decided rows in window.")
            lines.append("")
            continue
        lines.append("| metric | value |")
        lines.append("|---|---:|")
        for k in (
            "tp_hit", "sl_hit", "expired", "pct_expired", "win_rate_decided",
            "avg_tp_distance_atr", "avg_sl_distance_atr", "avg_pnl_bricks", "avg_pnl_dollars",
        ):
            lines.append(f"| {k} | {s.get(k)} |")
        lines.append("")

    lines.append(
        "**Flip bar (roadmap):** tp_distance_atr DOWN toward 0.5–2, pct_expired DOWN, "
        "closed pnl UP/flat at similar n on promoted vs control → `dynamic_atr_mode=live`. "
        "Kill if exp% flat/up or expectancy worse."
    )
    lines.append("")

    # OOS geometry band (09-25+)
    oos = fetch(
        "nt_signal_sim",
        {
            "select": "symbol,ts,outcome,tp_distance_atr,sl_distance_atr,pnl_bricks,pnl_dollars",
            "source": "eq.sweep-light",
            "ts": "gte.2026-09-25T00:00:00Z",
            "order": "ts.asc",
        },
    )
    oos_decided = [x for x in oos if x.get("outcome") in ("tp_hit", "sl_hit", "expired")]
    print(f"OOS decided since 09-25: {len(oos_decided)}", flush=True)

    def band(tp):
        if tp is None:
            return "unknown"
        if 9.0 <= tp <= 17.0:
            return "TP 9-17 (replay band)"
        if tp < 9.0:
            return "TP <9 (tight)"
        return "TP >17 (wide)"

    def sl_band(sl):
        if sl is None:
            return "—"
        if 0.5 <= sl <= 2.0:
            return "SL 0.5-2 (replay band)"
        return f"SL {sl}"

    by_tp: dict[str, list] = defaultdict(list)
    for x in oos_decided:
        by_tp[band(num(x.get("tp_distance_atr")))].append(x)

    lines.append("## 2. OOS geometry replay (09-25 → now)")
    lines.append("")
    lines.append("| TP band | n | pct_expired | win_rate_decided | avg pnl_bricks | avg tp_atr | avg sl_atr |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for label in sorted(by_tp):
        s = summarize(by_tp[label])
        lines.append(
            f"| {label} | {s['n']} | {s.get('pct_expired')} | {s.get('win_rate_decided')} | "
            f"{s.get('avg_pnl_bricks')} | {s.get('avg_tp_distance_atr')} | {s.get('avg_sl_distance_atr')} |"
        )
    lines.append("")

    # slice by halves for stability
    mid = datetime(2026, 9, 28, tzinfo=timezone.utc)
    for name, lo, hi in (("H1 09-25..09-28", None, mid), ("H2 09-28..", mid, None)):
        rows = [
            x for x in by_tp.get("TP 9-17 (replay band)", [])
            if (parse_ts(x.get("ts")) is not None)
            and (lo is None or parse_ts(x["ts"]) >= lo)
            and (hi is None or parse_ts(x["ts"]) < hi)
        ]
        s = summarize(rows)
        lines.append(f"- **{name}** replay-band n={s['n']} exp%={s.get('pct_expired')} wr={s.get('win_rate_decided')} pnl_bricks={s.get('avg_pnl_bricks')}")
    lines.append("")
    lines.append(
        "**Replay bar:** TP 9–17 / SL 0.5–2 stays **positive expectancy** on OOS halves. "
        "If only one half is positive → hold, re-run after more settles."
    )
    lines.append("")
    lines.append("Research-only. No flag flips in this script.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
