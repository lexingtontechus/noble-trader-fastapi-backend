"""
Shared LLM call helper for the self-learning services (Phase 3 MVP
triage, the macro-forecast agent) — worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md.

OpenRouter primary, NVIDIA NIM fallback, both via the same
OpenAI-compatible ``/chat/completions`` schema — mirrors the pattern
already live in ``nobletradingapp``'s
``supabase/functions/eod-report-generate/index.ts``
(``generateCommentary``/``callChatCompletions``), reused here rather
than re-decided, per explicit instruction NOT to use Anthropic. No SDK
dependency — raw ``httpx``, same style as ``TIMESFM_API_KEYS`` calls in
``orchestrator.py``.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("regime.self_learning.llm_client")

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

# Free-tier model IDs shift over time on both platforms — these match
# what nobletradingapp verified working directly against both APIs on
# 2026-09-05. Re-verify (or override via env var) if these start
# failing.
_OPENROUTER_MODEL_DEFAULT = "minimax/minimax-m3:free"
_NVIDIA_NIM_MODEL_DEFAULT = "meta/llama-3.2-11b-vision-instruct"

_REQUEST_TIMEOUT = 30.0

_warned_no_provider = False


def _openrouter_api_key() -> str:
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def _nvidia_nim_api_key() -> str:
    return os.getenv("NVIDIA_NIM_API_KEY", "").strip()


def _openrouter_model() -> str:
    return os.getenv("OPENROUTER_MODEL", "").strip() or _OPENROUTER_MODEL_DEFAULT


def _nvidia_nim_model() -> str:
    return os.getenv("NVIDIA_NIM_MODEL", "").strip() or _NVIDIA_NIM_MODEL_DEFAULT


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Lenient JSON extraction: first ``{`` to last ``}``, tolerating a
    model that wraps its answer in prose or markdown fencing despite
    being asked not to."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


async def _call_chat_completions(
    base_url: str, api_key: str, model: str, system_prompt: str, user_prompt: str, max_tokens: int,
) -> Optional[str]:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
        )
        if not resp.is_success:
            raise RuntimeError(f"{base_url} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not text or not text.strip():
            raise RuntimeError(f"{base_url} returned an empty response")
        return text.strip()


async def call_llm_with_provider(
    system_prompt: str, user_prompt: str, max_tokens: int = 512,
) -> Optional[tuple[dict[str, Any], str]]:
    """OpenRouter primary, NVIDIA NIM fallback. Returns ``(parsed_json,
    provider)`` from whichever provider succeeds first — ``provider``
    is ``"openrouter"`` or ``"nvidia_nim"`` — or ``None`` if both fail,
    both are unset, or neither response is valid JSON. Never raises —
    best-effort, matches every other self-learning I/O function in
    this codebase."""
    global _warned_no_provider
    openrouter_key = _openrouter_api_key()
    nim_key = _nvidia_nim_api_key()

    if not openrouter_key and not nim_key:
        if not _warned_no_provider:
            logger.warning(
                "Neither OPENROUTER_API_KEY nor NVIDIA_NIM_API_KEY is set — "
                "LLM-assisted self-learning features will no-op until one is "
                "added as a Studio secret."
            )
            _warned_no_provider = True
        return None

    if openrouter_key:
        try:
            text = await _call_chat_completions(
                _OPENROUTER_BASE_URL, openrouter_key, _openrouter_model(),
                system_prompt, user_prompt, max_tokens,
            )
            parsed = extract_json(text)
            if parsed is not None:
                return parsed, "openrouter"
            logger.warning("llm_client: OpenRouter response wasn't valid JSON, falling back to NVIDIA NIM")
        except Exception as exc:
            logger.warning("llm_client: OpenRouter call failed, falling back to NVIDIA NIM: %s", exc)

    if nim_key:
        try:
            text = await _call_chat_completions(
                _NVIDIA_NIM_BASE_URL, nim_key, _nvidia_nim_model(),
                system_prompt, user_prompt, max_tokens,
            )
            parsed = extract_json(text)
            if parsed is not None:
                return parsed, "nvidia_nim"
            logger.warning("llm_client: NVIDIA NIM response wasn't valid JSON either — giving up")
        except Exception as exc:
            logger.warning("llm_client: NVIDIA NIM call failed: %s", exc)

    return None


async def call_llm(
    system_prompt: str, user_prompt: str, max_tokens: int = 512,
) -> Optional[dict[str, Any]]:
    """Same as ``call_llm_with_provider`` but for callers that don't
    care which provider answered — returns just the parsed JSON."""
    result = await call_llm_with_provider(system_prompt, user_prompt, max_tokens)
    return result[0] if result is not None else None
