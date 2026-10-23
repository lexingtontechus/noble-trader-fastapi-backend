---
# CLI, artifacts, determinism

## Entry point

`scripts/run_edge_lab.py` — ONE CLI for the whole proof engine.

```
Usage:
    .venv/bin/python scripts/run_edge_lab.py [--symbols BTCUSD XAUUSD ...]
        [--data-dir data/regime_edge] [--out-dir worklog/kairos/reports/regime_edge]
        [--bars-limit N] [--seed 7] [--checkpoint-dir <dir>]
```

## What one run does (per symbol)

1. Load TDVA OHLCV CSVs (`data.py`).
2. Compute causal features (`features.py:compute_all`).
3. Assign regime values + tri-state label (`values.py`).
4. Reproduce the live HMM head-to-head (`hmm_ref.py`) -> 24h direction skill.
5. Build per-slot trades under CURRENT geometry (`brackets.py` + `edge.py:build_trades`).
6. Edge surface: decile buckets + Spearman + permutation (`edge.py`).
7. Repo-style bucket calibration (level-only, vol-tier) (`edge.py`).
8. Walk-forward lockbox cells (`edge.py:walkforward_cells`).
9. Corrected geometry via the physics solver (`barrier.py`).
10. The decisive backtest: kairos vs corrected vs null (`books.py`).
11. TSFM reframe parsed from the repo's own vol_backtest artifacts (`tsfm.py`).

Everything seeded (default `--seed 7`).

## Default symbols now 14 / ACTIVE (commit fd2cb64)

`DEFAULT_SYMBOLS = list(rbar.ACTIVE_SYMBOLS)` — the full ACTIVE set from the
Supabase symbol table (2026-10-21): BTCUSD XAUUSD XAGUSD XAUEUR XAUAUD
USDJPY EURUSD GBPUSD USDCAD USDJPY AUDUSD AAPL US500 US30USD. The original
5-symbol `20261020_055039` run is the comparability baseline.

- Cache built by `scripts/export_edge_lab_data.py --source tdva --hours 2160`
  (constructs the 9 added CSVs; XAUEUR/XAUAUD built from GC=F/FX ratios).
- `SYMBOL_CLASS` now prices US500/US30USD as INDEX (cost 0.05); migration 217
  corrected the registry drift (originally `stocks`).

## Runtime / size (updated)

- 5-symbol test subset: ~10-15 min wall (canonical run `20261020_055039`).
- Full 14-symbol ACTIVE set: ~30-40 min wall (run `20261021_035147`).
- `EDGE_LAB_FAST=1` -> ~4x faster (MC sub=8).

## Checkpoints / resume (added fd2cb64)

`--checkpoint-dir <dir>`: every symbol is pickled at `_ckpt/<SYM>.pkl`.
Re-running the SAME command resumes from checkpoints; per-symbol compute is
pure + seeded, so a resumed run is byte-identical to uninterrupted. The ~30 min
14-symbol run survives host kills. `--recompute` ignores existing checkpoints.




## Artifacts (one per run)

`<out-dir>/<UTC run_id>/` contains:

| file | size/role |
|---|---|---|
| `edge_lab.json` | 0.92 MB; THE artifact (schema-asserted finite, <= 3 MB) |
| `summary.md` | headline verdicts + numbers, human-readable |
| `records.csv` | 9,462 rows; per-slot audit trail (one row per book x slot) |

## Output schema (`edge_lab.json`)

Top-level keys (`report.py:build_edge_lab`):

1. `meta` — run_id, generated_at, engine, symbols, period, bars/symbol,
   band_source provenance, approximation_notes (HMM cadence, MC convention,
   data basis, cost convention, entry basis, monitoring bias), fix_manifest_note.
2. `verdicts` — 17 records (W-C1..W-G5), each:
   id, title, verdict (PROVEN-FAIL/MEASURED/PROVEN-OK), evidence (string),
   numbers (dict), source (engine / repo-measured), fix_status,
   fix_summary.
3. `hmm` — per-symbol n_decided, acc, majority, skill, flip rates,
   conf_low_er; + approximation_note (the cadence deviation text).
4. `regime_values` — per-symbol 1500-bar regime tail (t, close, trend, chop,
   vol, regime label).
5. `edge_surface` — per-symbol decile buckets (by_feature) + spearman tests + best_spearman.
6. `gate_backtest` — per-symbol books (kairos/corrected/null) + DM (kc/cn p raw + Holm) +
   pooled + thresholds (er_gate tau* per symbol) + gate_stats.
7. `calibration` — per-symbol base_rate + regime_conditional (AUC/Brier/ECE),
   reliability curves, n_lockbox, repo_bucket_auc.
8. `barrier` — crosscheck (3 cases vs repo barrier_null) + per-symbol current/corrected
   geometry with pi_tp/pi_sl/pi_e, EV_null/EV_drift, mu_hat, cost_atr.
9. `tsfm_reframe` — parsed vol_backtest (har beats naive, chronos beats har,
   qlike best arm, phase-0.5 recalib accepted) + conclusion.
10. `fixes` — `[]` at engine time; filled by the Task 40-b coordinator from
    repo-side implementation metadata.

## Determinism

- All RNG seeded (default 7). Every HMM fit uses `random_state=42`; refit
schedule is absolute in bar index, so truncating the series never changes
earlier outputs (causal).
- **No lookahead** — pinned by `test_no_lookahead_features_values` (full vs
truncated series identity for all t < N).
- **Byte-identical measured sections on re-run.** Witness run
`20261020_053412` confirms it. The engine asserts every float is finite
(`report.assert_finite`) before writing, and JSON serializes with
`allow_nan=False`.
- `records.csv` carries the per-slot audit trail for every book.

## Data provenance

`data/regime_edge/SOURCE.json` records what the engine was actually fed
(yfinance proxies vs TDVA, fetched_at, bars per symbol). The engine reports
the marker's own record — a hardcoded feed claim is a lie the moment the
export moves to another source.

## Sources

- `scripts/run_edge_lab.py` (full CLI).
- `regime_edge/report.py` (`build_edge_lab`, `write_json`, `assert_finite`,
  `sanitize`).
- `regime_edge/data.py`, `tsfm.py`.
- `tests/test_regime_edge.py` — `test_edge_lab_schema_smoke` (end-to-end:
  runs the CLI on synthetic data, asserts all 10 top-level keys present,
  17 verdicts, all floats finite, artifact < 3 MB).
