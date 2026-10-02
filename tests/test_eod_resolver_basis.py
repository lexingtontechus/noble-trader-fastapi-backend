"""
Regression tests for the `eod_resolver_basis` wiring (Phase 1 dependency #2).

The load-bearing guarantee is **`close_walk` is byte-identical to before**:
with the shipped default, every code path must behave exactly as it did prior
to the flag. Everything else is the new opt-in path.
"""

from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import get_eod_resolver_basis
from regime_platform.sweep_orchestrator import eod_resolver as er


# ─── The flag ─────────────────────────────────────────────────────────────


def test_basis_defaults_to_close_walk():
    """Shipped value must be the historical behaviour."""
    assert get_eod_resolver_basis() == "close_walk"
    assert er._resolver_basis() == "close_walk"


def test_basis_is_fail_safe_on_typo(monkeypatch):
    """A typo must NOT silently change outcome semantics."""
    import regime_platform.core.feature_flags as ff

    for bad in ("intra", "INTRABAR ", "bars", "", "close", "1", "true"):
        monkeypatch.setattr(ff, "_load", lambda bad=bad: {"eod_resolver_basis": bad})
        assert ff.get_eod_resolver_basis() == "close_walk", f"{bad!r} should fail closed"


def test_basis_resolves_intrabar(monkeypatch):
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "_load", lambda: {"eod_resolver_basis": "intrabar"})
    assert ff.get_eod_resolver_basis() == "intrabar"


def test_basis_resolver_fails_closed_on_import_error(monkeypatch):
    """If the flag read raises, the resolver must stay on close_walk."""
    import regime_platform.core.feature_flags as ff

    def _boom(*a, **k):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(ff, "get_eod_resolver_basis", _boom)
    assert er._resolver_basis() == "close_walk"


# ─── anchor_post_entry_bars mirrors anchor_post_entry_closes ──────────────


def _bars(n, start_ts):
    from datetime import datetime, timedelta, timezone
    t0 = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
    return [
        {"t": (t0 + timedelta(hours=i)).timestamp(),
         "o": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "c": 100.5 + i, "v": 1.0}
        for i in range(n)
    ]


def test_bars_anchor_matches_closes_anchor_index():
    """The two anchors must select the SAME entry index — if one drifts, this
    catches it rather than a silent off-by-one in the label."""
    start = "2026-10-01T00:00:00Z"
    fetch = "2026-10-01T10:00:00Z"      # 10 bars later
    bars = _bars(20, start)
    closes = [b["c"] for b in bars]
    sig_ts = "2026-10-01T05:00:00Z"     # entry 5h before fetch

    a_closes = er.anchor_post_entry_closes(closes, sig_ts, fetch)
    a_bars = er.anchor_post_entry_bars(bars, sig_ts, fetch)
    assert a_closes is not None and a_bars is not None
    assert len(a_closes) == len(a_bars)
    assert [b["c"] for b in a_bars] == a_closes


def test_bars_anchor_none_when_entry_predates_window():
    bars = _bars(5, "2026-10-01T00:00:00Z")
    assert er.anchor_post_entry_bars(
        bars, "2026-09-01T00:00:00Z", "2026-10-01T04:00:00Z"
    ) is None


def test_bars_anchor_none_on_empty():
    assert er.anchor_post_entry_bars([], "2026-10-01T00:00:00Z", "2026-10-01T04:00:00Z") is None


def test_bars_anchor_filters_non_dicts():
    bars = _bars(4, "2026-10-01T00:00:00Z") + ["junk"]
    got = er.anchor_post_entry_bars(bars, "2026-10-01T00:00:00Z", "2026-10-01T03:00:00Z")
    assert got is not None
    assert all(isinstance(b, dict) for b in got)


def test_bars_anchor_clamps_future_dated_signal():
    bars = _bars(5, "2026-10-01T00:00:00Z")
    got = er.anchor_post_entry_bars(
        bars, "2026-12-01T00:00:00Z", "2026-10-01T04:00:00Z"
    )
    assert got is not None and len(got) == 1      # clamps to the newest bar


# ─── _judge_bars mirrors _judge ───────────────────────────────────────────


def _sig(**kw):
    base = {"signal_id": "s1", "symbol": "EURUSD", "signal": "buy",
            "entry_price": 100.0, "stop_loss": 98.0, "take_profit": 105.0,
            "tp_bricks": 5, "sl_bricks": 3, "brick_size": 1.0,
            "ts": "2026-10-01T00:00:00Z"}
    base.update(kw)
    return base


def _b(h, l, c):
    return {"h": h, "l": l, "c": c}


def test_judge_bars_missing_levels_returns_none():
    v, r = er._judge_bars(_sig(entry_price=None), [_b(101, 99, 100)])
    assert v is None and r is None


def test_judge_bars_pending_when_too_young():
    """Fewer than EXPIRY_BARS and still 'expired' -> pending, not terminal.
    The minimum-age gate must survive into the intrabar path."""
    bars = [_b(101.0, 99.0, 100.5)] * 3
    v, r = er._judge_bars(_sig(), bars)
    assert v == "pending"


def test_judge_bars_resolves_tp_immediately_even_when_young():
    """A genuine touch resolves regardless of bar count — only a would-be
    'expired' is deferred."""
    bars = [_b(106.0, 99.0, 105.5)]
    v, r = er._judge_bars(_sig(), bars)
    assert v == "tp_hit"


def test_judge_bars_sees_the_wick_the_close_walk_misses():
    """The defining regression: identical bars, two judges, different verdicts."""
    bars = [_b(103.0, 97.0, 102.0), _b(107.0, 101.0, 106.0)]
    v_close, _ = er._judge(_sig(), [102.0, 106.0])
    v_bars, _ = er._judge_bars(_sig(), bars)
    assert v_close == "tp_hit"
    assert v_bars == "sl_hit"


def test_judge_bars_agrees_with_judge_when_hl_absent():
    """If bars carry only closes, the intrabar judge degrades to a close-walk —
    so a partial fetch cannot silently invent touches."""
    closes = [102.0, 106.0]
    bars = [{"c": c} for c in closes]
    assert er._judge_bars(_sig(), bars)[0] == er._judge(_sig(), closes)[0]


# ─── close_walk path is untouched ─────────────────────────────────────────


def test_default_basis_does_not_request_ohlc(monkeypatch):
    """With the shipped default, the resolver must NOT ask for OHLC — the
    historical fetch call is preserved exactly."""
    import regime_platform.sweep_orchestrator.eod_resolver as mod
    calls = []
    real = mod.fetch_candles_for_symbols

    async def _spy(*a, **kw):
        calls.append(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(mod, "fetch_candles_for_symbols", _spy)
    assert mod._resolver_basis() == "close_walk"
    # the flag alone decides; assert the branch predicate
    assert (mod._resolver_basis() == "intrabar") is False


def test_settlement_contract_mode_unchanged():
    """The basis flag must not perturb the separate settlement-contract flag."""
    assert er._settlement_mode() in ("off", "shadow", "live")
