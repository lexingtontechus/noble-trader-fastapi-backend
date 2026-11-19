# Supabase Logs Ingest — composition, Kairos requirements, and reduction plan

**Date:** 2026-10-24
**Type:** Evidence note (measurement + proposed levers). **No behaviour change yet.**
**Trigger:** operator screenshot of the org usage page — 1.32 GB Logs Ingest against a
1 GB free-plan quota (0.32 GB overage), daily bars roughly 20–104 MB, 2026-10-03 → 10-24.
**Project:** `pcvscowltlrxzgxjurcr` (NobleTrading, us-west-1, PG 17.11) — the org's only project.
**Method:** ClickHouse `logs` stream via the Management API `query_logs` endpoint (read-only),
plus `pg_settings` / `pg_indexes` / `pg_stat_*` / `cron.job` reads. No writes were issued.

---

## 1. Headline

**Kairos is not the problem.** Over a measured 24 h window, the `nt_*` / `v_talaria_*` REST
surface owned by Kairos accounts for **2.5 % of total ingest**. The billing driver is the
**nobletradingapp commerce surface** — five `users` / `subscriptions` / `subscription_events` /
`payment_transactions` / `plans` paths that together are **76 % of the window**.

The Postgres log-config levers the docs lead with are **already applied** by the platform
(§3). There is no free win in `pg_settings`. The remaining levers are all *request-count*
levers, and they live in nobletradingapp and in pg_cron, not in Kairos' Python.

---

## 2. What is actually being logged

Measured window **2026-10-23T23:59Z → 2026-10-24T23:59Z** (the API caps `query_logs` at 24 h).
Total **9,294 events / 13.05 MB** of `event_message + log_attributes`.

| source | events | bytes | share | what it is |
|---|---:|---:|---:|---|
| `edge_logs` | 4,842 | 10,140,252 | **77.7 %** | API-gateway access log — one line per HTTP request to any `/rest/v1/*`, `/auth/*`, `/functions/*` |
| `postgres_logs` | 2,008 | 1,320,833 | 10.1 % | Postgres server log: pg_cron start/completed pairs, checkpoints, restart lines |
| `function_logs` | 1,511 | 956,089 | 7.3 % | Edge Function `console.*` output (per invocation) |
| `function_edge_logs` | 105 | 214,278 | 1.6 % | Edge-runtime lifecycle (Boot / Shutdown / "booted (time: Nms)") |
| `postgrest_logs` | 284 | 104,995 | 0.8 % | PostgREST internal — 132 × *"Warp server error: Thread killed by timeout manager"* |
| `pgbouncer_logs` | 287 | 100,099 | 0.8 % | pgbouncer connect/close pairs |
| `auth_logs` | 84 | 46,877 | 0.4 % | GoTrue health probes |
| `realtime_logs` | 14 | 10,099 | 0.1 % | |
| `storage_logs` | 7 | 7,994 | 0.1 % | |
| `supavisor_logs` | 2 | 1,711 | 0.0 % | |

**Measurement caveats — read before quoting any of this:**
- The window is a **light** day. Two consecutive 24 h windows measured 12.94 MB and 13.05 MB,
  but the dashboard's cycle average is ~60 MB/day (1.32 GB / ~22 days). Absolute bytes here are
  a lower bound; **the composition percentages are the transferable result.**
- Counts drift **upward between queries** (e.g. `edge_logs` returned 4,842 then 4,920 for the
  same window; `pgbouncer_logs` 285 then 287) because the window is filtered on *event*
  `timestamp` and late-arriving events land after the query starts. Treat ±2 % as noise.
- One per-IP aggregate returned exactly 1,000 rows — the API caps large result sets. Use
  aggregates, not raw row dumps.

### 2a. Inside `edge_logs` — the 76 % that matters

