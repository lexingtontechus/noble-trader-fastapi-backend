# Router Edge Model (R2) — the strategy router as a measured next-candle edge predictor + the TSFM exit ladder

> **Date**: 2026-10-23 · **Repo**: built on `700f794` (origin/main; the operator
> had already pushed the sandbox's 9 commits + 7 of their own — Phase 0.5
> recalib, sql215/217 fixes, event-odds migrations, key rotation, edge-lab
> config migration) · **Scope**:
> `docs/scopes/20261023_TSFM_VS_REASONING_LLM_AND_ROUTER_EDGE.md` ·
> **Evidence base**: `kairos-review-worklogs/20261023_STRIDE_TSFM_VALIDATOR_VS_LLM_REASONING_REVIEW.md`
> (sandbox side), STRIDE arXiv 2605.08625, Brini VOLARE 2607.05291,
> Rahimikia 2511.18578, vol-backtest Phase 0 runs.

## Verdict

The operator's two asks — (1) rethink TSFM vs a reasoning LLM (agentic vs
zero-shot), (2) implement the strategy router as a next-candle edge
predictor — are answered by ONE architecture: **the router state becomes the
conditioning for a measured `p_router = P(next candle up | symbol,
archetype, direction)`**, TimesFM's p_timesfm is scored on the **identical
outcome stream** (the debate becomes `router_skill` vs `timesfm_skill` in a
report), and the reasoning LLM's seat is the categorical prior (agentic
structured-choice), never the number. Ships **shadow** — the EV blend is
byte-identical until the report's evidence promotes it.

## What was built

| Piece | File | What it does |
|---|---|---|
| Router Edge Model | `regime_platform/sweep_orchestrator/router_edge.py` (new, ~630 LOC) | Cells `(symbol, archetype, direction)` = decay-weighted Beta (2^(−age/τ), PBM convention, τ=90d default) anchored on the symbol base rate (k0=20 pseudo-counts); the per-tick loop RESOLVE (first-new-bar close-vs-close, ties skipped) → SCORE (both p_router and p_timesfm skill accumulators on the same outcome) → ROUTE + READ + STAMP; report with W-X2-style skill-vs-majority |
| Storage | param_store tier `router_edge` | `<archetype>:<direction>` cells, `_base`, `_router`/`_timesfm` skills, `_last` stamp — local-SQLite authority, existing Supabase mirror, restart-safe, **zero schema change** (the JSON-artifact-first shape vol-recalib §1.7.2 prescribed) |
| EV blend source | `ev_engine.py` | `p_router` param + `P_WIN_WEIGHTS_FULL["p_router"]=5/14` (peer of p_timesfm; shared /14 denominator because the renormalizer only sees RATIOS — a /19 entry would have made p_router 0.74× of p_timesfm, caught by tests) + short-inversion + `EVFullResult.p_router` + `_ALLOWED_WEIGHT_KEYS` |
| Orchestrator wiring | `orchestrator.py` | 5b-3c block BEFORE compute_ev: label hoist (macro/trend pre-fetch under the SAME guards as 5b1/5b2, which now REUSE via sentinels — identical fetch count), the tick, `p_router_blend` into compute_ev under `router_edge_mode=live` with weight override `router_edge_weight/14` |
| TimesFM exit ladder | `orchestrator.py` + `heavy_sweep.py` | `timesfm_blend_mode`: live (byte-identical default) / shadow (fetch+score, withheld from blend — the A/B) / **off (no fetch at all — zero network)**; wired on both sweep paths |
| Flags | `feature_flags.py` + yaml | `router_edge_mode` (shadow), `router_edge_weight` (5.0), `router_edge_k0` (20), `router_edge_tau_days` (90), `router_edge_min_n_eff` (30), `timesfm_blend_mode` (live) — full docstring'd ladder semantics |
| Measurement | `scripts/measure_router_edge.py` | Report + verdict banner: promote router / A/B TimesFM / exit TimesFM, stated against the numbers |
| Counterfactual fix | `leverage_eval.py` | `markov_weight()` denominator made flag-aware (live source subset) so the markov-withdrawal identity matches whatever blend produced the measured rows |

## Design decisions worth recording

