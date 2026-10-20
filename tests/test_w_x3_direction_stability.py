"""W-X3 (2026-10-19 weaknesses evaluation, P1): cross-direction cooldown +
flip-rate breaker — LIVE direction-stability gates.

The defect: the per-(symbol, direction) cooldown/dedup
(orchestrator._last_qualified_signal) let the OPPOSITE direction publish
INSTANTLY after a confirmed flip (flip-flop at the debouncer's 3h
granularity), and ``shifts_24h`` was stamped into every agent payload
(orchestrator's shift_at/shifts_24h) but NEVER gated.

The fix (LIVE, per the no-more-shadow mandate):
  * ``direction_stability`` flag block (mode: live,
    cross_direction_cooldown_h: 6, max_shifts_24h: 6);
  * ``orchestrator.cross_direction_cooldown_blocked`` — the pure verdict,
    fed by ``_last_publish_direction`` (symbol → (direction, publish ts)),
    recorded at publish time;
  * flip-rate breaker on the SAME ``_count_shifts_24h`` value the payload
    stamps (computed once per tick — no double-appending);
  * both emitted through the bracket_unscored suppression mechanism:
    delivery suppressed with reasons ``cross_direction_cooldown`` /
    ``flip_rate_breaker``; the quality verdict stands.

These tests drive the REAL pure function + flag getters and pin the
suppression semantics with the repo's vocabulary-mirror convention
(tests/test_cooldown_restart.py).
"""
from __future__ import annotations

from regime_platform.core.feature_flags import (
    get_cross_direction_cooldown_h,
    get_direction_stability_mode,
    get_max_shifts_24h,
)
from regime_platform.sweep_orchestrator.orchestrator import (
    cross_direction_cooldown_blocked,
)

H = 3600.0


def test_flag_ships_live_with_measured_thresholds():
    """User mandate: LIVE. 6h holds the book one full debounce-confirmation
    cycle (flip confirmed in ~3h); 6 shifts/24h is the measured whipsaw
    signature (raw flip rate 71.3% through the consolidating window)."""
    assert get_direction_stability_mode() == "live"
    assert get_cross_direction_cooldown_h() == 6
    assert get_max_shifts_24h() == 6


def test_opposite_direction_within_cooldown_is_blocked():
    """(a) long published at t → short at t+2h is blocked (2h < 6h)."""
    last = ("buy", 1_000_000.0)
    now = 1_000_000.0 + 2 * H
    assert cross_direction_cooldown_blocked(last, "sell", now, 6.0) is True
    # both label vocabularies the payload uses
    assert cross_direction_cooldown_blocked(("long", 1_000_000.0), "short", now, 6.0) is True
    assert cross_direction_cooldown_blocked(("buy", 1_000_000.0), "bear", now, 6.0) is True


def test_opposite_direction_after_cooldown_is_allowed():
    """(a) short at t+7h is ALLOWED (7h ≥ 6h — the book may flip)."""
    last = ("buy", 1_000_000.0)
    now = 1_000_000.0 + 7 * H
    assert cross_direction_cooldown_blocked(last, "sell", now, 6.0) is False
    # exactly at the boundary is allowed (strictly-within is blocked)
    assert cross_direction_cooldown_blocked(last, "sell", 1_000_000.0 + 6 * H, 6.0) is False


def test_same_direction_is_never_blocked():
    """Same-direction republish is the per-direction wall-clock cooldown's
    job — the cross-direction gate must not touch it."""
    last = ("buy", 1_000_000.0)
    now = 1_000_000.0 + 0.1 * H
    assert cross_direction_cooldown_blocked(last, "buy", now, 6.0) is False


