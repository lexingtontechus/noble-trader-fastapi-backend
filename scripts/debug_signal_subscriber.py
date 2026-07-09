#!/usr/bin/env python3
"""
Redis debug signal subscriber with Discord forwarding.

Listens to "signal.debug.noble_trader" and:
  1. Prints every signal to stdout (qualified AND rejected)
  2. Forwards each signal to a Discord debug webhook as a rich embed

Usage:
  # Set REDIS_URL and DISCORD_WEBHOOK_DEBUG env vars first:
  export REDIS_URL=rediss://:token@host:6379
  export DISCORD_WEBHOOK_DEBUG=https://discord.com/api/webhooks/.../...

  # Then run:
  python3 scripts/debug_signal_subscriber.py

  # Or pass the webhook URL as a CLI arg:
  python3 scripts/debug_signal_subscriber.py \
    --discord-webhook https://discord.com/api/webhooks/.../...

  # Filter to a specific symbol:
  python3 scripts/debug_signal_subscriber.py --symbol BTC

  # Show only rejected signals:
  python3 scripts/debug_signal_subscriber.py --rejected-only

  # Disable Discord forwarding (just print to stdout):
  python3 scripts/debug_signal_subscriber.py --no-discord

Requires DEBUG_SIGNALS=1 env var to be set on the sweep orchestrator so
it publishes to the "signal.debug.noble_trader" channel.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

REPO = "/home/z/my-project/noble-trader-fastapi-backend"
sys.path.insert(0, REPO)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("debug_sub")


# ── Discord rate limiter ──────────────────────────────────────────────────
# Discord allows ~5 messages per 2 seconds per webhook. We track timestamps
# and drop messages above the limit (fire-and-forget — don't block the
# subscriber loop).

class DiscordRateLimiter:
    """Sliding-window rate limiter for Discord webhook posts."""

    def __init__(self, max_per_window: int = 5, window_seconds: float = 2.0):
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._timestamps: deque = deque()

    def can_send(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) < self.max_per_window

    def record_send(self) -> None:
        self._timestamps.append(time.monotonic())


# ── Discord embed builder ─────────────────────────────────────────────────

# Color palette (decimal integers for Discord API)
COLOR_QUALIFIED = 0x2ECC71    # Green
COLOR_REJECTED = 0xE74C3C     # Red
COLOR_TIMESFM_MISSING = 0xF39C12  # Orange (TimesFM unavailable)
COLOR_INFO = 0x3498DB         # Blue


def build_debug_embed(data: dict) -> dict:
    """Build a Discord embed for a debug signal.

    Color-coded:
      Green  — qualified (all gates passed)
      Red    — rejected (at least one gate failed)
      Orange — TimesFM unavailable (Gate 4 failed)
    """
    symbol = data.get("symbol", "?")
    qualified = data.get("qualified", False)
    signal = data.get("signal", "?")
    regime = data.get("regime", "?")
    regime_conf = data.get("regime_conf", 0)
    kelly_f = data.get("kelly_f", 0)
    p_timesfm = data.get("p_timesfm")
    timesfm_conf = data.get("timesfm_confidence")
    ev = data.get("ev", 0)
    rr = data.get("rr", 0)
    ev_allowed = data.get("ev_allowed", False)
    ev_reason = data.get("ev_reason", "")
    failed_gates = data.get("failed_gates", [])
    entry = data.get("entry_price", 0)
    sl = data.get("stop_loss", 0)
    tp = data.get("take_profit", 0)
    sl_bricks = data.get("sl_bricks", 0)
    tp_bricks = data.get("tp_bricks", 0)
    ts_ms = data.get("ts", 0)

    # Determine color
    if qualified:
        color = COLOR_QUALIFIED
        status_emoji = "✅"
        status_text = "QUALIFIED"
    elif p_timesfm is None:
        color = COLOR_TIMESFM_MISSING
        status_emoji = "⚠️"
        status_text = "REJECTED (TimesFM unavailable)"
    else:
        color = COLOR_REJECTED
        status_emoji = "❌"
        status_text = "REJECTED"

    # Build fields
    fields = [
        {"name": "Symbol", "value": symbol, "inline": True},
        {"name": "Signal", "value": f"{status_emoji} {signal}", "inline": True},
        {"name": "Status", "value": status_text, "inline": True},
        {"name": "Regime", "value": regime, "inline": True},
        {"name": "Regime Conf", "value": f"{regime_conf:.4f}", "inline": True},
        {"name": "Kelly", "value": f"{kelly_f:.6f}", "inline": True},
    ]

    # TimesFM fields
    if p_timesfm is not None:
        fields.append({
            "name": "P_timesfm",
            "value": f"{p_timesfm:.4f} (conf: {timesfm_conf:.4f})",
            "inline": True,
        })
    else:
        fields.append({
            "name": "P_timesfm",
            "value": "⚠️ None (unavailable)",
            "inline": True,
        })

    # EV/RR fields
    fields.append({"name": "EV", "value": f"{ev:.4f}", "inline": True})
    fields.append({"name": "RR", "value": f"{rr:.4f}", "inline": True})
    fields.append({
        "name": "EV Allowed",
        "value": "✅" if ev_allowed else "❌",
        "inline": True,
    })

    # Trade levels
    fields.append({"name": "Entry", "value": f"${entry}", "inline": True})
    fields.append({"name": "Stop Loss", "value": f"${sl}", "inline": True})
    fields.append({"name": "Take Profit", "value": f"${tp}", "inline": True})
    fields.append({
        "name": "Bricks",
        "value": f"SL: {sl_bricks} | TP: {tp_bricks}",
        "inline": True,
    })

    # Failed gates (only if rejected)
    if failed_gates:
        fields.append({
            "name": "❌ Failed Gates",
            "value": ", ".join(failed_gates),
            "inline": False,
        })

    # EV reason (only if rejected by EV gate)
    if ev_reason and not ev_allowed:
        fields.append({
            "name": "EV Reason",
            "value": ev_reason[:1024],  # Discord field value limit
            "inline": False,
        })

    # Timestamp
    timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

    title = f"{'✅' if qualified else '❌'} {symbol} {signal.upper()} — {'QUALIFIED' if qualified else 'REJECTED'}"

    return {
        "title": title,
        "description": f"Regime: **{regime}** (conf={regime_conf:.4f})",
        "color": color,
        "timestamp": timestamp,
        "fields": fields,
        "footer": {"text": "Noble Trader Debug Subscriber | signal.debug.noble_trader"},
    }


async def send_to_discord(webhook_url: str, embed: dict, rate_limiter: DiscordRateLimiter) -> bool:
    """Post an embed to Discord. Returns True if sent, False if dropped/failed."""
    if not rate_limiter.can_send():
        log.warning("Discord rate limit reached — dropping debug message")
        return False

    payload = {"embeds": [embed]}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 204):
                rate_limiter.record_send()
                return True
            elif resp.status_code == 429:
                # Rate limited by Discord — extract retry-after
                retry_after = resp.headers.get("Retry-After", "2")
                log.warning(f"Discord 429 rate limited — retry after {retry_after}s")
                return False
            else:
                log.error(f"Discord webhook HTTP {resp.status_code}: {resp.text[:200]}")
                return False
    except Exception as e:
        log.error(f"Discord webhook error: {e}")
        return False


# ── Statistics tracker ────────────────────────────────────────────────────

class SignalStats:
    """Track running statistics for periodic summary logging."""

    def __init__(self):
        self.total = 0
        self.qualified = 0
        self.rejected = 0
        self.by_symbol: dict = defaultdict(lambda: {"total": 0, "qualified": 0})
        self.by_failed_gate: dict = defaultdict(int)
        self.start_time = time.time()

    def record(self, data: dict):
        self.total += 1
        symbol = data.get("symbol", "?")
        qualified = data.get("qualified", False)
        failed_gates = data.get("failed_gates", [])

        if qualified:
            self.qualified += 1
            self.by_symbol[symbol]["qualified"] += 1
        else:
            self.rejected += 1
            for gate in failed_gates:
                self.by_failed_gate[gate] += 1
        self.by_symbol[symbol]["total"] += 1

    def summary(self) -> str:
        elapsed = time.time() - self.start_time
        rate = self.total / max(elapsed, 1) * 60  # signals per minute
        pct = (self.qualified / self.total * 100) if self.total > 0 else 0

        lines = [
            f"📊 Stats ({elapsed:.0f}s elapsed, {rate:.1f} sig/min):",
            f"   Total: {self.total} | Qualified: {self.qualified} ({pct:.1f}%) | Rejected: {self.rejected}",
        ]

        if self.by_failed_gate:
            top_gates = sorted(self.by_failed_gate.items(), key=lambda x: -x[1])[:5]
            gate_str = ", ".join(f"{g}={n}" for g, n in top_gates)
            lines.append(f"   Top rejection gates: {gate_str}")

        if self.by_symbol:
            top_symbols = sorted(self.by_symbol.items(), key=lambda x: -x[1]["total"])[:5]
            sym_str = ", ".join(
                f"{s}({d['qualified']}/{d['total']})" for s, d in top_symbols
            )
            lines.append(f"   Top symbols (qualified/total): {sym_str}")

        return "\n".join(lines)


# ── Main subscriber loop ─────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Subscribe to debug signal channel + forward to Discord")
    parser.add_argument("--symbol", default=None, help="Filter to specific symbol")
    parser.add_argument("--rejected-only", action="store_true", help="Only show rejected signals")
    parser.add_argument("--qualified-only", action="store_true", help="Only show qualified signals")
    parser.add_argument("--no-discord", action="store_true", help="Disable Discord forwarding (just print to stdout)")
    parser.add_argument("--discord-webhook", default=None,
                        help="Discord webhook URL (or set DISCORD_WEBHOOK_DEBUG env var)")
    parser.add_argument("--stats-interval", type=int, default=300,
                        help="Print stats summary every N seconds (default: 300)")
    args = parser.parse_args()

    from regime_platform.core.settings import settings
    import redis.asyncio as aioredis

    redis_url = settings.redis_url_effective
    if not redis_url:
        print("ERROR: Set REDIS_URL or UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN")
        sys.exit(1)

    # Resolve Discord webhook URL
    discord_webhook = args.discord_webhook or os.environ.get("DISCORD_WEBHOOK_DEBUG")
    if args.no_discord:
        discord_webhook = None

    # Validate webhook URL if provided
    if discord_webhook:
        parsed = urlparse(discord_webhook)
        if parsed.scheme != "https" or "discord.com/api/webhooks" not in parsed.path:
            print(f"ERROR: Invalid Discord webhook URL: {discord_webhook[:50]}...")
            sys.exit(1)

    print("=" * 80)
    print("  Debug Signal Subscriber" + (" + Discord Forwarder" if discord_webhook else ""))
    print(f"  Redis:     {redis_url[:50]}...")
    print(f"  Channel:   signal.debug.noble_trader")
    print(f"  Discord:   {'✅ ' + discord_webhook[:60] + '...' if discord_webhook else '❌ disabled (--no-discord)'}")
    print(f"  Filter:    symbol={args.symbol or 'ALL'}, "
          f"rejected_only={args.rejected_only}, qualified_only={args.qualified_only}")
    print(f"  Stats:     every {args.stats_interval}s")
    print("=" * 80)
    print()
    print("Waiting for signals... (Ctrl+C to stop)")
    print()

    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("signal.debug.noble_trader")

    rate_limiter = DiscordRateLimiter(max_per_window=5, window_seconds=2.0)
    stats = SignalStats()
    last_stats_time = time.time()

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
            except json.JSONDecodeError:
                continue

            # Apply filters
            if args.symbol and data.get("symbol") != args.symbol:
                continue
            if args.rejected_only and data.get("qualified"):
                continue
            if args.qualified_only and not data.get("qualified"):
                continue

            # Record stats
            stats.record(data)

            # ── Print to stdout ───────────────────────────────────────
            ts = datetime.fromtimestamp(data.get("ts", 0) / 1000).strftime("%H:%M:%S")
            symbol = data.get("symbol", "?")
            qualified = data.get("qualified", False)
            signal = data.get("signal", "?")
            regime = data.get("regime", "?")
            regime_conf = data.get("regime_conf", 0)
            kelly_f = data.get("kelly_f", 0)
            p_timesfm = data.get("p_timesfm")
            ev = data.get("ev", 0)
            rr = data.get("rr", 0)
            ev_allowed = data.get("ev_allowed", False)
            failed_gates = data.get("failed_gates", [])

            status = "✓ QUALIFIED" if qualified else "✗ REJECTED"
            print(f"[{ts}] {symbol:8} {status}")
            print(f"         signal={signal:8} regime={regime:30} conf={regime_conf:.4f}")
            print(f"         kelly_f={kelly_f:.6f}  p_timesfm={p_timesfm}  "
                  f"ev={ev:.4f}  rr={rr:.4f}  ev_allowed={ev_allowed}")
            if not qualified and failed_gates:
                print(f"         FAILED GATES: {failed_gates}")
            print()

            # ── Forward to Discord ────────────────────────────────────
            if discord_webhook:
                embed = build_debug_embed(data)
                # Fire-and-forget (don't block the subscriber loop)
                asyncio.create_task(
                    send_to_discord(discord_webhook, embed, rate_limiter)
                )

            # ── Periodic stats summary ────────────────────────────────
            now = time.time()
            if now - last_stats_time >= args.stats_interval:
                print(stats.summary())
                print()
                last_stats_time = now

    except KeyboardInterrupt:
        print("\n\nStopping...")
        print(stats.summary())
    finally:
        await pubsub.unsubscribe("signal.debug.noble_trader")
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