| `request.path` | method | events | bytes | share of window |
|---|---|---:|---:|---:|
| `/rest/v1/subscriptions` | GET | 1,781 | 3,929,627 | 30.1 % |
| `/rest/v1/users` | GET | 1,895 | 3,737,031 | 28.6 % |
| `/rest/v1/subscription_events` | GET | 471 | 1,082,026 | 8.3 % |
| `/rest/v1/payment_transactions` | POST | 473 | 959,407 | 7.3 % |
| `/rest/v1/plans` | GET | 103 | 216,219 | 1.7 % |
| **commerce subtotal** | | **4,723** | **9,924,310** | **76.0 %** |
| `/rest/v1/v_talaria_daily_close` | GET | 32 | 80,053 | 0.6 % |
| `/rest/v1/v_talaria_latest_signals` | GET | 8 | 21,784 | 0.2 % |
| `/rest/v1/v_talaria_symbols` | GET | 8 | 16,081 | 0.1 % |
| `/rest/v1/v_signal_agreement_cache` | POST | 8 | 16,046 | 0.1 % |
| `/rest/v1/v_correlation_cache` | POST | 8 | 15,924 | 0.1 % |
| other `nt_*` / `v_*` / `*_test` | mixed | 100 | 200,660 | 1.5 % |
| `/auth/v1/health`, `/admin/*`, `/rest-admin/*` | mixed | 42 | 40,000 | 0.3 % |

All 4,723 commerce requests carry `user_agent = Deno/2.1.4 (variant; SupabaseEdgeRuntime/1.77.7)`.
**Kairos' entire REST footprint is 147 requests / 325,987 bytes (2.5 %)**, all
`python-httpx/0.28.1`.

Two shapes dominate, and both are *unconditional*:

| URL shape | events/24h | bytes/24h |
|---|---:|---:|
| `/rest/v1/users?select=id,clerk_user_id,role&email=ilike.<user>` | 656 (dev) + 280 + 188 + … | 1,181,763 + 836,556 + … |
| `/rest/v1/subscriptions?select=user_id&helio_discord_id=eq.<id>&limit=1` | 656 | 1,181,763 |

### 2b. Inside `function_logs` — 6.5 % of the window is three repeating messages

| message | level | events | bytes |
|---|---|---:|---:|
| `No local user for Helio sub <id> (email=…, discord=…)` | warning | 864 | 520,224 |
| `Clerk metadata sync 404: {"errors":[{"message":"not found"…}]}` | error | 475 | 337,250 |
| `booted (time: Nms)` / `shutdown` | log | 212 | 123,661 |

The 864 "No local user" warnings are ~**9 distinct subscription ids × 94 sync runs** — the
same 9 lines re-emitted on every 15-minute poll. The 475 Clerk 404s are **3 distinct
`clerk_user_id`s × 94 runs**.

### 2c. Inside `postgres_logs` — pg_cron bookkeeping, not queries

| message | events | bytes |
|---|---:|---:|
| `cron job 924 starting/completed: SELECT public.refresh_talaria_dashboard_cache()` | 563 | 359,706 |
| `checkpoint starting: time` | 278 | 153,422 |
| `cron job 886 starting/completed: select` (Studio `/health` probe) | 281 | 177,305 |
| `cron job 920 starting/completed: select` (Render `/health` probe) | 188 | 117,415 |
| `cron job 917 starting/completed` (helio-subscription-sync) | 188 | 117,543 |
| `schema "noble" does not exist` | 4 | 25,251 |

**pg_cron emits two log lines per run, unconditionally.** That is 1,438 of the 2,008
`postgres_logs` events, and it is the only structural Postgres-side cost left.

---

## 3. Kairos' logging requirements — what must stay, what is free

### 3a. Current Postgres log config (read live from `pg_settings`)

| setting | live value | source | verdict |
|---|---|---|---|
| `log_statement` | **`ddl`** | `postgresql.conf` | ✅ correct — Kairos never needs statement text; its audit trail lives in `nt_*` tables |
| `log_min_duration_statement` | **`-1`** (off) | default | ⚠️ no slow-query coverage at all — see §3c |
| `log_min_messages` | `warning` | default | ✅ docs' recommended default |
| `log_connections` / `log_disconnections` | `off` | platform-defaults | ✅ already off |
| `log_autovacuum_min_duration` | `600000` ms (10 min) | default | ✅ effectively off for normal runs |
| `log_temp_files` | `-1` (off) | default | ✅ off |
| `log_checkpoints` | `on` | platform-defaults | ✅ leave alone — low volume, and it is the evidence trail for the 2026-09-27 IO incident |
| `log_lock_waits` | `on` | platform-defaults | ✅ leave alone — produced **zero** lines in the measured window, so it costs nothing today |
| `log_duration` | `off` | default | ✅ off |
| `pgaudit.log` / `pgaudit.role` | `none` / `''` | default | ✅ pgAudit is **not installed** (`pg_extension` has no `pgaudit`) — nothing to tune |

**Conclusion: the docs' "two biggest levers" (`log_statement`, `log_min_duration_statement`)
and the "often-overlooked" set are already in their best state.** Do not spend a migration on
`pg_settings` — there is no volume to recover there. The one setting that is *deliberately
off* is `log_min_duration_statement`, which is a coverage decision, not a cost decision (§3c).