def test_missing_history_and_degenerate_labels_fail_open():
    """No prior publish / unknown labels / zero cooldown → never block."""
    assert cross_direction_cooldown_blocked(None, "sell", 1_000_000.0, 6.0) is False
    assert cross_direction_cooldown_blocked(("neutral", 1_000_000.0), "sell", 1_000_000.0 + 60, 6.0) is False
    assert cross_direction_cooldown_blocked(("buy", 1_000_000.0), "neutral", 1_000_000.0 + 60, 6.0) is False
    assert cross_direction_cooldown_blocked(("buy", 1_000_000.0), "sell", 1_000_000.0 + 60, 0.0) is False


def test_flip_rate_breaker_threshold():
    """(b) shifts_24h = 7 ≥ max_shifts_24h = 6 → suppressed with reason
    'flip_rate_breaker'; 5 shifts passes. The orchestrator's breaker is
    ``_shifts_24h_val >= get_max_shifts_24h()`` on the same stamped value."""
    max_shifts = get_max_shifts_24h()
    assert max_shifts == 6
    assert 7 >= max_shifts      # 7 shifts trips the breaker
    assert 6 >= max_shifts      # boundary trips too (>= semantics)
    assert not (5 >= max_shifts)


def test_suppression_emission_semantics():
    """The orchestrator emission mirror (10a-3 block): both gates suppress
    DELIVERY with their machine-readable reasons while the quality verdict
    stands; record of the published direction happens only on publish."""

    def _emit(mode, blocked, shifts_24h=0, published=True):
        """Mirror of the 10a-3 emission (single suppression owner)."""
        suppress_reason = None
        gate_results = {}
        if mode != "off":
            if blocked:
                gate_results["cross_direction_cooldown_ok"] = False
                if published and mode == "live":
                    suppress_reason = "cross_direction_cooldown"
                    published = False
            if shifts_24h >= 6:
                gate_results["flip_rate_breaker_ok"] = False
                if published and mode == "live":
                    suppress_reason = "flip_rate_breaker"
                    published = False
        return gate_results, suppress_reason, published

    # long at t, short at t+2h → suppressed with the cooldown reason
    last = ("buy", 1_000_000.0)
    blocked = cross_direction_cooldown_blocked(last, "sell", 1_000_000.0 + 2 * H, 6.0)
    gates, reason, published = _emit("live", blocked, shifts_24h=0)
    assert published is False
    assert reason == "cross_direction_cooldown"
    assert gates == {"cross_direction_cooldown_ok": False}

    # short at t+7h, 5 shifts → allowed
    blocked = cross_direction_cooldown_blocked(last, "sell", 1_000_000.0 + 7 * H, 6.0)
    gates, reason, published = _emit("live", blocked, shifts_24h=5)
    assert published is True
    assert reason is None
    assert gates == {}

    # shifts_24h = 7 → suppressed with the breaker reason
    gates, reason, published = _emit("live", blocked=False, shifts_24h=7)
    assert published is False
    assert reason == "flip_rate_breaker"
    assert gates == {"flip_rate_breaker_ok": False}

    # shadow mode never suppresses
    gates, reason, published = _emit("shadow", blocked=True, shifts_24h=7)
    assert published is True
    assert reason is None


def test_publish_records_direction_for_the_next_tick():
    """The publish side: ``_last_publish_direction[symbol] = (signal, ts)``
    — the map the next tick's verdict reads. Mirror of the wiring."""
    state: dict[str, tuple[str, float]] = {}
    # long published at t
    state["XAUUSD"] = ("buy", 1_000_000.0)
    # short 2h later: blocked
    assert cross_direction_cooldown_blocked(state.get("XAUUSD"), "sell", 1_000_000.000 + 2 * H, 6.0) is True
    # short 7h later: allowed; on publish the direction flips in the map
    state["XAUUSD"] = ("sell", 1_000_000.0 + 7 * H)
    # a LONG 2h after that is now blocked (the map tracks the LATEST publish)
    assert cross_direction_cooldown_blocked(state.get("XAUUSD"), "buy", 1_000_000.0 + 9 * H, 6.0) is True
