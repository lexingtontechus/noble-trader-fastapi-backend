# Hourly calibration resolve (Kairos V3 lever 1)

> **Date:** 2026-09-25 (worklog dated 09-26, when the first shadow day reads) · **Branch:** `kairos` → `main`
> **Scope:** `worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md` §14.2 lever 1 · **Evidence:** `reports/20260925_calibration_step2.md` §A
> **Flag:** `calibration_resolve_hourly_mode` (`off | shadow | live`), ships **shadow**
> **Roadmap:** `nt_roadmap.calibration_resolve_hourly` (heartbeat each run; review 2026-09-27)

---

## 1. Why

TP/SL outcomes are written to `nt_calibration_records` by one job only: the nightly
`eod-lookback` at **01:17 UTC** (`eod_resolver.resolve_pending_signals`). The live bias
(`fetch_calibration_bias`, which drives `eod_correction` and the 4h intraday correction passes)
can only use records that exist. So every outcome waits for the next nightly run.

- **Measured latency:** `resolved_at − exit_ts` has a median of **11.1h** and a p90 of **23.8h**
  (1,000 recent TP/SL, `resolved_at` hours are only 01:xx and 22:xx UTC).
- **What it costs on S2:** Kairos step 1 found calibration error carries into the next qualified
  signal on an **hours** scale (0–6h β +0.41, p 0.004; gone by ~24h). Evidence that arrives about
  11h late has lost most of its information before the bias sees it.

Walk-forward on 446 qualified S2 targets (common subset of 162), scored with the same
correction as the production bias rule (7d entry window, episodes, 3-day half-life, ≥ 28 raw,
≥ 2 episodes):

| Evidence latency | S2 scorable | Δ Brier (negative = better) |
|---|---|---|
| nightly resolver (today) | 31% | −0.025 ± 0.012 |
| **hourly resolver** (this change) | **40%** | **−0.043 ± 0.013** |
| instant (upper bound) | – | 3h HL: −0.102 |

This lever changes **when** evidence arrives, not the bias formula. The half-life lever (3h
signal-level bias) is separate and comes next, one lever at a time.

A dry run against live data at 2026-09-25 23:10 UTC scanned 2,203 pending in-window signals in
7.7s. **75 TP/SL outcomes** (6 TP / 69 SL; AAPL, BTCUSD and XAUUSD 19–20 each) were already
decided but would not reach calibration until 01:17 UTC. Another 2,097 were still too young to
judge (minimum-age gate), and there were 0 errors.

