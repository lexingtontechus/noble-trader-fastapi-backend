"""
Dead-mechanism detector — Phase 2, detector 2 of 3
(worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS2,
self_learning_phase_2_detectors, RM-37).

This codebase has accumulated many off/shadow/live-gated mechanisms,
each writing to its own per-symbol Supabase table (``nt_macro_regime``,
``nt_trend_regime``, ``nt_regime_debounce_shadow``, etc.). A mechanism
whose mode is ``shadow``/``live`` SHOULD be writing fresh rows on some
known cadence — if it stops (a silent exception, a broken query, a
schema drift) nothing currently notices, because "is this table still
being updated" isn't anyone's job. This is a plain staleness check,
not a classifier — same "plain functions, not LLM calls" Phase 2 design
as ``regime_price_mismatch.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def detect_dead_mechanism(
    mechanism_name: str,
    mode: str,
    last_write_at: datetime | None,
    now: datetime,
    max_staleness: timedelta,
) -> dict[str, Any] | None:
    """``off`` mechanisms are never flagged — nothing is expected to be
    writing. ``shadow``/``live`` mechanisms are flagged if they have
    NEVER written a row, or their last write is older than
    ``max_staleness``.

    Returns ``None`` if the mechanism looks healthy (or is off).
    """
    if mode == "off":
        return None

    if last_write_at is None:
        return {
            "mechanism": mechanism_name,
            "mode": mode,
            "reason": "never_written",
            "last_write_at": None,
            "staleness_seconds": None,
        }

    staleness = now - last_write_at
    if staleness > max_staleness:
        return {
            "mechanism": mechanism_name,
            "mode": mode,
            "reason": "stale",
            "last_write_at": last_write_at.isoformat(),
            "staleness_seconds": staleness.total_seconds(),
        }

    return None
