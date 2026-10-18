# Geometry mechanism & formulation review — is dynamic-ATR + reopt + reachability correct, why params are off, and where LLM components can supplement

**Date:** 2026-10-13
**Type:** Methodology review + architecture scope (no code change). Companion to `20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md` (the wiring triage); this one reviews the *mechanism design* and maps LLM surfaces onto the defects.
**Trigger:** Operator questions — *"is the geometry & dynamic tiers+reopt (reachability) the correct mechanisms and formulated correctly? Any recommendations for why the params are so off resulting in poor winrate?"* and *"can any llm components supplement any of these issues?"*
**Status:** Review complete. Six formulation defects, one arithmetic crux, six recommendations, LLM supplement map with the repo's own evidence bar applied.
**Roadmap:** feeds `calibration_reachability_reframe` + `geometry harmony` follow-ups + the strategy-layer (router) track.

---

## 0. Plain terms — what this worklog answers

1. **Is the mechanism family right?** Yes — vol-normalized geometry, realized-outcome reachability measurement, bracket-priced EV. The architecture is sound.
2. **Is it formulated correctly?** No — six specific defects, below. The best-formulated piece is the envelope walk; the worst is the objective it optimizes.
3. **Why are the params so off (poor winrate)?** Not tuning. Three stacked causes: hand-set priors, a learning loop that cannot run at current evidence volume, and — the crux — **entry timing below the random-walk null**, which no TP/SL choice can overcome.
4. **Can LLM components supplement?** Yes, in four places — but never where the repo has already paid twice for the lesson (direction-from-price). Map in §5.

---

## 1. Verdict on the mechanism

**Family: CORRECT.** The three-layer design —
`solve geometry in ATR units → ship it → measure realized outcomes per (symbol, vol_tier, bucket) → price the next signal from the measurement`
— is the right architecture, and it is now actually wired end-to-end at HEAD (G1–G4 done; see the companion triage §2). The envelope walk (`walk_envelopes`, `tiered_atr.py:97-179`, intrabar, `sl_first`) is the best-formulated component in the chain: it sees the wicks a close-path walk cannot, and its basis agrees with the intrabar resolver.

**Formulation: SIX DEFECTS.**

### D1 — The objective is unconditional over a mixed window
`select_tp_sl` (`tiered_atr.py:464-558`) maximizes `mean(pnl − cost_atr)` over **all** historical light-path entries, with `e1>0 ∧ e2>0` (both halves) as the only robustness check. The era-clean evidence says the edge is violently conditional — WR spans 2.6%→23.3% by symbol; the ID45 gold-buy inversion is 38.2% vs 6.5% for the same symbol/direction under different context. **An unconditional mean over a mixed population cannot find a conditional edge — it averages it away.** Meanwhile the *pricing* side conditions on `(symbol, vol_tier, bucket)`. Selection and pricing see different distributions; that discrepancy is why a consistency gate is needed at all. A correctly formulated system conditions both stages on the same key and the gate becomes a tautology.

### D2 — The "solve" is a 3×3 grid argmax over operator-set points
`tp_grid × sl_grid` from the config bands = 9 candidates (`tiered_atr.py:493-494`). The band *values* were hand-set from one-day replays (`20261012_GEOMETRY_G2_G3_AND_FOREX_BAND_REPLAY.md`; n≈34 for XAUUSD). The params are priors with a search light shone on them, not learned quantities; the optimum (if any exists) can sit between grid points the solve never visits.

### D3 — The expiry cap contradicts the band widening
`max_expiry: 0.20` (`feature_flags.yaml:557`) rejects any candidate whose walk expires >20% — but a 4.5-ATR TP *necessarily* expires more than a 1.5-ATR TP (measured: the 4+ bucket runs 26% expired; the 1-2 bucket ~7%). **The widened bands can never clear the walk gate.** The widening reaches the book only via `_band_center_pick` — i.e., as an unvalidated prior, not a solved pick. The operator widened the bands while the solver's expiry cap still vetoes everything the widening implies. Expiry is already priced in the 3-outcome EV via `p_expire`; a hard walk-side reject double-counts it.