1. **Every tick, not only qualified signals.** Conditioning on the qualified
   sub-population would select on the very gates the model is meant to
   measure past. Neutral ticks stamp the `stand_by:none` cell — the
   no-setup population is itself a measurable conditioning.
2. **Byte-identical by default.** `router_edge_mode=shadow` + `timesfm_blend_mode=live`
   reproduce today's blend exactly; the regression test re-derives the
   pre-R2 2-source pool by hand and matches to the ulp.
3. **The weight-table catch.** The first implementation used `5/19` for
   p_router (thinking in final shares) — tests proved that makes p_router
   only 14/19 of p_timesfm's influence. Correct: entries share the /14
   denominator; only ratios reach the renormalizer; shares become 5/19
   automatically when all four are live.
4. **The label hoist.** route() needs macro/trend; 5b1/5b2 fetched them
   AFTER compute_ev. Fix: pre-fetch above compute_ev under identical guards;
   5b1/5b2 reuse via sentinels. Identical fetch COUNT, earlier position
   (nightly/daily tables — position within a tick cannot change values).
5. **0.5 is the honest cold-start.** An empty cell emits the uninformative
   prior (not None): the skill scorer skips p==0.5, so nothing scores until
   evidence exists — no cold-start overconfidence by construction.
6. **Heavy sweep gets the TSFM ladder only.** The router edge is a
   light-sweep instrument (the heavy path is the geometry grid);
   heavy/light `p_win_raw` diverge slightly when router_edge goes live —
   documented boundary.
7. **The decision matrix** (scope doc §1): TSFM zero-shot = keep
   provisionally/measure/prepare exit; empirical router conditional = the
   replacement candidate; reasoning-LLM zero-shot numbers = the mirage,
   ruled out; reasoning-LLM agentic categorical = the future rung (event
   vol prior → chronos2_cov covariates; router-state second opinion;
   claims issuer; stamp-grounded rationale).

## Verification

- `tests/test_router_edge.py`: **38/38** — cells/decay/tie-skip, shrinkage
  math, stamp/resolve loop (no double-resolution while a bar is forming),
  both skills on the same outcome, selection-bias guard, live-mode gating +
  sell inversion, fail-open, report shapes, ev_engine p_router (regression
  to the ulp, renormalization, short inversion, temp_weights), flag
  defaults + YAML + vocabulary.
- EV/router/timesfm regression battery: **208/208** (16 files).
- Full suite: **2,313 passed / 20 failed / 22 skipped** — the 20 are the
  pre-existing environment failures, **byte-verified identical on clean
  HEAD via `git stash`** (hmmlearn/slowapi were re-installed this session;
  the remaining 20 are DB/network-dependent). `regime_edge` 19/19.
- Import smoke: orchestrator/heavy_sweep/router_edge import clean; flags
  read `shadow`/`live` from the shipped YAML; empty report renders.
- 3 legacy tests updated to the R2 table semantics with intent preserved
  (`test_p_historical_blend` ghost-key test now pins the 5:4:5:5 ratio set;
  `test_p_markov_hold_n_mode` key-set + the originals-byte-identical
  guarantee; `test_leverage_eval` weight-table consistency now pins the
  live-subset denominator).

## What this did NOT do

- No live flag flips: shadow/live defaults preserve today's behavior
  exactly. The promotion is the operator's move on report evidence.
- No Supabase migration (param_store tiers; graduate to a table when an
  arm earns it — the vol-recalib pattern).
- No LLM call anywhere in the engine: the reasoning-LLM slots are DESIGNED
  (scope doc §5), not built — they need the odds store / Clef-Flash ladder
  and their own shadow-first evidence.
- No change to the vol program (Phase 0.5 thread) or to p_win_bracket's
  ownership of the published probability.

## Operator moves

1. Deploy as-is (shadow) — zero behavior change; stamps begin accumulating
   on the next light sweeps.
2. After a few days: `python -m scripts.measure_router_edge.py` — read the
   verdict banner.
3. On evidence: `router_edge_mode: live` (router skill wins) and/or
   `timesfm_blend_mode: shadow` (the A/B) → `off` (negative-skill exit,
   also drops the :8002 dependency).
4. The reasoning-LLM rung (scope §5) is the next design conversation —
   it composes with this, it does not replace it.
