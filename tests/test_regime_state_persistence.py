"""Restart-safe regime state + accelerated G2 check (2026-09-25,
worklog/kairos/20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md).

A Studio restart must leave the next tick exactly where the previous
process left it: same debounced label, same run start / ATR-at-start, same
label-continuity means and EMAs. Plus the weekly-check helpers (#5 week
snap, #4 heartbeat, per-horizon maturity, pre-168 tier reconstruction)."""

from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from regime_platform.core.regime_engine import RegimeLabelDebouncer
from regime_platform.sweep_orchestrator import kairos as K
from regime_platform.sweep_orchestrator import regime_state_store as S

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("kairos_weekly_check", ROOT / "scripts" / "kairos_weekly_check.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)


def _orch():
    return SimpleNamespace(
        _regime_label_means={}, _regime_debouncers={}, _kairos_runs=K.KairosRunTracker(),
        _markov_ema={}, _regime_trend_ema={}, _last_regime={},
    )


class TestDebouncerRoundTrip:
    def test_mid_challenge_state_survives(self):
        d = RegimeLabelDebouncer(3)
        for lab in ("a", "b", "b"):          # b is 2/3 of the way to promotion
            d.update(lab)
        r = RegimeLabelDebouncer.from_dict(json.loads(json.dumps(d.to_dict())), 3)
        assert r.update("b") == d.update("b") == "b"   # third b promotes in both

    def test_cold_debouncer_differs_from_restored(self):
        d = RegimeLabelDebouncer(3)
        d.update("a")
        r = RegimeLabelDebouncer.from_dict(d.to_dict(), 3)
        assert r.update("b") == "a"          # restored: one flap is held
        assert RegimeLabelDebouncer(3).update("b") == "b"   # cold: accepted at once


class TestRunTrackerRoundTrip:
    def test_age_and_atr_start_survive(self):
        t = K.KairosRunTracker()
        t.update("EURUSD", "low_vol_bull", 1000.0, 0.5)
        r = K.KairosRunTracker.from_dict(json.loads(json.dumps(t.to_dict())))
        age, atr0 = r.update("EURUSD", "low_vol_strong_bull", 1000.0 + 7200, 0.9)
        assert age == pytest.approx(2.0) and atr0 == 0.5

    def test_bad_entries_skipped(self):
        r = K.KairosRunTracker.from_dict({"X": ["c"], "Y": ["low_vol|bull", 5.0, None]})
        assert r.update("Y", "low_vol_bull", 5.0 + 3600, 1.0) == (pytest.approx(1.0), 1.0)  # missing ATR back-filled
        assert r.update("X", "low_vol_bull", 10.0, 1.0)[0] == 0.0


class TestStateStore:
    def _populated(self):
        o = _orch()
        o._regime_label_means["AAPL"] = {"vol": {"low_vol": 0.01, "bad": float("nan")}, "trend": {"bull": 0.002}}
        d = RegimeLabelDebouncer(3)
        d.update("low_vol_bull")
        o._regime_debouncers["AAPL"] = d
        o._kairos_runs.update("AAPL", "low_vol_bull", time.time() - 3600, 1.2)
        o._markov_ema["AAPL"] = 0.61
        o._regime_trend_ema["AAPL"] = {"bull": 0.7}
        o._last_regime["AAPL"] = "low_vol_bull"
        return o

    def test_save_restore_fresh(self, tmp_path):
        p = tmp_path / "s.json"
        assert S.save(self._populated(), p)
        o2 = _orch()
        res = S.restore(o2, max_age_hours=3, n_confirm=3, path=p)
        assert res["restored"] and res["symbols"] == 1
        assert o2._regime_label_means["AAPL"]["vol"] == {"low_vol": 0.01}     # NaN dropped
        assert o2._regime_debouncers["AAPL"].update("high_vol_bear") == "low_vol_bull"
        age, atr0 = o2._kairos_runs.update("AAPL", "low_vol_bull", time.time(), 2.0)
        assert age == pytest.approx(1.0, abs=0.01) and atr0 == 1.2
        assert o2._markov_ema["AAPL"] == 0.61 and o2._last_regime["AAPL"] == "low_vol_bull"

    def test_stale_snapshot_ignored(self, tmp_path):
        p = tmp_path / "s.json"
        S.save(self._populated(), p)
        o2 = _orch()
        res = S.restore(o2, max_age_hours=3, n_confirm=3, path=p, now=time.time() + 4 * 3600)
        assert not res["restored"] and res["reason"].startswith("stale")
        assert o2._regime_debouncers == {} and o2._regime_label_means == {}

    def test_missing_corrupt_and_version(self, tmp_path):
        assert S.restore(_orch(), max_age_hours=3, n_confirm=3, path=tmp_path / "none.json")["reason"] == "no_snapshot"
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert S.restore(_orch(), max_age_hours=3, n_confirm=3, path=bad)["reason"].startswith("error")
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"version": 0, "saved_at": time.time()}))
        assert S.restore(_orch(), max_age_hours=3, n_confirm=3, path=old)["reason"] == "version_mismatch"

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        p = tmp_path / "s.json"
        S.save(self._populated(), p)
        assert p.exists() and not (tmp_path / "s.json.tmp").exists()

    def test_flag_defaults_live_and_yaml(self, monkeypatch):
        import yaml

        from regime_platform.core import feature_flags as ff
        monkeypatch.setattr(ff, "_load", lambda: {})
        assert ff.get_regime_state_persist_mode() == "live"
        assert ff.get_regime_state_persist_max_age_hours() == 3.0
        monkeypatch.setattr(ff, "_load", lambda: {"regime_state_persist_mode": "OFF"})
        assert ff.get_regime_state_persist_mode() == "off"
        cfg = yaml.safe_load((ROOT / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
        assert cfg["regime_state_persist_mode"] == "live"

    def test_orchestrator_wiring(self):
        src = (ROOT / "regime_platform/sweep_orchestrator/orchestrator.py").read_text(encoding="utf-8")
        assert "self._restore_regime_state()" in src.split("async def start(")[1][:400]
        assert "self._persist_regime_state()" in src.split("async def stop(")[1][:300]
        assert src.count("self._persist_regime_state()") >= 2


class TestWeeklyHelpers:
    def test_week_snap_fixes_w1_label(self):
        # The 09-24 23:22 run was labelled W1; by §12.2 it is W0.
        end, lab = W.schedule_week(datetime(2026, 9, 24, 23, 22, tzinfo=timezone.utc))
        assert lab == "W0" and end == datetime(2026, 9, 24, 8, tzinfo=timezone.utc)
        assert W.schedule_week(datetime(2026, 10, 1, 8, tzinfo=timezone.utc))[1] == "W1"
        assert W.schedule_week(datetime(2026, 11, 5, 9, tzinfo=timezone.utc))[1] == "W6"
        assert W.schedule_week(datetime(2026, 9, 23, tzinfo=timezone.utc))[1] == "pre-W0"

    def test_per_horizon_maturity(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        hr = pd.Series(pd.to_datetime([now - timedelta(hours=x) for x in (6, 7, 26, 27)]))
        assert W.mature_mask(hr, 4, now).tolist() == [False, True, True, True]
        assert W.mature_mask(hr, 24, now).tolist() == [False, False, False, True]

    def test_reconstruct_tier(self):
        t = pd.DataFrame({
            "qualified": [True, False, False, False],
            "regime_conf": [0.9, 0.9, 0.5, 0.9], "effective_kelly": [0.1, 0.1, 0.1, 0.1],
            "ev": [1, 1, 1, 1], "p_win": [0.6, 0.6, 0.6, 0.6], "p_win_dir": [None, None, None, 0.4],
            "sl_bricks": [3, 3, 3, 3], "tp_bricks": [5, 5, 5, 5],
        })
        assert W.reconstruct_tier(t).tolist() == ["qualified", "suppressed", "gate_fail", "gate_fail"]

    def test_heartbeat_state_parsable(self):
        s = K.heartbeat_state({"drift_backfill": {"patched": 3}}, datetime(2026, 9, 25, 3, 20, tzinfo=timezone.utc))
        assert s.startswith("last_run 2026-09-25T03:20:00Z")
        assert datetime.fromisoformat(s.split()[1].replace("Z", "+00:00")).hour == 3

    def test_kairos_daily_hourly_and_heartbeats(self):
        src = (ROOT / "regime_platform/sweep_orchestrator/orchestrator.py").read_text(encoding="utf-8")
        blk = src.split('id="kairos-daily"')[0][-400:]
        assert 'from_crontab("20 * * * *")' in blk
        assert 'write_job_heartbeat("kairos_daily_job"' in src
