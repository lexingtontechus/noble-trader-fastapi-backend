---
name: regime-edge-proof-engine
version: 0.1.0
description: Edge Lab proof engine for Kairos. Audits and fixes.
author: Hermes Agent
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [kairos, edge-lab, proof-engine, regime-edge, barrier-physics]
---

# regime-edge-proof-engine

An **offline audit engine** that assigns numeric VALUES to regimes across a time series and MEASURES whether those values predict the edge — instead of shadow-mode guessing. It produced the 17-verdict weaknesses evaluation of Kairos and justified the 7 live corrective fixes in the 2026-10-20 definitive remediation.

It does NOT run in the live signal path, does not place trades, does not gate signals. The **live** per-signal gate of the same name is its own skill:
`regime-edge-gate` (`.hermes/skills/regime-edge-gate/`).

## Two things called regime_edge — do not conflate

| | Offline proof engine (this skill) | Live qualification gate |
|---|---|---|
| Code | `regime_edge/` + `scripts/run_edge_lab.py` | `sweep_orchestrator/regime_edge.py` |
| Job | Audits Kairos vs history; verdicts + corrected geometry | Reads `nt_regime_edge`; rejects signal if WR<min_win_rate |
| Cadence | Daily 18:30 UTC; `edge_lab_run_mode` | Per-signal; `regime_edge_mode` |
| Data | TDVA OHLCV CSVs at `data/regime_edge/` | Live `nt_signal_sim` rows |
| Output | `worklog/.../regime_edge/<run>/edge_lab.json` | `nt_regime_edge` + local param store |
| Why separate | The run is evidence that would justify a gate flip; keying it to the gate mode makes that evidence unobservable when needed | Read at sweep from local store (fallback Supabase) |

## When to Use

- You need the why behind verdicts W-X1/X2/X3/X4/X5, W-G2/G4 and the 7 live fixes.
- You are re-running the engine or verifying a run (`scripts/run_edge_lab.py`).
- You are interpreting `edge_lab.json` / `summary.md` / `records.csv`.
- You are investigating `edge_lab_run_mode` cadence or barrier-floor numbers feeding W-C4/W-G2.
- You need the no-lookahead / gambler's-ruin / walk-forward-lockbox math.

Open a reference on demand: `skill_view(name='regime-edge-proof-engine', file_path='references/<file>')`.

## Prerequisites

- Repo: `noble-trader-fastapi-backend` (Kairos), `nobletradingapp` (Edge Lab dashboard).
- Data: `data/regime_edge/*.csv` (git-ignored; refresh via `scripts/export_edge_lab_data.py`; `SOURCE.json` records provenance).
- Python env: `PYTHONPATH=$PWD/src`, `.venv` has numpy/pandas/scipy/scikit-learn/hmmlearn/yaml.
- TDVA credentials only to refresh the cache; engine runs from CSVs.
- Live gate wiring needs the Kairos sweep process (see `noble-trader-repo-guide`).

## How to Run

Invoke through the `terminal` tool:

```bash
# proof-engine tests (19 + 62)
PYTHONPATH=$PWD/src .venv/bin/pytest tests/test_regime_edge.py tests/test_w_c1_shipped_ev.py tests/test_w_c2_heavy_fail_closed.py tests/test_w_c3_view_predicate.py tests/test_w_c4_ev_cost.py tests/test_w_x3_direction_stability.py tests/test_w_x4_chop_gate.py tests/test_w_g2_barrier_floor.py -q
# one full proof-engine run, 14 ACTIVE symbols seeded 7 (~30-40 min wall), resume-safe
PYTHONPATH=$PWD/src .venv/bin/python scripts/run_edge_lab.py
# 5-symbol comparability baseline (20261020_055039) or smoke run
PYTHONPATH=$PWD/src .venv/bin/python scripts/run_edge_lab.py --bars-limit 5000
# 4x faster smoke
EDGE_LAB_FAST=1 PYTHONPATH=$PWD/src .venv/bin/python scripts/run_edge_lab.py --checkpoint-dir <dir>
# refresh the 14-symbol TDVA cache first
PYTHONPATH=$PWD/src .venv/bin/python scripts/export_edge_lab_data.py --source tdva --hours 2160
```

## Quick Reference

```
run_edge_lab.py  --symbols [...] --data-dir data/regime_edge --out-dir worklog/kairos/reports/regime_edge
                 --bars-limit N (smoke) --seed 7 --checkpoint-dir <d> --recompute
EDGE_LAB_FAST=1  faster MC (sub=8)
edge_lab_run_mode: shadow|live|off  daily 18:30 UTC (orchestrator.py:1978); live->publish to nt_edge_lab_run
regime_edge_mode:  shadow|live|off   per-signal gate (orchestrator.py:5505)
```

## Verification

```bash
PYTHONPATH=$PWD/src .venv/bin/pytest tests/test_regime_edge.py -q   # 19 passed
ls worklog/kairos/reports/regime_edge/<latest>/{edge_lab.json,summary.md,records.csv}
# byte-identical measured sections on re-run (witness 20261020_053412)
```

## Reference Index (load on demand)

| load when | file |
|---|---|
| ONE-sentence answer | `references/what-it-is.md` |
| Explaining to a non-engineer | `references/plain-language.md` |
| Regimes, features, tri-state hysteresis | `references/regimes-and-features.md` |
| Bracket resolution / SL-first / MTM | `references/brackets.md` |
| Barrier physics, gambler's ruin, corrected geometry | `references/barrier-geometry.md` |
| Gate backtest (kairos vs corrected vs null) | `references/gate-backtest.md` |
| 17 verdicts + 7 live fixes | `references/verdicts-and-fixes.md` |
| CLI / artifacts / determinism | `references/edge-lab-cli.md` |
| Live wiring / cadence / nt_edge_lab_run | `references/live-wiring.md` |
| 14-symbol ACTIVE expansion (commit fd2cb64) | `references/_update_note.md` |
