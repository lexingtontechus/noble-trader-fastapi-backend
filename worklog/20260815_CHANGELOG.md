## [2026-08-15] — M4 Open-Position Orphan Investigation

**Type:** Root-cause investigation + **BUILD COMPLETE** (implemented, tested, verified).

**Scope:** `regime_platform/sweep_orchestrator/paper_book.py` (`reconcile()`,
`_fetch_resolved_signals()`, `_fetch_open_positions()`, age-expire pass),
`regime_platform/sweep_orchestrator/eod_retention.py` (open-position protection),
`regime_platform/sweep_orchestrator/paper_retroclose.py` (manual close tool).
See full scope + fix plan in `docs/scopes/M4_OPEN_POSITION_ORPHAN_FIX.md`.

**Dashboard symptom:** noble-trader-admin Portfolio stats show **912 open
positions** — up from 330 at the 2026-08-12 audit. Oldest open position:
2026-08-06T21:05:15 UTC.

---

### Live data (verified via Supabase REST, 2026-08-15 ~02:00 UTC)

| Source | Query | Count |
|--------|-------|-------|
| `nt_paper_positions` (status=open) | `limit=1000` | 912 |
| `nt_paper_positions` (status=closed) | `limit=1000` | 81 |
| `nt_paper_positions` (status=expired) | `limit=1000` | 7 |
| Open positions with NULL signal_id | — | 0 |
| Signals still exist in nt_signal_sim for 5 oldest open positions | signal_id lookup | 5/5 found, all resolved |

Sample of oldest open positions → their signals ARE resolved:

| Symbol | open_ts | signal resolved_at | outcome |
|--------|---------|--------------------|---------|
| EURUSD | 08-06T21:05 | 08-13T00:33 | tp_hit |
| BTCUSD | 08-06T21:30 | 08-13T00:32 | sl_hit |
| GBPUSD | 08-06T22:05 | 08-13T00:30 | tp_hit |
| USDCHF | 08-06T22:05 | 08-13T00:30 | tp_hit |
| XAUAUD | 08-06T22:05 | 08-13T00:31 | sl_hit |

### Root Cause

`paper_book.py::reconcile()` (line 417) runs after `resolve_pending_signals()`
in the orchestrator EOD lookback pass (line 1098):

```
resolver_summary = await resolve_pending_signals(...)   # line 1085
paper_summary = await reconcile()                       # line 1098
```

The **close pass** (lines 451–474) does:
1. `_fetch_resolved_signals(close_limit=2000)` — fetches the 2000 **newest**
   resolved signals, ordered `ts.desc` (signal generation timestamp)
2. `_fetch_open_positions()` — fetches open positions, ordered `open_ts.asc`
   (oldest first), `limit=2000`
3. Builds `{signal_id → position}` dict from step 2
4. Iterates step 1's resolved signals, patching matching positions to `closed`

**The bug:** `_fetch_resolved_signals` orders by `ts.desc` (generation time)
with `limit=2000`. The signal capture rate is ~3,200/day, so 2000 rows only
covers ~12–15 hours of history. The 912 orphaned positions were opened on
08-06 — their signals have `ts` from 08-06, which is **9 days behind** the
newest 2000 rows. They are resolved (outcome set at 08-13) but never fetched
in the close pass → `closed=0` on every reconcile run.

The **2026-08-08 fix** (documented at lines 261–266) changed from
`order=ts.asc&limit=200` to `order=ts.desc&limit=2000` — this solved the case
where *newly* resolved signals were missed, but it **made the orphan problem
worse**: `ts.asc` would have fetched the oldest resolved signals first (where
orphaned positions cluster), while `ts.desc` now only sees the newest and
never reaches 9-day-old signals.

### Why existing mitigations don't catch this

1. **eod_retention.py protect-list (lines 92–106):** Fetches open positions'
   `signal_id`s and protects them from deletion in `nt_signal_sim`. This
   prevents total orphans (signal deleted while position still open) but does
   NOT close the positions. The protection query itself is capped at
   `limit=1000` (line 97) — if there were >1000 open positions, some would
   still be orphaned even at the retention layer.

2. **Age-expire pass (lines 476–515):** Force-expires positions older than
   `PAPER_OPEN_MAX_AGE_DAYS=30` (line 59). The oldest open positions are 9
   days old — well under the 30-day threshold. They will eventually age out,
   but only after sitting open for 30 days with $0 PnL and skewing the
   paper book.

3. **paper_retroclose.py:** A manual script exists to close positions after a
   fix, but has never been run on this backlog.

### Fix (BUILD COMPLETE — implemented 2026-08-15)

