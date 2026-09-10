# 2026-09-02 — `nt_macro_regime` frozen since 08-29 (missing `computed_at`)

Scope only (found during the shadow-to-live check-in against
`worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md`), not built yet.

> **Priority**: P1 · **Task**: Fix `nt_macro_regime` frozen since 08-29
> due to a missing `computed_at` key in the upsert payload ·
> **Status**: resolved · **Date**: 2026-09-02 · **Roadmap refs**: RM-3

## Finding

`nt_macro_regime` has been serving the SAME row for every symbol since
**2026-08-29 06:00:04 UTC** — confirmed by direct query, all 14 rows
bit-identical (`macro_label`, `macro_strength`, `macro_score_smoothed`,
`computed_at`) — despite the refresh running and reporting success
repeatedly since then: the scheduled daily job (06:00 UTC) and, since
this session's scheduler-catch-up fix landed
(`20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md`), 4 additional
catch-up-triggered refreshes today alone (03:08:59, 10:06, 14:09,
18:14), each logging `macro_filter: refreshed 14 symbol(s)` — a clean
success message every time, for a row that never actually changes.

The catch-up mechanism itself is working exactly as designed: it
correctly reads `nt_macro_regime`'s real `max(computed_at)`, correctly
recognizes it as stale (>20h old, `MACRO_REGIME_STALE_AFTER`), and
correctly fires `_run_macro_regime_refresh()`. The bug is downstream of
that, inside the refresh itself.

## Root cause

`macro_filter.py::compute_macro_regime()` (lines 105-111) returns:

```python
return {
    "macro_score": round(macro_score, 6),
    "macro_score_smoothed": round(macro_score_smoothed, 6),
    "macro_label": macro_label,
    "macro_strength": round(macro_strength, 4),
    "sma_window_days": window,
}
```

No `computed_at` key. `refresh_macro_regime()` spreads this straight
into `output_rows` (`{"symbol": symbol, **computed}`) and hands it to
`upsert_macro_regime()`, which POSTs with
`Prefer: resolution=merge-duplicates` — PostgREST's upsert only `SET`s
columns present in the payload's JSON, so `computed_at` — absent from
every row — is never touched after the very first insert (whenever that
row was first created, apparently 2026-08-29's 06:00 run). Same bug
class as migration 123's own documented lesson ("122's initial version
omitted `updated_at` from the payload, so it silently never refreshed
after the first insert") — recurring here in a different table that
predates that lesson being written down.

That alone explains the frozen `computed_at`. The fact that
`macro_label`/`macro_score_smoothed` are ALSO bit-identical across 4
days of real market movement (confirmed: AAPL had a documented large
move this week) suggests the whole row may not be updating at all, not
just the timestamp column — worth confirming directly (see
Verification below) before assuming the fix is only cosmetic.

## Impact

**Bigger than "one stale diagnostic table."** The live per-tick read
(`fetch_macro_regime`, called by both sweep paths before applying the
macro Kelly multiplier and before capturing `macro_label`/
`macro_strength` onto `nt_signal_sim`) has been serving this same frozen
snapshot since 08-29. Two consequences:

1. `macro_regime_mode: shadow` — zero live effect either way (shadow
   doesn't touch Kelly), so no trading impact.
2. **`nt_macro_edge_by_symbol`'s aligned-vs-opposed evidence has been
   accumulating against a static classification, not a live one, for
   the entire period this data has been building.** Every
   `nt_signal_sim` row captured since 08-29 carries whichever frozen
   `macro_label` its symbol happened to have at that moment — the
   "aligned" and "opposed" buckets in today's check-in snapshot
   (BTCUSD looking good, US30USD looking bad, several symbols showing
   only "neutral") reflect trades measured against a macro read that
   never moved, not the actual evolving macro trend. This doesn't
   necessarily make the accumulated evidence *wrong* (the macro label
   the market roughly deserved on 08-29 may still be roughly right days
   later for a 200-day-SMA-based signal — slow-moving by design), but
   it means the **2026-09-21 macro filter decision review target is
   currently unreachable as scoped** — real, moving evidence hasn't
   actually been accumulating.

## Proposed fix (not built)

Add `computed_at` to `compute_macro_regime()`'s return dict (a fresh
`datetime.now(timezone.utc).isoformat()`, same pattern every other
upsert in this codebase already follows — `record_trend_probs_gate_shadow`,
`upsert_bracket_reachability`, etc.), or set it explicitly in
`refresh_macro_regime()` right before the upsert call. Small, one-line
class of fix — the same shape as the already-documented migration 123
lesson.

**Also verify** (before or right after the fix): whether `macro_label`/
`macro_score_smoothed` were ALSO frozen (whole-row no-op) or whether
only `computed_at` was stuck while the other fields were quietly
updating correctly the whole time — changes how much of the existing
`nt_macro_edge_by_symbol` evidence (accumulated 08-26 onward per its
own `computed_at`) needs to be treated as suspect vs. genuinely useful.

## Recommendation

Fix before the macro filter's evidence clock continues — otherwise more
data piles up on top of a frozen read, same problem, larger dataset.
Once fixed, the 2026-09-21 macro filter decision review target
(`20260831_SHADOW_TO_LIVE_ROADMAP.md`) should probably be treated as
restarting from the fix date, not 08-31, since only post-fix data can be
trusted to reflect a live macro read.

## Build notes (2026-09-02, same day)

Fixed as scoped: `computed_at` is now set explicitly in
`refresh_macro_regime()` (a fresh `datetime.now(timezone.utc)` per row,
right before the upsert call) rather than inside `compute_macro_regime()`
— that function is documented as pure/no-I/O, and a wall-clock read
doesn't belong in it.

No new mocked test added — this file's own header explicitly documents
that `refresh_macro_regime`/`fetch_macro_regime` are "read-only
diagnostics verified via a standalone real-data run instead" (same
precedent as `bracket_reachability.py`/`calibration_bias_bucket.py`,
neither of which has a mocked-Supabase suite either) — followed that
convention rather than breaking it.

