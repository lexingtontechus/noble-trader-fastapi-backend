## [Unreleased] — 2026-10-04

### Major — LLM value audit: Clef-Flash repurposed, p_clef shadow stamp shipped

#### The audit

Four LLM-adjacent surfaces were measured against live data. Three produced output that
**nothing read**; one (Clef-Flash) had no output at all:

| Surface | Live data | Reader | Disposition |
|---|---|---|---|
| Clef-Flash | 0 episodes | none | **repurpose** → text classifier |
| BGE-small embeddings | unverified | `knowledge_index` (shadow) | verify, then decide |
| Macro forecast agent | 33 symbols daily | none | wire into `macro_filter` (shadow) |
| Self-learning triage | 54 findings / 54 reports | none | surface in nobletradingapp + fix detector |

See `docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md`.

#### Clef-Flash is not a direction classifier

The platform has already paid for this lesson twice: two independent fitted models over
the same price series measured **below a constant** — legacy 5-state HMM skill **−0.0404**,
first-passage skill **−0.0691** (base rate 0.7041). An LLM re-reading the same series
inherits the same sub-chance ceiling. **Do not add `p_clef` as a 4th EV source.**

Its one structural edge over the chat LLMs already in the codebase is **structured logits
instead of free text** — no `extract_json()`, no markdown-wrap failures, no
provider-retirement churn. That edge is real only where the input is **text**.

#### `p_clef` shadow stamp (migration 194)

Three nullable research columns on `nt_signal_sim`:

| Column | Meaning |
|---|---|
| `p_clef_dir` | P(model agrees with the signal direction) — discrimination column |
| `p_clef_wait` | P(model says "wait") — abstention column |
| `p_clef_symbol` | model slug used (provenance) |

- New caller `regime_platform/services/decision/clef_flash.py` — three-way
  `choice(buy, sell, wait)`; `p_clef_dir` is direction-relative so buy/sell rows compare.
- New flag **`clef_flash_mode: shadow`** (`off | shadow | live`). `off` = no HTTP call.
- Fail-open: any failure → all-None; 10s timeout; light sweep only.
- **RESEARCH ONLY** — never in `P_WIN_WEIGHTS_FULL`, never passed to `compute_ev()`, never
  touches `qualified` / Kelly / SL / TP. Written and never read back.

`nt_roadmap` id=89 repurposed: the old gate (`correlation(p_clef, p_win_bracket)` after
≥200 episodes) was unmeetable — `p_clef` never existed. New gate: parse-failure rate +
latency vs the chat path, and `AUC(p_clef_dir) > AUC(p_win_dir)` on ≥200 decided rows.

#### Verification

`tests/test_clef_flash.py` — 23 passed. Full suite: **1762 passed, 21 skipped**.

## [Unreleased] — 2026-10-01

### Major — the first-passage pivot: HMM detour retired, replacement tested and failed

#### The question, and the detour

The platform's central question is **P(price reaches TP before SL | this signal,
this bracket, right now)**. `tiered_atr.walk()` already answers it **exactly** on
the actual price path (first-touch, direction-signed, ATR-scaled, 48-bar expiry,
TP-first on a tie) — and the same machinery selects live geometry and proposes
monthly clamps.

For most of its life the platform answered it *indirectly*: `p_markov_hold_n =
T^N[current][target]`, a matrix power over a separate 5-state Gaussian HMM fit on
9 **unscaled** features. On **31,328 decided signals** that HMM scored **below
both trivial baselines**:

| model | accuracy | skill vs baseline |
|---|---:|---:|
| legacy 5-state HMM (its own directional calls) | 0.5251 | **−0.0404** |
| trend_hmm (RegimeHMM-derived) | 0.3230 | −0.2426 |
| naive "always predict the majority direction" | **0.5655** | — |
| shrunk per-symbol base rate | **0.5898** | — |

Positive skill on only 5/14 symbols (mean −0.0622); FLAT (65% of rows)
indistinguishable from the base rate; calls mis-mixed (UP 17.6% / DOWN 17.4%
against a 43.5/56.5 DOWN population). **It was a noisy detour to a question the
platform answers directly.**

#### RM-1 — the shadow comparison retired

`markov_source_mode: trend_hmm_shadow` → **`legacy_hmm`**. The comparison ran
2026-08-25 → 2026-10-01 and answered negatively: legacy won accuracy on **13/14**
symbols (weighted 0.5274 vs 0.3191; USDJPY +0.0007 is a tie), and state
disagreement was **65.9%** over 63,919 compares against a **66.7%** 3-class random
baseline — two independent random classifiers, no convergence in 5 weeks. The
`trend_hmm_shadow` / `trend_hmm_live` code paths are left in place (dormant); a
re-evaluation needs only a YAML edit.

#### `p_markov_hold_n` withdrawn from the EV blend

New flag **`p_markov_hold_n_mode: "off"`**. The sweep passes no
`transition_matrix` / `current_state` / `p_markov_override` to `compute_ev()`, so
the engine's existing missing-source renormalisation drops the source — the blend
becomes **`p_regime` 0.5 / `p_timesfm` 0.5**. `P_WIN_WEIGHTS_FULL` keeps its 5:4:5
relative weights, so flipping back to `"live"` restores the prior blend exactly,
with no migration.

> **The legacy HmmModel is deliberately NOT deleted.** It still fits each tick and
> feeds the TA buffer + `nt_sweep_result`'s legacy `markov_p_up` / `markov_p_dn`
> columns, which the research programme measures against. See the retirement
> impact section below.

#### Phase 1 — the instruments

