# 2026-08-24 — Pending corrective actions: verify after tomorrow's 00:30 UTC EOD run

**Purpose:** checklist for the next session. The batched-candle-fetch fix
(`ef64aa7`, see `20260824_EOD_RESOLVER_BATCHED_CANDLE_FETCH.md`) deployed
live at 05:23:57 UTC today (clean restart confirmed, no errors) but is not
exercisable until `resolve_pending_signals` runs again — that's once daily
at **00:30 UTC**, so first real signal is the **2026-08-25 00:30 UTC** run.
Nothing below can be checked before then.

---

## UPDATE — 00:56 UTC 2026-08-25: deployment gap closed, both items confirmed live

Studio restarted; deployed HEAD is now `fe6c598` (includes `62f54d7`).
Verified directly against the deployed source on disk, not just git log:
- `orchestrator.py`: `eod-lookback` trigger is `CronTrigger.from_crontab("32 0 * * *")`.
- `price_fetcher.py`: `MAX_429_RETRIES=1`, `RETRY_AFTER_CAP_SECONDS=10.0` present
  and wired into the retry loop.

Clean restart, no startup errors. **Both the 429-retry and cron-offset
items are done — nothing left to build.** Neither gets a real live
exercise until tomorrow's 00:32 UTC resolver run (today's already ran at
the old 00:30 tick, before this restart, and succeeded on batching alone
— see the update below). Next actionable checkpoint remains the 04:30 UTC
intraday correction cycle for the kill-switch question.

---

## UPDATE — 00:50 UTC 2026-08-25: the 00:30 UTC run happened — batching fix alone was enough

The daily `eod-lookback` run fired on schedule and resolved cleanly:
```
eod_resolver: resolved 590 pending in 6 batch(es) ({'expired': 148, 'sl_hit': 442}), errors=0
```
Zero 429s, zero errors — **the batching fix (`ef64aa7`) alone was sufficient**,
even though it ran at the *original* `00:30` tick, still colliding head-on
with the light sweep's own batch call at the same minute.

**Deployment gap found**: the Studio's deployed `git log -1` is still
`ef64aa7` — the retry-with-backoff + cron-offset commit (`62f54d7`) was
never pushed out to production. It exists on `origin/main`, tested, but
not live. This run's success shows it wasn't *strictly* required tonight,
but it's still the extra safety margin (a busier symbol roster, an API
having a worse night, etc. could still trip 429s that batching alone
doesn't fully rule out) and should be deployed at the next restart.

**Bias movement for the three kill-switched symbols — mixed, with a
formula caveat**:

| Symbol | n before→after | bias before→after | Read |
|---|---|---|---|
| AAPL | 427→545 (+118) | 0.4466→0.4714 | Fresh calibration evidence arrived |
| US500 | 63→63 (+0) | 0.6771→0.7565 | **No new evidence.** All of US500's resolved signals tonight landed as `expired` (excluded from calibration by design — a timeout is neither win nor loss). The bias number still changed because tonight's write comes from the daily lookback's correction pass, which computes bias via `avg_pred` (enforced), while the 4h intraday cycles use `avg_pred_raw` (pre-enforcement) — same 63 records, different formula, not real movement. **New candidate finding**: why do 100% of US500's resolved signals expire rather than hit TP/SL? Possibly its SL/TP distances are miscalibrated relative to its typical 1h move size. Not investigated — flagging only. |
| XAGUSD | 711→764 (+53) | 0.3739→0.3714 | Fresh evidence arrived, bias barely moved (0.0025) — leans toward genuinely persistent miscalibration for this symbol specifically, not just stale/frozen data |

**Kill switch status: unchanged.** Stagnation streak/tier tracking only
runs in the 4h intraday correction cycles (`raw_bias_mode=True`) — the
daily lookback's correction pass doesn't touch it. AAPL/US500/XAGUSD
remain at tier 2 / kelly=0.0 right now. **The real test is the next
intraday cycle at 04:30 UTC** (~3h40m out from 00:50 UTC): it will read
tonight's newly-written `bayesian_penalty` row (source=`eod_correction`)
as the most recent history entry for the streak calculation — since that
row's bias value differs from the frozen streak value (different formula,
see table above), the streak may reset rather than cleanly extend to 6.
Genuinely unpredictable from here; needs a live check at/after 04:30 UTC.

---

## UPDATE — 20:34 UTC 2026-08-24: kill switch fired for AAPL/US500/XAGUSD

12h calibration analysis (08:34→20:34 UTC) found the situation escalated
past "frozen bias" while this checklist was open:

