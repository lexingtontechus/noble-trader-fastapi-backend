"""
Regime-vs-realized-price mismatch detector — Phase 2, detector 1 of 3
(worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS2,
self_learning_phase_2_detectors, RM-37).

Motivated directly by the real AAPL case this session diagnosed: the
regime classifier read "low_vol_strong_bull" continuously 2026-09-01
through 2026-09-09 (confidence 0.92-1.00), while the real price peaked
at $328.07 (09-03) and declined to $315.45 by 09-09 — a real -3.85%
drawdown the regime engine never registered. `EVEngine.detect_regime_shift()`
cannot catch this failure mode: it only fires when the regime engine's
OWN output changes, not when it SHOULD have changed and didn't. This
module is a plain statistical function (per the 4-phase scope: Phase 2
is "plain functions, not LLM calls") that checks regime labels against
realized price action directly — a second, independent detector on the
same question the classifier itself is supposed to answer.

Deliberately NOT wired into any live decision path — this is a
diagnostic/investigative tool (Phase 2's own scope), not a gate. Output
feeds a future Phase 3 (LLM-assisted triage, not built) or direct human
review; nothing here auto-applies anything.
"""

from __future__ import annotations

from typing import Any, TypedDict


class DailyRegimeRow(TypedDict):
    regime_label: str | None
    regime_conf: float
    day_open: float
    day_close: float


def regime_implied_direction(regime_label: str | None) -> str | None:
    """Extract the bull/bear direction from a combined regime_label —
    format is ``f"{vol_state}_vol_{trend_state}"`` (see
    ``regime_engine.py``'s own ``regime_label=f"{vol_state}_vol_{trend_state}"``
    and its ``TREND_LABELS = ["strong_bear", "bear", "bull", "strong_bull"]``).
    Returns ``"up"``/``"down"`` for the bull/bear family, ``None`` for
    anything else (neutral/flat, unrecognized, or missing).
    """
    if not regime_label:
        return None
    parts = regime_label.split("_vol_")
    if len(parts) != 2:
        return None
    trend = parts[1]
    if trend in ("bull", "strong_bull"):
        return "up"
    if trend in ("bear", "strong_bear"):
        return "down"
    return None


def detect_regime_price_mismatch(
    daily_rows: list[DailyRegimeRow],
    min_confidence: float = 0.85,
    min_price_move_pct: float = 0.02,
    min_consecutive_days: int = 3,
) -> dict[str, Any] | None:
    """Walk ``daily_rows`` (chronological) for the EARLIEST run of at
    least ``min_consecutive_days`` where the regime's implied direction
    stays constant and its confidence averages at or above
    ``min_confidence`` — then check whether the realized price, at ANY
    point within that run, showed a peak-to-trough reversal (for an
    "up" run) or trough-to-peak rally (for a "down" run) of at least
    ``min_price_move_pct`` against the running extreme reached so far.

    CORRECTED after the first real 21-day backtest (worklog
    20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md, Phase 2
    build note): an earlier draft compared the run's overall peak only
    to its FINAL day's price, which missed the real AAPL case in a full
    historical run — the regime run didn't break until 09-11, one day
    AFTER price had already partially recovered (09-10: $325.95, only
    -0.65% off the $328.07 peak), diluting away the real -3.85%
    mismatch that existed the day before (09-09: $315.45). A transient
    mismatch that later partially recovers before the regime label
    itself changes is exactly the failure mode this detector exists to
    catch, so it must be evaluated as of its WORST point within the
    run, not just the run's last day.

    Returns ``None`` if no qualifying mismatch is found.
    """
    n = len(daily_rows)
    for start in range(n):
        direction = regime_implied_direction(daily_rows[start]["regime_label"])
        if direction is None:
            continue

        end = start
        while (
            end + 1 < n
            and regime_implied_direction(daily_rows[end + 1]["regime_label"]) == direction
        ):
            end += 1
        run_len = end - start + 1
        if run_len < min_consecutive_days:
            continue

        run = daily_rows[start:end + 1]
        avg_conf = sum(r["regime_conf"] for r in run) / run_len
        if avg_conf < min_confidence:
            continue

        # Track the worst point reached AT ANY DAY within the run, not
        # just the run's final day.
        worst_move_pct = 0.0
        worst_day_index = 0
        worst_extreme = run[0]["day_open"]
        if direction == "up":
            running_peak = run[0]["day_open"]
            for i, r in enumerate(run):
                running_peak = max(running_peak, r["day_open"], r["day_close"])
                if running_peak <= 0:
                    continue
                for price in (r["day_open"], r["day_close"]):
                    move_pct = (price - running_peak) / running_peak  # <= 0
                    if move_pct < worst_move_pct:
                        worst_move_pct = move_pct
                        worst_day_index = i
                        worst_extreme = running_peak
            mismatch = worst_move_pct <= -min_price_move_pct
        else:  # "down"
            running_trough = run[0]["day_open"]
            for i, r in enumerate(run):
                running_trough = min(running_trough, r["day_open"], r["day_close"])
                if running_trough <= 0:
                    continue
                for price in (r["day_open"], r["day_close"]):
                    move_pct = (price - running_trough) / running_trough  # >= 0
                    if move_pct > worst_move_pct:
                        worst_move_pct = move_pct
                        worst_day_index = i
                        worst_extreme = running_trough
            mismatch = worst_move_pct >= min_price_move_pct

        if mismatch:
            return {
                "start_index": start,
                "end_index": end,
                "worst_day_index": start + worst_day_index,
                "run_length_days": run_len,
                "regime_direction": direction,
                "avg_regime_conf": round(avg_conf, 4),
                "extreme_price": worst_extreme,
                "worst_price_move_pct": round(worst_move_pct, 4),
            }

    return None
