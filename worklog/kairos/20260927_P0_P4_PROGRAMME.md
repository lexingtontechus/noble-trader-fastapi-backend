# P0–P4 programme — beyond p_win (event-time edge, TimesFM covariates, text)

**Date:** 2026-09-27 · **Type:** programme worklog · **Parent:** research `research/financial-llm-regime-edge/REPORT.md`, R1 `20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md`, scope `scopes/20260927_P0_HALFLIFE_T2_SCOPE.md`  
**Constraint (binding):** Kairos is a **primary** component — no bolt-on complexity. Queries and code stay lean. No LLM in P0–P1.

---

## 0. Why this programme

`p_win` is a level forecast and does not rank within cells (IC≈0). Age/plane knobs are dead. What still has signal: **typed regime transitions in event time** (T1 +0.117 → refresh +0.121) and **post-transition freshness**. Hedge/academic practice uses LLMs as **features and typed-label extractors**, not price oracles; TSFMs need **decision-aligned** outputs (quantiles / ranks), not raw MSE.

External evidence (see research report): TimesFM-class is a weak prior; LLM news alpha fades; text can **lead** regime boundaries; edge decay is event-time half-life after an event.

---

## 1. Programme map

| Phase | Product | Builds on | Status |
|---|---|---|---|
| **P0** | Event-time half-life `h` by transition type (T2) | R1 event-time windows, `kairos_rank` | **DONE 2026-09-27** |
| **P1** | TimesFM interface: quantiles / **dynamic covariates** / rank features | `p_timesfm`, server.py:8002 | **Scope next** |
| **P2** | Macro/event text → typed pivot labels + T-Layer prior | `nt_macro_forecast`, HMM cells | **Done 2026-09-28 (P2.3 stamps gated)** — see `20260928_P2_PIVOTS_COMPLETE.md` |
| **P3** | R-Layer ranker (replace level `p_win` as ranker) | P0 τ/type + P1 features | Open |
| **P4** | News/macro embedding residual + small LLM (LFM / Qwen-class) | Partial: macro forecast path | Partially built |

```text
prices/HMM ──► transitions (τ, type) ──► P0 half-life ──► freshness weight
                     │                         │
TimesFM 2.5/3.0 ─────┼── covariates/quantiles ─┼──► P3 R-Layer rank score
                     │                         │
text (P2/P4) ────────┴── typed pivots ─────────┘
```

---

## 2. P0 — DONE (research-only)

**Question T2:** does edge decay in event time after a transition, with stable `h` by type?

| Stratum | n | b (fresh lift) | h | p_b | Half-split | Verdict |
|---|---:|---:|---:|---:|---|---|
| **dir_flip** | 12,871 | +0.205 | **6h** | 0.00 | b>0 both halves | **supported** |
| vol_expand | 10,446 | +0.102 | 12h | 0.17 | sign flip | not_supported |
| vol_contract | 8,828 | −0.063 | 6h | 0.78 | sign flip | not_supported |
| both | 4,388 | −0.132 | 3h | 0.95 | unstable | not_supported |
| pooled | 36,533 | +0.124 | **12h** | 0.00 | stable | supported |

T1 refresh: fresh − deep = **+0.121**. Model: `y_acc ≈ a + b·2^(−τ/h)`, circular-shift null, chronological half-split.

**Build:** `kairos_rank.exp_decay_fit` / `event_time_halflife` / `halflife_null` · `scripts/kairos_halflife.py` (parallel PostgREST pages) · `tests/test_kairos_halflife.py` (20 green with rank tests).

**Product meaning (not wired):** treat **`dir_flip` freshness as a ~6h information clock**; do **not** age-knob other types. Do not put `h` into `rank_score` until post-ATR mass (R4 bar).

**Report:** `reports/20260927_halflife_t2.md` · **Scope:** `scopes/20260927_P0_HALFLIFE_T2_SCOPE.md`

---

## 3. P1 — TimesFM interface (next build)

**Goal:** stop treating `p_timesfm = sigmoid(k·Δp)` as a win probability. Feed the **EV / R-Layer** decision-aligned TimesFM outputs, including **dynamic covariates**.

### 3.1 Why covariates (user note, 2026-09-27)

TimesFM **3.0** is the multivariate / dynamic-covariate line (cross-attention over aux channels). User: **this may already be possible on 2.5** with a multi-channel input matrix — treat 3.0 as the capability target and 2.5 as the experiment vehicle on Studio (`server.py` :8002).

Source guide (user attachment): `TimesFM Multivariate Guide.md` — four covariate buckets, stationary transforms, `[Batch, Features, Time]` payload to `POST /forecast`.

