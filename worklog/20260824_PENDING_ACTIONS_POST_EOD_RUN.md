# 2026-08-24 — Pending corrective actions: verify after tomorrow's 00:30 UTC EOD run

**Purpose:** checklist for the next session. The batched-candle-fetch fix
(`ef64aa7`, see `20260824_EOD_RESOLVER_BATCHED_CANDLE_FETCH.md`) deployed
live at 05:23:57 UTC today (clean restart confirmed, no errors) but is not
exercisable until `resolve_pending_signals` runs again — that's once daily
at **00:30 UTC**, so first real signal is the **2026-08-25 00:30 UTC** run.
Nothing below can be checked before then.

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
