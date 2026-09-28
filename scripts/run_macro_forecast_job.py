"""
run_macro_forecast_job.py — Studio-schedulable macro classify job.

Lightning Studio → Deploy → schedule this script (see
https://lightning.ai/docs/platform/build/agents.md). This is a **script**
with LLM calls, not an autonomous trading agent.

  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/run_macro_forecast_job.py

Env (Studio secrets / .env.local):
  LOCAL_LLM_MODEL | LOCAL_LLM_BASE_URL | OPENROUTER_API_KEY | NVIDIA_NIM_API_KEY
  SUPABASE_URL, SUPABASE_KEY
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.local")

from regime_platform.services.self_learning.macro_forecast_agent import (  # noqa: E402
    run_macro_forecast,
)


async def main() -> None:
    # Prefer local/reliable providers; OpenRouter often 402s without credits.
    if os.getenv("P2_SKIP_OPENROUTER", "1") == "1":
        os.environ["OPENROUTER_API_KEY"] = ""
    summary = await run_macro_forecast()
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