**Verified against live Supabase, not just "should work"**: ran
`python -m regime_platform.sweep_orchestrator.macro_filter` standalone.
POST returned `200 OK`, direct query afterward confirms all 14 symbols'
`computed_at` jumped to 2026-09-02 20:54:xx (from the frozen
2026-08-29 06:00:04). Labels held steady (AAPL/BTCUSD/US30USD/US500
`bull`, XAGUSD `bear`, rest `neutral`) while strengths shifted by a
small amount (e.g. AAPL 0.8787 -> 0.8873) — consistent with the
smoothed score having continued to evolve correctly underneath the
whole time (this function always recomputes fresh from current daily
bars; only the persisted `computed_at`, and apparently the persisted
`macro_score`/`macro_label`/`macro_strength` values themselves, were
stuck — the small deltas here are one day's worth of real movement
landing for the first time in days, not a discontinuity). Whether
`macro_label` genuinely never changed for 4 days or just happened not
to isn't fully separable after the fact — either way, moot now: this
run is the first real fresh timestamp, and everything from here on is
live.

**Not done**: no code change to backfill or discard the 08-29-through-09-02
`nt_macro_edge_by_symbol` evidence window — flagged in the
Recommendation above as suspect, left for a human call on whether to
reset the review clock rather than silently deciding that here.

## Confirmed live post-restart (2026-09-02, same day)

Studio restarted 21:05:03 UTC with this fix deployed. Confirmed, not
just assumed:
- Scheduler came up clean — all jobs registered including
  "Macro regime refresh (daily)" and "Bracket reachability table
  (weekly)", zero errors.
- The startup catch-up check (`_catch_up_stale_jobs`) did NOT fire a
  staleness warning for `nt_macro_regime` this time — correctly, since
  the manual verification run 11 minutes earlier (20:54:27 UTC) left it
  well inside `MACRO_REGIME_STALE_AFTER` (20h). This is the first
  restart where that catch-up check has found this table genuinely
  fresh rather than re-diagnosing the same 08-29 staleness — direct
  evidence the fix is holding under the real deployed code path, not
  just my standalone verification run.

Closing this out: bug found, fixed, verified against live Supabase, and
now confirmed clean through an actual studio restart with the fix
deployed.