### D4 — The gate stack demands certainty that 20-path folds cannot contain
`min_fold_n: 20`, `min_oos: 0.0` (`yaml:533-536`) — OOS expectancy merely *positive*. A fold of 20 paths estimating a p≈0.15 quantity has a CI of roughly ±25pp; "OOS > 0" is a coin flip even when true expectancy is exactly zero. Stacked with the consistency gate (tol 0.20, n≥30, `tiered_atr.py:629-660`), the filters reject nearly every walked pick → **68/77 shipped geometries are the zero-evidence band centre** (`20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`). Each gate is individually defensible; jointly they require more evidence than the book generates, and the failure mode is a silent revert to the operator prior. The system is overconfident about its *params* in exactly the way it diagnoses overconfidence in `p_win`.

### D5 — Wrong arbitration when walk and measurement disagree
`check_pick_reachability_consistency` (`tiered_atr.py:634-660`) rejects the walked pick → band-center substitutes. But the measured bucket is the *realized ground truth* and the walk is the *model*. When model and measurement disagree, the correct move is to condition/shrink the model toward the measurement — not discard both and substitute a third quantity (static mid-band) that satisfies neither. This is a two-stage design (select-by-walk, price-by-table) where a single stage should be.

### D6 — Denominator and cost errors
- Close-only ATR: `compute_atr` = mean |close[i]−close[i−1]| over 14 bars (`heavy_sweep.py:202-221`) understates true range (no high/low) by ~20-40%. Every "4.5 ATR" is really ~3 true-range ATRs. Internally consistent (pick/apply/settle share the unit) but it corrupts external comparability and any literature-calibrated prior.
- Flat `cost_atr: 0.1` across all symbols (`yaml:558`) — XAUUSD spread/ATR and EURUSD's differ by roughly an order of magnitude. A flat cost term overprices one book and underprices the other.
- Population drift: the reachability table's history is dominated by the **old entry process** (brick-timed entries at Renko reversals). The new process enters at arbitrary hourly closes — bucket rates partially measure a selection effect that no longer exists. (`nt_bracket_reachability` era-clean flag mitigates but does not separate entry regimes.)

---

## 2. Why the params are "so off" — the arithmetic nobody escapes

**Breakeven vs measured win rates:**

| Geometry | RR | Breakeven p* | Best measured bucket |
|---|---|---|---|
| Widened forex band (tp 4.52 / sl 2.75) | 1.64 | **37.9%** | 2-4 bucket: **14.8%** TP |
| XAGUSD best symbol WR | needs RR > 3.3 | 23.2% measured | only tight-SL artifacts "achieve" this in-sample |
| Random-entry null (zero drift), first-touch | — | p ≈ SL/(TP+SL) ≈ **37.5%** at the widened band | measured 13-15% |

The last row is the crux: **measured TP rates sit below the random-walk null for the same bracket.** That is not a geometry problem — it means entries are systematically *adverse-timed*: the HMM label flips to bull after the move has matured, the entry buys into exhaustion, and mean reversion carries price to the stop. The repo's own ID45 finding (fading gold buys wins 38.2% vs following them 6.5%) is this disease diagnosed from the other side.

**Causal chain for "params so off":**
1. Band params were hand-set priors from tiny replays — never learned (D2).
2. The learning loop cannot run: 60-path floor + gate stack vs ~2-3 resolvable paths/symbol/day → 88% of shipped geometry is band-center (D4).
3. Even if it ran, the unconditional objective averages away the only conditional edge that exists (D1).
4. Entry timing sits below the random null, so **no TP/SL pair within these bands reaches breakeven**. Geometry reshapes the loss distribution; it cannot manufacture a win rate.

**Corollary (honest):** a correctly-formulated geometry engine with the current entry process outputs **no-trade for most cells**. The poor winrate is the entry layer's, worn as the geometry layer's.

