# 2026-09-08 — Root cause found: deadline-unaware backtest scoring +
# fictional expiry PnL. Supersedes most of 20260907's ATR-anchoring
# direction.

**Status: two fixes shipped in code (not yet deployed), one proven only
in simulation, two decisions made, one historical-data remediation
queued.** This document is the corrected, load-bearing scope — read this
before `20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md`,
which spent most of a day on ATR-anchored brick-size widening (Phase A)
before real-data evidence here showed that mechanism was solving the
wrong variable.

## Why Phase A (ATR-anchoring) is superseded, not just paused

> **Priority**: P1 · **Task**: Retire ATR-anchored brick widening (Phase
> A) after real-data proof it solved the wrong variable ·
> **Status**: resolved · **Date**: 2026-09-08 · **Roadmap refs**: RM-5

Phase A's premise: the grid search's candidate brick sizes never got
close enough to a symbol's ATR to reach the historically-good "0.5-1"/
"1-2" distance/ATR buckets, so widening the candidate set with
ATR-anchored sizes should fix it. Built, backfilled, validated against
real TDVA history (see that worklog's Phase A.2 section) — and it
partially worked (XAUUSD/XAUEUR started reaching "0.5-1") but was
inconsistent, symbol-dependent, and risked actively harming USDCHF/USDJPY
(both already excellent in "4+", both got dragged toward untested
buckets purely because a wider candidate set gave the same broken scoring
function more places to be wrong about).

The actual defect, found 2026-09-08: it was never about the candidate
*set* — it was about how any candidate, wide or narrow, gets *scored*.
`RenkoPipeline.backtest_with_analytics` has no concept of a deadline; it
lets a simulated trade run until it closes, however long that takes, over
a 90-day window. `eod_resolver.py` gives every real signal exactly 48
hours. A bracket whose backtested "wins" mostly take 100+ hours to close
looks great to the selector and mostly times out live. Proven directly
(`scripts/sim_deadline_aware_scoring.py`, single most-recent 90-day window
per symbol, same candidate grid as production — no widening): XAUUSD's
actual current pick produces 10 trades, 0 wins, 0 losses, 10 expired,
$0 — every one of its "wins" in the backtest takes longer than 48h. Fixing
*only* the scoring (same 5 original brick-size candidates, no ATR
anchoring) picks a different bracket that produces 15 trades, 13 wins, 2
losses, 0 expired, 86.7% win rate, +$468.70. One variable changed.

**Consequence for Phase A**: retired, not built further.
`heavy_sweep_atr_anchored_bricks_mode` stays shipped at `off` (already the
default, zero live impact) but is not a priority to advance to
`shadow`/`live`. The `nt_heavy_sweep_atr_anchored_shadow` table and
`scripts/backfill_atr_anchored_bricks_shadow.py` are left in place as
historical record, not deleted, but nothing further should be built on
that path unless the deadline-aware fix below turns out insufficient on
its own once it's actually in production.

## Root cause 1 — deadline-unaware backtest scoring

> **Priority**: P0 · **Task**: Diagnose deadline-unaware Sharpe scoring
> in heavy-sweep grid search (root cause of gold-family near-100%
> expiry) · **Status**: pending (proven in simulation only, per this
> section's own status line) · **Date**: 2026-09-08 · **Roadmap refs**:
> RM-19

**Status: proven in simulation, NOT yet built into production.**

`heavy_sweep._renko_grid_search_sync`/`pick_best_combo` score every
candidate by Sharpe over the FULL uncapped trade set from
`RenkoPipeline.backtest_with_analytics`. Needs to instead: for each
simulated trade, if `exit_ts - entry_ts > EXPIRY_BARS` (48 — reusing
`eod_resolver.EXPIRY_BARS`, not a new/different threshold), treat its
`pnl_bricks` as 0 before computing Sharpe, exactly mirroring what the live
resolver already does to real signals. Pick the best combo under this
corrected Sharpe.

**Verified mechanism** (read `renko/pipeline.py`/`renko/trade_journal.py`
before writing any code — not assumed): `TradeRecord.entry_ts`/`exit_ts`
are bar indices when `backtest()` is called without explicit timestamps
(which is how `_renko_grid_search_sync` calls it) — so
`exit_ts - entry_ts` is exactly hours-held, directly comparable to
`EXPIRY_BARS`. `TradeJournal.get_stats()`'s `sharpe_estimate` is
`mean(pnl_bricks) / std(pnl_bricks, ddof=1)` — reused verbatim in the
simulation, not reinvented.

**Not yet done**: wiring this into `_renko_grid_search_sync`/
`pick_best_combo` for real (today's work was the simulation script proving
it, `scripts/sim_deadline_aware_scoring.py`, not a production change).
Should ship the same way every other scoring change in this codebase has
— a mode gate in `config/feature_flags.yaml` (`off|shadow|live`), given
this changes what gets selected for every symbol immediately once live
(no per-symbol carve-out, per the earlier global-flag decision).

## Root cause 2 — expired scored as fictional $0

> **Priority**: P0 · **Task**: Fix expired-signal PnL fabrication
> (hardcoded $0) in `eod_resolver.compute_pnl` · **Status**: pending
> (fixed in code, not yet deployed, historical data not yet remediated,
> per this section's own status line) · **Date**: 2026-09-08 ·
> **Roadmap refs**: RM-8

**Status: fixed in code, tests passing, NOT yet deployed, historical data
NOT yet remediated.**

`eod_resolver.resolve_signal_outcome` already walks the full 48-bar
window to compute `mfe`/`mae` — the real price at bar 48 was always in
memory and simply never captured into `exit_price` when nothing hit
TP/SL. `compute_pnl` then hardcoded `pnl_bricks = 0.0` for anything not
`tp_hit`/`sl_hit`. Confirmed via direct query before fixing: 59,913 of
59,913 expired rows in `nt_signal_sim` had `exit_price IS NULL` — the
real outcome of every expired signal ever generated was unrecoverable
from the table itself. 52.3% showed `abs(mfe) > abs(mae)`, i.e. plausibly
sitting in profit when the clock ran out and recorded as a flat zero
regardless.

**Fixed**: `resolve_signal_outcome` now sets `exit_price = window[-1]`
when expired (the position's real last-known price). `compute_pnl` now
computes real mark-to-market `pnl_bricks` from `entry_price`/`exit_price`
for expired, same brick-unit convention as `tp_hit`/`sl_hit` so all three
outcome types stay summable. Falls back to the old `0.0` only when
price data is genuinely absent (defensive, matches old rows). Files:
`regime_platform/sweep_orchestrator/eod_resolver.py`,
`tests/test_eod_resolver.py` (2 tests updated, 3 new). Full suite:
707 passed, 0 failed.

**Explicitly not changed**: the 48-hour deadline itself, or what counts
as "expired" vs "resolved" — only what gets recorded as the financial
consequence of expiring. `EXPIRY_BARS=48` is untouched.

**Not yet done**: Studio deployment (this fix has zero effect until the
live resolver process runs this code), and the historical remediation
below.

## Decision — expired stays out of win/loss, permanently pending a fix

> **Priority**: P2 · **Task**: Decide expired signals stay excluded from
> win/loss/calibration counts; require an expiry-rate threshold alert
> instead · **Status**: blocked (alert mechanism and threshold not yet
> decided/built) · **Date**: 2026-09-08 · **Roadmap refs**: RM-14

User decision, 2026-09-08: even with real mark-to-market PnL now
computed, `expired` does **not** get folded into win/loss/calibration
counts as a loss (or a win). It stays excluded from
`nt_calibration_bias_by_bucket`/`nt_calibration_records`'s population
exactly as today (`outcome in (tp_hit, sl_hit)` only) — no change to that
filter. Instead: **a threshold alert is required** — when a symbol's
expiry rate crosses some threshold (not yet numerically decided), that
should surface as an actionable alert, and the priority is to drive the
expiry rate down (via root cause 1's fix, primarily) before ever
revisiting whether expired should count toward calibration at all.

**Not yet built**: the alert mechanism itself. Needs: a threshold value
(propose per-symbol `pct_expired` computed on a rolling window, not
all-time — all-time will always look bad given 59,913 historical rows
under the old broken scoring), a delivery channel (this codebase already
has `DISCORD_WEBHOOK_STATUS`/`DISCORD_WEBHOOK_DEBUG` wired for exactly
this kind of operational alert — reuse, don't build new plumbing), and a
decision on cadence (checked every heavy sweep? A separate periodic job?).
Scoped, not built.

## Decision — `config/brick_params.yaml` git-tracking bug, fixed

> **Priority**: P3 · **Task**: Untrack `config/brick_params.yaml` (live-
> mutated runtime file wrongly committed, same anti-pattern as
> `tuning_registry.yaml`) · **Status**: resolved · **Date**: 2026-09-08

User was told the orchestrator updates this file; user's intuition said
that was wrong. Both were half-right: the orchestrator does rewrite it —
on Studio's local disk. The committed copy in this repo does not receive
those rewrites (confirmed: last real commit predates today by 7+ weeks,
missing XAUUSD/XAUEUR/XAUAUD entirely, `defaults:` block sized for a ~$1
asset, nonsensical for gold). This is the **exact same anti-pattern**
already caught and fixed for `config/tuning_registry.yaml`
(`.gitignore`'s own 2026-08-23 comment: "live-mutated runtime state, NOT
an audit trail... don't repeat that for this one") — just never applied
to this second file. Fixed: `config/brick_params.yaml` added to
`.gitignore`, `git rm --cached` run (file kept locally, untracked going
forward). The real audit trail already exists without building anything
new: `nt_signal_sim` captures `brick_size`/`sl_bricks`/`tp_bricks` per
signal at generation time, so "what geometry was live for symbol X at
time Y" is answerable from real data, not from this file.

## Decision — historical contamination gets recomputed, not deleted

> **Priority**: P1 · **Task**: Decide to recompute (not delete) 59,913
> contaminated expired-signal rows via real historical-price backfill ·
> **Status**: pending ("Not yet built. Next concrete step.") ·
> **Date**: 2026-09-08 · **Roadmap refs**: RM-8

All 59,913 expired `nt_signal_sim` rows are within TDVA's ~208-day
history depth (earliest 2026-08-06, latest 2026-09-06 — zero rows beyond
recoverable range, checked directly). Deleting them destroys recoverable,
real data. Correct action: re-fetch the real historical price at each
row's 48-hour mark and re-run the fixed `resolve_signal_outcome`/
`compute_pnl` against it, patching `exit_price`/`pnl_bricks`/`pnl_dollars`
in place — same rows, same signal_id, real numbers instead of fiction.

**Not yet built.** Next concrete step.

## Build notes (2026-09-08, later same day)

> **Priority**: P0 · **Task**: Backfill the 59,913 contaminated expired-
> signal rows and ship `heavy_sweep_deadline_aware_scoring_mode` (off) ·
> **Status**: resolved (both built/verified; Studio deploy tracked
> separately) · **Date**: 2026-09-08 · **Roadmap refs**: RM-8, RM-19

**Priority 1 (historical remediation) — done.** `scripts/backfill_expired_mark_to_market.py`: fetched real TDVA history per symbol, recomputed `exit_price`/`pnl_bricks`/`pnl_dollars` for all 59,913 contaminated rows via the fixed `compute_pnl`. 59,912 patched cleanly; 1 hit a transient Cloudflare 502 and was retried individually. **Verified: 0 of 59,913 `outcome='expired'` rows have `exit_price IS NULL` post-run** (was 59,913 of 59,913 before). Real historical data, not deletion — nothing was discarded.

**Priority 2 (deadline-aware scoring in production) — built, not deployed.** `heavy_sweep_deadline_aware_scoring_mode` (`off|shadow|live`, default `off`) added to `config/feature_flags.yaml`. `ComboResult` gains `deadline_aware_sharpe` (always computed — cheap, reuses the trade list already in hand); `EXPIRY_BARS` imported directly from `eod_resolver` (not duplicated, no circular import — confirmed before importing). New pure functions `deadline_aware_sharpe()` and `select_best_combo_deadline_aware()` (same shape/discipline as Phase A's `select_best_combo_with_atr_anchoring` — off is byte-identical to before, shadow logs+persists without changing the real pick, live applies the corrected pick). Wired into `run_heavy_sweep_for_symbol` right after the (now-deprioritized) ATR-anchoring block, operating on the same `grid_results`. New shadow table `nt_heavy_sweep_deadline_aware_shadow` (migration 136, applied) + `record_heavy_sweep_deadline_aware_shadow()`, mirroring the ATR-anchoring shadow table exactly. 14 new tests (`tests/test_heavy_sweep_deadline_aware_scoring.py`), full suite 714 passed / 0 failed / 21 pre-existing skips.

**Explicitly not done yet**: Studio deployment (nothing above has any live effect until this ships to the running orchestrator process); the multi-window historical validation this worklog said should substitute for a multi-week organic shadow wait (same method as `scripts/backfill_atr_anchored_bricks_shadow.py`, applied to scoring instead of candidate width); the threshold alert for expiry rate (Discord-based, not yet built); `nt_calibration_bias_by_bucket`/`nt_bracket_reachability` still need recomputing now that the underlying `nt_signal_sim` population has real expired PnL instead of fictional zeros.

## Build notes — roadmap infrastructure (2026-09-08, same day)

> **Priority**: P1 · **Task**: Build `nt_roadmap` table and rewrite
> `pulse-check` to query it instead of stale hand-maintained prose ·
> **Status**: resolved · **Date**: 2026-09-08

Audit of the prior two weeks (triggered by this session) found the
pattern behind everything above: `pulse-check`'s `SKILL.md` had
accumulated "known baseline, don't re-report" prose exceptions one
incident at a time, to the point of explicitly telling itself that a high
`expired` rate was "not a new problem" — actively suppressing the signal
that would have surfaced both root causes in this document weeks earlier.
The nominal "roadmap" it pointed to (`worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md`)
was itself just a dated worklog — written once, never mechanically
re-checked against today's date, which is how `macro_regime_mode`'s
review clock kept running through a 4-day data freeze with nobody
noticing it had gone stale.

**Built**: `nt_roadmap` (migration 137) — one row per mode-gated
mechanism/pending action, with a real `review_at` pulse-check can query
directly (`WHERE review_at < now() AND status NOT IN ('resolved',
'superseded')`) instead of a judgment call over prose. Seeded with 16
rows from this session's audit, including corrected dates
(`macro_regime_mode`'s clock reset) and previously-untracked items (4
flagged `overdue` on day one: today's missed Tier1/2 check, both
undeployed/unverified fixes from this session, and two prior commits with
no confirmed-deployed status). `pulse-check`'s `SKILL.md` rewritten to
query this table first, every run, plus an SSH-based deployed-commit-vs-HEAD
check replacing reliance on the stale hand-maintained CHANGELOG.

**The three-artifact distinction, going forward**: `worklog/*.md` stays
historical narrative, append-only, never revisited — unchanged. Deployment
status is answered by directly querying Studio's running commit (§1 of
pulse-check), not a maintained changelog file. `nt_roadmap` is the one
and only place "what's pending and when does it need attention" lives —
updated in place, not append-only, and now the first thing any pulse
check reports.

## Priority order

1. **Historical remediation** (recompute the 59,913 contaminated rows) —
   mechanical, no design decisions pending, ready to build now. **Done**
   (59,912/59,913 backfilled cleanly, 1 manual retry).
2. **Root cause 1 in production** (`_renko_grid_search_sync`/
   `pick_best_combo` deadline-aware rescoring) — the highest-value fix,
   currently proven only in a standalone script. **Done** — flipped
   `heavy_sweep_deadline_aware_scoring_mode` to `live` after 70-anchor
   multi-window validation (win% 58.5→74.5, expired% 49.3→42.0).
3. **Threshold alert for expiry rate** — needs a numeric threshold
   decided, then reuses existing Discord alert plumbing. **Done** —
   `expiry_rate_alert.py` built, 50% threshold (data-driven, tunable via
   `feature_flags.yaml`), live-smoke-tested (see below).
4. **Studio deployment** of both eod_resolver.py fixes and (once built)
   the production scoring fix — nothing above has any live effect until
   this happens. **Confirmed deployed** — Studio `git log -1` matches
   local HEAD.
5. **USDCAD's directional miscalibration** — separate problem, not
   reachability or timing, not addressed by anything above. Needs its own
   investigation thread. **Not started.**

## Priority #3 built — expiry-rate threshold alert

> **Priority**: P1 · **Task**: Build the per-symbol expiry-rate
> threshold alert (Discord, cooldown-gated) · **Status**: resolved
> (built, tested, live-smoke-tested; Studio deploy tracked separately) ·
> **Date**: 2026-09-08 · **Roadmap refs**: RM-14

`regime_platform/sweep_orchestrator/expiry_rate_alert.py`: checked once
daily at the end of `_run_eod_lookback` (after the resolver batch
produces fresh numbers), reads `v_symbol_signal_scorecard_7d` (migration
138/140), fires `notifications.discord.send_risk_alert` (#system-status,
existing plumbing — no new Discord integration built) per symbol whose
7-day `expired_pct` exceeds a threshold, gated by a per-symbol cooldown
(`nt_expiry_rate_alerts`, migration 141) so a persisting condition
doesn't re-alert every day. All four parameters
(`enabled`/`threshold_pct`/`min_resolved`/`cooldown_hours`) live in
`config/feature_flags.yaml`'s `expiry_rate_alert` section — no env var,
no Studio Secret, same config-centralization discipline as everything
else today. `threshold_pct=50.0` chosen from today's real cross-symbol
spread (AUDUSD 83.8%, EURUSD 75.4%, XAUUSD 47.7%, XAGUSD 36.8%) —
flagged in the YAML comment as data-driven, not a settled long-term
number. 6 new unit tests (721 total passing). Live-smoke-tested against
production data before considering it done: correctly identified AUDUSD
(83.9%, n=31) and BTCUSD (61.1%, n=18) over threshold and sent real
Discord alerts to #system-status; correctly suppressed a repeat AUDUSD
alert on a second run (cooldown active). Code pushed, not yet deployed
to Studio.

## Explicitly not in this scope

Regime labelling, calibration bias enforcement's classification logic,
`compute_ev`, Kelly sizing, the light-sweep qualification gate stack, and
`EXPIRY_BARS` itself (the 48h deadline stays exactly where it is — this
whole document is about scoring and recording against that fixed
deadline, not moving it).

## Follow-up pulse check (2026-09-08, next morning) — deploy confirmed, one new bug found and fixed

> **Priority**: P1 · **Task**: Verify Studio deployment, run Tier1/2
> diagnostics, and fix the `nt_hmm_shadow_accuracy.computed_at` freeze
> bug found during verification · **Status**: resolved (fix committed/
> pushed; will self-verify on next weekly run) · **Date**: 2026-09-08 ·
> **Roadmap refs**: RM-17, RM-11, RM-16

Git pushed (`f0be810`) end of prior session; user deployed to Studio
overnight. Ran `pulse-check` (rewritten version, §0 `nt_roadmap` first)
plus the deferred Tier1/2 first-look and the two deployment-verification
roadmap items.

**Deployment — confirmed, with one open caveat.** SSH-confirmed Studio's
`git log -1` HEAD is exactly `f0be810`, matching local. A process restart
happened at 2026-09-08 18:14 UTC (triggered the `nt_bracket_reachability`
startup-catchup guard, confirming it was a real restart, not just a file
sync). Also confirmed via the same method: `dfec69a` (Kelly direction-
aware sizing) and `7cb8616` (EOD catch-up) are both in Studio's commit
ancestry — the two previously-unverified deployments from the prior audit
are resolved. **Caveat, not yet closed**: zero signals have passed through
the batch resolver since the 18:14 restart (last batch ran 00:32 UTC,
before the deploy) — `expired_missing_exit_price = 0` today is real but
not yet a live test of the fix under the new code; that test is tonight's
00:32 UTC `eod-lookback` run, not yet observed as of this writing.

**Tier1/2 first-look — done, with a real (if early) finding.** The
weekly chained diagnostics job (`_run_bracket_reachability`) fired twice
today via the restart catch-up (10:28 and 18:14 UTC), refreshing
`nt_bracket_reachability`/`nt_calibration_bias_by_bucket`/
`nt_macro_edge_by_symbol` and, eventually, `nt_hmm_shadow_accuracy` (see
bug below). Real data now exists for the first time. Early read, not yet
conclusive ahead of the 2026-09-21 review: **`trend_hmm` accuracy is
notably worse than `legacy` for the gold-family symbols specifically**
(XAUUSD: legacy 57.2% vs trend_hmm 17.5%; XAUEUR: legacy 57.4% vs
trend_hmm 11.9%), on real sample sizes (n in the hundreds per symbol).
Mixed/roughly-even elsewhere (e.g. USDJPY: legacy 40.9% vs trend_hmm
45.9%). Worth tracking into the review, not a decision on its own — this
is the first time this table has ever had real data, not a trend.

**New bug found and fixed: `nt_hmm_shadow_accuracy.computed_at` frozen on
every re-run.** The orchestrator log showed the job genuinely re-computing
fresh rows each run (`[hmm-shadow-accuracy] done: 52 row(s)` at
18:15:55 UTC), but every row's `computed_at` stayed pinned at the very
first insert (`2026-09-08 10:26:09`), even after that later run. Root
cause: `compute_tier1_accuracy`/`compute_tier2_brier` (both deliberately
pure, no wall-clock dependency) never set `computed_at`, so the upsert
payload omitted the column entirely — and `Prefer: resolution=merge-
duplicates` never touches a column absent from the payload. **Identical
bug class to the 2026-09-02 `nt_macro_regime.computed_at` incident**,
just never patched in this sibling module. Fixed:
`build_hmm_shadow_accuracy()` now stamps `computed_at` centrally right
before the upsert; `NT_HMM_SHADOW_ACCURACY_COLUMNS` now includes it (it
would have been silently stripped even if set upstream — found this while
fixing, not before). New regression test
(`test_build_hmm_shadow_accuracy_stamps_computed_at_on_every_row`). Full
suite: 715 passed, 0 failed. Committed and pushed (`b8ebd85`) — not yet
deployed; will self-verify next time the weekly job runs.

**Weekly cron trigger — downgraded, not closed.** The real question from
last night ("why does `_run_bracket_reachability`'s Sun 07:00 UTC
`CronTrigger` never fire on schedule") is still unanswered. But the
catch-up safety net fired correctly twice today (10:28 and 18:14 UTC
restarts), so the operational risk is lower than framed last night —
Studio restarts every 2-6h, comfortably under the 6.5-day staleness
threshold. Still worth root-causing eventually, no longer urgent.

**New, not-yet-triaged finding**: `nt_roadmap`/pulse-check §7 showed
EURUSD (8 days) and USDCHF (5 days) with no qualified signal in that
window — not investigated further this session. Candidate for today's
work if prioritized.

All of the above reflected in `nt_roadmap` (rows updated/added:
`deadline_aware_scoring_studio_deploy`, `kelly_direction_aware_sizing_deploy`,
`eod_resolver_catchup_deploy`, `nt_calibration_bias_by_bucket_recompute`,
`weekly_chained_job_not_firing`, new row
`hmm_shadow_accuracy_computed_at_freeze`).

## Resolution reached (2026-09-08, later same day) — deadline-aware scoring flipped to live

> **Priority**: P0 · **Task**: Flip `heavy_sweep_deadline_aware_scoring_mode`
> to `live` after 70-anchor multi-window real-data validation ·
> **Status**: resolved (flipped live same day; Studio deploy tracked
> separately) · **Date**: 2026-09-08 · **Roadmap refs**: RM-19, RM-20

User directive: stop waiting for calendar review dates once evidence is
strong; test and verify continuously; work toward a self-learning
platform. Applied directly to `heavy_sweep_deadline_aware_scoring_mode`,
which had been sitting at `off` with a 09-11 review date.

**Multi-window validation, run same day, not deferred**:
`scripts/backfill_deadline_aware_scoring.py` — same real-outcome
methodology as `scripts/sim_deadline_aware_scoring.py` (single window,
prior day), extended to 70 historical anchors across all 14 symbols
(reusing the multi-window/multiprocessing mechanics from
`scripts/backfill_atr_anchored_bricks_shadow.py`, and the actual
production `ComboResult`/`deadline_aware_sharpe`/
`select_best_combo_deadline_aware` functions directly — not a
re-derived approximation).

**Result**: corrected scoring beat the uncapped method on every aggregate
metric, over real historical price data:

| Method | avg win% | avg expired% | total P/L |
|---|---|---|---|
| original (today's method) | 58.5% | 49.3% | $59,658.69 |
| corrected (the fix) | 74.5% | 42.0% | $66,631.94 |

11 of 14 symbols improved or held flat on win rate, several dramatically
(US30USD 42.0%→85.6% win rate with 4.3x PnL; US500 34.8%→82.6% with 2.4x
PnL; GBPUSD 30.0%→75.0%; USDJPY 45.0%→80.0%). No catastrophic regression
on any symbol — the handful with a small PnL dip (AAPL, BTCUSD, XAUAUD,
XAUUSD) all still gained win-rate/reliability. USDCAD stayed bad either
way (0%→10%, still effectively broken) — consistent with its
already-diagnosed separate directional-miscalibration problem, which this
fix was never meant to touch and correctly doesn't.

**Decision**: `heavy_sweep_deadline_aware_scoring_mode` flipped from
`off` to `live` in `config/feature_flags.yaml` same day. Full suite
re-verified: 715 passed, 0 failed. Config-only change (no code touched)
— committed and pushed. **Not yet deployed to Studio** — the config
change has zero live effect until the running process picks it up.
`nt_roadmap` updated: the mechanism's own row marked resolved with a new
09-11 review date (monitor real production outcomes once deployed, not
more backtesting); new row `deadline_aware_scoring_live_deploy` tracks
the deployment step itself.

## Config-centralization fix — 8 more parameters migrated (2026-09-08)

> **Priority**: P2 · **Task**: Migrate 8 `os.getenv()` qualification-gate
> parameters into `feature_flags.yaml` · **Status**: resolved
> (migrated, tests pass; Studio deploy tracked separately) ·
> **Date**: 2026-09-08

Parallel agent audit (launched alongside the above, per the user's
"run parallel subagents" instruction) found 8 qualification-gate
parameters still read via `os.getenv(...)` with an ACTIVE `.env.local`/
Studio entry and zero `feature_flags.yaml` counterpart —
`MIN_REGIME_CONF`, `MIN_KELLY`, `MIN_TIMESFM_CONFIDENCE`, `MIN_EV`,
`MIN_P_WIN`, `TIMESFM_GATE_MODE`, `SWEEP_AFTER_HOURS`,
`SIGNAL_COOLDOWN_MINUTES` — the exact undetectable-divergence shape that
caused the `MIN_RR` incident, just not yet triggered.

Migrated using `.env.local`'s actual current values (not code fallback
defaults) into a new `qualification_gates` section + 3 standalone keys in
`config/feature_flags.yaml`, with 8 new getters in `feature_flags.py`
following the exact `get_min_rr()` precedent. **Found one real,
surprising divergence in the process**: `SWEEP_AFTER_HOURS` was
configured `true` (sweep all symbols regardless of market hours) —
the *opposite* of the code's own comment describing `false` as the
recommended default. Flagged explicitly in the new config's comment for
review rather than silently resolved either way. Also confirmed via a
separate read of `tuning_registry.py`: 5 of these 8 already have
declared `PARAM_BOUNDS` as future Layer-1-autonomous-tuner candidates —
that tuner isn't built yet, so `feature_flags.yaml` remains the correct,
active read path today, same relationship `MIN_RR` already has with that
registry.

Full suite re-verified after migration: 715 passed, 0 failed. All 8
getters spot-checked to return the exact expected values. Committed and
pushed, not yet deployed.

## Two more real findings from parallel agent investigation (2026-09-08)

> **Priority**: P1 · **Task**: Root-cause the weekly-cron-never-firing
> pattern (accepted as-is) and the USDCHF calibration-governor lockup
> (found, not yet fixed) · **Status**: pending (USDCHF unresolved) ·
> **Date**: 2026-09-08 · **Roadmap refs**: RM-16, RM-22

**Weekly cron trigger — root cause found, decided not to fix.**
`BRACKET_REACHABILITY_STALE_AFTER = 6.5 days` (orchestrator.py:604) is
shorter than the job's own 7-day cadence, so the catch-up safety net
always preempts the real Sun-07:00-UTC `CronTrigger` before it gets a
clean week to itself — confirmed via 3/3 historical `computed_at`
timestamps landing at 6.6-7.1 day intervals on non-Sunday days. Decision:
not changing the threshold. The deeper cause is architectural
(`AsyncIOScheduler`'s in-memory job store recomputes `next_run_time`
forward from "now" on every restart, documented 2026-09-01) — widening
the threshold would only let tables go more stale without fixing cron
reliability, since restarts (every 2-6h) already vastly outpace any
staleness window that would matter. The catch-up net is objectively
providing *better* freshness than the intended weekly cadence ever
would. Accepted as-is.

**USDCHF calibration governor stuck.** Raw Kelly math shows a genuine
positive edge (`mu/sigma2` ≈ 65-110, raw Kelly `full=1.0`), but
calibration-bias enforcement has been force-zeroing effective Kelly
(0.0322→0.0145) and crushing `p_win` (avg 0.28, 100% of 1,181 sweeps
fail `p_win_ok<0.55`) for 8+ consecutive days, based on a frozen
`OVERCONFIDENT bias=0.5619, n=294` sample that isn't updating. Not yet
investigated further — added to `nt_roadmap` as
`usdchf_calibration_governor_stuck`. EURUSD checked in the same pass:
genuinely quiet market, not a bug, no action taken.

## Studio deployment confirmed + three more findings (2026-09-08, continued)

> **Priority**: P1 · **Task**: Confirm Studio deployment; fully root-
> cause the USDCHF calibration lock; fix the `sweep_after_hours` hot-
> reload gap; catalog the 3-convention P/L methodology split; ship the
> per-symbol W/L scorecard views · **Status**: pending (deployment
> confirmed and `sweep_after_hours`/scorecard shipped, but USDCHF and
> the P/L methodology decision remain open — see report for the
> ambiguity note) · **Date**: 2026-09-08 · **Roadmap refs**: RM-21,
> RM-22, RM-23

**Studio is current.** `ssh noble-trader-unified "cd /home/zeus/content/noble-trader && git log -1"` matches local HEAD (`5b917de`) exactly — the deadline-aware-scoring live flip and the 8-parameter config migration are both deployed and running.

**USDCHF calibration governor — fully root-caused, not just observed.**
This is a permanent self-lock, not a slow-to-clear stale value. Chain:
(1) a burst of 294 near-duplicate calibration records was written
2026-09-02→2026-09-04 (5-minute cadence, virtually all `outcome=loss`,
`realized_win_rate` exactly `0.0` in `v_eod_calibration_bias_latest`) —
likely a casualty of the same pricing/scoring bugs fixed today, not
investigated further; (2) that produces `bias=0.56` (OVERCONFIDENT,
threshold 0.10), firing `bayesian_penalty` + `kelly_reduction` (kelly
×0.5) every 4h cycle since; (3) adjusted kelly (0.0322 raw × 0.5 =
0.0161) falls below `MIN_KELLY=0.02`, so USDCHF can never qualify a new
signal; (4) `_write_calibration_record` (`eod_resolver.py:538-539`) only
fires for actually-entered `tp_hit`/`sl_hit` outcomes — with no new
qualified signal ever entered, no new evidence can ever arrive to move
the bias; (5) `stagnation_streak` hit 9 (≥ `STAGNATION_RESET_CYCLES=8`)
on 2026-09-08, resetting `stagnation_tier` to 0 — this only stops
*escalation* (a harder kill-switch), it does not lift the underlying
penalty. The only real exit today is the 7-day rolling window
(`v_eod_calibration_bias_latest WHERE entered_at >= now()-7days`)
draining the poisoned Sept 2-4 rows on its own: `n` drops below 20
(→`INSUFFICIENT_DATA`) around 2026-09-09, fully empty by 2026-09-11.
No code fix applied — this is a design gap (calibration-evidence
deadlock with no self-recovery path other than time) worth a real
decision, e.g. exempting an `INSUFFICIENT_DATA` symbol from
`kelly_reduction` so it can requalify and generate fresh evidence sooner
rather than waiting on the window to empty. Updated on `nt_roadmap`
with `review_at=2026-09-11`.

**`sweep_after_hours` — verified live/load-bearing, not legacy; found and fixed a real hot-reload bug.**
Full trace confirmed this is NOT dead code: it gates
`orchestrator.py::_sweep_group`'s market-hours filter — `true` (today's
live value) sweeps every symbol regardless of market hours; `false`
skips stocks/commodities when their market is closed (crypto/forex are
always swept either way). But the same-day config-centralization
migration had captured it as a module-level constant read once at
import — unlike the neighboring `MARKOV_SOURCE_MODE`, which is
documented to re-read fresh per call so a YAML edit takes effect on the
next tick, not the next deploy. Fixed immediately: `_sweep_group()` now
calls `get_sweep_after_hours()` fresh each tick. 715 tests still pass.
Left the value at `true` (bypass market-hours filter) since that's the
real pre-migration production setting, not a new decision to make here.

**P/L methodology — three inconsistent conventions found, none fixed.**
Full trace of every dollar-P/L computation in the codebase: (1)
`nt_signal_sim.pnl_dollars` (`eod_resolver.compute_pnl`) =
`pnl_bricks * brick_size`, with **no** qty/lot/contract-size term at
all — a notional "1 unit of the underlying" figure, identical
convention for every symbol/asset class, NOT risk-normalized and NOT
comparable across symbols with different price scales (a gold/index
`brick_size` in absolute price terms dwarfs a forex pair's). This is
what feeds the new per-symbol scorecard below. (2)
`renko/risk_manager.py` (live executor / backtest engine) =
`pnl_bricks * brick_size * qty`, with `qty` from real equity-based Kelly
sizing (`position_usd = equity * min(effective_kelly, 0.10)`). (3)
`sweep_orchestrator/paper_book.py` ("paper book", `nt_paper_positions`)
= `pnl_bricks * brick_size * size_units`, with its own, different,
$1000-base-notional Kelly sizing model. `heavy_sweep.py`'s actual
sweep-selection Sharpe scoring never converts to dollars at all — it
stays in brick units. No `DOLLAR_PER_BRICK`/`LOT_SIZE`/`CONTRACT_SIZE`
constant exists anywhere. Not fixed — added to `nt_roadmap` as
`pnl_methodology_three_conventions`; this needs a real decision (keep
the 1-unit notional convention and label it clearly everywhere, or
recompute on an equal-risk/paper_book-style basis for cross-symbol
comparability), not a silent pick.

**Per-symbol W/L + P/L scorecard, promoted to a live webapp view.**
Turned yesterday's one-off table into `v_symbol_signal_scorecard`
(migration 138): `symbol | qualified | resolved | win | loss | expired |
win_pct | expired_pct | pnl_notional_1unit | last_qualified_ts`, grouped
from `nt_signal_sim`, expired excluded from win/loss/PnL (per the
existing `#3` decision), granted `SELECT` to `anon`/`authenticated` so
the webapp can query it directly. In the process found
`v_qualified_signal_detail` (the earlier per-signal detail view) had
**no** `anon`/`authenticated` grant at all since it was created —
the webapp could never have read it; fixed in migration 139. Both are
now live in Supabase (view-only changes, no Studio code deploy
required). Follow-up (same day): the base view is an all-time lifetime
aggregate, not useful on its own for "how are we doing lately" or
trend-over-time. Added `v_symbol_signal_scorecard_7d` (rolling 7-day
window) and `v_symbol_signal_scorecard_monthly` (one row per calendar
month × symbol), same columns/caveats, migration 140.

## `sweep_after_hours` vs. cron scheduling — confirmed not redundant

> **Priority**: P3 · **Task**: Confirm `sweep_after_hours`'s in-tick
> market-hours filter is not redundant with APScheduler's own cron ·
> **Status**: n/a (pure investigation, confirms existing design) ·
> **Date**: 2026-09-08

User asked whether APScheduler's own cron trigger already handles
market hours, making the `sweep_after_hours` in-tick filter redundant.
Traced definitively: no. Every symbol's `regime_cron` is a flat,
hours-agnostic expression (`*/5 * * * *` for light sweeps, `0 20 * * 0`
for the weekly heavy sweep) applied identically across asset classes —
`SweepOrchestrator._refresh_symbols()` groups symbols purely by matching
cron STRING, so stocks/forex/commodities/crypto sharing a cron string
land in the same job with the same broad-firing schedule
(`orchestrator.py:1191-1198`, `supabase/migrations/028_add_tradingview_datasource.sql`).
`is_market_open()`/`sweep_after_hours`, called only inside `_sweep_group()`
at tick-time, is the *only* place that discriminates per-symbol market
hours. The cron layer structurally cannot express this (one job's
symbol list spans multiple sessions) — not redundant, doing necessary
work the schedule itself doesn't do.

## USDCHF calibration governor — reclassified: not a design gap, a poisoned-sample problem

> **Priority**: P1 · **Task**: Reclassify the USDCHF calibration lock as
> a non-independent-sample poisoning problem in the bias-counting view,
> not a design gap · **Status**: pending ("pending an explicit decision
> on scope") · **Date**: 2026-09-08 · **Roadmap refs**: RM-22

Deeper dig (per user pushback: "not a design gap, it's a data issue
now — is there a way to correct or sanitize the data?") found the exact
shape of the poisoning. All 294 rows in the frozen bias sample are
**real, distinct, correctly-computed** trades (294 distinct `signal_id`s,
each with its own real entry/exit price) — this is NOT corrupted or
duplicated data in the row-integrity sense. The problem is
**non-independence**: entries run every ~5 minutes continuously from
2026-09-02 01:05 for roughly 24h straight, each with a *confidently
bullish* `predicted_p_win` (0.68–0.80), each held ~40 hours, and
**every single one** eventually hit its stop-loss — because it is the
same underlying multi-day adverse price move, re-entered on essentially
every sweep tick with no effective de-duplication, not 294 independent
trials of a bad model. `nt_eod_actions.detail.distinct_days=3` was
already a (currently inert) hint of exactly this — the system recorded
that n=294 spans only 3 calendar days but the enforcement logic never
acts on that signal, it only checks raw row count (`>=20`) against
`v_eod_calibration_bias_latest`.

**Why this isn't a quick SQL cleanup**: deleting or altering these 294
rows would mean editing real historical trade records to make a number
look better — the numbers in each row are correct, individually. The
actual defect is upstream, in how `v_eod_calibration_bias_latest`
counts "n" (raw row count instead of independent episodes) — and that
view also feeds `calibration_enforcement.fetch_historical_p_win()`,
which feeds the EV blend's `p_historical` source for **every** symbol,
not just USDCHF. Changing that counting methodology is a real, general
fix (e.g. capping counted samples to one per correction cycle, or
requiring a minimum number of distinct entry episodes rather than raw
rows) — but it touches the same calibration-bias pipeline that feeds
`compute_ev`, which is explicitly out of rework scope. Flagged, not
built, pending an explicit decision on scope. `SIGNAL_COOLDOWN_MINUTES`
(migrated to `feature_flags.yaml` earlier today, now confirmed correctly
sourced at 60 minutes) should prevent this exact "same signal every 5
minutes" pattern going forward regardless of whether it fixes the
already-poisoned historical window.

## Heavy sweep: weekly → daily, light/heavy mutex added (2026-09-08)

> **Priority**: P1 · **Task**: Change heavy-sweep cadence weekly→daily
> and add a light/heavy sweep mutex to prevent concurrent
> `brick_params.yaml` writes · **Status**: resolved (730 tests passing) ·
> **Date**: 2026-09-08 · **Roadmap refs**: RM-25

User pushback: doesn't APScheduler's own cron already handle market
hours, making `sweep_after_hours` redundant, and shouldn't heavy sweep
run daily given intraday triggers exist and the "TimesFM makes daily
unnecessary" assumption might not hold? Investigated both directly.

**`sweep_after_hours` — confirmed NOT redundant.** Every cron schedule
(`regime_cron='*/5 * * * *'`, `sweep_cron` weekly) is flat and
hours-agnostic, mixing asset classes in the same job by matching cron
STRING (`orchestrator.py::_refresh_symbols`). `is_market_open()` inside
`_sweep_group` is the only place that discriminates per-symbol market
hours — the cron layer structurally can't express it. Also found and
fixed a real bug from the same-day config migration: `SWEEP_AFTER_HOURS`
was read once at import instead of hot-reloading like its sibling
`MARKOV_SOURCE_MODE` — `_sweep_group()` now calls `get_sweep_after_hours()`
fresh each tick.

**Heavy sweep cadence — the "TimesFM makes daily unnecessary" assumption
does not hold, confirmed via direct code read.** `heavy_sweep.py`'s own
comment: *"Skip the heavy sweep entirely if TimesFM is already confident
… This negates the need for daily heavy sweeps."* That conflates two
different questions — TimesFM's confidence gate answers "is the
DIRECTIONAL forecast already confident," not "is the currently-selected
brick/SL/TP GEOMETRY still well-matched to this symbol's volatility." A
confident direction call says nothing about bracket-fit — real data
already showed EURUSD/USDCAD/USDJPY sitting at 82-94% expiry even under
the corrected deadline-aware scorer, i.e. exactly the failure mode a
stale weekly bracket produces, regardless of TimesFM confidence. The
mid-week safety net meant to catch this (`emergency_heavy_sweep`,
triggered off calibration bias) is also unreliable now that the bias
signal itself is known to run on 0.1-1.9% real independence (see below).
Real measured cost settles the compute question: `nt_sweep_result`'s
last 30 heavy-sweep durations average **28.8s**, worst case **93s** — a
full 14-symbol pass averages **~6.7 min**, worst-case **~22 min** serial.
Daily is trivial. Changed `nt_symbol.sweep_cron` from `0 20 * * sun` to
`0 20 * * *` for all 14 active symbols (migration 142), same time-of-day.

**Light/heavy overlap — closed.** Before this, `run_heavy_sweep_for_symbol`
never acquired the per-symbol `_sweep_locks` `asyncio.Lock` the light
sweep already uses to prevent re-entrant concurrent ticks — light and
heavy sweep for the same symbol could run fully concurrently, a rare
weekly coincidence that becomes a real daily one once cadence changes.
Fixed by wrapping the existing implementation: `run_heavy_sweep_for_symbol`
now acquires the shared lock (blocking — it's not latency-sensitive) and
delegates to `_run_heavy_sweep_for_symbol_locked` for the actual work;
light sweep's existing "skip if locked" behavior means a light tick
landing mid-heavy-sweep is simply skipped until the next 5-minute tick,
rather than racing on `config/brick_params.yaml`. No new lock type —
reused the existing mechanism. 730 tests passing after both changes.

## Calibration-bias counting fix — built (2026-09-08)

> **Priority**: P0 · **Task**: Replace the calendar-day clustering-
> discount proxy with real episode-based interval-merge counting for
> calibration bias (both `fetch_calibration_bias` and
> `v_eod_calibration_bias_latest`) · **Status**: resolved (verified live
> against production, 730 tests passing; resolves the USDCHF lockup) ·
> **Date**: 2026-09-08 · **Roadmap refs**: RM-22

User: "not a design gap, it's a data issue — is there a way to correct
or sanitize the data? Follow my intuition, it is not wrong." Built the
fix, and found it's bigger than USDCHF: there are actually TWO parallel,
independently-computed bias mechanisms sharing the same underlying flaw.

**Found first: an existing but insufficient prior fix.** `eod_correction.py`
doesn't read the SQL view analyzed in the prior turns at all — it calls
`fetch_calibration_bias`/`fetch_calibration_bias_raw` (`supabase_client.py`),
which already had a 2026-08-25 "clustering discount"
(`_clustering_discounted_n`) built for a near-identical prior incident
(USDCAD, `stagnation_kill_switch` fired off n=69 records from one 6-hour
burst). That discount used **distinct calendar days** as an independence
proxy — and USDCHF's burst evaded it by construction: it spanned exactly
3 calendar dates (Sept 2-4), which exactly equalled the discount's own
`EOD_BIAS_MIN_DISTINCT_DAYS=3` default, disabling the discount entirely
even though those "3 days" were one continuous ~68-hour episode.

**Second, separate mechanism, same flaw, never fixed at all:**
`v_eod_calibration_bias_latest` (the SQL view `calibration_enforcement.
fetch_historical_p_win()` reads for the EV blend's `p_historical` source)
had **zero** clustering protection — plain `COUNT(*)`. Checking it
directly (not assumed) showed the same systemic pattern across every
symbol, not just USDCHF: raw row counts in the hundreds to over a
thousand collapsing to single-digit real episodes (AAPL 1,329→7, XAUAUD
1,294→3, BTCUSD 946→1, USDJPY 1,162→6…) — a 0.1-1.9% real-independence
ratio platform-wide.

**Fix: real episode counting, replacing the calendar-day proxy
entirely.** New `_group_into_episodes` (`supabase_client.py`) merges
`nt_calibration_records` rows whose `[entered_at, closed_at]` windows
overlap in time into one independent episode — a proper interval-merge
using a **running max** of `closed_at` seen so far (not just the
immediately-preceding row: an earlier long-hold record can still overlap
a later record even if a short-hold record in between has already
closed — verified this distinction matters with a direct regression
test). Each episode collapses to one representative point (mean
predicted p_win, win fraction) before the final per-symbol aggregation
and recency-decay weighting (`_decay_weighted_episode_stats`, same decay
shape as before, now applied post-collapse). Applied to BOTH mechanisms:
`fetch_calibration_bias`/`_raw` (now returns `None` — the existing
"insufficient data" path — when episodes fall below
`calibration_bias_min_episodes`, instead of a discounted-but-still-fired
verdict) and `v_eod_calibration_bias_latest` (migration 143, same
episode-merge logic in SQL, `INSUFFICIENT_DATA` threshold changed from
"< 20 raw rows" to "< 2 episodes").

**New config**: `calibration_bias_min_episodes` (`feature_flags.yaml`,
default 2) — explicitly flagged as a conservative placeholder, not a
settled number. Real data at build time showed most symbols sitting at
only 1-7 independent episodes per 7-day window; this threshold directly
controls how often the OVERCONFIDENT/UNDERCONFIDENT enforcement can fire
at all. Retired the old `EOD_BIAS_MIN_DISTINCT_DAYS` setting and
`_clustering_discounted_n` entirely (dead once replaced) — the SQL
view's threshold is a hardcoded mirror since views can't read YAML,
flagged as a manual-sync point.

**Verified live, not just in tests.** USDCHF now reads `n=1 episode,
status=INSUFFICIENT_DATA` on both paths (was `n=294, OVERCONFIDENT`)
within minutes of shipping — `fetch_calibration_bias`/`_raw` return
`None` directly against production data, meaning the next `eod_correction`
cycle will not renew USDCHF's penalty, and the existing one ages out
within `ACTION_FRESH_WINDOW_HOURS` (8h) rather than waiting until the
7-day window fully drains (~2026-09-11). 9 new/updated tests (some
existing 2026-08-25 USDCAD-incident tests needed realistic `closed_at`
fixtures added, not just patched assertions), 730 total passing.

**10% expiry rate — set as the aspirational target, not enforced today.**
Confirms the per-symbol-baseline alert design direction (not a flat
threshold) — `expiry_rate_alert`'s `threshold_pct=50.0` stays as the
near-term operational floor; 10% is the bar to measure progress against
as daily heavy-sweep cadence and the deadline-aware scorer mature, given
several symbols structurally sit at 60-94% expiry today even under the
corrected scorer.
