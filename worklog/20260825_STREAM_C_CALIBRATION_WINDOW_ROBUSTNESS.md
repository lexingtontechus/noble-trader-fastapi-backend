# 2026-08-25 — Stream C: calibration-window decay weighting + clustering guard

**Scope:** `regime_platform/sweep_orchestrator/supabase_client.py`
(`fetch_calibration_bias`, `fetch_calibration_bias_raw`),
`regime_platform/sweep_orchestrator/eod_correction.py` (surface new fields),
`regime_platform/core/settings.py` (two new constants). Part of the
brick-timing/calibration hardening plan
(`.claude/plans/goofy-rolling-swing.md`, Part 2, Stream C — the first and
safest of three independently-shippable streams; Streams B and A are
gated on this one shipping and being observed for a full 7-day window
first, per the plan's own sequencing).

**Status:** Fixed, tested (revert-and-confirm verified), full suite
unaffected. Not yet deployed.

---

> **Priority**: P1 · **Task**: Add calibration-window decay weighting and
> burst-clustering discount to `fetch_calibration_bias` (Stream C of the
> brick-timing/calibration hardening plan) ·
> **Status**: blocked (code deployed to Studio, but live behavioral
> verification is deferred until the orchestrator's Redis connection
> issue is resolved — unrelated external dependency) ·
> **Date**: 2026-08-25

## Problem

`fetch_calibration_bias`/`fetch_calibration_bias_raw` compute bias over a
hard 7-day rolling window (`EOD_BIAS_WINDOW_DAYS=7`) with no protection
against a single anomalous burst dominating that window. Confirmed live:
USDCAD's `stagnation_kill_switch` this week was driven by `n=69` calibration
records — all 69 written within one ~6-hour stretch (2026-08-23
00:40-06:35) — counted with exactly the same confidence as if they'd been
69 records spread evenly across the full week. As the user put it: "one
bad/good week can carry over" — the 7-day boundary is arbitrary and
clustering within it isn't detected at all.

## Fix

Two independent, complementary mitigations, both in `fetch_calibration_bias`
and `fetch_calibration_bias_raw`:

1. **Recency decay weighting** (`_decay_weighted_bias_stats`): each
   record's contribution to `avg_pred`/`win_rate` is weighted by
   `0.5 ** (age_days / EOD_BIAS_DECAY_HALFLIFE_DAYS)` (default half-life 3
   days) instead of counted equally up to an abrupt 7-day cliff. A 3-day-old
   record counts half as much as a fresh one; a 6-day-old record counts a
   quarter. Falls back to weight=1.0 for any record with an unparseable
   `entered_at`, rather than dropping it.
2. **Clustering discount** (`_clustering_discounted_n`): when the window's
   records span fewer than `EOD_BIAS_MIN_DISTINCT_DAYS` (default 3)
   distinct calendar days, the `n` returned to callers — the value fed
   straight into `bayesian_adjusted_p_win`'s shrink math — is scaled down
   proportionally (`raw_n * distinct_days / min_distinct_days`). A burst
   crammed into a few hours now carries the sample size of a few hours, not
   a full week. Chosen over an outright "insufficient data" veto because it
   degrades gracefully — a symbol with a genuine ongoing problem shouldn't
   lose its OVERCONFIDENT/UNDERCONFIDENT status just because the evidence
   happens to be clustered, it should just be trusted less.

The true (undiscounted) `raw_n` and `distinct_days` are returned alongside
the discounted `n` for observability, and now flow into
`nt_eod_actions.detail` (`eod_correction.py`) next to the existing
`stagnation_streak`/`stagnation_tier`/`source` fields — auditable going
forward without having to re-derive it from raw tables during the next
investigation, the way this one had to be.

**Not doing:** widening the window itself (e.g. to 21-30 days) — that
would slow reaction to genuine regime shifts and isn't needed; the
clustering guard and decay weighting solve the actual observed failure
mode without sacrificing responsiveness.

## Verification

Extended the existing `tests/test_calibration_bias_window.py` (pre-dates
this session, from the original 2026-08-13 rolling-window feature) with a
new `TestClusteringGuardAndDecayWeighting` class:
- `test_usdcad_style_burst_is_discounted_below_raw_count` — the exact
  USDCAD pathology (69 losses, 0 wins, all within 6 hours) — asserts `n`
  is discounted to ~1/3 of `raw_n`, `distinct_days == 1`.
- `test_records_spread_across_enough_days_are_not_discounted` — same
  69-loss/0-win composition spread across 7 real distinct days — asserts
  no discount applies.
- `test_decay_weighting_favors_recent_records_over_stale_ones` — a stale
  majority (50 old losses) plus a fresh minority (10 recent wins) —
  asserts the decay-weighted win_rate is pulled meaningfully above the
  plain unweighted average.
- `test_all_records_same_instant_has_no_decay_effect` — sanity check
  isolating that decay weighting alone (no clustering) is a pure no-op
  when every record shares one timestamp, confirming the discount in the
  first test comes from `distinct_days`, not decay.

Reverted the fix (`git stash`) and confirmed the first three genuinely fail
against the old code (`KeyError: 'distinct_days'` / plain-average
assertion failure) before restoring — the fourth is a baseline sanity
check, correctly passing on both old and new code since it exercises the
no-op case.

Updated the pre-existing 8 tests in the same file to spread their fake
rows across `entered_at` values proportionally (new `_rows()` helper
parameter) so they exercise window/threshold logic unaffected by either
new mechanism — one test's bias-value tolerance was loosened slightly
(`abs=1e-4` → `abs=0.02`) to absorb small rounding noise from the
day-chunking fixture, since its purpose (verify the window filter applies)
doesn't depend on 4-decimal precision.

Full suite: 37 failed / 16 errors unchanged from the pre-existing baseline
(missing local deps: `hmmlearn`, `slowapi`; one pre-existing lazy-import
test-isolation quirk — confirmed identical on `main` via `git stash`
comparison), 366 passed (baseline 362 + this fix's 4 new tests). Zero
regressions.

## Deployment status

Deployed to the Studio (`c6b661a` confirmed via `git log -1` on the box,
2026-08-25 ~06:02 UTC restart). **Not yet behaviorally verifiable**: the
orchestrator process starts but its Redis connection fails during the
protocol handshake (TCP connects fine; RESP read fails immediately —
"Connection closed by server") on this and the prior restart, so the
scheduler never starts at all — no sweeps, no EOD corrections, nothing
runs, unrelated to this change. Per explicit direction, not investigating
this further — Redis is being deprecated from this architecture soon.
Stream C's code is confirmed correctly deployed; live verification (that
the discount/decay actually shows up in `nt_eod_actions.detail` on a real
cycle) is deferred until the orchestrator can actually run again.

## Not done in this pass

Streams B (drift-triggered re-sweeps) and A (wall-clock backtest/live
parity) remain scoped in the plan but explicitly not started — gated on
this stream shipping and being observed for a full 7-day window first
(the window itself needs to complete a cycle to confirm its own effect is
visible), per the plan's sequencing discipline.
