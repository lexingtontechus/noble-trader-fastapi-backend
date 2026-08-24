# 2026-08-24 — TradingView 429 retry-with-backoff + eod-lookback cron offset

**Scope:** `regime_platform/sweep_orchestrator/price_fetcher.py`,
`regime_platform/sweep_orchestrator/orchestrator.py`. These are the two
deferred items from `20260824_EOD_RESOLVER_BATCHED_CANDLE_FETCH.md` — built
now since they're independent of the other open items (AUDUSD/US500 NaN
startprob bug, the still-open AAPL/US500 bias question, Change B's gating).

**Status:** Fixed, tested (revert-and-confirm verified), full suite
unaffected. Not yet deployed.

---

## Fix 1 — retry 429s with backoff instead of skipping outright

`fetch_tradingview_batch` (`price_fetcher.py`) already parsed the `Retry-After`
header on a 429 but only used it to log a message before giving up on the
chunk — the actual wait never happened. Combined with today's earlier
incident (4 single-symbol resolver requests all 429'd within ~100ms), a
short wait would very likely have let a retry succeed within the same run.

Added one bounded retry per chunk (`MAX_429_RETRIES = 1`): on a 429, sleep
`min(Retry-After, RETRY_AFTER_CAP_SECONDS=10.0)` and retry once; if the
retry also 429s, give up exactly like before (log + skip, symbol absent
from the result). The cap prevents a large/misconfigured `Retry-After` from
stalling a sweep tick.

Applies to every caller of `fetch_tradingview_batch` /
`fetch_candles_for_symbols` (light sweep, heavy sweep, eod_resolver) — not
resolver-specific, since the underlying rate-limit risk isn't either.

## Fix 2 — offset eod-lookback off the light sweep's cron grid

`eod-lookback` (`resolve_pending_signals` + `run_eod_correction`) was
scheduled `"30 0 * * *"` (00:30 UTC daily) — exactly on the light sweep's
`*/5 * * * *` grid, guaranteeing both jobs fire their own TradingView batch
calls at the same wall-clock minute every single day. Moved to `"32 0 * * *"`
(00:32 UTC) — 2 minutes clear of the light sweep's :30 tick and 3 minutes
before its :35 tick. This reduces collision likelihood; it doesn't eliminate
it outright (a slow resolver run spanning several minutes could still
overlap the next light-sweep tick), which is now much less likely given
fix #1 in `20260824_EOD_RESOLVER_BATCHED_CANDLE_FETCH.md` cut the
resolver's own request count from N (one per symbol) to ~2 batched calls.

## Verification

**Fix 1** — 3 new tests in `TestBatch429RetryWithBackoff`
(`tests/test_price_fetcher_order.py`):
- `test_429_then_success_retries_and_recovers` — 429 then 200 recovers,
  sleeps for the parsed `Retry-After` value.
- `test_429_retry_after_is_capped_not_taken_verbatim` — a 600s `Retry-After`
  is capped to `RETRY_AFTER_CAP_SECONDS` (10s), not honored verbatim.
- `test_429_exhausts_retries_and_skips_chunk_like_before` — two consecutive
  429s give up cleanly (`MAX_429_RETRIES + 1` = 2 total attempts), matching
  the old skip-and-continue behavior, no crash.

Reverted the fix and confirmed the new tests genuinely fail against the old
code (ImportError on the new `MAX_429_RETRIES`/`RETRY_AFTER_CAP_SECONDS`
constants — a stronger signal than a plain assertion failure, since the
tests can't even collect without the new code). Restored and confirmed
7/7 pass in `test_price_fetcher_order.py` (4 pre-existing + 3 new).

**Fix 2** — config-only cron string change; no test asserts the literal
schedule string, verified by inspection (`_reschedule_groups`'s own
`*/5 * * * *` light-sweep grid vs the new `32 0 * * *` — 2-minute clearance
confirmed by reading both trigger definitions side by side).

**Local environment note**: `pytest-asyncio` (declared in `pyproject.toml`,
`>=0.23.0`) was not actually installed in this local Windows checkout,
silently breaking every `@pytest.mark.asyncio` test in the suite (they were
failing with "async def functions are not natively supported" rather than
running) — this affected unrelated pre-existing tests too, not something
this session introduced. Installed it locally (`pip install
"pytest-asyncio>=0.23.0"`) to actually exercise these tests directly rather
than trust file-pattern-matching alone. Before: 95 failed / 301 passed on
`main`. After installing the plugin (still on `main`, before this session's
fixes): 37 failed / 362 passed — the extra passes are previously-silent
async tests now actually running; the 37 remaining failures (16 collection
errors from missing `hmmlearn` + 3 `test_stagnation_breaker.py` failures
from an unrelated module lazy-import/test-isolation quirk +
`test_regime_stability.py` et al.) are pre-existing on `main`, confirmed via
`git stash` comparison, and out of scope for this fix.

## Not done in this pass

- Investigating/installing `hmmlearn` and other missing local deps — the
  Studio remains the source of truth for full-suite verification; fixing
  the local dev environment further is out of scope here.
- The `test_stagnation_breaker.py` lazy-import failure — pre-existing,
  unrelated to anything touched this session, not investigated.
- AUDUSD/US500 Markov HMM NaN startprob bug, AAPL/US500 bias root cause,
  Change B — all still open per `20260824_PENDING_ACTIONS_POST_EOD_RUN.md`.
