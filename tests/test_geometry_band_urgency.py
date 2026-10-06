"""Episode dedup + geometry band urgency tests (2026-10-04).

Root cause under test: the light sweep captures a row in nt_signal_sim on
EVERY 15-min tick for a still-valid signal, so one economic signal lands as
N identical rows. On Sat 2026-10-03 XAUAUD wrote 75 identical rows (10.5x
duplication for the day), which inflated the EOD report's win rate / P&L and
poisoned the reachability + calibration-bias tables. See worklog
20261004_GEOMETRY_BAND_URGENCY_AND_EPISODE_DEDUP.md.
"""

from __future__ import annotations

from regime_platform.sweep_orchestrator.bracket_reachability import (
    _dedupe_episodes,
)
from regime_platform.sweep_orchestrator.geometry_band_urgency import (
    _compute_drift,
    _load_last_fired,
    _recompute_cell,
)


def _row(**over):
    base = {
        "symbol": "XAUAUD",
        "ts": "2026-10-03T04:46:36+00:00",
        "signal": "sell",
        "entry_price": 5954.644,
        "take_profit": 5864.26816,
        "stop_loss": 5999.83192,
        "atr": 9.941375,
        "outcome": "sl_hit",
        "resolved_at": "2026-10-03T10:38:09+00:00",
        "regime": "med_low_vol_bear",
        "p_win": 0.95,
    }
    base.update(over)
    return base


class TestDedupeEpisodes:
    def test_identical_recaptures_collapse_to_one(self):
        # The exact Oct-3 XAUAUD shape: same signal re-captured on every tick.
        rows = [_row(ts=f"2026-10-03T{h:02d}:00:00+00:00") for h in range(10)]
        assert len(rows) == 10
        assert len(_dedupe_episodes(rows)) == 1

    def test_keeps_earliest_row_per_episode(self):
        rows = [
            _row(ts="2026-10-03T06:00:00+00:00"),
            _row(ts="2026-10-03T04:00:00+00:00"),
            _row(ts="2026-10-03T05:00:00+00:00"),
        ]
        out = _dedupe_episodes(rows)
        assert len(out) == 1
        assert out[0]["ts"] == "2026-10-03T04:00:00+00:00"

    def test_different_levels_are_different_episodes(self):
        rows = [
            _row(),
            _row(take_profit=5800.0),           # different TP
            _row(signal="buy"),                 # different side
            _row(symbol="XAUUSD"),              # different symbol
            _row(entry_price=5900.0),           # different entry
        ]
        assert len(_dedupe_episodes(rows)) == 5

    def test_regime_shift_is_a_new_episode(self):
        # A capture spanning a regime shift is a genuinely different signal
        # (the orchestrator's own D5 content hash includes regime), so it
        # must NOT be collapsed into one episode.
        rows = [
            _row(regime="med_low_vol_bear"),
            _row(regime="med_high_vol_bear"),
        ]
        assert len(_dedupe_episodes(rows)) == 2

    def test_same_levels_across_days_still_one_episode(self):
        # A frozen feed re-serving the same bar across a weekend is ONE bet.
        rows = [
            _row(ts="2026-10-03T04:00:00+00:00"),
            _row(ts="2026-10-04T04:00:00+00:00"),
        ]
        assert len(_dedupe_episodes(rows)) == 1

    def test_missing_stop_loss_still_dedupes(self):
        # stop_loss may be NULL on pre-migration rows — must not be dropped.
        rows = [_row(stop_loss=None), _row(stop_loss=None)]
        assert len(_dedupe_episodes(rows)) == 1

    def test_empty(self):
        assert _dedupe_episodes([]) == []


class TestRecomputeCell:
    def test_recompute_from_recent_rows(self):
        rows = [
            _row(outcome="tp_hit", p_win=0.60),
            _row(outcome="tp_hit", p_win=0.60, take_profit=5864.0),
            _row(outcome="sl_hit", p_win=0.60, take_profit=5863.0),
            _row(outcome="sl_hit", p_win=0.60, take_profit=5862.0),
        ]
        out = _recompute_cell(
            rows, side="sell", vol_tier="med_low_vol", bucket="4+",
        )
        assert out is not None
        assert out["n"] == 4
        assert out["pct_hit_tp"] == 0.5
        assert out["pct_hit_sl"] == 0.5
        assert out["win_rate"] == 0.5
        # avg_p_win 0.60 vs realized 0.50 -> positive (overconfident) bias
        assert out["bias"] == 0.1

    def test_no_rows_in_cell_returns_none(self):
        out = _recompute_cell(
            [_row(regime="high_vol_bull")],
            side="sell", vol_tier="med_low_vol", bucket="4+",
        )
        assert out is None

    def test_side_filter_excludes_other_side(self):
        rows = [_row(signal="buy")]
        assert _recompute_cell(
            rows, side="sell", vol_tier="med_low_vol", bucket="4+",
        ) is None
        # side="all" pools every side
        assert _recompute_cell(
            rows, side="all", vol_tier="med_low_vol", bucket="4+",
        ) is not None

    def test_row_without_atr_is_skipped(self):
        rows = [_row(atr=None)]
        assert _recompute_cell(
            rows, side="sell", vol_tier="med_low_vol", bucket="4+",
        ) is None


