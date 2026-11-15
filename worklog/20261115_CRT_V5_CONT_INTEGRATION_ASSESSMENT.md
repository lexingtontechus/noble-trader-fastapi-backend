# 2026-11-15 — CRT v5 CONT Integration Assessment (pre-build scope)

## Context

Operator uploaded `CRT_v5_CONT_MT4.zip` (5 files, reviewed in full) and asked:

1. Assess how to integrate CRT as a **separate orchestrator/engine** into the Kairos codebase.
2. Which **existing functions/features can CRT leverage**.
3. Evaluate the proposed layout: **`regime_platform_crt` + `regime_edge_crt`**.

This is an assessment only — no code written. Companion session task: Task 67.

## 1. What CRT v5 CONT is (review of the zip)

| File | LOC | What it is |
|---|---|---|
| `CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4` | 3,217 | Live MT4 EA: setup detection (C1/C2/C3), CONT execution, EV gates, decay monitor, rolling-window EV gate, confidence sizing, risk caps, dashboard/semi-auto pad |
| `CRT_EV_Research_Export.mq4` | 559 | MQL4 offline labeler: raw setups across EntryBuf×SLBuf×R×Hold grid → compact-path CSV (bars-to-SL, bars-to-R, close-R at holds), features (sweep_atr, chop, atr_pct, session), `#meta` cost line |
| `crt_ev_optimizer.py` | 556 | **Already Python/pandas/numpy**: walk-forward (max-LCB selection, 24m/6m folds), OOS concat → EV/t/PF/folds%, decay-monitor replay, enable thresholds, config CSV + HTML report |
| `baseline_check.py` | 140 | No-fitting baseline (v3 filters) with cost-burden analysis |
| `README_v5_CONT.md` | — | Pipeline: export → baseline → walk-forward → demo; gate semantics; known limits |

**The strategy in one line**: when C2 one-sided-sweeps a C1 extreme and closes back inside C1, place a stop order **in the sweep direction at the C2 sweep extreme** (CONT leg), SL beyond the C2 opposite extreme, valid during C3 only. Per-option (symbol×TF×dir×session) settings come from a walk-forward config; live trading is gated by optimizer-enable → filters → CUSUM decay state (NORMAL/CAUTION/OFF) → rolling-window EV (45d, min-n 15, option→TF pooled fallback), decision frozen at C2 close.

**The philosophy is already Kairos's**: OOS-honest selection (LCB, not mean), pre-declared decay rules, falsifiable enable thresholds, honest limits documented (thin H1 rolling window; rolling window overlapping training data). It maps onto the claims/honesty layer naturally.

## 2. Port triage — what survives the MT4→Kairos move

| CRT artifact | Fate |
|---|---|
| `crt_ev_optimizer.py` + `baseline_check.py` | **~90% reusable verbatim** — already Python, pandas/numpy. Needs: IO-path/config-artifact changes, provenance stamps, report location under `worklog/kairos/reports/`, UTC discipline. Moves to `regime_edge_crt/`. |
| Research-export labeling logic (`VariantRow`, setup detect, chop/ATRPct/session) | **Rewritten once in Python** (or bypassed — see data plan) — becomes `regime_edge_crt/labeling.py`; the compact-path CSV schema stays so the optimizer ingests unchanged |
| EA engine (detection, gates, decay, sizing, frozen decision) | **Rewritten as the live engine package** — MQL4 semantics ported 1:1; order/execution layer dropped (paper-first) |
| MT4 execution (OrderSend/stop orders, MagicNumber, broker clock, spread inputs) | **Dropped** — no MT4 in this repo; MT5 bridge untouched per standing contract. Paper via `nt_paper_positions`; live-execution is a later, separately-gated decision |
| Dashboard / trade pad / SEMI-AUTO UI | **Dropped** — read surface becomes REST + the claims leaderboard |

## 3. Integration seams — what CRT can leverage (verified in tree)

Ranked by leverage:

