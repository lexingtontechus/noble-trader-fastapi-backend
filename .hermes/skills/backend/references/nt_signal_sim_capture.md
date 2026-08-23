# nt_signal_sim signal-capture hook (2026-08-06, built)

Every generated non-neutral signal (BUY/SELL — qualified OR not) is
fire-and-forget persisted to the `nt_signal_sim` Supabase table for the
EOD signal simulator.

## Schema facts (verified live via PostgREST — treat as ground truth)

- `nt_sweep_result` has NO `created_at` — timestamp column is **`sweep_timestamp`**.
  Views joining it MUST use `sweep_timestamp` (a `created_at` ref fails live with
  `column "created_at" does not exist`).
- `CREATE POLICY` has NO `IF NOT EXISTS` — idempotent re-runnable migrations MUST
  `DROP POLICY IF EXISTS "Service role full access" ON <table>` before
  `CREATE POLICY` (else `ERROR: 42710 ... already exists`).
- PostgREST returns `[]` (HTTP 200) for an existing-but-empty table; `PGRST205`
  "Could not find the table" means the table does NOT exist.
- `nt_signal_sim.signal_id` is `UUID PRIMARY KEY` with NO DB default → the capture
  writer MUST inject `str(uuid.uuid4())` (in `log_signal_sim`, not the payload
  builder). `nt_eod_actions.action_id` HAS `gen_random_uuid()` default.
- **Downstream signal_id gotcha (live 23502):** the capture payload
  (`_build_signal_sim_payload`) deliberately does NOT carry `signal_id` — any
  downstream consumer of the capture payload (e.g. `paper_book.open_position`)
  that inserts a row joining to `nt_signal_sim` MUST look the real signal_id up
  by (symbol, ts) from the ledger, or every insert fails with NOT NULL 23502.
- **Postgres 42803 (window+aggregate same column):** a SELECT that mixes a plain
  aggregate `SUM(x)` with a window `SUM(x) OVER (...)` over the SAME column under
  GROUP BY fails with "column must appear in the GROUP BY". Fix: aggregate in a
  CTE (`GROUP BY 1`), then apply the window over the pre-aggregated column with
  NO GROUP BY in the outer query.

## Capture mechanism + verified topology

### `supabase_client.py`
- `NT_SIGNAL_SIM_COLUMNS` frozenset (28 cols — capture fields + `kelly_f`/`effective_kelly`
  (migration 105, Part II paper sizing) + resolver fields `outcome`/`exit_price`/`exit_ts`/`hold_bars`/`mfe`/`mae`/`pnl_bricks`/`pnl_dollars`/`resolved_at`
  that stay NULL at capture) + `log_signal_sim(payload)` (filter → `_insert_row("nt_signal_sim")`,
  never raises, mirrors `log_sweep_result`).

### `orchestrator.py`
- `_build_signal_sim_payload(...)` module-level helper (keyword-only, unit-testable; prices 6dp full-value, p_win/ev 4dp)
- Capture block inside `_process_symbol`, placed AFTER the Redis publish section so
  `qualified` is FINAL (cooldown suppression AND content-dedup suppression both
  flip it to False before that point). Wrapped in `try/except RuntimeError`
  around `asyncio.create_task(log_signal_sim(...))` (no running loop in sync tests → skip, never break the sweep).
- `_process_symbol(sym, prices, p_timesfm=None, source="sweep-light")` —
  new optional `source` keyword stamps provenance onto the capture row.

### SWEEP-PATH TOPOLOGY (verify before wiring any per-signal hook)
`_process_symbol` is called ONLY from:
- `_sweep_group` (light sweep, `source="sweep-light"`)
- `_stale_check_loop` (stale retry, default source)

The **HEAVY sweep** (`heavy_sweep.py` `HeavySweepMixin`
`run_heavy_sweep_for_symbol` → `_push_heavy_signal`) NEVER calls
`_process_symbol` — it has its own qualification (`_evaluate_heavy_qualification`)
+ Redis push and only fires for QUALIFIED signals. A hook added to
`_process_symbol` does NOT cover heavy signals; wiring it there
requires a separate call in `heavy_sweep.py`.

### Companion EOD components (2026-08-06)
- `regime_platform/sweep_orchestrator/eod_resolver.py` + `eod_correction.py`
- Migrations `102_nt_signal_sim.sql` + `103_eod_metrics_views.sql`
- Tests:
  - `tests/test_signal_sim_capture.py` (12 tests: capture + EOD-correction helpers)
  - `tests/test_eod_resolver.py` (16 tests: close-walk resolution + PnL convention)

## Verification
- `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/test_signal_sim_capture.py -q` → 7 passed
- Full suite: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q` → 246 passed, 23 skipped, 0 failed (baseline was 239/23; +7 are the new capture tests)