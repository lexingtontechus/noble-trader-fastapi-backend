---
# Live wiring — cadence, flags, the published artifact

## The daily audit job

Scheduled in `orchestrator.py` at startup (`_run_edge_lab`, job id
`edge-lab-daily`):

```python
CronTrigger.from_crontab("30 18 * * *")   # daily 18:30 UTC
# runs 24 min after the heavy sweep (06:18/17:18 local) to avoid TV batch collision
misfire_grace_time=7200, coalesce=True
```

- Gated by `FEATURE`: `get_edge_lab_run_mode() != "off"`. If `"off"`, the
  job is NOT registered at all.
- **Not** gated on `regime_edge_mode` (the per-signal gate). The run is the
  evidence that would justify a gate flip; keying it to the gate's own mode
  makes the evidence unobservable exactly when needed.

## The three modes (`edge_lab_run_mode`)

| mode | what happens |
|---|---|---|
| `off` | No job registered. No data refresh, no engine run. |
| `shadow` (default) | 1) Refresh TDVA cache, 2) run the engine, 3) write artifact to `worklog/`, 4) heartbeat nt_roadmap(`edge_lab_run`). NO Supabase write. |
| `live` | shadow + publish `edge_lab.json` to `nt_edge_lab_run` table for the Edge Lab dashboard page. |

Fail-closed but never raises out of the scheduler tick: a failed run is
heartbeated with the error; the next day's tick is the retry (no
in-place retry).

## The two-run sequence

1. **Data refresh** — `scripts/export_edge_lab_data.py --source tdva
   --hours 2160` (730 days = 17520 hourly bars; the TDVA feed, the live
   path's own). Writes `data/regime_edge/*.csv` + `SOURCE.json`.
2. **Engine** — `scripts/run_edge_lab.py --checkpoint-dir
   <repo>/worklog/kairos/reports/regime_edge/_ckpt`. Resume-safe via
the checkpoint dir.

The orchestrator reads the resulting `edge_lab.json` via
`worklog/kairos/reports/regime_edge/_ckpt` run_id detection.

## Publishing to the dashboard

When `edge_lab_run_mode == "live"`:

- `edge_lab.json` -> `nt_edge_lab_run` table (the nobletradingapp Edge Lab
  page reads it via the `v_edge_lab_run` view).
- The run writes a heartbeat to the `edge_lab_run` key in the local param
   store / nt_roadmap.

## The SEPARATE per-signal gate (`regime_edge_mode`)

This is the LIVE gate that reads the engine's *conceptual output* (regime edge
buckets) — but NOT the engine's daily run. It reads `nt_regime_edge`
(produced by `regime_platform/sweep_orchestrator/regime_edge.py`'s
`rebuild_regime_edge`, which buckets resolved `nt_signal_sim` rows by
(symbol, signal, regime)).

Wiring in `_process_symbol` (`orchestrator.py:5505`):

```python
"regime_edge_ok": True,                                    # fail-open default
if get_regime_edge_mode() == "live":                      # off | shadow | live
    _edge = await get_regime_edge(symbol, signal, regime_label)
    _reject, _reason = should_reject_by_regime_edge(
        _edge,
        min_n=get_regime_edge_config()["min_n"],          # default 20
        min_win_rate=get_regime_edge_config()["min_win_rate"],  # default 0.50
    )
    if _reject:
        gate_results["regime_edge_ok\ = False
# in shadow mode: the lookup runs but never rejects (stamps only)
```

- Read order: local param store (runtime authority; no DB round-trip) ->
  Supabase `nt_regime_edge` (cold local store fallback).
- Fail-open: None or n < min_n -> pass. Reject only when n >= min_n AND
  win_rate < min_win_rate.
- Both `edge_lab_run_mode` and `regime_edge_mode` default to `shadow` — the
  gate is wired but not yet enforcing; the audit run is wired but not yet
  publishing. Separate levers, separate defaults.

## The feature_flags block

```yaml
regime_edge_mode: "shadow"            # per-signal gate: off | shadow | live
regime_edge:
  min_n: 20
  min_win_rate: 0.50
  rebuild_interval_hours: 24
edge_lab_run_mode: "shadow"          # daily audit run: off | shadow | live
edge_lab_run:
  data_source: "tdva"               # tdva | yfinance(legacy)
  hours: 2160                       # 730 days
```

Accessors: `get_regime_edge_mode()`, `get_regime_edge_config()`,
`get_edge_lab_run_mode()`, `get_edge_lab_run_config()` in
`regime_platform/core/feature_flags.py` (lines 2162-2200). The three-place
invariant (YAML + `_DEFAULTS` + getter) holds for both blocks.

## Sources

- `regime_platform/sweep_orchestrator/orchestrator.py:1968-1994` (job reg) +
  `:3229-3330` (`_run_edge_lab`).
- `regime_platform/sweep_orchestrator/regime_edge.py` (live gate wiring).
- `regime_platform/core/feature_flags.py:190-212` (defaults) + `:2162-2200`
  (accessors).
- `config/feature_flags.yaml` lines 709-720 (live config block).
- `docs/ACTIVE_MODES.md` (operator cheat-sheet of live vs shadow gates).
