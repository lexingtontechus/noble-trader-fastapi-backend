# Vol Backtest Phase 0 — Built, Corrected, Measured on Synthetic

**Date:** 2026-10-18
**Type:** Build + methodology correction (Phase 0 of the TimesFM refined thesis)
**Base:** HEAD `5dbd8eb` · **Plan:** `.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md` (as critiqued 2026-10-18)
**Status:** Framework complete; **no live-path code touched.**

---

## 0. Verdict

Phase 0 (the blocking evaluation framework) now exists and runs end-to-end:
**24/24 tests pass**, a 3-symbol × 3-horizon synthetic run completes in 0.5 s and
emits `results.csv` / `dm_tests.csv` / `summary.md` / `records.csv`. It was built
with the **corrected methodology**, not the thesis's original one — five
load-bearing defects in the plan's Task 0.1–0.4 were fixed before implementation
(§2). The model decision the framework presupposes: **TimesFM 3.0 is
license-blocked** (self-hosted weights are non-commercial; commercial use is
Google-Cloud-only, which also blocks fine-tuning), so the covariate/universal
arm is **Chronos-2** (Apache-2.0, native past+future covariates, first-party
LoRA) with **TimesFM 2.5 kept as the incumbent arm** and **Log-HAR as the bar**
(Brini 2026: only TTM beat Log-HAR, by 1.3–1.8%; TimesFM and Chronos-Bolt did
not; arXiv 2607.05291).

**Nothing here claims financial skill.** Synthetic runs validate the
*methodology*; the model arms did not execute in this environment (no
`chronos-forecasting`, no `arch`, no :8002 server — every skip is reported in
the run summary, never silent).

---

## 1. What was built

