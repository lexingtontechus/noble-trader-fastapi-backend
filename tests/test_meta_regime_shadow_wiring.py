"""Tests for meta_regime_model.record_tick_and_update_shadow — the
per-tick wiring that was missing entirely before 2026-09-09 (worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §11, "what
happened to all the shadow mode tracking?").
"""

import asyncio

import regime_platform.sweep_orchestrator.meta_regime_model as m


def _row(i, label="bear"):
    return {"ts": float(i), "direction": "sell", "regime_label": label,
            "regime_conf": 0.7, "p_win": 0.55, "kelly_f": 0.1}


def test_always_appends_tick_even_when_mode_is_off(monkeypatch):
    appended = []

    async def fake_append(row):
        appended.append(row)

    monkeypatch.setattr("regime_platform.core.feature_flags.get_signal_stream_meta_model_mode", lambda: "off")
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.append_tick",
        fake_append,
    )

    asyncio.run(m.record_tick_and_update_shadow(
        symbol="EURUSD", ts=1.0, direction="sell",
        regime_label="bear", regime_conf=0.7, p_win=0.55, kelly_f=0.1,
    ))

    assert len(appended) == 1
    assert appended[0]["symbol"] == "EURUSD"


def test_off_mode_never_calls_fetch_history_or_persists_shadow(monkeypatch):
    async def fail_if_called(*a, **kw):
        raise AssertionError("should not be called in off mode")

    monkeypatch.setattr("regime_platform.core.feature_flags.get_signal_stream_meta_model_mode", lambda: "off")
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.append_tick",
        lambda row: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.fetch_history",
        fail_if_called,
    )

    asyncio.run(m.record_tick_and_update_shadow(
        symbol="EURUSD", ts=1.0, direction="sell",
        regime_label="bear", regime_conf=0.7, p_win=0.55, kelly_f=0.1,
    ))


def test_shadow_mode_with_insufficient_history_does_not_persist(monkeypatch):
    async def fake_append(row):
        return None

    async def fake_fetch_history(symbol):
        return [_row(i) for i in range(10)]  # < MIN_TICKS_TO_FIT

    async def fail_if_called(*a, **kw):
        raise AssertionError("should not persist without enough history")

    monkeypatch.setattr("regime_platform.core.feature_flags.get_signal_stream_meta_model_mode", lambda: "shadow")
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.append_tick",
        fake_append,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.fetch_history",
        fake_fetch_history,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.record_signal_stream_meta_shadow",
        fail_if_called,
    )

    asyncio.run(m.record_tick_and_update_shadow(
        symbol="EURUSD", ts=1.0, direction="sell",
        regime_label="bear", regime_conf=0.7, p_win=0.55, kelly_f=0.1,
    ))


def test_shadow_mode_with_enough_history_fits_and_persists(monkeypatch):
    recorded = {}

    async def fake_append(row):
        return None

    async def fake_fetch_history(symbol):
        # 60 stable + 5 clearly different -> enough for a fit, matches
        # test_meta_regime_model.py's own synthetic pattern.
        history = [_row(i, label="bull") for i in range(60)]
        return history

    async def fake_record(symbol, result):
        recorded["symbol"] = symbol
        recorded["result"] = result

    monkeypatch.setattr("regime_platform.core.feature_flags.get_signal_stream_meta_model_mode", lambda: "shadow")
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.append_tick",
        fake_append,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.fetch_history",
        fake_fetch_history,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.record_signal_stream_meta_shadow",
        fake_record,
    )

    asyncio.run(m.record_tick_and_update_shadow(
        symbol="EURUSD", ts=1.0, direction="sell",
        regime_label="bear", regime_conf=0.7, p_win=0.55, kelly_f=0.1,
    ))

    assert recorded["symbol"] == "EURUSD"
    assert recorded["result"]["state"] in (m.STATE_COHERENT, m.STATE_NOISY)
    assert recorded["result"]["n_ticks_fit"] == 60


def test_append_tick_failure_does_not_prevent_shadow_update(monkeypatch):
    """append_tick and the shadow update are independent -- a cache-write
    failure must not silently block the (separately fetched) shadow
    read/persist, and must not raise into the caller."""
    recorded = {}

    async def failing_append(row):
        raise RuntimeError("disk full")

    async def fake_fetch_history(symbol):
        return [_row(i, label="bull") for i in range(60)]

    async def fake_record(symbol, result):
        recorded["called"] = True

    monkeypatch.setattr("regime_platform.core.feature_flags.get_signal_stream_meta_model_mode", lambda: "shadow")
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.append_tick",
        failing_append,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.signal_stream_cache.fetch_history",
        fake_fetch_history,
    )
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.record_signal_stream_meta_shadow",
        fake_record,
    )

    asyncio.run(m.record_tick_and_update_shadow(
        symbol="EURUSD", ts=1.0, direction="sell",
        regime_label="bear", regime_conf=0.7, p_win=0.55, kelly_f=0.1,
    ))

    assert recorded.get("called") is True
