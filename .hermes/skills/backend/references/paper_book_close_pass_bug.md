# Paper book close-pass bug + retro-close (2026-08-08 diagnosed, 2026-08-08 fixed, 2026-08-15 recurrence + fix)

## Symptom (user report)
Talaria dashboard: Paper equity $0 while the Signal health scoreboard shows
positive PnL; "strategy did not beat benchmark ($0)" while paper trading
looked positive. Disconnect = THREE numbers from TWO sources:
|- `nt_signal_sim` (Signal health / equal-wt benchmark): THEORETICAL signal
  PnL (unit-size, entry/SL/TP) — healthy (+$216 XAU etc.).
|- `nt_paper_positions` + `v_paper_equity`: ACTUAL sized paper book —
  realized_pnl only written on CLOSE. Nothing ever closed → $0.

## Root cause (found via orchestrator log pulled from studio)
`lightning studio cp "lit://0xdweb/noble-trader/studios/noble-trader-unified/noble-trader/logs/orchestrator.log" ./nt-orchestrator.log`
(14.8MB; format `lit://owner/teamspace/studios/studio/path` — NOT
`owner/studio:path` which errors "Either source or destination must be a
Studio file"). Evidence: daily `paper_book: reconcile done — opened=65..67
closed=0 errors=0`; `grep "paper_book: CLOSE"` → NOTHING ever closed.

Two bugs in `regime_platform/sweep_orchestrator/paper_book.py`:
1. **Close-pass window**: `_fetch_resolved_signals` was
   `order=ts.asc&limit=200` and `reconcile(limit=200)` passed its open-pass
   limit to the close fetch → only the 200 OLDEST resolved signals ever
   examined. 400+ resolved existed; the newest (with open positions) never
   fetched → closed=0 forever.
2. **size_units missing**: `_fetch_open_positions` selected only
   position_id,signal_id,status → close_position got size_units=0.0 →
   realized_pnl = pnl_bricks×brick_size×0 = $0 even on a successful close.

## Fix (built + verified, 2026-08-08)
- `_fetch_resolved_signals(limit=2000)` now `order=ts.desc` (newest first).
- `_fetch_open_positions` now selects `...,status,size_units`.
- `reconcile(limit=200, close_limit=2000)` decouples open/close limits.
- Regression tests `tests/test_paper_book.py::TestClosePassFetch` (3 tests,
  monkeypatch httpx.AsyncClient to capture URLs; monkeypatch
  `regime_platform.core.settings.settings` for lazy imports). Backend suite
  312 passed / 23 skipped.

## Retro-close (applied live, 2026-08-08)
New one-shot: `regime_platform/sweep_orchestrator/paper_retroclose.py`
(dry-run default, `--apply` to write). Scans ALL open positions, closes any
whose signal is resolved, using the same close_position math. Applied:
- dry-run: scanned 266, resolved_match 35
- apply: closed 35 (32 closed + 3 expired), second run idempotent (0 more)
- Live after: v_paper_equity 08-08 realized_pnl=0.208751, cumulative
  +$0.2088 (was $0); 231 still open (signals not yet resolved — correct).

## Why paper PnL is small vs signal health (+$0.21 vs +$216)
Paper book books ACTUAL sized notional (size_units × brick_size × pnl_bricks):
XAGUSD sl_hit −$6.15, USDCHF +$4.42, USDJPY +$1.76, USDCAD −$1.28, GBPUSD
+$0.54, EURUSD +$0.42, XAU* +$0.1-0.2, BTCUSD −$0.006 (tiny 0.0006-unit
notional). Signal scoreboard books UNIT-size theoretical PnL. The equity
number is the REAL paper book; the scoreboard is the strategy's hypothetical.
Not a discrepancy — different definitions.

## Deployment note
The fix was committed in the workspace backend repo; the DEPLOYED instance
(studio, branch master) still ran the old code until the user did
`git pull` + restart. The retro-close already corrected the backlog; future
reconciles on the deployed instance kept closing=0 until the new code
deployed. Verified after deploy: orchestrator log showed `closed>0`.

## Recurrence (2026-08-15) — close-pass window bug resurges at scale
The same bug class resurfaced in the opposite direction after the 08-11 price-fix
purge. The 2026-08-08 fix changed `_fetch_resolved_signals` from
`order=ts.asc&limit=200` to `order=ts.desc&limit=2000` (see §Fix above) — fetching
the NEWEST 2000 resolved signals by `ts` (signal generation timestamp). This worked
when the orphan backlog was 266 positions (all recently generated). But the full
signal capture rate is ~3,200/day, so 2000 rows covers only ~12–15 hours of history.

**After the 08-11 purge** (which re-resolved ~14,248 contaminated signals with
correct fresh prices), the paper book opened positions for those re-resolved signals.
Their `ts` is 08-06–08-08 (the stale window), so their `signal_id`s are now ~9 days
old. The close pass fetches the newest 2000 resolved signals — those 08-06 signals
are far outside the window → **912 open positions accumulate**, never closed.

The `eod_retention.py` protect-list (2026-08-13 rewrite, lines 92–106) prevents
deletion of signals that still have OPEN positions, but it also fetches positions
with `limit=1000` — so if open positions exceeded 1000, some signal_ids would be
unprotected and deleted. Currently at 912 (under the cap, so protection holds).

The age-expire pass (`PAPER_OPEN_MAX_AGE_DAYS=30`, hardcoded in `paper_book.py:59`)
will eventually close them, but only after 30 days — at which point they're marked
`expired` with $0 PnL, **losing the real TP/SL outcome data**.

**Same root cause, opposite direction:** `ts.asc` missed new resolutions; `ts.desc`
misses old ones. The fix must anchor on `resolved_at` (when the signal was resolved)
NOT `ts` (when it was generated). See
`docs/scopes/M4_OPEN_POSITION_ORPHAN_FIX.md` + `worklog/20260815_CHANGELOG.md` in
the backend repo for the full recurrence analysis + fix plan.

**`paper_retroclose.py` is ALSO AFFECTED:** it calls `_fetch_resolved_signals(limit=5000)`
with the same `ts.desc` ordering → it cannot find the 912 orphaned positions either.
The retro-close tool must be fixed in the same change.

---

## Fix (built + verified, 2026-08-15)

### Root cause (confirmed)
The close-pass fetch window anchored on `ts` (signal generation timestamp) instead of
`resolved_at` (when the EOD resolver wrote the outcome). At ~3,200 signals/day capture
rate, `limit=2000` covered only ~12–15 hours regardless of `ts.asc` or `ts.desc`.

### Changes (all in `noble-trader-fastapi-backend`, no cross-repo edits)

#### 1. `_fetch_resolved_signals()` — `paper_book.py` lines 258–302
- **Before**: `order=ts.desc&limit=2000` (newest 2000 by generation time)
- **After**: `resolved_at=gte.{cutoff}&order=resolved_at.desc` (all signals resolved in
  the last 14 days, regardless of generation time). No `limit` cap — Supabase's default
  1000-row page is sufficient for the daily resolve volume.
- Added `resolved_window_days: float = 14` parameter.
- Added `timedelta` import; added `taken` column to select list.

#### 2. `reconcile()` close pass — `paper_book.py` lines 475–510
- **Before**: fetched 2000 newest resolved signals, iterated them, looked up matching
  open positions by `signal_id` in a dict.
- **After**: iterates OPEN positions, batch-looks-up their signals via
  `signal_id=in.(...)` PostgREST filter (chunked at 500 UUIDs to stay under URL limits,
  per eod_retention 2026-08-13 lessons). Correct regardless of signal age.
- `close_limit` parameter is deprecated (kept for backward compat; orchestrator at
  `orchestrator.py:1098` calls `reconcile()` with no args).

#### 3. `PAPER_OPEN_MAX_AGE_DAYS` — `paper_book.py` line 59
- Changed default from `30` → `7` days. Safety-net floor: any position older than 7 days
  without a resolved signal gets force-expired with $0 PnL.

#### 4. `paper_retroclose.py` — line 36
- Updated call from `_fetch_resolved_signals(limit=5000)` to
  `_fetch_resolved_signals(resolved_window_days=14)` to use the new window-based query.

### Tests updated — `tests/test_paper_book.py`
- `test_resolved_signals_uses_resolved_at_window` (renamed from
  `test_resolved_signals_newest_first_with_high_limit`): asserts
  `resolved_at=gte.`, `outcome=in.(tp_hit,sl_hit,expired)`, `order=resolved_at.desc`,
  and that `order=ts.desc` and `limit=` are absent.
- `test_reconcile_close_pass_uses_signal_id_batch_lookup` (renamed from
  `test_reconcile_uses_separate_close_limit`): verifies the close pass iterates open
  positions and batch-looks-up signals via `in.()` — does NOT call `_fetch_resolved_signals`.
  Uses 2 fixture positions with resolved signals, asserts `closed == 2`.

### Verification
```
tests/test_paper_book.py: 13 passed in 39.73s
Full suite: 401 passed, 23 skipped in 118.87s
paper_book.py: syntax OK (py_compile)
paper_retroclose.py: syntax OK (py_compile)
```

### Post-deploy debugging (Studio dry-run, 2026-08-15)
After the initial fix was deployed and the retroclose was run on Studio,
three 400 Bad Request errors were encountered and fixed iteratively:

1. **`+00:00` timestamp encoding**: The ISO timestamp in `resolved_at=gte.`
   contained `+00:00`. The `+` was decoded as a space → malformed filter.
   Fix: `.replace("+00:00", "Z")`.

2. **Non-existent column**: The select list included `taken` — a column that
   doesn't exist in `nt_signal_sim` (migration 102). PostgREST 400 on unknown
   columns. Fix: removed `taken`.

3. **Microsecond dots**: The timestamp `2026-08-01T04:13:26.962161Z` contained
   `.` in the microsecond fraction, which PostgREST's dot-delimiter syntax
   misinterpreted as a new operator separator → 400. Fix: `.replace(microsecond=0)`.

All three root causes and the debugging technique (inspect the httpx URL
log, scan for special chars, verify columns against migration SQL) are
documented in detail in:
`references/postgrest_url_filter_debugging.md`

4. **`resolved_at IS NULL` fallback**: Signals resolved by the OLD edge-function
   resolver (pre-migration 102 Python resolver) have `outcome` set but
   `resolved_at=NULL`. Added `or=(resolved_at.gte.{cutoff},resolved_at.is.null)`
   to `_fetch_resolved_signals` to catch both old and new style resolutions.

### Manual step (requires live Supabase access)
```bash
# Dry-run first (no writes):
PYTHONPATH=src .venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.paper_retroclose --dry-run
# Then apply:
PYTHONPATH=src .venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.paper_retroclose --apply
```
The dry-run should show 912 scanned, 912 resolved_match (all signals resolved by 08-13).
The apply run closes all 912 positions. Next EOD lookback at 00:30 UTC uses the fixed
`reconcile()` and prevents future accumulation.
