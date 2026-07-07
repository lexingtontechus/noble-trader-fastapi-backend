# Signal Notifications Setup Guide

This guide explains how to configure and use the enhanced signal notification system in Noble Trader v7.4.

## Overview

The signal notification system provides real-time trading signal delivery through multiple channels:

- **Redis Pub/Sub**: Internal message routing between services
- **Signal Aggregator**: Centralized signal routing and management
- **Discord Webhooks**: Rich formatting for trading signals
- **Telegram Bot**: HTML-formatted messages with emojis
- **External Webhooks**: Forward signals to external platforms (Slack, Teams, etc.)

## Configuration

### 1. Redis Configuration

Required for internal signal routing:

```bash
# For local Redis
export REDIS_URL="redis://localhost:6379"

# For Upstash Redis (managed service)
export UPSTASH_REDIS_REST_URL="https://usw1-smiling-parrot-42069.upstash.io"
export UPSTASH_REDIS_REST_TOKEN="AcXrAAIjc2YxM2M3NzY..."
```

### 2. Discord Webhooks

Configure webhooks for different signal types:

```bash
# Trading signals
export DISCORD_WEBHOOK_SIGNALS="https://discord.com/api/webhooks/12345/abc..."

# Order executions
export DISCORD_WEBHOOK_EXECUTIONS="https://discord.com/api/webhooks/12345/def..."

# System status
export DISCORD_WEBHOOK_STATUS="https://discord.com/api/webhooks/12345/ghi..."
```

### 3. Telegram Bot Configuration

Set up bot token and chat ID:

```bash
# Get bot token from @BotFather
export TELEGRAM_BOT_TOKEN="1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ..."

# Get chat ID by sending a message to your bot and using:
export TELEGRAM_CHAT_ID="123456789"
```

### 4. External Webhook Configuration

For forwarding signals to external platforms:

```bash
# URL to receive signal payloads
export EXTERNAL_SIGNAL_WEBHOOK_URL="https://your-webhook-endpoint.com/signals"
```

## Usage Examples

### Sending a Signal via Redis

Signals are automatically published to Redis when qualified signals are generated:

```python
# This happens automatically in the Renko pipeline
# Signals are published to "signal.raw.noble_trader" channel
```

### Manual Signal Testing

Test the notification system:

```python
import asyncio
from regime_platform.services.signal_aggregator import get_signal_aggregator
from notifications.telegram import telegram_notifier

async def test_notifications():
    # Test Telegram
    await telegram_notifier.send_signal(
        symbol="SPY",
        direction="LONG",
        pattern="bull_run",
        price=450.00,
        confidence=0.85,
        regime="bull_regime",
        position_size_usd=10000,
        kelly_fraction=0.25
    )
    
    # Test signal aggregator
    await get_signal_aggregator()
```

## Message Formats

### Telegram Signal Format

Signals are sent with rich HTML formatting:

```
📈 SPY Trading Signal
🐂 Bull Run

💰 Price: $450.00
📊 Confidence: 🟢 85%
🎯 Direction: LONG
🏛️ Regime: 🟢 bull_regime
💸 Position: $10,000
🎲 Kelly F: 🟢 0.250
⚡ Sizing: Dynamic Masaniello
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>Noble Trader Dynamic Regime Platform</i>
```

### Discord Signal Format

Discord messages include embeds with color coding:

- 🔴 RED for short/SELL signals
- 🟢 GREEN for long/BUY signals
- 🟡 YELLOW for warning/neutral signals

### External Webhook Format

JSON payload with standardized fields:

```json
{
  "type": "trading_signal",
  "symbol": "SPY",
  "direction": "LONG",
  "pattern": "bull_run",
  "price": 450.00,
  "confidence": 0.85,
  "regime": "bull_regime",
  "position_size_usd": 10000,
  "kelly_fraction": 0.25,
  "timestamp": "2024-01-01T12:00:00Z",
  "source": "noble_trader"
}
```

## Rate Limiting

Services implement intelligent rate limiting:

- **Telegram**: 30 messages per minute
- **Discord**: 1 webhook per second per channel
- **External Webhooks**: Configurable, default 10/minute

## Monitoring and Logging

Check system status:

```bash
# Check running services
curl http://localhost:8000/health

# Check logs for signal delivery
tail -f logs/app.log | grep -i "signal.*telegram\|signal.*discord"
```

## Troubleshooting

### Common Issues

1. **Redis Connection Failed**
   - Check REDIS_URL or Upstash credentials
   - Verify Redis service is running

2. **Telegram Bot Not Working**
   - Verify bot token is valid
   - Check chat ID (must have started a chat with the bot)
   - Rate limiting may temporarily block messages

3. **Discord Webhook Failing**
   - Verify webhook URLs are valid
   - Check Discord channel permissions
   - Webhook may be deleted/deactivated

4. **External Webhooks Not Delivering**
   - Check URL accessibility
   - Verify endpoint accepts POST requests
   - Check authentication requirements

### Debug Mode

Enable debug logging:

```bash
export LOG_LEVEL=DEBUG
python -m uvicorn main:app --log-level debug
```

## Advanced Configuration

### Custom Payload Transformers

Extend the external webhook service:

```python
class CustomPayloadTransformer:
    def transform(self, signal_data):
        # Add custom fields
        signal_data["custom_field"] = "value"
        return signal_data
```

### Signal Filtering

Configure signal routing:

```python
# Only forward specific symbols
SIGNAL_FILTER_SYMBOLS = ["SPY", "QQQ"]

# Only forward high-confidence signals
SIGNAL_FILTER_CONFIDENCE = 0.8
```

## Security Considerations

- Store webhook URLs and tokens securely
- Use HTTPS for external webhooks
- Validate webhook payloads
- Monitor for abuse and unauthorized access

## Version History

- **v7.4**: Initial implementation of unified signal notification system
- Enhanced Redis pub/sub integration
- Added Telegram signal formatting
- Added external webhook forwarding
- Added comprehensive monitoring and logging