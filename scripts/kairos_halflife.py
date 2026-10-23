"""
kairos_halflife.py — T2 event-time edge half-life (read-only).

Scope: worklog/kairos/scopes/20260927_P0_HALFLIFE_T2_SCOPE.md
Output: worklog/kairos/reports/20260927_halflife_t2.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_halflife.py
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
    event_window,
    exp_decay_fit,
    halflife_null,
    transition_type,
    y_accuracy,
)

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260927_halflife_t2.md"

VOL_ORDER = ("low_vol", "med_low_vol", "med_high_vol", "high_vol")
TAU_EDGES = (0.0, 1.0, 3.0, 6.0, 12.0, 24.0, 48.0, math.inf)
MIN_BIN_N = 30


def fetch(table: str, params: dict) -> list[dict]:
    """Exact count then parallel offset pages (PostgREST caps limit at 1000)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
    if not total:
        # fallback: serial until short page
        rows = list(head)
        off = 1000
        while True:
            b, _ = page(off)
            rows.extend(b)
            if len(b) < 1000:
                break
            off += 1000
        return rows
    if total <= 1000:
        return head
    rows = list(head)
    offs = list(range(1000, total, 1000))
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(page, o) for o in offs]
        for fut in as_completed(futs):
            b, _ = fut.result()
            rows.extend(b)
    rows.sort(key=lambda r: r.get("ts") or "")
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


def bin_means(tau: list[float], y: list[float]) -> list[tuple[float, float, int, float]]:
    """(lo, hi, n, mean_y) per TAU_EDGES cell."""
    out = []
    for i in range(len(TAU_EDGES) - 1):
        lo, hi = TAU_EDGES[i], TAU_EDGES[i + 1]
        vals = [v for t, v in zip(tau, y) if lo <= t < hi]
        out.append((lo, hi, len(vals), statistics.fmean(vals) if vals else float("nan")))
    return out


def fit_type(
    tau: list[float],
    y: list[float],
    label: str,
    lines: list[str],
) -> tuple[bool, float | None]:
    """Write fit table + T2 half-split. Returns (supported, h)."""
    fit = exp_decay_fit(tau, y)
    null = halflife_null(tau, y, n_draws=200, seed=20260927)
    n = len(tau)
    if fit is None:
        lines.append(f"### {label}")
        lines.append("")
        lines.append(f"n={n} — too few points for a fit.")
        lines.append("")
        return False, None

    # chronological half-split (caller passes observation order)
    mid = n // 2
    f1 = exp_decay_fit(tau[:mid], y[:mid])
    f2 = exp_decay_fit(tau[mid:], y[mid:])
    b_ok = f1 is not None and f2 is not None and f1["b"] > 0 and f2["b"] > 0
    h_ok = (
        f1 is not None
        and f2 is not None
        and f1["h"] > 0
        and f2["h"] > 0
        and (f1["h"] / f2["h"] <= 4.0)
        and (f2["h"] / f1["h"] <= 4.0)
    )
    p_b = null.get("p_b")
    sig = p_b is not None and p_b < 0.05
    supported = bool(sig and b_ok and h_ok and fit["b"] > 0)

    lines.append(f"### {label}")
    lines.append("")
    lines.append("| field | value |")
    lines.append("|---|---:|")
    lines.append(f"| n | {n} |")
    lines.append(f"| a (deep) | {fit['a']:+.4f} |")
    lines.append(f"| b (fresh lift) | {fit['b']:+.4f} |")
    lines.append(f"| h (hours) | {fit['h']:.2f} |")
    lines.append(f"| p_b (one-sided null) | {p_b if p_b is None else round(p_b, 4)} |")
    if f1 and f2:
        lines.append(f"| half1 b / h | {f1['b']:+.4f} / {f1['h']:.2f} |")
        lines.append(f"| half2 b / h | {f2['b']:+.4f} / {f2['h']:.2f} |")
    lines.append(f"| half-split b>0 both | {b_ok} |")
    lines.append(f"| half-split h stable (≤4×) | {h_ok} |")
    lines.append("")
    verdict = "supported" if supported else ("underpowered" if n < 80 else "not_supported")
    if fit["b"] <= 0 and sig:
        verdict = "reversed"
    lines.append(f"→ T2 [{label}]: **{verdict}**")
    lines.append("")
    return supported, fit["h"]


