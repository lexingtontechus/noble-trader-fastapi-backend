"""Tests for the local param store — single source of truth.

Worklog 20261008_PARAM_SOURCE_OF_TRUTH.md. The contract under test:

  • The LOCAL store is the runtime authority (write_params / read_*).
  • Supabase is a best-effort MIRROR (sync_to_supabase) — never a read
    path for the sweep.
  • The pre-sweep optimizer writes to the local store, not to Supabase
    directly.
  • Consumers (regime_edge, dynamic_deadline) read local first, falling
    back to their previous path only when the local store is cold.

These are WIRING tests: they drive the real public entry points and assert
the observable contract (what got stored / what got read back), not
internal call counts.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from regime_platform.core import param_store
from regime_platform.sweep_orchestrator import tiered_atr as ta


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the store at a per-test SQLite file (never the repo's)."""
    monkeypatch.setenv("PARAM_STORE_SQLITE", str(tmp_path / "param_store.sqlite"))
    yield
    param_store.clear()


def _run(coro):
    return asyncio.run(coro)


async def _async_ret(value):
    """Awaitable returning ``value`` — for monkeypatching async callables."""
    return value


# ── Store primitives ─────────────────────────────────────────────────────

def test_write_then_read_roundtrip():
    rows = [
        {"symbol": "XAUUSD", "tier_type": param_store.TIER_DYNAMIC_ATR,
         "tier_key": "2-4", "tier_value": -0.3166, "n": 134},
        {"symbol": "EURUSD", "tier_type": param_store.TIER_DYNAMIC_ATR,
         "tier_key": "2-4", "tier_value": -0.2452, "n": 134},
    ]
    written = _run(param_store.write_params(rows))
    assert written == 2

    assert param_store.read_float("XAUUSD", param_store.TIER_DYNAMIC_ATR, "2-4") == pytest.approx(-0.3166)
    assert param_store.read_float("EURUSD", param_store.TIER_DYNAMIC_ATR, "2-4") == pytest.approx(-0.2452)
    # Missing key → None (fail-open), never a raise.
    assert param_store.read_value("XAUUSD", param_store.TIER_DYNAMIC_ATR, "9-9") is None


def test_upsert_overwrites_same_key():
    key = {"symbol": "XAUUSD", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "2-4", "n": 10}
    _run(param_store.write_params([{**key, "tier_value": 0.1}]))
    _run(param_store.write_params([{**key, "tier_value": 0.9, "n": 20}]))
    assert param_store.read_float("XAUUSD", param_store.TIER_DYNAMIC_ATR, "2-4") == pytest.approx(0.9)
    # Only one row for the key (upsert, not append).
    assert param_store.stats()["total"] == 1


def test_json_object_value_roundtrip():
    """regime_edge carries {win_rate, expectancy_r} as a JSON object."""
    _run(param_store.write_params([{
        "symbol": "AAPL", "tier_type": param_store.TIER_REGIME_EDGE,
        "tier_key": "buy:high_vol_bull", "n": 30,
        "tier_value": {"win_rate": 0.12, "expectancy_r": -0.42},
    }]))
    parsed = param_store.read_dict("AAPL", param_store.TIER_REGIME_EDGE, "buy:high_vol_bull")
    assert parsed == {"win_rate": 0.12, "expectancy_r": -0.42}
    # A JSON object is not a float.
    assert param_store.read_float("AAPL", param_store.TIER_REGIME_EDGE, "buy:high_vol_bull") is None


def test_read_all_and_stats():
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
        {"symbol": "B", "tier_type": param_store.TIER_REGIME_EDGE, "tier_key": "sell:x", "tier_value": 0.2, "n": 6},
    ]))
    stats = param_store.stats()
    assert stats["total"] == 2
    assert stats["by_tier_type"][param_store.TIER_DYNAMIC_ATR] == 1
    assert stats["by_tier_type"][param_store.TIER_REGIME_EDGE] == 1
    assert stats["newest_computed_at"] is not None


# ── Mirror is best-effort, never the read path ───────────────────────────

