"""
Feature Flags — git-tracked config for deliberately-set mode flags
════════════════════════════════════════════════════════════════════

Reads ``config/feature_flags.yaml``. See that file's header comment for
the full rationale (2026-08-26 config-centralization correction,
worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md): real secrets
belong in Lightning AI Studio Secrets, auto-tuned runtime state belongs
in ``tuning_registry.py``'s (gitignored) YAML, and everything else a
person deliberately sets — mode flags, thresholds — belongs here,
committed and reviewable in a diff.

Deliberately does NOT consult any environment variable. An existing
Studio Secret with a matching name has zero effect on the values this
module returns.

Hot-reloads on mtime change so an edit takes effect on the next call —
no process restart needed — cheap enough to call from hot per-tick
paths (one ``stat()`` per call; the YAML itself is only re-parsed when
the mtime actually changes).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("regime.core.feature_flags")

_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "feature_flags.yaml"

_DEFAULTS: dict[str, Any] = {
    # 2026-10-01: trend_hmm_shadow -> legacy_hmm (retired). See
    # p_markov_hold_n_mode below and worklog/kairos/reports/
    # 20261001_rm1_markov_source_eval.md — the shadow comparison answered
    # its question negatively (legacy beat trend_hmm 13/14 symbols;
    # 65.9% state disagreement vs 66.7% random). The trend_hmm values
    # remain selectable for a future re-evaluation; they are simply not
    # computed by default any more.
    "markov_source_mode": "legacy_hmm",
    # off | live — whether p_markov_hold_n participates in the EV blend.
    # "off" (2026-10-01): the sweep stops supplying transition_matrix/
    # current_state/p_markov_override to compute_ev(), so the engine's own
    # missing-source renormalisation drops the source and the blend becomes
    # p_regime / p_timesfm at 50/50. The legacy HmmModel still fits each
    # tick (it feeds the TA buffer + nt_sweep_result's legacy markov_p_up/
    # markov_p_dn columns) — it is kept running deliberately as the
    # substrate for the "bolster legacy HMM" research programme. "live"
    # restores the source to the blend; it must clear the re-entry bar in
    # that programme first (see the RM-1 evaluation's §6/§7).
    "p_markov_hold_n_mode": "off",
    # on | off — does ev_scale multiply position size in the sweep path?
    # "on" (shipped default, byte-identical to pre-flag behaviour).
    # "off" (2026-10-02, worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.2): drops
    # the ev_scale factor from effective_kelly, so size becomes
    # kelly_f x 1.0. ev_scale is min(1, ev_per_dollar/0.50), a function of
    # p_win — and p_win measured at bracket AUC 0.4869 with REL 0.0616 vs
    # RES 0.0008 (negative skill, overconfident). Sizing on a quantity with
    # below-chance discrimination is worse than not sizing on it. The
    # withdrawal is NOT neutral: removing the p_markov source alone moved
    # mean ev_scale 0.5576 -> 0.8403 and took 23% of signals from unsized to
    # sized (kairos/reports/20261002_leverage_eval.md). Restore to "on" only
    # when a calibrated probability replaces p_win in ev_scale
    # (20261002_SIGNAL_QUALITY_PLAN.md Phase 3).
    "ev_scale_sizing_mode": "on",
    # close_walk | intrabar — which basis the EOD resolver uses for TP/SL.
    # "close_walk" (shipped default): unchanged. Walks post-entry CLOSES and
    # takes the first close beyond a level. Optimistic — a bar whose close sits
    # between SL and TP counts as "no touch" even when its wick crossed one.
    # "intrabar": walks post-entry HIGH/LOW via resolve_signal_outcome_ohlc, so
    # a level is touched if price traded through it at any point in the bar.
    # Same basis as the mae_*_pct / mfe_*_pct columns, which makes a survivable
    # label internally consistent (worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.1;
    # quant-audit R1 "close-only ≠ first-touch").
    #
    # Changing this CHANGES OUTCOME SEMANTICS: it re-labels tp_hit/sl_hit on
    # every future resolution, and the survivor base rate measured at 0.1186
    # against the close-walk (vs 0.2805 bracket) is expected to move again.
    # FLIPPED TO "intrabar" 2026-10-02. The §3.1 kill criterion was run FIRST
    # and NOT met at any horizon (ghost gap 0.116-0.190 across 4h/24h/48h), so
    # the close-walk label was genuinely optimistic, not a measurement artefact.
    # Because the flip re-labels history, `calibration_cutover_mode` below must
    # be on AND the cutover timestamp set (migration 192) — otherwise the
    # calibration loop mixes two bases and every bias number is meaningless.
    "eod_resolver_basis": "intrabar",
    # on | off — restrict calibration reads to post-cutover evidence.
    # "on" (shipped 2026-10-02 with the basis flip): fetch_calibration_bias /
    # fetch_calibration_bias_raw read `v_calibration_records_current` (migration
    # 192) instead of the base table, so records labelled under the OLD
    # close_walk basis are excluded from bias / Brier / enforcement.
    # The view returns ALL rows when nt_calibration_cutover is empty, so this is
    # inert until an operator sets the cutover timestamp — flipping the flag and
    # setting the timestamp are separate, and either alone is safe.
    # "off" restores the pre-flip behaviour (read the base table).
    "calibration_cutover_mode": "on",
    "markov_flat_gate_threshold_stds": 0.5,
    "markov_min_occupancy": 0.10,
    "trend_probs_gate_mode": "shadow",
    "macro_regime_mode": "shadow",
    "macro": {
        "sma_window_days": 200,
        "ema_half_life_days": 10,
        "bear_bull_threshold": 0.05,
        "strength_saturation": 0.15,
        "boost_scale": 0.30,
        "penalty_scale": 0.50,
        "by_symbol": {
            "AAPL": {"enabled": False},
        },
    },
    "heavy_sweep_reachability_mode": "shadow",
    # 2026-09-29 GOAL: wr>=0.5, exp<=0.2. Settings BY CLASS; one live decision.
    #   commodity  TP 1-4  / SL 2-4   wr 0.54  exp 0%   pnl +1.28  GOAL
    #   equity     TP 8-14 / SL 6-12  wr 0.81  exp 2%   pnl +3.95  GOAL
    #   forex      TP 1-4  / SL 2-4   wr 0.44  exp 0%   pnl +1.38  (wr short)
    #   crypto     TP 3-8  / SL 1-3   wr 0.41  exp 11%  pnl +1.47  (wr short)
    "dynamic_atr_mode": "live",
    "dynamic_atr_risk_cap": 0.35,
    # SINGLE DEFINITION (2026-10-12, docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_
    # REDESIGN.md §1.3): these values MUST mirror config/feature_flags.yaml's
    # `dynamic_atr` global block — they are only the fallback for a missing
    # config file, never a second source of truth. The former `by_class` block
    # (commodity/equity/forex/crypto with DIFFERENT bands, incl. a forex band
    # whose SL exceeded its TP) was removed: it was shadowed by the YAML's own
    # by_class and would have silently changed behaviour if that key were ever
    # dropped. Per-symbol/class bands live ONLY in the YAML.
    "dynamic_atr": {
        "lookback_days": 10,
        "tp_atr": [1.71, 2.0, 3.0, 5.0, 8.0, 13.0],
        "sl_atr": [0.35, 1.0, 2.0, 3.5, 5.0, 8.0],
        "tp_atr_min": 2.0,
        "tp_atr_max": 20.0,
        "sl_atr_min": 0.15,
        "sl_atr_max": 12.0,
        "max_expiry": 0.20,
        "cost_atr": 0.1,
        "min_signals": 60,
        # vol_ratio = rv24/rv168 of 1h log returns at entry (VOLATILITY, not
        # volume). Optional band: walk + live pick only inside [min, max].
        # None = no gate. BTCUSD ships low-vol-only (see 20260930 worklog).
        "vol_ratio_min": None,
        "vol_ratio_max": None,
        "vol_ratio_short": 24,
        "vol_ratio_long": 168,
    },
    "atr_anchored_evidence_mode": "live",
    "atr_reopt_enabled": True,
    "atr_reopt_urgency": {
        "enabled": True,
        "elevated_ratio": 1.25,
        # 4h cycle floor — durable in nt_roadmap (not in-memory).
        # Was 48h in-memory: wiped on Studio restart → double-fire.
        "min_reopt_hours": 4.0,
        "cooldown_hours": 4.0,
        "vol_ratio_short": 24,
        "vol_ratio_long": 168,
        "auto_apply_clamps": True,
    },
    "heavy_publish_geometry_gate": "live",
    "geometry_intraday_scale_mode": "live",
    "geometry_tuning_sweep_mode": "live",
    "geometry_tuning_cooldown_hours": 6,
    "geometry_tuning_min_consecutive": 2,
    "light_publish_geometry_gate": "shadow",
    "dynamic_deadline_mode": "shadow",
    "dynamic_deadline": {
        "derive_from_reachability": True,
        "soft_deadline_percentile": 0.75,
        "hard_deadline_percentile": 0.90,
        "min_soft_deadline_hours": 12.0,
        "max_hard_deadline_hours": 120.0,
        "min_n_for_high_confidence": 50,
        "min_n_for_medium_confidence": 20,
        "by_symbol": {
            "XAUUSD": {"soft_deadline_hours": 18.0, "hard_deadline_hours": 36.0},
            "XAUEUR": {"soft_deadline_hours": 18.0, "hard_deadline_hours": 36.0},
            "XAUAUD": {"soft_deadline_hours": 18.0, "hard_deadline_hours": 36.0},
            "XAGUSD": {"soft_deadline_hours": 18.0, "hard_deadline_hours": 36.0},
            "USDCAD": {"soft_deadline_hours": 36.0, "hard_deadline_hours": 72.0},
            "USDJPY": {"soft_deadline_hours": 24.0, "hard_deadline_hours": 48.0},
        },
    },
    "dynamic_deadline_reopt": {
        "enabled": True,
        "cycle_hours": 4.0,
        "drift_threshold": 0.15,
        "min_n_for_update": 20,
    },
    "regime_edge_mode": "shadow",
    "regime_edge": {
        "min_n": 20,
        "min_win_rate": 0.50,
        "rebuild_interval_hours": 24,
    },
    # Edge-lab proof-engine CADENCE (2026-10-21).  Deliberately a SEPARATE lever
    # from ``regime_edge_mode``: that flag decides whether the regime_edge
    # QUALIFICATION GATE is live; this one decides whether the daily regime_edge
    # PROOF RUN executes.  The run is the evidence that would justify a gate flip,
    # so keying the run to the gate's own mode makes the evidence unobservable
    # exactly when it is needed — the same conflation the repo guide warns about
    # for parameter-vs-gate flags.
    #   off    — no job registered.
    #   shadow — refresh TDVA data, run the engine, write the artifact to the
    #            worklog, heartbeat nt_roadmap(edge_lab_run).  No Supabase write.
    #   live   — shadow + publish edge_lab.json to nt_edge_lab_run (the
    #            nobletradingapp edge-lab page reads v_edge_lab_run).
    "edge_lab_run_mode": "shadow",
    "edge_lab_run": {
        "data_source": "tdva",
        "hours": 2160,
    },
    # 2026-10-17 — aligned to the live config/feature_flags.yaml values.
    # These keys had drifted: _DEFAULTS said off/2.0 while the shipped YAML
    # said live/1.5. The YAML always won at runtime (every accessor reads
    # _load().get(key, _DEFAULTS[key])), so live behaviour was correct — but
    # the defaults misrepresented it, and a fresh environment with no YAML
    # would have silently run the NARROW grid (5 bricks, 30 combos) instead
    # of the live one (8 bricks, 72 combos). Same bug class as the
    # heavy_sweep_atr_anchored_bricks_mode unquoted-`off` incident
    # (roadmap id=48). See
    # docs/scopes/20261017_HEAVY_SWEEP_GRID_AND_COST_NOTE.md.
    "min_rr": 1.5,
    "heavy_sweep_grid": {
        "max_sharpe": 10.0,
        "min_trades": 10,
    },
    "heavy_sweep_atr_anchored_bricks_mode": "live",
    "heavy_sweep_deadline_aware_scoring_mode": "live",
    "settlement_contract_mode": "live",
    # 2026-10-08: aligned to the live intrabar resolver's actual behaviour
    # (it hardcoded SL-first). See config/feature_flags.yaml + worklog
    # 20261008_GEOMETRY_BASIS_DIVERGENCE.md.
    "settlement_same_bar_policy": "sl_first",
    # dynamic_atr consistency gate (2026-10-08, worklog
    # 20261008_GEOMETRY_BASIS_DIVERGENCE.md Stage 5): a pick whose walked TP
    # rate disagrees with the platform's measured bucket rate is the optimizer
    # contradicting its own measurements. shadow logs; live falls back to the
    # heavy pick; off disables.
    # GEOMETRY HARMONY (2026-10-12, worklog 20261012_GEOMETRY_HARMONY.md): the
    # ATR pick is the SINGLE SOURCE OF TRUTH for TP/SL. Brick is the documented
    # fail-open fallback, not a parallel system. All three gates live so the
    # solved geometry actually ships and artifacts are rejected at the gate
    # (the 12:1 RR the solver produced on intrabar envelopes). Fail-open is
    # preserved: no valid pick → brick fallback, logged as fallback.
    "dynamic_atr_consistency_mode": "live",
    "dynamic_atr_consistency_tol": 0.2,
    "dynamic_atr_light_mode": "live",
    # Walk-forward held-out gate (2026-10-09, worklog 20261009_WALKFORWARD_SPLIT.md):
    # select_tp_sl's e1>0 AND e2>0 is an in-sample split on the same data the
    # bracket was optimised over, so a RR=6 pick passes trivially. This measures
    # the pick on the half it was NOT chosen on. shadow logs; live rejects the
    # overfit pick (heavy kept); off disables. Thin folds fail open.
    "dynamic_atr_walkforward_mode": "live",
    # calibration_bias_published_only (2026-10-12, worklog
    # 20261012_POOLED_ATR_AND_SCORECARD_FILTER.md): the scorecard/bias STANDARD
    # is the PUBLISHED book (qualified=true) — what users experience. The full
    # population stays available as a diagnostic stratifier. When on, bias
    # readers query v_calibration_records_published (qualified=true only).
    "calibration_bias_published_only": True,
    "dynamic_atr_walkforward_min_fold_n": 20,
    "dynamic_atr_walkforward_min_oos": 0.0,
    # Part B single source of truth (2026-10-09, worklog
    # 20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md): "yaml" (historical
    # read) | "store" (param_store brick_state tier is the geometry authority;
    # YAML is the derived cache). Ships "yaml" so the flip is one config edit.
    "brick_params_source": "yaml",
    "p_win_bracket_mode": "shadow",
    "min_p_win_bracket": 0.20,
    "p_win_in_ev_mode": "live",
    # p_win_bracket_source (2026-10-12, worklog
    # 20261012_BUCKET_INVERSION_HARMONIZATION.md): how p_win_bracket is computed
    # from the reachability cell. all_in (default): p_win_bracket = the table's
    # all-in TP share (pct_hit_tp) — the honest number, harmonized with the
    # diagnostic table and reopt. derived (legacy): p_resolve ×
    # beta_shrunk(tp/(tp+sl)) — prior-inflated for thin resolved cells, which
    # priced dead 0%-TP narrow buckets at ~0.5 and inverted winnability vs
    # bucket width. all_in removes that inversion; derived retained for A/B.
    "p_win_bracket_source": "all_in",
    # ── Posterior Barrier Model (2026-10-14, worklog
    # 20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md) ──────────────
    # One probability (analytic barrier null + Dirichlet posterior on all
    # three outcome classes), one price (3-outcome EV with costs), one gate
    # (EV lower credible bound > 0). Every mode flag below ships at its
    # legacy/no-op default: the Phase A build is shadow-only (stamps +
    # posterior writes); each Phase B/C/D flip is a one-line config edit
    # gated on its era-clean replay acceptance bar. The flips:
    #   pbm_mode           off|shadow   — stamps + resolver posterior writes
    #   pbm_ev_mode        off|shadow|live — Phase B: EV input swap
    #   pbm_geometry_mode  grid|posterior  — Phase C: continuous solve
    #   pbm_sizing_mode    legacy|conservative — Phase D: bound-scaled Kelly
    "pbm_mode": "shadow",
    "pbm_ev_mode": "off",
    "pbm_geometry_mode": "grid",
    "pbm_sizing_mode": "legacy",
    # prior pseudo-counts: evidence needed to move a cell off physics
    "pbm_posterior_k0": 30.0,
    # outcome decay half-life basis, days (exponential 2^(-age/tau))
    "pbm_posterior_tau_days": 14.0,
    # per-bar log-vol in ATR units for the analytic anchor; 1.0 is the
    # proposal's sigma = ATR_true_range/price by construction
    "pbm_sigma_atr": 1.0,
    # lower-bound mass for the one gate (0.05 = the "5% credible bound")
    "pbm_ev_bound_q": 0.05,
    # per-symbol cost table (ATR units); empty = honest fallback to the
    # band's flat cost_atr until the operator tunes real spreads
    "pbm_cost_atr_by_symbol": {},
    # evidence-triggered reopt: minimum NEW resolved outcomes per symbol
    # before a re-solve is worth its churn (Phase C)
    "pbm_reopt_min_new_outcomes": 25,
    # claims layer (2026-10-27, worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md):
    #   claims_mode  off|shadow — Phase A: nt_signal_claims registration at
    #   qualification (bracket claim mandatory) + resolver-side scoring through
    #   the SAME settlement contract. shadow = writes only; nothing reads the
    #   ledger until Phase B's claims_publish flip. Fails open on read error.
    "claims_mode": "shadow",
    # ev_hard_floor (2026-10-11, worklog 20261011_CALIBRATION_REACHABILITY_REFRAME.md):
    # when p_win_in_ev_mode=live, the p_win hard floor is 0.0 (the bracket
    # probability is on a different scale), so negative-EV signals were
    # publishing at ev=-40 with kelly=0. This is the missing gate: a signal
    # whose effective EV is below this floor CANNOT publish, regardless of the
    # reachability probability. Default 0.0 = EV must be positive to publish
    # (the honest expectation: a trade you expect to lose money on must not
    # ship). This is the live fix for "calibrated but large losses".
    "ev_hard_floor": 0.0,
    "min_p_win_bracket_by_asset_class": {
        "metal": 0.20,
        "forex": 0.30,
        "equity": 0.40,
        "crypto": 0.50,
        "commodity": 0.30,
        "index": 0.40,
        "etf": 0.40,
    },
    "rank_score_calibration_mode": "shadow",
    "rank_score_calibration_slope": 0.5,
    "geometry_band_urgency": {
        "enabled": True,
        "urgency_threshold": 0.15,
        "min_n": 20,
        "cooldown_hours": 4,
        "min_age_hours": 4,
        # lookback_days MUST be listed here: get_geometry_band_urgency_config()
        # only copies keys already present in _DEFAULTS, so a YAML value for a
        # key missing here is silently ignored (same trap as the MIN_RR split).
        "lookback_days": 7,
        "drift_threshold_pct": 0.10,
        "drift_threshold_hours": 6.0,
        "by_symbol": {
            "FOREX": {"urgency_threshold": 0.15, "min_n": 20},
            "XAUUSD": {"urgency_threshold": 0.20, "min_n": 30},
            "XAGUSD": {"urgency_threshold": 0.15, "min_n": 20},
            "BTCUSD": {"urgency_threshold": 0.10, "min_n": 15},
        },
    },
    "p_win_bracket_unscored_policy": "fail_closed",
    "geometry_incubation_mode": "shadow",
    "geometry_incubation_min_days": 7,
    "geometry_incubation_min_resolved": 20,
    "geometry_incubation_max_expiry_regression": 0.10,
    # Adaptive (evidence-relative) time floor — see geometry_incubation.adaptive_min_days
    # (worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md Part B). off = static
    # min_days (the historical behaviour); live = per-symbol floor derived from
    # the fresh nt_signal_sim resolution rate, clamped to [floor_days, cap_days].
    "geometry_incubation_adaptive_days_mode": "shadow",
    "geometry_incubation_adaptive_floor_days": 1,
    "geometry_incubation_adaptive_cap_days": 7,
    # Per-signal dynamic settlement expiry (worklog 20261009_P90_HOURS_LIVE_MODE.md).
    # fixed = the historical hard-coded EXPIRY_BARS=48 (byte-identical);
    # shadow = compute + log the dynamic expiry, still settle on 48 (zero live
    # change); live = settle each signal on a per-bracket expiry derived from
    # nt_bracket_reachability (median*safety for tight buckets, p90 for the
    # wide 4+ bucket, clamped to [floor_hours, cap_hours]).
    "settlement_expiry_mode": "fixed",
    "settlement_expiry_floor_hours": 12.0,
    "settlement_expiry_cap_hours": 72.0,
    "settlement_expiry_tight_safety_mult": 2.0,
    "signal_sim_retention_resolved_days": 120,
    "kairos_mode": "shadow",
    # clef_flash_mode (2026-10-04, docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md
    # + 20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md): off | shadow | live.
    # shadow (default): call the Cloudflare Clef-Flash /decision endpoint for
    # qualifiable light-sweep signals and stamp p_clef_dir/p_clef_wait/
    # p_clef_symbol on nt_signal_sim (migration 194). RESEARCH ONLY — never in
    # P_WIN_WEIGHTS_FULL, never passed to compute_ev(), never touches
    # qualified/kelly/SL/TP. live: reserved for the classifier cutover at
    # kairos_pivot/macro_forecast (does not change the shadow stamp). off: no
    # HTTP call, no stamp.
    "clef_flash_mode": "shadow",
    # p_win_reachability_mode (2026-10-11, worklog
    # 20261011_REACHABILITY_PROB_BUILD.md): off | shadow | live.
    # off (default): no reachability stamp. shadow: compute the
    # reachability-conditioned probability P_reach and the blend-vs-reach
    # delta and stamp p_reach / p_reach_delta on nt_signal_sim — MEASUREMENT
    # ONLY, EV/Kelly still use the existing blend. live: P_reach replaces the
    # direction-only p_win in EV/Kelly (gated on the walk-forward validation).
    "p_win_reachability_mode": "off",
    "regime_warm_start_mode": "off",
    "regime_label_debounce_mode": "off",
    "regime_label_debounce_ticks": 3,
    "regime_state_persist_mode": "live",
    "regime_state_persist_max_age_hours": 3.0,
    "light_sweep_catchup_mode": "live",
    "calibration_resolve_hourly_mode": "shadow",
    "calibration_evidence_pool_mode": "shadow",
    "calibration_evidence_tiers": ["qualified", "suppressed"],
    "eod_resolver_scan_mode": "full",
    "eod_resolver_page_pause_s": 2.0,
    "qualification_gates": {
        "min_regime_conf": 0.60,
        "min_kelly": 0.02,
        "min_timesfm_confidence": 0.05,
        "min_ev": 0.0,
        "min_p_win": 0.55,
    },
    "timesfm_gate_mode": "soft",
    "timesfm_map_mode": "zmap_live",
    "timesfm": {
        "sigmoid_k": 10.0,
        "clamp_min": 0.05,
        "clamp_max": 0.95,
    },
    "sweep_after_hours": False,
    "price_staleness_max_hours": 1.0,
    "signal_cooldown_minutes": 15.0,
    "expiry_rate_alert": {
        "enabled": True,
        "threshold_pct": 50.0,
        "min_resolved": 15,
        "cooldown_hours": 24.0,
    },
    "calibration_bias_min_episodes": 2,
    "signal_stream_meta_model_mode": "off",
    "renko_brick_retention_days": 2,
    "sweep_result_retention_days": 30,
    "regime_log_retention_days": 30,
    "knowledge_index_enabled": True,
    "self_learning_detectors_enabled": True,
    "self_learning_triage_enabled": True,
    "macro_forecast_agent_enabled": True,
    "trend_regime_mode": "off",
    "context_alignment_gate_mode": "shadow",
    "dynamic_min_rr_mode": "shadow",
    "dynamic_min_rr_consolidation_value": 2.0,
    "timesfm_k_adaptive_mode": "shadow",
    "timesfm_k_floor": 5.0,
    "timesfm_k_ceiling": 60.0,
    # ── Router Edge Model (R2, 2026-10-23) — the strategy router as a
    # measured next-candle edge predictor + the TimesFM exit ladder.
    # See router_edge.py's module docstring and
    # docs/scopes/20261023_TSFM_VS_REASONING_LLM_AND_ROUTER_EDGE.md.
    "router_edge_mode": "shadow",
    "router_edge_weight": 5.0,
    "router_edge_k0": 20.0,
    "router_edge_tau_days": 90.0,
    "router_edge_min_n_eff": 30.0,
    "timesfm_blend_mode": "live",
    "trend": {
        "sma_short_days": 10,
        "sma_mid_days": 20,
        "sma_long_days": 50,
        "rsi_period": 14,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "crossover_freshness_days": 3,
        "strength_saturation": 0.10,
        "boost_scale": 0.3,
        "penalty_scale": 0.5,
    },
    # ── Task 40-b corrective gates (2026-10-19 weaknesses evaluation) ──────
    # User mandate: NO MORE SHADOW MODE — every corrective mechanism below
    # ships LIVE with a measured threshold. Each block still honours the
    # off|shadow|live mode vocabulary of its sibling flags so an operator
    # can demote one to shadow/off for triage without new code.
    #
    # W-X4 chop gate: Kaufman efficiency_24h is computed and stamped every
    # light tick (kairos.py) but nothing consumed it — in the measured
    # 69.1%-consolidating window the platform published at conf 0.649-0.793
    # the whole time. 0.30 floor: ER below ~0.3 is a two-sided market where
    # the 4-state HMM's forced bull/bear direction is rank noise.
    "chop_gate": {
        "mode": "live",
        "min_efficiency_24h": 0.30,
    },
    # W-X3 direction stability: the per-direction cooldown let the OPPOSITE
    # direction publish instantly after a confirmed flip (flip-flop at 3h
    # granularity), and shifts_24h was stamped but never gated.
    "direction_stability": {
        "mode": "live",
        "cross_direction_cooldown_h": 6,
        "max_shifts_24h": 6,
    },
    # W-G2/G4 barrier floor: tight-SL bands are SL-farms by physics (repo's
    # own barrier_null, sigma=1 ATR/bar, T=48: XAUUSD 4.94/1.00 -> pi_L
    # 0.831; BTCUSD 8.07/1.77 -> pi_L 0.791). pi_e_cap is stamp+warn only
    # while expiry is globally fixed at 48h — enforcing it would starve the
    # wide equity bands (US500 pi_E=0.70) entirely.
    "barrier_floor": {
        "mode": "live",
        "pi_sl_cap": 0.62,
        "pi_e_cap": 0.25,
    },
    # W-C4 cost-aware EV: live EV charged expiry as zero PnL and included NO
    # spread/slippage anywhere (only the shadow PBM charged cost_atr).
    # Round-trip (spread + slippage + entry-lag allowance) in ATR units,
    # multiplied by the current ATR at the compute_ev call sites. "metals"
    # is accepted as an alias of the canonical "metal" asset-class key.
    "ev_costs": {
        "mode": "live",
        "cost_atr_by_class": {
            "forex": 0.04,
            "metals": 0.06,
            "crypto": 0.10,
            "equity": 0.06,
            "index": 0.05,
        },
    },
    # ── EOD recalibration knobs (2026-10-20) ──────────────────────────────
    # Migrated out of settings.py, which read them as env-overridable Settings
    # fields. The values below are the code defaults that were live (no
    # `.env.local` entry, no Studio Secret) — see config/feature_flags.yaml's
    # own comment for the rationale and the MIN_RR precedent.
    "eod_correction": {
        "intraday_enabled": True,
        "interval_hours": 4,
    },
    "eod_bias": {
        "window_days": 7,
        "min_records": 20,
    },
    "eod_drift_monitor": {
        "enabled": True,
    },
    # TimesFM service knobs (model config + fallback toggle). Separate block
    # from `timesfm` above — _load() shallow-merges, so folding these into it
    # would replace that dict and drop sigmoid_k/clamp_min/clamp_max.
    "timesfm_service": {
        "model_id": "google/timesfm-2.5-200m-pytorch",
        "max_context": 1024,
        "max_horizon": 256,
        "disable_fallback": False,
    },
}

_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None


def _load() -> dict[str, Any]:
    global _cache, _cache_mtime
    try:
        mtime = _YAML_PATH.stat().st_mtime
    except OSError:
        if _cache is None:
            logger.warning(
                "feature_flags: %s not found — using hardcoded defaults %s",
                _YAML_PATH, _DEFAULTS,
            )
            _cache = dict(_DEFAULTS)
        return _cache

    if _cache is None or mtime != _cache_mtime:
        try:
            import yaml
            with open(_YAML_PATH, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            _cache = {**_DEFAULTS, **loaded}
            _cache_mtime = mtime
            logger.info("feature_flags: loaded %s", _YAML_PATH)
        except Exception as exc:
            logger.error(
                "feature_flags: failed to load %s (%s: %s) — keeping previous values",
                _YAML_PATH, type(exc).__name__, exc,
            )
            if _cache is None:
                _cache = dict(_DEFAULTS)
    return _cache


def get_markov_source_mode() -> str:
    """``legacy_hmm`` | ``trend_hmm_shadow`` | ``trend_hmm_live``.

    Since 2026-10-01 the shipped value is ``legacy_hmm`` — the
    ``trend_hmm_shadow`` comparison was retired after it answered its
    question negatively (see ``get_p_markov_hold_n_mode``).
    """
    return str(_load().get("markov_source_mode", _DEFAULTS["markov_source_mode"])).lower()


def get_p_markov_hold_n_mode() -> str:
    """``off`` | ``live`` — does p_markov_hold_n feed the EV blend?

    ``off`` (shipped 2026-10-01) makes the sweep pass no Markov source to
    ``compute_ev()``, so the engine renormalises it out and the blend is
    p_regime / p_timesfm. Anything other than an explicit ``live`` is
    treated as ``off`` — a typo must not silently re-enable a source that
    was deliberately withdrawn.
    """
    raw = str(
        _load().get("p_markov_hold_n_mode", _DEFAULTS["p_markov_hold_n_mode"])
    ).lower()
    return "live" if raw == "live" else "off"


def get_ev_scale_sizing_mode() -> str:
    """``on`` | ``off`` — does ``ev_scale`` multiply position size?

    ``on`` (shipped default) keeps ``effective_kelly = kelly_f × ev_scale``.
    ``off`` (2026-10-02) makes it ``kelly_f × 1.0``, decoupling size from
    ``p_win`` — see ``_DEFAULTS["ev_scale_sizing_mode"]`` for the rationale
    and ``worklog/20261002_SIGNAL_QUALITY_PLAN.md`` §3.2.

    Anything other than an explicit ``off`` is treated as ``on``, so a typo
    cannot silently change position sizing.

    **YAML booleans are handled explicitly.** An unquoted ``off`` in YAML 1.1
    parses as the boolean ``False``, whose ``str()`` is ``"false"`` — which
    would resolve to ``"on"``, the opposite of what the author wrote. Mapping
    the booleans directly avoids that footgun (the file still quotes the value,
    but this makes an unquoted one harmless rather than misleading).
    """
    raw = _load().get("ev_scale_sizing_mode", _DEFAULTS["ev_scale_sizing_mode"])
    if isinstance(raw, bool):
        return "on" if raw else "off"
    return "off" if str(raw).lower() == "off" else "on"


def get_calibration_cutover_mode() -> str:
    """``on`` | ``off`` — restrict calibration reads to post-cutover evidence?

    ``on`` (shipped) makes the bias readers use ``v_calibration_records_current``
    (migration 192), excluding records labelled under the pre-flip basis. The view
    is inert until ``nt_calibration_cutover`` has a row, so this flag alone
    changes nothing. Anything that is not an explicit ``off`` resolves to ``on``
    — the safe direction here is to *exclude* stale-basis evidence, not include it.
    """
    raw = _load().get("calibration_cutover_mode", _DEFAULTS["calibration_cutover_mode"])
    return "off" if str(raw).lower() == "off" else "on"


def get_eod_resolver_basis() -> str:
    """``close_walk`` | ``intrabar`` — the EOD resolver's TP/SL basis.

    ``close_walk`` (shipped default) is the historical behaviour. ``intrabar``
    walks high/low instead, making the label consistent with the ``mae_*_pct``
    columns. Anything that is not an explicit ``intrabar`` resolves to
    ``close_walk``, so a typo cannot silently change outcome semantics.
    """
    raw = _load().get("eod_resolver_basis", _DEFAULTS["eod_resolver_basis"])
    return "intrabar" if str(raw).lower() == "intrabar" else "close_walk"


def get_markov_flat_gate_threshold_stds() -> float:
    """Standard deviations of trend-feature noise a component's mean must
    clear before being treated as directional (Tier 1/2 HMM fix)."""
    try:
        return float(_load().get(
            "markov_flat_gate_threshold_stds",
            _DEFAULTS["markov_flat_gate_threshold_stds"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["markov_flat_gate_threshold_stds"]


def get_markov_min_occupancy() -> float:
    """Minimum Viterbi-occupancy share a trend component must clear
    before its mean is trusted at all (root-cause classifier)."""
    try:
        return float(_load().get(
            "markov_min_occupancy", _DEFAULTS["markov_min_occupancy"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["markov_min_occupancy"]


def get_trend_probs_gate_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Tier 2 HMM fix)."""
    return str(_load().get(
        "trend_probs_gate_mode", _DEFAULTS["trend_probs_gate_mode"],
    )).lower()


