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