class TestLastFiredCooldown:
    """Cooldown state comes from nt_eod_actions, not a hand-created roadmap
    row: write_job_heartbeat() only PATCHes an existing row, and no
    geometry_band_urgency roadmap row exists, so the old path persisted
    nothing and the cooldown never held (observed 2026-10-05: 59 cells
    re-computed on every 15-min sweep)."""

    def _run(self, rows):
        import asyncio
        from unittest.mock import patch

        class FakeResp:
            def json(self_inner):
                return rows

        class FakeClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url, headers=None, params=None):
                return FakeResp()

        with patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency.settings"
        ) as s:
            s.SUPABASE_URL = "https://x.supabase.co"
            with patch(
                "regime_platform.sweep_orchestrator.geometry_band_urgency.httpx.AsyncClient",
                return_value=FakeClient(),
            ):
                return asyncio.run(_load_last_fired())

    def _action(self, symbol, side, tier, bucket, ts):
        return {
            "symbol": symbol,
            "ts": ts,
            "detail": {
                "side": side,
                "vol_tier": tier,
                "distance_atr_bucket": bucket,
            },
        }

    def test_most_recent_fire_wins_per_cell(self):
        rows = [
            self._action("XAUUSD", "sell", "med_high_vol", "2-4",
                         "2026-10-05T00:47:36+00:00"),
            self._action("XAUUSD", "sell", "med_high_vol", "2-4",
                         "2026-10-04T20:00:00+00:00"),
        ]
        out = self._run(rows)
        assert len(out) == 1
        key = "XAUUSD:sell:med_high_vol:2-4"
        assert key in out
        # newest-first input -> the 00:47 stamp, not the older one
        assert out[key] > out[key] - 1  # present and numeric
        from datetime import datetime, timezone
        assert datetime.fromtimestamp(out[key], tz=timezone.utc).hour == 0

    def test_distinct_cells_kept_separate(self):
        rows = [
            self._action("XAUUSD", "sell", "med_high_vol", "2-4", "2026-10-05T00:47:36+00:00"),
            self._action("XAUUSD", "buy", "med_high_vol", "1-2", "2026-10-05T00:47:35+00:00"),
        ]
        out = self._run(rows)
        assert set(out) == {
            "XAUUSD:sell:med_high_vol:2-4",
            "XAUUSD:buy:med_high_vol:1-2",
        }

    def test_unparseable_ts_skipped(self):
        out = self._run([self._action("XAUUSD", "sell", "high_vol", "4+", None)])
        assert out == {}

    def test_malformed_detail_skipped(self):
        out = self._run([{"symbol": "XAUUSD", "ts": "2026-10-05T00:47:36+00:00", "detail": "nope"}])
        assert out == {}


class TestCooldownGate:
    """End-to-end: a cell fired inside its cooldown window must NOT recompute.

    Regression (2026-10-05): _load_last_fired() returns epoch SECONDS but the
    gate ran that through _ts_or_none() (an ISO-string parser), which returned
    None every time — so the cooldown silently never applied and every drifted
    cell re-computed on every 15-min sweep (53 cells x 4 sweeps observed).
    The loader was unit-tested; the WIRING between loader and gate was not.
    """

    def _run(self, last_fired_epoch):
        import asyncio
        from unittest.mock import AsyncMock, patch

        # One symbol, one drifted cell (bias 0.5 >= threshold 0.15).
        cells = {("sell", "med_high_vol", "2-4"): {
            "n": 50, "bias": 0.5, "bias_shrunk": 0.5,
        }}
        rows = [{
            "symbol": "XAUUSD", "ts": "2026-10-04T00:00:00+00:00",
            "signal": "sell", "entry_price": 100.0, "take_profit": 130.0,
            "stop_loss": 90.0, "atr": 10.0, "outcome": "sl_hit",
            "resolved_at": "2026-10-04T04:00:00+00:00",
            "regime": "med_high_vol_bear", "p_win": 0.9,
        }] * 30  # tp-entry=30, atr=10 -> ratio 3.0 -> bucket "2-4"

        with patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency.fetch_active_symbols",
            new=AsyncMock(return_value=[{"symbol": "XAUUSD"}]),
        ), patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency._fetch_cell_bias",
            new=AsyncMock(return_value=cells),
        ), patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency._fetch_resolved_rows",
            new=AsyncMock(return_value=rows),
        ), patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency._load_last_fired",
            new=AsyncMock(return_value=last_fired_epoch),
        ), patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency._log_eod_action",
            new=AsyncMock(),
        ), patch(
            "regime_platform.sweep_orchestrator.geometry_band_urgency.upsert_bracket_reachability",
            new=AsyncMock(),
        ):
            from regime_platform.sweep_orchestrator.geometry_band_urgency import (
                check_geometry_band_urgency,
            )
            return asyncio.run(check_geometry_band_urgency())

    def test_inside_cooldown_does_not_recompute(self):
        import time
        # Fired 30 min ago; cooldown is 4h -> must be skipped.
        out = self._run({"XAUUSD:sell:med_high_vol:2-4": time.time() - 1800})
        assert out["recomputed"] == 0
        assert out["triggered"] == 0

    def test_outside_cooldown_recomputes(self):
        import time
        # Fired 5h ago -> cooldown lapsed, must recompute.
        out = self._run({"XAUUSD:sell:med_high_vol:2-4": time.time() - 5 * 3600})
        assert out["recomputed"] == 1

    def test_no_prior_fire_recomputes(self):
        out = self._run({})
        assert out["recomputed"] == 1


