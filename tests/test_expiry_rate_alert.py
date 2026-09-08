"""
Tests for regime_platform/sweep_orchestrator/expiry_rate_alert.py — priority
#3, worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.

check_expiry_rate_alerts() does I/O (Supabase reads/writes, Discord send)
via local imports inside the function body — patching the module attributes
on regime_platform.sweep_orchestrator.supabase_client and
notifications.discord still works because the local `from x import y`
resolves `x.y` fresh on every call.
"""

import asyncio

import regime_platform.sweep_orchestrator.expiry_rate_alert as expiry_rate_alert
import regime_platform.sweep_orchestrator.supabase_client as supabase_client
from regime_platform.sweep_orchestrator.expiry_rate_alert import (
    check_expiry_rate_alerts,
)

DEFAULT_CFG = {
    "enabled": True,
    "threshold_pct": 50.0,
    "min_resolved": 15,
    "cooldown_hours": 24.0,
}


def _scorecard_row(symbol="USDCHF", resolved=100, expired=70):
    expired_pct = round(100.0 * expired / resolved, 1) if resolved else None
    return {"symbol": symbol, "resolved": resolved, "expired": expired, "expired_pct": expired_pct}


def _patch_config(monkeypatch, **overrides):
    cfg = {**DEFAULT_CFG, **overrides}
    monkeypatch.setattr(expiry_rate_alert, "get_expiry_rate_alert_config", lambda: cfg)


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not have been called")


def test_disabled_config_is_a_no_op(monkeypatch):
    _patch_config(monkeypatch, enabled=False)
    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", _fail_if_called)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result == {"enabled": False, "checked": 0, "alerted": []}


def test_below_threshold_no_alert(monkeypatch):
    _patch_config(monkeypatch)

    async def fake_scorecard():
        return [_scorecard_row(resolved=100, expired=30)]  # 30% < 50% threshold

    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", fake_scorecard)
    monkeypatch.setattr(supabase_client, "fetch_expiry_rate_alert_state", _fail_if_called)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result["checked"] == 1
    assert result["alerted"] == []


def test_below_min_resolved_is_skipped_entirely(monkeypatch):
    _patch_config(monkeypatch, min_resolved=15)

    async def fake_scorecard():
        # 100% expired, but only 3 resolved -- too small a sample to alert on.
        return [_scorecard_row(resolved=3, expired=3)]

    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", fake_scorecard)
    monkeypatch.setattr(supabase_client, "fetch_expiry_rate_alert_state", _fail_if_called)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result["checked"] == 0
    assert result["alerted"] == []


def test_above_threshold_fires_alert_and_records_state(monkeypatch):
    _patch_config(monkeypatch)
    sent = {}
    recorded = {}

    async def fake_scorecard():
        return [_scorecard_row(symbol="USDCHF", resolved=100, expired=70)]  # 70%

    async def fake_alert_state(symbol):
        return None  # never alerted before

    async def fake_send_risk_alert(*, symbol, risk_type, message, data=None, level=None):
        sent["symbol"] = symbol
        sent["risk_type"] = risk_type
        sent["message"] = message
        return True

    async def fake_record(symbol, expired_pct, resolved_n):
        recorded["symbol"] = symbol
        recorded["expired_pct"] = expired_pct
        recorded["resolved_n"] = resolved_n

    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", fake_scorecard)
    monkeypatch.setattr(supabase_client, "fetch_expiry_rate_alert_state", fake_alert_state)
    monkeypatch.setattr(supabase_client, "record_expiry_rate_alert", fake_record)

    import notifications.discord as discord_module
    monkeypatch.setattr(discord_module.discord_notifier, "send_risk_alert", fake_send_risk_alert)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result["alerted"] == ["USDCHF"]
    assert sent["symbol"] == "USDCHF"
    assert sent["risk_type"] == "expiry_rate"
    assert "70.0%" in sent["message"]
    assert recorded == {"symbol": "USDCHF", "expired_pct": 70.0, "resolved_n": 100}


def test_above_threshold_within_cooldown_is_suppressed(monkeypatch):
    _patch_config(monkeypatch, cooldown_hours=24.0)
    from datetime import datetime, timezone

    async def fake_scorecard():
        return [_scorecard_row(symbol="USDCHF", resolved=100, expired=70)]

    async def fake_alert_state(symbol):
        return {"symbol": symbol, "last_alert_at": datetime.now(timezone.utc).isoformat()}

    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", fake_scorecard)
    monkeypatch.setattr(supabase_client, "fetch_expiry_rate_alert_state", fake_alert_state)
    monkeypatch.setattr(supabase_client, "record_expiry_rate_alert", _fail_if_called)

    import notifications.discord as discord_module
    monkeypatch.setattr(discord_module.discord_notifier, "send_risk_alert", _fail_if_called)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result["alerted"] == []


def test_multiple_symbols_only_alerts_the_ones_over_threshold(monkeypatch):
    _patch_config(monkeypatch)
    alerted_symbols = []

    async def fake_scorecard():
        return [
            _scorecard_row(symbol="USDCHF", resolved=100, expired=70),  # 70% -> alert
            _scorecard_row(symbol="XAGUSD", resolved=100, expired=20),  # 20% -> no alert
        ]

    async def fake_alert_state(symbol):
        return None

    async def fake_send_risk_alert(*, symbol, risk_type, message, data=None, level=None):
        alerted_symbols.append(symbol)
        return True

    async def fake_record(symbol, expired_pct, resolved_n):
        pass

    monkeypatch.setattr(supabase_client, "fetch_symbol_scorecard_7d", fake_scorecard)
    monkeypatch.setattr(supabase_client, "fetch_expiry_rate_alert_state", fake_alert_state)
    monkeypatch.setattr(supabase_client, "record_expiry_rate_alert", fake_record)

    import notifications.discord as discord_module
    monkeypatch.setattr(discord_module.discord_notifier, "send_risk_alert", fake_send_risk_alert)

    result = asyncio.run(check_expiry_rate_alerts())

    assert result["checked"] == 2
    assert alerted_symbols == ["USDCHF"]
