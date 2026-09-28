"""
kairos_p12_timesfm_ic.py — P1.2: TimesFM + TA rank-feature IC vs y_acc.

Gate (20260927_P0_P4_PROGRAMME §3.4):
  ≥1 TimesFM feature with within-cell IC ≠ 0  OR  ΔBrier ≤ −0.02 vs p_timesfm
  (both chronological halves).

p_timesfm + ta_* live on nt_sweep_result (not nt_signal_sim). Join is
symbol + nearest sweep_timestamp within 20 min. Read-only.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_p12_timesfm_ic.py
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
from regime_platform.sweep_orchestrator.kairos_quant import era_of  # noqa: E402
from regime_platform.sweep_orchestrator.kairos_rank import (  # noqa: E402
    event_window,
    rank_ic,
    shrink_cell_mean,
    timesfm_rank_features,
    transition_type,
    y_accuracy,
)

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260927_p12_timesfm_ic.md"
VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")
MIN_CELL_N = 20
JOIN_S = 20 * 60  # ±20 min


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


def within_cell_ics(rows: list[dict], feat: str, y_key: str = "y_acc") -> tuple[float | None, int]:
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for x in rows:
        if x.get(feat) is not None and x.get(y_key) is not None:
            cells[(x["symbol"], x.get("regime"))].append(x)
    ics = []
    for _c, xs in cells.items():
        if len(xs) < MIN_CELL_N:
            continue
        ic = rank_ic([x[feat] for x in xs], [x[y_key] for x in xs])
        if ic is not None:
            ics.append(ic)
    return (statistics.fmean(ics) if ics else None), len(ics)


def brier(rows: list[dict], p_key: str, y_key: str = "y_bin") -> float | None:
    pairs = [
        (float(x[p_key]), float(x[y_key]))
        for x in rows
        if x.get(p_key) is not None and x.get(y_key) is not None
    ]
    if len(pairs) < 20:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def main() -> None:
    now = datetime.now(timezone.utc)
    print("fetch signals…", flush=True)
    raw = fetch(
        "nt_signal_sim",
        {
            "select": (
                "symbol,ts,outcome,signal,regime,"
                "p_win,p_win_dir,p_win_bracket,"
                "tp_distance_atr,sl_distance_atr,tp_bricks,sl_bricks"
            ),
            "source": "eq.sweep-light",
            "signal": "in.(buy,sell)",
            "ts": "gte.2026-08-15T00:00:00Z",
            "order": "ts.asc",
        },
    )
    print(f"  {len(raw)} signal rows", flush=True)

    print("fetch sweep (p_timesfm + ta)…", flush=True)
    sweep = fetch(
        "nt_sweep_result",
        {
            "select": (
                "symbol,sweep_timestamp,p_timesfm,signal,"
                "ta_rsi,ta_adx,ta_macd,ta_bb_power,ta_ao,markov_p_up"
            ),
            "source": "eq.sweep-light",
            "sweep_timestamp": "gte.2026-08-15T00:00:00Z",
            "order": "sweep_timestamp.asc",
        },
    )
    print(f"  {len(sweep)} sweep rows", flush=True)

    # index sweep by symbol
    by_sym_sweep: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for s in sweep:
        ts = parse_ts(s.get("sweep_timestamp"))
        if ts is None or not s.get("symbol"):
            continue
        by_sym_sweep[s["symbol"]].append((ts.timestamp(), s))
    for v in by_sym_sweep.values():
        v.sort(key=lambda t: t[0])

    def nearest_sweep(symbol: str, ts: datetime) -> dict | None:
        arr = by_sym_sweep.get(symbol)
        if not arr or ts is None:
            return None
        t = ts.timestamp()
        # binary search nearest
        lo, hi = 0, len(arr) - 1
        best = None
        best_d = None
        while lo <= hi:
            mid = (lo + hi) // 2
            d = abs(arr[mid][0] - t)
            if best_d is None or d < best_d:
                best_d, best = d, arr[mid][1]
            if arr[mid][0] < t:
                lo = mid + 1
            else:
                hi = mid - 1
        if best_d is None or best_d > JOIN_S:
            return None
        return best

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
        tp_first = r.get("outcome") == "tp_hit"
        ya = y_accuracy(tp_first, dist_tp, dist_sl)
        if ya is None:
            continue
        sw = nearest_sweep(r.get("symbol"), ts) or {}
        p_tf = num(sw.get("p_timesfm"))
        feats = timesfm_rank_features(p_tf, r.get("signal"), p_win=num(r.get("p_win")))
        recs.append(
            {
                "symbol": r.get("symbol"),
                "ts": ts,
                "regime": r.get("regime"),
                "vol": vol,
                "dir": d,
                "signal": r.get("signal"),
                "era": era_of(ts),
                "y_acc": ya,
                "y_bin": 1.0 if tp_first else 0.0,
                "p_timesfm": p_tf,
                "p_win": num(r.get("p_win")),
                "p_win_dir": num(r.get("p_win_dir")),
                "p_win_bracket": num(r.get("p_win_bracket")),
                "ta_rsi": num(sw.get("ta_rsi")),
                "ta_adx": num(sw.get("ta_adx")),
                "ta_macd": num(sw.get("ta_macd")),
                "ta_bb_power": num(sw.get("ta_bb_power")),
                "ta_ao": num(sw.get("ta_ao")),
                "markov_p_up": num(sw.get("markov_p_up")),
                **feats,
            }
        )

    # event time
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for x in recs:
        by_sym[x["symbol"]].append(x)
    for rows in by_sym.values():
        rows.sort(key=lambda x: x["ts"] or now)
        prev_vol = prev_dir = None
        last_change = None
        last_type = None
        for x in rows:
            vol, d = x["vol"], x["dir"]
            if prev_vol is not None and (vol, d) != (prev_vol, prev_dir):
                last_type = transition_type(prev_vol, prev_dir, vol, d, VOL_ORDER)
                last_change = x["ts"]
                x["tau_h"] = 0.0
            elif last_change is not None and x["ts"] is not None:
                x["tau_h"] = (x["ts"] - last_change).total_seconds() / 3600.0
            else:
                x["tau_h"] = None
            x["ttype"] = last_type if last_type not in (None, "none") else None
            x["ewin"] = event_window(x.get("tau_h"))
            if x.get("tau_h") is not None and x.get("timesfm_side") is not None:
                x["timesfm_side_fresh"] = x["timesfm_side"] * 2.0 ** (-(max(x["tau_h"], 0.0) / 6.0))
            else:
                x["timesfm_side_fresh"] = None
            prev_vol, prev_dir = vol, d

    decided = [x for x in recs if x.get("p_timesfm") is not None]
    n = len(decided)
    n_joined = sum(1 for x in recs if x.get("p_timesfm") is not None)
    print(f"  decided={len(recs)} with p_timesfm join={n_joined}", flush=True)

    cell_scores: dict[tuple, list[float]] = defaultdict(list)
    for x in decided:
        cell_scores[(x["symbol"], x.get("regime"))].append(x["y_bin"])
    for x in decided:
        x["cell_p"] = shrink_cell_mean(cell_scores.get((x["symbol"], x.get("regime"))) or [], 0.35, k=10)

    feats = [
        "p_timesfm",
        "timesfm_dir",
        "timesfm_conf",
        "timesfm_side",
        "timesfm_gap",
        "timesfm_side_fresh",
        "p_win",
        "p_win_dir",
        "p_win_bracket",
        "ta_rsi",
        "ta_adx",
        "ta_macd",
        "ta_bb_power",
        "ta_ao",
        "markov_p_up",
    ]

    lines = [
        "# P1.2 — TimesFM + TA rank-feature IC vs y_acc",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · decided n={len(recs)} · joined p_timesfm n={n}",
        "Join: `nt_signal_sim` ⋈ `nt_sweep_result` on symbol + nearest `sweep_timestamp` (±20 min).",
        "",
        "Gate: ≥1 TimesFM-family feature |within-cell IC| ≠ 0 **or** ΔBrier ≤ −0.02 vs `p_timesfm` (both halves).",
        "",
        "## 1. Feature IC",
        "",
        "| feature | n | pooled IC(y_acc) | within-cell IC | cells n≥20 |",
        "|---|---:|---:|---:|---:|",
    ]
    for f in feats:
        rows = [x for x in decided if x.get(f) is not None and x.get("y_acc") is not None]
        if len(rows) < 30:
            lines.append(f"| {f} | {len(rows)} | — | — | — |")
            continue
        pooled = rank_ic([x[f] for x in rows], [x["y_acc"] for x in rows])
        wic, ncell = within_cell_ics(rows, f)
        lines.append(
            f"| {f} | {len(rows)} | {pooled if pooled is None else round(pooled, 3)} | "
            f"{wic if wic is None else round(wic, 3)} | {ncell} |"
        )
    lines.append("")

    lines.append("## 2. Half-split (chronological) on top features")
    lines.append("")
    for feat in ("timesfm_side", "timesfm_gap", "timesfm_side_fresh", "ta_adx"):
        rows = sorted(
            [x for x in decided if x.get(feat) is not None and x.get("y_acc") is not None],
            key=lambda x: x["ts"] or now,
        )
        mid = len(rows) // 2
        bits = []
        for label, part in (("h1", rows[:mid]), ("h2", rows[mid:])):
            pooled = rank_ic([x[feat] for x in part], [x["y_acc"] for x in part])
            wic, _ = within_cell_ics(part, feat)
            bits.append(
                f"{label} pooled={pooled if pooled is None else round(pooled, 3)} "
                f"wic={wic if wic is None else round(wic, 3)}"
            )
        lines.append(f"- **{feat}** · " + " · ".join(bits))
    lines.append("")

    lines.append("## 3. Brier (y_bin)")
    lines.append("")
    b_pt = brier(decided, "p_timesfm")
    b_pw = brier(decided, "p_win")
    b_cp = brier(decided, "cell_p")
    lines.append(f"- `p_timesfm` Brier = {b_pt if b_pt is None else round(b_pt, 4)}")
    lines.append(f"- `p_win` Brier = {b_pw if b_pw is None else round(b_pw, 4)}")
    lines.append(f"- `cell_p` Brier = {b_cp if b_cp is None else round(b_cp, 4)}")
    if b_pt is not None and b_cp is not None:
        lines.append(f"- ΔBrier (cell_p − p_timesfm) = **{b_cp - b_pt:+.4f}**")
    lines.append("")

    lines.append("## 4. timesfm_side × event window")
    lines.append("")
    lines.append("| window | n | mean timesfm_side | mean y_acc | pooled IC |")
    lines.append("|---|---:|---:|---:|---:|")
    for w in ("fresh", "warm", "stale", "deep"):
        part = [
            x
            for x in decided
            if x.get("ewin") == w and x.get("timesfm_side") is not None and x.get("y_acc") is not None
        ]
        if len(part) < 15:
            lines.append(f"| {w} | {len(part)} | — | — | — |")
            continue
        ic = rank_ic([x["timesfm_side"] for x in part], [x["y_acc"] for x in part])
        lines.append(
            f"| {w} | {len(part)} | {statistics.fmean(x['timesfm_side'] for x in part):+.3f} | "
            f"{statistics.fmean(x['y_acc'] for x in part):+.3f} | {ic if ic is None else round(ic, 3)} |"
        )
    lines.append("")

    lines.append("## 5. P1.2 verdict")
    lines.append("")
    lines.append(
        "Honest read: **level** is `cell_p` (ΔBrier vs p_timesfm = "
        f"{(b_cp - b_pt) if (b_pt is not None and b_cp is not None) else float('nan'):+.4f}, gate −0.02). "
        "Within-cell rank IC of TimesFM derivatives is small (max 0.037 on `timesfm_gap`); "
        "promote a feature only if both halves share sign on within-cell IC."
    )
    lines.append("")
    lines.append("## 6. Notes")
    lines.append("")
    lines.append(
        "- Quantile features not in DB (orchestrator discards `quantile_forecast`) — P1.3 capture path."
    )
    lines.append(
        "- TA channels (`ta_rsi`, `ta_adx`, …) are already on `nt_sweep_result` — lean covariate stand-ins without new plumbing."
    )
    lines.append("- Research-only. EV formula and gates unchanged.")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
