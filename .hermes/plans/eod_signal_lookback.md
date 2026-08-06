# EOD Signal-Generation Lookback — Implementation Plan

**Status:** APPROVED (2026-08-06) — Build Code authorized
**Scope:** Backend only (`noble-trader-fastapi-backend`). NO agent component.
**Goal:** For every generated signal, resolve its forward outcome (TP hit / SL hit / expired) using post-entry TDVA prices, then compute calibration/decay/exit metrics and drive a self-correction loop.

---

## Architecture (verified against code)

```
Sweep tick (_process_symbol, orchestrator.py:1128)
  ├─ trade_signal = {signal, entry_price, stop_loss, take_profit}   (line 1239)
  ├─ brick_size, sl_bricks, tp_bricks                                (line 1228)
  ├─ p_timesfm, regime_label, regime_conf, kelly_f, effective_kelly  (in scope)
  ├─ qualified = all(gate_results.values())                          (line 1429)
  │
  ├─ [PHASE 2] CAPTURE ──► nt_signal_sim (fire-and-forget insert, all non-neutral)
  │
  ├─ log_sweep_result ──► nt_sweep_result (existing)

EOD job (new module, daily ~00:30 UTC, APScheduler in orchestrator)
  ├─ [PHASE 3] RESOLVER: rows WHERE outcome IS NULL
  │     ├─ fetch post-entry TDVA 1h bars (price_fetcher.fetch_candles_for_symbols)
  │     ├─ walk forward: high>=TP → tp_hit | low<=SL → sl_hit | 48 bars → expired
  │     ├─ write outcome + exit_price/ts/hold_bars/mfe/mae/pnl
  │     └─ write nt_calibration_records {symbol, venue, predicted_p_win, outcome}
  │
  └─ [PHASE 5] SELF-CORRECTION
        ├─ |bias| > 0.10 → emergency heavy sweep + Bayesian p_win penalty + Kelly ×0.5
        └─ else → parameter-decay check; keep cadence

Metrics (PHASE 4): read-only SQL views over nt_signal_sim + nt_sweep_result.
```

## Phases

### Phase 1 — Migration: `nt_signal_sim` table
Supabase migration `supabase/migrations/102_nt_signal_sim.sql`.
Columns: signal_id uuid pk, symbol, venue, ts timestamptz, signal, entry_price,
stop_loss, take_profit, brick_size, sl_bricks, tp_bricks, p_win, ev, regime,
regime_conf, source (sweep-light|sweep-heavy), qualified bool,
outcome (tp_hit|sl_hit|expired|open, null), exit_price, exit_ts, hold_bars,
mfe, mae, pnl_bricks, pnl_dollars, resolved_at.
Index: (symbol, ts DESC), (outcome) partial WHERE outcome IS NULL.

### Phase 2 — Capture hook (`orchestrator.py`)
After `qualified` is computed (~line 1429), if `trade_signal["signal"] not in
("neutral","unknown")`: fire-and-forget insert via new
`supabase_client.log_signal_sim(payload)`. Pattern mirrors `log_sweep_result`
(`_filter_payload` + `_insert_row`). Must NOT block or fail the sweep.

### Phase 3 — EOD resolver (`regime_platform/sweep_orchestrator/eod_resolver.py`)
New module. Entry: `resolve_pending_signals()` async.
- Query nt_signal_sim WHERE outcome IS NULL (limit, ordered oldest first)
- Group by symbol; fetch post-entry bars via `fetch_candles_for_symbols`
  (1h interval). NOTE: fetcher returns CLOSE prices only — for TP/SL touch
  resolution use close-walk (close>=TP → tp_hit for buy; close<=SL → sl_hit),
  plus intra-bar high/low if available in the raw bar dict (best-effort).
- Expiry window = 48 bars (2 days of 1h bars).
- Compute pnl_bricks/pnl_dollars using the same convention as the pipeline:
  pnl_bricks = distance in bricks; pnl_dollars = pnl_bricks × brick_size.
- Update row + write nt_calibration_records.
- CLI: `python -m regime_platform.sweep_orchestrator.eod_resolver`

### Phase 4 — Metrics (SQL)
`supabase/migrations/103_eod_metrics_views.sql` — 4 views:
1. `v_eod_calibration_bias` — daily predicted p_win vs realized win rate + bias + status
2. `v_eod_sweep_drift` — nt_sweep_result backtest wr vs realized wr per symbol
3. `v_eod_exit_attribution` — outcome × regime × signal breakdown
4. `v_eod_parameter_decay` — realized win rate by (brick,sl,tp) over time

### Phase 5 — Self-correction (`eod_correction.py`)
Daily job: read calibration bias (fetch_calibration_bias).
- |bias| > 0.10 → run emergency heavy sweep for the symbol
  (`run_heavy_sweep_for_symbol`), apply Bayesian penalty
  (`p_win' = (p_win×n + 0.5×10)/(n+10)`), Kelly ×0.5.
- Log to `nt_eod_actions` (audit table, migration 102 also creates it).

## Open decisions (locked per user)
- Bar resolution: **1h sweep bars** (close-walk + best-effort high/low)
- Expiry window: **48 bars**
- Capture scope: **all non-neutral signals** (tagged qualified)
- Table host: **Supabase**

## Verification
1. pytest suite stays green (239 passed / 23 skipped baseline)
2. Migration applies (SQL syntax check)
3. Capture hook writes rows on next sweep tick
4. Resolver manual run resolves pending rows; calibration bias becomes non-null
5. Views return rows after resolution

## Deployment status (2026-08-06)
- ✅ Committed + pushed: `1feb82e` → origin/main (11 files, 1797 insertions)
- ✅ Scheduler wired: `eod-lookback` job daily 00:30 UTC (verified next fire
  `2026-08-08 00:30 UTC`); full suite 267 passed / 23 skipped
- ⏳ **Studio git pull — BLOCKED from agent**: SSH publickey denied (key not
  server-registered; auth_token empty), `studio cp` uploads don't land
  (`.lightninglink` indirection → process reads uv-cache editable snapshot),
  and `lightning studio` has NO exec/shell/run subcommand. **User action:
  in the studio web terminal run**
  `cd /teamspace/studios/this_studio/noble-trader && git pull origin master`
  then restart the studio (or re-run `uv pip install -e .` to rebuild the
  editable snapshot).
- ⏳ **SQL migrations — BLOCKED from agent**: no `sbp_` PAT (the env token is
  `sb_publi…`, not a management-API PAT), no psql/asyncpg/psycopg in the venv,
  no Postgres DSN. Prior migrations (100/101) were applied via the Supabase
  dashboard SQL editor. **User action: paste the combined migration file
  `supabase/migrations/104_eod_lookback_combined.sql` into the Supabase
  dashboard → SQL editor** (creates nt_signal_sim + nt_eod_actions + 4 views).

