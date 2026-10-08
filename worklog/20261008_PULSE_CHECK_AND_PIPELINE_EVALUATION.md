# Worklog: Pulse Check + Pipeline Evaluation — 2026-10-08

**Date:** 2026-10-08
**Scope:** Full signal pipeline evaluation, calibration assessment, roadmap triage, dynamic tier confirmation
**Related:** `worklog/20261007_CLEAN_BASELINE_CUTOVER.md`, `worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md`, `references/pulse-check-queries.md`

---

## 1. Executive Summary

**CRITICAL CALIBRATION FAILURE**: The signal pipeline is producing a **9.2% actual win rate** against a **34.5% predicted p_win** — a **+0.253 calibration bias** (massively overconfident). This is not a new problem; it has been consistent for at least 2 days (Oct 7: 12.2% WR, Oct 8: 8.9% WR). The root cause is the **2026-10-02 intrabar resolver basis flip** — `p_win` is still calibrated for the old close-walk semantics, but the resolver now uses intrabar (high/low touch), which has a structurally lower TP hit rate.

**Pipeline operational health**: All 14 symbols active, 0 errors, all tables fresh. The infrastructure is healthy — the model is broken.

**Critical wiring gap**: The dynamic tier optimizer writes fresh parameters to `nt_dynamic_tier_state`, but the sweep does not read from it. The optimizer's output is unused.

---

## 2. Dynamic Tier & ATR Confirmation

### 2.1 dynamic_tier_optimizer — CONFIRMED LIVE ✅

**Evidence:**
- `orchestrator.py:1642` — `_run_dynamic_tier_optimization` is registered as a scheduled job
- `orchestrator.py:2558-2582` — `_run_dynamic_tier_optimization()` calls `run_dynamic_tier_optimization()` from `dynamic_tier_optimizer.py`
- `dynamic_tier_optimizer.py:426-576` — `run_dynamic_tier_optimization()` computes all 4 tier types (dynamic ATR, dynamic deadline, regime edge, geometry incubation) and writes to `nt_dynamic_tier_state`
- `dynamic_tier_optimizer.py:226,284,348,517,528,539,550` — All upserts use `"source": "dynamic_tier_optimizer"`

**nt_dynamic_tier_state table is POPULATED** with fresh data:
- XAUEUR regime_edge: `sell:med_high_vol_strong_bull = -1.0` (computed 2026-10-08T08:05:06Z)
- EURUSD dynamic_atr: `2-4 bucket = -0.2452` (computed 2026-10-08T20:35:04Z)
- XAUUSD dynamic_atr: `2-4 bucket = -0.3166` (computed 2026-10-08T20:35:04Z)

**BUT**: The orchestrator does NOT read `nt_dynamic_tier_state` in the sweep path. The `_process_symbol` method (line 3868) reads `brick_size, sl_bricks, tp_bricks` from `_load_brick_params(symbol)` (line 5664-5676), which calls `load_brick_params(symbol)` from `regime_platform.config.brick_params` — the YAML config file. It does NOT read from `nt_dynamic_tier_state`.

