"""
LLM Forecasting / classify API (Studio-local, same pattern as TimesFM server.py).

Exposes a small instruct model (default XHToken/Spark-X2.5-1.7B-INT8) as
OpenAI-compatible chat completions for macro_forecast_agent / kairos_pivot.

Run on Studio (mirror TimesFM :8002):
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe llm_server.py

Secrets / env:
  LOCAL_LLM_MODEL     HF id or local path
                      (default XHToken/Spark-X2.5-1.7B-INT8)
  LOCAL_LLM_PORT      default 8004 (8003 is sweep orchestrator)
  LOCAL_LLM_API_KEY   optional shared secret (X-API-Key)
  HF_HOME             Honor Studio hf_cache (same as TimesFM). Only falls
                      back to <repo>/.cache/huggingface when unset.

Clients set:
  LOCAL_LLM_BASE_URL=http://127.0.0.1:8004
  LOCAL_LLM_MODEL=<same id or "local">
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

# HF cache BEFORE transformers import. Studio sets
# HF_HOME=/teamspace/studios/this_studio/hf_cache (TimesFM lives there).
# Do not clobber that — only default when unset (local/dev).
if not os.environ.get("HF_HOME"):
    _CACHE_DIR = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "huggingface")
    )
    os.environ["HF_HOME"] = _CACHE_DIR
    os.environ["HF_HUB_CACHE"] = os.path.join(_CACHE_DIR, "hub")
    os.makedirs(os.environ["HF_HUB_CACHE"], exist_ok=True)
else:
    os.environ.setdefault(
        "HF_HUB_CACHE", os.path.join(os.environ["HF_HOME"], "hub")
    )

import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Security  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.security import APIKeyHeader  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("llm-server")

# Spark-X2.5 INT8: official 8-bit (JSON/schema-safe) — trust_remote_code for
# custom Spark modeling. Set LOCAL_LLM_MODEL to override (e.g. LFM or Q4 GGUF).
MODEL_ID = os.getenv("LOCAL_LLM_MODEL", "XHToken/Spark-X2.5-1.7B-INT8")
PORT = int(os.getenv("LOCAL_LLM_PORT", "8004"))
DEVICE = os.getenv("LOCAL_LLM_DEVICE", "auto")
TRUST_REMOTE_CODE = os.getenv("LOCAL_LLM_TRUST_REMOTE_CODE", "1") == "1"


def _resolve_model_path(model_id: str) -> str:
    """Prefer a local snapshot when offline (2026-09-30 llm.log crash).

    HF id + ``HF_HUB_OFFLINE=1`` fails even when the weights sit in
    ``HF_HOME/hub/models--org--name/snapshots/<rev>`` — transformers still
    wants hub metadata. If a snapshot exists, load from that path.

    Also: if model_id looks like an HF hub id (contains ``/`` but isn't a
    real path), fall back to the materialised local tree at
    ``spark-int8-local`` — the Studio env var ``LOCAL_LLM_MODEL`` is set to
    the hub id by default, which breaks offline loading.
    """
    if os.path.isdir(model_id):
        return model_id
    # Fall back to local materialised tree if the HF hub id fails
    hub = os.path.join(
        os.environ.get("HF_HUB_CACHE")
        or os.path.join(os.environ.get("HF_HOME", ""), "hub")
    )
    if not hub or not os.path.isdir(hub):
        # Try the canonical spark-int8-local path
        local_path = "/teamspace/studios/this_studio/hf_cache/spark-int8-local"
        if os.path.isdir(local_path):
            log.info("Fell back to local spark-int8 tree for %s", model_id)
            return local_path
        return model_id
    slug = model_id.replace("/", "--")
    root = os.path.join(hub, f"models--{slug}", "snapshots")
    if not os.path.isdir(root):
        return model_id
    revs = sorted(os.listdir(root), reverse=True)
    for rev in revs:
        cand = os.path.join(root, rev)
        if os.path.isfile(os.path.join(cand, "config.json")):
            log.info("Resolved MODEL_ID %s → %s", model_id, cand)
            return cand
    return model_id


MODEL_ID = _resolve_model_path(MODEL_ID)

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
    log.info("Loading local LLM: %s (HF_HOME=%s)", MODEL_ID, os.environ.get("HF_HOME"))
    t0 = time.time()
    from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

    tok = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=TRUST_REMOTE_CODE)
    load_kwargs: dict = {"trust_remote_code": TRUST_REMOTE_CODE}
    # device_map requires `accelerate` — fall back to default placement
    # (2026-09-30 Studio: transformers raises ValueError without it).
    try:
        import accelerate  # noqa: F401

        if DEVICE:
            load_kwargs["device_map"] = DEVICE
    except ImportError:
        log.warning("accelerate not installed — loading without device_map")
    mdl = AutoModelForCausalLM.from_pretrained(MODEL_ID, **load_kwargs)
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
