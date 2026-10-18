# Kairos calibration, geometry & reachability — full triage: 3 determinations, all findings, priority plan

**Date:** 2026-10-13
**Type:** Independent review/triage (whole-chain audit: signal generation → qualification → geometry → reachability → calibration → publish). No code changed.
**Trigger:** Operator request — *"review/triage the kairos calibration, geometry & reachability issues. Claude has added unnecessary weight the code. Determine: (1) signals generated selects the best choice + calibration bias (qualified signal); (2) qualified signals have the correct entry/TP/SL price (reachability & geometry bands); (3) qualified signals calibration accurately forecasted the signal p_win and predicts the next signal."*
**Method:** Four parallel deep-dive analyses (signal selection & bias; geometry & reachability; calibration & p_win forecasting; dead-weight audit), every load-bearing claim verified first-hand at HEAD `aa9fff1`.
**Status:** Diagnosis complete. Verdicts + priority triage below. Evidence is file:line at HEAD.

---

## 0. The three determinations — verdicts up front

| # | Determination | Verdict | One line |
|---|---|---|---|
| 1 | Signals generated → best choice + calibration bias (qualified signal) | **NO / PARTIAL** | No best-choice selection exists (regime-label string match, one signal per symbol per tick); calibration bias is applied correctly on the **light path only** — the heavy path still has the enforcement clobber. "Qualified" = weak floors passed, not edge. |
| 2 | Qualified signals have correct entry/TP/SL (reachability & geometry bands) | **STRUCTURALLY YES, EVIDENTIALLY WEAK** | G1–G4 of the coherent-flow redesign are all DONE at HEAD (docs stale on G1); geometry is one in-band ATR solve applied on both paths — but 68/77 shipped geometries are the zero-evidence band centre, scored-dead buckets can still publish, entry is uncosted, ATR is close-only. |
| 3 | Calibration accurately forecasts p_win + predicts the next signal | **NO** | The repo's own instruments: AUC 0.4992 / BSS −0.15 on n=74k; era-clean bias +0.21…+0.63 on 109/111 cells; every constructed predictor fails its pre-registered bar out-of-fold. Nothing predicts the next signal live. Era-clean book: WR 12.5%, expectancy_R −0.58. |

Supporting verdict — **"unnecessary weight": CONFIRMED, ~30% of `sweep_orchestrator` (~10.2k LOC) is off-path** (research code in the prod package + shadow-stamp blocks), `orchestrator.py` is a 6,212-line monolith, 99 feature flags. Details in §4.

---

## 1. Determination 1 — best-choice selection + calibration bias

### 1.1 No best-choice mechanism exists

- Direction is a **one-shot string match** on the HMM regime label: `orchestrator.py:687-720` (`_compute_trade_signal`: `"bull" in lower → buy`, `"bear" in lower → sell`, else neutral). One signal per symbol per tick. Heavy path identical (`heavy_sweep.py:1253`, `:1174`). No argmax, no candidate set, no competition between alternatives, no live abstention.
- The only "best-of" logic in the system is **geometry** (heavy-sweep Sharpe argmax `pick_best_combo`, `heavy_sweep.py:334-362`; the dynamic-ATR solve). The **signal itself is never selected — it is derived**.
- The **strategy router** — the one component that *would* select (one archetype or `stand_by` per signal, `strategy_router.py:204-298`) — is **shadow/stamp-only** (`config/strategy_registry.yaml:206 mode: shadow`; stamp at `orchestrator.py:4738-4757` → `nt_signal_sim.strategy_archetype` `:5837`; migration 206 "no live effect"). Its own R2 measurement declined **99.6% of signals** (924/928 `stand_by`, `worklog/kairos/reports/20261012_r2_router_measure.md`) because macro/trend labels are usually neutral/missing → alignment `None` → `stand_by` (`strategy_router.py:142-150`). Heavy rows get no archetype stamp at all (`heavy_sweep.py:1798-1840`).
- `stand_by` exists **only inside the shadow router**. The live engine always emits a direction per symbol per tick.

### 1.2 Calibration bias application — fixed on light, ALIVE on heavy