def test_sync_to_supabase_is_best_effort(monkeypatch):
    """A Supabase failure must NOT raise — local stays authoritative."""
    _run(param_store.write_params([{
        "symbol": "X", "tier_type": param_store.TIER_DYNAMIC_ATR,
        "tier_key": "1-2", "tier_value": 0.5, "n": 3,
    }]))

    async def _boom(rows):
        raise RuntimeError("supabase down")

    import regime_platform.sweep_orchestrator.supabase_client as sc
    monkeypatch.setattr(sc, "upsert_dynamic_tier_state", _boom)

    result = _run(param_store.sync_to_supabase())
    assert result["ok"] is False
    assert result["mirrored"] == 0
    # Local authority survived the mirror failure.
    assert param_store.read_float("X", param_store.TIER_DYNAMIC_ATR, "1-2") == pytest.approx(0.5)


def test_sync_to_supabase_normalises_json_values(monkeypatch):
    """A dict tier_value is JSON-encoded before the text-column upsert."""
    _run(param_store.write_params([{
        "symbol": "AAPL", "tier_type": param_store.TIER_REGIME_EDGE,
        "tier_key": "buy:x", "tier_value": {"win_rate": 0.3, "expectancy_r": -0.1}, "n": 30,
    }]))
    captured = {}

    async def _capture(rows):
        captured["rows"] = rows

    import regime_platform.sweep_orchestrator.supabase_client as sc
    monkeypatch.setattr(sc, "upsert_dynamic_tier_state", _capture)

    async def _remote(symbols=None):
        return []

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    monkeypatch.setattr(sc, "delete_dynamic_tier_state_rows", lambda keys: _async_ret(0))

    _run(param_store.sync_to_supabase())
    assert captured["rows"], "mirror was not called"
    v = captured["rows"][0]["tier_value"]
    assert isinstance(v, str)
    assert json.loads(v)["win_rate"] == 0.3


def test_sync_to_supabase_excludes_brick_state(monkeypatch):
    """brick_state must NOT mirror to nt_dynamic_tier_state (it goes to
    nt_brick_params_state) — otherwise it creates phantom parity keys."""
    _run(param_store.write_params([
        {"symbol": "X", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.5, "n": 3},
        {"symbol": "X", "tier_type": param_store.TIER_BRICK_STATE, "tier_key": "latest", "tier_value": {"brick_size": 1.0}, "n": 1},
    ]))
    captured = {}

    async def _capture(rows):
        captured["rows"] = rows

    async def _remote(symbols=None):
        return []

    import regime_platform.sweep_orchestrator.supabase_client as sc
    monkeypatch.setattr(sc, "upsert_dynamic_tier_state", _capture)
    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    monkeypatch.setattr(sc, "delete_dynamic_tier_state_rows", lambda keys: _async_ret(0))

    _run(param_store.sync_to_supabase())
    types = {r["tier_type"] for r in captured["rows"]}
    assert param_store.TIER_BRICK_STATE not in types
    assert param_store.TIER_DYNAMIC_ATR in types


