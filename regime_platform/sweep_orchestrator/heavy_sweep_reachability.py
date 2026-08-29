"""
Heavy-Sweep Reachability Awareness — Phase 3a of the calibration reframe
════════════════════════════════════════════════════════════════════════

worklog/20260825_CALIBRATION_REFRAME_SCOPE.md, "Phase 3 scope". A live
simulation against real price data (5 symbols) found the heavy sweep's
Sharpe-only grid pick (``heavy_sweep.pick_best_combo``) sometimes lands
in a distance/ATR bucket (``bracket_reachability.py``'s buckets) with
negative realized expectancy (``nt_calibration_bias_by_bucket.
expectancy_r``) — and for 2 of 5 symbols tested, the grid never even
generated a candidate in a positive-expectancy bucket at all, so a pure
post-hoc filter had nothing to fall back to for those symbols.

``reachability_aware_pick`` is a PURE function (no I/O) — mirrors
``pick_best_combo``'s own ``min_rr``/``max_sharpe``/``min_trades``
filter internally (kept as a local copy rather than shared, same
"mirror, don't share" convention ``calibration_enforcement.py`` already
uses for ``BAYES_PRIOR_COUNT``/``KELLY_REDUCTION``, so this module has
no import-time dependency on ``heavy_sweep.py`` and there's no circular
import), then additionally requires each surviving combo's
``distance/ATR`` bucket to show ``expectancy_r > 0`` OR have no
calibration data yet for that bucket — an unscored bucket is not
evidence AGAINST a combo, only a scored-negative one is.

Deliberately does NOT change ``heavy_sweep.pick_best_combo`` — Phase 3a
is a shadow computation only (``heavy_sweep_reachability_mode=off|
shadow|live``, ``config/feature_flags.yaml``, default ``off``); see
``heavy_sweep.py``'s wiring for how the mode gate uses this function's
output.

Combos are duck-typed (only ``.brick_size``/``.sl_bricks``/
``.tp_bricks``/``.sharpe``/``.total_trades`` are read) rather than
importing ``heavy_sweep.ComboResult`` — avoids a circular import
(``heavy_sweep.py`` imports this module, not the other way around).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.heavy_sweep_reachability")

# Mirrors heavy_sweep.py's own MIN_RR/MAX_SHARPE/MIN_TRADES defaults —
# kept as a local copy (see module docstring) rather than imported, so
# a divergence would need to be deliberate on both sides, not silent.
_MIN_RR_DEFAULT = 2.0
_MAX_SHARPE_DEFAULT = 10.0
_MIN_TRADES_DEFAULT = 10


def _bucket_for_combo(combo: Any, atr: float | None) -> Optional[str]:
    if not atr:
        return None
    distance = combo.tp_bricks * combo.brick_size
    return _bucket_for(distance / atr)


def reachability_aware_pick(
    combos: list[Any],
    atr: float | None,
    bias_by_bucket: dict[str, float | None],
    min_rr: float = _MIN_RR_DEFAULT,
    max_sharpe: float = _MAX_SHARPE_DEFAULT,
    min_trades: int = _MIN_TRADES_DEFAULT,
) -> dict[str, Any]:
    """Compare the baseline Sharpe-only pick against a reachability/
    expectancy-aware pick over the SAME quality-filtered candidate pool.

    Args:
        combos: the full, unfiltered grid-search results for one symbol
            (``heavy_sweep._renko_grid_search_sync``'s return value).
        atr: the symbol's ATR(14) at sweep time. ``None``/``0`` disables
            the reachability leg entirely (every combo's bucket becomes
            unknown, so nothing is excluded) — same fail-open behavior
            as an unscored bucket, never gates blindly on missing data.
        bias_by_bucket: ``{distance_atr_bucket: expectancy_r}`` for this
            symbol from ``nt_calibration_bias_by_bucket``. A bucket
            absent from this dict (no calibration data yet) is treated
            the same as ``None`` — kept, not excluded.

    Returns:
        {
          "baseline_bucket": str | None,
          "baseline_sharpe": float | None,
          "filtered_bucket": str | None,      # None if nothing survived
          "filtered_sharpe": float | None,
          "filtered_combo": Any | None,
          "nothing_survived": bool,           # a baseline existed, but no reachable candidate did
          "pick_changed": bool,
        }
    """
    quality_filtered = [
        c for c in combos
        if c.tp_bricks / max(c.sl_bricks, 1) >= min_rr
        and c.sharpe <= max_sharpe
        and c.total_trades >= min_trades
    ]
    baseline = max(quality_filtered, key=lambda c: c.sharpe) if quality_filtered else None

    def _is_reachable(combo: Any) -> bool:
        bucket = _bucket_for_combo(combo, atr)
        if bucket is None:
            return True
        expectancy = bias_by_bucket.get(bucket)
        return expectancy is None or expectancy > 0

    reachable = [c for c in quality_filtered if _is_reachable(c)]
    filtered = max(reachable, key=lambda c: c.sharpe) if reachable else None

    def _combo_key(combo: Any | None) -> tuple | None:
        if combo is None:
            return None
        return (combo.brick_size, combo.sl_bricks, combo.tp_bricks)

    return {
        "baseline_bucket": _bucket_for_combo(baseline, atr) if baseline else None,
        "baseline_sharpe": baseline.sharpe if baseline else None,
        "filtered_bucket": _bucket_for_combo(filtered, atr) if filtered else None,
        "filtered_sharpe": filtered.sharpe if filtered else None,
        "filtered_combo": filtered,
        "nothing_survived": baseline is not None and filtered is None,
        "pick_changed": (
            baseline is not None and filtered is not None
            and _combo_key(filtered) != _combo_key(baseline)
        ),
    }


async def fetch_bias_by_bucket(symbol: str) -> dict[str, float | None]:
    """``{distance_atr_bucket: expectancy_r}`` for one symbol, from
    ``nt_calibration_bias_by_bucket``. Best-effort — returns ``{}`` on
    any failure (callers then treat every bucket as unscored, i.e. keep
    everything — fail-open, same as a missing ``atr``)."""
    if not settings.SUPABASE_URL:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_calibration_bias_by_bucket"),
                headers=_headers(),
                params={
                    "select": "distance_atr_bucket,expectancy_r",
                    "symbol": f"eq.{symbol}",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        if not isinstance(rows, list):
            return {}
        return {r["distance_atr_bucket"]: r.get("expectancy_r") for r in rows}
    except Exception as exc:
        logger.warning("fetch_bias_by_bucket: %s: fetch failed: %s", symbol, exc)
        return {}
