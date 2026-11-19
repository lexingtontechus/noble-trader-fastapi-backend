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
  LOCAL_LLM_API_KEY   shared secret (X-API-Key). When unset, the server
                      generates a random dev key, persists it to
                      .cache/local_llm_api_key (0600, NEVER logged) and
                      requires it — auth is fail-closed (scan finding 5).
                      Same-host clients (llm_client) read the file.
  LOCAL_LLM_HOST      bind address, default 127.0.0.1 (loopback). Set
                      0.0.0.0 only when external access is intended AND a
                      real LOCAL_LLM_API_KEY is provisioned.
  LOCAL_LLM_TRUST_REMOTE_CODE  default 0 (fail-safe). Set 1 explicitly to
                      execute custom modeling code (Spark-X2.5 needs it;
                      start.sh / on_start.sh export it).
  HF_HOME             Honor Studio hf_cache (same as TimesFM). Only falls
                      back to <repo>/.cache/huggingface when unset.

Clients set:
  LOCAL_LLM_BASE_URL=http://127.0.0.1:8004
  LOCAL_LLM_MODEL=<same id or "local">
"""
from __future__ import annotations

import logging
import os
import secrets
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
HOST = os.getenv("LOCAL_LLM_HOST", "127.0.0.1")
DEVICE = os.getenv("LOCAL_LLM_DEVICE", "auto")
# Fail-safe default (scan finding 2): remote modeling code executes only on
# explicit opt-in. The shipped launchers export LOCAL_LLM_TRUST_REMOTE_CODE=1
# for the custom Spark architecture; a bare run no longer executes repo code.
TRUST_REMOTE_CODE = os.getenv("LOCAL_LLM_TRUST_REMOTE_CODE", "0") == "1"
if TRUST_REMOTE_CODE:
    log.warning(
        "LOCAL_LLM_TRUST_REMOTE_CODE=1 — third-party modeling code will execute "
        "at load time. Vendor/hash-pin the model tree and audit it."
    )


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

# Dev key lives in a protected local file, never in the log stream (the
# TimesFM-style "log the generated key" pattern leaked a valid credential —
# scan finding 10). Stable across restarts so same-host callers can read it.
_DEV_KEY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache", "local_llm_api_key"
)


def _load_or_create_dev_key() -> str:
    try:
        with open(_DEV_KEY_FILE, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(_DEV_KEY_FILE), exist_ok=True)
    with open(_DEV_KEY_FILE, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    try:
        os.chmod(_DEV_KEY_FILE, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX modes
    return key


def _valid_keys() -> frozenset[str]:
    env_keys = frozenset(
        k.strip() for k in os.getenv("LOCAL_LLM_API_KEY", "").split(",") if k.strip()
    )
    if env_keys:
        return env_keys
    # No configured key: still require authentication — a random dev key
    # persisted to .cache/local_llm_api_key. Fail-closed, not fail-open.
    return frozenset({_load_or_create_dev_key()})


async def verify_api_key(api_key: str | None = Security(api_key_header)) -> None:
    keys = _valid_keys()
    if not keys:
        raise HTTPException(status_code=503, detail="LLM server auth not configured")
    if not api_key or not any(secrets.compare_digest(api_key, k) for k in keys):
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Loading local LLM: %s (HF_HOME=%s)", MODEL_ID, os.environ.get("HF_HOME"))
    t0 = time.time()
    from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
    # Compatibility shim: transformers 5.x renamed 'input_embeds' → 'inputs_embeds'
    # and removed 'cache_position' from create_causal_mask(). The Spark-X2.5 model's
    # custom modeling_spark.py (trust_remote_code) uses the old API. Must patch
    # before model import so the model picks up the patched reference.
    try:
        import transformers.masking_utils as _masking

        _orig_cc = _masking.create_causal_mask
        _orig_sc = _masking.create_sliding_window_causal_mask

        def _compat_cc(**kwargs):
            if "input_embeds" in kwargs:
                kwargs["inputs_embeds"] = kwargs.pop("input_embeds")
            kwargs.pop("cache_position", None)
            return _orig_cc(**kwargs)

        def _compat_sc(**kwargs):
            if "input_embeds" in kwargs:
                kwargs["inputs_embeds"] = kwargs.pop("input_embeds")
            kwargs.pop("cache_position", None)
            return _orig_sc(**kwargs)

        _masking.create_causal_mask = _compat_cc
        _masking.create_sliding_window_causal_mask = _compat_sc
        log.info("Applied transformers 5.x compat shim for Spark-X2.5 modeling")
    except ImportError:
        log.warning("Could not apply compat shim — masking_utils not found")

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
    # Fix: Spark-X2.5 generation_config.json ships top_k=-1 which transformers
    # rejects in TopKLogitsWarper. Override to 0 (no top-k filtering).
    if hasattr(mdl, "generation_config"):
        mdl.generation_config.top_k = 0
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
            top_k=0,  # Fix: Spark-X2.5 config has top_k=-1, invalid for transformers
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
    if not os.getenv("LOCAL_LLM_API_KEY", "").strip():
        log.warning(
            "No LOCAL_LLM_API_KEY set — requiring the dev key persisted at %s "
            "(key material is never logged). Set LOCAL_LLM_API_KEY for production.",
            _DEV_KEY_FILE,
        )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