## 2. What changed

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/eod_resolver.py` | `_judge()` — pure verdict (close-walk + **unchanged** minimum-age gate) extracted from `_resolve_one`, which now delegates to it, so writer and dry run can never judge differently. `_fetch_pending(..., offset=0)` for dry-run paging. New `next_nightly_resolve()` / `evidence_hours_saved()` (pure) and `dry_run_pending_signals()` (read-only: same pending query, same `LIGHT_BARS` candle window, same `anchor_post_entry_closes`, same `_judge`; no PATCH, no insert). |
| `regime_platform/sweep_orchestrator/orchestrator.py` | Job `calibration-resolve-hourly` at **`38 * * * *`**: clear of the `:00/:15/:30/:45` light sweeps (TradingView batch collisions, 2026-08-24), the `:17` nightly and the `:20` Kairos job. `_run_calibration_resolve_hourly()`: off → nothing, shadow → dry run + log, live → `resolve_pending_signals()` (the nightly's own function). A shared **`_resolver_lock`** (`_get_resolver_lock()`) is now taken by the nightly `_run_eod_lookback` resolver step too. Live hourly **skips** (does not queue) if the lock is held, because `nt_calibration_records` has no `signal_id` uniqueness and concurrent passes would double-insert. Heartbeat → `nt_roadmap.calibration_resolve_hourly`. |
| `regime_platform/core/feature_flags.py` | `_DEFAULTS["calibration_resolve_hourly_mode"]="shadow"`, `get_calibration_resolve_hourly_mode()` (unknown → shadow). |
| `config/feature_flags.yaml` | `calibration_resolve_hourly_mode: shadow` with rationale. |
| `tests/test_calibration_resolve_hourly.py` | 27 tests (§5). |

**Deliberately unchanged:**
- The close-walk, `EXPIRY_BARS` and the minimum-age gate.
- `_write_calibration_record` and the Brier-uses-raw rule.
- The nightly schedule (it still owns paper reconcile, correction, retention, reachability and
  emergency heavies).
- `fetch_calibration_bias` and every `EOD_BIAS_*` knob.
- The 4h correction passes.

In live mode the hourly job only moves outcomes earlier. The correction passes still run at
04:30/08:30/…, and they will simply see fresher records.

## 3. Modes

| Mode | Behaviour | Writes |
|---|---|---|
| `off` | job fires, returns | none |
| `shadow` (default) | `dry_run_pending_signals()`. Logs `[calib:hourly] shadow — would resolve N now ({...}), K TP/SL (qualified q) reaching calibration X.Xh before the nightly; insufficient_bars=… windowed_out=… errors=… scanned=…` | heartbeat row only |
| `live` | `resolve_pending_signals()` under the shared lock. Logs `[calib:hourly] live — resolved N ({...}), X.Xh before the nightly` | `nt_signal_sim` outcome/exit/pnl/drift + `nt_calibration_records` (same as nightly) |

The flag is read on every run, so flipping it in `config/feature_flags.yaml` needs no restart.
The job is registered unconditionally.

## 4. Promotion bar (shadow → live)

Read after **≥ 24 shadow runs** (one full day, which spans a nightly):

1. **No errors.** `errors = 0` in ≥ 95% of runs, and no TradingView 429s in the `:38` minute.
2. **Runtime.** p95 runtime < 60s, so the job finishes well before the `:45` light sweep.
3. **Parity.** For signals the shadow reported as TP/SL at hour *h*, the nightly resolved the
   same outcome. Shadow and writer share `_judge`, so a mismatch means a data difference
   (candle revision), not a logic difference.
4. **Volume.** The TP/SL counts are material: tens per hour, not zero.

Then flip to `live`. What to measure after the flip, one lever at a time:
- `resolved_at − exit_ts` median should drop from ~11h to ≤ 1.5h.
- S2 coverage of the production bias, for the ≥ 28-raw floor.
- Re-run `scripts/kairos_calibration_step2.py` §A on post-flip data: the "production rule,
  nightly" row should converge toward the hourly row.

**Kill / rollback:** set `calibration_resolve_hourly_mode: shadow` (or `off`).
- The nightly is untouched, so nothing depends on the hourly job.
- Outcomes already written are the same outcomes the nightly would have written. There's no
  data to unwind.

**Interaction with the tiered-ATR flip:**
- `atr_anchored_evidence_mode` went live 2026-09-25 and the first live heavy is 09-26 17:06 UTC.
- Tight brackets resolve in hours, which raises TP/SL volume per hour and makes this lever
  matter more.
- The flip reads (09-29) compare promoted vs control symbols. Hourly resolution affects both
  groups equally.
- Still, flip this lever **after** the 09-26 heavy has run, so the first post-geometry day is
  read on one change.

## 5. Tests

`tests/test_calibration_resolve_hourly.py` has 27 tests:
- the flag getter, and the YAML shipping shadow;
- nightly-time helpers, including the day roll and the exact 01:17 boundary;
- `_judge` TP, SL, young → pending (the gate is preserved), missing levels, and `_resolve_one`
  delegating to it;
- the dry run never PATCHes or inserts, pages with offset, respects `max_total`, counts
  windowed_out and missing candles, and never raises;
- the job: off is a no-op; shadow never calls the writer; live calls the writer under the lock;
  live skips when the lock is busy; a writer exception is caught with the lock released; the
  nightly takes the same lock; the job is registered at `:38`.

The existing resolver/EOD/catch-up tests pass (`test_eod_resolver`, `test_eod_lookback_wiring`,
`test_batch_b_measurement`, `test_scheduler_catchup`, `test_light_sweep_catchup`): 115 passed
across the 6 files.

## 6. Verify after deploy

```bash
ssh noble-trader-unified 'cd /teamspace/studios/this_studio/noble-trader;
  grep -a "Calibration resolve (hourly)" logs/orchestrator.log | tail -1;      # job registered
  grep -a "\[calib:hourly\]" logs/orchestrator.log | tail -5'                  # first run at :38
```

`nt_roadmap.calibration_resolve_hourly.current_state` should read `last_run … {"mode": "shadow",
"would_resolve": …}` after the first `:38`.
