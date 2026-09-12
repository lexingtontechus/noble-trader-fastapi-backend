"""Tests for the shared OpenRouter/NVIDIA NIM LLM client
(regime_platform/services/self_learning/llm_client.py), worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md.

All network I/O is mocked. Covers: JSON extraction, the OpenRouter
primary -> NVIDIA NIM fallback ordering (on HTTP failure, on network
exception, and on a non-JSON response), and the "neither key set"
no-op path -- mirrors nobletradingapp's generateCommentary/
callChatCompletions fallback shape, just in Python.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regime_platform.services.self_learning import llm_client


# ── extract_json ──────────────────────────────────────────────────────

class TestExtractJson:
    def test_plain_json(self):
        text = '{"a": 1, "b": null}'
        assert llm_client.extract_json(text) == {"a": 1, "b": None}

    def test_json_with_surrounding_prose(self):
        text = 'Sure, here you go:\n{"a": 1}\nHope that helps!'
        assert llm_client.extract_json(text) == {"a": 1}

    def test_no_json_returns_none(self):
        assert llm_client.extract_json("no json here at all") is None

    def test_malformed_json_returns_none(self):
        assert llm_client.extract_json("{not: valid json}") is None


def _mock_client(resp=None, post_side_effect=None):
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _ok_response(text: str):
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


def _fail_response(status=500, text="server error"):
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = status
    resp.text = text
    return resp


# ── call_llm ──────────────────────────────────────────────────────────

class TestCallLlm:
    @pytest.mark.asyncio
    async def test_neither_key_set_returns_none_without_network_call(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        llm_client._warned_no_provider = False
        with patch("httpx.AsyncClient") as mock_client_cls:
            result = await llm_client.call_llm("system", "user")
        assert result is None
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_openrouter_success_returns_parsed_json(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        mock_client = _mock_client(resp=_ok_response('{"narrative": "ok"}'))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await llm_client.call_llm("system", "user")
        assert result == {"narrative": "ok"}

    @pytest.mark.asyncio
    async def test_openrouter_http_failure_falls_back_to_nvidia_nim(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

        or_client = _mock_client(resp=_fail_response())
        nim_client = _mock_client(resp=_ok_response('{"narrative": "from nim"}'))
        with patch("httpx.AsyncClient", side_effect=[or_client, nim_client]):
            result = await llm_client.call_llm("system", "user")
        assert result == {"narrative": "from nim"}

    @pytest.mark.asyncio
    async def test_openrouter_network_exception_falls_back_to_nvidia_nim(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

        or_client = _mock_client(post_side_effect=RuntimeError("boom"))
        nim_client = _mock_client(resp=_ok_response('{"narrative": "from nim"}'))
        with patch("httpx.AsyncClient", side_effect=[or_client, nim_client]):
            result = await llm_client.call_llm("system", "user")
        assert result == {"narrative": "from nim"}

    @pytest.mark.asyncio
    async def test_openrouter_non_json_response_falls_back_to_nvidia_nim(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

        or_client = _mock_client(resp=_ok_response("not json at all"))
        nim_client = _mock_client(resp=_ok_response('{"narrative": "from nim"}'))
        with patch("httpx.AsyncClient", side_effect=[or_client, nim_client]):
            result = await llm_client.call_llm("system", "user")
        assert result == {"narrative": "from nim"}

    @pytest.mark.asyncio
    async def test_both_providers_fail_returns_none(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

        or_client = _mock_client(resp=_fail_response())
        nim_client = _mock_client(resp=_fail_response())
        with patch("httpx.AsyncClient", side_effect=[or_client, nim_client]):
            result = await llm_client.call_llm("system", "user")
        assert result is None

    @pytest.mark.asyncio
    async def test_only_nvidia_nim_key_set_skips_openrouter(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")

        nim_client = _mock_client(resp=_ok_response('{"narrative": "from nim"}'))
        with patch("httpx.AsyncClient", return_value=nim_client) as mock_client_cls:
            result = await llm_client.call_llm("system", "user")
        assert result == {"narrative": "from nim"}
        assert mock_client_cls.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_response_content_is_treated_as_failure(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        mock_client = _mock_client(resp=_ok_response(""))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await llm_client.call_llm("system", "user")
        assert result is None


# ── call_llm_with_provider ────────────────────────────────────────────

class TestCallLlmWithProvider:
    @pytest.mark.asyncio
    async def test_openrouter_success_reports_openrouter_provider(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
        mock_client = _mock_client(resp=_ok_response('{"a": 1}'))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await llm_client.call_llm_with_provider("system", "user")
        assert result == ({"a": 1}, "openrouter")

    @pytest.mark.asyncio
    async def test_fallback_reports_nvidia_nim_provider(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-test-key")
        or_client = _mock_client(resp=_fail_response())
        nim_client = _mock_client(resp=_ok_response('{"a": 2}'))
        with patch("httpx.AsyncClient", side_effect=[or_client, nim_client]):
            result = await llm_client.call_llm_with_provider("system", "user")
        assert result == ({"a": 2}, "nvidia_nim")
