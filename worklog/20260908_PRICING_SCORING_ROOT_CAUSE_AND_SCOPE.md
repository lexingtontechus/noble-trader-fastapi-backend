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

All 59,913 expired `nt_signal_sim` rows are within TDVA's ~208-day
history depth (earliest 2026-08-06, latest 2026-09-06 — zero rows beyond
recoverable range, checked directly). Deleting them destroys recoverable,
real data. Correct action: re-fetch the real historical price at each
row's 48-hour mark and re-run the fixed `resolve_signal_outcome`/
`compute_pnl` against it, patching `exit_price`/`pnl_bricks`/`pnl_dollars`
in place — same rows, same signal_id, real numbers instead of fiction.

**Not yet built.** Next concrete step.

## Build notes (2026-09-08, later same day)

**Priority 1 (historical remediation) — done.** `scripts/backfill_expired_mark_to_market.py`: fetched real TDVA history per symbol, recomputed `exit_price`/`pnl_bricks`/`pnl_dollars` for all 59,913 contaminated rows via the fixed `compute_pnl`. 59,912 patched cleanly; 1 hit a transient Cloudflare 502 and was retried individually. **Verified: 0 of 59,913 `outcome='expired'` rows have `exit_price IS NULL` post-run** (was 59,913 of 59,913 before). Real historical data, not deletion — nothing was discarded.

**Priority 2 (deadline-aware scoring in production) — built, not deployed.** `heavy_sweep_deadline_aware_scoring_mode` (`off|shadow|live`, default `off`) added to `config/feature_flags.yaml`. `ComboResult` gains `deadline_aware_sharpe` (always computed — cheap, reuses the trade list already in hand); `EXPIRY_BARS` imported directly from `eod_resolver` (not duplicated, no circular import — confirmed before importing). New pure functions `deadline_aware_sharpe()` and `select_best_combo_deadline_aware()` (same shape/discipline as Phase A's `select_best_combo_with_atr_anchoring` — off is byte-identical to before, shadow logs+persists without changing the real pick, live applies the corrected pick). Wired into `run_heavy_sweep_for_symbol` right after the (now-deprioritized) ATR-anchoring block, operating on the same `grid_results`. New shadow table `nt_heavy_sweep_deadline_aware_shadow` (migration 136, applied) + `record_heavy_sweep_deadline_aware_shadow()`, mirroring the ATR-anchoring shadow table exactly. 14 new tests (`tests/test_heavy_sweep_deadline_aware_scoring.py`), full suite 714 passed / 0 failed / 21 pre-existing skips.

**Explicitly not done yet**: Studio deployment (nothing above has any live effect until this ships to the running orchestrator process); the multi-window historical validation this worklog said should substitute for a multi-week organic shadow wait (same method as `scripts/backfill_atr_anchored_bricks_shadow.py`, applied to scoring instead of candidate width); the threshold alert for expiry rate (Discord-based, not yet built); `nt_calibration_bias_by_bucket`/`nt_bracket_reachability` still need recomputing now that the underlying `nt_signal_sim` population has real expired PnL instead of fictional zeros.

## Build notes — roadmap infrastructure (2026-09-08, same day)

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
   mechanical, no design decisions pending, ready to build now.
2. **Root cause 1 in production** (`_renko_grid_search_sync`/
   `pick_best_combo` deadline-aware rescoring) — the highest-value fix,
   currently proven only in a standalone script.
3. **Threshold alert for expiry rate** — needs a numeric threshold
   decided, then reuses existing Discord alert plumbing.
4. **Studio deployment** of both eod_resolver.py fixes and (once built)
   the production scoring fix — nothing above has any live effect until
   this happens.
5. **USDCAD's directional miscalibration** — separate problem, not
   reachability or timing, not addressed by anything above. Needs its own
   investigation thread.

## Explicitly not in this scope

Regime labelling, calibration bias enforcement's classification logic,
`compute_ev`, Kelly sizing, the light-sweep qualification gate stack, and
`EXPIRY_BARS` itself (the 48h deadline stays exactly where it is — this
whole document is about scoring and recording against that fixed
deadline, not moving it).
