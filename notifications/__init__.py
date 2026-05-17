"""
Notifications Package — Discord webhook integration for the Noble Trader Agent.

Channels:
  - #trade-signals   → Renko pattern signals (bull run, bear run, reversal, etc.)
  - #trade-executions → Alpaca order fills, SL/TP hits
  - #system-status   → Warmup completions, pipeline resets, errors, health

Usage::

    from notifications import discord_notifier

    # Send a signal alert (auto-routes to #trade-signals)
    await discord_notifier.send_signal(symbol="SPY", direction="LONG",
                                       pattern="bull_run", price=450.25,
                                       confidence=0.85)

    # Send a trade execution alert (auto-routes to #trade-executions)
    await discord_notifier.send_execution(symbol="SPY", side="buy",
                                          qty=10, price=450.25,
                                          order_id="abc-123")

    # Send a system status alert (auto-routes to #system-status)
    await discord_notifier.send_status(message="Pipeline warmup complete",
                                       symbol="SPY", level="info")
"""