def test_sync_to_supabase_prunes_stale_mirror_keys(monkeypatch):
    """Mirror keys absent from the local authority are deleted."""
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
    ]))

    async def _remote(symbols=None):
        # mirror has the local key + two stale keys
        return [
            {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": "0.1", "n": 5},
            {"symbol": "A", "tier_type": param_store.TIER_REGIME_EDGE, "tier_key": "buy:stale", "tier_value": "{}", "n": 5},
            {"symbol": "B", "tier_type": param_store.TIER_DYNAMIC_DEADLINE, "tier_key": "low_vol", "tier_value": "{}", "n": 5},
        ]

    deleted = {}

    async def _delete(keys):
        deleted["keys"] = keys
        return len(keys)

    import regime_platform.sweep_orchestrator.supabase_client as sc
    monkeypatch.setattr(sc, "upsert_dynamic_tier_state", lambda rows: _async_ret(None))
    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    monkeypatch.setattr(sc, "delete_dynamic_tier_state_rows", _delete)

    result = _run(param_store.sync_to_supabase())
    assert result["ok"] is True
    assert result["pruned"] == 2
    stale_keys = {(k["symbol"], k["tier_key"]) for k in deleted["keys"]}
    assert stale_keys == {("A", "buy:stale"), ("B", "low_vol")}


def test_parity_report_ignores_brick_state(monkeypatch):
    """brick_state must not appear in parity (it mirrors elsewhere)."""
    _run(param_store.write_params([
        {"symbol": "X", "tier_type": param_store.TIER_BRICK_STATE, "tier_key": "latest", "tier_value": {"a": 1}, "n": 1},
    ]))

    async def _remote(symbols=None):
        return []  # nt_dynamic_tier_state has no brick_state row (correct)

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    report = _run(param_store.parity_report())
    assert report["ok"] is True
    assert report["local_keys"] == 0


# ── Optimizer writes local, not Supabase ─────────────────────────────────

def test_optimizer_writes_local_store(monkeypatch):
    """The pre-sweep optimizer must persist to the LOCAL store."""
    import regime_platform.sweep_orchestrator.dynamic_tier_optimizer as opt

    async def _fake_symbols():
        return [{"symbol": "XAUUSD", "asset_class": "commodity",
                 "tradingview_symbol": "OANDA:XAUUSD"}]

    async def _fake_resolved(symbols, **kw):
        return []

    async def _fake_reach(symbols=None):
        return []

    async def _fake_incubation():
        return {}

    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        pick = ta.DynamicAtrPick(tp_atr=4.94, sl_atr=1.0, exp_atr=1.23,
                                 exp_h1=0.5, exp_h2=0.7, expiry=0.05, n=134)
        await ta.write_pick_to_store(symbol, pick, source="test")
        return pick

    monkeypatch.setattr(opt, "fetch_active_symbols", _fake_symbols)
    monkeypatch.setattr(opt, "_fetch_resolved_rows", _fake_resolved)
    monkeypatch.setattr(opt, "_fetch_reachability_rows", _fake_reach)
    monkeypatch.setattr(opt, "_compute_geometry_incubation", _fake_incubation)
    # 2026-10-09 (worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): the
    # optimizer no longer computes its own ATR — it calls the one picker,
    # which persists the usable geometry. Stub the picker instead.
    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)

    summary = _run(opt.run_dynamic_tier_optimization())
    assert summary["atr_picks"] == 1
    # The ATR tier is written by the PICKER, not this row list — so the
    # optimizer's own row count is independent of the ATR pick (2026-10-09).
    assert "state_rows_written" in summary
    # The local authority holds the USABLE geometry (not a bucket label).
    stored = ta.read_pick_from_store("XAUUSD")
    assert stored is not None
    assert stored.tp_atr == pytest.approx(4.94)
    assert stored.sl_atr == pytest.approx(1.0)


# ── Consumer read paths (local first) ────────────────────────────────────

def test_regime_edge_reads_local_store(monkeypatch):
    from regime_platform.sweep_orchestrator import regime_edge

    _run(param_store.write_params([{
        "symbol": "AAPL", "tier_type": param_store.TIER_REGIME_EDGE,
        "tier_key": "buy:high_vol_bull", "n": 40,
        "tier_value": {"win_rate": 0.11, "expectancy_r": -0.5},
    }]))

    # If the local read works, Supabase must not be consulted. Make any
    # httpx call explode so a regression is loud.
    class _BoomClient:
        def __init__(self, *a, **k):
            raise AssertionError("Supabase must not be read when local store has the key")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

    edge = _run(regime_edge.get_regime_edge("AAPL", "buy", "high_vol_bull"))
    assert edge is not None
    assert edge["source"] == "param_store"
    assert edge["n"] == 40
    assert edge["win_rate"] == pytest.approx(0.11)
    assert edge["expectancy_r"] == pytest.approx(-0.5)


def test_regime_edge_gate_uses_local_win_rate():
    from regime_platform.sweep_orchestrator import regime_edge

    edge = {"symbol": "AAPL", "signal": "buy", "regime": "high_vol_bull",
            "n": 40, "win_rate": 0.11, "expectancy_r": -0.5}
    reject, reason = regime_edge.should_reject_by_regime_edge(edge, min_n=20, min_win_rate=0.50)
    assert reject is True
    assert "WR=11.0%" in reason


def test_deadline_tier_reads_local_store(monkeypatch):
    from regime_platform.sweep_orchestrator import dynamic_deadline

    _run(param_store.write_params([{
        "symbol": "XAUUSD", "tier_type": param_store.TIER_DYNAMIC_DEADLINE,
        "tier_key": "high_vol", "n": 60,
        "tier_value": {
            "soft_deadline_hours": 30.0,
            "hard_deadline_hours": 60.0,
            "median_hours": 25.0,
            "confidence": "high",
        },
    }]))

    tier = dynamic_deadline.read_local_deadline_tier("XAUUSD", "high_vol")
    assert tier is not None
    assert tier.soft_deadline_hours == pytest.approx(30.0)
    assert tier.hard_deadline_hours == pytest.approx(60.0)
    assert tier.median_hours == pytest.approx(25.0)
    assert tier.confidence == "high"
    assert tier.n == 60
    # decay is the consumer contract: full credit at/below soft, zero at hard.
    assert tier.decay(30.0) == pytest.approx(1.0)
    assert tier.decay(60.0) == pytest.approx(0.0)
    assert tier.decay(45.0) == pytest.approx(0.5)


