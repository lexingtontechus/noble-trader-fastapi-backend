"""Tests for the rolling calibration-bias window (intraday cadence plan, C)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.supabase_client import (
    _decay_weighted_episode_stats,
    _episode_summaries,
    _group_into_episodes,
    fetch_calibration_bias,
)


class FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.captured = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.captured = params
        return FakeResp(self.rows)


def _run(symbol="EURUSD", rows=None, **kwargs):
    client = FakeClient(rows if rows is not None else [])
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._url",
            return_value="https://x.supabase.co/rest/v1/nt_calibration_records",
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        result = asyncio.run(fetch_calibration_bias(symbol, **kwargs))
    return result, client


def _rows(n, wins, pred=0.90, spread_days=7, hold_hours=1.0):
    """``n`` fake calibration rows, ``wins`` of them wins, spread evenly
    across ``min(spread_days, n)`` distinct calendar days AND staggered
    within each day (2026-09-08: rows now carry a realistic ``closed_at``
    — ``entered_at + hold_hours`` — and are spaced ``2 * hold_hours``
    apart within a day, so genuinely-spread-out rows form their OWN
    independent episode each under the overlap-merge algorithm
    (``_group_into_episodes``) rather than artificially collapsing to one
    shared-instant episode per day. This keeps ``n`` (now a real episode
    count, not a distinct-days-scaled approximation) matching ``raw_n``
    for data that is genuinely spread out — see
    ``TestClusteringGuardAndDecayWeighting`` for the burst case where rows
    overlap and DO collapse."""
    if n <= 0:
        return []
    days = max(min(spread_days, n), 1)
    base, extra = divmod(n, days)
    chunk_sizes = [base + 1 if d < extra else base for d in range(days)]
    cumulative = 0
    win_boundaries = [0]
    for size in chunk_sizes:
        cumulative += size
        win_boundaries.append(round(wins * cumulative / n))
    out = []
    now = datetime.now(timezone.utc)
    for day, size in enumerate(chunk_sizes):
        wins_in_chunk = win_boundaries[day + 1] - win_boundaries[day]
        for j in range(size):
            entered_dt = now - timedelta(days=day) - timedelta(hours=j * hold_hours * 2)
            closed_dt = entered_dt + timedelta(hours=hold_hours)
            out.append({
                "predicted_p_win": pred,
                "outcome": "win" if j < wins_in_chunk else "loss",
                "entered_at": entered_dt.isoformat(),
                "closed_at": closed_dt.isoformat(),
            })
    return out


class TestRollingWindow:
    def test_default_window_adds_entered_at_filter(self):
        # 30 records, 15 wins → bias ≈ 0.90 - 0.50 = 0.40 (loose tolerance:
        # recency decay weighting, 2026-08-25, introduces small rounding
        # noise from _rows()'s day-chunking when n isn't evenly divisible
        # by spread_days — see TestDecayWeightingAndClusteringGuard for
        # exact weighting behavior).
        result, client = _run(rows=_rows(30, 15))
        assert result is not None
        assert result["n"] == 30
        assert result["bias"] == pytest.approx(0.40, abs=0.02)
        assert "entered_at" in client.captured
        assert client.captured["entered_at"].startswith("gte.")
        assert "symbol" in client.captured

    def test_window_days_zero_is_all_time(self):
        _, client = _run(rows=_rows(25, 20), window_days=0)
        assert "entered_at" not in client.captured

    def test_explicit_window_days_used(self):
        _, client = _run(rows=_rows(25, 20), window_days=3)
        assert client.captured["entered_at"].startswith("gte.")
        # cutoff should be ~3 days ago
        cutoff = client.captured["entered_at"][4:]
        from datetime import timedelta

        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cutoff)).total_seconds()
        assert 2.5 * 86400 < age < 3.5 * 86400

    def test_below_min_records_returns_none(self):
        result, _ = _run(rows=_rows(19, 10), min_records=20)
        assert result is None

    def test_default_min_records_uses_settings(self):
        # settings.EOD_BIAS_MIN_RECORDS default 20 — 19 rows → None
        result, _ = _run(rows=_rows(19, 10))
        assert result is None

    def test_venue_filter_preserved(self):
        _, client = _run(rows=_rows(25, 20), venue="OANDA")
        assert client.captured["venue"] == "eq.OANDA"

    def test_calibrated_status(self):
        # 25 records, 20 wins → win_rate 0.80 vs pred 0.82 → bias +0.02 → CALIBRATED
        result, _ = _run(rows=_rows(25, 20, pred=0.82))
        assert result["status"] == "CALIBRATED"

    def test_underconfident_status(self):
        # 25 records, 24 wins → win_rate 0.96 vs pred 0.80 → bias -0.16 → UNDERCONFIDENT
        result, _ = _run(rows=_rows(25, 24, pred=0.80))
        assert result["status"] == "UNDERCONFIDENT"


class TestClusteringGuardAndDecayWeighting:
    """Regression coverage for two live incidents:

    2026-08-25 (USDCAD): ``stagnation_kill_switch`` fired off n=69
    calibration records that were ALL written within one ~6-hour stretch
    (2026-08-23 00:40-06:35) — a burst, not a representative week —
    counted with the same confidence as 69 records spread evenly over the
    full 7-day window. See worklog/20260825_US500_USDCAD_TIME_HORIZON_SCOPE.md.
    The original fix (a distinct-calendar-days discount) is superseded
    2026-09-08 by real episode counting (below), because:

    2026-09-08 (USDCHF): the distinct-days discount missed a burst that
    happened to span exactly its own default threshold of 3 calendar
    dates while still being one continuous ~68-hour overlapping episode
    (294 raw records, 1 real episode) — see worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md. Episode counting
    (``_group_into_episodes``, merging overlapping ``[entered_at,
    closed_at]`` windows) catches both incidents by construction, not by
    a calendar-day proxy that can be evaded."""

    def _clustered_rows(self, n, wins, pred, hours_span=6.0, hold_hours=1.0):
        """All ``n`` rows squeezed into a single ``hours_span``-hour window
        — the exact USDCAD/USDCHF pathology (one narrow, overlapping
        burst), not spread across distinct days. Each row holds for
        ``hold_hours`` (default 1h, realistic vs. the ~5min entry cadence
        this represents) so consecutive rows' ``[entered_at, closed_at]``
        windows genuinely overlap under ``_group_into_episodes`` — without
        a realistic hold duration, zero-duration rows at strictly
        increasing instants would never overlap at all, defeating the
        point of this fixture. Anchored to noon "today" (UTC) rather than
        the raw current time so a `hours_span` up to ~11h can never
        spuriously straddle a midnight calendar-day boundary regardless of
        when this test actually runs."""
        anchor = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        out = []
        for i in range(n):
            offset_hours = (i / max(n - 1, 1)) * hours_span
            entered_dt = anchor - timedelta(hours=offset_hours)
            closed_dt = entered_dt + timedelta(hours=hold_hours)
            out.append({
                "predicted_p_win": pred,
                "outcome": "win" if i < wins else "loss",
                "entered_at": entered_dt.isoformat(),
                "closed_at": closed_dt.isoformat(),
            })
        return out

    def test_usdcad_style_burst_collapses_to_one_episode_and_is_vetoed(self):
        """69 losses, 0 wins, all within 6 hours with overlapping 1h holds
        — the entire burst is ONE continuous episode, not 69 (or a
        fractionally-discounted ~23, the old distinct-days approximation).
        With the default ``calibration_bias_min_episodes=2``, one episode
        is not enough evidence at all — the function must return None
        (the SAME "insufficient data" signal as too few raw records),
        stronger and more correct than the old partial discount."""
        rows = self._clustered_rows(69, wins=0, pred=0.98, hours_span=6.0)
        result, _ = _run(rows=rows, min_records=20)

        assert result is None

    def test_usdcad_style_burst_episode_math_directly(self):
        """Same burst, inspected with min_episodes=1 (bypassing the
        insufficient-data veto) to confirm the underlying episode count
        and bias math: raw_n=69 real rows collapse to exactly 1 episode."""
        rows = self._clustered_rows(69, wins=0, pred=0.98, hours_span=6.0)
        result, _ = _run(rows=rows, min_records=20, min_episodes=1)

        assert result is not None
        assert result["raw_n"] == 69
        assert result["distinct_days"] == 1
        assert result["n"] == 1
        assert result["n"] < result["raw_n"]

    def test_records_spread_across_enough_days_are_not_discounted(self):
        """The exact same 69-loss/0-win composition, but spread across a
        full week instead of one burst — n must NOT be discounted, since
        this really is a representative week's worth of evidence."""
        rows = _rows(69, wins=0, pred=0.98, spread_days=7)
        result, _ = _run(rows=rows, min_records=20)

        assert result is not None
        assert result["distinct_days"] >= 3
        assert result["n"] == pytest.approx(result["raw_n"], rel=1e-6)

    def test_decay_weighting_favors_recent_records_over_stale_ones(self):
        """A window with a stale majority (bad, old) and a fresh minority
        (good, recent) — decay weighting must pull the computed bias
        toward the RECENT evidence more than a plain unweighted average
        would, so a bad week doesn't linger at full strength right up until
        it drops off the 7-day cliff."""
        now = datetime.now(timezone.utc)
        stale_bad = [
            {
                "predicted_p_win": 0.95,
                "outcome": "loss",
                "entered_at": (now - timedelta(days=6, hours=12)).isoformat(),
            }
            for _ in range(50)
        ]
        fresh_good = [
            {
                "predicted_p_win": 0.95,
                "outcome": "win",
                "entered_at": (now - timedelta(hours=1)).isoformat(),
            }
            for _ in range(10)
        ]
        rows = stale_bad + fresh_good
        result, _ = _run(rows=rows, min_records=20)

        unweighted_win_rate = 10 / 60  # what a plain average would give
        assert result is not None
        assert result["win_rate"] > unweighted_win_rate + 0.05, (
            f"expected decay weighting to pull win_rate above the plain "
            f"average of {unweighted_win_rate:.3f}, got {result['win_rate']}"
        )

    def test_all_records_same_instant_has_no_decay_effect(self):
        """Sanity check on the weighting formula itself: when every record
        shares the same entered_at (age=0 for all — and therefore the same
        episode, since none starts after another's close), decay weighting
        must be a pure no-op — the plain unweighted average.
        ``min_episodes=1`` isolates the decay-formula check from the
        (correct, separately-tested) episode-count veto: 30 rows sharing
        one instant are genuinely ONE episode, which is exactly what this
        test wants to average over, not treat as insufficient."""
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            {"predicted_p_win": 0.90, "outcome": "win" if i < 15 else "loss", "entered_at": now_iso}
            for i in range(30)
        ]
        result, _ = _run(rows=rows, min_records=20, min_episodes=1)
        assert result is not None
        assert result["n"] == 1
        assert result["bias"] == pytest.approx(0.40, abs=1e-4)