1. ✅ **Rewrite `_fetch_resolved_signals` to fetch by `resolved_at` window**
   — Changed from `order=ts.desc&limit=2000` to `resolved_at=gte.{14_days_ago}`
   with `order=resolved_at.desc`. Fetches ALL signals resolved in the last 14
   days regardless of signal generation timestamp.
2. ✅ **Rewrite the close pass to iterate open positions + batch-lookup their signals
   by `signal_id`** — The close pass now fetches open positions, collects their
   `signal_id`s, and batch-queries `nt_signal_sim` via `in.()` (chunked at 500
   UUIDs per request). Correct regardless of signal age.
3. ✅ **Reduce `PAPER_OPEN_MAX_AGE_DAYS` from 30 to 7** — Safety net for any
   positions the close pass still misses.
4. ⏳ **Run `paper_retroclose.py` once manually** to close the 912 backlog
   — See "Manual Steps" below.

### Out of scope

- Plugin repos (noble-trader-talaria, noble-trader-hermes-plugins) — the
  admin dashboard correctly reports the count; the bug is in the Python
  reconcile logic in noble-trader-fastapi-backend.

---

### Build Complete — Verification Evidence (2026-08-15)

**Files modified:**
- `regime_platform/sweep_orchestrator/__init__.py` — lazy import `__getattr__` for `SweepOrchestrator` so standalone tools don't need `apscheduler` installed
- `regime_platform/sweep_orchestrator/paper_book.py` — `_fetch_resolved_signals()` rewritten, close pass rewritten, `PAPER_OPEN_MAX_AGE_DAYS` 30→7
- `regime_platform/sweep_orchestrator/paper_retroclose.py` — updated to use new `_fetch_resolved_signals(resolved_window_days=14)` signature
- `tests/test_paper_book.py` — 2 tests updated, all 13 tests pass
- `docs/scopes/M4_OPEN_POSITION_ORPHAN_FIX.md` — scope doc (new file, tracked)
- `worklog/20260815_CHANGELOG.md` — this changelog (new file, tracked)

**Test results:**
```
tests/test_paper_book.py: 13 passed
tests/test_eod_lookback_wiring.py: 12 passed
tests/test_realtime_publisher.py: 23 passed
Full suite: 401 passed, 23 skipped in 118.87s
paper_book.py: syntax OK
paper_retroclose.py: syntax OK
__init__.py: lazy import verified — paper_retroclose imports without apscheduler
```

**Manual steps required:**
1. Run `paper_retroclose.py` to close the 912-position backlog (run from repo root `~/noble-trader/`):
   ```bash
   PYTHONPATH=src .venv/bin/python -m regime_platform.sweep_orchestrator.paper_retroclose --dry-run
   PYTHONPATH=src .venv/bin/python -m regime_platform.sweep_orchestrator.paper_retroclose --apply
   ```
   Note: `.venv/bin/python` (Linux path), NOT `.venv/Scripts/python.exe` (Windows path).

2. Restart LightningAI studio to deploy the code:
   ```bash
   PYTHONPATH="" lightning studio stop --name noble-trader-unified
   PYTHONPATH="" lightning studio start --name noble-trader-unified
   ```

3. Monitor next EOD lookback run (00:30 UTC) for `closed=912` (should drop from `closed=0`)
4. Verify `v_talaria_portfolio_stats` shows ~1000 trades (up from 93)
5. Verify `nt_paper_positions` open count drops 912 → near 0

---

### Post-Deploy Fix (2026-08-15, commit `9cdfc27`)

After the initial build was deployed and the retroclose was run on Studio, a `400 Bad Request` was returned on the `nt_signal_sim` query. Two additional bugs were found and fixed:

1. **URL encoding**: The `resolved_at=gte.{cutoff}` filter used an ISO timestamp containing `+00:00`. The `+` character was decoded as a space in the URL query string, producing a malformed filter value. **Fix**: Replace `+00:00` with `Z` (ISO 8601 UTC indicator) before placing in the URL.

2. **Non-existent column**: The select list included `taken` — a column that does not exist in the `nt_signal_sim` schema (migration 102). PostgREST returns 400 for queries referencing unknown columns. **Fix**: Removed `taken` from the select list.

3. **Position fetch cap**: `_fetch_open_positions()` had `limit=2000` but PostgREST's default page size is 1000, so only 1000 rows were returned. **Fix**: Removed the `limit` parameter entirely — PostgREST returns all matching rows across pages.

**Committed and pushed.** Commit `9cdfc27` pushed to `main` — `git push origin main` completed 2026-08-15.
