# Baseline snapshot — Batch A freeze

> Captured 2026-09-16 for worklog
> `20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md` Phase 0.1.
> Compare later phases against **these** values, not against memory.

## Qualification gates (`config/feature_flags.yaml`)

| Key | Value |
|---|---|
| min_regime_conf | 0.60 |
| min_kelly | 0.02 |
| min_timesfm_confidence | 0.05 |
| min_ev | 0.0 |
| min_p_win | 0.55 |
| min_rr | 1.5 |
| timesfm_gate_mode | soft |
| signal_cooldown_minutes | 60 |

## Settlement / expiry

| Key | Value |
|---|---|
| EXPIRY_BARS | 48 (1h bars) |
| Historical same-bar priority | tp_first (close-walk) |
| Historical PnL | brick counts for TP/SL; MTM for expired (2026-09-08) |
| Costs | none on resolver path |

## Heavy sweep / selection

| Key | Value |
|---|---|
| heavy_sweep_deadline_aware_scoring_mode | live |
| heavy_sweep_atr_anchored_bricks_mode | shadow |
| heavy_sweep_reachability_mode | shadow |
| heavy_sweep_grid.max_sharpe | 5.0 |
| heavy_sweep_grid.min_trades | 5 |
| Default brick YAML | brick_size varies; sl_bricks=3, tp_bricks=6 |

## EV Engine (live effective)

| Item | Value |
|---|---|
| Sources | p_regime, p_markov_hold_n, p_timesfm (p_historical renormalized out) |
| Nominal weights | 0.25 / 0.20 / 0.25 (+0.30 historical unused) |
| Pooling | logit |
| MARKOV_HOLD_STEPS | 10 (env; not tp_bricks) |
| min_rr in EV allow | 1.5 (feature_flags) |

## Batch A new flags (introduced this freeze)

| Key | Value |
|---|---|
| settlement_contract_mode | live |
| settlement_same_bar_policy | tp_first |

## Recent structural metrics (from retrospective, for comparison only)

| Metric | Pre Sep 8 | Week Sep 8–15 |
|---|---|---|
| Qualified / week | 477 | 114–116 |
| Win rate (resolved) | 30.9% | 28.9% |
| Expiry rate | 60.6% | 21.1% |
| Weekly PnL | +$24,058 | −$892 |
| Calibration bias | +0.269 | +0.341 |

## Verification commands

```bash
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/test_settlement_contract.py tests/test_eod_resolver.py -q -p no:cacheprovider
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_settlement_parity.py
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_calibration_gap.py
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```
