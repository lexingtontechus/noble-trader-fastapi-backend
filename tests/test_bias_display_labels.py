"""Bias DISPLAY label splits (README "Status thresholds", implemented 2026-10-12).

The README documents a tiered, user-facing bias vocabulary that is distinct
from the ±0.10 enforcement classifier:

    |bias| < 0.05   CALIBRATED
    +0.05..+0.15    MILD_OVERCONFIDENT
    +0.15..+0.30    OVERCONFIDENT
    > +0.30         SEVERE_OVERCONFIDENT
    -0.05..-0.15    MILD_UNDERCONFIDENT
    -0.15..-0.30    UNDERCONFIDENT
    < -0.30         SEVERE_UNDERCONFIDENT
    < 20 records    INSUFFICIENT_DATA

These pin the exact boundaries and the single-definition contract
(`eod_correction._classify_bias_display`), so the router cannot re-implement
the bands.
"""
from regime_platform.sweep_orchestrator.eod_correction import (
    DISPLAY_CALIBRATED_BAND,
    DISPLAY_MILD_BAND,
    DISPLAY_MIN_RECORDS,
    DISPLAY_SEVERE_BAND,
    _classify_bias,
    _classify_bias_display,
)


def test_band_constants_match_readme():
    assert DISPLAY_CALIBRATED_BAND == 0.05
    assert DISPLAY_MILD_BAND == 0.15
    assert DISPLAY_SEVERE_BAND == 0.30
    assert DISPLAY_MIN_RECORDS == 20


def test_insufficient_data_dominates_every_bias():
    """Fewer than 20 records → INSUFFICIENT_DATA regardless of magnitude."""
    for b in (0.0, 0.25, -0.4, 0.99):
        assert _classify_bias_display(b, 0) == "INSUFFICIENT_DATA"
        assert _classify_bias_display(b, 19) == "INSUFFICIENT_DATA"


def test_overconfident_tiers_and_boundaries():
    n = 50
    assert _classify_bias_display(0.0, n) == "CALIBRATED"
    assert _classify_bias_display(0.049, n) == "CALIBRATED"
    # at exactly 0.05 the label leaves CALIBRATED (README: |bias| < 0.05)
    assert _classify_bias_display(0.05, n) == "MILD_OVERCONFIDENT"
    assert _classify_bias_display(0.149, n) == "MILD_OVERCONFIDENT"
    # at 0.15 → OVERCONFIDENT
    assert _classify_bias_display(0.15, n) == "OVERCONFIDENT"
    assert _classify_bias_display(0.30, n) == "OVERCONFIDENT"
    # strictly beyond 0.30 → SEVERE
    assert _classify_bias_display(0.301, n) == "SEVERE_OVERCONFIDENT"
    assert _classify_bias_display(0.9, n) == "SEVERE_OVERCONFIDENT"


def test_underconfident_tiers_and_boundaries():
    n = 50
    assert _classify_bias_display(-0.049, n) == "CALIBRATED"
    assert _classify_bias_display(-0.05, n) == "MILD_UNDERCONFIDENT"
    assert _classify_bias_display(-0.149, n) == "MILD_UNDERCONFIDENT"
    assert _classify_bias_display(-0.15, n) == "UNDERCONFIDENT"
    assert _classify_bias_display(-0.30, n) == "UNDERCONFIDENT"
    assert _classify_bias_display(-0.301, n) == "SEVERE_UNDERCONFIDENT"
    assert _classify_bias_display(-0.9, n) == "SEVERE_UNDERCONFIDENT"


def test_display_and_enforcement_are_distinct():
    """The same bias can carry different enforcement and display labels —
    that separation is the whole point (a +0.06 is enforcement-CALIBRATED
    but display-MILD_OVERCONFIDENT)."""
    n = 50
    # +0.06: enforcement CALIBRATED (inside ±0.10), display MILD_OVERCONFIDENT
    assert _classify_bias(0.06) == "CALIBRATED"
    assert _classify_bias_display(0.06, n) == "MILD_OVERCONFIDENT"
    # +0.25: enforcement OVERCONFIDENT, display OVERCONFIDENT (agree here)
    assert _classify_bias(0.25) == "OVERCONFIDENT"
    assert _classify_bias_display(0.25, n) == "OVERCONFIDENT"


def test_full_label_vocabulary_is_reachable():
    """All eight documented labels are producible."""
    n = 50
    produced = {
        _classify_bias_display(b, n)
        for b in (-0.9, -0.2, -0.08, 0.0, 0.08, 0.2, 0.9)
    } | {_classify_bias_display(0.5, 5)}
    assert produced == {
        "CALIBRATED", "MILD_OVERCONFIDENT", "OVERCONFIDENT", "SEVERE_OVERCONFIDENT",
        "MILD_UNDERCONFIDENT", "UNDERCONFIDENT", "SEVERE_UNDERCONFIDENT",
        "INSUFFICIENT_DATA",
    }


def test_router_response_exposes_display_labels():
    """The API response carries the tiered display labels alongside the
    enforcement ones — computed by the single classifier, not re-implemented."""
    from regime_platform.routers.calibration import _build_bias_response

    enforced = {"avg_pred": 0.31, "win_rate": 0.09, "bias": 0.22, "n": 120}
    raw = {"avg_pred_raw": 0.40, "bias": 0.31, "n": 120}
    resp = _build_bias_response("XAUUSD", enforced, raw, window_days=7)
    assert resp.status == "OVERCONFIDENT"            # enforcement (±0.10)
    assert resp.status_display == "OVERCONFIDENT"    # display (tiered)
    assert resp.status_raw == "OVERCONFIDENT"
    assert resp.status_raw_display == "SEVERE_OVERCONFIDENT"  # 0.31 > 0.30

    # thin sample → display INSUFFICIENT_DATA, enforcement still classified
    thin = _build_bias_response("EURUSD", {"avg_pred": 0.2, "win_rate": 0.1, "bias": 0.11, "n": 4}, None, window_days=7)
    assert thin.status == "OVERCONFIDENT"
    assert thin.status_display == "INSUFFICIENT_DATA"
