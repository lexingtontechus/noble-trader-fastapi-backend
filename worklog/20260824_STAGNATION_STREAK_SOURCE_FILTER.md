# 2026-08-24 — Stagnation streak diluted by drift_monitor's own bayesian_penalty writes

**Scope:** `regime_platform/sweep_orchestrator/supabase_client.py`,
`regime_platform/sweep_orchestrator/eod_correction.py`. Third of three findings
from digging into "Finding 2" of the 2026-08-24 HMM-convergence investigation.

**Status:** Fixed, tested, full suite green. Not yet deployed.

---

## Problem

`_stagnation_streak` (fix #3c, 2026-08-22) reads a symbol's last 8
`bayesian_penalty` rows via `fetch_recent_action_bias(symbol,
action="bayesian_penalty", limit=8)` to detect "N consecutive 4h correction
cycles with unmoved bias." But `drift_monitor.check_drift` **also** writes
`action="bayesian_penalty"` rows — same action name, different methodology
(48h window, `EOD_DRIFT_THRESHOLD` instead of the 4h cycle's own bias calc,
tagged `detail.source: "drift_monitor"`). The fetch has no way to tell them
apart, so both interleave in the same 8-row lookback.

Depending on the drift-monitor rows' bias values relative to the real 4h-cycle
history, this can either mask genuine stagnation (a drift row breaks the
streak early) or — worse, and what the new regression test demonstrates —
**inflate the streak past its real value**, potentially triggering Tier 2's
hard kill switch (`kelly_f` forced to 0.0) off of unrelated drift-monitor noise
instead of genuine repeated 4h-cycle stagnation.

## Fix

- `eod_correction.py`: tag every action this module writes with
  `detail.source = "eod_correction"` (symmetric with drift_monitor's existing
  `detail.source: "drift_monitor"` tag).
- `supabase_client.fetch_recent_action_bias`: new optional `source` param,
  adds `detail->>source=eq.<source>` server-side (PostgREST JSONB filter).
- `eod_correction.py`'s stagnation-streak call site passes
  `source="eod_correction"`.

Rows written before this fix has no `detail.source` key at all and won't
match the new filter — a one-time, acceptable reset of accumulated streak
history for any symbol currently mid-streak, not a bug to work around.

## Verification

The first version of the regression test (`test_drift_monitor_rows_do_not_count_toward_the_streak`)
passed even against the reverted, unfiltered code — investigated why rather
than trust the green result: the single injected drift-monitor row's bias
value happened to be far enough from the real cycles' bias that
`_stagnation_streak`'s consecutive-match loop broke at the same point whether
the row was filtered out or merely encountered mid-loop, producing an
identical outcome either way. Redesigned with 3 drift-monitor rows, all with
bias values close enough to count as "unmoved" if included — reverted the fix
again and confirmed this version genuinely fails (`streak=5, tier=2, kill
switch fires` instead of the correct `streak=2, tier=1, no kill switch`),
then restored.

New tests: 2 direct unit tests for `fetch_recent_action_bias`'s query
construction (`detail->>source` filter present/absent), 1 real-interaction
regression test (extends the existing shared-fake-table harness from the
2026-08-23 clear-order fix, with the fake table's `get()` now also
implementing `detail->>source` filtering), 1 call-argument assertion.

Full suite: **635 passed, 23 skipped, 0 failed** (up from 632).

## Not done in this pass

Nothing further scoped from this investigation — all three findings from the
HMM-convergence session (Finding 3: `inf`/profit_factor; Finding 2: enforcement
defeated by non-penalty latest row; Finding 1: this one) are now fixed.
Change B (retiring the separate Markov HMM) remains scoped but not started,
per the approved plan, pending Change A's live stability observation window.
