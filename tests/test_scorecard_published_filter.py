"""E2E: scorecard published-book filter (2026-10-12, worklog
20261012_POOLED_ATR_AND_SCORECARD_FILTER.md).

The calibration bias / scorecard reads nt_calibration_records, fed by EVERY
signal (no qualified filter) — validated 962 suppressed : 38 published. The
PUBLISHED book (qualified=true) is the standard users experience; the full
population stays as a diagnostic stratifier.

These tests pin the read-table selection: published_only on -> the published
view; off/tiers -> the base/tiered table; fail-closed on an unreadable flag.
"""
from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator import supabase_client as sc
from regime_platform.core import feature_flags as ff


def test_published_only_defaults_true():
    """The published book is the standard by default."""
    assert ff.get_calibration_bias_published_only() is True


def test_read_table_is_published_view_when_on(monkeypatch):
    """published_only on + no tiers -> v_calibration_records_published."""
    monkeypatch.setattr(ff, "get_calibration_bias_published_only", lambda: True)
    assert sc._calibration_read_table() == sc.PUBLISHED_CALIBRATION_VIEW


def test_read_table_is_base_when_off(monkeypatch):
    """published_only off + cutover off -> the base table (full rollback)."""
    monkeypatch.setattr(ff, "get_calibration_bias_published_only", lambda: False)
    monkeypatch.setattr(ff, "get_calibration_cutover_mode", lambda: "off")
    assert sc._calibration_read_table() == "nt_calibration_records"


def test_tiers_take_precedence_over_published(monkeypatch):
    """An explicit evidence-tier request overrides the published filter — the
    diagnostic stratifier can still read the full tiered population."""
    monkeypatch.setattr(ff, "get_calibration_bias_published_only", lambda: True)
    assert sc._calibration_read_table(("qualified", "suppressed")) == sc.TIERED_CALIBRATION_VIEW


def test_read_table_fails_closed_on_unreadable_flag(monkeypatch):
    """An exception reading the flag must NOT silently change the population —
    fail closed to the base table."""
    def _boom():
        raise RuntimeError("flag unreadable")
    monkeypatch.setattr(ff, "get_calibration_bias_published_only", _boom)
    assert sc._calibration_read_table() == "nt_calibration_records"


def test_published_view_constant_is_migration_209():
    assert sc.PUBLISHED_CALIBRATION_VIEW == "v_calibration_records_published"
