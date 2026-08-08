"""
Tests for the Part II paper portfolio (regime_platform/sweep_orchestrator/paper_book.py).

Covers the pure sizing + PnL functions (no network):
  - compute_size: kelly sizing with cap, effective_kelly preference, fallback
  - compute_realized_pnl: $ PnL convention and r-multiple
  - PAPER_POSITION_COLUMNS completeness
"""

import pytest

from regime_platform.sweep_orchestrator.paper_book import (
    PAPER_POSITION_COLUMNS,
    compute_size,
    compute_realized_pnl,
)


class TestComputeSize:
    def test_effective_kelly_preferred(self):
        # effective_kelly 0.10 on base 1000 → notional 100 (10% of base)
        s = compute_size(entry_price=1.1000, kelly_f=0.05, effective_kelly=0.10)
        assert s["size_notional"] == pytest.approx(100.0, abs=0.01)
        assert s["size_units"] == pytest.approx(90.909, abs=0.01)

    def test_kelly_fallback_when_no_effective(self):
        s = compute_size(entry_price=1.1000, kelly_f=0.05)
        assert s["size_notional"] == pytest.approx(50.0, abs=0.01)

    def test_flat_fallback_when_neither(self):
        # No kelly → flat 5% of base = 50
        s = compute_size(entry_price=2.0000)
        assert s["size_notional"] == pytest.approx(50.0, abs=0.01)

    def test_hard_cap_20_percent(self):
        # kelly 1.0 would give 1000 — capped at 20% of base = 200
        s = compute_size(entry_price=1.0000, kelly_f=1.0, effective_kelly=1.0)
        assert s["size_notional"] == pytest.approx(200.0, abs=0.01)
        assert s["size_units"] == pytest.approx(200.0, abs=0.01)

    def test_kelly_clamped_to_unit_interval(self):
        s = compute_size(entry_price=1.0000, kelly_f=5.0)
        assert s["size_notional"] <= 200.0

    def test_zero_entry_units_zero(self):
        s = compute_size(entry_price=0.0, kelly_f=0.1)
        assert s["size_units"] == 0.0


class TestComputeRealizedPnl:
    def test_tp_hit_pnl(self):
        # 6-brick TP, brick 0.001, size_units 28.99 ($200 @ 6.9) → $0.174
        pnl = compute_realized_pnl(
            "tp_hit", pnl_bricks=6, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == pytest.approx(0.1739, abs=0.0005)
        assert pnl["r_multiple"] == pytest.approx(2.0, abs=0.0001)

    def test_sl_hit_pnl_negative(self):
        pnl = compute_realized_pnl(
            "sl_hit", pnl_bricks=-3, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == pytest.approx(-0.0870, abs=0.0005)
        assert pnl["r_multiple"] == pytest.approx(-1.0, abs=0.0001)

    def test_expired_zero_r(self):
        pnl = compute_realized_pnl(
            "expired", pnl_bricks=0, brick_size=0.001,
            size_units=28.9855, sl_bricks=3,
        )
        assert pnl["realized_pnl"] == 0.0
        assert pnl["r_multiple"] == 0.0


class TestColumns:
    def test_columns_include_all_position_fields(self):
        for col in (
            "position_id", "signal_id", "symbol", "venue", "direction",
            "entry_price", "stop_loss", "take_profit",
            "size_units", "size_notional", "open_ts", "close_ts", "status",
            "exit_price", "realized_pnl", "pnl_bricks", "r_multiple",
            "source", "resolved_at",
        ):
            assert col in PAPER_POSITION_COLUMNS, f"missing column: {col}"


# ─── Close-pass regression tests (2026-08-08) ───────────────────────────────
# Two live bugs left the paper book permanently $0:
#   1. _fetch_resolved_signals ordered ts.ASC + reconcile passed limit=200 →
#      only the 200 OLDEST resolved signals were ever examined, so the newest
#      resolved signals (with open positions) were never closed → closed=0.
#   2. _fetch_open_positions did NOT select size_units → every close would
#      have booked realized_pnl = pnl_bricks × brick_size × 0 = $0 anyway.
# These tests pin the FIXED URL shapes (monkeypatched httpx, no network).
class TestClosePassFetch:
    def _fake_settings(self, monkeypatch):
        class FakeSettings:
            SUPABASE_URL = "https://test.supabase.co"
            SUPABASE_KEY = "test-key"
        import regime_platform.core.settings as settings_mod
        monkeypatch.setattr(settings_mod, "settings", FakeSettings())

    def test_resolved_signals_newest_first_with_high_limit(self, monkeypatch):
        """Close pass must fetch resolved signals ts.DESC with a generous
        limit — NOT the 200-oldest asc window that caused closed=0 forever."""
        import httpx
        import regime_platform.sweep_orchestrator.paper_book as pb
        self._fake_settings(monkeypatch)

        captured = {}
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return []
        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, headers=None):
                captured["url"] = url
                return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        import asyncio
        result = asyncio.run(pb._fetch_resolved_signals(2000))
        assert result == []
        assert "order=ts.desc" in captured["url"], captured["url"]
        assert "limit=2000" in captured["url"], captured["url"]
        assert "outcome=in.(tp_hit,sl_hit,expired)" in captured["url"]

    def test_open_positions_selects_size_units(self, monkeypatch):
        """Close pass needs size_units from the position row or PnL = $0."""
        import httpx
        import regime_platform.sweep_orchestrator.paper_book as pb
        self._fake_settings(monkeypatch)

        captured = {}
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return []
        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, headers=None):
                captured["url"] = url
                return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        import asyncio
        result = asyncio.run(pb._fetch_open_positions())
        assert result == []
        assert "select=position_id,signal_id,status,size_units" in captured["url"], captured["url"]

    def test_reconcile_uses_separate_close_limit(self, monkeypatch):
        """reconcile() must pass close_limit to the close fetch, not the
        open-pass limit."""
        import regime_platform.sweep_orchestrator.paper_book as pb
        calls = {}

        async def fake_open_positions():
            return []
        async def fake_qualified(limit):
            calls["open_limit"] = limit
            return []
        async def fake_resolved(limit):
            calls["close_limit"] = limit
            return []

        monkeypatch.setattr(pb, "_fetch_open_positions", fake_open_positions)
        monkeypatch.setattr(pb, "_fetch_qualified_without_position", fake_qualified)
        monkeypatch.setattr(pb, "_fetch_resolved_signals", fake_resolved)

        import asyncio
        asyncio.run(pb.reconcile(limit=200, close_limit=2000))
        assert calls.get("open_limit") == 200, calls
        assert calls.get("close_limit") == 2000, calls
