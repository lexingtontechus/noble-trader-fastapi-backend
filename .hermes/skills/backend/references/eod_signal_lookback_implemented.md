# EOD Signal-Lookback — Implemented (2026-08-06)

Backend-only signal-generation lookback (NO agent component). Every generated
BUY/SELL signal is captured to Supabase `nt_signal_sim`; a daily resolver
scores each one forward against post-entry TDVA 1h bars (tp_hit / sl_hit /
expired); calibration bias, sweep drift, exit attribution and parameter-decay
metrics are computed; a self-correction pass writes audit rows to
`nt_eod_actions`.

Plan doc: `.hermes/plans/eod_signal_lookback.md` (includes a Deployment
status section). **Committed + pushed**: `1feb82e` (feat) + `498359a` (docs)
→ origin/main. Studio deploy + SQL migration application are **user-owned**
(see Deployment status below — the agent has NO remote-exec path into the
studio or the Supabase DB).

## Architecture

```
Sweep tick (_process_symbol light, run_heavy_sweep_for_symbol heavy)
  ├─ trade_signal = {signal, entry_price, stop_loss, take_profit}
  ├─ [CAPTURE] if signal not in (neutral, unknown):
  │     _build_signal_sim_payload(...) -> asyncio.create_task(log_signal_sim(payload))
  │     (fire-and-forget; NEVER break the sweep; RuntimeError -> skip)
  └─ log_sweep_result -> nt_sweep_result (existing)

EOD resolver (regime_platform/sweep_orchestrator/eod_resolver.py, CLI: python -m ...)
  └─ rows WHERE outcome IS NULL -> fetch post-entry 1h closes ->
       close-walk TP/SL -> PATCH outcome -> insert nt_calibration_records

EOD correction (regime_platform/sweep_orchestrator/eod_correction.py, CLI: python -m ...)
  └─ fetch_calibration_bias -> |bias|>0.10 -> emergency/bayesian/kelly audit rows
```

## Schema (migrations 102 + 103)

- `nt_signal_sim`: signal_id UUID PK (NO DB default — capture must supply it),
  symbol, venue, ts, signal, entry_price/stop_loss/take_profit, brick_size,
  sl_bricks, tp_bricks, p_win, ev, regime, regime_conf, source
  (sweep-light|sweep-heavy), qualified, outcome (tp_hit|sl_hit|expired|open),
  exit_price, exit_ts, hold_bars, mfe, mae, pnl_bricks, pnl_dollars, resolved_at.
  Index (symbol, ts DESC); partial index on outcome WHERE outcome IS NULL.
- `nt_eod_actions`: action_id, symbol, ts, action
  (emergency_heavy_sweep|bayesian_penalty|kelly_reduction|parameter_decay_check),
  bias, detail jsonb.
- Views (103): v_eod_calibration_bias, v_eod_sweep_drift,
  v_eod_exit_attribution, v_eod_parameter_decay (all CREATE OR REPLACE VIEW).

## Capture hooks (TWO call sites — easy to miss the heavy one)

1. Light sweep: `orchestrator.py _process_symbol` — placed AFTER the Redis
   publish so `qualified` is FINAL (cooldown + content-dedup can flip it).
   `_build_signal_sim_payload` is a module-level helper in orchestrator.py
   (keyword-only args, 6dp price rounding).
2. Heavy sweep: `heavy_sweep.py run_heavy_sweep_for_symbol` §10a — added
   separately; the heavy path does NOT call `_process_symbol`, so without
   this block heavy signals are never captured. Uses the same
   `_build_signal_sim_payload` (imported lazily inside the function).

`source` param: `_process_symbol(source: str = "sweep-light")`; heavy caller
passes `source="sweep-heavy"` (or the §10a block hardcodes it).

## Resolver semantics (eod_resolver.py)

- `resolve_signal_outcome(entry, stop, tp, closes, direction, expiry_bars)`
  — pure close-walk, testable. BUY: tp_hit when close >= TP (checked FIRST),
  sl_hit when close <= SL. SELL: tp_hit when close <= TP, sl_hit when close
  >= SL. Same-bar TP priority. Expired after EXPIRY_BARS=48 with no touch.
  mfe/mae measured over walked bars.
