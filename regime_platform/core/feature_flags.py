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
    "markov_source_mode": "trend_hmm_shadow",
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
    "min_rr": 2.0,
    "heavy_sweep_grid": {
        "max_sharpe": 10.0,
        "min_trades": 10,
    },
    "heavy_sweep_atr_anchored_bricks_mode": "off",
    "heavy_sweep_deadline_aware_scoring_mode": "off",
    "regime_warm_start_mode": "off",
    "regime_label_debounce_mode": "off",
    "regime_label_debounce_ticks": 3,
    "qualification_gates": {
        "min_regime_conf": 0.60,
        "min_kelly": 0.02,
        "min_timesfm_confidence": 0.05,
        "min_ev": 0.0,
        "min_p_win": 0.55,
    },
    "timesfm_gate_mode": "soft",
    "sweep_after_hours": False,
    "signal_cooldown_minutes": 60.0,
    "expiry_rate_alert": {
        "enabled": True,
        "threshold_pct": 50.0,
        "min_resolved": 15,
        "cooldown_hours": 24.0,
    },
    "calibration_bias_min_episodes": 2,
    "signal_stream_meta_model_mode": "off",
    "renko_brick_retention_days": 2,
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
    """``legacy_hmm`` | ``trend_hmm_shadow`` | ``trend_hmm_live``."""
    return str(_load().get("markov_source_mode", _DEFAULTS["markov_source_mode"])).lower()


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


def get_heavy_sweep_reachability_mode() -> str:
    """``off`` | ``shadow`` | ``live`` (Phase 3a of the calibration
    reframe — reachability/expectancy-aware heavy-sweep combo pick)."""
    return str(_load().get(
        "heavy_sweep_reachability_mode", _DEFAULTS["heavy_sweep_reachability_mode"],
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
