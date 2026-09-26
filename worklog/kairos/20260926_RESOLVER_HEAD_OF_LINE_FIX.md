# Resolver head-of-line fix: decided TP/SL were not being written

> **Date:** 2026-09-26 · **Branch:** `kairos` → `main`
> **Flag:** `eod_resolver_scan_mode` (`full | head`), ships **`full`, which is live in the nightly on deploy**; `eod_resolver_page_pause_s: 2.0`
> **Related:** `20260926_HOURLY_CALIBRATION_RESOLVE.md` (its live mode calls the same function), `20260926_CALIBRATION_EVIDENCE_POOL.md`

---

## 1. What was wrong

`resolve_pending_signals` (nightly 01:17 UTC; also the hourly resolve in live mode) reads the pending queue
(`nt_signal_sim` where `outcome IS NULL`) **oldest-first**, 1,000 rows per batch. It stopped at the first
batch that resolved nothing. That guard came from the 2026-08-23 429 incident, where the loop re-fetched the
same rows.

At any given time, the oldest ~1,000 pending signals are mostly still inside their 48-bar window, so they
can't be judged yet. The queue ages by entry time, not by when the signal was decided. A signal that hits SL
3h after entry sits **behind** hundreds of older signals that haven't been decided yet. The resolver never got
that far, so the decided signal waited until the head expired, often more than a day later.

The worst part is that **the missed outcomes are mostly losses**. A stop-loss is usually hit sooner than a
take-profit, because live brackets sit about 9 ATR away for SL and about 17 ATR for TP. Most of the
early-decided signals behind the head are therefore SL. Each nightly under-wrote losses, and calibration
evidence leaned toward wins.

## 2. Evidence (2026-09-26)

| Check | Result |
|---|---|
| 00:38 hourly shadow (read-only, whole queue) | 12 TP / 83 SL already decided |
| 01:17 nightly (actual writes) | 15 TP / **21 SL** |
| Dry run right after the nightly, 01:25 | **72 SL still decided and unwritten**, all at queue positions **1,024–1,893** (0 inside the first 1,000) |
| Nightly log | `batch made no forward progress … stopping`, 2,830 insufficient_bars |
| Replay on the live queue, 03:11: old code | 7 TP / 16 SL written; 5 batches re-judging the same head (4,904 judgements), 22.5s |
| Replay on the live queue, 03:11: new code | **8 TP / 110 SL** written (suppressed 15, qualified 2); 3 batches, each row judged once (2,073), 11.9s |

Where the nightly did reach, it matched the shadow (unknown tier: 12 SL = 12 SL).

**Downstream effect:** AAPL alone had 21 of the unwritten SL. Its all-tier verdict that night was
*underconfident* (−0.25), and the 4h correction passes enforce that verdict. It is at least partly an
artifact of missing losses.

The old code also wasted work. Whenever a batch resolved a few rows (expiries at the head), it re-read from
offset 0 and re-judged the same young head, fetching candles again each time.

## 3. Fix

`eod_resolver.resolve_pending_signals`:

- **`full` (default).** Page with an offset that counts only the rows this run has seen and left pending.
  Rows that reached a terminal outcome have left the `outcome IS NULL` pool, so they don't count. Scan to the
  end of the queue, and stop when a short batch makes no progress. Each row is judged once per run. The loop
  always finishes (the offset only grows while rows stay pending), and `max_total` (12,000) still caps it.
- **`eod_resolver_page_pause_s` (2.0s)** between batches. Each batch is one batched TradingView fetch
  (≤ 2 HTTP requests for 14 symbols). With about 2,300 pending signals that's 3 batches, about 4s of pauses.
- **`head`**: the old behaviour, kept for rollback only.

Unchanged: the verdict function `_judge` (shared with the dry run), window anchoring, the minimum-age gate,
the 48-bar expiry, the PATCH and calibration writers, the resolver lock, the schedule, and the bias formula.

**The hourly resolve is now consistent.** Its shadow dry run already scanned the whole queue, while live
(`resolve_pending_signals`) would have stopped at the head. After this fix, shadow numbers match what live
would write. The parity gap in §2 was this bug, not a disagreement between shadow and live.

## 4. Expected change after deploy

- **Next nightly:** a one-time catch-up of already-decided outcomes, mostly SL; about 110 TP/SL at 03:11,
  against about 23 under the old code. After that, each nightly writes everything decided by then.
- **Calibration and bias:** the loss share in `nt_calibration_records` goes up. Biases move toward
  *overconfident* where losses were missing (AAPL in particular). The 4h correction passes and the drift
  monitor act on the corrected pool. This is a correction to the evidence, not a new knob.
- **Runtime:** about 12s against about 22s, with fewer candle fetches.

## 5. Tests

`tests/test_resolver_head_of_line.py`, 21 tests. It uses a fake PostgREST pool where a terminal PATCH removes
the row and the offset pages through what remains.

- `head` reproduces the bug.
- `full` reaches decided rows behind the head, writes their calibration records, and judges each row once.
- Offset sequence `[0, 3, 6, 6]`. It stops on an all-young queue and on an exact multiple of the limit.
- An all-decided queue drains at offset 0, and `max_total` still caps.
- Pause between batches, never before the first. Flag getters and the shipped YAML.

The existing no-progress and multi-batch tests in `tests/test_eod_resolver.py` pass unchanged.

Full suite: **1,297 passed**; only the 2 known `test_hmm_batch_consolidation` failures remain.

## 6. Verify after deploy / rollback

```bash
ssh noble-trader-unified 'cd /teamspace/studios/this_studio/noble-trader;
  grep -a "eod_resolver: resolved\|reached end of pending queue\|429" logs/orchestrator.log | tail -5'
```

Pass criteria for the next nightly:
- `scan_mode: full`;
- TP/SL written ≈ the preceding :38 shadow's count;
- no 429s;
- runtime < 60s.

Then re-read the `[eod:pool]` lines, since the all-tier biases should shift.

**Rollback:** set `eod_resolver_scan_mode: head` (read every run, no restart needed).
