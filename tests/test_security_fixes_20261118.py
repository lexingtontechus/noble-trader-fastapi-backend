"""Regression tests for the 2026-11-18 security fixes (scan scan_d5e17dd9d64a3cf9d2d49860).

Each test pairs the original attack trigger with the legitimate behavior
through the same boundary:
- finding 14: shared API key compared constant-time, fail-closed when unset
- finding 5:  llm_server auth fail-closed (dev key file, never logged)
- finding 7:  /prices requires the key; ticker/range validated + encoded
- finding 6:  /crt data routes require the key; /crt/health stays public
- finding 18: kill-switch gate fails CLOSED with a TTL-cached last-known state
- finding 12/16: log scrubbing covers propagated child-logger records and
  api_key query strings (uvicorn.access shape)
"""
from __future__ import annotations

import logging
import sys
import time
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from regime_platform.core.settings import settings

# ── finding 14: api_key._valid ──────────────────────────────────────────────

def test_api_key_fail_closed_when_unset(monkeypatch):
    from regime_platform.auth import api_key
    monkeypatch.setattr(settings, "API_KEY", "")
    assert api_key._valid(None) is False
    assert api_key._valid("") is False
    assert api_key._valid("anything") is False


def test_api_key_constant_time_compare(monkeypatch):
    from regime_platform.auth import api_key
    monkeypatch.setattr(settings, "API_KEY", "correct-key")
    assert api_key._valid("correct-key") is True
    assert api_key._valid("wrong-key") is False
    assert api_key._valid(None) is False


# ── finding 5: llm_server fail-closed auth ──────────────────────────────────

async def test_llm_server_requires_dev_key_when_env_unset(monkeypatch, tmp_path):
    import llm_server
    dev_file = tmp_path / "local_llm_api_key"
    monkeypatch.setattr(llm_server, "_DEV_KEY_FILE", str(dev_file))
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)

    # Attack trigger: no key configured, anonymous request must NOT pass.
    with pytest.raises(HTTPException) as ei:
        await llm_server.verify_api_key(api_key=None)
    assert ei.value.status_code == 401

    # The dev key was persisted to the protected file (and nowhere else).
    dev_key = dev_file.read_text(encoding="utf-8").strip()
    assert len(dev_key) >= 32
    assert await llm_server.verify_api_key(api_key=dev_key) is None
    with pytest.raises(HTTPException):
        await llm_server.verify_api_key(api_key="wrong")


async def test_llm_server_env_keys_enforced(monkeypatch, tmp_path):
    import llm_server
    monkeypatch.setattr(llm_server, "_DEV_KEY_FILE", str(tmp_path / "k"))
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "k1,k2")
    assert await llm_server.verify_api_key(api_key="k2") is None
    with pytest.raises(HTTPException) as ei:
        await llm_server.verify_api_key(api_key="k3")
    assert ei.value.status_code == 401


def test_llm_server_trust_remote_code_default_off(monkeypatch):
    # Fail-safe default: with no env opt-in, remote modeling code is OFF.
    import importlib

    import llm_server
    monkeypatch.delenv("LOCAL_LLM_TRUST_REMOTE_CODE", raising=False)
    reloaded = importlib.reload(llm_server)
    try:
        assert reloaded.TRUST_REMOTE_CODE is False
        monkeypatch.setenv("LOCAL_LLM_TRUST_REMOTE_CODE", "1")
        assert importlib.reload(llm_server).TRUST_REMOTE_CODE is True
    finally:
        monkeypatch.delenv("LOCAL_LLM_TRUST_REMOTE_CODE", raising=False)
        importlib.reload(llm_server)


# ── finding 7: /prices auth + input validation ──────────────────────────────

@pytest.fixture()
def prices_client(monkeypatch):
    from regime_platform.routers import prices
    monkeypatch.setattr(settings, "API_KEY", "test-key")
    app = FastAPI()
    app.include_router(prices.router)
    return TestClient(app)


def test_prices_requires_api_key(prices_client):
    r = prices_client.get("/prices/AAPL")
    assert r.status_code == 401


def test_prices_rejects_ticker_injection(prices_client):
    # Alternate malicious representation: encoded '?' in the path param.
    r = prices_client.get("/prices/AAPL%3Frange%3D1d", headers={"X-API-Key": "test-key"})
    assert r.status_code == 400


def test_prices_rejects_range_injection(prices_client):
    r = prices_client.get("/prices/AAPL?range=90d%26foo%3Dbar", headers={"X-API-Key": "test-key"})
    assert r.status_code == 400


