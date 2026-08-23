# predicted_p_win_raw — calibration audit field (built 2026-08-13)

## Why it exists (the masked-calibration problem)
Enforcement (orchestrator §5b) shrinks `p_win` toward 0.5 **BEFORE** it is
stored, so `nt_calibration_records.predicted_p_win` records the ENFORCED
probability. The bias math (`avg_pred − win_rate`) then converges toward 0 partly
BECAUSE the enforcement is correcting — masking the model's true miscalibration.
`predicted_p_win_raw` (the pre-shrink value) lets the calibration loop separate
**"is the model still miscalibrated?"** from **"is enforcement compensating?"**.

Relevance: **calibration, NOT live trades** (backend is not a trade executor; for
any live path the *used* probability is the correct audit input).

## Implementation (repo-side, DEPLOYED 2026-08-14; capture flag confirmed ON 2026-08-17)
- Flag confirmed `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW=True` in LightningAI Studio
  as of 2026-08-17. Migration 114 (`ADD COLUMN IF NOT EXISTS predicted_p_win_raw`)
  applied in Supabase SQL editor. Capturing pre-enforcement p_win values.
- `settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` — default **False** (deploy-safe).
- Orchestrator §5b stashes `p_win_raw = ev_result.p_win` BEFORE
  `apply_calibration_adjustment`; `_build_signal_sim_payload` includes
  `predicted_p_win_raw` in nt_signal_sim ONLY when the flag is on (default path
  is byte-identical — no behavior change until enabled).
- `supabase_client.NT_SIGNAL_SIM_COLUMNS` + `eod_resolver.CALIBRATION_RECORD_COLUMNS`
  extended with `predicted_p_win_raw` + `calibration_brier`.
- Resolver `_write_calibration_record`: `predicted_p_win_raw` pass-through +
  `calibration_brier` = (1−p)² on win / p² on loss, using raw when present else
  the stored (enforced) p_win.
- Tests: `tests/test_eod_resolver.py::TestCalibrationRecordPayload` (raw
  pass-through, brier win/loss, whitelist) + `tests/test_eod_lookback_wiring.py::TestSignalSimPayloadRawPWin`
  (3 gating tests). Full suite 401 passed / 23 skipped.

## Migration requirement (critical sequencing)
`nt_signal_sim` does NOT have the column; `nt_calibration_records` already has
both `predicted_p_win_raw` and `calibration_brier`. There is NO DDL tooling in
the backend repo — schema changes go through the Supabase SQL editor.

**Naming convention (user-corrected 2026-08-13):** `supabase/migrations/` uses a
3-digit SEQUENTIAL prefix `NNN_snake_name.sql` (latest was 113), NOT a date
prefix. This migration is `114_add_predicted_p_win_raw.sql` — renumber when
adding: `ls supabase/migrations/ | sort | tail -1` to find the next NNN.

```sql
ALTER TABLE public.nt_signal_sim
    ADD COLUMN IF NOT EXISTS predicted_p_win_raw double precision;
```
Repo copy: `supabase/migrations/114_add_predicted_p_win_raw.sql`.
**Enabling the flag without the column → PGRST204 on every nt_signal_sim insert →
signal capture STOPS.** Apply the migration first, then flip the flag.

## Diagnostic queries (copy-paste)

When asking about calibration bias, these are the queries that answer the actual questions. Run against live Supabase via the repo's own settings module (`from regime_platform.core.settings import settings` → loads `.env.local`; never print the key). Pattern: httpx GET `{SUPABASE_URL}/rest/v1/{table}` with `apikey` + `Authorization: Bearer` from `_headers()`.

### Q1: What does the view actually show? (post-enforcement p_win)

```sql
-- v_eod_calibration_bias — what the correction loop's trigger sees
-- Reads nt_signal_sim.p_win = POST-ENFORCEMENT value
SELECT day, symbol,
       avg_predicted_p_win,
       realized_win_rate,
       bias,
       status
FROM v_eod_calibration_bias
WHERE day >= current_date - 7
ORDER BY day DESC, symbol;
```

**Answers:** "Is the deployed p_win still overconfident after enforcement?" — this is what drives `nt_eod_actions`. If bias > 0.10 → OVERCONFIDENT → penalty actions written.

