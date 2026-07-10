#!/usr/bin/env python3
"""
Redis Streams diagnostic tool for signal.raw.noble_trader.

Tests whether the orchestrator is writing qualified signals to the
Redis Stream (XADD) and lets you inspect them.

Usage:
  # Set REDIS_URL first:
  export REDIS_URL=rediss://:token@host:6379

  # 1. Check stream exists and show length:
  python3 scripts/test_signal_stream.py info

  # 2. Read the latest N entries:
  python3 scripts/test_signal_stream.py read --count 5

  # 3. Read entries from the last hour:
  python3 scripts/test_signal_stream.py read --since 1h

  # 4. Tail the stream in real-time (like tail -f):
  python3 scripts/test_signal_stream.py tail

  # 5. Publish a test entry to verify your downstream consumer works:
  python3 scripts/test_signal_stream.py publish-test

  # 6. Check if your downstream consumer group is reading:
  python3 scripts/test_signal_stream.py groups

  # 7. Read from a specific consumer group:
  python3 scripts/test_signal_stream.py read-group --group myconsumer --count 5
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

REPO = "/home/z/my-project/noble-trader-fastapi-backend"
sys.path.insert(0, REPO)

STREAM_KEY = "signal.raw.noble_trader"


async def get_redis():
    """Connect to Redis using the project's settings."""
    from regime_platform.core.settings import settings
    import redis.asyncio as aioredis

    redis_url = settings.redis_url_effective
    if not redis_url:
        print("ERROR: Set REDIS_URL or UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN")
        sys.exit(1)

    return aioredis.from_url(redis_url, decode_responses=True)


def format_entry(entry_id: str, fields: dict) -> str:
    """Pretty-print a stream entry."""
    try:
        payload = json.loads(fields.get("payload", "{}"))
    except (json.JSONDecodeError, TypeError):
        payload = fields

    ts_str = "?"
    if "-" in entry_id:
        ts_ms = int(entry_id.split("-")[0])
        ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    symbol = payload.get("symbol", "?")
    signal = payload.get("signal", "?")
    qualified = payload.get("qualified", "?")
    regime = payload.get("regime", "?")
    kelly = payload.get("kelly_f", "?")
    ev = payload.get("ev", "?")
    p_timesfm = payload.get("p_timesfm", "?")
    asset_class = payload.get("asset_class", "?")
    venue = payload.get("venue", "?")

    lines = [
        f"  ┌─ Entry ID: {entry_id}  ({ts_str})",
        f"  │ Symbol:    {symbol}  ({asset_class} / {venue})",
        f"  │ Signal:    {signal}  Qualified: {qualified}",
        f"  │ Regime:    {regime}",
        f"  │ Kelly:     {kelly}  EV: {ev}  p_timesfm: {p_timesfm}",
    ]
    # Show full payload for detailed inspection
    if len(fields) > 1 or "payload" not in fields:
        lines.append(f"  │ Raw fields: {json.dumps(fields, indent=2)[:500]}")
    lines.append(f"  └─")
    return "\n".join(lines)


async def cmd_info(args):
    """Show stream info: exists, length, first/last entry."""
    r = await get_redis()
    try:
        exists = await r.exists(STREAM_KEY)
        if not exists:
            print(f"✗ Stream '{STREAM_KEY}' does NOT exist.")
            print()
            print("This means the orchestrator has never XADDed to it.")
            print("Possible causes:")
            print("  1. No qualified signals have been generated yet")
            print("  2. The orchestrator is using pub/sub (PUBLISH) not Streams (XADD)")
            print("  3. The orchestrator hasn't been restarted with the XADD fix")
            print()
            print("Check if pub/sub messages are flowing instead:")
            print("  python3 scripts/debug_signal_subscriber.py")
            return

        length = await r.xlen(STREAM_KEY)
        print(f"✓ Stream '{STREAM_KEY}' exists")
        print(f"  Length: {length} entries")

        if length > 0:
            # Get first and last entry
            first = await r.xrange(STREAM_KEY, count=1)
            last = await r.xrevrange(STREAM_KEY, count=1)
            if first:
                first_id, first_fields = first[0]
                ts_ms = int(first_id.split("-")[0])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                print(f"  First entry: {first_id}  ({ts.strftime('%Y-%m-%d %H:%M:%S UTC')})")
            if last:
                last_id, last_fields = last[0]
                ts_ms = int(last_id.split("-")[0])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                print(f"  Last entry:  {last_id}  ({ts.strftime('%Y-%m-%d %H:%M:%S UTC')})")

            # Show consumer groups
            groups = await r.xinfo_groups(STREAM_KEY)
            if groups:
                print(f"\n  Consumer groups ({len(groups)}):")
                for g in groups:
                    print(f"    {g.get('name','?')}: "
                          f"consumers={g.get('consumers',0)}, "
                          f"pending={g.get('pending',0)}, "
                          f"last-delivered-id={g.get('last-delivered-id','?')}")
            else:
                print(f"\n  No consumer groups (downstream uses XREAD without a group)")

    finally:
        await r.aclose()


