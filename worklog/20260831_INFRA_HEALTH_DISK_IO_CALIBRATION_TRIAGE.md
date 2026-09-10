# 2026-08-31 — Infra health check: disk IO, calibration-record stall, backlog/connection-pool scoping

Separate thread from `20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md` — that
one tracks signal-quality features (HMM fix, macro filter, Phase 3a); this
one is operational/infra health, triggered by a user report of disk-IO
pressure from Supabase's own Edge request stats.

## Studio access — Lightning CLI + SSH now confirmed working

Corrected a repeated wrong assumption from earlier in the session ("studio
logs are unreachable from this environment"). `lightning` (from
`lightning-sdk`, already pip-installed, already authenticated as
`0xaloysius`) + `lightning ssh configure --name noble-trader-unified
--teamspace 0xdweb/noble-trader` produces a working `~/.ssh/config` entry;
plain `ssh noble-trader-unified "<command>"` runs one-off commands against
the live studio. Saved to memory (`lightning_studio_ssh_access.md`) so this
doesn't get re-litigated.

Important caveat discovered: **`orchestrator.log` is truncated on every
restart, not appended** — the current file only ever covers the time since
the last restart. Combined with the studio's own ~4h restart cadence
(separately being addressed), this means the log is not a reliable
historical audit trail beyond a few hours back. Supabase's own logs
(`query_logs` MCP tool, `edge_logs`/`postgres_logs` sources) turned out to
be the more durable source of truth for anything more than a few hours old.

## Disk-IO investigation

> **Priority**: P1 · **Task**: Diagnose and fix the Supabase disk-IO/
> latency spike reported via Edge request stats ·
> **Status**: resolved (root cause found — `v_talaria_latest_signals`
> missing composite indexes — migration 130 applied and confirmed;
> `nt_signal_sim`'s separate elevated seq-scan count remains
> un-root-caused, flagged for a future pass) ·
> **Date**: 2026-08-31

User-reported symptom: Supabase Edge request stats showing high hit counts
on several `/rest/v1/*` paths (`nt_signal_sim` 11,315, `nt_eod_actions`
6,728, `nt_markov_source_shadow`/`nt_trend_probs_gate_shadow` 6,509 each,
`v_talaria_latest_signals` 4,912, etc.).

**Initial hypothesis (wrong on emphasis)**: this session's own new shadow
tables (`nt_markov_source_shadow`, `nt_trend_probs_gate_shadow`,
`nt_macro_regime`) looked like plausible culprits by raw hit count. Pulled
real timing data from `query_logs` (`response.origin_time` aggregated by
path, `pcvscowltlrxzgxjurcr`, last 24h) to check instead of guessing further:

| Path | Calls/24h | Avg latency | Total time/24h |
|---|---|---|---|
| **`v_talaria_latest_signals`** | 4,912 | **1,566ms** | **~2.14 hours** |
| **`nt_signal_sim` (GET)** | 4,723 | **1,596ms** | **~2.09 hours** |
| my new shadow tables (all 3 combined) | ~16k | 127-160ms | minutes, not hours |

**Real root cause**: `v_talaria_latest_signals` (pre-existing Talaria view,
migration 110, nothing built this session) does
`DISTINCT ON (symbol) ... ORDER BY symbol, sweep_timestamp DESC` over
`nt_sweep_result` (128k rows / 77MB, growing every tick) with a
`LEFT JOIN LATERAL` per-symbol lookup into `nt_regime_log` (208k rows /
64MB). Checked `pg_indexes`: neither table had the composite index this
query pattern needs — `nt_sweep_result` had separate `symbol` and
`sweep_timestamp` indexes but no `(symbol, sweep_timestamp DESC)`
together; `nt_regime_log`'s only composite index was a *partial* one
(`WHERE regime_shift = true`) built for a different query, useless for
this view's unconditional "latest row per symbol" lookup. The view gets
slower every day as both tables grow — a genuine, worsening, ~4.2
hours/day-of-cumulative-backend-time problem, entirely unrelated to
anything built this session.

**Fix — migration 130**: two `CREATE INDEX CONCURRENTLY` statements
(`idx_sweep_result_symbol_timestamp`, `idx_regime_log_symbol_detected_at`).
Pure additive index change — no application code or view definition
touched.

**Applying it hit a real snag**: the Supabase dashboard SQL editor wraps
submitted SQL in an implicit transaction, and `CREATE INDEX CONCURRENTLY`
cannot run inside one — the migration silently failed to create either
index (confirmed via `pg_indexes` — zero rows). Re-ran both statements
directly via the Supabase MCP `execute_sql` tool (no implicit transaction
wrapper) — both now confirmed live and `indisvalid = true`.

