"""Hourly calibration resolve (Kairos V3 §14.2 lever 1, 2026-09-25).

worklog/kairos/20260926_HOURLY_CALIBRATION_RESOLVE.md. Covers:
  1. flag getter: default shadow, unknown → shadow, off/live honoured;
  2. nightly-time helpers (evidence_hours_saved) incl. the day roll;
  3. _judge is the single verdict used by both writer and dry run
     (minimum-age gate preserved);
  4. dry_run_pending_signals: read-only (never PATCHes / inserts), pages
     with offset, counts TP/SL per symbol + qualified, windowed_out;
  5. _run_calibration_resolve_hourly: off → nothing; shadow → dry run only,
     never the writer; live → writer under the shared lock; live skips when
     the lock is held; heartbeat written in shadow and live;
  6. the nightly _run_eod_lookback takes the same lock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator import eod_resolver as mod


def _sig(i, symbol="AAPL", ts="2026-09-25T00:00:00Z", qualified=False, **kw):
    s = {
        "signal_id": f"sig-{i}", "symbol": symbol, "signal": "buy",
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "tp_bricks": 6, "sl_bricks": 3, "brick_size": 1.0,
        "ts": ts, "tradingview_symbol": f"TV:{symbol}", "qualified": qualified,
    }
    s.update(kw)
    return s


# ─── 1. flag ─────────────────────────────────────────────────────────────


class TestFlag:
    @pytest.mark.parametrize("raw,expected", [
        (None, "shadow"), ("shadow", "shadow"), ("live", "live"), ("OFF", "off"), ("bogus", "shadow"),
    ])
    def test_getter(self, raw, expected):
        from regime_platform.core import feature_flags as ff
        cfg = {} if raw is None else {"calibration_resolve_hourly_mode": raw}
        with patch.object(ff, "_load", return_value=cfg):
            assert ff.get_calibration_resolve_hourly_mode() == expected

    def test_yaml_ships_live(self):
        """Flipped shadow -> live 2026-09-27 (worklog/20260927_PULSE_ROADMAP_
        DECISIONS_AND_QUALITY_FORK.md §3 item 1: 'calibration_resolve_hourly
        -> Flip live'). Asserts the shipped value so a silent revert is caught."""
        from pathlib import Path

        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
        assert cfg["calibration_resolve_hourly_mode"] == "live"


# ─── 2. nightly-time helpers ─────────────────────────────────────────────


class TestNightlyHelpers:
    def test_before_nightly_same_day(self):
        now = datetime(2026, 9, 25, 0, 38, tzinfo=timezone.utc)
        assert mod.next_nightly_resolve(now) == datetime(2026, 9, 25, 1, 17, tzinfo=timezone.utc)
        assert mod.evidence_hours_saved(now) == pytest.approx(0.65)

    def test_after_nightly_rolls_to_next_day(self):
        now = datetime(2026, 9, 25, 1, 38, tzinfo=timezone.utc)
        assert mod.next_nightly_resolve(now) == datetime(2026, 9, 26, 1, 17, tzinfo=timezone.utc)
        assert mod.evidence_hours_saved(now) == pytest.approx(23.65)

    def test_exactly_at_nightly_is_next_day(self):
        now = datetime(2026, 9, 25, 1, 17, tzinfo=timezone.utc)
        assert mod.next_nightly_resolve(now).day == 26


# ─── 3. _judge ───────────────────────────────────────────────────────────


class TestJudge:
    def test_tp(self):
        v, r = mod._judge(_sig(1), [105.0, 111.0])
        assert v == "tp_hit" and r["outcome"] == "tp_hit"

    def test_sl(self):
        v, _ = mod._judge(_sig(1), [99.0, 94.0])
        assert v == "sl_hit"

    def test_young_no_touch_is_pending_not_expired(self):
        v, r = mod._judge(_sig(1), [101.0, 102.0, 103.0])
        assert v == "pending" and r["outcome"] == "expired"

    def test_missing_levels(self):
        assert mod._judge(_sig(1, stop_loss=None), [101.0]) == (None, None)

    def test_resolve_one_uses_same_verdict(self):
        """Writer and dry run cannot diverge: _resolve_one delegates to _judge."""
        with (
            patch.object(mod, "_patch_signal", new=AsyncMock(return_value=True)) as p,
            patch.object(mod, "_write_calibration_record", new=AsyncMock()) as w,
        ):
            assert asyncio.run(mod._resolve_one(_sig(1), [101.0, 102.0])) == "pending"
            p.assert_not_awaited()
            assert asyncio.run(mod._resolve_one(_sig(2), [105.0, 111.0])) == "tp_hit"
            p.assert_awaited_once()
            w.assert_awaited_once()


# ─── 4. dry run ──────────────────────────────────────────────────────────


def _run_dry(pages, candles, active=None, **kw):
    fetch = AsyncMock(side_effect=pages)
    with (
        patch.object(mod, "_fetch_pending", new=fetch),
        patch.object(mod, "fetch_candles_for_symbols", new=AsyncMock(return_value=candles)),
        patch.object(mod, "anchor_post_entry_closes", side_effect=lambda closes, *a, **k: list(closes)),
        patch.object(mod, "_patch_signal", new=AsyncMock()) as patch_mock,
        patch.object(mod, "_insert_row", new=AsyncMock()) as insert_mock,
        patch.object(mod, "_write_calibration_record", new=AsyncMock()) as calib_mock,
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            new=AsyncMock(return_value=active or []),
        ),
        patch.object(mod.settings, "SUPABASE_URL", "https://x.supabase.co"),
    ):
        out = asyncio.run(mod.dry_run_pending_signals(**kw))
    return out, fetch, patch_mock, insert_mock, calib_mock