- **Skill diagnostic.** `compute_tier1_accuracy` now returns `base_rate` (the
  symbol's own majority-direction share over ALL decided rows) and
  **`skill = accuracy − base_rate`** — the number that made the failure legible.
  Persisted via **migration 191** (2 nullable columns on `nt_hmm_shadow_accuracy`).
  Validated against live data: the shipped function reproduces mean legacy skill
  **−0.0622**, positive on 5/14, matching the hand-rolled analysis exactly.
- **`first_passage.py`** (new, pure, no I/O). P(TP before SL) over direction-signed
  ATR-scaled paths, reusing `walk()`'s first-touch rule so a probability computed
  here and a grid picked there can never disagree about what "hit" means. Expired
  paths excluded from the denominator and reported separately. Plus the D5 overlay:
  `carry = floor + (ceiling − floor) × √(freshness × type)`, reusing the
  Kairos-proven primitives verbatim (surprise stays rejected, Spearman 0.004).
  **Offline only** — nothing imported by the sweep path.

#### Phase 2 — the result: 1/5 bar items pass

`fp_eval.py` + `scripts/kairos_fp_eval.py`, on 6,904 decided light episodes (45d,
one closed 48-bar path per signal, episode-deduped):

| bar | requirement | result | detail |
|---|---|---|---|
| B1 | beat the base-rate control | **FAIL** | skill **−0.0691** vs base rate 0.7041 |
| B2 | coverage ≥ 0.30 | **PASS** | 0.9968 |
| B3 | positive skill ≥ 2/3 of symbols | **FAIL** | 3/14 |
| B4 | holds in both era halves | **FAIL** | wrong-signed in the first half |
| B5 | overlay adds skill | **FAIL** | **ΔD +0.0236 vs bare +0.0502 (Δ −0.0265)** |

**The decisive reading:** a **bracket-blind** trailing per-symbol base rate scores
ΔD **+0.059** — *above* first-passage's +0.050. The bracket conditioning bought
nothing; the arm is a momentum read. And the trivial base-rate arm (+0.076 ΔD,
100% coverage, zero fitting) is now the **best-scoring candidate on the table**.

**The D5 overlay subtracts** (Δ −0.0265). A real finding, not a null: the
freshness/type prior is genuinely supported on regime-*label* accuracy (+0.117),
but does not transfer to a first-passage *level*.

#### Three bugs caught in the build

Each would have produced a confident wrong answer:

1. **Look-ahead** — the first cut scored each signal on its own path. With one path
   per signal that is a binary outcome, not a probability (~1.0 by construction).
   Rebuilt as a walk-forward trailing rate (score first, append the label after).
2. **A silent parser failure** — `split_cell` was hand-rolled against a `vol|dir`
   format the raw label never uses (`med_high_vol_strong_bull`), so every `ewin`
   was `None` and the overlay was a **no-op that still reported as measured**
   (Δ exactly +0.0000). Now delegates to `kairos.regime_cell`, with a regression test.
3. **A metric blind to the change** — B5 was first scored on sign-accuracy, but
   shrinking toward 0.5 *preserves the sign*, so accuracy could never see the
   overlay. Moved to ΔD, which sees ranking. That turned a fake `+0.0000` into the
   real `−0.0265`.

Plus the honest floor: `trailing_base_rate`, bracket-blind, so a geometry-aware arm
cannot claim credit for momentum.

#### What this does and does not change

**Unchanged:** `trend_probs_gate_mode: live` (Tier 1/2 magnitude classifier, Brier
improved on 14/14 symbols) — a different mechanism from the markov source.
`p_regime`, Kelly, geometry, the 7 gates, calibration enforcement, and all
`distance_atr_bucket` calibration bands.

**Killed:** option E as specified, and the D5 overlay in its current form.

**Survives:** the §2.4 thesis (the HMM still scores below the naive baseline; the
D1/D2 retirement is unaffected — nothing here argues for turning the HMM back on).

**Decision requested** (kill criteria met, 3 weeks early): take option F (retire
the HMM permanently, ship the calibrated base rate), one time-boxed retry
(drop the overlay; condition the level on the regime **cell**), or bank the finding.

#### Retirement impact (CORRECTED — the first assessment was wrong)

> **Correction (2026-10-01, follow-up).** An earlier version of this entry
> claimed the minimum correct retirement "deletes `model.py` only" and that
> `ta_fetcher`/`ta_extract` were unaffected. **That is wrong.** `model.py` is
> **load-bearing for the TA pipeline**, which is itself load-bearing for
> calibration and reachability. Full dependency map:

| consumer | imports from | can it lose the HMM? |
|---|---|---|
| `p_regime` (EV blend) | `RegimeHMM` (`core/regime_engine.py`) | **yes — no coupling at all** |
| regime labelling | `RegimeHMM` | **yes — no coupling** |
| `trend_probs_gate_mode: live` | `RegimeHMM` | **yes — no coupling** |
| `ta_fetcher.py` | **`model._compute_ta_indicators_local`** | **NO — must keep** |
| `trend_filter.py` | **`model._compute_rsi`** | **NO — must keep** |
| `nt_regime_log.ta_*` | `FeatureBuffer` + `ta_fetcher` + `ta_extract` | **NO — must keep** |
| `nt_signal_sim.spread_bps` (Kairos) | `quote_fetcher.spread_bps_by_symbol()` | **NO — must keep** |
| `FeatureBuffer` indices 1–3 | `quote_fetcher` | **NO — must keep** |
| `eod_correction` `degeneracy_breaker` | `markov_p_up`/`markov_p_dn` as **diagnostic context in the action payload** | degrades (loses forensic detail; the trigger is `p_win`, so the action still fires) |
| `nt_sweep_result.markov_*` (4 cols) | `HmmModel.emit_markov` | only these are HMM-exclusive |
| `nt_signal_sim.legacy_markov_state` | `HmmModel.emit_markov` | only these are HMM-exclusive |

**What `model.py` actually contains** — only about half of it is the model:

| symbol | kind | disposition |
|---|---|---|
| `HmmModel`, `_FittedModel`, `_aggregate_transition` | the 5-state HMM | **retire** |
| `N_STATES`, `STATE_LABELS`, `STATES_3`, `MIN_FIT_BARS`, `MODEL_PATH`, `STATE_MAP_THRESHOLD_STDS` | HMM constants | **retire** |
| `_compute_rsi`, `_compute_macd`, `_compute_ao`, `_compute_bb_power`, `_compute_adx_close_only` | **pure TA math** | **keep** |
| `_compute_ta_indicators_local` | **TA bundle** (calls the 5 above) | **keep** |
| `_compute_log_returns` | **pure math** | **keep** |

**Minimum correct retirement is therefore a SPLIT, not a delete:**

1. Extract the TA math into a new public module (e.g.
   `regime_platform/services/indicators.py`) — it is no longer HMM-specific, and
   `trend_filter.py` importing a *private* `_compute_rsi` from a model module is
   itself the smell that hid this dependency.
2. Delete only the `HmmModel` class, `_FittedModel`, `_aggregate_transition` and
   the HMM constants.
3. Repoint `ta_fetcher.py` and `trend_filter.py` at the new module.
4. Leave `feature_buffer.py`, `quote_fetcher.py`, `ta_extract.py` untouched.
5. `orchestrator.py` / `heavy_sweep.py`: stop the per-tick `fit()`; keep the
   `quote_fetcher` / `ta_fetcher` calls and `latest_ta()` writes.

Skipping step 1 silently breaks TA → `nt_regime_log.ta_*` → the calibration and
reachability inputs.

#### ⚠ Open risk introduced by the withdrawal (not yet measured)

Removing `p_markov_hold_n` from the logit pool is **not leverage-neutral**. The
HMM's `p_markov` values cluster low (the model was near-degenerate), so dropping
the source **raises `p_win`**, which raises `ev_per_dollar`, which raises
`ev_scale` (`min(1, ev_per_dollar/0.5)`), which multiplies Kelly. Worked example
on the live weights (p_regime 5/14 · p_markov 4/14 · p_timesfm 5/14), RR 5:3:

| p_regime | p_markov | p_timesfm | p_win before | p_win after | **ev_scale before → after** |
|---:|---:|---:|---:|---:|---|
| 0.60 | 0.10 | 0.60 | 0.4163 | 0.6000 | **0.220 → 1.000 (+0.78)** |
| 0.50 | 0.10 | 0.50 | 0.3480 | 0.5000 | **0.000 → 0.667** |
| 0.40 | 0.05 | 0.40 | 0.2440 | 0.4000 | **0.000 → 0.133** |
| 0.55 | 0.50 | 0.55 | 0.5358 | 0.5500 | 0.858 → 0.933 (+0.08) |

**The withdrawal can take a signal from zero size to full size.** That is a live
sizing change with real risk implications, and it was made without measuring it.
It must be validated before the retirement is treated as settled.

#### Version anchors

`pyproject.toml`, `main.py`, `docs/openapi.yaml`, `docs/Setup/openapi.yaml`,
`tests/test_version_consistency.py` — **no bump** (8.2.0 stands; flag + additive
column + offline research only).

#### Worklogs

`worklog/kairos/reports/20261001_rm1_markov_source_eval.md` ·
`docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md` ·
`worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md` ·
`worklog/20261001_PHASE2_FIRST_PASSAGE_RESULT.md` ·
`worklog/kairos/reports/20261001_fp_eval.md`

---

## [8.2.0] - 2026-09-30

### Major — per-symbol vol-regime geometry + unattended ATR re-opt

#### Dynamic ATR (live, fractional price brackets)
- **Per-symbol / forex-pooled** `by_symbol` grids (0.05→0.01 solve). Exact `entry ± tp_atr×ATR` — no brick snap.
- **`vol_ratio` gate** (`rv24/rv168` at entry — **volatility, not volume**): BTCUSD **8.07/1.77 only if vol_ratio≤0.71**; XAUUSD **4.94/1.00** ungated (45d walk).
- Fail-closed: no qualifying pick inside clamps → heavy bricks kept.
- Fix: `UnboundLocalError trade_signal` in the pre-signature dynamic log path (aborted apply).

#### ATR re-opt (rediscovery clock)
- **`atr-reopt-monthly`** APScheduler 1st 03:30 UTC — free lattice 45d, wr band + both halves, drift vs `by_symbol`. Report + `nt_roadmap` heartbeat.
- **`atr-reopt-urgency`** every 4h: `vol_ratio≥1.25` → **auto-run** free lattice + optional `auto_apply_clamps` (skips vol-gated symbols). Discord `DISCORD_WEBHOOK_STATUS` is FYI of the action, never a human to-do.
- Worklogs: `20260930_VOL_REGIME_ATR_BTC_XAU.md`, `20260930_MONTHLY_ATR_REOPT.md`.

#### LLM server
- Offline HF-id → local snapshot resolve; load without `device_map` when `accelerate` missing.

#### Version anchors
`pyproject.toml`, `main.py`, `docs/openapi.yaml`, `docs/Setup/openapi.yaml`, `tests/test_version_consistency.py` → **8.2.0**.

---

## [8.1.0] - 2026-09-28

### Major — Kairos RANK surface + TimesFM quantile & covariate capture

#### Kairos
- **RANK board** — `kairos_rank_score` (freshness + type + recent TP + cell_p) stamped on `nt_signal_sim` (migration 172) and exposed on `v_talaria_latest_signals` (183). Walk-forward: Spearman 0.126 / AUC 0.67 (all eras), 0.785 post-debounce; shadow P60 ΔD +0.23…+0.30. **Measurement only** — not a live gate.
- **Event-time STATE / transition** — FRESH/WARM/STALE/DEEP + `dir_flip` typing on realtime + Discord.
- Worklogs: `worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md`, `20260928_TIMESFM_QUANTILE_CAPTURE.md`, rank-board scope.

#### TimesFM quantiles & covariates (P1.3)
- Capture discarded `quantile_forecast` → `timesfm_q_lo/mid/hi`, `timesfm_q_spread`, `timesfm_q_pos` on `nt_signal_sim` (**migration 186**).
- `extract_q10_q50_q90` + `timesfm_rank_features` quantile path (`kairos_rank.py`); stamps even when `p_timesfm` is missing.
- XReg dynamic covariates validated on TimesFM 2.5 (P1.1); `timesfm_gap` is the only TimesFM-family feature with stable within-cell IC (+0.037).

#### Ops / data hygiene
- Renko orphan high-`brick_index` purge after brick upsert (same-grid).
- Dashboard cache `renkoBricks` limited to each symbol's **live `brick_size` grid** (migration 184) + support indexes (185).

#### Version anchors
`pyproject.toml`, `main.py`, `docs/openapi.yaml`, `docs/Setup/openapi.yaml`, `tests/test_version_consistency.py` → **8.1.0**.

---

## [8.0.0] - 2026-09-26 (no version bump)

### Changed — dynamic ATR selector (`dynamic_atr_mode`, still shadow)
- `tiered_atr.py` now picks TP/SL in ATR per asset class by re-walking the class's last 10 days of signals (48-bar resolver rule) and taking the best expectancy that is positive in both halves, expiry <= 25%, TP/SL >= min_rr. No qualifying pick -> heavy pick kept. Settings in the new `dynamic_atr:` block of `config/feature_flags.yaml`. Log tag `[dynamic:atr]`.
- Removed the old band-limited risk pick (TP <= 2 ATR, `risk_capped_pick`, `TIER_TP_ATR_BAND`); it could not reach the far-TP / close-SL region that pays.
- Rolling walk-forward 08-26 -> 09-24 (4,647 signals, cost 0.1 ATR): recorded -1.55 ATR/signal, new selector +0.82, old selector -1.50.

### Removed
- `payoff_geometry` (reverted in `5d96872`) and `scripts/kairos_geometry_replay.py`.

---

## [8.0.0] - 2026-09-25 (assessment addendum — no version bump)

### Dynamic / tiered ATR — live-flip assessment (hybrid promote first)

No production flags flipped in this release. Decision + roadmap + clarifications only.

#### Assessment

- **Wide grid** = live `config/brick_params.yaml` (price-% heavy pick) → `tp_distance_atr` ~10–50, bucket `4+`.
- **Tight ATR** = ATR-anchored candidates (~0.15/0.3/0.6 ATR) in shadow — **13/14** symbols, widened Sharpe wins.
- **Dynamic ATR** = risk-capped tiered picker (`dynamic_atr_mode`, cap 0.35) that would write those tight bricks.

**Decision:** flip **`atr_anchored_evidence_mode: live`** (hybrid promote only). Hold `dynamic_atr_mode` and full `heavy_sweep_atr_anchored_bricks_mode` until one heavy cycle + ~7d show promoted `tp_atr`↓ / long-lag↓ / closed PnL↑ (**P0.4**).

**Activated 2026-09-25:** `atr_anchored_evidence_mode: live` in `config/feature_flags.yaml` (hybrid promote). 18/18 `test_p0_p1_geometry_promote` + `test_tiered_atr` pass. Full ATR grid and `dynamic_atr_mode` remain shadow.

#### Clarifications

- **P0.4** is the *full* ATR-grid live flip bar (`20260919_HEAVY_GEOMETRY_PROMOTE…`): Stage 1 green **or** hybrid already live with one cycle of promoted-symbol improvement. Shadow Sharpe alone does not satisfy it.
- **`min_rr` stays inert** on light qualification (2026-09-20). Tight ATR changes `brick_size` / ATR distance, not necessarily `tp_bricks/sl_bricks`. RR remains a heavy-grid **geometry pre-filter** only; no MIN_RR code change. See `worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md` §1.2.

#### Docs / roadmap

- Worklog: `worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md`
- `nt_roadmap`: `atr_anchored_evidence_mode_flip`, `dynamic_atr_mode_flip`, `heavy_sweep_atr_anchored_full_flip`, `min_rr_geometry_guard`

---

## [8.0.0] - 2026-09-24

### Major — Twin-bar program: calibration + reachability methodology (code version realigned)

#### Shipped in this release (fail-closed unscored bracket)

- `p_win_bracket_unscored_policy: fail_closed` (default) — unscored `p_win_bracket` cannot be QUALIFIED.
- Bracket evidence gate moved **before** cooldown stamp / Discord so `nt_sweep_result.qualified` and `nt_signal_sim` agree with what was published.
- G2 stamps: `suppress_reason='bracket_unscored'` (`gate_outcome='suppressed'` when other gates passed); `failed_gates` may include `p_win_bracket_ok` on `gate_fail` rows.
- `bracket_gate_decision()` distinguishes `reject_low` vs `reject_unscored` (threshold reject still requires `p_win_bracket_mode: live`).
- Migration **169** — comment + `v_bracket_unscored_rejects` review view.
- Research capture keeps every non-neutral row (fail-open measurement).

**Scope of this release:** engineering methodology and locked operator decisions. Implementation is phased (E1–E4). No live signal-path behavior flips in this commit; version marks the architecture baseline and the end of measurement-only roadmap items.

> **Note on numbering:** `docs/Setup/CHANGELOG.md` had drifted to a 9.x series while code anchors (`pyproject.toml`, `main.py`, both `openapi.yaml`) tracked `7.9.0`. **8.0.0** is the code/AGENTS.md series. This entry realigns the changelog to the code version. Historical 9.x/5.x blocks below are retained as-is.

#### Why

Two product bars did **not** move 2026-08-25 → 2026-09-24 despite a large shadow-mode program:

| Bar | Miss |
|---|---|
| **Calibration** (regime label · direction · strength) | Direction still `substring(regime_label)`; `p_win` bias +0.27 → **+0.34**; gold/FX +0.36…+0.46 |
| **Reachability** (entry · SL · TP · P/L · W/L) | Close-only TP-first; PnL from brick counts not exit−entry; three `expired` meanings; costs = 0 |

What *did* land and is kept: expiry 60.6%→21.1%, deadline-aware selection live, shadow-leak P0 fixes, gated `p_regime` (Brier 14/14), debounced labels, settlement-contract module, vol-tier keys, `p_win_bracket` shadow.

Root cause: recommended work measured and gated flags around an **unchanged core** (category error in EV, substring direction, non-fill PnL). Shadow logs cannot disprove a model.

#### Added — Definitive methodology (scope)

- `worklog/20260924_CALIBRATION_REACHABILITY_DEFINITIVE_METHODOLOGY.md`
  - **Lever 1** — Price EV/gates/Kelly on `p_win_bracket` (or formally kill the estimator). Experiment **E1** with pass/kill bars.
  - **Lever 2** — Replace substring direction with scored multi-source `p_dir` (may abstain / disagree). Label scorecard + direction tournament. Experiment **E2**.
  - **Lever 3** — Settlement physics: OHLC first-touch, `sl_first`, PnL=`exit−entry−costs`, single expired taxonomy, heavy↔live parity. Experiment **E3**.
  - **Lever 4** — Experiment protocol: every `*_mode` maps to an E-id or is deleted; max window 28d / n_ep=50; default is **kill**.

#### Changed — Locked operator decisions (2026-09-24)

| Decision | Lock |
|---|---|
| Unscored `p_win_bracket` | **Dual-stream:** live book fail-closed (no size); research fail-open + `bracket_unscored` |
| Same-bar policy | **`sl_first`** going forward (`tp_first` = lineage only) |
| Direction authority | Shadow may **flip** side; live **veto-to-neutral** until E2 passes |
| Experiment windows | 28d or n_ep=50 → default **kill** |
| Orphan shadows | Bind to E-id or **delete** in hygiene PR (`dynamic_min_rr`, `timesfm_k_adaptive`, meta-model, context-alignment) |

#### Changed — Version anchors (AGENTS.md convention)

All moved **7.9.0 → 8.0.0** together:

- `pyproject.toml`
- `main.py` (FastAPI `version=` + health `version`)
- `docs/openapi.yaml` + `docs/Setup/openapi.yaml` (info.version + example)
- `tests/test_version_consistency.py` (asserts `8.0.0`)
- `AGENTS.md` Version section

#### Not changed (this release)

- Live qualification gates, EV weights, brick_params, Discord delivery
- `p_win_bracket_mode` / `p_win_in_ev_mode` remain **shadow** until E1
- Kairos v3 stays parallel research (geometry-free; does not fix PnL/W-L)

#### Next (implementation train)

1. Hygiene PR: orphan flags + version already bumped  
2. L1.a/b reliability harness + L2.a label scorecard + L3.a/e OHLC fixtures + expired taxonomy  
3. E1 replay (3-outcome + costs) with fill-PnL dual-report → flip or kill bracket pricing  
4. E2 direction tournament → E3 settlement defaults → E4 geometry only after E1  

---

## [9.1.0] - 2026-07-24

### Major changes — Multivariate Gaussian HMM replaces discrete MarkovChain + TA indicator persistence

#### Added — Multivariate Gaussian HMM for `p_markov` source

The discrete 3-state price-only `MarkovChain` (`regime_platform/services/statistics/markov_chain.py`) is replaced as the live `p_markov` source by a **5-state Gaussian HMM** trained on a 9-feature observation vector per symbol. The HMM is a drop-in replacement at the contract level — it emits the same 3×3 transition matrix (`{UP, DOWN, FLAT}`, rows sum to 1.0) and the same `current_state` string that the EV engine v4 expects — but the underlying model is substantially better-informed.

- **5 new files** in `regime_platform/services/hmm/`:
  - `__init__.py` — public API (`FeatureBuffer`, `HmmModel`, `latest_ta`, constants)
  - `feature_buffer.py` — thread-safe per-symbol rolling 300-bar window, 9 features per vector
  - `model.py` — `HmmModel` class with `bootstrap_from_ohlcv()`, `fit()` (Baum-Welch via `hmmlearn`), `emit_markov()` (5-state → 3-state aggregation by per-state mean log_return), `save()`/`load()` (pickle), `status()`
  - `quote_fetcher.py` — async concurrent fetch of TVDA `/api/quote/{symbol}` for 20 symbols via `asyncio.gather`, extracts 3 microstructure features (effective spread bps, imbalance, volume persistence)
  - `ta_fetcher.py` — async concurrent fetch of TVDA `/api/ta/{symbol}/indicators` for 20 symbols via `asyncio.gather`, extracts 5 TA indicators (ADX, AO, BB Power, MACD, RSI)
  - `ta_extract.py` — `latest_ta(buffer, symbol)` helper that returns the 5 TA values from the buffer's latest bar as a Supabase-shaped dict (nullable)

- **Feature vector (9 features, indices matter)**:
  - 0: `log_return` (from `/api/price/batch`)
  - 1: `effective_spread_bps` (from `/api/quote`)
  - 2: `imbalance_I` (from `/api/quote`)
  - 3: `volume_persistence` (from `/api/quote`)
  - 4: `adx` (from `/api/ta`)
  - 5: `ao` (from `/api/ta`)
  - 6: `bb_power` (from `/api/ta`)
  - 7: `macd` (from `/api/ta`)
  - 8: `rsi` (from `/api/ta`)

- **Lifecycle**: The HMM bootstraps from `/api/price/batch` (200 bars) on first sweep for each symbol — computes log_return + 5 TA indicators locally from close prices (microstructure features are 0.0 until the first live `/api/quote` poll lands, typically within the same sweep tick). Every subsequent sweep tick does a concurrent `asyncio.gather` over 20 symbols for both `/api/quote` and `/api/ta`, pushes the new feature vector to the buffer, refits the HMM (Baum-Welch on 300 bars × 9 features — cheap), and emits the 3×3 transition matrix + current state.

- **5→3 state aggregation**: The 5 latent Gaussian states are mapped to `{UP, DOWN, FLAT}` by clustering on per-state mean log_return. States with mean log_return more than 0.25 std from zero are `UP` (positive) or `DOWN` (negative); the rest are `FLAT`. The 5×5 transition matrix is then aggregated to 3×3 by summing the relevant rows/columns.

- **No new env vars. No gates. No shadow mode. No cold-start handling.** The HMM fits from t=0 on whatever data is in the buffer; if the buffer is empty, `emit_markov()` returns `(None, None)` and the orchestrator falls back to `p_up = p_dn = 0.5` (neutral defaults — same behavior as the old chain when no data was available).

#### Changed — Orchestrator + heavy sweep rewired to use HMM

- **`orchestrator.py`**: `SweepOrchestrator.__init__` now constructs `self._hmm_buffer = FeatureBuffer()` and `self._hmm_model = HmmModel(self._hmm_buffer)`. In `_sweep_group` (after the price fetch, before per-symbol processing), the orchestrator bootstraps any unbootstrapped symbols from the 200-bar price history, then concurrently fetches `/api/quote` + `/api/ta` for all 20 symbols via `asyncio.gather`, then refits the HMM for each symbol. In `_process_symbol`, the call to the old `_build_markov_chain()` is replaced with `self._hmm_model.emit_markov(symbol)`.
- **`heavy_sweep.py`**: Same rewiring — the heavy sweep now bootstraps any unbootstrapped symbols from the 2160-bar (90d) price history and calls `emit_markov()` instead of `_build_markov_chain()`.
- **`statistics/__init__.py`**: `MarkovChain` removed from the public `__all__` exports. The `markov_chain.py` module is preserved as an internal module because the renko router's ad-hoc `/ev/compute` endpoint (which takes arbitrary user-supplied prices and can't use the persistent HMM) still imports it directly.

