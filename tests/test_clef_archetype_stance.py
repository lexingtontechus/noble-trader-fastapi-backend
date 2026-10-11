"""R4 (Clef-Flash archetype classifier) + R5 (multi-asset stance) tests.

Both are the pure/deterministic halves — state builders, the parse wrapper
via a mocked /decision response, and the agreement helper. The HTTP call
itself (decide) is already covered by the existing clef_flash tests; here we
pin the R4/R5-specific contracts:
  * ARCHETYPE_OPTIONS vocab matches the R0 strategy_registry archetypes,
  * build_*_state drops None fields and carries the right indicators,
  * classify_archetype parses a /decision response into ClefArchetype and
    agrees_with() compares to the R1 router verdict,
  * classify_stance parses into Stance (buy/sell/hold),
  * fail-open: a malformed/empty response returns None.
"""

from __future__ import annotations

import pytest

from regime_platform.services.decision import clef_flash as cf
from regime_platform.sweep_orchestrator.clef_stance import (
    build_stance_state,
    classify_stance,
)


# ── R4: vocab parity with the strategy registry ─────────────────────────────

def test_archetype_vocab_matches_registry():
    """The classifier's labels MUST be a superset of the R0 registry's
    archetype names (+ stand_by), or the agreement read is meaningless."""
    from regime_platform.core.feature_flags import get_strategy_registry
    registry_names = set(get_strategy_registry().keys())
    classifier_labels = set(cf.ARCHETYPE_OPTIONS.keys())
    # Every registry archetype the classifier might be compared against must
    # be in its vocab.
    assert registry_names <= classifier_labels, (
        f"registry archetypes missing from classifier vocab: "
        f"{registry_names - classifier_labels}"
    )
    assert "stand_by" in classifier_labels


def test_build_archetype_state_drops_none_and_carries_indicators():
    st = cf.build_archetype_state(
        symbol="XAUUSD", signal="buy", regime="low_vol_bull",
        macro_label="bear", trend_label="trending_down",
        momentum_24h=-0.03, vol_ratio=0.8, deviation_z=-2.1, p_win_dir=0.4,
    )
    assert st["symbol"] == "XAUUSD"
    assert st["momentum_24h"] == -0.03
    assert st["deviation_z"] == -2.1
    # None fields are dropped, not passed as null.
    st2 = cf.build_archetype_state(symbol="XAUUSD", signal="buy")
    assert set(st2.keys()) == {"symbol", "signal"}


# ── R4: parse + agreement ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_archetype_parses_and_agrees(monkeypatch):
    """A mocked /decision response -> ClefArchetype; agrees_with compares to
    the R1 router verdict."""
    async def fake_decide(state, questions, *, timeout=None):
        assert "archetype" in questions
        return {"archetype": {"probabilities": {
            "momentum": 0.2, "mean_reversion": 0.7, "stand_by": 0.1}}}

    monkeypatch.setattr(cf, "decide", fake_decide)
    arch = await cf.classify_archetype(
        symbol="XAUUSD", signal="buy", regime="low_vol_bull",
        macro_label="bear", trend_label="trending_down",
    )
    assert arch is not None
    assert arch.top == "mean_reversion"
    assert abs(arch.confidence - 0.7) < 1e-9
    assert arch.prob_of("mean_reversion") == 0.7
    # Agreement vs the R1 router: gold buy into downtrend -> mean_reversion.
    assert arch.agrees_with("mean_reversion") is True
    assert arch.agrees_with("momentum") is False
    # Router produced nothing -> agreement undefined (None), not False.
    assert arch.agrees_with(None) is None


@pytest.mark.asyncio
async def test_classify_archetype_fail_open(monkeypatch):
    """Malformed / empty response -> None (stamp stays NULL)."""
    async def fake_decide_bad(state, questions, *, timeout=None):
        return {"archetype": {"probabilities": {}}}
    async def fake_decide_none(state, questions, *, timeout=None):
        return None
    monkeypatch.setattr(cf, "decide", fake_decide_bad)
    assert await cf.classify_archetype(symbol="XAUUSD", signal="buy") is None
    monkeypatch.setattr(cf, "decide", fake_decide_none)
    assert await cf.classify_archetype(symbol="XAUUSD", signal="buy") is None


# ── R5: stance classifier ───────────────────────────────────────────────────

def test_build_stance_state_forms_direction_no_signal():
    """The stance state carries indicators but NO stated signal — the overlay
    forms its own direction, unlike the per-signal direction stamp."""
    st = build_stance_state(
        symbol="BTCUSD", regime="high_vol_bull", macro_label="bull",
        momentum_24h=0.05, vol_ratio=1.2, deviation_z=1.8,
    )
    assert "signal" not in st
    assert st["symbol"] == "BTCUSD"
    assert st["momentum_24h"] == 0.05


@pytest.mark.asyncio
async def test_classify_stance_parses(monkeypatch):
    async def fake_classify_choice(*, instructions, options, state, question_id="answer"):
        return {"buy": 0.6, "sell": 0.1, "hold": 0.3}
    # classify_stance imports classify_choice from clef_flash at call time.
    monkeypatch.setattr(cf, "classify_choice", fake_classify_choice)
    stance = await classify_stance(symbol="BTCUSD", regime="high_vol_bull")
    assert stance is not None
    assert stance.stance == "buy"
    assert abs(stance.confidence - 0.6) < 1e-9
    assert stance.as_stamp() == "buy"


@pytest.mark.asyncio
async def test_classify_stance_fail_open(monkeypatch):
    async def fake_classify_choice_none(*, instructions, options, state, question_id="answer"):
        return None
    monkeypatch.setattr(cf, "classify_choice", fake_classify_choice_none)
    assert await classify_stance(symbol="BTCUSD") is None
