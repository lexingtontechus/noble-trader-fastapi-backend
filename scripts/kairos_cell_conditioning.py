"""
kairos_cell_conditioning.py — does conditioning the reachability cell help?
═══════════════════════════════════════════════════════════════════════════════

Scope: `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` §3.3.

The falsifiable test of the thesis: if the geometry cell dominates
``P(reach TP before SL)``, then conditioning it on a direction signal should NOT
improve out-of-fold AUC. If direction does add, the thesis is incomplete and
direction earns a place as a conditioning term.

Output: worklog/kairos/reports/YYYYMMDD_cell_conditioning.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_cell_conditioning.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator import cell_conditioning as cc  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
LOOKBACK_DAYS = int(os.environ.get("CELL_LOOKBACK_DAYS", "60"))
N_FOLDS = int(os.environ.get("CELL_FOLDS", "5"))


def fetch(since: str) -> list[dict]:
    rows, off = [], 0
    with httpx.Client(timeout=180) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H, params={
                "select": "symbol,signal,ts,entry_price,take_profit,atr,brick_size,"
                          "regime,regime_conf,p_win,outcome,drift_1h_pct",
                "source": "eq.sweep-light",
                "signal": "in.(buy,sell)",
                "outcome": "in.(tp_hit,sl_hit)",
                "ts": f"gte.{since}",
                "order": "ts.asc",
                "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows.extend(b)
            if len(b) < 1000:
                break
            off += len(b)
    return rows


def build(rows: list[dict]) -> tuple[list[dict], dict]:
    """Attach ``realised`` and keep only rows that can enter a cell."""
    out = []
    diag = {"fetched": len(rows), "no_atr": 0, "no_prices": 0, "no_cell": 0, "ok": 0}
    for r in rows:
        if r.get("atr") is None:
            diag["no_atr"] += 1
            continue
        if r.get("entry_price") is None or r.get("take_profit") is None:
            diag["no_prices"] += 1
            continue
        if cc.cell_key(r) is None:
            diag["no_cell"] += 1
            continue
        r = dict(r)
        r["realised"] = r.get("outcome") == "tp_hit"
        out.append(r)
        diag["ok"] += 1
    return out, diag


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def render(rows: list[dict], diag: dict, results: list[dict], now: datetime) -> str:
    L: list[str] = []
    base = next((d for d in results if d["name"] == "cell"), {})
    L.append("# Does conditioning the reachability cell improve out-of-fold AUC?")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"{N_FOLDS}-fold expanding walk-forward · light sweep, resolved rows only")
    L.append("")
    L.append("The falsifiable test of "
             "`docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` §3.3. "
             "Every cell rate is estimated on **training rows only**; each fold is scored "
             "with rates frozen before it. Adding conditioning always improves in-sample "
             "fit, so only out-of-fold AUC is reported.")
    L.append("")

    # ── Verdict ──
    L.append("## Verdict")
    L.append("")
    if base.get("auc") is not None:
        fs = cc.fold_stats(base.get("per_fold") or [])
        L.append(f"**Cell alone: AUC {base['auc']:.4f}** "
                 f"(n={base.get('n')}, base rate {base.get('base_rate')}, "
                 f"fold spread ±{fs.get('sd')})")
        L.append("")
        L.append("| conditioning added | AUC | Δ vs cell alone | read |")
        L.append("|---|---:|---:|---|")
        for d in results:
            if d["name"] == "cell" or d.get("auc") is None:
                continue
            gain = d["auc"] - base["auc"]
            L.append(f"| `{d['name']}` | {d['auc']:.4f} | {_fmt(gain, '.4f', plus=True)} | "
                     f"{cc.verdict(base, d)} |")
        L.append("")
    else:
        L.append("_No scored folds — see coverage below._")
        L.append("")

    # ── Full table ──
    L.append("## All models (walk-forward, out-of-fold)")
    L.append("")
    L.append("| model | n | AUC (oof) | AUC (in-sample ceiling) | Brier | REL | RES | BSS |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for d in results:
        if d.get("n"):
            L.append(f"| `{d['name']}` | {d['n']} | {_fmt(d['auc'])} | "
                     f"{_fmt(d.get('in_sample_auc'))} | {_fmt(d['brier'])} | "
                     f"{_fmt(d['rel'])} | {_fmt(d['res'])} | {_fmt(d['bss'], '.4f', plus=True)} |")
    L.append("")
    L.append("`REL` lower is better, `RES` higher is better, `BSS > 0` beats the base rate. "
             "The **in-sample ceiling** is fit and scored on the same rows — it is an upper "
             "bound, never a result. It exists to separate *no signal* from *no transferable "
             "signal*.")
    L.append("")
    L.append("### Did the signal transfer, or was there none?")
    L.append("")
    for d in results:
        if d.get("n"):
            L.append(f"- `{d['name']}` — {cc.transfer_verdict(d)}")
    L.append("")

    # ── Per-fold stability ──
    L.append("## Per-fold AUC (stability)")
    L.append("")
    L.append("A gain that only appears in one fold is noise. The bar is a gain that "
             "holds across folds.")
    L.append("")
    names = [d["name"] for d in results if d.get("per_fold")]
    L.append("| model | " + " | ".join(f"f{i+1}" for i in range(N_FOLDS)) + " |")
    L.append("|---|" + "---:|" * N_FOLDS)
    for d in results:
        if not d.get("per_fold"):
            continue
        cells = [_fmt(f.get("auc")) for f in d["per_fold"]]
        L.append(f"| `{d['name']}` | " + " | ".join(cells) + " |")
    L.append("")

    # ── Cell composition ──
    L.append("## Coverage")
    L.append("")
    L.append(f"- fetched: **{diag['fetched']}** resolved light signals")
    L.append(f"- usable (ATR + prices + bucketable): **{diag['ok']}** "
             f"({100*diag['ok']/max(1,diag['fetched']):.1f}%)")
    L.append(f"- dropped: {diag['no_atr']} no ATR, {diag['no_prices']} no prices, "
             f"{diag['no_cell']} unbucketable")
    L.append("")
    if rows:
        from collections import Counter
        buckets = Counter(cc.cell_key(r)[2] for r in rows if cc.cell_key(r))
        syms = Counter(cc.cell_key(r)[0] for r in rows if cc.cell_key(r))
        L.append(f"- distinct cells: **{len({cc.cell_key(r) for r in rows if cc.cell_key(r)})}** "
                 f"across {len(syms)} symbols")
        L.append(f"- bucket mix: " + ", ".join(f"{b}={n}" for b, n in
                                               sorted(buckets.items(), key=lambda x: -x[1])))
        top_b = buckets.most_common(1)[0]
        top_pct = 100 * top_b[1] / len(rows)
        L.append(f"- **concentration: `{top_b[0]}` holds {top_pct:.1f}% of all rows** "
                 f"({top_b[1]} of {len(rows)})")
        L.append("")
        if top_pct > 50:
            L.append(f"> **The bucket space is degenerate.** One bucket holds "
                     f"{top_pct:.0f}% of the population, so a `(symbol, vol_tier, bucket)` "
                     f"key is barely finer than `(symbol, vol_tier)` — the "
                     f"distance/ATR dimension is not doing the work the thesis assumes. "
                     f"This is the audit's G2 in data: brackets are built in %-price "
                     f"space, so almost everything lands in the widest ATR bucket. A "
                     f"bucket that holds three-quarters of the data cannot discriminate.")
            L.append("")

        # ── The cell rate table (the model, in full) ──
        L.append("## The cell table — per symbol × vol_tier × bucket")
        L.append("")
        L.append("This is the model under the thesis: `P(tp_hit | cell)`. Rows below "
                 f"`min_n={cc.MIN_CELL_N}` are unreliable and back off in the ladder.")
        L.append("")
        L.append("| symbol | vol_tier | bucket | n | P(tp_hit) |")
        L.append("|---|---|---|---:|---:|")
        agg: dict[tuple, list[int]] = {}
        for r in rows:
            k = cc.cell_key(r)
            if not k:
                continue
            a = agg.setdefault(k, [0, 0])
            a[0] += 1
            a[1] += 1 if r["realised"] else 0
        for k, (n, w) in sorted(agg.items(), key=lambda x: -x[1][0]):
            L.append(f"| {k[0]} | {k[1]} | {k[2]} | {n} | {w/n:.3f} |")
        L.append("")

    # ── Caveats ──
    L.append("## Caveats")
    L.append("")
    L.append("- **Resolved rows only** (`tp_hit`/`sl_hit`). Expired is excluded because "
             "it has no TP/SL verdict; that is a selected sub-population and the "
             "single biggest remaining threat to this measurement (audit C2/R6). A "
             "first-touch resolver over the full population is the fix.")
    L.append("- **`atr` is the stored capture-time ATR** where present. Rows predating "
             "the `atr` column are dropped, not proxied — so this cannot inherit the "
             "audit's R4 look-ahead (proxy ATR uses *current* ATR for historical rows).")
    L.append("- **Cell rates are fit out-of-fold** but the *bucket definition* "
             "(`distance/ATR` thresholds) is fixed a priori, not tuned here — so there "
             "is no threshold-selection leak.")
    L.append("- **Shrinkage α is fixed** at the module default across all models, so it "
             "cannot favour one. It is not tuned on the evaluation data.")
    L.append("- **5 folds, expanding window.** Per-fold AUCs are shown because a pooled "
             "number can hide a gain that lives in one fold.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"fetching {LOOKBACK_DAYS}d…")
    raw = fetch(since)
    print(f"  fetched: {len(raw)}")
    rows, diag = build(raw)
    print(f"  usable: {len(rows)}  {diag}")
    results = cc.compare(rows, n_folds=N_FOLDS)
    for d in results:
        print(f"  {d['name']:16s} n={d.get('n')} auc={d.get('auc')}")
    now = datetime.now(timezone.utc)
    text = render(rows, diag, results, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_cell_conditioning.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
