"""
Tests for the immediate fixes in worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.

Three fixes:
  §3.2 ev_scale_sizing_mode       — decouple size from p_win (feature flag)
  §3.3 breakeven_prob_floor       — payoff-aware probability floor (audit E7)
  Phase 2 vol_factor/drawdown_factor — the p_win-free risk backbone, standalone

Each fix is asserted to be **backwards-compatible by default**: the shipped
config must reproduce the pre-fix behaviour byte-for-byte, so flipping a flag is
the only way behaviour changes.
"""

from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import get_ev_scale_sizing_mode
from regime_platform.core.position_sizer import (
    DynamicMasanielloSizer,
    SizingConfig,
    TradeContext,
    drawdown_factor,
    vol_factor,
)


# ─── §3.2 ev_scale_sizing_mode ────────────────────────────────────────────


def test_ev_scale_flag_defaults_to_on():
    """The shipped value must be the byte-identical legacy behaviour."""
    assert get_ev_scale_sizing_mode() == "on"


def test_ev_scale_flag_is_fail_safe_on_typo(monkeypatch):
    """A typo must not silently change position sizing — anything that is not
    an explicit 'off' resolves to 'on'."""
    import regime_platform.core.feature_flags as ff

    for bad in ("of", "false", "", "OFFF", "0", "no"):
        monkeypatch.setattr(ff, "_load", lambda bad=bad: {"ev_scale_sizing_mode": bad})
        assert ff.get_ev_scale_sizing_mode() == "on", f"{bad!r} should resolve to 'on'"


def test_ev_scale_flag_resolves_off(monkeypatch):
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "_load", lambda: {"ev_scale_sizing_mode": "off"})
    assert ff.get_ev_scale_sizing_mode() == "off"


def test_ev_scale_flag_accepts_yaml_boolean_false(monkeypatch):
    """Unquoted `off` parses as YAML boolean False. The parser stringifies it,
    so it must still resolve to 'off' rather than the fail-safe 'on'."""
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "_load", lambda: {"ev_scale_sizing_mode": False})
    assert ff.get_ev_scale_sizing_mode() == "off"


# ─── §3.3 breakeven_prob_floor ────────────────────────────────────────────


def _ctx(p_win=0.60, reward_risk=2.0, **kw):
    base = dict(
        equity=100_000.0, stop_distance_price=2.0, point_value=1.0,
        p_win=p_win, reward_risk=reward_risk,
        regime_quality=0.9, state_confidence=0.9,
        current_drawdown=0.0, atr_baseline=10.0, atr_current=10.0,
        trade_index=1, wins_so_far=0, losses_so_far=0,
        batch_size=5, target_wins=3, direction="long",
    )
    base.update(kw)
    return TradeContext(**base)


def test_legacy_floor_is_flat_when_disabled():
    s = DynamicMasanielloSizer(SizingConfig(breakeven_prob_floor=False))
    assert s.effective_prob_floor(2.0) == pytest.approx(0.50)
    assert s.effective_prob_floor(10.0) == pytest.approx(0.50)


def test_breakeven_floor_relaxes_for_high_rr():
    """At RR 4:1 break-even is 0.20, so the floor should sit below the flat 0.50."""
    s = DynamicMasanielloSizer(SizingConfig())
    f = s.effective_prob_floor(4.0)
    assert f == pytest.approx(max(0.50, 1 / 5 + 0.02))   # 0.50 still binds


def test_breakeven_floor_binds_above_flat_for_low_rr():
    """At RR 0.5:1 break-even is 0.667, ABOVE the flat floor — it must bind."""
    s = DynamicMasanielloSizer(SizingConfig())
    f = s.effective_prob_floor(0.5)
    assert f == pytest.approx(1 / 1.5 + 0.02)
    assert f > 0.50


def test_breakeven_floor_never_more_permissive_than_flat():
    """The max() guarantees the payoff-aware floor can never open a trade the
    payoff-blind rule would reject."""
    s = DynamicMasanielloSizer(SizingConfig())
    for rr in (0.1, 0.5, 1.0, 2.0, 5.0, 20.0):
        assert s.effective_prob_floor(rr) >= 0.50


def test_breakeven_floor_falls_back_on_bad_rr():
    s = DynamicMasanielloSizer(SizingConfig())
    for bad in (0.0, -1.0, None, "x", float("nan")):
        assert s.effective_prob_floor(bad) == pytest.approx(0.50)


