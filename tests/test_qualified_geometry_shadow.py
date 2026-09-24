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


# ── B5 split (2026-09-24): B gate is geometry-risk only ─────────────────


def test_light_gate_rejects_only_on_risk_over_cap():
    from regime_platform.sweep_orchestrator.geometry_risk import (
        light_geometry_would_reject,
    )

    assert light_geometry_would_reject(0.36, risk_cap=0.35) is True
    assert light_geometry_would_reject(0.35, risk_cap=0.35) is False  # strict >
    assert light_geometry_would_reject(0.10, risk_cap=0.35) is False


def test_light_gate_fails_open_when_risk_unscored():
    from regime_platform.sweep_orchestrator.geometry_risk import (
        light_geometry_would_reject,
    )

    assert light_geometry_would_reject(None, risk_cap=0.35) is False


def test_light_gate_signature_has_no_bracket_input():
    """Bracket floor is R2's gate (p_win_bracket_mode); B must not read it."""
    import inspect

    from regime_platform.sweep_orchestrator.geometry_risk import (
        light_geometry_would_reject,
    )

    params = inspect.signature(light_geometry_would_reject).parameters
    assert set(params) == {"geometry_risk_hat", "risk_cap"}


def test_b_gate_block_no_longer_reads_bracket():
    import inspect

    from regime_platform.sweep_orchestrator import orchestrator

    src = inspect.getsource(orchestrator)
    start = src.index("# ── B: light-path geometry QUALIFIED gate")
    end = src.index("sim_payload = _build_signal_sim_payload(", start)
    block = src[start:end]
    assert "light_geometry_would_reject(" in block
    assert "_p_win_bracket" not in block
    assert "get_min_p_win_bracket" not in block
    # false stamp only when risk was actually scored (fail-open → NULL)
    assert '_light_gate == "shadow" and _risk_scored' in block