**Does NOT answer:** "Is the raw model still miscalibrated?" (enforcement shrink is already in `avg_predicted_p_win`).

### Q2: Raw model vs enforced vs realized (the masked-calibration check)

```sql
-- nt_calibration_records — separates model miscalibration from enforcement compensation
-- predicted_p_win_raw = pre-Bayesian-shrink (what the model actually output)
-- predicted_p_win    = post-shrink (what was stored + used)
SELECT symbol,
       COUNT(*)                                      AS n_records,
       AVG(predicted_p_win_raw)                      AS raw_avg_p_win,      -- pre-shrink
       AVG(predicted_p_win)                          AS enforced_avg_p_win,  -- post-shrink
       AVG(predicted_p_win) - AVG(predicted_p_win_raw) AS avg_shrink,        -- enforcement mag
       AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS realized_wr,
       AVG(predicted_p_win_raw)
         - AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS raw_bias,     -- model miscalibration
       AVG(predicted_p_win)
         - AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS enforced_bias  -- what view sees
FROM nt_calibration_records
WHERE entered_at >= now() - interval '7 days'
  AND predicted_p_win_raw IS NOT NULL
GROUP BY symbol
ORDER BY raw_bias DESC;
```

**Answers:** "Is the model still miscalibrated, separate from enforcement?" — `raw_bias` is the true model error; `enforced_bias` is what `v_eod_calibration_bias` reports. If `raw_bias >> enforced_bias`, enforcement is doing most of the compensating. If both are small, the model itself is well-calibrated.

**Requires:** migration 114 applied + `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW=True` (confirmed ON 2026-08-17).

### Q3: Direct nt_calibration_records forensics (entry validity, outcome mix)

```sql
-- Per-symbol calibration record health — entry validity + outcome distribution
SELECT symbol,
       COUNT(*)                                        AS n_records,
       COUNT(CASE WHEN outcome = 'tp_hit' THEN 1 END) AS wins,
       COUNT(CASE WHEN outcome = 'sl_hit' THEN 1 END) AS losses,
       COUNT(CASE WHEN outcome = 'expired' THEN 1 END) AS expired,
       AVG(predicted_p_win)                           AS avg_pred_p_win,
       AVG(predicted_p_win_raw)                       AS avg_raw_p_win,
       MIN(entered_at)                                AS oldest_entry,
       MAX(entered_at)                                AS newest_entry
FROM nt_calibration_records
WHERE entered_at >= now() - interval '7 days'
GROUP BY symbol
ORDER BY symbol;
```

**Answers:** "Is this symbol's calibration data trustworthy?" — checks n-records vs 20-threshold, win/loss balance (degenerate split = `min(wins,losses)==0` = artifact flag), entry recency.

### Q4: Failed heavy sweep traces (no_combo_passed_filters)

```sql
-- Heavy sweep failures — why did the emergency re-optimization find nothing?
SELECT symbol, sweep_timestamp,
       n_combos_tested,               -- how many of 90 survived MIN_RR pre-filter
       regime, regime_conf,
       kelly_f, sweep_duration_ms,
       error                           -- "no_combo_passed_filters (tested N, MIN_RR=…, …)"
FROM nt_sweep_result
WHERE source = 'sweep-heavy'
  AND qualified = false
  AND symbol IN ('EURUSD', 'GBPUSD', 'USDCAD')  -- the 3 that failed at MIN_RR=2.0
  AND sweep_timestamp >= now() - interval '7 days'
ORDER BY sweep_timestamp DESC;
```

**Answers:** "Was this a filter artifact or a genuine 'no combo works'?" — `n_combos_tested` tells you how many combos survived the RR pre-filter. If few tested and all failed → likely the RR threshold was too strict (the Aug 17 MIN_RR fix addresses this). If many tested and all failed → no geometry works on this price data → consider signal reduction or different brick params.

### Q5: Enforcement activity log (live stamps)

