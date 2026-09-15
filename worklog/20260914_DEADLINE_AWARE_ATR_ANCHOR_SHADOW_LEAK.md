# Deadline-Aware / ATR-Anchor Shadow Leak — Live Production Bug, Fixed Same-Day

> **Priority**: P0 (live production behavior diverged from configured
> flag, silently) · **Status**: fixed, tested, verified · **Date**:
> 2026-09-14

## What happened

While tracing why AAPL/USDJPY/XAGUSD's heavy sweep couldn't escape the
"4+" ATR bucket (explaining that to the user), inspecting the exact call
order in `heavy_sweep.py` surfaced a compositional bug between two
independently-shipped shadow/live feature flags:

1. **`heavy_sweep_atr_anchored_bricks_mode`** (Phase 3b, flipped
   `off`→`shadow` 2026-09-13, worklog
   `20260913_PHASE_3B_GRID_WIDENING_SCOPE.md`) — adds 3 new tighter
   ATR-scaled brick candidates to the grid. In `shadow` mode,
   `select_best_combo_with_atr_anchoring()` correctly restricts its
   returned `chosen` combo to the original (non-widened) brick sizes
   only — the widened pick is computed for comparison/logging but never
   returned as `chosen`.
2. **`heavy_sweep_deadline_aware_scoring_mode`** (root-cause fix, shipped
   `live` 2026-09-08, worklog
   `20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`, well before Phase
   3b existed) — re-scores every candidate in `grid_results` using a
   48h-deadline-capped Sharpe, and (already being `live`) overwrites
   `best` with whatever wins under that corrected metric.

**The bug**: `select_best_combo_deadline_aware()` was called with the
raw `grid_results` — the *full* grid, which already includes the
ATR-anchored candidates whenever `atr_anchored_mode != "off"` — not with
stage 1's restricted candidate set. Stage 2 had no awareness of
`atr_anchored_mode` or stage 1's `chosen` at all; it simply re-derived
`best` from the unrestricted pool and overwrote it.

**Net effect**: any symbol where an ATR-anchored (tight) candidate beat
the baseline on deadline-aware Sharpe got that candidate promoted straight
into production — with `heavy_sweep_atr_anchored_bricks_mode` still
reading `"shadow"` in config. The two flags were each correct in
isolation and never tested together, so this shipped silently for ~2
days (2026-09-13 flip → 2026-09-14 discovery) with zero test coverage
catching it (confirmed: no existing test in `tests/` referenced either
selection function or their composition before this fix).

## Confirmed live impact

```
XAUUSD nt_sweep_result, source=sweep-heavy:
  2026-09-14 17:26 UTC  brick=1.253558  sl=3  tp=5   sharpe=1.6981  <- WIDENED combo, live
  2026-09-13 17:12 UTC  brick=16.308047 sl=7  tp=12  sharpe=0.6620  <- baseline, pre-leak
```

`1.253558` / `1.6981` are the exact values `nt_heavy_sweep_atr_anchored_shadow`
recorded as the *widened* comparison combo for XAUUSD's most recent
sweep (`last_widened_bucket='0.5-1', last_widened_sharpe=1.6981`) — proof
this was the shadow-only candidate landing directly in production, not
a coincidence.

This means the Phase 3b Stage 1 review (scoped for 2026-10-05) had a
false premise as of this discovery: it was intended to compare "shadow
observations" against a stable, untouched baseline, but production had
already been silently running on ATR-anchored brackets for symbols
where the widened combo won on deadline-aware Sharpe — for ~2 days
before anyone noticed.

## Fix

`heavy_sweep.py`, the deadline-aware call site: restrict the candidate
pool fed into `select_best_combo_deadline_aware()` to
`original_brick_sizes` only, unless `atr_anchored_mode == "live"`:

```python
deadline_candidates = (
    grid_results if atr_anchored_mode == "live"
    else [c for c in grid_results if c.brick_size in original_brick_sizes]
)
deadline_selection = select_best_combo_deadline_aware(deadline_candidates, deadline_mode)
```

This makes the two shadow flags compose correctly: whichever stage's
mode is more restrictive governs what can reach `best`. Both selection
helper functions themselves (`select_best_combo_with_atr_anchoring`,
`select_best_combo_deadline_aware`) were already correct in isolation —
no change needed there. The bug was purely in how the call site chained
them.

Byte-identical behavior preserved for the two combinations already
covered by existing worklogs' claims:
- Both flags `off` → unchanged (pre-Phase-A behavior, still exactly
  `pick_best_combo(grid_results)` reading raw `c.sharpe`).
- `atr_anchored_mode="off"`, `deadline_mode="live"` → unchanged
  (`original_brick_sizes` contains every brick size present when mode is
  off, so the restriction is a no-op) — this is the exact configuration
  the 2026-09-08 root-cause fix was validated against (win% 58.5→74.5),
  so that validation still holds.

