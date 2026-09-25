"""Light-sweep startup catch-up (2026-09-25, worklog/kairos/
20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md §6).

The 04:18 UTC deploy restart on 2026-09-25 dropped the 04:15 slot for all
14 symbols: a fresh APScheduler never fires a slot that passed while the
process was down. These tests pin the pure slot/symbol logic and the
fail-safe wiring (no double tick, no tick when the read fails).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger

from regime_platform.sweep_orchestrator import orchestrator as orch_mod
from regime_platform.sweep_orchestrator.orchestrator import (
    LIGHT_CATCHUP_MIN_LEAD,
    _missed_light_slot,
    _unswept_symbols,
)

UTC = timezone.utc
TRIG = CronTrigger.from_crontab("*/15 * * * *", timezone="UTC")


def t(h, m, s=0):
    return datetime(2026, 9, 25, h, m, s, tzinfo=UTC)


# ── _missed_light_slot ─────────────────────────────────────────────────


def test_live_case_0418_restart_finds_0415_slot():
    assert _missed_light_slot(TRIG, t(4, 18, 1)) == t(4, 15)


def test_restart_just_after_slot_returns_that_slot():
    assert _missed_light_slot(TRIG, t(4, 15, 30)) == t(4, 15)


def test_restart_exactly_on_slot_counts_it_as_past():
    assert _missed_light_slot(TRIG, t(4, 15)) == t(4, 15)


def test_skipped_when_next_tick_is_too_close():
    # 04:26 → next fire 04:30 is 4 min away (< 5): let the real tick run.
    assert _missed_light_slot(TRIG, t(4, 26)) is None
    # 04:24:59 → 5m01s lead: catch up the 04:15 slot.
    assert _missed_light_slot(TRIG, t(4, 24, 59)) == t(4, 15)


def test_min_lead_boundary_is_inclusive_of_exactly_five_minutes():
    now = t(4, 30) - LIGHT_CATCHUP_MIN_LEAD  # 04:25:00, lead exactly 5 min
    assert _missed_light_slot(TRIG, now) == t(4, 15)


def test_only_most_recent_slot_is_returned():
    # Down 03:50 → 04:20: 04:00 and 04:15 both missed; only 04:15 is caught up.
    assert _missed_light_slot(TRIG, t(4, 20)) == t(4, 15)


def test_no_slot_within_lookback_returns_none():
    hourly = CronTrigger.from_crontab("0 */6 * * *", timezone="UTC")
    assert _missed_light_slot(hourly, t(4, 18)) is None  # last fire 00:00 > 60 min ago
    assert _missed_light_slot(hourly, t(6, 20)) == t(6, 0)


# ── _unswept_symbols ──────────────────────────────────────────────────


SYMS = [{"symbol": s, "asset_class": "fx"} for s in ("AAPL", "EURUSD", "XAUUSD")]


def test_unswept_all_missing():
    assert _unswept_symbols(SYMS, set()) == SYMS


def test_unswept_partial_slot_after_mid_group_death():
    assert [s["symbol"] for s in _unswept_symbols(SYMS, {"AAPL"})] == ["EURUSD", "XAUUSD"]


def test_unswept_none_missing():
    assert _unswept_symbols(SYMS, {"AAPL", "EURUSD", "XAUUSD", "BTCUSD"}) == []


# ── _catch_up_light_sweep_slot wiring ─────────────────────────────────


class _FakeOrch:
    """Just enough of SweepOrchestrator for the catch-up method."""

    _catch_up_light_sweep_slot = orch_mod.SweepOrchestrator._catch_up_light_sweep_slot

    def __init__(self, jobs):
        self._scheduler = SimpleNamespace(get_jobs=lambda: jobs)
        self.calls: list[tuple[str, list[str]]] = []

    async def _sweep_group(self, cron_expr, symbols):
        self.calls.append((cron_expr, [s["symbol"] for s in symbols]))


def _job(job_id="sweep-starsl15_star_star_star_star", expr="*/15 * * * *", syms=SYMS):
    return SimpleNamespace(id=job_id, args=[expr, syms],
                           trigger=CronTrigger.from_crontab(expr, timezone="UTC"))


def _run(orch, monkeypatch, swept, *, mode="live", now=t(4, 18, 1)):
    import regime_platform.core.feature_flags as ff
    import regime_platform.sweep_orchestrator.supabase_client as sc

    seen = []

    async def fake_fetch(since):
        seen.append(since)
        return swept

    monkeypatch.setattr(ff, "get_light_sweep_catchup_mode", lambda: mode)
    monkeypatch.setattr(sc, "fetch_light_swept_symbols_since", fake_fetch)

    async def go():
        await orch._catch_up_light_sweep_slot(now=now)
        await asyncio.sleep(0)  # let create_task'd sweep run
        await asyncio.sleep(0)

    asyncio.run(go())
    return seen


def test_wiring_runs_missing_symbols_for_missed_slot(monkeypatch):
    o = _FakeOrch([_job()])
    seen = _run(o, monkeypatch, swept={"AAPL"})
    assert seen == [t(4, 15)]
    assert o.calls == [("*/15 * * * *", ["EURUSD", "XAUUSD"])]


def test_wiring_noop_when_slot_fully_swept(monkeypatch):
    o = _FakeOrch([_job()])
    _run(o, monkeypatch, swept={"AAPL", "EURUSD", "XAUUSD"})
    assert o.calls == []


def test_wiring_fail_safe_when_read_fails(monkeypatch):
    o = _FakeOrch([_job()])
    _run(o, monkeypatch, swept=None)
    assert o.calls == []


def test_wiring_off_flag_disables(monkeypatch):
    o = _FakeOrch([_job()])
    seen = _run(o, monkeypatch, swept=set(), mode="off")
    assert seen == [] and o.calls == []


def test_wiring_ignores_non_light_jobs_and_near_tick(monkeypatch):
    heavy = SimpleNamespace(id="heavy-sweep-x", args=["0 20 * * *", SYMS],
                            trigger=CronTrigger.from_crontab("0 20 * * *", timezone="UTC"))
    o = _FakeOrch([heavy, _job()])
    seen = _run(o, monkeypatch, swept=set(), now=t(4, 27))  # next tick 04:30 < 5 min
    assert seen == [] and o.calls == []


def test_flag_getter_defaults_live(monkeypatch):
    import regime_platform.core.feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {})
    assert ff.get_light_sweep_catchup_mode() == "live"
    monkeypatch.setattr(ff, "_load", lambda: {"light_sweep_catchup_mode": "OFF"})
    assert ff.get_light_sweep_catchup_mode() == "off"
    monkeypatch.setattr(ff, "_load", lambda: {"light_sweep_catchup_mode": "bogus"})
    assert ff.get_light_sweep_catchup_mode() == "live"


def test_yaml_ships_live():
    import regime_platform.core.feature_flags as ff
    assert ff.get_light_sweep_catchup_mode() == "live"
