# nt_renko_bricks timeout + unbounded growth (2026-10-01)

**Status:** DIAGNOSED — not yet built. Scope only, pending approval.
**Repos touched by this incident so far:** `nobletradingapp` (cache-clobber
fix, Vercel `?refresh=1` 504 fix), `nobletradingapp-backend` (concurrent
chunk fetch, 120-day lookback bound), `noble-trader-fastapi-backend`
(**this repo** — migration 187, the only durable schema change).

---

## 1. Symptom chain

A single root cause (`nt_renko_bricks` size) surfaced through three
different failure modes across three repos before being traced back here:

1. **"Backend timed out (cold start...)"** on the dashboard Portfolio tab
   (Signal risk overview panel) — client-side message in
   `nobletradingapp/utils/portfolio-cache.js`, misleading: the Render service
   was healthy, this was never a cold start.
2. **Render logs**, repeated:
   ```
   ERROR:webapp_backend.supabase:supabase GET v_talaria_daily_close failed
   HTTP 500: {"code":"57014","details":null,"hint":null,
   "message":"canceling statement due to statement timeout"}
   ```
3. **Vercel `FUNCTION_INVOCATION_TIMEOUT` 504s** on
   `/api/portfolio-analytics/correlation` and `.../signal-agreement` with
   `?refresh=1` — a secondary bug (those routes awaited the recompute inline
   on refresh regardless of cache state), fixed independently in
   `nobletradingapp` but surfaced by the same underlying slow query.

## 2. Root cause #1 — missing covering index (FIXED, migration 187)

`v_talaria_daily_close` (nobletradingapp migration 0011) is:

```sql
SELECT DISTINCT ON (b.symbol, b.session_date)
  b.symbol, b.session_date, b.close_price, b.ts
FROM public.nt_renko_bricks b
ORDER BY b.symbol, b.session_date, b.brick_index DESC, b.ts DESC;
```

The only existing index near this shape, `idx_nt_renko_bricks_live_grid`
(migration 185, `(symbol, brick_size, session_date DESC, brick_index DESC)`),
has `brick_size` wedged between `symbol` and `session_date` — it does not
match this view's ordering. Without a matching index, Postgres had to sort
per `DISTINCT ON` group across the whole table for every symbol in the
IN-list.

**Fix:** `supabase/migrations/187_renko_daily_close_covering_index.sql`
added `idx_nt_renko_bricks_daily_close (symbol, session_date, brick_index
DESC, ts DESC)` — purpose-built for this view's exact `DISTINCT ON` + `ORDER
BY`. Applied directly to production 2026-10-01; checked into this repo for
schema parity.

**Verified live** (user ran this in the Supabase SQL editor after applying
187 and redeploying the backend):

```
EXPLAIN (ANALYZE, BUFFERS, TIMING)
SELECT DISTINCT ON (symbol, session_date) ...
WHERE symbol IN (<14 plan symbols>)
ORDER BY symbol, session_date, brick_index DESC, ts DESC;
```

```
Index Scan using idx_nt_renko_bricks_daily_close on nt_renko_bricks
  (actual time=4.652..16339.887 rows=860303 loops=1)
  Buffers: shared hit=852602 read=3540 dirtied=114 written=31