---

## 3. Recommendations (ranked by leverage)

- **R1 — Fix entry timing (the actual WR lever).** Condition entries on regime age (the EDGE_STATE_MATRIX lineage), add a deviation-Z gate (do not enter when extended), or enter on pullback/brick-reversal rather than label-flip. The ID45 counter-trend cohort is a ready-made mean-reversion archetype — the router that would route to it declines 99.6% and is shadow (`20261012_r2_router_measure.md`). Promote the router or hand-wire the one proven inversion cell.
- **R2 — Replace grid-argmax + table-lookup with an analytic prior + hierarchical update.** Model price as a diffusion with the symbol's current vol; the double-barrier first-passage gives `p(TP)`, `p(SL)`, `p(expire)` for **any** geometry in closed form — no 60-path floor. Update with the reachability table as a Beta/Dirichlet likelihood: the table sharpens the prior as n accrues; the walk shrinks toward the measurement when they disagree (fixes D5 by construction). Search continuous geometry in-band maximizing priced 3-outcome EV × a measured drift multiplier (currently ≈1, honestly). This kills evidence starvation *structurally* and prices cold geometries without freezing them at band-center. Note: the repo's first-passage attempt failed because it used HMM transition matrices as the drift model — the barrier math was not the problem; the drift input was.
- **R3 — Unify the conditioning key** `(symbol, vol_tier, side, bucket)` across solve, price, and table (use migration 159's per-side rows; stop pricing off `signal=eq.all`, `p_win_bracket.py:199-204`).
- **R4 — Evidence-triggered reopt, not calendar.** Re-solve when ≥k new resolved paths accumulate; at current volume monthly reopt is churn without information. Size walk-forward bars to what n=20 folds can certify (pooled class-level with shrinkage, not sign tests).
- **R5 — Mechanical:** true-range ATR (OHLC already fetched for the envelopes — use it); per-symbol `cost_atr = spread_bps/ATR`; resolve the `max_expiry` vs widened-band contradiction (D3) by pricing expiry rather than rejecting it.
- **R6 — Accept what a correct mechanism honestly outputs.** With directional skill at AUC 0.499 and positive costs, the correct answer is sparse trading. The current machinery reaches that conclusion expensively (10 gates, ~3k LOC of geometry/reachability modules) and noisily (band-center when starved).

---

## 4. LLM components in the repo today (census + measured evidence)

| Surface | LOC | Mode at HEAD | Measured evidence |
|---|---|---|---|
| `clef_flash.py` (Cloudflare `@cf/clef-flash`) | 486 | `clef_flash_mode: shadow` (yaml:1356) | `p_clef_dir` AUC **0.58 = chance** (retire as direction gate); `p_clef_wait` AUC **0.61 for sl_hit** — the one measured-positive LLM output in the platform |
| `clef_stance.py` (hourly cross-asset stance) | 121 | shadow stamps (migration 207) | not yet evaluated |
| `macro_forecast_agent.py` (12m forecast classify) | 472 | writes `nt_macro_forecast` daily — **no reader** (LLM audit §0) | diagnostic only |
| `kairos_pivot.py` (event/pivot labeling) | 326 | research overlay, "never a live gate" | P2.3 lead-lag **undecidable** — 2 unique events, 0 historical labels (`20260928_p23_narrative_leadlag.md`) |
| `triage.py` (self-learning triage) | 277 | writes findings — **no reader** | 54/54 triaged, marginal value (LLM audit) |
| `llm_client.py` + `llm_server.py` (chat path) | 516 | serving pivot/macro/triage | fragile: `extract_json()` parse failures, free-model retirement incidents |
| BGE-small embeddings (Worker) | — | deployed, `knowledge_index.py` shadow | retrieval quality **never verified** |

The standing rule (LLM audit §1, earned twice): **no LLM derives direction/trend/regime from price data.** Legacy HMM 0.5251 vs 0.5655 naive baseline; first-passage −0.0691 vs 0.7041 base rate; `p_clef_dir` 0.58. An LLM re-reading the same series is a strictly worse copy of a sub-chance signal — non-deterministic, non-reproducible, slower, costlier, uncalibrated.

---

## 5. The LLM supplement map — defect-by-defect

### 5.1 Where LLMs CANNOT supplement (deterministic engineering — no LLM, fix the code)

| Issue | Why not LLM |
|---|---|
| D2/D4 grid + gate starvation, R2 analytic prior | Closed-form barrier math + Bayesian update; an LLM would inject unquantifiable error into the one place the platform finally has honest arithmetic |
| D3 expiry-cap contradiction | Config edit |
| D6 close-only ATR, flat cost | Use the OHLC already fetched; per-symbol spread |
| P0 wiring bugs (heavy-path clobber, era-filter leak) | One-line guards; LLMs do not run in the tick path |
| Calibration survivorship (expired excluded) | Include the population; measurement design |

### 5.2 Where LLMs CAN supplement (with the evidence bar)

**S1 — `p_clef_wait` as a publish suppressor (measured, promotable now).** AUC 0.61 for sl_hit is the only above-chance LLM number the platform has. Map: it targets the *publish* side of D4/D5 — when the shipped geometry is a zero-evidence band-center pick (68/77 today) or the bucket is scored-low-but-shadow, `p_clef_wait` high → suppress. This complements (not replaces) the deterministic fixes: `p_win_bracket_mode: live` (P1-5 in the companion triage) handles scored-low; `p_clef_wait` adds a measured veto where evidence is *absent*. Promotion bar: ≥200 shadow episodes, era-clean AUC ≥ current 0.61 sustained, fail-open NULL on any Worker failure (the P_CLEF_SHADOW_STAMP contract). **It stays a suppressor — never an EV source, never direction.**

**S2 — Role A archetype-routing classifier (the router's judgment input).** The deterministic router declines 99.6% because macro/trend labels are neutral/missing; an LLM fed *interaction indicators* (regime×deviation, macro×trend agreement, vol×bracket — the wider-lens §5 table) can produce the setup-type classification `{mean_reversion, momentum, range_fade, breakout, stand_by}` the router lacks. Map: this is the strategy-layer lever for R1 — the entry-timing fix. Bar: shadow archetype-stamp measured against the deterministic router's choice first (`20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` R4); promote only on measured discrimination. The `p_clef_dir`-was-chance lesson applies in full.

**S3 — Macro narrative → conditioning dimension (R3 key extension).** The LLM audit's §1.2 finding: the one *genuinely orthogonal* LLM capability is text-vs-tape agreement — does the strategic narrative (`nt_macro_forecast.thesis/drivers`, 33 symbols daily, currently unread) agree with the realized regime shift? Map: extends D1's conditioning key from `(symbol, vol_tier, side, bucket)` toward a narrative-agreement cell — the direction the unconditional objective needs. Wire into `macro_filter.py` shadow-first (audit §2.3 disposition). Note the lead-lag prerequisite: needs *historical* labeled events before any lead-lag claim (P2.3 had 2 events, 0 historical).

**S4 — LLM agents as code/wiring auditors (no discrimination bar — does not touch trading).** The companion triage's P0 findings are exactly the class an LLM reviewer catches and tests miss: the heavy-path clobber survived because `test_ev_not_clobbered_by_enforcement.py` pins arithmetic, not wiring; stale docs (ACTIVE_MODES, G1 status, yaml:927 comment, `calibration_bias_bucket.py:28` docstring) misled the review itself. An LLM audit pass over (a) every PR diff for gate-order/mutation regressions, (b) docs-vs-HEAD reconciliation across the 243 worklogs, (c) flag yaml-vs-defaults drift (the `rank_score` live/shadow contradiction), is low-risk, immediately valuable, and the supplement this review itself demonstrates. This is an *engineering-loop* LLM use — it never runs in the tick path, so the reproducibility objection does not apply.

**S5 — Reliability swap: Clef-Flash structured logits replacing the fragile chat path.** For `kairos_pivot.py` / `macro_forecast_agent.py` / `triage.py` classification calls: structured `choice` output, no `extract_json()` layer, no OpenRouter key churn (audit §1.1). Operational value only — measurable as parse-failure rate and latency. No trading behavior changes.

### 5.3 The honest constraint (unchanged)

Every trading-adjacent LLM role (S1-S3) earns promotion only on **measured discrimination, era-clean, walk-forward** — the bar the platform already enforces and `p_clef_dir` 0.58 stands as the warning for. An LLM's plausible narrative is not evidence; the base rate beating every constructed candidate remains the finding to beat. S4/S5 carry no such bar because they do not touch the decision path.

---

## 6. What this does NOT fix (stated plainly)

- No code changed. R1-R6 are design recommendations; S1-S5 are scoped supplements, none promoted.
- R2's analytic prior does not create edge — with drift ≈ 0 it prices the book honestly toward no-trade, which is the correct output for the current entry layer.
- The LLM map does not rescue the winrate. S1 suppresses bad publishes; S2/S3 are conditional-edge *candidates* with unmeasured discrimination; the winrate lever remains R1 (entry timing).
- The P0 wiring fixes (heavy-path guard, EV-floor parity, era filter) are prerequisites — no mechanism critique matters while a heavy signal can still be repriced from −EV to +EV.

---

## 7. Build sequence (if adopted)

1. P0 wiring fixes (companion triage §5) — prerequisites.
2. R5 mechanical corrections (true-range ATR, per-symbol cost, D3 config contradiction) — small, era-clean verifiable.
3. R3 key unification + R4 evidence-triggered reopt — measurement alignment.
4. S1 `p_clef_wait` suppressor — ≥200 shadow episode bar.
5. R2 analytic prior + hierarchical update — replaces the grid-argmax/table-lookup pair; the structural fix for D1/D2/D4/D5.
6. R1 entry conditioning + S2 router classifier + S3 macro conditioning — the strategy layer, shadow-first, one lever at a time.
7. S4/S5 LLM engineering loop — independent of 1-6, can start immediately.

---

## 8. Files / lineage

- Solver internals verified first-hand: `tiered_atr.py:464-558` (`select_tp_sl` objective, grids, both-halves bar, `allow_best_effort`), `:614-660` (consistency gate, tol/min_n), `:758-775` (`_band_center_pick`), `:812-1103` (`dynamic_atr_pick` gate order), `feature_flags.yaml:525-559` (modes, `max_expiry: 0.20`, `cost_atr: 0.1`, `min_signals: 60`), `heavy_sweep.py:202-221` (close-only ATR).
- LLM surfaces: `config/feature_flags.yaml:1340-1356` (clef modes), `docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md` (the standing rule + dispositions), `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` §5 (Role A/B, AUC evidence, honesty constraint), `docs/scopes/20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md` (stamp contract), `worklog/kairos/reports/20260928_p23_narrative_leadlag.md` (undecidable lead-lag).
- Companion: `worklog/20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md` (wiring triage, P0/P1/P2), `worklog/20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md` (68/77 band-center measurement).
- Breakeven arithmetic: measured bucket table from `20261012_BUCKET_INVERSION_HARMONIZATION.md`; era-clean WR span from `20261011_CALIBRATION_REACHABILITY_REFRAME.md`; ID45 from `20261010_TIER1_CONTEXT_GATE_INVERSION.md`.

> **Restoration note (2026-10-13, later session):** this file was originally written into a working clone at `worklog/` that was lost to an environment reset; content unchanged. Commit to the repo's `worklog/` directory as `20261013_GEOMETRY_MECHANISM_FORMULATION_REVIEW_AND_LLM_SUPPLEMENT.md`.