- `stagnation_streak` climbed 3→4→5 across the 12:30 / 16:30 / 20:30 UTC
  intraday correction cycles for all three symbols, bias identical
  (bit-for-bit) every cycle: AAPL 0.4466, US500 0.6771, XAGUSD 0.3739.
  XAGUSD is new to this list (`commodities`, not `stocks` — confirms the
  problem was never asset-class-specific).
- At streak=5 (≥ `STAGNATION_TIER2_CYCLES=4`), all three hit
  `stagnation_kill_switch` at the 20:30 UTC cycle — **kelly forced to 0.0,
  publishing blocked entirely** (`eod_correction.py:527-539`). This is
  Finding 1's source-tagging fix working correctly: it detected a genuine,
  real 5-cycle stagnation and escalated as designed.
- Root cause unchanged from this morning: `n` (calibration record count) is
  identical to hours ago for all three (AAPL 427, US500 63, XAGUSD 711) —
  zero fresh resolved outcomes all day. Confirmed fleet-wide: **zero
  signals resolved, any symbol, in the entire 12h window** — expected,
  since `resolve_pending_signals` only runs once/day and this window falls
  entirely between runs.
- **Timing correction to this checklist's original framing**: the
  meaningful test isn't "tomorrow" — it's **tonight's 00:32 UTC run**
  (~4h out from 20:34 UTC), the first live exercise of both this fix and
  `20260824_RESOLVER_429_RETRY_AND_CRON_OFFSET.md`'s two fixes together.
- Escalation path if tonight's run does NOT get fresh data flowing: streak
  6 @ 04:30, 7 @ 08:30, then `STAGNATION_RESET_CYCLES=8` @ ~12:30 UTC
  2026-08-25 forces a tier reset + fresh emergency sweep automatically —
  no manual intervention required either way.

### Assessment: is a manual backlog drain required right now?

**No.** Recommend waiting for the natural 00:32 UTC cron rather than
manually invoking `resolve_pending_signals` (or the drain script) ahead of
it. Reasoning:

1. **No time actually saved.** `run_eod_correction` (the pass that reads
   calibration bias and could lift the kill switch) only runs on the fixed
   4h-boundary schedule (`eod-correction-intraday-{04,08,12,16,20}` +
   `eod-lookback` at 00:32) — it is not triggered by the resolver finishing.
   Manually draining now would populate fresh `nt_calibration_records` rows
   sooner, but nothing re-reads them until the next scheduled correction
   pass anyway, which is the **same 00:32 UTC run** either way. A manual
   drain buys zero clock time here unless `run_eod_correction` were also
   invoked manually right after — a bigger, less-tested intervention than
   just letting the cron fire.
2. **No urgency.** The kill switch is doing exactly what it's designed to
   do — safely idling 3 already-poorly-performing symbols rather than
   trading on stale/frozen p_win estimates. Nothing gets worse by waiting
   4 hours; the reset-at-streak-8 safety net fires automatically regardless.
3. **Risk asymmetry.** The last manual drain (2026-08-23) caused a real
   production incident (runaway resolver loop, TradingView 429 storm,
   required an SSH kill). The fixes from that incident plus today's
   batching/retry/cron-offset changes have never been exercised via the
   real cron path yet — letting the actual scheduled job run them for the
   first time is a lower-risk, more representative test than another
   ad-hoc manual invocation, especially with zero time benefit to justify
   the risk.

**If tonight's 00:32 UTC run also fails to drain the backlog**, that would
be a stronger signal something in the fix is incomplete — worth a live
SSH check at that point, not a reason to intervene manually beforehand.

### Other corrective actions surfaced by the 12h analysis

- **Legacy Markov HMM NaN bug is wider than first spotted** (item 5 below
  originally named only AUDUSD/US500): confirmed over the past 12h across
  AUDUSD, BTCUSD, EURUSD, USDCAD, XAUAUD, XAUEUR, XAUUSD — 7+ symbols, most
  of the active roster. Still not investigated. Given the spread, this
  strengthens the case for treating Change B (retire this model) as higher
  priority once Change A's stability window closes, rather than patching
  the legacy model in place.
- **New candidate investigation, not yet scoped**: once the resolver
  backlog clears and AAPL/US500/XAGUSD get fresh calibration evidence, if
  bias *still* doesn't move toward CALIBRATED, that points to genuine
  miscalibration in the underlying p_win model for these three symbols
  specifically (not a plumbing bug) — a different, deeper investigation
  than anything scoped so far. Don't start this until the 00:32 UTC run's
  outcome is known (per item 3 below — this is exactly the fork item 3
  already anticipates).
- Change B stability window: still not started, still gated on Change A's
  observation period (item 6 below) — no change to that status.

---

## 1. Confirm the batching fix actually eliminated the 429 collision