Light-path ordering at HEAD (`_process_symbol`): `compute_ev` (`:4322-4333`) → `p_win_raw` stash (`:4472`) → `apply_bracket_to_ev` (live, `:4479-4488`) → rank stamp (`:4535-4537`) → `apply_calibration_adjustment` with `rank_score` (`:4539-4552`).

**The enforcement-clobber fix (worklog 20261012_ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md) is PRESENT on the light path** — verified first-hand, `orchestrator.py:4569-4578`:

```python
if _bracket_applied_to_ev:
    # Kelly sizing may still be reduced by enforcement, but the
    # probability and EV stay the reachability values.
    effective_kelly = calib_adj["kelly_f"]
```
Bracket EV is terminal; enforcement records diagnostics and adjusts Kelly only. The `else` branch (`:4579-4604`) does the directional shrink + `recompute_ev_fields` correctly.

**The identical bug is still UNGUARDED on the heavy path** — verified first-hand, `heavy_sweep.py:1478-1483` applies bracket EV, then `:1506-1536`:

```python
if calib_adj and calib_adj.get("applied"):
    ev_result.p_win = calib_adj["p_win"]          # :1526 — no _bracket_applied_to_ev guard
    _recomputed = self._ev_engine.recompute_ev_fields(
        ev_result.p_win, ev_result.profit, ev_result.stake,
    )                                              # :1528 — p_expire DROPPED in recompute
    ev_result.ev = _recomputed["ev"]              # :1531
```

A low-reachability heavy signal with an active overconfidence penalty can still be shrunk toward 0.5, repriced to +EV, and published — the exact XAUAUD failure mode. Heavy also does not pass `rank_score` (`:1517-1524`), so the live rank-conditioned shrink is light-only. **The clobber-fix test (`tests/test_ev_not_clobbered_by_enforcement.py`) never imports `orchestrator` or `heavy_sweep` — it pins arithmetic, not wiring, which is exactly how the heavy path could carry the regression unnoticed.**