def get_macro_regime_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (long-range macro filter)."""
    return str(_load().get(
        "macro_regime_mode", _DEFAULTS["macro_regime_mode"],
    )).lower()


def get_macro_config() -> dict[str, Any]:
    """Macro filter configuration (SMA window, thresholds, per-symbol overrides)."""
    defaults = dict(_DEFAULTS.get("macro", {}))
    cfg = _load().get("macro", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def is_macro_enabled_for_symbol(symbol: str) -> bool:
    """Per-symbol macro filter enable/disable.

    Stage 2 (2026-10-05): AAPL's 200-day SMA is a constant (always "bull"),
    not a signal. The macro filter is disabled for AAPL — the HMM regime
    is the correct clock for a 22-40h signal. See worklog/20261005_AAPL_MACRO_REFRAME.md.
    """
    cfg = get_macro_config()
    by_symbol = cfg.get("by_symbol", {})
    sym_cfg = by_symbol.get(symbol, {})
    return sym_cfg.get("enabled", True)


def _macro_number(key: str) -> float:
    try:
        macro_cfg = _load().get("macro", _DEFAULTS["macro"])
        if not isinstance(macro_cfg, dict):
            macro_cfg = _DEFAULTS["macro"]
        return float(macro_cfg.get(key, _DEFAULTS["macro"][key]))
    except (TypeError, ValueError):
        return _DEFAULTS["macro"][key]


def get_macro_sma_window_days() -> int:
    return int(_macro_number("sma_window_days"))


def get_macro_ema_half_life_days() -> float:
    return _macro_number("ema_half_life_days")


def get_macro_bear_bull_threshold() -> float:
    return _macro_number("bear_bull_threshold")


def get_macro_strength_saturation() -> float:
    return _macro_number("strength_saturation")


def get_macro_boost_scale() -> float:
    return _macro_number("boost_scale")


def get_macro_penalty_scale() -> float:
    return _macro_number("penalty_scale")


def get_trend_regime_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (medium-term 10-50 day trend
    filter — SMA crossover + RSI, worklog
    20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7)."""
    return str(_load().get(
        "trend_regime_mode", _DEFAULTS["trend_regime_mode"],
    )).lower()


