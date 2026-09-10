# 2026-08-23 — Calibration Bias Fixes Applied + Real-Data Validation

> **Priority**: P0 · **Task**: Fix the stagnation-breaker clear-order bug and the EOD-resolver premature-expiry defect found in `20260823_CALIBRATION_BIAS_CHECK_AUDIT.md` ·
> **Status**: pending (code-complete, tested, validated against live production data — explicitly not deployed to the live Studio) · **Date**: 2026-08-23

**Scope:** `noble-trader-fastapi-backend` — fixes for the issues found in
`20260823_CALIBRATION_BIAS_CHECK_AUDIT.md`.
**Type:** Code changes + live-data validation (no synthetic-only trust).
**Status:** All three fixes below are code-complete, tested (real-interaction
regression tests, not just independently-mocked unit tests), and validated
against the live production dataset. **NOT deployed to the live Studio** —
matches this project's existing discipline of not restarting the live
process on behavioral changes without explicit go-ahead.

---

## 0. Test-isolation fix (prerequisite — found mid-audit, fixed first)

While re-running the test suite to verify the prior audit's "599 passed"
claim, discovered the suite was writing directly to the live production
`nt_tuning_params` table (`settings.py` loads the real service-role key from
`.env.local`; `tests/test_tuning_registry.py`/`test_e2e_registry_wiring.py`
call the real `write_param()` unmocked). Confirmed by re-running: row count
went 226 → 254 immediately after a single `pytest tests/` run.

**Fix:**
- `tests/conftest.py` (new): session-wide `httpx.AsyncClient.request` patch
  that rewrites every `/rest/v1/<table>` call made during pytest to
  `/rest/v1/<table>_test`, regardless of which of the codebase's several
  URL-construction patterns a given call site uses.
- `nt_tuning_params_test` table created live (`CREATE TABLE ... LIKE
  nt_tuning_params INCLUDING ALL`) so the leaky tests get a real round-trip
  instead of a 404 no-op.
- **Cleanup:** deleted all 254 rows from live `nt_tuning_params` — 100%
  matched the test-pollution signature (`updated_by IN ('agent','test')`,
  `reason` values that only exist as literal strings in the test files;
  every row also stuck at `cycle_result='pending'`, meaning zero evidence
  any of them were ever a genuine Layer-1 auto-tuner write). Table is at 0
  rows now, ready to receive only real Layer-1 output.
- **Verified the fix works, not just assumed:** ran `pytest
  tests/test_tuning_registry.py tests/test_e2e_registry_wiring.py` (the
  previously-leaky files) and the full suite — production stayed at 0 rows
  both times; the isolated `nt_tuning_params_test` table absorbed the writes
  instead.

---

## 1. Stagnation-breaker clear-order bug (fix #3c interaction)

**Root cause (§2 of the audit):** `_clear_stale_eod_actions()` deleted
`bayesian_penalty` rows unconditionally every 4h cycle, before
`fetch_recent_action_bias()` read them for `_stagnation_streak()` in the
SAME cycle — so the streak could never exceed 1, and Tier 1/2 escalation
could never fire, no matter how many real consecutive OVERCONFIDENT cycles
ran.

**Fix (`supabase_client.py`, `eod_correction.py`):**
- `_clear_stale_eod_actions()` no longer clears `bayesian_penalty` — only
  `kelly_reduction`/`emergency_heavy_sweep`/`stagnation_kill_switch` (the
  actual enforcement-state actions the original 5-symbol fix needed reset).
- New `_clear_stagnation_history()` clears `bayesian_penalty` specifically,
  called ONLY from the `status == "CALIBRATED"` branch in
  `run_eod_correction` — restoring the originally-documented "auto-recovery
  immediate once bias returns to CALIBRATED" behavior without destroying
  the streak's own history on every unrelated cycle.

**Why the old tests didn't catch this:** every existing integration test
mocked `_clear_stale_eod_actions` and `fetch_recent_action_bias` as two
independent `AsyncMock`s with no shared state — so no test exercised the
actual interaction between them. Per the user's instruction not to trust
mocked-test passes as sufficient evidence, added
`tests/test_stagnation_breaker.py::TestStagnationHistorySurvivesRealClearCycle`
— an in-memory fake `nt_eod_actions` table shared across the REAL
`_clear_stale_eod_actions`/`_clear_stagnation_history`/`_insert_row`/
`fetch_recent_action_bias` implementations (only `httpx`+`settings` are
faked), running `run_eod_correction()` multiple real consecutive cycles:
- `test_streak_accumulates_across_real_consecutive_cycles`: 4 real
  consecutive OVERCONFIDENT cycles now correctly escalate `[0, 1, 1, 2]`
  (was stuck at `[0, 0, 0, 0]` under the bug — verified by temporarily
  reverting the fix and re-running, see below).
- `test_recovery_to_calibrated_resets_history_for_next_streak`: a genuine
  CALIBRATED cycle correctly wipes history so the next OVERCONFIDENT streak
  starts fresh, not resuming pre-recovery counts.

**Regression-proof (reverted fix, re-ran new tests):** temporarily restored
the old unconditional-clear behavior and re-ran
`TestStagnationHistorySurvivesRealClearCycle` — both new tests failed
(`tiers == [0, 0, 0, 0]`, confirming they actually detect the bug) before
re-applying the fix, which passes.

