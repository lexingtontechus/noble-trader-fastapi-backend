"""Tests for the rolling calibration-bias window (intraday cadence plan, C)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.supabase_client import fetch_calibration_bias


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


def _rows(n, wins, pred=0.90, spread_days=7):
    """``n`` fake calibration rows, ``wins`` of them wins, spread evenly
    across ``min(spread_days, n)`` distinct calendar days (2026-08-25: the
    window now applies a clustering discount below
    EOD_BIAS_MIN_DISTINCT_DAYS=3 and recency decay weighting — spreading
    across enough distinct days, with each day's win count proportional to
    its share of the rows, keeps these pre-existing tests exercising
    window/threshold logic unaffected by either mechanism)."""
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
        entered_at = (now - timedelta(days=day)).isoformat()
        for j in range(size):
            out.append({
                "predicted_p_win": pred,
                "outcome": "win" if j < wins_in_chunk else "loss",
                "entered_at": entered_at,
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
    """Regression coverage for a live incident (2026-08-25): USDCAD's
    stagnation_kill_switch fired off n=69 calibration records that were ALL
    written within one ~6-hour stretch (2026-08-23 00:40-06:35) — a burst,
    not a representative week — counted with the same confidence as 69
    records spread evenly over the full 7-day window. See
    worklog/20260825_US500_USDCAD_TIME_HORIZON_SCOPE.md."""

    def _clustered_rows(self, n, wins, pred, hours_span=6.0):
        """All ``n`` rows squeezed into a single ``hours_span``-hour window
        — the exact USDCAD pathology (one narrow burst), not spread across
        distinct days. Anchored to noon "today" (UTC) rather than the raw
        current time so a `hours_span` up to ~11h can never spuriously
        straddle a midnight calendar-day boundary regardless of when this
        test actually runs."""
        anchor = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        out = []
        for i in range(n):
            offset_hours = (i / max(n - 1, 1)) * hours_span
            out.append({
                "predicted_p_win": pred,
                "outcome": "win" if i < wins else "loss",
                "entered_at": (anchor - timedelta(hours=offset_hours)).isoformat(),
            })
        return out

    def test_usdcad_style_burst_is_discounted_below_raw_count(self):
        """69 losses, 0 wins, all within 6 hours (1 distinct day) — n fed
        to the Bayesian shrink must be scaled down well below the raw 69,
        not treated as a full week's worth of evidence."""
        rows = self._clustered_rows(69, wins=0, pred=0.98, hours_span=6.0)
        result, _ = _run(rows=rows, min_records=20)

        assert result is not None
        assert result["raw_n"] == 69
        assert result["distinct_days"] == 1
        # min_distinct_days default is 3 → n should be scaled to ~69 * (1/3)
        assert result["n"] == pytest.approx(69 * (1 / 3), rel=0.05)
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
        shares the same entered_at (age=0 for all), decay weighting must be
        a pure no-op — the plain unweighted average — isolating that the
        discount in test_usdcad_style_burst_is_discounted_below_raw_count
        comes from clustering (distinct_days), not decay."""
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            {"predicted_p_win": 0.90, "outcome": "win" if i < 15 else "loss", "entered_at": now_iso}
            for i in range(30)
        ]
        result, _ = _run(rows=rows, min_records=20)
        assert result is not None
        assert result["bias"] == pytest.approx(0.40, abs=1e-4)
