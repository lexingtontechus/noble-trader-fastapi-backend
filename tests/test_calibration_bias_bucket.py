"""
Tests for regime_platform/sweep_orchestrator/calibration_bias_bucket.py's
``build_calibration_bias_by_bucket`` — vol-tiered bucketing (2026-09-13,
worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md). Same fetch-mock pattern
as test_calibration_bias_window.py (FakeClient/FakeResp), since this
module's build function does real I/O (nt_signal_sim fetch,
nt_bracket_reachability join fetch, upsert).
"""

import asyncio
from unittest.mock import AsyncMock, patch

from regime_platform.sweep_orchestrator.calibration_bias_bucket import (
    build_calibration_bias_by_bucket,
)


class FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class FakeClient:
    """Serves different rows depending on which table is being queried
    (distinguished by the ``select``/params PostgREST sends), so one
    fixture can stand in for both the nt_signal_sim fetch and the
    nt_bracket_reachability join fetch in the same test run."""

    def __init__(self, signal_sim_rows, reachability_rows=None):
        self.signal_sim_rows = signal_sim_rows
        self.reachability_rows = reachability_rows or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        select = (params or {}).get("select", "")
        if "distance_atr_bucket,pct_expired" in select or "vol_tier,distance_atr_bucket,pct_expired" in select:
            return FakeResp(self.reachability_rows)
        return FakeResp(self.signal_sim_rows)


def _run(signal_sim_rows, reachability_rows=None):
    client = FakeClient(signal_sim_rows, reachability_rows)
    with (
        patch(
            "regime_platform.sweep_orchestrator.calibration_bias_bucket.settings.SUPABASE_URL",
            "https://x.supabase.co",
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_bias_bucket._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_bias_bucket._url",
            return_value="https://x.supabase.co/rest/v1/nt_signal_sim",
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_bias_bucket.httpx.AsyncClient",
            return_value=client,
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_bias_bucket.upsert_calibration_bias_by_bucket",
            new=AsyncMock(),
        ),
    ):
        return asyncio.run(build_calibration_bias_by_bucket())


def _row(symbol, regime, entry, tp, atr, outcome, tp_bricks=5, sl_bricks=3, p_win=0.6):
    return {
        "symbol": symbol,
        "ts": "2026-09-01T00:00:00Z",
        "entry_price": entry,
        "take_profit": tp,
        "atr": atr,
        "p_win": p_win,
        "outcome": outcome,
        "resolved_at": "2026-09-01T04:00:00Z",
        "tp_bricks": tp_bricks,
        "sl_bricks": sl_bricks,
        "regime": regime,
    }


def test_same_symbol_same_bucket_different_vol_tier_stay_separate():
    """The exact motivating case: XAUUSD 'sell' hits in med_high_vol_bear
    vs. loses in med_low_vol_bear, same distance/ATR bucket. Must produce
    two separate output rows, not one blended one."""
    rows = [
        # med_high_vol_bear: wins. Distinct take_profit per row so each is a
        # separate signal (post-2026-10-04 episode dedup collapses rows that
        # share symbol/signal/regime/entry/tp/sl — these must NOT collapse).
        _row("XAUUSD", "med_high_vol_bear", 100.0, 140.0, 10.0, "tp_hit"),
        _row("XAUUSD", "med_high_vol_bear", 100.0, 141.0, 10.0, "tp_hit"),
        _row("XAUUSD", "med_high_vol_bear", 100.0, 142.0, 10.0, "sl_hit"),
        # med_low_vol_bear: same bucket (distance≈40, atr=10 -> ratio 4 -> "4+"), all losses
        _row("XAUUSD", "med_low_vol_bear", 100.0, 140.0, 10.0, "sl_hit"),
        _row("XAUUSD", "med_low_vol_bear", 100.0, 141.0, 10.0, "sl_hit"),
    ]
    out = _run(rows)
    by_key = {(r["symbol"], r["vol_tier"], r["distance_atr_bucket"]): r for r in out}

    assert ("XAUUSD", "med_high_vol", "4+") in by_key
    assert ("XAUUSD", "med_low_vol", "4+") in by_key

    high_vol_row = by_key[("XAUUSD", "med_high_vol", "4+")]
    low_vol_row = by_key[("XAUUSD", "med_low_vol", "4+")]

    assert high_vol_row["win_rate"] == round(2 / 3, 4)
    assert low_vol_row["win_rate"] == 0.0
    # Different win rates -> different expectancy_r -> proves they were
    # NOT folded into one (symbol, bucket) aggregate.
    assert high_vol_row["expectancy_r"] != low_vol_row["expectancy_r"]


def test_missing_regime_buckets_into_unknown_not_dropped():
    rows = [_row("EURUSD", None, 1.10, 1.14, 0.10, "tp_hit")]
    out = _run(rows)
    assert len(out) == 1
    assert out[0]["vol_tier"] == "unknown"


def test_reachability_join_is_keyed_on_vol_tier_too():
    """pct_expired must be looked up by (symbol, vol_tier, bucket), not
    just (symbol, bucket) — a reachability row for the wrong vol_tier
    must not leak its pct_expired into this bucket."""
    signal_sim_rows = [
        _row("XAUUSD", "low_vol_bear", 100.0, 104.0, 10.0, "tp_hit"),  # ratio 0.4 -> "<0.5"
    ]
    reachability_rows = [
        {"symbol": "XAUUSD", "vol_tier": "high_vol", "distance_atr_bucket": "<0.5", "pct_expired": 0.9},
        {"symbol": "XAUUSD", "vol_tier": "low_vol", "distance_atr_bucket": "<0.5", "pct_expired": 0.1},
    ]
    out = _run(signal_sim_rows, reachability_rows)
    assert len(out) == 1
    assert out[0]["vol_tier"] == "low_vol"
    assert out[0]["pct_expired"] == 0.1  # NOT 0.9 (the high_vol row's value)


def test_expectancy_r_well_defined_at_zero_wins_per_vol_tier():
    rows = [
        _row("USDCAD", "high_vol_bull", 1.30, 1.34, 0.10, "sl_hit"),
        _row("USDCAD", "high_vol_bull", 1.30, 1.34, 0.10, "sl_hit"),
    ]
    out = _run(rows)
    assert len(out) == 1
    assert out[0]["win_rate"] == 0.0
    assert out[0]["expectancy_r"] == -1.0