### 3b. Kairos' actual footprint

| what | measured | requirement |
|---|---:|---|
| REST calls to `nt_*` / `v_talaria_*` / cache views | **147 req / 326 KB per 24 h** | keep — this is the durable audit mirror |
| `nt_eod_actions` audit writes | part of the above | **keep** — CHECK-constrained audit log, the repo's designated audit surface |
| `nt_tuning_params` ledger | 15 POST / 24 KB per 24 h (3,940 rows total, append-only) | **keep** — read-precedence tier 2 (`config/*` → table → settings default) |
| `nt_roadmap` heartbeats | 0 REST reads/writes observed in the window | keep — liveness gate input (`pulse_check_r2.py`, `watchdog_liveness.py`) |
| `weekly_maintenance()` `RAISE NOTICE` (~30/run) | weekly | **negligible** — 1 run/week |
| `refresh_talaria_dashboard_cache()` skip-notice | 0 hits (0 failed cron runs in 2 days) | **keep** — it is the only diagnostic for the overlap guard added in migration 175 |

**Kairos requirement statement:** Kairos needs *per-write audit* and *per-sweep liveness*, both
of which are satisfied by its own `nt_*` tables. It needs **no** Postgres statement logging and
**no** connection logging. Its current config is already aligned with that.

### 3c. The one open coverage gap (flag, do not act)

`log_min_duration_statement = -1` means a slow query leaves **no trace**. This repo has
already been bitten by exactly that class: migration 175's header records **~27 GB of
`temp_blks_written`** from six `ORDER BY ts ASC` query shapes, out of 649 GB lifetime
`temp_bytes`, discovered only via `pg_stat_statements` (which is installed and retained).
`pg_stat_statements` is a *cumulative* counter, so it cannot tell you *when* a regression
appeared. If a slow-query tripwire is wanted, the cheap form is
`log_min_duration_statement = 1000` (1 s) — high enough to stay quiet on a 60-connection
Micro tier, low enough to catch a 10× regression. **This adds volume, so it is the opposite of
this note's goal and should be a separate, deliberate decision** — not bundled into a
cost-reduction change.

---

## 4. Reduction levers, ordered by measured impact

All figures are share of the measured 13.05 MB/24 h window. Percentages transfer; absolute
bytes are a light-day lower bound.