- `compute_pnl(outcome, tp_bricks, sl_bricks, brick_size)`: tp_hit=+tp_bricks,
  sl_hit=-sl_bricks (abs() guard), expired=0; pnl_dollars = pnl_bricks * brick.
- Calibration records: ONLY tp_hit/sl_hit -> win/loss. Expired is NOT written
  to nt_calibration_records (a timeout is neither win nor loss; counting it
  as loss understates model p_win).
- exit_ts approximated as signal.ts + hold_bars hours (fetcher drops bar
  timestamps).
- REST: `_fetch_pending` uses `outcome=is.null&order=ts.asc`; `_patch_signal`
  PATCHes by `signal_id=eq.<uuid>` (PostgREST handles UUID-vs-string).

## Scheduler wiring (orchestrator.py)

- `eod-lookback` APScheduler job: `CronTrigger.from_crontab("30 0 * * *")`
  (daily **00:30 UTC**) → `self._run_eod_lookback()`. Registered in `start()`
  right after the `refresh-symbols` interval job; `misfire_grace_time=3600`,
  `coalesce=True`. Verified next-fire `2026-08-08 00:30 UTC` — the `30 0 * * *`
  form is unambiguous (unlike day-of-week).
- `_run_eod_lookback` calls `eod_resolver.resolve_pending_signals()` then
  `eod_correction.run_eod_correction()` in try/except blocks — best-effort,
  never raised out of the scheduler tick.
- Cron verification one-liner used this session:
  `CronTrigger.from_crontab('30 0 * * *', timezone='UTC').get_next_fire_time(None, datetime(2026,8,7,12,0,tzinfo=timezone.utc))`.

## Deployment status (committed + pushed, 2026-08-06)

- Commits: `1feb82e` feat(eod) — 11 files / 1797 insertions; `498359a`
  docs(eod) plan status. Working tree clean.
- Full suite: 267 passed / 23 skipped / 0 failed (run with
  `-p no:cacheprovider`).
- **Studio deploy — USER-OWNED.** The agent CANNOT run `git pull` in
  LightningAI: SSH publickey denied (key not server-registered), `studio cp`
  uploads do NOT land (`.lightninglink` indirection — the process reads a
  uv-cache editable snapshot), and `lightning studio` has NO exec/shell/run
  subcommand (only connect/cp/ls/rm/ssh/start/stop/env/switch). User command:
  `cd /teamspace/studios/this_studio/noble-trader && git pull origin master`,
  then restart / `uv pip install -e .` to rebuild the editable snapshot.
- **SQL migrations — USER-OWNED.** No `sbp_` PAT exists (env
  `SUPABASE_ACCESS_TOKEN` is `sb_publi…` — anon/publishable, NOT a
  management-API PAT; the management query endpoint returns
  "JWT could not be decoded"), no psql/asyncpg/psycopg2 in the venv, no
  Postgres DSN. Prior migrations (100/101) were applied via the Supabase
  dashboard SQL editor. User action: paste the COMBINED file
  `supabase/migrations/104_eod_lookback_combined.sql` (concatenation of 102
  + 103, created for exactly this) into dashboard → SQL editor.
- When the user says "I can run both sql scripts + lightning deployment",
  hand them those two exact steps; do NOT burn time re-attempting SSH/cp.

## Correction semantics (eod_correction.py)

- `bayesian_adjusted_p_win(p_win, n)`: (p_win*n + 0.5*10)/(n+10).
- `_classify_bias(bias)`: >0.10 OVERCONFIDENT, <-0.10 UNDERCONFIDENT, else
  CALIBRATED.
- OVERCONFIDENT/UNDERCONFIDENT -> three nt_eod_actions rows
  (emergency_heavy_sweep, bayesian_penalty, kelly_reduction ×0.5).
  NOTE: it is an AUDIT/RECOMMENDATION pass only — the actual heavy-sweep
  trigger is consumed by an orchestrator hook wired separately.
- CALIBRATED -> parameter_decay_check when realized wr declined >0.05
  (last 3 days vs prior 3 days from nt_signal_sim).

