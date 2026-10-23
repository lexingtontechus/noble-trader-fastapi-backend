"""
Tests for fix #2 (worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §4):
unifying the calibration-bias API with the live enforcement loop's windowed
query + thresholds, instead of a separately-implemented all-time query with
its own hardcoded ±0.05 threshold.
"""

import pytest

from regime_platform.core.settings import settings
from regime_platform.core.feature_flags import get_eod_bias_window_days
from regime_platform.middleware.rate_limit import limiter
from regime_platform.routers import calibration as calibration_router
from regime_platform.sweep_orchestrator import eod_correction


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """The endpoints are @limiter.limit()-decorated (slowapi), which requires
    a real starlette.requests.Request instance to check limits. These tests
    call the route functions directly (no ASGI request), so disable the
    limiter's enforcement for the duration of each test."""
    was_enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = was_enabled


# ─── Threshold sync guard (mirrors the EOD_ACTION_FRESH_HOURS pattern) ────

def test_enforced_threshold_settings_mirror_eod_correction():
    """settings.EOD_BIAS_ENFORCED_THRESHOLD MUST mirror
    eod_correction.OVERCONFIDENT_BIAS / UNDERCONFIDENT_BIAS — if these drift
    apart, the public API and the live enforcement loop disagree again."""
    assert eod_correction.OVERCONFIDENT_BIAS == settings.EOD_BIAS_ENFORCED_THRESHOLD
    assert eod_correction.UNDERCONFIDENT_BIAS == -settings.EOD_BIAS_ENFORCED_THRESHOLD


# ─── _build_bias_response — shared assembly function ──────────────────────

def test_build_bias_response_both_views_present():
    enforced = {"bias": 0.15, "n": 50, "win_rate": 0.40, "avg_pred": 0.55}
    raw = {"bias": 0.45, "n": 50, "win_rate": 0.40, "avg_pred": 0.55, "avg_pred_raw": 0.85}

    resp = calibration_router._build_bias_response("AAPL", enforced, raw, window_days=7)

    assert resp.symbol == "AAPL"
    assert resp.window_days == 7
    # Enforced view: bias=0.15 > OVERCONFIDENT_BIAS(0.10) -> OVERCONFIDENT
    assert resp.bias == 0.15
    assert resp.status == "OVERCONFIDENT"
    assert resp.n_records == 50
    # Raw view: bias=0.45 >= EOD_BIAS_RAW_OVERCONFIDENT(0.30) -> OVERCONFIDENT
    assert resp.bias_raw == 0.45
    assert resp.avg_predicted_p_win_raw == 0.85
    assert resp.status_raw == "OVERCONFIDENT"
    # Thresholds are surfaced, not hidden
    assert resp.threshold_enforced == settings.EOD_BIAS_ENFORCED_THRESHOLD
    assert resp.threshold_raw_overconfident == settings.EOD_BIAS_RAW_OVERCONFIDENT
    assert resp.threshold_raw_underconfident == settings.EOD_BIAS_RAW_UNDERCONFIDENT


def test_build_bias_response_enforced_and_raw_can_disagree():
    """The whole point of exposing both views: a symbol can be CALIBRATED
    under the enforced (post-shrink) view while still OVERCONFIDENT under
    the raw (pre-shrink) view — this is the exact AAPL/US500 masking bug
    documented in the audit, now made visible instead of hidden."""
    enforced = {"bias": 0.02, "n": 50, "win_rate": 0.50, "avg_pred": 0.52}
    raw = {"bias": 0.45, "n": 50, "win_rate": 0.50, "avg_pred": 0.52, "avg_pred_raw": 0.95}

    resp = calibration_router._build_bias_response("AAPL", enforced, raw, window_days=7)

    assert resp.status == "CALIBRATED"
    assert resp.status_raw == "OVERCONFIDENT"


def test_build_bias_response_only_enforced_available():
    enforced = {"bias": -0.02, "n": 30, "win_rate": 0.52, "avg_pred": 0.50}
    resp = calibration_router._build_bias_response("EURUSD", enforced, None, window_days=7)
    assert resp.status == "CALIBRATED"
    assert resp.status_raw == "INSUFFICIENT_DATA"  # default, never set
    assert resp.bias_raw is None
    assert resp.n_records == 30


def test_build_bias_response_only_raw_available():
    """n_records falls back to the raw count when the enforced fetch
    returned None (e.g. below min_records only for one of the two calls)."""
    raw = {"bias": 0.10, "n": 25, "win_rate": 0.40, "avg_pred": 0.50, "avg_pred_raw": 0.50}
    resp = calibration_router._build_bias_response("XAUEUR", None, raw, window_days=7)
    assert resp.status == "INSUFFICIENT_DATA"  # default, never set
    assert resp.status_raw == "CALIBRATED"
    assert resp.n_records == 25