class TestDryRun:
    def test_counts_and_writes_nothing(self):
        rows = [
            _sig(1, "AAPL", qualified=True, gate_outcome="qualified"),
            _sig(2, "AAPL", gate_outcome="suppressed"),
            _sig(3, "US500", gate_outcome="gate_fail"),
            _sig(4, "AAPL", gate_outcome="gate_fail"),
            _sig(5, "AAPL"),  # pre-G2 row, gate_outcome NULL
        ]
        candles = {"AAPL": [105.0, 111.0], "US500": [101.0, 102.0]}
        out, _, p, i, c = _run_dry([rows], candles, limit=10)
        assert out["would_resolve"] == 4
        assert out["by_outcome"] == {"tp_hit": 4}
        assert out["by_symbol"] == {"AAPL": {"tp_hit": 4, "sl_hit": 0}}
        assert out["qualified_tp_sl"] == 1
        assert out["by_gate"] == {
            "qualified": {"tp_hit": 1, "sl_hit": 0},
            "suppressed": {"tp_hit": 1, "sl_hit": 0},
            "gate_fail": {"tp_hit": 1, "sl_hit": 0},
            "unknown": {"tp_hit": 1, "sl_hit": 0},
        }
        assert out["insufficient_bars"] == 1
        assert out["scanned"] == 5
        p.assert_not_awaited()
        i.assert_not_awaited()
        c.assert_not_awaited()

    def test_pages_with_offset_until_short_page(self):
        page1 = [_sig(i) for i in range(2)]
        page2 = [_sig(9)]
        out, fetch, *_ = _run_dry([page1, page2], {"AAPL": [105.0, 111.0]}, limit=2)
        assert out["scanned"] == 3
        offsets = [c.kwargs.get("offset", 0) for c in fetch.call_args_list]
        assert offsets == [0, 2]

    def test_max_total_bounds_paging(self):
        full = [_sig(i) for i in range(2)]
        out, fetch, *_ = _run_dry([full, full, full, full], {"AAPL": [101.0]}, limit=2, max_total=4)
        assert fetch.call_count == 2
        assert out["scanned"] == 4

    def test_windowed_out_and_missing_candles(self):
        rows = [_sig(1, "AAPL"), _sig(2, "US500")]
        fetch = AsyncMock(side_effect=[rows])
        with (
            patch.object(mod, "_fetch_pending", new=fetch),
            patch.object(mod, "fetch_candles_for_symbols", new=AsyncMock(return_value={"AAPL": [105.0]})),
            patch.object(mod, "anchor_post_entry_closes", return_value=None),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(mod.settings, "SUPABASE_URL", "https://x.supabase.co"),
        ):
            out = asyncio.run(mod.dry_run_pending_signals(limit=10))
        assert out["windowed_out"] == 1   # AAPL had candles, entry outside window
        assert out["errors"] == 1         # US500 no candles
        assert out["would_resolve"] == 0

    def test_query_failure_never_raises(self):
        with (
            patch.object(mod, "_fetch_pending", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(mod.settings, "SUPABASE_URL", "https://x.supabase.co"),
        ):
            out = asyncio.run(mod.dry_run_pending_signals())
        assert out["errors"] == 1 and out["would_resolve"] == 0

    def test_no_supabase_is_noop(self):
        with patch.object(mod.settings, "SUPABASE_URL", ""):
            out = asyncio.run(mod.dry_run_pending_signals())
        assert out["scanned"] == 0


# ─── 5/6. orchestrator job + shared lock ─────────────────────────────────


def _orch():
    from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
    return object.__new__(SweepOrchestrator)


DRY = {"would_resolve": 3, "by_outcome": {"tp_hit": 1, "sl_hit": 1, "expired": 1},
       "by_symbol": {"AAPL": {"tp_hit": 1, "sl_hit": 1}}, "qualified_tp_sl": 1,
       "insufficient_bars": 5, "windowed_out": 0, "errors": 0, "scanned": 8, "hours_saved": 2.6}


def _run_job(orch, mode, hold_lock=False):
    dry = AsyncMock(return_value=dict(DRY))
    writer = AsyncMock(return_value={"resolved": 2, "by_outcome": {"tp_hit": 1, "sl_hit": 1}})
    hb = AsyncMock(return_value=True)

    async def go():
        lock = orch._get_resolver_lock()
        if hold_lock:
            await lock.acquire()
        try:
            with (
                patch("regime_platform.core.feature_flags.get_calibration_resolve_hourly_mode", return_value=mode),
                patch.object(mod, "dry_run_pending_signals", new=dry),
                patch.object(mod, "resolve_pending_signals", new=writer),
                patch("regime_platform.sweep_orchestrator.kairos.write_job_heartbeat", new=hb),
            ):
                await orch._run_calibration_resolve_hourly()
        finally:
            if hold_lock:
                lock.release()

    asyncio.run(go())
    return dry, writer, hb


class TestHourlyJob:
    def test_off_does_nothing(self):
        dry, writer, hb = _run_job(_orch(), "off")
        dry.assert_not_awaited()
        writer.assert_not_awaited()
        hb.assert_not_awaited()

    def test_shadow_never_writes(self):
        dry, writer, hb = _run_job(_orch(), "shadow")
        dry.assert_awaited_once()
        writer.assert_not_awaited()
        mech, summary = hb.await_args.args
        assert mech == "calibration_resolve_hourly"
        assert summary["mode"] == "shadow" and summary["would_resolve"] == 3
        assert "by_symbol" not in summary  # heartbeat stays one line

    def test_live_calls_writer(self):
        orch = _orch()
        dry, writer, hb = _run_job(orch, "live")
        writer.assert_awaited_once()
        dry.assert_not_awaited()
        summary = hb.await_args.args[1]
        assert summary["resolved"] == 2 and "hours_saved" in summary
        assert not orch._get_resolver_lock().locked()

    def test_live_skips_when_resolver_busy(self):
        dry, writer, hb = _run_job(_orch(), "live", hold_lock=True)
        writer.assert_not_awaited()
        assert hb.await_args.args[1]["skipped"] == "resolver_busy"

    def test_writer_failure_never_raises(self):
        orch = _orch()
        hb = AsyncMock(return_value=True)

        async def go():
            with (
                patch("regime_platform.core.feature_flags.get_calibration_resolve_hourly_mode", return_value="live"),
                patch.object(mod, "resolve_pending_signals", new=AsyncMock(side_effect=RuntimeError("x"))),
                patch("regime_platform.sweep_orchestrator.kairos.write_job_heartbeat", new=hb),
            ):
                await orch._run_calibration_resolve_hourly()

        asyncio.run(go())
        assert hb.await_args.args[1]["error"] == "RuntimeError"
        assert not orch._get_resolver_lock().locked()

    def test_nightly_takes_the_same_lock(self):
        orch = _orch()
        seen = {}

        async def writer(*a, **k):
            seen["locked"] = orch._get_resolver_lock().locked()
            return {"resolved": 0, "by_outcome": {}, "errors": 0}

        async def go():
            with (
                patch.object(mod, "resolve_pending_signals", new=writer),
                patch("regime_platform.sweep_orchestrator.paper_book.reconcile",
                      new=AsyncMock(return_value={"opened": 0, "closed": 0, "errors": 0})),
                patch("regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                      new=AsyncMock(return_value={"checked": 0, "actions": [], "errors": 0})),
                patch("regime_platform.sweep_orchestrator.eod_retention.run_eod_retention",
                      new=AsyncMock(return_value={"deleted_resolved": 0, "expired_stale": 0, "errors": 0})),
                patch.object(orch, "_run_bracket_reachability", new=AsyncMock(), create=True),
            ):
                try:
                    await orch._run_eod_lookback()
                except Exception:  # later EOD parts are not under test here
                    pass

        asyncio.run(go())
        assert seen.get("locked") is True
        assert not orch._get_resolver_lock().locked()

    def test_job_registered_at_38(self):
        import inspect

        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
        src = inspect.getsource(SweepOrchestrator.start)
        assert 'id="calibration-resolve-hourly"' in src
        assert 'from_crontab("38 * * * *")' in src