async def cmd_read(args):
    """Read entries from the stream."""
    r = await get_redis()
    try:
        length = await r.xlen(STREAM_KEY)
        if length == 0:
            print(f"✗ Stream '{STREAM_KEY}' is empty (no entries).")
            return

        if args.since:
            # Parse time suffix (e.g., "1h", "30m", "2h")
            s = args.since.lower()
            if s.endswith("h"):
                hours = int(s[:-1])
                since_ms = int((time.time() - hours * 3600) * 1000)
            elif s.endswith("m"):
                minutes = int(s[:-1])
                since_ms = int((time.time() - minutes * 60) * 1000)
            else:
                print(f"Invalid --since format: {args.since} (use like '1h' or '30m')")
                return
            entries = await r.xrange(STREAM_KEY, min=f"{since_ms}-0", count=args.count)
            print(f"Reading entries from the last {args.since} (max {args.count}):")
        else:
            entries = await r.xrevrange(STREAM_KEY, count=args.count)
            print(f"Latest {len(entries)} entries (newest first):")

        if not entries:
            print("  (no entries matched)")
            return

        for entry_id, fields in entries:
            print(format_entry(entry_id, fields))
            print()

    finally:
        await r.aclose()


async def cmd_tail(args):
    """Tail the stream in real-time (like tail -f)."""
    r = await get_redis()
    try:
        # Start from the latest entry
        last_id = "$"
        print(f"Tailing stream '{STREAM_KEY}' in real-time (Ctrl+C to stop)...")
        print()

        while True:
            # XREAD BLOCK 5000ms (5s timeout) starting from last_id
            result = await r.xread({STREAM_KEY: last_id}, block=5000, count=100)
            if result:
                for _stream, entries in result:
                    for entry_id, fields in entries:
                        print(format_entry(entry_id, fields))
                        print()
                        last_id = entry_id
            # No output — loop and block again

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        await r.aclose()


async def cmd_publish_test(args):
    """Publish a test entry to the stream."""
    r = await get_redis()
    try:
        test_payload = {
            "type": "test_signal",
            "symbol": args.symbol or "TEST",
            "ts": int(time.time() * 1000),
            "qualified": True,
            "signal": "buy",
            "regime": "test_regime",
            "regime_conf": 0.99,
            "kelly_f": 0.05,
            "ev": 1.5,
            "rr": 2.0,
            "p_timesfm": 0.65,
            "asset_class": "crypto",
            "venue": "test",
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "_test": True,
            "_message": "This is a test entry from scripts/test_signal_stream.py",
        }
        entry_id = await r.xadd(
            STREAM_KEY,
            {"payload": json.dumps(test_payload)},
            maxlen=10000,
            approximate=True,
        )
        print(f"✓ Published test entry to '{STREAM_KEY}'")
        print(f"  Entry ID: {entry_id}")
        print(f"  Symbol:   {test_payload['symbol']}")
        print(f"  Signal:   {test_payload['signal']}")
        print()
        print("Your downstream consumer should now see this entry.")
        print("If it doesn't, check:")
        print("  1. Is your consumer reading from the same Redis instance?")
        print("  2. Is your consumer using XREAD or XREADGROUP (not SUBSCRIBE)?")
        print("  3. Is your consumer reading from the stream key 'signal.raw.noble_trader'?")
        print("  4. Is your consumer's last-delivered-id correct (not stuck at $)?")

    finally:
        await r.aclose()


