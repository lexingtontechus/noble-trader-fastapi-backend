"""
Tests for the backtest_cache service - Redis-backed caching with 1h TTL.

Covers:
  - Cache key generation (deterministic hashing)
  - Cache MISS when no Redis / empty cache
  - Cache HIT when data exists
  - Graceful fallback when Redis is unavailable
  - TTL and invalidation
  - Cache stats endpoint
  - Integration: RenkoBacktestResponse.cached field
"""

import pytest
import json
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from regime_platform.services.backtest_cache import (
    BacktestCache,
    _make_cache_key,
    KEY_PREFIX,
    DEFAULT_TTL,
)


# ─── Cache Key Tests ────────────────────────────────────────────────────────


class TestCacheKey:
    """Test deterministic cache key generation."""

    def test_deterministic_key(self):
        """Same input should always produce the same key."""
        body = {"symbol": "SPY", "brick_size": 0.5, "sl_bricks": 3}
        key1 = _make_cache_key("renko", body)
        key2 = _make_cache_key("renko", body)
        assert key1 == key2

    def test_different_params_different_key(self):
        """Different params should produce different keys."""
        body_a = {"symbol": "SPY", "brick_size": 0.5}
        body_b = {"symbol": "SPY", "brick_size": 0.25}
        key_a = _make_cache_key("renko", body_a)
        key_b = _make_cache_key("renko", body_b)
        assert key_a != key_b

    def test_different_prefix_different_key(self):
        """Different prefix (e.g. renko vs hmm) should produce different keys."""
        body = {"symbol": "SPY", "brick_size": 0.5}
        key_renko = _make_cache_key("renko", body)
        key_hmm = _make_cache_key("hmm", body)
        assert key_renko != key_hmm

    def test_key_format(self):
        """Key should follow the format 'bt:{prefix}:{sha256_hash}'."""
        body = {"symbol": "SPY"}
        key = _make_cache_key("renko", body)
        parts = key.split(":")
        assert parts[0] == KEY_PREFIX
        assert parts[1] == "renko"
        assert len(parts[2]) == 64  # SHA-256 hex digest length

    def test_prices_replaced_with_length(self):
        """Large price arrays should be replaced with their length for stable hashing."""
        body = {"symbol": "SPY", "prices": [100.0] * 500}
        key1 = _make_cache_key("renko", body)

        # Same length, same other params → same key
        body2 = {"symbol": "SPY", "prices": [200.0] * 500}
        key2 = _make_cache_key("renko", body2)
        assert key1 == key2  # Same length, same symbol

        # Different length → different key
        body3 = {"symbol": "SPY", "prices": [100.0] * 300}
        key3 = _make_cache_key("renko", body3)
        assert key1 != key3

    def test_key_order_independent(self):
        """Key should be the same regardless of dict key order."""
        body_a = {"symbol": "SPY", "brick_size": 0.5}
        body_b = {"brick_size": 0.5, "symbol": "SPY"}
        key_a = _make_cache_key("renko", body_a)
        key_b = _make_cache_key("renko", body_b)
        assert key_a == key_b  # sorted keys → deterministic

    def test_does_not_mutate_input(self):
        """_make_cache_key should NOT mutate the input dict."""
        body = {"symbol": "SPY", "prices": [1.0, 2.0, 3.0]}
        original = dict(body)
        _make_cache_key("renko", body)
        assert body == original


# ─── Cache Disabled (No REDIS_URL) ──────────────────────────────────────────


