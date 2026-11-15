"""discord_notify.py — the #crt Discord signal sender (operator addition, 2026-11-15).

Sends CRT lab signals to the operator's Discord channel **#crt** via webhook.
Configured from ``config/crt/crt_discord.yaml`` (the self-contained signal
config) — but the webhook URL itself resolves ENV-FIRST: the
``DISCORD_WEBHOOK_CRT`` environment variable overrides the yaml's
``webhook_url`` (operator instruction, 2026-11-15 — the secret lives in the
environment, not the repo; see ``regime_edge_crt.config.load_discord_config``).
Events: handshake / baseline / walkforward / mint / error (engine events,
e.g. ``fill``/``resolve``, pass through with default-on gating).

Contract (the lab never breaks because Discord did):

- FAIL-OPEN: any failure (network, 4xx/5xx, timeout, malformed config) is
  printed to stderr and swallowed. A Discord outage must never fail a run.
- BOUNDED: 10s timeout; at most ``max_attempts`` POSTs per message; a 429
  honors ``Retry-After`` capped at ``rate_limit.max_sleep`` seconds.
- THROTTLED: sliding window ``messages_per_window`` per ``window_seconds``
  (Discord's documented ~5 messages / 2s per webhook).
- CHUNKED: content splits at ``max_content_chars`` (1900 < Discord's 2000
  cap) on newline boundaries.
- MASKED: the webhook URL is never printed (last 6 chars only).

No ``regime_platform`` imports — the research twin stays standalone; this
module needs only httpx (a repo requirement).
"""
from __future__ import annotations

import sys
import time

import httpx

EVENT_ORDER = ("handshake", "baseline", "walkforward", "mint", "error")


def mask_url(url: str) -> str:
    if not url:
        return "<unset>"
    return f"webhook:…{url[-6:]}"


class CRTDiscord:
    """One configured sender. Construct via ``from_config()``."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled")) and bool(cfg.get("webhook_url"))
        self.webhook_url = str(cfg.get("webhook_url") or "")
        self.username = str(cfg.get("username") or "CRT Lab")
        self.label = str(cfg.get("label") or "CRT-0")
        self.timeout = float(cfg.get("timeout_seconds", 10.0))
        self.max_attempts = max(1, int(cfg.get("max_attempts", 2)))
        rl = cfg.get("rate_limit") or {}
        self.per_window = max(1, int(rl.get("messages_per_window", 5)))
        self.window = float(rl.get("window_seconds", 2.0))
        self.max_sleep = float(rl.get("max_sleep", 3.0))
        self.max_chars = max(200, int(cfg.get("max_content_chars", 1900)))
        self.events = {k: bool(v) for k, v in (cfg.get("events") or {}).items()}
        self._sent_times: list[float] = []
        self._client = None
        if self.enabled:
            try:
                self._client = httpx.Client(timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - fail-open by contract
                self._fail(f"init: {exc!r}")
                self._client = None

    # -- fail-open plumbing -------------------------------------------------
    def _fail(self, what: str) -> None:
        print(f"[crt-discord] {mask_url(self.webhook_url)} {what} — signal skipped "
              "(fail-open, the lab continues)", file=sys.stderr)

    # -- rate limiting -------------------------------------------------------
    def _throttle(self) -> None:
        now = time.monotonic()
        self._sent_times = [t for t in self._sent_times if now - t < self.window]
        if len(self._sent_times) >= self.per_window:
            wait = min(self.window - (now - self._sent_times[0]) + 0.05, self.max_sleep)
            if wait > 0:
                time.sleep(wait)
            now = time.monotonic()
            self._sent_times = [t for t in self._sent_times if now - t < self.window]
        self._sent_times.append(time.monotonic())

    # -- chunking ------------------------------------------------------------
    def _chunks(self, content: str):
        text = content.rstrip()
        while len(text) > self.max_chars:
            cut = text.rfind("\n", 0, self.max_chars)
            if cut < self.max_chars // 2:
                cut = self.max_chars
            yield text[:cut].rstrip()
            text = text[cut:].lstrip()
        if text:
            yield text

    # -- transport -----------------------------------------------------------
    def _post(self, body: dict) -> tuple[bool, str]:
        if self._client is None:
            return False, "no client"
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            try:
                resp = self._client.post(self.webhook_url, json=body)
                if resp.status_code in (200, 204):
                    return True, ""
                if resp.status_code == 429:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", "1"))
                    except (ValueError, TypeError):
                        retry_after = 1.0
                    if attempt < self.max_attempts:
                        time.sleep(min(retry_after, self.max_sleep))
                        continue
                    return False, f"429 after {attempt} attempts"
                if resp.status_code >= 500 and attempt < self.max_attempts:
                    continue
                return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
            except Exception as exc:  # noqa: BLE001 - fail-open by contract
                return False, f"transport: {exc!r}"
        return False, "attempts exhausted"

    # -- public API ----------------------------------------------------------
    def send(self, content: str) -> bool:
        """Send raw content (chunked, throttled). Returns success; never raises."""
        if not self.enabled:
            return False
        ok_any = True
        for chunk in self._chunks(content):
            self._throttle()
            body = {"content": chunk, "username": self.username}
            ok, why = self._post(body)
            if not ok:
                self._fail(why)
                ok_any = False
        return ok_any

    def notify(self, event: str, title: str, body: str) -> bool:
        """Event-gated send with the uniform envelope: bold CRT header + body."""
        if not self.events.get(event, True):
            return False
        text = f"**{self.label} | {event.upper()} — {title}**\n{body}"
        return self.send(text)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - fail-open by contract
                pass


def from_config(cfg: dict) -> CRTDiscord:
    """Build the notifier from a discord config mapping (load_discord_config)."""
    return CRTDiscord(cfg)


def event_message(event: str, title: str, body: str, label: str = "CRT-0") -> str:
    """Uniform event envelope: bold CRT header + body (Markdown-safe)."""
    return f"**{label} | {event.upper()} — {title}**\n{body}"