Check `logs/orchestrator.log` around 2026-08-25 00:30 UTC for `regime.sweep.prices`
lines from the resolver's candle fetch:

- **Expect:** one (or `ceil(N_symbols/10)`) `TradingView batch complete: N/N
  symbols successful` line from the resolver, not N separate single-symbol
  `1/1 successful` lines back-to-back.
- **Expect:** no `429 Too Many Requests` during the resolver's fetch window.
- If 429s still occur despite batching (e.g. the light sweep's own 2-chunk
  batch plus the resolver's now-1-2-chunk batch still exceed the per-second
  limit), that confirms the collision is scheduling-driven, not
  call-count-driven — see item 4 below.

## 2. Confirm AAPL/US500/US30USD's backlog actually drains

Query (same as used to diagnose this):
```sql
select symbol, coalesce(outcome,'PENDING') as outcome, count(*),
       min(ts) as oldest, max(resolved_at) as last_resolved
from nt_signal_sim
where symbol in ('AAPL','US500','US30USD')
group by symbol, coalesce(outcome,'PENDING')
order by symbol, outcome;
```
- **Expect:** the ~300-row PENDING pile per symbol drops sharply as signals
  from ~2026-08-23 00:38-00:40 UTC (now past their 48h expiry) resolve to
  `tp_hit`/`sl_hit`/`expired`.
- Also re-run the overdue check to confirm nothing is *actually* stuck past
  48h (this returned zero rows on 08-24 — should stay zero):
```sql
select symbol, count(*) from nt_signal_sim
where outcome is null and ts < now() - interval '48 hours'
group by symbol;
```

## 3. Check whether AAPL/US500 calibration bias actually moves

This is the real open question from the original audit — today's fix removes
one *confirmed* source of lost resolution attempts, but that isn't proven to
be the full explanation for the frozen OVERCONFIDENT bias. After the 00:30
run:
```sql
select symbol, action, bias, ts, detail->>'source' as source
from nt_eod_actions
where symbol in ('AAPL','US500') and action='bayesian_penalty'
order by ts desc limit 10;
```
- If bias starts moving off 0.4464/0.6771-ish → the resolver bug likely was
  the (or a major) cause. Close this thread.
- If bias stays frozen even with a fresh drain of resolved outcomes → the
  cause is elsewhere (e.g. genuinely persistent miscalibration in the
  underlying p_win model for these two symbols) and needs fresh
  investigation — don't assume today's fix explains it without this check.

## 4. Retry-with-backoff + cron offset — DONE (commit `62f54d7`, same day)

Both items originally deferred here were built later on 2026-08-24 — see
`20260824_RESOLVER_429_RETRY_AND_CRON_OFFSET.md`:

- `fetch_tradingview_batch` now retries once on a 429, honoring (capped)
  `Retry-After` before giving up, instead of skipping the chunk outright.
- `eod-lookback` moved from `"30 0 * * *"` to `"32 0 * * *"` — no longer
  shares the light sweep's `*/5 * * * *` grid tick.

Both deployed live — confirm as part of item 1's log check above: the
resolver's own job should now start at `00:32:xx` UTC, not `00:30:xx`, and
any 429 that does occur should show a retry attempt line before either
succeeding or exhausting (`MAX_429_RETRIES=1`, i.e. 2 total attempts) rather
than skipping immediately.

## 5. New live bug spotted, unrelated to anything fixed today

```
regime.hmm.model: hmm.emit: AUDUSD/US500 — predict failed
  (ValueError: startprob_ must sum to 1 (got nan)), using FLAT
```
Recurring across multiple sweep ticks for AUDUSD and US500 — the legacy
5-state Markov HMM (`regime_platform/services/hmm/model.py`, the model
Change B's approved plan already targets for retirement) is producing NaN
start/transition probabilities and silently falling back to FLAT regime.
Not investigated yet. Worth determining:
- Is this the same root cause Change A fixed in `RegimeHMM` (unstandardized
  feature scale → covariance collapse → NaN), just manifesting in the
  *other* HMM that Change A didn't touch?
- Does it justify accelerating Change B (retire this model entirely) rather
  than patching it, given the plan already flagged this model as the more
  severely broken of the two (confirmed variance-ratio blowups up to
  18,000x for BTCUSD features)?

## 6. Change B stability-window check-in

Change A (`RegimeHMM` feature standardization, commit `5bf94b0`) has been
live since ~00:55 UTC 2026-08-24. The approved plan
(`.claude/plans/goofy-rolling-swing.md`) gates Change B on "a day or two" of
clean logs. By 2026-08-25 00:30 UTC that's ~24h — check convergence-warning
frequency (`hmmlearn.base: Model is not converging`) has actually dropped
for the RegimeHMM-driven symbols before considering Change B ready to start.