```sql
-- grep orchestrator.log for enforcement stamps — what's being applied live?
-- [sweep:orch] <sym>: calibration ENFORCED (OVERCONFIDENT, bias=…) — p_win→…, kelly→…
-- Pattern: grep "calibration ENFORCED" orchestrator.log
-- Or query nt_eod_actions directly:
SELECT symbol, action, ts,
       detail->>'p_win_before'  AS p_win_before,   -- JSONB extract (if stored)
       detail->>'p_win_after'   AS p_win_after,
       detail->>'kelly_before'  AS kelly_before,
       detail->>'kelly_after'   AS kelly_after
FROM nt_eod_actions
WHERE action IN ('bayesian_penalty', 'kelly_reduction', 'emergency_heavy_sweep')
  AND ts >= now() - interval '7 days'
ORDER BY ts DESC;
```

**Answers:** "What enforcement is actually being applied?" — shows the live p_win/Kelly adjustments being stamped every 5-min sweep.

---

## Raw-bias mode classification thresholds (LIVE verified 2026-08-22)

### Two threshold sets exist in the code

| Mode | Overconfident | Underconfident | Calibrated band |
|------|--------------|---------------|-----------------|
| **Standard** (`raw_bias_mode=False`) — nightly 00:30 EOD + light sweeps | `> 0.10` | `< -0.10` | `-0.10 ≤ bias ≤ 0.10` |
| **Raw** (`raw_bias_mode=True`) — 4h intraday passes (04:30/08:30/12:30/16:30/20:30) | `> 0.30` | `< -0.20` | **`-0.20 ≤ bias ≤ 0.30`** |

**The raw-bias band is `-0.20 < bias < 0.30` — NOT `-0.2 < bias < 0.7`.** The 0.7 figure
is an incorrect guess that appeared in early conversation history; it was never a
code threshold.

### Code locations
- `_classify_bias_raw()` in `eod_correction.py:99` — classifies using raw thresholds
- `_classify_bias()` in `eod_correction.py:91` — classifies using enforced thresholds (0.10)
- `settings.EOD_BIAS_RAW_OVERCONFIDENT` (default `0.30`) in `settings.py:314`
- `settings.EOD_BIAS_RAW_UNDERCONFIDENT` (default `-0.20`) in `settings.py:318`

### Why the wider raw band (0.30 vs 0.10)?
Raw model output is naturally more extreme — p_win values before Bayesian shrink
cluster near 0.8–1.0 for confident symbols, whereas enforced p_win after repeated
shrinks settles into 0.5–0.8. A wider threshold on raw values avoids false-positive
overconfidence flags on normal model variance.

### Cycle reset mechanism (raw mode only)

At each 4h intraday pass with `raw_bias_mode=True`:
1. `fetch_calibration_bias_raw()` reads `predicted_p_win_raw` (pre-enforcement)
2. `_classify_bias_raw()` classifies using 0.30/-0.20 thresholds
3. `_clear_stale_eod_actions()` is called **unconditionally for ALL symbols** at
   the start of the 4h pass (before bias computation), clearing stale penalty
   actions written by the previous cycle. This ensures low-volume symbols with
   <20 calibration records (which enter the degeneracy path) still have their
   stale actions purged. See `eod_correction.py:326` and
   `worklog/20260822_STALE_ACTION_CLEARING_SCOPE.md`.
4. If status == OVERCONFIDENT/UNDERCONFIDENT → writes new `nt_eod_actions` with
   Bayesian shrink applied to the RAW p_win (not the already-shrunk enforced value)

### Live evidence (2026-08-22 04:30 UTC first raw-bias pass)

```
[eod:correction-intraday] done: 14 symbols checked, 6 actions, 0 errors
[eod:correction-intraday] emergency heavy sweeps requested: ['AAPL', 'US500']

[eod:correction] AAPL: OVERCONFIDENT bias=0.4447 → emergency sweep + p_win'=0.5553 + Kelly×0.50
[eod:correction] US500: OVERCONFIDENT bias=0.6771 → emergency sweep + p_win'=0.6528 + Kelly×0.50
[eod:correction] complete: 14 symbols, 6 actions, 0 errors

_clear_stale_eod_actions XAGUSD: cleared stale penalty actions
_clear_stale_eod_actions XAUEUR: cleared stale penalty actions
_clear_stale_eod_actions XAUUSD: cleared stale penalty actions
_clear_stale_eod_actions BTCUSD: cleared stale penalty actions
_clear_stale_eod_actions USDCHF: cleared stale penalty actions
_clear_stale_eod_actions USDJPY: cleared stale penalty actions
```