## Integration gotchas (found during assembly — do NOT repeat)

0. **Supabase migration column/policy gotchas (BOTH bit LIVE on the user's
   first apply — fixed in `9f396f2` + `7ad9cdd`):**
   - **nt_sweep_result has NO `created_at` — the timestamp column is
     `sweep_timestamp`.** Any view joining nt_sweep_result must use
     `sweep_timestamp` (hit in v_eod_sweep_drift → whole migration failed
     with `column "created_at" does not exist`). Verified via live REST
     `?select=*&limit=1` column list.
   - **`CREATE POLICY` has no `IF NOT EXISTS`.** Re-running the combined
     migration after the table (and its auto policy) exists fails with
     `ERROR 42710: policy "Service role full access" already exists`.
     Idempotent pattern:
     ```sql
     ALTER TABLE x ENABLE ROW LEVEL SECURITY;
     DROP POLICY IF EXISTS "Service role full access" ON x;
     CREATE POLICY "Service role full access" ON x FOR ALL USING (true) WITH CHECK (true);
     ```
   - Combined `104_eod_lookback_combined.sql` is regenerated from fixed
     102+103 — it is convenience-only for the dashboard SQL editor; 102/103
     are canonical. Re-running 104 after partial apply is now safe.
   - **Postgres 42803** (hit in migration 105 v_paper_equity): do NOT mix a
     plain aggregate and a window function over the SAME column in one
     SELECT that has GROUP BY. Fix: aggregate in the CTE (`GROUP BY 1`),
     then window `SUM(realized_pnl) OVER (ORDER BY day ...)` over the
     pre-aggregated column in the outer query (no GROUP BY in outer).
     See `references/paper_portfolio.md` for the fixed view shape.
1. **signal_id UUID must be injected in `log_signal_sim`** (supabase_client.py),
   NOT in the payload builder — migration declares `signal_id UUID PRIMARY KEY`
   with no default, so an insert without it fails NOT NULL. Add `import uuid`;
   `sim_payload["signal_id"] = str(uuid.uuid4())`.
2. **Heavy-sweep capture is a separate block** — the heavy path never calls
   `_process_symbol`; forgetting §10a means heavy signals never reach
   nt_signal_sim.
3. `fetch_calibration_bias` was enhanced to return
   `{bias, status, n, win_rate, avg_pred}` (was {bias, status}) so the
   correction module can read `n`.
4. Run full suite with `-p no:cacheprovider` on this repo — plain
   `pytest tests/ -q` can appear to hang (cold cacheprovider on the heavy
   backtest files); the cached run takes ~100s, not 20+ min.

## Parallel sub-agent lessons (used for this build)

- Sub-agents editing the SAME repo in parallel WILL collide on shared files
  (two agents wrote `tests/test_signal_sim_capture.py`; the parent's
  write_file overwrote the sibling's version — reconcile by checking
  on-disk state, not the transcript).
- Sub-agents can be truncated by API rate limits mid-task (503/429) and still
  report `status=completed` with an error summary. ALWAYS verify on-disk
  deliverables after delegation — this session 2 of 3 agents finished early
  (missing eod_correction.py + both test files), which the parent completed.
- Split by FILE OWNERSHIP, not by phase: Agent A = new .sql files only,
  Agent B = orchestrator+supabase_client, Agent C = new eod_*.py modules.
  Even so, parents must own integration (UUID injection, heavy capture
  block, full-suite run).

## Tests + counts

- tests/test_eod_resolver.py (16) + tests/test_signal_sim_capture.py (12) =
  28 tests, all pass.
- **Part II paper portfolio (2026-08-06, commit `5b27aed`)** adds
  tests/test_paper_book.py (10) + kelly assertions → full suite is now
  **277 passed / 23 skipped / 0 failed**. See `references/paper_portfolio.md`.
- Full suite after EOD build: **267 passed / 23 skipped / 0 failed**
  (baseline before EOD: 239 passed / 23 skipped).
- The 23 skips are unchanged intentional deprecations (v7.3 walk_forward/
  monte_carlo, P5 execution-modeling fields, env-dependent cache tests).
