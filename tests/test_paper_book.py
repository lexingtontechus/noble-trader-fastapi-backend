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


# ─── Close-pass regression tests (2026-08-08 + M4 orphan fix 2026-08-15) ────────
# Two live bugs left the paper book permanently $0:
#   1. _fetch_resolved_signals ordered ts.ASC + reconcile passed limit=200 →
#      only the 200 OLDEST resolved signals were ever examined, so the newest
#      resolved signals (with open positions) were never closed → closed=0.
#   2. _fetch_open_positions did NOT select size_units → every close would
#      have booked realized_pnl = pnl_bricks × brick_size × 0 = $0 anyway.
#
# The M4 orphan bug (2026-08-15) was a third regression of #1: the 2026-08-08
# fix changed to ts.desc&limit=2000, which only saw the newest 2000 resolved
# signals (~12–15h at 3,200 sig/day) and NEVER reached signals resolved 9+
# days ago whose positions were still open.
#
# These tests pin the FIXED query shapes (monkeypatched httpx, no network).
class TestClosePassFetch:
    def _fake_settings(self, monkeypatch):
        class FakeSettings:
            SUPABASE_URL = "https://test.supabase.co"
            SUPABASE_KEY = "test-key"
        import regime_platform.core.settings as settings_mod
        monkeypatch.setattr(settings_mod, "settings", FakeSettings())

    def test_resolved_signals_uses_resolved_at_window(self, monkeypatch):
        """Close pass must fetch resolved signals filtered on resolved_at=gte.{cutoff}
        — NOT order=ts.desc&limit=2000 which only saw the newest 2000 rows
        and missed 9-day-old resolved signals (M4 orphan bug)."""
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
            async def get(self, url, headers=None, params=None):
                captured["url"] = url
                captured["params"] = str(params) if params else ""
                return FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        import asyncio
        result = asyncio.run(pb._fetch_resolved_signals(resolved_window_days=14))
        assert result == []
        assert "resolved_at.gte." in captured["url"], captured["url"]
        assert "or=(resolved_at.gte." in captured["url"], "must include resolved_at IS NULL fallback for old-style resolved signals"
        # Microseconds in timestamp contain '.' which conflicts with PostgREST
        # column.operator.value dot-delimiter syntax — must be stripped.
        # The timestamp value is between 'gte.' and the closing ')' of the or clause.
        import re
        ts_match = re.search(r'resolved_at\.gte\.([^,)&]+)', captured["url"])
        assert ts_match is not None, "could not extract timestamp from URL"
        ts_value = ts_match.group(1)
        assert "." not in ts_value, f"timestamp {ts_value} must not contain microsecond dots"
        assert "outcome=in.(tp_hit,sl_hit,expired)" in captured["url"]
        assert "order=resolved_at.desc" in captured["url"], captured["url"]
        assert "taken" not in captured["url"], "non-existent 'taken' column in select"
        # Must NOT use the old ts.desc&limit pattern
        assert "order=ts.desc" not in captured["url"], captured["url"]
        assert "limit=" not in captured["url"], captured["url"]

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

    def test_reconcile_close_pass_uses_signal_id_batch_lookup(self, monkeypatch):
        """reconcile() close pass must iterate open positions and batch-lookup
        their signals by signal_id (in.() query) — NOT iterate resolved signals
        from _fetch_resolved_signals (which used the broken ts.desc&limit window,
        M4 orphan bug 2026-08-15)."""
        import regime_platform.sweep_orchestrator.paper_book as pb
        from unittest.mock import AsyncMock
        import asyncio

        open_positions = [
            {"position_id": "p1", "signal_id": "sig-a", "status": "open",
             "size_units": 10.0, "open_ts": "2026-08-06T21:05:15Z"},
            {"position_id": "p2", "signal_id": "sig-b", "status": "open",
             "size_units": 20.0, "open_ts": "2026-08-06T21:30:00Z"},
        ]
        resolved_signals = [
            {"signal_id": "sig-a", "outcome": "tp_hit", "pnl_bricks": 6,
             "brick_size": 0.001, "sl_bricks": 3, "exit_price": 1.1100,
             "resolved_at": "2026-08-13T00:33:00Z"},
            {"signal_id": "sig-b", "outcome": "sl_hit", "pnl_bricks": -3,
             "brick_size": 0.001, "sl_bricks": 3, "exit_price": 1.0900,
             "resolved_at": "2026-08-13T00:32:00Z"},
        ]
        sig_by_id = {s["signal_id"]: s for s in resolved_signals}

        calls = {}

        async def fake_open_positions():
            calls["open_positions_called"] = True
            return open_positions

        async def fake_resolved(limit=2000):
            calls["resolved_called"] = True
            return []

        # Patch _fetch_open_positions to return our test positions
        monkeypatch.setattr(pb, "_fetch_open_positions", fake_open_positions)
        # Patch _fetch_resolved_signals to detect if the old close pass
        # path is still used (it should NOT be called by the close pass)
        monkeypatch.setattr(pb, "_fetch_resolved_signals", fake_resolved)
        # Patch _patch_position so close_position doesn't hit the network
        monkeypatch.setattr(pb, "_patch_position", AsyncMock(return_value=True))
        # Mock the httpx client for the batch lookup
        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return resolved_signals

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def get(self, url, headers=None, params=None):
                calls["batch_lookup_url"] = url
                calls["batch_lookup_params"] = params
                return FakeResp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        # Also patch _fetch_qualified_without_position to avoid network on open pass
        monkeypatch.setattr(pb, "_fetch_qualified_without_position", AsyncMock(return_value=[]))
        # Patch open_position to avoid network
        monkeypatch.setattr(pb, "open_position", AsyncMock(return_value=False))

        result = asyncio.run(pb.reconcile())
        assert calls.get("open_positions_called") is True
        # The close pass should NOT call _fetch_resolved_signals anymore
        assert not calls.get("resolved_called"), "close pass still calls _fetch_resolved_signals (old broken pattern)"
        # The close pass should use signal_id in.() batch lookup
        assert "batch_lookup_url" in calls, "close pass did not perform batch signal lookup"
        assert "in.(" in str(calls.get("batch_lookup_params", "")), calls.get("batch_lookup_params")
        # All 2 positions should close (both have resolved signals in the mock)
        assert result["closed"] == 2, f"expected 2 closed, got {result}"
