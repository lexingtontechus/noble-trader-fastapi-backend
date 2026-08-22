"""
Tests for _apply_regime_kelly_floor() — fix #3b
(worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2 Mechanism 2 / §4).

Live evidence (2026-08-22): AAPL's raw Kelly came back 0.0 because the
measured mean hourly return (mu) was NEGATIVE (-0.00007129) — a genuinely
negative historical edge, not just sampling noise. The old always-floor
logic still force-sized it to kelly_f=0.0397 purely from high regime
confidence, feeding a persistent overconfidence signal the calibration
loop could detect but never fix.
"""

from regime_platform.sweep_orchestrator.orchestrator import _apply_regime_kelly_floor


def test_negative_mu_withholds_floor_by_default():
    """The exact live AAPL scenario: kelly_f=0, mu negative, regime
    confidently directional -> floor must be WITHHELD, not applied."""
    kelly_f, applied, withheld = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=-0.00007129, regime_label="low_vol_bull", regime_conf=0.9967,
    )
    assert kelly_f == 0.0
    assert applied is False
    assert withheld is True


def test_nonnegative_mu_still_gets_floored():
    """mu >= 0 is the case the floor was designed for (sampling noise
    zeroing out a genuinely-edged regime) — must still work."""
    kelly_f, applied, withheld = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=0.0001, regime_label="high_vol_strong_bull", regime_conf=0.99,
    )
    assert kelly_f > 0.0
    assert applied is True
    assert withheld is False


def test_mu_exactly_zero_is_treated_as_nonnegative():
    kelly_f, applied, withheld = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=0.0, regime_label="bull", regime_conf=0.8,
    )
    assert applied is True
    assert withheld is False


def test_require_nonneg_mu_false_restores_old_always_floor_behaviour():
    """The settings escape hatch (KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU=False)
    must reproduce the pre-fix behaviour exactly, for a quick rollback."""
    kelly_f, applied, withheld = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=-0.00007129, regime_label="low_vol_bull", regime_conf=0.9967,
        require_nonneg_mu=False,
    )
    assert kelly_f > 0.0
    assert applied is True
    assert withheld is False


def test_kelly_already_nonzero_is_left_untouched():
    kelly_f, applied, withheld = _apply_regime_kelly_floor(
        kelly_f=0.15, mu=-0.5, regime_label="bull", regime_conf=0.9,
    )
    assert kelly_f == 0.15
    assert applied is False
    assert withheld is False


def test_excluded_regime_labels_never_get_floored():
    for label in ("unknown", "market_closed", "neutral"):
        kelly_f, applied, withheld = _apply_regime_kelly_floor(
            kelly_f=0.0, mu=0.001, regime_label=label, regime_conf=0.9,
        )
        assert kelly_f == 0.0
        assert applied is False
        assert withheld is False


def test_floor_scales_with_regime_confidence_and_caps_at_006():
    low_conf_kelly, _, _ = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=0.0, regime_label="bull", regime_conf=0.5,
    )
    high_conf_kelly, _, _ = _apply_regime_kelly_floor(
        kelly_f=0.0, mu=0.0, regime_label="bull", regime_conf=0.99,
    )
    assert low_conf_kelly == 0.0        # (0.5-0.5)*2 = 0 -> floor = 0
    assert 0.0 < high_conf_kelly <= 0.06
