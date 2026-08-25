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
import hashlib
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from regime_platform.core.settings import settings
from regime_platform.core.regime_engine import RegimeHMM, RegimeSnapshot
from regime_platform.core.position_sizer import PositionSizer, PositionRequest
from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL
from regime_platform.services.hmm import FeatureBuffer, HmmModel, latest_ta
from regime_platform.services.hmm import quote_fetcher as hmm_quote_fetcher
from regime_platform.services.hmm import ta_fetcher as hmm_ta_fetcher
from regime_platform.sweep_orchestrator.market_hours import is_market_open
from regime_platform.sweep_orchestrator.price_fetcher import (
    fetch_candles_for_symbols,
    fetch_tradingview_batch,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_active_symbols,
    log_regime_entry,
    log_sweep_result,
    log_signal_sim,
    log_renko_bricks,
    log_error,
    fetch_calibration_bias,
)
from regime_platform.sweep_orchestrator.heavy_sweep import HeavySweepMixin

logger = logging.getLogger("regime.sweep.orch")

# ─── Constants ────────────────────────────────────────────────────

STALE_THRESHOLD_S = 1200          # 20 minutes
STALE_CHECK_INTERVAL_S = 60       # check every 60 s
REFRESH_SYMBOLS_INTERVAL_S = 300  # re-read nt_symbol every 5 min
STAGGER_SECONDS = 5               # gap between symbols within a group

# ── Markov chain parameters (v5 EV engine) ──────────────────────────
# Rolling window for the Markov transition matrix.
#
# User decision 7.3: rolling window = 7 days.
# User decision 7.4: Markov chain states ≈ 200 bars (renko bricks).
#
# Both decisions point at the same parameter: the rolling window size.
# 7.3 sets it to 7 days; 7.4 says ≈200 bars. With the default 1h candle
# interval (see price_fetcher.py:68), 7 days = 7 × 24 = 168 bars — close
# to 200. We default to 168 (exact 7-day window) and let the user
# override via either MARKOV_WINDOW_BARS or MARKOV_WINDOW_DAYS env var.
#
# 7 days balances adaptivity (recovers from regime shifts within a week)
# against stability (avoids thrashing on noisy intraday windows).
MARKOV_WINDOW_DAYS = int(os.getenv("MARKOV_WINDOW_DAYS", "7"))
# Bars-per-day derived from candle interval. 1h candles = 24 bars/day.
# Override only if you change the candle interval away from 1h.
MARKOV_BARS_PER_DAY = int(os.getenv("MARKOV_BARS_PER_DAY", "24"))
MARKOV_WINDOW_BARS = int(
    os.getenv("MARKOV_WINDOW_BARS", str(MARKOV_WINDOW_DAYS * MARKOV_BARS_PER_DAY))
)
# Multi-step hold horizon (T^N). User decision 7.5: max steps = 10.
# Covers a typical TP-brick count for swing trades (5-10 bricks at 1
# brick/bar). Can be overridden per-call via compute_ev(n_steps=...).
# When None, the EV engine uses tp_bricks as N (brick velocity model:
# 1 brick = 1 bar).
MARKOV_HOLD_STEPS = int(os.getenv("MARKOV_HOLD_STEPS", "10"))

# ─── Qualification gate thresholds (configurable via env vars) ───────────
# These determine which sweep signals qualify and get sent to Discord
# directly (discord_delivery.py). Tune them via env vars to control signal
# volume vs. quality.
#
# IMPORTANT: the EV/RR gate (Gate 6) is the most common blocker. The EV
# engine requires rr >= MIN_RR (default 2.0), but with default brick
# params sl_bricks=3, tp_bricks=5, the actual RR is 5/3 = 1.67 — below
# the 2.0 minimum. Either:
#   (a) lower MIN_RR to 1.5 (via env: MIN_RR=1.5), or
#   (b) increase tp_bricks to 6+ in config/brick_params.yaml per symbol, or
#   (c) decrease sl_bricks to 2 in config/brick_params.yaml per symbol
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
TIMESFM_GATE_MODE = os.getenv("TIMESFM_GATE_MODE", "soft").lower()

# MARKOV_SOURCE_MODE (Part 1 Change B, 2026-08-25): which source feeds
# p_markov_hold_n's 3x3 {UP,DOWN,FLAT} transition matrix.
#   "legacy_hmm" (default): the standalone 5-state HmmModel fit on 9
#       unscaled features (regime_platform/services/hmm/model.py) — the
#       source of the "startprob_ must sum to 1 (got nan)" / "state
#       collapse detected" warnings confirmed live 2026-08-25 across most
#       symbols. Unchanged behavior.
#   "trend_hmm_shadow": compute BOTH the legacy value and the new
#       RegimeHMM-derived value (see regime_engine.py's
#       trend_transition_matrix_3state()), log a comparison line, but
#       still feed the LEGACY value to the live blend. No behavior
#       change — observation only.
#   "trend_hmm_live": feed the new RegimeHMM-derived value to the live
#       blend instead of the legacy model's.
# The new derivation reuses RegimeHMM's trend model — already stabilized
# by the 2026-08-24 feature-standardization fix (Change A) — instead of
# fitting a second, separate, numerically-unstable HMM.
MARKOV_SOURCE_MODE = os.getenv("MARKOV_SOURCE_MODE", "legacy_hmm").lower()

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

# ── TimesFM forecast → probability mapping ─────────────────────────────────
# The TimesFM point forecast (a price) is converted to p(up-move) via:
#   p_timesfm = sigmoid(k * relative_price_change)
# where k controls how aggressively price deltas map to confidence.
# k=20 → 1% move ≈ p=0.73, 5% move ≈ p=0.99 (EXCESSIVELY aggressive)
# k=10 → 1% move ≈ p=0.62, 5% move ≈ p=0.92 (more reasonable)
# k=5  → 1% move ≈ p=0.55, 5% move ≈ 0.99 (conservative)
# The high k=20 was found to systematically produce near-1.0 p_win values,
# causing chronic OVERCONFIDENT bias across all symbols (verified 2026-08-13).
TIMESFM_SIGMOID_K = float(os.getenv("TIMESFM_SIGMOID_K", "20.0"))

# Clamp range for p_timesfm — prevents extreme certainty from a single
# source dominating the logit-pooled blend. Defaults [0.001, 0.999] were
# too permissive — a 5% forecast at k=20 produced p=0.993, effectively
# bypassing downstream calibration. [0.05, 0.95] provides a sane ceiling.
TIMESFM_CLAMP_MIN = float(os.getenv("TIMESFM_CLAMP_MIN", "0.001"))
TIMESFM_CLAMP_MAX = float(os.getenv("TIMESFM_CLAMP_MAX", "0.999"))

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
        # sigmoid(k * relative_change) gives a smooth [0,1] mapping where
        # k controls aggressiveness. Defaults k=20 → ±5% ≈ 0.92 / 0.08.
        # See TIMESFM_SIGMOID_K, TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX above.
        if last_price <= 0:
            logger.warning(
                f"[sweep:orch] {symbol}: last_price<=0 ({last_price}) — "
                f"cannot compute p_timesfm"
            )
            return None
        rel_change = (forecast_value - last_price) / last_price
        # math.exp is faster than importing scipy; manual sigmoid is fine.
        _k = TIMESFM_SIGMOID_K
        _z = _k * rel_change
        if _z >= 0:
            p_timesfm = 1.0 / (1.0 + math.exp(-_z))
        else:
            ez = math.exp(_z)
            p_timesfm = ez / (1.0 + ez)
        # Clamp to [TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX] so confidence is
        # never exactly 0 or 1 (avoids edge cases in downstream EV/Kelly math).
        p_timesfm = max(TIMESFM_CLAMP_MIN, min(TIMESFM_CLAMP_MAX, p_timesfm))
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


def _build_markov_chain(
    prices: list[float],
    brick_size: float,
    window: int = MARKOV_WINDOW_BARS,
) -> tuple[MarkovChain | None, dict[str, dict[str, float]] | None, str]:
    """Build a MarkovChain from the price series and return (chain, T, current_state).

    DEPRECATED: This function is kept for backward compat with the renko
    router's /ev/compute endpoint, which builds a Markov chain from a
    user-supplied price list. The live sweep path now uses the multivariate
    HMM via ``HmmModel.emit_markov()`` (see ``_emit_hmm_markov`` below).

    Returns ``(None, None, "FLAT")`` when there are insufficient prices
    (<3) or brick_size is non-positive.
    """
    from regime_platform.services.statistics.markov_chain import MarkovChain
    if not prices or len(prices) < 3 or brick_size <= 0:
        return None, None, "FLAT"
    try:
        chain = MarkovChain(brick_size=brick_size, window=window)
        T = chain.fit_from_prices(prices)
        return chain, T, chain.current_state
    except Exception as exc:
        logger.warning(
            f"[sweep:orch] MarkovChain build failed ({type(exc).__name__}: {exc}) — "
            f"proceeding without Markov source"
        )
        return None, None, "FLAT"


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
    # ── OANDA metals (migration 031 multi-exchange symbols) ──────
    # XAUUSD / XAUEUR / XAUAUD / XAGUSD are quoted with 3-4 decimal
    # places on OANDA. Do NOT truncate them to the 2dp default — the
    # full value must survive the sweep → Discord → Supabase pipeline
    # (see nt_sweep_result.entry_price / stop_loss / take_profit).
    "XAUUSD": 3, "XAUEUR": 4, "XAUAUD": 4, "XAGUSD": 4,
    "XAU": 3, "XAG": 4,
}


def _price_decimals(symbol: str) -> int:
    return _PRICE_DECIMALS.get(symbol, 2)


# ─── Venue derivation ───────────────────────────────────────────────


def _apply_regime_kelly_floor(
    kelly_f: float,
    mu: float,
    regime_label: str,
    regime_conf: float,
    require_nonneg_mu: bool = True,
) -> tuple[float, bool, bool]:
    """Pure logic for the regime-based Kelly floor (fix #3b, 2026-08-22 audit).

    Hourly returns are noisy; mean historical return (``mu``) can be <= 0
    even in a strong-confidence regime, driving raw Kelly to 0. This floor
    exists so the sizer doesn't zero out sizing purely from sampling noise
    in a genuinely-edged regime — but it must not force-size a symbol whose
    MEASURED edge is actually negative just because regime confidence is
    high (confirmed live for AAPL: mu=-0.00007129 still got floored to
    kelly_f=0.0397, feeding a persistent overconfidence signal the
    calibration loop could detect but never fix). When
    ``require_nonneg_mu`` is True (default), the floor is withheld for
    ``mu < 0``.

    Returns:
        (new_kelly_f, floor_applied, floor_withheld_negative_mu)
    """
    if kelly_f != 0.0 or regime_label in ("unknown", "market_closed", "neutral"):
        return kelly_f, False, False
    if require_nonneg_mu and mu < 0.0:
        return kelly_f, False, True
    # Scale the floor by regime confidence (0.5-0.99 -> 0.02-0.06)
    floor = 0.04 * max(0.0, min(1.0, (regime_conf - 0.5) * 2.0))
    return round(min(floor, 0.06), 6), True, False


