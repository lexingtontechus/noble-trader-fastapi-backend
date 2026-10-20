"""W-X4 (2026-10-19 weaknesses evaluation, P1): a LIVE chop gate on
efficiency_24h — the one number that directly measures "up-and-down
quickly".

The defect: efficiency_24h (Kaufman ER, kairos.py::efficiency_ratio) is
computed and stamped on every light tick but NO gate consumed it anywhere
(grep-verified; kairos_mode is stamp-only). Through the measured
69.1%-consolidating replay window the platform published at conf
0.649-0.793 the whole time — regime_conf is not a chop brake, and the
4-state HMM has no neutral state to abstain with.

The fix (LIVE, per the no-more-shadow mandate):
  * ``chop_gate`` flag block (mode: live, min_efficiency_24h: 0.30) —
    mechanism supports off|shadow|live like sibling flags;
  * ``kairos.chop_gate_decision`` — the pure verdict;
  * orchestrator light path: verdict computed in the gate region from the
    same 1h OHLCV the sweep already fetched; emission follows the exact
    ``bracket_unscored`` fail-closed suppression mechanism (quality verdict
    stands, delivery suppressed with reason ``chop_gate_low_er``);
    missing ER fails OPEN with a ``chop_gate_skip_no_er`` stamp (cold start
    must not brick the sweep).

These tests drive the REAL decision function + flag getters, and pin the
suppression semantics with the repo's established vocabulary mirror
(same pattern as tests/test_ev_hard_floor_gate.py and
tests/test_cooldown_restart.py).
"""
from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_chop_gate_min_efficiency,
    get_chop_gate_mode,
)
from regime_platform.sweep_orchestrator.kairos import (
    chop_gate_decision,
    efficiency_ratio,
)


def test_flag_ships_live_with_measured_threshold():
    """User mandate: LIVE, not shadow. 0.30 = the measured floor — the FOREX
    band centre clears it; a 24h path dominated by back-and-forth does not."""
    assert get_chop_gate_mode() == "live"
    assert get_chop_gate_min_efficiency() == 0.30


def test_low_er_suppressed():
    """efficiency 0.22 < 0.30 → suppressed with reason."""
    ok, status = chop_gate_decision(0.22, min_efficiency=0.30, mode="live")
    assert ok is False
    assert status == "suppressed_low_er"


def test_high_er_passes():
    """efficiency 0.41 ≥ 0.30 → passes."""
    ok, status = chop_gate_decision(0.41, min_efficiency=0.30, mode="live")
    assert ok is True
    assert status == "pass"


def test_missing_er_fails_open_with_stamp():
    """None (cold start / <25 completed bars) → passes WITH the skip stamp —
    observable fail-open, never a crash."""
    ok, status = chop_gate_decision(None, min_efficiency=0.30, mode="live")
    assert ok is True
    assert status == "skip_no_er"
    # garbage input is treated as missing, not as 0
    ok2, status2 = chop_gate_decision("nan-ish", min_efficiency=0.30, mode="live")
    assert ok2 is True
    assert status2 == "skip_no_er"


def test_off_mode_never_suppresses():
    ok, status = chop_gate_decision(0.01, min_efficiency=0.30, mode="off")
    assert ok is True
    assert status == "off"


def test_shadow_mode_reports_but_never_enforces():
    ok, status = chop_gate_decision(0.22, min_efficiency=0.30, mode="shadow")
    assert ok is True
    assert status == "would_suppress_low_er"
    ok2, status2 = chop_gate_decision(0.41, min_efficiency=0.30, mode="shadow")
    assert (ok2, status2) == (True, "pass")


def test_suppression_emission_semantics():
    """The orchestrator emission mirror (bracket_unscored mechanism): a
    failing chop verdict suppresses DELIVERY with reason
    'chop_gate_low_er' while the quality verdict stands; a skip passes with
    a stamp. This is the exact code shape wired at orchestrator 10a-2."""

    def _emit(mode: str, ok: bool, published: bool):
        """Mirror of the 10a-2 block (single suppression owner)."""
        suppress_reason = None
        gate_results = {}
        if mode != "off" and not ok:
            gate_results["chop_gate_ok"] = False
            if published and mode == "live":
                suppress_reason = "chop_gate_low_er"
                published = False
        return gate_results, suppress_reason, published

    # 0.22 → suppressed with reason
    ok, _ = chop_gate_decision(0.22, min_efficiency=0.30, mode="live")
    gates, reason, published = _emit("live", ok, published=True)
    assert published is False
    assert reason == "chop_gate_low_er"
    assert gates == {"chop_gate_ok": False}

    # 0.41 → passes untouched
    ok, _ = chop_gate_decision(0.41, min_efficiency=0.30, mode="live")
    gates, reason, published = _emit("live", ok, published=True)
    assert published is True
    assert reason is None
    assert gates == {}

    # None → passes with stamp (skip is observable in passed_gates)
    ok, status = chop_gate_decision(None, min_efficiency=0.30, mode="live")
    gates, reason, published = _emit("live", ok, published=True)
    assert published is True
    assert reason is None
    assert status == "skip_no_er"


def test_efficiency_ratio_feeds_the_gate_with_real_math():
    """End-to-end math sanity: a trending 24h window (monotone up) yields
    ER=1.0 → pass; a pure oscillation yields ER≈0 → suppressed. Uses the
    real efficiency_ratio the stamp and the gate share."""
    trending = [float(i) for i in range(26)]
    chop = [1.0, 0.0] * 13
    er_trend = efficiency_ratio(trending, 24)
    er_chop = efficiency_ratio(chop, 24)
    assert er_trend == 1.0
    assert er_chop == pytest.approx(0.0, abs=1e-9)
    ok_t, _ = chop_gate_decision(er_trend, min_efficiency=0.30, mode="live")
    ok_c, st_c = chop_gate_decision(er_chop, min_efficiency=0.30, mode="live")
    assert ok_t is True
    assert ok_c is False
    assert st_c == "suppressed_low_er"
