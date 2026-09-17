# Active modes (single table)

> Generated for Batch F streamline (worklog 20260916). **This file is the
> operator cheat-sheet.** Authoritative values live in
> `config/feature_flags.yaml` (hot-reload on mtime). Do not duplicate
> incident essays here — link worklogs instead.

Last verified against `config/feature_flags.yaml` + code: 2026-09-17.

## Cadence (live)

| Job | When |
|---|---|
| Light sweep | **every 15 min** (`*/15`, two symbol batches per slot) |
| Heavy sweep | Sunday 20:00 UTC |
| EOD resolve + reachability rebuild | daily 01:17 UTC |
| Intraday calibration correction | every 4h |

## Aggression label (not SL/TP tiering)

`passive` / `mid` / `aggressive` = **order-style** from `ev_per_dollar`
(high EV → passive limit OK; low EV → need aggressive fill). **Does not**
change `sl_bricks`/`tp_bricks` (those stay `brick_params.yaml`). Use
`EVEngine.edge_tier()` for high/mid/low EV language.

## Live behavior

| Flag | Mode | Effect |
|---|---|---|
| `settlement_contract_mode` | **live** | EOD resolver uses `settlement_contract.py` for outcome+PnL |
| `settlement_same_bar_policy` | **tp_first** | Historical same-bar TP priority (flip after A/B replay) |
| `heavy_sweep_deadline_aware_scoring_mode` | **live** | Grid Sharpe zeros trades held > 48h |
| `timesfm_gate_mode` | **soft** | Low TimesFM confidence shrinks blend weight, does not veto |
| `timesfm.sigmoid_k` / `clamp_*` | 10 / [0.05, 0.95] | Price→p mapping (yaml only) |
| `min_rr` | 1.5 | Shared light EV allow + heavy grid filter |
| `qualification_gates.*` | see yaml | regime_conf 0.60, kelly 0.02, timesfm conf 0.05, ev 0, p_win 0.55 |
| `signal_cooldown_minutes` | 60 | Open prior signal blocks; restart on resolve |
| `calibration_bias_min_episodes` | 2 | Min independent episodes for bias verdict |
| `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` | **True** (settings) | Raw p_win always captured when present |
| `NOBLE_TRADER_CALIBRATION_RAW_REQUIRED` | **True** (settings) | No enforced fallback for Brier/raw-bias |

## Shadow (compute + persist; no live gate change)

| Flag | Effect |
|---|---|
| `p_win_bracket_mode` | Persists `p_win_bracket` + would-reject log; gates still use directional `p_win` |
| `heavy_sweep_reachability_mode` | Reachability-aware pick comparison only |
| `heavy_sweep_atr_anchored_bricks_mode` | Extra ATR brick candidates in grid (shadow scoring) |
| `macro_regime_mode` | Capture macro_label; Kelly only if flipped live |
| `trend_regime_mode` | Capture trend_label |
| `context_alignment_gate_mode` | 8th gate computed, not in `qualified` |
| `dynamic_min_rr_mode` | Would-raise MIN_RR logged |
| `timesfm_k_adaptive_mode` | Adaptive k logged, not applied |
| `regime_label_debounce_mode` | Debounced label compared, not used for direction |
| `signal_stream_meta_model_mode` | Coherent/noisy stream read |
| `trend_probs_gate_mode` | Gated p_regime compared |
| `markov_source_mode` | **trend_hmm_shadow** — legacy still feeds blend |

## Off

| Flag | Notes |
|---|---|
| `regime_warm_start_mode` | EM warm-start disabled after mixed real-data results |

## Measurement columns (not modes)

| Item | Where |
|---|---|
| Dual-report scorecard | `v_symbol_signal_scorecard*` → `pnl_closed_only` / `pnl_all_resolved` / `expiry_pct` |
| p_win_dir vs p_win_bracket | `nt_signal_sim` (migration 154) |
| Reachability provenance | `n_stored` / `n_proxy` (migration 153) |

## Related docs

- Pipeline: `docs/SIGNAL_PIPELINE.md`
- Quant audit: `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`
- Program worklog: `worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md`
