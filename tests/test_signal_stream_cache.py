"""Tests for regime_platform/sweep_orchestrator/signal_stream_cache.py —
the SQLite local lookback cache, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §6.

Points the module at a throwaway temp file (monkeypatching ``_DB_PATH``)
so tests never touch the real config/signal_stream_cache.sqlite.
"""

import asyncio

import pytest

import regime_platform.sweep_orchestrator.signal_stream_cache as cache


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_DB_PATH", tmp_path / "test_signal_stream_cache.sqlite")


def _row(symbol="XAUUSD", ts=1000.0, direction="sell", regime_label="bear",
         regime_conf=0.8, p_win=0.55, kelly_f=0.4):
    return {
        "symbol": symbol, "ts": ts, "direction": direction,
        "regime_label": regime_label, "regime_conf": regime_conf,
        "p_win": p_win, "kelly_f": kelly_f,
    }


def test_append_and_fetch_roundtrip():
    asyncio.run(cache.append_tick(_row(ts=100.0)))
    asyncio.run(cache.append_tick(_row(ts=200.0)))

    rows = asyncio.run(cache.fetch_history("XAUUSD"))

    assert len(rows) == 2
    assert rows[0]["ts"] == 100.0
    assert rows[1]["ts"] == 200.0
    assert rows[0]["regime_label"] == "bear"
    assert rows[0]["p_win"] == pytest.approx(0.55)


def test_fetch_history_filters_by_symbol():
    asyncio.run(cache.append_tick(_row(symbol="XAUUSD", ts=100.0)))
    asyncio.run(cache.append_tick(_row(symbol="EURUSD", ts=100.0)))

    rows = asyncio.run(cache.fetch_history("XAUUSD"))

    assert len(rows) == 1
    assert all(r is not None for r in rows)  # sanity


def test_fetch_history_since_ts_filters_older_rows():
    asyncio.run(cache.append_tick(_row(ts=100.0)))
    asyncio.run(cache.append_tick(_row(ts=200.0)))
    asyncio.run(cache.append_tick(_row(ts=300.0)))

    rows = asyncio.run(cache.fetch_history("XAUUSD", since_ts=200.0))

    assert [r["ts"] for r in rows] == [200.0, 300.0]


def test_prune_older_than_deletes_and_returns_count():
    asyncio.run(cache.append_tick(_row(ts=100.0)))
    asyncio.run(cache.append_tick(_row(ts=200.0)))
    asyncio.run(cache.append_tick(_row(ts=300.0)))

    deleted = asyncio.run(cache.prune_older_than(250.0))

    assert deleted == 2
    remaining = asyncio.run(cache.fetch_history("XAUUSD"))
    assert [r["ts"] for r in remaining] == [300.0]


def test_row_count():
    asyncio.run(cache.append_tick(_row(ts=100.0)))
    asyncio.run(cache.append_tick(_row(ts=200.0)))

    assert asyncio.run(cache.row_count("XAUUSD")) == 2
    assert asyncio.run(cache.row_count("EURUSD")) == 0


def test_fetch_history_empty_returns_empty_list_not_none():
    rows = asyncio.run(cache.fetch_history("NEVER_SEEN"))
    assert rows == []


def test_append_tick_never_raises_on_bad_row(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(asyncio, "to_thread", boom)
    # Should log and swallow, not raise.
    asyncio.run(cache.append_tick(_row()))


def test_resync_from_supabase_returns_zero_when_no_data(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **kw):
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    n = asyncio.run(cache.resync_from_supabase("XAUUSD", days=7))
    assert n == 0