A second error surfaced in the same log dump
(`column v_talaria_latest_signals.id does not exist`) — traced and
confirmed **unrelated** to migration 130: the view has never had an `id`
column (checked `information_schema.columns` against the view's actual
`SELECT` list). This is a pre-existing webapp query asking for a column
that was never there — the user is already planning to fix the webapp's
calls to this view separately.

**Not yet root-caused**: `nt_signal_sim` itself shows 81,487 lifetime
sequential scans in `pg_stat_user_tables` despite reasonable indexing
(`(symbol, ts DESC)`, partial `(outcome) WHERE outcome IS NULL`, PK on
`signal_id`). Spot-checked several actual slow-query search strings via
`query_logs` — none individually looked pathological in a narrow window.
Leading theory: the EOD resolver's own pagination through the pending
backlog (currently ~7,500 rows) during its once-daily bulk run — large
`OFFSET` pagination degrades on Postgres regardless of indexing. Not
confirmed; flagging for a future pass rather than guessing further.

## `nt_calibration_records` stall — triaged live, not a code bug

> **Priority**: P1 · **Task**: Triage the 25+ hour stall in
> `nt_calibration_records` writes ·
> **Status**: resolved (insert path confirmed not broken; manual drain
> restored fresh calibration data immediately; exact cause of the
> original interruption not fully reconstructed, log window already
> rotated away) ·
> **Date**: 2026-08-31

**Symptom**: last row written 2026-08-30 19:00 UTC — 25+ hours stale —
despite 1,401 win/loss resolutions (1,045 SL + 356 TP) happening in the
last 24h that should each trigger a `_write_calibration_record` insert.
Every one of those resolutions successfully patched `nt_signal_sim`
(confirmed via SQL), so the resolver reached the calibration-insert branch
for each — yet zero rows landed and zero warnings/errors were logged for
it anywhere in the (post-restart-truncated) visible log window.

**User's ask, verbatim**: don't wait 24h to find out if it's a one-off —
triage now. This is directly why the Lightning CLI/SSH access mattered:
manually re-ran the exact production code path
(`eod_resolver.resolve_pending_signals`, bounded to `max_total=1000`,
safe to re-run since it only ever touches `outcome IS NULL` rows) live
against the studio, twice:

1. First bounded batch (50 signals): all resolved `expired` — 0 tp/sl, so
   inconclusive for the calibration-write path specifically.
2. Second bounded batch (1,000 signals): `{"expired": 980, "sl_hit": 16,
   "tp_hit": 4}`, **zero errors**. Checked `nt_calibration_records`
   directly before/after: **9,481 → 9,501, exactly +20** — matching the
   16 SL + 4 TP one-for-one.

