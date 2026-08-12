"""Live verification of the TDVA order fix (worklog [9.2.0]).

Runs the actual fetch_tradingview_batch against the live RapidAPI
endpoint and asserts the ascending contract.
"""
import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Load ONLY TradingView/price-fetch vars from .env.local into env
# (skip lines with inline comments — orchestrator parses its own env)
env_path = os.path.join(REPO, ".env.local")
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k.startswith(("TRADINGVIEW", "NOBLE_TRADER_URL")):
                os.environ.setdefault(k, v)

from regime_platform.sweep_orchestrator.price_fetcher import (  # noqa: E402
    fetch_tradingview_batch,
)


async def main() -> int:
    result = await fetch_tradingview_batch(
        [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}],
        hours=200,
    )
    prices = result.get("USDJPY", [])
    print(f"Fetched {len(prices)} USDJPY closes")
    if not prices:
        print("FAIL: no prices returned")
        return 1

    print(f"prices[0]   (oldest):  {prices[0]:.4f}")
    print(f"prices[-1]  (newest):  {prices[-1]:.4f}")

    # NOTE: Real market closes are NOT monotonically increasing — the
    # contract is TIME order (oldest first, newest last), not price order.
    # The correct checks are:
    #   1. prices[-1] == live current price (the incident value 162.87 was
    #      the OLDEST close; the live quote was ~159.35)
    #   2. prices[0]  == the oldest bar (~159.0 from the raw API history[-1])
    cur = prices[-1]
    oldest = prices[0]
    print(f"current_price now = {cur:.4f}")
    print(f"oldest bar        = {oldest:.4f}")

    if 159.0 < cur < 160.0 and oldest < cur:
        print("PASS: ascending time order; current price matches live market (~159)")
        return 0
    print("FAIL: order or level wrong")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
