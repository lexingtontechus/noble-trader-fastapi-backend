#!/usr/bin/env python3
"""run_vol_backtest.py — Phase 0 runner: RV forecast benchmark, measured or nothing.

Implements the corrected Phase 0 of the TimesFM refined thesis
(`.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md` + 2026-10-18 review):

  * reference bar = **Log-HAR** (Brini 2026: only TTM beat Log-HAR, by
    1.3-1.8%; TimesFM and Chronos-Bolt did not), naive = secondary null
  * loss = **QLIKE**; MAE/RMSE on log(RV); directional accuracy vs the
    **persistence rule** (not a coin flip); 80% interval coverage
  * DM tests with **Newey-West HAC** lag (overlap-aware) + **Holm** correction
  * per-symbol results — never pooled across symbols (vol-scale dominance)
  * model arms (TimesFM 2.5 / Chronos-2 / Chronos-2 covariates) run only when
    their runtime is present, and every skip is REPORTED, never silent

Data sources (pick one):
    --data-dir data/vol_backtest    one <SYMBOL>.csv per symbol (timestamp,close)
    --yfinance                      hourly bars via yfinance (proxy tickers)
    --synthetic                     deterministic two-regime vol series (CI/smoke)

Outputs (under --out, default worklog/kairos/reports/vol_backtest/<runid>/):
    results.csv     long-format per-(symbol,horizon,arm) metrics
    dm_tests.csv    DM vs har + DM vs naive, Holm-adjusted
    summary.md      human verdict incl. acceptance-criteria check
    records.csv     raw per-cutoff forecasts (debug / replay)

Examples:
    # CI / methodology smoke (no models, ~seconds)
    python scripts/run_vol_backtest.py --synthetic --horizons 24 --cutoffs 40

    # full synthetic sweep
    python scripts/run_vol_backtest.py --synthetic --horizons 6,24,48,72

    # real data, all arms that are installed/reachable
    python scripts/run_vol_backtest.py --data-dir data/vol_backtest \
        --symbols BTCUSD,XAUUSD,XAGUSD,USDJPY,USDCHF --horizons 24,72

    # with the repo's TimesFM server up (:8002) and Chronos-2 installed
    TIMESFM_BASE_URL=http://localhost:8002 TIMESFM_API_KEY=... \
    python scripts/run_vol_backtest.py --data-dir data/vol_backtest ...
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vol_backtest.arms import NaiveRV, EWMARV, LogHAR, HAR_EWMA, GARCH11  # noqa: E402
from vol_backtest.model_arms import TimesFM25HTTP, Chronos2, Chronos2Covariate  # noqa: E402
from vol_backtest.harness import backtest_symbol, aggregate_metrics, dm_family  # noqa: E402
from vol_backtest.data import THESIS_SYMBOLS, load_data_dir, load_synthetic, load_yfinance  # noqa: E402
from vol_backtest.realized import annualize_horizon  # noqa: E402


def build_arms(selected: list[str]) -> list:
    catalog = {
        "naive": NaiveRV,
        "ewma": EWMARV,
        "har": LogHAR,
        "har_ewma": HAR_EWMA,
        "garch": GARCH11,
        "timesfm25": TimesFM25HTTP,
        "chronos2": Chronos2,
        "chronos2_cov": Chronos2Covariate,
    }
    arms = []
    for name in selected:
        cls = catalog.get(name)
        if cls is None:
            raise SystemExit(f"unknown arm: {name} (have {sorted(catalog)})")
        arms.append(cls())
    return arms


def write_summary(
    out_dir: Path,
    metric_rows: list[dict],
    dm_rows: list[dict],
    skipped: dict[str, str],
    arm_stats: dict,
    horizons: list[int],
    symbols: list[str],
    source: str,
    elapsed_s: float,
) -> None:
    import pandas as pd

    lines: list[str] = []
    a = lines.append
    a("# Vol Forecast Backtest — Phase 0 (measured or nothing)")
    a("")
    a(f"**Run:** {time.strftime('%Y-%m-%d %H:%M:%S')} · source: `{source}` · "
      f"symbols: {', '.join(symbols)} · horizons: {horizons} · wall: {elapsed_s:.1f}s")
    a("")
    a("Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat "
      "Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. "
      "Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC "
      "(overlap-aware) + Holm. Per-symbol rows — not pooled.")
    a("")

    if skipped:
        a("## Skipped arms (reported, never silent)")
        a("")
        a("| arm | reason |")
        a("|---|---|")
        for k, v in sorted(skipped.items()):
            a(f"| `{k}` | {v} |")
        a("")

    a("## Acceptance criteria check (thesis §benchmark, corrected nulls)")
    a("")
    a("| criterion | verdict per symbol (see dm_tests.csv for stats) |")
    a("|---|---|")
    df = pd.DataFrame(metric_rows)
    dmd = pd.DataFrame(dm_rows) if dm_rows else pd.DataFrame(
        columns=["symbol", "horizon", "arm_a", "arm_b", "p_holm", "mean_loss_diff"]
    )
    for h in horizons:
        sub = df[df["horizon"] == h] if not df.empty else df
        for sym in symbols:
            s = sub[(sub["symbol"] == sym)] if not sub.empty else sub
            if s.empty:
                continue
            try:
                naive = float(s[s["arm"] == "naive"]["mae"].iloc[0])
            except (IndexError, ValueError):
                naive = float("nan")
            crit = []
            for _, row in s.iterrows():
                if row["arm"] in ("naive", "har", "ewma", "har_ewma", "garch"):
                    continue
                d = dmd[(dmd["symbol"] == sym) & (dmd["horizon"] == h)
                        & (dmd["arm_a"] == row["arm"])]
                beats_har = bool(
                    (not d.empty) and d["significant_holm"].iloc[0]
                ) if not d.empty else False
                cov = row["coverage80"]
                cov_ok = _isfinite(cov) and 0.80 <= cov <= 0.95
                crit.append(
                    f"`{row['arm']}`: qlike={row['qlike']:.4f} "
                    f"dir={row['dir_acc']:.3f} (null={row['dir_null_persistence']:.3f}) "
                    f"cov80={f'{cov:.3f}' if _isfinite(cov) else 'n/a'} "
                    f"{'| DM-beats-HAR✓' if beats_har else '| DM-beats-HAR✗'}"
                )
            if crit:
                a(f"| H={h} · {sym} — MAE vs naive: {naive:.6g} (raw scale; "
                  f"use qlike+DM for decisions) | " + "<br>".join(crit) + " |")
    a("")

    a("## Metrics (per symbol × horizon × arm)")
    a("")
    if df.empty:
        a("_(no rows)_")
    else:
        for h in horizons:
            a(f"### horizon = {h} bars")
            a("")
            sub = df[df["horizon"] == h]
            if sub.empty:
                continue
            cols = ["symbol", "arm", "n", "qlike", "mae_log", "dir_acc",
                    "dir_null_persistence", "coverage80"]
            a(sub[cols].to_markdown(index=False, floatfmt=".4f"))
            a("")
        # per-(symbol,horizon) qlike winner table
        a("### QLIKE winners (lower = better)")
        a("")
        win_rows = []
        for (sym, h), g in df.groupby(["symbol", "horizon"]):
            g2 = g.dropna(subset=["qlike"])
            if g2.empty:
                continue
            best = g2.loc[g2["qlike"].idxmin()]
            win_rows.append({
                "symbol": sym, "horizon": h,
                "best_arm": best["arm"], "qlike": best["qlike"],
                "har_qlike": float(g[g["arm"] == "har"]["qlike"].iloc[0])
                if (g["arm"] == "har").any() else float("nan"),
            })
        if win_rows:
            a(pd.DataFrame(win_rows).to_markdown(index=False, floatfmt=".4f"))
        a("")

    a("## Diebold-Mariano (QLIKE, HAC + Holm)")
    a("")
    if dmd.empty:
        a("_(no DM rows)_")
    else:
        show = ["symbol", "horizon", "arm_a", "arm_b", "n", "mean_loss_diff",
                "dm_stat", "p_value", "p_holm", "hac_lag", "significant_holm"]
        for ref in ("har", "naive"):
            sub = dmd[dmd["arm_b"] == ref]
            if sub.empty:
                continue
            a(f"#### vs `{ref}`")
            a("")
            a(sub[show].to_markdown(index=False, floatfmt=".4f"))
            a("")

    a("## Arm runtime")
    a("")
    a("| arm | calls | successes | seconds |")
    a("|---|---|---|---|")
    for name, st in sorted(arm_stats.items()):
        a(f"| `{name}` | {st.n_calls} | {st.n_success} | {st.seconds:.1f} |")
    a("")

    a("## Honesty notes")
    a("")
    a("- Model arms were executed only if listed above under successes; "
      "`chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the "
      ":8002 server respectively. Synthetic runs validate METHODOLOGY, not "
      "financial skill.")
    a("- Coverage is the empirical 80% central-interval coverage; acceptance "
      "band [0.80, 0.95] (too narrow or too wide both fail).")
    a("- MAE on the raw scale is per-symbol only — do not average across "
      "symbols; cross-symbol comparison lives in qlike/mae_log.")
    a("- Nothing here is wired into the live orchestrator/EV/Kelly path. "
      "Phase 3/4 integration is gated on these results.")
    a("")

    (out_dir / "summary.md").write_text("\n".join(lines))


def _isfinite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true", help="deterministic synthetic series")
    src.add_argument("--data-dir", type=Path, help="dir with <SYMBOL>.csv files")
    src.add_argument("--yfinance", action="store_true", help="hourly bars via yfinance")
    ap.add_argument("--symbols", default=",".join(THESIS_SYMBOLS),
                    help="comma-separated symbol list")
    ap.add_argument("--horizons", default="6,24,48,72", help="comma-separated horizons (bars)")
    ap.add_argument("--arms", default="naive,ewma,har,har_ewma,garch,timesfm25,chronos2,chronos2_cov",
                    help="comma-separated arm subset")
    ap.add_argument("--no-models", action="store_true", help="baselines only")
    ap.add_argument("--spacing", type=int, default=24, help="cutoff spacing in bars (default 24)")
    ap.add_argument("--cutoffs", type=int, default=None,
                    help="cap on cutoffs per (symbol,horizon) — smoke runs")
    ap.add_argument("--min-train", type=int, default=720, help="warmup bars before first cutoff")
    ap.add_argument("--seed", type=int, default=7, help="synthetic seed")
    ap.add_argument("--days", type=int, default=365, help="synthetic days")
    ap.add_argument("--out", type=Path, default=None, help="output dir (default worklog/kairos/reports/vol_backtest/<runid>)")
    args = ap.parse_args()

    import pandas as pd

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    if args.no_models:
        arm_names = [a for a in arm_names if a in ("naive", "ewma", "har", "har_ewma", "garch")]

    # ---- data ----
    if args.synthetic:
        series_list = load_synthetic(symbols, n_days=args.days, seed=args.seed)
        source = f"synthetic(seed={args.seed}, days={args.days})"
    elif args.data_dir is not None:
        series_list = load_data_dir(args.data_dir, symbols)
        source = f"csv:{args.data_dir}"
    else:
        series_list = load_yfinance(symbols)
        source = "yfinance(hourly)"
    if not series_list:
        raise SystemExit("no series loaded — check --symbols / --data-dir / --yfinance")

    # ---- arms ----
    arms = build_arms(arm_names)
    arm_names = [a.name for a in arms]

    runid = time.strftime("%Y%m%d_%H%M%S")
    out_dir = args.out or (ROOT / "worklog" / "kairos" / "reports" / "vol_backtest" / runid)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    all_records: list = []
    arm_stats: dict = {}
    skipped: dict[str, str] = {}
    for s in series_list:
        res = backtest_symbol(
            s, arms, horizons,
            spacing=args.spacing,
            min_train_bars=args.min_train,
            max_cutoffs=args.cutoffs,
        )
        all_records += res.records
        for k, v in res.arm_stats.items():
            st = arm_stats.setdefault(k, type(v)())
            st.n_calls += v.n_calls
            st.n_success += v.n_success
            st.seconds += v.seconds
        skipped.update(res.skipped_arms)
        print(f"[{s.symbol}] bars={len(s.prices)} records={len(res.records)} "
              f"skipped={list(res.skipped_arms)}", flush=True)
    elapsed = time.perf_counter() - t0

    metric_rows = aggregate_metrics_from_records(all_records)
    dm_rows = dm_family_from_records(all_records)

    import pandas as pd

    pd.DataFrame([r.__dict__ for r in all_records]).to_csv(out_dir / "records.csv", index=False)
    pd.DataFrame(metric_rows).to_csv(out_dir / "results.csv", index=False)
    pd.DataFrame(dm_rows).to_csv(out_dir / "dm_tests.csv", index=False)
    (out_dir / "run_config.json").write_text(json.dumps({
        "source": source, "symbols": symbols, "horizons": horizons,
        "arms": arm_names, "spacing": args.spacing,
        "cutoffs_cap": args.cutoffs, "min_train": args.min_train,
        "skipped_arms": skipped,
    }, indent=2))

    write_summary(
        out_dir, metric_rows, dm_rows, skipped, arm_stats, horizons,
        symbols, source, elapsed,
    )
    print(f"\nDone in {elapsed:.1f}s -> {out_dir}")
    print(f"  results.csv  ({len(metric_rows)} metric rows)")
    print(f"  dm_tests.csv ({len(dm_rows)} DM rows)")
    print(f"  summary.md")


def aggregate_metrics_from_records(records):
    from vol_backtest.harness import BacktestResult, aggregate_metrics

    res = BacktestResult(records=records)
    return aggregate_metrics(res)


def dm_family_from_records(records):
    from vol_backtest.harness import BacktestResult, dm_family

    res = BacktestResult(records=records)
    return dm_family(res)


if __name__ == "__main__":
    main()
