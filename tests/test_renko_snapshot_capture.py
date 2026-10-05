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
from datetime import datetime, timezone

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
        headers = {"content-range": "0-0/0"}

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

        async def delete(self, url, headers=None, params=None):
            captured["delete"] = {"url": url, "headers": headers, "params": params or {}}
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
    ``captured`` dict the test can inspect after awaiting log_renko_bricks.

    Also captures the follow-up orphan-purge DELETE (if any) under
    ``captured["delete"]``."""
    import httpx

    captured = {}

    class FakeResp:
        is_success = True
        status_code = 201
        text = ""
        headers = {"content-range": "0-0/0"}

        def __init__(self, status_code=201, headers=None):
            self.status_code = status_code
            self.headers = headers or {"content-range": "0-0/0"}

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

        async def delete(self, url, headers=None, params=None):
            captured["delete"] = {"url": url, "headers": headers, "params": params or {}}
            return FakeResp(200, {"content-range": "0-0/3"})

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


# ─── Orphan high-brick_index purge (2026-09-28) ────────────────────────────
# build_from_prices resets index to 0 and upserts only 0..N-1. A prior longer
# build's rows N..M on the same (symbol, brick_size, session_date) are never
# overwritten and win brick_index DESC — charts plot them as "last 10".


@pytest.mark.asyncio
async def test_log_renko_bricks_purges_orphan_high_indices(monkeypatch):
    """After upserting 0..2, DELETE same-grid rows with brick_index > 2."""
    captured = _fake_post_capture(monkeypatch)

    await log_renko_bricks(
        "XAUAUD",
        [
            {"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0},
            {"index": 1, "direction": "up", "open_price": 101.0, "close_price": 102.0},
            {"index": 2, "direction": "down", "open_price": 102.0, "close_price": 101.0},
        ],
        brick_size=1.474358,
        session_date="2026-09-28",
    )

    assert "delete" in captured, "orphan purge DELETE was not issued"
    params = captured["delete"]["params"]
    assert params["symbol"] == "eq.XAUAUD"
    assert params["session_date"] == "eq.2026-09-28"
    assert params["brick_index"] == "gt.2"
    # brick_size band must be present (same 0.1% tol as renko_retention)
    assert "brick_size.gte." in params["and"]
    assert "brick_size.lte." in params["and"]


@pytest.mark.asyncio
async def test_orphan_purge_brick_size_band_covers_roundtrip(monkeypatch):
    """Tolerance band is relative 0.1% so float round-trip never misses."""
    captured = _fake_post_capture(monkeypatch)

    await log_renko_bricks(
        "XAGUSD",
        [{"index": 7, "direction": "up", "open_price": 60.0, "close_price": 60.1}],
        brick_size=0.114717,
        session_date="2026-09-28",
    )

    params = captured["delete"]["params"]
    assert params["brick_index"] == "gt.7"
    # 0.114717 * 1e-3 = 1.14717e-4 → band ≈ [0.114602, 0.114832]
    and_clause = params["and"]
    assert "brick_size.gte." in and_clause
    assert "brick_size.lte." in and_clause
    # Parse the band endpoints and check they bracket the value within 0.1%.
    import re
    m = re.search(r"brick_size\.gte\.([0-9.]+),brick_size\.lte\.([0-9.]+)", and_clause)
    assert m, f"band not parseable: {and_clause}"
    low, high = float(m.group(1)), float(m.group(2))
    assert low < 0.114717 < high
    assert (high - low) == pytest.approx(0.114717 * 2e-3, rel=1e-6)


@pytest.mark.asyncio
async def test_orphan_purge_skipped_when_insert_fails(monkeypatch):
    """A failed upsert must not DELETE — orphans would be the only remaining
    live rows if we purged on error."""
    import httpx

    captured = {}

    class FakeResp:
        def __init__(self, status_code=201, text="", headers=None):
            self.status_code = status_code
            self.is_success = status_code < 400
            self.text = text
            self.headers = headers or {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["post"] = True
            return FakeResp(409, "conflict")

        async def delete(self, url, headers=None, params=None):
            captured["delete"] = params or {}
            return FakeResp(200)

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
        "AAPL",
        [{"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0}],
        brick_size=0.088433,
        session_date="2026-09-28",
    )
    assert captured.get("post") is True
    assert "delete" not in captured


@pytest.mark.asyncio
async def test_purge_orphan_helper_never_raises_on_delete_error(monkeypatch):
    """_purge_orphan_renko_bricks is best-effort — network failure returns 0."""
    from regime_platform.sweep_orchestrator.supabase_client import (
        _purge_orphan_renko_bricks,
    )

    class BoomClient:
        async def delete(self, *a, **k):
            raise RuntimeError("network down")

    result = await _purge_orphan_renko_bricks(
        BoomClient(),
        symbol="US500",
        brick_size=2.278393,
        session_date="2026-09-28",
        max_brick_index=10,
    )
    assert result == 0


# ─── session_date derivation (2026-10-04, migration 196) ───────────────────
# The sweep call site does NOT pass session_date, so the column used to fall to
# the DB CURRENT_DATE default — the SWEEP RUN date — stamping every brick in a
# multi-week window with one (wrong) date. That collapsed ~30 real trading days
# into 3 and starved v_talaria_daily_close. session_date must now come from the
# brick's OWN ts unless the caller explicitly passes one.


@pytest.mark.asyncio
async def test_session_date_derived_from_brick_ts_when_not_passed(monkeypatch):
    """No session_date arg → each row's session_date == its own ts::date (UTC)."""
    captured = _fake_post_capture(monkeypatch)

    # Two real per-bar epochs on DIFFERENT UTC days (2026-09-22 and 2026-09-23).
    day1 = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc).timestamp()
    day2 = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc).timestamp()
    bricks = [
        {"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0, "timestamp": day1},
        {"index": 1, "direction": "up", "open_price": 101.0, "close_price": 102.0, "timestamp": day2},
    ]
    await log_renko_bricks(
        "AUDUSD", bricks, brick_size=0.00005, has_real_timestamps=True,
    )

    rows = captured["json"]
    assert rows[0]["session_date"] == "2026-09-22"
    assert rows[1]["session_date"] == "2026-09-23"


@pytest.mark.asyncio
async def test_explicit_session_date_still_wins(monkeypatch):
    """An explicit session_date arg overrides per-brick derivation (single grid)."""
    captured = _fake_post_capture(monkeypatch)

    day1 = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc).timestamp()
    day2 = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc).timestamp()
    bricks = [
        {"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0, "timestamp": day1},
        {"index": 1, "direction": "up", "open_price": 101.0, "close_price": 102.0, "timestamp": day2},
    ]
    await log_renko_bricks(
        "AUDUSD", bricks, brick_size=0.00005, session_date="2026-09-28",
        has_real_timestamps=True,
    )

    rows = captured["json"]
    assert all(r["session_date"] == "2026-09-28" for r in rows)


@pytest.mark.asyncio
async def test_session_date_derived_from_fallback_ts_too(monkeypatch):
    """When timestamps are synthesised (has_real_timestamps=False), session_date
    still derives from the resulting ts — never the run date / DB default."""
    captured = _fake_post_capture(monkeypatch)

    # sweep_ts = 2026-10-04 23:00 UTC; 2 bricks -> hourly spacing back to 22:00.
    sweep_ts = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc).timestamp()
    bricks = [
        {"index": 0, "direction": "up", "open_price": 100.0, "close_price": 101.0},
        {"index": 1, "direction": "up", "open_price": 101.0, "close_price": 102.0},
    ]
    await log_renko_bricks(
        "AUDUSD", bricks, brick_size=0.00005, sweep_ts=sweep_ts,
        has_real_timestamps=False,
    )

    rows = captured["json"]
    assert all(r["session_date"] == "2026-10-04" for r in rows)
