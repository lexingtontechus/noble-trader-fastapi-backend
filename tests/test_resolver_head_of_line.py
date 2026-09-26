"""Resolver head-of-line fix (2026-09-26, worklog/kairos/
20260926_RESOLVER_HEAD_OF_LINE_FIX.md).

The pending queue is oldest-first. Before the fix the resolver stopped at the
first batch that resolved nothing, so TP/SL already decided behind a head of
still-young signals were never reached on that run. These tests drive
``resolve_pending_signals`` against a fake ``outcome IS NULL`` pool that
behaves like PostgREST (terminal PATCH removes the row; offset pages).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator import eod_resolver as mod


def _sig(i: int, decided: bool) -> dict:
    # decided → closes hit TP (111 ≥ 110); young → 3 bars, no touch → pending.
    return {
        "signal_id": f"sig-{i}", "symbol": "AAPL", "signal": "buy",
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        "ts": f"2026-09-25T{i % 24:02d}:00:00Z", "tradingview_symbol": "NASDAQ:AAPL",
        "_decided": decided,
    }


class _Pool:
    """Fake nt_signal_sim pending pool: ordered, offset-paged, terminal
    PATCH removes the row (it no longer matches outcome IS NULL)."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.fetch_calls: list[tuple[int, int]] = []

    async def fetch(self, limit, min_ts=None, offset=0):
        self.fetch_calls.append((limit, offset))
        return [dict(r) for r in self.rows[offset:offset + limit]]

    async def patch_signal(self, signal_id, patch_):
        if patch_.get("outcome") in ("tp_hit", "sl_hit", "expired"):
            self.rows = [r for r in self.rows if r["signal_id"] != signal_id]
        return True


def _anchor(closes, ts, fetch_ts):  # bypass real-time windowing
    return list(closes)


def _run(pool, *, mode="full", limit=3, max_total=0, pause=0.0):
    # Per-signal closes: decided rows hit TP, young rows have 3 flat bars.
    real_judge = mod._judge

    def judge(sig, closes):
        closes = [105.0, 111.0] if sig.get("_decided") else [101.0, 102.0, 103.0]
        return real_judge(sig, closes)

    write_cal = AsyncMock()
    sleep = AsyncMock()
    with (
        patch.object(mod, "_fetch_pending", new=pool.fetch),
        patch.object(mod, "_patch_signal", new=pool.patch_signal),
        patch.object(mod, "_write_calibration_record", new=write_cal),
        patch.object(mod, "fetch_candles_for_symbols", new=AsyncMock(return_value={"AAPL": [101.0]})),
        patch.object(mod, "anchor_post_entry_closes", side_effect=_anchor),
        patch.object(mod, "_judge", side_effect=judge),
        patch.object(mod.asyncio, "sleep", new=sleep),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
              new=AsyncMock(return_value=[{"symbol": "AAPL", "tradingview_symbol": "NASDAQ:AAPL"}])),
        patch("regime_platform.core.feature_flags.get_eod_resolver_scan_mode", return_value=mode),
        patch("regime_platform.core.feature_flags.get_eod_resolver_page_pause_s", return_value=pause),
        patch.object(mod.settings, "SUPABASE_URL", "https://x.supabase.co"),
    ):
        summary = asyncio.run(mod.resolve_pending_signals(limit=limit, max_total=max_total))
    return summary, write_cal, sleep


# The live shape: a head of young signals, decided ones further down.
def _head_of_line_pool():
    return _Pool([_sig(i, False) for i in range(6)] + [_sig(i, True) for i in range(6, 9)]
                 + [_sig(9, False)])