def _evaluate_regime_shift(
    ev_engine: EVEngine,
    prev_regime: str | None,
    prev_trend_probs: dict[str, float] | None,
    regime_label: str,
    trend_probs: dict[str, float] | None,
    alert_threshold: float,
) -> dict[str, Any] | None:
    """Pure decision logic for Layer 2's regime-shift alert (2026-08-23,
    worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3).

    Returns the ``shift_info`` dict from ``EVEngine.detect_regime_shift()``
    when a shift occurred AND its magnitude crosses ``alert_threshold``;
    returns ``None`` otherwise, including the cold-start case (no prior
    snapshot for this symbol yet — ``prev_regime``/``prev_trend_probs``
    are ``None`` on a symbol's first tick).
    """
    if prev_regime is None or prev_trend_probs is None:
        return None
    shift_info = ev_engine.detect_regime_shift(
        {"regime_label": prev_regime, "trend_probs": prev_trend_probs},
        {"regime_label": regime_label, "trend_probs": trend_probs or {}},
    )
    if shift_info["shifted"] and shift_info["magnitude"] >= alert_threshold:
        return shift_info
    return None


def _derive_venue(sym: dict[str, Any]) -> str:
    """Derive the execution venue / exchange from the symbol config.

    Returns the EXCHANGE name (not the data source). For TradingView
    symbols, the exchange is extracted from the ``tradingview_symbol``
    field which is in ``EXCHANGE:SYMBOL`` format (e.g., ``COINBASE:BTCUSD``
    → exchange is ``Coinbase``).

    Priority:
      1. exchange column (if set directly in nt_symbol)
      2. TradingView — extract exchange from ``EXCHANGE:SYMBOL`` format
      3. Hyperliquid — exchange is always ``Hyperliquid``
      4. Alpaca — exchange is always ``Alpaca``
      5. Yahoo — data source only, no exchange
      6. ``Unknown`` — fallback
    """
    # Check the exchange column first (migration 031)
    if sym.get("exchange") and sym["exchange"] != "UNKNOWN":
        ex = sym["exchange"]
        exchange_map = {
            "COINBASE": "Coinbase",
            "OANDA": "OANDA",
            "BINANCE": "Binance",
            "BYBIT": "Bybit",
            "KRAKEN": "Kraken",
            "AMEX": "AMEX",
            "NYSE": "NYSE",
            "NASDAQ": "NASDAQ",
            "TVC": "TradingView",
            "FXCM": "FXCM",
            "HYPERLIQUID": "Hyperliquid",
            "ALPACA": "Alpaca",
            "YAHOO": "Yahoo",
        }
        return exchange_map.get(ex.upper(), ex.title())

    # Fall back to extracting from tradingview_symbol
    tv_sym = sym.get("tradingview_symbol")
    if tv_sym and ":" in tv_sym:
        exchange = tv_sym.split(":")[0].strip()
        exchange_map = {
            "COINBASE": "Coinbase",
            "OANDA": "OANDA",
            "BINANCE": "Binance",
            "BYBIT": "Bybit",
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


# ─── Signal-sim capture (nt_signal_sim) ───────────────────────────


def _build_signal_sim_payload(
    *,
    symbol: str,
    venue: str,
    ts: str,
    signal: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    brick_size: float,
    sl_bricks: int,
    tp_bricks: int,
    p_win: float,
    ev: float,
    regime: str,
    regime_conf: float,
    source: str,
    qualified: bool,
    kelly_f: float | None = None,
    effective_kelly: float | None = None,
    predicted_p_win_raw: float | None = None,
) -> dict[str, Any]:
    """Build the ``nt_signal_sim`` payload for one generated signal.

    Called from ``_process_symbol`` for every non-neutral BUY/SELL
    signal (qualified or not) so the EOD signal simulator can evaluate
    it later. Prices are rounded to 6dp (full value) — same rule as the
    rest of the signal path (2026-08-05 price-precision fix); do NOT
    round to ``_price_decimals()`` (2dp default for XAUUSD truncates).
    Outcome/exit/PNL fields are intentionally absent here — the EOD
    resolver fills them later.

    ``kelly_f`` / ``effective_kelly`` (kelly × EV scale) are the sizing
    inputs for the Part II paper portfolio (migration 105 adds the
    columns); nullable so legacy callers are unaffected.

    ``predicted_p_win_raw`` (2026-08-13) is the PRE-enforcement p_win and
    is included in the payload only when
    ``settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW`` is enabled (the DB
    column must exist first — see supabase/migrations/114_add_predicted_p_win_raw.sql).
    """
    payload = {
        "symbol": symbol,
        "venue": venue,
        "ts": ts,
        "signal": signal,
        "entry_price": round(float(entry_price), 6),
        "stop_loss": round(float(stop_loss), 6),
        "take_profit": round(float(take_profit), 6),
        "brick_size": brick_size,
        "sl_bricks": sl_bricks,
        "tp_bricks": tp_bricks,
        "p_win": round(float(p_win), 4),
        "ev": round(float(ev), 4),
        "regime": regime,
        "regime_conf": round(float(regime_conf), 4),
        "source": source,
        "qualified": qualified,
        "kelly_f": round(float(kelly_f), 6) if kelly_f is not None else None,
        "effective_kelly": round(float(effective_kelly), 6) if effective_kelly is not None else None,
    }
    if (
        settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW
        and predicted_p_win_raw is not None
    ):
        payload["predicted_p_win_raw"] = round(float(predicted_p_win_raw), 4)
    return payload


# ─── Orchestrator ─────────────────────────────────────────────────


class SweepOrchestrator(HeavySweepMixin):
    """Reads symbols from Supabase, schedules sweep groups, handles stale retry.

    Inherits heavy-sweep methods from HeavySweepMixin (Phase B):
      • run_heavy_sweep_for_symbol(sym, prices=None)
      • _heavy_sweep_group(cron_expr, symbols)  — cron tick handler
      • _renko_grid_search_sync / _regime_sync / _kelly_sync  — CPU-bound helpers
      • _persist_brick_params_to_yaml / _push_heavy_signal
    """

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None

        # Per-symbol in-process lock (2026-08-25, Redis deprecation) — was a
        # Redis SET NX/DELETE distributed lock ("Layer 1, same as edge
        # functions") guarding against light sweep and heavy sweep hitting
        # the same symbol concurrently on their independent cron schedules.
        # The deployment is a single process, so the race no longer needs
        # to be distributed, just serialized in-process. Created lazily per
        # symbol on first use.
        self._sweep_locks: dict[str, asyncio.Lock] = {}

        # Track last-successful-sweep timestamps for stale detection
        self._last_ok: dict[str, float] = {}
        # Last-known regime labels per symbol (for shift detection)
        self._last_regime: dict[str, str] = {}
        # Last-known HMM trend-posterior dict per symbol (Layer 2,
        # 2026-08-23 — worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3): the
        # label alone isn't enough to compute a real shift MAGNITUDE via
        # EVEngine.detect_regime_shift(), which needs the full posterior
        # distribution on both sides.
        self._last_trend_probs: dict[str, dict[str, float]] = {}
        # ── Markov EMA smoothing (2026-08-22 audit fix #3a) ────────────
        # Per-symbol EMA of the raw T^N P_markov_hold_N estimate. See
        # _smoothed_markov_hold_prob() below and
        # worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2 Mechanism 1
        # (AAPL/US500 current_state flipping tick-to-tick swung p_win
        # between ~0.47 and ~0.85 even though the market wasn't moving).
        self._markov_ema: dict[str, float] = {}
        # ── Regime-HMM stability (2026-08-23, worklog/
        # 20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md R1) ──────────────
        # Per-symbol label→mean maps from the LAST fit, fed back into the
        # next fit() call so RegimeHMM can match new components to their
        # previous label instead of re-sorting blind (fix #2 — stops
        # AAPL-style bull/bear relabeling with no price move). Per-symbol
        # EMA of the trend posterior dict, mirroring _markov_ema — feeds
        # ONLY the EV blend (compute_p_regime), not the regime_label/
        # regime_shift bookkeeping, which stays driven by the raw
        # (label-continuity-fixed) prediction.
        self._regime_label_means: dict[str, dict[str, dict[str, float]]] = {}
        self._regime_trend_ema: dict[str, dict[str, float]] = {}
        # Per-symbol cache of the Bayesian-shrunk realized win rate
        # (p_historical EV-blend source, R3) — same TTL pattern as
        # _TIMESFM_CACHE, since calibration bias only actually changes on
        # the 4h EOD-correction / daily EOD-resolver cadence, not every
        # 5-minute sweep tick.
        self._p_historical_cache: dict[str, tuple[Optional[float], float]] = {}
        # ── Signal cooldown tracking ──────────────────────────────────
        # Maps (symbol, direction) → timestamp of last PUBLISHED signal.
        # Used to suppress duplicate signals within the cooldown window.
        # Key: (symbol, "buy" or "sell"), Value: time.time() timestamp.
        self._last_qualified_signal: dict[tuple[str, str], float] = {}

        # ── Content-hash dedup (D5) ──────────────────────────────────
        # Even after cooldown expires, the same signal content (same
        # regime + price-band + brick-params) should NOT be republished.
        # We hash a small subset of the agent_payload to detect content
        # duplication independent of the cooldown timer.
        # Key: (symbol, direction), Value: hash string of last published content.
        self._last_signal_hash: dict[tuple[str, str], str] = {}

        # ── Regime-shift tracking for the agent heartbeat ─────────────
        # Per-symbol list of epoch-ms timestamps when a regime shift was
        # detected. Used to compute `shifts_24h` (count of shifts in the
        # trailing 24h) for the heartbeat payload the agent consumes.
        # (The agent's NobleTraderHeartbeat schema requires shift_at +
        # shifts_24h; the proxy forwards the payload verbatim, so the
        # backend must supply them.)
        self._shift_counts: dict[str, list[float]] = {}

        # ── Emergency heavy-sweep cooldown (intraday correction, 2026-08-13) ──
        # Last time an emergency heavy sweep re-optimised each symbol (epoch
        # seconds). The 4h intraday correction passes skip symbols swept within
        # EOD_HEAVY_SWEEP_COOLDOWN_HOURS to bound backtest cost; the nightly
        # 00:30 pass is NOT subject to the cooldown.
        self._heavy_sweep_last: dict[str, float] = {}

        # Shared engine instances
        self._sizer = PositionSizer()
        self._ev_engine = EVEngine()
        # ── Multivariate HMM (replaces discrete MarkovChain) ─────────
        # The HMM is the p_markov source. It boots from the sweep's
        # existing /api/price/batch data (log_return + 5 TA indicators
        # computed locally) and enriches with live /api/quote + /api/ta
        # data each sweep tick.
        self._hmm_buffer = FeatureBuffer()
        self._hmm_model = HmmModel(self._hmm_buffer)
        self._hmm_bootstrapped: set[str] = set()
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
            f"TIMESFM_SIGMOID_K={TIMESFM_SIGMOID_K}, "
            f"TIMESFM_CLAMP=[{TIMESFM_CLAMP_MIN}, {TIMESFM_CLAMP_MAX}]"
        )

    # ── Public lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler and background checks."""
        # ── TimesFM startup self-check (v5) ───────────────────────────
        # Fail loud when TimesFM env vars are unset — every fetch will
        # 401 silently and p_timesfm will be None for all symbols. We
        # don't hard-abort (the orchestrator can still run without
        # TimesFM, blending 3 sources instead of 4), but we log a
        # prominent warning so ops can fix the env vars before relying
        # on the signals. Set TIMESFM_REQUIRED=1 to make this fatal.
        _timesfm_keys = os.getenv("TIMESFM_API_KEYS")
        _timesfm_url = (
            os.getenv("TIMESFM_INFERENCE_URL")
            or os.getenv("TIMESFM_URL")
            or os.getenv("TIMESFM_SERVICE_URL")
        )
        if not _timesfm_keys:
            msg = (
                "TIMESFM_API_KEYS env var is not set — every TimesFM fetch "
                "will 401 and p_timesfm will be None for all symbols. The "
                "EV engine will fall back to a 3-source blend (no TimesFM), "
                "reducing P_win accuracy. Set TIMESFM_API_KEYS or expect "
                "degraded signal quality."
            )
            if os.getenv("TIMESFM_REQUIRED", "0") == "1":
                logger.error(f"FATAL: {msg}")
                raise RuntimeError(msg)
            else:
                logger.warning(f"WARNING: {msg}")
        elif not _timesfm_url:
            logger.warning(
                "TIMESFM_API_KEYS is set but no TIMESFM_INFERENCE_URL / "
                "TIMESFM_URL env var found — will default to "
                "http://localhost:8000/forecast (the gateway). If the "
                "gateway is not running, p_timesfm will be None."
            )
        else:
            logger.info(
                f"TimesFM startup OK — API keys set, URL={_timesfm_url}"
            )

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

        # EOD signal-lookback: forward-resolve pending nt_signal_sim rows
        # then run the calibration self-correction audit. Daily 00:32 UTC —
        # after the market day closes (forex 22:00 UTC / equities 21:00 UTC)
        # and before the 05:00 UTC weekend heavy-sweep window. Offset 2
        # minutes off the light sweep's `*/5 * * * *` grid (2026-08-24):
        # at :30 exactly, both jobs fired their own TradingView batch calls
        # concurrently and the resolver's requests got 429'd — confirmed
        # live. This doesn't guarantee no overlap (a slow resolver run can
        # still run into the next light-sweep tick), but removes the
        # guaranteed every-single-day collision at the shared :30 mark.
        self._scheduler.add_job(
            self._run_eod_lookback,
            trigger=CronTrigger.from_crontab("32 0 * * *"),
            id="eod-lookback",
            name="EOD signal lookback (resolve + correct)",
            misfire_grace_time=3600,
            coalesce=True,
        )

        # Intraday correction-only passes (2026-08-13 plan, B = every 4h):
        # recompute bias + rewrite nt_eod_actions at 04:30/08:30/12:30/16:30/20:30
        # UTC so a mid-day regime shift moves the bias in hours, not 24h. No
        # resolver/retention (the nightly owns those); emergency heavy sweeps
        # run only for newly-flagged symbols subject to the cooldown.
        if settings.EOD_CORRECTION_INTRADAY_ENABLED:
            interval = max(1, int(getattr(settings, "EOD_CORRECTION_INTERVAL_HOURS", 4) or 4))
            for k in range(1, 24 // interval):
                hour = (0 + k * interval) % 24
                self._scheduler.add_job(
                    self._run_eod_correction_only,
                    trigger=CronTrigger(minute="30", hour=str(hour)),
                    id=f"eod-correction-intraday-{hour:02d}",
                    name=f"EOD intraday correction ({hour:02d}:30 UTC)",
                    misfire_grace_time=3600,
                    coalesce=True,
                )
            logger.info(
                "Intraday EOD correction scheduled every %dh at hours: %s",
                interval,
                [(k * interval) % 24 for k in range(1, 24 // interval)],
            )

        # Layer 1 auto-tuner (2026-08-23, worklog/PARAMETER_TUNING_PLAYBOOK.md
        # §2.5.2): daily, 01:30 UTC — an hour after the nightly EOD lookback
        # (00:30) so resolved outcomes are fresh, and clear of the 4h
        # intraday correction slots (:30 past hours 4/8/12/16/20). Gated on
        # the SAME kill switch write_param() itself checks, so a disabled
        # job doesn't even bother computing the health metric every day.
        if getattr(settings, "TUNING_AUTONOMY_ENABLED", True):
            self._scheduler.add_job(
                self._run_auto_tuning,
                trigger=CronTrigger.from_crontab("30 1 * * *"),
                id="auto-tuner-daily",
                name="Layer 1 auto-tuner (daily)",
                misfire_grace_time=3600,
                coalesce=True,
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
        """Fetch active symbols from Supabase and (re)schedule groups.

        Also synchronises the local brick_params.yaml ``symbols:`` section
        with ``nt_symbol.active=true`` — adds seed defaults for newly-active
        symbols and prunes entries for symbols no longer active. The heavy
        sweep (weekly) is still the only writer of OPTIMISED params; this
        sync only ensures the YAML has entries for every active symbol so
        the light sweep can read them between heavy-sweep runs.
        """
        symbols = await fetch_active_symbols()
        if not symbols:
            logger.warning("No active symbols found; sweep groups will be empty")
            return

        # Sync YAML brick_params to nt_symbol.active=true
        try:
            from regime_platform.config.brick_params import sync_yaml_to_nt_symbol
            sync_result = sync_yaml_to_nt_symbol(symbols)
            if sync_result["added"] or sync_result["pruned"]:
                logger.info(
                    "[sweep:orch] YAML sync: +%d -%d (~%d unchanged)",
                    sync_result["added"], sync_result["pruned"],
                    sync_result["unchanged"],
                )
        except Exception as exc:
            logger.warning(
                "[sweep:orch] YAML sync failed (%s) — proceeding with stale YAML",
                exc,
            )

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
            "Symbol registry refreshed: %d symbols in %d light-sweep cron group(s)",
            total,
            len(groups),
        )
        for cron_expr, sym_list in groups.items():
            logger.info(
                "  Light [%s]: %s",
                cron_expr,
                [s["symbol"] for s in sym_list],
            )

        # ── Phase B: group by sweep_cron for heavy sweep jobs ──────
        # Mirrors the light-sweep grouping above. Default sweep_cron is
        # '0 20 * * sun' (Sunday 20:00 UTC, migration 026) when NULL.
        # NOTE: APScheduler's from_crontab uses PYTHON weekday convention
        # (0=Monday) — the cron-conventional '0 20 * * 0' fires MONDAY.
        # Use the 'sun' name (or '6') for Sunday.
        heavy_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sym in symbols:
            cron_raw = sym.get("sweep_cron") or "0 20 * * sun"
            for cron in self._split_cron(cron_raw):
                heavy_groups[cron].append(sym)
        for cron in heavy_groups:
            heavy_groups[cron].sort(key=lambda s: s["symbol"])
        self._reschedule_heavy_groups(heavy_groups)

        heavy_total = sum(len(v) for v in heavy_groups.values())
        logger.info(
            "Heavy sweep registry: %d symbols in %d heavy-sweep cron group(s)",
            heavy_total,
            len(heavy_groups),
        )
        for cron_expr, sym_list in heavy_groups.items():
            logger.info(
                "  Heavy [%s]: %s",
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
                # Refresh args when the symbol set changed (H3): the old
                # `if job_id in existing: continue` froze the ORIGINAL
                # sym_list, so symbols added to nt_symbol were never swept
                # until the cron expr changed.
                try:
                    job = self._scheduler.get_job(job_id)
                    old_syms = job.args[1] if job and len(job.args) > 1 else None
                    new_syms = [s["symbol"] for s in sym_list]
                    if old_syms != new_syms:
                        self._scheduler.modify_job(
                            job_id,
                            args=[cron_expr, sym_list],
                            name=f"Light sweep [{cron_expr}] {len(sym_list)} symbols",
                        )
                        logger.info(
                            "Refreshed %s symbols (%d → %d): %s",
                            job_id, len(old_syms or []), len(new_syms), new_syms,
                        )
                except Exception as exc:
                    logger.warning("Failed to refresh %s args: %s", job_id, exc)
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

    def _reschedule_heavy_groups(
        self, groups: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Replace all heavy-sweep-* jobs to reflect current groups.

        Phase B — mirrors ``_reschedule_groups`` but for heavy sweep jobs.
        Heavy sweep jobs use the ``heavy-sweep-{cron_id}`` ID prefix so
        they don't collide with light sweep jobs (which use ``sweep-{cron_id}``).

        Heavy sweep jobs have a larger ``misfire_grace_time`` (1h) because
        they run infrequently (weekend 20:00 UTC by default) and we don't
        want a brief backend restart at 20:01 UTC to skip the entire week's
        heavy sweep.
        """
        assert self._scheduler is not None

        existing = {
            job.id
            for job in self._scheduler.get_jobs()
            if job.id.startswith("heavy-sweep-")
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
            job_id = f"heavy-sweep-{safe_id}"
            desired.add(job_id)

            if job_id in existing:
                # Refresh args when the symbol set changed (H3) — same
                # frozen-sym_list defect as the light sweep jobs.
                try:
                    job = self._scheduler.get_job(job_id)
                    old_syms = job.args[1] if job and len(job.args) > 1 else None
                    new_syms = [s["symbol"] for s in sym_list]
                    if old_syms != new_syms:
                        self._scheduler.modify_job(
                            job_id,
                            args=[cron_expr, sym_list],
                            name=f"Heavy sweep [{cron_expr}] {len(sym_list)} symbols",
                        )
                        logger.info(
                            "Refreshed %s symbols (%d → %d): %s",
                            job_id, len(old_syms or []), len(new_syms), new_syms,
                        )
                except Exception as exc:
                    logger.warning("Failed to refresh %s args: %s", job_id, exc)
                continue

            self._scheduler.add_job(
                func=self._heavy_sweep_group,
                trigger=CronTrigger.from_crontab(cron_expr),
                args=[cron_expr, sym_list],
                id=job_id,
                name=f"Heavy sweep [{cron_expr}] {len(sym_list)} symbols",
                misfire_grace_time=3600,   # 1h — don't skip the week's sweep
                coalesce=True,
            )
            logger.info(
                "Scheduled heavy: %s → %s",
                cron_expr,
                [s["symbol"] for s in sym_list],
            )

        for job_id in existing - desired:
            try:
                self._scheduler.remove_job(job_id)
                logger.info("Removed stale heavy-sweep job: %s", job_id)
            except Exception:
                pass

    # ── Sweep group runner ────────────────────────────────────────

    async def _run_eod_lookback(self) -> None:
        """EOD signal-lookback: resolve pending signals + self-correction.

        Scheduled daily 00:32 UTC (job id ``eod-lookback`` — offset 2 minutes
        off the light sweep's `*/5 * * * *` grid, 2026-08-24, to avoid a
        guaranteed daily TradingView-request collision at the shared :30
        mark). Runs the two EOD modules in order:

          1. ``eod_resolver.resolve_pending_signals`` — forward-resolve every
             ``nt_signal_sim`` row with ``outcome IS NULL`` against post-entry
             TDVA 1h bars (48-bar expiry, close-walk TP/SL) and write outcomes
             + calibration records.
          2. ``eod_correction.run_eod_correction`` — read per-symbol
             calibration bias; when |bias| > 0.10 write audit rows
             (emergency_heavy_sweep / bayesian_penalty / kelly_reduction),
             otherwise flag parameter decay.

        Best-effort: failures are logged and counted, never raised out of
        the scheduler tick.
        """
        from regime_platform.sweep_orchestrator.eod_resolver import (
            resolve_pending_signals,
        )
        from regime_platform.sweep_orchestrator.eod_correction import (
            run_eod_correction,
        )
        from regime_platform.sweep_orchestrator.paper_book import reconcile

        try:
            resolver_summary = await resolve_pending_signals(
                limit=settings.EOD_RESOLVE_LIMIT,
                max_total=settings.EOD_RESOLVE_MAX_TOTAL,
            )
            logger.info(
                "[eod:lookback] resolver done: %s",
                {k: v for k, v in resolver_summary.items() if k != "errors"},
            )
        except Exception as exc:
            logger.error("[eod:lookback] resolver failed: %s", exc)

        # Part II paper portfolio: reconcile open/close after resolution.
        try:
            paper_summary = await reconcile()
            logger.info(
                "[eod:lookback] paper reconcile done: opened=%d closed=%d errors=%d",
                paper_summary.get("opened", 0),
                paper_summary.get("closed", 0),
                paper_summary.get("errors", 0),
            )
        except Exception as exc:
            logger.error("[eod:lookback] paper reconcile failed: %s", exc)

        try:
            correction_summary = await run_eod_correction()
            logger.info(
                "[eod:lookback] correction done: %d symbols checked, %d actions, %d errors",
                correction_summary.get("checked", 0),
                len(correction_summary.get("actions", [])),
                correction_summary.get("errors", 0),
            )
        except Exception as exc:
            logger.error("[eod:lookback] correction failed: %s", exc)
            correction_summary = {}

        # ── Part III: emergency heavy sweeps (2026-08-10) ───────────────
        # The EOD correction pass RECOMMENDS `emergency_heavy_sweep` for
        # over/underconfident symbols — this is the missing consumer that
        # actually runs them. Re-optimising brick params on fresh data is
        # the only mechanism that can genuinely move the bias back toward 0
        # (better params → better predictions → calibration re-converges).
        # Best-effort: each symbol is fetched + swept in isolation; a
        # failure is logged, never raised out of the tick.
        # L3 fix (2026-08-21): Also trigger emergency heavy sweeps for
        # degeneracy_breaker actions — symbols with p_win≈0 and no
        # calibration records stuck in the HMM collapse deadlock.
        emergency_syms = sorted({
            a.get("symbol")
            for a in (correction_summary.get("actions") or [])
            if a.get("action") in ("emergency_heavy_sweep", "degeneracy_breaker")
                and a.get("symbol")
        })
        if emergency_syms:
            logger.info(
                "[eod:lookback] emergency heavy sweeps requested: %s",
                emergency_syms,
            )
            await self._execute_emergency_heavy_sweeps(emergency_syms)

        # ── Part IV: retention / prune (2026-08-12) ───────────────────────
        # Keep nt_signal_sim bounded:
        #   1. DELETE resolved rows older than EOD_RETENTION_RESOLVED_DAYS.
        #   2. Mark unresolved rows older than EOD_RETENTION_STALE_DAYS as
        #      'expired' (they have sat past the 48-bar window with no touch;
        #      neither win nor loss → no calibration record, same convention
        #      as the resolver). This frees the pending partial index and
        #      stops the resolver re-picking stale rows forever.
        # Best-effort: failures are logged and counted, never raised.
        try:
            from regime_platform.sweep_orchestrator.eod_retention import (
                run_eod_retention,
            )
            retention_summary = await run_eod_retention()
            logger.info(
                "[eod:lookback] retention done: %s", retention_summary,
            )
        except Exception as exc:
            logger.error("[eod:lookback] retention failed: %s", exc)

    async def _execute_emergency_heavy_sweeps(self, emergency_syms: list[str]) -> None:
        """Run emergency heavy sweeps for the given symbols (best-effort).

        Shared by the nightly lookback (no cooldown) and the intraday
        correction passes (cooldown applied by the caller via
        ``_heavy_sweep_last``). Each symbol is fetched + swept in isolation;
        failures are logged, never raised out of the tick.
        """
        if not emergency_syms:
            return
        try:
            from regime_platform.sweep_orchestrator.supabase_client import (
                fetch_active_symbols,
            )
            all_symbols = await fetch_active_symbols()
            sym_map = {s.get("symbol"): s for s in all_symbols}
            for sym_name in emergency_syms:
                sym = sym_map.get(sym_name)
                if not sym:
                    logger.warning(
                        "[eod:lookback] emergency sweep: %s not in active symbols — skipped",
                        sym_name,
                    )
                    continue
                try:
                    result = await self.run_heavy_sweep_for_symbol(sym)
                    self._heavy_sweep_last[sym_name] = time.time()
                    logger.info(
                        "[eod:lookback] emergency heavy sweep %s: %s",
                        sym_name,
                        result.skip_reason or result.error or "ok",
                    )
                except Exception as exc:
                    logger.error(
                        "[eod:lookback] emergency heavy sweep %s failed: %s",
                        sym_name, exc,
                    )
        except Exception as exc:
            logger.error(
                "[eod:lookback] emergency heavy sweep batch failed: %s", exc,
            )

    async def _run_auto_tuning(self) -> None:
        """Layer 1 autonomous parameter self-tuning (daily, 01:30 UTC).

        See ``regime_platform/sweep_orchestrator/auto_tuner.py`` and
        worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.2. Writes tuned values
        into the tuning registry (bounded, rate-limited, self-reverting on
        regression) for the Tier D pilot parameter family — it does NOT
        yet change live behaviour, since no read site has been migrated
        to consume the registry (that's separate, not-yet-done work).
        Best-effort: failures are logged, never raised.
        """
        from regime_platform.sweep_orchestrator.auto_tuner import run_auto_tuning

        try:
            summary = await run_auto_tuning()
            logger.info(
                "[auto-tuner] done: metric=%s, checked=%d, evaluated=%d, "
                "reverted=%d, tuned=%d, target=%s, skipped=%s, errors=%d",
                summary.get("metric_now"), summary.get("checked", 0),
                summary.get("evaluated", 0), summary.get("reverted", 0),
                summary.get("tuned", 0), summary.get("target"),
                summary.get("skipped"), summary.get("errors", 0),
            )
        except Exception as exc:
            logger.error("[auto-tuner] failed: %s", exc)

    async def _write_regime_shift_alert(self, symbol: str, shift_info: dict[str, Any]) -> None:
        """Write a ``regime_shift_alert`` row to ``nt_eod_actions`` (Layer 2,
        2026-08-23 — worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3, migration 118).

        Detection-only: flags a large regime-posterior shift for a
        human/agent to review Tier B/C parameters (EV blend weights, gate
        thresholds) against fresh live evidence — mirrors the live-
        evidence-gathering, bounded-fix, test-gated workflow this session
        used manually, but triggered by a detected shift instead of a
        human asking. Does NOT itself change any parameter. Best-effort:
        failures are logged, never raised into the sweep tick.
        """
        try:
            from regime_platform.sweep_orchestrator.supabase_client import _insert_row
            row = {
                "symbol": symbol,
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "regime_shift_alert",
                "bias": round(float(shift_info.get("magnitude") or 0.0), 4),
                "detail": shift_info,
            }
            ok = await _insert_row("nt_eod_actions", row)
            if ok:
                logger.warning(
                    "[regime-shift] %s: ALERT — %s shift, magnitude=%.4f, "
                    "confidence=%.4f (%s -> %s)",
                    symbol, shift_info.get("direction"),
                    shift_info.get("magnitude", 0.0), shift_info.get("confidence", 0.0),
                    shift_info.get("prev_label"), shift_info.get("curr_label"),
                )
        except Exception as exc:
            logger.warning("[regime-shift] %s: alert write failed: %s", symbol, exc)

    async def _run_eod_correction_only(self) -> None:
        """Intraday calibration correction pass (2026-08-13 plan, B = every 4h).

        Scheduled at 04:30/08:30/12:30/16:30/20:30 UTC (job ids
        ``eod-correction-intraday-*``). Recomputes per-symbol calibration
        bias over the rolling window and rewrites ``nt_eod_actions`` so a
        mid-day regime shift moves the bias in hours, not 24h. Emergency
        heavy sweeps run only for NEWLY-flagged symbols, subject to
        ``EOD_HEAVY_SWEEP_COOLDOWN_HOURS`` (the nightly pass is exempt).

        Does NOT run the resolver or retention — the nightly 00:30 lookback
        owns those. Best-effort: failures are logged, never raised.
        """
        from regime_platform.sweep_orchestrator.eod_correction import (
            run_eod_correction,
        )

        try:
            correction_summary = await run_eod_correction(raw_bias_mode=True)
            logger.info(
                "[eod:correction-intraday] done: %d symbols checked, %d actions, %d errors",
                correction_summary.get("checked", 0),
                len(correction_summary.get("actions", [])),
                correction_summary.get("errors", 0),
            )
        except Exception as exc:
            logger.error("[eod:correction-intraday] correction failed: %s", exc)
            return

        # L3 fix (2026-08-21): Also trigger emergency heavy sweeps for
        # degeneracy_breaker actions (symbols with p_win≈0 and no calibration).
        emergency_syms = sorted({
            a.get("symbol")
            for a in (correction_summary.get("actions") or [])
            if a.get("action") in ("emergency_heavy_sweep", "degeneracy_breaker")
                and a.get("symbol")
        })
        if not emergency_syms:
            return

        cooldown_h = float(getattr(settings, "EOD_HEAVY_SWEEP_COOLDOWN_HOURS", 12) or 12)
        now = time.time()
        to_sweep = [
            s for s in emergency_syms
            if now - self._heavy_sweep_last.get(s, 0.0) >= cooldown_h * 3600.0
        ]
        skipped = [s for s in emergency_syms if s not in to_sweep]
        if skipped:
            logger.info(
                "[eod:correction-intraday] heavy-sweep cooldown skips: %s",
                skipped,
            )
        if to_sweep:
            logger.info(
                "[eod:correction-intraday] emergency heavy sweeps requested: %s",
                to_sweep,
            )
            await self._execute_emergency_heavy_sweeps(to_sweep)

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

        # ── Price fetch (TDVA batch — Phase B consolidation) ──────
        # All symbols route through TDVA's POST /api/price/batch endpoint.
        # The fetcher auto-chunks into groups of 10 (TV hard limit per
        # request) and makes ⌈N/10⌉ HTTP calls for N symbols.
        valid_prices: dict[str, list[float]] = {}

        try:
            tv_results = await fetch_tradingview_batch(active, 200)
            for sym_name, prices in tv_results.items():
                if len(prices) < 81:
                    logger.warning(
                        "%s: insufficient data (%d bars, need >=81)",
                        sym_name, len(prices),
                    )
                    continue
                valid_prices[sym_name] = prices
        except Exception as exc:
            logger.warning("TDVA batch fetch failed: %s", exc)

        if not valid_prices:
            return

        # ── HMM: bootstrap + batch fetch + fit ─────────────────────────
        # On first sweep for a symbol, bootstrap the feature buffer from
        # the price history (log_return + 5 TA indicators computed locally).
        # Then fetch live /api/quote/batch (⌈N/10⌉ batched POSTs — TVDA
        # hard-limits to 10 symbols/request) and compute the 5 TA values
        # LOCALLY from the same batch OHLCV (TVDA has no batch TA endpoint;
        # the old 11× per-symbol /api/quote + /api/ta fan-out tripped the
        # RapidAPI 429 rate limit → "fetched 0/11 symbols" every tick).
        # Then refit the HMM. This replaces the old discrete MarkovChain.
        hmm_symbols = [
            s for s in active if s["symbol"] in valid_prices
        ]

        # Bootstrap any symbols not yet bootstrapped
        for sym in hmm_symbols:
            symbol = sym["symbol"]
            if symbol not in self._hmm_bootstrapped:
                pushed = self._hmm_model.bootstrap_from_ohlcv(
                    symbol, valid_prices[symbol],
                )
                if pushed > 0:
                    self._hmm_bootstrapped.add(symbol)

        # Batch fetch: /api/quote/batch + local TA computation (~1-2s)
        prev_closes = {
            s["symbol"]: valid_prices[s["symbol"]][-1]
            for s in hmm_symbols
        }
        try:
            await hmm_quote_fetcher.fetch_and_update(
                hmm_symbols, self._hmm_buffer, prev_closes,
            )
        except Exception as exc:
            logger.warning("HMM quote fetch failed: %s", exc)

        try:
            await hmm_ta_fetcher.fetch_and_update(
                hmm_symbols, self._hmm_buffer,
                closes_by_symbol=valid_prices,
            )
        except Exception as exc:
            logger.warning("HMM TA fetch failed: %s", exc)

        # Refit HMM for each symbol (cheap: Baum-Welch on 300 bars × 9 features)
        for sym in hmm_symbols:
            self._hmm_model.fit(sym["symbol"])

        # Sequential processing with stagger
        symbols_to_process = [s for s in active if s["symbol"] in valid_prices]
        for i, sym in enumerate(symbols_to_process):
            symbol = sym["symbol"]
            prices = valid_prices[symbol]
            start_ts = time.time()

            # Per-symbol in-process lock — see __init__ for context.
            lock = self._sweep_locks.setdefault(symbol, asyncio.Lock())
            if lock.locked():
                logger.info("%s: locked — skipping (concurrent sweep)", symbol)
                continue
            await lock.acquire()

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
                result = await self._process_symbol(sym, prices, p_timesfm, source="sweep-light")
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
                    "regime_shift": result.get("regime_shift", False),
                    "prev_regime": result.get("prev_regime", ""),
                    "size_mult": result.get("size_mult", 1.0),
                    # ── TA indicators (migration 036) ──────────────────────
                    # Populated from the HMM feature buffer's latest bar
                    # for this symbol (captured in _process_symbol §3a).
                    # All nullable — None when buffer is empty or TA fetch
                    # failed for this tick.
                    "ta_adx": result.get("ta_adx"),
                    "ta_ao": result.get("ta_ao"),
                    "ta_bb_power": result.get("ta_bb_power"),
                    "ta_macd": result.get("ta_macd"),
                    "ta_rsi": result.get("ta_rsi"),
                })
            except Exception as exc:
                logger.error("%s: sweep failed: %s", symbol, exc)
                await log_error(symbol, "sweep-light", str(exc)[:1000])
            finally:
                lock.release()

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

    def _count_shifts_24h(
        self, symbol: str, now_ms: float, regime_shift: bool
    ) -> int:
        """Count regime shifts for `symbol` in the trailing 24h.

        Maintains a per-symbol list of shift timestamps (epoch ms) and prunes
        anything older than 24h. If this signal itself is a shift, its
        timestamp is recorded before counting. Returns the count (>= 0).
        """
        window_ms = 24 * 60 * 60 * 1000
        stamps = self._shift_counts.get(symbol, [])
        if regime_shift:
            stamps.append(now_ms)
        # Prune entries older than 24h.
        stamps = [t for t in stamps if (now_ms - t) <= window_ms]
        self._shift_counts[symbol] = stamps
        return len(stamps)

    async def _process_symbol(
        self,
        sym: dict[str, Any],
        prices: list[float],
        p_timesfm: float | None = None,
        source: str = "sweep-light",
    ) -> dict[str, Any]:
        """Run one light-sweep cycle for a single symbol.

        Args:
            sym: nt_symbol row dict.
            prices: Hourly close prices.
            p_timesfm: TimesFM probability (cached), if available.
            source: Sweep provenance — ``"sweep-light"`` (default, from
                ``_sweep_group`` / stale-retry) or ``"sweep-heavy"``.
                Stamped onto the ``nt_signal_sim`` capture row.

        Returns a dict with sweep result fields for logging.
        """
        symbol = sym["symbol"]
        np_prices = np.array(prices, dtype=float)

        # ── 1. Regime detection ──
        # Fix #2 (R1, 2026-08-23): pass the PREVIOUS fit's label→mean maps
        # so RegimeHMM matches new components to their previous label
        # instead of re-sorting blind every tick — see regime_engine.py's
        # module docstring. ``symbol_prev_means`` is empty on a symbol's
        # first fit (falls back to rank-sort, unchanged from before).
        symbol_prev_means = self._regime_label_means.get(symbol, {})
        model = await RegimeHMM.from_runtime_config()
        model.fit(
            np_prices,
            prev_vol_means=symbol_prev_means.get("vol"),
            prev_trend_means=symbol_prev_means.get("trend"),
        )
        regime: RegimeSnapshot = model.predict(np_prices)
        self._regime_label_means[symbol] = {
            "vol": model.vol_label_means,
            "trend": model.trend_label_means,
        }

        regime_label = regime.regime_label or "unknown"
        regime_conf = regime.confidence
        trend_probs = dict(zip(RegimeHMM.TREND_LABELS, regime.trend_probs))

        # R4 (2026-08-23, worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md):
        # the trend HMM's own EM fit is a live trust signal for p_regime that
        # was previously computed and discarded. Non-convergence is the
        # TRIGGER, not a separately-derived score — no diagnostic firing
        # means no override, byte-identical to pre-R4 behaviour.
        _regime_weight_override: dict[str, float] = {}
        if not getattr(regime, "trend_converged", True):
            scale = max(0.0, min(1.0, float(
                getattr(settings, "REGIME_NONCONVERGENCE_WEIGHT_SCALE", 0.5)
            )))
            _regime_weight_override = {"p_regime": P_WIN_WEIGHTS_FULL["p_regime"] * scale}
            logger.warning(
                "%s: trend HMM did not converge cleanly this tick — "
                "discounting p_regime weight to %.4f (scale=%.2f)",
                symbol, _regime_weight_override["p_regime"], scale,
            )

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

        # ── 2b. Regime-based Kelly floor (fix #3b, 2026-08-22 audit) ──
        # See _apply_regime_kelly_floor()'s docstring for the full rationale
        # and worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2/§4 fix #3b.
        _require_nonneg_mu = bool(
            getattr(settings, "KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU", True)
        )
        kelly_f, _floor_applied, _floor_withheld = _apply_regime_kelly_floor(
            kelly_f, _mu, regime_label, regime_conf, _require_nonneg_mu,
        )
        if _floor_withheld:
            logger.info(
                "%s: Kelly was 0, regime floor WITHHELD — measured mu=%.8f "
                "is negative (regime=%s, conf=%.3f); a negative historical "
                "edge is not force-sized just because regime confidence is high.",
                symbol, _mu, regime_label, regime_conf,
            )
        elif _floor_applied:
            logger.info(
                "%s: Kelly was 0 — applied regime floor %.4f (conf=%.3f, regime=%s, mu=%.8f)",
                symbol, kelly_f, regime_conf, regime_label, _mu,
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

        # ── 3. HMM markov emit (replaces discrete MarkovChain) ──────────
        # The multivariate Gaussian HMM was refit earlier in this sweep
        # tick (in _sweep_group). Here we emit the 3-state transition matrix
        # + current state — a drop-in replacement for the old
        # _build_markov_chain() return shape.
        #
        # p_up / p_dn are kept for backward-compat with the Supabase
        # nt_sweep_result schema (legacy columns). They are read directly
        # from the transition matrix: P(UP|current_state) and P(DOWN|current_state).
        _markov_T, _markov_current_state = self._hmm_model.emit_markov(symbol)
        p_up, p_dn = 0.5, 0.5  # neutral defaults; replaced if HMM emits

        # Part 1 Change B (2026-08-25): optionally derive the same matrix
        # shape from RegimeHMM's own (Change-A-stabilized) trend model
        # instead of the separate, numerically-unstable legacy HmmModel
        # above. See MARKOV_SOURCE_MODE's docstring near the top of this
        # file. Errors here never affect the live blend — they only skip
        # the shadow comparison / live substitution, falling back to
        # whatever the legacy model already produced above.
        if MARKOV_SOURCE_MODE in ("trend_hmm_shadow", "trend_hmm_live"):
            try:
                _trend_hmm_T, _trend_hmm_current_state = model.trend_transition_matrix_3state(
                    regime.trend_state
                )
                if MARKOV_SOURCE_MODE == "trend_hmm_shadow":
                    logger.info(
                        "%s: markov shadow — legacy current_state=%s row=%s | "
                        "trend_hmm current_state=%s row=%s",
                        symbol, _markov_current_state, _markov_T,
                        _trend_hmm_current_state, _trend_hmm_T,
                    )
                elif MARKOV_SOURCE_MODE == "trend_hmm_live":
                    _markov_T, _markov_current_state = _trend_hmm_T, _trend_hmm_current_state
            except Exception as exc:
                logger.warning(
                    "%s: trend_hmm markov derivation failed (%s: %s), "
                    "using legacy_hmm value", symbol, type(exc).__name__, exc,
                )

        # ── 3a. TA indicator snapshot (migration 036) ──────────────────
        # Pull the latest 5 TA values (ADX, AO, BB Power, MACD, RSI) from
        # the HMM feature buffer for persistence to nt_sweep_result +
        # nt_regime_log. Best-effort: returns {None × 5} if the buffer is
        # empty (e.g. cold start before the first /api/ta poll landed).
        # See regime_platform/services/hmm/ta_extract.py.
        _ta_snapshot = latest_ta(self._hmm_buffer, symbol)

        # ── 4. Direction & trade signal ──
        current_price = float(np_prices[-1])
        direction = "short" if "bear" in regime_label.lower() else "long"

        brick_size, sl_bricks, tp_bricks = await self._load_brick_params(symbol)

        # Extract p_up / p_dn from the HMM's 3x3 transition matrix
        if _markov_T is not None and _markov_current_state:
            try:
                state_row = _markov_T.get(_markov_current_state, {})
                p_up = float(state_row.get("UP", 0.5))
                p_dn = float(state_row.get("DOWN", 0.5))
                # DIAGNOSTIC (2026-08-22, worklog 20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md
                # §2 Finding B / Mechanism 1): AAPL/US500 p_win has been swinging between
                # ~0.47 and ~0.85 tick-to-tick while regime/TimesFM stay frozen. This log
                # line disambiguates the two candidate causes before any fix is built:
                #   - current_state flips (UP<->DOWN classification noise at the 5->3
                #     state-mapping boundary), vs.
                #   - current_state is stable but the matrix ROW itself is noisy
                #     (transition-matrix re-estimation instability tick-to-tick).
                # Remove once the mechanism is confirmed and a fix lands.
                logger.info(
                    "%s: markov diag — current_state=%s row=%s (p_up=%.4f, p_dn=%.4f)",
                    symbol, _markov_current_state, state_row, p_up, p_dn,
                )
            except Exception:
                pass
        else:
            logger.info(
                "%s: markov diag — no transition matrix/current_state "
                "(T is None=%s, current_state=%r)",
                symbol, _markov_T is None, _markov_current_state,
            )

        trade_signal = _compute_trade_signal(
            regime_label, current_price, brick_size, sl_bricks, tp_bricks,
        )

        # ── 5. EV Engine (v6 — 4-source logit pool) ──
        # p_historical (R3, 2026-08-23): Bayesian-shrunk realized win rate
        # for this symbol, reusing the existing calibration-bias
        # infrastructure as a first-class blend INPUT (replaces the old
        # p_pattern source, which was always None server-side — pattern
        # stats lived only in the agent's DuckDB, a single-user-era
        # assumption that no longer holds; removed rather than kept null).
        #
        # Fix #3a (2026-08-22 audit): feed an EMA-smoothed P_markov_hold_N
        # into the blend instead of the raw per-tick T^N estimate — see
        # _smoothed_markov_hold_prob() for why (AAPL/US500 current_state
        # flapping). Computed once per tick and reused by the soft-TimesFM-
        # gate recompute below so both calls see the same smoothed value.
        #
        # Fix #3 (R1, 2026-08-23): also EMA-smooth trend_probs itself
        # before it drives p_regime — supplementary to the RegimeHMM-level
        # variance-floor + label-continuity fixes in regime_engine.py.
        _smoothed_p_markov = self._smoothed_markov_hold_prob(
            symbol, _markov_T, _markov_current_state, direction, MARKOV_HOLD_STEPS,
        )
        _smoothed_trend_probs = self._smoothed_trend_probs(symbol, trend_probs)
        _p_historical = await self._fetch_p_historical_cached(symbol)
        # R4: apply the non-convergence discount (empty dict = no-op, exact
        # pre-R4 behaviour) around the compute_ev() call itself, not a
        # separately-tracked parallel state.
        with self._ev_engine.temp_weights(_regime_weight_override):
            ev_result = self._ev_engine.compute_ev(
                trend_probs=_smoothed_trend_probs,
                direction=direction,
                p_historical=_p_historical,
                transition_matrix=_markov_T,
                current_state=_markov_current_state,
                p_timesfm=p_timesfm,
                brick_size=brick_size,
                sl_bricks=sl_bricks,
                tp_bricks=tp_bricks,
                n_steps=MARKOV_HOLD_STEPS,
                p_markov_override=_smoothed_p_markov,
            )
        # EVFullResult is a dataclass — use attribute access.
        # In v5, markov_multiplier is always 1.0 (Markov is in the blend).
        effective_kelly = kelly_f * ev_result.ev_scale

        # ── 5a. Qualification gates ──
        # Only a qualified signal is sent to Discord / persisted as actionable.
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
        #   "hard": reject if confidence < MIN_TIMESFM_CONFIDENCE
        #   "soft" (default): don't reject, but recompute EV with reduced TimesFM weight
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
            # The EV engine v5 log-odds blend weights TimesFM at 0.25 by
            # default. When confidence is low, we reduce that weight
            # proportionally, then recompute p_win and EV. This way, a
            # low-confidence TimesFM forecast doesn't veto the signal —
            # it just contributes less. The engine itself renormalises
            # remaining weights to sum to 1.0 over available sources.
            if p_timesfm is not None and confidence is not None:
                base_weight = 0.25  # matches P_WIN_WEIGHTS_FULL["p_timesfm"]
                scale = min(1.0, confidence / MIN_TIMESFM_CONFIDENCE) if MIN_TIMESFM_CONFIDENCE > 0 else 1.0
                if scale < 1.0:
                    # Recompute EV with adjusted TimesFM weight using the
                    # EVEngine.temp_weights() context manager. This replaces
                    # the previous direct mutation of
                    # ``self._ev_engine._full_weights`` (audit 2026-07-23):
                    # the public API guarantees weight restoration even if
                    # compute_ev raises, and validates the key/value shape
                    # before the override is applied.
                    # Merge with the R4 regime-non-convergence override
                    # (empty dict if not triggered) so both discounts apply
                    # together when both conditions hold this tick.
                    with self._ev_engine.temp_weights(
                        {**_regime_weight_override, "p_timesfm": base_weight * scale}
                    ):
                        adjusted_ev_result = self._ev_engine.compute_ev(
                            trend_probs=_smoothed_trend_probs,
                            direction=direction,
                            p_historical=_p_historical,
                            transition_matrix=_markov_T,
                            current_state=_markov_current_state,
                            p_timesfm=p_timesfm,
                            brick_size=brick_size,
                            sl_bricks=sl_bricks,
                            tp_bricks=tp_bricks,
                            n_steps=MARKOV_HOLD_STEPS,
                            # Reuse the SAME smoothed values computed above —
                            # not fresh calls — so both compute_ev() calls in
                            # this tick agree on P_markov_hold_N (fix #3a) and
                            # P_regime/P_historical (R1 fix #3 / R3).
                            p_markov_override=_smoothed_p_markov,
                        )
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
        # This ensures all downstream code (rejection log, signal payload,
        # return dict) uses the soft-gate-adjusted values.
        # In hard/off mode, effective_ev_result IS ev_result (no change).
        if TIMESFM_GATE_MODE == "soft" and effective_ev_result is not ev_result:
            ev_result = effective_ev_result
            # Recompute effective_kelly with the adjusted ev_scale.
            # In v5, markov_multiplier is always 1.0 (Markov is in the blend).
            effective_kelly = kelly_f * ev_result.ev_scale

        # ── 5b. EOD calibration enforcement (live consumer) ────────────
        # The EOD self-correction pass (daily 00:30) writes nt_eod_actions
        # recommendations (bayesian_penalty / kelly_reduction /
        # emergency_heavy_sweep) when a symbol is over/underconfident.
        # THIS is the missing consumer side: before the qualification gates
        # we apply the correction live — shrink p_win toward 0.5 (Bayesian)
        # and halve Kelly — so the gates + sizing see the corrected values.
        # Only fetched when the signal is qualifiable (non-neutral) to avoid
        # a Supabase round-trip for every neutral tick. (2026-08-10)
        # Stash the PRE-enforcement p_win for the raw-calibration audit
        # (NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW). The enforcement below may
        # shrink ev_result.p_win; the raw value travels to nt_signal_sim →
        # nt_calibration_records so the bias loop can separate model
        # miscalibration from enforcement compensation (2026-08-13).
        p_win_raw = ev_result.p_win
        calib_adj = None
        if signal_is_qualifiable and settings.EOD_CALIBRATION_ENFORCE:
            from regime_platform.sweep_orchestrator.calibration_enforcement import (
                apply_calibration_adjustment,
            )
            calib_adj = await apply_calibration_adjustment(
                symbol,
                p_win=ev_result.p_win,
                kelly_f=effective_kelly,
            )
            if calib_adj and calib_adj.get("applied"):
                # The agent must see the CORRECTED p_win so its synthesizer
                # doesn't re-inflate overconfidence from a stale raw value.
                ev_result.p_win = calib_adj["p_win"]
                effective_kelly = calib_adj["kelly_f"]
                # ── Fix #1 (2026-08-22 audit): recompute derived fields ──
                # ev/ev_per_dollar/rr/allowed/ev_scale/aggression were
                # previously left stale (computed from the PRE-enforcement
                # p_win) after this mutation — corrupting the aggression
                # (execution-style) decision and all EV telemetry persisted
                # downstream. See worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md.
                _recomputed = self._ev_engine.recompute_ev_fields(
                    ev_result.p_win, ev_result.profit, ev_result.stake,
                )
                ev_result.ev = _recomputed["ev"]
                ev_result.ev_per_dollar = _recomputed["ev_per_dollar"]
                ev_result.rr = _recomputed["rr"]
                ev_result.allowed = _recomputed["allowed"]
                ev_result.aggression = _recomputed["aggression"]
                ev_result.ev_scale = _recomputed["ev_scale"]
                logger.info(
                    "[sweep:orch] %s: calibration ENFORCED (%s, bias=%s) — "
                    "p_win→%.4f, kelly→%.6f, ev→%.4f, aggression→%s",
                    symbol, calib_adj.get("status"), calib_adj.get("bias"),
                    ev_result.p_win, effective_kelly, ev_result.ev, ev_result.aggression,
                )

        # ── 5c. Drift monitor (2026-08-13 plan, D) ────────────────────
        # Fast intraday bias-drift detection BETWEEN the scheduled passes:
        # when a symbol's short-window realized win rate lags its predicted
        # p_win by more than EOD_DRIFT_THRESHOLD, an immediate
        # bayesian_penalty action is written to nt_eod_actions so the NEXT
        # sweep enforces it (no waiting for the 4h/24h cadence). Only checked
        # for qualifiable signals to avoid a Supabase round-trip on every
        # neutral tick; self-guarded (min sample + fresh-action skip) so it
        # never churns actions every 5 minutes.
        if signal_is_qualifiable and settings.EOD_DRIFT_MONITOR_ENABLED:
            from regime_platform.sweep_orchestrator.drift_monitor import check_drift
            drift_row = await check_drift(symbol)
            if drift_row:
                logger.info(
                    "[sweep:orch] %s: drift monitor action %s (bias=%s)",
                    symbol, drift_row.get("action"), drift_row.get("bias"),
                )

        # ── p_win soft-gate (v5): modulator, not hard gate ────────
        # Hard floor: p_win < 0.50 = negative EV = NEVER publish.
        # Soft band: 0.50 ≤ p_win < MIN_P_WIN = publish with kelly shrink.
        #   kelly_shrink = (p_win - 0.50) / (MIN_P_WIN - 0.50)
        #   At p_win=0.50 → shrink=0.0 (zero kelly)
        #   At p_win=MIN_P_WIN → shrink=1.0 (full kelly)
        # Above MIN_P_WIN: full kelly, no shrink.
        #
        # This lets the agent's autonomy_gate decide whether to trade
        # marginal signals (with reduced size) rather than the backend
        # silently rejecting them. The agent gets more data to work with
        # and can apply its own Bayesian alpha + calibration bias on top.
        P_WIN_HARD_FLOOR = 0.50
        if ev_result.p_win < P_WIN_HARD_FLOOR:
            p_win_ok = False
            p_win_kelly_shrink = 0.0
        elif ev_result.p_win < MIN_P_WIN:
            # Soft band — publish but with reduced kelly
            p_win_ok = True  # passes the gate (publish)
            if MIN_P_WIN > P_WIN_HARD_FLOOR:
                p_win_kelly_shrink = (ev_result.p_win - P_WIN_HARD_FLOOR) / (MIN_P_WIN - P_WIN_HARD_FLOOR)
            else:
                p_win_kelly_shrink = 1.0
        else:
            p_win_ok = True
            p_win_kelly_shrink = 1.0

        p_win_kelly_shrink = max(0.0, min(1.0, p_win_kelly_shrink))

        gate_results = {
            "signal_qualifiable": signal_is_qualifiable,
            "regime_conf_ok": regime_conf >= MIN_REGIME_CONF,
            # Gate on the FINAL effective kelly (post calibration-enforcement
            # shrink), not the raw kelly: a raw-kelly gate could qualify a
            # symbol that publishes below MIN_KELLY after the ×0.5 penalty.
            "kelly_min_ok": effective_kelly >= MIN_KELLY,
            "timesfm_available": p_timesfm is not None,
            "timesfm_confidence_ok": timesfm_confidence_ok,
            "ev_allowed": effective_ev_result.allowed,
            # ── p_win soft-gate (v5) ────────────────────────────────────
            # Hard floor at p_win < 0.50 (negative EV, never publish).
            # Soft band 0.50 ≤ p_win < MIN_P_WIN publishes with kelly
            # shrink so the agent can still choose to trade at reduced
            # size. Above MIN_P_WIN: full kelly.
            "p_win_ok": p_win_ok,
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
            "p_win_kelly_shrink": round(p_win_kelly_shrink, 4),
            # ── EOD calibration enforcement stamp (2026-08-10) ─────────
            "calibration_enforced": bool(calib_adj and calib_adj.get("applied")),
            "calibration_status": (calib_adj or {}).get("status"),
            "calibration_bias": round(calib_adj["bias"], 4) if calib_adj and calib_adj.get("bias") is not None else None,
            "ev_reason": effective_ev_result.reason,
            "timesfm_gate_mode": TIMESFM_GATE_MODE,
        }

        # A signal is qualified only if ALL gates pass.
        # NOTE (M1): `timesfm_available` is informational EXCEPT in "hard"
        # mode. When TimesFM is down (p_timesfm is None) in soft/off modes,
        # the blended EV simply runs on the remaining sources — an outage
        # must not zero out every qualified signal. In hard mode the gate
        # still requires TimesFM (binary veto), matching the documented
        # "hard" contract.
        if TIMESFM_GATE_MODE == "hard":
            qualified = all(gate_results.values())
        else:
            qualified = all(
                v for k, v in gate_results.items() if k != "timesfm_available"
            )

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

        # ── 6. Calibration bias ──
        # Only fetch calibration when the signal is qualified — it's an
        # extra Supabase round-trip that's only useful for sizing decisions
        # on signals we'll actually publish.
        venue_name = _derive_venue(sym)
        cal = await fetch_calibration_bias(symbol, venue=venue_name) if qualified else None

        # ── 7. Regime shift detection ──
        prev_regime = self._last_regime.get(symbol)
        prev_trend_probs = self._last_trend_probs.get(symbol)
        regime_shift = (
            prev_regime is not None
            and prev_regime not in ("unknown", "market_closed")
            and regime_label not in ("unknown", "market_closed")
            and prev_regime != regime_label
        )

        # Layer 2 (2026-08-23, worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3):
        # compute the FULL shift signal (direction/magnitude/confidence),
        # not just the label-change boolean above, and flag it for
        # agent review when it's large enough to warrant reconsidering
        # Tier B/C parameters (EV blend weights, gate thresholds) —
        # deliberately excluded from Layer 1's routine daily nudging.
        # This is detection-only: it does not itself change any parameter.
        try:
            shift_info = _evaluate_regime_shift(
                self._ev_engine, prev_regime, prev_trend_probs,
                regime_label, trend_probs,
                getattr(settings, "REGIME_SHIFT_ALERT_THRESHOLD", 0.35),
            )
        except Exception as exc:
            logger.warning("[regime-shift] %s: detection failed: %s", symbol, exc)
            shift_info = None
        if shift_info is not None:
            await self._write_regime_shift_alert(symbol, shift_info)

        self._last_regime[symbol] = regime_label
        self._last_trend_probs[symbol] = trend_probs or {}

        # ── 8. Build the enriched signal payload ──
        now_ms = int(time.time() * 1000)

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

        # ── Apply p_win soft-gate kelly shrink (v5) ────────────────────
        # In the soft band (0.50 ≤ p_win < MIN_P_WIN), kelly is shrunk
        # linearly toward 0. The agent still sees the signal and can
        # choose to trade at the reduced size (or skip via its own
        # autonomy_gate). Above MIN_P_WIN: shrink = 1.0 (no effect).
        if qualified:
            effective_kelly_final = effective_kelly * size_mult * p_win_kelly_shrink
        else:
            effective_kelly_final = 0.0

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
            # ── plan_ids (Fix #17 — per-plan Discord routing) ───────────
            # UUID[] from nt_symbol.plan_ids (migration 032). Source:
            # fetch_active_symbols() in supabase_client.py:60 already
            # SELECTs this column, so no extra DB call is needed here.
            # Downstream Discord notifier uses this array to fan out the
            # alert only to subscribers whose plan_id is in the list.
            # Empty/None = no plan routing (falls back to broadcast).
            "plan_ids": sym.get("plan_ids") or [],
            "regime": regime_label,
            "regime_conf": round(regime_conf, 4),
            "signal": trade_signal["signal"],
            # ── FULL PRECISION PRICES (2026-08-05) ──────────────────────
            # The heartbeat payload feeds the Discord signal embed via the
            # SignalAggregator. Rounding to `decimals` (2dp default for
            # XAUUSD/XAUEUR/XAUAUD/XAGUSD) truncated entry/stop/tp before
            # Discord ever saw them — the card showed $0.70 SL for AUDUSD
            # and $432.16 TP for XAUUSD. Keep 6dp so Discord delivers the
            # SAME full value that is stored in nt_sweep_result.
            "entry_price": round(trade_signal["entry_price"], 6),
            "stop_loss": round(trade_signal["stop_loss"], 6),
            "take_profit": round(trade_signal["take_profit"], 6),
            "aggression": ev_result.aggression,
            "brick_size": brick_size,
            "sl_bricks": sl_bricks,
            "tp_bricks": tp_bricks,
            "kelly_f": round(kelly_f, 6),
            "effective_kelly": round(effective_kelly_final, 6),
            "ev": round(ev_result.ev, 4),
            "ev_per_dollar": round(ev_result.ev_per_dollar, 4),
            "rr": round(ev_result.rr, 4),
            "ev_scale": round(ev_result.ev_scale, 4),
            "p_win": round(ev_result.p_win, 4),
            # ── P_win source breakdown (v5) ─────────────────────────────
            # Agent uses these to re-blend locally with pattern stats from
            # its own DuckDB (which the server doesn't have access to).
            "p_regime": round(ev_result.p_regime, 4),
            "p_markov": round(ev_result.p_markov, 4),
            "p_timesfm": round(ev_result.p_timesfm, 4) if p_timesfm is not None else None,
            # v6 (2026-08-23): p_pattern -> p_historical. Payload key
            # renamed to match — the value is no longer a pattern win-rate
            # placeholder (always null previously), so keeping the old key
            # name would mislabel real data to any downstream consumer.
            "p_historical": round(ev_result.p_historical, 4) if ev_result.p_historical else None,
            "sources_used": list(ev_result.sources_used),
            "weights_used": ev_result.weights_used or {},
            "timesfm_available": p_timesfm is not None,
            # ── HIGH #8 (2026-07-23): single-step Markov T ──────────────
            # The agent's synthesizer uses `markov_persistence` to detect
            # trending regimes via decision-tree adaptive thresholds
            # (markov_persistence > 0.7 ⇒ "let winners run" branch).
            # Previously the agent used heartbeat.p_markov (the T^N
            # multi-step hold probability) as a proxy for single-step
            # persistence. That approximation is conservative but wrong
            # in mean-reverting regimes (T^N can be high while single-
            # step T is low → adaptive thresholds fire incorrectly).
            #
            # We now send the single-step T[current][target] value
            # explicitly so the agent doesn't have to approximate.
            # `p_markov_single_step` = T[current_state][target_state]
            # where target = "UP" for long, "DOWN" for short. We also
            # send `markov_transition_matrix` so the agent can re-derive
            # it (or compute other step counts) if needed.
            "p_markov_single_step": round(
                _markov_T.get(_markov_current_state, {}).get(
                    "UP" if direction == "long" else "DOWN", 0.5
                ), 4
            ) if _markov_T and _markov_current_state else 0.5,
            "markov_transition_matrix": _markov_T,
            "markov_current_state": _markov_current_state,
            # ── p_win soft-gate kelly shrink (v5) ───────────────────────
            # 1.0 = full kelly (p_win >= MIN_P_WIN). <1.0 = linearly shrunk
            # in the soft band (0.50 ≤ p_win < MIN_P_WIN). 0.0 = hard floor
            # (p_win < 0.50, but those signals are not published anyway).
            # Agent can use this to decide whether to trade at reduced size
            # or skip via its autonomy_gate.
            "p_win_kelly_shrink": round(p_win_kelly_shrink, 4),
            # ── Calibration bias (v5 — actually wired now) ─────────────
            # Used by the agent's synthesizer to shrink regime_conf
            # toward 0.5 when the server-side P_win is overconfident
            # (bias > 0.10). Also applied in backtest/sim as a P_win
            # correction (see backtest/engine.py).
            "calibration_bias": round(cal["bias"], 4) if cal else None,
            "calibration_status": cal["status"] if cal else None,
            "regime_shift": "true" if regime_shift else "false",
            "prev_regime": prev_regime or "",
            "size_mult": round(size_mult, 2),
            # ── Regime-shift bookkeeping (agent NobleTraderHeartbeat requires
            # these; the proxy forwards the payload verbatim, so the backend
            # must emit them). shift_at = ms timestamp of the last shift (0 if
            # no shift on this signal); shifts_24h = count of shifts in the
            # trailing 24h, tracked per-symbol on this orchestrator instance.
            "shift_at": int(now_ms) if regime_shift else 0,
            "shifts_24h": self._count_shifts_24h(symbol, now_ms, regime_shift),
            # ── p_imbalance (order-flow imbalance, 0-1) ──────────────────
            # NOTE: the backend deprecated the true L2-derived p_imbalance in
            # favour of p_microstructure (composite of L1/L2). The agent still
            # requires p_imbalance, so we supply the closest available
            # order-flow/persistence proxy: p_markov (the HMM multi-step hold
            # probability), which is already in [0,1] and order-flow-flavoured.
            # When a real L2 imbalance is unavailable this is a best-effort
            # stand-in — not the legacy L2 top-of-book imbalance.
            "p_imbalance": round(float(ev_result.p_markov), 4) if ev_result.p_markov is not None else 0.5,
            "qualified": qualified,
        })

        if qualified:
            # ── D5: Content-hash dedup ────────────────────────────
            # Hash the signal's "meaningful" content (symbol, direction,
            # regime, brick params, price band) to detect duplicates
            # even after cooldown expires. This prevents placeholder
            # spam when the regime is stable but cooldown has lapsed.
            #
            # We hash a small subset of agent_payload — not the whole
            # thing — because timestamps + kelly_f + EV fluctuate even
            # when the actionable signal content is unchanged.
            try:
                payload_obj = (
                    json.loads(agent_payload) if isinstance(agent_payload, str)
                    else agent_payload
                )
                content_fields = {
                    "symbol": payload_obj.get("symbol"),
                    "direction": payload_obj.get("direction") or payload_obj.get("signal"),
                    "regime": payload_obj.get("regime"),
                    "brick_size": payload_obj.get("brick_size"),
                    "sl_bricks": payload_obj.get("sl_bricks"),
                    "tp_bricks": payload_obj.get("tp_bricks"),
                    # Price band (rounded to brick_size granularity so
                    # small intra-band moves don't trigger republish)
                    "price_band": (
                        round(
                            float(payload_obj.get("entry_price", 0))
                            / max(float(payload_obj.get("brick_size", 1)), 1e-9)
                        )
                        if payload_obj.get("entry_price")
                        else None
                    ),
                }
                content_hash = hashlib.sha256(
                    json.dumps(content_fields, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
            except Exception as exc:
                logger.warning(
                    f"[sweep:orch] {symbol}: content-hash failed ({exc}) — publishing without dedup"
                )
                content_hash = ""

            dedup_key = (symbol, trade_signal["signal"])
            if (
                content_hash
                and content_hash == self._last_signal_hash.get(dedup_key)
            ):
                # Same content as last publish — suppress even if cooldown expired
                qualified = False
                gate_results["content_dedup_ok"] = False
                logger.info(
                    f"[sweep:orch] {symbol}: DEDUP SUPPRESSED — "
                    f"signal content unchanged (hash={content_hash[:8]}...)"
                )
            else:
                gate_results["content_dedup_ok"] = True
                if content_hash:
                    self._last_signal_hash[dedup_key] = content_hash

            if qualified:
                # ── Record cooldown timestamp ──────────────────────────
                # Mark this (symbol, direction) as published so subsequent
                # sweeps within the cooldown window are suppressed.
                self._last_qualified_signal[(symbol, trade_signal["signal"])] = time.time()
                logger.info(
                    f"[sweep:orch] {symbol}: QUALIFIED {trade_signal['signal']} signal "
                    f"(cooldown timer started: {SIGNAL_COOLDOWN_MINUTES}min)"
                )

        # ── 10b. Direct Discord delivery ────────────────────────────
        # Placed here — AFTER the dedup block above — so `qualified` is
        # FINAL (cooldown suppression AND content-dedup suppression have
        # both already flipped it to False where applicable).
        if qualified and settings.NOBLE_TRADER_DISCORD_SIGNALS_ENABLED:
            try:
                from regime_platform.sweep_orchestrator.discord_delivery import (
                    maybe_send_discord_signal,
                )

                payload_obj = (
                    json.loads(agent_payload)
                    if isinstance(agent_payload, str)
                    else agent_payload
                )
                asyncio.create_task(
                    maybe_send_discord_signal(
                        payload_obj,
                        source=source,
                    )
                )
            except Exception as discord_exc:
                logger.warning(
                    f"[sweep:orch] {symbol}: direct Discord dispatch failed — "
                    f"{type(discord_exc).__name__}: {discord_exc}"
                )

        # ── 11a. Signal-sim capture (nt_signal_sim) ─────────────────
        # Fire-and-forget persistence of EVERY generated non-neutral
        # signal (qualified or not) for the EOD signal simulator. Placed
        # here — AFTER the dedup block above — so `qualified` is FINAL
        # (cooldown suppression AND content-dedup suppression have both
        # already flipped it to False where applicable).
        # Must NEVER break the sweep: log_signal_sim swallows insert
        # failures, and a missing event loop only skips the capture.
        # NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY (default False): when
        # True, only QUALIFIED signals are persisted (trims ~93% noise;
        # opt-in — changes what the sim measures).
        if trade_signal["signal"] not in ("neutral", "unknown"):
            try:
                sim_payload = _build_signal_sim_payload(
                    symbol=symbol,
                    venue=venue_name,
                    ts=datetime.now(timezone.utc).isoformat(),
                    signal=trade_signal["signal"],
                    entry_price=trade_signal["entry_price"],
                    stop_loss=trade_signal["stop_loss"],
                    take_profit=trade_signal["take_profit"],
                    brick_size=brick_size,
                    sl_bricks=sl_bricks,
                    tp_bricks=tp_bricks,
                    p_win=ev_result.p_win,
                    ev=ev_result.ev,
                    regime=regime_label,
                    regime_conf=regime_conf,
                    source=source,
                    qualified=qualified,
                    kelly_f=kelly_f,
                    effective_kelly=effective_kelly,
                    predicted_p_win_raw=p_win_raw,
                )
                if settings.NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY and not qualified:
                    logger.debug(
                        "[sweep:orch] %s: signal-sim capture skipped (qualified-only mode)",
                        symbol,
                    )
                else:
                    asyncio.create_task(log_signal_sim(sim_payload))
                # Part II paper portfolio: open a kelly-sized paper position
                # for QUALIFIED signals (fire-and-forget; never breaks sweep).
                if qualified:
                    from regime_platform.sweep_orchestrator.paper_book import open_position
                    asyncio.create_task(open_position(sim_payload))
                    # ── 11a1. Realtime broadcast (Talaria dashboard) ──────
                    # Fire-and-forget publish of the QUALIFIED signal to the
                    # realtime:signals broadcast channel. Built from the sim
                    # payload so the dashboard sees exactly the same prices
                    # (6dp full-value) and sizing inputs that were persisted.
                    # Never raises: broadcast_signal swallows failures and
                    # skips silently when there is no running event loop.
                    from regime_platform.sweep_orchestrator.realtime_publisher import (
                        broadcast_signal,
                        build_signal_payload,
                    )
                    broadcast_signal(build_signal_payload(
                        symbol=symbol,
                        direction=trade_signal["signal"],
                        entry_price=sim_payload["entry_price"],
                        stop_loss=sim_payload["stop_loss"],
                        take_profit=sim_payload["take_profit"],
                        kelly_f=sim_payload.get("kelly_f"),
                        effective_kelly=sim_payload.get("effective_kelly"),
                    ), plan_ids=sym.get("plan_ids") or [])
                # ── 11b. Renko brick capture (nt_renko_bricks) ─────────
                # Rebuild the brick series from the sweep's price window and
                # persist one row per brick for the admin plugin's chart.
                # Best-effort: log_renko_bricks swallows failures; a missing
                # event loop or zero bricks just skips. Uses the same
                # brick_size the sweep already loaded for this symbol.
                try:
                    from renko.brick_engine import RenkoBrickEngine
                    engine = RenkoBrickEngine(brick_size=brick_size)
                    bricks = engine.build_from_prices(prices)
                    if bricks:
                        asyncio.create_task(log_renko_bricks(
                            symbol=symbol,
                            bricks=[b.to_dict() for b in bricks],
                            brick_size=brick_size,
                            sweep_ts=time.time(),
                        ))
                except Exception as exc:
                    logger.debug(
                        "%s: renko brick capture skipped: %s", symbol, exc,
                    )
            except RuntimeError:
                # No running event loop (e.g. a test invoking
                # _process_symbol synchronously) — skip the capture.
                pass

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
            venue=_derive_venue(sym),
            # TA indicators (migration 036) — best-effort, may be None.
            ta_adx=_ta_snapshot["ta_adx"],
            ta_ao=_ta_snapshot["ta_ao"],
            ta_bb_power=_ta_snapshot["ta_bb_power"],
            ta_macd=_ta_snapshot["ta_macd"],
            ta_rsi=_ta_snapshot["ta_rsi"],
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
            "regime_shift": regime_shift,
            "prev_regime": prev_regime or "",
            "size_mult": size_mult,
            # ── TA indicators (migration 036) ──────────────────────────
            # Spread the latest TA snapshot (captured in §3a above) into
            # the result dict so the caller (_sweep_group) can persist
            # them to nt_sweep_result via log_sweep_result().
            **_ta_snapshot,
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

                # Single-symbol batch fetch (TDVA — Phase B consolidation)
                try:
                    batch = await fetch_candles_for_symbols([sym], 200)
                    prices = batch.get(symbol, [])
                except Exception as exc:
                    logger.warning(
                        "%s: stale retry — fetch failed: %s", symbol, exc
                    )
                    continue
                if len(prices) < 81:
                    logger.warning(
                        "%s: stale retry — insufficient data (%d)", symbol, len(prices)
                    )
                    continue

                lock = self._sweep_locks.setdefault(symbol, asyncio.Lock())
                if lock.locked():
                    continue
                await lock.acquire()

                try:
                    await self._process_symbol(sym, prices)
                    self._last_ok[symbol] = time.time()
                    logger.info("%s: stale retry successful", symbol)
                except Exception as exc:
                    logger.error("%s: stale retry failed: %s", symbol, exc)
                    await log_error(symbol, "sweep-light-stale", str(exc)[:1000])
                finally:
                    lock.release()

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
        """Load brick_size, sl_bricks, tp_bricks from the YAML config (v5).

        Replaces the Redis ``trading:config:{symbol}`` hash lookup. The
        YAML file (``config/brick_params.yaml``) is the source of truth;
        the heavy sweep (weekly) rewrites it with ATR-scaled optimised
        values. Light sweep reads from the YAML — no Redis round-trip.

        Falls back to (0.5, 3, 6) if the YAML is missing or malformed.
        """
        from regime_platform.config.brick_params import load_brick_params
        return load_brick_params(symbol)

    def _smoothed_markov_hold_prob(
        self,
        symbol: str,
        transition_matrix: Optional[dict[str, dict[str, float]]],
        current_state: Optional[str],
        direction: str,
        n_steps: int,
    ) -> Optional[float]:
        """EMA-smooth the raw T^N P_markov_hold_N estimate for ``symbol``
        (audit fix #3a, 2026-08-22).

        Root cause: in calm/low-volatility markets, the HMM's discretized
        ``current_state`` (UP/DOWN/FLAT) can flip tick-to-tick at the
        classification boundary, and each state's transition-matrix row is
        near-deterministic — so the raw T^N hold probability swings between
        near-0 and near-1 every 5 minutes even though nothing in the market
        actually moved (confirmed live for AAPL: current_state flipped
        UP->FLAT between two consecutive ticks with no price move; see
        worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2 Mechanism 1).

        Smoothing the OUTPUT probability (rather than adding hysteresis to
        the state classification itself) dampens this regardless of whether
        the underlying cause is current_state flipping or transition-matrix
        re-estimation noise — it doesn't require knowing which one it is.

        Returns ``None`` when the raw estimate itself is unavailable
        (no transition matrix / current_state) — mirrors
        ``EVEngine.compute_p_markov_hold_n``'s missing-source contract so
        the blend still renormalises around it rather than treating a
        stale smoothed value as fresh data.
        """
        raw = EVEngine.compute_p_markov_hold_n(
            transition_matrix, current_state, direction, n_steps
        )
        if raw is None:
            return None

        alpha = max(0.0, min(1.0, float(getattr(settings, "MARKOV_EMA_ALPHA", 0.30))))
        previous = self._markov_ema.get(symbol)
        smoothed = raw if previous is None else (alpha * raw + (1.0 - alpha) * previous)
        self._markov_ema[symbol] = smoothed
        return smoothed

    def _smoothed_trend_probs(
        self, symbol: str, trend_probs: dict[str, float]
    ) -> dict[str, float]:
        """EMA-smooth the HMM trend posterior for ``symbol`` before it
        feeds the EV blend (R1 fix #3, 2026-08-23, supplementary to the
        RegimeHMM-level fixes in ``regime_engine.py`` — variance floor +
        label continuity. Mirrors ``_smoothed_markov_hold_prob``).

        Only affects what ``compute_ev()`` sees as ``trend_probs`` (via
        ``compute_p_regime``) — it deliberately does NOT touch
        ``regime_label``/``regime_shift`` bookkeeping (Layer 2), which
        stays driven by the raw, label-continuity-fixed prediction so
        shift *detection* isn't itself dampened by this smoothing.
        """
        alpha = max(0.0, min(1.0, float(getattr(settings, "REGIME_EMA_ALPHA", 0.30))))
        previous = self._regime_trend_ema.get(symbol)
        if not previous:
            smoothed = dict(trend_probs)
        else:
            keys = set(trend_probs) | set(previous)
            blended = {
                k: alpha * trend_probs.get(k, 0.0) + (1.0 - alpha) * previous.get(k, 0.0)
                for k in keys
            }
            total = sum(blended.values())
            smoothed = {k: v / total for k, v in blended.items()} if total > 0 else blended
        self._regime_trend_ema[symbol] = smoothed
        return smoothed

    # _fetch_p_historical_cached lives on HeavySweepMixin (heavy_sweep.py)
    # since both the light-sweep path (this class) and the heavy-sweep
    # path (the mixin) call it — see that module for the implementation.