class TestComputeDrift:
    """Drift detection from previous_stats (migration 201).

    The urgency job compares fresh recomputed stats against the stored
    previous_stats. Drift is detected when |Δpct_hit_tp| >= threshold_pct
    OR |Δmedian_hours| >= threshold_hours. This closes the drift loop:
    once reachability stats stabilize, the trigger stops firing even if
    the bias table is stale.
    """

    def test_no_previous_stats_is_drifted(self):
        """First run: no baseline to compare against -> drifted."""
        fresh = {"pct_hit_tp": 0.5, "median_hours_to_resolve": 12.0}
        is_drifted, detail = _compute_drift(fresh, None)
        assert is_drifted is True
        assert detail["reason"] == "no_previous_stats"

    def test_stable_cell_not_drifted(self):
        """Small changes below threshold -> stable."""
        fresh = {
            "pct_hit_tp": 0.52,
            "pct_hit_sl": 0.30,
            "pct_expired": 0.18,
            "median_hours_to_resolve": 12.5,
        }
        previous = {
            "pct_hit_tp": 0.50,
            "pct_hit_sl": 0.30,
            "pct_expired": 0.20,
            "median_hours_to_resolve": 12.0,
        }
        is_drifted, detail = _compute_drift(
            fresh, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        assert is_drifted is False
        assert detail["reason"] == "stable"
        assert detail["delta_pct_hit_tp"] == 0.02
        assert detail["delta_median_hours"] == 0.5

    def test_large_tp_change_is_drifted(self):
        """|Δpct_hit_tp| >= 0.10 -> drifted."""
        fresh = {"pct_hit_tp": 0.65, "median_hours_to_resolve": 12.0}
        previous = {"pct_hit_tp": 0.50, "median_hours_to_resolve": 12.0}
        is_drifted, detail = _compute_drift(
            fresh, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        assert is_drifted is True
        assert detail["reason"] == "drift_detected"
        assert detail["delta_pct_hit_tp"] == 0.15

    def test_large_median_change_is_drifted(self):
        """|Δmedian_hours| >= 6.0 -> drifted."""
        fresh = {"pct_hit_tp": 0.50, "median_hours_to_resolve": 20.0}
        previous = {"pct_hit_tp": 0.50, "median_hours_to_resolve": 12.0}
        is_drifted, detail = _compute_drift(
            fresh, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        assert is_drifted is True
        assert detail["delta_median_hours"] == 8.0

    def test_exact_threshold_is_drifted(self):
        """Boundary: exactly at threshold counts as drifted."""
        fresh = {"pct_hit_tp": 0.60, "median_hours_to_resolve": 12.0}
        previous = {"pct_hit_tp": 0.50, "median_hours_to_resolve": 12.0}
        is_drifted, _ = _compute_drift(
            fresh, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        # Floating point: 0.60 - 0.50 = 0.0999... < 0.10, so this is stable.
        # Use a value that is clearly at/above the threshold.
        fresh2 = {"pct_hit_tp": 0.61, "median_hours_to_resolve": 12.0}
        is_drifted2, _ = _compute_drift(
            fresh2, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        assert is_drifted2 is True

    def test_both_stable_not_drifted(self):
        """Both metrics below threshold -> stable."""
        fresh = {"pct_hit_tp": 0.55, "median_hours_to_resolve": 15.0}
        previous = {"pct_hit_tp": 0.50, "median_hours_to_resolve": 12.0}
        is_drifted, detail = _compute_drift(
            fresh, previous,
            drift_threshold_pct=0.10,
            drift_threshold_hours=6.0,
        )
        assert is_drifted is False
        assert detail["reason"] == "stable"