Execution Time: 21795.433 ms
```

**The index IS being used.** The plan is correct. It is still too slow,
because of root cause #2.

## 3. Root cause #2 — no bound on history scanned (table is 800k+ rows)

The index fix only solved the *sort*. The query still asks for **every
historical `(symbol, session_date)` row** for 14 symbols with no date
filter — and the planner has to walk every matching index entry to produce
the `DISTINCT ON` result, regardless of how good the index is. Confirmed:
`nt_renko_bricks` currently has **800k+ rows**; the EXPLAIN above walked
**860,303 index entries** in 21.8s to return 54 output rows.

Correlation only ever needs `MIN_WINDOW + 10` (~20-31) shared trading
sessions (`regime_platform/routers/webapp.py`) — never a symbol's entire
history. The query had no `session_date` lower bound at all.

**A second latent bug found in the same function:** the chunked fetch
(`fetch_daily_closes`, `nobletradingapp-backend/regime_platform/services/
supabase_client.py`) passed `limit=2000` per 4-symbol chunk with **no
`order` parameter**. PostgREST/Postgres make no guarantee about *which*
2000 rows an unordered, limited query returns — on this access pattern the
underlying scan order favored the oldest sessions for the
alphabetically-first symbol in each chunk, which could have been silently
starving the other symbols of any rows at all. This is a likely contributor
to correlation's long-standing "insufficient history" / thin-overlap
problem, independent of the timeout.

**Fix applied in `nobletradingapp-backend` (sibling repo, not this one):**
bounded the query to the last 120 calendar days (generous buffer over the
~20-31 sessions actually needed, across mixed FX/crypto/equities weekends
and holidays) with an explicit `session_date.desc` order so `limit` always
keeps the newest data. No schema change required — `idx_nt_renko_bricks_
daily_close` added by migration 187 already satisfies this access pattern
(equality on `symbol`, range on `session_date`, matching sort via backward
scan).

## 4. Root cause #3 — why is the table 800k+ rows at all? (NOT YET FIXED)

This is the part that belongs to this repo and is NOT resolved by anything
above.

### 4a. The retention design assumes a 2-day steady state

`renko_retention.py` (nightly, Part VI of `orchestrator.py::
_run_eod_lookback`, 01:17 UTC) is supposed to:

1. Delete rows whose `brick_size` no longer matches the symbol's current
   `config/brick_params.yaml` value (orphaned grids from daily brick_size
   re-optimization).
2. Delete rows older than `renko_brick_retention_days`
   (`config/feature_flags.yaml`, **default 2** — "today + yesterday," the
   same steady state migration 111 established, 2026-08-09).

If this were actually holding, `nt_renko_bricks` would be bounded to ~2
session-days × live grids per symbol — nowhere near 800k rows.

### 4b. Likely bug in the pruner's symbol discovery

`renko_retention.py::_distinct_symbols()`:

```python
resp = await client.get(
    _url("nt_renko_bricks"),
    headers=_headers(),
    params={"select": "symbol", "limit": "10000"},
)
...
rows = resp.json() or []
return sorted({r.get("symbol") for r in rows if r.get("symbol")})
```

No `order`, no DB-side `DISTINCT` — it fetches the first 10,000 raw rows
(unordered) and dedupes client-side. With 800k+ rows spread unevenly across
the roster, it's plausible that a handful of high-volume symbols fill the
entire 10,000-row cap before the scan ever reaches the rest of the roster —
meaning **those symbols silently never get pruned at all**, every night,
indefinitely. This is consistent with the observed growth and is a bug
regardless of whether it's the full explanation: the symbol roster should
come from `v_talaria_symbols` / `config/brick_params.yaml` (the same source
`_delete_stale_brick_size` already reads per-symbol), not from a capped,
unordered scan of the very table being pruned.

**Not yet confirmed against production logs** — would need
`[renko:retention] complete: ...` log lines (or a fresh
`run_renko_brick_retention()` dry invocation) to verify `symbols_checked`
is actually landing at the full roster size on each run, and whether
`deleted_old_sessions` is tracking anything close to expected daily brick
volume. This is a concrete next step, not yet done.

### 4c. The real conflict: retention bounding the table would break correlation

`v_talaria_daily_close` (the only daily-close price history persisted
anywhere in this platform — there is no separate OHLC/daily-bar table) is
a resample of `nt_renko_bricks`. If `renko_brick_retention_days: 2` were
actually enforced, correlation would be **permanently** stuck at
"insufficient history" — which is in fact exactly what
`docs/worklog-portfolio-analytics-cache-and-thin-history.md` (nobletradingapp
repo) already documented on 2026-09-30: *"2 complete sessions across 14
symbols."* The table being 800k+ rows (i.e. retention NOT actually working)
is accidentally the only reason correlation has enough history to compute
anything at all.

**This means a naive fix that just makes `renko_retention.py` work
correctly would immediately regress correlation back to permanently
insufficient-history**, the exact problem the 2026-09-30 worklog already
fixed around (thin-history fallback, pairwise correlation, dropped-symbol
labeling). Any cleanup here has to separate the two concerns before
touching retention enforcement.

## 5. Proposed fix (NOT YET BUILT — pending approval)

1. **Separate daily-close history from brick-level retention.** Add a
   small, durable table (or a materialized nightly snapshot) that persists
   one row per `(symbol, session_date, close_price)` — written once per
   session close, kept for the full correlation lookback window
   (120+ days per §3 above), independent of brick-level retention. This is
   on the order of symbols × trading-days rows (tens of thousands over a
   year for a 14-symbol roster), not hundreds of thousands.
2. **Fix `renko_retention.py::_distinct_symbols()`** to source the roster
   from `v_talaria_symbols` (or `config/brick_params.yaml`, same source
   `_delete_stale_brick_size` already uses per-symbol) instead of a capped,
   unordered scan of `nt_renko_bricks` itself.
3. **Then let `renko_brick_retention_days` actually bind** — once daily
   closes live independently (step 1), the brick table can be safely pruned
   back to its designed ~2-day live-grid steady state without starving
   correlation.
4. **One-time manual cleanup** of the current 800k-row backlog, scoped to
   NOT touch rows within the correlation lookback window (120 days) until
   step 1 ships, then a normal retention-days-bounded prune after.

## 6. What's already shipped vs. what's still open

| # | Fix | Repo | Status |
|---|---|---|---|
| 1 | Covering index `idx_nt_renko_bricks_daily_close` | `noble-trader-fastapi-backend` | ✅ shipped, migration 187, applied to prod |
| 2 | 120-day lookback bound + explicit order on `fetch_daily_closes` | `nobletradingapp-backend` | ✅ shipped |
| 3 | Concurrent (not serial) chunk fetch in `fetch_daily_closes` | `nobletradingapp-backend` | ✅ shipped |
| 4 | Cache-clobber fix (failed recompute no longer nulls a good cached payload) | `nobletradingapp` | ✅ shipped |
| 5 | `?refresh=1` no longer blocks the Vercel function inline | `nobletradingapp` | ✅ shipped |
| 6 | Thin-history 422 → honest `insufficient_history` label | `nobletradingapp-backend` | ✅ shipped (2026-09-30 session) |
| 7 | Durable daily-close storage independent of brick retention | `noble-trader-fastapi-backend` | ❌ **not built** — scoped §5.1 |
| 8 | `_distinct_symbols()` roster-source fix | `noble-trader-fastapi-backend` | ❌ **not built** — scoped §5.2 |
| 9 | Let brick retention actually bind to 2 days | `noble-trader-fastapi-backend` | ❌ **not built**, blocked on #7 — scoped §5.3 |
| 10 | One-time 800k-row backlog cleanup | `noble-trader-fastapi-backend` | ❌ **not built**, blocked on #7 — scoped §5.4 |

## 7. Database size audit (2026-10-01, user-provided)

User pulled Supabase's own table/index size report. The 5 largest objects
in the whole database:

| Object | Size | % of DB |
|---|---|---|
| `public.nt_renko_bricks` | 305.7 MB | 50.51% |
| `public.nt_sweep_result` | 103.41 MB | 17.09% |
| `public.nt_regime_log` | 79.47 MB | 13.13% |
| `public.idx_nt_renko_bricks_live_grid` | 73.57 MB | 12.16% |
| `public.nt_renko_bricks_symbol_brick_index_session_date_brick_size_key` | 53.97 MB | 8.92% |

**These 5 objects are 101.8% nominal / effectively the entire database.**
Three of the five are `nt_renko_bricks` itself or its indexes — table
305.7 MB + `idx_nt_renko_bricks_live_grid` 73.57 MB (migration 185) +
the unique constraint index backing its upsert conflict target
(`(symbol, brick_index, session_date, brick_size)`, migration 145)
53.97 MB = **433.24 MB, 71.6% of the whole database**, all driven by the
exact same root cause already diagnosed in §4: retention not actually
binding. `idx_nt_renko_bricks_daily_close` (migration 187, this incident's
fix) isn't in the top 5 yet, but it is built on the same unbounded table and
will keep growing in lockstep with it until retention is fixed — it is not
a second problem, it is the same problem's newest symptom.

`nt_sweep_result` (103.41 MB, 17.09%) and `nt_regime_log` (79.47 MB, 13.13%)
are a **separate, previously-undiagnosed instance of the same class of bug**
— append-only, write-heavy tables (~174K and ~178K inserts respectively per
migration 177's 2026-09-27 note) with **no retention/prune pass at all**.
Confirmed by searching this repo for any `DELETE`/prune job targeting
either table: none exists. Compare to `nt_signal_sim`, which has had a
dedicated nightly retention pass (`eod_retention.py`) since 2026-08-12
specifically because it hit the same unbounded-growth problem first
(17,992 rows observed pre-fix). `nt_sweep_result` and `nt_regime_log` never
got the equivalent treatment — migration 177 (2026-09-27) only found and
fixed *duplicate indexes* on them, not the underlying row-count growth, and
no later migration added retention.

**No live consumer needs more than a short window of either table**,
confirmed by reading every query against them in this repo:
- `v_talaria_latest_signals` (migration 183, materialized view) only reads
  the single latest row per symbol via `DISTINCT ON` — needs recency, not
  depth.
- `scripts/noble_calibration.py` Section 5 (failed heavy-sweep audit) reads
  `nt_sweep_result` with `sweep_timestamp gte.<7 days ago>` — a 7-day
  window, by design.
- `scripts/kairos_p12_timesfm_ic.py` pulled a fixed historical slice
  (`gte.2026-08-15T00:00:00Z`) for one dated research study, already run —
  not a recurring live dependency requiring unbounded retention.
- No other call site in this repo reads either table beyond those three
  patterns.

So unlike `nt_renko_bricks` (where retention is in genuine tension with
correlation's need for history, §4c), **`nt_sweep_result` and
`nt_regime_log` have no such tension** — nothing reads more than ~7-30 days
back. These are a strictly simpler fix: add a nightly prune pass mirroring
`eod_retention.py`'s existing shape, no separate-storage step required
first.

## 8. Updated proposed fix (supersedes §5 scope for sizing, same intent)

Two independent cleanup tracks, since `nt_renko_bricks` has a real
retention-vs-correlation conflict (§4c) and `nt_sweep_result`/`nt_regime_log`
do not:

**Track A — `nt_renko_bricks` (blocked on separating daily-close storage):**
1. Durable daily-close table/snapshot, independent of brick retention (§5.1).
2. Fix `renko_retention.py::_distinct_symbols()` roster source (§5.2).
3. Let `renko_brick_retention_days` actually bind once (1) ships (§5.3).
4. One-time backlog cleanup, scoped to preserve the correlation lookback
   window until (1) ships (§5.4).
2'. **New, from this size audit:** once retention genuinely binds, revisit
    whether `idx_nt_renko_bricks_live_grid` (73.57 MB) and the
    `(symbol, brick_index, session_date, brick_size)` unique constraint
    index (53.97 MB) are still sized appropriately — both will shrink
    automatically as the underlying table shrinks, no separate action
    needed, but worth confirming post-cleanup rather than assuming.

**Track B — `nt_sweep_result` + `nt_regime_log` (independent, no blocker,
can ship first):**
1. New `sweep_result_retention_days` / `regime_log_retention_days` config
   keys in `config/feature_flags.yaml`, read via a getter in
   `regime_platform/core/feature_flags.py` (same pattern as
   `get_renko_brick_retention_days`/`get_signal_sim_retention_resolved_days`).
   Proposed default: 30 days — comfortably above every live consumer's
   actual need (7 days for the failed-sweep audit; `v_talaria_latest_signals`
   needs only the newest row).
2. New `regime_platform/sweep_orchestrator/sweep_result_retention.py`
   (mirrors `eod_retention.py`'s shape exactly: best-effort, PostgREST
   `Prefer: count=exact` for true deleted-row counts, never raises).
   Deletes rows from both tables older than the configured cutoff.
3. Wire into `orchestrator.py::_run_eod_lookback` as a new Part (after
   existing Part VI renko retention), same best-effort try/except pattern
   as every other part of that method.
4. One-time backlog cleanup migration for the existing 103MB/79MB backlog,
   same cutoff.

Track B has no architectural conflict and could ship independently of
Track A if useful to de-risk/sequence the work — it is strictly lower risk
(no history-retention tradeoff to reconcile first).

## 9. Decision log

| Date | Decision |
|---|---|
| 2026-10-01 | Index fix (187) diagnosed and applied; confirmed via EXPLAIN that the index works but the table is too large for an unbounded scan regardless. |
| 2026-10-01 | 120-day lookback bound shipped in `nobletradingapp-backend` as the query-level fix — no further schema change needed for this half. |
| 2026-10-01 | User asked for root-cause cleanup of `nt_renko_bricks` size itself. Diagnosed retention-vs-correlation conflict (§4c) and pruner bug candidate (§4b). **Scope written, no code built yet** — awaiting explicit build authorization per workflow (Plan > Iterate > Build > Review > Approve push). |
## 10. Replay/research script audit — do any read these 3 tables? (2026-10-01)

User asked whether any `replay_*`/research script depends on
`nt_renko_bricks`, `nt_sweep_result`, or `nt_regime_log` history, since
those would be a third constraint on retention depth (like correlation's
120-day need, §4c) that has to be accounted for before any cleanup.

**Checked every script matching `*replay*` in `scripts/` (7 total) plus
every other script directly referencing any of the 3 tables.**

| Script | Table(s) read | Window |
|---|---|---|
| `control_geometry_replay.py` | `nt_signal_sim` | `ts gte.2026-09-20` (fixed dated pull, already run) |
| `dynamic_atr_class_replay.py` | `nt_signal_sim`, `nt_symbol` | `ts gte.2026-09-20` (same) |
| `replay_tiered_atr_geometry.py` | `nt_bracket_reachability` | `--live` flag, no date filter but a separate small aggregate table, not one of the 3 |
| `replay_settlement_parity.py` | `nt_signal_sim` | `--live` flag |
| `replay_p_win_bracket_gate.py` | `nt_signal_sim` | `--live` flag |
| `replay_class_goal_table.py` | `nt_signal_sim` | — |
| `replay_calibration_gap.py` | `nt_signal_sim` | `--live` flag |
| `kairos_p12_timesfm_ic.py` | `nt_sweep_result` (+ `nt_signal_sim`) | `sweep_timestamp gte.2026-08-15` (fixed dated pull, one-time study, already run) |
| `kairos_p3_rlayer.py` | `nt_sweep_result` (+ `nt_signal_sim`) | `sweep_timestamp gte.2026-08-15` (same) |
| `check_sweep_result_consistency.py` | `nt_sweep_result` | one-off diagnostic, `limit=200` newest rows, no date filter needed (TDVA order fix verification, 2026-08-11) |
| `noble_calibration.py` §5 | `nt_sweep_result` | `sweep_timestamp gte.<7 days ago>` (already documented §7) |

**Result: zero replay/research scripts touch `nt_renko_bricks` or
`nt_regime_log` at all.** Every script that touches `nt_sweep_result` either
(a) reads a fixed, already-executed historical date range tied to a
one-time study (not a recurring dependency that would be broken by
retroactive pruning of OLDER data than its range, since that data has
already been pulled and the report already written), or (b) deliberately
windows to a short recent period (7 days, or "latest 20/200 rows").

**This confirms Track B has no retention-depth constraint analogous to
§4c.** Unlike `nt_renko_bricks` → `v_talaria_daily_close` → correlation
(which needs 120 days), nothing anywhere in this repo — live app code or
offline research script — needs more than ~7-30 days of `nt_sweep_result`
or `nt_regime_log` history. The two fixed-date studies
(`kairos_p12_timesfm_ic.py`, `kairos_p3_rlayer.py`) already ran against
their target window; a future re-run of either would need to pull live
data again regardless of retention policy, since they're one-time
snapshots-in-time studies, not scripts meant to be re-run unmodified
against a shifting "last N days" window.

## 11. Track B scope (2026-10-01) — ready to build pending approval

Confirms the design sketched in §8 Track B, now fully grounded (schema,
existing-code patterns, no retention-depth conflict per §10). No FK/
protect-list concern either: unlike `nt_signal_sim` (protected from deletion
by open `nt_paper_positions.signal_id` references, `eod_retention.py`'s
`_fetch_protected_signal_ids`), neither `nt_sweep_result` nor
`nt_regime_log` is referenced by any other table's rows — a plain
age-cutoff DELETE is safe with no protect-list step.

### 11.1 Config

`config/feature_flags.yaml` — two new keys, same comment-block convention
as `renko_brick_retention_days` (§4a) and
`signal_sim_retention_resolved_days`:

```yaml
# sweep_result_retention_days (2026-10-01, worklog
# 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§11): nt_sweep_result
# has NO retention pass today and is 103.41 MB / 17.09% of the database
# (~174K inserts/day-over-day per migration 177's 2026-09-27 note). No live
# consumer (app code or research script, see §10) reads more than ~7 days
# back. 30 gives headroom above every confirmed need.
sweep_result_retention_days: 30

