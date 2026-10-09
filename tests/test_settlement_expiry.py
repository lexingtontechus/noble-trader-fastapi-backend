"""Per-signal dynamic settlement expiry — pure math + shadow wiring.

worklog/20261009_P90_HOURS_LIVE_MODE.md
"""

from __future__ import annotations

from regime_platform.sweep_orchestrator.settlement_expiry import (
    dynamic_expiry_hours,
    hours_to_bars,
)


class TestDynamicExpiryHours:
    def _row(self, *, bucket, n=100, median=4.0, p90=50.0):
        return {
            "distance_atr_bucket": bucket, "n": n,
            "median_hours_to_resolve": median, "p90_hours": p90,
        }

    def test_no_row_fails_open(self):
        assert dynamic_expiry_hours(None) is None

    def test_insufficient_n_fails_open(self):
        # n below the min_n floor → None (caller uses fixed 48).
        assert dynamic_expiry_hours(self._row(bucket="1-2", n=5)) is None

    def test_tight_bucket_uses_median_times_safety(self):
        # median 4.3h * 2.0 safety = 8.6h, within [floor=12, cap] → floored to 12.
        h = dynamic_expiry_hours(self._row(bucket="1-2", median=4.3, p90=23.0))
        assert h == 12.0  # floor clamps the 8.6h up to 12h

    def test_tight_bucket_median_scales_above_floor(self):
        # median 12h * 2.0 = 24h — above floor, below cap.
        h = dynamic_expiry_hours(self._row(bucket="2-4", median=12.0, p90=40.0))
        assert h == 24.0

    def test_wide_bucket_uses_p90(self):
        # 4+ bucket → p90, not median*safety. median 41, p90 56 → 56.
        h = dynamic_expiry_hours(self._row(bucket="4+", median=41.0, p90=56.0))
        assert h == 56.0

    def test_wide_bucket_p90_capped_at_72(self):
        h = dynamic_expiry_hours(self._row(bucket="4+", median=41.0, p90=90.0))
        assert h == 72.0  # cap

    def test_floor_clamps_low(self):
        h = dynamic_expiry_hours(
            self._row(bucket="4+", median=1.0, p90=2.0),
            floor_hours=12.0,
        )
        assert h == 12.0

    def test_zero_median_falls_back_to_p90(self):
        h = dynamic_expiry_hours(self._row(bucket="1-2", median=0, p90=30.0))
        assert h == 30.0

    def test_custom_safety_mult(self):
        # median 6h * 3.0 = 18h.
        h = dynamic_expiry_hours(
            self._row(bucket="1-2", median=6.0, p90=40.0),
            tight_safety_mult=3.0,
        )
        assert h == 18.0


class TestHoursToBars:
    def test_none_falls_back_to_fixed(self):
        assert hours_to_bars(None, fixed_bars=48) == 48

    def test_converts_hours_to_bars_ceil(self):
        assert hours_to_bars(8.6, fixed_bars=48) == 9

    def test_upper_clamp_at_1_5x_fixed(self):
        # 90h would be 90 bars, clamped to 48*1.5=72.
        assert hours_to_bars(90.0, fixed_bars=48) == 72

    def test_minimum_one_bar(self):
        assert hours_to_bars(0.1, fixed_bars=48) == 1


class TestFlagDefaults:
    def test_ships_fixed(self):
        from regime_platform.core.feature_flags import (
            get_settlement_expiry_mode,
            get_settlement_expiry_floor_hours,
            get_settlement_expiry_cap_hours,
            get_settlement_expiry_tight_safety_mult,
        )
        # fixed = byte-identical to today's hard-coded EXPIRY_BARS=48.
        assert get_settlement_expiry_mode() == "fixed"
        assert get_settlement_expiry_floor_hours() == 12.0
        assert get_settlement_expiry_cap_hours() == 72.0
        assert get_settlement_expiry_tight_safety_mult() == 2.0