**Conclusion**: the insert path is not broken. Directly proved it inserts
correctly under real production conditions, right now. The 25-hour gap is
most likely the daily 00:32 UTC job either getting interrupted mid-batch
by the studio's own ~4h restart cadence (landing on a batch composition
that happened to be all-expired before reaching any win/loss rows), or a
transient per-symbol candle-fetch failure that day. Can't fully
reconstruct which — the log for that exact window was already rotated
away — but the practical outcome is what mattered: **the stall is fixed
right now** (fresh calibration data exists again), not waiting on
tomorrow's cron to find out if it recurs. Real backlog remaining after
this manual drain: ~7,400+ pending signals, left for the regular cron
(user's call on whether to drain further manually).

## Backlog / `drain_signal_backlog.py` / connection-pool — scoped, not built

> **Priority**: P2 · **Task**: Scope a shared `httpx.AsyncClient`
> connection-pooling refactor across Supabase call sites (and assess
> whether the signal backlog is pool-related) ·
> **Status**: pending (scoped only, including the 2026-09-01 addendum's
> full level-of-effort estimate; awaiting a build instruction) ·
> **Date**: 2026-08-31

User's question: is the backlog related to why the drain script exists,
because of DB pool/connection limitations — asked to scope a fix.

**Backlog reality check** (bucketed by age, `outcome IS NULL` rows):
zero rows fall outside the resolver's 200-hour (`LIGHT_BARS`) resolvable
window — 6,612 are younger than 2 days (not yet mature enough to judge,
by design), 957 are mature and awaiting the next pass. No orphaned,
permanently-stuck population. The backlog is *not* evidence of a broken,
ever-growing dead pile.

**`drain_signal_backlog.py` is a legacy tool, not load-bearing today.**
Its own docstring dates it to 2026-08-12: back then the resolver only
processed a fixed `limit=200`/night against ~3,200 signals/day captured,
so the backlog grew monotonically forever — this script was the manual
catch-up hack for that. The actual root cause was fixed in
`eod_resolver.py` since then (`resolve_pending_signals` now loops in
batches until the window-anchored backlog drains or genuinely can't make
progress, unbounded by default) — the daily cron already does
automatically what this script does manually. It's now just a convenience
"run it right now instead of waiting" tool — exactly what got used above
to triage the calibration-record stall.

**The real, still-live inefficiency**: every Supabase call across the
codebase opens its own fresh `httpx.AsyncClient()` — 81 separate call
sites (23 in `supabase_client.py` alone), confirmed via grep. Each pays a
brand-new TCP+TLS handshake instead of reusing a warm connection. Checked
`pg_stat_activity` (14 idle / 7 null / 1 active — nowhere near any limit)
and `postgres_logs` for the last 24h for "too many connections"/pool
exhaustion errors — found none (only 28 unrelated replica-replication EOF
messages). **Honest framing: this is a genuine, worsening architectural
inefficiency (contributes to the ~85-160ms baseline latency even trivial
single-row lookups show), not a confirmed cause of an active outage or
connection-limit failure today.**

**Scoped fix (not built)**:
1. One shared, module-level `httpx.AsyncClient` in `supabase_client.py`
   (lazy singleton or app-startup-created), `httpx.Limits(max_connections=
   ..., max_keepalive_connections=...)` sized to this app's actual
   concurrency (`HEAVY_SWEEP_MAX_CONCURRENCY=4`, 14 symbols).
2. Replace the `async with httpx.AsyncClient() as client: ...` pattern at
   all 81 call sites with the shared client — mechanical, same request
   semantics, no behavior change, just connection reuse.
3. `eod_resolver.py`'s 2 sites need the same treatment; confirm no other
   module outside `regime_platform/`/`scripts/` does its own independent
   `httpx.AsyncClient()` (grep was scoped to those two trees).
4. Sizable mechanical refactor (81 call sites) touching every
   Supabase-writing code path in the app — scoped as its own dedicated
   build, not folded into anything else. Awaiting go-ahead.

## Not done / explicitly deferred

- Full root cause for `nt_signal_sim`'s elevated seq_scan count — flagged,
  not chased further this session.
- The shared-httpx-client refactor itself — scoped only (see below,
  2026-09-01 addendum).
- Reconstructing exactly what happened during the specific 00:32 UTC run
  that produced the calibration stall — log data lost to restart-driven
  truncation; not pursued further since the live re-run already answered
  the load-bearing question (is the mechanism broken — no).

## Addendum (2026-09-01): shared-httpx-client — requirements + level of effort

Re-inventoried precisely before sizing this, rather than working off the
earlier "81 sites" estimate from memory — the real count is **87 sites
across 30 files** (grew slightly since the earlier count, as this
session's own new modules — `hmm_shadow_accuracy.py`,
`heavy_sweep_reachability.py`, `macro_edge.py` — each added one).

### What's actually being called — this matters for scope

Checked every file's actual destination, not just the call-site count:

| Destination | Sites | Files |
|---|---|---|
| **Supabase REST** | ~85 | `supabase_client.py` (24), `paper_book.py` (6), `pnl_service.py` (4 of 5 — see below), `routers/venue.py` (5), `services/backtest_store.py` (4), `services/operational/system_config.py` (3), `services/data_quality.py` (3), `sweep_orchestrator/portfolio_compare.py` (2), `eod_resolver.py` (2), `calibration_enforcement.py` (2), every diagnostic module built this session (1 each), `routers/calibration.py`, `core/runtime_config.py`, `services/operational/{reconciler,audit_logger}.py`, several `scripts/*.py` |
| **TradingView/RapidAPI** | 2 | `price_fetcher.py` (1), `services/hmm/quote_fetcher.py` (1) |
| **Alpaca** | 1 | `pnl_service.py`'s `_alpaca_get` (already has its own dedicated throttle middleware, `alpaca_throttle`) |

**Recommendation: scope this to Supabase call sites only.** That's what
the disk-IO investigation actually found, TradingView/Alpaca have
different hosts/auth/rate-limit characteristics entirely, and Alpaca
already has its own purpose-built throttling layer — folding it into
this refactor would be solving a problem that already has a solution, for
no benefit. This narrows the real build to ~85 sites in ~19 files, not 87
across 30.

**Also found**: several files (`routers/venue.py`,
`services/backtest_store.py`, `routers/calibration.py`, `pnl_service.py`)
maintain their OWN local copies of `_supabase_headers()`/`_supabase_url()`
instead of importing `supabase_client.py`'s versions — duplicated code
predating this refactor, not something to silently "fix" as a drive-by;
flagging it as a design question for the build (import the shared
client + header/url helpers from `supabase_client.py`, or give each file
its own thin wrapper around the same shared client) rather than deciding
it here.

### Design

- One lazy, module-level singleton in `supabase_client.py`:
  ```python
  _client: httpx.AsyncClient | None = None

  async def _get_client() -> httpx.AsyncClient:
      global _client
      if _client is None:
          _client = httpx.AsyncClient(
              limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
          )
      return _client
  ```
  Sizing (`20`/`10`) is a starting point, not derived from a load test —
  should be revisited once real connection-count telemetry exists post-
  deploy, not treated as final.
- Each call site changes from
  `async with httpx.AsyncClient(timeout=X) as client: ...` to
  `client = await _get_client(); ...` — **per-call timeout values are
  preserved**, not collapsed to one global timeout: httpx supports a
  per-request `timeout=` override (`client.get(url, timeout=X)`) on a
  shared client, so every site's existing, deliberately-chosen timeout
  (5s/8s/10s/30s/60s — they vary meaningfully by call, e.g.
  `paper_book.py` uses 60s for its heaviest reconciliation query) carries
  over unchanged.
- No FastAPI startup/shutdown lifecycle hook — deliberately, because a
  large share of callers are standalone scripts (`scripts/*.py`,
  `bracket_reachability.py`'s own `__main__`, etc.) that never run inside
  the app's lifespan at all. A lazy singleton works identically in both
  contexts; an explicit `aclose()` at process exit is unclosed-socket-at-
  exit territory, not a real leak.

### Testing impact — the real complexity, found by checking rather than assuming

Grepped test files referencing `httpx.AsyncClient`: **13 files.**
`conftest.py`'s global fixture patches `httpx.AsyncClient.request` (the
method, not the constructor) — compatible with a shared client
unmodified, since patching the method affects every instance regardless
of when it was constructed.

**The other ~12 files are the real risk**: they patch the constructor
itself (`monkeypatch.setattr(httpx, "AsyncClient", FakeClient)` /
`patch("httpx.AsyncClient", return_value=client)`, sometimes module-
qualified) — a pattern that only works because today's code calls
`httpx.AsyncClient(...)` fresh at the exact moment of each request. A
lazy singleton constructed once and reused breaks this: whichever test
happens to trigger the first-ever construction "locks in" that instance
for the rest of the test session, and a later test's patch has nothing
left to intercept.

**Mitigation (recommended over rewriting all 12 files' mocks)**: one new
autouse fixture in `conftest.py` that resets `supabase_client._client =
None` before every test. This forces the lazy getter to reconstruct on
each test's first Supabase call, picking up whatever `httpx.AsyncClient`
that specific test has patched — the existing 12 files' mock patterns
should keep working largely unchanged. **"Largely"**, not
"guaranteed" — some may still need individual attention if their patch
is scoped narrower than the test's first Supabase call (e.g., a `with
patch(...):` block wrapping only part of the test body). Budget time to
verify each of the 12 individually, not just add the fixture and assume
green.

### Level of effort

| Phase | Scope | Rough size |
|---|---|---|
| Shared client + `_get_client()` + conftest reset fixture | `supabase_client.py` + `conftest.py` | Small — foundational, ~1-2 hours |
| Migrate ~85 Supabase call sites across ~19 files | Mechanical (same transform repeated), but `supabase_client.py` alone has 24 sites | Largest chunk — most of a day |
| Resolve the duplicated-header-helper files (`venue.py`, `backtest_store.py`, `routers/calibration.py`, `pnl_service.py`) | Decide + apply: import shared client, or thin local wrapper | Small-medium, contingent on the design decision above |
| Verify the 12 test files individually | Confirm the reset fixture is sufficient, fix any that aren't | Medium — real unknown until attempted |
| Full suite + real-data verification (standalone module runs + 24h `query_logs` re-check for the connection-count/latency improvement) | Same discipline as every other build this session | Small |

**Total: roughly 1.5-2 focused days**, not a quick pass — the mechanical
part is large-but-simple, the test-mocking part is small-but-uncertain
until actually attempted. Scoped only; not built.