def _trend_number(key: str) -> float:
    try:
        trend_cfg = _load().get("trend", _DEFAULTS["trend"])
        if not isinstance(trend_cfg, dict):
            trend_cfg = _DEFAULTS["trend"]
        return float(trend_cfg.get(key, _DEFAULTS["trend"][key]))
    except (TypeError, ValueError):
        return _DEFAULTS["trend"][key]


def get_trend_sma_short_days() -> int:
    return int(_trend_number("sma_short_days"))


def get_trend_sma_mid_days() -> int:
    return int(_trend_number("sma_mid_days"))


def get_trend_sma_long_days() -> int:
    return int(_trend_number("sma_long_days"))


def get_trend_rsi_period() -> int:
    return int(_trend_number("rsi_period"))


def get_trend_rsi_overbought() -> float:
    return _trend_number("rsi_overbought")


def get_trend_rsi_oversold() -> float:
    return _trend_number("rsi_oversold")


def get_trend_crossover_freshness_days() -> int:
    return int(_trend_number("crossover_freshness_days"))


def get_trend_strength_saturation() -> float:
    return _trend_number("strength_saturation")


def get_trend_boost_scale() -> float:
    return _trend_number("boost_scale")


def get_trend_penalty_scale() -> float:
    return _trend_number("penalty_scale")


def get_context_alignment_gate_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Tier 1 corrected — an
    independent 8th qualification gate that fails only when BOTH macro
    and trend filters confidently oppose the trade direction. Worklog
    20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part
    10)."""
    return str(_load().get(
        "context_alignment_gate_mode", _DEFAULTS["context_alignment_gate_mode"],
    )).lower()


def get_dynamic_min_rr_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (regime-conditional MIN_RR floor.
    Worklog 20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md
    Part 11)."""
    return str(_load().get(
        "dynamic_min_rr_mode", _DEFAULTS["dynamic_min_rr_mode"],
    )).lower()