### 3.2 Covariate set (lean, what we already compute)

| Bucket | Channels (P1 keep-list) | Notes |
|---|---|---|
| Target | Close / log-return | log-return for stationarity (guide §2) |
| Technical | ADX or trend-strength proxy, RSI, ATR ratio run | Prefer series we already stamp on `nt_signal_sim` |
| System state | HMM `P(regime)` / cell posteriors, optional markov hold | Align to light-sweep bars — **no new HMM** |
| Cross-asset | cluster-mate log-returns (gold complex / USD index / EUR bloc) | ~3–5 series, not a full matrix |
| Skip for P1 | L2 book imbalance, full depth | No reliable L2 on this book yet |

Stationarity (binding): no raw EMAs; use % distance / log-return / z-score. Context window 500–1000 bars per guide.

### 3.3 Deliverables

| Artefact | What |
|---|---|
| `timesfm_multivariate.py` (orchestrator or server) | Build `[1, F, T]` tensor + call :8002; fallback univariate if channel missing |
| Feature allowlist | Frozen column list — **no** feature dump |
| p_map | Prefer **quantile / interval hit** under recorded brackets over sigmoid(k·Δp); keep clamp but calibrate on `predicted_p_win_raw` |
| Rank features | `timesfm_m12`, `timesfm_q10/q90`, `timesfm_dir_agree` → R-Layer only |
| Report | Walk-forward: does any TimesFM feature have within-cell IC ≠ 0 on `y_acc`? |

### 3.4 Stage gates

| Gate | Pass |
|---|---|
| P1.1 | Multivariate call runs on 1 symbol with HMM + RSI + ATR + cluster return (or documented 2.5 limitation) |
| P1.2 | ≥1 TimesFM feature with within-cell IC ≠ 0 **or** ΔBrier ≤ −0.02 vs current `p_timesfm` (both halves) |
| P1.3 | If fail → demote `p_timesfm` weight; do not gold-plate 3.0 migration |

**Out of P1:** LoRA fine-tuning, training from scratch, gating live, changing EV weights.

### 3.5 P1.1 result (2026-09-27) — 2.5 covariates **work** (XReg path)

Probe: `scripts/timesfm_p1_covariates_probe.py` · report `reports/20260927_p1_timesfm_covariates.md`

| Check | Result |
|---|---|
| `forecast_with_covariates` on 2.5 class | **Present** (XReg residual path, not variate attention) |
| Needs | `ForecastConfig(return_backcast=True)` + `timesfm[xreg]` (jax + sklearn — installed for probe) |
| A univariate `forecast()` | OK · 0.40s |
| B `xreg + timesfm` + 4 dynamic covariates (trend_z, mom, vol_abs, cluster_ret) | **OK** · 5.5s · finite outputs |
| C multi-series `timesfm + xreg` (2 series) | **OK** · 0.87s · shape (2, 1) |
| Native joint multivariate attention | **TimesFM 3.0 only** (`google/timesfm-3.0-pytorch`, non-commercial weights — OK for this non-live repo per user 2026-09-27) |

**Caveats:** XReg path first values sat on last price in the synthetic probe — verify level/delta mapping before any `p_timesfm` use. Server `/forecast` still univariate-only. Univariate response already returns **quantiles** (shape `1×H×10`) that the orchestrator discards — cheapest P1 win before any 3.0 swap.

**P1.2 next:** wire lean channels on live light bars → extract rank features (q10/q50/q90 spread, direction) → IC on `y_acc`. Keep EV formula untouched.

### 3.6 P1.2 result (2026-09-28) — `timesfm_gap` ranks; level is `cell_p`

Script `scripts/kairos_p12_timesfm_ic.py` · report `reports/20260927_p12_timesfm_ic.md`  
Join: `nt_signal_sim` ⋈ `nt_sweep_result` (p_timesfm + ta_* live on sweep, not signal) · n=37,899 decided.

| Feature | within-cell IC | h1 / h2 wic | Verdict |
|---|---:|---|---|
| **`timesfm_gap` (p_timesfm − p_win)** | **+0.037** | **+0.039 / +0.042** | **PASS (stable both halves)** |
| timesfm_side_fresh | +0.031 | −0.019 / +0.034 | sign flip — fail |
| timesfm_side | +0.021 | −0.038 / +0.022 | sign flip — fail |
| p_timesfm (level) | +0.016 | — | weak ranker |
| ta_adx | −0.053 | +0.057 / −0.072 | sign flip — fail |

**Brier:** `cell_p` 0.167 vs `p_timesfm` 0.257 (Δ **−0.091**, gate −0.02) — **level should be cell-shrink**, not raw `p_timesfm` (matches R1).

