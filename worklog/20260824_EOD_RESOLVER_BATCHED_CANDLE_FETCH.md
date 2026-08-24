# 2026-08-24 — EOD resolver: batch candle fetch instead of one HTTP call per symbol

**Scope:** `regime_platform/sweep_orchestrator/eod_resolver.py` only.

**Status:** Fixed, tested (2 new regression tests, revert-and-confirm verified), full
`test_eod_resolver.py` suite green (32/32). Not yet deployed.

---

## Problem

Investigated why AAPL/US500's calibration bias has stayed frozen OVERCONFIDENT —
initial hypothesis (asset_class=stocks market-hours gap) didn't hold up: US30USD
shares `asset_class='stocks'` and showed the identical PENDING-accumulation
pattern, but was NOT among the symbols that failed candle fetch on the day in
question. Checking live logs for the 2026-08-24 00:30 UTC `resolve_pending_signals`
run found the real cause:

`resolve_pending_signals` looped over pending signals grouped by symbol and called
`fetch_candles_for_symbols` **once per symbol** — N sequential single-symbol HTTP
requests with no delay between them. This ran concurrently with the light sweep's
own (correctly-batched, 10-symbols-per-request) TradingView call at the same
`00:30:00` cron tick. Confirmed live:

```
00:30:00,959  US500 candle fetch: 1/1 successful
00:30:00,994  USDCHF candle fetch: HTTP 429 Too Many Requests
00:30:01,025  USDJPY candle fetch: HTTP 429 Too Many Requests
00:30:01,063  XAUEUR candle fetch: HTTP 429 Too Many Requests
00:30:01,088  AAPL candle fetch: HTTP 429 Too Many Requests
```

Four consecutive single-symbol requests all rate-limited within ~100ms of each
other. The resolver treats a 429-emptied response identically to "no data exists"
— it marked all of those symbols' pending signals as `errors` for the run, and the
2026-08-23 no-forward-progress guard correctly stopped the batch (0 resolved),
which means those symbols got **zero** resolution attempts that day. Not an
asset-class issue, not a real data gap — self-inflicted rate limiting from firing
too many small requests instead of one batched one.

(Separately confirmed via direct query that nothing is actually stuck: no
`nt_signal_sim` row anywhere is pending past its 48h expiry window. The frozen
AAPL/US500 bias is a real, separate question from this bug — this fix removes one
source of lost resolution attempts, not a proven full explanation.)

## Fix

Replaced the per-symbol loop's HTTP call with a single batched call for every
symbol in the current `resolve_pending_signals` batch — `fetch_candles_for_symbols`
already accepts a list and auto-chunks into `ceil(N/10)` requests per TradingView's
batch limit (`price_fetcher.BATCH_CHUNK_SIZE`), exactly the pattern the light sweep
already uses. Per-symbol failure handling (missing `tradingview_symbol`, empty
response for one symbol within the batch) is preserved — a partial batch failure
only costs that symbol's signals, not the whole batch.

## Verification

Two new regression tests in `TestResolvePendingSignalsBatchedCandleFetch`:
- `test_fetches_candles_once_for_all_symbols_not_once_per_symbol` — 3 symbols in
  one pending batch, asserts `fetch_candles_for_symbols` is called exactly once
  with all 3 symbols, not 3 times.
- `test_one_symbol_missing_from_batch_response_does_not_block_others` — one
  symbol absent from the batched response still resolves the other symbol
  normally (1 call, not 2).

Both reverted against the old per-symbol-loop code and confirmed to genuinely
fail (3 calls / 2 calls respectively) before restoring the fix.

Full suite: local Windows checkout has 16 pre-existing collection errors
(missing `hmmlearn` and other deps not installed locally, present on the Studio)
and 95 pre-existing failures — confirmed via `git stash` comparison these are
identical on `main` before this change (299 passed) and after (301 passed, the
2 new tests only). `test_eod_resolver.py` alone: 32/32 passed.

## Not done in this pass

Two other angles flagged during the investigation, not built here (scope
discipline — one fix at a time):
1. `fetch_tradingview_batch` parses the 429 `Retry-After` header but never
   uses it — just logs and skips the chunk. A retry-with-backoff would recover
   from a 429 within the same run instead of losing that day's attempt entirely.
2. The `eod-lookback` cron (`resolve_pending_signals`) and the light sweep's own
   `00:30 UTC` tick are scheduled at the same wall-clock minute, which is what
   creates the concurrent-API-pressure window in the first place. Offsetting
   the resolver by even a minute would reduce collision likelihood independent
   of the batching fix.