def get_dynamic_min_rr_consolidation_value() -> float:
    try:
        return float(_load().get(
            "dynamic_min_rr_consolidation_value",
            _DEFAULTS["dynamic_min_rr_consolidation_value"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["dynamic_min_rr_consolidation_value"]


def get_timesfm_k_adaptive_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (TimesFM sigmoid-k dampening
    driven by trend_strength. Worklog
    20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part
    12)."""
    return str(_load().get(
        "timesfm_k_adaptive_mode", _DEFAULTS["timesfm_k_adaptive_mode"],
    )).lower()


def get_timesfm_k_floor() -> float:
    try:
        return float(_load().get("timesfm_k_floor", _DEFAULTS["timesfm_k_floor"]))
    except (TypeError, ValueError):
        return float(_DEFAULTS["timesfm_k_floor"])


def get_timesfm_blend_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — the TimesFM EXIT ladder (R2,
    2026-10-23; router_edge.py). ``live`` (default): p_timesfm feeds the
    directional EV blend exactly as today (byte-identical). ``shadow``:
    TimesFM is still fetched and skill-scored on the router-edge outcome
    stream, but p_timesfm is withheld from compute_ev (the blend
    renormalizes over the remaining sources) — the A/B measurement that
    decides keep-vs-cut. ``off``: not fetched at all (zero network cost);
    the renormalization handles its absence. The measured verdict comes
    from router_edge_report()['timesfm_skill'] vs ['router_skill'] on the
    SAME stamped outcomes."""
    mode = str(_load().get(
        "timesfm_blend_mode", _DEFAULTS["timesfm_blend_mode"],
    )).lower()
    return mode if mode in ("off", "shadow", "live") else "live"


def get_router_edge_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — the Router Edge Model (R2).
    ``shadow`` (default): the router's verdict is turned into a measured
    P(next candle up | symbol, archetype, direction) — stamped, resolved,
    skill-scored per tick — with NO live effect (the EV blend is
    byte-identical). ``live``: p_router additionally enters compute_ev as
    a blend source (evidence-gated per cell by router_edge_min_n_eff).
    ``off``: the tick is skipped entirely (zero cost)."""
    mode = str(_load().get(
        "router_edge_mode", _DEFAULTS["router_edge_mode"],
    )).lower()
    return mode if mode in ("off", "shadow", "live") else "shadow"


def get_router_edge_weight() -> float:
    """Relative blend weight for p_router when router_edge_mode=live.
    Default 5.0 = a peer of p_timesfm's 5 (same scale as the raw weights
    behind P_WIN_WEIGHTS_FULL; the engine renormalises at blend time)."""
    try:
        return float(_load().get(
            "router_edge_weight", _DEFAULTS["router_edge_weight"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["router_edge_weight"])


def get_router_edge_k0() -> float:
    """Shrinkage pseudo-counts anchoring each cell on the symbol base rate
    (the PBM anchor convention; see router_edge.predict_p_up)."""
    try:
        return float(_load().get(
            "router_edge_k0", _DEFAULTS["router_edge_k0"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["router_edge_k0"])


def get_router_edge_tau_days() -> float:
    """Evidence half-life (days) for the decay-weighted cell counts —
    2^(-age/tau), the PBM decay convention."""
    try:
        return float(_load().get(
            "router_edge_tau_days", _DEFAULTS["router_edge_tau_days"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["router_edge_tau_days"])


def get_router_edge_min_n_eff() -> float:
    """Per-cell effective-sample gate before p_router may enter the live
    blend (shadow stamps always flow — being wrong in shadow costs
    nothing). Default 30."""
    try:
        return float(_load().get(
            "router_edge_min_n_eff", _DEFAULTS["router_edge_min_n_eff"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["router_edge_min_n_eff"])


def get_timesfm_k_ceiling() -> float:
    try:
        return float(_load().get("timesfm_k_ceiling", _DEFAULTS["timesfm_k_ceiling"]))
    except (TypeError, ValueError):
        return _DEFAULTS["timesfm_k_ceiling"]


def get_heavy_sweep_reachability_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Phase 3a of the calibration
    reframe — reachability/expectancy-aware heavy-sweep combo pick)."""
    return str(_load().get(
        "heavy_sweep_reachability_mode", _DEFAULTS["heavy_sweep_reachability_mode"],
    )).lower()


def get_dynamic_atr_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — tiered ATR risk-capped pick."""
    return str(_load().get(
        "dynamic_atr_mode", _DEFAULTS.get("dynamic_atr_mode", "shadow"),
    )).lower()


def get_dynamic_atr_consistency_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — pick ↔ reachability consistency gate.

    2026-10-08 (worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md Stage 5):
    a pick whose walked TP rate disagrees with the platform's own measured
    bucket rate by more than ``dynamic_atr_consistency_tol`` is the optimizer
    contradicting its own measurements. ``shadow`` logs; ``live`` falls back
    to the heavy pick; ``off`` disables.
    """
    return str(_load().get(
        "dynamic_atr_consistency_mode",
        _DEFAULTS.get("dynamic_atr_consistency_mode", "shadow"),
    )).lower()


def get_dynamic_atr_consistency_tol() -> float:
    try:
        return float(_load().get(
            "dynamic_atr_consistency_tol",
            _DEFAULTS.get("dynamic_atr_consistency_tol", 0.20),
        ))
    except (TypeError, ValueError):
        return 0.20


def get_dynamic_atr_light_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — light-sweep ATR geometry apply.

    2026-10-09 (worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): the light
    path now reads the SAME persisted ATR pick the heavy path chose, and
    applies it to the bracket. ``shadow`` (default) computes and logs the
    would-apply bracket without changing the published signal; ``live``
    applies it; ``off`` disables. Ships shadow-first because this changes a
    live signal's geometry.
    """
    return str(_load().get(
        "dynamic_atr_light_mode",
        _DEFAULTS.get("dynamic_atr_light_mode", "shadow"),
    )).lower()


def get_dynamic_atr_walkforward_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — walk-forward held-out gate on the pick.

    2026-10-09 (worklog 20261009_WALKFORWARD_SPLIT.md): ``select_tp_sl``'s
    only robustness check is an in-sample time split on the same data the
    bracket was optimised over, so a RR=6 pick passes trivially. This measures
    the chosen bracket on the half it was NOT chosen on. ``shadow`` logs the
    in/out-of-sample expectancy; ``live`` rejects an overfit pick (heavy kept);
    ``off`` disables. Thin folds fail open. Unknown values fall back to shadow.
    """
    v = str(_load().get(
        "dynamic_atr_walkforward_mode",
        _DEFAULTS.get("dynamic_atr_walkforward_mode", "shadow"),
    )).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_dynamic_atr_walkforward_min_fold_n() -> int:
    """Minimum DECIDED signals per fold before the walk-forward gate trusts the
    held-out result. Below this the gate fails open (insufficient_data). 20
    mirrors the platform's calibration min-episode convention."""
    try:
        return int(_load().get(
            "dynamic_atr_walkforward_min_fold_n",
            _DEFAULTS.get("dynamic_atr_walkforward_min_fold_n", 20),
        ))
    except (TypeError, ValueError):
        return 20


def get_dynamic_atr_walkforward_min_oos() -> float:
    """Minimum out-of-sample (held-out fold) net expectancy, in ATR, for a pick
    to pass the walk-forward gate. 0.0 = the held-out half must be
    non-negative net of cost."""
    try:
        return float(_load().get(
            "dynamic_atr_walkforward_min_oos",
            _DEFAULTS.get("dynamic_atr_walkforward_min_oos", 0.0),
        ))
    except (TypeError, ValueError):
        return 0.0


def get_brick_params_source() -> str:
    """``yaml`` | ``store`` — where the sweep reads brick geometry from.

    2026-10-09 (worklog 20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md
    Part B): ``store`` makes the LOCAL param store's ``brick_state`` tier the
    geometry authority and demotes ``config/brick_params.yaml`` to a derived
    cache. ``yaml`` (default) is the historical read. Read per call, so a flip
    needs no restart. Unknown values fail safe to ``yaml``.
    """
    v = str(_load().get(
        "brick_params_source", _DEFAULTS.get("brick_params_source", "yaml"),
    )).lower()
    return v if v in ("yaml", "store") else "yaml"


def get_dynamic_atr_config(
    asset_class: str | None = None, symbol: str | None = None
) -> dict[str, Any]:
    """Dynamic ATR selector settings (``dynamic_atr`` block).

    Precedence: ``by_symbol.<SYMBOL>`` → ``by_class.<asset_class_key>`` → global.
    Per-symbol is the default unit (each symbol's own path stats); forex majors
    share ``by_symbol.FOREX`` — a SINGLE definition (the former duplicate
    ``by_class.forex`` band, which disagreed on the tp grid, was removed
    2026-10-12).

    SINGLE SOURCE (2026-10-12, docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_
    REDESIGN.md §3.1): a caller MUST pass ``symbol`` whenever one is known, so
    the symbol's own band always applies. Passing ``symbol=None`` skips
    ``by_symbol`` and silently resolves against the class/global band — that
    divergence stored out-of-band picks (e.g. BTCUSD tp=13.0 vs its own max
    8.80). A pooled solve widens the EVIDENCE window, never the band.

    2026-10-21 registry-drift correction: the by_class fallback key is
    resolved symbol-aware (``asset_class_key(asset_class, symbol)``), so a
    class override for the TRUE class (index/metal) applies to US500/US30USD
    and XAU*/XAG* even while the registry still carries its catch-all values.
    """
    cfg = dict(_DEFAULTS["dynamic_atr"])
    raw = _load().get("dynamic_atr")
    if isinstance(raw, dict):
        cfg.update({
            k: v for k, v in raw.items()
            if k in cfg and v is not None and k not in ("by_class", "by_symbol")
        })
    by_class, by_symbol = {}, {}
    if isinstance(raw, dict):
        if isinstance(raw.get("by_class"), dict):
            by_class = raw["by_class"]
        if isinstance(raw.get("by_symbol"), dict):
            by_symbol = raw["by_symbol"]
    elif isinstance(cfg.get("by_class"), dict):
        by_class = cfg["by_class"]
    if isinstance(cfg.get("by_symbol"), dict):
        by_symbol = {**cfg["by_symbol"], **by_symbol}
    cfg.pop("by_class", None)
    cfg.pop("by_symbol", None)
    if symbol:
        sym_u = str(symbol).strip().upper()
        ov = by_symbol.get(sym_u)
        # Major FX share one grid (fractional-dollar book).
        if ov is None and sym_u in (
            "AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY",
        ):
            ov = by_symbol.get("FOREX")
        if isinstance(ov, dict):
            cfg.update({k: v for k, v in ov.items() if v is not None})
            return cfg
    if asset_class:
        from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key

        ov = by_class.get(asset_class_key(asset_class, symbol))
        if isinstance(ov, dict):
            cfg.update({k: v for k, v in ov.items() if v is not None})
    return cfg


def get_dynamic_atr_risk_cap() -> float:
    try:
        return float(_load().get("dynamic_atr_risk_cap", 0.35))
    except (TypeError, ValueError):
        return 0.35


def get_atr_anchored_evidence_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — P0 evidence promote into deadline pool."""
    return str(_load().get(
        "atr_anchored_evidence_mode", _DEFAULTS.get("atr_anchored_evidence_mode", "shadow"),
    )).lower()


def get_heavy_publish_geometry_gate() -> str:
    """``off`` | ``shadow`` | ``live`` — block heavy QUALIFIED on neg deadline
    Sharpe or geometry risk over cap when live."""
    return str(_load().get(
        "heavy_publish_geometry_gate", _DEFAULTS.get("heavy_publish_geometry_gate", "live"),
    )).lower()


def get_geometry_intraday_scale_mode() -> str:
    return str(_load().get(
        "geometry_intraday_scale_mode",
        _DEFAULTS.get("geometry_intraday_scale_mode", "shadow"),
    )).lower()


def get_geometry_tuning_sweep_mode() -> str:
    return str(_load().get(
        "geometry_tuning_sweep_mode",
        _DEFAULTS.get("geometry_tuning_sweep_mode", "shadow"),
    )).lower()


def get_geometry_tuning_cooldown_hours() -> float:
    try:
        return float(_load().get("geometry_tuning_cooldown_hours", 6))
    except (TypeError, ValueError):
        return 6.0


def get_geometry_tuning_min_consecutive() -> int:
    try:
        return int(_load().get("geometry_tuning_min_consecutive", 2))
    except (TypeError, ValueError):
        return 2


def get_light_publish_geometry_gate() -> str:
    """``off`` | ``shadow`` | ``live`` — light-path QUALIFIED geometry quality gate."""
    return str(_load().get(
        "light_publish_geometry_gate",
        _DEFAULTS.get("light_publish_geometry_gate", "shadow"),
    )).lower()


def get_min_rr() -> float:
    """Minimum reward:risk ratio — shared by the light-sweep
    qualification gate (EVEngine.min_rr) and the heavy sweep's grid
    search (see this key's own comment in feature_flags.yaml for the
    2026-08-13 incident this single-source-of-truth value prevents)."""
    try:
        return float(_load().get("min_rr", _DEFAULTS["min_rr"]))
    except (TypeError, ValueError):
        return _DEFAULTS["min_rr"]


def _heavy_sweep_grid_number(key: str) -> float:
    try:
        grid_cfg = _load().get("heavy_sweep_grid", _DEFAULTS["heavy_sweep_grid"])
        if not isinstance(grid_cfg, dict):
            grid_cfg = _DEFAULTS["heavy_sweep_grid"]
        return float(grid_cfg.get(key, _DEFAULTS["heavy_sweep_grid"][key]))
    except (TypeError, ValueError):
        return _DEFAULTS["heavy_sweep_grid"][key]


def get_heavy_sweep_max_sharpe() -> float:
    return _heavy_sweep_grid_number("max_sharpe")


def get_heavy_sweep_min_trades() -> int:
    return int(_heavy_sweep_grid_number("min_trades"))


def get_heavy_sweep_atr_anchored_bricks_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Phase A of worklog
    20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md
    — ATR-anchored brick-size candidates added to the heavy-sweep grid,
    gated separately from ``heavy_sweep_reachability_mode``)."""
    return str(_load().get(
        "heavy_sweep_atr_anchored_bricks_mode",
        _DEFAULTS["heavy_sweep_atr_anchored_bricks_mode"],
    )).lower()


def get_heavy_sweep_deadline_aware_scoring_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (root-cause fix, worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md — rescores heavy-sweep
    grid candidates with any trade held longer than EXPIRY_BARS treated as
    0 PnL, matching eod_resolver.py's live deadline, instead of the
    unlimited-patience backtest Sharpe)."""
    return str(_load().get(
        "heavy_sweep_deadline_aware_scoring_mode",
        _DEFAULTS["heavy_sweep_deadline_aware_scoring_mode"],
    )).lower()


def get_settlement_contract_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Batch A, worklog
    20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md — shared
    settlement contract for outcome + PnL). ``live`` (default): resolver
    delegates to settlement_contract. ``off``: historical inline walk.
    ``shadow``: contract computes and logs mismatches; writes still use
    contract outcomes when live path is active — see eod_resolver."""
    return str(_load().get(
        "settlement_contract_mode",
        _DEFAULTS["settlement_contract_mode"],
    )).lower()


def get_settlement_same_bar_policy() -> str:
    """``tp_first`` | ``sl_first`` — same-bar TP/SL priority when both
    levels are touched on one bar. ``tp_first`` preserves historical
    live outcomes; ``sl_first`` matches heavy-sweep OCO default and is
    the conservative target after replay evidence (worklog Phase 1.4)."""
    return str(_load().get(
        "settlement_same_bar_policy",
        _DEFAULTS["settlement_same_bar_policy"],
    )).lower()


def get_p_win_bracket_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Batch C). shadow: compute and
    persist p_win_bracket + would-reject flag; gates still use directional
    p_win. live: add p_win_bracket as a qualification gate."""
    return str(_load().get(
        "p_win_bracket_mode", _DEFAULTS["p_win_bracket_mode"],
    )).lower()


def get_min_p_win_bracket() -> float:
    try:
        return float(_load().get("min_p_win_bracket", _DEFAULTS["min_p_win_bracket"]))
    except (TypeError, ValueError):
        return _DEFAULTS["min_p_win_bracket"]


def get_p_win_bracket_source() -> str:
    """``all_in`` | ``derived`` (2026-10-12, worklog
    20261012_BUCKET_INVERSION_HARMONIZATION.md). all_in (default):
    p_win_bracket = the reachability table's all-in TP share (pct_hit_tp) —
    the honest, harmonized number. derived (legacy): p_resolve ×
    beta_shrunk(tp/(tp+sl)), prior-inflated for thin resolved cells, which
    priced dead 0%-TP narrow buckets at ~0.5 and inverted winnability vs
    bucket width. Unknown falls back to all_in (the honest default)."""
    v = str(_load().get(
        "p_win_bracket_source", _DEFAULTS["p_win_bracket_source"],
    )).lower()
    return v if v in ("all_in", "derived") else "all_in"


# ── Posterior Barrier Model (2026-10-14) ─────────────────────────────────────
# Getters mirror posterior.py's lazy reads so non-sweep callers (tests,
# scripts, routers) can read the same flags without importing the service.
# Phase A ships everything at its no-op default; see _DEFAULTS comments.


def get_pbm_mode() -> str:
    """``off`` | ``shadow`` — stamps + resolver posterior writes."""
    return str(_load().get("pbm_mode", _DEFAULTS["pbm_mode"])).lower()


def get_pbm_ev_mode() -> str:
    """Phase B: ``off`` | ``shadow`` | ``live`` — the EV input swap."""
    return str(_load().get("pbm_ev_mode", _DEFAULTS["pbm_ev_mode"])).lower()


def get_pbm_geometry_mode() -> str:
    """Phase C: ``grid`` | ``posterior`` — continuous solve vs grid walk."""
    return str(_load().get("pbm_geometry_mode", _DEFAULTS["pbm_geometry_mode"])).lower()


def get_pbm_sizing_mode() -> str:
    """Phase D: ``legacy`` | ``conservative`` — bound-scaled Kelly."""
    return str(_load().get("pbm_sizing_mode", _DEFAULTS["pbm_sizing_mode"])).lower()


def get_pbm_reopt_min_new_outcomes() -> int:
    try:
        return max(1, int(_load().get(
            "pbm_reopt_min_new_outcomes",
            _DEFAULTS["pbm_reopt_min_new_outcomes"],
        )))
    except (TypeError, ValueError):
        return _DEFAULTS["pbm_reopt_min_new_outcomes"]


def get_claims_mode() -> str:
    """Claims layer, Phase A: ``off`` | ``shadow`` — ledger writes + scoring.

    ``shadow`` (shipped): register bracket claims at qualification and score
    them inside the EOD resolver through the same settlement contract;
    nothing downstream reads ``nt_signal_claims``. ``off`` = full no-op
    (no registration, no scoring). A ``live`` value behaves as ``shadow``
    until Phase B gives it distinct meaning.
    """
    return str(_load().get("claims_mode", _DEFAULTS["claims_mode"])).lower()


def get_calibration_bias_published_only() -> bool:
    """Scorecard/bias standard = the PUBLISHED book (qualified=true), 2026-10-12
    worklog 20261012_POOLED_ATR_AND_SCORECARD_FILTER.md. When True, bias readers
    query v_calibration_records_published (qualified=true only) — what users
    experience. The full population stays in nt_calibration_records as a
    diagnostic stratifier. Default True."""
    val = _load().get(
        "calibration_bias_published_only",
        _DEFAULTS.get("calibration_bias_published_only", True),
    )
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on", "live")


def get_ev_hard_floor() -> float:
    """Minimum effective EV required to publish (2026-10-11, worklog
    20261011_CALIBRATION_REACHABILITY_REFRAME.md). Default 0.0 = a signal
    must have POSITIVE expected value to ship. When p_win_in_ev_mode=live
    the directional p_win hard floor is 0.0, so without this gate a
    reachability probability of 0.08 passes trivially and an ev=-40 signal
    publishes with kelly=0 — the 'calibrated but large losses' defect. A
    negative-EV trade you expect to lose money on must never publish."""
    try:
        return float(_load().get("ev_hard_floor", _DEFAULTS["ev_hard_floor"]))
    except (TypeError, ValueError):
        return _DEFAULTS["ev_hard_floor"]


def get_p_win_bracket_unscored_policy() -> str:
    """``fail_closed`` | ``fail_open`` (2026-09-24 operator decision).

    What to do when ``p_win_bracket`` is unscored (no bucket / thin
    ``n_stored`` / lookup miss) on the qualification path:

    - ``fail_closed`` (default): unscored cannot be QUALIFIED — live book
      does not publish or size without a scored bracket probability.
      ``nt_signal_sim`` still captures the row
      (``suppress_reason='bracket_unscored'``).
    - ``fail_open``: legacy behaviour — keep the directional signal
      (research / comparison only; do not ship this on the live book).
    """
    return str(_load().get(
        "p_win_bracket_unscored_policy",
        _DEFAULTS["p_win_bracket_unscored_policy"],
    )).lower()


def get_p_win_in_ev_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (R2, 2026-09-22).

    off: EV/Kelly keep directional p_win (status quo).
    shadow: compute bracket EV comparison and log; live EV unchanged.
    live: EV + Kelly use p_win_dir (bracket probability, renamed from
    p_win_bracket). ``p_win_ok`` gate scores p_win_dir against
    ``min_p_win_bracket`` when live. Unscored bracket fails open to
    directional EV.
    """
    return str(_load().get(
        "p_win_in_ev_mode", _DEFAULTS["p_win_in_ev_mode"],
    )).lower()


def get_min_p_win_bracket_for_asset_class(
    asset_class: str | None, symbol: str | None = None
) -> float:
    """Per-asset-class ``min_p_win_bracket`` threshold (2026-10-04).

    Falls back to the global ``min_p_win_bracket`` when the class is not
    found in the per-class map. Used by the ``p_win_ok`` gate when
    ``p_win_in_ev_mode=live``.

    2026-10-21 registry-drift correction: pass ``symbol`` so the TRUE class
    key resolves (US500/US30USD → index 0.40 — unchanged from their legacy
    equity value; XAU*/XAG* → metal 0.20 — was the commodity 0.30 catch-
    all; pin ``min_p_win_bracket_by_asset_class.metal`` in the YAML to keep
    the old 0.30 bar if that is the calibrated intent).
    """
    try:
        from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key
        key = asset_class_key(asset_class, symbol)
    except Exception:
        key = "default"
    overrides = _load().get("min_p_win_bracket_by_asset_class", {})
    if isinstance(overrides, dict) and key in overrides:
        try:
            return float(overrides[key])
        except (TypeError, ValueError):
            pass
    return get_min_p_win_bracket()


# ── Task 40-b corrective gates (2026-10-19 weaknesses evaluation) ─────────
# Four LIVE flag blocks: chop_gate (W-X4), direction_stability (W-X3),
# barrier_floor (W-G2/G4), ev_costs (W-C4). All ship live per the user's
# no-more-shadow mandate; each getter keeps the off|shadow|live vocabulary.


def _block(key: str) -> dict[str, Any]:
    """Merged view of a nested flag block (YAML wins per-key over _DEFAULTS)."""
    cfg = dict(_DEFAULTS.get(key, {}))
    raw = _load().get(key)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if v is not None})
    return cfg