**P1.2 gate: PASS** on `timesfm_gap` (within-cell IC ≠ 0, both halves).  
**Do not** expand covariate research yet — gap is a *disagreement* feature between two scorers, not a new TimesFM channel. Quantiles + dynamic covariates stay P1.3/3.0-track. R-Layer (P3) can use `timesfm_gap` immediately as a rank input.

**Covariates research/scope:** no expansion needed for P1.2. Open only if P3 wants quantiles (`q_spread`) or 3.0 variate attention — already scoped in `20260831`/`20260915` docs.

---

## 4. P2 — Macro/event text → typed pivots (T-Layer)

**Goal:** leading **boundary** channel, not continuous forecasting.

| Piece | Spec |
|---|---|
| Inputs | `nt_macro_forecast` + calendar/FOMC-style events (already cached in `kairos.py`) |
| Labels | `major_pivot` / `incremental` / `no_change` on reaction-function / tool / trajectory (literature taxonomy) — **or** product types `dir_flip` / `vol_expand` if mapped cleanly |
| Prior | 0–1 pivots / unit-time base rate to suppress over-detection |
| Use | (a) T-Layer: raise flip prior before HMM confirms; (b) confirm/deny HMM `dir_flip` (union+dedup); (c) R-Layer feature **only when macro state calm** |
| Gate | LLM features **off** under macro-shock / vol_expand stress (documented sign-flip risk) |

**Stage:** P2.1 extraction eval on labeled historical events · P2.2 lead-lag vs prices · P2.3 only then stamp.

### 4.1 P2.1 build (2026-09-28) — extractor ready; live eval needs LLM keys

| Piece | Path |
|---|---|
| Pure helpers | `kairos_pivot.py` — `validate_pivot`, `map_to_product_type`, `pivot_prior_ok`, `is_policy_event`, batch prompt |
| Eval script | `scripts/kairos_p2_pivot_extract.py` — gold (10) + calendar sample (policy/data split) |
| Tests | `tests/test_kairos_pivot.py` (7 green) |
| Report | `reports/20260927_p2_pivot_extract.md` |

**Status:** schema + prior + product map built. Live gold accuracy / major_pivot rates **blocked locally** — `OPENROUTER_API_KEY` / `NVIDIA_NIM_API_KEY` are Studio secrets (same `llm_client` as `nt_macro_forecast`). Re-run the script on Studio to fill §1–§2.

**Not built yet:** P2.2 lead-lag vs HMM `dir_flip`, P2.3 stamps.

### 4.2 P2.2 result (2026-09-28) — short co-movement only, **not** a multi-day lead

Script `scripts/kairos_p22_leadlag.py` · report `reports/20260927_p22_leadlag.md`  
Flips n=4,448 (`dir_flip`+`both`) · T0 n=755 (policy 170 + ≥5% surprise data 585) · 2025-01 → now.

| Window after T0 | flips/h | **lift vs baseline** |
|---|---:|---:|
| **0–6h** | 0.384 | **1.31×** |
| 6–24h | 0.272 | 0.93× |
| 24–72h | 0.223 | 0.76× |
| 72–168h | 0.287 | 0.98× |

Baseline 0.293 flips/h.

**Read:** calendar/policy **timestamps** mildly cluster with immediate `dir_flip` (1.3× in 0–6h) and **do not** lead for days. The literature “text leads weeks” result is about **statement text**, not the calendar print. T-Layer therefore needs **narrative** (FOMC minutes / macro_forecast `narrative`) as the leading channel — not more calendar plumbing.

**P2.3 stamps remain gated** until a narrative lead-lag is shown. Do not raise flip priors on calendar T0 alone.

---

## 5. P3 — R-Layer ranker

**Goal:** rank within cell; keep `p_win` as **eligibility / EV level** only.

Frozen target: `y_acc = 1{TP} − SL/(TP+SL)` (episode unit).

Minimal feature set (do not grow this list casually):

| # | Feature | Required? | Source |
|---|---|---|---|
| 1 | τ + **`dir_flip` × exp(−τ/6h)** | **Yes** | P0 |
| 2 | type (`dir_flip` prior only) | **Yes** | P0 |
| 3 | `recent_tp_rate(3h)` | **Yes** | `rank_score` |
| 4 | cell-shrink `cell_p` | **Yes** | `rank_score` |
| 5 | **`timesfm_gap`** (p_timesfm − p_win) | **Yes** | P1.2 PASS |
| 6 | P2 pivot intensity | Optional | only if lead-lag passes |
| 7 | TimesFM quantiles (`q_spread`, `q_pos`) | **Optional** | P1.3 capture — **not required for P3** |
| 8 | Dynamic covariates / 3.0 MV | Optional | experiment only |

