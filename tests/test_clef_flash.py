"""Tests for the Clef-Flash structured-decision caller and its shadow stamp.

Scope: docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md and
docs/scopes/20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md.

All network I/O is mocked. Covers: the state/questions builders, the
direction-answer parser (buy/sell sides, missing shape, wait default), the
fail-open contract (no config -> None), the end-to-end decide_direction path,
and the payload/whitelist wiring that carries the stamp onto nt_signal_sim.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regime_platform.services.decision import clef_flash


# ── build_direction_state ─────────────────────────────────────────────

class TestBuildState:
    def test_drops_none_fields(self):
        state = clef_flash.build_direction_state(
            symbol="XAUUSD", signal="buy", regime=None, regime_conf=0.81,
        )
        assert state["symbol"] == "XAUUSD"
        assert state["signal"] == "buy"
        assert state["regime_conf"] == 0.81
        assert "regime" not in state  # None dropped

    def test_carries_context_only(self):
        state = clef_flash.build_direction_state(
            symbol="EURUSD", signal="sell", regime="med_high_vol_bear",
            regime_conf=0.77, entry_price=1.085, stop_loss=1.090,
            take_profit=1.075, tp_distance_atr=4.2, sl_distance_atr=3.0,
            macro_label="bear", trend_label="trending_down", p_win_dir=0.34,
        )
        # No raw candles / renko series — context only (scope §1 rule).
        assert set(state) == {
            "symbol", "signal", "regime", "regime_conf", "entry_price",
            "stop_loss", "take_profit", "tp_distance_atr", "sl_distance_atr",
            "macro_label", "trend_label", "p_win_dir",
        }


# ── build_direction_questions ─────────────────────────────────────────

class TestBuildQuestions:
    def test_three_way_choice(self):
        q = clef_flash.build_direction_questions()
        assert q["direction"]["type"] == "choice"
        assert set(q["direction"]["criteria"]) == {"buy", "sell", "wait"}


# ── parse_direction_answer ────────────────────────────────────────────

class TestParseAnswer:
    def _answers(self, buy, sell, wait):
        return {"direction": {"choice": "buy", "probabilities": {
            "buy": buy, "sell": sell, "wait": wait}}}

    def test_buy_side_takes_buy_probability(self):
        d = clef_flash.parse_direction_answer(self._answers(0.85, 0.04, 0.11), signal="buy")
        assert d is not None
        assert d.p_clef_dir == pytest.approx(0.85)
        assert d.p_clef_wait == pytest.approx(0.11)

    def test_sell_side_takes_sell_probability(self):
        d = clef_flash.parse_direction_answer(self._answers(0.85, 0.04, 0.11), signal="sell")
        assert d is not None
        assert d.p_clef_dir == pytest.approx(0.04)  # direction-relative

    def test_missing_direction_returns_none(self):
        assert clef_flash.parse_direction_answer({"other": {}}, signal="buy") is None

    def test_missing_probabilities_returns_none(self):
        assert clef_flash.parse_direction_answer(
            {"direction": {"choice": "buy"}}, signal="buy") is None

    def test_none_answers_returns_none(self):
        assert clef_flash.parse_direction_answer(None, signal="buy") is None

    def test_missing_wait_defaults_zero(self):
        d = clef_flash.parse_direction_answer(
            {"direction": {"probabilities": {"buy": 0.9, "sell": 0.1}}}, signal="buy")
        assert d is not None
        assert d.p_clef_wait == 0.0

    def test_model_slug_carried(self):
        d = clef_flash.parse_direction_answer(
            self._answers(0.5, 0.3, 0.2), signal="buy", model="@cf/cloudflare/clef-flash")
        assert d.model == "@cf/cloudflare/clef-flash"


# ── decide / decide_direction (fail-open + mocked I/O) ─────────────────

def _mock_client(resp=None, post_side_effect=None):
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _ok_response(payload):
    resp = MagicMock()
    resp.is_success = True
    resp.json = MagicMock(return_value=payload)
    return resp


@pytest.mark.asyncio
async def test_decide_no_config_returns_none(monkeypatch):
    monkeypatch.delenv("CF_DECISION_URL", raising=False)
    monkeypatch.delenv("CF_EMBEDDINGS_URL", raising=False)
    assert await clef_flash.decide({"symbol": "X"}, {}) is None


@pytest.mark.asyncio
async def test_decide_parses_answers(monkeypatch):
    monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
    resp = _ok_response({"answers": {"direction": {"probabilities": {
        "buy": 0.8, "sell": 0.1, "wait": 0.1}}}, "model": "@cf/cloudflare/clef-flash"})
    with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
        answers = await clef_flash.decide({"symbol": "X"}, {})
    assert answers["direction"]["probabilities"]["buy"] == 0.8


@pytest.mark.asyncio
async def test_decide_http_error_returns_none(monkeypatch):
    monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 500
    resp.text = "boom"
    with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
        assert await clef_flash.decide({"symbol": "X"}, {}) is None


@pytest.mark.asyncio
async def test_decide_exception_returns_none(monkeypatch):
    monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
    with patch("httpx.AsyncClient",
               return_value=_mock_client(post_side_effect=RuntimeError("net"))):
        assert await clef_flash.decide({"symbol": "X"}, {}) is None


@pytest.mark.asyncio
async def test_decide_direction_end_to_end(monkeypatch):
    monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
    resp = _ok_response({"answers": {"direction": {"choice": "buy", "probabilities": {
        "buy": 0.853, "sell": 0.040, "wait": 0.107}}}})
    with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
        d = await clef_flash.decide_direction(symbol="XAUUSD", signal="buy")
    assert d is not None
    assert d.p_clef_dir == pytest.approx(0.853)
    assert d.p_clef_wait == pytest.approx(0.107)


# ── classify_choice (the categorizer primitive) ───────────────────────

class TestClassifyChoice:
    @pytest.mark.asyncio
    async def test_returns_distribution(self, monkeypatch):
        monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
        resp = _ok_response({"answers": {"answer": {"choice": "no_change", "probabilities": {
            "major_pivot": 0.05, "incremental": 0.15, "no_change": 0.80}}}})
        with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
            probs = await clef_flash.classify_choice(
                instructions="classify", options={"a": "x", "b": "y"}, state={"k": 1})
        assert probs == {"major_pivot": 0.05, "incremental": 0.15, "no_change": 0.80}

    @pytest.mark.asyncio
    async def test_empty_options_returns_none(self):
        assert await clef_flash.classify_choice(
            instructions="x", options={}, state={}) is None

    @pytest.mark.asyncio
    async def test_no_config_returns_none(self, monkeypatch):
        monkeypatch.delenv("CF_DECISION_URL", raising=False)
        monkeypatch.delenv("CF_EMBEDDINGS_URL", raising=False)
        assert await clef_flash.classify_choice(
            instructions="x", options={"a": "1"}, state={}) is None

    @pytest.mark.asyncio
    async def test_missing_answer_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
        resp = _ok_response({"answers": {"other": {"probabilities": {"a": 1.0}}}})
        with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
            assert await clef_flash.classify_choice(
                instructions="x", options={"a": "1"}, state={}) is None

    @pytest.mark.asyncio
    async def test_non_numeric_probs_dropped(self, monkeypatch):
        monkeypatch.setenv("CF_EMBEDDINGS_URL", "https://worker.example.dev")
        resp = _ok_response({"answers": {"answer": {"probabilities": {
            "a": 0.7, "b": "bad"}}}})
        with patch("httpx.AsyncClient", return_value=_mock_client(resp=resp)):
            probs = await clef_flash.classify_choice(
                instructions="x", options={"a": "1", "b": "2"}, state={})
        assert probs == {"a": 0.7}


# ── kairos_pivot cutover (flag-gated fallback) ────────────────────────

@pytest.mark.asyncio
async def test_pivot_uses_clef_when_live(monkeypatch):
    from regime_platform.sweep_orchestrator import kairos_pivot as kp
    from regime_platform.core import feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "live"})

    async def _fake_clef(event, *, structure_note=None):
        return {"label": "incremental", "axis": "none", "intensity": 0.62,
                "surprise": 0.0, "summary": "", "product_type": "none",
                "as_of": event.get("date"), "event_title": event.get("title")}

    monkeypatch.setattr(kp, "_label_event_clef", _fake_clef)
    v = await kp.label_event({"title": "CPI", "date": "2026-10-04", "country": "US"})
    assert v["label"] == "incremental"


@pytest.mark.asyncio
async def test_pivot_falls_back_to_chat_when_clef_none(monkeypatch):
    from regime_platform.sweep_orchestrator import kairos_pivot as kp
    from regime_platform.core import feature_flags as ff
    from regime_platform.services.self_learning import llm_client
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "live"})

    async def _clef_none(event, *, structure_note=None):
        return None

    async def _chat(system, prompt, max_tokens=512):
        return ({"label": "no_change", "axis": "none", "intensity": 0.1,
                 "surprise": 0.0, "summary": "routine"}, "nvidia_nim")

    monkeypatch.setattr(kp, "_label_event_clef", _clef_none)
    monkeypatch.setattr(llm_client, "call_llm_with_provider", _chat)
    v = await kp.label_event({"title": "CPI", "date": "2026-10-04"})
    assert v["label"] == "no_change"


@pytest.mark.asyncio
async def test_pivot_shadow_does_not_call_clef(monkeypatch):
    from regime_platform.sweep_orchestrator import kairos_pivot as kp
    from regime_platform.core import feature_flags as ff
    from regime_platform.services.self_learning import llm_client
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "shadow"})

    called = {"clef": False}

    async def _clef(event, *, structure_note=None):
        called["clef"] = True
        return None

    async def _chat(system, prompt, max_tokens=512):
        return ({"label": "no_change", "axis": "none", "intensity": 0.1,
                 "surprise": 0.0, "summary": "x"}, "nvidia_nim")

    monkeypatch.setattr(kp, "_label_event_clef", _clef)
    monkeypatch.setattr(llm_client, "call_llm_with_provider", _chat)
    v = await kp.label_event({"title": "CPI", "date": "2026-10-04"})
    assert v["label"] == "no_change"
    assert called["clef"] is False  # shadow must not touch the classifier path


# ── flag ──────────────────────────────────────────────────────────────

def test_clef_flash_mode_default(monkeypatch):
    from regime_platform.core import feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {})
    assert ff.get_clef_flash_mode() == "shadow"


def test_clef_flash_mode_unknown_falls_back_to_shadow(monkeypatch):
    from regime_platform.core import feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "banana"})
    assert ff.get_clef_flash_mode() == "shadow"


def test_clef_flash_mode_off(monkeypatch):
    from regime_platform.core import feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "off"})
    assert ff.get_clef_flash_mode() == "off"


# ── payload + whitelist wiring ────────────────────────────────────────

def test_whitelist_has_p_clef_columns():
    from regime_platform.sweep_orchestrator.supabase_client import NT_SIGNAL_SIM_COLUMNS
    assert {"p_clef_dir", "p_clef_wait", "p_clef_symbol"} <= NT_SIGNAL_SIM_COLUMNS


def test_payload_stamps_p_clef():
    from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
    payload = _build_signal_sim_payload(
        symbol="XAUUSD", venue="OANDA", ts="2026-10-04T00:00:00Z", signal="buy",
        entry_price=2650.12, stop_loss=2630.44, take_profit=2698.10,
        brick_size=0.5, sl_bricks=3, tp_bricks=6, p_win=0.61, ev=0.3,
        regime="med_high_vol_bull", regime_conf=0.81, source="sweep-light",
        qualified=True, p_clef_dir=0.853, p_clef_wait=0.107,
        p_clef_symbol="@cf/cloudflare/clef-flash",
    )
    assert payload["p_clef_dir"] == 0.853
    assert payload["p_clef_wait"] == 0.107
    assert payload["p_clef_symbol"] == "@cf/cloudflare/clef-flash"


def test_payload_omits_p_clef_when_none():
    from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
    payload = _build_signal_sim_payload(
        symbol="XAUUSD", venue="OANDA", ts="2026-10-04T00:00:00Z", signal="buy",
        entry_price=2650.12, stop_loss=2630.44, take_profit=2698.10,
        brick_size=0.5, sl_bricks=3, tp_bricks=6, p_win=0.61, ev=0.3,
        regime="med_high_vol_bull", regime_conf=0.81, source="sweep-light",
        qualified=True,
    )
    assert "p_clef_dir" not in payload
    assert "p_clef_symbol" not in payload


# ── orchestrator stamp guards ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_stamp_non_light_source_returns_none():
    from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
    obj = object.__new__(SweepOrchestrator)
    res = await obj._clef_flash_stamp(
        symbol="XAUUSD", signal="buy", regime_label="x", regime_conf=0.8,
        source="sweep-heavy",
    )
    assert res == (None, None, None)


@pytest.mark.asyncio
async def test_stamp_off_mode_returns_none(monkeypatch):
    from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
    from regime_platform.core import feature_flags as ff
    monkeypatch.setattr(ff, "_load", lambda: {"clef_flash_mode": "off"})
    obj = object.__new__(SweepOrchestrator)
    res = await obj._clef_flash_stamp(
        symbol="XAUUSD", signal="buy", regime_label="x", regime_conf=0.8,
        source="sweep-light",
    )
    assert res == (None, None, None)