def get_chop_gate_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — W-X4 chop gate on efficiency_24h.

    live: a light-path signal whose trailing-24h Kaufman efficiency ratio is
    below ``min_efficiency_24h`` is suppressed with reason
    ``chop_gate_low_er``. shadow: compute + log only. off: no gate.
    """
    return str(_block("chop_gate").get("mode", "live")).lower()


def get_chop_gate_min_efficiency() -> float:
    """Minimum trailing-24h Kaufman ER required to publish (default 0.30)."""
    try:
        return float(_block("chop_gate").get("min_efficiency_24h", 0.30))
    except (TypeError, ValueError):
        return 0.30


def get_direction_stability_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — W-X3 direction-stability gates.

    live: (a) the OPPOSITE direction cannot publish within
    ``cross_direction_cooldown_h`` hours of the last publish (suppress
    reason ``cross_direction_cooldown``); (b) ``shifts_24h`` at or above
    ``max_shifts_24h`` trips the flip-rate breaker (suppress reason
    ``flip_rate_breaker``).
    """
    return str(_block("direction_stability").get("mode", "live")).lower()


def get_cross_direction_cooldown_h() -> float:
    """Hours the book must keep one direction before flipping (default 6)."""
    try:
        return float(_block("direction_stability").get("cross_direction_cooldown_h", 6))
    except (TypeError, ValueError):
        return 6.0


def get_max_shifts_24h() -> int:
    """Max regime shifts in the trailing 24h before the breaker trips (6)."""
    try:
        return int(_block("direction_stability").get("max_shifts_24h", 6))
    except (TypeError, ValueError):
        return 6


def get_barrier_floor_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — W-G2/G4 barrier-physics floor.

    live: a geometry pick whose analytic P(SL first) exceeds ``pi_sl_cap``
    is adjusted within the symbol's band or rejected (reason
    ``barrier_floor_sl_cap``). Stamps pi_tp/pi_sl/pi_e either way.
    """
    return str(_block("barrier_floor").get("mode", "live")).lower()


def get_barrier_floor_pi_sl_cap() -> float:
    """Cap on analytic P(SL first) for a published bracket (default 0.62)."""
    try:
        return float(_block("barrier_floor").get("pi_sl_cap", 0.62))
    except (TypeError, ValueError):
        return 0.62


def get_barrier_floor_pi_e_cap() -> float:
    """Warn-only cap on P(expired) (default 0.25) — stamp+warn while the
    deadline is globally fixed at 48h; enforcing it would starve the wide
    equity bands (US500 pi_E = 0.70) outright."""
    try:
        return float(_block("barrier_floor").get("pi_e_cap", 0.25))
    except (TypeError, ValueError):
        return 0.25


def get_ev_cost_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — W-C4 cost-aware EV.

    live: compute_ev / apply_bracket_to_ev subtract the per-class round-trip
    cost (cost_atr x current ATR) once per trade. shadow: log the would-be
    cost only. off: cost-blind (historical).
    """
    return str(_block("ev_costs").get("mode", "live")).lower()


def get_ev_cost_atr_for_asset_class(
    asset_class: str | None, symbol: str | None = None
) -> float:
    """Round-trip cost in ATR units for an asset class (default 0.05).

    Lookup normalizes the class key via ``tiered_atr.asset_class_key`` and
    accepts the ``metals`` alias for the canonical ``metal`` key; unknown
    classes fall back to the flat default.

    2026-10-21 registry-drift correction: pass ``symbol`` so the TRUE class
    key resolves — US500/US30USD charge index 0.05 (were overcharged the
    equity 0.06) and XAU*/XAG* charge metal 0.06 (were silently taking the
    0.05 default fall-through, UNDERcharged). Value-only calls keep the
    historical resolution for research replays.
    """
    try:
        from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key
        key = asset_class_key(asset_class, symbol)
    except Exception:
        key = str(asset_class or "default").strip().lower() or "default"
    table = _block("ev_costs").get("cost_atr_by_class", {})
    if isinstance(table, dict):
        for k in (key, {"metals": "metal", "metal": "metals"}.get(key, key)):
            if k in table:
                try:
                    return float(table[k])
                except (TypeError, ValueError):
                    break
    return 0.05