**Quantiles are optional (2026-09-28):** not stored today; P1.2’s ranking signal is `timesfm_gap` disagreement, not quantile shape. Do not block P3 or build a capture path first.

Model: logistic or small GBM — **not** a deep net. Walk-forward, symbol-day cluster CI, Holm over metrics. Promotion bar: ΔD CI > 0 on ≥200 post-ATR episodes (same as shadow gate). Fail-open stamp only until then.

### 5.1 P3 result (2026-09-28) — keep **base** `rank_score`; `timesfm_gap` marginal

Script `scripts/kairos_p3_rlayer.py` · report `reports/20260928_p3_rlayer.md` · n=38,012 decided (gap-joined).

| Score | pooled IC | within-cell IC | ΔD @P60 |
|---|---:|---:|---|
| **base** (fresh+type+recent_tp+cell_p) | 0.278 | **0.342** | **+0.410** [+0.401, +0.419] |
| P3 (+ timesfm_gap) | **0.292** | 0.331 | +0.406 [+0.397, +0.414] |
| timesfm_gap alone | 0.063 | 0.041 | — |

Half-split stable both sides (P3 pooled slightly higher). **Decision:** ship **base `rank_score`** as the R-Layer; keep `timesfm_gap` as optional / shadow weight only — does not beat base on within-cell IC. No live gate flip.

**Also shipped:** `docs/LLM/existing_symbols_12m_forecast_model_2027.md` + `ForecastSource` for all non-commodity `nt_symbol` rows (forex, metals, indices, equities, crypto). Next agent run fills `nt_macro_forecast` universe.

---

## 6. P4 — Macro forecast (partial) + small LLM

**Already built:** macro forecast writes **`nt_macro_forecast`** via **OpenRouter / NVIDIA**. That is the production text/macro path — do not fork it.

**Add (when P2 needs extraction quality):**

| Option | Role |
|---|---|
| Keep OpenRouter/NVIDIA | Default for `nt_macro_forecast` + pivot extraction |
| **LFM** (or Qwen / Llama 7–14B instruct) | Local / cheap extractor for typed labels + structured JSON |
| Skip | BloombergGPT-scale, local TSFM duplicate, free-form “market commentary” |

**News embeddings (later):** RAM-style bottleneck embeddings → short-horizon residual for liquid names; expect **adoption decay**; schedule re-fit. Not a P2 blocker.

**De-bias:** anonymize tickers in offline text backtests (distraction bias); point-in-time vintages only.

---

## 7. Decision summary

1. **T2 is real for `dir_flip`:** freshness lift decays with **h ≈ 6h** in event time (pooled 12h). Age remains dead.  
2. **P1 is the next build:** TimesFM **dynamic covariates** on 2.5 experiment path toward 3.0; decision-aligned outputs into ranking, not another sigmoid.  
3. **Text is P2/P4:** typed pivots + existing `nt_macro_forecast`; small LLM (LFM / Qwen-class) only as extractor.  
4. **P3 ranks; `p_win` stays level/gate.** No live knobs until post-ATR power.  
5. **Stay lean:** allowlisted covariates, episode units, fail-open stamps, one lever at a time.

---

## 8. Open items

- Confirm whether Studio `server.py` TimesFM 2.5 accepts multi-channel `inputs` as in the multivariate guide, or needs a 3.0 checkpoint.  
- Which posteriors the live HMM exposes cheaply for covariates (avoid recompute).  
- Macro-shock detector for gating P2/P3 text features (simple vol_expand or calendar flag).  
- Pre-existing suite fail: `test_yaml_ships_shadow` (`calibration_resolve_hourly_mode` live vs expected shadow) — not this programme.

---

## 9. Index

| Path | What |
|---|---|
| `scopes/20260927_P0_HALFLIFE_T2_SCOPE.md` | P0 scope + result table |
| `reports/20260927_halflife_t2.md` | Live T2 report |
| `scripts/timesfm_p1_covariates_probe.py` | P1.1 covariates probe |
| `reports/20260927_p1_timesfm_covariates.md` | P1.1 result |
| `research/financial-llm-regime-edge/REPORT.md` | External evidence (5 angles + arXiv) |
| `worklog/20260831_TIMESFM_3_UPGRADE_SCOPE.md` | TimesFM 2.5→3.0 upgrade scope (license/Phase A/B) |
| `worklog/20260915_TIMESFM3_IMPLEMENTATION_SCOPE.md` | TimesFM-3 implementation scope (`forecast_with_covariates`) |
| `TimesFM Multivariate Guide.md` (user) | Covariate buckets + tensor layout |