class TestHeadOfLine:
    def test_head_mode_reproduces_the_bug(self):
        pool = _head_of_line_pool()
        summary, write_cal, _ = _run(pool, mode="head")
        assert summary["resolved"] == 0            # stopped at the young head
        assert write_cal.await_count == 0
        assert len(pool.fetch_calls) == 1
        assert summary["scan_mode"] == "head"

    def test_full_mode_reaches_decided_rows_behind_the_head(self):
        pool = _head_of_line_pool()
        summary, write_cal, _ = _run(pool, mode="full")
        assert summary["resolved"] == 3
        assert summary["by_outcome"] == {"tp_hit": 3}
        assert write_cal.await_count == 3          # calibration records written
        assert summary["insufficient_bars"] == 7   # each young row judged once
        assert [s["signal_id"] for s in pool.rows] == [f"sig-{i}" for i in (0, 1, 2, 3, 4, 5, 9)]
        assert summary["scan_mode"] == "full"

    def test_offset_advances_only_past_rows_left_pending(self):
        pool = _head_of_line_pool()
        _run(pool, mode="full")
        # batch1 young×3 → skip 3; batch2 young×3 → skip 6; batch3 decided×3
        # (all left the pool) → skip stays 6; batch4 = sig-9 (short, no progress) → end.
        assert [off for _, off in pool.fetch_calls] == [0, 3, 6, 6]

    def test_no_row_judged_twice(self):
        pool = _head_of_line_pool()
        seen: list[str] = []
        orig = pool.fetch

        async def spy(limit, min_ts=None, offset=0):
            rows = await orig(limit, min_ts=min_ts, offset=offset)
            seen.extend(r["signal_id"] for r in rows)
            return rows

        pool.fetch = spy
        _run(pool, mode="full")
        assert len(seen) == len(set(seen)) == 10

    def test_terminates_on_all_young_queue(self):
        pool = _Pool([_sig(i, False) for i in range(7)])
        summary, _, _ = _run(pool, mode="full")
        assert summary["resolved"] == 0
        assert summary["insufficient_bars"] == 7
        assert [off for _, off in pool.fetch_calls] == [0, 3, 6]

    def test_terminates_on_exact_multiple_of_limit(self):
        pool = _Pool([_sig(i, False) for i in range(6)])
        summary, _, _ = _run(pool, mode="full")
        assert summary["insufficient_bars"] == 6
        assert [off for _, off in pool.fetch_calls] == [0, 3, 6]  # last fetch empty

    def test_all_decided_drains_without_offset(self):
        pool = _Pool([_sig(i, True) for i in range(7)])
        summary, _, _ = _run(pool, mode="full")
        assert summary["resolved"] == 7 and pool.rows == []
        assert all(off == 0 for _, off in pool.fetch_calls)

    def test_max_total_still_caps(self):
        pool = _Pool([_sig(i, False) for i in range(30)])
        summary, _, _ = _run(pool, mode="full", max_total=9)
        assert summary["batches"] == 3

    def test_pause_between_batches_not_before_first(self):
        pool = _head_of_line_pool()
        summary, _, sleep = _run(pool, mode="full", pause=2.0)
        assert sleep.await_count == summary["batches"] - 1
        sleep.assert_awaited_with(2.0)

    def test_no_pause_when_zero(self):
        pool = _head_of_line_pool()
        _, _, sleep = _run(pool, mode="full", pause=0.0)
        sleep.assert_not_awaited()


class TestFlags:
    @pytest.mark.parametrize("raw,expected", [(None, "full"), ("head", "head"), ("FULL", "full"), ("x", "full")])
    def test_scan_mode(self, raw, expected):
        from regime_platform.core import feature_flags as ff
        cfg = {} if raw is None else {"eod_resolver_scan_mode": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_eod_resolver_scan_mode() == expected

    @pytest.mark.parametrize("raw,expected", [(None, 2.0), (0, 0.0), (5, 5.0), (-1, 0.0), (99, 30.0), ("x", 2.0)])
    def test_page_pause(self, raw, expected):
        from regime_platform.core import feature_flags as ff
        cfg = {} if raw is None else {"eod_resolver_page_pause_s": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_eod_resolver_page_pause_s() == expected

    def test_yaml_ships_full(self):
        from pathlib import Path
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "feature_flags.yaml")
                             .read_text(encoding="utf-8"))
        assert cfg["eod_resolver_scan_mode"] == "full"
        assert cfg["eod_resolver_page_pause_s"] == 2.0
