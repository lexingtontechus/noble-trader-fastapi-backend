"""
Sweep Orchestrator — Core Engine
═════════════════════════════════

Reads active symbols from ``nt_symbol``, groups them by ``regime_cron``, and
schedules one APScheduler job per unique cron expression.  Each job processes
its symbols sequentially with a stagger delay to spread API load.

Stale-symbol recovery is built in: a 60-second background loop re-processes
any symbol whose last successful sweep is older than 20 minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import defaultdict
from typing import Any

import numpy as np
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from regime_platform.core.regime_engine import RegimeHMM, RegimeSnapshot
from regime_platform.core.position_sizer import PositionSizer, PositionRequest
from regime_platform.services.statistics.ev_engine import EVEngine
from regime_platform.services.redis_pool import redis_pool
from renko.trading_heartbeat import CONFIG_TTL
from regime_platform.sweep_orchestrator.market_hours import is_market_open
from regime_platform.sweep_orchestrator.price_fetcher import (
    fetch_latest_candles,
    fetch_tradingview_batch,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_active_symbols,
    log_regime_entry,
    log_sweep_result,
    log_error,
    fetch_calibration_bias,
)

logger = logging.getLogger("regime.sweep.orch")

# ─── Constants ────────────────────────────────────────────────────

STALE_THRESHOLD_S = 1200          # 20 minutes
STALE_CHECK_INTERVAL_S = 60       # check every 60 s
REFRESH_SYMBOLS_INTERVAL_S = 300  # re-read nt_symbol every 5 min
LIGHT_LOCK_TTL_S = 90             # per-symbol lock TTL (matches edge fn)
STAGGER_SECONDS = 5               # gap between symbols within a group

# ─── Qualification gate thresholds (configurable via env vars) ───────────
# These determine which sweep signals get published to Redis pub/sub
# "signal.raw.noble_trader" and forwarded to Discord. Tune them via env
# vars to control signal volume vs. quality.
#
# IMPORTANT: the EV/RR gate (Gate 6) is the most common blocker. The EV
# engine requires rr >= MIN_RR (default 2.0), but with default brick
# params sl_bricks=3, tp_bricks=5, the actual RR is 5/3 = 1.67 — below
# the 2.0 minimum. Either:
#   (a) lower MIN_RR to 1.5 (via env: MIN_RR=1.5), or
#   (b) increase tp_bricks to 6+ in your Redis config per symbol, or
#   (c) decrease sl_bricks to 2 in your Redis config per symbol
MIN_REGIME_CONF = float(os.getenv("MIN_REGIME_CONF", "0.60"))
MIN_KELLY = float(os.getenv("MIN_KELLY", "0.02"))
MIN_TIMESFM_CONFIDENCE = float(os.getenv("MIN_TIMESFM_CONFIDENCE", "0.05"))
# MIN_RR and MIN_EV are read by the EVEngine on init — see _ev_engine below.
# We patch the engine's thresholds after construction.
MIN_EV = float(os.getenv("MIN_EV", "0.0"))
MIN_RR = float(os.getenv("MIN_RR", "2.0"))
# ── TimesFM gate mode ────────────────────────────────────────────────────
# Controls how TimesFM confidence affects signal qualification:
#
#   "hard" (default): reject signal if |p_timesfm - 0.5| < MIN_TIMESFM_CONFIDENCE
#       This is a binary veto — one uncertain source kills the signal.
#       Problem: goes counter to the blended EV philosophy. The EV blend
#       already weights TimesFM at 0.35; a low-confidence forecast already
#       pulls p_win toward 0.5. The hard gate double-counts the penalty.
#
#   "soft": don't reject, but reduce TimesFM's blend weight proportionally
#       to its confidence:
#         effective_weight = base_weight * min(1.0, confidence / threshold)
#       At confidence=threshold → full weight (0.35)
#       At confidence=threshold/2 → half weight (0.175)
#       At confidence=0 → zero weight (like fallback mode)
#       This preserves the blend: one uncertain source contributes less,
#       but doesn't veto signals that other sources support.
#
#   "off": no TimesFM gate at all. Rely entirely on the blended EV.
#       If TimesFM is uncertain, the blend naturally reduces p_win, which
#       reduces EV, which the EV gate handles.
TIMESFM_GATE_MODE = os.getenv("TIMESFM_GATE_MODE", "hard").lower()
# When True, publish ALL signals (even rejected ones) to Redis channel
# "signal.debug.noble_trader" for diagnostic visibility. Set to "1" to
# enable. Default: disabled (only qualified signals go to production
# channel).
DEBUG_SIGNALS = os.getenv("DEBUG_SIGNALS", "0") == "1"
# When True, sweep ALL symbols regardless of market hours. Useful for:
#   - Regime tracking outside market hours (you still want to know the
#     regime even if you can't trade)
#   - Testing/debugging on weekends or after close
# When False (default), stocks/commodities are skipped when the market
# is closed (matching the TS edge function behavior). Crypto and forex
# are always swept (24/7 and Sun-Fri respectively).
SWEEP_AFTER_HOURS = os.getenv("SWEEP_AFTER_HOURS", "0") == "1"

# ── Signal cooldown (prevents signal spam) ────────────────────────────────
# After a qualified signal is published for a (symbol, direction) pair,
# suppress subsequent qualified signals for the same pair until the
# cooldown expires. This prevents the same BUY/SELL from being published
# every 5 minutes when the regime is stable.
#
# Set to 0 to disable cooldown (every qualified sweep is published).
# Recommended: 60 (1 hour) — gives ~3-8 signals per day per symbol.
SIGNAL_COOLDOWN_MINUTES = float(os.getenv("SIGNAL_COOLDOWN_MINUTES", "60"))

# ── Minimum p_win threshold ───────────────────────────────────────────────
# Require the blended p_win (from the EV engine's log-odds pooling) to be
# above this value. This is a DYNAMIC quality filter — unlike MIN_RR which
# is a static property of brick params, p_win changes sweep-to-sweep based
# on how regime + ext_signal + TimesFM agree.
#
# Set to 0.0 to disable (accept any positive-EV signal).
# Recommended: 0.55 — filters out signals where the blend is only marginally
# directional (p_win just barely above 0.5).
MIN_P_WIN = float(os.getenv("MIN_P_WIN", "0.55"))

# ── TimesFM fetcher with 15-minute cache ────────────────────────────────
# Mirrors supabase/functions/_shared/utils.ts:timesfmPost()
# Using a simple in-memory cache (not Redis for this hot path)
_TIMESFM_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (p, timestamp)
_TIMESFM_CACHE_TTL_S = 900  # 15 minutes

async def _fetch_timesfm_cached(symbol: str, prices: list[float]) -> tuple[float, float] | None:
    """
    Fetch TimesFM forecast for a symbol with a 15-minute cache.

    Returns ``(p_timesfm, confidence)`` where:
      * ``p_timesfm``     — probability that the next bar closes UP, in [0, 1].
                            Derived from the forecasted price vs. the last
                            observed price (not the raw forecast itself).
      * ``confidence``    — ``abs(p_timesfm - 0.5)`` in [0, 0.5].

    Returns ``None`` if the TimesFM service is unreachable or returns an
    unrecoverable error. Cache misses and cache hits share the same return
    shape (the previous implementation returned ``(p, age_seconds)`` on cache
    hits — a tuple-shape bug that broke downstream consumers).

    Uses the same inference URL as the edge function.
    """
    now = time.time()
    cached = _TIMESFM_CACHE.get(symbol)
    if cached is not None:
        p, ts = cached
        if now - ts < _TIMESFM_CACHE_TTL_S:
            confidence = abs(p - 0.5)
            logger.debug(
                f"[sweep:orch] {symbol}: TimesFM cache hit "
                f"(p={p:.4f}, confidence={confidence:.4f}, age={now-ts:.0f}s)"
            )
            return p, confidence
        # Cache expired — fall through to fresh fetch.
        _TIMESFM_CACHE.pop(symbol, None)

    # ── Fresh fetch (this branch was unreachable in the previous code —
    #    the entire fetch block was nested inside `if cached:`, so the
    #    very first call for any symbol silently returned None).
    try:
        import httpx

        # ── Resolve the TimesFM inference URL ─────────────────────────
        # Priority:
        #   1. TIMESFM_INFERENCE_URL env var (explicit override)
        #   2. TIMESFM_URL env var (legacy alias)
        #   3. http://localhost:8000/forecast (the gateway/proxy on port
        #      8000, which routes /forecast → TimesFM on port 8002)
        #
        # If the gateway is down or misconfigured, we fall back to hitting
        # TimesFM directly on port 8002 (see _TIMESFM_FALLBACK_URL below).
        primary_url = (
            os.getenv("TIMESFM_INFERENCE_URL") or
            os.getenv("TIMESFM_URL") or
            "http://localhost:8000/forecast"
        )
        # Direct fallback: bypass the gateway, hit TimesFM server.py on
        # port 8002. Only used if the primary URL returns a connection
        # error or 502/503 (gateway down or model still loading).
        # Can be disabled by setting TIMESFM_DISABLE_FALLBACK=1.
        fallback_url = os.getenv(
            "TIMESFM_DIRECT_URL", "http://127.0.0.1:8002/forecast"
        )
        use_fallback = os.getenv("TIMESFM_DISABLE_FALLBACK", "0") != "1"

        # API key is REQUIRED by server.py's /forecast endpoint
        # (verify_api_key dependency). The previous request omitted the
        # X-API-Key header, which caused every call to 401.
        TIMESFM_API_KEY = os.getenv("TIMESFM_API_KEYS", "").split(",")[0].strip()

        # Use last 500 bars (same as heavy sweep), capped to TimesFM's
        # max_context (default 1024).
        price_samples = prices[-500:] if len(prices) >= 500 else prices
        if len(price_samples) < 2:
            logger.warning(
                f"[sweep:orch] {symbol}: not enough price samples for TimesFM "
                f"(got {len(price_samples)})"
            )
            return None

        # Forecast horizon = 1 (next bar). The previous payload
        # ``{symbol, prices}`` did NOT match server.py's ForecastRequest
        # schema, which requires ``{horizon, inputs}`` — every call 422'd.
        last_price = float(price_samples[-1])
        request_body = {
            "horizon": 1,
            "inputs": [price_samples],
        }
        headers = {"Content-Type": "application/json"}
        if TIMESFM_API_KEY:
            headers["X-API-Key"] = TIMESFM_API_KEY

        # ── Try primary URL first, fall back to direct port 8002 ──────
        # The gateway (port 8000) sometimes returns 502 if the TimesFM
        # process on port 8002 hasn't started yet, or 503 if the model
        # is still loading. Hitting port 8002 directly gives us a more
        # specific error and avoids the gateway as a single point of
        # failure.
        urls_to_try = [primary_url]
        if use_fallback and primary_url != fallback_url:
            urls_to_try.append(fallback_url)

        last_error = None
        resp = None
        for url in urls_to_try:
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    resp = await client.post(
                        url,
                        json=request_body,
                        headers=headers,
                    )
                # If we got a real HTTP response (even an error), don't
                # try the fallback — the server is reachable, the error
                # is a real one (401, 422, 500, etc.) that the fallback
                # won't fix.
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as conn_err:
                last_error = conn_err
                logger.debug(
                    f"[sweep:orch] {symbol}: TimesFM URL {url} unreachable: "
                    f"{conn_err}"
                )
                continue  # try the fallback URL

        if resp is None:
            # All URLs failed to connect
            logger.warning(
                f"[sweep:orch] {symbol}: TimesFM unreachable at all tried URLs "
                f"({[u for u in urls_to_try]}) — last error: {last_error}"
            )
            return None

        # ── Handle specific HTTP error codes ──────────────────────────
        if resp.status_code == 401:
            logger.error(
                f"[sweep:orch] {symbol}: TimesFM 401 Unauthorized — "
                f"TIMESFM_API_KEYS env var not set or key mismatch. "
                f"URL={resp.url}"
            )
            return None
        if resp.status_code == 422:
            logger.error(
                f"[sweep:orch] {symbol}: TimesFM 422 Unprocessable Entity — "
                f"payload schema mismatch. URL={resp.url}, "
                f"body_keys={list(request_body.keys())}, "
                f"resp={resp.text[:200]}"
            )
            return None
        if resp.status_code == 503:
            # server.py returns 503 when state["model"] is None (model
            # still loading). This is transient — don't log at ERROR,
            # just INFO, so logs don't fill up during the 30-60s model
            # load window at startup.
            logger.info(
                f"[sweep:orch] {symbol}: TimesFM 503 (model still loading) — "
                f"will retry next sweep cycle"
            )
            return None
        if resp.status_code == 502:
            # Gateway/proxy couldn't reach TimesFM on port 8002.
            # This usually means server.py isn't running.
            logger.warning(
                f"[sweep:orch] {symbol}: TimesFM 502 Bad Gateway — "
                f"server.py on port 8002 not reachable. "
                f"Check that 'python server.py' is running."
            )
            return None
        resp.raise_for_status()
        data = resp.json()

        # TimesFM returns {"point_forecast": [[next_value]], ...}.
        # The forecast is a *price* (or price-like value), NOT a probability.
        # Convert to a probability of an up-move:
        #   p_up = sigmoid(k * (forecast - last_price) / last_price)
        # where k tunes how aggressively price deltas map to confidence.
        # k=20 means a 5% predicted up-move → p_up ≈ 0.92.
        point_forecast = data.get("point_forecast") or []
        if not point_forecast or not point_forecast[0]:
            logger.warning(
                f"[sweep:orch] {symbol}: TimesFM returned empty point_forecast"
            )
            return None

        try:
            forecast_value = float(point_forecast[0][0])
        except (TypeError, IndexError, ValueError) as e:
            logger.warning(
                f"[sweep:orch] {symbol}: TimesFM response unparseable: {e}"
            )
            return None

        # Map forecasted price → probability of up-move.
        # sigmoid(20 * relative_change) gives a smooth [0,1] mapping where
        # ±5% predicted move ≈ 0.92 / 0.08 (high confidence).
        if last_price <= 0:
            logger.warning(
                f"[sweep:orch] {symbol}: last_price<=0 ({last_price}) — "
                f"cannot compute p_timesfm"
            )
            return None
        rel_change = (forecast_value - last_price) / last_price
        # math.exp is faster than importing scipy; manual sigmoid is fine.
        _k = 20.0
        _z = _k * rel_change
        if _z >= 0:
            p_timesfm = 1.0 / (1.0 + math.exp(-_z))
        else:
            ez = math.exp(_z)
            p_timesfm = ez / (1.0 + ez)
        # Clamp to [0.001, 0.999] so confidence is never exactly 0 or 1
        # (avoids edge cases in downstream EV/Kelly math).
        p_timesfm = max(0.001, min(0.999, p_timesfm))
        confidence = abs(p_timesfm - 0.5)

        _TIMESFM_CACHE[symbol] = (p_timesfm, now)
        logger.info(
            f"[sweep:orch] {symbol}: TimesFM fetched "
            f"(forecast={forecast_value:.6f}, last={last_price:.6f}, "
            f"rel_change={rel_change*100:+.3f}%, "
            f"p_timesfm={p_timesfm:.4f}, confidence={confidence:.4f})"
        )
        return p_timesfm, confidence

    except Exception as e:
        # The previous implementation logged only the exception message and
        # returned None — making the root cause hard to find. Include the
        # exception type and a stack-trace hint in the log.
        logger.warning(
            f"[sweep:orch] {symbol}: TimesFM fetch failed "
            f"({type(e).__name__}: {e}) — proceeding without"
        )
        return None


def _markov_probs(regime_label: str) -> tuple[float, float]:
    """Return (p_up, p_dn) for a given regime label, matching TS computeMarkovProbs."""
    lower = regime_label.lower()
    is_bull = "bull" in lower
    is_bear = "bear" in lower
    is_high_vol = "high_vol" in lower

    if is_bull:
        return (0.55, 0.45) if is_high_vol else (0.65, 0.35)
    if is_bear:
        return (0.45, 0.55) if is_high_vol else (0.35, 0.65)
    return (0.5, 0.5)


def _compute_trade_signal(
    regime_label: str,
    current_price: float,
    brick_size: float,
    sl_bricks: int,
    tp_bricks: int,
) -> dict[str, Any]:
    """Derive a simple trade signal from the regime label (ported from TS)."""
    lower = regime_label.lower()
    is_bull = "bull" in lower
    is_bear = "bear" in lower

    if is_bull:
        signal = "buy"
        entry = current_price
        stop_loss = current_price - (brick_size * sl_bricks)
        take_profit = current_price + (brick_size * tp_bricks)
    elif is_bear:
        signal = "sell"
        entry = current_price
        stop_loss = current_price + (brick_size * sl_bricks)
        take_profit = current_price - (brick_size * tp_bricks)
    else:
        signal = "neutral"
        entry = current_price
        stop_loss = current_price - (brick_size * sl_bricks)
        take_profit = current_price + (brick_size * tp_bricks)

    return {
        "signal": signal,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


# ─── Price decimals per symbol ────────────────────────────────────

_PRICE_DECIMALS: dict[str, int] = {
    "BTC": 1, "ETH": 2, "SOL": 4,
    "BTCUSD": 1, "ETHUSD": 2, "SOLUSD": 4,
    "AAPL": 2, "AMZN": 2, "GOOGL": 2, "META": 2,
    "MSFT": 2, "NVDA": 2, "TSLA": 2, "SPY": 2, "QQQ": 2,
    "EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "AUDUSD": 5, "USDCAD": 5,
    "GOLD": 2, "SILVER": 2, "OIL": 2, "COPPER": 2,
}


def _price_decimals(symbol: str) -> int:
    return _PRICE_DECIMALS.get(symbol, 2)


# ─── Venue derivation ───────────────────────────────────────────────


def _derive_venue(sym: dict[str, Any]) -> str:
    """Derive the execution venue / exchange from the symbol config.

    Returns the EXCHANGE name (not the data source). For TradingView
    symbols, the exchange is extracted from the ``tradingview_symbol``
    field which is in ``EXCHANGE:SYMBOL`` format (e.g., ``COINBASE:BTCUSD``
    → exchange is ``Coinbase``).

    Priority:
      1. TradingView — extract exchange from ``EXCHANGE:SYMBOL`` format
      2. Hyperliquid — exchange is always ``Hyperliquid``
      3. Alpaca — exchange is always ``Alpaca``
      4. Yahoo — data source only, no exchange
      5. ``Unknown`` — fallback
    """
    tv_sym = sym.get("tradingview_symbol")
    if tv_sym and ":" in tv_sym:
        # Extract exchange from EXCHANGE:SYMBOL format
        # e.g., "COINBASE:BTCUSD" → "Coinbase"
        # e.g., "OANDA:EURUSD" → "OANDA"
        exchange = tv_sym.split(":")[0].strip()
        # Title-case common exchanges for readability
        exchange_map = {
            "COINBASE": "Coinbase",
            "OANDA": "OANDA",
            "BINANCE": "Binance",
            "BYBIT": "Bybit",
            "KRaken": "Kraken",
            "KRAKEN": "Kraken",
            "AMEX": "AMEX",
            "NYSE": "NYSE",
            "NASDAQ": "NASDAQ",
            "TVC": "TradingView",
            "FXCM": "FXCM",
        }
        return exchange_map.get(exchange.upper(), exchange.title())
    if sym.get("hyperliquid_coin"):
        return "Hyperliquid"
    if sym.get("alpaca_symbol"):
        return "Alpaca"
    if sym.get("yahoo_ticker"):
        return "Yahoo"
    return "Unknown"


# ─── Orchestrator ─────────────────────────────────────────────────


class SweepOrchestrator:
    """Reads symbols from Supabase, schedules sweep groups, handles stale retry."""

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        self._redis = None

        # Track last-successful-sweep timestamps for stale detection
        self._last_ok: dict[str, float] = {}
        # Last-known regime labels per symbol (for shift detection)
        self._last_regime: dict[str, str] = {}
        # ── Signal cooldown tracking ──────────────────────────────────
        # Maps (symbol, direction) → timestamp of last PUBLISHED signal.
        # Used to suppress duplicate signals within the cooldown window.
        # Key: (symbol, "buy" or "sell"), Value: time.time() timestamp.
        self._last_qualified_signal: dict[tuple[str, str], float] = {}

        # Shared engine instances
        self._sizer = PositionSizer()
        self._ev_engine = EVEngine()
        # ── Patch EV engine thresholds from env vars ──────────────────
        # EVEngine.__init__ defaults to min_ev=0.0, min_rr=2.0. Override
        # with env-configured values so the user can tune without code
        # changes. The RR threshold is the most common blocker — with
        # default brick params (sl=3, tp=5), RR=1.67 < 2.0, blocking
        # every signal.
        self._ev_engine.min_ev = MIN_EV
        self._ev_engine.min_rr = MIN_RR
        logger.info(
            f"Qualification thresholds: "
            f"MIN_REGIME_CONF={MIN_REGIME_CONF}, MIN_KELLY={MIN_KELLY}, "
            f"MIN_TIMESFM_CONFIDENCE={MIN_TIMESFM_CONFIDENCE}, "
            f"TIMESFM_GATE_MODE={TIMESFM_GATE_MODE}, "
            f"MIN_EV={MIN_EV}, MIN_RR={MIN_RR}, "
            f"MIN_P_WIN={MIN_P_WIN}, "
            f"SIGNAL_COOLDOWN_MINUTES={SIGNAL_COOLDOWN_MINUTES}, "
            f"DEBUG_SIGNALS={DEBUG_SIGNALS}"
        )

    # ── Public lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler and background checks."""
        self._redis = await redis_pool.get_client()
        if not self._redis:
            logger.error("Redis not available — sweep orchestrator cannot start")
            return

        self._scheduler = AsyncIOScheduler(timezone="UTC")

        # Initial symbol load → create sweep jobs
        await self._refresh_symbols()

        # Periodic symbol-registry refresh
        self._scheduler.add_job(
            self._refresh_symbols,
            trigger="interval",
            seconds=REFRESH_SYMBOLS_INTERVAL_S,
            id="refresh-symbols",
            name="Refresh symbol registry",
            misfire_grace_time=60,
        )

        self._scheduler.start()
        logger.info("Sweep orchestrator scheduler started")

        # Start the stale-symbol background checker in the event loop
        asyncio.create_task(self._stale_check_loop())

    async def stop(self) -> None:
        """Gracefully shut down the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Sweep orchestrator scheduler stopped")

    # ── Symbol registry ───────────────────────────────────────────

    async def _refresh_symbols(self) -> None:
        """Fetch active symbols from Supabase and (re)schedule groups."""
        symbols = await fetch_active_symbols()
        if not symbols:
            logger.warning("No active symbols found; sweep groups will be empty")
            return

        # Group by regime_cron expression
        # Support multi-expression cron (e.g. "5,20 * * * 1-5 / 35 * * * 0,6")
        # by splitting on " / " and assigning the symbol to each part.
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sym in symbols:
            cron_raw = sym.get("regime_cron", "*/15 * * * *")
            for cron in self._split_cron(cron_raw):
                groups[cron].append(sym)

        # Sort within groups for deterministic stagger order
        for cron in groups:
            groups[cron].sort(key=lambda s: s["symbol"])

        self._reschedule_groups(groups)

        total = sum(len(v) for v in groups.values())
        logger.info(
            "Symbol registry refreshed: %d symbols in %d cron group(s)",
            total,
            len(groups),
        )
        for cron_expr, sym_list in groups.items():
            logger.info(
                "  Group [%s]: %s",
                cron_expr,
                [s["symbol"] for s in sym_list],
            )

    def _reschedule_groups(self, groups: dict[str, list[dict[str, Any]]]) -> None:
        """Replace all sweep-* jobs to reflect current groups."""
        assert self._scheduler is not None

        existing = {
            job.id
            for job in self._scheduler.get_jobs()
            if job.id.startswith("sweep-")
        }
        desired: set[str] = set()

        for cron_expr, sym_list in groups.items():
            safe_id = (
                cron_expr.replace(" ", "_")
                .replace("*", "star")
                .replace("/", "sl")
                .replace(",", "com")
                .replace("-", "min")
            )
            job_id = f"sweep-{safe_id}"
            desired.add(job_id)

            if job_id in existing:
                continue

            self._scheduler.add_job(
                func=self._sweep_group,
                trigger=CronTrigger.from_crontab(cron_expr),
                args=[cron_expr, sym_list],
                id=job_id,
                name=f"Light sweep [{cron_expr}] {len(sym_list)} symbols",
                misfire_grace_time=120,
                coalesce=True,
            )
            logger.info(
                "Scheduled: %s → %s",
                cron_expr,
                [s["symbol"] for s in sym_list],
            )

        # Remove jobs that no longer correspond to any group
        for job_id in existing - desired:
            try:
                self._scheduler.remove_job(job_id)
                logger.info("Removed stale sweep job: %s", job_id)
            except Exception:
                pass

    # ── Sweep group runner ────────────────────────────────────────

    async def _sweep_group(
        self, cron_expr: str, symbols: list[dict[str, Any]]
    ) -> None:
        """Process all symbols in one cron group."""
        # Filter by market hours
        if SWEEP_AFTER_HOURS:
            active = symbols
            logger.info(
                "Running sweep group [%s]: %d symbols (SWEEP_AFTER_HOURS=1, "
                "market-hours filter bypassed)",
                cron_expr,
                len(active),
            )
        else:
            active = [s for s in symbols if is_market_open(s.get("asset_class", ""))]
            filtered = [s for s in symbols if not is_market_open(s.get("asset_class", ""))]
            if filtered:
                # Log at INFO level (was debug) so the user can see WHY
                # symbols are being skipped. This is the most common source
                # of "why isn't my symbol being swept?" confusion.
                logger.info(
                    "Running sweep group [%s]: %d symbols active, %d filtered "
                    "(market closed): %s",
                    cron_expr,
                    len(active),
                    len(filtered),
                    [(s["symbol"], s.get("asset_class", "?")) for s in filtered],
                )
            else:
                logger.info(
                    "Running sweep group [%s]: %d symbols active",
                    cron_expr,
                    len(active),
                )
        if not active:
            return

        # ── Price fetch (batch for TradingView, parallel for others) ──
        # TradingView symbols are fetched in ONE batch API call (POST /api/price/batch)
        # to reduce API calls from N to 1. Non-TV symbols (HL, Alpaca) are
        # fetched in parallel as before.
        valid_prices: dict[str, list[float]] = {}

        # Split symbols by data source
        tv_symbols = [s for s in active if s.get("data_source") == "tradingview"]
        non_tv_symbols = [s for s in active if s.get("data_source") != "tradingview"]

        # ── Batch fetch TradingView symbols (1 API call) ─────────────
        if tv_symbols:
            try:
                tv_results = await fetch_tradingview_batch(tv_symbols, 200)
                for sym_name, prices in tv_results.items():
                    if len(prices) < 81:
                        logger.warning(
                            "%s: insufficient data (%d bars, need >=81)",
                            sym_name, len(prices),
                        )
                        continue
                    valid_prices[sym_name] = prices
            except Exception as exc:
                logger.warning("TradingView batch fetch failed: %s", exc)

        # ── Parallel fetch non-TV symbols (HL, Alpaca) ───────────────
        if non_tv_symbols:
            price_futs = [fetch_latest_candles(s, 200) for s in non_tv_symbols]
            price_results = await asyncio.gather(*price_futs, return_exceptions=True)

            for sym, prices in zip(non_tv_symbols, price_results):
                if isinstance(prices, Exception):
                    logger.warning("Price fetch failed for %s: %s", sym["symbol"], prices)
                    continue
                if len(prices) < 81:
                    logger.warning(
                        "%s: insufficient data (%d bars, need >=81)",
                        sym["symbol"],
                        len(prices),
                    )
                    continue
                valid_prices[sym["symbol"]] = prices

        if not valid_prices:
            return

        # Sequential processing with stagger
        symbols_to_process = [s for s in active if s["symbol"] in valid_prices]
        for i, sym in enumerate(symbols_to_process):
            symbol = sym["symbol"]
            prices = valid_prices[symbol]
            start_ts = time.time()

            # Per-symbol lock (Layer 1, same as edge functions)
            lock_key = f"lock:sweep:{symbol}"
            locked = await self._redis.set(lock_key, "1", nx=True, ex=LIGHT_LOCK_TTL_S)
            if not locked:
                logger.info("%s: locked — skipping (concurrent sweep)", symbol)
                continue

            # Fetch TimesFM (cached 15min)
            p_timesfm_result = await _fetch_timesfm_cached(symbol, prices)

            # Validate TimesFM result before processing
            if p_timesfm_result is None:
                logger.warning(
                    f"[sweep:orch] {symbol}: TimesFM unavailable — skipping qualification checks, "
                    f"using fallback mode (p_timesfm=None)"
                )
                sweep_window = "sweep-light (TimesFM unavailable)"
                p_timesfm = None
            else:
                sweep_window = "sweep-light"
                p_timesfm, timesfm_confidence = p_timesfm_result

            result: dict[str, Any] = {"regime_label": "error", "kelly_f": 0}
            try:
                result = await self._process_symbol(sym, prices, p_timesfm)
                self._last_ok[symbol] = time.time()
                await log_sweep_result({
                    "symbol": symbol,
                    "asset_class": sym.get("asset_class", ""),
                    "source": "sweep-light",
                    "brick_size": result.get("brick_size", 0.5),
                    "sl_bricks": result.get("sl_bricks", 3),
                    "tp_bricks": result.get("tp_bricks", 5),
                    "regime": result["regime_label"],
                    "regime_conf": round(result["regime_conf"], 4),
                    "kelly_f": round(result.get("effective_kelly", result["kelly_f"]), 6),
                    "markov_p_up": round(result["markov_p_up"], 4),
                    "markov_p_dn": round(result["markov_p_dn"], 4),
                    "sharpe": round(result.get("sharpe", 0), 4),
                    "total_return": round(result.get("total_return", 0), 6),
                    "annual_return": round(result.get("annual_return", 0), 6),
                    "max_drawdown_pct": round(result.get("max_drawdown_pct", 0), 6),
                    "win_rate": round(result.get("win_rate", 0), 4),
                    "profit_factor": round(result.get("profit_factor", 0), 4),
                    "n_trades": result.get("n_trades"),
                    "sweep_window": sweep_window,
                    "sweep_duration_ms": int((time.time() - start_ts) * 1000),
                    # ── New fields (migration 029) ──────────────────────────
                    "effective_kelly": round(result.get("effective_kelly", 0), 6),
                    "signal": result.get("signal", "neutral"),
                    "ev": round(result.get("ev", 0), 4),
                    "p_win": round(result.get("p_win", 0), 4),
                    "qualified": result.get("qualified", False),
                    "p_timesfm": round(p_timesfm, 4) if p_timesfm is not None else None,
                    "entry_price": round(result.get("entry_price", 0), 6),
                    "stop_loss": round(result.get("stop_loss", 0), 6),
                    "take_profit": round(result.get("take_profit", 0), 6),
                    "aggression": result.get("aggression", "mid"),
                    "venue": _derive_venue(sym),
                })
            except Exception as exc:
                logger.error("%s: sweep failed: %s", symbol, exc)
                await log_error(symbol, "sweep-light", str(exc)[:1000])
            except Exception as exc:
                logger.error("%s: sweep failed: %s", symbol, exc)
                await log_error(symbol, "sweep-light", str(exc)[:1000])
            finally:
                await self._redis.delete(lock_key)

            elapsed = time.time() - start_ts
            logger.info(
                "%s: done in %.1fs — regime=%s, kelly=%.3f",
                symbol,
                elapsed,
                result["regime_label"],
                result["kelly_f"],
            )

            if i < len(symbols_to_process) - 1:
                await asyncio.sleep(STAGGER_SECONDS)

    # ─── Per-symbol processing ─────────────────────────────────────

    async def _process_symbol(
        self,
        sym: dict[str, Any],
        prices: list[float],
        p_timesfm: float | None = None,
    ) -> dict[str, Any]:
        """Run one light-sweep cycle for a single symbol.

        Returns a dict with sweep result fields for logging.
        """
        symbol = sym["symbol"]
        np_prices = np.array(prices, dtype=float)

        # ── 1. Regime detection ──
        model = await RegimeHMM.from_runtime_config()
        model.fit(np_prices)
        regime: RegimeSnapshot = model.predict(np_prices)

        regime_label = regime.regime_label or "unknown"
        regime_conf = regime.confidence
        trend_probs = dict(zip(RegimeHMM.TREND_LABELS, regime.trend_probs))

        # ── 2. Kelly sizing ──
        returns = np.diff(np.log(np_prices)).tolist()
        # Annualise factor: 8760 for 24/7 assets (crypto), 252 for stocks/ETFs
        asset_class = sym.get("asset_class", "")
        annualise_factor = 8760 if asset_class == "crypto" else 252
        kelly_result = self._sizer.size(
            PositionRequest(
                returns=returns,
                kelly_fraction=0.5,
                target_vol=0.15,
                regime=regime,
            ),
            annualise_factor=annualise_factor,
        )
        kelly_f = kelly_result.recommended_f

        # ── 2a. Kelly edge diagnostics ──
        _mu = float(np.mean(np.array(returns, dtype=float)))
        _sigma2 = float(np.var(np.array(returns, dtype=float), ddof=1))
        logger.info(
            "%s: kelly math — mu=%.8f, sigma2=%.8f, mu/sigma2=%.4f, "
            "full=%s, frac=%s, vol_scaled=%s, regime_gated=%s, recommended=%s",
            symbol, _mu, _sigma2, (_mu / _sigma2 if _sigma2 > 0 else 0.0),
            kelly_result.full_kelly_f, kelly_result.fractional_f,
            kelly_result.vol_scaled_f, kelly_result.regime_gated_f,
            kelly_result.recommended_f,
        )

        # ── 2b. Regime-based Kelly floor ──
        # Hourly returns are noisy; mean can be ≤ 0 even in a strong bull
        # regime, driving raw Kelly to 0. When the HMM is confidently
        # directional, apply a floor so the sizer never returns 0.
        if kelly_f == 0.0 and regime_label not in ("unknown", "market_closed", "neutral"):
            # Scale the floor by regime confidence (0.5–0.99 → 0.02–0.06)
            _floor = 0.04 * max(0.0, min(1.0, (regime_conf - 0.5) * 2.0))
            kelly_f = round(min(_floor, 0.06), 6)
            logger.info(
                "%s: Kelly was 0 — applied regime floor %.4f (conf=%.3f, regime=%s)",
                symbol, kelly_f, regime_conf, regime_label,
            )

        # ── 2b. Return statistics for Supabase ──
        np_returns = np.array(returns, dtype=float)
        total_return = float(np_prices[-1] / np_prices[0] - 1.0) if np_prices[0] > 0 else 0.0
        annual_return = (1.0 + total_return) ** (8760 / max(len(returns), 1)) - 1.0
        cum_returns = np.cumprod(1.0 + np_returns)
        peak = np.maximum.accumulate(cum_returns)
        drawdowns = (cum_returns - peak) / peak
        max_drawdown_pct = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0
        win_rate = float(np.sum(np_returns > 0) / len(np_returns)) if len(np_returns) > 0 else 0.0
        gross_profit = float(np.sum(np_returns[np_returns > 0])) if np.any(np_returns > 0) else 0.0
        gross_loss = abs(float(np.sum(np_returns[np_returns < 0]))) if np.any(np_returns < 0) else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

        # ── 3. Markov probabilities ──
        p_up, p_dn = _markov_probs(regime_label)

        # ── 4. Direction & trade signal ──
        current_price = float(np_prices[-1])
        direction = "short" if "bear" in regime_label.lower() else "long"

        brick_size, sl_bricks, tp_bricks = await self._load_brick_params(symbol)

        trade_signal = _compute_trade_signal(
            regime_label, current_price, brick_size, sl_bricks, tp_bricks,
        )

        # ── 5. EV Engine ──
        ev_result = self._ev_engine.compute_ev(
            trend_probs=trend_probs,
            direction=direction,
            p_timesfm=p_timesfm,
            brick_size=brick_size,
            sl_bricks=sl_bricks,
            tp_bricks=tp_bricks,
        )
        # EVFullResult is a dataclass — use attribute access
        effective_kelly = kelly_f * ev_result.ev_scale * ev_result.markov_multiplier

        # ── 5a. Qualification gates ──
        # Only publish qualified signals to signal.raw.noble_trader
        signal_is_qualifiable = trade_signal["signal"] not in ("neutral", "unknown")

        if p_timesfm is not None:
            confidence = abs(p_timesfm - 0.5)
        else:
            confidence = None

        # ── Evaluate each gate individually ──────────────────────────
        # We evaluate all gates (not short-circuit) so the rejection log
        # can show the user EVERYTHING that's wrong, not just the first
        # failure. This is critical for tuning — if you can't see which
        # gates are failing, you can't optimize the thresholds.
        #
        # ── TimesFM gate mode ────────────────────────────────────────
        # TIMESFM_GATE_MODE controls how TimesFM confidence affects
        # qualification:
        #   "hard" (default): reject if confidence < MIN_TIMESFM_CONFIDENCE
        #   "soft": don't reject, but recompute EV with reduced TimesFM weight
        #   "off": no TimesFM gate at all (rely on blended EV)
        #
        # The soft mode preserves the blended EV philosophy: one uncertain
        # source shouldn't veto the entire signal — it should just
        # contribute less to the blend. See the env var docstring above
        # for the full rationale.
        timesfm_confidence_ok = True  # default for "off" mode
        adjusted_ev_result = ev_result  # default: use original EV result

        if TIMESFM_GATE_MODE == "hard":
            # Original behavior: hard reject if confidence below threshold
            timesfm_confidence_ok = (
                confidence is not None
                and confidence >= MIN_TIMESFM_CONFIDENCE
            )
        elif TIMESFM_GATE_MODE == "soft":
            # Soft gate: don't reject, but recompute EV with reduced weight.
            # The EV engine's log-odds blend already weights TimesFM at 0.35.
            # When confidence is low, we reduce that weight proportionally,
            # then recompute p_win and EV. This way, a low-confidence TimesFM
            # forecast doesn't veto the signal — it just contributes less.
            if p_timesfm is not None and confidence is not None:
                base_weight = 0.35  # matches P_WIN_WEIGHTS_TIMESFM["p_timesfm"]
                scale = min(1.0, confidence / MIN_TIMESFM_CONFIDENCE) if MIN_TIMESFM_CONFIDENCE > 0 else 1.0
                if scale < 1.0:
                    # Recompute EV with adjusted TimesFM weight.
                    # We need to re-run the EV engine with custom weights.
                    # The EV engine supports runtime weight override via
                    # set_weights(), but that mutates shared state. Instead,
                    # we temporarily override, compute, then restore.
                    original_weights = dict(self._ev_engine._timesfm_weights)
                    adjusted_weights = dict(original_weights)
                    adjusted_weights["p_timesfm"] = base_weight * scale
                    # Renormalize so weights sum to 1.0
                    total_w = sum(adjusted_weights.values())
                    if total_w > 0:
                        adjusted_weights = {k: v / total_w for k, v in adjusted_weights.items()}
                    self._ev_engine._timesfm_weights = adjusted_weights
                    try:
                        adjusted_ev_result = self._ev_engine.compute_ev(
                            trend_probs=trend_probs,
                            direction=direction,
                            p_timesfm=p_timesfm,
                            brick_size=brick_size,
                            sl_bricks=sl_bricks,
                            tp_bricks=tp_bricks,
                        )
                    finally:
                        # Restore original weights
                        self._ev_engine._timesfm_weights = original_weights
                    # In soft mode, the TimesFM gate always "passes" —
                    # the penalty is applied via the reduced weight, not
                    # via a binary reject.
                    timesfm_confidence_ok = True
                else:
                    # Confidence meets threshold — full weight, no adjustment
                    timesfm_confidence_ok = True
            # If p_timesfm is None, timesfm_confidence_ok stays True (handled
            # by the timesfm_available gate below)
        elif TIMESFM_GATE_MODE == "off":
            # No TimesFM gate — rely entirely on the blended EV
            timesfm_confidence_ok = True
        else:
            logger.warning(
                f"Unknown TIMESFM_GATE_MODE={TIMESFM_GATE_MODE!r}, "
                f"defaulting to 'hard' behavior"
            )
            timesfm_confidence_ok = (
                confidence is not None
                and confidence >= MIN_TIMESFM_CONFIDENCE
            )

        # Use the (possibly adjusted) EV result for gate evaluation
        effective_ev_result = adjusted_ev_result

        # ── In soft mode, the adjusted EV result replaces the original ──
        # This ensures all downstream code (rejection log, Redis writes,
        # agent payload, return dict) uses the soft-gate-adjusted values.
        # In hard/off mode, effective_ev_result IS ev_result (no change).
        if TIMESFM_GATE_MODE == "soft" and effective_ev_result is not ev_result:
            ev_result = effective_ev_result
            # Recompute effective_kelly with the adjusted ev_scale
            effective_kelly = kelly_f * ev_result.ev_scale * ev_result.markov_multiplier

        gate_results = {
            "signal_qualifiable": signal_is_qualifiable,
            "regime_conf_ok": regime_conf >= MIN_REGIME_CONF,
            "kelly_min_ok": kelly_f >= MIN_KELLY,
            "timesfm_available": p_timesfm is not None,
            "timesfm_confidence_ok": timesfm_confidence_ok,
            "ev_allowed": effective_ev_result.allowed,
            # ── New: p_win threshold (dynamic quality filter) ───────
            # Unlike MIN_RR (static per symbol), p_win changes sweep-to-
            # sweep based on how regime + ext_signal + TimesFM agree.
            # Filters out signals where the blend is only marginally
            # directional (p_win just barely above 0.5).
            "p_win_ok": ev_result.p_win >= MIN_P_WIN,
        }

        passed_gates = {
            "signal": trade_signal["signal"],
            **gate_results,
            "regime_conf": regime_conf,
            "kelly_f": kelly_f,
            "timesfm_missing": p_timesfm is None,
            "timesfm_confidence": round(confidence, 4) if confidence is not None else None,
            "ev": round(effective_ev_result.ev, 4),
            "ev_per_dollar": round(effective_ev_result.ev_per_dollar, 4),
            "rr": round(effective_ev_result.rr, 4),
            "p_win": round(ev_result.p_win, 4),
            "ev_reason": effective_ev_result.reason,
            "timesfm_gate_mode": TIMESFM_GATE_MODE,
        }

        # A signal is qualified only if ALL gates pass.
        qualified = all(gate_results.values())

        # ── Cooldown check (suppress signal spam) ────────────────────
        # Even if all gates pass, suppress the signal if the same
        # (symbol, direction) was published recently. This prevents the
        # same BUY/SELL from being published every 5 minutes when the
        # regime is stable.
        cooldown_suppressed = False
        if qualified and SIGNAL_COOLDOWN_MINUTES > 0:
            cooldown_key = (symbol, trade_signal["signal"])
            last_ts = self._last_qualified_signal.get(cooldown_key, 0)
            elapsed_min = (time.time() - last_ts) / 60.0
            if elapsed_min < SIGNAL_COOLDOWN_MINUTES:
                cooldown_suppressed = True
                logger.info(
                    f"[sweep:orch] {symbol}: COOLDOWN SUPPRESSED — "
                    f"{trade_signal['signal']} signal suppressed "
                    f"({elapsed_min:.1f}min since last, cooldown={SIGNAL_COOLDOWN_MINUTES}min)"
                )
                # Mark as not qualified so it doesn't get published
                qualified = False
                gate_results["cooldown_ok"] = False
            else:
                gate_results["cooldown_ok"] = True
        else:
            gate_results["cooldown_ok"] = True

        if not qualified and not cooldown_suppressed:
            # ── Detailed rejection log ────────────────────────────────
            # Show EVERY gate's status + actual value vs threshold, so
            # the user can see exactly what's blocking signals and tune
            # accordingly. The previous log was misleading — it printed
            # threshold values even for gates that passed.
            failed_gates = [name for name, ok in gate_results.items() if not ok]
            # Build the confidence line separately to avoid f-string nesting issues
            if confidence is not None:
                conf_str = f"{confidence:.4f} >= {MIN_TIMESFM_CONFIDENCE}? {'✓' if gate_results['timesfm_confidence_ok'] else '✗ FAIL'}"
            else:
                conf_str = "N/A (TimesFM unavailable)"
            logger.info(
                f"[sweep:orch] {symbol}: REJECTED — {len(failed_gates)} gate(s) failed: "
                f"{failed_gates}\n"
                f"    Gate details (TIMESFM_GATE_MODE={TIMESFM_GATE_MODE}):\n"
                f"      signal={trade_signal['signal']!r}  qualifiable={signal_is_qualifiable}  "
                f"(must be 'buy' or 'sell')\n"
                f"      regime_conf={regime_conf:.4f}  >= {MIN_REGIME_CONF}? "
                f"{'✓' if gate_results['regime_conf_ok'] else '✗ FAIL'}\n"
                f"      kelly_f={kelly_f:.6f}  >= {MIN_KELLY}? "
                f"{'✓' if gate_results['kelly_min_ok'] else '✗ FAIL'}\n"
                f"      p_timesfm={'None' if p_timesfm is None else f'{p_timesfm:.4f}'}  "
                f"available={gate_results['timesfm_available']}  "
                f"confidence={conf_str}\n"
                f"      ev={ev_result.ev:.4f}  rr={ev_result.rr:.4f}  "
                f"ev_allowed={ev_result.allowed}  "
                f"(need ev>{MIN_EV} AND rr>={MIN_RR})\n"
                f"      p_win={ev_result.p_win:.4f}  >= {MIN_P_WIN}? "
                f"{'✓' if gate_results['p_win_ok'] else '✗ FAIL'}\n"
                f"      ev_reason={ev_result.reason or 'passed'}\n"
                f"    brick_params: sl={sl_bricks}, tp={tp_bricks}, "
                f"brick_size={brick_size}  -> rr={ev_result.rr:.4f}"
            )

        # ── Debug channel: publish ALL signals for visibility ────────
        # When DEBUG_SIGNALS=1, publish every signal (qualified or not)
        # to "signal.debug.noble_trader" so you can subscribe and inspect
        # what's being filtered. This is essential for threshold tuning —
        # you can't optimize what you can't see.
        # NOTE: decimals must be computed BEFORE this block because the
        # debug payload uses it for price rounding. Previously decimals
        # was defined later (at the Redis write section), causing
        # UnboundLocalError when DEBUG_SIGNALS=1.
        decimals = _price_decimals(symbol)

        if DEBUG_SIGNALS:
            _debug_ts = int(time.time() * 1000)
            debug_payload = json.dumps({
                "type": "debug_signal",
                "symbol": symbol,
                "ts": _debug_ts,
                "qualified": qualified,
                "signal": trade_signal["signal"],
                "regime": regime_label,
                "regime_conf": round(regime_conf, 4),
                "kelly_f": round(kelly_f, 6),
                "p_timesfm": round(p_timesfm, 4) if p_timesfm is not None else None,
                "timesfm_confidence": round(confidence, 4) if confidence is not None else None,
                "ev": round(ev_result.ev, 4),
                "rr": round(ev_result.rr, 4),
                "ev_allowed": ev_result.allowed,
                "ev_reason": ev_result.reason,
                "failed_gates": [k for k, v in gate_results.items() if not v],
                "entry_price": round(trade_signal["entry_price"], decimals),
                "stop_loss": round(trade_signal["stop_loss"], decimals),
                "take_profit": round(trade_signal["take_profit"], decimals),
                "sl_bricks": sl_bricks,
                "tp_bricks": tp_bricks,
                "brick_size": brick_size,
            })
            try:
                await self._redis.publish("signal.debug.noble_trader", debug_payload)
            except Exception:
                pass  # don't let debug publish break the sweep

        # ── 6. Calibration bias ──
        # Only fetch calibration when the signal is qualified — it's an
        # extra Supabase round-trip that's only useful for sizing decisions
        # on signals we'll actually publish.
        cal = await fetch_calibration_bias(symbol) if qualified else None

        # ── 7. Regime shift detection ──
        prev_regime = self._last_regime.get(symbol)
        regime_shift = (
            prev_regime is not None
            and prev_regime not in ("unknown", "market_closed")
            and regime_label not in ("unknown", "market_closed")
            and prev_regime != regime_label
        )
        self._last_regime[symbol] = regime_label

        # ── 8. Write to Redis ──
        # DEPRECATED: trading:config:{symbol} HSET and trading:heartbeat:{symbol}
        # SET were the "poll" backup mechanism. The platform has moved to a
        # push model (Redis Streams XADD + pub/sub PUBLISH to
        # signal.raw.noble_trader). Downstream consumers now read from the
        # stream, not from per-symbol config hashes.
        #
        # The config_dict, heartbeat_key, and agent_channel code below is
        # commented out but preserved for reference. The agent_payload
        # (pub/sub + stream) is still active.
        #
        # config_key = f"trading:config:{symbol}"
        # heartbeat_key = f"trading:heartbeat:{symbol}"
        # agent_channel = f"agent:{symbol}"
        now_ms = int(time.time() * 1000)

        # ── DEPRECATED: config_dict was written to trading:config:{symbol} ──
        # Preserved for reference. Downstream consumers now read from the
        # Redis Stream (signal.raw.noble_trader) instead of polling this hash.
        #
        # config_dict: dict[str, str | float] = {
        #     "regime": regime_label,
        #     "regime_conf": round(regime_conf, 4),
        #     "brick_size": brick_size,
        #     "sl_bricks": sl_bricks,
        #     "tp_bricks": tp_bricks,
        #     "markov_p_up": round(p_up, 4),
        #     "markov_p_dn": round(p_dn, 4),
        #     "kelly_f": round(kelly_f, 6),
        #     "effective_kelly": round(effective_kelly, 6),
        #     "last_update": now_ms,
        #     "sweep_timestamp": now_ms,
        #     "signal": trade_signal["signal"],
        #     "entry_price": round(trade_signal["entry_price"], decimals),
        #     "stop_loss": round(trade_signal["stop_loss"], decimals),
        #     "take_profit": round(trade_signal["take_profit"], decimals),
        #     "ev": round(ev_result.ev, 4),
        #     "ev_per_dollar": round(ev_result.ev_per_dollar, 4),
        #     "p_win": round(ev_result.p_win, 4),
        #     "p_regime": round(ev_result.p_regime, 4),
        #     "p_markov": round(ev_result.p_markov, 4),
        #     "aggression": ev_result.aggression,
        #     "ev_scale": round(ev_result.ev_scale, 4),
        #     "regime_shift": "true" if regime_shift else "false",
        #     "prev_regime": prev_regime or "",
        #     "shift_at": str(now_ms) if regime_shift else "0",
        #     "price_decimals": decimals,
        #     "config_ts": round(time.time(), 3),
        # }
        # if p_timesfm is not None:
        #     config_dict["p_timesfm"] = round(p_timesfm, 4)
        # if cal:
        #     config_dict["calibration_bias"] = round(cal["bias"], 4)
        #     config_dict["calibration_status"] = cal["status"]

        # Compute size multiplier based on TimesFM confidence
        size_mult = 1.0
        if qualified and p_timesfm is not None:
            confidence = abs(p_timesfm - 0.5)
            if confidence >= 0.30:
                size_mult = 1.2
            elif confidence >= 0.15:
                size_mult = 1.0
            else:
                size_mult = 0.6

        effective_kelly_final = effective_kelly * size_mult if qualified else 0.0

        agent_payload = json.dumps({
            "type": "heartbeat",
            "symbol": symbol,
            "ts": now_ms,
            # ── Venue + asset class info (for Discord routing) ───────────
            # The signal aggregator uses asset_class to route signals to
            # per-venue Discord webhooks (crypto/equities/metals). Venue
            # is derived from the symbol config's execution fields.
            "asset_class": sym.get("asset_class", ""),
            "venue": _derive_venue(sym),
            "regime": regime_label,
            "regime_conf": round(regime_conf, 4),
            "signal": trade_signal["signal"],
            "entry_price": round(trade_signal["entry_price"], decimals),
            "stop_loss": round(trade_signal["stop_loss"], decimals),
            "take_profit": round(trade_signal["take_profit"], decimals),
            "aggression": ev_result.aggression,
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "kelly_f": round(kelly_f, 6),
            "effective_kelly": round(effective_kelly_final, 6),
            "ev": round(ev_result.ev, 4),
            "p_win": round(ev_result.p_win, 4),
            "regime_shift": "true" if regime_shift else "false",
            "prev_regime": prev_regime or "",
            "size_mult": round(size_mult, 2),
            "qualified": qualified,
        })

        ttl = sym.get("heartbeat_ttl_seconds", 900)
        try:
            # ── Redis push model (replaces deprecated poll model) ────────
            # The old poll model wrote to:
            #   HSET trading:config:{symbol}  — DEPRECATED (commented out above)
            #   SET  trading:heartbeat:{symbol} — DEPRECATED
            #   PUBLISH agent:{symbol}          — DEPRECATED (liveness)
            #
            # The new push model publishes qualified signals via:
            #   PUBLISH signal.raw.noble_trader  — for in-process SignalAggregator
            #   XADD   signal.raw.noble_trader   — for downstream consumers (persistent)
            #
            # Only qualified signals are pushed. Non-qualified sweeps don't
            # produce a Redis message — the downstream consumer only sees
            # actionable signals.
            pipe = self._redis.pipeline(transaction=False)

            # DEPRECATED — kept for reference. Downstream now reads from stream.
            # pipe.hset(config_key, mapping=safe_config_dict)
            # pipe.expire(config_key, CONFIG_TTL)
            # pipe.set(heartbeat_key, str(now_ms), ex=ttl)
            # pipe.publish(agent_channel, agent_payload)

            if qualified:
                # ── Publish qualified signals via BOTH pub/sub AND Streams ──
                pipe.publish("signal.raw.noble_trader", agent_payload)
                pipe.xadd(
                    "signal.raw.noble_trader",
                    {"payload": agent_payload},
                    maxlen=10000,
                    approximate=True,
                )
                # ── Record cooldown timestamp ──────────────────────────
                # Mark this (symbol, direction) as published so subsequent
                # sweeps within the cooldown window are suppressed.
                self._last_qualified_signal[(symbol, trade_signal["signal"])] = time.time()
                logger.info(
                    f"[sweep:orch] {symbol}: PUBLISHED {trade_signal['signal']} signal "
                    f"(cooldown timer started: {SIGNAL_COOLDOWN_MINUTES}min)"
                )
            await pipe.execute()
            logger.debug(
                f"[sweep:orch] {symbol}: Redis push OK "
                f"(qualified={qualified})"
            )
        except Exception as redis_exc:
            # The previous implementation let this exception propagate,
            # which aborted the rest of the sweep (Supabase log, return
            # value). Log loudly and continue — a Redis hiccup should NOT
            # prevent the sweep result from being recorded in Supabase.
            logger.error(
                f"[sweep:orch] {symbol}: Redis pipeline failed — "
                f"{type(redis_exc).__name__}: {redis_exc}. "
                f"Continuing without Redis state update; downstream "
                f"consumers may see stale data until next sweep."
            )

        # ── 11. Log to Supabase ──
        await log_regime_entry(
            symbol=symbol,
            source="sweep-light",
            regime=regime_label,
            regime_conf=regime_conf,
            markov_p_up=p_up,
            markov_p_dn=p_dn,
            kelly_f=effective_kelly,
            prev_regime=prev_regime,
            regime_shift=regime_shift,
        )

        return {
            "regime_label": regime_label,
            "regime_conf": regime_conf,
            "kelly_f": kelly_f,
            "effective_kelly": effective_kelly,
            "markov_p_up": p_up,
            "markov_p_dn": p_dn,
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "signal": trade_signal["signal"],
            "entry_price": trade_signal["entry_price"],
            "stop_loss": trade_signal["stop_loss"],
            "take_profit": trade_signal["take_profit"],
            "aggression": ev_result.aggression,
            "ev": ev_result.ev,
            "p_win": ev_result.p_win,
            "sharpe": kelly_result.sharpe_ratio,
            "total_return": round(total_return, 6),
            "annual_return": round(annual_return, 6),
            "max_drawdown_pct": round(max_drawdown_pct, 6),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4),
            "n_trades": len(returns),
            "passed_gates": passed_gates,
            "qualified": qualified,
        }

    # ── Stale-symbol background check ─────────────────────────────

    async def _stale_check_loop(self) -> None:
        """Every 60 seconds, re-process symbols whose last sweep is >20 min old."""
        while True:
            await asyncio.sleep(STALE_CHECK_INTERVAL_S)
            now = time.time()
            stale = [
                sym
                for sym, ts in list(self._last_ok.items())
                if now - ts > STALE_THRESHOLD_S
            ]
            if not stale:
                continue

            logger.info("Stale check: %d symbol(s) need refresh: %s", len(stale), stale)

            symbols = await fetch_active_symbols()
            sym_map = {s["symbol"]: s for s in symbols}

            for symbol in stale:
                sym = sym_map.get(symbol)
                if not sym:
                    continue

                if not is_market_open(sym.get("asset_class", "")):
                    continue

                prices = await fetch_latest_candles(sym, 200)
                if len(prices) < 81:
                    logger.warning(
                        "%s: stale retry — insufficient data (%d)", symbol, len(prices)
                    )
                    continue

                lock_key = f"lock:sweep:{symbol}"
                locked = await self._redis.set(
                    lock_key, "1", nx=True, ex=LIGHT_LOCK_TTL_S
                )
                if not locked:
                    continue

                try:
                    await self._process_symbol(sym, prices)
                    self._last_ok[symbol] = time.time()
                    logger.info("%s: stale retry successful", symbol)
                except Exception as exc:
                    logger.error("%s: stale retry failed: %s", symbol, exc)
                    await log_error(symbol, "sweep-light-stale", str(exc)[:1000])
                finally:
                    await self._redis.delete(lock_key)

                await asyncio.sleep(STAGGER_SECONDS)

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _split_cron(cron_raw: str) -> list[str]:
        """Split a possibly multi-expression cron string.

        Supports formats like:
          "5,20,35,50 * * * 1-5 / 5,35 * * * 0,6"  (weekday + weekend)
          "*/15 * * * *"                             (single expression)

        The ``/`` is a standalone token separating two 5-field expressions.
        Returns a list of individual 5-field cron expressions.
        """
        tokens = cron_raw.strip().split()
        if "/" in tokens:
            idx = tokens.index("/")
            before = " ".join(tokens[:idx])
            after = " ".join(tokens[idx + 1:])
            valid = []
            for expr in (before, after):
                fields = expr.split()
                if len(fields) == 5:
                    valid.append(expr)
                else:
                    logger.warning("Skipping invalid cron sub-expression: %r", expr)
            return valid or ["*/15 * * * *"]

        if len(tokens) == 5:
            return [cron_raw.strip()]

        logger.warning("Skipping invalid cron expression: %r", cron_raw)
        return ["*/15 * * * *"]

    async def _load_brick_params(self, symbol: str) -> tuple[float, int, int]:
        """Load brick_size, sl_bricks, tp_bricks from Redis config or defaults.

        Uses ``hmget`` to fetch only the three fields actually needed rather
        than the full ~28-field hash — cheaper on every run, and on a cold
        start (new Redis instance) it returns ``None`` values that fall back
        to the defaults below.
        """
        try:
            brick_size_s, sl_s, tp_s = await self._redis.hmget(
                f"trading:config:{symbol}", "brick_size", "sl_bricks", "tp_bricks"
            )
            return (
                float(brick_size_s) if brick_size_s is not None else 0.5,
                int(sl_s) if sl_s is not None else 3,
                int(tp_s) if tp_s is not None else 5,
            )
        except Exception:
            pass
        return (0.5, 3, 5)
