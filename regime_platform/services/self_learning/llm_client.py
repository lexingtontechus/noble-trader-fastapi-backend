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

# 2026-09-12: minimax/minimax-m3:free (nobletradingapp's original
# default, matched here 2026-09-11) was retired -- OpenRouter now 404s
# with "This model is unavailable for free... use minimax/minimax-m3"
# (the paid slug). Switched to "openrouter/auto", OpenRouter's own
# meta-router -- it picks a suitable underlying model per request and
# respects the account-level default/fallback model configured in the
# OpenRouter dashboard (set to their free-models router as of
# 2026-09-12), so this default shouldn't go stale the same way a
# specific free-tier model ID does. NVIDIA NIM's default is unaffected
# by this issue -- still verified working.
_OPENROUTER_MODEL_DEFAULT = "openrouter/auto"
_NVIDIA_NIM_MODEL_DEFAULT = "meta/llama-3.2-11b-vision-instruct"

_REQUEST_TIMEOUT = 90.0

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


def _local_model_id() -> str:
    return os.getenv("LOCAL_LLM_MODEL", "").strip()


def _local_base_url() -> str:
    """OpenAI-compatible server root, e.g. http://127.0.0.1:8080 — NOT a bare host."""
    raw = os.getenv("LOCAL_LLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    if raw in ("localhost", "127.0.0.1"):
        logger.warning(
            "LOCAL_LLM_BASE_URL=%r is a host only — use e.g. "
            "http://127.0.0.1:8080 (llama.cpp/vLLM). Ignoring bare host.",
            raw,
        )
        return ""
    if not raw.startswith("http://") and not raw.startswith("https://"):
        logger.warning("LOCAL_LLM_BASE_URL=%r missing http(s):// — ignoring.", raw)
        return ""
    return raw


_local_pipe = None


def _get_local_pipe():
    """Lazy transformers pipeline for a small local instruct model."""
    global _local_pipe
    if _local_pipe is not None:
        return _local_pipe
    model_id = _local_model_id()
    if not model_id:
        return None
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

        tok = AutoTokenizer.from_pretrained(model_id)
        mdl = AutoModelForCausalLM.from_pretrained(model_id, device_map="auto")
        _local_pipe = pipeline(
            "text-generation",
            model=mdl,
            tokenizer=tok,
            max_new_tokens=512,
            do_sample=False,
        )
        logger.info("llm_client: local model ready %s", model_id)
        return _local_pipe
    except Exception as exc:
        logger.warning("llm_client: local model load failed: %s: %r", type(exc).__name__, exc)
        return None


async def _call_local(system_prompt: str, user_prompt: str, max_tokens: int) -> Optional[str]:
    """Local path: OpenAI-compatible LOCAL_LLM_BASE_URL, else transformers."""
    import asyncio

    base = _local_base_url()
    if base:
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{base}/v1/chat/completions",
                    headers={"Content-Type": "application/json"},
                    json={
                        "model": _local_model_id() or "local",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": max_tokens,
                        "temperature": 0.1,
                    },
                )
                if not resp.is_success:
                    raise RuntimeError(f"local {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                return ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        except Exception as exc:
            logger.warning("llm_client: local HTTP failed: %s: %r", type(exc).__name__, exc)
            return None

    pipe = _get_local_pipe()
    if pipe is None:
        return None

    def _gen() -> str:
        out = pipe(
            f"{system_prompt}\n\n{user_prompt}",
            max_new_tokens=max_tokens,
            return_full_text=False,
        )
        return (out[0]["generated_text"] if out else "").strip()

    return await asyncio.to_thread(_gen)


async def call_llm_with_provider(
    system_prompt: str, user_prompt: str, max_tokens: int = 512,
) -> Optional[tuple[dict[str, Any], str]]:
    """Local first (if configured), then OpenRouter, then NVIDIA NIM.

    Returns ``(parsed_json, provider)`` — provider is ``"local"``,
    ``"openrouter"``, or ``"nvidia_nim"``. Never raises.
    """
    global _warned_no_provider

    if _local_model_id() or _local_base_url():
        try:
            text = await _call_local(system_prompt, user_prompt, max_tokens)
            parsed = extract_json(text or "")
            if parsed is not None:
                return parsed, "local"
            logger.warning("llm_client: local response wasn't valid JSON")
        except Exception as exc:
            logger.warning("llm_client: local call failed: %s: %r", type(exc).__name__, exc)

    openrouter_key = _openrouter_api_key()
    nim_key = _nvidia_nim_api_key()

    if not openrouter_key and not nim_key and not (_local_model_id() or _local_base_url()):
        if not _warned_no_provider:
            logger.warning(
                "No LLM provider configured (LOCAL_LLM_MODEL / LOCAL_LLM_BASE_URL, "
                "OPENROUTER_API_KEY, or NVIDIA_NIM_API_KEY) — "
                "LLM-assisted self-learning features will no-op."
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
            logger.warning(
                "llm_client: OpenRouter call failed, falling back to NVIDIA NIM: %s: %r",
                type(exc).__name__, exc,
            )

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
            logger.warning("llm_client: NVIDIA NIM call failed: %s: %r", type(exc).__name__, exc)

    return None


async def call_llm(
    system_prompt: str, user_prompt: str, max_tokens: int = 512,
) -> Optional[dict[str, Any]]:
    """Same as ``call_llm_with_provider`` but for callers that don't
    care which provider answered — returns just the parsed JSON."""
    result = await call_llm_with_provider(system_prompt, user_prompt, max_tokens)
    return result[0] if result is not None else None