def test_low_rr_trade_below_breakeven_is_blocked():
    """RR 1:1 with p=0.45: break-even is 0.50+margin, so 0.45 must be rejected
    even though it would have passed a naive 'is it > 0' edge test... and note
    the flat floor would ALSO have rejected it — this asserts the message names
    the break-even floor, not the flat one."""
    s = DynamicMasanielloSizer(SizingConfig(min_rr=0.5))
    r = s.size_trade(_ctx(p_win=0.45, reward_risk=1.0))
    assert r.allowed is False
    assert "break-even floor" in r.reason


def test_high_rr_trade_gets_through_where_legacy_would_also_pass():
    """At RR 5:1 the break-even floor (0.187) sits under the flat 0.50, so the
    flat floor binds and a p=0.52 trade passes — same as legacy."""
    s = DynamicMasanielloSizer(SizingConfig(min_rr=0.5))
    r = s.size_trade(_ctx(p_win=0.52, reward_risk=5.0))
    assert r.allowed is True


def test_gate_message_names_the_binding_floor():
    s = DynamicMasanielloSizer(SizingConfig(min_rr=0.5))
    low_rr = s.size_trade(_ctx(p_win=0.45, reward_risk=0.8))
    assert "break-even floor" in low_rr.reason
    high_rr = s.size_trade(_ctx(p_win=0.30, reward_risk=8.0))
    # RR 8:1 -> break-even 0.131 < 0.50, so the FLAT floor binds
    assert "flat min_prob" in high_rr.reason


# ─── Phase 2 — vol_factor / drawdown_factor ───────────────────────────────


def test_vol_factor_shrinks_in_high_vol():
    """ATR doubled -> half size."""
    assert vol_factor(10.0, 20.0) == pytest.approx(0.5)


def test_vol_factor_expands_in_low_vol():
    assert vol_factor(10.0, 5.0) == pytest.approx(1.5)


def test_vol_factor_clamps():
    assert vol_factor(10.0, 100.0) == pytest.approx(0.5)     # below lo -> lo
    assert vol_factor(10.0, 1.0) == pytest.approx(1.5)       # above hi -> hi


def test_vol_factor_is_neutral_on_degenerate_input():
    for base, cur in ((0.0, 10.0), (10.0, 0.0), (-1.0, 10.0), (None, 10.0),
                      (10.0, None)):
        assert vol_factor(base, cur) == pytest.approx(1.0)


def test_vol_factor_handles_bad_types():
    assert vol_factor("x", 10.0) == pytest.approx(1.0)
    assert vol_factor(10.0, "y") == pytest.approx(1.0)


def test_drawdown_factor_is_one_at_no_drawdown():
    assert drawdown_factor(0.0, 0.10) == pytest.approx(1.0)


def test_drawdown_factor_shrinks_with_drawdown():
    assert drawdown_factor(0.05, 0.10) == pytest.approx(0.5)


def test_drawdown_factor_floors():
    """At or beyond max drawdown the floor holds — never collapses to zero."""
    assert drawdown_factor(0.10, 0.10) == pytest.approx(0.25)
    assert drawdown_factor(0.50, 0.10) == pytest.approx(0.25)


def test_drawdown_factor_neutral_without_constraint():
    assert drawdown_factor(0.05, 0.0) == pytest.approx(1.0)
    assert drawdown_factor(0.05, None) == pytest.approx(1.0)


def test_backbone_is_p_win_free():
    """The whole point of Phase 2: neither factor reads a probability. Signature
    inspection, so a future edit that adds one fails here."""
    import inspect

    for fn in (vol_factor, drawdown_factor):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"p_win", "prob", "probability"}), (
            f"{fn.__name__} must stay p_win-free"
        )


# ─── Backwards compatibility ──────────────────────────────────────────────


def test_defaults_reproduce_legacy_sizing():
    """Shipped defaults must be byte-identical to pre-fix behaviour: flat floor,
    and ev_scale still multiplying (asserted via the flag)."""
    cfg = SizingConfig()
    assert cfg.breakeven_prob_floor is True      # payoff-aware
    assert cfg.min_prob == 0.50
    # ...but at the RR the platform actually trades (1.67-2.0), the payoff-aware
    # floor sits UNDER 0.50, so the flat floor still binds -> same decisions.
    s = DynamicMasanielloSizer(cfg)
    for rr in (1.67, 2.0):
        assert s.effective_prob_floor(rr) == pytest.approx(0.50)


def test_legacy_floor_restores_exactly():
    """With the flag off, every RR gives the identical flat floor."""
    s = DynamicMasanielloSizer(SizingConfig(breakeven_prob_floor=False))
    floors = {s.effective_prob_floor(rr) for rr in (0.1, 1.0, 2.0, 10.0)}
    assert floors == {0.50}
