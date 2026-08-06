"""
Regression tests: TDVA full batch consolidation for the HMM fetchers
(2026-08-05).

Two reported bugs:
  1. `hmm.quote: fetched 0/11 symbols` + HTTP 429 — quote_fetcher fired
     11 concurrent `GET /api/quote/{symbol}` calls.
  2. `hmm.ta: fetched 0/11 symbols` + HTTP 429 — ta_fetcher fired 11
     concurrent `GET /api/ta/{symbol}/indicators` calls.

Fix (#3 full batch consolidation):
  • quote_fetcher → POST /api/quote/batch chunked at BATCH_CHUNK_SIZE (10)
    — N symbols become ⌈N/10⌉ HTTP calls, never N.
  • ta_fetcher → computes the 5 TA indicators LOCALLY from the batch
    closes the sweep already fetched (TVDA exposes no batch TA endpoint),
    so it makes ZERO HTTP calls.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from regime_platform.services.hmm import quote_fetcher, ta_fetcher  # noqa: E402
from regime_platform.services.hmm.feature_buffer import FeatureBuffer  # noqa: E402


# ── quote_fetcher: /api/quote/batch, chunked at 10 ──────────────────────

def _fake_batch_response(symbols):
    """Shape per the TVDA docs: {success, data: {total, successful, failed,
    data: [{success, symbol, data: {bid, ask, lp, volume, average_volume}}]}}"""
    return {
        "success": True,
        "data": {
            "total": len(symbols),
            "successful": len(symbols),
            "failed": 0,
            "data": [
                {
                    "success": True,
                    "symbol": sym,
                    "data": {
                        "bid": 100.0,
                        "ask": 100.05,
                        "lp": 100.03,
                        "volume": 5000,
                        "average_volume": 10000,
                    },
                }
                for sym in symbols
            ],
        },
    }


def test_quote_fetcher_chunks_11_symbols_into_2_calls():
    """11 symbols → 2 batched POSTs (10 + 1), NOT 11 concurrent GETs."""
    symbols = [
        {"symbol": f"SYM{i}", "tradingview_symbol": f"EXCHANGE:SYM{i}"}
        for i in range(11)
    ]
    buffer = FeatureBuffer()
    prev_closes = {f"SYM{i}": 100.0 for i in range(11)}

    calls: list[dict] = []

    class _FakeResp:
        status_code = 200

        def json(self):
            return _fake_batch_response(calls[-1]["symbols"])

    async def _fake_post(self, url, headers=None, json=None):
        calls.append({"url": url, "symbols": json["symbols"]})
        return _FakeResp()

    with patch.object(quote_fetcher.httpx.AsyncClient, "post", _fake_post):
        import asyncio

        pushed = asyncio.run(
            quote_fetcher.fetch_and_update(symbols, buffer, prev_closes)
        )

    assert pushed == 11
    assert len(calls) == 2, f"expected 2 chunked POSTs, got {len(calls)}"
    assert [len(c["symbols"]) for c in calls] == [10, 1]
    assert all(c["url"].endswith("/api/quote/batch") for c in calls)


def test_quote_fetcher_extracts_microstructure():
    symbols = [{"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"}]
    buffer = FeatureBuffer()
    prev_closes = {"XAUUSD": 100.0}

    async def _fake_post(self, url, headers=None, json=None):
        class _FakeResp:
            status_code = 200

            def json(self):
                return _fake_batch_response(["XAUUSD"])

        return _FakeResp()

    with patch.object(quote_fetcher.httpx.AsyncClient, "post", _fake_post):
        import asyncio

        pushed = asyncio.run(
            quote_fetcher.fetch_and_update(symbols, buffer, prev_closes)
        )

    assert pushed == 1
    vec = buffer.latest("XAUUSD")
    # spread = (100.05 - 100.0) / 100.025 * 10000 ≈ 4.99875
    assert vec[1] == pytest.approx(4.99875, abs=1e-4)
    # volume_persistence = 5000 / 10000 = 0.5
    assert vec[3] == pytest.approx(0.5)
    # log_return = ln(100.03 / 100)
    assert vec[0] == pytest.approx(np.log(1.0003), abs=1e-6)


def test_quote_fetcher_no_api_key_skips():
    symbols = [{"symbol": "SYM0", "tradingview_symbol": "EXCHANGE:SYM0"}]
    buffer = FeatureBuffer()
    with patch.object(
        quote_fetcher.settings, "TRADINGVIEW_API_KEY", ""
    ):
        import asyncio

        pushed = asyncio.run(
            quote_fetcher.fetch_and_update(symbols, buffer, {})
        )
    assert pushed == 0


# ── ta_fetcher: local TA from batch closes, ZERO HTTP ────────────────────

def test_ta_fetcher_computes_locally_no_http():
    """TA must be computed in-process from batch closes — assert no httpx
    call is ever made (no per-symbol /api/ta fan-out)."""
    symbols = [{"symbol": f"SYM{i}"} for i in range(11)]
    closes = {
        f"SYM{i}": [100.0 + (j * 0.1) for j in range(200)]
        for i in range(11)
    }
    buffer = FeatureBuffer()

    # Phase B: the TA fetcher is pure local computation — it must not even
    # import httpx (no per-symbol /api/ta fan-out, no HTTP path at all).
    assert not hasattr(ta_fetcher, "httpx"), (
        "ta_fetcher must not depend on httpx after batch consolidation"
    )

    import asyncio

    pushed = asyncio.run(
        ta_fetcher.fetch_and_update(symbols, buffer, closes_by_symbol=closes)
    )

    assert pushed == 11
    vec = buffer.latest("SYM0")
    assert vec.shape == (9,)
    # TA features populated with real values (not all-zero)
    assert any(vec[4:9] != 0.0)


def test_ta_fetcher_skips_missing_closes():
    symbols = [{"symbol": "SYM0"}]
    buffer = FeatureBuffer()
    import asyncio

    pushed = asyncio.run(
        ta_fetcher.fetch_and_update(symbols, buffer, closes_by_symbol={})
    )
    assert pushed == 0


def test_ta_fetcher_carries_forward_microstructure():
    symbols = [{"symbol": "XAUUSD"}]
    closes = {"XAUUSD": [4030.0 + (j * 0.5) for j in range(200)]}
    buffer = FeatureBuffer()
    buffer.push("XAUUSD", np.array([0.01, 5.0, 0.2, 0.8, 0, 0, 0, 0, 0]))

    import asyncio

    pushed = asyncio.run(
        ta_fetcher.fetch_and_update(symbols, buffer, closes_by_symbol=closes)
    )

    assert pushed == 1
    vec = buffer.latest("XAUUSD")
    # features 0-3 carried forward from previous bar
    assert vec[0] == pytest.approx(0.01)
    assert vec[1] == pytest.approx(5.0)
    assert vec[2] == pytest.approx(0.2)
    assert vec[3] == pytest.approx(0.8)
    # features 4-8 = computed local TA
    assert vec[4:9].any() != 0.0
