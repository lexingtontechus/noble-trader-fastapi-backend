"""
Supabase Realtime broadcast publisher (fire-and-forget, best-effort)
════════════════════════════════════════════════════════════════════

Talaria dashboard integration. Publishes live qualified signals and paper
portfolio events over Supabase Realtime *broadcast* channels:

    ``realtime:signals``  →  event ``"signal"``  (qualified light/heavy signals)
    ``realtime:paper``    →  event ``"paper"``   (paper position opened/closed)

TRANSPORT (verified 2026-08-06):
  supabase-py is NOT installed in this repo — ``supabase.__version__`` raises
  AttributeError and ``supabase.realtime`` is missing; the ``supabase/``
  directory at the repo root is the edge-functions/migrations folder, not a
  Python package. The REST layer (``supabase_client.py``) talks to PostgREST
  over httpx directly.

  So this module speaks the Realtime *wire protocol* directly over the
  installed ``websockets`` package (>=12; currently 15.x): connect to
  ``/realtime/v1/websocket?apikey=<service-role-key>&vsn=1.0.0``, join the
  phoenix channel topic, send a ``broadcast`` event, close. No new
  dependencies. The service-role key authorizes the join.

  NOTE (2026-08-08, verified live): the join deliberately does NOT set
  ``private: true``. A private join flips the channel into authorization mode
  for ALL members, which blocks anon-key clients (the Talaria plugin) from
  receiving broadcasts (join ok, zero delivery). A public join keeps the
  channel public; the service key still authorizes it, and anon clients
  receive.

DESIGN RULES (mirror ``log_signal_sim`` in supabase_client.py):
  • NEVER raises. Failures are counted + logged, never propagated, never
    break the sweep.
  • Fire-and-forget: the caller schedules an asyncio task; if there is no
    running event loop the broadcast is skipped (debug log), exactly like
    the RuntimeError guard in the orchestrator's capture block. All IO runs
    inside the task, so the sweep loop is never blocked.
  • Published UNCONDITIONALLY on both channels — subscription / enforcement
    is the dashboard's job (it subscribes with its own key), not ours.
  • Env toggle: ``NOBLE_REALTIME_BROADCAST`` (default ``"1"``) — set to
    ``"0"`` to disable, read via the same pydantic settings as the Supabase
    REST client.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import websockets

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.sweep.realtime")

# ─── Broadcast channels (phoenix topics) ─────────────────────────────
SIGNALS_TOPIC = "realtime:signals"
PAPER_TOPIC = "realtime:paper"
SIGNALS_EVENT = "signal"
PAPER_EVENT = "paper"

# Per-topic failure counters (observability + tests).
_failures: dict[str, int] = {}

_ref_counter = itertools.count(1)

# Tight timeouts — this is best-effort telemetry, never a blocking path.
_CONNECT_TIMEOUT = 5.0
_DRAIN_TIMEOUT = 2.0
_TOTAL_TIMEOUT = 12.0


# ─── Config / URL ────────────────────────────────────────────────────


def _broadcast_enabled() -> bool:
    """Gate on Supabase config + env toggle (NOBLE_REALTIME_BROADCAST)."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return False
    return settings.NOBLE_REALTIME_BROADCAST == "1"


def _realtime_ws_url() -> str:
    """Build the Realtime websocket URL from ``settings.SUPABASE_URL``.

    Same shape supabase-js uses: ``wss://<host>/realtime/v1/websocket?apikey=<key>&vsn=1.0.0``.
    The ``apikey`` query param carries the service-role JWT — the server
    authorizes the join from it, so no extra auth headers are needed.
    """
    parts = urlsplit(settings.SUPABASE_URL)
    scheme = "wss" if parts.scheme in ("https", "wss") else "ws"
    netloc = parts.netloc or (parts.hostname or "")
    base = urlunsplit((scheme, netloc, "/realtime/v1/websocket", "", ""))
    query = urlencode({"apikey": settings.SUPABASE_KEY, "vsn": "1.0.0"})
    return f"{base}?{query}"


# ─── Public API ──────────────────────────────────────────────────────


