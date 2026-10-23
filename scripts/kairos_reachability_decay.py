"""
kairos_reachability_decay.py — is the cell's failure non-stationarity?

Scope: `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` §3.3 result.

`cell_conditioning.py` found the reachability cell has real in-sample signal
(AUC 0.7256) that does not transfer (out-of-fold 0.4994). Two explanations:

  (a) non-stationarity — the cell's rate moves, so an all-history estimate
      averages over regimes that no longer apply. Fix: a trailing window.
  (b) overfitting — no real signal; the in-sample number is memorised noise.

Test 1 (window sweep) asks whether recency recovers transfer.
Test 2 (era stability) asks whether the rate actually moves.

Output: worklog/kairos/reports/YYYYMMDD_reachability_decay.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_reachability_decay.py
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
from regime_platform.sweep_orchestrator import reachability_decay as rd  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
LOOKBACK_DAYS = int(os.environ.get("RD_LOOKBACK_DAYS", "120"))
N_FOLDS = int(os.environ.get("RD_FOLDS", "5"))
N_ERAS = int(os.environ.get("RD_ERAS", "6"))
WINDOWS = (7.0, 14.0, 30.0, 60.0, None)


def _ts_seconds(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def fetch(since: str) -> list[dict]:
    rows, off = [], 0
    with httpx.Client(timeout=180) as c:
        while True:
            r = c.get(f"{U}/rest/v1/nt_signal_sim", headers=H, params={
                "select": "symbol,signal,ts,entry_price,take_profit,atr,regime,"
                          "regime_conf,p_win,outcome",
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
    out = []
    diag = {"fetched": len(rows), "no_ts": 0, "no_atr": 0, "no_cell": 0, "ok": 0}
    for r in rows:
        ts = _ts_seconds(r.get("ts"))
        if ts is None:
            diag["no_ts"] += 1
            continue
        if r.get("atr") is None:
            diag["no_atr"] += 1
            continue
        if cc.cell_key(r) is None:
            diag["no_cell"] += 1
            continue
        r = dict(r)
        r["_ts"] = ts
        r["realised"] = r.get("outcome") == "tp_hit"
        r["_cell"] = cc.cell_key(r)
        out.append(r)
        diag["ok"] += 1
    out.sort(key=lambda x: x["_ts"])
    return out, diag


def _fmt(v, spec=".4f", plus=False) -> str:
    if v is None:
        return "—"
    try:
        return f"{v:{'+' if plus else ''}{spec}}"
    except (TypeError, ValueError):
        return str(v)


def render(rows: list[dict], diag: dict, sweep: list[dict],
           stab: dict, rates: list[dict], counts: list[dict], now: datetime) -> str:
    L: list[str] = []
    L.append("# Is the reachability cell's failure non-stationarity?")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"{N_FOLDS}-fold expanding walk-forward · light sweep, resolved rows only")
    L.append("")
    L.append("`cell_conditioning.py` measured the cell at in-sample AUC 0.7256 but "
             "out-of-fold 0.4994 — real signal that does not transfer. This separates "
             "the two explanations: **(a) non-stationarity** (the rate moves, so fit it "
             "on a trailing window) vs **(b) overfitting** (no signal; the in-sample "
             "number is memorised noise).")
    L.append("")

    # ── Test 1 ──
    L.append("## Test 1 — does recency recover transfer?")
    L.append("")
    L.append(f"**{rd.recency_verdict(sweep)}**")
    L.append("")
    L.append("| training lookback | out-of-fold AUC | Brier | BSS | mean train n | folds > 0.5 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for r in sweep:
        label = "all history (baseline)" if r["window_days"] is None else f"{r['window_days']:.0f}d"
        if not r.get("n"):
            L.append(f"| {label} | _no scored folds_ | | | | |")
            continue
        fs = cc.fold_stats(r.get("per_fold") or [])
        L.append(f"| {label} | {_fmt(r['auc'])} | {_fmt(r['brier'])} | "
                 f"{_fmt(r['bss'], '.4f', plus=True)} | {r.get('mean_train_n')} | "
                 f"{fs.get('n_above_half')}/{fs.get('n_folds')} |")
    L.append("")
    L.append("Same folds, same shrinkage, same population — only the training lookback "
             "changes. So a difference between rows is attributable to recency alone.")
    L.append("")

    # ── Test 2 ──
    L.append("## Test 2 — is the rate actually unstable?")
    L.append("")
    L.append(f"**{rd.stability_verdict(stab)}**")
    L.append("")
    L.append("| metric | value | meaning |")
    L.append("|---|---:|---|")
    L.append(f"| cells scored | {stab.get('n_cells')} | present in ≥1 era with adequate n |")
    L.append(f"| mean abs drift | {_fmt(stab.get('mean_abs_drift'))} | avg \\|rate(e+1) − rate(e)\\| |")
    L.append(f"| mean range | {_fmt(stab.get('mean_range'))} | avg (max − min) across eras |")
    L.append(f"| sign-flip share | {_fmt(stab.get('sign_flip_frac'))} | consecutive pairs crossing 0.5 |")
    L.append(f"| split-half corr | {_fmt(stab.get('split_half_corr'))} | first-half vs second-half rate |")
    L.append(f"| consecutive pairs | {stab.get('n_consecutive_pairs')} | denominator for drift/flips |")
    L.append("")
    L.append("A stable rate that still fails to transfer points at overfitting from a "
             "different direction; a rate that swings is direct evidence of "
             "non-stationarity. Test 1 says whether the fix works; Test 2 says whether "
             "the diagnosis is right.")
    L.append("")

    # ── Era detail ──
    L.append("## Era-by-era cell rates")
    L.append("")
    L.append(f"{N_ERAS} equal-*time* chunks of the history. Cells below "
             f"`min_n={cc.MIN_CELL_N}` in an era are omitted from that era.")
    L.append("")
    if rates:
        # show the largest cells (by median count) and their rate trajectory
        keys = {}
        for c in counts:
            for k, n in c.items():
                keys.setdefault(k, []).append(n)
        top = sorted(keys, key=lambda k: -sorted(keys[k])[len(keys[k]) // 2])[:12]
        L.append("| cell | " + " | ".join(f"era{i+1}" for i in range(len(rates))) + " |")
        L.append("|---|" + "---:|" * len(rates))
        for k in top:
            cells = []
            for r in rates:
                v = r.get(k)
                cells.append("—" if v is None else f"{v:.2f}")
            sym, vol, bkt = k if isinstance(k, tuple) and len(k) == 3 else (str(k), "", "")
            L.append(f"| {sym} · {vol} · {bkt} | " + " | ".join(cells) + " |")
        L.append("")
        L.append("_Twelve largest cells by median era count. A row that stays flat is "
                 "stable; a row that walks is the non-stationarity being tested._")
        L.append("")
    else:
        L.append("_No era rates computed — insufficient history._")
        L.append("")

    # ── Coverage ──
    L.append("## Coverage")
    L.append("")
    L.append(f"- fetched: **{diag['fetched']}** resolved light signals")
    L.append(f"- usable: **{diag['ok']}** ({100*diag['ok']/max(1,diag['fetched']):.1f}%)")
    L.append(f"- dropped: {diag['no_ts']} no timestamp, {diag['no_atr']} no ATR, "
             f"{diag['no_cell']} unbucketable")
    if rows:
        span = (rows[-1]["_ts"] - rows[0]["_ts"]) / rd.DAY
        L.append(f"- span: **{span:.0f} days** "
                 f"({datetime.fromtimestamp(rows[0]['_ts'], timezone.utc):%Y-%m-%d} → "
                 f"{datetime.fromtimestamp(rows[-1]['_ts'], timezone.utc):%Y-%m-%d})")
    L.append("")

    # ── Caveats ──
    L.append("## Caveats")
    L.append("")
    if rows:
        span_days = (rows[-1]["_ts"] - rows[0]["_ts"]) / rd.DAY
        if span_days < LOOKBACK_DAYS * 0.7:
            L.append(f"- ⚠ **The available history is only {span_days:.0f} days**, not the "
                     f"{LOOKBACK_DAYS}d requested — `nt_signal_sim` does not reach further "
                     f"back for resolved light rows. Every window ≥ the span is therefore "
                     f"identical to 'all history' (see the train_n column), and each era "
                     f"is only ~{span_days/N_ERAS:.0f} days. **This bounds the test**: a "
                     f"trailing window cannot be meaningfully shorter than an era without "
                     f"fitting on a single regime sample.")
            L.append("")
    L.append("- **Resolved rows only** (`tp_hit`/`sl_hit`). Expired has no TP/SL verdict "
             "and is excluded — a selected sub-population, and the biggest remaining "
             "threat to this measurement (audit C2/R6).")
    L.append("- **A short window trades bias for variance.** A 7d window may be *more* "
             "current but fits cell rates on very few rows, so a low AUC there is not "
             "evidence against recency — check `mean train n` beside it.")
    L.append("- **The era split is equal-*time*, not equal-count**, because the question "
             "is whether one calendar period predicts the next.")
    L.append("- **`split_half_corr` needs ≥3 cells** with varying rates to be defined; "
             "`None` means it could not be computed, not zero correlation.")
    L.append("- **Rows lacking stored ATR are dropped**, not proxied — avoids the audit's "
             "R4 look-ahead.")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- If Test 1 finds a working window **and** Test 2 finds drift, the fix is a "
             "decayed/rolling cell rate in `nt_bracket_reachability`.")
    L.append("- If Test 2 finds the rate **stable** but Test 1 finds no window works, the "
             "cell is not the model and the thesis needs replacing, not tuning.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    print(f"fetching {LOOKBACK_DAYS}d…")
    raw = fetch(since)
    print(f"  fetched: {len(raw)}")
    rows, diag = build(raw)
    print(f"  usable: {len(rows)}  {diag}")
    if not rows:
        print("no usable rows — aborting")
        return

    print("test 1: window sweep…")
    sweep = rd.window_sweep(rows, cc.SPECS["cell"], windows=WINDOWS, n_folds=N_FOLDS)
    for r in sweep:
        w = "all" if r["window_days"] is None else f"{r['window_days']:.0f}d"
        print(f"  {w:>6s}  auc={r.get('auc')}  train_n={r.get('mean_train_n')}")

    print("test 2: era stability…")
    rates, counts = rd.era_rates(rows, n_eras=N_ERAS)
    stab = rd.stability(rates, counts)
    print(f"  {stab}")

    now = datetime.now(timezone.utc)
    text = render(rows, diag, sweep, stab, rates, counts, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_reachability_decay.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
