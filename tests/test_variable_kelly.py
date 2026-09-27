"""Variable binary Kelly (shadow) — pure math + context tiers."""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.variable_kelly import (
    binary_kelly,
    context_multiplier,
    variable_kelly,
)


class TestBinaryKelly:
    def test_positive_edge(self):
        # p=0.75, b=5/3 → f* = (0.75*5/3 - 0.25)/(5/3) = 0.6; half → 0.3
        f = binary_kelly(0.75, 5 / 3, fraction=0.5)
        assert f == pytest.approx(0.3)

    def test_zero_when_negative_edge(self):
        assert binary_kelly(0.4, 1.0, fraction=0.5) == 0.0

    def test_none_on_bad_input(self):
        assert binary_kelly(None, 2.0) is None
        assert binary_kelly(0.6, 0.0) is None
        assert binary_kelly(0.6, -1.0) is None

    def test_clips_to_unit(self):
        f = binary_kelly(0.99, 10.0, fraction=1.0)
        assert f is not None and 0.0 <= f <= 1.0


class TestContextMultiplier:
    def test_fresh_dir_flip_upweights(self):
        m = context_multiplier(event_window="fresh", transition_type="dir_flip")
        assert m is not None and m > 1.0

    def test_deep_downweights(self):
        m = context_multiplier(event_window="deep", transition_type="none")
        assert m is not None and m < 1.0

    def test_never_uses_age(self):
        # API surface has no age argument — structural guarantee.
        import inspect

        sig = inspect.signature(context_multiplier)
        assert "age" not in sig.parameters
        assert "regime_age" not in sig.parameters

    def test_unknown_context_is_none(self):
        assert context_multiplier() is None

    def test_clipped_band(self):
        m = context_multiplier(
            event_window="fresh",
            transition_type="dir_flip",
            vol_tier="low_vol",
        )
        assert m is not None and 0.70 <= m <= 1.35


class TestVariableKelly:
    def test_shadow_composition(self):
        r = variable_kelly(
            p_win=0.75,
            reward_risk=5 / 3,
            event_window="fresh",
            transition_type="dir_flip",
        )
        assert r.binary_f == pytest.approx(0.3)
        assert r.context_mult is not None and r.context_mult > 1.0
        assert r.effective_shadow == pytest.approx(
            min(1.0, r.binary_f * r.context_mult)
        )

    def test_fail_open(self):
        r = variable_kelly(p_win=None, reward_risk=None)
        assert r.binary_f is None
        assert r.effective_shadow is None