def build_signal_payload(
    *,
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    kelly_f: float | None = None,
    effective_kelly: float | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """Build the ``signal`` broadcast payload for one qualified signal.

    Mirrors ``_build_signal_sim_payload``'s price-precision rule: prices are
    rounded to 6dp at FULL value — do NOT round to per-symbol decimals
    (2dp truncates XAUUSD/XAGUSD entries).

    ``kelly`` prefers ``effective_kelly`` (kelly × EV scale) and falls back
    to ``kelly_f`` — same sizing input the paper book uses.
    """
    return {
        "symbol": symbol,
        "direction": direction,
        "kelly": round(float(effective_kelly), 6) if effective_kelly is not None
                 else (round(float(kelly_f), 6) if kelly_f is not None else None),
        "entry_price": round(float(entry_price), 6),
        "stop_loss": round(float(stop_loss), 6),
        "take_profit": round(float(take_profit), 6),
        "ts": float(ts) if ts is not None else time.time(),
    }


def broadcast_signal(payload: dict[str, Any]) -> None:
    """Fire-and-forget best-effort publish to ``realtime:signals`` / ``signal``.

    Never raises. Without a running event loop the broadcast is skipped
    (debug log), mirroring the orchestrator's capture-path guard.
    """
    if not _broadcast_enabled():
        return
    _fire_and_forget(SIGNALS_TOPIC, SIGNALS_EVENT, payload)


def broadcast_paper(payload: dict[str, Any]) -> None:
    """Fire-and-forget best-effort publish to ``realtime:paper`` / ``paper``.

    Never raises. Without a running event loop the broadcast is skipped
    (debug log).
    """
    if not _broadcast_enabled():
        return
    _fire_and_forget(PAPER_TOPIC, PAPER_EVENT, payload)


def failure_count(topic: str) -> int:
    """Number of failed publish attempts for a topic (observability/tests)."""
    return _failures.get(topic, 0)


# ─── Internals ───────────────────────────────────────────────────────


def _next_ref() -> str:
    return str(next(_ref_counter))


def _fire_and_forget(topic: str, event: str, payload: dict[str, Any]) -> None:
    """Schedule ``_publish`` as a task on the running loop, or skip."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop (e.g. a test invoking the sweep
        # synchronously) — skip, exactly like log_signal_sim's guard.
        logger.debug("realtime: no running event loop — skipping %s broadcast", topic)
        return
    try:
        loop.create_task(_publish(topic, event, payload))
    except RuntimeError:
        # Loop is closed / shutting down — skip.
        logger.debug("realtime: loop not accepting tasks — skipping %s broadcast", topic)


async def _publish(topic: str, event: str, payload: dict[str, Any]) -> None:
    """One-shot connect → join → broadcast → close. Never raises."""
    try:
        async with asyncio.timeout(_TOTAL_TIMEOUT):
            await _publish_once(topic, event, payload)
    except Exception as exc:
        _failures[topic] = _failures.get(topic, 0) + 1
        logger.error("realtime: broadcast %s/%s failed: %s", topic, event, exc)


async def _publish_once(topic: str, event: str, payload: dict[str, Any]) -> None:
    """Raw phoenix-protocol broadcast over one short-lived websocket.

    Frames are sent in order on the same connection, so the ``phx_join``
    is processed by the server before the ``broadcast`` frame; the brief
    recv-drain just lets the join reply / processing flush before close.
    """
    url = _realtime_ws_url()
    async with websockets.connect(url, open_timeout=_CONNECT_TIMEOUT) as ws:
        await _join_channel(ws, topic)
        await ws.send(json.dumps({
            "topic": topic,
            "event": "broadcast",
            "payload": {
                "type": "broadcast",
                "event": event,
                "payload": payload,
            },
            "ref": _next_ref(),
        }))
        # Drain a frame or two so the server has time to process (join
        # reply / broadcast ack). Best-effort — ignore what comes back.
        try:
            await asyncio.wait_for(ws.recv(), timeout=_DRAIN_TIMEOUT)
        except Exception:
            pass
    logger.debug("realtime: broadcast sent to %s (event=%s)", topic, event)


async def _join_channel(ws: Any, topic: str) -> None:
    """Send the phoenix ``phx_join`` for ``topic`` (service-key authorized)."""
    await ws.send(json.dumps({
        "topic": topic,
        "event": "phx_join",
        "payload": {
            "config": {
                "broadcast": {"ack": False, "self": False},
                "presence": {"key": ""},
            },
        },
        "ref": _next_ref(),
    }))
