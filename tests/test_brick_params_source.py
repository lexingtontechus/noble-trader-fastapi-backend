"""Brick-params single source of truth — worklog
20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md Part B.

The contract under test:
  * the store's `brick_state` tier is the geometry AUTHORITY when
    `brick_params_source: store`;
  * the YAML is the derived cache and the fail-open fallback;
  * `yaml` (the shipped default) is byte-identical to the historical read;
  * a cold store NEVER leaves the sweep without geometry.
"""
from __future__ import annotations

import asyncio

import pytest

from regime_platform.config import brick_params as bp
from regime_platform.core import param_store
from regime_platform.core import feature_flags as ff


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("PARAM_STORE_SQLITE", str(tmp_path / "param_store.sqlite"))
    yield
    param_store.clear()


def _seed_store(symbol: str, brick: float, sl: int, tp: int):
    _run(param_store.write_params([{
        "symbol": symbol, "tier_type": param_store.TIER_BRICK_STATE,
        "tier_key": "latest", "n": 1,
        "tier_value": {"symbol": symbol, "brick_size": brick,
                       "sl_bricks": sl, "tp_bricks": tp},
        "source": "test",
    }]))


def _seed_yaml(tmp_path, monkeypatch, symbol: str, brick: float, sl: int, tp: int):
    """Point the loader at a temp YAML with one symbol."""
    yml = tmp_path / "brick_params.yaml"
    yml.write_text(
        "defaults:\n  brick_size: 0.5\n  sl_bricks: 3\n  tp_bricks: 6\n"
        f"symbols:\n  {symbol}:\n    brick_size: {brick}\n"
        f"    sl_bricks: {sl}\n    tp_bricks: {tp}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BRICK_PARAMS_YAML", str(yml))
    bp.reload()


# ── flag ─────────────────────────────────────────────────────────────────

def test_flag_defaults_yaml():
    assert ff.get_brick_params_source() == "yaml"


def test_flag_fails_safe_on_unknown(monkeypatch):
    monkeypatch.setattr(ff, "_load", lambda: {"brick_params_source": "banana"})
    assert ff.get_brick_params_source() == "yaml"


# ── store read ───────────────────────────────────────────────────────────

def test_load_from_store_returns_triple():
    _seed_store("EURUSD", 5.4e-05, 3, 12)
    got = bp.load_from_store("EURUSD")
    assert got == (pytest.approx(5.4e-05), 3, 12)


def test_load_from_store_none_when_cold():
    assert bp.load_from_store("NOPE") is None


def test_load_from_store_none_when_partial():
    _run(param_store.write_params([{
        "symbol": "X", "tier_type": param_store.TIER_BRICK_STATE,
        "tier_key": "latest", "tier_value": {"brick_size": 0.5}, "source": "t",
    }]))
    assert bp.load_from_store("X") is None


# ── authoritative read ───────────────────────────────────────────────────

def test_yaml_mode_is_the_historical_read(tmp_path, monkeypatch):
    """Default mode: the YAML wins even when the store disagrees."""
    monkeypatch.setattr(ff, "_load", lambda: {"brick_params_source": "yaml"})
    _seed_yaml(tmp_path, monkeypatch, "EURUSD", 0.001, 3, 5)
    _seed_store("EURUSD", 9.9, 7, 9)   # store says something else
    assert bp.load_brick_params_authoritative("EURUSD") == (0.001, 3, 5)


def test_store_mode_reads_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "_load", lambda: {"brick_params_source": "store"})
    _seed_yaml(tmp_path, monkeypatch, "EURUSD", 0.001, 3, 5)
    _seed_store("EURUSD", 0.000123, 4, 11)
    assert bp.load_brick_params_authoritative("EURUSD") == (pytest.approx(0.000123), 4, 11)


def test_store_mode_falls_back_to_yaml_when_cold(tmp_path, monkeypatch):
    """The fail-open contract: an empty store must NOT starve the sweep."""
    monkeypatch.setattr(ff, "_load", lambda: {"brick_params_source": "store"})
    _seed_yaml(tmp_path, monkeypatch, "EURUSD", 0.002, 5, 9)
    # no store row seeded
    assert bp.load_brick_params_authoritative("EURUSD") == (0.002, 5, 9)


def test_store_mode_falls_back_when_symbol_absent_from_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ff, "_load", lambda: {"brick_params_source": "store"})
    _seed_yaml(tmp_path, monkeypatch, "GBPUSD", 0.0009, 3, 7)
    _seed_store("EURUSD", 0.1, 1, 1)   # a different symbol only
    assert bp.load_brick_params_authoritative("GBPUSD") == (0.0009, 3, 7)


# ── the orchestrator actually routes through the authoritative read ──────

def test_orchestrator_load_routes_through_authoritative(monkeypatch):
    """A regression guard: the light path must not bypass the flag."""
    import inspect
    from regime_platform.sweep_orchestrator import orchestrator as orch

    src = inspect.getsource(orch.SweepOrchestrator._load_brick_params)
    assert "load_brick_params_authoritative" in src
    # And it must NOT call the raw YAML loader directly any more.
    assert "from regime_platform.config.brick_params import load_brick_params\n" not in src
