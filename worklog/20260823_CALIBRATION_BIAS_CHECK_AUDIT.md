# 2026-08-23 — Calibration Bias Check (Independent Verification of R1–R4 + Fixes #1–#3c)

**Scope:** `noble-trader-fastapi-backend` — cross-check of the 2026-08-22/23 audit
worklogs and the R1–R4 recommendations against the **live** system: production
Supabase (`NobleTrading`, project `pcvscowltlrxzgxjurcr`), the local test suite,
and the current `orchestrator_0830.log` snapshot.
**Type:** Read-only verification audit. No code changed, no data written.
**Origin:** follow-up to `20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`,
`20260822_STALE_ACTION_CLEARING_SCOPE.md`, `20260822_RAW_BIAS_CYCLE_RESET_PLAN.md`,
and `20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md`. Purpose: don't just re-read
those worklogs' conclusions — independently confirm or refute them against
live data, since prior worklogs have been self-reporting "implemented and
working" without always re-checking against production state after later
changes landed on top.
**Status:** Two previously-unflagged issues found. Everything else in the prior
worklogs checks out.

---

## Summary

| # | Item | Verdict |
|---|---|---|
| 1 | 5 low-volume-symbol stale actions (AUDUSD/EURUSD/GBPUSD/USDCAD/XAUAUD) | ✅ **Confirmed resolved** — live query, zero stale rows left |
| 2 | R1–R4 code present, tested | ✅ **Confirmed** — 599 passed, 23 skipped, 0 failed (re-ran locally, matches worklog exactly) |
| 3 | Stagnation breaker (fix #3c, Tier 1/2 escalation) | ❌ **Silently broken in production** — new finding, see §2 |
| 4 | AAPL/US500 overconfidence resolved | ❌ **Not resolved, not moving** — bias/`n` bit-for-bit frozen since the Aug 23 investigation |
| 5 | Four disconnected bias-threshold definitions (Finding A, fix #2) | ⚠️ **Live-confirmed material impact**, not just theoretical — see §4 |
| 6 | Parameter tuning (Layer 0/1) | ❌ **No evidence Layer 1 has ever run for real** — see §5 |
| 7 | HMM convergence | ❌ **Not fixed** — R4 only discounts its blend impact when detected; the underlying non-convergence is unaddressed and was already widespread a day before it was "discovered" |
| 8 | `config/tuning_registry.yaml` missing locally | ✅ Not a bug — git-ignored, lazy-created by design (`tuning_registry.py:79-86`, confirmed in `.gitignore` and `PARAMETER_TUNING_PLAYBOOK.md` §2.4) |

---

## 1. Stale symbols — confirmed resolved

Live query against `nt_eod_actions` for AUDUSD/EURUSD/GBPUSD/USDCAD/XAUAUD:
AUDUSD/EURUSD/XAUAUD have **zero** rows; GBPUSD/USDCAD have only new
`regime_shift_alert` rows from today. The Aug 18–19 `kelly_reduction` stale
actions described in `20260822_STALE_ACTION_CLEARING_SCOPE.md` are gone. The
unconditional `_clear_stale_eod_actions()` call (`eod_correction.py:343`)
works exactly as designed for its original purpose.

---

## 2. New finding — the stagnation breaker (fix #3c) can never escalate past Tier 0

`eod_correction.py:343` calls `_clear_stale_eod_actions(symbol)`
**unconditionally, for every symbol, at the top of every 4h raw-bias cycle** —
this is fix #1's mechanism from §1 above. `_clear_stale_eod_actions`
(`supabase_client.py:605-645`) DELETEs rows where
`action IN (bayesian_penalty, kelly_reduction, emergency_heavy_sweep, stagnation_kill_switch)`
for that symbol (`supabase_client.py:625`).

Later in the **same loop iteration**, `eod_correction.py:467-469` calls
`fetch_recent_action_bias(symbol, action="bayesian_penalty", limit=8)` to
compute the stagnation streak (`_stagnation_streak`, `eod_correction.py:181-204`)
— but the previous cycle's `bayesian_penalty` row was just deleted by the same
cycle's own clear call, moments earlier. The new `bayesian_penalty` row for
*this* cycle isn't written until `eod_correction.py:491-493`, after the streak
check already ran. Net effect: `fetch_recent_action_bias` never sees more than
0-1 rows of history, `_stagnation_streak` can never exceed 1, and
`_stagnation_tier` (`eod_correction.py:207-220`) can never reach Tier 1 (needs
streak ≥ 2) or Tier 2 (needs streak ≥ 4), no matter how many consecutive
cycles a symbol stays OVERCONFIDENT.

**Live confirmation:** AAPL and US500 — both flagged OVERCONFIDENT for days
per the Aug 22/23 worklogs — each have exactly **one** `bayesian_penalty` row
in the entire `nt_eod_actions` table right now, both showing
`detail.stagnation_streak=1, detail.stagnation_tier=0`. `degeneracy_breaker`
and `stagnation_kill_switch` both have **zero rows, ever**, in the live table.

This is a real, previously-unflagged interaction bug between two fixes shipped
in the same 2026-08-22 batch (the unconditional-clear fix and fix #3c) — each
was tested and works correctly in isolation (their respective unit tests mock
the other's side effects), but they defeat each other in the live raw-bias-mode
cycle, which runs every 4h and is the dominant mode (vs. the once-daily nightly
pass, which is not gated by the clear call and would accumulate normally).

**Side note (ruled out as the cause):** migration 116's `nt_eod_actions.action`
CHECK constraint — flagged in `PARAMETER_TUNING_PLAYBOOK.md` §2.4 as "not yet
applied to live Supabase," which would independently explain 0 rows for
`degeneracy_breaker`/`stagnation_kill_switch` — is actually **already applied**
live (`pg_get_constraintdef` confirms both action names are in the live CHECK
constraint). That worklog note is stale. The 0-row count is fully explained by
the clearing-order bug above, not a blocked insert.

---

## 3. AAPL/US500 — frozen, not improving

Live `v_eod_calibration_bias_latest` right now: **US500 `n=63, bias=0.6771`**,
**AAPL `n=425, bias=0.4443`** — bit-for-bit identical (to 3-4 decimal places)
to the numbers in `20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md`'s investigation
snapshot. Zero new calibration records have landed for either symbol since
that investigation, independently confirming that worklog's root cause 3/4
(the EOD resolver's no-minimum-age gate marking signals `expired` — and thus
never calibration-recordable — before enough bars accumulate) is still
completely unaddressed. R1–R4 fixed blend/regime-HMM mechanics; none of them
touch why these two symbols stop producing fresh evidence. Combined with §2,
the self-correction loop for AAPL/US500 is doubly inert right now: no new
evidence arrives, and even if it did, the escalation tier couldn't accumulate
across cycles.

---

## 4. Finding A (four bias-threshold definitions) — live, material impact today

Today's board directly illustrates the practical consequence flagged in
`20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md` Finding A: USDCHF, USDJPY, and
XAUEUR are **OVERCONFIDENT under the enforced ±0.10 threshold but CALIBRATED
under the raw ±0.30/-0.20 threshold**, live, right now. Half of today's
"overconfident" symbols are a threshold-definition artifact, not the same kind
of problem AAPL/US500/XAGUSD have (which are overconfident under both
definitions). Any dashboard or alert built on one threshold without the other
will over- or under-report the size of the problem.

---

## 5. Parameter tuning (Layer 0/1) — no evidence of a real autonomous tuning run

`config/tuning_registry.yaml`'s absence in this local checkout is expected —
it's git-ignored and created lazily on first write (`tuning_registry.py:79-86`,
`.gitignore`, `PARAMETER_TUNING_PLAYBOOK.md` §2.4) — **not** a bug on its own.

But the live `nt_tuning_params` table (226 rows total) does not look like
genuine Layer-1 output. `PARAMETER_TUNING_PLAYBOOK.md` §2.5.2 describes Layer 1
as a once-daily job taking **one bounded step in one direction** for whichever
pilot parameter has gone longest without a write. Instead, all 226 rows are
timestamped within a single ~1-second window (2026-08-23 06:27:56–06:28:50
UTC), with the same parameters written many times each in that window and
values oscillating both directions (e.g. `MIN_RR`: 1.5 → 1.55 → 1.6 → 1.5 →
1.8 → 1.6 → 1.8 → 1.5 → 1.7 → 1.8 → 1.6, eleven writes in under 3 seconds),
one row for `SOME_NEW_PARAM_NOT_YET_BOUNDED` (not in `PARAM_BOUNDS` at all),
and — every single one of the 226 rows still shows `cycle_result="pending"`.
That pattern (rapid-fire, bidirectional, includes a not-yet-real parameter
name, no cycle ever resolved) is consistent with a test run writing against
production credentials, not a scheduled production job. Whatever the source,
there is no row in this table that looks like a real, resolved Layer-1 tuning
cycle, and the rollback/evaluation half of Layer 1 (updating `cycle_result`)
has apparently never executed even once against this table.

**Not independently verified:** whether this pollution originated from this
repo's own test suite (accidentally hitting real credentials instead of a
mock) or from a manual/CI run elsewhere. Re-running the local suite just now
did not add any new rows (confirmed via row count before/after), so today's
local `pytest` run is not the source.

---

## 6. HMM convergence — not fixed, only newly discounted when detected

`Model is not converging` appears **963 times** in `orchestrator_0830.log`
(covers 2026-08-22 00:39–21:36 UTC) — i.e. this was already a large, standing
problem a full day *before* `20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md`
"discovered" it via the live-data validation script and before R4 was scoped
to react to it. This weakens that worklog's own hedge that the widespread
non-convergence "is more likely a data-quality artifact of thin weekend data" —
it was already frequent on a non-weekend log. R4 (built and confirmed firing
live on US500) discounts `p_regime`'s blend weight when the trend HMM didn't
converge on that tick — a real, working mitigation of the *symptom*. It does
not change convergence itself; that remains "Lever A," explicitly scoped but
not built.

---

## Verification trail

- Read: `20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`,
  `20260822_STALE_ACTION_CLEARING_SCOPE.md`,
  `20260822_RAW_BIAS_CYCLE_RESET_PLAN.md` (partial),
  `20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md`,
  `worklog/references/20260821_l1_l2_l3_recommendations.md`,
  `worklog/PARAMETER_TUNING_PLAYBOOK.md` (§2.4, §2.5).
- Read code: `regime_platform/core/tuning_registry.py`,
  `regime_platform/sweep_orchestrator/eod_correction.py` (lines 225-520),
  `regime_platform/sweep_orchestrator/supabase_client.py`
  (`_clear_stale_eod_actions`, `fetch_recent_action_bias`).
- Local: `pytest tests/ -q` → **599 passed, 23 skipped, 0 failed** (matches the
  worklog's post-R4 number exactly).
- Local log: `grep`-counted patterns in `orchestrator_0830.log`
  (2026-08-22 00:39–21:36 UTC snapshot — the most recent local snapshot;
  **note:** no local snapshot covers the Aug 23 R1–R4 deploy window, so
  anything specific to that deploy was checked via live Supabase instead of
  local logs).
- Live Supabase (`pcvscowltlrxzgxjurcr`, via MCP): `list_migrations`
  (tracking table only goes up to 012 — not a reliable signal, since later
  migrations are evidently applied without being registered there),
  `pg_constraint` on `nt_eod_actions` (confirmed migration 116's CHECK is
  live), `nt_tuning_params` schema + full row dump (confirmed migration 117
  is live), `nt_eod_actions` row counts/detail for
  `degeneracy_breaker`/`stagnation_kill_switch`/`regime_shift_alert`, recent
  `bayesian_penalty` rows for AAPL/US500 with `detail` JSON, `nt_eod_actions`
  for the 5 stale-action symbols, `v_eod_calibration_bias_latest` full board.
- No writes were made to Supabase; no code was changed.

---

## Recommended next actions

1. **Fix the stagnation-breaker/clear-order bug (§2) — highest priority.**
   Either (a) skip clearing `bayesian_penalty` rows specifically (only clear
   `kelly_reduction`/`emergency_heavy_sweep`/`stagnation_kill_switch` at the
   cycle boundary, since those are the "enforcement state" that needs
   resetting — the bias-history rows are what streak-tracking needs to
   persist across cycles), or (b) move the streak computation to read history
   *before* the unconditional clear call runs, or (c) stop clearing
   `bayesian_penalty` at all and instead cap `fetch_recent_action_bias`'s
   window so old rows age out naturally. (a) is probably closest to the
   original intent of both fixes — needs a decision on whether stagnation
   history should survive the "reset" the clear call is named for.
2. **Investigate the `nt_tuning_params` pollution (§5)** before trusting this
   table as Layer 1's audit trail for anything. Find what wrote those 226
   rows at 06:27-06:28 UTC today — check CI logs, whoever ran
   `test_auto_tuner.py`/`test_e2e_registry_wiring.py` around that time, and
   whether those tests have a Supabase mock/fixture gap. Until resolved,
   don't treat any row in this table as evidence Layer 1 has run for real.
3. **Prioritize root cause 3/4 from the Aug 23 worklog (EOD resolver
   minimum-age gate)** — this is the actual blocker for AAPL/US500 (and
   likely other low-liquidity/session-bound symbols) ever generating fresh
   calibration evidence. R1-R4 and fix #1-3c have nothing to act on until
   this is fixed; confirmed today that bias/`n` for both symbols hasn't moved
   at all since the fixes deployed.
4. **Decide on a single source of truth for "calibrated"** (Finding A) —
   today's board shows this isn't theoretical; 3 of today's 6 "overconfident"
   symbols are only overconfident under one of the two live threshold
   definitions. At minimum, any dashboard/alert should show both
   `status`/`status_raw` side by side (the API already does this per fix #2 —
   confirm any consumer built on top of it actually surfaces both).
5. **Wait for Monday market open** (per the Aug 23 worklog's own caveat) before
   drawing conclusions about whether R1/R4 improve real AAPL/US500 behavior —
   today's verification window is still the same weekend/closed-market period
   that worklog flagged as an unreliable test bed.
6. **No action needed** on `config/tuning_registry.yaml`'s local absence — confirmed
   intentional (git-ignored, lazy-created).