async def cmd_groups(args):
    """Show consumer groups and their status."""
    r = await get_redis()
    try:
        exists = await r.exists(STREAM_KEY)
        if not exists:
            print(f"✗ Stream '{STREAM_KEY}' does not exist.")
            return

        groups = await r.xinfo_groups(STREAM_KEY)
        if not groups:
            print(f"Stream '{STREAM_KEY}' has no consumer groups.")
            print()
            print("If your downstream uses XREAD without a group, that's fine.")
            print("If your downstream uses XREADGROUP, it needs to create a group first:")
            print(f"  XGROUP CREATE signal.raw.noble_trader <group-name> $ MKSTREAM")
            return

        print(f"Consumer groups for '{STREAM_KEY}' ({len(groups)} total):")
        print()
        for g in groups:
            name = g.get("name", "?")
            consumers = g.get("consumers", 0)
            pending = g.get("pending", 0)
            last_id = g.get("last-delivered-id", "?")
            print(f"  Group: {name}")
            print(f"    Consumers:      {consumers}")
            print(f"    Pending (ACK):  {pending}")
            print(f"    Last delivered: {last_id}")
            print()

            # Show individual consumers in this group
            if consumers > 0:
                consumer_info = await r.xinfo_consumers(STREAM_KEY, name)
                for c in consumer_info:
                    cname = c.get("name", "?")
                    idle = c.get("idle", 0)
                    idle_s = idle / 1000
                    cpending = c.get("pending", 0)
                    print(f"      Consumer: {cname}  idle={idle_s:.0f}s  pending={cpending}")
                print()

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await r.aclose()


async def cmd_read_group(args):
    """Read entries from a consumer group."""
    r = await get_redis()
    try:
        # Try to read from the group
        result = await r.xreadgroup(
            groupname=args.group,
            consumername=args.consumer or "diagnostic-cli",
            streams={STREAM_KEY: ">"},
            count=args.count,
        )
        if not result:
            print(f"No new entries for group '{args.group}'.")
            print("This means the group has already consumed all entries up to the latest.")
            print()
            print("To re-read entries, use XREAD without a group:")
            print(f"  python3 {sys.argv[0]} read --count {args.count}")
            return

        for _stream, entries in result:
            for entry_id, fields in entries:
                print(format_entry(entry_id, fields))
                print()

        # Acknowledge the entries so they don't show as pending
        entry_ids = [eid for _s, entries in result for eid, _f in entries]
        await r.xack(STREAM_KEY, args.group, *entry_ids)
        print(f"✓ Read and ACKed {len(entry_ids)} entries from group '{args.group}'")

    except Exception as e:
        print(f"Error: {e}")
        print()
        print("If the group doesn't exist, create it first:")
        print(f"  python3 {sys.argv[0]} create-group --group {args.group}")
    finally:
        await r.aclose()


async def cmd_create_group(args):
    """Create a consumer group for the stream."""
    r = await get_redis()
    try:
        # id="$" means "start reading from new entries only (not historical)"
        # mkstream=True creates the stream if it doesn't exist yet
        # The group is created ONCE and persists in Redis until deleted.
        await r.xgroup_create(
            STREAM_KEY,
            args.group,
            id=args.start or "$",
            mkstream=True,
        )
        print(f"✓ Created consumer group '{args.group}' on stream '{STREAM_KEY}'")
        print(f"  Start position: {args.start or '$ (new entries only)'}")
        print()
        print("Your downstream consumer can now use XREADGROUP:")
        print(f"  XREADGROUP GROUP {args.group} <consumer-name> COUNT 10 BLOCK 5000 \\")
        print(f"    STREAMS {STREAM_KEY} >")
        print()
        print("To read from the beginning of the stream instead of new-only,")
        print(f"  delete and recreate with --start 0:")
        print(f"  python3 {sys.argv[0]} destroy-group --group {args.group}")
        print(f"  python3 {sys.argv[0]} create-group --group {args.group} --start 0")

    except Exception as e:
        err_str = str(e)
        if "BUSYGROUP" in err_str:
            print(f"✓ Consumer group '{args.group}' already exists (normal on restart)")
            print("  No action needed — the group is ready for use.")
        else:
            print(f"✗ Error creating group: {e}")
    finally:
        await r.aclose()


