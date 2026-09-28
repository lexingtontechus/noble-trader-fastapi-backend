"""
local_llm_smoke.py — verify a small local instruct model for classify/JSON.

Preferred for Studio (reliability over cloud APIs):
  LOCAL_LLM_BASE_URL=http://127.0.0.1:8080   # llama.cpp / vLLM OpenAI-compatible
  or
  LOCAL_LLM_MODEL=Qwen/Qwen2.5-3B-Instruct   # transformers load

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/local_llm_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")


async def main() -> None:
    from regime_platform.services.self_learning.llm_client import (
        _local_base_url,
        _local_model_id,
        call_llm_with_provider,
    )

    print("LOCAL_LLM_BASE_URL=", _local_base_url() or "(unset)")
    print("LOCAL_LLM_MODEL=", _local_model_id() or "(unset)")
    if not _local_base_url() and not _local_model_id():
        print("Set one of LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL (e.g. Qwen/Qwen2.5-3B-Instruct, LFM-2-1.2B).")
        return

    sys_prompt = "You extract JSON only. No markdown."
    user = (
        'Return {"events":[{"label":"no_change","axis":"none",'
        '"intensity":0.0,"surprise":0.0,"summary":"ok"}]}'
    )
    result = await call_llm_with_provider(sys_prompt, user, max_tokens=200)
    if result is None:
        print("FAIL: no parsed JSON from local provider")
        return
    parsed, provider = result
    print("provider=", provider)
    print("parsed=", parsed)


if __name__ == "__main__":
    asyncio.run(main())
