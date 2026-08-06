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
- ✅ **Studio deployed + live** (user pull + restart 2026-08-06): orchestrator
  boot 07:49:51 UTC registered `eod-lookback`; capture flowing (44 pending
  rows in nt_signal_sim as of 08:07 UTC); studio repo HEAD = `7ad9cdd`
- ✅ **SQL 102+103 applied** on Supabase (tables + views live); 104 fixed
  (`sweep_timestamp` not `created_at`; `DROP POLICY IF EXISTS` before
  `CREATE POLICY`) — re-runnable, committed `7ad9cdd`

---

# Part II — Portfolio Sim (added 2026-08-06)

**Status:** SCOPED — approved for planning; build gated on first EOD outcomes.
**Scope:** Backend only. Option A (paper portfolio fed by the signal stream)
is the recommended build; Option B (/multi_asset optimizer comparison) is a
follow-on. Option C (MetaApi demo account) remains OUT OF SCOPE — it is
trade execution, and the user's scope correction was explicit: lookback on
signal generation, not execution.

## Architecture (grounded in the completed Part I)

Part I already resolves every captured signal's forward outcome
(tp_hit/sl_hit/expired) and stores per-signal PnL in `nt_signal_sim`
(`exit_price`, `hold_bars`, `pnl_bricks`, `pnl_dollars`). The paper
portfolio is a **sum layer** over those outcomes — no new resolution
engine, no broker, no demo account.

```
Part I (done):
  capture ─► nt_signal_sim ─► EOD resolver ─► outcome + pnl per signal
                                                │
Part II Phase 2 (paper book):                   ▼
  qualified rows ─► nt_paper_positions (size_units, open/close, realized_pnl)
                         │
                         ▼
              portfolio views: equity curve, per-symbol PnL, drawdown, win rate

Part II Phase 3 (comparison, optional):
  paper-portfolio equity vs /multi_asset optimizer output (regime-adjusted
  weights) — answers "is signal-driven sizing better than naive allocation?"
```

## Part II — Phase 2: Paper portfolio (Option A — recommended)

Goal: "would we have made money following these signals?" — track the
qualified signal stream as a paper book and roll it up.

1. **Migration `105_paper_portfolio.sql`** — new table `nt_paper_positions`:
   - `position_id` uuid pk (generated at insert, like nt_signal_sim)
   - `signal_id` uuid NOT NULL (join to nt_signal_sim)
   - `symbol`, `venue`, `direction` (buy/sell)
   - `entry_price`, `stop_loss`, `take_profit` (copied from the signal)
   - `size_units` double precision — position size
   - `size_notional` double precision — size_units × entry_price
   - `open_ts`, `close_ts`, `status` (open|closed|expired)
   - `exit_price`, `realized_pnl`, `pnl_bricks`, `r_multiple`
   - `source` (sweep-light|sweep-heavy), `resolved_at`
   - Index on (symbol, open_ts DESC); partial index on status='open'
2. **Capture-side addition** — extend `_build_signal_sim_payload` with
   `kelly_f` and `effective_kelly` (available in `_process_symbol` scope;
   currently NOT stored in nt_signal_sim — needed for sizing). Add the two
   columns to nt_signal_sim via migration 105 (ALTER TABLE ... ADD COLUMN
   IF NOT EXISTS, idempotent like 021 pattern).
3. **Paper-book builder** (`regime_platform/sweep_orchestrator/paper_book.py`):
   - `open_position(sig)` — for each **qualified** row (outcome IS NULL,
     qualified=true): compute `size_units` from a sizing rule
     (default `size_units = base_notional × kelly_f × ev_scale / entry_price`,
     base_notional env-tunable, e.g. NOBLE_PAPER_BASE_NOTIONAL=1000);
     insert nt_paper_positions with status='open'.
   - Called fire-and-forget beside the existing `log_signal_sim` capture
     (must never break the sweep — same pattern as Part I capture).
   - `close_position(sig)` — when the EOD resolver writes an outcome for a
     signal, update the linked position: status=closed (tp_hit/sl_hit) or
     expired; set exit_price, close_ts, realized_pnl = pnl_dollars ×
     (size_units / 1) — NOTE sizing units: define realized_pnl as
     pnl_dollars × size_notional_scale so a 0.001-brick signal on $1000
     notional gives realistic $ figures. Exact convention locked at build.
   - Integration point: resolver's `_resolve_one` already PATCHes nt_signal_sim
     — add a second PATCH/INSERT to nt_paper_positions in the same pass
     (or a follow-up `paper_book.reconcile()` that scans closed signals
     without positions and creates/updates them — idempotent, re-runnable).
4. **Portfolio views** (same migration or `106_paper_views.sql`):
   - `v_paper_equity` — daily equity curve: date, open positions, cumulative
     realized PnL, mark-to-market of open positions (last known close).
   - `v_paper_perf` — per symbol: n_trades, win_rate, total_pnl, avg_r,
     max_drawdown.
   - `v_paper_exposure` — per symbol: open notional, % of base portfolio.
5. **CLI**: `python -m regime_platform.sweep_orchestrator.paper_book`
   (reconcile pass — idempotent; safe to run manually or via a scheduler
   hook next to eod-lookback).

### Part II Phase 2 — Open decisions (user to confirm at build)
- Sizing: kelly-based (`size = base_notional × kelly_f × ev_scale / entry`)
  vs equal-weight per signal vs fixed $ per symbol. Recommend kelly-based
  with a hard cap (e.g. max 20% of base_notional per position).
- PnL convention: `realized_pnl = pnl_dollars × (size_units / base_units)`
  where base_units=1 means "1 brick-unit" — OR scale to notional directly.
  Must match what "would we have made" means in $ for the user.
- Reconcile cadence: fold into the existing daily `eod-lookback` job (run
  paper_book.reconcile after resolve_pending_signals) vs separate cron.

## Part II — Phase 3: Portfolio-optimizer comparison (Option B — follow-on)

Goal: compare signal-driven paper portfolio vs naive/optimized allocation.

1. **Reuse `/multi_asset` optimizers** (`routers/multi_asset.py`):
   `optimise_portfolio` (regime-adjusted μ, max-Sharpe) and
   `optimise_with_masaniello` — compute a target weight vector across the
   11 symbols using realized returns (which nt_signal_sim outcomes now give).
2. **Comparison view** `v_paper_vs_optimized`:
   - daily paper equity (from v_paper_equity)
   - daily equal-weight portfolio equity (naive baseline)
   - daily optimizer-weight portfolio equity (rebalanced at sweep cadence)
   - per-symbol: signal win rate vs optimizer weight.
3. **Deliverable**: a weekly report (SQL query or small script) showing
   whether the signal-driven paper book beats equal-weight and
   optimizer-driven allocation over the same window.
4. Gated on Phase 2 having ≥ N days of resolved outcomes (suggest ≥ 7).

## Part II — Verification
1. Migration 105 applies (dashboard SQL editor, same pattern as 102/104)
2. Paper-book builder creates positions for qualified rows on next sweep
3. Resolver close pass updates positions; realized_pnl matches pnl_dollars × scale
4. v_paper_equity / v_paper_perf return rows after first closes
5. Full suite stays green (267 passed / 23 skipped baseline)


