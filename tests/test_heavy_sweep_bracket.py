"""Heavy-sweep R2 wiring — bracket lookup + apply_bracket_to_ev parity with light."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from regime_platform.sweep_orchestrator.p_win_bracket import (
    apply_bracket_to_ev,
    expire_prob,
)


class _Engine:
    def recompute_ev_fields(self, p_win, profit, stake, min_rr=None, p_expire=0.0):
        return {
            "ev": 1.0,
            "ev_per_dollar": 0.5,
            "rr": 2.0,
            "allowed": True,
            "aggression": "mid",
            "ev_scale": 0.8,
        }


def test_apply_bracket_to_ev_mutates_result():
    ev = SimpleNamespace(
        p_win=0.6, ev=0.0, ev_per_dollar=0.0, rr=1.0,
        allowed=False, aggression="passive", ev_scale=1.0,
        profit=2.0, stake=1.0,
    )
    ok = apply_bracket_to_ev(
        ev, p_win_bracket=0.25, p_resolve=0.7, engine=_Engine(),
    )
    assert ok is True
    assert ev.p_win == 0.25
    assert ev.ev == 1.0
    assert ev.allowed is True


def test_apply_bracket_unscored_fails_open():
    ev = SimpleNamespace(
        p_win=0.6, ev=9.9, ev_per_dollar=1.0, rr=2.0,
        allowed=True, aggression="mid", ev_scale=1.0,
        profit=2.0, stake=1.0,
    )
    ok = apply_bracket_to_ev(
        ev, p_win_bracket=None, p_resolve=None, engine=_Engine(),
    )
    assert ok is False
    assert ev.p_win == 0.6
    assert ev.ev == 9.9


def test_expire_prob():
    import pytest

    assert expire_prob(None) == 0.0
    assert expire_prob(0.7) == pytest.approx(0.3)
    assert expire_prob(0.0) == 1.0
    assert expire_prob(1.0) == 0.0


def test_heavy_module_has_r2_wiring():
    """Source-level guard: heavy_sweep must call lookup + apply_bracket_to_ev."""
    import inspect

    from regime_platform.sweep_orchestrator import heavy_sweep

    src = inspect.getsource(heavy_sweep)
    assert "lookup_bracket_probs" in src
    assert "apply_bracket_to_ev" in src
    assert "p_win_in_ev_mode" in src
    assert "R2 SHADOW would set EV from p_win_bracket" in src