def test_build_bias_response_neither_available_is_insufficient_data():
    resp = calibration_router._build_bias_response("AUDUSD", None, None, window_days=7)
    assert resp.status == "INSUFFICIENT_DATA"
    assert resp.status_raw == "INSUFFICIENT_DATA"
    assert resp.n_records == 0
    assert resp.bias == 0.0
    assert resp.bias_raw is None


# ─── Endpoint wiring: calls the SAME functions the enforcement loop uses ──

@pytest.mark.asyncio
async def test_get_calibration_bias_calls_shared_fetch_functions(monkeypatch):
    """The endpoint must call fetch_calibration_bias / fetch_calibration_bias_raw
    (the exact functions eod_correction.py uses) rather than a separate
    all-time query, and must pass through the window_days it was given."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SECRET_KEY", "test-key")

    calls = {}

    async def fake_fetch_calibration_bias(symbol, venue=None, *, window_days=None, min_records=None):
        calls["enforced_window_days"] = window_days
        return {"bias": 0.05, "n": 40, "win_rate": 0.45, "avg_pred": 0.50}

    async def fake_fetch_calibration_bias_raw(symbol, venue=None, *, window_days=None, min_records=None):
        calls["raw_window_days"] = window_days
        return {"bias": 0.35, "n": 40, "win_rate": 0.45, "avg_pred": 0.50, "avg_pred_raw": 0.80}

    import regime_platform.sweep_orchestrator.supabase_client as supabase_client
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias", fake_fetch_calibration_bias)
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias_raw", fake_fetch_calibration_bias_raw)

    resp = await calibration_router.get_calibration_bias(
        request=None, symbol="AAPL", venue=None, window_days=3,
    )

    assert calls["enforced_window_days"] == 3
    assert calls["raw_window_days"] == 3
    assert resp.status == "CALIBRATED"      # 0.05 < 0.10
    assert resp.status_raw == "OVERCONFIDENT"  # 0.35 >= 0.30


@pytest.mark.asyncio
async def test_get_calibration_bias_defaults_window_to_settings(monkeypatch):
    """When window_days is not passed, it must default to
    eod_bias.window_days (config/feature_flags.yaml) — the same window the
    enforcement loop uses — not the old all-time (unwindowed) behavior."""
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SECRET_KEY", "test-key")

    seen = {}

    async def fake_fetch(symbol, venue=None, *, window_days=None, min_records=None):
        seen["window_days"] = window_days
        return None

    import regime_platform.sweep_orchestrator.supabase_client as supabase_client
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias", fake_fetch)
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias_raw", fake_fetch)

    await calibration_router.get_calibration_bias(
        request=None, symbol="AAPL", venue=None, window_days=None,
    )

    assert seen["window_days"] == get_eod_bias_window_days()


@pytest.mark.asyncio
async def test_get_calibration_status_iterates_active_symbols(monkeypatch):
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(settings, "SUPABASE_SECRET_KEY", "test-key")

    async def fake_fetch_active_symbols():
        return [{"symbol": "AAPL"}, {"symbol": "US500"}]

    async def fake_fetch_calibration_bias(symbol, venue=None, *, window_days=None, min_records=None):
        return {"bias": 0.02, "n": 40, "win_rate": 0.50, "avg_pred": 0.52}

    async def fake_fetch_calibration_bias_raw(symbol, venue=None, *, window_days=None, min_records=None):
        return {"bias": 0.40, "n": 40, "win_rate": 0.50, "avg_pred": 0.52, "avg_pred_raw": 0.90}

    import regime_platform.sweep_orchestrator.supabase_client as supabase_client
    monkeypatch.setattr(supabase_client, "fetch_active_symbols", fake_fetch_active_symbols)
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias", fake_fetch_calibration_bias)
    monkeypatch.setattr(supabase_client, "fetch_calibration_bias_raw", fake_fetch_calibration_bias_raw)

    resp = await calibration_router.get_calibration_status(request=None, window_days=None)

    assert resp.total_symbols == 2
    symbols = {r.symbol for r in resp.results}
    assert symbols == {"AAPL", "US500"}
    for r in resp.results:
        assert r.status == "CALIBRATED"       # enforced bias 0.02
        assert r.status_raw == "OVERCONFIDENT"  # raw bias 0.40