**All observed bias values fall within -0.20 ≤ bias ≤ 0.70**:
- AAPL: 0.4447 (above 0.30 raw threshold → OVERCONFIDENT; below 0.70 → within operational range)
- US500: 0.6771 (above 0.30 raw threshold → OVERCONFIDENT; below 0.70 → within operational range)
- XAGUSD: 0.3351–0.4005 (drift monitor re-flagged after raw-bias pass cleared, using 0.15 threshold)
- XAUEUR: 0.5001 (drift monitor re-flagged after raw-bias pass cleared, using 0.15 threshold)

**Status**: AAPL raw bias unchanged (0.4447) — model-level miscalibration persists
**Status**: After the stale-action-clearing fix (deployed 2026-08-22 22:48 UTC → live at 04:30 UTC Aug 23), all 14 symbols should have stale actions cleared unconditionally. Pre-fix, only 6 symbols were cleared per pass (20:30 pass showed 6 clears only).

### Three bias values, three thresholds — don't conflate them

| Value name | Source | Threshold for penalties | Operational ceiling |
|---|---|---|---|
| **Raw bias** | `predicted_p_win_raw - win_rate` (pre-shrink) | 0.30 (raw overconfident) | — |
| **Enforced bias** | `predicted_p_win - win_rate` (post-shrink) | 0.10 (enforced overconfident) | — |
| **Drift bias** | Same as enforced, but only on CALIBRATED symbols | 0.15 (drift monitor) | — |

**Operational bias range**: -0.20 ≤ bias ≤ 0.70. This is NOT a classification threshold — it's a health boundary. Values above 0.30 (raw) or 0.10 (enforced) get penalized; values below -0.20 or above 0.70 indicate a systemic model failure requiring investigation, not just a penalty action.

- US500's bias of 0.6771 is **above 0.30** (classified OVERCONFIDENT → penalized via emergency sweep) but **below 0.70** (within operational range → not a systemic crisis)
- US500's bias of 0.7565 (before raw-bias pass) **exceeded 0.70** (systemic → emergency heavy sweep triggered)

## nt_calibration_records schema / null-columns provenance
18 columns; measured live 2026-08-13 (14.6k rows):
- **Always populated (12)** — resolver whitelist: `id, symbol, venue,
  predicted_p_win, outcome, aggression, entry_price, exit_price, entered_at,
  closed_at, actual_pnl, error_source`.
- **100% null (6)**:
  - `p_regime, p_ext_signal, p_markov, p_timesfm` — written ONLY by
    `renko/trade_journal.py` (live-trade path). All rows are
    `error_source='eod_resolver'` (no live trades logged) → null. They answer
    "why did the model think p_win=0.97? regime/pattern/markov/timesfm?" for
    bias forensics.
  - `predicted_p_win_raw, calibration_brier` — reserved (this build); no writer
    until the flag + migration land.
- `fetch_calibration_bias` only reads `predicted_p_win` + `outcome` (+
  `entered_at` window) → the nulls never affect bias math.

## PostgREST query gotchas (hit when probing calibration tables)

- **Aggregates**: `?select=symbol,venue,count()` groups automatically. Do NOT pass a separate `group=` param — PostgREST parses it as a filter → PGRST100.
- **Count probes must use a real column per table**: `select=id&limit=1` + `Prefer: count=exact` → HTTP 400 when the table lacks that column. Per table: `nt_calibration_records` → `id`; `nt_signal_sim` → `signal_id` (no `id` column); `nt_eod_actions` → `ts`.
- **`order=col.desc` puts NULLS FIRST**: `order=entered_at.desc` returns newest first. `order=closed_at.desc` returns unresolved rows (closed_at NULL) first — use `order=closed_at.desc.nullslast` for true most-recently-resolved.
- **`entered_at`/`closed_at` mirror SIGNAL times, not write times** — judge recency by the orchestrator run's log line, not these columns (resolver drains oldest-first, so freshly inserted rows can carry days-old `entered_at`).
- Full-table aggregation when >1000 rows: use server-side `select=symbol,count()` (exact, cheap) instead of fetching pages and aggregating client-side.