def test_deadline_local_read_missing_returns_none():
    from regime_platform.sweep_orchestrator import dynamic_deadline

    assert dynamic_deadline.read_local_deadline_tier("NOPE", "high_vol") is None


# ── Parity enforcement ───────────────────────────────────────────────────

def test_parity_report_detects_divergence(monkeypatch):
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
    ]))

    async def _remote(symbols=None):
        return [
            {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": "0.9", "n": 5},
            {"symbol": "B", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": "0.2", "n": 5},
        ]

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    report = _run(param_store.parity_report())
    assert report["ok"] is False
    assert report["counts"]["value_mismatch"] == 1
    assert report["counts"]["missing_local"] == 1
    assert report["counts"]["missing_in_mirror"] == 0


def test_parity_report_clean_when_in_sync(monkeypatch):
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
    ]))

    async def _remote(symbols=None):
        return [
            {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": "0.1", "n": 5},
        ]

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)
    report = _run(param_store.parity_report())
    assert report["ok"] is True
    assert report["counts"] == {"missing_in_mirror": 0, "missing_local": 0, "value_mismatch": 0}


# ── FU2: nightly parity enforcement ──────────────────────────────────────

def test_run_parity_check_writes_audit_row_on_divergence(monkeypatch):
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
    ]))

    async def _remote(symbols=None):
        return []  # mirror empty → divergence

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)

    captured = {}

    async def _capture(symbol, action, detail=None):
        captured["symbol"] = symbol
        captured["action"] = action
        captured["detail"] = detail
        return True

    import regime_platform.sweep_orchestrator.geometry_risk as gr
    monkeypatch.setattr(gr, "write_eod_action", _capture)

    report = _run(param_store.run_parity_check())
    assert report["ok"] is False
    assert captured["action"] == "parameter_decay_check"
    assert captured["detail"]["source"] == "param_store_parity"
    assert captured["detail"]["missing_in_mirror"] == 1


def test_run_parity_check_no_audit_row_when_in_sync(monkeypatch):
    _run(param_store.write_params([
        {"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": 0.1, "n": 5},
    ]))

    async def _remote(symbols=None):
        return [{"symbol": "A", "tier_type": param_store.TIER_DYNAMIC_ATR, "tier_key": "1-2", "tier_value": "0.1", "n": 5}]

    monkeypatch.setattr(param_store, "_fetch_supabase_rows", _remote)

    called = {"n": 0}

    async def _capture(symbol, action, detail=None):
        called["n"] += 1
        return True

    import regime_platform.sweep_orchestrator.geometry_risk as gr
    monkeypatch.setattr(gr, "write_eod_action", _capture)

    report = _run(param_store.run_parity_check())
    assert report["ok"] is True
    assert called["n"] == 0


# ── FU1: brick-state local authority ─────────────────────────────────────

def test_brick_state_local_write_then_read(monkeypatch):
    from regime_platform.sweep_orchestrator import geometry_risk

    _run(geometry_risk.persist_brick_state_local(
        "XAUUSD", brick_size=3.29, sl_bricks=3, tp_bricks=8,
        atr_at_write=5.48, asset_class="commodities", source="heavy_sweep",
    ))

    # fetch_brick_state must read the local authority without touching httpx.
    class _BoomClient:
        def __init__(self, *a, **k):
            raise AssertionError("Supabase must not be read when local store has the key")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

    state = _run(geometry_risk.fetch_brick_state("XAUUSD"))
    assert state is not None
    assert state["brick_size"] == pytest.approx(3.29)
    assert state["atr_at_write"] == pytest.approx(5.48)
    assert state["sl_bricks"] == 3


def test_brick_state_falls_back_when_local_cold(monkeypatch):
    """Cold local store → None when Supabase is unavailable (fail-open)."""
    from regime_platform.sweep_orchestrator import geometry_risk

    monkeypatch.setattr(geometry_risk.settings, "SUPABASE_URL", "", raising=False)
    assert _run(geometry_risk.fetch_brick_state("NOPE")) is None
