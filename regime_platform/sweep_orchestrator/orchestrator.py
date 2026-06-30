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
from regime_platform.sweep_orchestrator.market_hours import is_market_open
from regime_platform.sweep_orchestrator.price_fetcher import (
    fetch_latest_candles,
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

        # Shared engine instances
        self._sizer = PositionSizer()
        self._ev_engine = EVEngine()

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
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sym in symbols:
            cron = sym.get("regime_cron", "*/15 * * * *")
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
        active = [s for s in symbols if is_market_open(s.get("asset_class", ""))]
        if not active:
            return

        logger.debug(
            "Running sweep group [%s]: %d symbols (%d filtered)",
            cron_expr,
            len(active),
            len(symbols) - len(active),
        )

        # Parallel price fetch (latest 200 bars)
        price_futs = [fetch_latest_candles(s, 200) for s in active]
        price_results = await asyncio.gather(*price_futs, return_exceptions=True)

        valid_prices: dict[str, list[float]] = {}
        for sym, prices in zip(active, price_results):
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

            result: dict[str, Any] = {"regime_label": "error", "kelly_f": 0}
            try:
                result = await self._process_symbol(sym, prices)
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
                    "kelly_f": round(result["kelly_f"], 6),
                    "markov_p_up": round(result["markov_p_up"], 4),
                    "markov_p_dn": round(result["markov_p_dn"], 4),
                    "sweep_window": "light",
                    "sweep_duration_ms": int((time.time() - start_ts) * 1000),
                })
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
        kelly_result = self._sizer.size(
            PositionRequest(
                returns=returns,
                kelly_fraction=0.5,
                target_vol=0.15,
                regime=regime,
            )
        )
        kelly_f = kelly_result.recommended_f

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

        # ── 6. Calibration bias ──
        cal = await fetch_calibration_bias(symbol)

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
        decimals = _price_decimals(symbol)
        config_key = f"trading:config:{symbol}"
        now_ms = int(time.time() * 1000)

        config_dict: dict[str, str | float] = {
            "regime": regime_label,
            "regime_conf": round(regime_conf, 4),
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "markov_p_up": round(p_up, 4),
            "markov_p_dn": round(p_dn, 4),
            "kelly_f": round(kelly_f, 6),
            "effective_kelly": round(effective_kelly, 6),
            "last_update": now_ms,
            "sweep_timestamp": now_ms,
            "signal": trade_signal["signal"],
            "entry_price": round(trade_signal["entry_price"], decimals),
            "stop_loss": round(trade_signal["stop_loss"], decimals),
            "take_profit": round(trade_signal["take_profit"], decimals),
            "ev": round(ev_result.ev, 4),
            "ev_per_dollar": round(ev_result.ev_per_dollar, 4),
            "p_win": round(ev_result.p_win, 4),
            "p_regime": round(ev_result.p_regime, 4),
            "p_markov": round(ev_result.p_markov, 4),
            "aggression": ev_result.aggression,
            "ev_scale": round(ev_result.ev_scale, 4),
            "regime_shift": "true" if regime_shift else "false",
            "prev_regime": prev_regime or "",
            "shift_at": str(now_ms) if regime_shift else "0",
            "price_decimals": decimals,
        }
        if cal:
            config_dict["calibration_bias"] = round(cal["bias"], 4)
            config_dict["calibration_status"] = cal["status"]

        await self._redis.hset(config_key, mapping=config_dict)

        # ── 9. Heartbeat ──
        ttl = sym.get("heartbeat_ttl_seconds", 900)
        await self._redis.set(
            f"trading:heartbeat:{symbol}",
            str(now_ms),
            ex=ttl,
        )

        # ── 10. Pub/Sub heartbeat for DOOM agents ──
        try:
            await self._redis.publish(
                f"agent:{symbol}",
                json.dumps({
                    "type": "heartbeat",
                    "symbol": symbol,
                    "ts": now_ms,
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
                    "ev": round(ev_result.ev, 4),
                    "p_win": round(ev_result.p_win, 4),
                    "regime_shift": "true" if regime_shift else "false",
                    "prev_regime": prev_regime or "",
                }),
            )
        except Exception:
            pass  # non-critical

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
            "markov_p_up": p_up,
            "markov_p_dn": p_dn,
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "signal": trade_signal["signal"],
            "ev": ev_result.ev,
            "p_win": ev_result.p_win,
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

    async def _load_brick_params(self, symbol: str) -> tuple[float, int, int]:
        """Load brick_size, sl_bricks, tp_bricks from Redis config or defaults."""
        try:
            config = await self._redis.hgetall(f"trading:config:{symbol}")
            if config:
                return (
                    float(config.get("brick_size", 0.5)),
                    int(config.get("sl_bricks", 3)),
                    int(config.get("tp_bricks", 5)),
                )
        except Exception:
            pass
        return (0.5, 3, 5)