class TestCacheDisabled:
    """Test cache behavior when REDIS_URL is not configured."""

    def test_disabled_by_default(self):
        """Cache should be disabled when no REDIS_URL is provided."""
        cache = BacktestCache(redis_url="", ttl=DEFAULT_TTL)
        assert cache.enabled is False

    @pytest.mark.asyncio
    async def test_get_returns_none_when_disabled(self):
        """Cache GET should return None when disabled."""
        cache = BacktestCache(redis_url="")
        result = await cache.get("renko", {"symbol": "SPY"})
        assert result is None

    @pytest.mark.asyncio
    async def test_set_returns_false_when_disabled(self):
        """Cache SET should return False when disabled."""
        cache = BacktestCache(redis_url="")
        result = await cache.set("renko", {"symbol": "SPY"}, {"data": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_invalidate_returns_false_when_disabled(self):
        """Cache INVALIDATE should return False when disabled."""
        cache = BacktestCache(redis_url="")
        result = await cache.invalidate("renko", {"symbol": "SPY"})
        assert result is False

    @pytest.mark.asyncio
    async def test_stats_returns_disabled(self):
        """Cache STATS should show disabled when no Redis."""
        cache = BacktestCache(redis_url="")
        stats = await cache.get_stats()
        assert stats["enabled"] is False
        assert stats["keys"] == 0


# ─── Cache Enabled (Mocked Redis) ──────────────────────────────────────────


class TestCacheEnabled:
    """Test cache behavior with a mocked Redis connection."""

    def _make_cache_with_mock(self) -> BacktestCache:
        """Create a cache instance that pretends to be enabled."""
        cache = BacktestCache.__new__(BacktestCache)
        cache._ttl = DEFAULT_TTL
        cache._enabled = True
        cache._url = "redis://localhost:6379"
        cache._redis = None
        return cache

    @pytest.mark.asyncio
    async def test_get_cache_hit(self):
        """GET should return cached data when key exists in Redis."""
        cache = self._make_cache_with_mock()

        # Mock the Redis client
        mock_client = AsyncMock()
        mock_client.get.return_value = json.dumps({"symbol": "SPY", "total_trades": 10})
        cache._redis = mock_client

        result = await cache.get("renko", {"symbol": "SPY"})
        assert result is not None
        assert result["symbol"] == "SPY"
        assert result["total_trades"] == 10

    @pytest.mark.asyncio
    async def test_get_cache_miss(self):
        """GET should return None when key does not exist in Redis."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.get.return_value = None
        cache._redis = mock_client

        result = await cache.get("renko", {"symbol": "AAPL"})
        assert result is None

    @pytest.mark.asyncio
    async def test_set_stores_data(self):
        """SET should store data in Redis with TTL."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.set.return_value = True
        cache._redis = mock_client

        success = await cache.set("renko", {"symbol": "SPY"}, {"symbol": "SPY", "trades": 5})
        assert success is True
        # Verify set was called with TTL
        mock_client.set.assert_called_once()
        call_args = mock_client.set.call_args
        assert call_args[1]["ex"] == DEFAULT_TTL  # TTL should be 3600

    @pytest.mark.asyncio
    async def test_invalidate_removes_key(self):
        """INVALIDATE should delete the key from Redis."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.delete.return_value = 1
        cache._redis = mock_client

        result = await cache.invalidate("renko", {"symbol": "SPY"})
        assert result is True
        mock_client.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalidate_key_not_found(self):
        """INVALIDATE should return False when key doesn't exist."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.delete.return_value = 0
        cache._redis = mock_client

        result = await cache.invalidate("renko", {"symbol": "AAPL"})
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_prefix(self):
        """CLEAR_PREFIX should delete all keys matching the prefix."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.keys.return_value = ["bt:renko:abc123", "bt:renko:def456"]
        mock_client.delete.return_value = 2
        cache._redis = mock_client

        count = await cache.clear_prefix("renko")
        assert count == 2

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """STATS should return key counts and memory info."""
        cache = self._make_cache_with_mock()

        mock_client = AsyncMock()
        mock_client.keys.side_effect = [
            ["bt:renko:a", "bt:renko:b"],  # renko keys
            ["bt:hmm:c"],  # hmm keys
        ]
        mock_client.info.return_value = {"used_memory_human": "1.5M"}
        cache._redis = mock_client

        stats = await cache.get_stats()
        assert stats["enabled"] is True
        assert stats["renko_keys"] == 2
        assert stats["hmm_keys"] == 1
        assert stats["total_keys"] == 3
        assert stats["ttl_seconds"] == DEFAULT_TTL


# ─── Graceful Fallback Tests ────────────────────────────────────────────────


class TestGracefulFallback:
    """Test that Redis failures never propagate to the caller."""

    def _make_cache_with_broken_redis(self) -> BacktestCache:
        """Create a cache instance with a Redis client that always fails."""
        cache = BacktestCache.__new__(BacktestCache)
        cache._ttl = DEFAULT_TTL
        cache._enabled = True
        cache._url = "redis://broken:6379"
        cache._redis = None
        return cache

    @pytest.mark.asyncio
    async def test_get_handles_redis_error(self):
        """GET should return None (cache miss) when Redis throws an error."""
        cache = self._make_cache_with_broken_redis()

        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")
        cache._redis = mock_client

        result = await cache.get("renko", {"symbol": "SPY"})
        assert result is None  # Should NOT raise

    @pytest.mark.asyncio
    async def test_set_handles_redis_error(self):
        """SET should return False when Redis throws an error."""
        cache = self._make_cache_with_broken_redis()

        mock_client = AsyncMock()
        mock_client.set.side_effect = Exception("Connection refused")
        cache._redis = mock_client

        result = await cache.set("renko", {"symbol": "SPY"}, {"data": "test"})
        assert result is False  # Should NOT raise

    @pytest.mark.asyncio
    async def test_invalidate_handles_redis_error(self):
        """INVALIDATE should return False when Redis throws an error."""
        cache = self._make_cache_with_broken_redis()

        mock_client = AsyncMock()
        mock_client.delete.side_effect = Exception("Connection refused")
        cache._redis = mock_client

        result = await cache.invalidate("renko", {"symbol": "SPY"})
        assert result is False  # Should NOT raise

    @pytest.mark.asyncio
    async def test_clear_prefix_handles_redis_error(self):
        """CLEAR_PREFIX should return 0 when Redis throws an error."""
        cache = self._make_cache_with_broken_redis()

        mock_client = AsyncMock()
        mock_client.keys.side_effect = Exception("Connection refused")
        cache._redis = mock_client

        result = await cache.clear_prefix("renko")
        assert result == 0  # Should NOT raise

    @pytest.mark.asyncio
    async def test_get_stats_handles_redis_error(self):
        """STATS should return error info when Redis throws."""
        cache = self._make_cache_with_broken_redis()

        mock_client = AsyncMock()
        mock_client.keys.side_effect = Exception("Connection refused")
        cache._redis = mock_client

        stats = await cache.get_stats()
        assert stats["enabled"] is True
        assert "error" in stats


# ─── RenkoBacktestResponse Cached Field ─────────────────────────────────────


class TestCachedField:
    """Test the `cached` field on response models."""

    def test_renko_response_default_not_cached(self):
        """RenkoBacktestResponse should default to cached=False."""
        from renko.router import RenkoBacktestResponse

        resp = RenkoBacktestResponse(
            symbol="SPY",
            total_ticks=500,
            total_bricks=120,
            config_used={},
            stats={},
            trades=[],
        )
        assert resp.cached is False

    def test_renko_response_cached_true(self):
        """RenkoBacktestResponse should accept cached=True for cache hits."""
        from renko.router import RenkoBacktestResponse

        resp = RenkoBacktestResponse(
            symbol="SPY",
            total_ticks=500,
            total_bricks=120,
            config_used={},
            stats={},
            trades=[],
            cached=True,
        )
        assert resp.cached is True

    def test_hmm_response_default_not_cached(self):
        """BacktestResponse (HMM) should default to cached=False."""
        from regime_platform.models.schemas import BacktestResponse

        resp = BacktestResponse(
            symbol="SPY",
            n_bars=500,
            n_trades=10,
            n_wins=6,
            n_losses=4,
            win_rate=0.6,
            total_return=0.15,
            annual_return=0.12,
            buy_hold_return=0.10,
            excess_return=0.05,
            monthly_return_avg=0.01,
            max_drawdown=0.08,
            max_drawdown_duration=30,
            avg_drawdown=0.03,
            var_95=0.02,
            cvar_95=0.03,
            annual_vol=0.15,
            downside_vol=0.10,
            sharpe_ratio=0.8,
            sortino_ratio=1.1,
            calmar_ratio=1.5,
            information_ratio=0.6,
            avg_win_pct=0.03,
            avg_loss_pct=0.02,
            avg_win_loss_ratio=1.5,
            max_consecutive_wins=4,
            max_consecutive_losses=3,
            avg_hold_bars=5.0,
            profit_factor=1.8,
            regime_distribution={},
            trades_by_regime={},
            equity_curve=[],
            drawdown_curve=[],
            trade_log=[],
        )
        assert resp.cached is False

    def test_hmm_response_cached_true(self):
        """BacktestResponse (HMM) should accept cached=True for cache hits."""
        from regime_platform.models.schemas import BacktestResponse

        resp = BacktestResponse(
            symbol="SPY",
            n_bars=500,
            n_trades=10,
            n_wins=6,
            n_losses=4,
            win_rate=0.6,
            total_return=0.15,
            annual_return=0.12,
            buy_hold_return=0.10,
            excess_return=0.05,
            monthly_return_avg=0.01,
            max_drawdown=0.08,
            max_drawdown_duration=30,
            avg_drawdown=0.03,
            var_95=0.02,
            cvar_95=0.03,
            annual_vol=0.15,
            downside_vol=0.10,
            sharpe_ratio=0.8,
            sortino_ratio=1.1,
            calmar_ratio=1.5,
            information_ratio=0.6,
            avg_win_pct=0.03,
            avg_loss_pct=0.02,
            avg_win_loss_ratio=1.5,
            max_consecutive_wins=4,
            max_consecutive_losses=3,
            avg_hold_bars=5.0,
            profit_factor=1.8,
            regime_distribution={},
            trades_by_regime={},
            equity_curve=[],
            drawdown_curve=[],
            trade_log=[],
            cached=True,
        )
        assert resp.cached is True