## Third leak point found: `reachability_aware_pick()` had the identical bug

While scoping whether to build/wire `reachability_aware_pick()` live
(user's next question, same session), tracing its exact call site
turned up the same root cause a third time. `heavy_sweep.py`'s stage 5a
(reachability shadow comparison, `heavy_sweep_reachability_mode`, live
in shadow since 2026-08-28 — this mechanism already exists and was
already running, not something to newly build) also called
`reachability_aware_pick()` with the raw, unrestricted `grid_results`
instead of the ATR-anchoring-restricted pool.

**Confirmed live**: `nt_heavy_sweep_reachability_shadow`'s XAUUSD row
showed `last_baseline_bucket='0.5-1', last_baseline_sharpe=1.6981` — its
internal "baseline" (meant to reproduce the TRUE production Sharpe-only
pick before applying the reachability filter) was actually the
ATR-anchored WIDENED combo, not the real `4+` baseline. This means:
- **`nt_heavy_sweep_reachability_shadow`'s `pick_changed`/
  `nothing_survived` counts have been unreliable since 2026-09-13**
  (when ATR-anchoring went to shadow) for the same structural reason —
  the shadow comparison's own "before" state was already contaminated.
- **A live flip of `heavy_sweep_reachability_mode` today would have
  risked promoting a still-shadow ATR-anchored candidate into
  production** via `reach["filtered_combo"]`, completely independent of
  whether `heavy_sweep_atr_anchored_bricks_mode` itself was live.

**Fix**: pass the same `deadline_candidates` restricted pool (added by
the fix above) into `reachability_aware_pick()` too, instead of raw
`grid_results`. Same one-line class of fix as the first leak.

## Tests added

`tests/test_heavy_sweep_shadow_mode_composition.py` — 8 new tests total
(0 existing coverage of any of these three functions' composition
before this fix):
1. `test_shadow_leak_reproduced_without_the_fix` — reproduces the exact
   bug (documents what would happen without the restriction).
2. `test_fix_keeps_atr_anchored_combo_out_of_production_in_shadow_mode`
   — confirms the fix.
3. `test_fix_allows_atr_anchored_combo_when_atr_anchored_mode_is_live` —
   confirms the restriction lifts correctly once ATR-anchoring itself
   goes live.
4. `test_fix_is_noop_when_atr_anchored_mode_is_off` — confirms no
   change to the pre-Phase-A path.
5. `test_both_modes_off_is_byte_identical_to_pre_phase_a` — confirms the
   claim in both functions' docstrings.
6. `test_atr_anchoring_stage_shadow_mode_unaffected_by_this_fix` —
   confirms stage 1 itself needed no change.
7. `test_reachability_baseline_leak_reproduced_without_the_fix` —
   reproduces the third leak point (reachability's own baseline
   contamination).
8. `test_reachability_fix_uses_restricted_pool_for_its_baseline` —
   confirms the fix for that third leak point.

Full suite: **996 passed, 21 skipped, 0 failed** (up from 988 before
this fix session — net +8).

## What this means for Phase 3b's Oct 5 review

- **The `nt_heavy_sweep_atr_anchored_shadow` data collected 2026-09-13
  17:12 through 2026-09-14 17:26 UTC is contaminated** — it was recording
  comparisons while some of those "widened" picks were simultaneously
  leaking into production, meaning the shadow table's own framing ("this
  is what WOULD happen if flipped live") was already partially true
  for an unknown subset of sweeps in that window. Not necessarily
  invalidating (the widened picks that leaked were, by definition, the
  ones scoring best — consistent with what a real "live" flip would
  have chosen anyway), but the shadow/production boundary was not clean
  for that ~26h window and should not be treated as pure shadow evidence
  when the Oct 5 review runs.
- **Going forward from this fix (2026-09-14 post-fix), the shadow
  isolation is real again** — data collected after this point is
  trustworthy shadow evidence.
- **No change to the Oct 5 review date** — this doesn't invalidate the
  overall Phase 3b thesis (XAUUSD's widened pick scoring well is
  actually a second confirming data point, consistent with the
  vol-tier/reachability findings), it just means the ~26h contaminated
  window should be flagged/excluded from whatever aggregate stats feed
  that review, not trusted as clean before-vs-after shadow comparison.

## Lesson

Two flags, each independently shadow-tested and each individually
correct, composed incorrectly because the second one (already `live`
before the first existed) was never revisited when the first was
introduced. Any future `_mode` flag added to `run_heavy_sweep_for_symbol`
needs to explicitly check: does an existing later-stage selection
function re-derive its pick from a raw candidate pool that could now
contain candidates an earlier stage restricted away? This is a
structural risk specific to this file's "each new axis re-scores
`grid_results` independently" pattern, not a one-off implementation
slip — worth a comment note at the top of the pick-chain (added above)
so the next flag added here checks this explicitly.
