"""
Sweep Orchestrator — Entry Point
═════════════════════════════════

Standalone asyncio application that:
  1. Starts the SweepOrchestrator (APScheduler + stale checker)
  2. Exposes a minimal HTTP health-check endpoint on port 8003

Usage:
    python -m regime_platform.sweep_orchestrator

Designed to be launched as a 4th process in ``start.sh`` alongside
the main API server, TimesFM server, and gateway proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator

logger = logging.getLogger("regime.sweep")

# ─── Health-check server ─────────────────────────────────────────


async def _health_server(orch: SweepOrchestrator, port: int) -> None:
    """Minimal HTTP health check on *port*.

    Responds to GET /health with JSON status.  This is intentionally
    a raw asyncio server (no FastAPI dependency) so the orchestrator
    stays lightweight.
    """
    loop = asyncio.get_event_loop()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("utf-8").strip()
            # Consume headers
            while True:
                line = (await reader.readline()).decode("utf-8").strip()
                if not line:
                    break
        except Exception:
            writer.close()
            return

        method, path, _ = (request_line + " / ").split(" ", 2)[:3]

        if method == "GET" and path == "/health":
            body = json.dumps({
                "status": "ok",
                "service": "sweep-orchestrator",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
                f"{body}"
            )
        else:
            response = (
                "HTTP/1.1 404 Not Found\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n"
                "\r\n"
            )

        writer.write(response.encode("utf-8"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, host="0.0.0.0", port=port)
    logger.info("Health server listening on 0.0.0.0:%d", port)

    async with server:
        await server.serve_forever()


# ─── Shutdown helpers ────────────────────────────────────────────

_shutdown_event = asyncio.Event()


def _signal_handler() -> None:
    _shutdown_event.set()


# ─── Main ────────────────────────────────────────────────────────


async def main() -> None:
    """Entry point: start orchestrator + health server, wait for shutdown."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )

    health_port = int(os.getenv("SWEEP_ORCHESTRATOR_PORT", "8003"))
    print(f"[sweep-orch] Starting sweep orchestrator (health port {health_port})...", flush=True)
    logger.info("Starting sweep orchestrator (health port %d)...", health_port)

    su = bool(os.environ.get("SUPABASE_URL"))
    sk = bool(os.environ.get("SUPABASE_KEY"))
    redis_url = os.environ.get("REDIS_URL", "")
    upstash_url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    print(f"[sweep-orch] SUPABASE_URL={'set' if su else 'NOT SET'}, SUPABASE_KEY={'set' if sk else 'NOT SET'}", flush=True)
    print(f"[sweep-orch] REDIS_URL={'set' if redis_url else 'NOT SET'}, UPSTASH_REDIS_REST_URL={'set' if upstash_url else 'NOT SET'}", flush=True)

    if not su or not sk:
        print("[sweep-orch] FATAL: Supabase not configured — sweeps cannot run", flush=True)

    # Show the effective Redis URL (password masked)
    from regime_platform.core.settings import settings as _settings
    _effective = _settings.redis_url_effective
    if _effective:
        import re as _re
        _masked = _re.sub(r':([^@]+)@', ':****@', _effective)
        print(f"[sweep-orch] Effective Redis URL: {_masked}", flush=True)

        # Test connection directly
        try:
            import redis.asyncio as _aioredis
            _test_client = _aioredis.from_url(
                _effective,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=True,
            )
            await _test_client.ping()
            print("[sweep-orch] Redis PING: OK", flush=True)
            await _test_client.aclose()
        except Exception as _e:
            print(f"[sweep-orch] Redis PING: FAILED — {_e}", flush=True)
            import traceback as _tb
            _tb.print_exc()
    else:
        print("[sweep-orch] Effective Redis URL: (empty — no Redis configured)", flush=True)

    orch = SweepOrchestrator()
    await orch.start()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (ValueError, RuntimeError):
            # Windows or restricted environments
            pass

    # Run health server and wait for shutdown signal
    try:
        await asyncio.gather(
            _health_server(orch, health_port),
            _shutdown_event.wait(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down sweep orchestrator...")
        await orch.stop()
        logger.info("Sweep orchestrator stopped")


if __name__ == "__main__":
    asyncio.run(main())