class TestGroupIntoEpisodes:
    """Direct tests of the pure interval-merge function (worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md)."""

    def _row(self, entered, closed=None, outcome="loss", pred=0.5):
        row = {
            "predicted_p_win": pred,
            "outcome": outcome,
            "entered_at": entered.isoformat(),
        }
        if closed is not None:
            row["closed_at"] = closed.isoformat()
        return row

    def test_non_overlapping_rows_are_separate_episodes(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            self._row(base, base + timedelta(hours=1)),
            self._row(base + timedelta(hours=2), base + timedelta(hours=3)),
            self._row(base + timedelta(hours=4), base + timedelta(hours=5)),
        ]
        episodes = _group_into_episodes(rows)
        assert len(episodes) == 3
        assert all(len(ep) == 1 for ep in episodes)

    def test_overlapping_rows_merge_into_one_episode(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Long-holding first row; three more entered while it's still open.
        rows = [
            self._row(base, base + timedelta(hours=40)),
            self._row(base + timedelta(minutes=5), base + timedelta(hours=1)),
            self._row(base + timedelta(minutes=10), base + timedelta(hours=1)),
            self._row(base + timedelta(hours=39), base + timedelta(hours=41)),
        ]
        episodes = _group_into_episodes(rows)
        assert len(episodes) == 1
        assert len(episodes[0]) == 4

    def test_running_max_not_just_immediately_preceding_row(self):
        """The exact edge case a naive LAG(closed_at)-on-previous-row
        algorithm gets wrong: row A holds long (0h-10h); row B is short
        and closes early (0h05-0h10); row C enters at 5h, AFTER B already
        closed but still WITHIN A's still-open window. A running-max merge
        correctly keeps A and C in the same episode; a plain
        immediately-preceding-row check would wrongly start a new episode
        at C because it only looks at B's (already-closed) end time."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        row_a = self._row(base, base + timedelta(hours=10))
        row_b = self._row(base + timedelta(minutes=5), base + timedelta(minutes=10))
        row_c = self._row(base + timedelta(hours=5))  # closed_at falls back to entered_at
        episodes = _group_into_episodes([row_a, row_b, row_c])
        assert len(episodes) == 1
        assert len(episodes[0]) == 3

    def test_missing_entered_at_is_dropped(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [self._row(base), {"predicted_p_win": 0.5, "outcome": "win"}]
        episodes = _group_into_episodes(rows)
        assert sum(len(ep) for ep in episodes) == 1

    def test_missing_closed_at_falls_back_to_zero_duration(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # No closed_at on either row, entered 1 minute apart -> each is its
        # own zero-duration episode (conservative: never merges forward).
        rows = [self._row(base), self._row(base + timedelta(minutes=1))]
        episodes = _group_into_episodes(rows)
        assert len(episodes) == 2


class TestEpisodeSummariesAndDecayWeighting:
    def test_episode_summary_averages_within_episode(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        episode = [
            {"predicted_p_win": 0.8, "outcome": "win", "entered_at": base.isoformat()},
            {"predicted_p_win": 0.6, "outcome": "loss", "entered_at": (base + timedelta(minutes=5)).isoformat()},
        ]
        summaries = _episode_summaries([episode])
        assert len(summaries) == 1
        assert summaries[0]["predicted_p_win"] == pytest.approx(0.7)
        assert summaries[0]["win_fraction"] == pytest.approx(0.5)
        assert summaries[0]["entered_at"] == base.isoformat()

    def test_episode_summary_raw_pred_is_none_without_raw(self):
        """Batch B: missing raw stays None — never silently becomes enforced."""
        episode = [
            {"predicted_p_win": 0.7, "predicted_p_win_raw": None, "outcome": "win", "entered_at": "2026-01-01T00:00:00+00:00"},
        ]
        summaries = _episode_summaries([episode])
        assert summaries[0]["predicted_p_win_raw"] is None
        assert summaries[0]["n_raw_present"] == 0
        assert summaries[0]["predicted_p_win"] == pytest.approx(0.7)

    def test_decay_weighted_episode_stats_favors_recent_episode(self):
        now = datetime.now(timezone.utc)
        stale = {"entered_at": (now - timedelta(days=6)).isoformat(), "predicted_p_win": 0.9, "win_fraction": 0.0}
        fresh = {"entered_at": (now - timedelta(hours=1)).isoformat(), "predicted_p_win": 0.9, "win_fraction": 1.0}
        avg_pred, win_rate = _decay_weighted_episode_stats([stale, fresh], "predicted_p_win", 3.0, now)
        assert win_rate > 0.5  # unweighted average would be exactly 0.5