async def cmd_destroy_group(args):
    """Delete a consumer group."""
    r = await get_redis()
    try:
        await r.xgroup_destroy(STREAM_KEY, args.group)
        print(f"✓ Destroyed consumer group '{args.group}' on stream '{STREAM_KEY}'")
        print("  All pending (unacked) entries are now unreadable.")
        print("  Recreate with: create-group --group <name>")
    except Exception as e:
        print(f"✗ Error destroying group: {e}")
    finally:
        await r.aclose()


async def cmd_pending(args):
    """Show pending (unacked) entries for a consumer group."""
    r = await get_redis()
    try:
        info = await r.xpending_range(STREAM_KEY, args.group, min="-", max="+", count=args.count)
        if not info:
            print(f"No pending (unacked) entries for group '{args.group}'.")
            print("All read entries have been acknowledged (XACK).")
            return

        print(f"Pending (unacked) entries for group '{args.group}' ({len(info)} total):")
        print()
        for p in info:
            entry_id = p.get("message_id", "?")
            consumer = p.get("consumer", "?")
            idle_ms = p.get("time_since_delivered", 0)
            deliveries = p.get("times_delivered", 0)
            ts_ms = int(entry_id.split("-")[0]) if "-" in entry_id else 0
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
            print(f"  Entry ID: {entry_id}")
            print(f"    Consumer:    {consumer}")
            print(f"    Idle:        {idle_ms / 1000:.0f}s")
            print(f"    Deliveries:  {deliveries} (re-sent count)")
            if ts:
                print(f"    Originally:  {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print()

    except Exception as e:
        print(f"Error: {e}")
        print()
        print("If the group doesn't exist, create it first:")
        print(f"  python3 {sys.argv[0]} create-group --group {args.group}")
    finally:
        await r.aclose()


async def main():
    parser = argparse.ArgumentParser(
        description="Redis Streams diagnostic for signal.raw.noble_trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="Show stream info: exists, length, first/last entry, consumer groups")

    p_read = sub.add_parser("read", help="Read entries from the stream")
    p_read.add_argument("--count", type=int, default=5, help="Max entries to read (default: 5)")
    p_read.add_argument("--since", help="Only entries from the last N time (e.g., '1h', '30m')")

    sub.add_parser("tail", help="Tail the stream in real-time (like tail -f)")

    p_pub = sub.add_parser("publish-test", help="Publish a test entry to verify downstream consumer")
    p_pub.add_argument("--symbol", default="TEST", help="Symbol to use in test payload")

    sub.add_parser("groups", help="Show consumer groups and their status")

    p_rg = sub.add_parser("read-group", help="Read from a consumer group")
    p_rg.add_argument("--group", required=True, help="Consumer group name")
    p_rg.add_argument("--consumer", help="Consumer name (default: diagnostic-cli)")
    p_rg.add_argument("--count", type=int, default=5, help="Max entries to read")

    p_cg = sub.add_parser("create-group", help="Create a consumer group (one-time setup)")
    p_cg.add_argument("--group", required=True, help="Consumer group name (e.g., 'trading-bot')")
    p_cg.add_argument("--start", help="Start position: '$' for new-only (default), '0' for from beginning")

    p_dg = sub.add_parser("destroy-group", help="Delete a consumer group")
    p_dg.add_argument("--group", required=True, help="Consumer group name to destroy")

    p_pd = sub.add_parser("pending", help="Show pending (unacked) entries for a consumer group")
    p_pd.add_argument("--group", required=True, help="Consumer group name")
    p_pd.add_argument("--count", type=int, default=20, help="Max entries to show")

    args = parser.parse_args()

    commands = {
        "info": cmd_info,
        "read": cmd_read,
        "tail": cmd_tail,
        "publish-test": cmd_publish_test,
        "groups": cmd_groups,
        "read-group": cmd_read_group,
        "create-group": cmd_create_group,
        "destroy-group": cmd_destroy_group,
        "pending": cmd_pending,
    }
    await commands[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
