"""
Tests for the Renko brick capture (nt_renko_bricks).

Covers:
  - log_renko_bricks builds rows matching the real table schema
    (brick_id serial PK; unique (symbol, brick_index, session_date,
    brick_size) as of migration 145)
  - missing Supabase config → returns silently (never raises)
  - empty brick list → skips without touching the network
  - row mapping: direction/open/close/high/low/brick_index/ts
  - real per-bar timestamps flow through build_from_prices ->
    Brick.to_dict() -> the persisted row (2026-09-09, worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 1")
  - the sweep_ts fallback fires ONLY when has_real_timestamps=False, not
    as the silent default
  - on_conflict now includes brick_size (migration 145 "Finding 2")
"""

import time
from datetime import datetime

import pytest

from regime_platform.sweep_orchestrator.supabase_client import log_renko_bricks


@pytest.mark.asyncio
async def test_log_renko_bricks_missing_config_is_silent(monkeypatch):
    """No SUPABASE_URL configured → returns silently (never raises)."""
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_URL",
        "",
    )
    result = await log_renko_bricks(
        "XAUUSD",
        [{"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0}],
        brick_size=1.0,
    )
    assert result is None


@pytest.mark.asyncio
async def test_log_renko_bricks_empty_list_skips(monkeypatch):
    """Empty brick list → logged + skipped without touching the network."""
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_URL",
        "https://test.supabase.co",
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_KEY",
        "test-key",
    )
    result = await log_renko_bricks("XAUUSD", [], brick_size=1.0)
    assert result is None


def test_log_renko_bricks_row_mapping():
    """Brick dict → table row uses the real nt_renko_bricks column names."""
    # The function builds rows internally; we can't call the private builder
    # directly, but we CAN assert the capture module imports and the schema
    # whitelist matches the real table (brick_index, not sweep_timestamp).
    from regime_platform.sweep_orchestrator.supabase_client import NT_RENKO_BRICKS_COLUMNS
    required = {
        "symbol", "direction", "brick_size", "open_price", "close_price",
        "high", "low", "brick_index", "ts",
    }
    missing = required - set(NT_RENKO_BRICKS_COLUMNS)
    assert not missing, f"missing from whitelist: {missing}"
    # The table has NO sweep_timestamp — the original 108 error (42703).
    assert "sweep_timestamp" not in NT_RENKO_BRICKS_COLUMNS


def test_brick_engine_builds_series_matching_row_shape():
    """RenkoBrickEngine produces dicts with the fields log_renko_bricks maps."""
    from renko.brick_engine import RenkoBrickEngine
    engine = RenkoBrickEngine(brick_size=1.0)
    bricks = engine.build_from_prices([100, 101, 102, 101, 100, 99, 100, 103])
    assert len(bricks) >= 1
    d = bricks[0].to_dict()
    for key in ("index", "direction", "open_price", "close_price", "high", "low", "timestamp", "brick_size"):
        assert key in d


@pytest.mark.asyncio
async def test_log_renko_bricks_uses_on_conflict(monkeypatch):
    """The POST URL must carry on_conflict so re-sweeps UPDATE, not grow.

    Regression guard: the table's PK is serial brick_id, so without
    ?on_conflict=symbol,brick_index,session_date PostgREST resolves
    merge-duplicates on brick_id → always INSERT → 409 on re-sweep and
    unbounded growth. Verified live 2026-08-07 (12k dup rows before fix).
    """
    import httpx
    from regime_platform.sweep_orchestrator.supabase_client import log_renko_bricks

    captured = {}

    class FakeResp:
        is_success = True
        status_code = 201
        text = ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_URL",
        "https://test.supabase.co",
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_KEY",
        "test-key",
    )

    await log_renko_bricks(
        "XAUUSD",
        [{"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0}],
        brick_size=1.0,
        session_date="2026-08-07",
    )
    assert "on_conflict=symbol,brick_index,session_date,brick_size" in captured.get("url", "")
    assert captured["json"][0]["brick_index"] == 0
    assert captured["json"][0]["session_date"] == "2026-08-07"
    assert captured["headers"]["Prefer"] == "resolution=merge-duplicates"


# ─── Real-timestamp threading (2026-09-09, worklog §7 "Finding 1") ─────────


def _fake_post_capture(monkeypatch):
    """Patch httpx.AsyncClient.post to capture the POSTed rows; returns the
    ``captured`` dict the test can inspect after awaiting log_renko_bricks."""
    import httpx

    captured = {}

    class FakeResp:
        is_success = True
        status_code = 201
        text = ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_URL",
        "https://test.supabase.co",
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.settings.SUPABASE_KEY",
        "test-key",
    )
    return captured


@pytest.mark.asyncio
async def test_build_from_prices_with_real_timestamps_lands_in_persisted_row(monkeypatch):
    """End-to-end: build_from_prices(prices, timestamps=real) -> Brick.to_dict()
    -> log_renko_bricks(has_real_timestamps=True) persists the REAL epoch,
    not a fabricated sweep_ts reconstruction."""
    from renko.brick_engine import RenkoBrickEngine

    captured = _fake_post_capture(monkeypatch)

    prices = [100.0, 101.0, 102.0]
    real_timestamps = [1_800_000_000.0, 1_800_003_600.0, 1_800_007_200.0]
    engine = RenkoBrickEngine(brick_size=1.0)
    bricks = engine.build_from_prices(prices, timestamps=real_timestamps)
    assert bricks, "expected at least one brick from a 3-unit price move"

    # sweep_ts is deliberately far from the real timestamps — if the
    # fallback ever fired silently, the persisted ts would land near "now"
    # instead of near the real_timestamps values, and this assertion would
    # catch it.
    await log_renko_bricks(
        "XAUUSD",
        [b.to_dict() for b in bricks],
        brick_size=1.0,
        sweep_ts=0.0,
        has_real_timestamps=True,
    )

    persisted_ts = [
        datetime.fromisoformat(row["ts"]).timestamp() for row in captured["json"]
    ]
    real_ts_used = [b.to_dict()["timestamp"] for b in bricks]
    assert persisted_ts == pytest.approx(real_ts_used)
    # None of the real 2026-era timestamps should have collapsed to the
    # epoch-0-anchored fallback range.
    assert all(ts > 1_000_000_000 for ts in persisted_ts)


@pytest.mark.asyncio
async def test_fallback_fires_only_when_has_real_timestamps_false(monkeypatch):
    """When the caller could not supply real timestamps, build_from_prices
    falls back to its own bare auto-incrementing index, and
    log_renko_bricks synthesises hourly spacing backward from sweep_ts —
    but ONLY because has_real_timestamps=False, not as a silent default."""
    from renko.brick_engine import RenkoBrickEngine

    captured = _fake_post_capture(monkeypatch)

    prices = [100.0, 101.0, 102.0]
    engine = RenkoBrickEngine(brick_size=1.0)
    bricks = engine.build_from_prices(prices)  # no timestamps -> bare index
    assert bricks

    sweep_ts = 1_900_000_000.0
    await log_renko_bricks(
        "XAUUSD",
        [b.to_dict() for b in bricks],
        brick_size=1.0,
        sweep_ts=sweep_ts,
        has_real_timestamps=False,
    )

    n = len(bricks)
    expected = [
        sweep_ts - (n - 1 - i) * 3600.0 for i in range(n)
    ]
    persisted_ts = [
        datetime.fromisoformat(row["ts"]).timestamp() for row in captured["json"]
    ]
    assert persisted_ts == pytest.approx(expected)


@pytest.mark.asyncio
async def test_has_real_timestamps_true_ignores_sweep_ts_even_if_provided(monkeypatch):
    """A caller could (mistakenly or not) pass both a real timestamp array
    AND a sweep_ts — has_real_timestamps=True must make the real value win,
    proving the fallback is gated on the flag, not merely on sweep_ts being
    absent."""
    from renko.brick_engine import RenkoBrickEngine

    captured = _fake_post_capture(monkeypatch)

    prices = [100.0, 101.0]
    real_timestamps = [1_800_000_000.0, 1_800_003_600.0]
    engine = RenkoBrickEngine(brick_size=1.0)
    bricks = engine.build_from_prices(prices, timestamps=real_timestamps)
    assert bricks

    await log_renko_bricks(
        "XAUUSD",
        [b.to_dict() for b in bricks],
        brick_size=1.0,
        sweep_ts=time.time(),
        has_real_timestamps=True,
    )

    persisted_ts = [
        datetime.fromisoformat(row["ts"]).timestamp() for row in captured["json"]
    ]
    # Must match the real per-brick timestamps, not a sweep_ts-anchored
    # hourly reconstruction.
    real_ts_used = [b.to_dict()["timestamp"] for b in bricks]
    assert persisted_ts == pytest.approx(real_ts_used)