def test_prices_legitimate_fetch_still_works(prices_client, monkeypatch):
    import pandas as pd
    fake = types.ModuleType("yfinance")

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period=None, interval=None):
            return pd.DataFrame({"Close": [1.0, 2.0, 3.0]})

    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    r = prices_client.get("/prices/AAPL?range=90d&interval=1h", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.json() == [1.0, 2.0, 3.0]


# ── finding 6: /crt data routes gated, health public ────────────────────────

@pytest.fixture()
def crt_client(monkeypatch):
    import crt.router as crt_router_mod
    monkeypatch.setattr(settings, "API_KEY", "test-key")
    monkeypatch.setattr(crt_router_mod, "_mode", lambda: "off")
    app = FastAPI()
    app.include_router(crt_router_mod.crt_router)
    return TestClient(app)


def test_crt_health_public(crt_client):
    assert crt_client.get("/crt/health").status_code == 200


@pytest.mark.parametrize("path", ["/crt/state", "/crt/claims", "/crt/promotion"])
def test_crt_data_routes_require_key(crt_client, path):
    assert crt_client.get(path).status_code == 401


def test_crt_state_with_key_dark_mode(crt_client):
    r = crt_client.get("/crt/state", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.json()["crt_mode"] == "off"


# ── finding 18: kill-switch gate fail-closed + TTL cache ────────────────────

def _ks_app(monkeypatch, is_halted_side_effect, halt_obj=None):
    from regime_platform.middleware import kill_switch_gate as mod
    monkeypatch.setattr(mod.KillSwitchGateMiddleware, "_last_known", {})

    stub = types.ModuleType("regime_platform.services.operational.kill_switch")

    class _KS:
        async def is_halted(self, user_id=None, symbol=None):
            if isinstance(is_halted_side_effect, Exception):
                raise is_halted_side_effect
            return is_halted_side_effect

        async def get_active_halt(self, user_id=None, symbol=None):
            return halt_obj

    stub.kill_switch = _KS()
    monkeypatch.setitem(sys.modules, "regime_platform.services.operational.kill_switch", stub)

    app = FastAPI()
    app.add_middleware(mod.KillSwitchGateMiddleware)

    @app.post("/renko/tick")
    def tick():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app), mod


def test_kill_switch_fail_closed_no_cache(monkeypatch):
    client, _ = _ks_app(monkeypatch, RuntimeError("supabase down"))
    r = client.post("/renko/tick")
    assert r.status_code == 403
    assert r.json()["error"] == "TRADING_HALTED_UNKNOWN"
    # Non-execution paths are unaffected by the outage.
    assert client.get("/health").status_code == 200


def test_kill_switch_halted_blocks(monkeypatch):
    halt = types.SimpleNamespace(model_dump=lambda mode="json": {"level": "global"})
    client, _ = _ks_app(monkeypatch, True, halt)
    r = client.post("/renko/tick")
    assert r.status_code == 403
    assert r.json()["error"] == "TRADING_HALTED"


def test_kill_switch_uses_fresh_cache_on_outage(monkeypatch):
    client, mod = _ks_app(monkeypatch, False)
    assert client.post("/renko/tick").status_code == 200  # primes cache (not halted)

    # Now the lookup fails — fresh cache says not halted → still allowed.
    stub = sys.modules["regime_platform.services.operational.kill_switch"]

    async def _boom(self, user_id=None, symbol=None):
        raise RuntimeError("outage")

    monkeypatch.setattr(stub.kill_switch, "is_halted", _boom.__get__(stub.kill_switch))
    assert client.post("/renko/tick").status_code == 200

    # Stale cache (older than TTL) + outage → fail closed.
    key = "-:-"
    halted, info, _ts = mod.KillSwitchGateMiddleware._last_known[key]
    mod.KillSwitchGateMiddleware._last_known[key] = (halted, info, time.monotonic() - 400)
    r = client.post("/renko/tick")
    assert r.status_code == 403
    assert r.json()["error"] == "TRADING_HALTED_UNKNOWN"


# ── findings 12/16: log scrubbing on propagated records ─────────────────────

def test_log_filter_scrubs_child_logger_records():
    from regime_platform.utils.log_filter import configure_logging_filter

    configure_logging_filter()
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        child = logging.getLogger("security_fix_test_child")
        secret = "sb_secret_" + "A" * 40
        child.warning("connecting with %s", secret)
        child.warning("GET /ws/AAPL?api_key=%s HTTP/1.1", "k" * 43)
    finally:
        root.removeHandler(handler)

    assert captured, "expected captured log records"
    joined = "\n".join(captured)
    assert secret not in joined
    assert "k" * 43 not in joined
    assert "[REDACTED]" in joined
