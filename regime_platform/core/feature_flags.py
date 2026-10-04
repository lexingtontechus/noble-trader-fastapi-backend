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
        "boost_scale": 0.3,
        "penalty_scale": 0.5,
    },
    "heavy_sweep_reachability_mode": "off",
    # 2026-09-29 GOAL: wr>=0.5, exp<=0.2. Settings BY CLASS; one live decision.
    #   commodity  TP 1-4  / SL 2-4   wr 0.54  exp 0%   pnl +1.28  GOAL
    #   equity     TP 8-14 / SL 6-12  wr 0.81  exp 2%   pnl +3.95  GOAL
    #   forex      TP 1-4  / SL 2-4   wr 0.44  exp 0%   pnl +1.38  (wr short)
    #   crypto     TP 3-8  / SL 1-3   wr 0.41  exp 11%  pnl +1.47  (wr short)
    "dynamic_atr_mode": "live",
    "dynamic_atr_risk_cap": 0.35,
    "dynamic_atr": {
        "lookback_days": 10,
        "tp_atr": [2.0, 4.0, 6.0, 8.0],
        "sl_atr": [1.0, 2.0, 3.0, 4.0],
        "tp_atr_min": 1.0,
        "tp_atr_max": 14.0,
        "sl_atr_min": 1.0,
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
        "by_class": {
            "commodity": {
                "tp_atr": [1.0, 2.0, 3.0, 4.0],
                "sl_atr": [2.0, 3.0, 4.0],
                "tp_atr_min": 1.0,
                "tp_atr_max": 4.0,
                "sl_atr_min": 2.0,
                "sl_atr_max": 4.0,
            },
            "equity": {
                "tp_atr": [8.0, 10.0, 12.0, 14.0],
                "sl_atr": [6.0, 8.0, 10.0, 12.0],
                "tp_atr_min": 8.0,
                "tp_atr_max": 14.0,
                "sl_atr_min": 6.0,
                "sl_atr_max": 12.0,
            },
            "forex": {
                "tp_atr": [1.0, 2.0, 3.0, 4.0],
                "sl_atr": [2.0, 3.0, 4.0],
                "tp_atr_min": 1.0,
                "tp_atr_max": 4.0,
                "sl_atr_min": 2.0,
                "sl_atr_max": 4.0,
            },
            "crypto": {
                "tp_atr": [3.0, 5.0, 8.0],
                "sl_atr": [1.0, 2.0, 3.0],
                "tp_atr_min": 3.0,
                "tp_atr_max": 8.0,
                "sl_atr_min": 1.0,
                "sl_atr_max": 3.0,
            },
        },
    },
    "atr_anchored_evidence_mode": "shadow",
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
    "min_rr": 2.0,
    "heavy_sweep_grid": {
        "max_sharpe": 10.0,
        "min_trades": 10,
    },
    "heavy_sweep_atr_anchored_bricks_mode": "off",
    "heavy_sweep_deadline_aware_scoring_mode": "off",
    "settlement_contract_mode": "live",
    "settlement_same_bar_policy": "tp_first",
    "p_win_bracket_mode": "shadow",
    "min_p_win_bracket": 0.20,
    "p_win_in_ev_mode": "live",
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
    "signal_sim_retention_resolved_days": 120,
    "kairos_mode": "shadow",
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
        return _DEFAULTS["timesfm_k_floor"]


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


def get_dynamic_atr_config(
    asset_class: str | None = None, symbol: str | None = None
) -> dict[str, Any]:
    """Dynamic ATR selector settings (``dynamic_atr`` block).

    Precedence (2026-09-30): ``by_symbol.<SYMBOL>`` → ``by_class.<asset_class_key>``
    → global. Per-symbol is the default unit (each symbol's own path stats);
    forex majors share ``by_class.forex``.
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

        ov = by_class.get(asset_class_key(asset_class))
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


def get_min_p_win_bracket_for_asset_class(asset_class: str | None) -> float:
    """Per-asset-class ``min_p_win_bracket`` threshold (2026-10-04).

    Falls back to the global ``min_p_win_bracket`` when the class is not
    found in the per-class map. Used by the ``p_win_ok`` gate when
    ``p_win_in_ev_mode=live``.
    """
    try:
        from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key
        key = asset_class_key(asset_class)
    except Exception:
        key = "default"
    overrides = _load().get("min_p_win_bracket_by_asset_class", {})
    if isinstance(overrides, dict) and key in overrides:
        try:
            return float(overrides[key])
        except (TypeError, ValueError):
            pass
    return get_min_p_win_bracket()


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
    cooldown_hours, min_age_hours.
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


