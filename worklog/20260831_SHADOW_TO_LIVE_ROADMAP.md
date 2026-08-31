# 2026-08-31 — Shadow-to-live roadmap: Tier 1/2, Phase 3a/3b

Scope only (per instruction) — nothing in this document is built yet. Ties
together the assessment from the previous exchange into concrete target
dates and decision metrics, plus the one new capture this actually
requires (Tier 1/2's outcome-linked evidence gap).

## Why this is needed: shadow tables measure disagreement, not accuracy

`nt_markov_source_shadow`/`nt_trend_probs_gate_shadow` (Tier 1/2) tell you
the old and new sources disagree — 75.6% and up to 98.7% per-symbol
disagreement as of the last checkpoint — but neither table links a
comparison to what the trade actually did. Flipping either to `live` on
disagreement data alone would be a guess dressed up as a decision.

This is the same gap the earlier "outcome-validation-layer" scope was
built to close — shelved mid-session because the root-cause classifier
wasn't fixed yet, and scoring a known-broken read would have measured the
wrong thing. The root cause is fixed now (`_classify_trend_components`,
verified against real data). That objection no longer applies. This scope
revives the idea in a smaller form: not a new standalone table with its
own capture pipeline, just 4 nullable columns on `nt_signal_sim`,
following the exact precedent already set by `atr` (migration 120) and
`macro_label`/`macro_strength` (migration 124) — capture at signal time,
analyze later against realized outcome.

**Macro filter and Phase 3a don't have this gap** — `nt_macro_edge_by_symbol`
and `nt_heavy_sweep_reachability_shadow` already capture outcome-linkable
or decision-relevant data. Their blocker is purely time-to-accumulate, not
a missing capture mechanism.

## Scope: `nt_signal_sim` capture columns for Tier 1/2

**New nullable columns** (one migration, mirrors migration 124's shape
exactly):
```sql
ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS legacy_markov_state TEXT,      -- Tier 1: legacy HmmModel's current_state (UP/DOWN/FLAT)
  ADD COLUMN IF NOT EXISTS trend_hmm_markov_state TEXT,   -- Tier 1: RegimeHMM's current_state (UP/DOWN/FLAT)
  ADD COLUMN IF NOT EXISTS raw_p_regime DOUBLE PRECISION,     -- Tier 2: ungated p_regime at signal time
  ADD COLUMN IF NOT EXISTS gated_p_regime DOUBLE PRECISION;   -- Tier 2: gated p_regime at signal time
```

**Capture site**: both sweep paths already compute every one of these four
values at the exact point they call `record_markov_shadow_compare`/
`record_trend_probs_gate_shadow` (`orchestrator.py`'s light-sweep block,
`heavy_sweep.py`'s mirror) — this is a matter of threading four
already-in-scope local variables into `_build_signal_sim_payload`'s kwargs
and the `NT_SIGNAL_SIM_COLUMNS` whitelist, identical mechanically to how
`atr`/`macro_label` were added. No new computation, no new fetch, no new
network call.

**Analysis** (new, weekly, chained onto the existing bracket-reachability
job — same precedent as `macro_edge.py`): a small module reading win/loss-
resolved `nt_signal_sim` rows with these columns populated:
- **Tier 1 metric**: per symbol, per source (legacy / trend_hmm) —
  coverage (% of rows where that source called non-FLAT) and accuracy
  (hit rate against realized direction, computed ONLY on that source's own
  non-FLAT calls — a FLAT call is "no answer," not a wrong answer, same
  rule used everywhere else this session).
- **Tier 2 metric**: Brier score, `(p_regime - realized)^2` with
  `realized ∈ {0,1}` (tp_hit=1, sl_hit=0), averaged separately for raw vs
  gated, per symbol and in aggregate — reusing `eod_resolver.py`'s
  existing `calibration_brier` convention, not a new metric.
- One new small table (`nt_hmm_shadow_accuracy` or similar), same shape
  as every other diagnostic this session — flagged explicitly since it's
  a new table; if you'd rather this stay a script-only, no-persisted-table
  diagnostic (no weekly upsert), say so before I build it.

**Size of this ask, stated plainly**: 1 migration (4 columns + 1 new
table), ~4 lines changed at each of 2 existing capture call sites, 1 new
~150-line analysis module chained onto an existing cron job. Smaller than
any single build this session other than the config-only fixes.

## Roadmap

| Item | Status | Next milestone | Target date | Decision metric |
|---|---|---|---|---|
| **Tier 1/2 capture columns** | Scoped (this doc) | Build + deploy | **2026-09-02** (this week) | — |
| **Tier 1/2 first-look review** | — | Sanity check only, not a decision | **2026-09-08** (~1 week post-deploy) | Confirm data is populating correctly per symbol; too little volume yet to decide anything |
| **Tier 1/2 decision review** | — | Go/no-go on `live` | **2026-09-21** (~3 weeks post-deploy) | Tier 1: is `trend_hmm`'s hit-rate accuracy on its own non-FLAT calls clearly higher than legacy's, at comparable or better coverage, across most of the 14 symbols? Tier 2: is gated `p_regime`'s average Brier score clearly lower than raw's, in aggregate and per-symbol? |
| **Macro filter decision review** | Already accumulating (restart this week) | Go/no-go on `live` + tune `boost_scale`/`penalty_scale` | **2026-09-21** (same window — aligns naturally) | Does `expectancy_r` under `aligned` clearly beat `opposed`, per symbol, in `nt_macro_edge_by_symbol`? |
| **Phase 3a first-look review** | Shadow since 2026-08-30 | Sanity check | **2026-09-14** (~2 weekly heavy-sweep cycles) | Confirm `n_compared` is accumulating per symbol; is the "nothing survives" pattern (found live for XAUUSD/AAPL in the original scoping) showing up again, and for which symbols? |
| **Phase 3a decision review** | — | Go/no-go on Phase 3b | **2026-09-28** (~4 weekly cycles) | Is `n_nothing_survived` a *persistent* pattern for specific symbols (not a one-off week)? Is `n_pick_changed` meaningful (>~20-30% of sweeps) for symbols where candidates DO exist? |
| **Phase 3b (grid widening)** | Not started — conditional | Scope, then build, only if 3a's review triggers it | **Contingent — no fixed date.** If triggered by the 2026-09-28 review, tentatively early-to-mid October | Success criterion once built: re-run the original live-simulation methodology — does the widened grid now produce a reachable, positive-expectancy candidate for the previously-failing symbols (target: their `n_nothing_survived` rate drops toward the healthy-symbol baseline, ~0%)? |
| **Phase 3a live flip** | — | Only after 3b ships (if triggered) and its own re-simulation confirms | **After Phase 3b**, not before — flipping 3a live pre-3b risks "no combo qualifies" for XAUUSD/AAPL-class symbols, a worse outcome than today | Same reachability metrics, re-measured post-3b |

## Explicitly not scoped here

- Building Phase 3b itself — contingent on the 2026-09-28 review, not
  scoped in detail until triggered (its rough shape is already sketched in
  `20260825_CALIBRATION_REFRAME_SCOPE.md`'s "Phase 3 scope" section).
- Any change to `ev_engine.py` — none of the four live flips require it;
  all routing is already wired through `effective_kelly` mutation /
  `p_regime` input swap / transition-matrix swap.
- The `httpx.AsyncClient` connection-reuse refactor and `nt_signal_sim`'s
  unresolved seq_scan count (from the 2026-08-31 infra-health thread) —
  tracked separately, unrelated to this roadmap.

## Build notes (2026-08-31, same day)

Built the scoped capture immediately after scoping it.

- **Migration 131**: 4 nullable columns on `nt_signal_sim`
  (`legacy_markov_state`, `trend_hmm_markov_state`, `raw_p_regime`,
  `gated_p_regime`) + new `nt_hmm_shadow_accuracy` table
  ((`symbol`, `metric`) PK, `metric` CHECK'd to `legacy`/`trend_hmm`/
  `raw`/`gated`). Not yet applied.
- **Capture wiring**: `_build_signal_sim_payload` gained the 4 kwargs
  (nullable, same precedent as `atr`/`macro_label`). Both sweep paths'
  Tier 1/2 blocks now stash their already-computed shadow values into
  dedicated `_sim_*`/`sim_*` locals — deliberately NOT reusing
  `_markov_current_state` directly for Tier 1, since that variable gets
  overwritten to the trend_hmm value in `trend_hmm_live` mode (legacy fit
  is skipped then) and reusing it would silently mislabel a trend_hmm
  value as "legacy" once live mode is ever flipped on. All four locals
  default to `None` before their respective conditional blocks so
  referencing them later in the function is always safe, mirroring how
  `_markov_current_state` was already handled.
- **`hmm_shadow_accuracy.py`** (new): `compute_tier1_accuracy` (coverage +
  accuracy against realized direction, FLAT calls excluded from the
  accuracy denominator only) and `compute_tier2_brier` (Brier score,
  reusing `eod_resolver.py`'s existing convention) — both pure functions.
  `build_hmm_shadow_accuracy` fetches + upserts, same dual module/CLI
  shape as `macro_edge.py`. Chained onto the existing weekly job
  (`_run_bracket_reachability`) behind a new `HMM_SHADOW_ACCURACY_ENABLED`
  kill switch — no new cron slot.
- **Tests** (`tests/test_hmm_shadow_accuracy.py`, 14 new): direction
  mapping for all 4 buy/sell × tp/sl combinations, FLAT-excluded-from-
  accuracy-not-coverage, missing-source-column exclusion, per-symbol
  isolation, Brier hand-computed single-row and multi-row averaging,
  missing-column and expired-row exclusion. Full suite: 659 passed, 21
  skipped (pre-existing/unrelated), 0 failed (up from 645).
- **Verified**: ran `hmm_shadow_accuracy.py` standalone against live
  Supabase — got a clean 400 (columns don't exist pre-migration), caught
  and logged without crashing, printed "No rows computed" rather than
  raising. Confirms the error path is safe to deploy ahead of the
  migration being applied, same as every other diagnostic this session.

**Not done**: migration 131 not yet applied. No accuracy data exists yet
— needs the migration applied, a studio restart, and real accumulation
time before the 2026-09-21 review target means anything.

## Deployed and confirmed live (2026-08-31, same day)

Migration 131 applied. Studio restarted 21:50:25 UTC — scheduler came up
clean, all jobs registered (including the two chained onto the weekly
job), zero errors. Confirmed independently via direct query (not just
taking the restart at face value) that all 4 capture columns are
populating on real signals from the first post-restart tick onward, e.g.:

| symbol | legacy_markov_state | trend_hmm_markov_state | raw_p_regime | gated_p_regime |
|---|---|---|---|---|
| XAUUSD | UP | FLAT | 0.970 | 0.500 |
| XAUAUD | UP | DOWN | 0.970 | 0.970 |
| XAGUSD | UP | DOWN | 0.970 | 0.970 |
| USDJPY | FLAT | FLAT | 0.651 | 0.486 |

Real, varied values — genuine Tier 1 disagreement showing up (XAUAUD/
XAGUSD legacy vs trend_hmm), Tier 2 gating actively firing on others
(XAUUSD/USDJPY raw vs gated). **The 2026-09-21 review clock starts now**
(2026-08-31T21:50:25Z) — only signals captured from this point forward
have the outcome-linkable columns populated; anything resolved before
this timestamp will read NULL for all four and won't contribute to
`hmm_shadow_accuracy.py`'s per-symbol counts.
