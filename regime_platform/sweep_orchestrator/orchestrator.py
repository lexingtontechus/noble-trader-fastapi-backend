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

# ── TimesFM fetcher with 15-minute cache ────────────────────────────────
# Mirrors supabase/functions/_shared/utils.ts:timesfmPost()
# Using a simple in-memory cache (not Redis for this hot path)
_TIMESFM_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (p, timestamp)
_TIMESFM_CACHE_TTL_S = 900  # 15 minutes

async def _fetch_timesfm_cached(symbol: str, prices: list[float]) -> tuple[float, float] | None:
    """
    Fetch TimesFM forecast for a symbol with a 15-minute cache.

    Returns (p_timesfm, confidence) or None if unavailable.
    Uses the same inference URL as the edge function.
    """
    now = time.time()
    cached = _TIMESFM_CACHE.get(symbol)
    if cached:
        p, ts = cached
        if now - ts < _TIMESFM_CACHE_TTL_S:
            logger.debug(
                f"[sweep:orch] {symbol}: TimesFM cache hit (p={p:.4f}, age={now-ts:.0f}s)"
            )
            return p, now - ts

    # Cache miss or expired — fetch from inference API
    try:
        import httpx
        TIMESFM_INFERENCE_URL = (
            os.getenv("TIMESFM_INFERENCE_URL") or
            os.getenv("TIMESFM_URL") or
            "http://localhost:8000/inference"
        )

        if not TIMESFM_INFERENCE_URL:
            logger.warning(
                f"[sweep:orch] {symbol}: TIMESFM_INFERENCE_URL not set — skipping TimesFM"
            )
            return None

        # Use last 500 bars (same as heavy sweep)
        price_samples = prices[-500:] if len(prices) >= 500 else prices

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.post(
                TIMESFM_INFERENCE_URL,
                json={"symbol": symbol, "prices": price_samples},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        # TimesFM returns { "predictions": { "BTC": 0.72 } }
        p_timesfm = float(data.get("predictions", {}).get(symbol, 0.5))
        confidence = abs(p_timesfm - 0.5)

        # Cache result
        _TIMESFM_CACHE[symbol] = (p_timesfm, now)
        logger.info(
            f"[sweep:orch] {symbol}: TimesFM fetched (p={p_timesfm:.4f}, "
            f"confidence={confidence:.4f})"
        )
        return p_timesfm, confidence

    except Exception as e:
        logger.warning(
            f"[sweep:orch] {symbol}: TimesFM fetch failed ({e}) — proceeding without"
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

            # Fetch TimesFM (cached 15min)
            p_timesfm, timesfm_confidence = await _fetch_timesfm_cached(symbol, prices)

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

        # ── 5a. Qualification gates (NEW) ──
        # Only publish qualified signals to signal.raw.noble_trader
        signal_is_qualifiable = trade_signal["signal"] not in ("neutral", "unknown")

        if p_timesfm is not None:
            confidence = abs(p_timesfm - 0.5)
            timesfm_too_uncertain = confidence < 0.05  # require certainty
        else:
            confidence = None
            timesfm_too_uncertain = False

        min_regime_conf = 0.60
        min_kelly = 0.02

        passed_gates = {
            "signal": trade_signal["signal"],
            "signal_qualifiable": signal_is_qualifiable,
            "regime_conf": regime_conf,
            "regime_conf_ok": regime_conf >= min_regime_conf,
            "kelly_f": kelly_f,
            "kelly_min_ok": kelly_f >= min_kelly,
            "timesfm_missing": p_timesfm is None,
            "timesfm_confidence": round(confidence, 4) if confidence is not None else None,
            "timesfm_too_uncertain": timesfm_too_uncertain,
            "ev_allowed": ev_result.allowed,
        }

        # Check if signal passes all qualification gates
        # TimesFM is now required for qualification (p_timesfm cannot be None)
        qualified = (
            signal_is_qualifiable
            and regime_conf >= min_regime_conf
            and kelly_f >= min_kelly
            and p_timesfm is not None  # TimesFM required
            and confidence >= 0.05     # Certainty required
            and ev_result.allowed
        )

        if not qualified:
            logger.info(
                f"[sweep:orch] {symbol}: SKIPPED — signal '{trade_signal['signal']}', "
                f"regime_conf={regime_conf:.3f} (< {min_regime_conf}), kelly_f={kelly_f:.4f} "
                f"(< {min_kelly}), timesfm_conf={round(confidence, 4) if confidence is not None else None}, "
                f"ev_allowed={ev_result.allowed}"
            )
            # Still return a result dict for logging
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
                "ev": ev_result.ev,
                "p_win": ev_result.p_win,
                "passed_gates": passed_gates,
                "qualified": False,
            }

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
        # Single pipelined round-trip: config hash (+ TTL), heartbeat, and
        # the DOOM-agent pub/sub heartbeat. Bundling cuts 3 round-trips to 1.
        decimals = _price_decimals(symbol)
        config_key = f"trading:config:{symbol}"
        heartbeat_key = f"trading:heartbeat:{symbol}"
        agent_channel = f"agent:{symbol}"
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
            "p_timesfm": round(p_timesfm, 4) if p_timesfm is not None else None,
            "price_decimals": decimals,
            # config_ts (float seconds) lets get_config()'s staleness check
            # treat orchestrator-written keys consistently with the TTL below.
            "config_ts": round(time.time(), 3),
        }
        if cal:
            config_dict["calibration_bias"] = round(cal["bias"], 4)
            config_dict["calibration_status"] = cal["status"]

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
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(config_key, mapping=config_dict)
        pipe.expire(config_key, CONFIG_TTL)   # 1h TTL — aligns with get_config staleness check
        pipe.set(heartbeat_key, str(now_ms), ex=ttl)
        pipe.publish(agent_channel, agent_payload)  # Always publish to agent:{symbol} (liveness)
        if qualified:
            pipe.publish("signal.raw.noble_trader", agent_payload)  # Only qualified signals
        await pipe.execute()

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