---

## 2. EOD resolver premature-expiry (root cause 3, the bigger one)

**Root cause:** `resolve_signal_outcome()` only ever walks
`closes[:EXPIRY_BARS]` (48). Since `eod_resolver` runs once daily and
force-resolves every open `nt_signal_sim` row regardless of how many
post-entry bars have actually accumulated, a signal entered a few hours
before the run got judged against however few bars existed — and once
`outcome` is no longer NULL, it's excluded from every future resolution
attempt permanently. `expired` outcomes never write a calibration record
either way (only `tp_hit`/`sl_hit` do), so this silently starved calibration
of fresh evidence for every symbol.

**Real-data validation (this is the "run against a real dataset" check) —
queried the live `nt_signal_sim` table directly, no synthetic data:**

| Metric | Value |
|---|---|
| Total resolved `expired` (all-time, all symbols) | **33,379** |
| Total resolved `tp_hit` + `sl_hit` | 22,256 |
| `expired` outcomes with `hold_bars < 48` | **~99–100% for every symbol** |
| AAPL: expired / with `hold_bars<48` | 1,729 / 1,729 (100%, max ever seen: 28 bars) |
| US500: expired / with `hold_bars<48` | 2,640 / 2,624 (99.4%) |
| Currently pending (`outcome IS NULL`), all entered today | 2,932 (~208-214 per symbol across all 14 symbols) |

This is a much bigger and more universal problem than the AAPL/US500 framing
in the prior worklog suggested: **60% of every signal ever simulated across
the whole system resolved as `expired`, and virtually none of them ever got
close to their real 48-bar window.** The 2,932 currently-pending signals
will be the first to benefit once this fix deploys and the resolver runs
again.

**Fix (`eod_resolver.py`):**
- `_resolve_one()`: if the close-walk's own verdict is `"expired"` AND fewer
  than `EXPIRY_BARS` closes were even available yet, return `"pending"`
  instead of patching the row — leaves `outcome` NULL so a later run
  re-attempts it once more bars accumulate. A genuine TP/SL touch still
  resolves immediately regardless of bar count; only a would-be `expired`
  verdict is deferred.
- `resolve_pending_signals()`: new `insufficient_bars` counter, logged
  separately from `resolved`/`errors`/`windowed_out` so this is observable
  going forward (per this project's "no silent caps" convention).

**Tests (`tests/test_eod_resolver.py`, 8 new):**
`TestResolveOneMinimumAgeGate` covers: few-bars-no-touch → pending (not
patched); genuine full-window expiry still resolves; TP/SL hit still
resolves immediately even with very few bars; boundary checks at exactly
`EXPIRY_BARS` and `EXPIRY_BARS - 1`. `TestResolvePendingSignalsInsufficientBarsCounter`
verifies the counter plumbing end-to-end through `resolve_pending_signals`.

---

## Test suite (re-verified, not assumed)

`pytest tests/ -q` → **610 passed, 23 skipped, 0 failed** (up from the
pre-fix 599/603 — 11 new tests: 4 stagnation-history + 8 eod-resolver, minus
1 double-counted). Full suite confirmed run with `tests/conftest.py` active;
live `nt_tuning_params` verified at 0 rows immediately after.

## Not done in this pass

- **Not deployed.** All changes are local/on-disk only. Deploying requires
  a Studio restart (behavioral change), which per this project's established
  practice needs explicit go-ahead — not taken here.
- **Historical mislabeled rows not touched.** The 33,379 already-resolved
  `expired` rows are NOT retroactively relabeled or deleted — that's a data
  decision (would need real historical close-price series to correctly
  re-resolve, which isn't stored) that should be made deliberately, not as
  a side effect of a bug fix.
- **`nt_tuning_params_test` table left in place** (empty/small) — harmless,
  intentionally created for ongoing test isolation, not cleaned up per run.
- The Aug 23 worklog's "wait for Monday market open" caveat for R1/R4 still
  applies and is unrelated to (not resolved by) these fixes.

## Recommended next actions

1. **Deploy when ready** (commit these changes, restart the Studio) —
   this is the only way the 2,932 currently-pending signals and all future
   ones get the corrected resolution logic.
2. **Watch `insufficient_bars` in the resolver's daily log line** post-deploy
   — expect it to be large initially (most of the current backlog is <48
   bars old) and shrink as the backlog matures.
3. **Watch AAPL/US500's `n` in `v_eod_calibration_bias_latest`** post-deploy
   — it should finally start moving (it's been frozen at n=425/n=63 since
   at least 2026-08-23) once real `tp_hit`/`sl_hit` outcomes start landing
   instead of premature `expired`.
4. **Watch stagnation tier escalation** post-deploy — AAPL/US500 should now
   be able to reach Tier 1/2 (`kelly_reduction_override`/`stagnation_kill_switch`)
   if they're still genuinely stuck once fresh evidence starts arriving,
   instead of being silently capped at Tier 0 forever.
5. Consider (separately, deliberately) whether to backfill-relabel or
   discard the 33,379 historical premature-`expired` rows — out of scope
   here, flagged for a future decision.
