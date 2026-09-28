"""
LLM Forecasting / classify API (Studio-local, same pattern as TimesFM server.py).

Exposes a small instruct model (default LiquidAI/LFM2.5-1.2B-Instruct) as
OpenAI-compatible chat completions for macro_forecast_agent / kairos_pivot.

Run on Studio (mirror TimesFM :8002):
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe llm_server.py

Secrets / env:
  LOCAL_LLM_MODEL     HF id or local path (default LiquidAI/LFM2.5-1.2B-Instruct)
  LOCAL_LLM_PORT      default 8004 (8003 is sweep orchestrator)
  LOCAL_LLM_API_KEY   optional shared secret (X-API-Key)
  HF_HOME             forced to .cache/huggingface like server.py

Clients set:
  LOCAL_LLM_BASE_URL=http://127.0.0.1:8004
  LOCAL_LLM_MODEL=<same id or "local">
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

# HF cache BEFORE transformers import (same as server.py)
_CACHE_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "huggingface"))
os.environ["HF_HOME"] = _CACHE_DIR
os.environ["HF_HUB_CACHE"] = os.path.join(_CACHE_DIR, "hub")
os.makedirs(os.environ["HF_HUB_CACHE"], exist_ok=True)

from fastapi import Depends, FastAPI, HTTPException, Request, Security  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.security import APIKeyHeader  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
import uvicorn  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("llm-server")

MODEL_ID = os.getenv("LOCAL_LLM_MODEL", "LiquidAI/LFM2.5-1.2B-Instruct")
PORT = int(os.getenv("LOCAL_LLM_PORT", "8004"))
DEVICE = os.getenv("LOCAL_LLM_DEVICE", "auto")

state: dict = {"pipe": None, "tokenizer": None}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _valid_keys() -> frozenset[str]:
    return frozenset(k.strip() for k in os.getenv("LOCAL_LLM_API_KEY", "").split(",") if k.strip())


async def verify_api_key(api_key: str | None = Security(api_key_header)) -> None:
    keys = _valid_keys()
    if not keys:
        return
    if not api_key or api_key not in keys:
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Loading local LLM: %s", MODEL_ID)
    t0 = time.time()
    from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

    tok = AutoTokenizer.from_pretrained(MODEL_ID)
    mdl = AutoModelForCausalLM.from_pretrained(MODEL_ID, device_map=DEVICE)
    state["tokenizer"] = tok
    state["pipe"] = pipeline(
        "text-generation",
        model=mdl,
        tokenizer=tok,
        max_new_tokens=1024,
        do_sample=False,
    )
    log.info("Model loaded in %.1fs", time.time() - t0)
    yield
    state["pipe"] = None


app = FastAPI(title="Local LLM API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "local"
    messages: list[ChatMessage]
    max_tokens: int = Field(default=512, ge=1, le=2048)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if state["pipe"] is not None else "loading",
        "model": MODEL_ID,
        "port": PORT,
    }


@app.post("/v1/chat/completions")
async def chat(req: ChatRequest, _auth: None = Depends(verify_api_key)):
    if state["pipe"] is None:
        raise HTTPException(status_code=503, detail="Model still loading")
    try:
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        text = state["tokenizer"].apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        out = state["pipe"](
            text,
            max_new_tokens=req.max_tokens,
            do_sample=req.temperature > 0,
            temperature=req.temperature if req.temperature > 0 else None,
            return_full_text=False,
        )
        content = (out[0]["generated_text"] if out else "").strip()
        return {
            "id": "local-llm",
            "object": "chat.completion",
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    except Exception as exc:
        log.exception("chat failed")
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
