# Scope: Fix orphaned OPEN paper positions (M4)

**Repo:** noble-trader-fastapi-backend
**Status:** AUDITED — root cause identified, fix not yet built
**Priority:** Critical
**Reported:** 2026-08-15

## Problem

The noble-trader-admin dashboard shows **912 open positions** (up from 330 at the 2026-08-12 audit), with the oldest dating back to 2026-08-06. These positions are **orphaned** — their parent signals have been resolved (tp_hit/sl_hit/expired) but the positions remain `status='open'` and are never closed by the paper_book reconcile pass.

## Root Cause

The `paper_book.py` `reconcile()` function closes positions by matching:
1. **Open positions** fetched via `_fetch_open_positions()` — fetches up to `limit=2000` rows ordered `open_ts.asc`
2. **Resolved signals** fetched via `_fetch_resolved_signals(limit=2000)` — fetches up to `limit=2000` rows ordered `ts.desc`

The close pass builds a dict from open positions keyed by `signal_id`, then iterates resolved signals to find matches. **The bug is in the fetch window**:

- The signal capture rate is ~3,200 signals/day
- `_fetch_resolved_signals` fetches the **2000 newest** resolved signals (ordered `ts.desc`)
- After 9 days of trading, the 2000-row window only covers ~12–15 hours of history
- Positions opened on 08-06 have signals that were resolved on 08-13 — their `ts` (signal generation time) is 08-06, which is **far outside** the newest-2000-row window

**The 2026-08-08 fix** (changing from `ts.asc` to `ts.desc`, raising limit from 200 to 2000) fixed the case where *newly* resolved signals were missed, but it made the problem **worse** for *older* positions: `ts.asc` would have found the oldest resolved signals first (where orphaned positions cluster), while `ts.desc` now finds only the newest and never reaches 9-day-old signals.

## Current Mitigations That Exist But Are Insufficient

1. **`eod_retention.py` protection (lines 92–106):** Before deleting resolved `nt_signal_sim` rows older than 30 days, it fetches open positions' `signal_id`s (capped at `limit=1000`) and protects them from deletion. This prevents total orphans but **does not close** the positions — it only prevents signal deletion. The positions remain open indefinitely.

2. **`paper_book.py` age-expire pass (lines 476–515):** Force-expires positions older than `PAPER_OPEN_MAX_AGE_DAYS` (default 30). Since the oldest open positions are only 9 days old, this hasn't triggered yet.

3. **`paper_retroclose.py`:** A manual script to close positions after a fix. Exists but was never run.

## Fix Plan

### Required Changes (noble-trader-fastapi-backend only)

1. **Change `_fetch_resolved_signals` to fetch by `resolved_at` window, not `ts.desc` limit.**
   - Instead of `order=ts.desc&limit=2000`, fetch `resolved_at=gte.{recent_window}` (e.g., last 14 days) with no row limit (or a very high one).
   - This ensures all signals resolved in the recent window are available for the close pass, regardless of when they were originally generated.
   - The `ts.desc` ordering and `limit=2000` was a performance optimization that breaks for long-running open positions.

2. **OR: Change the close pass to iterate open positions (not resolved signals) and look up each signal individually.**
   - For each open position, fetch its signal via `signal_id=eq.{position.signal_id}`.
   - If the signal is resolved, close the position.
   - This is O(open_count) REST calls but is correct. With 912 open positions, it's ~1 batch request.
   - Better: batch the signal_ids in a single `in.(...)` query.

3. **Reduce `PAPER_OPEN_MAX_AGE_DAYS` to 7 (from 30).**
   - A signal that hasn't resolved in 7 days is likely orphaned.
   - The sweep TTL is 60 minutes for hot signals; 7 days is extremely generous.
   - This provides a safety net for any positions the close pass still misses.

### Files to Modify

| File | Line(s) | Change |
|------|---------|--------|
| `regime_platform/sweep_orchestrator/paper_book.py` | `_fetch_resolved_signals()` (258–289) | Change query to use `resolved_at` window instead of `ts.desc&limit=2000` |
| `regime_platform/sweep_orchestrator/paper_book.py` | `reconcile()` (417–522) | Change close pass to iterate open positions + batch-lookup their signals by signal_id, instead of iterating resolved signals |
| `regime_platform/sweep_orchestrator/paper_book.py` | `PAPER_OPEN_MAX_AGE_DAYS` (59) | Reduce from 30 to 7 |
| `regime_platform/sweep_orchestrator/paper_retroclose.py` | entire file (53 lines) | Run once manually after the fix to close the 912 backlog |

### Files to Review (already correct)

| File | Note |
|------|------|
| `regime_platform/sweep_orchestrator/eod_retention.py` | Protection logic is correct (skips open-position signal_ids) — keep as safeguard |
| `regime_platform/sweep_orchestrator/portfolio_compare.py` | Uses `pnl_dollars` not `realized_pnl` — unaffected |

## Verification

1. Run `paper_retroclose.py` manually — verify all 912 open positions are closed
2. Check `v_talaria_portfolio_stats` reflects the full closed trade set (should go from 93 → ~1000 trades)
3. Verify `v_paper_equity` curve includes the newly-closed positions' PnL
4. Confirm the next scheduled `reconcile()` run keeps `closed=0` open positions (prevents regression)
5. Run the full pytest suite

## Out of Scope

- Plugin repo changes (noble-trader-talaria, noble-trader-hermes-plugins) — the admin dashboard correctly reports the count; the bug is in the data layer
- Supabase schema changes (no FK constraint needed — the fix is in the Python reconcile logic)