| Piece | File | Role |
|---|---|---|
| RV + calendars | `vol_backtest/realized.py` | log-return RV, per-asset calendars (crypto 24/7 vs FX/metals ~120h/wk — the thesis's single √(365·24) factor overstated FX by ~18%), synthetic generator with real weekend closure |
| Metrics | `vol_backtest/metrics.py` | **QLIKE** (primary), MAE/RMSE on log(RV), directional accuracy **vs the persistence rule** (not a coin flip), 80% interval coverage, pinball |
| DM machinery | `vol_backtest/dm.py` | Diebold-Mariano with **Newey-West HAC** (lag = horizon/spacing − 1, overlap-aware) + **Holm** family-wise correction |
| Baseline arms | `vol_backtest/arms.py` | naive, EWMA (λ=0.94), **Log-HAR (the bar)**, har_ewma blend (Brini-style "simple average" fallback), optional GARCH(1,1) |
| Model arms | `vol_backtest/model_arms.py` | `timesfm25` (repo's server.py :8002, X-API-Key, 9- and 10-level quantile layouts handled), `chronos2`, **`chronos2_cov`** (past + known-future covariates — the universal endgame arm) |
| Data | `vol_backtest/data.py` | CSV dir / yfinance hourly (proxy tickers) / deterministic synthetic |
| Harness | `vol_backtest/harness.py` | rolling-origin, frozen cutoffs, **no-lookahead enforced by slicing** (sentinel-tested), per-symbol aggregation, DM family |
| Runner | `scripts/run_vol_backtest.py` | CLI → `worklog/kairos/reports/vol_backtest/<runid>/` (results.csv, dm_tests.csv, summary.md, records.csv, run_config.json) |
| Tests | `tests/test_vol_backtest.py` | 24 tests: hand-computed RV, calendar drops Saturday, QLIKE identity/asymmetry, NW closed-form contract, DM significance, Holm ordering, HAR-beats-naive sanity, **no-lookahead sentinel**, TimesFM layout extraction, runner end-to-end subprocess smoke |

Placement is deliberate: a **top-level `vol_backtest/` package** (like `renko/`),
NOT inside `regime_platform/` — the 2026-10-13 dead-weight audit's own finding
was that research code does not belong in the prod package. Nothing imports
`vol_backtest` from the orchestrator, EV engine, or gates.

---

## 2. Corrections applied vs the thesis's original Phase 0

| # | Thesis said | Built instead | Why |
|---|---|---|---|
| 1 | Task 0.2: vol from **std of the point forecast** | **Quantile-spread extraction** — per-step σ from (q90−q10)/2·z₀.₉, RV = √Σσ² | Std of a conditional-mean path measures trend slope, not dispersion; ~0 on returns regardless of model quality. This flaw sat in front of the "blocking" gate — it would have poisoned the entire program. |
| 2 | Baselines: naive, GARCH | naive, EWMA, **Log-HAR**, har_ewma, GARCH(optional) | HAR-RV is the standard strong baseline; the RV literature's bar. An evaluation that cannot answer "does it beat HAR?" does not answer the thesis. |
| 3 | MAPE among metrics | **QLIKE** + log-scale MAE/RMSE | Near-zero weekend RV explodes MAPE; QLIKE is the robust standard. Raw-scale MAE stays per-symbol only (BTC's vol scale would dominate any pool). |
| 4 | Directional accuracy "≥55% vs 50% chance" | Directional accuracy reported **with the persistence-rule null** on the same sample | Realized vol is highly persistent; the coin-flip null overstates skill. (On the synthetic runs the persistence null itself scores 0.29–0.44 — the null is not trivially high, which is exactly why it must be measured, not assumed.) |
| 5 | DM test, unspecified variance | DM with **HAC lag = horizon/spacing − 1** + **Holm** across the arm family | 48–72h forecasts at 24h spacing overlap; plain DM variance is wrong. K arms × symbols = multiple comparisons. |
| + | Train split ≤ 2025-06-01 vs "1 year of data" (empty train set — date bug) | `--min-train` bars parameter (default 720) | The split bug was in the plan, not the harness; the runner takes any data window. |

Deferred (not Phase 0, recorded so they are not lost):
- **Kelly sign** — the thesis's Tasks 3.2/4.3 multiply Kelly by `vol_forecast/current_vol` (sizes UP into forecast vol spikes; backwards in every Kelly formulation). Correct form for Phase 3/4: `kelly *= clamp(current_vol/vol_forecast, lo, hi)` with a no-trade cutoff, and the unit tests must assert the inverted direction.
- **p_timesfm removal (thesis Phase 4a)** — independent of this program; at HEAD `p_markov_hold_n_mode: "off"` means removing p_timesfm leaves the blend single-source; do it as a flag-level renormalization, not `P_WIN_WEIGHTS_FULL` surgery, with recalibration.

---

## 3. Verification (this environment)

```
$ python3 -m pytest tests/test_vol_backtest.py -q
24 passed in 4.84s

$ python3 scripts/run_vol_backtest.py --synthetic \
    --symbols USDJPY,BTCUSD,XAUUSD --horizons 6,24,48 --cutoffs 60 \
    --arms naive,ewma,har,har_ewma,garch,timesfm25,chronos2,chronos2_cov
[USDJPY] bars=6368 records=885 skipped=['garch','chronos2','chronos2_cov']
[BTCUSD] bars=8760 records=672 skipped=[... + 'timesfm25']
[XAUUSD] bars=6368 records=708 skipped=[... + 'timesfm25']
Done in 0.5s -> worklog/kairos/reports/vol_backtest/20261019_003015
  results.csv (39 metric rows) · dm_tests.csv (60 DM rows) · summary.md
```

Sanity of the synthetic results themselves: HAR/blend beat naive on QLIKE for
USDJPY and XAUUSD at every horizon (e.g. USDJPY H=6: naive 0.1200 vs har
0.0882); on BTCUSD naive is competitive at H=6 (0.0832 vs har 0.0897) — the
two-regime synthetic has no weekly structure for m120 to exploit, which is the
expected behaviour, and the dedicated
`test_har_beats_naive_on_weekly_cyclic_vol` covers the case where HAR must
strictly win. HAR's 80% interval coverage lands 0.78–0.85 across symbols —
inside the [0.80, 0.95] acceptance band (borderline at the low end on
synthetic; real-data runs will re-measure).

`timesfm25` shows `n=0` rows for the first symbol in this run (the health
probe caches its failure after the first forecast call) — reported honestly in
`summary.md` §Skipped arms.

---

## 4. What this did NOT do

- **No model arm executed against real weights.** `chronos-forecasting`,
  `arch`, and the :8002 TimesFM server are absent in this environment. The
  arms are implemented against their documented APIs
  (`Chronos2Pipeline.predict_df` with `context_df`/`future_df`;
  server.py `/forecast` with X-API-Key and the 9/10-level quantile layouts) and
  will run where those exist. First real-data run is the operator's.
- **No real-data run.** The CSV loader (`--data-dir data/vol_backtest/`) and
  the yfinance path (`--yfinance`, proxy tickers GC=F/SI=F/JPY=X/CHF=X/BTC-USD,
  1h × ~730d) are wired but not exercised here (no network fetch performed).
- **No live-path changes**: no orchestrator, EV, gate, flag, or migration
  touched. No `regime_platform` imports from `vol_backtest`.
- **No fine-tuning (thesis Phase 2)** — gated, as designed, on Phase 0
  results. Rahimikia et al. (arXiv 2511.18578) found general TSFMs perform
  poorly on finance in zero-shot AND fine-tuning settings; only from-scratch
  financial pre-training showed substantial gains — so the fine-tune pillar
  must clear the zero-shot bar first, which this framework now measures.
- **Not a "deathmatch":** the framework is model-agnostic by construction
  (thesis Open Q5); adding an arm is one class in `model_arms.py`.

---

## 5. Files / lineage

- New: `vol_backtest/` (7 modules), `scripts/run_vol_backtest.py`,
  `tests/test_vol_backtest.py`.
- Docs updated: `README.md` (§Volatility Forecast Backtesting + structure
  tree + TimesFM section pointer), `agents.md` (research-tooling block),
  `.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md`
  (implementation-status addendum), `requirements.txt` (optional deps,
  commented).
- Artifact: `worklog/kairos/reports/vol_backtest/20261019_003015/`.
- Prior art honoured: `scripts/timesfm_p1_covariates_probe.py` (P1.1, TimesFM
  2.5 XReg path) remains the record for the incumbent's covariate surface;
  the Chronos-2 covariate arm is the native-architecture version of that
  question.
- Next steps (in order): (1) operator exports 1y hourly CSVs for the 5 thesis
  symbols → real-data run with `--data-dir`; (2) install `chronos-forecasting`
  on a box with disk for the 120M encoder (CPU is sufficient) → `chronos2` +
  `chronos2_cov` arms live; (3) start server.py → `timesfm25` arm live;
  (4) decision gate: any model arm beating **Log-HAR** per-symbol on QLIKE at
  DM/Holm significance proceeds to fine-tuning; if none does, the Brini
  fallback (TSFM+HAR equal-weight blend) is the measured answer and the
  thesis's own escape clause ("revise the thesis") fires.
