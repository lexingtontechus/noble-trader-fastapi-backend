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