# regime_log_retention_days (same worklog): nt_regime_log is 79.47 MB /
# 13.13% of the database, same no-retention-pass gap, same confirmed
# no-consumer-needs-more-than-days audit (§10).
regime_log_retention_days: 30
```

`regime_platform/core/feature_flags.py` — add both to `_DEFAULTS` (mirrors
line 181's `"renko_brick_retention_days": 2`) plus two getters mirroring
`get_renko_brick_retention_days()` (lines 1149-1161) exactly:

```python
def get_sweep_result_retention_days() -> int:
    try:
        return int(_load().get(
            "sweep_result_retention_days", _DEFAULTS["sweep_result_retention_days"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["sweep_result_retention_days"]


def get_regime_log_retention_days() -> int:
    try:
        return int(_load().get(
            "regime_log_retention_days", _DEFAULTS["regime_log_retention_days"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["regime_log_retention_days"]
```

### 11.2 New module: `regime_platform/sweep_orchestrator/sweep_result_retention.py`

Mirrors `renko_retention.py`'s shape exactly (it is the most structurally
similar existing module — simple age-cutoff DELETE, no protect-list, no
per-symbol config lookup needed):

- `_parse_content_range_total()` — copy of the same helper (3 existing
  copies of this already exist in `eod_retention.py`/`renko_retention.py`;
  a 4th copy keeps the mirror-the-sibling-module convention rather than
  introducing a new shared-util refactor as a drive-by in this change).
- `_delete_old_sweep_results(client, cutoff_iso) -> int` — `DELETE
  nt_sweep_result WHERE sweep_timestamp < cutoff_iso`, `Prefer:
  count=exact`, same try/except-log-return-0 pattern as
  `renko_retention._delete_old_sessions`.
- `_delete_old_regime_log(client, cutoff_iso) -> int` — same shape against
  `nt_regime_log WHERE detected_at < cutoff_iso` (column name per migration
  036 / `fetch_latest_regime`'s `order=detected_at.desc`).
- `run_sweep_result_retention() -> dict[str, Any]` — orchestrates both
  deletes, returns `{deleted_sweep_result, deleted_regime_log, errors}`,
  never raises (same contract as `run_renko_brick_retention` /
  `run_eod_retention`).
- CLI entry (`python -m regime_platform.sweep_orchestrator
  .sweep_result_retention`), same as both sibling modules.

### 11.3 Wiring: `orchestrator.py::_run_eod_lookback`

New try/except block directly after the existing "Part VI: renko brick
retention" block (line ~2071-2091), same best-effort pattern as every other
part of this method:

```python
# ── Part VII: sweep_result / regime_log retention (2026-10-01) ─────
# nt_sweep_result + nt_regime_log have no retention pass — 103.41MB /
# 79.47MB and growing with every sweep tick, no consumer needs more than
# ~30 days (worklog 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§10).
try:
    from regime_platform.sweep_orchestrator.sweep_result_retention import (
        run_sweep_result_retention,
    )
    sweep_retention_summary = await run_sweep_result_retention()
    logger.info(
        "[eod:lookback] sweep_result/regime_log retention done: %s",
        sweep_retention_summary,
    )
except Exception as exc:
    logger.error("[eod:lookback] sweep_result/regime_log retention failed: %s", exc)
```

### 11.4 Tests: `tests/test_sweep_result_retention.py`

Mirrors `tests/test_renko_retention.py`'s shape (mocked `FakeResponse`/
`FakeClient`, never touches real Supabase):
- `_delete_old_sweep_results` / `_delete_old_regime_log`: correct DELETE
  params (column name, `lt.<cutoff>` filter), `Prefer: count=exact` header,
  `Content-Range` parsing for both exact (`0-0/42`) and capped (`*/0`)
  shapes.
- `run_sweep_result_retention`: full-flow summary counts; config not
  configured (`SUPABASE_URL` empty) returns zeroed summary without raising;
  one delete failing does not prevent the other from running (independent
  try/except per table, same as `renko_retention`'s stale-brick_size vs.
  old-sessions passes).
- Config getter defaults + override from `feature_flags.yaml`
  (`test_retention_days_getter_default_and_floor`-style, mirroring
  `test_eod_retention.py` line 232).

### 11.5 One-time backlog migration

New `supabase/migrations/188_sweep_result_regime_log_one_time_prune.sql`
(next available number after 187) — same shape as migration 145's
one-time `nt_renko_bricks` prune (§6's migration table reference):

```sql
DELETE FROM nt_sweep_result WHERE sweep_timestamp < (now() - interval '30 days');
DELETE FROM nt_regime_log WHERE detected_at < (now() - interval '30 days');
```

Plain `DELETE` (not `CONCURRENTLY` — N/A for DELETE anyway; consistent
with migration 177/187's noted constraint that Supabase's SQL Management
API runs inside an implicit transaction).

### 11.6 Why this is lower-risk than Track A and can ship independently

- No protect-list / FK concern (§11, opening paragraph).
- No retention-depth conflict with any consumer (§10) — unlike
  `nt_renko_bricks`, there is no feature that needs the deleted history to
  keep working.
- Purely additive new module + 2 config keys + 1 wiring block + 1
  migration — touches zero existing read paths, zero existing call sites,
  zero existing tests' assertions (new test file only).
- Directly mirrors two already-shipped, already-tested sibling modules
  (`eod_retention.py`, `renko_retention.py`) — no novel design risk.

## 13. Track A scope (2026-10-01) — nt_renko_bricks, NOT YET BUILT

Grounded against the actual schema/call sites (no guessing): `v_talaria_daily_close`
(migration 0011, owned by `nobletradingapp`) resamples `nt_renko_bricks`
directly; `fetch_daily_closes` (owned by `nobletradingapp-backend`) is its
only reader, bounded to 120 days (`DAILY_CLOSE_LOOKBACK_DAYS`); the brick
table itself has unique key `(symbol, brick_index, session_date,
brick_size)` (migration 145) and three existing indexes — none with
`session_date` as a leading/sole column (checked all of
`idx_nt_bricks_symbol` `(symbol, ts desc)`, `idx_nt_renko_bricks_live_grid`
`(symbol, brick_size, session_date desc, brick_index desc)`, and
`idx_nt_renko_bricks_daily_close` `(symbol, session_date, brick_index desc,
ts desc)`) — this last point turns out to matter a lot, see §13.0 below.

### 13.0 New candidate root cause — the nightly age-based DELETE may itself be timing out silently

Re-reading `renko_retention.py::_delete_old_sessions` (the step that
actually enforces `renko_brick_retention_days`, independent of the
`_distinct_symbols()` roster bug in §4b — this DELETE has NO symbol
filter at all, it is a blanket
`DELETE FROM nt_renko_bricks WHERE session_date < cutoff`) against the
table's actual indexes: **none of the three existing indexes have
`session_date` as a leading column.** All three lead with `symbol`. A
`WHERE session_date < cutoff` predicate with no `symbol` equality can't
use any of them — Postgres has to sequential-scan the entire table (same
800k+ rows root cause #2 already hit on the SELECT side, migration 187).

This is a strong, previously-undiagnosed candidate explanation for why
`renko_brick_retention_days: 2` has never actually bound despite running
nightly (Part VI, 01:17 UTC) since 2026-09-09: the DELETE has very
plausibly been silently timing out (`resp.is_success` False → logged as a
WARNING, swallowed by the best-effort design per `_delete_old_sessions`'s
own code) every single night, for as long as the table has been large
enough to force a full scan. This is NOT yet confirmed — no Postgres
server log access in this session (the Supabase MCP connector available
here is bound to an unrelated project, `SAVI`, confirmed §12's prior
`list_projects`/`execute_sql` attempts) — but it is a cheap, concrete,
pre-build verification step with a real answer, not a guess: run `EXPLAIN
(ANALYZE) DELETE FROM nt_renko_bricks WHERE session_date <
'<2-days-ago>'` (or grep Render/Studio logs for
`[renko:retention] old-session DELETE HTTP` warnings) before writing any
fix code, since it changes what the fix needs to include.

**If confirmed, this reframes §4a-c**: the real blocking fix is not just
"separate daily-close storage so retention can finally be turned on" — it
is "add a `session_date`-leading index so the age-based DELETE can
physically complete within Supabase's statement_timeout," *in addition to*
the daily-close separation (because even once the DELETE can run, it still
must not delete the correlation window until that window lives
elsewhere). Both are needed; neither alone fixes the table size.

### 13.1 New durable daily-close storage, independent of brick retention

New table (same anon-read pattern as `nt_renko_bricks` itself, migration
108 — this repo already serves `nobletradingapp`'s anon key reads
directly, no new precedent):

```sql
CREATE TABLE IF NOT EXISTS public.nt_daily_close (
  symbol        text NOT NULL,
  session_date  date NOT NULL,
  close_price   numeric NOT NULL,
  ts            timestamptz NOT NULL,
  captured_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, session_date)
);

ALTER TABLE public.nt_daily_close ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access" ON public.nt_daily_close
  FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "anon read nt_daily_close" ON public.nt_daily_close
  FOR SELECT USING (true);

GRANT SELECT ON public.nt_daily_close TO anon;
```

Sized at symbols × trading-days — tens of thousands of rows over a year
for a 14-39 symbol roster (`v_talaria_symbols`' observed range per
migration 179's comment), not hundreds of thousands. Primary key IS the
natural dedup key (no serial id needed, unlike `nt_renko_bricks`'s
brick-level granularity).

**One-time backfill** (run once, before any brick cleanup, so the current
120-day correlation window is preserved in the new table from day one —
this is the step that makes the sequencing in §13.6 safe):

```sql
INSERT INTO nt_daily_close (symbol, session_date, close_price, ts)
SELECT DISTINCT ON (symbol, session_date) symbol, session_date, close_price, ts
FROM nt_renko_bricks
WHERE session_date >= (CURRENT_DATE - INTERVAL '120 days')
ORDER BY symbol, session_date, brick_index DESC, ts DESC
ON CONFLICT (symbol, session_date) DO NOTHING;
```

**New nightly snapshot module**: `regime_platform/sweep_orchestrator/
daily_close_snapshot.py`, mirrors `renko_retention.py`'s shape — reads the
same `DISTINCT ON (symbol, session_date) ... ORDER BY symbol, session_date,
brick_index DESC, ts DESC` query (the index from migration 187 already
supports it) for a short rolling window (e.g. last 3 days, to self-heal
any day a prior run missed), upserts into `nt_daily_close` via
`on_conflict=symbol,session_date` + `Prefer: resolution=merge-duplicates`
(same upsert pattern `log_renko_bricks` already uses against
`nt_renko_bricks` itself). Wired as a **new Part VI** in
`orchestrator.py::_run_eod_lookback`, inserted BEFORE the existing renko
brick retention part (which becomes Part VII) — ordering matters: the
snapshot must capture a session's close before that session could ever be
pruned by brick retention, even though in steady state both run the same
night with no actual race (retention only touches `session_date < cutoff`,
never today's/yesterday's rows).

### 13.2 Fix `renko_retention.py::_distinct_symbols()` roster source

Replace the capped (`limit=10000`), unordered, client-deduped scan of
`nt_renko_bricks` itself with the roster this repo already has a
first-class fetcher for: `fetch_active_symbols()`
(`supabase_client.py:43`, reads `nt_symbol WHERE active = true` — the same
underlying table `v_talaria_symbols` is a view over). This is a reuse, not
new code: `_distinct_symbols()`'s only job is producing a symbol list to
drive `_delete_stale_brick_size`'s per-symbol loop, and
`fetch_active_symbols()` already returns exactly that, ordered, uncapped,
with no dependency on the very table being pruned.

### 13.3 Session_date-leading index (new, from §13.0)

```sql
CREATE INDEX IF NOT EXISTS idx_nt_renko_bricks_session_date
  ON public.nt_renko_bricks (session_date);
```

Supports `_delete_old_sessions`' blanket `WHERE session_date < cutoff`
DELETE (and the one-time backlog cleanup in §13.4) without a full-table
sequential scan. Plain `CREATE INDEX` (not `CONCURRENTLY` — same
Supabase-SQL-Management-API constraint noted in migrations 177/187).

### 13.4 One-time backlog cleanup — chunked, not a single giant DELETE

Once §13.1's backfill has run (so the 120-day correlation window is
already durable in `nt_daily_close`) and §13.3's index exists, the
800k-row backlog can be trimmed back to the 2-day steady state. A single
`DELETE FROM nt_renko_bricks WHERE session_date < cutoff` against ~800k
candidate rows risks a long lock / large single transaction even with the
new index (every existing one-time migration DELETE in this repo so far —
111, 145 — operated on much smaller backlogs). Safer as a chunked loop,
e.g. run from the Studio (same execution context as the backup script),
not as a single in-migration statement:

```python
# one-time, run manually from the Studio, NOT part of a migration file
while True:
    deleted = await _delete_old_sessions_page(client, cutoff, page_size=50_000)
    if deleted == 0:
        break
```

(`_delete_old_sessions` would need a `LIMIT`-equivalent — PostgREST DELETE
has no `limit` parameter; the chunking would have to go through a `SELECT
brick_id ... LIMIT 50000` then `DELETE WHERE brick_id IN (...)`, or a raw
SQL `DELETE ... WHERE brick_id IN (SELECT brick_id FROM nt_renko_bricks
WHERE session_date < cutoff LIMIT 50000)` executed via whatever genuine
SQL-execution path is available at build time — the Supabase MCP
connector in THIS session is not it, see §13.0).

### 13.5 Let `renko_brick_retention_days` actually bind

No config change needed — it already defaults to 2 (§4a). "Letting it
bind" here means: once §13.2 (roster fix) and §13.3 (index, so the DELETE
stops timing out) ship, the existing nightly Part VII (renumbered from VI)
genuinely prunes to the 2-day steady state going forward, with no further
code change. Verify via the `[renko:retention] complete: ...` log line
actually showing `deleted_old_sessions` > 0 and `symbols_checked` matching
the full active roster size on the first few runs post-deploy — the
concrete verification step already flagged as open in §4b.

### 13.6 Critical cross-repo dependency — this repo's build is necessary but not sufficient

**Building §13.1-13.5 in this repo does NOT by itself fix correlation.**
`v_talaria_daily_close` (migration 0011, owned by `nobletradingapp`) is a
view defined directly over `nt_renko_bricks`:

```sql
CREATE OR REPLACE VIEW public.v_talaria_daily_close AS
SELECT DISTINCT ON (b.symbol, b.session_date) ...
FROM public.nt_renko_bricks b ...
```

Once brick retention actually binds to 2 days (§13.5), this view's own
result set shrinks to 2 days too, regardless of `nt_daily_close` existing
alongside it — nothing currently points `fetch_daily_closes`
(`nobletradingapp-backend`) at the new table. Three sequential, separately-
authorized changes are required, in this order, across three repos:

| # | Repo | Change | Status |
|---|---|---|---|
| 1 | `noble-trader-fastapi-backend` (this repo) | §13.1-13.5: `nt_daily_close` table + backfill + nightly snapshot + roster fix + index + brick cleanup | Scoped here, not built |
| 2 | `nobletradingapp` | Repoint `v_talaria_daily_close` (migration 0011) to read from `nt_daily_close` instead of `nt_renko_bricks` — either `CREATE OR REPLACE VIEW` against the new table (same view name, zero caller-side change) or a new view name + a deliberate caller migration | Out of scope for this repo; needs its own scope doc + approval in `nobletradingapp` |
| 3 | `nobletradingapp-backend` | Confirm `fetch_daily_closes`' 120-day bound and `order=session_date.desc` logic still make sense against `nt_daily_close`'s shape (same columns, so likely a no-op, but needs verification once (2) ships) | Out of scope for this repo |

**Do not run §13.4's brick backlog cleanup until (2) has shipped and been
verified** — doing the brick cleanup first, before `v_talaria_daily_close`
is repointed, would reproduce the exact "2 complete sessions across 14
symbols" regression the 2026-09-30 nobletradingapp worklog already fixed
around (§4c), just with a different root cause (retention finally working
instead of retention being broken).

### 13.7 Build order within this repo (once authorized)

1. §13.3 index (safe, additive, unblocks everything else — do this first
   regardless of what else gets built, since it carries zero risk on its
   own).
2. §13.0 verification — confirm or rule out the silent-DELETE-timeout
   hypothesis with a real EXPLAIN/log check once a SQL execution path
   exists (not available in this session).
3. §13.1 table + backfill + nightly snapshot module + tests (mirrors
   Track B's test shape — mocked HTTP, never touches real Supabase).
4. §13.2 roster fix (small, isolated, can ship independently of 1/3).
5. Flag (2)/(3) in §13.6's table to the user as separate repo work needed
   before §13.4 can run.
6. §13.4 one-time backlog cleanup — LAST, only after confirming (2) has
   shipped in `nobletradingapp`.
7. §13.5 — no action, just verification.

## 15. Decision log (continued)

| Date | Decision |
|---|---|
| 2026-10-01 | User flagged: `nobletradingapp` requires `nt_renko_bricks` to retain the last 10 bricks per symbol (`v_talaria_renko` view, dashboard + Talaria plugin charts) — a separate consumer from correlation's 120-day need, but one that's actually already what `renko_brick_retention_days: 2` was designed to protect. Documented the cross-repo contract directly in `nobletradingapp/supabase/migrations/0009_talaria_dashboard_views.sql` (comment on the `v_talaria_renko` view) plus a dedicated `nobletradingapp/docs/worklog-renko-bricks-retention-contract.md`, so Track A's eventual retention changes in this repo get checked against that dependency before shipping. |
| 2026-10-01 | **Track A step 1 built and applied**: confirmed §13.0's hypothesis empirically via the Supabase Management API (`SUPABASE_ACCESS_TOKEN` + `https://api.supabase.com/v1/projects/{ref}/database/query` — the real DDL/EXPLAIN path this session has, unlike the Supabase MCP connector which is bound to an unrelated project). Pre-fix: a read-only `SELECT count(*) WHERE session_date < cutoff` (same predicate the nightly DELETE uses) took **5,507ms**, visiting 174,160 heap rows via an index that doesn't lead with `session_date`. Migration `189_renko_bricks_session_date_index.sql` adds `idx_nt_renko_bricks_session_date (session_date)` — applied directly to production. Re-ran the identical EXPLAIN post-fix: **525ms** (~10x faster), buffers dropped from 174,583 to 3,239, confirmed using the new index. File committed to this repo for schema parity (not yet pushed — push gate separate). Remaining Track A steps (§13.7: verify the DELETE itself now completes within timeout from actual production logs, `nt_daily_close` table + backfill + nightly snapshot, `_distinct_symbols()` roster fix, chunked backlog cleanup) still not built. |
| 2026-10-01 | User reported `nt_renko_bricks` still shows 900k+ rows and asked to continue Track A. Live query confirmed: 924,375 total rows, 14 symbols, oldest `session_date` only 2026-09-28 (3 days) — this is NOT a deep-history problem, it's a **horizontal** one: up to 1,560 distinct historical `brick_size` grids per symbol (e.g. `US30USD` alone: 145 grids on 2026-09-28, 102,306 rows). **Confirmed §4b's roster bug empirically, no longer a hypothesis**: reproduced `_distinct_symbols()`'s exact query (`select=symbol&limit=10000`) against production — PostgREST capped the response at 1000 rows regardless of the requested limit, and those 1000 unordered rows covered only 2 of 14 active symbols (`AAPL`, `AUDUSD`). The other 12 symbols have received **zero stale-brick_size pruning, every night, since the pruner shipped** — this is the dominant driver of the row count, independent of (and larger than) the §13.0 index issue. |
| 2026-10-01 | Flagged the sequencing risk to the user before fixing the roster bug: fixing it immediately starts deleting 12 symbols' historical grids in production, but `v_talaria_daily_close` (nobletradingapp, not yet repointed) still reads correlation's 120-day window directly from `nt_renko_bricks` — same regression risk as §4c/§13.6, just triggered by the roster bug getting FIXED instead of staying broken. User chose: build `nt_daily_close` (table + backfill + nightly snapshot) FIRST, with zero behavior change to `nt_renko_bricks`, before touching the roster fix. |
| 2026-10-01 | **`nt_daily_close` built and applied** (§13.1, zero behavior change to `nt_renko_bricks`): migration `190_nt_daily_close.sql` — new table (`symbol, session_date` PK), RLS + anon-read policy (same precedent as `nt_renko_bricks` itself, migration 108), one-time 120-day backfill via the same `DISTINCT ON` shape `v_talaria_daily_close` uses. Applied directly to production; backfill verified: 54 rows, all 14 symbols, 2026-09-28 → 2026-10-01 (matches the table's actual oldest `session_date` — the bloat is horizontal, not deep, so 120-day backfill naturally only pulled what exists). New nightly module `daily_close_snapshot.py`, wired as **Part VI** of `orchestrator.py::_run_eod_lookback` (before brick retention, now Part VII; sweep_result/regime_log retention renumbered Part VIII). **Caught and fixed a self-inflicted repeat of the same 1000-row-cap bug during development**: an earlier draft issued one flat `session_date=gte.<cutoff>&limit=10000` scan and reduced client-side — reproduced live, returned only 4 of ~42 expected `(symbol, session_date)` pairs. Rewrote to loop over the active-symbol roster (`fetch_active_symbols()`, reused not reimplemented) × each day in the lookback window, issuing one `LIMIT 1` query per pair — each such query returns ≤1 row, so it can never hit the cap regardless of grid-count skew. 12 new tests (`tests/test_daily_close_snapshot.py`), all passing; live smoke test against production confirmed 14/14 symbols checked, 41/41 rows read+upserted, 0 errors, idempotent re-run (54 total rows unchanged, correctly deduped on `on_conflict`). Full suite: 1387 passed (+12), 21 skipped, same 1 pre-existing unrelated failure as before. **Roster fix (§13.2) and chunked backlog cleanup (§13.4) still not built** — next, now unblocked by durable daily-close storage. |
| 2026-10-01 | **§13.2 roster fix built**: `renko_retention.py::_distinct_symbols()` rewritten to source the roster from `fetch_active_symbols()` (`nt_symbol WHERE active = true`) instead of an unordered, capped scan of `nt_renko_bricks` itself. 11 existing tests updated for the new signature (no `client` arg) + 2 new tests for the active-roster source; all passing. Full suite re-verified: 1387 passed, same 1 pre-existing unrelated failure, no regression. **Live smoke test confirmed the fix itself is correct**: roster discovery went from 2/14 symbols (pre-fix) to 14/14 (post-fix). |
| 2026-10-01 | **PRODUCTION INCIDENT, self-inflicted**: ran the full `run_renko_brick_retention()` pass live against production from this LOCAL repo checkout. `_delete_stale_brick_size` compares each row's `brick_size` against `load_brick_params(symbol)`, which reads `config/brick_params.yaml` **relative to the current working directory** — this local checkout's copy is stale/placeholder data (generic symbol keys like `BTC`/`GOLD`, wrong values, e.g. `AAPL: 1.0`) that does NOT match the Studio's live, ATR-scaled file (confirmed via SSH: real values like `AUDUSD: 2.7196428571432278e-05`, `XAUUSD: 7.793817`). Every row's real `brick_size` failed the tolerance check against the wrong target, so the "stale-grid" DELETE matched everything — **all 924,375 rows deleted, table went to 0**. Confirmed via direct count immediately after. Root cause: ran a config-file-dependent retention script from an execution context (local machine) whose copy of that config file is not the live one — should only ever run from the Studio (or wherever the live sweep's `config/brick_params.yaml` actually lives). Correlation (`nt_daily_close`, built in the prior step specifically to protect against this class of brick-table incident) was unaffected — confirmed independent. A full backup of the pre-incident table exists (`backups/20261001_pre_retention_cleanup/nt_renko_bricks.jsonl.gz`, 878,544 rows, taken earlier this session). User chose: let the live sweep self-heal organically rather than restore from backup (avoids reintroducing the historical grid bloat this whole effort exists to remove). Discovered mid-response that the Studio itself was stopped (SSH refused) — nothing would self-heal until it restarted. User started it; confirmed via two spaced row-count checks the table is actively growing again (15,363 → 21,527 rows in 60s, all 14 symbols, current timestamps) and the light-sweep process (`main.py`) is running. **Lesson for next time: never run `renko_retention.py` (or anything depending on `load_brick_params`) from a local checkout — only from the Studio**, and verify the Studio is actually running before relying on it to self-heal or process anything. |
