"""
Tests for PositionSizer's direction-aware Kelly sizing (2026-09-04,
worklog 20260904_KELLY_DIRECTION_AWARE_SIZING.md).

Live evidence: XAGUSD/XAUEUR/XAUUSD were all reading a real,
price-confirmed decline (bear regime, negative measured mu) — a genuine
short setup — yet kelly_f was 0.0 on every tick, because
PositionSizer.size() always asked "would a LONG have had positive
edge?" regardless of the trade's actual direction.
"""

from regime_platform.core.position_sizer import PositionRequest, PositionSizer

_sizer = PositionSizer()

# A clear, consistent decline — same shape as the live XAUUSD case.
_DECLINING_RETURNS = [-0.001, -0.0015, -0.0008, -0.0012, -0.0009, -0.0011] * 10
# A clear, consistent rally — the mirror image.
_RALLYING_RETURNS = [0.001, 0.0015, 0.0008, 0.0012, 0.0009, 0.0011] * 10


def test_default_direction_is_long_unchanged_from_before():
    req_explicit = PositionRequest(returns=_RALLYING_RETURNS, direction="long")
    req_default = PositionRequest(returns=_RALLYING_RETURNS)
    r1 = _sizer.size(req_explicit)
    r2 = _sizer.size(req_default)
    assert r1 == r2


def test_long_with_declining_returns_still_zeros_out():
    # Regression: a long position in a real decline has no edge —
    # unaffected by this fix.
    result = _sizer.size(PositionRequest(returns=_DECLINING_RETURNS, direction="long"))
    assert result.full_kelly_f == 0.0
    assert result.recommended_f == 0.0


def test_short_with_declining_returns_now_gets_positive_edge():
    # The actual bug: a short in a genuine decline should have real,
    # positive edge, not zero.
    result = _sizer.size(PositionRequest(returns=_DECLINING_RETURNS, direction="short"))
    assert result.full_kelly_f > 0.0
    assert result.sharpe_ratio > 0.0
    assert result.kelly_edge > 0.0


def test_short_with_rallying_returns_correctly_zeros_out():
    # Misaligned case (regime says short, but recent price action was
    # actually up) — must stay zero, not just "not obviously wrong."
    result = _sizer.size(PositionRequest(returns=_RALLYING_RETURNS, direction="short"))
    assert result.full_kelly_f == 0.0
    assert result.recommended_f == 0.0


def test_short_on_declining_returns_mirrors_long_on_negated_returns():
    # Proves the fix is a pure sign-flip of mu, not a parallel formula —
    # short-on-decline must produce identical numbers to long-on-the-
    # sign-flipped-equivalent-rally.
    negated = [-x for x in _DECLINING_RETURNS]
    short_result = _sizer.size(PositionRequest(returns=_DECLINING_RETURNS, direction="short"))
    long_result = _sizer.size(PositionRequest(returns=negated, direction="long"))
    assert short_result.full_kelly_f == long_result.full_kelly_f
    assert short_result.sharpe_ratio == long_result.sharpe_ratio
    assert short_result.kelly_edge == long_result.kelly_edge


def test_variance_unaffected_by_direction():
    # sigma/vol-scaling must be identical either way — only mu flips.
    long_result = _sizer.size(PositionRequest(returns=_DECLINING_RETURNS, direction="long"))
    short_result = _sizer.size(PositionRequest(returns=_DECLINING_RETURNS, direction="short"))
    assert long_result.vol_scaled_f == 0.0  # long: no edge, clipped to 0 regardless of vol
    # Both went through the same vol-scaling formula on the same sigma —
    # confirm indirectly via a case where both are nonzero (rallying
    # returns for long, declining for short) landing on comparable scale.
    rallying_long = _sizer.size(PositionRequest(returns=_RALLYING_RETURNS, direction="long"))
    assert rallying_long.vol_scaled_f == short_result.vol_scaled_f
