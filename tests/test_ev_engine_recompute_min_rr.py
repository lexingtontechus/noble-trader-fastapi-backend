"""Tests for EVEngine.recompute_ev_fields()'s optional ``min_rr``
override (2026-09-11, worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 11).

Narrow, focused coverage of just the new parameter — the method's
pre-existing behavior (recomputing ev/ev_per_dollar/rr/aggression/
ev_scale for a changed p_win) is exercised live via the calibration-
enforcement path and not otherwise unit-tested in this repo.
"""

from regime_platform.services.statistics.ev_engine import EVEngine


def test_no_override_falls_back_to_instance_min_rr():
    engine = EVEngine(min_ev=0.0, min_rr=2.0)
    # profit/stake chosen so rr=1.5 -- fails the instance's own min_rr=2.0.
    result = engine.recompute_ev_fields(p_win=0.6, profit=1.5, stake=1.0)
    assert result["rr"] == 1.5
    assert result["allowed"] is False


def test_override_can_raise_the_effective_floor_and_reject():
    engine = EVEngine(min_ev=0.0, min_rr=1.5)
    # rr=1.5 clears the instance's own min_rr=1.5 ...
    baseline = engine.recompute_ev_fields(p_win=0.6, profit=1.5, stake=1.0)
    assert baseline["allowed"] is True
    # ... but a per-call override to 2.0 rejects the same combo, without
    # mutating the shared engine instance's own min_rr attribute.
    overridden = engine.recompute_ev_fields(
        p_win=0.6, profit=1.5, stake=1.0, min_rr=2.0,
    )
    assert overridden["allowed"] is False
    assert engine.min_rr == 1.5  # instance attribute untouched


def test_override_can_lower_the_effective_floor_and_accept():
    engine = EVEngine(min_ev=0.0, min_rr=2.0)
    baseline = engine.recompute_ev_fields(p_win=0.6, profit=1.5, stake=1.0)
    assert baseline["allowed"] is False
    overridden = engine.recompute_ev_fields(
        p_win=0.6, profit=1.5, stake=1.0, min_rr=1.0,
    )
    assert overridden["allowed"] is True


def test_override_does_not_change_rr_or_ev_values():
    # min_rr only gates `allowed` -- it must never affect the arithmetic
    # fields, which are purely functions of p_win/profit/stake.
    engine = EVEngine(min_ev=0.0, min_rr=2.0)
    a = engine.recompute_ev_fields(p_win=0.6, profit=1.5, stake=1.0)
    b = engine.recompute_ev_fields(p_win=0.6, profit=1.5, stake=1.0, min_rr=1.0)
    assert a["ev"] == b["ev"]
    assert a["ev_per_dollar"] == b["ev_per_dollar"]
    assert a["rr"] == b["rr"]
    assert a["aggression"] == b["aggression"]
    assert a["ev_scale"] == b["ev_scale"]