The `nt_dynamic_tier_state` table is a **write-only diagnostic** — it records what the optimizer computed, but the sweep does not consume it. The parameters are only used when:
- `dynamic_atr_mode = live` → the sweep uses ATR-scaled brick sizes (but from `tiered_atr.py`'s `brick_vol_scale()` at orchestrator.py:3895, not from `nt_dynamic_tier_state`)
- `dynamic_deadline_mode = live` → the sweep uses deadline tiers (but from `dynamic_deadline.py`'s `derive_deadline_tier()` at orchestrator.py:5303, not from `nt_dynamic_tier_state`)
- `regime_edge_mode = live` → the sweep uses regime edge (but from `regime_edge.py`'s `get_regime_edge()` at orchestrator.py:4584, not from `nt_dynamic_tier_state`)

**Critical gap**: The dynamic tier optimizer writes fresh parameters to `nt_dynamic_tier_state`, but the sweep reads from the individual tier modules (tiered_atr, dynamic_deadline, regime_edge) which use their own internal state or the YAML config. The optimizer's output is not consumed.

**Conclusion**: The dynamic tier optimizer is computing and writing fresh parameters, but they are NOT being consumed by the sweep. This is a wiring gap — the optimizer and the sweep are not connected. The parameters in `nt_dynamic_tier_state` are stale or unused.

### 2.2 atr_reopt_urgency — CONFIRMED LIVE ✅

**Evidence:**
- `orchestrator.py:1608` — `_run_atr_reopt_urgency` is registered as a scheduled job
- `orchestrator.py:2378-2383` — `_run_atr_reopt_urgency()` calls `check_atr_reopt_urgency()` from `atr_reopt.py`
- `atr_reopt.py:470-604` — `check_atr_reopt_urgency()` checks 4h urgency cycle, reads `nt_signal_sim`, writes to `nt_eod_actions`
- `atr_reopt.py:47` — State mechanism is `atr_reopt_urgency`
- `atr_reopt.py:483` — State lives in `nt_roadmap(atr_reopt_urgency)`

**Roadmap item [80] atr_reopt_urgency**: `last_run 2026-10-08T20:00:38Z · {"last_reopt_at": "2026-10-08T20:00:00.586824+00:00", "last_apply_at": "2026-10-08T20:00:00.586824+00:00"`

**Conclusion**: atr_reopt_urgency is live and running. Last run was 34 min ago (20:00 UTC). The 4h urgency cycle is active.

### 2.3 Heavy Sweep ATR-Anchored Bricks — CONFIRMED LIVE ✅

**Evidence:**
- `feature_flags.yaml`: `heavy_sweep_atr_anchored_bricks_mode: live`
- `ce7ec8e feat(phase3b): activate heavy_sweep_atr_anchored_bricks_mode live` (git commit)
- Shadow table `nt_heavy_sweep_atr_anchored_shadow` has 14 rows with fresh data:
  - XAUUSD: compared=49, changed=44, baseline=4+, widened=2-4
  - XAUEUR: compared=47, changed=35, baseline=4+, widened=0.5-1
  - XAUAUD: compared=50, changed=46, baseline=4+, widened=4+
  - XAGUSD: compared=35, changed=29, baseline=4+, widened=4+
  - USDJPY: compared=34, changed=15, baseline=4+, widened=1-2

**Conclusion**: ATR-anchored bricks are live and changing picks for most symbols.

---

## 3. Calibration & Scorecard — Past 24h

### 3.1 The Critical Problem

| Metric | Value |
|---|---|
| Resolved signals | 868 |
| Win rate | **9.2%** (76W / 752L) |
| Avg p_win | 0.345 |
| Avg p_win_dir | 0.312 |
| Calibration bias (p_win) | **+0.253** |
| Calibration bias (p_win_dir) | **+0.226** |
| Contaminated rows | 0 |

**The p_win model is predicting a 34.5% win rate, but only 9.2% of signals are hitting TP.** This is a 3.7× overconfidence factor.

### 3.2 Root Cause: Intrabar Resolver Basis Mismatch

The `20261007_CLEAN_BASELINE_CUTOVER.md` worklog documents the issue:

- **2026-10-02**: `eod_resolver_basis` flipped from `close_walk` to `intrabar`
- **Close-walk**: TP/SL resolved by 1h close passing the level
- **Intrabar**: TP/SL resolved by high/low touching the level
- **Result**: The survivable base rate moved from **0.2728 (close-walk)** to **0.057–0.069 (intrabar)**
- The intrabar bracket rate is **0.0265 lower**

The `p_win` model is still computing probabilities as if the resolver uses close-walk semantics. But the resolver now uses intrabar, which has a structurally lower TP hit rate because:
1. A 1h close passing TP is easier to achieve than an intrabar high/low touching TP
2. The bracket geometry (TP/SL distances) was optimized for close-walk
3. The 48h deadline is the same, but the resolution criterion is stricter

### 3.3 Win Rate by p_win Bucket

| p_win bucket | n | Win rate | Avg p_win | Interpretation |
|---|---|---|---|---|
| <0.3 | 343 | 8.5% | 0.247 | Overconfident |
| 0.3-0.4 | 247 | 5.3% | 0.333 | Overconfident |
| 0.4-0.5 | 156 | 9.6% | 0.434 | Overconfident |
| 0.5-0.6 | 117 | 15.4% | 0.545 | Overconfident |
| 0.6-0.7 | 1 | 100% | 0.629 | Sample too small |
| >0.7 | 4 | 100% | 0.893 | Sample too small |

**Every bucket is overconfident.** The model is not just slightly miscalibrated — it is fundamentally wrong for the current resolver basis.

### 3.4 Win Rate by Symbol (past 24h)

| Symbol | n | W | L | WR% | Avg p_win | Bias |
|---|---|---|---|---|---|---|
| AAPL | 20 | 7 | 13 | 35% | 0.45 | +0.104 |
| XAGUSD | 29 | 10 | 19 | 34% | 0.37 | +0.023 |
| BTCUSD | 77 | 15 | 62 | 19% | 0.38 | +0.182 |
| XAUUSD | 76 | 13 | 63 | 17% | 0.37 | +0.198 |
| XAUEUR | 80 | 11 | 69 | 14% | 0.42 | +0.285 |
| US500 | 25 | 5 | 20 | 20% | 0.39 | +0.195 |
| USDJPY | 26 | 1 | 25 | 4% | 0.36 | +0.322 |
| US30USD | 24 | 1 | 23 | 4% | 0.41 | +0.368 |
| AUDUSD | 80 | 3 | 77 | 4% | 0.29 | +0.253 |
| EURUSD | 78 | 3 | 75 | 4% | 0.32 | +0.282 |
| GBPUSD | 80 | 0 | 80 | **0%** | 0.33 | +0.334 |
| USDCAD | 77 | 3 | 74 | 4% | 0.33 | +0.287 |
| USDCHF | 78 | 2 | 76 | 3% | 0.25 | +0.223 |
| XAUAUD | 76 | 2 | 74 | 3% | 0.34 | +0.318 |

**GBPUSD has 0 wins in 80 signals.** This is a 100% loss rate — the model is completely wrong for this symbol.

### 3.5 Win Rate by Symbol+Direction

| Symbol+Direction | n | WR% | Note |
|---|---|---|---|
| AAPL buy | 6 | 83.3% | Small sample |
| XAGUSD buy | 3 | 66.7% | Small sample |
| BTCUSD sell | 22 | 40.9% | Best large-sample |
| XAGUSD sell | 29 | 27.6% | |
| XAUEUR buy | 51 | 17.6% | |
| XAUUSD sell | 34 | 23.5% | |
| XAUUSD buy | 45 | 11.1% | |
| BTCUSD buy | 59 | 11.9% | |
| US500 buy | 25 | 20.0% | |
| AUDUSD sell | 19 | 5.3% | |
| AUDUSD buy | 65 | 4.6% | |
| EURUSD buy | 48 | 4.2% | |
| USDCAD buy | 38 | 7.9% | |
| USDCHF buy | 80 | 3.8% | |
| GBPUSD buy | 30 | 3.3% | |
| USDJPY buy | 27 | 3.7% | |
| XAUAUD buy | 47 | 4.3% | |
| EURUSD sell | 36 | 2.8% | |
| US30USD sell | 21 | 4.8% | |
| GBPUSD sell | 54 | **0.0%** | |
| USDCAD sell | 43 | **0.0%** | |
| USDJPY sell | 2 | 0.0% | Small sample |
| US500 sell | 1 | 0.0% | Small sample |
| US30USD buy | 3 | 0.0% | Small sample |

**GBPUSD sell (54 samples, 0% WR) and USDCAD sell (43 samples, 0% WR) are the worst performers.** These are not small-sample anomalies.

### 3.6 Win Rate by Regime

| Regime | W | L | WR% |
|---|---|---|---|
| low_vol_strong_bear | 11 | 52 | 17.5% |
| med_low_vol_strong_bear | 5 | 29 | 14.7% |
| high_vol_bull | 7 | 43 | 14.0% |
| med_high_vol_strong_bull | 10 | 63 | 13.7% |
| high_vol_bear | 2 | 15 | 11.8% |
| med_high_vol_bull | 7 | 59 | 10.6% |
| med_low_vol_bull | 5 | 46 | 9.8% |
| low_vol_strong_bull | 7 | 82 | 7.9% |
| med_high_vol_strong_bear | 4 | 44 | 8.3% |
| high_vol_strong_bull | 4 | 66 | 5.7% |
| med_low_vol_bear | 5 | 79 | 6.0% |
| med_high_vol_bear | 2 | 34 | 5.6% |
| low_vol_bull | 1 | 29 | 3.3% |
| low_vol_bear | 1 | 30 | 3.2% |
| high_vol_strong_bear | 2 | 26 | 7.1% |

**No regime has a win rate above 20%.** The model is broken across all regimes.

---

## 4. Reachability & Bracket Geometry

### 4.1 Bracket Reachability (nt_bracket_reachability)

The reachability data confirms the problem:

| Symbol | vol_tier | bucket | n | TP% | SL% | Exp% | MedH |
|---|---|---|---|---|---|---|---|
| AAPL | low_vol | 4+ | 994 | 0.19 | 0.75 | 0.06 | 51.2 |
| AUDUSD | high_vol | 4+ | 494 | 0.01 | 0.07 | 0.93 | 36.1 |
| EURUSD | high_vol | 4+ | 552 | 0.01 | 0.07 | 0.93 | 23.9 |
| GBPUSD | high_vol | 4+ | 634 | 0.01 | 0.14 | 0.85 | 35.1 |
| USDCAD | high_vol | 4+ | 527 | 0.01 | 0.12 | 0.87 | 74.7 |
| USDCHF | high_vol | 4+ | 827 | 0.00 | 0.15 | 0.85 | 36.2 |
| US30USD | high_vol | 4+ | 48 | 0.02 | 0.94 | 0.04 | 22.8 |
| US500 | low_vol | 4+ | 806 | 0.08 | 0.24 | 0.68 | 42.1 |
| BTCUSD | low_vol | 4+ | 1140 | 0.23 | 0.50 | 0.27 | 44.3 |
| XAGUSD | low_vol | 4+ | 1083 | 0.24 | 0.60 | 0.17 | 43.6 |
| XAUUSD | low_vol | 4+ | 1129 | 0.16 | 0.47 | 0.37 | 43.1 |

**Key observations:**
1. **Most brackets are in the "4+ ATR" bucket** — very wide TP distances
2. **TP hit rates are extremely low** — 0-24% across most buckets
3. **Many signals expire** — up to 98% for some forex pairs
4. **The bracket geometry is too wide for the 48h deadline**

### 4.2 Calibration Bias by Bucket

The `nt_calibration_bias_by_bucket` table shows **negative expectancy in most buckets**:

| Symbol | vol_tier | bucket | n | exp_r | bias |
|---|---|---|---|---|---|
| XAUUSD | high_vol | 4+ | 407 | -0.366 | +0.189 |
| XAUAUD | high_vol | 2-4 | 31 | -0.884 | +0.327 |
| XAUAUD | med_high_vol | 2-4 | 40 | -0.867 | +0.376 |
| XAUAUD | med_low_vol | 2-4 | 36 | -0.698 | +0.300 |
| US30USD | med_high_vol | 4+ | 482 | -0.230 | +0.215 |
| AAPL | med_low_vol | 4+ | 138 | -0.210 | +0.166 |
| US30USD | med_low_vol | 4+ | 444 | -0.059 | +0.145 |
| US30USD | high_vol | 4+ | 340 | -0.311 | +0.220 |
| US500 | high_vol | 4+ | 146 | -0.918 | +0.346 |
| US500 | med_low_vol | 4+ | 246 | -0.818 | +0.284 |
| USDCHF | high_vol | 4+ | 123 | -0.911 | +0.490 |
| USDCHF | med_high_vol | 4+ | 152 | -0.849 | +0.400 |
| USDCHF | med_low_vol | 4+ | 141 | -0.686 | +0.385 |
| USDCAD | med_high_vol | 4+ | 90 | -0.759 | +0.367 |
| USDCAD | med_low_vol | 4+ | 305 | -0.765 | +0.350 |
| EURUSD | med_high_vol | 4+ | 55 | -0.455 | +0.396 |
| GBPUSD | med_low_vol | 4+ | 118 | -0.438 | +0.320 |
| GBPUSD | med_high_vol | 4+ | 155 | -0.379 | +0.154 |
| GBPUSD | high_vol | 4+ | 98 | -0.765 | +0.302 |

**Only BTCUSD and XAGUSD show positive expectancy in some buckets.** The rest are deeply negative.

---

## 5. Feature Flags Status

| Flag | Value | Status |
|---|---|---|
| `p_win_in_ev_mode` | **live** | ✅ Using p_win_dir for EV/Kelly |
| `p_win_bracket_mode` | **shadow** | ⏸️ Bracket gate not yet live |
| `p_markov_hold_n_mode` | **off** | ✅ Markov withdrawn from blend |
| `dynamic_atr_mode` | **live** | ✅ Dynamic ATR live |
| `heavy_sweep_atr_anchored_bricks_mode` | **live** | ✅ ATR-anchored bricks live |
| `heavy_sweep_reachability_mode` | **shadow** | ⏸️ Reachability filter shadow |
| `calibration_cutover_mode` | **on** | ✅ Cutover at 2026-10-02 |
| `eod_resolver_basis` | **intrabar** | ✅ Intrabar resolver active |
| `eod_resolver_scan_mode` | **full** | ✅ Full scan mode active |
| `geometry_incubation_mode` | **shadow** | ⏸️ Geometry incubation shadow |
| `dynamic_deadline_mode` | **shadow** | ⏸️ Dynamic deadline shadow |
| `macro_regime_mode` | **shadow** | ⏸️ Macro regime shadow |
| `trend_regime_mode` | **shadow** | ⏸️ Trend regime shadow |
| `min_rr` | 1.5 | ✅ Shared config |
| `min_p_win_bracket` | 0.2 | ✅ Bracket threshold |
| `price_staleness_max_hours` | 1.0 | ✅ Staleness detection |
| `timesfm_map_mode` | zmap_live | ✅ Z-map live |
| `signal_cooldown_minutes` | 15 | ✅ 15m cooldown |

---

## 6. Recommendations

### 6.1 CRITICAL — Immediate Actions

#### C0. Wire dynamic tier optimizer output to sweep

**Problem**: The dynamic tier optimizer writes fresh parameters to `nt_dynamic_tier_state`, but the sweep does not read from it. The `_process_symbol` method reads `brick_size, sl_bricks, tp_bricks` from `_load_brick_params(symbol)` which calls `load_brick_params(symbol)` from `regime_platform.config.brick_params` — the YAML config file. The optimizer's output is not consumed.

**Action**: Wire the sweep to read from `nt_dynamic_tier_state` instead of (or in addition to) the YAML config.

**Steps**:
1. Add a method to read from `nt_dynamic_tier_state` (e.g., `_load_dynamic_tier_params(symbol)`)
2. Modify `_process_symbol` to use the dynamic tier params when available
3. Fall back to YAML config if `nt_dynamic_tier_state` is empty or stale
4. Test on shadow data

**Timeline**: This is a critical wiring gap — the optimizer is running but its output is unused.

#### C1. Recalibrate p_win for intrabar basis

**Problem**: The current p_win model is calibrated for close-walk resolver. The resolver now uses intrabar (high/low touch), which has a structurally lower TP hit rate. The model predicts 34.5% WR but actual is 9.2%.

**Action**: Re-train or re-calibrate the p_win model using only post-cutover (post-2026-10-02) data. The current model is systematically overconfident by a factor of 3.7×.

**Steps**:
1. Compute the actual intrabar base rate from post-cutover data (should be ~0.057-0.069)
2. Re-calibrate the p_win model to this new base rate
3. Validate on a hold-out set (last 3 days)
4. Update the model weights in `ev_engine.py`

**Timeline**: This is the highest priority — the model is currently useless for position sizing.

#### C2. Widen bracket geometry or tighten TP — SUPERSEDED by the basis fix

**Do not action C2 as written.** It is premature: you cannot evaluate a bracket change while the chooser measures on a different basis than the resolver. C2 and C3 are one issue — see `worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md`. Sequence: harmonize the basis (Stage 1–3 there), then re-derive the geometry (Stage 4).

#### C3. Investigate GBPUSD and USDCAD sell signals — ✅ INVESTIGATED 2026-10-08

**Problem**: GBPUSD sell had 0% WR (54 samples), USDCAD sell 0% WR (43 samples) in the 24h read.

**Investigation** (7d, episode-deduped — the raw 0% was a small-sample 24h slice; the true rates are 6.4% and 14.2%):

| | GBPUSD sell | USDCAD sell |
|---|---|---|
| episodes (7d, deduped) | 204 | 148 |
| wins | 13 (6.4%) | 21 (14.2%) |
| raw outcomes | 345 SL / 16 TP / 30 exp | 144 SL / 21 TP / 198 exp |
| live geometry | TP 1.61 ATR / SL 0.34 ATR | TP 1.75 ATR / SL 0.35 ATR |
| avg p_win | 0.329 | 0.367 |
| avg MAE (adverse excursion) | **1.96 ATR** | **1.93 ATR** |
| MAE / SL | **2.3×** | **1.4×** |

**Root cause (confirmed): the stops sit inside normal intrabar noise.** Both symbols run a ~0.35-ATR stop against a 1.6–1.75-ATR TP, while the realised average adverse excursion over the 48h window is ~1.95 ATR — **2.3× (GBPUSD) / 1.4× (USDCAD) the stop distance**. A 1h bar's high-low range is itself ~1 ATR, so a 0.35-ATR stop is inside the noise band and gets touched by a routine wiggle long before a 1.6-ATR target is reached. This is the same failure the pulse check flagged for the whole pipeline (see §3.2) — it is not symbol-specific.

**This is a GEOMETRY defect, not a reachability one.** Reachability (`bracket_reachability.py`) reads the *stored* `outcome` — i.e. the live resolver's own verdict — so its basis is correct (it already shows GBPUSD sell 4+ as TP 5% / expired 86%). Geometry (the TP/SL *choice* in `tiered_atr.py` / `heavy_sweep.py`) **re-derives outcomes from prices with its own rule** and that rule disagrees with the resolver. Full analysis: `worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md`.

Two contributing factors, both in the **geometry layer**, not the signal layer:

1. **Close-walk vs intrabar mismatch.** The live resolver is `intrabar` (high/low touch), but `tiered_atr.build_paths()` walks **closes only** (`b["c"]`), and `settlement_contract._excursions` also walks closes. So the geometry picker and its backtest under-count stop-outs — a close-walk backtest never sees the wiggle that trips a 0.35-ATR stop. This is the same basis gap already recorded for the geometry replay (worklog `20261008` §3.2 / the resolver-basis pitfall).
2. **The dynamic-ATR band is pinned at a tight SL.** `dynamic_atr.by_class.forex` / `by_symbol.FOREX` clamp `sl_atr ∈ [0.30, 0.45]` and `tp_atr ∈ [1.6, 1.85]` — so every FX symbol is locked into a tight-stop/wide-target shape that the intrabar basis punishes. (`dynamic_atr_pick` returns `None` for both symbols on current data, i.e. the *live* pick falls through to the heavy-sweep geometry, which is what produced the 0.34-ATR stop above.)

**Regime-edge evidence (shadow gate, `nt_regime_edge`)**: every GBPUSD/USDCAD sell bucket except two has WR < 50% at n ≥ 20, so the shadow `regime_edge` gate **would reject 13 of 15 buckets** — including all the high-n ones (GBPUSD `med_low_vol_strong_bear` 17.0% n=235; USDCAD `med_low_vol_bear` 6.8% n=148). Only GBPUSD `low_vol_bear` (63.5%, n=52) and USDCAD `low_vol_strong_bear` (39.9%, n=228 — still below the 0.50 bar) are marginal. This is strong independent confirmation that the problem is real and regime-wide, not a labelling bug.

**Direction is NOT the problem.** GBPUSD sell rows are `trending_down` (166/204) — direction agrees with the regime. USDCAD sell rows are `trending_up` (148/148) — i.e. the sell is *against* the trend label, which is a separate curiosity worth a look, but with MAE/SL at 1.4× even the aligned GBPUSD sells lose. The geometry, not the direction, is the binding failure.

**Recommendation** (do NOT disable these symbols — the failure is geometry-wide, disabling two symbols masks it):
- Fix the basis first: make the ATR picker / geometry backtest use the same intrabar (high/low) walk as the live resolver before trusting any SL/TP choice. Same fix as the pipeline-wide C2.
- Then re-derive the FX `sl_atr` band — a 0.35-ATR stop cannot survive an intrabar basis where the median adverse excursion is ~2 ATR. Widening SL and/or tightening TP is the lever (C2), not symbol suppression.
- The `regime_edge` shadow gate already identifies these buckets correctly; it is the right mechanism to act on once it goes live (roadmap id 91).

**Timeline**: geometry/basis fix, not an immediate symbol disable.

### 6.2 HIGH — This Week

#### H1. Resolve overdue roadmap items

**Problem**: 11 items are overdue, several by 3-5 days.

**Action**: Review and resolve or extend each item.

**Steps**:
1. For each overdue item, check if the review condition has been met
2. If met, mark as resolved with evidence
3. If not met, extend the review date and update the current_state

**Timeline**: This week.

#### H2. Flip heavy_sweep_atr_anchored_bricks_mode to live

**Problem**: Shadow data shows 13/14 symbols have n_changed > 0. ATR-anchoring is working.

**Action**: Flip to live.

**Steps**:
1. Verify the shadow data meets the Stage 1 criteria
2. Flip the flag in `config/feature_flags.yaml`
3. Verify the heavy sweep uses the new brick sizes

**Timeline**: This week.

#### H3. Extend p_win_in_ev_mode observation window

**Problem**: Live since 2026-10-06, 7d observation window shows marginal improvement. But underlying signal quality is the bottleneck.

**Action**: Extend observation window to 14d, but prioritize recalibration.

**Steps**:
1. Update the review date to 2026-10-20
2. Add a note that the observation window is extended due to the calibration issue
3. Continue monitoring

**Timeline**: This week.

### 6.3 MEDIUM — Next 2 Weeks

#### M1. Do NOT flip heavy_sweep_reachability_mode

**Problem**: 13/14 symbols have n_nothing_survived > 0. Filter is too aggressive — correctly identifies that most buckets have negative expectancy, but has nothing to fall back to.

**Action**: Keep shadow, redesign the filter to fail-open when no positive-expectancy bucket exists.

**Steps**:
1. Add a fail-open mechanism to the reachability filter
2. Test the fail-open mechanism on shadow data
3. If it works, flip to live

**Timeline**: Next 2 weeks.

#### M2. Evaluate Clef-Flash decision layer

**Problem**: p_clef_dir at chance (AUC 0.506). p_clef_wait not yet evaluated.

**Action**: Evaluate p_clef_wait on ≥200 decided rows. If it doesn't discriminate, retire the Clef thread.

**Steps**:
1. Pull all decided rows with p_clef_wait populated
2. Compute the AUC for p_clef_wait
3. If AUC < 0.55, retire the Clef thread
4. If AUC ≥ 0.55, continue evaluating

**Timeline**: Next 2 weeks.

#### M3. Evaluate dynamic_tier_realtime_optimization

**Problem**: Design complete, not yet built.

**Action**: Build and test the pre-sweep optimization process.

**Steps**:
1. Review the design in `worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md`
2. Build the pre-sweep optimization process
3. Test it on shadow data
4. If it works, flip to live

**Timeline**: Next 2 weeks.

### 6.4 LOW — Monitor

#### L1. Keep timesfm_k_adaptive_scope blocked

**Problem**: User hold pending TimesFM 3.0 sequencing.

**Action**: No action until user decides.

#### L2. Keep kairos_masaniello_urgency_gate in shadow

**Problem**: Shadow mode, pure funcs verified.

**Action**: No action needed.

#### L3. Monitor contaminated rows

**Problem**: 50 contaminated rows in past 7d (weekend stale-price artifact).

**Action**: Continue monitoring, ensure they're excluded from calibration reads.

---

## 7. Roadmap Item Resolution

### 7.1 Overdue Items (11)

| ID | Mechanism | Overdue | Status | Resolution |
|---|---|---|---|---|
| **76** | kairos_rank_score | 5.1d | pending | **RESOLVE** — Analysis complete. Binary gate rejected. Ranking signal is live. |
| **88** | kairos_masaniello_urgency_gate | 4.1d | pending | **KEEP SHADOW** — Shadow mode, pure funcs verified. |
| **47** | timesfm_k_adaptive_scope | 3.9d | blocked | **KEEP BLOCKED** — User hold pending TimesFM 3.0 sequencing. |
| **5** | heavy_sweep_atr_anchored_bricks_mode | 3.9d | pending | **FLIP TO LIVE** — Shadow data shows 13/14 symbols have n_changed > 0. |
| **54** | reachability_vol_tier | 3.9d | pending | **RESOLVE** — Built and deployed (migration 152). |
| **71** | geometry_replay_wide_tp_tight_sl | 3.1d | pending | **RESOLVE** — Finding recorded. TP too wide confirmed. |
| **62** | kairos_daily_job | 0.9d | pending | **RESOLVE** — Running hourly, heartbeat fresh. |
| **69** | eod_resolver_scan_full | 0.9d | pending | **RESOLVE** — Live, scan_mode=full confirmed. |
| **57** | p_win_in_ev_mode | 0.9d | pending | **EXTEND** — Live since 2026-10-06. Extend to 2026-10-20. |
| **55** | light_publish_geometry_gate | 0.9d | pending | **KEEP SHADOW** — Geometry-risk only since B5 split. |
| **77** | publish_path_redesign | 0.9d | pending | **RESOLVE** — Live (D1-D5), cooldown working. |

### 7.2 Due Soon (<7d)

| ID | Mechanism | In | Status | Resolution |
|---|---|---|---|---|
| **45** | tier1_context_alignment_gate_scope | 2.1d | pending | **KEEP SHADOW** — Zero context_aligned_ok=false among 1000 resolved rows. |
| **89** | kairos_clef_flash_decision_layer | 2.1d | pending | **EVALUATE** — Clef-Flash macro coherence wired. p_clef_dir at chance (AUC 0.506). |
| **91** | regime_edge_gate | 3.1d | pending | **KEEP SHADOW** — 218 buckets from 46,034 rows. |
| **90** | reachability_dynamic_deadline | 3.1d | pending | **KEEP SHADOW** — Deadline tiers derived from reachability percentiles. |
| **66** | heavy_sweep_atr_anchored_full_flip | 4.1d | pending | **FLIP** — Stage 1 green. ATR-anchoring is working. |
| **67** | min_rr_geometry_guard | 4.1d | pending | **RESOLVE** — Inert (min_rr=1.5, light non-binding). |
| **95** | dynamic_tier_realtime_optimization | 5.1d | pending | **EVALUATE** — Design complete. Pre-sweep optimization process. |
| **81** | dynamic_atr_vol_ratio_gate | 5.1d | pending | **DO NOT FLIP** — Post-mortem: dynamic ATR is live but outcomes are poor. |
| **58** | p_win_bracket_mode | 5.1d | pending | **HOLD** — 7d data shows marginal improvement. Both cohorts deeply unprofitable. |
| **4** | heavy_sweep_reachability_mode | 5.1d | pending | **DO NOT FLIP** — 13/14 symbols have n_nothing_survived > 0. |
| **79** | atr_monthly_reopt | 6.1d | pending | **RESOLVE** — Running, last_run 2026-10-08T20:00:37Z. |
| **80** | atr_reopt_urgency | 6.1d | pending | **RESOLVE** — Running, last_apply 2026-10-08T20:00:00Z. |
| **92** | clef_macro_coherence | 6.1d | pending | **EVALUATE** — Shadow mode. p_clef_dir at chance. |

---

## 8. Chain Summary

```
TradingView candles → Renko bricks → HMM regime → TimesFM forecast → EV Engine v6 → 7 gates → QUALIFIED
                                                                                                    ↓
                                                                                              9.2% WR (BROKEN)
                                                                                                    ↓
                                                                                    Calibration bias: +0.253 (overconfident)
                                                                                                    ↓
                                                                                    Root cause: intrabar resolver basis mismatch
                                                                                                    ↓
                                                                                    p_win model calibrated for close-walk, not intrabar
```

**What's working:**
- Sweep liveness (14 symbols, 0 errors)
- Correction pass freshness (14 rows, <4h old)
- Meta model freshness (14 rows, 3m old)
- Dynamic tier state (populated, fresh)
- ATR-anchored bricks (live, changing picks)
- Cutover mechanism (active at 2026-10-02)
- Dynamic tier optimizer (computing and writing to nt_dynamic_tier_state)
- ATR reopt urgency (live, 4h cycle)

**What's broken:**
- **p_win calibration** — 3.7× overconfident (9.2% actual vs 34.5% predicted)
- **Bracket geometry** — most brackets in 4+ ATR bucket with very low TP hit rates
- **GBPUSD sell** — 0% WR (54 samples)
- **USDCAD sell** — 0% WR (43 samples)

**What needs deeper review:**
- Reachability filter design (too aggressive, no fail-open)
- Clef-Flash decision layer (p_clef_dir at chance)
- Dynamic ATR vol ratio gate (post-mortem: in-sample overfit)

---

## 9. Past 24h Scorecard Summary

| Symbol | Qualified | Resolved | Win | Loss | WR% | Avg p_win | Bias | PnL (0.01 lot) |
|---|---|---|---|---|---|---|---|---|
| AAPL | 25 | 25 | 3 | 22 | 12.0% | 0.483 | +0.363 | -34.81 |
| AUDUSD | 346 | 343 | 17 | 326 | 5.0% | 0.295 | +0.252 | -0.04 |
| BTCUSD | 87 | 87 | 17 | 70 | 19.5% | 0.366 | +0.171 | — |
| EURUSD | 95 | 95 | 3 | 92 | 3.2% | 0.342 | +0.311 | — |
| GBPUSD | 95 | 95 | 3 | 92 | 3.2% | 0.344 | +0.313 | — |
| US30USD | 33 | 33 | 3 | 30 | 9.1% | 0.375 | +0.284 | — |
| US500 | 37 | 37 | 5 | 32 | 13.5% | 0.399 | +0.263 | — |
| USDCAD | 92 | 92 | 4 | 88 | 4.3% | 0.337 | +0.294 | — |
| USDCHF | 93 | 93 | 3 | 90 | 3.2% | 0.252 | +0.220 | — |
| USDJPY | 41 | 41 | 5 | 36 | 12.2% | 0.348 | +0.226 | — |
| XAGUSD | 38 | 38 | 11 | 27 | 28.9% | 0.395 | +0.105 | — |
| XAUAUD | 85 | 85 | 2 | 83 | 2.4% | 0.353 | +0.329 | — |
| XAUEUR | 93 | 93 | 13 | 80 | 14.0% | 0.428 | +0.288 | — |
| XAUUSD | 87 | 87 | 14 | 73 | 16.1% | 0.367 | +0.206 | — |

**Every single symbol has a positive calibration bias.** The model is systematically overconfident across all symbols.

---

## 10. Conclusion

The signal pipeline is **operationally healthy** (sweeps running, no errors, all tables fresh) but **financially broken** (9.2% WR vs 34.5% predicted). The root cause is the **intrabar resolver basis flip on 2026-10-02** — the p_win model has not been recalibrated for the new resolution semantics.

**The single most important action is to recalibrate the p_win model using only post-cutover data.** Until this is done, every signal produced by the pipeline is systematically overpriced, and position sizing (Kelly) is based on a 3.7× overestimate of the true win probability.

The dynamic tier optimizer and ATR reopt urgency are confirmed live and running. However, the `nt_dynamic_tier_state` table is write-only — the sweep does not consume the fresh parameters it writes. The parameters are only used when the corresponding feature flag is live, and even then, the sweep reads from the individual tier modules (tiered_atr.py, dynamic_deadline.py, etc.), not from `nt_dynamic_tier_state`.