def get_rank_score_calibration_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-10-04).

    off: standard Bayesian shrink (status quo).
    shadow: compute rank-conditioned adjustment and log; live unchanged.
    live: apply rank-conditioned adjustment after standard Bayesian shrink.
    """
    return str(_load().get(
        "rank_score_calibration_mode", _DEFAULTS["rank_score_calibration_mode"],
    )).lower()


def get_rank_score_calibration_slope() -> float:
    """How strongly the rank score modulates the Bayesian shrink (0.0–1.0).

    0.0 = no modulation (standard shrink). 1.0 = full modulation.
    At 1.0: high-rank signals get 50% less shrink, low-rank get 50% more.
    """
    try:
        return float(_load().get(
            "rank_score_calibration_slope", _DEFAULTS["rank_score_calibration_slope"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["rank_score_calibration_slope"]


def get_geometry_band_urgency_config(symbol: str | None = None) -> dict[str, Any]:
    """Geometry band urgency settings (``geometry_band_urgency`` block).

    Replicates the ``get_dynamic_atr_config()`` pattern: global defaults
    + ``by_symbol`` overrides. Per-symbol is the default unit (each
    symbol's own bucket bias); forex majors share ``by_symbol.FOREX``.

    Returns a dict with: enabled, urgency_threshold, min_n,
    cooldown_hours, min_age_hours, lookback_days.
    """
    cfg = dict(_DEFAULTS["geometry_band_urgency"])
    raw = _load().get("geometry_band_urgency", {})
    if isinstance(raw, dict):
        cfg.update({
            k: v for k, v in raw.items()
            if k in cfg and v is not None and k != "by_symbol"
        })
    by_symbol = {}
    if isinstance(raw, dict) and isinstance(raw.get("by_symbol"), dict):
        by_symbol = raw["by_symbol"]
    elif isinstance(cfg.get("by_symbol"), dict):
        by_symbol = cfg["by_symbol"]
    cfg.pop("by_symbol", None)
    if symbol:
        sym_u = str(symbol).strip().upper()
        ov = by_symbol.get(sym_u)
        # Major FX share one config (fractional-dollar book).
        if ov is None and sym_u in (
            "AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY",
        ):
            ov = by_symbol.get("FOREX")
        if isinstance(ov, dict):
            cfg.update({k: v for k, v in ov.items() if v is not None})
    return cfg


def get_geometry_incubation_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-09-23, worklog
    20260923_GEOMETRY_FORWARD_TEST_SCOPE.md) — forward-test/incubation
    gate before a heavy-sweep brick geometry pick is written to
    config/brick_params.yaml.

    off: byte-identical to pre-migration behavior — heavy sweep writes
    straight to YAML, nt_geometry_incubation is never touched.
    shadow: heavy sweep STILL writes straight to YAML (zero live
    behavior change) but every pick is also tracked so promote/reject
    verdicts can be reviewed before anything gates real writes.
    live: a new candidate is held as status=incubating (previous live
    combo keeps trading) until reconcile_incubations() promotes it.
    """
    return str(_load().get(
        "geometry_incubation_mode", _DEFAULTS["geometry_incubation_mode"],
    )).lower()


def get_geometry_incubation_min_days() -> float:
    try:
        return float(_load().get(
            "geometry_incubation_min_days",
            _DEFAULTS["geometry_incubation_min_days"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["geometry_incubation_min_days"])


def get_signal_sim_retention_resolved_days() -> int:
    """Days to keep RESOLVED nt_signal_sim rows before the nightly retention
    pass deletes them (eod_retention.run_eod_retention). 120 since
    2026-09-23 (was 30 via settings.EOD_RETENTION_RESOLVED_DAYS, removed):
    edge-state research needs ≥ 60 days of regime + geometry + MFE/MAE,
    which only nt_signal_sim carries. Floored at 7 so a bad value can't
    wipe the ledger the resolver/correction passes still read."""
    try:
        v = int(_load().get(
            "signal_sim_retention_resolved_days",
            _DEFAULTS["signal_sim_retention_resolved_days"],
        ))
    except (TypeError, ValueError):
        v = int(_DEFAULTS["signal_sim_retention_resolved_days"])
    return max(v, 7)


def get_kairos_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-09-24, docs/scopes/
    20260924_KAIROS_V3_SCOPE.md P0). shadow (default): stamp the Kairos
    capture columns (rel_volume_1h, clv_1h, efficiency_24h, atr_ratio_run,
    spread_bps) on nt_signal_sim and refresh the daily calendar cache — no
    effect on qualification, sizing, SL/TP or delivery. off: stamp nothing.
    live: reserved (P3 knobs); behaves as shadow until a live scope exists.
    Unknown values fall back to shadow."""
    v = str(_load().get("kairos_mode", _DEFAULTS["kairos_mode"])).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_clef_flash_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-10-04, docs/scopes/
    20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md +
    20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md). shadow (default):
    call the Cloudflare Clef-Flash ``/decision`` endpoint for qualifiable
    light-sweep signals and stamp p_clef_dir/p_clef_wait/p_clef_symbol on
    nt_signal_sim. RESEARCH ONLY — never an EV source, never a gate.
    live: reserved for the classifier cutover at kairos_pivot.py /
    macro_forecast_agent.py (does not change the shadow stamp). off: no
    HTTP call, no stamp. Unknown values fall back to shadow."""
    v = str(_load().get("clef_flash_mode", _DEFAULTS["clef_flash_mode"])).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_p_win_reachability_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-10-11, worklog
    20261011_REACHABILITY_PROB_BUILD.md). off (default): no reachability
    stamp. shadow: compute the reachability-conditioned probability P_reach
    + the blend-vs-reach delta and stamp p_reach / p_reach_delta on
    nt_signal_sim — MEASUREMENT ONLY (EV/Kelly unchanged). live: P_reach
    replaces the direction-only p_win in EV/Kelly (gated on walk-forward).
    Unknown values fall back to off (fail-safe: never stamp unless asked)."""
    v = str(_load().get("p_win_reachability_mode", _DEFAULTS["p_win_reachability_mode"])).lower()
    return v if v in ("off", "shadow", "live") else "off"


def get_geometry_incubation_min_resolved() -> int:
    try:
        return int(_load().get(
            "geometry_incubation_min_resolved",
            _DEFAULTS["geometry_incubation_min_resolved"],
        ))
    except (TypeError, ValueError):
        return int(_DEFAULTS["geometry_incubation_min_resolved"])


def get_geometry_incubation_max_expiry_regression() -> float:
    try:
        return float(_load().get(
            "geometry_incubation_max_expiry_regression",
            _DEFAULTS["geometry_incubation_max_expiry_regression"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["geometry_incubation_max_expiry_regression"])


def get_geometry_incubation_adaptive_days_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-10-09, worklog
    20261009_ATR_GEOMETRY_SINGLE_SOURCE.md Part B) — adaptive, evidence-relative
    time floor for the incubation gate.

    off: the historical static ``geometry_incubation_min_days`` (7).
    shadow: compute the adaptive per-symbol floor and LOG it, but still gate
    on the static floor (zero live behaviour change).
    live: gate on the adaptive floor derived from the symbol's fresh
    ``nt_signal_sim`` resolution rate, clamped to
    ``[adaptive_floor_days, adaptive_cap_days]``.

    Unknown values fall back to shadow. A cold/None rate fails open to
    ``adaptive_cap_days`` (== the old floor), so behaviour is never looser
    than the pre-change gate when there is no evidence.
    """
    v = str(_load().get(
        "geometry_incubation_adaptive_days_mode",
        _DEFAULTS["geometry_incubation_adaptive_days_mode"],
    )).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_geometry_incubation_adaptive_floor_days() -> float:
    try:
        return float(_load().get(
            "geometry_incubation_adaptive_floor_days",
            _DEFAULTS["geometry_incubation_adaptive_floor_days"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["geometry_incubation_adaptive_floor_days"])


def get_geometry_incubation_adaptive_cap_days() -> float:
    try:
        return float(_load().get(
            "geometry_incubation_adaptive_cap_days",
            _DEFAULTS["geometry_incubation_adaptive_cap_days"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["geometry_incubation_adaptive_cap_days"])


def get_settlement_expiry_mode() -> str:
    """``fixed`` | ``shadow`` | ``live`` (2026-10-09, worklog
    20261009_P90_HOURS_LIVE_MODE.md) — per-signal dynamic settlement expiry.

    fixed: the historical hard-coded ``EXPIRY_BARS = 48`` (byte-identical).
    shadow: compute the dynamic per-bracket expiry and LOG the fixed-vs-
    dynamic delta, but still settle on EXPIRY_BARS (zero live change).
    live: settle each signal on a per-bracket expiry derived from
    ``nt_bracket_reachability`` (median*safety for tight buckets, p90 for
    the wide 4+ bucket), clamped to ``[floor_hours, cap_hours]``; a cold/
    missing row fails open to EXPIRY_BARS.

    Unknown values fall back to fixed.
    """
    v = str(_load().get(
        "settlement_expiry_mode", _DEFAULTS["settlement_expiry_mode"],
    )).lower()
    return v if v in ("fixed", "shadow", "live") else "fixed"


def get_settlement_expiry_floor_hours() -> float:
    try:
        return float(_load().get(
            "settlement_expiry_floor_hours",
            _DEFAULTS["settlement_expiry_floor_hours"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["settlement_expiry_floor_hours"])


def get_settlement_expiry_cap_hours() -> float:
    try:
        return float(_load().get(
            "settlement_expiry_cap_hours",
            _DEFAULTS["settlement_expiry_cap_hours"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["settlement_expiry_cap_hours"])


def get_settlement_expiry_tight_safety_mult() -> float:
    try:
        return float(_load().get(
            "settlement_expiry_tight_safety_mult",
            _DEFAULTS["settlement_expiry_tight_safety_mult"],
        ))
    except (TypeError, ValueError):
        return float(_DEFAULTS["settlement_expiry_tight_safety_mult"])


def get_regime_warm_start_mode() -> str:
    """``off`` | ``live`` (root-cause fix for regime_label flapping —
    warm-starts RegimeHMM's EM fit from the previous tick's own
    converged params instead of a fresh auto-init every tick)."""
    return str(_load().get(
        "regime_warm_start_mode", _DEFAULTS["regime_warm_start_mode"],
    )).lower()


def get_regime_label_debounce_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Fix 2 — hysteresis on
    RegimeSnapshot.regime_label via RegimeLabelDebouncer)."""
    return str(_load().get(
        "regime_label_debounce_mode", _DEFAULTS["regime_label_debounce_mode"],
    )).lower()


def get_regime_label_debounce_ticks() -> int:
    """Consecutive ticks of agreement required before RegimeLabelDebouncer
    promotes a new label."""
    try:
        return int(_load().get(
            "regime_label_debounce_ticks", _DEFAULTS["regime_label_debounce_ticks"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["regime_label_debounce_ticks"]


def get_regime_state_persist_mode() -> str:
    """``off`` | ``live`` (2026-09-25): persist restart-sensitive regime
    state (debouncers, run tracker, label means, EMAs) across restarts.
    Unknown values fall back to live."""
    v = str(_load().get("regime_state_persist_mode", _DEFAULTS["regime_state_persist_mode"])).lower()
    return v if v in ("off", "live") else "live"


def get_regime_state_persist_max_age_hours() -> float:
    """Snapshots older than this are ignored on restore (cold start)."""
    try:
        return float(_load().get(
            "regime_state_persist_max_age_hours", _DEFAULTS["regime_state_persist_max_age_hours"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["regime_state_persist_max_age_hours"]


def get_light_sweep_catchup_mode() -> str:
    """``off`` | ``live`` (2026-09-25): on start, re-run the most recent
    light-sweep slot a restart dropped. Unknown values fall back to live."""
    v = str(_load().get("light_sweep_catchup_mode", _DEFAULTS["light_sweep_catchup_mode"])).lower()
    return v if v in ("off", "live") else "live"


def get_calibration_resolve_hourly_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-09-25, worklog/kairos/
    20260926_HOURLY_CALIBRATION_RESOLVE.md). Hourly TP/SL resolution into
    nt_signal_sim + nt_calibration_records so calibration evidence arrives
    within ~1h of the outcome instead of at the next 01:17 UTC nightly run.
    shadow: read-only dry run that logs what would resolve now and the
    evidence hours it would save. Unknown values fall back to shadow."""
    v = str(_load().get(
        "calibration_resolve_hourly_mode", _DEFAULTS["calibration_resolve_hourly_mode"],
    )).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_calibration_evidence_pool_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (2026-09-26, Kairos V3 §14.3,
    worklog/kairos/20260926_CALIBRATION_EVIDENCE_POOL.md). Which signals'
    outcomes count as calibration evidence for eod_correction's bias.
    shadow: the all-tier bias still drives enforcement; the tier-filtered
    bias (``calibration_evidence_tiers``) is computed and logged beside it.
    live: the tier-filtered bias drives enforcement (all-tier fallback when
    the pool bias is INSUFFICIENT). Unknown values fall back to shadow."""
    v = str(_load().get(
        "calibration_evidence_pool_mode", _DEFAULTS["calibration_evidence_pool_mode"],
    )).lower()
    return v if v in ("off", "shadow", "live") else "shadow"


def get_calibration_evidence_tiers() -> tuple[str, ...]:
    """Tiers counted by the evidence pool (migration 171 ``evidence_tier``).
    Default ``("qualified", "suppressed")``. Unknown names are dropped; an
    empty or invalid list falls back to the default rather than silently
    meaning 'every tier'."""
    allowed = ("qualified", "suppressed", "gate_fail", "unknown")
    raw = _load().get("calibration_evidence_tiers", _DEFAULTS["calibration_evidence_tiers"])
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",")]
    try:
        clean = tuple(dict.fromkeys(str(t).lower() for t in raw if str(t).lower() in allowed))
    except TypeError:
        clean = ()
    return clean or tuple(_DEFAULTS["calibration_evidence_tiers"])


def get_eod_resolver_scan_mode() -> str:
    """``full`` | ``head`` (2026-09-26, worklog/kairos/
    20260926_RESOLVER_HEAD_OF_LINE_FIX.md). How ``resolve_pending_signals``
    walks the oldest-first pending queue. ``full`` pages past rows that stay
    pending and scans to the end of the queue, so an already-decided TP/SL
    behind a head of still-young signals is written on this run. ``head`` is
    the pre-fix behaviour (stop at the first batch that resolves nothing) —
    rollback only. Unknown values fall back to ``full``."""
    v = str(_load().get("eod_resolver_scan_mode", _DEFAULTS["eod_resolver_scan_mode"])).lower()
    return v if v in ("full", "head") else "full"


def get_eod_resolver_page_pause_s() -> float:
    """Seconds to wait between resolver batches (each batch is one batched
    TradingView candle fetch). Default 2.0, clamped to [0, 30]."""
    try:
        v = float(_load().get("eod_resolver_page_pause_s", _DEFAULTS["eod_resolver_page_pause_s"]))
    except (TypeError, ValueError):
        v = float(_DEFAULTS["eod_resolver_page_pause_s"])
    return min(max(v, 0.0), 30.0)


def _qualification_gate_number(key: str) -> float:
    try:
        gates_cfg = _load().get("qualification_gates", _DEFAULTS["qualification_gates"])
        if not isinstance(gates_cfg, dict):
            gates_cfg = _DEFAULTS["qualification_gates"]
        return float(gates_cfg.get(key, _DEFAULTS["qualification_gates"][key]))
    except (TypeError, ValueError):
        return _DEFAULTS["qualification_gates"][key]


def get_min_regime_conf() -> float:
    """Gate 2 — minimum HMM regime confidence to qualify a signal.
    2026-09-08 config-centralization fix (was os.getenv("MIN_REGIME_CONF"))."""
    return _qualification_gate_number("min_regime_conf")


def get_min_kelly() -> float:
    """Gate 3 — minimum Kelly fraction to qualify a signal.
    2026-09-08 config-centralization fix (was os.getenv("MIN_KELLY"))."""
    return _qualification_gate_number("min_kelly")


def get_min_timesfm_confidence() -> float:
    """Gate 5 — minimum ``|p_timesfm - 0.5|`` for TimesFM's hard-gate mode.
    2026-09-08 config-centralization fix (was os.getenv("MIN_TIMESFM_CONFIDENCE"))."""
    return _qualification_gate_number("min_timesfm_confidence")


def get_min_ev() -> float:
    """Gate 6a — minimum expected value to qualify a signal.
    2026-09-08 config-centralization fix (was os.getenv("MIN_EV"))."""
    return _qualification_gate_number("min_ev")


def get_min_p_win() -> float:
    """Gate 6b — minimum blended p_win to qualify a signal (full weight;
    a soft band below this down to 0.50 still qualifies with a
    proportional Kelly shrink — see orchestrator.py's usage). 2026-09-08
    config-centralization fix (was os.getenv("MIN_P_WIN"))."""
    return _qualification_gate_number("min_p_win")


def get_timesfm_gate_mode() -> str:
    """``hard`` | ``soft`` | ``off`` — how TimesFM confidence affects
    qualification (see orchestrator.py's inline comment at this value's
    usage for full semantics). 2026-09-08 config-centralization fix (was
    os.getenv("TIMESFM_GATE_MODE"))."""
    return str(_load().get(
        "timesfm_gate_mode", _DEFAULTS["timesfm_gate_mode"],
    )).lower()


def get_timesfm_map_mode() -> str:
    """``sigmoid`` | ``zmap_shadow`` | ``zmap_live`` (2026-09-17).

    z-map standardizes TimesFM rel_change by per-symbol rolling σ before
    Φ(). zmap_shadow logs both but feeds sigmoid to EV; zmap_live feeds
    z-map. See worklog/20260917_TIMESFM_ZMAP_FIX.md.
    """
    return str(_load().get(
        "timesfm_map_mode", _DEFAULTS["timesfm_map_mode"],
    )).lower()


def _timesfm_map_number(key: str) -> float:
    try:
        cfg = _load().get("timesfm", _DEFAULTS["timesfm"])
        if not isinstance(cfg, dict):
            cfg = _DEFAULTS["timesfm"]
        return float(cfg.get(key, _DEFAULTS["timesfm"][key]))
    except (TypeError, ValueError):
        return _DEFAULTS["timesfm"][key]


def get_timesfm_sigmoid_k() -> float:
    """TimesFM price→probability sigmoid k (feature_flags.yaml timesfm block)."""
    return _timesfm_map_number("sigmoid_k")


def get_timesfm_clamp_min() -> float:
    return _timesfm_map_number("clamp_min")


def get_timesfm_clamp_max() -> float:
    return _timesfm_map_number("clamp_max")


def get_sweep_after_hours() -> bool:
    """When true, sweep all symbols regardless of market hours (crypto/
    forex are always swept regardless of this flag — only affects stocks/
    commodities). 2026-09-08 config-centralization fix (was
    os.getenv("SWEEP_AFTER_HOURS") == "1")."""
    val = _load().get("sweep_after_hours", _DEFAULTS["sweep_after_hours"])
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def get_price_staleness_max_hours() -> float:
    """Maximum hours since last price change before a sweep is skipped.

    0 disables the check (always sweep). Default 4h — if the latest
    price hasn't changed in 4 hours, the market is likely closed
    (weekend/holiday) and sweeping would generate signals on stale data.
    """
    try:
        return float(_load().get(
            "price_staleness_max_hours", _DEFAULTS["price_staleness_max_hours"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["price_staleness_max_hours"]


def get_signal_cooldown_minutes() -> float:
    """Minutes to suppress a repeat qualified signal for the same
    (symbol, direction) pair. 2026-09-08 config-centralization fix (was
    os.getenv("SIGNAL_COOLDOWN_MINUTES"))."""
    try:
        return float(_load().get(
            "signal_cooldown_minutes", _DEFAULTS["signal_cooldown_minutes"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["signal_cooldown_minutes"]


def get_expiry_rate_alert_config() -> dict[str, Any]:
    """Priority #3, worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md:
    threshold config for the expiry-rate Discord alert
    (``sweep_orchestrator/expiry_rate_alert.py``). ``enabled`` (bool),
    ``threshold_pct`` (float — 7-day ``expired_pct`` at/above this fires),
    ``min_resolved`` (int — skip symbols with too few resolved trades in
    the window to be meaningful), ``cooldown_hours`` (float — minimum gap
    between repeat alerts for the same symbol while the condition
    persists). Missing/malformed keys fall back individually to
    ``_DEFAULTS["expiry_rate_alert"]`` rather than discarding the whole
    dict, so a partial override in the YAML doesn't silently reset
    everything else to defaults."""
    defaults = _DEFAULTS["expiry_rate_alert"]
    cfg = _load().get("expiry_rate_alert", {})
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        enabled = bool(cfg.get("enabled", defaults["enabled"]))
    except (TypeError, ValueError):
        enabled = defaults["enabled"]
    try:
        threshold_pct = float(cfg.get("threshold_pct", defaults["threshold_pct"]))
    except (TypeError, ValueError):
        threshold_pct = defaults["threshold_pct"]
    try:
        min_resolved = int(cfg.get("min_resolved", defaults["min_resolved"]))
    except (TypeError, ValueError):
        min_resolved = defaults["min_resolved"]
    try:
        cooldown_hours = float(cfg.get("cooldown_hours", defaults["cooldown_hours"]))
    except (TypeError, ValueError):
        cooldown_hours = defaults["cooldown_hours"]
    return {
        "enabled": enabled,
        "threshold_pct": threshold_pct,
        "min_resolved": min_resolved,
        "cooldown_hours": cooldown_hours,
    }


def get_calibration_bias_min_episodes() -> int:
    """Minimum number of independent EPISODES (not raw rows) required
    before ``fetch_calibration_bias``/``fetch_calibration_bias_raw``
    (supabase_client.py) will return a bias verdict at all — worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md's counting fix.

    Replaces the 2026-08-25 ``_clustering_discounted_n``/distinct-calendar-
    days discount, which USDCHF's incident showed can be evaded by a burst
    that happens to straddle exactly ``EOD_BIAS_MIN_DISTINCT_DAYS``
    calendar dates while still being one continuous overlapping episode
    (294 raw records, 1 real episode, 3 calendar dates). Episodes are
    counted by merging records whose ``[entered_at, closed_at]`` windows
    overlap — see ``_group_into_episodes``.

    Default 2 is a deliberately conservative starting point, not a
    settled number: real post-deploy data showed most symbols sit at only
    1-7 independent episodes per rolling 7-day window, so this threshold
    directly controls how often the enforcement mechanism can act at all.
    Needs monitoring against real data post-deploy (see nt_roadmap) —
    too high and the mechanism goes permanently dormant, too low and it's
    back to counting noise as evidence."""
    try:
        return int(_load().get(
            "calibration_bias_min_episodes",
            _DEFAULTS["calibration_bias_min_episodes"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["calibration_bias_min_episodes"]


def get_signal_stream_meta_model_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — see this key's long comment in
    feature_flags.yaml for the full rationale. Ships at ``off``;
    ``live`` has no consumer yet (2026-09-09,
    worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md)."""
    return str(_load().get(
        "signal_stream_meta_model_mode", _DEFAULTS["signal_stream_meta_model_mode"],
    )).lower()


def get_knowledge_index_enabled() -> bool:
    """Whether the nightly local knowledge-index rebuild job runs
    (2026-09-10, Phase 1 of worklog
    20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §3). Purely
    additive/local — read-only w.r.t. trading, indexes the codebase +
    worklogs + nt_roadmap notes into a local SQLite file for future
    root-cause retrieval. Default on; a simple on/off switch rather than
    the off/shadow/live convention used elsewhere, since there's no
    "correctness comparison against a live baseline" for a search index
    — it either helps future investigation or it's off."""
    value = _load().get(
        "knowledge_index_enabled", _DEFAULTS["knowledge_index_enabled"],
    )
    return bool(value)


def get_self_learning_detectors_enabled() -> bool:
    """Whether the nightly Phase 2 self-learning detector job runs
    (2026-09-12, self_learning_phase_2_detectors, RM-37). Purely
    diagnostic — runs regime_price_mismatch/dead_mechanism/
    redundant_computation against real data and writes findings to
    nt_self_learning_findings for human review; nothing reads that
    table to make a live decision. Same simple on/off convention as
    knowledge_index_enabled, for the same reason (no live-baseline
    comparison for a diagnostic sweep)."""
    value = _load().get(
        "self_learning_detectors_enabled", _DEFAULTS["self_learning_detectors_enabled"],
    )
    return bool(value)


def get_self_learning_triage_enabled() -> bool:
    """Whether the nightly Phase 3 MVP LLM-triage job runs (2026-09-11,
    self_learning_phase_3_4_forecasting_rescope, RM-50). Reads
    untriaged rows from nt_self_learning_findings, asks an LLM for a
    short narrative and an optional bounded Tier D parameter-change
    proposal, writes both to nt_self_learning_reports for human
    review. No write access to any live parameter — same simple
    on/off convention as self_learning_detectors_enabled, for the same
    reason (purely diagnostic, no live-baseline comparison needed)."""
    value = _load().get(
        "self_learning_triage_enabled", _DEFAULTS["self_learning_triage_enabled"],
    )
    return bool(value)


def get_macro_forecast_agent_enabled() -> bool:
    """Whether the nightly long-term (12-month) macro-forecast agent
    job runs (2026-09-11, self_learning_phase_3_4_forecasting_rescope,
    RM-50 follow-up). Classifies each of the 12 commodities in
    docs/LLM/commodity_12m_forecast_model_2027.md into structured
    trend/direction/strength fields matching this codebase's existing
    label vocabularies, writes to nt_macro_forecast. Shadow-mode
    diagnostic only — nothing reads that table to make a live decision
    yet (blending it into macro_filter.py is a separate, not-yet-built
    step). Same simple on/off convention as
    self_learning_detectors_enabled, for the same reason."""
    value = _load().get(
        "macro_forecast_agent_enabled", _DEFAULTS["macro_forecast_agent_enabled"],
    )
    return bool(value)


def get_atr_reopt_enabled() -> bool:
    """Monthly ATR free-lattice re-opt (proposal-only). See worklog
    20260930_MONTHLY_ATR_REOPT.md. Same simple on/off as
    macro_forecast_agent_enabled — never auto-writes YAML."""
    value = _load().get("atr_reopt_enabled", _DEFAULTS.get("atr_reopt_enabled", True))
    return bool(value)


def get_atr_reopt_urgency_config() -> dict[str, Any]:
    """Vol-spike Discord nudge to run monthly_atr_reopt early.

    ``elevated_ratio`` is ``rv24/rv168`` at entry (1.25 = +25% short-term
    vol). Cooldown-gated per symbol. See worklog 20260930_MONTHLY_ATR_REOPT.md.
    """
    defaults = dict(_DEFAULTS["atr_reopt_urgency"])
    cfg = _load().get("atr_reopt_urgency", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def get_dynamic_deadline_mode() -> str:
    """Dynamic deadline tier mode: off | shadow | live (default shadow)."""
    return _load().get(
        "dynamic_deadline_mode", _DEFAULTS.get("dynamic_deadline_mode", "shadow"),
    )


def get_dynamic_deadline_config() -> dict[str, Any]:
    """Dynamic deadline tier configuration.

    Returns the ``dynamic_deadline`` block with per-symbol overrides,
    percentile derivation settings, and confidence thresholds.
    """
    defaults = dict(_DEFAULTS["dynamic_deadline"])
    cfg = _load().get("dynamic_deadline", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def get_dynamic_deadline_config_for_symbol(symbol: str) -> dict[str, Any]:
    """Per-symbol dynamic deadline config (by_symbol override or defaults)."""
    cfg = get_dynamic_deadline_config()
    by_symbol = cfg.get("by_symbol", {})
    sym_cfg = by_symbol.get(symbol, {})
    return {
        "soft_deadline_hours": sym_cfg.get("soft_deadline_hours", 24.0),
        "hard_deadline_hours": sym_cfg.get("hard_deadline_hours", 48.0),
        "derive_from_reachability": cfg.get("derive_from_reachability", True),
        "min_soft_deadline_hours": cfg.get("min_soft_deadline_hours", 12.0),
        "max_hard_deadline_hours": cfg.get("max_hard_deadline_hours", 120.0),
    }


def get_dynamic_deadline_reopt_config() -> dict[str, Any]:
    """Dynamic deadline reopt urgency configuration.

    4h cycle: re-derive deadline tiers from fresh reachability data.
    Same pattern as atr_reopt_urgency.
    """
    defaults = dict(_DEFAULTS["dynamic_deadline_reopt"])
    cfg = _load().get("dynamic_deadline_reopt", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def get_regime_edge_mode() -> str:
    """Regime edge mode: off | shadow | live (default shadow)."""
    return str(_load().get(
        "regime_edge_mode", _DEFAULTS.get("regime_edge_mode", "shadow"),
    )).lower()


def get_regime_edge_config() -> dict[str, Any]:
    """Regime edge configuration (min_n, min_win_rate, rebuild interval)."""
    defaults = dict(_DEFAULTS.get("regime_edge", {}))
    cfg = _load().get("regime_edge", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def get_edge_lab_run_mode() -> str:
    """Edge-lab proof-run cadence: off | shadow | live (default shadow).

    NOT ``regime_edge_mode``.  That decides the qualification gate; this decides
    whether the daily audit run executes.  ``shadow`` writes the artifact to the
    worklog only; ``live`` also publishes it to ``nt_edge_lab_run``.
    """
    return str(_load().get(
        "edge_lab_run_mode", _DEFAULTS.get("edge_lab_run_mode", "shadow"),
    )).lower()


def get_edge_lab_run_config() -> dict[str, Any]:
    """Edge-lab run config (data_source, hours)."""
    defaults = dict(_DEFAULTS.get("edge_lab_run", {}))
    cfg = _load().get("edge_lab_run", {})
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in defaults and v is not None:
                defaults[k] = v
    return defaults


def get_renko_brick_retention_days() -> int:
    """Days of ``nt_renko_bricks`` ``session_date`` history to keep in the
    nightly retention pass (``renko_retention.run_renko_brick_retention``,
    Part VI of orchestrator.py's eod-lookback cycle). 2026-09-09, worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 2".
    Default 2 reuses migration 111's own precedent for this table ("Keep at
    most 2 sessions (today + yesterday)"), not a new number."""
    try:
        return int(_load().get(
            "renko_brick_retention_days", _DEFAULTS["renko_brick_retention_days"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["renko_brick_retention_days"]


def get_sweep_result_retention_days() -> int:
    """Days of ``nt_sweep_result`` ``sweep_timestamp`` history to keep in
    the nightly retention pass (``sweep_result_retention
    .run_sweep_result_retention``, Part VII of orchestrator.py's
    eod-lookback cycle). 2026-10-01, worklog
    20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§11: nt_sweep_result
    had NO retention pass and reached 103.41 MB / 17.09% of the database.
    A full-repo audit (§10) confirmed no live consumer (app code or
    research script) reads more than ~7 days back; default 30 gives
    headroom above every confirmed need."""
    try:
        return int(_load().get(
            "sweep_result_retention_days", _DEFAULTS["sweep_result_retention_days"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["sweep_result_retention_days"]


def get_regime_log_retention_days() -> int:
    """Days of ``nt_regime_log`` ``detected_at`` history to keep in the
    nightly retention pass (same module/part as
    ``get_sweep_result_retention_days`` — one pass prunes both tables).
    Same worklog/audit as above; nt_regime_log was 79.47 MB / 13.13% of
    the database with no retention pass."""
    try:
        return int(_load().get(
            "regime_log_retention_days", _DEFAULTS["regime_log_retention_days"],
        ))
    except (TypeError, ValueError):
        return _DEFAULTS["regime_log_retention_days"]


# ═══════════════════════════════════════════════════════════════════════════
# Strategy Registry (R0, 2026-10-10)
# ───────────────────────────────────────────────────────────────────────────
# docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §4, R0.
#
# A SEPARATE YAML (config/strategy_registry.yaml) with its own mtime-hot-reload
# loader, mirroring _load()/feature_flags.yaml. Kept separate because it is a
# catalog of structured archetype blocks (nested dicts), not a bag of scalar
# mode flags — mixing it into feature_flags.yaml would bloat that file and
# blur the two concerns (mode flags vs strategy definitions).
#
# R0 = the file + this loader exist; NOTHING consumes them yet. The first
# consumer is strategy_router.py (R1), which reads get_strategy_registry() and
# get_router_policy() — it does NOT hardcode these values. Until R1 ships,
# editing the YAML has zero live effect (a config nobody reads is inert, the
# same fail-safe posture as every other mode here).
# ═══════════════════════════════════════════════════════════════════════════

_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "strategy_registry.yaml"
_registry_cache: dict[str, Any] | None = None
_registry_mtime: float | None = None

# Fail-open defaults if the YAML is missing/corrupt: an EMPTY registry. An
# empty catalog means the router (R1) finds no matching archetype and applies
# router.default_on_no_match — i.e. it cannot invent a strategy from nothing.
# This is the safe direction: a broken registry degrades to "no routing", never
# to a wrong routing.
_REGISTRY_DEFAULTS: dict[str, Any] = {"strategy_registry": {}, "router": {
    "mode": "shadow",
    "default_on_no_match": "stand_by",
    "precedence": [],
}}


def _load_registry() -> dict[str, Any]:
    """Hot-reload config/strategy_registry.yaml (same mtime pattern as _load).

    Re-parses only when the file's mtime changes; safe to call from a per-tick
    path (one stat() per call, YAML parsed on change only). On any read/parse
    failure returns the previous cache, or _REGISTRY_DEFAULTS on cold start —
    never raises, so a corrupt registry cannot take down the sweep.
    """
    global _registry_cache, _registry_mtime
    try:
        mtime = _REGISTRY_PATH.stat().st_mtime
    except OSError:
        if _registry_cache is None:
            logger.warning(
                "strategy_registry: %s not found — using empty defaults (no routing)",
                _REGISTRY_PATH,
            )
            _registry_cache = dict(_REGISTRY_DEFAULTS)
        return _registry_cache

    if _registry_cache is None or mtime != _registry_mtime:
        try:
            import yaml
            with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            # Merge over defaults so a partial file still has a valid router
            # policy; the strategy_registry block replaces wholesale (a catalog
            # is not deep-merged — a removed archetype must actually be gone).
            merged = dict(_REGISTRY_DEFAULTS)
            if isinstance(loaded, dict):
                raw_reg = loaded.get("strategy_registry") or {}
                # Normalize every archetype block so R1's router can read the
                # predicate fields unconditionally: requires_opposes and
                # requires_aligned default to False when a block omits them
                # (an archetype declares only the one that applies to it).
                # Without this, the router would need a get() default per
                # field and a typo'd/missing key would silently mean "no
                # constraint" instead of failing loudly.
                norm_reg: dict[str, Any] = {}
                if isinstance(raw_reg, dict):
                    for aname, ablk in raw_reg.items():
                        if not isinstance(ablk, dict):
                            norm_reg[aname] = ablk
                            continue
                        b = dict(ablk)
                        b.setdefault("requires_opposes", False)
                        b.setdefault("requires_aligned", False)
                        b.setdefault("enabled", False)
                        norm_reg[aname] = b
                merged["strategy_registry"] = norm_reg
                router = loaded.get("router")
                if isinstance(router, dict):
                    merged["router"] = {**merged["router"], **router}
            _registry_cache = merged
            _registry_mtime = mtime
            logger.info("strategy_registry: loaded %s", _REGISTRY_PATH)
        except Exception as exc:
            logger.error(
                "strategy_registry: failed to load %s (%s: %s) — keeping previous values",
                _REGISTRY_PATH, type(exc).__name__, exc,
            )
            if _registry_cache is None:
                _registry_cache = dict(_REGISTRY_DEFAULTS)
    return _registry_cache


def get_strategy_registry() -> dict[str, Any]:
    """The archetype catalog: ``{archetype_name: archetype_block}``.

    Each block has the predicate fields (asset_classes, symbols, directions,
    requires_opposes, requires_aligned, regime_vol, enabled) and trade-shape
    fields (entry/exit/risk) declared in config/strategy_registry.yaml. R1's
    router iterates this to resolve a signal's archetype. Empty dict if the
    YAML is missing (fail-open to "no routing").
    """
    return dict(_load_registry().get("strategy_registry") or {})


def get_strategy_archetype(name: str) -> dict[str, Any] | None:
    """One archetype block by name, or None. Convenience for the router and
    for tests that pin a single archetype's shape."""
    reg = _load_registry().get("strategy_registry") or {}
    blk = reg.get(str(name))
    return dict(blk) if isinstance(blk, dict) else None


def get_router_policy() -> dict[str, Any]:
    """The router policy block: mode (off|shadow|live), default_on_no_match,
    precedence. Read by R1's strategy_router.py. Ships at mode=shadow.
    """
    return dict(_load_registry().get("router") or _REGISTRY_DEFAULTS["router"])


def get_router_mode() -> str:
    """``off`` | ``shadow`` | ``live``. shadow (R1 default): the router
    computes + stamps the archetype but applies no live effect. live (R6):
    the selected archetype's entry/exit/risk drives geometry/deadline/
    calibration, one archetype at a time behind its own flag."""
    mode = str(_load_registry().get("router", {}).get("mode", "shadow")).lower()
    return mode if mode in ("off", "shadow", "live") else "shadow"


# ── EOD recalibration knobs (2026-10-20) ─────────────────────────────────────
# Migrated out of settings.py, where they were env-overridable Settings fields
# — a leftover Studio Secret with a matching name silently overrode the code
# default (the MIN_RR shape). Env is no longer consulted; these read the YAML
# block and fall back to _DEFAULTS, so a missing/unreadable config file cannot
# change behaviour. See config/feature_flags.yaml's own comment for rationale.


def get_eod_correction_intraday_enabled() -> bool:
    """Master toggle for the 04:30/08:30/12:30/16:30/20:30 UTC correction-only
    passes. off = nightly-only (the pre-2026-08-13 behaviour)."""
    val = _block("eod_correction").get("intraday_enabled", True)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def get_eod_correction_interval_hours() -> int:
    """Cadence (hours) of the intraday correction passes. Floored at 1 so a
    bad YAML value cannot produce a zero/negative range in the job loop."""
    try:
        return max(1, int(_block("eod_correction").get("interval_hours", 4)))
    except (TypeError, ValueError):
        return 4


def get_eod_bias_window_days() -> int:
    """Rolling bias window, in days (``fetch_calibration_bias``/``_raw``)."""
    try:
        return int(_block("eod_bias").get("window_days", 7))
    except (TypeError, ValueError):
        return 7


def get_eod_bias_min_records() -> int:
    """Minimum raw records before a bias verdict is returned at all; below
    this the readers return None (INSUFFICIENT_DATA)."""
    try:
        return int(_block("eod_bias").get("min_records", 20))
    except (TypeError, ValueError):
        return 20


def get_eod_drift_monitor_enabled() -> bool:
    """Whether ``drift_monitor.check_drift`` runs its between-passes fast path
    on each light sweep."""
    val = _block("eod_drift_monitor").get("enabled", True)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


# ── TimesFM service knobs (2026-10-20) ───────────────────────────────────────
# model_id / max_context / max_horizon were read from env at server.py import
# time; disable_fallback from env in the orchestrator's fetch path. All four
# are now YAML-only. TIMESFM_READY_TIMEOUT is deliberately NOT here — it is
# read by start.sh / on_start.sh / the Dockerfile (shell has no YAML parser).


def get_timesfm_model_id() -> str:
    """HF model id loaded by ``server.py`` (the GPU inference process). An
    empty/blank value falls back to the default rather than loading nothing."""
    default = str(_DEFAULTS["timesfm_service"]["model_id"])
    val = _block("timesfm_service").get("model_id", default)
    return str(val).strip() or default


def get_timesfm_max_context() -> int:
    """Max input context length for the TimesFM forecast call."""
    try:
        return int(_block("timesfm_service").get("max_context", 1024))
    except (TypeError, ValueError):
        return 1024


def get_timesfm_max_horizon() -> int:
    """Max forecast horizon for the TimesFM forecast call."""
    try:
        return int(_block("timesfm_service").get("max_horizon", 256))
    except (TypeError, ValueError):
        return 256


def get_timesfm_disable_fallback() -> bool:
    """True = gateway URL only; the orchestrator's direct-to-:8002 fallback is
    disabled. False (default) keeps the historical fallback behaviour."""
    val = _block("timesfm_service").get("disable_fallback", False)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


