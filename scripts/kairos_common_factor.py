"""
kairos_common_factor.py — is the cell rate cross-sectional or market-wide?

Scope: `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md`
(§3.3 follow-up).

`reachability_decay.py` found the cell rate drifts hard between eras (mean |Δ| 0.20)
yet no trailing window recovers transfer. The explanation that fits both is a
**shared market-wide factor**: all cells move together, so the ordering between
them is stable-but-uninformative and refitting re-estimates the same factor.

This measures the variance split (cell / time / residual) and then asks the
decision-relevant question: does ranking cells by their **factor-removed** rate
predict better than ranking by the raw rate?

Output: worklog/kairos/reports/YYYYMMDD_common_factor.md

Run:  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_common_factor.py
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
from regime_platform.sweep_orchestrator import common_factor as cf  # noqa: E402

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}"}
LOOKBACK_DAYS = int(os.environ.get("CF_LOOKBACK_DAYS", "60"))
WINDOW_DAYS = float(os.environ.get("CF_WINDOW_DAYS", "3"))
MIN_N = int(os.environ.get("CF_MIN_N", "20"))
N_FOLDS = int(os.environ.get("CF_FOLDS", "4"))


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
                "select": "symbol,signal,ts,entry_price,take_profit,atr,regime,outcome",
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
    diag = {"fetched": len(rows), "no_atr": 0, "no_cell": 0, "ok": 0}
    for r in rows:
        t = _ts_seconds(r.get("ts"))
        if t is None or r.get("atr") is None:
            diag["no_atr"] += 1
            continue
        k = cc.cell_key(r)
        if k is None:
            diag["no_cell"] += 1
            continue
        out.append({"_ts": t, "_cell": k, "realised": r.get("outcome") == "tp_hit",
                    "symbol": r.get("symbol"), "regime": r.get("regime")})
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


def render(rows: list[dict], diag: dict, m: dict, d: dict,
           pred: dict, now: datetime) -> str:
    L: list[str] = []
    L.append("# Is the reachability cell cross-sectional or market-wide?")
    L.append("")
    L.append(f"Generated {now:%Y-%m-%d %H:%M} UTC · lookback {LOOKBACK_DAYS}d · "
             f"{WINDOW_DAYS:.0f}d windows · min_n {MIN_N} · {N_FOLDS} folds")
    L.append("")
    L.append("`reachability_decay.py` found the cell rate drifts hard between eras "
             "(mean |Δ| 0.20) yet **no trailing window recovers transfer**. The "
             "explanation that fits both is a **shared market-wide factor**: if all "
             "cells load on one driver, they move together (high drift), the ordering "
             "between them is stable-but-uninformative, and refitting just re-estimates "
             "the same factor. This decomposes the cell-rate matrix to test it.")
    L.append("")

    # ── The decomposition ──
    L.append("## 1. Variance decomposition")
    L.append("")
    L.append("```")
    L.append("R[cell, window] = μ + a[cell] + b[window] + e[cell, window]")
    L.append("                     └ cell ┘  └  time  ┘  └  residual  ┘")
    L.append("```")
    L.append("")
    L.append(f"**{cf.factor_verdict(d)}**")
    L.append("")
    L.append("| component | variance | share | what it means |")
    L.append("|---|---:|---:|---|")
    L.append(f"| **cell** (cross-sectional) | {_fmt(d.get('var_cell'), '.5f')} | "
             f"**{_fmt(d.get('share_cell'), '.1%')}** | does this cell win more than others? |")
    L.append(f"| **time** (common factor) | {_fmt(d.get('var_time'), '.5f')} | "
             f"**{_fmt(d.get('share_time'), '.1%')}** | does this *period* win more? |")
    L.append(f"| **residual** | {_fmt(d.get('var_resid'), '.5f')} | "
             f"**{_fmt(d.get('share_resid'), '.1%')}** | cell×time interaction + noise |")
    L.append("")
    L.append(f"Observations: **{d.get('n_obs')}** (cell, window) pairs over "
             f"{d.get('n_cells')} cells × {d.get('n_windows')} windows · "
             f"grand mean {_fmt(d.get('grand_mean'))}")
    L.append("")
    L.append("Unweighted means (each observed pair counts once), so a few very large "
             "cells cannot define the \"market\" factor.")
    L.append("")

    # ── The predictive test ──
    L.append("## 2. Does removing the factor reveal an edge?")
    L.append("")
    L.append(f"**{cf.predictive_verdict(pred)}**")
    L.append("")
    L.append("| ranking | out-of-fold AUC | n |")
    L.append("|---|---:|---:|")
    L.append(f"| raw cell rate (what the model uses) | {_fmt(pred.get('raw_auc'))} | "
             f"{pred.get('n_raw')} |")
    L.append(f"| cell rate **with the period factor removed** | "
             f"{_fmt(pred.get('residual_auc'))} | {pred.get('n_residual')} |")
    L.append("")
    L.append("Both ranked on the same rows with the same fold structure, so a "
             "difference is attributable to the factor removal alone.")
    L.append("")

    # ── Cell effects ──
    L.append("## 3. Cell effects (a[cell]) — the cross-sectional ranking")
    L.append("")
    L.append("Each cell's average deviation from the grand mean, across all windows. "
             "This is the ordering a cross-sectional model would exploit.")
    L.append("")
    ce = d.get("cell_effects") or {}
    L.append("| cell | a[cell] |")
    L.append("|---|---:|")
    for k, v in list(ce.items())[:20]:
        sym, vol, bkt = k if isinstance(k, tuple) and len(k) == 3 else (str(k), "", "")
        L.append(f"| {sym} · {vol} · {bkt} | {_fmt(v, '.4f', plus=True)} |")
    L.append("")
    if ce:
        spread = max(ce.values()) - min(ce.values())
        L.append(f"Spread of cell effects: **{spread:.4f}** across {len(ce)} cells.")
        L.append("")

    # ── Window effects ──
    L.append("## 4. Window effects (b[window]) — the common factor")
    L.append("")
    we = d.get("window_effects") or {}
    L.append("| window | b[window] |")
    L.append("|---|---:|")
    for k, v in we.items():
        L.append(f"| w{k} | {_fmt(v, '.4f', plus=True)} |")
    L.append("")
    if we:
        wv = list(we.values())
        L.append(f"Range of the common factor: **{min(wv):+.4f} to {max(wv):+.4f}** "
                 f"(spread {max(wv)-min(wv):.4f}).")
        L.append("")

    # ── Coverage ──
    L.append("## 5. Coverage")
    L.append("")
    L.append(f"- fetched: **{diag['fetched']}** resolved light signals")
    L.append(f"- usable: **{diag['ok']}** ({100*diag['ok']/max(1,diag['fetched']):.1f}%)")
    L.append(f"- dropped: {diag['no_atr']} no ATR/timestamp, {diag['no_cell']} unbucketable")
    if rows:
        span = (rows[-1]["_ts"] - rows[0]["_ts"]) / 86400
        L.append(f"- span: **{span:.0f} days** · {len(m.get('windows') or [])} windows "
                 f"of {WINDOW_DAYS:.0f}d")
        if span < LOOKBACK_DAYS * 0.7:
            L.append(f"- ⚠ available history is **{span:.0f}d**, not the {LOOKBACK_DAYS}d "
                     f"requested — `nt_signal_sim` starts 2026-08-23. Only "
                     f"~{len(m.get('windows') or [])} windows exist, which is the "
                     f"binding limit on this decomposition's power.")
    L.append("")

    # ── Caveats ──
    L.append("## 6. Caveats")
    L.append("")
    L.append("- **Resolved rows only** — expired excluded (no TP/SL verdict). A selected "
             "sub-population; audit C2/R6.")
    L.append("- **40 days of history.** The whole system started 2026-08-23, so the "
             "window count is small and the decomposition's power is bounded. Treat the "
             "shares as directional, not precise.")
    L.append("- **Unweighted two-way decomposition** (no interaction term estimated "
             "separately); `residual` therefore absorbs both the true cell×time "
             "interaction and sampling noise. That is the conservative reading — it "
             "cannot manufacture a cell effect.")
    L.append("- **`a[cell]` is an average over the observed windows**, so a cell present "
             "in few windows has a noisier effect. `min_n` bounds within-window noise, "
             "not across-window coverage.")
    L.append("")
    L.append("## Notes")
    L.append("")
    L.append("- If **time dominates**, the reachability table is a market-state read, "
             "not a cell-quality read — the model should condition on a *market* "
             "variable, not enumerate cells.")
    L.append("- If **cell dominates** but the residual test still fails, the cell "
             "ordering exists but is not stable enough to trade.")
    L.append("- If **residual dominates**, the cell rate is mostly noise and neither "
             "a cell table nor a factor adjustment will help.")
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
        print("no usable rows")
        return

    m = cf.build_matrix(rows, window_seconds=WINDOW_DAYS * 86400, min_n=MIN_N)
    print(f"  matrix: {len(m['cells'])} cells x {len(m['windows'])} windows")
    d = cf.decompose_matrix(m)
    print(f"  shares: cell={d.get('share_cell')} time={d.get('share_time')} "
          f"resid={d.get('share_resid')}")
    pred = cf.compare_predictions(rows, window_seconds=WINDOW_DAYS * 86400,
                                  min_n=MIN_N, n_folds=N_FOLDS)
    print(f"  predictive: raw={pred.get('raw_auc')} residual={pred.get('residual_auc')}")

    now = datetime.now(timezone.utc)
    text = render(rows, diag, m, d, pred, now)
    out = ROOT / "worklog" / "kairos" / "reports" / f"{now:%Y%m%d}_common_factor.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
