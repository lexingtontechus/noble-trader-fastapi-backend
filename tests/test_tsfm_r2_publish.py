"""Offline suite for tsfm_r2_publish (scope G1).

Stubs the roster read, the TDVA fetch, and httpx — zero network. Covers the
fail-loud validation contract, shadow's zero-upload guarantee, the secret
fail-closed, CSV schema, and manifest semantics.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from regime_platform.sweep_orchestrator import tsfm_r2_publish as mod

NOW = time.time()
ROSTER = [
    {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"},
    {"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"},
]


def make_rows(n=5000, end=None, step=3600):
    end = end if end is not None else NOW - 3600
    return [
        {"t": int(end - (n - 1 - i) * step), "o": 100.0 + i * 0.01,
         "h": 101.0 + i * 0.01, "l": 99.0 + i * 0.01,
         "c": 100.5 + i * 0.01, "v": 1000.0}
        for i in range(n)
    ]


class FakeClient:
    """Captures PUTs; returns 200. Subclass/attr to inject failures."""

    calls: list = []
    fail_urls: set = set()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def put(self, url, content=None, headers=None):
        FakeClient.calls.append({"url": url, "content": content, "headers": headers})
        if url in FakeClient.fail_urls:
            return SimpleNamespace(status_code=500, text="boom")
        return SimpleNamespace(status_code=200, text='{"ok":true}')


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_urls = set()
    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mod.settings, "TSFM_R2_WRITE_SECRET", "test-secret")
    monkeypatch.setattr(mod.settings, "TSFM_R2_BRIDGE_URL", "https://bridge.test")
    monkeypatch.setattr(mod, "get_tsfm_r2_publish_symbols", lambda: ["BTCUSD", "USDJPY"])

    async def roster():
        return ROSTER

    monkeypatch.setattr(mod, "fetch_active_symbols", roster)
    state = {"ohlcv": {"BTCUSD": make_rows(), "USDJPY": make_rows()}}

    async def fetch(symbols, hours=None, interval=None, include_timestamps=None, ohlcv_out=None):
        assert hours == mod.FETCH_BARS == 5000
        assert interval == "1h" and include_timestamps is True
        for s in symbols:
            if s["symbol"] in state["ohlcv"]:
                ohlcv_out[s["symbol"]] = state["ohlcv"][s["symbol"]]
        return ({}, {})

    monkeypatch.setattr(mod, "fetch_tradingview_batch", fetch)
    return state


@pytest.mark.asyncio
async def test_off_is_full_noop(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("off must not read the roster")
    monkeypatch.setattr(mod, "fetch_active_symbols", boom)
    out = await mod.publish_tsfm_prices("off")
    assert out["published"] == 0 and not out["errors"]
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_shadow_zero_uploads(wired):
    out = await mod.publish_tsfm_prices("shadow")
    assert FakeClient.calls == []
    assert out["published"] == 0 and not out["errors"]
    assert set(out["symbols"]) == {"BTCUSD", "USDJPY"}
    assert out["symbols"]["BTCUSD"]["rows"] == 5000
    assert out["symbols"]["BTCUSD"]["md5"]


@pytest.mark.asyncio
async def test_live_publishes_csvs_and_manifest(wired):
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 2 and out["manifest_written"] and not out["errors"]
    urls = [c["url"] for c in FakeClient.calls]
    assert urls == [
        "https://bridge.test/tsfm/BTCUSD.csv",
        "https://bridge.test/tsfm/USDJPY.csv",
        "https://bridge.test/tsfm/manifest.json",
    ]
    for c in FakeClient.calls:
        assert c["headers"]["X-Write-Secret"] == "test-secret"
    csv = FakeClient.calls[0]["content"].decode()
    lines = csv.splitlines()
    assert lines[0] == "timestamp,open,high,low,close,volume"
    first_ts = lines[1].split(",")[0]
    assert first_ts.startswith("20") and "T" in first_ts and first_ts.endswith("Z")
    ts = [ln.split(",")[0] for ln in lines[1:]]
    assert ts == sorted(ts) and all(t.endswith("Z") for t in ts)
    assert len(lines) == 5001
    manifest = json.loads(FakeClient.calls[2]["content"])
    assert manifest["publisher"] == "backend" and manifest["source"] == "tdva"
    assert set(manifest["symbols"]) == {"BTCUSD", "USDJPY"}
    assert manifest["symbols"]["BTCUSD"]["rows"] == 5000
    assert manifest["errors"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate,reason", [
    (lambda rows: rows[:999], "rows 999"),
    (lambda rows: [{**rows[0], "t": None}] + rows[1:], "null timestamp"),
    (lambda rows: list(reversed(rows)), "ascending"),
    (lambda rows: [{**rows[0], "c": float("nan")}] + rows[1:], "non-finite"),
    (lambda rows: [{**rows[0], "v": -1.0}] + rows[1:], "volume"),
    (lambda rows: make_rows(end=NOW - 4 * 3600), "age"),
])
async def test_validation_rejects_and_keeps_previous(wired, mutate, reason):
    wired["ohlcv"]["BTCUSD"] = mutate(make_rows())
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 1  # USDJPY still ships
    assert "BTCUSD" in out["errors"] and reason in out["errors"]["BTCUSD"]
    urls = [c["url"] for c in FakeClient.calls]
    assert "https://bridge.test/tsfm/BTCUSD.csv" not in urls
    manifest = json.loads(FakeClient.calls[-1]["content"])
    assert "BTCUSD" in manifest["errors"] and "BTCUSD" not in manifest["symbols"]


@pytest.mark.asyncio
async def test_missing_rows_rejected(wired):
    del wired["ohlcv"]["USDJPY"]
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 1
    assert "no rows" in out["errors"]["USDJPY"]


@pytest.mark.asyncio
async def test_live_without_secret_fails_closed(wired, monkeypatch):
    monkeypatch.setattr(mod.settings, "TSFM_R2_WRITE_SECRET", "")
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 0 and "_secret" in out["errors"]
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_put_failure_recorded_manifest_still_written(wired):
    FakeClient.fail_urls = {"https://bridge.test/tsfm/BTCUSD.csv"}
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 1 and out["manifest_written"]
    assert "HTTP 500" in out["errors"]["BTCUSD"]
    manifest = json.loads(FakeClient.calls[-1]["content"])
    assert set(manifest["symbols"]) == {"USDJPY"}


@pytest.mark.asyncio
async def test_fetch_exception_never_raises(wired, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("tdva down")
    monkeypatch.setattr(mod, "fetch_tradingview_batch", boom)
    out = await mod.publish_tsfm_prices("live")
    assert out["published"] == 0 and "tdva down" in out["errors"]["_fetch"]
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_inactive_symbol_reported(wired, monkeypatch):
    monkeypatch.setattr(mod, "get_tsfm_r2_publish_symbols",
                        lambda: ["BTCUSD", "USDJPY", "XAUUSD"])
    out = await mod.publish_tsfm_prices("shadow")
    assert "XAUUSD" in out["errors"] and out["skipped"] >= 1
    assert set(out["symbols"]) == {"BTCUSD", "USDJPY"}


@pytest.mark.asyncio
async def test_roster_fallback_uses_bootstrap(wired, monkeypatch):
    async def empty():
        return []
    monkeypatch.setattr(mod, "fetch_active_symbols", empty)
    out = await mod.publish_tsfm_prices("shadow", roster_fallback=True)
    assert out["roster"].startswith("bootstrap-fallback")
    assert set(out["symbols"]) == {"BTCUSD", "USDJPY"}  # wanted filter applies
    assert not out["errors"]
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_roster_empty_without_fallback_fails_loud(wired, monkeypatch):
    async def empty():
        return []
    monkeypatch.setattr(mod, "fetch_active_symbols", empty)
    out = await mod.publish_tsfm_prices("shadow")
    assert "_roster" in out["errors"] and not out["symbols"]
    assert "roster" not in out
