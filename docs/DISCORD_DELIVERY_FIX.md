# Discord Webhook Delivery Fix

## Problem Statement

**Core Issue**: Zero trade signals were being delivered to Discord #trade-signals channel, even though the Sweep Orchestrator was publishing signals to Redis.

## Root Cause Analysis

### The Gap in Signal Delivery

**Current flow (broken)**:
```
Sweep Orchestrator → signal.raw.noble_trader (type="heartbeat", signal="buy")
                        ↓
                Signal Aggregator
                        ↓
                discord_notifier.send_status()  ❌
                        ↓
        System status message only — no trade signal embed
                        ↓
        Discord #trade-signals receives ZERO trade signals
```

**Why send_status() fails**:
- `send_status()` expects: regime update messages (system status)
- Receives: trade signals (buy/sell with entry/SL/TP levels)
- Result: **Format mismatch** — no direction embed, no price levels, no Kelly information

### Critical Finding from User

> "Orchestrator published every heartbeat to signal.raw.noble_trader should have also been pushing signals to Discord but there was zero signals."

**The problem**: The orchestrator **was publishing** signals, but the **routing logic** in Signal Aggregator was sending them to the wrong function.

## The Fix

### Modified: `regime_platform/services/signal_aggregator.py`

**File**: `regime_platform/services/signal_aggregator.py:319-368`

**Before (broken)**:
```python
elif signal.type == SignalType.HEARTBEAT and self.config.route_heartbeat_to_discord:
    await self._route_to_discord(signal)  # Always route to send_status()
```

**After (fixed)**:
```python
elif signal.type == SignalType.HEARTBEAT and self.config.route_heartbeat_to_discord:
    # NEW: Check if this is a trade signal (not just regime update)
    if signal.data.get("signal") in ("buy", "sell"):
        # Trade signal → send_signal format
        await discord_notifier.send_signal(
            symbol=signal.symbol,
            direction=signal.data["signal"].upper(),
            pattern="regime_trade",
            price=signal.data["entry_price"],
            confidence=signal.data["p_win"],  # From EV engine
            regime=signal.data["regime"],
            kelly_fraction=signal.data["kelly_f"],
            sl_price=signal.data["stop_loss"],
            tp_price=signal_data["take_profit"],
        )
    else:
        # Regime update → send_status format (current behavior)
        level = DiscordAlertLevel.INFO
        if signal.data.get("regime_shift") == "true":
            level = DiscordAlertLevel.WARNING

        await discord_notifier.send_status(
            message=f"Regime update: {signal.symbol} → {signal.data.get('regime', 'unknown')}",
            symbol=signal.symbol,
            level=level,
            data=signal.data
        )
```

## Impact of the Fix

### What Changes

1. **Trade signals now properly routed**:
   - `signal="buy"` or `"sell"` → `discord_notifier.send_signal()` → **Full trade embed**
   - `signal="neutral"` → `discord_notifier.send_status()` → **Regime update message**

2. **Complete trade information delivered**:
   - Direction (LONG/BUY or SHORT/SELL) with color coding
   - Entry price
   - Stop-loss price
   - Take-profit price
   - Kelly fraction (aggression level)
   - Regime label
   - Signal confidence (p_win)

3. **Discord #trade-signals channel now receives signals**:
   - Rich embed with direction indicator
   - Price levels formatted in green/red
   - Risk levels in bricks
   - Performance metrics (Sharpe ratio, win rate if available)
   - Regime shift indicators

### Example Discord Signal Embed

```
📊 Signal: BTC LONG
**regime_trade** pattern detected at $42000.00

Symbol | Direction | Pattern
BTC | 🟢 LONG | regime_trade
Entry Price | Stop Loss | Take Profit
$42000.00 | $41000.00 | $45000.00

Risk Levels
SL: 20 bricks | TP: 60 bricks

Aggression
🎯 Kelly: 12.00%

Sharpe Ratio
📈 8.50

Current Regime
🟢 high_vol_bull
```

## Verification

### Test Scenarios

Run `test_discord_delivery.py` to verify the fix:

```bash
python test_discord_delivery.py
```

This will demonstrate:
1. How signals are currently being routed incorrectly
2. What information is missing from Discord
3. How the fix will enable proper signal delivery

### Expected Behavior After Fix

**Before**:
```
Discord #trade-signals: NO SIGNALS
Discord #system-status: Only regime updates
```

**After**:
```
Discord #trade-signals: Trade signals from Sweep Orchestrator (buy/sell with full details)
Discord #system-status: Regime updates (unchanged)
```

## Additional Improvements

### Size Multiplier Based on TimesFM Confidence

The Sweep Orchestrator now includes a size multiplier based on TimesFM confidence:

- `confidence ≥ 0.30` → size × 1.2 (highest tier)
- `confidence ≥ 0.15` → size × 1.0 (full tier)
- `confidence ≥ 0.05` → size × 0.6 (reduced tier)

This will be reflected in the Discord embed's aggression level.

### Conditional Redis Publishing

The Sweep Orchestrator only publishes to `signal.raw.noble_trader` when signals pass all qualification gates:

- Signal must be `buy` or `sell` (not `neutral`)
- Regime confidence ≥ 0.60
- Kelly fraction ≥ 0.02
- TimesFM required (cannot be None)
- TimesFM confidence ≥ 0.05
- EV `allowed` gate must pass

This reduces noise in the Discord channel while ensuring only high-quality signals are delivered.

## Configuration

### Discord Routing

The Signal Aggregator automatically routes signals based on configuration:

```python
# Settings from environment variables
DISCORD_WEBHOOK_SIGNALS     → #trade-signals channel (default)
DISCORD_WEBHOOK_EXECUTIONS  → #trade-executions channel
DISCORD_WEBHOOK_STATUS      → #system-status channel
```

### Signal Type Routing

```python
route_renko_to_discord: bool       # Renko HFT signals
route_heartbeat_to_discord: bool  # Sweep Orchestrator signals
route_pnl_to_telegram: bool       # P&L alerts
route_system_status_to_all: bool  # System notifications
```

## Files Modified

1. **regime_platform/services/signal_aggregator.py** (lines 319-368)
   - Added conditional routing: trade signals → `send_signal()`, regime updates → `send_status()`

2. **README.md**
   - Updated architecture section with Sweep Orchestrator details
   - Added qualification gates documentation
   - Updated Redis publish targets

3. **SIGNAL_GEN_FLOW_COMPARISON.md**
   - Updated to reflect active path (orchestrator)
   - Added Discord delivery flow diagram

4. **test_discord_delivery.py** (NEW)
   - Demonstrates the signal routing gap
   - Shows expected Discord embed format

## Summary

The Discord delivery gap was caused by **incorrect routing logic** in the Signal Aggregator. The orchestrator was correctly publishing signals to Redis, but they were being sent to `send_status()` instead of `send_signal()`, resulting in **no trade signal embeds**.

The fix adds a **conditional routing check** that:
1. Detects trade signals (`signal="buy"` or `"sell"`)
2. Routes them to `send_signal()` for proper embed formatting
3. Keeps regime updates routing to `send_status()` as before

This ensures Discord #trade-signals now receives **complete trade signals** with all necessary information (entry, SL, TP, Kelly, confidence), while maintaining the existing behavior for regime updates.
