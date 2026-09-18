# Signal pipeline (single reference)

> Batch F streamline (2026-09-16). One path: **generate → qualify →
> settle → calibrate → select**. Mode flags: `docs/ACTIVE_MODES.md`.
> Methodology findings: `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`.

## Diagram

```
TradingView 1h candles (200 light / 2160 heavy)
        │
        ├──────────────────────────────────────────────┐
        ▼                                              ▼
┌───────────────────┐                        ┌──────────────────┐
│ Heavy sweep       │  brick_params.yaml     │ Light sweep      │
│ daily 20:00 UTC   │ ─────────────────────► │ every ~15 min     │
│ Renko grid+Sharpe │                        │ HMM + TimesFM    │
│ deadline-aware    │                        │ EV + gates       │
└─────────┬─────────┘                        └────────┬─────────┘
          │ reachability shadow                      │
          ▼                                          ▼
   nt_heavy_sweep_*_shadow                 Discord + nt_signal_sim
                                                         │
                                                         ▼
                                            SettlementContract (48h)
                                                         │
                    ┌────────────────────────────────────┼──────────────┐
                    ▼                                    ▼              ▼
         nt_calibration_records              reachability buckets   scorecard
         raw + enforced p_win                (stored ATR only)      dual-report
                    │                                    │
                    ▼                                    ▼
         enforcement / drift                    expectancy_r (diag)
                    │
                    ▼
         p_win_dir + p_win_bracket (shadow capture)
```

## 1. Generate (light sweep)

**Module:** `regime_platform/sweep_orchestrator/orchestrator.py::_process_symbol`

| Step | What |
|---|---|
| Bars | 200 × 1h closes (`price_fetcher`) |
| Direction | `short` if `"bear" in regime_label else long` — **sole switch** |
| Bracket | `entry=last close`; SL/TP = entry ∓ `brick_size × sl/tp_bricks` from `config/brick_params.yaml` |
| p_dir | Logit pool: `p_regime` + `p_markov_hold_n` + `p_timesfm` (`ev_engine.py`) |
| Markov N | `resolve_markov_n_steps(tp_bricks)` — TP distance, not global 10 |
| EV | `p·TP − (1−p)·SL`; allow if EV>0 and RR≥`min_rr` |

`p_historical` is **not** in the live weight table (Batch F).

## 2. Qualify (gates)

Order: construct → **calibration enforce** → gates → cooldown.

| Gate | Threshold (yaml) |
|---|---|
| non-neutral signal | — |
| `regime_conf` | ≥ 0.60 |
| `effective_kelly` (post-enforce) | ≥ 0.02 |
| TimesFM | soft mode: weight shrink, not veto |
| EV allowed | EV>0, RR≥1.5 |
| `p_win` | hard floor 0.50; soft band to 0.55 shrinks Kelly (`p_win_soft_band` stamp) |
| cooldown | 60 min; open prior blocks |

Heavy path uses the **same enforce-before-gates order** (Batch D).

**Bracket gate (`p_win_bracket`)** is **shadow** only — logs would-reject.

## 3. Settle (outcome + PnL)

**Module:** `settlement_contract.py` (`settlement_contract_mode: live`)

- Deadline 48 × 1h bars
- Close-walk (OHLC touch when highs/lows present)
- Same-bar policy: `tp_first` (default) / `sl_first`
- Expired: MTM at last close
- PnL: brick counts for TP/SL; MTM for expired

**Horizon classes** (canonical — `worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md`):

| Class | Outcomes | Meaning |
|-------|----------|---------|
| **Short-horizon** | `tp_hit` / `sl_hit` within 48h | What gates, EV, scorecard win% are designed for |
| **Long-lag / geometry** | `expired` (open past 48h; often 72h–10d+) | Not a chosen swing — **geometry risk**; long-lag % feeds dynamic ATR |

## 4. Calibrate

| Piece | Rule |
|---|---|
| Population | `tp_hit`/`sl_hit` only for bias (expired excluded from W/L) |
| Long-lag | Not a calibration win/loss — **is** a geometry risk input (`pct_expired`, p90 hours) |
| Raw vs enforced | `predicted_p_win_raw` required for Brier/raw-bias |
| Bias | Episode-collapsed, 7d window, 3d half-life decay |
| Enforcement | Overconfident: Bayesian p_win shrink + Kelly×0.5. **Underconfident: no Kelly cut** |
| Reachability | `p_resolve × Beta P(win\|resolve)` by `(symbol, vol_tier, distance/ATR)`; stored ATR only |

## 5. Select (heavy sweep)

- 90d Renko grid; **deadline-aware Sharpe is live**
- Reachability pick / ATR-anchored extras: shadow
- Writes `config/brick_params.yaml` weekly

## Key modules

| Concern | Path |
|---|---|
| Light generate + gates | `sweep_orchestrator/orchestrator.py` |
| Heavy grid | `sweep_orchestrator/heavy_sweep.py` |
| EV / p_dir | `services/statistics/ev_engine.py` |
| Settlement | `sweep_orchestrator/settlement_contract.py` |
| Resolve | `sweep_orchestrator/eod_resolver.py` |
| p_win_bracket | `sweep_orchestrator/p_win_bracket.py` |
| Enforcement | `sweep_orchestrator/calibration_enforcement.py` |
| Feature flags | `config/feature_flags.yaml` |
| Brick params | `config/brick_params.yaml` |

## Replay / proof scripts

| Script | Purpose |
|---|---|
| `scripts/replay_settlement_parity.py` | Policy / OHLC A/B |
| `scripts/replay_calibration_gap.py` | Expired-as-loss vs resolved-only bias |
| `scripts/replay_p_win_bracket_gate.py` | Bracket gate go/no-go |
