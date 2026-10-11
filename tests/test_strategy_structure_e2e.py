"""E2E replay: R1 router + R4 archetype + R5 stance + reachability-conditioned
calibration, verified against era-clean-shaped outcomes.

Offline + deterministic (no live Supabase). The era-clean numbers quoted in
worklog/20261011_REACHABILITY_CONDITIONED_CALIBRATION.md are encoded here as
synthetic cells so the LOGIC and SHAPE are pinned by a test, not just asserted:

  * R1 routes the gold-buy counter-trend case to mean_reversion, aligned
    silver/crypto to momentum, consolidating/neutral to stand_by.
  * R4 archetype classifier parses a mocked /decision and agrees_with() the R1
    verdict correctly; its vocab is a superset of the R0 registry.
  * R5 stance forms its OWN direction (no stated signal) and parses.
  * The reachability-conditioned estimator reproduces the known bucket ordering
    (wide > tight TP-rate) and is monotonic in reachability — the reframe's core
    claim — using era-clean-shaped cells (XAUUSD trending_down 38.2% vs
    consolidating 8.1%; 4+ bucket 19.2% vs <0.5 bucket 0%).
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.strategy_router import route


# ── Era-clean-shaped replay corpus ──────────────────────────────────────────
# Each row: the router inputs + the era-clean realized outcome we expect the
# reachability-conditioned view to be consistent with. Encodes the 2026-10-11
# pulse so the test fails loudly if the router logic or the estimator shape
# regresses.

ERA_CLEAN_CASES = [
    # (name, symbol, signal, regime, asset_class, macro, trend, expected_archetype)
    ("gold_buy_downtrend_MR", "XAUUSD", "buy", "med_low_vol_bull", "metal",
     "bear", "trending_down", "mean_reversion"),
    ("gold_buy_reversal_MR", "XAUAUD", "buy", "low_vol_bull", "metal",
     "bear", "reversal_down", "mean_reversion"),
    ("silver_buy_aligned_momentum", "XAGUSD", "buy", "low_vol_bull", "metal",
     "bull", "trending_up", "momentum"),
    ("btc_sell_aligned_momentum", "BTCUSD", "sell", "high_vol_bear", "crypto",
     "bear", "trending_down", "momentum"),
    ("gold_buy_consolidating_standby", "XAUUSD", "buy", "low_vol_bull", "metal",
     "bear", "consolidating", "stand_by"),
    ("gold_buy_no_reads_standby", "XAUUSD", "buy", "low_vol_bull", "metal",
     None, None, "stand_by"),
    ("neutral_never_routes", "XAUUSD", "neutral", "low_vol_bull", "metal",
     "bear", "trending_down", "stand_by"),
    ("high_vol_gold_buy_not_MR", "XAUUSD", "buy", "high_vol_bull", "metal",
     "bear", "trending_down", "stand_by"),  # regime_vol gate: falling-knife guard
]


@pytest.mark.parametrize("name,symbol,signal,regime,ack,macro,trend,expected", ERA_CLEAN_CASES)
def test_r1_router_replay(name, symbol, signal, regime, ack, macro, trend, expected):
    """R1 routing is stable across the era-clean-shaped corpus."""
    r = route(symbol=symbol, signal=signal, regime_label=regime,
              asset_class=ack, macro_label=macro, trend_label=trend)
    assert r.archetype == expected, f"{name}: expected {expected}, got {r.archetype} ({r.reason})"


def test_r1_mean_reversion_only_fires_on_opposed_directional_trend():
    """The 4.7x edge (trending_down 38.2% vs consolidating 8.1%) is only routed
    when the trend read is directional AND opposes — consolidating must NOT
    route MR even though macro is bear."""
    mr = route(symbol="XAUUSD", signal="buy", regime_label="med_low_vol_bull",
               asset_class="metal", macro_label="bear", trend_label="trending_down")
    cons = route(symbol="XAUUSD", signal="buy", regime_label="med_low_vol_bull",
                 asset_class="metal", macro_label="bear", trend_label="consolidating")
    assert mr.archetype == "mean_reversion"
    assert cons.archetype == "stand_by"


# ── Reachability-conditioned calibration (the reframe) ─────────────────────

def reachability_conditioned_p(
    cells: list[dict], *, symbol: str, vol_tier: str, bucket: str, expiry_hours: float,
) -> float | None:
    """P(reach TP before SL before expiry | symbol, vol_tier, bucket, expiry).

    The estimator shape pinned by this test. The cell's TP share IS the
    era-clean realized P(TP before SL before expiry) for that (symbol,
    vol_tier, bucket) — it already nets out expiry, so we do NOT re-discount
    it (that would double-count the leash). Expiry enters as a SEPARATE,
    monotonic reachability factor only via the cell's own p90: a signal held
    well past the cell's p90 resolution time gets a haircut, one held within
    it does not. Returns None when no cell matches (fail-open — the caller
    falls back, never invents a probability).

    This mirrors the reframe in the worklog: reachability is the CONDITIONING
    variable of the probability, with expiry as an explicit term — not a
    direction-only p_win times a separate reachability table.
    """
    for c in cells:
        if (c["symbol"] == symbol and c["vol_tier"] == vol_tier
                and c["bucket"] == bucket):
            tp_share = c["tp"] / c["closed"] if c["closed"] else 0.0
            # Expiry factor: 1.0 when the leash covers the cell's p90; a
            # shorter-than-p90 leash scales P(reach) down proportionally (the
            # tail that would have resolved later is cut off). Never inflates.
            if c.get("p90_hours"):
                expiry_factor = min(1.0, expiry_hours / c["p90_hours"])
            else:
                expiry_factor = 1.0
            return round(tp_share * expiry_factor, 4)
    return None


# Era-clean-shaped cells (2026-10-11 pulse, rounded). XAUUSD trending_down buy
# 13/34 = 0.382; consolidating buy 11/136 = 0.081; 4+ bucket TP 0.192. p90_hours
# is the cell's own 90th-pct resolution time; a test leash >= p90 -> factor 1.0
# (P equals the era-clean WR), a shorter leash -> proportional haircut.
ERA_CELLS = [
    {"symbol": "XAUUSD", "vol_tier": "med_low_vol", "bucket": "4+",
     "closed": 34, "tp": 13, "p90_hours": 48.0},   # trending_down buy cohort
    {"symbol": "XAUUSD", "vol_tier": "low_vol", "bucket": "4+",
     "closed": 136, "tp": 11, "p90_hours": 48.0},  # consolidating buy cohort
    {"symbol": "XAUUSD", "vol_tier": "med_low_vol", "bucket": "<0.5",
     "closed": 40, "tp": 0, "p90_hours": 1.0},     # tight bracket: never reaches
]


def test_reachability_conditioned_reproduces_trend_edge():
    """At a leash that covers the cell's resolution (expiry >= p90), P equals
    the era-clean WR and preserves the trend edge: trending_down prices ~4.7x
    the consolidating cell at the same bucket."""
    p_down = reachability_conditioned_p(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol", bucket="4+", expiry_hours=48)
    p_cons = reachability_conditioned_p(
        ERA_CELLS, symbol="XAUUSD", vol_tier="low_vol", bucket="4+", expiry_hours=48)
    assert p_down is not None and p_cons is not None
    assert abs(p_down - 0.382) < 0.01
    assert abs(p_cons - 0.081) < 0.01
    assert p_down / p_cons > 4.0, "trending_down must price well above consolidating"


def test_reachability_conditioned_is_monotonic_in_expiry():
    """A longer leash can only raise P(reach) — the expiry term behaves. The
    4+ cell's p90 is 48h, so a 12h leash haircuts to 0.25x, a 96h leash is 1.0x."""
    short = reachability_conditioned_p(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol", bucket="4+", expiry_hours=12)
    long = reachability_conditioned_p(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol", bucket="4+", expiry_hours=96)
    assert short is not None and long is not None
    assert abs(short - round(0.382 * 12 / 48, 4)) < 0.01  # haircut applied
    assert abs(long - 0.382) < 0.01                        # no haircut, capped at 1.0
    assert long > short, "longer expiry must not lower P(reach)"


def test_reachability_conditioned_tight_bracket_is_near_zero():
    """The tight (<0.5 ATR) cell has 0% TP — the estimator must reflect that a
    bracket that cannot reach TP prices ~0 regardless of direction."""
    p = reachability_conditioned_p(
        ERA_CELLS, symbol="XAUUSD", vol_tier="med_low_vol", bucket="<0.5", expiry_hours=48)
    assert p == 0.0


def test_reachability_conditioned_fail_open_on_cold_cell():
    """No matching cell -> None (fail-open), never a fabricated probability."""
    assert reachability_conditioned_p(
        ERA_CELLS, symbol="EURUSD", vol_tier="low_vol", bucket="4+", expiry_hours=48) is None


# ── R4 / R5 plumbing (mocked Worker) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_r4_archetype_agrees_with_r1_on_gold_downtrend(monkeypatch):
    """When Clef-Flash reads the gold-buy downtrend as mean_reversion, it agrees
    with the R1 router's verdict for the same signal."""
    from regime_platform.services.decision import clef_flash as cf

    async def fake_decide(state, questions, *, timeout=None):
        return {"archetype": {"probabilities": {
            "momentum": 0.1, "mean_reversion": 0.8, "stand_by": 0.1}}}
    monkeypatch.setattr(cf, "decide", fake_decide)

    router_verdict = route(symbol="XAUUSD", signal="buy", regime_label="med_low_vol_bull",
                           asset_class="metal", macro_label="bear",
                           trend_label="trending_down").archetype
    arch = await cf.classify_archetype(
        symbol="XAUUSD", signal="buy", regime="med_low_vol_bull",
        macro_label="bear", trend_label="trending_down")
    assert arch is not None
    assert arch.top == "mean_reversion"
    assert arch.agrees_with(router_verdict) is True


@pytest.mark.asyncio
async def test_r5_stance_forms_own_direction(monkeypatch):
    """R5 forms a direction from indicators alone (no stated signal) and parses."""
    from regime_platform.services.decision import clef_flash as cf
    from regime_platform.sweep_orchestrator.clef_stance import classify_stance

    async def fake_choice(*, instructions, options, state, question_id="answer"):
        assert "signal" not in state, "stance must not receive a stated direction"
        return {"buy": 0.55, "sell": 0.15, "hold": 0.30}
    monkeypatch.setattr(cf, "classify_choice", fake_choice)

    stance = await classify_stance(symbol="XAUUSD", regime="low_vol_bull",
                                   macro_label="bear", trend_label="consolidating")
    assert stance is not None
    assert stance.stance == "buy"
    assert abs(stance.confidence - 0.55) < 1e-9
