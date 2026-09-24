# Active modes (single table)

> Generated for Batch F streamline (worklog 20260916). **This file is the
> operator cheat-sheet.** Authoritative values live in
> `config/feature_flags.yaml` (hot-reload on mtime). Do not duplicate
> incident essays here — link worklogs instead.

Last verified against `config/feature_flags.yaml` + code: 2026-09-22.

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
| `dynamic_atr_mode` | **shadow** | Tiered ATR risk-capped pick logged; live pick unchanged |
| `timesfm_gate_mode` | **soft** | Low TimesFM confidence shrinks blend weight, does not veto |
| `trend_probs_gate_mode` | **live** (2026-09-22) | Gated p_regime feeds EV (Brier beat raw 14/14) |
| `regime_label_debounce_mode` | **live** (2026-09-22) | 3-tick debounced label drives direction; reduces flip-flop |
| `timesfm.sigmoid_k` / `clamp_*` | 10 / [0.05, 0.95] | Price→p mapping (yaml only) |
| `timesfm_map_mode` | **zmap_live** | EV uses z-map p; shorts use 1−P(up) |
| `min_rr` | **1.5** (yaml `get_min_rr()`, not env) | Shared light EV allow + heavy grid filter. **Maintained non-factor on light qualification (2026-09-20):** RR is brick geometry; live rejects bind on kelly/regime/p_win, not `ev_allowed`. Not a strong/weak tier. |
| `qualification_gates.*` | see yaml | regime_conf 0.60, kelly 0.02, timesfm conf 0.05, ev 0, p_win 0.55 |
| `signal_cooldown_minutes` | 60 | Open prior signal blocks; restart on resolve |
| `calibration_bias_min_episodes` | 2 | Min independent episodes for bias verdict |
| `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` | **True** (settings) | Raw p_win always captured when present |
| `NOBLE_TRADER_CALIBRATION_RAW_REQUIRED` | **True** (settings) | No enforced fallback for Brier/raw-bias |

## Shadow (compute + persist; no live gate change)

| Flag | Effect |
|---|---|
| `p_win_bracket_mode` | Persists `p_win_bracket` + would-reject log; gates still use directional `p_win`. Gate thr `min_p_win_bracket: 0.20` (2026-09-22). Flip **after** `p_win_in_ev_mode: live`. |
| `p_win_bracket_unscored_policy` | **fail_closed** (2026-09-24): unscored `p_win_bracket` ⇒ `qualified=false`, `suppress_reason=bracket_unscored` on `nt_signal_sim` (G2). Research capture keeps the row. `fail_open` is research-only legacy. |
| `p_win_in_ev_mode` | **shadow** (2026-09-22 R2) — EV/Kelly still directional `p_win`; logs would-be bracket EV. `live` substitutes `p_win_bracket` (3-outcome, expire not a loss); `p_win_ok` keeps gating `p_win_raw`. Flip after `nt_live_expectancy_daily` bar (see `worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md` + `20260923_L1_STATUS_AND_NT_ROADMAP.md`). |
| `heavy_sweep_reachability_mode` | Reachability-aware pick comparison only |
| `heavy_sweep_atr_anchored_bricks_mode` | Extra ATR brick candidates in grid (shadow scoring) |
| `macro_regime_mode` | Capture macro_label; Kelly only if flipped live |
| `trend_regime_mode` | Capture trend_label |
| `context_alignment_gate_mode` | 8th gate computed, not in `qualified` |
| `dynamic_min_rr_mode` | Would-raise MIN_RR logged. **Superseded 2026-09-20:** no live flip; 1.5→2.0 step / matrix not pursued — base `min_rr` is already inert on light path |
| `timesfm_k_adaptive_mode` | Adaptive k logged, not applied |
| `signal_stream_meta_model_mode` | Coherent/noisy stream read |
| `markov_source_mode` | **trend_hmm_shadow** — legacy still feeds blend |

## Off

| Flag | Notes |
|---|---|
| `regime_warm_start_mode` | EM warm-start disabled after mixed real-data results |

## Measurement columns (not modes)

| Item | Where |
|---|---|
| Dual-report scorecard | `v_symbol_signal_scorecard*` → `pnl_closed_only` / `pnl_all_resolved` / `expiry_pct` (also `GROUP BY signal` = buy/sell) |
| BUY vs SELL outcomes | `v_signal_outcome_by_side` (qualified-stratified TP/SL/expired; migration 159) |
| BUY vs SELL reach gap | `v_reachability_side_gap` + `nt_bracket_reachability.signal=buy\|sell` |
| p_win_dir vs p_win_bracket | `nt_signal_sim` (migration 154) |
| Reachability provenance | `n_stored` / `n_proxy` (migration 153) |

## Related docs

- Pipeline: `docs/SIGNAL_PIPELINE.md`
- Quant audit: `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`
- Program worklog: `worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md`
