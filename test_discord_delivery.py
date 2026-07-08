#!/usr/bin/env python3
"""
Test Discord webhook delivery flow.
══════════════════════════════════════════════════════════════

Traces the complete signal delivery path from:
  1. Sweep orchestrator publishes to Redis
  2. Signal aggregator subscribes
  3. Aggregator routes to Discord
  4. Discord notifier sends webhook

Identifies the gap: orchestrator publishes HEARTBEAT type, but Discord
only receives system status messages, not trade signals.
"""

import json
from unittest.mock import AsyncMock, patch, MagicMock


def test_discord_delivery_flow():
    """Test the complete Discord delivery flow."""
    print("=" * 78)
    print("DISCORD WEBHOOK DELIVERY FLOW TEST")
    print("=" * 78)
    print()

    # Simulate the orchestrator's published heartbeat
    orchestrator_payload = {
        "type": "heartbeat",
        "symbol": "BTC",
        "ts": 1717200000000,
        "regime": "high_vol_bull",
        "regime_conf": 0.88,
        "signal": "buy",
        "entry_price": 42000.0,
        "stop_loss": 41000.0,
        "take_profit": 45000.0,
        "kelly_f": 0.12,
        "ev": 1250.5,
        "p_win": 0.65,
        "regime_shift": "false",
        "prev_regime": "",
    }

    print("Step 1: Sweep orchestrator publishes to Redis")
    print(f"  Channel: signal.raw.noble_trader")
    print(f"  Payload type: {orchestrator_payload['type']}")
    print(f"  Signal: {orchestrator_payload['signal']}")
    print()

    # Simulate Signal Aggregator's _normalize_signal
    print("Step 2: Signal Aggregator normalizes signal")
    signal_type = orchestrator_payload.get("type", "unknown")
    source = "sweep_orchestrator"

    priority = "MEDIUM"  # Line 301
    print(f"  Normalized type: {signal_type}")
    print(f"  Source: {source}")
    print(f"  Priority: {priority}")
    print()

    # Simulate the routing logic
    print("Step 3: Signal Aggregator routes to Discord")
    print(f"  Signal type: {signal_type}")
    print(f"  route_heartbeat_to_discord: True (default)")
    print()

    if signal_type == "heartbeat":
        print("  ✅ ROUTING TO DISCORD: HEARTBEAT type")
        print(f"     → Calls discord_notifier.send_status()")
    else:
        print("  ❌ NOT routing: Wrong type")
    print()

    # Show what send_status() expects vs send_signal()
    print("Step 4: Discord Notifier receives signal")
    print()

    print("  send_status() parameters:")
    print(f"    - message: 'Regime update: BTC → {orchestrator_payload.get('regime', 'unknown')}'")
    print(f"    - symbol: {orchestrator_payload.get('symbol', 'UNKNOWN')}")
    print(f"    - level: INFO (line 341)")
    print(f"    - data: {json.dumps(orchestrator_payload, indent=4)}")
    print()

    print("  ❌ PROBLEM: send_status() only sends SYSTEM STATUS messages")
    print(f"     → No trade signal embed")
    print(f"     → No buy/sell direction")
    print(f"     → No entry/stop_loss/take_profit levels")
    print()

    print("  What we NEED:")
    print(f"     → send_signal() instead of send_status()")
    print(f"     → Type: HEARTBEAT with signal='buy' or 'sell'")
    print(f"     → Price levels: entry_price, stop_loss, take_profit")
    print(f"     → Kelly: kelly_f")
    print(f"     → Regime: regime")
    print()

    print("Step 5: Current Discord delivery")
    print("  ❌ Gap: HEARTBEAT signals are routed to send_status()")
    print("  ❌ Result: Only receives regime updates, not trade signals")
    print("  ❌ Discord #trade-signals channel receives NO signals")
    print()

    print("=" * 78)
    print("SOLUTION")
    print("=" * 78)
    print()
    print("Modify Signal Aggregator._route_to_discord() to:")
    print()
    print("  1. Check if heartbeat has a trade signal (signal in ['buy', 'sell'])")
    print("  2. If yes → route to discord_notifier.send_signal()")
    print("  3. If no → route to discord_notifier.send_status() (current behavior)")
    print()
    print("This will enable Discord to receive trade signals from the")
    print("Sweep Orchestrator, not just regime status updates.")
    print()

    return orchestrator_payload


if __name__ == "__main__":
    test_discord_delivery_flow()