1. **Candle source**: `regime_platform/sweep_orchestrator/price_fetcher.py::fetch_tradingview_batch` / `fetch_candles_for_symbols` — TDVA OHLCV+timestamps, 10-symbol batching, bounded 429 retry; intervals 5m/15m/1h/4h/1D. CRT needs M15/M30/H1 → **M30 must be derived by 2×15m aggregation** (no 30m key in the map). This is CRT's only data seam.
2. **Walk-forward harness template**: `vol_backtest/` (`arms.py` Arm protocol, `harness.py` frozen-cutoff rolling-origin, `recalib.py` per-cell MZ+k shrinkage/min-n + artifact JSON, `dm.py` DM+Holm) + `scripts/run_vol_backtest.py` offline CLI → `config/claims_vol_fan_recalib.json` → flag-gated live consumption. The CRT optimizer already *is* this shape (LCB folds, per-cell enable, decay replay); the port follows the repo's artifact/flag conventions instead of CSV-in-MQL4\Files.
3. **Research-twin template**: `regime_edge/` package + `scripts/run_edge_lab.py` + `SweepOrchestrator._run_edge_lab` (orchestrator.py:3255) — TDVA export → resumable per-symbol checkpoints → `worklog/kairos/reports/regime_edge/<runid>/` → optional publish flag; **`regime_edge/brackets.py::resolve()` is an intrabar SL-first triple-barrier resolver — CRT's label basis (SL-wins-ties, conservative same-bar) matches it exactly**. `features.py` causal-feature discipline (no-lookahead, test-pinned) is the discipline CRT's sweep_atr/chop/atr_pct features must adopt.
4. **Paper book**: `sweep_orchestrator/paper_book.py` on `nt_paper_positions` — the column whitelist **already has a `source` column**; CRT rows drop in with `source='crt_cont'`. Caveat: `reconcile()` joins on `nt_signal_sim.signal_id` — CRT needs either its own thin open/close pass (fork of ~200 lines) or a CRT signal ledger feeding the same table.
5. **Settlement/EOD**: `settlement_contract` + `eod_resolver.resolve_pending_signals` (intrabar, `sl_first`, batched candle fetch, `nt_calibration_records`) — CRT paper trades resolved by the **same nightly machinery and the same basis** if CRT signals are written `nt_signal_sim`-shaped; otherwise CRT's resolver is a ~100-line sibling reusing `fetch_candles_for_symbols` + `settlement_contract`.
6. **Claims layer**: `claims.py` / `claims_resolver.py` / `claims_leaderboard.py` on `nt_signal_claims` (migration 218; `kind ('bracket'|'vol_fan'|'thesis')`, `source`, fan frozen at write) — CRT's pre-registered falsifiable claims (per-option OOS EV, decay-state transitions, rolling-gate verdicts) belong here; needs an **additive CHECK-constraint migration for new `kind`/`source` values** (precedent: migration 220). Immutable-claims contract respected (new rows only).
7. **Feature flags**: `regime_platform/core/feature_flags.py` (`_DEFAULTS` + typed getters + mtime hot-reload) + `config/feature_flags.yaml` — a `crt_*` family (`crt_mode: off|shadow|live`, `crt_paper_source`, `crt_config_path`, `crt_decay.*`, `crt_rolling.*`) slots in with zero infra; `scripts/reconcile_feature_flags.py` audits drift.
8. **Orchestrator registration**: `SweepOrchestrator.start()` APScheduler blocks + `sweep_orchestrator/__main__.py` standalone process (health socket, `start.sh` process list) — CRT's live loop mounts as a job family (aligned to bar closes, cf. `nt_symbol.regime_cron` 15-min cadence) or a sibling `__main__`; the renko precedent shows both patterns coexisting.
9. **Decay/EV substrate**: no existing CUSUM (verified: `drift_monitor` = rolling win-rate-vs-predicted bias, 3-tier 0.15/0.30/0.10 bands; `live_expectancy` = daily cohort expectancy table; `reachability_decay` = offline trailing-window AUC). **CRT brings its own 30-line CUSUM class** (from `crt_ev_optimizer.py::DecayMonitor`, rules identical to the EA) and writes state through the existing seam: single current-state row (`nt_symbol_calibration_state` pattern) + action rows (`nt_eod_actions` pattern) + rolling-EV table (`nt_live_expectancy_daily` upsert pattern).
10. **Sizing precedents**: `position_sizer.DynamicMasanielloSizer` (`conf_factor` = t-like confidence clip — CRT's OOS-t scaled base→max risk 0.25%→0.50%) and `variable_kelly.binary_kelly` + `context_multiplier` (shadow-only ladder discipline) — CRT's confidence sizing reuses the pattern, ships as a shadow column first.
11. **Physics cross-check**: `sweep_orchestrator/barrier.py::barrier_null` + `ev_lower_bound_z` — analytic first-passage EV lower bound; an independent sanity check on CRT's empirical EV gates, free.
12. **Sessions/clock**: `market_hours.is_market_open` (ZoneInfo, America/New_York DST-aware) covers market-open only — CRT's NY 09:30–12:00 / Asia 20:00–22:00 ET windows + per-C3-bar session labeling need a small additive session-window helper (no reuse of the MQL4 broker-clock detection — Kairos is UTC-native).
13. **Renko engine-precedent**: `renko/` — an entire independent engine mounted with a 2-line seam in `main.py` (131, 438-439: `from renko.router import renko_router` → conditional include), in-memory `_pipelines` + snapshot hydration on `nt_renko_snapshot`, runtime-config hot-invalidation hook. This is the template for the CRT engine's REST surface and state model (though CRT state is cheaper: recompute per closed bar, like the EA).
14. **Registries**: `nt_symbol` (universe/timeframes), `nt_roadmap` heartbeats (research job liveness), `nt_daily_close`.

## 4. Naming assessment: `regime_platform_crt` + `regime_edge_crt`

**`regime_edge_crt` — endorse as proposed.** It mirrors `regime_edge/` exactly (offline twin: data export, labeling, walk-forward, reports, publish flag) and is discoverable next to its sibling.

**`regime_platform_crt` — recommend a refinement.** The live platform is one package (`regime_platform`); a top-level `regime_platform_crt` reads as a *fork of the platform*, which it must never be (5-clause-style discipline: additive, no platform behavior changes). Repo convention for independent engines is a **short top-level package**: `renko/`, `vol_backtest/`, `regime_edge/`. Recommendation:

```
crt/                      # the engine (renko-style top-level package)
  setup.py                # C1/C2/C3 detection + features (port of EvaluateArmedAt/ExportTF)
  config_loader.py        # walk-forward config artifact (port of LoadOptConfig/FindCfg)
  gates.py                # enabled→filters→decay→rolling gate, frozen at C2 close (SelectTargetOpt)
  decay.py                # CUSUM NORMAL/CAUTION/OFF (port of ComputeMonitors/DecayMonitor)
  labeler.py              # triple-barrier R labeling (port of LabelMode/LabelCustomR)
  engine.py               # per-closed-bar loop: detect→gate→paper order (frozen decision)
  paper.py                # nt_paper_positions writer (source='crt_cont')
  router.py               # APIRouter(prefix="/crt") — 2-line mount in main.py (renko seam)
  __main__.py             # optional standalone process (sibling of sweep_orchestrator)
regime_edge_crt/          # the research twin (regime_edge-style)
  data.py labeling.py optimizer.py baseline.py report.py
scripts/run_crt_lab.py    # offline CLI (run_vol_backtest.py style)
config/crt_cont.json      # walk-forward artifact (claims_vol_fan_recalib.json style)
```

The "orchestrator" identity survives as `crt/engine.py` + either a `CRTOrchestrator` APScheduler job family inside `SweepOrchestrator.start()` (cheapest — reuses stale-recovery, heartbeats, resolver locks; recommended first) or its own `__main__` process in `start.sh` (isolation, later). If the operator strongly prefers the literal `regime_platform_crt` name it is viable — but it must contain only the engine, never a copy of platform code; the renko precedent is the safer convention.

## 5. Data plan (the real constraint)

- **Bootstrap corpus = the MT4 research CSVs themselves** (`CRT_CONT_RESEARCH_<SYMBOL>.csv`): the compact-path schema is already the optimizer's input; ingesting them gives years of M15/M30/H1 labeled variants with zero acquisition cost and exact parity with the MT4-side pipeline. The `#meta` line carries per-symbol spread/point/clock for cost parity.
- **Ongoing refresh = TDVA-native export** (extend `scripts/export_edge_lab_data.py` style): M15+H1 native, M30 = 2×15m aggregation, D1 for prior-day sweep features (`LIQ_PRIOR_DAY`). Constraint: TDVA caps at ~5,000 bars/request → multi-year backfills need paged windowed fetches; ~5y M15 (~180k bars) is the heavy case — checkpointed, resumable, run offline like the edge lab.
- **Spread/slippage**: the EA hardcodes per-symbol points (10 XAUUSD / 2 majors / half-spread BTC). Port to a `crt_costs` config block keyed by symbol-class, mirroring `regime_edge/barrier.py::cost_atr_by_class`.

## 6. Phased rollout (shadow-first, flag-gated, additive)

- **CRT-0 (research only, zero live surface)**: `regime_edge_crt/` + `scripts/run_crt_lab.py`; ingest MT4 CSVs; run `baseline_check` + walk-forward (the optimizer's thresholds verbatim: z=1.28, min_train_n 30, min_oos_n 30, min_oos_ev 0.02, min_oos_t 1.0, require final LCB>0); mint `config/crt_cont.json` (per-option rows: model CONT, eb/sb/R/hold, filters, enabled, base_ev/sd, oos_n/t, train_end). Report under `worklog/kairos/reports/regime_edge_crt/<runid>/`.
- **CRT-1 (engine ships dark — paper)**: `crt/` engine + `crt_mode: shadow`; per-closed-bar loop on TDVA candles; gate chain with decision frozen at C2 close; fills → `nt_paper_positions(source='crt_cont')`; resolution via the CRT resolver sibling (same sl_first intrabar basis). No `nt_signal_sim` writes, no resolver changes, MT5 untouched.
- **CRT-2 (claims + monitors)**: additive CHECK migration for `kind IN (..., 'crt_option')` / `source IN (..., 'crt_walkforward')`; pre-register per-option claims at config deploy (OOS EV, decay thresholds, rolling-gate verdicts) — scored by the same EOD machinery; CUSUM state rows + rolling-EV table; confidence sizing as a shadow column (variable_kelly precedent).
- **CRT-3 (leaderboard-gated)**: the claims leaderboard arbitrates promotion to `live` exactly as it arbitrates the TSFM sidecar — no operator eyeballing; this closes the loop with the lab's standing question ("is the sidecar/CRT-class edge real after costs?").

## 7. Fidelity checklist (numbers to preserve — the determinism bar)

Setup: ATR(14) at C2 close; sweep_atr = depth/ATR; chop lookback 6 (wick ≤0.70, overlap ≤0.75, dir-changes ≤4); vol percentile lookback 500, bands 0.33/0.67 (bits 1/2/4, unknown→mid); liquidity lookback 10. CONT: entry = C2 sweep extreme ± eb·ATR, SL = opposite extreme ± sb·ATR, R fixed per option, hold bars; SL wins ties; entry on ask (buy) with slippage, market-entry spread on C3C only; expiry = signed MTM/risk at hold. Gate: CUSUM k = max(0.05, 0.5·μ₀/σ₀), H_caution 12, H_off 18, roll_n 60, roll_off_ev −0.10, react_n 10; rolling window 45d, min_n 15, EV>0, OPTION_THEN_TF pooled fallback. Sizing: 0.25%→0.50% on OOS t∈[1,3], CAUTION ×0.5, open-risk cap 1%, daily-loss stop 2%. Optimizer: folds 24m/6m, LCB z=1.28, enable = OOS n≥30 ∧ EV>0.02 ∧ t≥1.0 ∧ final LCB>0. Jest as before: byte-stable labels on frozen windows (the EGARCH seed-7 bar).

## 8. Gaps and honest risks

- **M30 absent** from the TDVA interval map — derive; verify parity vs MT4 M30 on an overlap window before trusting.
- **TDVA bar depth** (~5k/request) makes the 5-year research corpus a paged, checkpointed job — or rely on the MT4 CSV bootstrap initially.
- **Thin H1 rolling window** (README: 5–20 setups/45d) — pooled fallback is a floor, not proof; claims must record n per verdict.
- **Rolling window overlaps training data** (README's own honesty note) — the decay monitor must only count setups after `train_end`, exactly as the EA does; carry this into the claims provenance.
- **Paper-book join** is `nt_signal_sim`-keyed — CRT forks the open/close pass or owns its ledger; do not bend `nt_signal_sim` to CRT.
- **Sessions**: per-bar ET session windows are new logic (market_hours is asset-class-level); DST handled by ZoneInfo, not the MQL4 NthSunday code.
- **Stop-order fill realism**: TDVA candles approximate fills the same way the research labeler does (h3+spread ≥ entry) — consistent with the repo's existing intrabar basis; no tick data.

## 9. Verdict

CRT v5 CONT is a strong structural fit: its research half is already Python and lands as `regime_edge_crt` nearly verbatim; its live half is a renko-style `crt/` engine that reuses candles, paper book, settlement basis, claims layer, flags, and the orchestrator scheduler — the only genuinely new math is a 30-line CUSUM and the session windows. Recommended naming: keep `regime_edge_crt`, replace `regime_platform_crt` with `crt/` + a `CRTOrchestrator` job family (or sibling process). Everything ships dark behind `crt_mode`, paper-first, claims-scored, and the leaderboard decides — same contract as the TSFM rebuild.
