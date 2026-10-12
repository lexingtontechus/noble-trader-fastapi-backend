"""
Tests for the calibration cutover (migration 192 + `calibration_cutover_mode`).

The load-bearing guarantee: **the cutover is inert until a timestamp is set**.
Shipping the flag "on" alongside the basis flip must not change any bias number
until an operator writes the cutover row — otherwise the flip and the cutover
would need a coordination window, and a gap between them would silently mix
close-walk and intrabar evidence.
"""

from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_calibration_cutover_mode,
    get_eod_resolver_basis,
)
from regime_platform.sweep_orchestrator import supabase_client as sc


# ─── The flags ────────────────────────────────────────────────────────────


def test_basis_is_flipped_to_intrabar():
    """The §3.1 kill criterion was not met at any horizon, so the flip shipped."""
    assert get_eod_resolver_basis() == "intrabar"


def test_cutover_mode_defaults_on():
    assert get_calibration_cutover_mode() == "on"


def test_cutover_mode_is_fail_safe_on_typo(monkeypatch):
    """Anything but an explicit 'off' resolves to 'on' — the safe direction is to
    EXCLUDE stale-basis evidence, not include it."""
    import regime_platform.core.feature_flags as ff

    for bad in ("of", "false", "", "TRUE", "0", "no"):
        monkeypatch.setattr(ff, "_load", lambda bad=bad: {"calibration_cutover_mode": bad})
        assert ff.get_calibration_cutover_mode() == "on", f"{bad!r} should resolve to 'on'"


def test_cutover_mode_resolves_off(monkeypatch):
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "_load", lambda: {"calibration_cutover_mode": "off"})
    assert ff.get_calibration_cutover_mode() == "off"


# ─── Read-table selection ─────────────────────────────────────────────────


def test_no_tiers_cutover_on_reads_the_cutover_view(monkeypatch):
    """published_only=off + cutover on -> the cutover view (the pre-209
    diagnostic stratifier path). With published_only on (default), the
    published view supersedes — see test_scorecard_published_filter."""
    import regime_platform.core.feature_flags as ff
    # Force the pre-209 path explicitly.
    monkeypatch.setattr(ff, "get_calibration_bias_published_only", lambda: False)
    assert sc._calibration_read_table(None) == "v_calibration_records_current"


def test_no_tiers_cutover_off_reads_the_base_table(monkeypatch):
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "get_calibration_cutover_mode", lambda: "off")
    assert sc._calibration_read_table(None) == "nt_calibration_records"


def test_tiers_always_read_the_tiered_view():
    """The tiered view carries the cutover predicate itself (migration 192), so
    passing tiers must select it regardless of the flag — the alternative would
    be composing two views and double-joining nt_signal_sim."""
    assert sc._calibration_read_table(("qualified",)) == "v_calibration_records_tiered"


def test_tiers_win_over_the_flag(monkeypatch):
    import regime_platform.core.feature_flags as ff

    monkeypatch.setattr(ff, "get_calibration_cutover_mode", lambda: "off")
    assert sc._calibration_read_table(("qualified", "suppressed")) == \
        "v_calibration_records_tiered"


def test_read_table_fails_closed_on_flag_error(monkeypatch):
    """An unreadable flag must not silently start filtering evidence."""
    import regime_platform.core.feature_flags as ff

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(ff, "get_calibration_cutover_mode", _boom)
    assert sc._calibration_read_table(None) == "nt_calibration_records"


def test_view_constants_are_distinct():
    assert sc.TIERED_CALIBRATION_VIEW != sc.CUTOVER_CALIBRATION_VIEW


# ─── Both bias readers honour the selection ───────────────────────────────


def test_fetch_calibration_bias_uses_the_selector():
    """Regression: the base-table literal must be gone from both readers."""
    import inspect

    for fn in (sc.fetch_calibration_bias, sc.fetch_calibration_bias_raw):
        src = inspect.getsource(fn)
        assert "_calibration_read_table(" in src, f"{fn.__name__} bypasses the selector"
        assert '_url("nt_calibration_records")' not in src, (
            f"{fn.__name__} still reads the base table directly"
        )


def test_raw_reader_preserves_the_raw_select_list():
    """The raw variant must keep selecting predicted_p_win_raw — it is the whole
    point of that function."""
    import inspect

    src = inspect.getsource(sc.fetch_calibration_bias_raw)
    assert "predicted_p_win_raw" in src