Enforcement mechanics (verified at HEAD, `calibration_enforcement.py`): shrink `p_win' = (p_win·n + 0.5·10)/(n+10)` (`:100-128`; at the typical n=2 the prior carries ~83%); rank-conditioned `n_eff = n·(1+slope·(0.5−rs)·2)`, slope 0.5 (`:507-543`); kelly ×0.5 **overconfidence-only** now (`:372-377`, `:438-457` — the audit's "same cut both directions" is outdated); stagnation kill-switch → p_win=0.5, kelly=0 (`:302-322`); R3 hierarchy cell (n≥10) → symbol → system (`:565-656`).

### 1.3 What "qualified" actually guarantees — weak floors, not edge

Live light gates (`orchestrator.py:4875-4907`, `qualified = all(gates)` at `:4969-4974`):

| Gate | Threshold | Status at HEAD |
|---|---|---|
| signal_qualifiable | buy/sell | live |
| regime_conf_ok | ≥ 0.60 | live |
| kelly_min_ok | — | **REMOVED on light** (`:4878-4882`); MIN_KELLY retained on heavy (`heavy_sweep.py:2208-2209`) |
| timesfm available/confidence | soft mode | soft never rejects (`:4382-4431`; yaml:50) |
| ev_allowed | ev > 0 AND rr ≥ 1.5 | live (`ev_engine.py:814`) |
| ev_floor_ok | ev ≥ 0.0 | live (`:4895`) — **absent on heavy** |
| p_win_ok (bracket path) | per-asset-class min 0.20/0.30/0.40/0.50, **hard floor 0.0** | live (`:4840-4845`; yaml:940-947) |
| p_win_ok (directional path) | hard floor 0.50, soft band → 0.55 | live |
| regime_edge_ok / context_aligned_ok | — | shadow → default pass (`:4906`, `:4937`) |

- On the **bracket path the p_win hard floor is 0.0** — `p_win_bracket = 0.056` (the XAUAUD case) *passes* `p_win_ok` with kelly shrink 0.28. **The EV floor is the single gate holding the book honest.**
- `p_win = 0.501` qualifies on the directional path. "Qualified" ≠ edge.
- Heavy/light divergence: heavy gates on **global** `min_p_win_bracket` 0.20 (`heavy_sweep.py:2200-2202`) vs light's per-asset-class (crypto 0.50); heavy has **no ev_floor gate**, no soft band → heavy is *more lenient* than light.
- Enforcement-vs-gates ordering is now **harmonized** (both enforce before gates; `heavy_sweep.py:1464` "Batch D 4.1: enforce BEFORE gates (parity with light sweep)") — the audit's C3 defect is fixed.
- Publish suppressors (never flip `qualified`): cooldown 15min (`:4992-5009`), content dedup (`:5281-5338`), `bracket_unscored` fail-closed (`:5362-5371`), bracket REJECT_LOW **inert** (needs `p_win_bracket_mode=live`, it's shadow, yaml:906), light geometry gate (shadow, `:5537-5564`).

### 1.4 Live vs dead in the kairos family

- **kairos_rank.py** — *partially live, mislabeled*: stamps display-only ("Never gates the signal", `orchestrator.py:2734-2740`; migration 172 "never read"), **but** `rank_score` live-modulates the calibration shrink (`rank_score_calibration_mode: live`, yaml:998; `orchestrator.py:4551` → `calibration_enforcement.py:507-543`). Undocumented live behavior, light-path only.
- **strategy_router.py** — shadow stamp only.
- **kairos_quant.py** (181 LOC), **kairos_pivot.py** (326 LOC) — research-only dead weight ("never a live gate").
- **kairos.py** — stamps/run-tracker/calendar/drift backfill; "Nothing here changes qualification, sizing, SL/TP or delivery."

### 1.5 Top defects (Determination 1)

1. **Heavy-path enforcement clobber unguarded** — `heavy_sweep.py:1526-1536` (see §1.2). P0.
2. **Heavy/light gate-set divergence** — no EV hard floor on heavy; global vs per-class bracket floor; MIN_KELLY asymmetry. P0.
3. **Bracket-path p_win soft band passes near-zero probabilities** — hard floor 0.0 (`orchestrator.py:4845`); the whole reachability discipline rests on `ev_floor_ok` alone. P1.
4. **kairos_rank live-modulates enforcement while documented display-only** — `orchestrator.py:4551` vs migration 172; asymmetric across paths. P1 (document or gate).
5. **Bias loop category mismatch + stale docs** — `calibration_bias_bucket.py:28` claims "Nothing downstream reads either table yet" while `:297-328`'s cell table is the live R3 enforcement input (`calibration_enforcement.py:602-627`); yaml:927 comment says unscored "fails open" while policy is `fail_closed` (`:919`).

---

## 2. Determination 2 — entry/TP/SL correctness (reachability & geometry bands)

### 2.1 G1–G4 status: ALL DONE at HEAD (docs stale on G1)

| Stage | Status | First-hand evidence |
|---|---|---|
| **G1** one config authority | **DONE** (the redesign doc's "not done" is stale) | `by_class` removed from YAML (`feature_flags.yaml:631-637`) **and** `_DEFAULTS` (`feature_flags.py:120-127`); pooled pick now clamped to the inheriting symbol's band (`dynamic_tier_optimizer.py:175-218` — `cfg=get_dynamic_atr_config(asset_class, symbol)`); store boundary guarded by `_pick_in_band` (`tiered_atr.py:778-809`, applied `:868-886`). `by_symbol.FOREX` is the widened replay band `tp [4.27,4.52,4.77] / sl [2.63,2.75,2.87]` (yaml:563-570). No `geometry_params()` function exists, but authority is functionally single (`get_dynamic_atr_config` + caller convention + `tests/test_geometry_config_single_source.py`). |
| **G2** solve never returns None | **DONE** | `select_tp_sl` `allow_best_effort=True` default (`tiered_atr.py:479`), best-effort in-band bracket tracked (`:530-534`) and returned when nothing clears the strict both-halves bar (`:551-558`), clamp re-check (`:539-548`); every former `return None` in `dynamic_atr_pick` replaced with `_band_center_pick(cfg)` (`:894, :902, :938, :1002-1003, :1036, :1082`); `_band_center_pick` = mid-band, n=0 (`:758-775`). |
| **G3** null → recompute | **DONE** | `_process_symbol` calls `dynamic_atr_pick(...)` store-hit-or-recompute (`orchestrator.py:4174-4187`); `dynamic_atr_light_mode: "live"` in YAML (`yaml:525`) **and** defaults (`feature_flags.py:220`). Residual (documented): brick TP/SL derivation still in `_compute_trade_signal` as pre-apply value; brick ships only via mode-off / `atr≤0` / **any exception** (`:4198-4199`, DEBUG-only log) / unconfigured band. |
| **G4** one bucket/reachability/EV | **DONE in effect** | Bucket, `p_win_bracket`, EV, sim stamp all read the **shipped** bracket (lookup after ATR apply, `orchestrator.py:4220-4226`); resolver basis intrabar (`yaml:287`). |

Live validation (repo's own, `worklog/20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`): 4 consecutive sweeps, **11/11 ATR geometry, zero brick**. The tight-brick era (1.5-ATR TP → 1-2 bucket → 93.1% SL) is over. `min_signals` is now **60** (yaml:559, not 80) and is no longer the binding constraint.

### 2.2 But "correct" is only weakly true — the evidence problem

1. **68/77 applied geometries are the operator's zero-evidence band centre** (n=0, exp=0). The consistency + walk-forward gates run **after** `select_tp_sl` picks a winner, and on failure **substitute `_band_center_pick` instead of moving to the next candidate** (`tiered_atr.py:1003-1095`) — violating the redesign's own §3.2 contract ("rejected candidate → next candidate, never nulls the result"). The binding constraint moved from path-count starvation to the consistency (n=29) + walk-forward (n=50) filters. The "dynamic" solve is mostly inert.
2. **Evidence asymmetry at publish**: an **unscored** bucket fails closed (suppress, `orchestrator.py:5362-5371`, policy `fail_closed` yaml:919) — but a **scored-dead** bucket (measured 6.9% TP) can still publish because the scored-low rejection needs `p_win_bracket_mode=live` and it is **shadow** (yaml:906; suppress path `:5380-5388`). Missing evidence blocks; bad evidence doesn't.
3. **Pooled picks bypass the consistency/walk-forward gates** (guards on `symbol` at `tiered_atr.py:1007`, `:1055`), and the live EV bracket lookup uses the pooled `signal="eq.all"` row (`p_win_bracket.py:196-206`) — migration 159's per-side reachability rows are measured but **never priced** (`p_win_bracket.py:199-204`; `bracket_reachability.py:357`).
4. **Entry is the uncosted last hourly close** — `orchestrator.py:4037` (`current_price = float(np_prices[-1])`, 200-bar hourly TradingView fetch at `:3406-3408`); no bid/ask/spread/slippage anywhere in the signal build (only an informational `spread_bps` stamp, `:2820-2823`); the only cost model is `cost_atr: 0.1` inside the picker walk.
5. **Close-only ATR** — `compute_atr` = mean |close[i]−close[i−1]| over 14 bars (`heavy_sweep.py:202-221`) understates true range (no high/low) → every band grid and bucket is denominated in a depressed volatility unit. Pick/apply/settle bases agree (all use the same close-only ATR; the picker's *walk* uses intrabar high/low envelopes, `tiered_atr.py:97-98`, `:152-179`), but ATR normalization is a vol-understating approximation.

### 2.3 Reachability machinery census (live vs shadow vs dead)

| Module | Status | Feeds publish? |
|---|---|---|
| `bracket_reachability.py` | **LIVE** (weekly job `orchestrator.py:1656-1664`; post-EOD `:3164`; band-urgency per sweep `:3671`) | indirectly — the table `p_win_bracket` reads |
| `p_win_bracket.py` | **LIVE in EV** (`p_win_in_ev_mode: live`, yaml:932) | **YES** — substitution into EV/Kelly + fail-closed unscored |
| `geometry_band_urgency.py` | **LIVE** (`enabled: true`, yaml:964-988) | keeps the table fresh (drift loop, migration 201) |
| `geometry_risk.py` | LIVE stamps + live intraday brick scale (yaml:723); light gate **shadow** (yaml:733) | heavy publish geometry gate LIVE (yaml:718, `heavy_sweep.py:1568-1582`) |
| `heavy_sweep_reachability.py` | **shadow** (yaml:464) | no — comparison rows only |
| `geometry_incubation.py` (665) | **shadow** (yaml:1021) | no |
| `reachability_prob.py` (273) | **OFF** (`p_win_reachability_mode` absent from yaml → default off, `feature_flags.py:332`) | no — stamp wiring (`orchestrator.py:5725-5766`) inert |
| `reachability_decay.py` (304) | **dead weight** — no importer, CLI-only | no |

### 2.4 Single most impactful geometry fix

**Move the consistency + walk-forward checks inside the `select_tp_sl` candidate loop** so the solve returns the best bracket that survives *all five* filters (rejected candidate → next candidate, per the redesign's own §3.2), instead of substituting a zero-evidence band centre after the fact. This converts the 68/77 evidence-free geometries into walked, filtered picks. Runner-up: flip `p_win_bracket_mode` to `live` (after the replay gate passes) so measured-dead brackets stop publishing.

---

## 3. Determination 3 — calibration accuracy & next-signal prediction

### 3.1 The repo's own measured evidence: p_win is NOT accurately forecasted

| Instrument (repo's own) | Result |
|---|---|
| Brier/Murphy decomposition, 45d, n=74,376, 14 symbols (`reports/20261004_masaniello_brier_decomp.md:1414-1418`) | **BSS −0.1531, AUC 0.4992, RES 0.0002** — "no better than a constant; no ordering" |
| `p_win` → bracket label (AAPL, same report :36-37) | **BSS −0.5084, AUC 0.4513** — below chance |
| `20261002_SIGNAL_QUALITY_PLAN.md:12,59-60,126` | bracket AUC **0.4869**, REL 0.0616 vs RES 0.0008; **AUC ≤ 0.5 under every label** (close-walk/intrabar/survivable) |
| Era-clean per-symbol (`20261011_CALIBRATION_REACHABILITY_REFRAME.md:16-33,74-77`) | published p_win **0.44–0.71** vs realized WR **2.6%–23.3%**; bias **+0.207…+0.627**; "No symbol is calibrated or underconfident"; **109/111 cells OVERCONFIDENT** |
| Two-week retrospective (`docs/20260915_TWO_WEEK_RETROSPECTIVE…:51,64,69`) | bias **+0.269 → +0.341 (worse)** while the mechanism got "healthier" |
| `orchestrator.py:4339-4343` | repo's own comment: sizing on a below-chance quantity (AUC 0.4869) "is worse than not sizing on it" |

The shrink-toward-0.5 enforcement cannot close a +0.6 gap (repo's own words).

### 3.2 Which p_win qualifies/publishes at HEAD — hybrid, bracket-first

- Live blend = **p_regime + p_timesfm only** (Markov T^N withdrawn: `p_markov_hold_n_mode: "off"`, yaml:236, `orchestrator.py:4265,4325-4332`; `p_historical` renormalized out — `ev_engine.py:109-118`); weights renormalize on missing sources (`ev_engine.py:686-709`); p_regime capped 0.97 (`:453,495`); logit clamp 0.01 (`:72-84`); p_timesfm = clamp(sigmoid(k·rel_change)) (`timesfm_k_adaptive.py:48-66`), inverted for shorts (`ev_engine.py:681-684`).
- `p_win_in_ev_mode: "live"` (yaml:932) → `apply_bracket_to_ev` **substitutes** (no blend) the bracket probability into EV/Kelly when scored (`orchestrator.py:4479-4516`; `p_win_bracket.py:291-323`); `ev_allowed` reads bracket EV; EV hard floor 0.0; unscored → fail-closed suppression.
- Bracket source = `"all_in"` (raw `pct_hit_tp`, `p_win_bracket.py:146-151`); era-clean at build (`bracket_reachability.py:223-225`); n floor `min_n_stored=3` → unscored (not 0.5); Beta(2,2) shrink on the derived path only.
- p_win gate: bracket vs per-asset-class min when scored (hard floor 0.0); directional vs MIN_P_WIN 0.55 / hard floor 0.50 (`orchestrator.py:4834-4849`, yaml:44).

### 3.3 Nothing predicts the next signal (live)

- **Markov T^N**: withdrawn from blend, yet **still fits every tick and ships the matrix to the agent payload** (`orchestrator.py:3969,5242`) — dead compute on a measured-negative-skill component (legacy HMM directional accuracy 0.5251 < "always DOWN" 0.5655; mean skill −0.0622, `20261001_HMM_FIRST_PASSAGE…:31-42`).
- **First-passage**: killed in Phase 2 — skill −0.0691 vs base-rate control 0.7041; D5 overlay actively harmful (Δ −0.0265) (`20261001_PHASE2_FIRST_PASSAGE_RESULT.md:12-21`).
- **TimesFM**: in the live blend but no measured positive skill on the bracket event (aggregate AUC 0.4992 includes it); k-adaptive shadow (yaml:442).
- **strategy_router R2**: UNDECIDABLE — 4 routed episodes vs bar 200; declines 99.6%.
- **cell_conditioning / common_factor / reachability_decay**: offline instruments — out-of-fold cell AUC 0.4994 (in-sample 0.7256 = memorization), BSS all negative (`reports/20261002_cell_conditioning.md:9-36`).
- **B1 MR cohort walkforwards (2026-10-12)**: all FAIL/NO_DATA (XAUUSD ratio 3.84× but n=34<40; commodities FAIL; crypto inverse 0.44×; forex 0 rows).
- **Closest to live forward-looking conditioning**: `rank_score` (freshness/type, IC 0.126–0.199, ΔD +0.233) — but post-ATR n=43, CI spans 0, and its only live use is modulating the calibration shrink.

### 3.4 The calibration loop itself still measures the wrong population

1. **Survivorship (audit C2) — STILL OPEN, verified**: expired signals intentionally excluded from `nt_calibration_records` (`eod_resolver.py:717-727` "Expired signals are intentionally NOT written here") and from bias-by-bucket (`calibration_bias_bucket.py:138` — `outcome in.(tp_hit,sl_hit)`). Calibration scores P(win | resolved) while the era-clean book loses on the all-in population (76.6% SL / 10.9% expired). Raw p_win capture IS fixed (C1: migration 114, `orchestrator.py:4472`, `eod_resolver.py:743-769`).
2. **Era-filter leak (NEW finding)**: the default bias read is `v_calibration_records_published` (migration 209:20-38) which has **no cutover predicate**; `_calibration_read_table` returns it whenever `calibration_bias_published_only=True` (default) with cutover ON (`supabase_client.py:2388-2393`; window bounded only by 7-day `entered_at` `:2478-2480`). Pre-intrabar rows can re-enter the live bias loop — breaking migration 192's "one era" invariant.
3. **Bracket probability not side-conditioned**: EV lookup queries `signal=eq.all` (`p_win_bracket.py:199-204`) although buy/sell rows exist (migration 159) — the R3 enforcement cell is side-aware but the EV input is not.
4. **Brier/reliability measured, never consumed**: `calibration_brier` written per record (`eod_resolver.py:770`) but read by no control path; `calibration_decomp.py` is offline-only; the ±0.10 bias band + `min_episodes: 2` still stamps +0.23 "CALIBRATED" — L1's recommended 0.05/10-episode bars not enforced.
5. **Enforcement clobber fix**: PRESENT on light (§1.2); heavy unguarded.

### 3.5 Edge: the repo's own walk-forward evidence

**Nothing has demonstrated walk-forward edge.** Era-clean book: WR 12.5%, expectancy_R **−0.58**. Every constructed candidate fails its own pre-registered bar; AUC ≤ 0.5 under every outcome label. The repo's own conclusion: *"the base rate beating every constructed candidate is itself the finding"* — the best "model" is a shrunk per-symbol base rate with zero fitting. The 2026-10-12 fixes make the *pricing and suppression* honest, but no predictive component of the stack has ever beaten chance out-of-fold.

---

## 4. The dead-weight audit ("Claude has added unnecessary weight")

### 4.1 Weight numbers

| Metric | Value |
|---|---|
| Total Python LOC | **133,615** |
| `regime_platform/` | 67,112 LOC / 176 files |
| `sweep_orchestrator/` | 34,368 LOC / 66 files |
| `orchestrator.py` | 6,212 lines; `_process_symbol` alone = **2,308 lines (37%)**; ≥25 distinct concerns; `start()` wires 18+ scheduled jobs |
| `renko/` | 8,168 LOC (LIVE — heavy-sweep grid search `heavy_sweep.py:1929-1930`) |
| `scripts/` | 23,751 LOC / 92 files |
| `tests/` | 30,345 LOC / 147 files (load-bearing paths covered: geometry_harmony, ev_not_clobbered, geometry_always_returns, atr_single_source, ev_allowed_reads_bracket, ev_hard_floor_gate) |
| Feature flags | **99 top-level keys** in 1,367-line yaml + **130 getters** in 2,051-line `feature_flags.py`; 17 shadow, 3 off, 1 inert-but-"live", 4 dead getters |
| Process weight | worklog/ 243 files (3.2 MB); docs/ 62 files; 148 SQL migrations; 4 launchers |

### 4.2 Off-path share

- **~30% of `sweep_orchestrator` (~10,231 LOC) is off-path**: ~15% research code embedded in the prod package + ~15% shadow-stamp blocks.
- **Research cluster inside prod package (3,465 LOC)**: `fp_eval` 756 + `calibration_quality_gate` 436 + `leverage_eval` 460 + `cell_conditioning` 436 + `common_factor` 314 + `reachability_decay` 304 + `first_passage` 269 + `calibration_decomp` 274 + `calibration_reframe` 216 — imported only by tests + one-off `scripts/kairos_*.py`.
- **Per-signal dead writes**: `meta_regime_model` + `signal_stream_cache` (599 LOC) — `signal_stream_meta_model_mode: off` (yaml:188), yet `record_tick_and_update_shadow` fires per non-neutral signal (`orchestrator.py:5865-5876`) → per-tick SQLite I/O with zero consumers.
- **orchestrator.py: 0% dead functions** — every def has a call site. The bloat is *structural* (monolith + shadow blocks), not dead code.

### 4.3 Ranked cut list

1. `main_v2.py` — imports nonexistent `regime_platform_v2`. Delete.
2. `main_v1.py` — never launched (render.yaml/start.sh/background.*/on_start.sh all use `main.py`). Delete.
3. Kairos research cluster (3,465 LOC, §4.2) — move to `scripts/` or delete.
4. `meta_regime_model` + `signal_stream_cache` (599) — stop the per-signal off-mode writes.
5. `portfolio_compare` (347, tests-only) + `paper_retroclose` (119, zero importers).
6. `reachability_prob` (273) — runtime-dead (flag off).
7. `renko/integration.py` (283) — zero importers.
8. Legacy HMM/`hmm_shadow_accuracy` branches — `markov_source_mode` retired to `legacy_hmm` but "deliberately LEFT in the tree" (yaml:208); stop per-tick Markov compute + payload shipping (`orchestrator.py:3969,5242`).
9. `rank_score_calibration_mode` flag — yaml "live" vs `_DEFAULTS` "shadow" (`feature_flags.py:270`), getter never called; slope applied unconditionally (`calibration_enforcement.py:355`). Reconcile. Plus dead getters (`get_dynamic_deadline_config_for_symbol`, `get_dynamic_deadline_reopt_config`, `get_macro_config`).
10. 3 redundant launchers; stale `docs/ACTIVE_MODES.md` (claims dynamic_atr & p_win_in_ev are shadow — both live at HEAD).

### 4.4 Confirmed vs refuted

**CONFIRMED:** orchestrator monolith; research-in-prod (~5,023 LOC = 15% of package); shadow-stamp sprawl (~5,208 LOC, 17 shadow flags); flag sprawl incl. one inert "live" flag; entry-point confusion (8 root entry files, 2 dead); heavy/light qualification divergence (EV floor absent on heavy).

**REFUTED (do not cut):** `renko/` is load-bearing; `_compute_trade_signal` is live (`orchestrator.py:4145`, `heavy_sweep.py:1408`); `drift_monitor` is live (`orchestrator.py:4813`); `server.py` (TimesFM :8002) vs `llm_server.py` (local LLM :8004) are different services, both launched; the FOREX double-definition was fixed 2026-10-12; `strategy_registry.yaml` IS consumed (`_load_registry`, router mode=shadow).

---

## 5. Priority triage

### P0 — correctness, small diffs, do now

1. **Port the `_bracket_applied_to_ev` guard to the heavy path** — `heavy_sweep.py:1506-1536`: when bracket EV was applied, keep p_win/ev terminal (Kelly-only adjustment), and preserve `p_expire` in any recompute. ~10 lines. Add a **wiring** test that imports the heavy path (the existing test pins arithmetic only).
2. **Add the EV hard-floor gate to heavy qualification** (light/heavy parity; heavy currently has no `ev_floor_ok`).
3. **Fix the default calibration read to preserve the era filter** — add the cutover predicate to `v_calibration_records_published` (or default `calibration_bias_published_only` off until fixed) so pre-intrabar rows cannot re-enter the live bias loop.

### P1 — evidence honesty

4. **Move consistency + walk-forward checks inside the `select_tp_sl` candidate loop** (rejected candidate → next candidate; never post-hoc band-centre substitution). Converts the 68/77 zero-evidence geometries into walked picks. Highest-leverage geometry fix.
5. **Promote `p_win_bracket_mode` to `live`** after `scripts/replay_p_win_bracket_gate.py` passes — measured-dead brackets must not publish while unscored ones are suppressed (close the evidence asymmetry).
6. **Feed expired into calibration measurement** — `P(win) = P(resolve) × P(win|resolve)`; include expired in the bias population (or dual-book it) so the loop sees the 76.6% SL / 10.9% expired reality.
7. **Side-condition the bracket EV lookup** (`signal=eq.all` → per-side; migration 159's rows already exist) and stop pooled picks bypassing the consistency/walk-forward gates (`tiered_atr.py:1007,1055`).
8. Document or gate the live `rank_score` → enforcement modulation (flag says live, defaults say shadow, migration says never-read).

### P2 — weight cut (the "Claude diet")

9. Execute the §4.3 cut list (dead entry points, research cluster relocation, off-mode per-signal writes, per-tick Markov compute, launcher consolidation, flag reconciliation).
10. Extract from the monolith: TimesFM block (~370 lines) → `timesfm_client.py`; `_build_signal_sim_payload` (`:973-1266`) → payload module; job wiring + subsystem runners (~650 lines) → `jobs.py`; `_process_symbol` stages → a pipeline module.
11. Reconcile stale docs (`ACTIVE_MODES.md`, the redesign doc's G1 status, yaml:927 fail-open comment, `calibration_bias_bucket.py:28` docstring).

---

## 6. What this review did NOT do (stated plainly)

- **No code was changed.** This is diagnosis + priority plan only.
- No live-data queries were run (all evidence is code at HEAD + the repo's own measured reports/worklogs).
- The P1 promotions (esp. `p_win_bracket_mode: live` and expired-in-calibration) change live behaviour and must go through the repo's own replay/walk-forward acceptance bars first.
- Nothing here manufactures edge. The honest near-term state: an honest pricer refusing to publish a no-edge book. The strategy layer (archetype router) remains the unpromoted lever — and its own R2 measurement is currently undecidable at 4/200 episodes.

---

## 7. Files / lineage

- Verified first-hand: `orchestrator.py:4569-4578` (light guard), `heavy_sweep.py:1478-1536` (heavy clobber), `orchestrator.py:4834-4849` (bracket p_win floors), `p_win_bracket.py:196-206` (`signal=eq.all` lookup), `tiered_atr.py:1003-1095` (post-select gates → band-centre), `calibration_bias_bucket.py:138` (expired excluded), `feature_flags.yaml:906/919/932` (gate modes).
- Supporting worklogs consumed: `20261012_REACHABILITY_GEOMETRY_FAILURE_DIAGNOSIS.md`, `20261012_ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md`, `20261012_GEOMETRY_HARMONY.md`, `20261012_GEOMETRY_G2_G3_AND_FOREX_BAND_REPLAY.md`, `20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`, `docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md`, `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`.
- Evidence tables: `worklog/kairos/reports/` (Brier decomp, cell conditioning, first-passage, R2 router, B1 MR walkforwards), `nt_signal_sim` / `nt_bracket_reachability` / `nt_dynamic_tier_state` (via the repo's own documented measurements).

> **Restoration note (2026-10-13, later session):** this file was originally written into a working clone at `worklog/` that was lost to an environment reset; content unchanged. Commit to the repo's `worklog/` directory as `20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md`.
