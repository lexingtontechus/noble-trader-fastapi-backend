import os
import asyncio
import sys

# Add the current directory to the Python path so we can import regime_platform
sys.path.insert(0, os.getcwd())

from regime_platform.core.settings import settings
import redis.asyncio as redis

async def test():
    url = settings.redis_url_effective
    print(f"Redis URL: {url}")
    if not url:
        print("No Redis URL configured")
        return
    try:
        r = redis.from_url(url)
        await r.ping()
        print("Redis ping successful")
    except Exception as e:
        print(f"Redis ping failed: {e}")
    finally:
        await r.close()

if __name__ == "__main__":
    asyncio.run(test())
