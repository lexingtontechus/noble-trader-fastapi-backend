"""
Noble Trader Gateway — single-port reverse proxy.

Runs on the PUBLIC port (default 8000) and routes to two backend services:

  /forecast*, /info*  → TimesFM server  (port 8002)
  /health              → Aggregated from both backends
  /*                   → Main app        (port 8001)

Usage (called by start.sh, not directly):
    python proxy.py

Requires httpx (already in requirements.txt). No other deps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("gateway")

# ── Backend URLs (override via env for production) ──────────────────────────

MAIN_URL = os.getenv("MAIN_URL", "http://127.0.0.1:8001")
TIMESFM_URL = os.getenv("TIMESFM_URL", "http://127.0.0.1:8002")
PORT = int(os.getenv("PORT", "8000"))

# Routes that should go to TimesFM
FORECAST_PREFIXES = ("/forecast", "/info")


# ── Shared HTTP client (connection-pooled) ──────────────────────────────────

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=90.0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ── App ─────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Gateway starting — public port {PORT}")
    log.info(f"  Main app    → {MAIN_URL}")
    log.info(f"  TimesFM     → {TIMESFM_URL}")
    yield
    await close_client()
    log.info("Gateway shut down")


app = FastAPI(title="Noble Trader Gateway", version="1.0.0", lifespan=lifespan)


# ── Core proxy logic ────────────────────────────────────────────────────────


async def _proxy_to(request: Request, backend: str) -> Response:
    """Forward an HTTP request to *backend* and return its response.

    Preserves the original method, body, query string, and all headers
    except ``host`` (which is set by httpx automatically).
    """
    client = await get_client()
    target_path = request.url.path
    if request.url.query:
        target_path += f"?{request.url.query}"
    url = f"{backend}{target_path}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)  # let httpx set the right Host

    try:
        resp = await client.request(
            method=request.method,
            url=url,
            content=body or None,
            headers=headers,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )
    except httpx.RequestError as exc:
        log.error("Proxy error %s → %s: %s", request.url.path, backend, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Backend unreachable",
                "backend": backend,
                "path": request.url.path,
                "detail": str(exc),
            },
        )


async def _health_check(url: str, label: str) -> dict:
    """Fetch /health from one backend, return a summary dict."""
    client = await get_client()
    try:
        t0 = time.time()
        resp = await client.get(f"{url}/health", timeout=5.0)
        elapsed = round((time.time() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            data["_reachable"] = True
            data["_latency_ms"] = elapsed
            return {label: data}
        else:
            return {
                label: {
                    "status": "error",
                    "_reachable": True,
                    "_http_status": resp.status_code,
                    "_latency_ms": elapsed,
                }
            }
    except Exception as exc:
        return {
            label: {
                "status": "unreachable",
                "_reachable": False,
                "_error": str(exc),
            }
        }


# ── Routes ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Aggregated health — probes both backends in parallel."""
    results = await asyncio.gather(
        _health_check(MAIN_URL, "main"),
        _health_check(TIMESFM_URL, "timesfm"),
    )

    main_status = results[0]["main"]
    timesfm_status = results[1]["timesfm"]

    # Determine overall status
    all_ok = (
        main_status.get("status") != "unreachable"
        and timesfm_status.get("status") != "unreachable"
    )

    return {
        "status": "ok" if all_ok else "degraded",
        "gateway": "active",
        "main": main_status,
        "timesfm": timesfm_status,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def catch_all(request: Request, path: str):
    """Route all requests to the appropriate backend.

    - ``/forecast*`` and ``/info*``  → TimesFM
    - Everything else                → Main app
    """
    if path.startswith(FORECAST_PREFIXES):
        return await _proxy_to(request, TIMESFM_URL)
    return await _proxy_to(request, MAIN_URL)


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(
        "proxy:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
