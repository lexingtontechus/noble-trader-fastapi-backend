"""Supabase timeout/retry + log-format fixes (2026-09-23).

Guards: empty ReadTimeout('') must still log a type name; markov shadow
failures stay WARNING; R2 paths use 15s + retry.
"""

from __future__ import annotations

import inspect

from regime_platform.sweep_orchestrator import p_win_bracket, supabase_client


def test_log_regime_entry_logs_type_and_uses_retry():
    src = inspect.getsource(supabase_client.log_regime_entry)
    assert "timeout=15.0" in src
    assert "for attempt in (1, 2)" in src
    assert "type(last_exc).__name__" in src
    assert 'failed: %s"' not in src  # bare exc only — the empty-string bug
    assert "(502, 503)" in src      # 2026-09-24: gateway errors retried


def test_insert_row_timeout_and_retry_policy():
    src = inspect.getsource(supabase_client._insert_row)
    assert "timeout=15.0" in src
    assert "idempotent" in src and "PGRST204" in src


def test_markov_shadow_is_warning_not_error():
    src = inspect.getsource(supabase_client.record_markov_shadow_compare)
    assert "timeout=15.0" in src
    assert "for attempt in (1, 2)" in src
    assert "logger.warning(" in src
    assert "logger.error(" not in src


def test_r3_cell_fetch_uses_retry():
    src = inspect.getsource(supabase_client.fetch_calibration_bias_cell)
    assert "timeout=15.0" in src
    assert "for attempt in (1, 2)" in src
    assert "type(last_exc).__name__" in src


def test_r2_lookup_bracket_probs_uses_retry():
    src = inspect.getsource(p_win_bracket.lookup_bracket_probs)
    assert "timeout=15.0" in src
    assert "for attempt in (1, 2)" in src
    assert "type(last_exc).__name__" in src


def test_r2_upserts_use_retry():
    for fn in (
        supabase_client.upsert_live_expectancy_daily,
        supabase_client.upsert_calibration_bias_by_cell,
    ):
        src = inspect.getsource(fn)
        assert "timeout=15.0" in src
        assert "for attempt in (1, 2)" in src
        assert "type(last_exc).__name__" in src
