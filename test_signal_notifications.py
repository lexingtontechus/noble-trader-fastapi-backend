#!/usr/bin/env python3
"""
Test script for signal notification services.
This script verifies that all services can be imported and started without errors.
"""

import asyncio
import sys
import os

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

async def test_services():
    """Test all signal notification services."""
    print("🧪 Testing signal notification services...")
    
    # Test 1: Redis configuration
    print("\n1. Testing Redis configuration...")
    try:
        from regime_platform.core.settings import settings
        if settings.redis_url_effective:
            print(f"✅ Redis URL configured: {settings.redis_url_effective[:30]}...")
        else:
            print("❌ No Redis URL configured")
            print("   Set REDIS_URL or UPSTASH_REDIS_REST_URL")
    except Exception as e:
        print(f"❌ Redis configuration error: {e}")
    
    # Test 2: Signal aggregator
    print("\n2. Testing Signal Aggregator...")
    try:
        from regime_platform.services.signal_aggregator import get_signal_aggregator
        aggregator = await get_signal_aggregator()
        print("✅ Signal Aggregator started successfully")
        await aggregator.stop()
        print("✅ Signal Aggregator stopped successfully")
    except Exception as e:
        print(f"❌ Signal Aggregator error: {e}")
    
    # Test 3: External webhook service
    print("\n3. Testing External Webhook Service...")
    try:
        from regime_platform.services.external_webhook_service import get_external_webhook_service
        webhook_service = await get_external_webhook_service()
        print("✅ External Webhook Service started successfully")
        await webhook_service.stop()
        print("✅ External Webhook Service stopped successfully")
    except Exception as e:
        print(f"❌ External Webhook Service error: {e}")
    
    # Test 4: Telegram service
    print("\n4. Testing Telegram Service...")
    try:
        from notifications.telegram import telegram_notifier
        if telegram_notifier.is_configured:
            # Test with a simple message
            result = await telegram_notifier._send_message("🔔 Test message from signal notification system")
            if result:
                print("✅ Telegram service working")
            else:
                print("⚠️ Telegram service connected but message failed")
        else:
            print("❌ Telegram not configured (missing bot token or chat ID)")
    except Exception as e:
        print(f"❌ Telegram service error: {e}")
    
    # Test 5: Discord service
    print("\n5. Testing Discord Service...")
    try:
        from notifications.discord import discord_notifier
        if discord_notifier.is_configured:
            print("✅ Discord service configured")
        else:
            print("❌ Discord not configured")
    except Exception as e:
        print(f"❌ Discord service error: {e}")
    
    print("\n🎯 Test completed!")

if __name__ == "__main__":
    asyncio.run(test_services())