| # | lever | where | est. cut | risk |
|---|---|---|---:|---|
| **L1** | Stop the Helio sync from re-emitting an unchanged reconciliation every 15 min | `nobletradingapp/supabase/functions/helio-subscription-sync/index.ts` | **~45–55 %** | low — data unchanged, only the write path short-circuits |
| **L2** | Raise the sync cadence 15 min → 60 min (or event-driven via webhook only) | `cron.job` 917 | ~**18 %** (≈¼ of L1's table set) | low–med — checkout UX already has a 15 s client-fired `/api/helio-sync` path |
| **L3** | Dedupe the two repeating `function_logs` messages (warn once per sub-id / clerk-id, with a cooldown) | same edge function | **~6.5 %** | very low |
| **L4** | Shorten `refresh_talaria_dashboard_cache` from 5 min to 15–30 min | `cron.job` 924 (migration 175/178/193) | ~**2.8 %** | very low — dashboard's own poll fallback is 300 s |
| **L5** | Retire or repoint the two `/health` probe crons (886, 920) | `cron.job` | ~**2.1 %** | **needs a decision** — see §5 |
| **L6** | Investigate the 132 × "Warp server error: Thread killed by timeout manager" | PostgREST, not a log-volume fix | ~0.3 % | symptom, not cost |

### L1 in detail — the real 76 % fix

Each `helio-subscription-sync` run (94 per 24 h) walks every active Helio paylink and, for each,
issues a fixed sequence regardless of whether anything changed:

```
users?email=ilike.<email>            → localUser        (1)
subscriptions?helio_discord_id=eq.<id> → fallback lookup   (1)
subscriptions?or=(helio_subscription_id.eq.<id>,and(user_id…)) → existing row  (1)
payment_transactions upsert           → unconditional      (1)
subscription_events?…helio_transaction_id=eq.<tx> → dedupe check  (1)
users PATCH discord_id                → unconditional      (1)
```

That is ~6 calls × 9 active subscriptions × 94 runs ≈ **5,000 requests/24 h** — matching the
measured 4,723 commerce requests almost exactly. The function *already* computes
`summary.unchanged` for the status/period case, but it still pays for the lookup and the
`payment_transactions` upsert on every tick.

Two concrete changes, in order of safety:
1. **Cache the "nothing changed" verdict.** The function already knows `existing.status ===
   status && existing.current_period_end === periodEnd` → `summary.unchanged`. Skip the
   `maybeInsertTransaction` / `maybeInsertEvent` / `users` PATCH when unchanged *and* the
   transaction id is unchanged. Saves ~2 of 6 calls.
2. **Guard the Clerk metadata sync** the same way — it is what produces the 475 404s.

Do **not** remove the reconciliation entirely: its own header states the webhook is
unreliable and this poller is the backstop (2026-08-09). L1 reduces *volume*, not *coverage*.

### L2 is the bigger structural lever

Even with L1 applied, 94 runs/day of a full Helio walk is the design. The function's header
says the 15-min cadence exists because "the Helio webhook is not robust"; but the checkout
path already fires `/api/helio-sync` every 15 s client-side while `status === pending`
(`app/portal/page.jsx:150-165`). Moving the cron to 60 min and letting the client path carry
the checkout window is the cleaner shape. **Estimate ~1 h to implement + verify, confidence:
medium** — it is a cadence change, not a code deletion.

### L4 in detail

`cron.job` 924 runs 288 times/24 h, each emitting 2 Postgres lines (~360 KB/24 h) plus the
dashboard's own 12-view aggregation. The dashboard's client poll fallback is 300 s
(`app/dashboard/page.jsx:3061`), so 5-min freshness is already finer than anything reads it.
Migration 178 moved 2 min → 5 min; 15 min is the next step and is a one-line
`cron.alter_job`. **Est. ~2.8 %, ~10 min, confidence: high.**

---

## 5. What needs a decision, not a measurement

1. **The `/health` probe crons (886, 920).** 886 probes the Studio `/health` every 10 min
   (143 runs/24 h), 920 probes the Render backend every ~14 min (95 runs/24 h). Both exist to
   detect an outage — but `scripts/watchdog_liveness.py` is the repo's designated off-box
   liveness gate, and neither cron writes anything (both are a bare `net.http_get`). If
   `watchdog_liveness.py` is the live gate, these two crons are **pure log volume**:
   ~238 lines / ~295 KB per 24 h. **Est. resolution: retire or repoint, ~2.1 %,
   confidence: medium** (depends on whether an operator still reads these as the outage signal).
2. **Plan tier.** At 1.32 GB/month, Supabase Pro ($25/mo) includes **20 GB** — the overage
   disappears entirely and no code changes are needed. Reducing to fit the 1 GB free quota
   means cutting ~25 % of ingest, which is *less* than L1 alone achieves. This is the
   cheapest option by a wide margin and should be weighed before any of the above.
3. **Whether the dashboard's 1.32 GB is the same quantity I measured.** My 24 h windows are
   ~13 MB; the cycle average is ~60 MB/day. The gap is either day-to-day variance (the chart's
   bars span 20–104 MB) or the `logs` stream under-reporting relative to the ingest meter.
   **Before acting on absolute numbers, confirm the dashboard's per-project breakdown and the
   exact cycle dates.** The composition in §2 is robust either way; the total is not.

---

## 6. Not worth doing

- **`pg_settings` migrations.** §3a — every tunable is already in its best state.
- **`pgAudit`.** Not installed.
- **`log_checkpoints` / `log_lock_waits`.** Zero measured volume; `log_checkpoints` is the
  incident trail.
- **Log drains.** Draining does **not** reduce ingest — it is a separate billing primitive
  (`docs/guides/platform/manage-your-usage/log-drains`).
- **The `*_test` tables.** 100 requests / 200 KB per 24 h (1.5 %). They are the safety net
  from `tests/conftest.py:44` that stops tests from writing to production tables. Keep them;
  note that `nt_signal_sim_test` (21 requests, 404s) has no `_test` counterpart for some
  shapes — a 404 is the intended behaviour, not a bug.

---

## 7. Job 924 `refresh_talaria_dashboard_cache` — slow-query review (2026-10-25)

A CPU/I/O cost, **not** a log-ingest contributor — `log_min_duration_statement = -1` and
`log_statement = ddl` mean none of its work is written to the log. Reviewed because it is
the largest recurring DB job on the instance.

### 7a. Measured runtime

| source | value |
| --- | --- |
| 4 direct samples (probe function) | 883 / 1,141 / 1,375 / **7,626 ms** |
| `cron.job_run_details`, 863 runs / 3 days | avg **4,903 ms**, p95 5,530, max 12,421, **0 failures** |
| `pg_stat_statements` | mean 3,493 ms |
| cadence | `2,7,12,…,57 * * * *` = every 5 min = **288 runs/day** → ~17–23 min CPU/day |

Not broken: `pg_try_advisory_xact_lock` prevents overlap, `SET statement_timeout TO '30s'`.

### 7b. Migration 175's premise is stale

175 was written to fix a 15–140 s refresh blowing the 120 s timeout. Today:

- `REFRESH MATERIALIZED VIEW CONCURRENTLY v_talaria_latest_signals` = **24 ms standalone,
  27 ms inside the function** (probe function returned `CONCURRENTLY_OK` — it *does* execute
  inside the plpgsql body; the matview is genuinely maintained).
- The live function carries `SET statement_timeout TO '30s'`, not 120 s.
- The live `renkoBricks` block is the migration-184 form (`live_brick_size` match, `LIMIT 10`),
  not 175's.

### 7c. The real cost — the payload fan-out

One run touches **454,215 rows** to emit a **26 kB** JSONB:

| relation | node accesses | rows touched |
| --- | --- | --- |
| `nt_signal_sim` (65 MB, 96,576 rows) | **11** (8 seq scans) | **345,334** |
| `nt_paper_positions` (5,520 rows) | **17** (16 seq scans) | 82,147 |
| `nt_calibration_records` | 3 | 22,044 |

2.68 GB of buffer accesses (350,923 pages) per run.

### 7d. Why it swings 883 ms → 7.6 s

`shared_buffers` = 28,672 pages = **224 MB** (free-tier limit, not changeable). The hot
working set is ~177 MB (`nt_signal_sim` 65 + `nt_sweep_result` 91 + `nt_calibration_records` 21)
plus indexes. Every 5-min run re-reads it; whether it is resident decides 900 ms vs 7,600 ms.
The first sample after a pause was the slowest — cold cache.

### 7e. Hot spots (one measured run)

| subquery | ms |
| --- | --- |
| `portfolioStats` (`v_talaria_portfolio_stats`) | 397 |
| `signalHealth` (`v_talaria_signal_health`) | 209 |
| `calibrationBoard` (`v_calibration_confidence_board`) | 178 |
| `vsEqualSymbol` (`v_paper_vs_optimized_symbol`) | 109 |
| `vsEqual` (`v_paper_vs_optimized_daily`) | 75 |
| `renkoBricks` | 46 |
| `outcomeBySide` (`v_signal_outcome_by_side`) | 30 |

Worst single plan shape: `v_paper_vs_optimized_symbol` runs a **Nested Loop with 96,576 index
probes** into `nt_paper_positions` over a full scan of `nt_signal_sim`.

### 7f. Levers, in order of leverage

1. **Cadence 5 min → 15 or 30 min.** Zero code, 3–6× less CPU and I/O. The payload is a 26 kB
   snapshot and the underlying sweep does not produce new data sub-5-min. Same lever as L2.
2. **Stop re-reading `nt_signal_sim` 8×.** One pass materialising per-symbol aggregates, or a
   covering index, removes most of the 345k rows.
3. **Fix the 96,576-probe nested loop** in `v_paper_vs_optimized_symbol` (hash join / pre-aggregate).
4. **`v_talaria_portfolio_stats` seq-scans `nt_paper_positions` 16×** — its equity-curve CTE is
   recomputed ~6×; `MATERIALIZED` CTEs or a single-pass rewrite.

### 7g. Not a defect

`nt_signal_sim` shows 16,942 dead tuples with `last_autovacuum = null`. Correct behaviour, not a
bug: the threshold is 50 + 0.2 × 96,576 = 19,365, so autovacuum simply has not triggered yet.

---

## 8. Related

- `20261016_SCALE_ARCHITECTURE_AND_GPU_DECISION.md` — the compute-sizing companion note.
- `20261017_HEAVY_SWEEP_GRID_AND_COST_NOTE.md` — the same "measure, don't quote a duration"
  discipline applied to the sweep.
- `supabase/migrations/175_dashboard_cache_refresh_guard_and_signal_sim_ts_index.sql` — the
  27 GB `temp_blks_written` finding behind §3c.
- `supabase/migrations/178_stagger_cron_schedules.sql` — the prior cron-cadence reduction.
