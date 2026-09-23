"""B2 stamp: qualified_geometry_shadow on sim rows for B5 reject-rate review."""

from __future__ import annotations

from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
from regime_platform.sweep_orchestrator.supabase_client import NT_SIGNAL_SIM_COLUMNS


def test_column_in_whitelist():
    assert "qualified_geometry_shadow" in NT_SIGNAL_SIM_COLUMNS


def test_payload_stamps_true_and_false():
    base = dict(
        symbol="EURUSD", venue="OANDA", ts="2026-09-23T00:00:00+00:00",
        signal="buy", entry_price=1.1, stop_loss=1.0, take_profit=1.2,
        brick_size=0.01, sl_bricks=3, tp_bricks=5,
        p_win=0.6, ev=0.1, regime="low_vol_bull", regime_conf=0.8,
        source="sweep-light", qualified=True,
    )
    p_true = _build_signal_sim_payload(**base, qualified_geometry_shadow=True)
    assert p_true["qualified_geometry_shadow"] is True
    p_false = _build_signal_sim_payload(**base, qualified_geometry_shadow=False)
    assert p_false["qualified_geometry_shadow"] is False
    p_none = _build_signal_sim_payload(**base)
    assert "qualified_geometry_shadow" not in p_none


def test_filter_keeps_stamp():
    from regime_platform.sweep_orchestrator.supabase_client import _filter_payload

    clean = _filter_payload(
        {"symbol": "X", "qualified_geometry_shadow": True, "nope": 1},
        NT_SIGNAL_SIM_COLUMNS,
    )
    assert clean["qualified_geometry_shadow"] is True
    assert "nope" not in clean


def test_orchestrator_sets_shadow_flag_in_gate_block():
    import inspect

    from regime_platform.sweep_orchestrator import orchestrator

    src = inspect.getsource(orchestrator)
    assert "_qualified_geometry_shadow = True" in src
    assert "qualified_geometry_shadow=_qualified_geometry_shadow" in src
