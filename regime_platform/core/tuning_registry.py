"""Tuning Registry — Layer 0 of agent-driven EV engine parameter tuning.

See worklog/PARAMETER_TUNING_PLAYBOOK.md §2 for the full design rationale.
Summary: most tunables in this system live in Studio Secrets (env vars,
human-UI-edited, restart-required to take effect) — a path that is
structurally incompatible with autonomous tuning, since there is no
realistic human review loop for individual adjustments. This module
generalizes the pattern ``config/brick_params.yaml`` + ``heavy_sweep.py``
already use successfully (file-based, hot-reloaded, no restart, no human
step) into a registry for ANY tunable parameter, not just brick geometry.

Read precedence: config file -> ``nt_tuning_params`` (Supabase, durable
audit trail + cross-instance fallback) -> caller-supplied default
(typically the ``settings.py`` factory value). Every read is clamped to
``PARAM_BOUNDS`` regardless of source — this is the actual safety
mechanism now that human review doesn't fire in practice.

This module provides the read/write/history primitives and enforces
bounds + rate limits. It does NOT decide WHEN or HOW MUCH to tune —
that's Layer 1 (a scheduled auto-tuner) and Layer 2 (an agent triggered
by a detected regime shift), neither of which is built yet.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.tuning_registry")

# ── Bounds ──────────────────────────────────────────────────────────────
# Hard clamp applied on every read, regardless of source (file/DB/default).
# Set once, rarely touched — this is deliberately the slow-moving half of
# the design; the values inside the bounds are what's meant to move often.
#
# Grouped by the tier taxonomy in worklog/PARAMETER_TUNING_PLAYBOOK.md §3.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    # Tier B — global blend/model weights (narrowest bounds, smallest
    # rate-limit steps: highest blast radius, every symbol at once).
    "MARKOV_EMA_ALPHA": (0.05, 0.80),
    "TIMESFM_SIGMOID_K": (5.0, 20.0),
    "TIMESFM_CLAMP_MIN": (0.01, 0.20),
    "TIMESFM_CLAMP_MAX": (0.80, 0.99),
    # Tier C — gate thresholds (safety margins, not performance knobs).
    "MIN_REGIME_CONF": (0.50, 0.85),
    "MIN_KELLY": (0.01, 0.10),
    "MIN_TIMESFM_CONFIDENCE": (0.02, 0.20),
    "MIN_EV": (0.0, 0.10),
    "MIN_RR": (1.2, 3.0),
    "MIN_P_WIN": (0.50, 0.70),
    # Tier D — calibration/enforcement mechanics (widest bounds — this
    # tier is explicitly meant to be tuned as live behaviour is observed).
    "EOD_BIAS_ENFORCED_THRESHOLD": (0.05, 0.20),
    "EOD_BIAS_RAW_OVERCONFIDENT": (0.15, 0.50),
    "EOD_BIAS_RAW_UNDERCONFIDENT": (-0.50, -0.10),
    "EOD_DRIFT_THRESHOLD": (0.05, 0.30),
    "BAYES_PRIOR_COUNT": (2, 30),
    "KELLY_REDUCTION": (0.10, 0.75),
    "STAGNATION_EPSILON": (0.005, 0.10),
    "STAGNATION_TIER1_CYCLES": (1, 6),
    "STAGNATION_TIER2_CYCLES": (2, 12),
    "STAGNATION_RESET_CYCLES": (4, 20),
    "STAGNATION_KELLY_REDUCTION_TIER1": (0.10, 0.50),
}

# Rate limit: maximum fractional move per write, relative to the current
# value — so a single tuning cycle can't jump straight to an extreme even
# though it's within bounds.
DEFAULT_MAX_STEP_FRACTION = 0.20

# ── File-backed cache (mirrors regime_platform/config/brick_params.py) ──

_DEFAULT_YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "tuning_registry.yaml"


def _yaml_path() -> Path:
    env_path = os.getenv("TUNING_REGISTRY_YAML")
    if env_path:
        return Path(env_path)
    return _DEFAULT_YAML_PATH


@lru_cache(maxsize=1)
def _load_yaml_cached() -> dict[str, Any]:
    """Load and cache the registry YAML. Cache is invalidated by
    ``reload()`` (called automatically after every ``write_param()``) or
    by setting ``TUNING_REGISTRY_YAML_NOCACHE=1``."""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — tuning registry file disabled")
        return {"params": {}}

    path = _yaml_path()
    if not path.exists():
        return {"params": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "params" not in data or not isinstance(data["params"], dict):
            data["params"] = {}
        return data
    except Exception as exc:
        logger.error("Failed to load tuning registry YAML (%s) — treating as empty", exc)
        return {"params": {}}


def reload() -> None:
    """Force a re-read of the YAML file on the next read call. Called
    automatically after ``write_param()``; exposed for tests / manual
    invalidation."""
    _load_yaml_cached.cache_clear()


def _params() -> dict[str, Any]:
    if os.getenv("TUNING_REGISTRY_YAML_NOCACHE") == "1":
        _load_yaml_cached.cache_clear()
    return _load_yaml_cached().get("params", {})


# ── Bounds enforcement ──────────────────────────────────────────────────

def clamp(name: str, value: float) -> float:
    """Clamp ``value`` into ``PARAM_BOUNDS[name]``. Parameters with no
    bounds entry pass through unchanged — but ``write_param()`` refuses to
    write an unbounded parameter by default (``require_bounds=True``), so
    in practice nothing reaches ``clamp()`` without a declared range
    unless a caller deliberately opts out."""
    bounds = PARAM_BOUNDS.get(name)
    if bounds is None:
        return value
    lo, hi = bounds
    return max(lo, min(hi, value))


# ── Reads ────────────────────────────────────────────────────────────────

def read_param(name: str, default: Optional[float] = None) -> float:
    """Read a tuned parameter: config file -> ``default``. Always clamped
    to ``PARAM_BOUNDS``.

    Synchronous and file-only (no Supabase fallback) so it's safe to call
    from hot per-tick code paths without an event loop — e.g. inside
    ``_process_symbol``. Use ``read_param_async()`` when the caller is
    already in an async context and wants the DB fallback for a cold
    file (first boot, or a fresh Studio instance before the file has
    synced).
    """
    params = _params()
    entry = params.get(name)
    if isinstance(entry, dict) and "value" in entry:
        return clamp(name, float(entry["value"]))
    if default is None:
        raise KeyError(
            f"tuning_registry: no file entry for {name!r} and no default given"
        )
    return clamp(name, float(default))


async def read_param_async(name: str, default: Optional[float] = None) -> float:
    """Same as ``read_param()``, but falls back to the Supabase
    ``nt_tuning_params`` table when the file has no entry — for callers
    already in an async context (e.g. the eventual Layer 1 tuner job)."""
    params = _params()
    entry = params.get(name)
    if isinstance(entry, dict) and "value" in entry:
        return clamp(name, float(entry["value"]))

    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_latest_tuning_param,
    )
    row = await fetch_latest_tuning_param(name)
    if row is not None and row.get("value") is not None:
        return clamp(name, float(row["value"]))

    if default is None:
        raise KeyError(
            f"tuning_registry: no file/DB entry for {name!r} and no default given"
        )
    return clamp(name, float(default))


# ── Writes ───────────────────────────────────────────────────────────────

def _write_yaml(params: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — cannot write tuning registry YAML")
        return
    path = _yaml_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"version": 1, "params": params}, f,
                           default_flow_style=False, sort_keys=True)
        reload()
    except Exception as exc:
        logger.error("Failed to write tuning registry YAML (%s)", exc)


async def write_param(
    name: str,
    new_value: float,
    *,
    reason: str,
    updated_by: str = "agent",
    max_step_fraction: float = DEFAULT_MAX_STEP_FRACTION,
    require_bounds: bool = True,
) -> dict[str, Any]:
    """Validate + write a tuned parameter: file first (immediately live
    for this process), then a ``nt_tuning_params`` audit row (durable,
    best-effort).

    Enforces, in order:
      1. ``name`` must have a ``PARAM_BOUNDS`` entry if
         ``require_bounds=True`` (default) — refuses to let a brand-new
         tunable ship live without a declared range.
      2. ``settings.TUNING_AUTONOMY_ENABLED`` — the global kill switch;
         when disabled, no write happens (reads are unaffected).
      3. The new value is clamped into ``PARAM_BOUNDS``.
      4. Rate limit: the move from the CURRENT value is capped at
         ``max_step_fraction`` of the current value (or 5% of the bound
         span, whichever is larger, so a near-zero current value doesn't
         make the limit vanish) — no single write can jump to an extreme.

    Returns ``{"applied": bool, "value": float | None, "previous_value":
    float | None, "clamped": bool, "rate_limited": bool, "reason": str}``.
    The Supabase audit write is best-effort — a failure there is logged,
    never raised, matching this codebase's other write paths (the file
    write is authoritative for this process regardless).
    """
    if require_bounds and name not in PARAM_BOUNDS:
        raise ValueError(
            f"tuning_registry: {name!r} has no PARAM_BOUNDS entry — add one "
            f"before writing to it (see worklog/PARAMETER_TUNING_PLAYBOOK.md §2.3)"
        )

    if not getattr(settings, "TUNING_AUTONOMY_ENABLED", True):
        return {
            "applied": False, "value": None, "previous_value": None,
            "clamped": False, "rate_limited": False,
            "reason": "tuning_autonomy_disabled",
        }

    params = dict(_params())
    current_entry = params.get(name) or {}
    previous_value = current_entry.get("value")

    clamped_value = clamp(name, float(new_value))
    was_clamped = clamped_value != float(new_value)

    rate_limited = False
    if previous_value is not None:
        prev = float(previous_value)
        bounds = PARAM_BOUNDS.get(name)
        span = (bounds[1] - bounds[0]) if bounds else (abs(prev) or 1.0)
        max_delta = max(abs(prev) * max_step_fraction, span * 0.05)
        if abs(clamped_value - prev) > max_delta:
            rate_limited = True
            direction = 1 if clamped_value > prev else -1
            clamped_value = clamp(name, prev + max_delta * direction)

    params[name] = {
        "value": clamped_value,
        "previous_value": previous_value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": updated_by,
        "reason": reason,
    }
    _write_yaml(params)

    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            insert_tuning_param_row,
        )
        await insert_tuning_param_row(
            name, clamped_value, previous_value, updated_by, reason,
            clamped=was_clamped, rate_limited=rate_limited,
            cycle_result="pending",
        )
    except Exception as exc:
        logger.warning("tuning_registry: DB audit write failed for %s: %s", name, exc)

    return {
        "applied": True,
        "value": clamped_value,
        "previous_value": previous_value,
        "clamped": was_clamped,
        "rate_limited": rate_limited,
        "reason": reason,
    }


async def get_history(name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent write history for ``name`` from the Supabase audit trail
    (newest first) — for auto-rollback / streak-style decisions. See
    ``supabase_client.fetch_tuning_param_history``."""
    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_tuning_param_history,
    )
    return await fetch_tuning_param_history(name, limit=limit)
