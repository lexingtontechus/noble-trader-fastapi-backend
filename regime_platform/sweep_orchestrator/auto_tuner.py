"""Layer 1 — autonomous continuous parameter self-tuning.

See worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.2 for the design. Summary:
a scheduled job (own cadence, daily — separate from the 4h correction
pass) that nudges ONE Tier D registry parameter per cycle by one bounded,
rate-limited step (via ``tuning_registry.write_param()``, which already
enforces ``PARAM_BOUNDS`` and the step-size cap), then evaluates on the
NEXT cycle whether a single system-wide health metric (mean |raw
calibration bias| across active symbols) improved or regressed, and
auto-reverts on regression.

Deliberately NOT a black-box optimizer (no RL, no Bayesian search) —
simple coordinate-ascent-with-a-governor, matching this codebase's
existing style and honest about the "arbitrary within bounds" reality
(worklog §1 revision note) instead of pretending a fancier algorithm
would make the adjustments less arbitrary.

Scope: Tier D only (calibration/enforcement mechanics — the tier
explicitly designed to be tuned as live behaviour is observed, and the
lowest blast radius). Tier B/C (blend weights, gate thresholds) are
deliberately excluded — those are Layer 2's job (agent-triggered on a
detected regime shift, not a routine daily nudge). See §3's tier table
for why.

IMPORTANT — this module WRITES tuned values into the registry. As of
2026-08-23 nothing downstream READS them yet: eod_correction.py,
calibration_enforcement.py, etc. still read settings.py/env vars
directly. Migrating those read sites to
``tuning_registry.read_param_async()`` is separate, not-yet-done work —
until then, this job's writes are inert with respect to live behaviour
(they still exercise the registry's bounds/rate-limit/rollback machinery
and build up a real audit trail, which is useful on its own, but do not
change what the orchestrator actually does).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from regime_platform.core import tuning_registry
from regime_platform.core.settings import settings

logger = logging.getLogger("regime.sweep.auto_tuner")

# ── Pilot scope: Tier D only ─────────────────────────────────────────────
TIER_D_PILOT_PARAMS: tuple[str, ...] = (
    "EOD_BIAS_ENFORCED_THRESHOLD",
    "EOD_BIAS_RAW_OVERCONFIDENT",
    "EOD_BIAS_RAW_UNDERCONFIDENT",
    "EOD_DRIFT_THRESHOLD",
    "BAYES_PRIOR_COUNT",
    "KELLY_REDUCTION",
    "STAGNATION_EPSILON",
    "STAGNATION_TIER1_CYCLES",
    "STAGNATION_TIER2_CYCLES",
    "STAGNATION_RESET_CYCLES",
    "STAGNATION_KELLY_REDUCTION_TIER1",
)

# Noise floor for the health metric (mean |raw bias|, roughly 0-1 scale):
# a move smaller than this is "neutral," not a real improvement/regression.
EVAL_EPSILON = 0.01

# Minimum per-symbol calibration records for that symbol's bias to count
# toward the health metric — mirrors settings.EOD_BIAS_MIN_RECORDS so the
# tuner isn't reacting to noise from thinly-traded symbols.
_MIN_RECORDS_FOR_METRIC = 20

_BASELINE_RE = re.compile(r"baseline_metric=([-0-9.eE]+)")


# ── Health metric ─────────────────────────────────────────────────────────

async def _compute_system_health_metric() -> Optional[float]:
    """Mean |raw calibration bias| across active symbols with enough
    records. Lower is better — the single north star this module's
    coordinate-ascent optimizes toward for every Tier D parameter (see
    module docstring for why one shared metric, not one per parameter).

    Returns ``None`` when no symbol currently has enough data (too early
    to tune against, not a real "zero bias").
    """
    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_active_symbols,
        fetch_calibration_bias_raw,
    )

    try:
        symbols = await fetch_active_symbols()
    except Exception as exc:
        logger.warning("[auto_tuner] fetch_active_symbols failed: %s", exc)
        return None

    abs_biases: list[float] = []
    for sym in symbols:
        symbol = sym.get("symbol") if isinstance(sym, dict) else None
        if not symbol:
            continue
        try:
            cal = await fetch_calibration_bias_raw(
                symbol, min_records=_MIN_RECORDS_FOR_METRIC,
            )
        except Exception:
            cal = None
        if cal and cal.get("bias") is not None:
            abs_biases.append(abs(float(cal["bias"])))

    if not abs_biases:
        return None
    return sum(abs_biases) / len(abs_biases)


# ── Pure decision logic (unit-testable without any network) ────────────

def _parse_baseline_metric(reason: Optional[str]) -> Optional[float]:
    if not reason:
        return None
    m = _BASELINE_RE.search(reason)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def classify_result(metric_now: float, baseline: float, eval_epsilon: float = EVAL_EPSILON) -> str:
    """'improved' | 'regressed' | 'neutral' — lower metric is better."""
    if metric_now < baseline - eval_epsilon:
        return "improved"
    if metric_now > baseline + eval_epsilon:
        return "regressed"
    return "neutral"


def pick_direction(history: list[dict[str, Any]]) -> int:
    """+1 (try increasing) or -1 (try decreasing) for the NEXT nudge,
    based on the most recently EVALUATED (non-pending) history entry:

      - 'improved'  -> keep going the same direction that worked.
      - 'regressed' -> flip relative to the direction that failed (the
        failed move has already been reverted by ``_evaluate_pending``
        by the time this is called for real, but the direction itself
        is still informative).
      - 'neutral'   -> keep the same direction (mild default — as good
        as any other arbitrary choice; see module docstring).

    ``history`` is newest-first (as returned by
    ``tuning_registry.get_history()``). Returns ``+1`` if there's no
    evaluated history yet — an arbitrary starting point; safety comes
    from bounds/rate-limit/rollback, not from this choice being clever.
    """
    for entry in history:
        result = entry.get("cycle_result")
        if result in (None, "pending"):
            continue
        prev = entry.get("previous_value")
        val = entry.get("value")
        if prev is None or val is None:
            continue
        try:
            last_direction = 1 if float(val) >= float(prev) else -1
        except (TypeError, ValueError):
            continue
        if result == "regressed":
            return -last_direction
        return last_direction  # 'improved' or 'neutral'
    return 1


# ── Evaluate a pending write from a prior cycle ─────────────────────────

async def _evaluate_pending(name: str, metric_now: float) -> Optional[dict[str, Any]]:
    """If ``name``'s most recent write is still 'pending' evaluation,
    classify it against ``metric_now``, record the result, and — if it
    regressed — revert to ``previous_value``.

    Returns an outcome dict, or ``None`` if there was nothing pending to
    evaluate (no history, already evaluated, or the baseline couldn't be
    parsed from a malformed/foreign ``reason`` string).
    """
    from regime_platform.sweep_orchestrator.supabase_client import (
        update_tuning_param_cycle_result,
    )

    history = await tuning_registry.get_history(name, limit=1)
    if not history:
        return None
    latest = history[0]
    if latest.get("cycle_result") != "pending":
        return None

    baseline = _parse_baseline_metric(latest.get("reason"))
    if baseline is None:
        return None

    result = classify_result(metric_now, baseline)
    row_id = latest.get("id")
    if row_id:
        await update_tuning_param_cycle_result(row_id, result)

    outcome: dict[str, Any] = {
        "param": name, "result": result, "baseline": baseline,
        "metric_now": metric_now, "reverted": False,
    }

    if result == "regressed":
        previous_value = latest.get("previous_value")
        if previous_value is not None:
            revert = await tuning_registry.write_param(
                name, float(previous_value),
                reason=f"auto_revert baseline_metric={metric_now:.6f}",
                updated_by="layer1_auto_tuner",
            )
            outcome["reverted"] = bool(revert.get("applied"))

    return outcome


# ── Pick which parameter to nudge this cycle (recency round-robin) ─────

async def _pick_next_param(names: tuple[str, ...]) -> Optional[str]:
    """Whichever pilot parameter has gone longest without a write (or has
    never been written) goes next. Uses the audit trail itself as the
    round-robin cursor — no separate state to maintain or lose."""
    oldest_name: Optional[str] = None
    oldest_ts: Optional[str] = None
    for name in names:
        history = await tuning_registry.get_history(name, limit=1)
        if not history:
            return name  # never tuned — prioritize it immediately
        ts = history[0].get("ts")
        if oldest_ts is None or (ts and ts < oldest_ts):
            oldest_ts = ts
            oldest_name = name
    return oldest_name


# ── Entry point ──────────────────────────────────────────────────────────

async def run_auto_tuning() -> dict[str, Any]:
    """Run one Layer 1 auto-tuning cycle: evaluate the previous cycle's
    pending write for every pilot parameter (auto-reverting regressions),
    then take one bounded step on whichever parameter is due next.

    Best-effort throughout — every per-parameter failure is caught,
    logged, and counted; this function never raises.
    """
    summary: dict[str, Any] = {
        "checked": 0, "evaluated": 0, "tuned": 0, "reverted": 0, "errors": 0,
        "metric_now": None, "skipped": None, "target": None,
    }

    if not getattr(settings, "TUNING_AUTONOMY_ENABLED", True):
        summary["skipped"] = "tuning_autonomy_disabled"
        logger.info("[auto_tuner] skipped — TUNING_AUTONOMY_ENABLED=false")
        return summary

    try:
        metric_now = await _compute_system_health_metric()
    except Exception as exc:
        logger.warning("[auto_tuner] health metric computation failed: %s", exc)
        summary["errors"] += 1
        return summary

    summary["metric_now"] = metric_now
    if metric_now is None:
        summary["skipped"] = "insufficient_data"
        logger.info("[auto_tuner] skipped — no symbol has enough calibration data yet")
        return summary

    for name in TIER_D_PILOT_PARAMS:
        try:
            outcome = await _evaluate_pending(name, metric_now)
        except Exception as exc:
            logger.warning("[auto_tuner] evaluate %s failed: %s", name, exc)
            summary["errors"] += 1
            continue
        if outcome is not None:
            summary["evaluated"] += 1
            if outcome["reverted"]:
                summary["reverted"] += 1
            logger.info(
                "[auto_tuner] %s: %s (baseline=%.6f, now=%.6f, reverted=%s)",
                name, outcome["result"], outcome["baseline"], metric_now,
                outcome["reverted"],
            )

    summary["checked"] = len(TIER_D_PILOT_PARAMS)

    try:
        target = await _pick_next_param(TIER_D_PILOT_PARAMS)
    except Exception as exc:
        logger.warning("[auto_tuner] pick target failed: %s", exc)
        target = None
    summary["target"] = target

    if target:
        try:
            history = await tuning_registry.get_history(target, limit=5)
            direction = pick_direction(history)
            lo, hi = tuning_registry.PARAM_BOUNDS[target]
            default = getattr(settings, target, None)
            current = tuning_registry.read_param(target, default=default)
            # Request the full bound extreme in the chosen direction —
            # write_param()'s own rate limiter clips this to one small
            # bounded step. Layer 1 decides DIRECTION only; Layer 0
            # decides step SIZE, so the two concerns stay separate.
            probe_value = hi if direction > 0 else lo
            result = await tuning_registry.write_param(
                target, probe_value,
                reason=f"auto_tune baseline_metric={metric_now:.6f}",
                updated_by="layer1_auto_tuner",
            )
            if result.get("applied"):
                summary["tuned"] += 1
                logger.info(
                    "[auto_tuner] %s: %.6f -> %.6f (direction=%+d, rate_limited=%s)",
                    target, current, result["value"], direction, result["rate_limited"],
                )
        except Exception as exc:
            logger.warning("[auto_tuner] nudge %s failed: %s", target, exc)
            summary["errors"] += 1

    logger.info(
        "[auto_tuner] complete: metric=%s, checked=%d, evaluated=%d, "
        "reverted=%d, tuned=%d, errors=%d",
        f"{metric_now:.6f}" if metric_now is not None else "n/a",
        summary["checked"], summary["evaluated"], summary["reverted"],
        summary["tuned"], summary["errors"],
    )
    return summary