#### Added — TA indicator persistence (migration 036)

- **Migration 036** (`036_add_ta_indicators_to_sweep_result_regime_log.sql`) adds 5 nullable `DOUBLE PRECISION` columns to BOTH `nt_sweep_result` and `nt_regime_log`:
  - `ta_adx`, `ta_ao`, `ta_bb_power`, `ta_macd`, `ta_rsi`
- The 5 TA values are pulled from the HMM `FeatureBuffer`'s latest bar via the new `latest_ta(buffer, symbol)` helper (`regime_platform/services/hmm/ta_extract.py`) and injected into:
  - `log_sweep_result({...})` calls in both `orchestrator.py` (light sweep) and `heavy_sweep.py` (heavy sweep)
  - `log_regime_entry(...)` calls in `orchestrator.py` (light sweep only — heavy sweep doesn't write to `nt_regime_log`)
- The 5 columns are added to the `NT_SWEEP_RESULT_COLUMNS` and `NT_REGIME_LOG_COLUMNS` whitelists in `supabase_client.py` so `_filter_payload()` doesn't drop them silently.
- `log_regime_entry()` signature extended with 5 `ta_*` kwargs (default `None`).
- 2 partial indexes on `nt_sweep_result`: `(symbol, ta_rsi) WHERE ta_rsi IS NOT NULL` and `(symbol, ta_adx) WHERE ta_adx IS NOT NULL` — supports the most common dashboard queries ("show me sweeps for symbol X where RSI was oversold"). No indexes on `nt_regime_log` (smaller table).
- **Both tables can be queried standalone for TA-aware analytics without a JOIN** — the data is duplicated intentionally so `nt_regime_log` (the skinny regime-transition table) remains a fast analytic surface.
- All 5 columns are nullable — best-effort. `None` is written when the HMM feature buffer is empty (e.g. cold start before the first `/api/ta` poll landed) or when the TA fetch failed for that tick (HTTP 429 / network error).

#### Added — Tests

- **`tests/test_hmm_feature_buffer.py`** — 13 tests (push, matrix, latest, n_bars, symbols, clear, window trimming, shape validation, multi-symbol independence).
- **`tests/test_hmm_model.py`** — 13 tests (bootstrap, fit, emit, 5→3 state aggregation, status).
- **`tests/test_hmm_ta_extract.py`** — 10 tests (latest_ta extraction, empty-buffer fallback, latest-not-oldest, column-order contract, whitelist membership, signature check).
- **36/36 HMM tests pass.** `hmmlearn>=0.3.2` was already in `requirements.txt`.

#### Migration order

1. Apply migration 036 to Supabase first.
2. Deploy the updated backend.
3. Verify with: `SELECT symbol, ta_adx, ta_ao, ta_bb_power, ta_macd, ta_rsi FROM nt_sweep_result WHERE created_at > now() - interval '1 hour' ORDER BY created_at DESC LIMIT 20;`

#### What's NOT changed

- The 4-source blend weights (p_pattern 0.30 + p_regime 0.25 + p_markov 0.20 + p_timesfm 0.25) are unchanged.
- The 8 qualification gates are unchanged.
- BayesianAlpha is untouched (it operates on realized trade outcomes, not on the HMM).
- The agent and proxy are untouched.
- `markov_chain.py` is preserved internally for the renko router's ad-hoc `/ev/compute` endpoint.
- The heartbeat schema is unchanged (`p_markov_hold_n` + `p_markov_single_step` + `markov_transition_matrix` + `markov_current_state` are still produced, just by the HMM instead of the discrete chain).

---

## [9.0.0] - 2026-07-15

### Major changes — TradingView integration, push model, signal qualification overhaul

#### Added — TradingView API data source (RapidAPI)

- **Migration 028**: Added `tradingview_symbol` column to `nt_symbol`. Updated `data_source` CHECK constraint to allow `'tradingview'`. Switched BTCUSD from Alpaca to TradingView (COINBASE:BTCUSD). Added 3 new symbols: USDJPY, EURUSD, XAUUSD (all OANDA via TradingView).
- **Settings**: Added `TRADINGVIEW_API_KEY`, `TRADINGVIEW_API_HOST`, `TRADINGVIEW_BASE_URL` env vars.
- **Price fetcher**: Added `fetch_tradingview_candles()` function with 3 response format parsers. Added dispatch branches in `fetch_sweep_candles()` and `fetch_latest_candles()`.
- **Supabase client**: Added `tradingview_symbol` to SELECT clause.
- **Orchestrator**: `_derive_venue()` now extracts the exchange name from `EXCHANGE:SYMBOL` format (e.g., `COINBASE:BTCUSD` → `Coinbase`).
- **Symbol cron schedule**: `regime_cron='*/5 * * * *'` (every 5 min), `sweep_cron='0 20 * * 0'` (Sunday 20:00 UTC). The 4-hour heavy sweep is deprecated — TimesFM replaces it.

#### Added — Redis Streams (push model)

- The orchestrator now publishes qualified signals via **both** Redis Pub/Sub (`PUBLISH`) AND Redis Streams (`XADD`) to `signal.raw.noble_trader`.
- Pub/Sub is used by the in-process SignalAggregator for Discord/Telegram routing.
- Streams (persistent, supports consumer groups + XACK + replay) are used by downstream external consumers (trading system, DOOM agent).
- Stream is auto-trimmed to ~10,000 entries via `MAXLEN ~`.

#### Deprecated — Redis poll model (trading:config + trading:heartbeat)

- **Commented out** the following Redis writes in the orchestrator (preserved for reference):
  - `HSET trading:config:{symbol}` — was the poll-based config hash
  - `SET trading:heartbeat:{symbol}` — was the poll-based heartbeat
  - `PUBLISH agent:{symbol}` — was the per-symbol liveness pub/sub
- Downstream consumers now read from the Redis Stream (`signal.raw.noble_trader`) instead of polling per-symbol config hashes.

#### Added — Signal cooldown (anti-spam)

- New env var `SIGNAL_COOLDOWN_MINUTES` (default: 60). After a qualified signal is published for a (symbol, direction) pair, subsequent signals for the same pair are suppressed until the cooldown expires.
- Prevents the same BUY/SELL from being published every 5 minutes when the regime is stable.
- Expected signal volume: ~3-8 per day per symbol (down from ~288 without cooldown).
- Set to 0 to disable.

#### Added — p_win threshold (dynamic quality filter)

- New env var `MIN_P_WIN` (default: 0.55). Added as Gate 7 in the qualification pipeline.
- Unlike `MIN_RR` (which is a static property of brick params), `p_win` changes sweep-to-sweep based on how regime + ext_signal + TimesFM agree via log-odds pooling.
- Filters out signals where the blend is only marginally directional.

#### Added — TIMESFM_GATE_MODE (hard/soft/off)

- New env var `TIMESFM_GATE_MODE` (default: hard). Controls how TimesFM confidence affects qualification:
  - `hard`: reject if `|p_timesfm - 0.5| < MIN_TIMESFM_CONFIDENCE` (binary veto)
  - `soft`: reduce TimesFM's blend weight proportionally to confidence, don't reject (preserves blended EV philosophy)
  - `off`: no TimesFM gate, rely entirely on blended EV
- In soft mode, the orchestrator temporarily overrides EV engine weights, recomputes EV with reduced TimesFM weight, then restores.

#### Changed — Qualification gates (7 gates, all configurable via env vars)

All thresholds are now configurable via env vars (no code changes needed to tune):

| Gate | Check | Env var | Default |
|---|---|---|---|
| 1 | Signal is buy/sell (not neutral) | — | — |
| 2 | HMM regime confidence >= threshold | `MIN_REGIME_CONF` | 0.60 |
| 3 | Kelly fraction >= threshold | `MIN_KELLY` | 0.02 |
| 4 | TimesFM available (p_timesfm not None) | — | — |
| 5 | TimesFM confidence >= threshold (mode-dependent) | `MIN_TIMESFM_CONFIDENCE` / `TIMESFM_GATE_MODE` | 0.05 / hard |
| 6 | EV allowed (ev > 0 AND rr >= threshold) | `MIN_EV` / `MIN_RR` | 0.0 / 2.0 |
| 7 | Blended p_win >= threshold | `MIN_P_WIN` | 0.55 |

Plus: `SIGNAL_COOLDOWN_MINUTES` (post-gate cooldown, default 60).

#### Changed — nt_sweep_result enriched (migration 029)

Added 11 new columns to capture the full signal picture:
- `effective_kelly`, `signal`, `ev`, `p_win`, `qualified`, `p_timesfm`
- `entry_price`, `stop_loss`, `take_profit`, `aggression`, `venue`
- `source` (was being sent but may not have been in schema)
- Plus 3 indexes: `idx_nt_sweep_result_qualified`, `idx_nt_sweep_result_signal`, `idx_nt_sweep_result_venue`

#### Changed — Discord embed format

- Title now includes the exchange: `Signal: BTCUSD SELL (Coinbase)`
- "Venue" field replaced with separate "Exchange" and "Asset Class" fields
- `_derive_venue()` extracts exchange from `EXCHANGE:SYMBOL` format (e.g., `COINBASE:BTCUSD` → `Coinbase`)
- Per-asset-class webhook routing: `DISCORD_WEBHOOK_CRYPTO/EQUITIES/METALS/FOREX`
- Debug webhook support: `DISCORD_WEBHOOK_DEBUG` + `DEBUG_SIGNALS=1`

#### Added — Diagnostic tools

- `scripts/debug_signal_subscriber.py` — subscribes to debug channel, forwards to Discord, shows running stats
- `scripts/test_signal_stream.py` — Redis Streams diagnostic (info, read, tail, publish-test, create-group, pending)
- `scripts/timesfm_gate_simulator.py` — simulates hard/soft/off gate modes against live data
- `scripts/signal_before_after.py` — shows raw signal → gate evaluation → treated payload
- `scripts/qualification_simulator.py` — simulates cooldown/p_win/RR threshold effects on signal volume
- `scripts/test_calibration_live.py` — live calibration record writer with timestamp validation
- `scripts/timesfm_gate_simulator.py` — threshold tuning tool

#### Fixed — Historical bugs from this session

- **InputValidationMiddleware**: `__init__` signature had `exempt_paths` first instead of `app` — FastAPI passed app positionally to `exempt_paths`, causing `TypeError: 'CORSMiddleware' object is not iterable` on every request
- **TimesFM fetcher**: cache-miss path was unreachable (nested inside `if cached:`); payload was `{symbol, prices}` but server.py expected `{horizon, inputs}`; missing `X-API-Key` header; treated raw forecasted price as probability
- **Redis DataError**: `config_dict['p_timesfm']` was set to `None` when TimesFM unavailable — `hset(mapping=...)` raised `DataError: Invalid input of type 'NoneType'`
- **SignalAggregator RateLimiter**: used `field(default_factory=list)` outside `@dataclass` — stored `Field` object instead of list, silently dropped every Discord message
- **SignalAggregator HEARTBEAT routing**: fix documented in `DISCORD_DELIVERY_FIX.md` was never applied — all heartbeats went to `send_status()` instead of `send_signal()`
- **TradeJournal calibration writer**: `asyncio.create_task()` in sync method raised `RuntimeError` when no event loop was running
- **Duplicate nt_regime_log writes**: `log_sweep_result()` wrote to both `nt_sweep_result` AND `nt_regime_log`, while `log_regime_entry()` also wrote to `nt_regime_log` — two rows per sweep
- **`decimals` UnboundLocalError**: debug channel code referenced `decimals` before it was defined

## [8.1.0] - 2026-07-05

### Added — Aggregate Heartbeat Channel

- **`signal.raw.noble_trader`** — a single aggregate Redis pub/sub channel that receives all heartbeat payloads alongside the per-symbol `agent:{symbol}` channels. This enables the Hermes L0 subscriber to listen to one channel instead of N per-symbol channels, eliminating the need for restarts when symbols are added. Both the Python SweepOrchestrator and TypeScript edge functions now publish to this channel.

## [8.0.0] - 2026-06-27

### Added — True Signal Gateway (Redis PUBLISH)

- **`PUBLISH agent:{symbol}`** — after the SweepOrchestrator writes the full config hash to Redis, the payload is published to the `agent:{symbol}` pub/sub channel. External agents subscribe and receive pushes only when a True Signal occurs. The orchestrator already has all computed data (regime, EV, signal) in local variables — no extra Redis reads needed.
- **`agent:{symbol}` Pub/Sub channel** — external DOOM agents subscribe and receive heartbeat + signal payloads. **Note:** `agent:{symbol}` is a pub/sub channel, not a Redis key — it will not appear under `KEYS`/`SCAN`. Validate with `PUBSUB NUMSUB agent:{symbol}`.

### Changed — Decoupled Liveness from State

- **Removed per-tick `trading:heartbeat:{symbol}` writes** — the Python backend no longer writes heartbeat on every tick (lines 757-770 of `router.py` commented out). The heartbeat is now written by the SweepOrchestrator on LightningAI every sweep cycle (liveness-only).
- **Removed per-snapshot `trading:config:{symbol}` cache writes** — `_save_snapshot()` no longer calls `trading_heartbeat.cache_config()` (method removed). The config hash is written exclusively by the SweepOrchestrator as a pipelined HSET + EXPIRE; the Python backend only reads it via `get_config()`.
- **Removed periodic 100th-tick L1 snapshot checkpoint** — `_save_snapshot()` in `router.py` now only fires on position events (`result.get("filtered")` or `result.get("trade_result")`), not on `brick_count % 100 == 0`. Reduces Redis SET volume by ~90%.

### Optimized — Config Writes

- **`redisUpdateSignal()` now conditional** — on regime shift or initial write cycles, `redisUpdateSignal()` is skipped because `redisSetConfig()` already writes all signal/EV/TimesFM fields. Only runs on non-shift cycles. Eliminates the duplicate HSET to `trading:config:{symbol}` per shift cycle (~50% reduction in config write commands).

### The 4 Gates (Light-Regime Path Only)

1. **Regime Shift Hard Block** — no regime shift in last 300s (5-min cooldown)
2. **Shift Velocity Cap** — < 3 shifts in 24h (not turbulent)
3. **Regime Confidence** — `regime_conf > 0.40`
4. **Normalized EV** — `ev_per_dollar > 0.15`

### Changed — EV Engine v4 Refactor

- **Renamed `p_imbalance` → `p_ext_signal`** — honest naming: the slot accepts any external directional vote (L2 order book, sentiment, alternative data), not just L2 imbalance specifically
- **Markov chain relocated** — moved from P_win blend (where it double-counted directionality with HMM) to a position-sizing multiplier applied to `ev_scale`. Markov persistence now scales position size [0.4×–1.0×] instead of being blended into probability
- **Log-odds pooling** — replaced linear `Σ w_i * p_i` blending with `inv_logit(Σ w_i * logit(p_i))`. Respects probability bounds and prevents overconfidence when signals agree
- **New weights** — 40% Regime + 25% External + 35% TimesFM (with TimesFM); 55% + 45% (fallback)
- **Added `CalibrationTracker`** — decile-bin tracker with Brier score, reliability diagrams, and bias detection

### Added — L2 Order Book Data Pipeline

- **`AlpacaL2Adapter`** — WebSocket subscription to Alpaca IEX/SIP order book depth (5–10 levels, ~100ms updates for US equities/crypto)
- **`HyperliquidL2Adapter`** — WebSocket subscription to Hyperliquid l2Book channel (full depth for crypto perps, no API key required)
- **`L2ImbalanceComputer`** — multi-level weighted imbalance ratio with sigmoid confidence mapping and persistence tracking over a sliding window
- **`L2FeedManager`** — symbol→venue router (reads `nt_symbol.data_source` from Supabase, falls back to hardcoded map), manages adapter lifecycle, exposes `get_signal(symbol)` for EV engine consumption
- **App lifecycle** — L2 pipeline starts automatically at boot if `l2.enabled=true` in runtime config, shuts down gracefully

### Added — Hyperliquid Support

- **`HL_API_URL` and `HL_WS_URL`** settings in `settings.py` (default to Hyperliquid mainnet)
- **Runtime config keys** — `l2.levels`, `l2.level_weights`, `l2.imbalance_threshold`, `l2.persistence_window`, `l2.enabled`, `ev.calibration_enabled`, `ev.calibration_min_records`, `ev.calibration_over_threshold`

### Changed — Database

- **`nt_calibration_records.p_imbalance` renamed to `p_ext_signal`** — matches the EV engine rename
- **Added `predicted_p_win_raw` and `calibration_brier` columns** to `nt_calibration_records`
- **Migration `024_ev_calibration_l2_config.sql`** — seeds all new config keys into `nt_system_config`

### Infrastructure

- **No `.env` changes needed for LightningAI** — secrets injected as env vars are read automatically by `pydantic-settings`
- **Edge functions** — the Supabase Edge Functions (`sweep`, `heavy-sweep`, `light-regime`) are **deprecated**. The unified engine is the Python `SweepOrchestrator` on **LightningAI** (APScheduler-driven, reads cron schedules from `nt_symbol.regime_cron`). The edge function code is retained in `supabase/functions/` for reference only.

### Changed — Cron Schedules (Migrations 026 + 027)

> **NOTE:** These Supabase pg_cron schedules are now deprecated for sweep invocation. The SweepOrchestrator on LightningAI reads per-symbol `regime_cron` values from the `nt_symbol` table directly. The migration SQL below documents the historical schedule structure; the `nt_symbol.regime_cron` column values match this cadence.

- **Heavy sweeps moved to weekends only** — all `sweep-heavy-*` cron jobs now fire on Saturday/Sunday only (`0,6` DOW) at 20:00 UTC. Previously ran 6×/day for crypto and weekdays for stocks
- **Light stock sweeps reshaped to US market cycle** — split into peak (15min frequency 12:00-21:00 UTC) and off-peak (1hr frequency 21:00-12:00 UTC) windows. ~50% reduction in daily runs
- **Light crypto runs 24/7 on weekdays** — no suppression during US trading hours
- **`isHeavyHour()` day-aware** — the edge function guard now only suppresses light mode on weekends when heavy sweeps run. Weekdays never suppress
- **`sim_retry_stale_symbols()` F-Lite-1 check removed** — no longer skips retries during any hour since heavy sweeps don't run on weekdays

---

## [7.1.0] - 2026-05-24

### Fixed - Supabase Environment Variable Configuration

- **HTTP 500 on all `/renko/*` GET endpoints** - Root cause: `SUPABASE_URL` and `SUPABASE_KEY` were not configured on Render, causing `runtime_config` to fail when reading config values from Supabase `system_config` table. All Renko GET endpoints (`/renko/signals`, `/renko/state`, `/renko/bricks`, etc.) use `_get_pipeline_async()` which calls `RenkoConfig.from_runtime_config()`, requiring Supabase connectivity. Now returns proper 401 (not 500) after env vars are correctly set.

### Changed - Documentation & OpenAPI Spec Updates

- **OpenAPI spec v7.1.0** - Updated authentication description to reflect unified auth (Clerk JWT, Local JWT, SaaS API Key, Static API Key) instead of Clerk-only
- **OpenAPI spec** - Added `ApiKeyAuth` security scheme (`X-API-Key` header)
- **OpenAPI spec** - Corrected auth levels for `/renko/regime` and `/renko/equity` from "Write" to "Admin" (matching router code `require_admin`)
- **OpenAPI spec** - Added 401/403 response documentation for admin-only endpoints
- **OpenAPI spec** - Updated `/renko/snapshot/restore` to indicate "Write" access requirement
- **Renko README** - Corrected auth levels in endpoint table (`/renko/regime` and `/renko/equity` are Admin, not Write)
- **Renko README** - Added Render deployment note about Supabase env var configuration
- **Renko README** - Added Supabase config 500 error to bug fix history table

### Deployment Notes

- Required Render environment variables: `SUPABASE_URL` + `SUPABASE_KEY` (service role key)
- There is NO `SUPABASE_PUBLISHABLE_KEY` variable - the backend needs the service role key for full database access
- Verify connectivity with `GET /renko/health` (no auth required)

---

## [5.1.0] - 2026-05-20

### Phase 6+7: Frontend UI

- **StatisticalRigorPanel.jsx** - New component with 4 sub-cards:
  - DSR Detail Card (observed_sharpe, expected_max_sharpe, p_value, n_trials, sample_length, threshold)
  - Bootstrap CI Table (4 metrics × 2 methods: percentile + circular block)
  - Multiple Testing Corrections (Bonferroni, Holm-Bonferroni, BH-FDR with summary counts)
  - Significance Tests (White's Reality Check + Hansen's SPA with consensus alert)
- **ExecutionModelingPanel.jsx** - New component with 5 sub-cards:
  - Almgren-Chriss Market Impact (participation rate, permanent/temporary/total impact, composition bar)
  - Fill Probability (order type, distance metrics, adverse selection, gauge visualization)
  - Financing Costs (borrow/margin/dividend breakdown with HTB badge)
  - Enhanced Cost Breakdown (bar chart per cost type)
  - Execution Modeling Summary (market impact + fill probability + financing overview)
- **BacktestResults.jsx** - Tabbed navigation (Overview / Statistics / Execution),
  integrates both new panels with proper data extraction from both runtime field names
  (`bootstrap_ci` and `bootstrap_cis`, `deflated_sharpe` and `deflated_sharpe_result`)
- **BacktestPanel.jsx** - "Deep Analysis" button calling /renko/statistics/rigor and
  /renko/execution/model via bffFetch; new onSignificanceTests and onExecutionModelDetail callbacks
- **RenkoPage.jsx** - New state (significanceTests, executionModelDetail) wired to both panels
- **types/backtest.ts** - Fixed type/runtime mismatches, added 15 new interfaces
  (BootstrapCIMetric, SignificanceTestResults, MarketImpactResult, FillProbabilityResult,
  BorrowCostResult, MarginCostResult, DividendCostResult, FinancingCostsResult,
  ExecutionModelDetail, MultipleTestingSummary, BootstrapCIMethod, etc.)

## [5.0.0] - 2026-05-20

### Phase 6: Statistical Rigor
- **Deflated Sharpe Ratio** - Lopez de Prado DSR adjusts observed Sharpe for N independent trials
- **Bootstrap Confidence Intervals** - Percentile + circular block bootstrap CIs for Sharpe, win rate, profit factor, max drawdown
- **Multiple Testing Correction** - Bonferroni, Holm-Bonferroni, Benjamini-Hochberg FDR
- **White's Reality Check + Hansen's SPA** - Bootstrap-based significance tests for data snooping

### Phase 7: Advanced Execution Modeling
- **Almgren-Chriss Market Impact** - Square-root model with permanent + temporary impact components
- **Fill Probability Model** - Logit-based fill probability for limit/stop/market orders with adverse selection
- **Borrow & Financing Costs** - Short borrow costs, margin financing, dividend costs for shorts

### Pipeline Enhancements (Phase 2-3)
- **Transaction Cost Modeling** - Slippage, commission, and spread tracked separately per trade
- **Gap-Through Stop Handling** - Fills at brick open when price gaps past stop level
- **OCO Resolution** - Configurable SL-first or TP-first when both trigger in same brick

### New API Endpoints
- `POST /renko/backtest/run` - Full backtest with Phase 2-7 analytics
- `POST /renko/backtest/run/stream` - SSE streaming backtest
- `POST /renko/backtest/optimize` - Parameter sweep with multiple testing correction
- `POST /renko/statistics/rigor` - Standalone statistical rigor tests
- `POST /renko/execution/model` - Execution cost modeling

### Institutional Maturity Score
- Overall: **2.8 -> 3.7** (crosses 3.5 institutional threshold)
- Data Quality: 1.5 -> 4.0
- Statistical Validation: 2.5 -> 4.0
- Execution Modeling: 2.5 -> 4.0

---

## [5.4.0] - 2026-05-19

### Added - Progressive/Chunked Backtest Loading (SSE Streaming)

#### `renko/pipeline.py` - New Method
- `backtest_chunked(prices, timestamps, regimes, chunk_size=150)` - Generator that yields intermediate results after each chunk of ticks for SSE streaming
- Progress events: `{ type: "progress", chunk, total_chunks, percent, trades_so_far, stats_so_far, bricks_so_far, ticks_so_far }`
- Complete event: `{ type: "complete", stats, total_bricks, total_ticks, trades }` - identical shape to `backtest()` return value
- `chunk_size=150` matches the warmup route's `CHUNK_SIZE` pattern

#### `renko/router.py` - New Endpoint
- `POST /renko/backtest/run/stream` - SSE streaming backtest endpoint
- Uses `StreamingResponse` with `text/event-stream` media type
- Processes prices in 150-tick chunks via `backtest_chunked()`
- Emits `progress`, `complete`, and `error` SSE event types
- Cache integration: checks Redis before streaming; if HIT, emits `complete` immediately
- Final `complete` result is cached in Redis (1h TTL) for subsequent requests
- Headers: `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no` (disables nginx buffering)

#### Tests - `tests/test_renko_backtest.py` (6 new tests)
- `test_chunked_yields_progress_events` - verifies ≥2 progress + 1 complete event
- `test_chunked_progress_structure` - required keys in each progress event
- `test_chunked_percent_increases` - monotonically increasing percent
- `test_chunked_complete_matches_backtest` - chunked result matches regular `backtest()`
- `test_chunked_final_percent_is_100` - last progress = 100%
- `test_chunked_with_regimes` - regime labels passed through correctly

### Changed
- `renko/pipeline.py` - added `import math` and `Generator` type hint
- Total backend tests: 78 (25 general + 25 Renko + 28 cache)

---

## [5.3.0] - 2026-05-19

### Added - Redis Backtest Result Caching (1h TTL)

#### `services/backtest_cache.py` - NEW FILE
- `BacktestCache` - async Redis-backed cache for backtest results with 1h TTL
- Deterministic cache keys via SHA-256 of canonical JSON (sorted keys, price arrays replaced with length)
- Key format: `bt:{prefix}:{sha256_hash}` where prefix is `renko`, `renko:compare`, `renko:optimize`, or `hmm`
- Graceful fallback: if `REDIS_URL` is not configured or Redis is unavailable, all operations are no-ops
- Redis failures NEVER propagate to the caller - cache miss is returned instead
- `get(prefix, body)` -> cached result dict or None (cache miss)
- `set(prefix, body, result)` -> True/False (stores with 1h TTL)
- `invalidate(prefix, body)` -> removes specific cached result
- `clear_prefix(prefix)` -> removes all cached results for a prefix (admin/debug)
- `get_stats()` -> key counts, memory usage, TTL info
- `close()` -> graceful Redis connection cleanup
- Singleton `backtest_cache` shared across all endpoints

#### Cache Integration - Renko Endpoints
- `POST /renko/backtest/run` - checks cache before running; stores result after; `cached` field in response
- `POST /renko/backtest/compare` - checks cache before running; stores result after
- `POST /renko/backtest/optimize` - checks cache before running; stores result after

#### Cache Integration - HMM Endpoints
- `POST /backtest/run` - checks cache before running; stores result after; `cached` field in response

#### Response Model Updates
- `RenkoBacktestResponse.cached: bool` - True if served from Redis cache (default False)
- `BacktestResponse.cached: bool` - True if served from Redis cache (default False)

#### Tests - `tests/test_backtest_cache.py` (28 new tests)
- Cache key generation: deterministic hashing, param sensitivity, prefix isolation, price array length substitution, key order independence, input immutability
- Cache disabled: all operations return None/False when REDIS_URL not set
- Cache enabled (mocked Redis): GET hit/miss, SET with TTL, INVALIDATE, CLEAR_PREFIX, STATS
- Graceful fallback: Redis errors never propagate (GET returns None, SET returns False)
- Response model `cached` field: default False, accepts True

### Changed
- `main.py` version bumped to 5.3.0
- Total backend tests: 72 (25 general + 19 Renko + 28 cache)

---

## [5.2.0] - 2026-03-04

### Added - Renko Backtest Endpoints (Isolated Pipeline)

#### `renko/router.py` - 3 New Endpoints

Three isolated backtest endpoints that run on standalone `RenkoPipeline` instances, never affecting the live pipeline:

- `POST /renko/backtest/run` - Run a full Renko pipeline backtest on historical prices with configurable parameters. Returns comprehensive stats including win rate, profit factor, Sharpe estimate, max drawdown, per-pattern breakdowns, per-regime breakdowns, and Kelly inputs.
- `POST /renko/backtest/compare` - Compare 2–10 Renko pipeline configurations side-by-side on the same price data. Returns per-config results plus a diff summary highlighting key metric differences between the first config and the best (by Sharpe estimate).
- `POST /renko/backtest/optimize` - Grid-search parameter sweep for Renko pipeline optimisation. Accepts a `param_grid` dict mapping parameter names to lists of values. Returns summary rows with key metrics for each combination, plus `best_by_sharpe` and `best_by_return` highlights. Max 50 combinations per request.

#### Pydantic Models

- `RenkoBacktestRequest` - Full pipeline config + historical prices (min 50 ticks) with optional timestamps, regimes, and signal_confidence_min
- `RenkoBacktestResponse` - symbol, total_ticks, total_bricks, config_used, stats, trades
- `RenkoCompareConfig` - Labelled config subset for comparison (brick_size, sl_bricks, tp_bricks, trailing_stop, trail_after_bricks, trail_distance_bricks, bull_trigger_n, bear_trigger_n, regime_gate, max_trades_per_session, max_daily_loss_bricks, max_consecutive_losses)
- `RenkoBacktestCompareRequest` - prices + symbol + 2–10 configs + optional timestamps/regimes
- `RenkoBacktestCompareResponse` - comparisons list + diff dict (first vs best by Sharpe)
- `RenkoBacktestOptimizeRequest` - prices + symbol + param_grid + fixed default params + optional timestamps/regimes
- `RenkoBacktestOptimizeResponse` - results list (params + metrics per combo) + best_by_sharpe + best_by_return + n_combinations

#### Internal Helpers

- `_build_renko_config(req)` - Build a `RenkoConfig` from a backtest request body
- `_run_isolated_backtest(prices, config, ...)` - Run a backtest in an isolated pipeline (never touches live state)

### Changed
- `main.py` version bumped to 5.2.0
- All three backtest endpoints require authentication (`get_authed_user`) and are rate-limited under `RATE_LIMIT_WRITE`
- Backtest runs are executed in thread pools via `asyncio.to_thread` to avoid blocking the event loop

---

## [5.1.0] - 2026-05-19

### Added - MCP Server + Circuit Breakers + Renko Pipeline v2 + Discord

#### MCP Server (Model Context Protocol)
- `fastapi-mcp` integration mounted at `/mcp`
- 17 operations excluded for security (auth, feed start/stop, renko writes, stream injection, backtest delete)
- Configurable auth via `MCP_AUTH_ENABLED` and `MCP_ISSUER` settings
- Header forwarding for `authorization` and `x-api-key`

#### Circuit Breakers
- Three-tier circuit breaker: error rate (>50%), latency (>5s), concurrent (>100)
- 30-second cooldown with automatic recovery
- Stats exposed in `/health` endpoint
- Per-breaker state tracking (closed, open, half-open)

#### Renko HFT Pipeline v2
- 14 endpoints under `/renko/` prefix
- 6-layer pipeline: brick -> swing -> pattern -> signal -> risk -> execution
- Supabase caching with 4-hour TTL
- Yahoo Finance warm-up integration
- Pattern confidence scoring
- Signal-to-trade paper execution

#### Discord Notifications
- `notifications/discord_notifier.py` - webhook-based alert system
- Regime change alerts with severity coloring
- Circuit breaker trip notifications
- Trade execution confirmations
- Graceful no-op when `DISCORD_WEBHOOK_URL` not configured
- Async batched sending with retry

### Changed
- `main.py` version bumped to 5.1.0
- `/health` endpoint now includes MCP status, circuit breaker stats, and Discord status
- Lifespan: startup logs GPU capabilities, restores Redis sessions with HMM re-fitting

---

## [5.0.0] - 2026-05-15

### Added - Renko HFT Pipeline (Initial)

#### `renko/` - NEW MODULE
- Self-contained Renko HFT pipeline with 6 processing layers
- Brick construction from tick data (fixed and ATR-based)
- Swing classification (impulse/correction labelling)
- Pattern detection (double-top/bottom, head-shoulders, flags)
- Signal filtering with volume and trend confirmation
- Risk management with position sizing and stop/TP
- Paper trade execution with P&L tracking
- Supabase caching layer (4h TTL)

#### Endpoints
- `POST /renko/brick` - build Renko bricks
- `POST /renko/swing` - classify swings
- `POST /renko/pattern` - detect patterns
- `POST /renko/signal` - generate filtered signals
- `POST /renko/risk` - compute risk for signal
- `POST /renko/execute` - paper-trade execution
- `GET /renko/status` - pipeline status (all symbols)
- `GET /renko/status/{symbol}` - single symbol status
- `DELETE /renko/status/{symbol}` - clear state
- `POST /renko/warmup` - Yahoo Finance warm-up
- `GET /renko/trades` - list trades
- `GET /renko/trades/{symbol}` - symbol trades
- `GET /renko/cache` - cache statistics
- `DELETE /renko/cache` - clear cache

---

## [4.0.0] - 2026-05-10

### Added - Strategy Signals + TDA Features + Observation V2

#### Strategy Signal Pipeline
- `routers/strategy.py` - `POST /strategy/signal`
- Generates trading signal (long/short/flat) based on regime
- Combines regime detection, Kelly sizing, and risk guidance
- Signal strength scoring (0-1)
- Long: low/med-low vol + bull/strong_bull
- Short: high/med-high vol + bear/strong_bear (strength × 0.5)
- Flat: all other regimes

#### Topological Data Analysis (TDA)
- `routers/tda.py` - `POST /tda/features`
- Persistent homology feature extraction via gtda
- Takens embedding with configurable dimension and delay
- Persistence diagrams (0D and 1D)
- Betti numbers at each filtration step
- Persistence entropy for regime change detection
- Anomaly score (std deviations from baseline)
- 10-dimensional feature vector for ML pipeline
- Optional baseline comparison

#### Observation V2
- `POST /observation/build-v2` - enhanced observation vector with optional TDA
- Extends 24-feature vector to 34 features when `include_tda=true`
- TDA anomaly score and regime change probability fields
- Same Markov feature guarantees as v1

#### `models/schemas.py` - Updated
- `StrategySignalRequest` / `StrategySignalResponse`
- `TDARequest` / `TDAResponse`
- `ObservationV2Request` / `ObservationV2Response`

### Changed
- `main.py` version bumped to 4.0.0
- Backtest router registered in app factory

---

## [3.2.0] - 2026-05-19

### Added - Walk-Forward Backtest Engine + Persistence + Optimization

#### `core/backtester.py` - NEW FILE
- 503-line walk-forward HMM backtest engine
- Rolling HMM fit on trailing window with configurable refit interval
- Strategy signal generation based on detected regime
- Regime-gated Kelly position sizing
- Risk check gates (VaR, max drawdown)
- Trade execution with configurable commission and slippage
- 30+ performance metrics: win rate, Sharpe, Sortino, Calmar, profit factor,
  max drawdown, VaR/CVaR, avg hold bars, regime distribution, trades by regime
- Equity curve, drawdown curve, and full trade log output
- Optional date labels in trade log (pass `dates` array)

#### `services/backtest_store.py` - NEW FILE
- Supabase persistence layer for backtest results
- `save_backtest_result()` - persist to `ta_backtest_result` table (user-scoped)
- `get_backtest_history()` - paginated history with summary metrics only
- `get_backtest_detail()` - full result by ID (user-scoped)
- `delete_backtest()` - delete by ID (user-scoped)
- Graceful degradation when Supabase is not configured

#### `routers/backtest.py` - NEW FILE
- `POST /backtest/run` - run walk-forward backtest (rate-limited, Clerk/JWT auth)
- `GET /backtest/history` - paginated list of saved results
- `GET /backtest/{id}` - full result by ID
- `POST /backtest/compare` - side-by-side comparison with diff summary
- `POST /backtest/optimize` - parameter grid sweep (max 50 combos)
- `POST /backtest/export` - CSV/JSON export with section selection
- `DELETE /backtest/{id}` - delete saved result

#### `models/schemas.py` - Updated
- `BacktestRequest` - extended with `dates` and `save` fields
- `BacktestResponse` - full response with 30+ metric fields
- `BacktestSummary` - lightweight summary for list views
- `BacktestHistoryResponse` - paginated summary list
- `BacktestCompareRequest` / `BacktestCompareResponse` - comparison + diff
- `BacktestOptimizeRequest` / `BacktestOptimizeResponse` - grid sweep
- `BacktestExportRequest` - export format + section selection

#### `supabase/migrations/00000000000007_backtest_results.sql` - NEW FILE
- `ta_backtest_result` table with RLS policies (user-scoped access)
- Summary columns + JSONB columns for heavy data (equity curve, trade log, etc.)

### Fixed
- `pnl_pct` was always returning 0.0 in backtest trade log - now correctly computed

### Changed
- All backtest endpoints require Clerk/JWT authentication
- Rate-limited under `RATE_LIMIT_COMPUTE`

---

## [3.1.1] - 2026-Q2 (Testing & Tooling Release)

### Added - Testing Tools & Developer Experience

#### `curl_test.sh` - NEW FILE
Bash script: 6 assertions against a live server.
1. GET /health -> 200
2. POST /analyse/full with 200 synthetic SPY bars -> 200
3. All 12 response fields present (regime_label, confidence, risk_multiplier,
   recommended_f, sharpe_ratio, var_95, cvar_95, max_drawdown, suggested_stop,
   suggested_tp, etc.)
4. 6-bar payload -> 422 (validation rejection)
5. Negative price -> 422 (validation rejection)
Supports custom base URL: `./curl_test.sh http://my-host:8000`
Coloured output with jq if installed, falls back to python3 -m json.tool.

#### `stream_spy_ticks.py` - NEW FILE (423 lines)
Python script: seeds SPY with 150 bars, pushes 50 live ticks via POST /stream/tick,
prints a formatted TickResponse for each tick.
- Pure stdlib + numpy - no third-party HTTP client needed
- Formatted output: regime label (colour-coded), f*, mult, confidence, VaR95,
  stop/TP, prob bars for vol and trend posteriors
- Regime-change banner printed mid-stream
- Full summary table at end: regime sequence, f* range, VaR range, RTT latency
- CLI options: --url, --symbol, --seed-bars, --ticks, --interval, --regime,
  --mu, --sigma, --no-color, --json
- --json flag pipes raw TickResponse JSON for jq processing
- Buffering stub handled gracefully (warming-up message)

#### `sse_alert_monitor.html` - NEW FILE (368 lines)
Browser EventSource monitor for GET /sse/alerts.
- EventSource auto-reconnect with error distinction (never-connected vs dropped)
- Every raw message logged via console.log; alerts routed by severity:
  critical -> console.error · warning -> console.warn · info -> console.info
- Alert entry: coloured regime labels, mult/f* extracted from message string,
  critical badge pulses 3× via CSS animation
- Stats bar: live counts per severity, heartbeat count, uptime timer, last alert
- Log capped at 300 entries; all server strings pass through escHtml() (XSS guard)
- Enter key connects/disconnects; works with any CORS-enabled SSE endpoint

#### `payload_spy_200bars.json` - NEW FILE
Ready-made 200-bar SPY JSON payload for curl / Postman / Insomnia testing.
4-segment multi-regime series (low-vol bull -> med bull -> med bear -> high-vol bear).

### Changed
- `README.md` - full rewrite: complete project structure tree, all 21 endpoints,
  Masaniello formula table, 24-feature index table, testing sequence, auth/Redis/GPU
  quickstart commands

---

## [3.1.0] - 2026-Q2

### Changed - Dynamic Masaniello + Kelly Unified Sizing Engine

#### `core/position_sizer.py` - Full Refactor

Two complementary sizing paths now live in the same module, independently usable.

**Path A - Portfolio-fraction Kelly (preserved, interface unchanged)**
`PositionSizer`, `PositionRequest`, `KellyResult` - stateless, returns-based.
All existing callers continue to work without modification.

**Path B - Dynamic Masaniello Sizer (new)**

Implements the formula:

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

New public symbols:

| Symbol | Purpose |
|--------|---------|
| `SizingConfig` | β, min/max risk, gates, kelly overlay, batch params |
| `TradeContext` | Per-trade inputs (equity, stop, point_value, p_win, R/R, regime fields) |
| `TradeContext.from_snapshot(snap, ...)` | Build context directly from `RegimeSnapshot` |
| `PositionSizeResult` | All intermediate factors + contracts + block reason |
| `PositionSizeResult.summary()` | One-line human-readable string |
| `BatchState` | Cumulative wins/losses/pnl/drawdown/peak_equity tracker |
| `DynamicMasanielloSizer` | Stateful sizer; `.batch` tracks inter-trade state |
| `.size_trade(ctx)` | Core formula computation |
| `.size_from_snapshot(snap, ...)` | Convenience wrapper - builds context + sizes |
| `.run_batch(contexts, outcomes, pnls)` | Size N trades sequentially with state injection |
| `monte_carlo_batch(sizer, ctx, n)` | Simulate N independent batches; return PnL/DD distribution |

**Pure factor functions (testable in isolation):**
`_masaniello_factor`, `_quality_factor`, `_drawdown_factor`, `_volatility_factor`,
`_fractional_kelly`, `_expected_edge`

**Factor details:**

`M_i = clip((W - w) / (N - i + 1), 0.0, 1.5)`
- Wins needed divided by trades remaining. Zero when target already met.
- `(0.5 + M_i)` term guarantees minimum 0.5× base risk even when M_i = 0.

`Q_i = prob_factor × regime_factor × conf_factor`
- `prob_factor  = clip((p_win - min_prob) / 0.10, 0.0, 1.5)`
- `regime_factor = clip(regime_quality, 0.0, 1.5)`  ← from `RegimeSnapshot.risk_multiplier`
- `conf_factor   = clip(state_confidence, 0.0, 1.0)` ← from `RegimeSnapshot.confidence`
- Q_i = 0 when any sub-factor is zero; acts as a quality gate.

`DD_i = clip(1 - dd / max_dd, 0.25, 1.0)`
- Floor at 0.25 preserves minimum position participation during drawdown.

`V_i = clip(ATR_baseline / ATR_current, 0.5, 1.5)`
- Inverted ATR ratio. High vol -> shrink; low vol -> mild expansion.

**Pre-flight gates (in priority order, fire before formula):**
1. equity > 0
2. stop_distance_price > 0
3. point_value > 0
4. regime_quality ≥ regime_floor
5. p_win ≥ min_prob
6. reward_risk ≥ min_rr
7. batch not halted
8. drawdown < batch_halt_dd (triggers halt on violation)
9. expected_edge > 0

**Kelly overlay:**
When `use_kelly_overlay=True`, the Masaniello fraction is capped at
`kelly_fraction × max(0, (b·p - q) / b)`. When kelly_cap = 0 (no edge),
the min_risk floor still applies (position is not zeroed out).

**Hard clamp:**
`risk_fraction = clip(f_i, min_risk, max_risk)` - always enforced last.

#### `test_position_sizer.py` - NEW FILE
95-test suite covering:
- Factor math boundary conditions (all 4 factors + Kelly + edge)
- Core formula with known inputs (manual verification)
- Gate ordering - each block reason fires correctly
- Kelly overlay (cap applied, cap=0 fallback)
- Hard clamp (upper and lower bounds)
- RegimeSnapshot integration (`from_snapshot`, `size_from_snapshot`)
- BatchState lifecycle (record, drawdown, reset)
- `run_batch` state injection and sequential progression
- Monte Carlo (p_target_hit ~69.5%, p_halt, mean/std/p10/p90 PnL)
- Path A interface preserved (KellyResult fields, regime gating, vol scaling)

### Fixed
- Uploaded `dataclass.md` bug: `_init_` (single underscores) corrected to `__init__` in `DynamicMasanielloSizer`
- Corrected Kelly gate test: `p=0.50, R=1.0` correctly yields `f=0.0` (not `p=0.50, R=2.0` which has positive edge)

---

## [3.0.0] - 2025-Q1

### Added - GPU HMM · Live Feeds · Correlation Regimes · Portfolio Optimisation

#### `core/gpu_engine.py` - NEW FILE
- `GpuRegimeHMM` - pomegranate PyTorch-backed 4-state HMM, identical interface to `RegimeHMM`
- `create_regime_hmm(force_cpu)` - auto-selecting factory: GPU if pomegranate + CUDA/MPS available, else hmmlearn CPU
- `gpu_capabilities()` - returns `{pomegranate_installed, cuda_available, mps_available, active_device, hmm_backend}`
- Transparent CPU fallback - zero code changes required when GPU unavailable
- Supports CUDA (NVIDIA), MPS (Apple Silicon), and CPU targets

#### `adapters/feed_adapters.py` - NEW FILE
- `FeedAdapter` - abstract base with exponential-backoff reconnect (max 10 attempts)
- `AlpacaFeedAdapter` - alpaca-py WebSocket, supports SIP and IEX feeds, equities + crypto
- `BinanceFeedAdapter` - native WebSocket kline stream, any interval (1m/5m/1h/...), mainnet + testnet
- `IBFeedAdapter` - ib_async TWS API, real-time 5-second bars, equities/futures/FX
- `OHLCVBar` - unified OHLCV dataclass with `source` field
- `FeedManager` - multi-adapter orchestrator, routes bars into `SessionRegistry`, chainable `.add()`

#### `core/correlation_regime.py` - NEW FILE
- `CorrelationRegimeDetector` - DCC-based portfolio-level correlation regime classifier
- Extracts rolling Pearson correlation matrices (configurable window, default 60 bars)
- Applies EWMA vol standardisation before correlation computation
- Fits a 4-state Gaussian HMM on upper-triangle correlation feature vectors
- States labelled by mean |ρ|: `low_corr` | `mid_corr` | `high_corr` | `crisis`
- `CorrelationSnapshot` - per-asset breakdowns + `blended_risk_multiplier` = mean(asset_mults) × corr_mult
- Correlation risk multipliers: `low_corr=1.00` · `mid_corr=0.85` · `high_corr=0.60` · `crisis=0.35`

#### `core/portfolio_optimiser.py` - NEW FILE
- `DrawdownOptimiser` - regime-aware max-Sharpe subject to drawdown constraint
- Three regime layers: (1) regime-adjusted μ, (2) per-asset weight bounds, (3) correlation exposure
- Drawdown constraint via Cornish-Fisher CVaR proxy (analytic - no simulation required)
- Correlation exposure scalar: `low_corr=1.00` · `mid_corr=0.90` · `high_corr=0.70` · `crisis=0.50`
- Weight bounds tightened per-asset: `high_vol_strong_bear -> max_weight × 0.25`
- Scipy SLSQP solver with configurable `max_iter`, `tol`, and `dd_penalty_lambda`
- `OptimisationResult` - weights, regime_adj_weights, expected return/vol/Sharpe/DD, convergence info

#### `routers/gpu.py` - NEW FILE
- `GET /gpu/capabilities` - hardware and backend report
- `POST /gpu/benchmark` - fit + predict latency benchmark on synthetic data, `force_cpu` flag

#### `routers/feeds.py` - NEW FILE
- `POST /feeds/start` - register and start feed adapters as background asyncio tasks
- `POST /feeds/stop` - graceful shutdown of all adapter tasks
- `GET /feeds/status` - adapter health + per-symbol bar counts

#### `routers/multi_asset.py` - NEW FILE
- `POST /correlation/detect` - fit DCC correlation HMM + return correlation regime snapshot
- `POST /optimise` - solve drawdown-controlled max-Sharpe with regime overlays
- `POST /optimise/full` - one-shot: correlation detect + portfolio optimise

#### `main_v4.py` - NEW FILE
- v3.0 app factory with GPU capability logging on startup
- `GET /health` now includes `gpu_backend` and `device` fields

### Changed
- `requirements.txt` - added optional GPU and feed adapter dependency comments
- `core/portfolio_optimiser.py` - fixed array ambiguity bug in `neg_sharpe_with_dd` (scipy `max()` applied to matrix)

### Architecture summary

```
GET  /gpu/capabilities       pomegranate install check -> CUDA/MPS/CPU selection
POST /gpu/benchmark          synthetic fit -> latency report

POST /feeds/start            FeedManager -> AlpacaFeedAdapter/BinanceFeedAdapter/IBFeedAdapter
                             -> SessionRegistry.get(symbol).tick(close, ts)

POST /correlation/detect     rolling corr features -> 4-state HMM -> corr regime label
POST /optimise               regime-adj μ -> SLSQP -> DD-constrained weights
POST /optimise/full          detect + optimise in one request
```

---

## [2.1.0] - 2024-Q4

### Added - Simulation · Portfolio · Redis Persistence · JWT Auth

#### `core/simulator.py` - NEW FILE
- `RegimeSimulator` - Markov-chain Monte Carlo regime transition simulator
- Builds a 16×16 combined transition matrix via Kronecker product of the fitted
  4-state vol and trend HMM transition matrices: `T_combined = T_vol ⊗ T_trend`
- Fits per-regime (μ, σ) return distributions from Viterbi-decoded historical paths
- Simulates `n_paths` forward trajectories of `horizon` steps with regime switching
- Returns: price fan (p5/p25/median/p75/p95), expected risk multiplier per step,
  terminal return distribution (mean, std, VaR95, CVaR95), most likely terminal
  regime, % paths with positive return, mean maximum drawdown across paths

#### `services/portfolio_service.py` - NEW FILE
- `PortfolioService` - aggregates regime + risk analytics across all active sessions
- Per-symbol: regime label, confidence, risk_multiplier, Kelly f*, VaR95, Sharpe
- Portfolio-level: dominant regime, regime consensus, avg risk multiplier,
  portfolio VaR (√ΣVaR² - independence assumption), portfolio CVaR
- Risk flags: `high_risk_count`, `concentration_flag`, `regime_divergence_flag`
- Regime divergence detection via normalised entropy of vol_state distribution

#### `services/redis_persistence.py` - NEW FILE
- `RedisPersistence` - optional Redis Streams price buffer persistence layer
- All methods are no-ops when `REDIS_URL` env var is not set (zero overhead)
- `restore(session)` - hydrates session price buffer from Redis on startup
- `append_price(symbol, price)` - appends to Redis Stream with MAXLEN cap
- `save_meta / load_meta` - stores session config (kelly_fraction, etc.)
- `list_persisted_symbols()` - returns all symbols with persisted streams
- Failure-tolerant: Redis errors never raise in the tick path (log + continue)

#### `auth/jwt_auth.py` - NEW FILE
- `create_access_token(data)` - creates signed HS256 JWT
- `decode_token(token)` - validates and decodes JWT -> `TokenData`
- `get_current_user` - FastAPI Depends for HTTP endpoint auth
- `require_write` - Depends requiring trader or admin role
- `require_admin` - Depends requiring admin role
- `ws_auth(websocket, token, api_key)` - WebSocket auth (closes 4001 on failure)
- `make_login_response(sub, role)` - standard token response helper
- API key fallback via `X-API-Key` header and `API_KEYS` env var
- `AUTH_ENABLED=false` disables auth entirely for development

#### `routers/simulate.py` - NEW FILE
- `POST /simulate/{symbol}` - fits HMM + simulates regime transition paths
- Parameters: `horizon` (1–252), `n_paths` (50–5000), `seed`, `current_price`

#### `routers/portfolio.py` - NEW FILE
- `GET /portfolio` - aggregated portfolio risk view across all active sessions
- Query params: `symbols` (CSV), `kelly_fraction`, `target_vol`, `base_risk_limit`
- `GET /portfolio/symbols` - list all seeded symbols

#### `routers/auth_router.py` - NEW FILE
- `POST /auth/token` - OAuth2 password flow, issues JWT
- `GET /auth/me` - returns current user info from token
- `POST /auth/refresh` - issues a fresh token (same sub/role)
- User store configured via `AUTH_USERS` env var (user:pass:role CSV)

#### `main_v3.py` - NEW FILE
- v2.1 app factory with `lifespan` context manager
- On startup: restores persisted sessions from Redis, re-fits HMMs
- All v2.0 endpoints unchanged; v2.1 routers added alongside
- `/health` returns Redis status and active session count

### Changed
- `requirements.txt` - added `python-jose[cryptography]`, `passlib[bcrypt]`, `redis`

### Architecture summary

```
POST /simulate/{symbol}    HMM fit -> 16-state Kronecker system -> Monte Carlo
GET  /portfolio            SessionRegistry -> per-symbol regime + risk -> aggregate
POST /auth/token           credential check -> JWT issue
WS   /ws/{symbol}          (auth-ready: pass ?token=<jwt> to authenticate)
REDIS_URL (env)            on startup -> restore buffers -> re-fit HMMs
```

---

## [2.2.0] - 2024-Q3

### Added - 24-Feature Observation Vector & Stateful Builder

#### `core/obs_builder.py` - NEW FILE
- `InferenceObservationBuilder` - stateful class that maintains a fitted `RegimeHMM`
  across calls, ensuring Markov features (indices 14–19) are always computed from
  live HMM posteriors, never from a uniform prior
- `F` - feature index namespace (single source of truth for all 24 indices)
- `Observation` - output dataclass with `.markov_features` slice property and
  `.is_markov_uniform(tol)` guard method
- `build(prices, high, low)` - full-window build; re-fits HMM every `refit_every` bars
- `build_from_tick(price, high, low)` - streaming single-bar variant
- `seed(prices, high, low)` - historical warm-up and initial HMM fit
- `update_recommended_f(f)` - injects current Kelly fraction for Masaniello feature

#### 24-Feature Vector

| Index | Feature | Notes |
|-------|---------|-------|
| 0 | `log_return_1bar` | 1-bar log return |
| 1 | `log_return_3bar` | 3-bar log return |
| 2 | `log_return_10bar` | 10-bar log return |
| 3 | `normalised_atr` | ATR(14) / price |
| 4 | `rolling_vol_20` | 20-bar σ (annualised) |
| 5 | `ema_distance` | (price − EMA20) / EMA20 |
| 6 | `hhll_score` | HH/HL structure score ∈ [−1, 1] |
| 7 | `vol_regime_percentile` | Vol percentile vs 252-bar history |
| 8 | `atr_vs_baseline` | ATR(14) / ATR(252-bar mean) |
| 9 | `vol_trend_slope` | Standardised 10-bar vol slope |
| 10–13 | `regime_prob_{0..3}` | Raw HMM state posteriors (unsorted) |
| **14** | **`vol_prob_low`** | P(low vol) - Markov feature |
| **15** | **`vol_prob_medium`** | P(med_low + med_high) - Markov feature |
| **16** | **`vol_prob_high`** | P(high vol) - Markov feature |
| **17** | **`trend_prob_down`** | P(strong_bear + ½·bear) - Markov feature |
| **18** | **`trend_prob_neutral`** | P(½·bear + ½·bull) - Markov feature |
| **19** | **`trend_prob_up`** | P(½·bull + strong_bull) - Markov feature |
| 20 | `regime_quality` | HMM confidence scalar |
| 21 | `state_confidence` | max(vol_probs) × max(trend_probs) |
| 22 | `masaniello_pressure` | f* × (1 − f*) - peaks at 0.25 when f=0.5 |
| 23 | `drawdown_factor` | Current DD from peak ∈ [−1, 0] |

#### `core/regime_engine.py` - Updated
- Added `_sanitize_model()` static method: fixes NaN `startprob_`, zero `transmat_`
  rows, NaN `means_`, and degenerate `_covars_` after EM convergence failure
- `fit()` calls `_sanitize_model()` on both sub-models after every EM run
- `predict()` adds NaN fallback: degenerate posteriors replaced with uniform, then
  re-normalised - guarantees finite output even on pathological price series

#### `test_obs_builder.py` - NEW FILE
- 7-test suite: shape, finite values, Markov non-uniformity, Masaniello response,
  drawdown sign, streaming tick path, and all-zero sentinel guard

### Fixed
- `RegimeHMM._sanitize_model()` writes to `_covars_` private attribute (not the
  property setter) to avoid hmmlearn's shape-validation error on `diag` covariance
- Uniform-prior bug documented and guarded: `is_markov_uniform()` method on
  `Observation` detects std < 0.05 across Markov features and warns

---

## [2.1.0] - 2024-Q3

### Changed - 4-State HMM Regime Detection

#### `core/regime_engine.py`
- **n_components: 3 -> 4** for both the volatility and trend HMMs
- Volatility states: `low | med_low | med_high | high`  (was `low | medium | high`)
- Trend states: `strong_bear | bear | bull | strong_bull`  (was `bear | neutral | bull`)
- Volatile feature set expanded: vol HMM now uses 3 features (20-bar RV, 5-bar RV, |return|); trend HMM uses 3 features (return, 10-bar cum, 30-bar cum)
- Trend state labelling now sorts on feature index 2 (30-bar cumulative return) for cleaner strong_bull/strong_bear separation
- Min bars requirement: 80 (was 50) to support stable 4-state EM convergence
- Risk multiplier range extended: **[0.10, 1.75]** (was [0.25, 1.50])
- 4×4 = 16 distinct regime cells (was 9)

#### Downstream changes (label references only)
- `routers/regime.py`, `routers/pipeline.py` - label zips use `RegimeHMM.VOL_LABELS` / `RegimeHMM.TREND_LABELS`
- `routers/stream_rest.py`, `routers/stream_ws.py` - same label zip update
- `models/schemas.py`, `models/stream_schemas.py` - `min_length` / `ge` validators updated to 81
- `services/stream_session.py` - `MIN_PRICES_FOR_FIT` updated from 51 -> 81

#### Added
- `test_platform_v4.py` - 4-state smoke test verifying 4 prob vectors, 16-cell multiplier table, and correct assertions

---

# Changelog

All notable changes to the Dynamic Regime Risk Management Platform are documented here.
Follows [Semantic Versioning](https://semver.org/).

---

## [2.0.0] - 2024-Q2

### Added - Live Streaming Layer

#### `services/stream_session.py` - `StreamSession`
- Per-symbol stateful streaming engine with rolling price deque (configurable window, default 500 bars)
- Background HMM re-fit via `asyncio.run_in_executor` every N bars (default 50) - non-blocking
- 3-bar alert debounce: regime must persist for 3 consecutive bars before a `RegimeAlert` fires
- NaN/zero-price guard: invalid prices are filtered from the buffer before every HMM call
- Multiple concurrent subscriber queues per session (bounded at 500 items; slow consumers drop ticks)
- `RegimeAlert` dataclass with `severity` field: `info` | `warning` | `critical`

#### `services/registry.py` - `SessionRegistry`
- Singleton session store (`asyncio.Lock`-protected)
- `get_or_create(symbol, ...)` - idempotent session creation with per-session config
- `stats()` - returns live metrics for all sessions (ready, n_bars, refit_count, tick_count, last_regime)

#### `routers/stream_rest.py` - REST streaming endpoints
- `POST /stream/seed` - seed a symbol session with historical prices
- `POST /stream/tick` - push one price tick, receive `TickResponse` or buffering stub
- `POST /stream/ticks` - batch tick ingestion / backtest replay
- `GET /stream/sessions` - list all active sessions + stats
- `GET /stream/session/{symbol}` - single session status
- `DELETE /stream/session/{symbol}` - remove a session

#### `routers/stream_ws.py` - WebSocket + SSE endpoints
- `WS /ws/{symbol}` - full-duplex WebSocket: seed / tick / subscribe / ping message types
- `GET /sse/{symbol}` - SSE stream pushing `TickResponse` JSON on every bar
- `GET /sse/alerts` - global SSE broadcast for all regime-change alerts across all symbols
- 30-second heartbeat in subscriber mode; 15-second SSE comment heartbeat

#### `models/stream_schemas.py` - Streaming Pydantic models
- `SeedRequest`, `TickIngest`, `TickResponse`, `SessionStatus`, `WsMessage`

#### `main_v2.py`
- Updated app factory including all streaming routers alongside existing batch routers

#### `test_streaming.py`
- Async smoke test: seed -> 160-tick stream -> subscriber queue -> regime transition assertions

### Fixed

- `RegimeHMM` covariance type changed from `"full"` to `"diag"` for numerical stability on short rolling windows (< 300 bars)
- NaN/zero-price guard added to `_refit()` and `_compute_tick()` in `StreamSession`
- Test data continuity bug fixed (live price segment now correctly continues from seed tail)

---

## [1.0.0] - 2024-Q1

### Added - Batch Risk Management API

#### `core/regime_engine.py` - `RegimeHMM`
- Dual 3-state Gaussian HMM for volatility and trend regime classification
- Forward-backward posterior probabilities (not Viterbi hard assignments)
- Semantic state labelling by sorting component means - no manual mapping
- `RegimeSnapshot` dataclass with `risk_multiplier` property [0.25 – 1.50]

#### `core/position_sizer.py` - `PositionSizer`
- Full Kelly Criterion: `f* = μ / σ²` (continuous log-return formula)
- Fractional Kelly: `f* × kelly_fraction` (default 0.5)
- Volatility-scaled Kelly: `fractional_f × (target_vol / realised_annual_vol)`
- Regime-gated sizing: `vol_scaled_f × risk_multiplier`
- Automatic diagnostic notes (over-Kelly, negative Sharpe, vol mismatch, regime warnings)

#### `core/risk_manager.py` - `RiskManager`
- Historical (empirical) VaR at 95% and 99%
- CVaR / Expected Shortfall at 95% and 99%
- Maximum drawdown (peak-to-trough)
- Annualised return, volatility, Sortino ratio, Calmar ratio
- Regime-adjusted max loss limit: `base_risk_limit × risk_multiplier`
- Suggested stop-loss (2× CVaR95) and take-profit (3:1 reward/risk)
- Risk budget utilisation: `recommended_f / max_safe_f`

#### `models/schemas.py` - Pydantic v2 models
- `PricePayload`, `RegimeRequest/Response`, `SizeRequest/Response`, `RiskRequest/Response`, `FullAnalysisRequest/Response`
- Field-level validation: `min_length=51`, `ge`, `le`, `gt`, all-positive price guard

#### Routers
- `POST /regime/detect`
- `POST /size/kelly`
- `POST /risk/analyse`
- `POST /analyse/full` - one-shot full pipeline

#### Infrastructure
- `main.py` - FastAPI app factory with CORS middleware
- `requirements.txt` - pinned dependency versions
- `test_platform.py` - batch pipeline smoke test with synthetic multi-regime prices

---

## Roadmap

### Planned for v2.1
- `POST /simulate/{symbol}` - ODE-based regime transition simulation (simupy integration)
- `GET /portfolio` - multi-symbol aggregated regime + risk summary
- Redis Streams persistence for price buffers (survive process restarts)
- JWT authentication middleware for WebSocket handshake

### Planned for v3.0
- GPU-accelerated HMM via `pomegranate` (PyTorch backend)
- Real-time OHLCV feed adapters (Alpaca, Interactive Brokers, Binance)
- Multi-asset correlation regime detection
- Drawdown-controlled portfolio optimisation layer