def main() -> None:
    now = datetime.now(timezone.utc)
    cols = (
        "symbol,ts,outcome,regime,tp_distance_atr,sl_distance_atr,tp_bricks,sl_bricks"
    )
    print("fetch…", flush=True)
    raw = fetch(
        "nt_signal_sim",
        {
            "select": cols,
            "source": "eq.sweep-light",
            "signal": "in.(buy,sell)",
            "ts": "gte.2026-08-15T00:00:00Z",
            "order": "ts.asc",
        },
    )
    print(f"  {len(raw)} rows — tagging event time…", flush=True)

    recs = []
    for r in raw:
        ts = parse_ts(r.get("ts"))
        vol, d = split_cell(r.get("regime"))
        tp_b, sl_b = num(r.get("tp_bricks")), num(r.get("sl_bricks"))
        tp_a, sl_a = num(r.get("tp_distance_atr")), num(r.get("sl_distance_atr"))
        dist_tp = tp_a if tp_a and tp_a > 0 else tp_b
        dist_sl = sl_a if sl_a and sl_a > 0 else sl_b
        recs.append(
            {
                "symbol": r.get("symbol"),
                "ts": ts,
                "outcome": r.get("outcome"),
                "vol": vol,
                "dir": d,
                "dist_tp": dist_tp,
                "dist_sl": dist_sl,
                "era": era_of(ts),
            }
        )

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for x in recs:
        by_sym[x["symbol"]].append(x)
    for rows in by_sym.values():
        rows.sort(key=lambda x: x["ts"] or now)

    # event time + last transition type (carry type until next flip)
    for _sym, rows in by_sym.items():
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
            prev_vol, prev_dir = vol, d

    # decided episodes (chronological per symbol already)
    decided = []
    for x in recs:
        if x["outcome"] not in ("tp_hit", "sl_hit"):
            continue
        ya = y_accuracy(x["outcome"] == "tp_hit", x["dist_tp"], x["dist_sl"])
        if ya is None:
            continue
        if x.get("tau_h") is None:
            continue
        x["y_acc"] = ya
        decided.append(x)

    lines = [
        "# T2 — event-time edge half-life",
        "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC · read-only · `kairos_rank.exp_decay_fit`",
        f"Method: y_acc ≈ a + b·2^(−τ/h) · circular-shift null · half-split stability",
        f"Rows {len(recs)} · decided with τ {len(decided)}",
        "",
    ]

    # T1 refresh
    fr = [x["y_acc"] for x in decided if x.get("ewin") == "fresh"]
    dp = [x["y_acc"] for x in decided if x.get("ewin") == "deep"]
    if fr and dp:
        dlt = statistics.fmean(fr) - statistics.fmean(dp)
        lines.append(f"**T1 refresh:** fresh − deep y_acc = {dlt:+.3f} (n_fresh={len(fr)} n_deep={len(dp)})")
        lines.append("")

    # binned by τ
    lines.append("## 1. Binned y_acc by τ (all types)")
    lines.append("")
    lines.append("| τ (h) | n | mean y_acc |")
    lines.append("|---|---:|---:|")
    tau_all = [x["tau_h"] for x in decided]
    y_all = [x["y_acc"] for x in decided]
    for lo, hi, n, m in bin_means(tau_all, y_all):
        hi_s = "∞" if math.isinf(hi) else f"{hi:g}"
        lines.append(f"| {lo:g}–{hi_s} | {n} | {m if n else float('nan'):.3f} |")
    lines.append("")

    # fits by type (chronological order for half-split)
    lines.append("## 2. Half-life fits by transition type")
    lines.append("")
    supported_any = False
    h_by_type: dict[str, float] = {}
    by_type: dict[str, list[dict]] = defaultdict(list)
    for x in decided:
        if x.get("ttype"):
            by_type[x["ttype"]].append(x)

    for t in sorted(by_type):
        rows = sorted(by_type[t], key=lambda x: x["ts"] or now)
        tau = [x["tau_h"] for x in rows]
        y = [x["y_acc"] for x in rows]
        print(f"  fit {t} n={len(rows)}…", flush=True)
        ok, h = fit_type(tau, y, t, lines)
        supported_any = supported_any or ok
        if h is not None:
            h_by_type[t] = h

    # pooled
    rows = sorted(decided, key=lambda x: x["ts"] or now)
    ok_pool, h_pool = fit_type(
        [x["tau_h"] for x in rows], [x["y_acc"] for x in rows], "pooled", lines
    )
    if ok_pool:
        supported_any = True

    # era note
    lines.append("## 3. Era mass (diagnostic)")
    lines.append("")
    lines.append("| era | n decided |")
    lines.append("|---|---:|")
    era_n: dict[str, int] = defaultdict(int)
    for x in decided:
        era_n[x["era"]] += 1
    for e in ("pre_debounce", "debounce_to_atr", "atr_to_resolver", "post_resolver"):
        lines.append(f"| {e} | {era_n.get(e, 0)} |")
    lines.append("")

    lines.append("## 4. T2 verdict")
    lines.append("")
    if supported_any:
        lines.append(
            f"**T2 supported** for at least one stratum. h by type: "
            + ", ".join(f"{k}={v:.1f}h" for k, v in sorted(h_by_type.items()))
            + (f"; pooled={h_pool:.1f}h" if h_pool else "")
        )
        lines.append("Research-only (P0.3). Do not wire into `rank_score` until post-ATR power.")
    else:
        lines.append("**T2 not supported** (or underpowered) at this sample.")
        lines.append("Keep coarse `fresh`/`deep` in `rank_score`; kill half-life as a product knob (P0.4).")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
