"""
Tests for the Renko brick capture (nt_renko_bricks).

Covers:
  - log_renko_bricks builds rows matching the real table schema
    (brick_id serial PK; unique (symbol, brick_index, session_date))
  - missing Supabase config → returns silently (never raises)
  - empty brick list → skips without touching the network
  - row mapping: direction/open/close/high/low/brick_index/ts
"""

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
    assert "on_conflict=symbol,brick_index,session_date" in captured.get("url", "")
    assert captured["json"][0]["brick_index"] == 0
    assert captured["json"][0]["session_date"] == "2026-08-07"
    assert captured["headers"]["Prefer"] == "resolution=merge-duplicates"
