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
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

import numpy as np
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from regime_platform.core.settings import settings
from regime_platform.core.regime_engine import RegimeHMM, RegimeLabelDebouncer, RegimeSnapshot
from regime_platform.core.feature_flags import (
    get_knowledge_index_enabled,
    get_self_learning_detectors_enabled,
    get_self_learning_triage_enabled,
    get_macro_forecast_agent_enabled,
    get_markov_source_mode,
    get_min_ev,
    get_min_kelly,
    get_min_p_win,
    get_min_regime_conf,
    get_min_rr,
    get_min_timesfm_confidence,
    get_regime_label_debounce_mode,
    get_regime_label_debounce_ticks,
    get_regime_warm_start_mode,
    get_signal_cooldown_minutes,
    get_sweep_after_hours,
    get_timesfm_gate_mode,
    get_timesfm_k_adaptive_mode,
    get_trend_probs_gate_mode,
)
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
    fetch_latest_qualified_signal,
)
from regime_platform.sweep_orchestrator.heavy_sweep import HeavySweepMixin, compute_atr

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
# Multi-step hold horizon (T^N). Fallback only — Batch D (2026-09-16):
# production call sites prefer n_steps = tp_bricks (bracket geometry) so
# Markov horizon matches the actual TP distance. This env default is used
# when tp_bricks is missing or invalid.
MARKOV_HOLD_STEPS = int(os.getenv("MARKOV_HOLD_STEPS", "10"))


def resolve_markov_n_steps(tp_bricks: float | int | None, fallback: int | None = None) -> int:
    """N for T^N: prefer tp_bricks (bars-to-TP under 1 brick = 1 bar)."""
    fb = fallback if fallback is not None else MARKOV_HOLD_STEPS
    try:
        n = int(tp_bricks) if tp_bricks is not None else 0
    except (TypeError, ValueError):
        n = 0
    return n if n >= 1 else int(fb)

# ─── Qualification gate thresholds (configurable via env vars) ───────────
# These determine which sweep signals qualify and get sent to Discord
# directly (discord_delivery.py). Tune them via env vars to control signal
# volume vs. quality.
#
# IMPORTANT: the EV/RR gate (Gate 6) is the most common blocker. The EV
# engine requires rr >= MIN_RR (default 2.0), but with default brick
# params sl_bricks=3, tp_bricks=5, the actual RR is 5/3 = 1.67 — below
# the 2.0 minimum. Either:
#   (a) lower min_rr to 1.5 in config/feature_flags.yaml, or
#   (b) increase tp_bricks to 6+ in config/brick_params.yaml per symbol, or
#   (c) decrease sl_bricks to 2 in config/brick_params.yaml per symbol
# 2026-09-08 config-centralization fix (same shape as MIN_RR below): these
# 4 were still os.getenv(...)-sourced with an active Studio-Secret/
# .env.local entry and no feature_flags.yaml counterpart — see that file's
# qualification_gates section for the full incident-precedent rationale.
MIN_REGIME_CONF = get_min_regime_conf()
MIN_KELLY = get_min_kelly()
MIN_TIMESFM_CONFIDENCE = get_min_timesfm_confidence()
# MIN_RR and MIN_EV are read by the EVEngine on init — see _ev_engine below.
# We patch the engine's thresholds after construction.
MIN_EV = get_min_ev()
# MIN_RR (2026-08-30 config-centralization fix): moved off a Studio-
# Secret/env-var (was float(os.getenv("MIN_RR", "2.0"))) onto
# config/feature_flags.yaml's min_rr — see that key's own comment for
# why: this value is INTENTIONALLY shared with heavy_sweep.py's grid
# search, and the two had already diverged once via independent env
# vars (2026-08-13 incident). One config value now, read by both
# modules, makes that recurrence structurally impossible rather than
# just less likely.
MIN_RR = get_min_rr()
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
# 2026-09-08 config-centralization fix — see feature_flags.yaml's
# timesfm_gate_mode entry.
TIMESFM_GATE_MODE = get_timesfm_gate_mode()

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
#       blend instead of the legacy model's. Also RETIRES the legacy
#       HmmModel's per-tick refit (heaviest part of its cost) — since
#       nothing reads its emit_markov() output in this mode, the fit()
#       loop is skipped entirely (see the light-sweep fit loop and
#       heavy_sweep.py's mirrored branch). TA/microstructure features
#       still work: they're read directly from the FeatureBuffer
#       (populated by the quote/TA fetchers independently of fit()),
#       not from the fitted model.
# The new derivation reuses RegimeHMM's trend model — already stabilized
# by the 2026-08-24 feature-standardization fix (Change A) — instead of
# fitting a second, separate, numerically-unstable HMM.
#
# Config source changed 2026-08-26 (config-centralization correction,
# worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md): this is no
# longer read from an env var / Studio Secret. An earlier attempt to
# flip the default here (legacy_hmm -> trend_hmm_shadow) had ZERO live
# effect because the Studio had MARKOV_SOURCE_MODE=legacy_hmm set as an
# explicit Secret — invisible outside the platform UI, silently
# overriding any code-level default. Now read from the git-tracked
# config/feature_flags.yaml (regime_platform/core/feature_flags.py) —
# a real secret belongs in Studio Secrets, but a deliberately-set mode
# flag belongs in a file that's visible in a diff. See that module for
# the hot-reload behavior (no restart needed to pick up an edit).
# Call get_markov_source_mode() fresh in each function that needs it
# (cheap — mtime-cached) rather than a module-level constant, so an edit
# to the YAML takes effect on the next tick, not the next deploy.

# When True, sweep ALL symbols regardless of market hours. Useful for:
#   - Regime tracking outside market hours (you still want to know the
#     regime even if you can't trade)
#   - Testing/debugging on weekends or after close
# When False (default), stocks/commodities are skipped when the market
# is closed (matching the TS edge function behavior). Crypto and forex
# are always swept (24/7 and Sun-Fri respectively).
# 2026-09-08 config-centralization fix — see feature_flags.yaml's
# sweep_after_hours entry (NOTE its own comment: the real configured
# value was `true`, the opposite of this code comment's stated default).
# NOT a module-level constant (2026-09-08 follow-up fix): the first cut of
# this migration captured get_sweep_after_hours() once at import time, so a
# YAML edit needed a process restart to take effect — inconsistent with
# get_markov_source_mode()'s documented "call fresh each time, cheap and
# mtime-cached" hot-reload pattern above. _sweep_group() now calls
# get_sweep_after_hours() directly instead of reading this name.

# ── Signal cooldown (prevents signal spam) ────────────────────────────────
# After a qualified signal is published for a (symbol, direction) pair,
# suppress subsequent qualified signals for the same pair until the
# cooldown expires. This prevents the same BUY/SELL from being published
# every 5 minutes when the regime is stable.
#
# Set to 0 to disable cooldown (every qualified sweep is published).
# Recommended: 60 (1 hour) — gives ~3-8 signals per day per symbol.
# 2026-09-08 config-centralization fix — see feature_flags.yaml's
# signal_cooldown_minutes entry.
SIGNAL_COOLDOWN_MINUTES = get_signal_cooldown_minutes()

# ── Minimum p_win threshold ───────────────────────────────────────────────
# Require the blended p_win (from the EV engine's log-odds pooling) to be
# above this value. This is a DYNAMIC quality filter — unlike MIN_RR which
# is a static property of brick params, p_win changes sweep-to-sweep based
# on how regime + ext_signal + TimesFM agree.
#
# Set to 0.0 to disable (accept any positive-EV signal).
# Recommended: 0.55 — filters out signals where the blend is only marginally
# directional (p_win just barely above 0.5).
# 2026-09-08 config-centralization fix — see feature_flags.yaml's
# qualification_gates.min_p_win entry.
MIN_P_WIN = get_min_p_win()

# ── TimesFM forecast → probability mapping ─────────────────────────────────
# The TimesFM point forecast (a price) is converted to p(up-move) via:
#   p_timesfm = sigmoid(k * relative_price_change)
# where k controls how aggressively price deltas map to confidence.
# k=20 → 1% move ≈ p=0.73, 5% move ≈ p=0.99 (EXCESSIVELY aggressive)
# k=10 → 1% move ≈ p=0.62, 5% move ≈ p=0.92 (more reasonable)
# Batch D/F: source of truth is config/feature_flags.yaml (timesfm block).
# Env is NOT read — feature_flags is the sole config surface (user 2026-09-16).


def _timesfm_mapping_defaults() -> tuple[float, float, float]:
    try:
        from regime_platform.core.feature_flags import (
            get_timesfm_clamp_max,
            get_timesfm_clamp_min,
            get_timesfm_sigmoid_k,
        )
        return get_timesfm_sigmoid_k(), get_timesfm_clamp_min(), get_timesfm_clamp_max()
    except Exception:
        return 10.0, 0.05, 0.95


TIMESFM_SIGMOID_K, TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX = _timesfm_mapping_defaults()

# ── TimesFM fetcher with 15-minute cache ────────────────────────────────
# Mirrors supabase/functions/_shared/utils.ts:timesfmPost()
# Using a simple in-memory cache (not Redis for this hot path)
_TIMESFM_CACHE: dict[str, tuple[float, float]] = {}  # symbol -> (p, timestamp)
_TIMESFM_CACHE_TTL_S = 900  # 15 minutes

async def _fetch_timesfm_cached(
    symbol: str,
    prices: list[float],
    k_override: float | None = None,
    shadow_k: float | None = None,
) -> tuple[float, float] | None:
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

    ``k_override``/``shadow_k`` (2026-09-11, worklog
    20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 12):
    ``k_override`` replaces ``TIMESFM_SIGMOID_K`` for this call's sigmoid
    mapping when given (``timesfm_k_adaptive_mode=live``) — the returned
    ``p_timesfm`` (and what gets cached) is computed with this k.
    ``shadow_k`` (``timesfm_k_adaptive_mode=shadow``) computes an
    ADDITIONAL probability with a different k purely for logging/
    comparison, reusing the same already-fetched forecast (no second
    network call) — never affects the returned/cached value. Both only
    apply on a fresh fetch (cache miss); a cache hit has no ``rel_change``
    to recompute from, so shadow comparison is skipped on hits — a
    known, accepted limitation given the cache TTL already matches the
    sweep cadence closely.

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
        from regime_platform.sweep_orchestrator.timesfm_k_adaptive import (
            map_forecast_to_probability,
        )
        _k = TIMESFM_SIGMOID_K if k_override is None else k_override
        # Pre-clamp sigmoid for the probe log (2026-09-17): live data showed
        # p_timesfm pinned near 0.5 — need to see whether rel_change is ~0
        # (flat forecast) or the clamp is crushing a real signal.
        import math
        try:
            _raw_sig = 1.0 / (1.0 + math.exp(-_k * rel_change))
        except OverflowError:
            _raw_sig = 0.0 if rel_change < 0 else 1.0
        p_timesfm = map_forecast_to_probability(
            rel_change, _k, TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX,
        )
        confidence = abs(p_timesfm - 0.5)

        # ── z-map (2026-09-17 worklog 20260917_TIMESFM_ZMAP_FIX.md) ────
        # Global k-sigmoid cannot resolve sub-percent TimesFM forecasts
        # (probe: +0.12% → p=0.503). Standardize by per-symbol rolling σ.
        from regime_platform.core.feature_flags import get_timesfm_map_mode
        from regime_platform.sweep_orchestrator import timesfm_zmap

        _map_mode = get_timesfm_map_mode()
        timesfm_zmap.record_rel_change(symbol, rel_change)
        p_zmap = timesfm_zmap.zmap_probability(symbol, rel_change)
        p_sigmoid = p_timesfm
        if _map_mode == "zmap_live" and p_zmap is not None:
            p_timesfm = max(
                TIMESFM_CLAMP_MIN,
                min(TIMESFM_CLAMP_MAX, p_zmap),
            )
            confidence = abs(p_timesfm - 0.5)
        # TimesFM probe: always log the full map chain so a stuck p≈0.5
        # is diagnosable from Studio logs alone.
        _flat = abs(rel_change) < 1e-6
        logger.info(
            "[timesfm:probe] %s forecast=%.6f last=%.6f rel_change=%.8f "
            "k=%.2f sigmoid_raw=%.4f p_sigmoid=%.4f p_zmap=%s "
            "p_used=%.4f mode=%s conf=%.4f flat=%s clamp=[%.3f,%.3f]",
            symbol, forecast_value, last_price, rel_change, _k,
            _raw_sig, p_sigmoid,
            f"{p_zmap:.4f}" if p_zmap is not None else "n/a",
            p_timesfm, _map_mode, confidence, _flat,
            TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX,
        )
        if _flat:
            logger.warning(
                "[timesfm:probe] %s: forecast equals last price "
                "(rel_change=0) — p_timesfm will stay ~0.5; TimesFM is "
                "not contributing directional information",
                symbol,
            )

        if shadow_k is not None:
            p_timesfm_shadow = map_forecast_to_probability(
                rel_change, shadow_k, TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX,
            )
            logger.info(
                "[sweep:orch] %s: TimesFM k-adaptive SHADOW — base_k=%.2f "
                "p=%.4f | adaptive_k=%.2f p_adaptive=%.4f (rel_change=%.4f)",
                symbol, TIMESFM_SIGMOID_K, p_timesfm, shadow_k,
                p_timesfm_shadow, rel_change,
            )

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


# Staleness thresholds for the startup catch-up check (2026-09-01,
# worklog/20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md) — see
# SweepOrchestrator._catch_up_stale_jobs()'s docstring for why this
# exists. Deliberately shorter than the full daily/weekly cycle so a
# legitimately-recent run isn't needlessly re-triggered by a routine
# restart.
MACRO_REGIME_STALE_AFTER = timedelta(hours=20)
BRACKET_REACHABILITY_STALE_AFTER = timedelta(hours=6.5 * 24)
# eod-lookback (2026-09-05, worklog 20260905_EOD_RESOLVER_SCHEDULER_MISS.md)
# — the only job that calls resolve_pending_signals. Found live: a crash
# in the ~6-minute gap between its 00:32 UTC daily fire and the prior
# tick swallowed that day's run entirely, and the fresh instance that
# came up afterward pushed the next fire to the FOLLOWING day — 45h of
# zero resolutions before this was caught. Same "just under one cycle"
# threshold as MACRO_REGIME_STALE_AFTER.
EOD_LOOKBACK_STALE_AFTER = timedelta(hours=20)

# eod-correction-intraday (RM-42, 2026-09-12, worklog
# 20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md
# §1.4 Option B): the 5 fixed :30 slots (04:30/08:30/12:30/16:30/20:30
# UTC) were found landing 30-50 minutes before a Studio restart on the
# day this was first scoped — but the restart cadence itself DRIFTS
# (confirmed live: re-phases after every deploy, so a static offset
# table would itself go stale). A catch-up guard is robust to that
# drift by construction, matching the 3 checks above — it doesn't care
# WHEN restarts happen, only whether a correction pass has actually run
# recently. Tighter than the 4h interval (not 20h like the others,
# which run once a day/week) so a genuinely-skipped slot is caught well
# before the next one is due, without re-triggering a routine restart's
# worth of "not actually stale yet."
EOD_CORRECTION_INTRADAY_STALE_AFTER = timedelta(hours=3.5)


def _is_stale(
    last_computed_at: datetime | None,
    threshold: timedelta,
    now: datetime | None = None,
) -> bool:
    """True if ``last_computed_at`` is missing or older than ``threshold``
    relative to ``now`` (defaults to the current UTC time). Pure function,
    no I/O — the actual data fetch lives in
    ``supabase_client.fetch_max_computed_at()``.
    """
    if last_computed_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_computed_at) > threshold


# Light-sweep startup catch-up (2026-09-25, worklog/kairos/
# 20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md §6). A fresh
# APScheduler computes the next */15 fire strictly forward from "now", so a
# restart spanning a slot silently drops it (found live 2026-09-25: the
# deploy restart at 04:18 lost the 04:15 slot for all 14 symbols). Only the
# MOST RECENT slot is caught up — a catch-up tick runs on current bars, so it
# stands in for the missed tick (keeping the debouncer's tick count honest)
# but can't reconstruct older ones. Skipped when the next scheduled fire is
# too close, so a catch-up never overlaps or doubles a real tick.
LIGHT_CATCHUP_LOOKBACK = timedelta(minutes=60)
LIGHT_CATCHUP_MIN_LEAD = timedelta(minutes=5)


def _missed_light_slot(
    trigger: Any,
    now: datetime,
    *,
    lookback: timedelta = LIGHT_CATCHUP_LOOKBACK,
    min_lead: timedelta = LIGHT_CATCHUP_MIN_LEAD,
) -> datetime | None:
    """Most recent scheduled fire time ``<= now`` for ``trigger`` (an
    APScheduler ``CronTrigger``), or ``None`` when there is none inside
    ``lookback`` or the next fire is less than ``min_lead`` away. Pure: no
    I/O; whether that slot was actually swept is the caller's question."""
    t = trigger.get_next_fire_time(None, now - lookback)
    last: datetime | None = None
    nxt: datetime | None = None
    for _ in range(10_000):  # bounded; */1 over 60 min is 61 steps
        if t is None:
            break
        if t > now:
            nxt = t
            break
        last = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    if last is None:
        return None
    if nxt is not None and nxt - now < min_lead:
        return None
    return last


def _unswept_symbols(
    sym_list: list[dict[str, Any]], swept_since_slot: set[str],
) -> list[dict[str, Any]]:
    """Symbols of a cron group with no ``sweep-light`` row since the slot
    (also covers a slot the old process died part-way through)."""
    return [s for s in sym_list if s.get("symbol") not in swept_since_slot]


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
    atr: float | None = None,
    macro_label: str | None = None,
    macro_strength: float | None = None,
    trend_label: str | None = None,
    trend_strength: float | None = None,
    context_aligned_ok: bool | None = None,
    legacy_markov_state: str | None = None,
    trend_hmm_markov_state: str | None = None,
    raw_p_regime: float | None = None,
    gated_p_regime: float | None = None,
    p_win_dir: float | None = None,
    p_win_bracket: float | None = None,
    p_resolve: float | None = None,
    p_win_resolve: float | None = None,
    bracket_bucket: str | None = None,
    bracket_vol_tier: str | None = None,
    bracket_gate_shadow: bool | None = None,
    qualified_geometry_shadow: bool | None = None,
    tp_distance_atr: float | None = None,
    sl_distance_atr: float | None = None,
    long_lag_hat: float | None = None,
    geometry_risk_hat: float | None = None,
    long_lag_risk: bool | None = None,
    kairos: dict[str, float | None] | None = None,
    gate_outcome: str | None = None,
    suppress_reason: str | None = None,
    failed_gates: list[str] | None = None,
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

    ``atr`` (2026-08-25, migration 120) is the symbol's ATR(14) at capture
    time (absolute price units, see ``heavy_sweep.compute_atr``) — feeds
    the bracket-reachability diagnostic (bracket_reachability.py).
    Nullable so legacy callers are unaffected.

    ``macro_label``/``macro_strength`` (2026-08-28, migration 124) are
    the macro filter's read for this symbol at capture time (see
    ``macro_filter.py``) — feed the win/loss+PnL evidence diagnostic
    (``macro_edge.py``). Nullable: absent when ``macro_regime_mode`` is
    ``off`` or no ``nt_macro_regime`` row exists yet for the symbol.

    ``trend_label``/``trend_strength`` (2026-09-10, worklog
    20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7) are
    the trend filter's read for this symbol at capture time (see
    ``trend_filter.py``) — a separate, medium-term (10-50 day SMA
    crossover + RSI) concern from macro_label/macro_strength above.
    Nullable: absent when ``trend_regime_mode`` is ``off`` or no
    ``nt_trend_regime`` row exists yet for the symbol.

    ``context_aligned_ok`` (2026-09-11, worklog
    20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 10)
    is the Tier 1 context-alignment gate's read at capture time (see
    ``context_alignment.py``) — computed from the SAME macro_label/
    trend_label above, never from p_win/ev_result. Nullable: absent
    when ``context_alignment_gate_mode`` is ``off``.

    ``legacy_markov_state``/``trend_hmm_markov_state`` and
    ``raw_p_regime``/``gated_p_regime`` (2026-08-31, migration 131,
    worklog 20260831_SHADOW_TO_LIVE_ROADMAP.md) are Tier 1/2's own
    shadow-comparison values at capture time — feed the outcome-linked
    accuracy diagnostic (``hmm_shadow_accuracy.py``), closing the gap
    where the shadow tables could show two sources disagree but not
    which one was actually right. Nullable: only populated during the
    real shadow-mode comparison (``markov_source_mode=trend_hmm_shadow``
    / ``trend_probs_gate_mode=shadow``), None otherwise.
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
        "atr": round(float(atr), 6) if atr is not None else None,
        "macro_label": macro_label,
        "macro_strength": round(float(macro_strength), 4) if macro_strength is not None else None,
        "trend_label": trend_label,
        "trend_strength": round(float(trend_strength), 4) if trend_strength is not None else None,
        "context_aligned_ok": context_aligned_ok,
        "legacy_markov_state": legacy_markov_state,
        "trend_hmm_markov_state": trend_hmm_markov_state,
        "raw_p_regime": round(float(raw_p_regime), 4) if raw_p_regime is not None else None,
        "gated_p_regime": round(float(gated_p_regime), 4) if gated_p_regime is not None else None,
    }
    # Batch B (2026-09-16): always persist pre-enforcement p_win when we have
    # it. Capture flag remains for emergency off; when on and raw is missing,
    # still omit the column but log an error so measurement gaps are visible.
    if predicted_p_win_raw is not None:
        payload["predicted_p_win_raw"] = round(float(predicted_p_win_raw), 4)
    elif settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW:
        logger.error(
            "signal-sim capture: predicted_p_win_raw missing while "
            "NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW=True — measurement will "
            "treat this row as raw-absent"
        )
    # Batch C: always stamp directional vs bracket probabilities when known
    if p_win_dir is not None:
        payload["p_win_dir"] = round(float(p_win_dir), 4)
    if p_win_bracket is not None:
        payload["p_win_bracket"] = round(float(p_win_bracket), 4)
    if p_resolve is not None:
        payload["p_resolve"] = round(float(p_resolve), 4)
    if p_win_resolve is not None:
        payload["p_win_resolve"] = round(float(p_win_resolve), 4)
    if bracket_bucket is not None:
        payload["bracket_bucket"] = bracket_bucket
    if bracket_vol_tier is not None:
        payload["bracket_vol_tier"] = bracket_vol_tier
    if bracket_gate_shadow is not None:
        payload["bracket_gate_shadow"] = bool(bracket_gate_shadow)
    if qualified_geometry_shadow is not None:
        payload["qualified_geometry_shadow"] = bool(qualified_geometry_shadow)
    if tp_distance_atr is not None:
        payload["tp_distance_atr"] = round(float(tp_distance_atr), 4)
    if sl_distance_atr is not None:
        payload["sl_distance_atr"] = round(float(sl_distance_atr), 4)
    if long_lag_hat is not None:
        payload["long_lag_hat"] = round(float(long_lag_hat), 4)
    if geometry_risk_hat is not None:
        payload["geometry_risk_hat"] = round(float(geometry_risk_hat), 4)
    if long_lag_risk is not None:
        payload["long_lag_risk"] = bool(long_lag_risk)
    # Kairos v3 P0.5 (2026-09-24, migration 167): geometry-free
    # market-movement stamps; only non-None values are written.
    if kairos:
        from regime_platform.sweep_orchestrator.kairos import KAIROS_CAPTURE_COLUMNS
        for k in KAIROS_CAPTURE_COLUMNS:
            v = kairos.get(k)
            if v is not None:
                payload[k] = v
    # Kairos G2 (2026-09-24, migration 168): qualified | suppressed |
    # gate_fail, so all-signal analyses can separate cooldown/dedup
    # suppression from genuine gate rejections.
    if gate_outcome is not None:
        payload["gate_outcome"] = gate_outcome
    if suppress_reason is not None:
        payload["suppress_reason"] = suppress_reason
    if failed_gates:
        payload["failed_gates"] = ",".join(failed_gates)
    return payload


class CooldownRestartProbe(NamedTuple):
    """Result of ``_probe_cooldown_restart`` (cooldown-restart fix,
    2026-09-08, worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
    — user directive "after a sl/tp hits > the cooldown should restart").

    ``status``:
      * ``"open"`` — a prior qualified signal exists for this
        ``(symbol, direction)`` and is still open (DB shows no outcome yet,
        neither TP nor SL has been crossed by the current price, and
        ``EXPIRY_BARS`` hours have not yet elapsed since entry). The
        caller must block the new signal unconditionally, regardless of
        elapsed wall-clock time.
      * ``"resolved"`` — the prior signal has resolved, one of three ways:
        the batch EOD resolver already recorded an outcome, the current
        price has already crossed TP/SL (detected live, ahead of the
        once-daily batch), or ``EXPIRY_BARS`` hours have elapsed with
        neither level touched (time-expired). ``resolved_at`` is the
        epoch-seconds moment the cooldown clock should restart from.
      * ``"unknown"`` — no prior qualified signal was found, or the
        Supabase lookup failed. The caller falls back to the existing
        wall-clock-only cooldown behavior, unchanged.
    """
    status: str
    resolved_at: float | None = None


async def _probe_cooldown_restart(
    symbol: str, direction: str, current_price: float | None,
) -> CooldownRestartProbe:
    """Is the last qualified ``(symbol, direction)`` signal still open?

    The official record of resolution (``nt_signal_sim.outcome``) is only
    populated once a day by ``eod_resolver.py``'s batch pass — waiting on
    it here would leave a just-closed position looking "open" for up to
    24h. Instead: check the DB-recorded outcome first, then fall back to
    a REAL-TIME check using the exact same TP/SL-crossing comparison
    ``eod_resolver.resolve_signal_outcome`` uses for the batch pass
    (imported, not re-implemented, so the two never drift), applied to
    just the current price rather than a full historical bar walk. This
    is intentionally a lighter check than the batch resolver: no mfe/mae,
    no exact hold_bars — just "would this already be closed."

    Best-effort: any failure here must never block a signal from
    qualifying — callers should catch and fall back to the existing
    wall-clock-only cooldown on exception, matching this module's
    pervasive "never break the sweep" convention.
    """
    row = await fetch_latest_qualified_signal(symbol, direction)
    if not row:
        return CooldownRestartProbe("unknown")

    # Lazy import (matches this module's existing eod_resolver-import
    # convention, e.g. `_run_eod_lookback` above) — avoids a module-level
    # dependency on the EOD resolver from the hot per-tick sweep path.
    from regime_platform.sweep_orchestrator.eod_resolver import (
        EXPIRY_BARS,
        resolve_signal_outcome,
    )
    from regime_platform.sweep_orchestrator.paper_book import _signal_epoch_ts

    # 1. Already resolved by the daily batch resolver (tp_hit/sl_hit/expired
    #    all count — a timed-out signal is not "still open" for cooldown
    #    purposes either). Prefer exit_ts (the resolver's own entry+hold_bars
    #    estimate of when it actually closed) over resolved_at (merely when
    #    last night's batch happened to run) as the restart moment.
    outcome = row.get("outcome")
    if outcome:
        resolved_ts = row.get("exit_ts") or row.get("resolved_at") or row.get("ts")
        return CooldownRestartProbe("resolved", _signal_epoch_ts({"ts": resolved_ts}))

    # 2. Not yet resolved in the DB. Figure out how old the entry is.
    entry_epoch = _signal_epoch_ts(row)
    age_hours = (time.time() - entry_epoch) / 3600.0

    # 3. Time-based expiry — EXPIRY_BARS post-entry 1h bars with neither
    #    level touched. The resolution moment is deterministic here
    #    (entry + EXPIRY_BARS hours), unlike the live-crossing case below.
    if age_hours >= EXPIRY_BARS:
        return CooldownRestartProbe("resolved", entry_epoch + EXPIRY_BARS * 3600.0)

    # 4. Live TP/SL-crossing check against the current price. Reuses
    #    resolve_signal_outcome with a single-element closes window
    #    (expiry_bars=1) — this is the SAME same-bar TP-priority
    #    comparison eod_resolver.py runs, just over one bar instead of a
    #    full historical walk, so "closed" can never mean something
    #    subtly different here than it does in the batch pass. An
    #    "expired" result from this 1-bar call means only "not crossed at
    #    this instant" — real expiry is decided by step 3 above, not here.
    entry_price = row.get("entry_price")
    stop_loss = row.get("stop_loss")
    take_profit = row.get("take_profit")
    if current_price is not None and None not in (entry_price, stop_loss, take_profit):
        try:
            live = resolve_signal_outcome(
                float(entry_price), float(stop_loss), float(take_profit),
                [float(current_price)],
                direction=row.get("signal") or direction,
                expiry_bars=1,
            )
        except Exception as exc:
            logger.warning(
                "[sweep:orch] %s: cooldown live TP/SL check failed (%s)",
                symbol, exc,
            )
            live = None
        if live is not None and live["outcome"] in ("tp_hit", "sl_hit"):
            # The exact intra-window crossing moment isn't knowable from a
            # single current-price sample — "now" is the documented
            # fallback (spec: crossing time if determinable, else "now").
            return CooldownRestartProbe("resolved", time.time())

    # 5. Neither DB-resolved, expired-by-time, nor live-crossed: still open.
    return CooldownRestartProbe("open")


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

        # Serializes every writing resolver pass (nightly eod-lookback,
        # its startup catch-up, and the hourly calibration resolve,
        # 2026-09-25) — two concurrent resolve_pending_signals() calls
        # would read the same outcome-IS-NULL rows and double-write
        # nt_calibration_records (the insert has no signal_id uniqueness).
        # Accessed via _get_resolver_lock() so instances built without
        # __init__ (tests use object.__new__) still get one.
        self._resolver_lock: asyncio.Lock | None = asyncio.Lock()

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
        # Per-symbol full fitted params (means/covars/transmat/startprob,
        # original units) from the LAST fit — warm-starts the NEXT fit()
        # call's EM initialization when regime_warm_start_mode=live
        # (2026-09-01, worklog 20260901_REGIME_LABEL_STABILITY_SCOPE.md).
        # Separate from _regime_label_means above: that dict solves "what
        # do we call this component," this one solves "where should EM
        # start looking" — independent mechanisms, both keyed by symbol.
        # Same in-memory-only lifecycle (lost on restart) as
        # _regime_label_means already is; not a regression.
        self._regime_warm_start_params: dict[str, dict[str, dict[str, np.ndarray]]] = {}
        # Fix 2 (2026-09-01, same worklog) — one RegimeLabelDebouncer per
        # symbol, persisted across ticks so its confirmed/candidate state
        # carries forward. Independent of both dicts above: this smooths
        # the OUTPUT label regardless of what produced it, rather than
        # trying to stabilize the fit itself. Same in-memory-only
        # lifecycle (lost on restart, restarts naturally just cold-start
        # each symbol's debouncer again) as everything else in this block.
        self._regime_debouncers: dict[str, RegimeLabelDebouncer] = {}
        # Kairos v3 P0.5 (2026-09-24): per-symbol regime-cell run tracker
        # (start ts + ATR at start → atr_ratio_run) and the latest sweep's
        # 1h OHLCV per symbol (price_fetcher ohlcv_out). In-memory, same
        # lifecycle as the debouncers above.
        from regime_platform.sweep_orchestrator.kairos import KairosRunTracker
        self._kairos_runs = KairosRunTracker()
        self._kairos_ohlcv: dict[str, list[dict[str, Any]]] = {}
        # symbol → (run_age_hours, atr_at_run_start) from this tick's update
        self._kairos_run_state: dict[str, tuple[float | None, float | None]] = {}
        self._regime_trend_ema: dict[str, dict[str, float]] = {}
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
            f"TIMESFM_CLAMP=[{TIMESFM_CLAMP_MIN}, {TIMESFM_CLAMP_MAX}], "
            f"MARKOV_SOURCE_MODE={get_markov_source_mode()} (config/feature_flags.yaml)"
        )

    # ── Public lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the scheduler and background checks."""
        # Restart-safe regime state (2026-09-25) — before any sweep can run.
        self._restore_regime_state()
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
        # then run the calibration self-correction audit. Daily 01:17 UTC
        # (was 00:32 UTC until 2026-09-10) — moved to land just after the
        # 01:12 UTC Studio restart (see harmonize note below) instead of 40
        # minutes before it, so a restart no longer routinely kills this job
        # mid-run. Still offset 2 minutes off the light sweep's grid
        # (`*/15 * * * *`, the :15 mark) for the same reason as the original
        # 00:32 choice (2026-08-24): landing exactly on the shared :00/:15/
        # :30/:45 marks caused concurrent TradingView batch calls and 429s.
        #
        # Studio restart harmonization (2026-09-10,
        # worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md):
        # Lightning AI free-plan Studio sleeps after ~4h and is restarted on
        # a fixed schedule (PDT): 06:00, 10:04, 14:08, 18:12, 22:16, 02:24
        # (= 13:00, 17:04, 21:08, 01:12, 05:16, 09:24 UTC). A restart kills
        # any in-flight asyncio work with no resume — a job scheduled near
        # the END of a ~4h window gets almost no runway before being killed;
        # one scheduled just AFTER a restart gets the full ~4h window. This
        # was root-caused as the real reason XAUAUD's heavy sweep went 4+
        # days stale (heavy-sweep was firing 20:00 UTC, only 68 minutes
        # before the 21:08 UTC restart) — see the same worklog section.
        self._scheduler.add_job(
            self._run_eod_lookback,
            trigger=CronTrigger.from_crontab("17 1 * * *"),
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

        # Self-learning knowledge index (2026-09-10, Phase 1 of worklog/
        # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §3):
        # nightly, 01:45 UTC — after the auto-tuner (01:30) so that day's
        # registry writes are captured too, and on the SAME in-process
        # scheduler as everything else. This replaces the original Layer 2
        # item 3 design (a Hermes cron job triggering an external agent
        # session on detected anomalies, PARAMETER_TUNING_PLAYBOOK.md
        # §2.5.3) — that design sat unbuilt for 3+ weeks because it
        # depended on configuration outside this repo; a job on the
        # scheduler this repo already runs has no such dependency. Purely
        # additive/local: indexes the codebase + worklogs + nt_roadmap
        # notes into a local SQLite file, never reads or writes anything
        # that influences a live signal or parameter.
        if get_knowledge_index_enabled():
            self._scheduler.add_job(
                self._run_knowledge_reindex,
                trigger=CronTrigger.from_crontab("45 1 * * *"),
                id="knowledge-index-nightly",
                name="Self-learning knowledge index (nightly)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Self-learning Phase 2 detectors (2026-09-12, worklog
        # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS10/
        # SS11, self_learning_phase_2_detectors, RM-37): nightly, 02:00
        # UTC — after knowledge-index (01:45), same restart-safe
        # 01:12-05:16 UTC Studio window as auto-tuner/knowledge-index (see
        # xauaud_heavy_sweep_stale_4d_force_refresh_not_firing for the
        # restart-schedule background). Runs regime_price_mismatch/
        # dead_mechanism/redundant_computation against real data and
        # writes findings to nt_self_learning_findings — purely
        # diagnostic, nothing downstream reads that table to make a live
        # decision.
        if get_self_learning_detectors_enabled():
            self._scheduler.add_job(
                self._run_self_learning_detectors,
                trigger=CronTrigger.from_crontab("0 2 * * *"),
                id="self-learning-detectors-nightly",
                name="Self-learning Phase 2 detectors (nightly)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Self-learning Phase 3 MVP LLM-triage (2026-09-11, worklog
        # 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md,
        # self_learning_phase_3_4_forecasting_rescope, RM-50): nightly,
        # 02:30 UTC — 30 min after the detectors job above so today's
        # findings are already written. Reads untriaged
        # nt_self_learning_findings rows, asks an LLM for a narrative +
        # an optional bounded Tier D parameter proposal, writes both to
        # nt_self_learning_reports — purely diagnostic, no write access
        # to any live parameter. Requires OPENROUTER_API_KEY or
        # NVIDIA_NIM_API_KEY (NOT Anthropic — corrected same day); logs
        # a warning and no-ops per finding without either.
        if get_self_learning_triage_enabled():
            self._scheduler.add_job(
                self._run_self_learning_triage,
                trigger=CronTrigger.from_crontab("30 2 * * *"),
                id="self-learning-triage-nightly",
                name="Self-learning Phase 3 MVP triage (nightly)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Long-term (12-month) macro-forecast agent (2026-09-11, worklog
        # 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md §4,
        # RM-50 follow-up): nightly, 02:45 UTC — same self-learning
        # window as the two jobs above. Reads docs/LLM/
        # commodity_12m_forecast_model_2027.md, classifies each of the
        # 12 commodities into structured trend/direction/strength
        # fields matching trend_filter.py/macro_filter.py's existing
        # label vocabularies, writes to nt_macro_forecast — shadow-mode
        # diagnostic, nothing reads that table to make a live decision
        # yet. Requires OPENROUTER_API_KEY or NVIDIA_NIM_API_KEY.
        if get_macro_forecast_agent_enabled():
            self._scheduler.add_job(
                self._run_macro_forecast_agent,
                trigger=CronTrigger.from_crontab("45 2 * * *"),
                id="macro-forecast-agent-nightly",
                name="Long-term macro-forecast agent (nightly)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Bracket reachability diagnostic (2026-08-25, Phase 1 of worklog/
        # 20260825_CALIBRATION_REFRAME_SCOPE.md): weekly, Sun 07:00 UTC —
        # after the 05:00 UTC weekend heavy-sweep window so that week's
        # sweeps have landed. ALSO rebuilt after every nightly EOD resolve
        # (2026-09-16, worklog/20260916_REACHABILITY_BOOTSTRAP_UNBLOCK.md)
        # so n_stored/n_proxy stay current for p_win_bracket shadow.
        if getattr(settings, "BRACKET_REACHABILITY_ENABLED", True):
            self._scheduler.add_job(
                self._run_bracket_reachability,
                trigger=CronTrigger.from_crontab("0 7 * * sun"),
                id="bracket-reachability-weekly",
                name="Bracket reachability table (weekly)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Macro filter daily refresh (2026-08-28, worklog/
        # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md "Macro filter —
        # full build scope"): 06:00 UTC daily, own cadence (independent
        # of both sweep paths) — before the light-sweep day starts and
        # clear of the 07:00 UTC Sunday reachability job. Read-only price
        # computation; nt_signal_sim capture / live Kelly application are
        # gated separately by macro_regime_mode (config/feature_flags.yaml).
        if getattr(settings, "MACRO_REGIME_ENABLED", True):
            self._scheduler.add_job(
                self._run_macro_regime_refresh,
                trigger=CronTrigger.from_crontab("0 6 * * *"),
                id="macro-regime-daily",
                name="Macro regime refresh (daily)",
                misfire_grace_time=3600,
                coalesce=True,
            )

        # Trend filter daily refresh (2026-09-10, worklog
        # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7):
        # 06:10 UTC daily — right after macro-regime-daily, own cadence,
        # inside the same 05:16-09:20 UTC Studio-restart window (see
        # xauaud_heavy_sweep_stale_4d_force_refresh_not_firing) so it gets
        # the same restart-safe runway. Registered unconditionally, same
        # as macro-regime-daily above — NOT gated on trend_regime_mode at
        # registration time, since feature_flags.yaml is meant to
        # hot-reload with no restart required (see that module's own
        # docstring); gating registration on the mode would silently
        # require a restart to pick up an off->shadow flip, breaking that
        # guarantee. Read-only price computation; nt_signal_sim capture /
        # live Kelly application are gated separately by trend_regime_mode
        # inside _process_symbol.
        self._scheduler.add_job(
            self._run_trend_regime_refresh,
            trigger=CronTrigger.from_crontab("10 6 * * *"),
            id="trend-regime-daily",
            name="Trend regime refresh (daily)",
            misfire_grace_time=3600,
            coalesce=True,
        )

        # Kairos v3 (2026-09-24, P0.3/P0.6; hourly since 2026-09-25 for the
        # accelerated G2 schedule, worklog/kairos/
        # 20260925_RESTART_SAFE_STATE_AND_ACCELERATED_G2.md): drift backfill
        # every hour at :20 (clear of the :00/:15/:30/:45 light-sweep grid
        # and the :17 EOD resolve), so every horizon is filled within ~1h
        # of its bar closing and a restart costs at most one hour, not a
        # day. The economic calendar refreshes inside the job at most once
        # per 20h. Each run heartbeats nt_roadmap(kairos_daily_job).
        self._scheduler.add_job(
            self._run_kairos_daily,
            trigger=CronTrigger.from_crontab("20 * * * *"),
            id="kairos-daily",
            name="Kairos drift backfill (hourly) + calendar cache (daily)",
            misfire_grace_time=1800,
            coalesce=True,
        )

        # Hourly calibration resolve (2026-09-25, Kairos V3 §14.2 lever 1,
        # worklog/kairos/20260926_HOURLY_CALIBRATION_RESOLVE.md): TP/SL
        # outcomes reach nt_calibration_records within ~1h instead of at the
        # 01:17 nightly. :38 sits clear of the :00/:15/:30/:45 light sweeps
        # (TradingView batch collisions, 2026-08-24), :17 nightly and :20
        # Kairos job. Registered unconditionally; the mode (off | shadow |
        # live) is read per run so a feature_flags.yaml flip needs no restart.
        self._scheduler.add_job(
            self._run_calibration_resolve_hourly,
            trigger=CronTrigger.from_crontab("38 * * * *"),
            id="calibration-resolve-hourly",
            name="Calibration resolve (hourly)",
            misfire_grace_time=1200,
            coalesce=True,
        )

        self._scheduler.start()
        logger.info("Sweep orchestrator scheduler started")

        # Startup catch-up for fixed-time jobs that can silently skip
        # across restarts (2026-09-01) — see
        # _catch_up_stale_jobs()'s own docstring.
        asyncio.create_task(self._catch_up_stale_jobs())

        # Start the stale-symbol background checker in the event loop
        asyncio.create_task(self._stale_check_loop())

    async def stop(self) -> None:
        """Gracefully shut down the scheduler."""
        self._persist_regime_state()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Sweep orchestrator scheduler stopped")

    # ── Symbol registry ───────────────────────────────────────────

    async def _refresh_symbols(self) -> None:
        """Fetch active symbols from Supabase and (re)schedule groups.

        Also synchronises the local brick_params.yaml ``symbols:`` section
        with ``nt_symbol.active=true`` — adds seed defaults for newly-active
        symbols and prunes entries for symbols no longer active. The heavy
        sweep (daily, changed from weekly 2026-09-08) is still the only
        writer of OPTIMISED params; this sync only ensures the YAML has
        entries for every active symbol so the light sweep can read them
        between heavy-sweep runs.
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
        # '0 20 * * *' (daily 20:00 UTC) when NULL — changed from weekly
        # 2026-09-08 (worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md):
        # the weekly cadence relied on "TimesFM makes frequent
        # re-optimization unnecessary," which conflates directional
        # confidence with bracket/TP/SL geometry fit — see heavy_sweep.py's
        # module docstring for the full reasoning and the real-cost
        # measurement (~7 min/day for all 14 symbols) that made daily the
        # obvious choice once the flawed assumption was found.
        heavy_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sym in symbols:
            cron_raw = sym.get("sweep_cron") or "0 20 * * *"
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
        off the light sweep's grid (`*/15 * * * *` as of 2026-09-09, was
        `*/5 * * * *`; the :30 offset lands on both), 2026-08-24, to avoid a
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
            async with self._get_resolver_lock():
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

        # ── Reachability rebuild (Batch C bootstrap, 2026-09-16) ────────
        # Weekly Sun job alone left n_stored at migration-153 defaults (0)
        # for days, so p_win_bracket never computed. Rebuild after every
        # EOD resolve so stored-ATR counts track fresh outcomes. Same
        # best-effort contract as the rest of this job.
        try:
            await self._run_bracket_reachability()
        except Exception as exc:
            logger.error("[eod:lookback] reachability rebuild failed: %s", exc)

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
            if a.get("action") in (
                "emergency_heavy_sweep",
                "degeneracy_breaker",
                "geometry_tuning_sweep",
            )
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
        #   1. DELETE resolved rows older than signal_sim_retention_resolved_days (config/feature_flags.yaml).
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

        # ── Part V: expiry-rate threshold alert (2026-09-08, priority #3) ──
        # Runs last, after resolution has produced fresh 7-day numbers —
        # see expiry_rate_alert.py's module docstring for the full
        # rationale. Best-effort: failures are logged and counted, never
        # raised (matching every other part of this method).
        try:
            from regime_platform.sweep_orchestrator.expiry_rate_alert import (
                check_expiry_rate_alerts,
            )
            expiry_alert_summary = await check_expiry_rate_alerts()
            logger.info(
                "[eod:lookback] expiry-rate alert check done: %s",
                expiry_alert_summary,
            )
        except Exception as exc:
            logger.error("[eod:lookback] expiry-rate alert check failed: %s", exc)

        # ── Part VI: renko brick retention (2026-09-09) ────────────────────
        # Keep nt_renko_bricks bounded — see renko_retention.py's module
        # docstring for the full root cause (worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7
        # "Finding 2"): daily brick_size re-optimization orphans the
        # previous grid's rows with nothing to ever clean them up. Deletes
        # (1) rows whose brick_size no longer matches the symbol's current
        # config/brick_params.yaml value, and (2) rows older than
        # renko_brick_retention_days. Best-effort: failures are logged and
        # counted, never raised (matching every other part of this method).
        try:
            from regime_platform.sweep_orchestrator.renko_retention import (
                run_renko_brick_retention,
            )
            renko_retention_summary = await run_renko_brick_retention()
            logger.info(
                "[eod:lookback] renko brick retention done: %s",
                renko_retention_summary,
            )
        except Exception as exc:
            logger.error("[eod:lookback] renko brick retention failed: %s", exc)

    async def _execute_emergency_heavy_sweeps(self, emergency_syms: list[str]) -> None:
        """Run emergency/geometry-tuning heavy sweeps (best-effort).

        T4 (2026-09-19): same single-symbol heavy path for
        ``geometry_tuning_sweep`` as for calibration emergencies — do not
        wait for the daily heavy cron to rewrite bricks.
        """
        if not emergency_syms:
            return
        try:
            from regime_platform.sweep_orchestrator.supabase_client import (
                fetch_active_symbols,
                fetch_last_full_heavy_sweep_ts,
            )
            from regime_platform.core.feature_flags import (
                get_geometry_tuning_cooldown_hours,
                get_geometry_tuning_sweep_mode,
            )
            cooldown_h = float(getattr(settings, "EOD_HEAVY_SWEEP_COOLDOWN_HOURS", 12) or 12)
            geo_mode = get_geometry_tuning_sweep_mode()
            geo_cd = get_geometry_tuning_cooldown_hours()
            now = datetime.now(timezone.utc)

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
                # Geometry enqueues only fire the sweep when mode=live
                # (write_eod_action already gated); cooldown uses shorter geo default.
                cd = geo_cd if geo_mode != "off" else cooldown_h

                last_full_ts = await fetch_last_full_heavy_sweep_ts(sym_name)
                if last_full_ts is not None:
                    age_h = (now - last_full_ts).total_seconds() / 3600.0
                    if age_h < cd:
                        logger.info(
                            "[eod:lookback] emergency sweep %s: skipped — "
                            "last full sweep %.1fh ago (< %.0fh cooldown)",
                            sym_name, age_h, cd,
                        )
                        continue

                try:
                    result = await self.run_heavy_sweep_for_symbol(sym)
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

    async def _run_knowledge_reindex(self) -> None:
        """Self-learning local knowledge-index rebuild (daily, 01:45 UTC).

        See ``regime_platform/services/self_learning/knowledge_index.py``
        and worklog
        20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §3.
        Indexes the codebase + worklogs + ``nt_roadmap`` notes into a
        local SQLite file (never Supabase — see that worklog section for
        why) for future root-cause retrieval. The heavy work
        (chunking/embedding/SQLite writes) already runs off the event
        loop via ``asyncio.to_thread`` inside ``build_index`` itself.
        Purely additive/local, read-only w.r.t. trading. Best-effort:
        failures are logged, never raised.
        """
        from regime_platform.services.self_learning.knowledge_index import build_index

        try:
            summary = await build_index()
            logger.info("[self-learning] knowledge index rebuilt: %s", summary)
        except Exception as exc:
            logger.error("[self-learning] knowledge index rebuild failed: %s", exc)

    async def _run_self_learning_detectors(self) -> None:
        """Phase 2 self-learning detectors (daily, 02:00 UTC).

        See ``regime_platform/services/self_learning/run_detectors.py``
        and worklog
        20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md
        SS10/SS11. Runs all three detectors (regime-vs-price mismatch,
        dead-mechanism staleness, redundant-computation agreement)
        against real data and writes any findings to
        ``nt_self_learning_findings``. Purely diagnostic, read-only
        w.r.t. trading. Best-effort: failures are logged, never raised.
        """
        from regime_platform.services.self_learning.run_detectors import run_all_detectors

        try:
            summary = await run_all_detectors()
            logger.info("[self-learning] detectors run: %s", summary)
        except Exception as exc:
            logger.error("[self-learning] detectors run failed: %s", exc)

    async def _run_self_learning_triage(self) -> None:
        """Phase 3 MVP LLM-triage (daily, 02:30 UTC).

        See ``regime_platform/services/self_learning/triage.py`` and
        worklog 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md
        §2. Reads untriaged ``nt_self_learning_findings`` rows, asks an
        LLM for a narrative + an optional bounded Tier D parameter
        proposal, writes both to ``nt_self_learning_reports``. No write
        access to any live parameter. Best-effort: failures are logged,
        never raised.
        """
        from regime_platform.services.self_learning.triage import run_triage

        try:
            summary = await run_triage()
            logger.info("[self-learning] triage run: %s", summary)
        except Exception as exc:
            logger.error("[self-learning] triage run failed: %s", exc)

    async def _run_macro_forecast_agent(self) -> None:
        """Long-term (12-month) macro-forecast agent (daily, 02:45 UTC).

        See ``regime_platform/services/self_learning/macro_forecast_agent.py``
        and worklog 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md
        §4. Classifies each of the 12 commodities in
        ``docs/LLM/commodity_12m_forecast_model_2027.md`` into structured
        trend/direction/strength fields, writes to ``nt_macro_forecast``.
        Shadow-mode diagnostic — nothing reads that table to make a live
        decision yet. Best-effort: failures are logged, never raised.
        """
        from regime_platform.services.self_learning.macro_forecast_agent import run_macro_forecast

        try:
            summary = await run_macro_forecast()
            logger.info("[self-learning] macro forecast agent run: %s", summary)
        except Exception as exc:
            logger.error("[self-learning] macro forecast agent run failed: %s", exc)

    async def _run_bracket_reachability(self) -> None:
        """Bracket reachability diagnostic (weekly, Sun 07:00 UTC), then
        the Phase 2 calibration-bias-by-bucket + entry-drift diagnostics.

        See ``regime_platform/sweep_orchestrator/bracket_reachability.py``
        and worklog/20260825_CALIBRATION_REFRAME_SCOPE.md (Phase 1/2).
        Phase 1 computes distance/ATR hit-rate + time-to-resolution
        buckets from resolved nt_signal_sim rows and upserts
        nt_bracket_reachability. Phase 2 (gated separately, runs after
        Phase 1 in the same job so its pct_expired join reads a fresh
        table) computes calibration bias/expectancy per bucket and
        post-entry drift per symbol. Both are read-only diagnostics —
        neither changes calibration bias enforcement, live qualification,
        or bracket selection. Best-effort: failures are logged, never
        raised.
        """
        from regime_platform.sweep_orchestrator.bracket_reachability import (
            build_reachability_table,
        )

        try:
            rows = await build_reachability_table()
            logger.info(
                "[bracket-reachability] done: %d bucket row(s) across %d symbol(s)",
                len(rows), len({r["symbol"] for r in rows}),
            )
        except Exception as exc:
            logger.error("[bracket-reachability] failed: %s", exc)

        if getattr(settings, "CALIBRATION_BIAS_BY_BUCKET_ENABLED", True):
            from regime_platform.sweep_orchestrator.calibration_bias_bucket import (
                build_calibration_bias_by_bucket,
                build_entry_drift,
            )

            try:
                bias_rows = await build_calibration_bias_by_bucket()
                logger.info(
                    "[calibration-bias-bucket] done: %d bucket row(s) across %d symbol(s)",
                    len(bias_rows), len({r["symbol"] for r in bias_rows}),
                )
            except Exception as exc:
                logger.error("[calibration-bias-bucket] failed: %s", exc)

            try:
                drift_rows = await build_entry_drift()
                logger.info(
                    "[entry-drift] done: %d symbol row(s)", len(drift_rows),
                )
            except Exception as exc:
                logger.error("[entry-drift] failed: %s", exc)

            # P2 flip evidence (migration 163): daily expectancy cohorts for
            # p_win_bracket accept vs the full qualified book.
            from regime_platform.sweep_orchestrator.live_expectancy import (
                build_live_expectancy_daily,
            )
            from regime_platform.core.feature_flags import (
                get_min_p_win_bracket,
            )

            try:
                exp_rows = await build_live_expectancy_daily(
                    min_p_win_bracket=get_min_p_win_bracket(),
                )
                logger.info(
                    "[live-expectancy] done: %d day×cohort row(s)", len(exp_rows),
                )
            except Exception as exc:
                logger.error("[live-expectancy] failed: %s", exc)

        # Macro-edge-by-symbol (2026-08-28, worklog/
        # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md): the win/loss+PnL
        # evidence for the macro filter (macro_filter.py). Chained onto
        # this same weekly job rather than a new cron slot — same
        # resolved-nt_signal_sim population as the two diagnostics above,
        # same cadence. Read-only: informs macro.boost_scale/penalty_scale
        # (config/feature_flags.yaml) review, does not auto-tune them.
        if getattr(settings, "MACRO_REGIME_ENABLED", True):
            from regime_platform.sweep_orchestrator.macro_edge import (
                build_macro_edge_table,
            )

            try:
                edge_rows = await build_macro_edge_table()
                logger.info(
                    "[macro-edge] done: %d bucket row(s) across %d symbol(s)",
                    len(edge_rows), len({r["symbol"] for r in edge_rows}),
                )
            except Exception as exc:
                logger.error("[macro-edge] failed: %s", exc)

        # Trend-edge-by-symbol (2026-09-13, worklog/
        # 20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md
        # §12): the win/loss+PnL evidence for the trend filter
        # (trend_filter.py), structural clone of macro-edge above. Chained
        # onto the same weekly job, same resolved-nt_signal_sim population,
        # same cadence. Gated on trend_regime_mode != "off" since
        # nt_signal_sim.trend_label is only ever populated in shadow/live —
        # querying with the mode off would just repeatedly find nothing.
        # Read-only: informs the trend_regime_mode shadow->live (or off)
        # decision, does not auto-tune it.
        from regime_platform.core.feature_flags import get_trend_regime_mode
        if get_trend_regime_mode() != "off":
            from regime_platform.sweep_orchestrator.trend_edge import (
                build_trend_edge_table,
            )

            try:
                trend_edge_rows = await build_trend_edge_table()
                logger.info(
                    "[trend-edge] done: %d bucket row(s) across %d symbol(s)",
                    len(trend_edge_rows), len({r["symbol"] for r in trend_edge_rows}),
                )
            except Exception as exc:
                logger.error("[trend-edge] failed: %s", exc)

        # HMM shadow accuracy (2026-08-31, worklog/
        # 20260831_SHADOW_TO_LIVE_ROADMAP.md): outcome-linked evidence for
        # the Tier 1/2 markov_source_mode/trend_probs_gate_mode live-mode
        # decision (review target 2026-09-21). Chained onto this same
        # weekly job — same resolved-nt_signal_sim population, same
        # cadence. Read-only: informs the decision, does not make it.
        if getattr(settings, "HMM_SHADOW_ACCURACY_ENABLED", True):
            from regime_platform.sweep_orchestrator.hmm_shadow_accuracy import (
                build_hmm_shadow_accuracy,
            )

            try:
                accuracy_rows = await build_hmm_shadow_accuracy()
                logger.info(
                    "[hmm-shadow-accuracy] done: %d row(s) across %d symbol(s)",
                    len(accuracy_rows), len({r["symbol"] for r in accuracy_rows}),
                )
            except Exception as exc:
                logger.error("[hmm-shadow-accuracy] failed: %s", exc)

    async def _run_macro_regime_refresh(self) -> None:
        """Macro filter daily refresh (2026-08-28, worklog/
        20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md "Macro filter —
        full build scope"). Recomputes the 200-day-SMA-based macro
        score/label/strength for every active symbol and upserts
        nt_macro_regime. Best-effort: failures are logged, never raised.
        """
        from regime_platform.sweep_orchestrator.macro_filter import (
            refresh_macro_regime,
        )

        try:
            rows = await refresh_macro_regime()
            logger.info("[macro-regime] done: %d symbol(s) refreshed", len(rows))
        except Exception as exc:
            logger.error("[macro-regime] failed: %s", exc)

    def _kairos_stamp(
        self, symbol: str, regime_label: str | None, atr: float | None, source: str,
    ) -> dict[str, float | None] | None:
        """Kairos v3 P0.5 capture stamps for one nt_signal_sim row
        (worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md). Light sweep only; None
        when kairos_mode is off or anything fails (fail-open — a stamp
        problem never touches the signal itself)."""
        if source != "sweep-light":
            return None
        try:
            from regime_platform.core.feature_flags import get_kairos_mode
            if get_kairos_mode() == "off":
                return None
            from regime_platform.sweep_orchestrator.kairos import capture_factors
            _age, atr_start = self._kairos_run_state.get(symbol, (None, None))
            spread = hmm_quote_fetcher.spread_bps_by_symbol().get(symbol)
            return capture_factors(
                self._kairos_ohlcv.get(symbol),
                atr_now=atr, atr_run_start=atr_start, spread_bps=spread,
                forming_last=True,
            )
        except Exception as exc:
            logger.debug("[kairos] stamp %s failed: %s", symbol, exc)
            return None

    async def _run_kairos_daily(self) -> None:
        """Kairos v3 P0.3 + P0.6 (hourly at :20 since 2026-09-25): drift
        backfill on every light-sweep signal whose horizon bars exist, plus
        the economic-calendar cache (refreshed only when > 20h old). Both
        best-effort and read-only for trading; the calendar respects
        kairos_mode=off, the drift backfill always runs (outcome data).
        Ends with a heartbeat on nt_roadmap(kairos_daily_job) so the weekly
        check can prove the job ran (scope §12.2 W1 must-pass)."""
        from regime_platform.core.feature_flags import get_kairos_mode
        from regime_platform.sweep_orchestrator.kairos import (
            DEFAULT_CALENDAR_PATH,
            backfill_drift_horizons,
            refresh_calendar_cache,
            write_job_heartbeat,
        )
        cal: dict[str, Any] = {"skipped": "fresh"}
        bf: dict[str, Any] = {}
        if get_kairos_mode() != "off":
            try:
                p = DEFAULT_CALENDAR_PATH
                if not p.exists() or time.time() - p.stat().st_mtime > 20 * 3600:
                    cal = await refresh_calendar_cache()
                logger.info("[kairos:daily] calendar %s", cal)
            except Exception as exc:
                cal = {"error": type(exc).__name__}
                logger.warning("[kairos:daily] calendar failed: %s", exc)
        try:
            bf = await backfill_drift_horizons()
            logger.info("[kairos:daily] drift backfill %s", bf)
        except Exception as exc:
            bf = {"error": type(exc).__name__}
            logger.warning("[kairos:daily] drift backfill failed: %s", exc)
        await write_job_heartbeat("kairos_daily_job", {"drift_backfill": bf, "calendar": cal})

    def _get_resolver_lock(self) -> asyncio.Lock:
        """The shared writing-resolver lock (created lazily if __init__
        was bypassed)."""
        lock = getattr(self, "_resolver_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._resolver_lock = lock
        return lock

    async def _run_calibration_resolve_hourly(self) -> None:
        """Hourly calibration resolve (Kairos V3 §14.2 lever 1, 2026-09-25).

        ``calibration_resolve_hourly_mode``:
          off    — nothing.
          shadow — ``dry_run_pending_signals``: read-only, same judge and
                   windowing as the writing resolver; logs what would
                   resolve now and the evidence-hours saved vs the next
                   nightly run. No writes.
          live   — ``resolve_pending_signals`` (the nightly's own function)
                   under the shared resolver lock; skipped, not queued, if
                   another resolver pass holds the lock.
        Heartbeats nt_roadmap(calibration_resolve_hourly) either way. Never
        raises out of the scheduler tick.
        """
        from regime_platform.core.feature_flags import get_calibration_resolve_hourly_mode
        from regime_platform.sweep_orchestrator.eod_resolver import (
            dry_run_pending_signals,
            evidence_hours_saved,
            resolve_pending_signals,
        )
        from regime_platform.sweep_orchestrator.kairos import write_job_heartbeat

        mode = get_calibration_resolve_hourly_mode()
        if mode == "off":
            return
        summary: dict[str, Any] = {"mode": mode}
        try:
            if mode == "shadow":
                dry = await dry_run_pending_signals(
                    limit=settings.EOD_RESOLVE_LIMIT,
                    max_total=settings.EOD_RESOLVE_MAX_TOTAL,
                )
                summary.update(dry)
                tp_sl = dry["by_outcome"].get("tp_hit", 0) + dry["by_outcome"].get("sl_hit", 0)
                logger.info(
                    "[calib:hourly] shadow — would resolve %d now (%s), %d TP/SL "
                    "(qualified %d) reaching calibration %.1fh before the nightly; "
                    "insufficient_bars=%d windowed_out=%d errors=%d scanned=%d",
                    dry["would_resolve"], dry["by_outcome"], tp_sl, dry["qualified_tp_sl"],
                    dry["hours_saved"], dry["insufficient_bars"], dry["windowed_out"],
                    dry["errors"], dry["scanned"],
                )
            else:
                lock = self._get_resolver_lock()
                if lock.locked():
                    summary["skipped"] = "resolver_busy"
                    logger.info("[calib:hourly] live — skipped, another resolver pass is running")
                else:
                    hours = evidence_hours_saved(datetime.now(timezone.utc))
                    async with lock:
                        res = await resolve_pending_signals(
                            limit=settings.EOD_RESOLVE_LIMIT,
                            max_total=settings.EOD_RESOLVE_MAX_TOTAL,
                        )
                    summary.update(res)
                    summary["hours_saved"] = hours
                    logger.info(
                        "[calib:hourly] live — resolved %d (%s), %.1fh before the nightly",
                        res.get("resolved", 0), res.get("by_outcome"), hours,
                    )
        except Exception as exc:  # noqa: BLE001
            summary["error"] = type(exc).__name__
            logger.warning("[calib:hourly] %s failed: %s", mode, exc)
        summary.pop("by_symbol", None)
        await write_job_heartbeat("calibration_resolve_hourly", summary)

    async def _run_trend_regime_refresh(self) -> None:
        """Trend filter daily refresh (2026-09-10, worklog
        20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7).
        Recomputes the SMA-10/20/50 crossover + RSI-14 read for every
        active symbol and upserts nt_trend_regime. Best-effort: failures
        are logged, never raised.
        """
        from regime_platform.sweep_orchestrator.trend_filter import (
            refresh_trend_regime,
        )

        try:
            rows = await refresh_trend_regime()
            logger.info("[trend-regime] done: %d symbol(s) refreshed", len(rows))
        except Exception as exc:
            logger.error("[trend-regime] failed: %s", exc)

    async def _catch_up_stale_jobs(self) -> None:
        """Startup catch-up for fixed-time-of-day/week jobs an in-memory
        APScheduler job store can silently skip across restarts
        (2026-09-01, worklog/
        20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md).

        ``add_job()`` on a fresh scheduler computes the next fire time
        strictly forward from "now" — it has no memory that today's/
        this-week's fire time already passed if the process wasn't alive
        to see it. Found live: the studio's restart cadence clusters
        right around the 06:00 UTC daily trigger, so
        ``macro-regime-daily`` and the weekly ``bracket-reachability``
        job kept silently rolling forward to "tomorrow"/"next Sunday"
        every cycle (`nt_macro_regime` sat 3.5 days stale, the weekly
        diagnostics 6 days stale).

        CORRECTION (2026-09-05, worklog
        20260905_EOD_RESOLVER_SCHEDULER_MISS.md): this docstring
        originally assumed "the 00:32 UTC EOD job — which lands in the
        gaps between restarts — kept firing normally." Confirmed false
        live: a crash in the ~6-minute gap between a light-sweep tick and
        the 00:32 ``eod-lookback`` fire swallowed an entire day's
        resolution run, and the next instance up pushed its next fire to
        the FOLLOWING day — 45h of zero ``nt_signal_sim`` resolutions
        before it was caught. Same failure mode, just a job that hadn't
        been added to this check's scope yet. It has now.

        Reads each job's own diagnostic table for its last real success
        via ``fetch_max_computed_at`` — no new config file, no second
        piece of state to keep in sync — and fires the job immediately
        (once) if it's gone stale. Thresholds
        (``MACRO_REGIME_STALE_AFTER``/``BRACKET_REACHABILITY_STALE_AFTER``/
        ``EOD_LOOKBACK_STALE_AFTER``) are deliberately shorter than the
        full cycle so a miss is caught the same day rather than waiting
        for a second cycle — the accepted tradeoff (also true before this
        change, for the other two) is that a restart late in a healthy
        cycle can re-trigger a job that wasn't actually stale; harmless,
        since all three targets are idempotent re-reads/re-computations.
        Best-effort: never raises into scheduler startup.
        """
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_max_computed_at,
        )

        now = datetime.now(timezone.utc)

        if getattr(settings, "MACRO_REGIME_ENABLED", True):
            try:
                last = await fetch_max_computed_at("nt_macro_regime")
                if _is_stale(last, MACRO_REGIME_STALE_AFTER, now):
                    logger.warning(
                        "[startup-catchup] nt_macro_regime last computed_at=%s — "
                        "stale, running macro regime refresh now instead of "
                        "waiting for the next 06:00 UTC window",
                        last,
                    )
                    asyncio.create_task(self._run_macro_regime_refresh())
            except Exception as exc:
                logger.warning(
                    "[startup-catchup] macro regime staleness check failed: %s", exc,
                )

        if getattr(settings, "BRACKET_REACHABILITY_ENABLED", True):
            try:
                last = await fetch_max_computed_at("nt_bracket_reachability")
                if _is_stale(last, BRACKET_REACHABILITY_STALE_AFTER, now):
                    logger.warning(
                        "[startup-catchup] nt_bracket_reachability last "
                        "computed_at=%s — stale, running the weekly diagnostics "
                        "job now instead of waiting for the next Sunday 07:00 "
                        "UTC window",
                        last,
                    )
                    asyncio.create_task(self._run_bracket_reachability())
            except Exception as exc:
                logger.warning(
                    "[startup-catchup] bracket reachability staleness check "
                    "failed: %s", exc,
                )

        # eod-lookback (2026-09-05, worklog
        # 20260905_EOD_RESOLVER_SCHEDULER_MISS.md) — not gated behind a
        # settings kill-switch, matching the base eod-lookback job itself
        # (unlike the two above, it isn't gated by one either).
        try:
            last = await fetch_max_computed_at("nt_signal_sim", column="resolved_at")
            if _is_stale(last, EOD_LOOKBACK_STALE_AFTER, now):
                logger.warning(
                    "[startup-catchup] nt_signal_sim last resolved_at=%s — "
                    "stale, running the EOD resolver now instead of "
                    "waiting for the next 00:32 UTC window",
                    last,
                )
                asyncio.create_task(self._run_eod_lookback())
        except Exception as exc:
            logger.warning(
                "[startup-catchup] eod-lookback staleness check failed: %s", exc,
            )

        # eod-correction-intraday (RM-42, 2026-09-12) — nt_symbol_calibration_state
        # is upserted per-symbol on EVERY run_eod_correction() pass (intraday
        # OR nightly, action="none"/CALIBRATED included, not just on an
        # anomaly) — so its max(computed_at) is a reliable "did a correction
        # pass run recently" signal regardless of which of the two callers
        # produced it.
        if settings.EOD_CORRECTION_INTRADAY_ENABLED:
            try:
                last = await fetch_max_computed_at("nt_symbol_calibration_state")
                if _is_stale(last, EOD_CORRECTION_INTRADAY_STALE_AFTER, now):
                    logger.warning(
                        "[startup-catchup] nt_symbol_calibration_state last "
                        "computed_at=%s — stale, running the intraday EOD "
                        "correction pass now instead of waiting for the next "
                        ":30 UTC slot",
                        last,
                    )
                    asyncio.create_task(self._run_eod_correction_only())
            except Exception as exc:
                logger.warning(
                    "[startup-catchup] eod-correction-intraday staleness check "
                    "failed: %s", exc,
                )

        await self._catch_up_light_sweep_slot()

    async def _catch_up_light_sweep_slot(self, now: datetime | None = None) -> None:
        """Re-run the most recent light-sweep slot a restart dropped, for
        the symbols that have no ``sweep-light`` row since it (see
        ``_missed_light_slot``). Flag ``light_sweep_catchup_mode``
        (off | live). Fail-safe: if the swept-symbols read fails, nothing
        runs — a double tick is worse than a missed one. Never raises."""
        from regime_platform.core.feature_flags import get_light_sweep_catchup_mode
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_light_swept_symbols_since,
        )

        try:
            if get_light_sweep_catchup_mode() == "off" or self._scheduler is None:
                return
            now = now or datetime.now(timezone.utc)
            for job in list(self._scheduler.get_jobs()):
                if not job.id.startswith("sweep-") or len(job.args) < 2:
                    continue
                cron_expr, sym_list = job.args[0], job.args[1]
                slot = _missed_light_slot(job.trigger, now)
                if slot is None:
                    continue
                swept = await fetch_light_swept_symbols_since(slot)
                if swept is None:
                    logger.warning(
                        "[startup-catchup] light sweep [%s]: swept-symbols read "
                        "failed — skipping catch-up (fail-safe)", cron_expr,
                    )
                    continue
                missing = _unswept_symbols(sym_list, swept)
                if not missing:
                    continue
                logger.warning(
                    "[startup-catchup] light sweep [%s] slot %s missed for %d/%d "
                    "symbols — running it now: %s",
                    cron_expr, slot.isoformat(), len(missing), len(sym_list),
                    [s.get("symbol") for s in missing],
                )
                asyncio.create_task(self._sweep_group(cron_expr, missing))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[startup-catchup] light sweep catch-up failed: %s", exc)

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
        heavy sweeps are dispatched for every flagged symbol; the
        ``EOD_HEAVY_SWEEP_COOLDOWN_HOURS`` cooldown is applied uniformly
        (this pass and the nightly pass alike) inside
        ``_execute_emergency_heavy_sweeps`` itself — see that method's
        docstring (2026-09-09 reconciliation with the now-unconditional
        daily heavy-sweep cadence).

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
        # Cooldown (2026-09-09, worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16) is
        # now applied uniformly inside _execute_emergency_heavy_sweeps
        # itself (durable, DB-backed, source-agnostic) — no separate
        # pre-filter needed here.
        emergency_syms = sorted({
            a.get("symbol")
            for a in (correction_summary.get("actions") or [])
            if a.get("action") in (
                "emergency_heavy_sweep",
                "degeneracy_breaker",
                "geometry_tuning_sweep",
            )
                and a.get("symbol")
        })
        if not emergency_syms:
            return

        logger.info(
            "[eod:correction-intraday] emergency heavy sweeps requested: %s",
            emergency_syms,
        )
        await self._execute_emergency_heavy_sweeps(emergency_syms)

    async def _sweep_group(
        self, cron_expr: str, symbols: list[dict[str, Any]]
    ) -> None:
        """Process all symbols in one cron group."""
        # Filter by market hours. Read fresh each call (not the module-level
        # SWEEP_AFTER_HOURS constant) so a feature_flags.yaml edit takes
        # effect on the next tick, not the next deploy — see the constant's
        # definition comment above.
        sweep_after_hours = get_sweep_after_hours()
        if sweep_after_hours:
            active = symbols
            logger.info(
                "Running sweep group [%s]: %d symbols (sweep_after_hours=true, "
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
        # Real per-bar timestamps, index-aligned with valid_prices[symbol]
        # (2026-09-09, worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7) —
        # threaded through to the Renko brick capture below instead of the
        # brick engine defaulting to a bare auto-incrementing index. Empty
        # for a symbol if the fetch had no usable time field for any bar.
        valid_timestamps: dict[str, list[float | None]] = {}

        try:
            _ohlcv: dict[str, list[dict[str, Any]]] = {}
            tv_results, tv_timestamps = await fetch_tradingview_batch(
                active, 200, include_timestamps=True, ohlcv_out=_ohlcv,
            )
            self._kairos_ohlcv.update(_ohlcv)
            for sym_name, prices in tv_results.items():
                if len(prices) < 81:
                    logger.warning(
                        "%s: insufficient data (%d bars, need >=81)",
                        sym_name, len(prices),
                    )
                    continue
                valid_prices[sym_name] = prices
                valid_timestamps[sym_name] = tv_timestamps.get(sym_name, [])
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
        # Skipped entirely in trend_hmm_live mode — the retired legacy
        # model's emit_markov() output is never read in that mode (see
        # MARKOV_SOURCE_MODE docstring above), and TA/microstructure
        # features are read directly from the FeatureBuffer independently
        # of fit(), not from the fitted model.
        if get_markov_source_mode() != "trend_hmm_live":
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

            # ── TimesFM k-adaptive dampening (2026-09-11, worklog
            # 20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md
            # Part 12) ────────────────────────────────────────────────
            # Runs BEFORE `trade_signal`/`signal_is_qualifiable` exist —
            # this point in the loop has no "is this signal qualifiable"
            # concept yet (that's determined much later), so this fetch
            # is gated only on the mode flag, not on signal qualifiability,
            # unlike every other longitudinal-filter block in this
            # function. Costs one extra nt_trend_regime read per symbol
            # per light-sweep tick when the mode is on (off by default =
            # zero cost) — architecturally consistent with
            # _fetch_timesfm_cached's own unconditional fetch at this
            # same point, not a new class of cost this function didn't
            # already have.
            timesfm_k_override: float | None = None
            timesfm_shadow_k: float | None = None
            k_adaptive_mode = get_timesfm_k_adaptive_mode()
            if k_adaptive_mode != "off":
                from regime_platform.core.feature_flags import (
                    get_timesfm_k_ceiling,
                    get_timesfm_k_floor,
                )
                from regime_platform.sweep_orchestrator.timesfm_k_adaptive import (
                    compute_adaptive_timesfm_k,
                )
                from regime_platform.sweep_orchestrator.trend_filter import (
                    get_trend_regime as _get_trend_regime_for_k,
                )
                try:
                    _trend_row_for_k = await _get_trend_regime_for_k(symbol)
                    _k_adaptive = compute_adaptive_timesfm_k(
                        TIMESFM_SIGMOID_K,
                        (_trend_row_for_k or {}).get("trend_strength"),
                        get_timesfm_k_floor(), get_timesfm_k_ceiling(),
                    )
                    if k_adaptive_mode == "live":
                        timesfm_k_override = _k_adaptive
                    else:
                        timesfm_shadow_k = _k_adaptive
                except Exception as exc:
                    logger.warning(
                        "[sweep:orch] %s: TimesFM k-adaptive fetch failed "
                        "(%s: %s), using base k", symbol, type(exc).__name__, exc,
                    )

            # Fetch TimesFM (cached 15min)
            p_timesfm_result = await _fetch_timesfm_cached(
                symbol, prices,
                k_override=timesfm_k_override, shadow_k=timesfm_shadow_k,
            )

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
                result = await self._process_symbol(
                    sym, prices, p_timesfm, source="sweep-light",
                    timestamps=valid_timestamps.get(symbol),
                )
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
            # Restart-safe regime state (2026-09-25): snapshot after every
            # symbol so a restart mid-group loses at most one tick.
            self._persist_regime_state()

            if i < len(symbols_to_process) - 1:
                await asyncio.sleep(STAGGER_SECONDS)

    def _persist_regime_state(self) -> None:
        """Snapshot debouncers / run tracker / label-continuity means /
        EMAs to disk (regime_state_store). Bookkeeping only; never raises."""
        try:
            from regime_platform.core.feature_flags import get_regime_state_persist_mode
            if get_regime_state_persist_mode() == "off":
                return
            from regime_platform.sweep_orchestrator import regime_state_store
            regime_state_store.save(self)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[regime-state] persist skipped: %s", exc)

    def _restore_regime_state(self) -> None:
        """Restore the last snapshot on start if it's fresh (see
        regime_state_store). Never raises."""
        try:
            from regime_platform.core.feature_flags import (
                get_regime_state_persist_max_age_hours,
                get_regime_state_persist_mode,
            )
            if get_regime_state_persist_mode() == "off":
                return
            from regime_platform.sweep_orchestrator import regime_state_store
            res = regime_state_store.restore(
                self,
                max_age_hours=get_regime_state_persist_max_age_hours(),
                n_confirm=get_regime_label_debounce_ticks(),
            )
            logger.info("[regime-state] restore %s", res)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[regime-state] restore skipped: %s", exc)

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
        timestamps: list[float | None] | None = None,
    ) -> dict[str, Any]:
        """Run one light-sweep cycle for a single symbol.

        Args:
            sym: nt_symbol row dict.
            prices: Hourly close prices.
            p_timesfm: TimesFM probability (cached), if available.
            source: Sweep provenance — ``"sweep-light"`` (default, from
                ``_sweep_group`` / stale-retry) or ``"sweep-heavy"``.
                Stamped onto the ``nt_signal_sim`` capture row.
            timestamps: Real per-bar epoch-seconds, index-aligned with
                ``prices`` (2026-09-09, worklog
                20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7).
                Optional and defaults to ``None`` for backward compatibility
                with existing callers/tests that invoke this synchronously
                without a timestamp array — the Renko brick-capture block
                below falls back to its own documented fallback when this
                is ``None`` or incomplete.

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

        # Warm-start (2026-09-01, worklog
        # 20260901_REGIME_LABEL_STABILITY_SCOPE.md) — root-cause fix for
        # regime_label flapping tick-to-tick. Gated separately from the
        # label-continuity mechanism above: off by default, since it
        # changes the fit itself rather than just relabeling its output.
        regime_warm_start_mode = get_regime_warm_start_mode()
        symbol_prev_params = (
            self._regime_warm_start_params.get(symbol, {})
            if regime_warm_start_mode == "live" else {}
        )

        model = await RegimeHMM.from_runtime_config()
        model.fit(
            np_prices,
            prev_vol_means=symbol_prev_means.get("vol"),
            prev_trend_means=symbol_prev_means.get("trend"),
            prev_vol_params=symbol_prev_params.get("vol"),
            prev_trend_params=symbol_prev_params.get("trend"),
        )
        regime: RegimeSnapshot = model.predict(np_prices)
        self._regime_label_means[symbol] = {
            "vol": model.vol_label_means,
            "trend": model.trend_label_means,
        }
        if regime_warm_start_mode == "live":
            self._regime_warm_start_params[symbol] = {
                "vol": model.vol_raw_params,
                "trend": model.trend_raw_params,
            }

        regime_label = regime.regime_label or "unknown"
        regime_conf = regime.confidence
        trend_probs = dict(zip(RegimeHMM.TREND_LABELS, regime.trend_probs))
        # Kelly-only direction read (2026-09-04, worklog
        # 20260904_KELLY_DIRECTION_AWARE_SIZING.md), deliberately NOT the
        # same `direction` computed later at "4. Direction & trade
        # signal" — that one is deliberately positioned AFTER the regime-
        # label debounce block below so it can pick up a debounced label
        # once regime_label_debounce_mode=live (see that block's own
        # comment). Kelly sizing/floor stay on the raw label here, same
        # "stays on raw" boundary Fix 2 already drew for the Kelly floor.
        _kelly_direction = "short" if "bear" in regime_label.lower() else "long"

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
                direction=_kelly_direction,
            ),
            annualise_factor=annualise_factor,
        )
        kelly_f = kelly_result.recommended_f

        # ── 2a. Kelly edge diagnostics ──
        # _mu/_sigma2 are the RAW price-direction mean/variance (unchanged
        # meaning from before this fix) — kept as-is since they're also
        # useful as a plain "which way has price actually been moving"
        # read. _mu_for_floor is the DIRECTION-ADJUSTED value (2026-09-04,
        # worklog 20260904_KELLY_DIRECTION_AWARE_SIZING.md) — mirrors what
        # PositionSizer.size() does internally, needed separately here
        # because _apply_regime_kelly_floor takes mu as a plain float, not
        # a KellyResult.
        _mu = float(np.mean(np.array(returns, dtype=float)))
        _sigma2 = float(np.var(np.array(returns, dtype=float), ddof=1))
        _mu_for_floor = _mu if _kelly_direction == "long" else -_mu
        logger.info(
            "%s: kelly math — direction=%s, mu=%.8f, sigma2=%.8f, mu/sigma2=%.4f, "
            "full=%s, frac=%s, vol_scaled=%s, regime_gated=%s, recommended=%s",
            symbol, _kelly_direction, _mu, _sigma2, (_mu / _sigma2 if _sigma2 > 0 else 0.0),
            kelly_result.full_kelly_f, kelly_result.fractional_f,
            kelly_result.vol_scaled_f, kelly_result.regime_gated_f,
            kelly_result.recommended_f,
        )

        # ── 2b. Regime-based Kelly floor (fix #3b, 2026-08-22 audit) ──
        # See _apply_regime_kelly_floor()'s docstring for the full rationale
        # and worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2/§4 fix #3b.
        # Passed _mu_for_floor (direction-adjusted), not raw _mu — see
        # 20260904_KELLY_DIRECTION_AWARE_SIZING.md: otherwise this floor
        # would stay blind to shorts even after the base Kelly calc above
        # is fixed, since it's a separate evaluation of the same question
        # ("is there a negative edge here") on the same underlying data.
        _require_nonneg_mu = bool(
            getattr(settings, "KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU", True)
        )
        kelly_f, _floor_applied, _floor_withheld = _apply_regime_kelly_floor(
            kelly_f, _mu_for_floor, regime_label, regime_conf, _require_nonneg_mu,
        )
        if _floor_withheld:
            logger.info(
                "%s: Kelly was 0, regime floor WITHHELD — measured mu=%.8f "
                "for direction=%s is negative (regime=%s, conf=%.3f); a "
                "negative historical edge is not force-sized just because "
                "regime confidence is high.",
                symbol, _mu_for_floor, _kelly_direction, regime_label, regime_conf,
            )
        elif _floor_applied:
            logger.info(
                "%s: Kelly was 0 — applied regime floor %.4f (conf=%.3f, "
                "regime=%s, direction=%s, mu=%.8f)",
                symbol, kelly_f, regime_conf, regime_label, _kelly_direction, _mu_for_floor,
            )

        # ── 2c. Regime-label debounce (Fix 2, 2026-09-01, worklog
        # 20260901_REGIME_LABEL_STABILITY_SCOPE.md) ──
        # Deliberately AFTER the Kelly floor above: that consumer stays on
        # the RAW label (same wide-blast-radius boundary this session has
        # held throughout — only direction selection and regime-shift
        # detection below are in scope, per the worklog). Everything from
        # here on reads whatever `regime_label` ends up bound to, so a
        # live-mode reassignment naturally also becomes what gets
        # persisted as nt_sweep_result/nt_signal_sim's "regime" column —
        # the worklog's "whichever is authoritative" — with no separate
        # code path needed for that.
        regime_label_debounce_mode = get_regime_label_debounce_mode()
        if regime_label_debounce_mode in ("shadow", "live"):
            try:
                debouncer = self._regime_debouncers.setdefault(
                    symbol, RegimeLabelDebouncer(get_regime_label_debounce_ticks()),
                )
                debounced_label = debouncer.update(regime_label)
                if regime_label_debounce_mode == "shadow":
                    if debounced_label != regime_label:
                        logger.info(
                            "%s: regime debounce shadow — raw=%s, debounced=%s (would differ)",
                            symbol, regime_label, debounced_label,
                        )
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        record_regime_debounce_shadow,
                    )
                    asyncio.create_task(record_regime_debounce_shadow(
                        symbol, raw_label=regime_label, debounced_label=debounced_label,
                    ))
                else:  # live
                    regime_label = debounced_label
            except Exception as exc:
                logger.warning(
                    "%s: regime debounce failed (%s: %s), using raw regime_label",
                    symbol, type(exc).__name__, exc,
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
        # tick (in _sweep_group) — UNLESS MARKOV_SOURCE_MODE=trend_hmm_live,
        # which skips that fit() entirely (see the fit loop above), since
        # nothing reads the legacy model's output in that mode. Here we
        # emit the 3-state transition matrix + current state — a drop-in
        # replacement for the old _build_markov_chain() return shape.
        #
        # p_up / p_dn are kept for backward-compat with the Supabase
        # nt_sweep_result schema (legacy columns). They are read directly
        # from the transition matrix: P(UP|current_state) and P(DOWN|current_state).
        markov_source_mode = get_markov_source_mode()
        _markov_T: dict[str, dict[str, float]] | None = None
        _markov_current_state: str | None = None
        # Tier 1/2 outcome-linked evidence (2026-08-31, worklog
        # 20260831_SHADOW_TO_LIVE_ROADMAP.md) — captured on nt_signal_sim
        # ONLY during the actual shadow-mode comparison below (both
        # legacy and trend_hmm current_state genuinely available and
        # uncorrupted at the same time), so a later live-mode decision
        # can be scored against realized outcomes. None otherwise — in
        # trend_hmm_live mode _markov_current_state itself gets
        # overwritten to the trend_hmm value (legacy fit is skipped), so
        # these dedicated capture vars avoid that ambiguity rather than
        # reusing _markov_current_state directly.
        _sim_legacy_markov_state: str | None = None
        _sim_trend_hmm_markov_state: str | None = None
        if markov_source_mode != "trend_hmm_live":
            _markov_T, _markov_current_state = self._hmm_model.emit_markov(symbol)
        p_up, p_dn = 0.5, 0.5  # neutral defaults; replaced if HMM emits

        # Part 1 Change B (2026-08-25): optionally derive the same matrix
        # shape from RegimeHMM's own (Change-A-stabilized) trend model
        # instead of the separate, numerically-unstable legacy HmmModel
        # above. See MARKOV_SOURCE_MODE's docstring near the top of this
        # file. In shadow mode, an error here only skips the comparison,
        # falling back to whatever the legacy model already produced. In
        # live mode there is no legacy value to fall back to (its fit()
        # was skipped above) — fall back to a neutral uniform matrix
        # instead, the same convention trend_transition_matrix_3state()
        # itself uses internally for an unmapped row.
        if markov_source_mode in ("trend_hmm_shadow", "trend_hmm_live"):
            try:
                _trend_hmm_T, _trend_hmm_current_state = model.trend_transition_matrix_3state(
                    regime.trend_state
                )
                if markov_source_mode == "trend_hmm_shadow":
                    logger.info(
                        "%s: markov shadow — legacy current_state=%s row=%s | "
                        "trend_hmm current_state=%s row=%s",
                        symbol, _markov_current_state, _markov_T,
                        _trend_hmm_current_state, _trend_hmm_T,
                    )
                    # HMM consolidation (2026-08-26): persist the running
                    # comparison so a live-mode decision can be made from
                    # real data instead of grep'ing logs this environment
                    # can't even reach (see MARKOV_SOURCE_MODE docstring).
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        record_markov_shadow_compare,
                    )
                    _legacy_p_up = (_markov_T or {}).get(_markov_current_state or "", {}).get("UP", 0.5)
                    _trend_p_up = _trend_hmm_T.get(_trend_hmm_current_state, {}).get("UP", 0.5)
                    _sim_legacy_markov_state = _markov_current_state
                    _sim_trend_hmm_markov_state = _trend_hmm_current_state
                    asyncio.create_task(record_markov_shadow_compare(
                        symbol,
                        state_agree=(_markov_current_state == _trend_hmm_current_state),
                        p_up_diff=_legacy_p_up - _trend_p_up,
                        legacy_state=_markov_current_state,
                        trend_hmm_state=_trend_hmm_current_state,
                    ))
                elif markov_source_mode == "trend_hmm_live":
                    _markov_T, _markov_current_state = _trend_hmm_T, _trend_hmm_current_state
            except Exception as exc:
                logger.warning(
                    "%s: trend_hmm markov derivation failed (%s: %s), using %s",
                    symbol, type(exc).__name__, exc,
                    "legacy_hmm value" if markov_source_mode == "trend_hmm_shadow"
                    else "neutral uniform-matrix fallback",
                )
                if markov_source_mode == "trend_hmm_live" and _markov_T is None:
                    _markov_T = {
                        s: {t: 1.0 / 3.0 for t in ("UP", "DOWN", "FLAT")}
                        for s in ("UP", "DOWN", "FLAT")
                    }
                    _markov_current_state = "FLAT"

        # ── 3a. TA indicator snapshot (migration 036) ──────────────────
        # Pull the latest 5 TA values (ADX, AO, BB Power, MACD, RSI) from
        # the HMM feature buffer for persistence to nt_sweep_result +
        # nt_regime_log. Best-effort: returns {None × 5} if the buffer is
        # empty (e.g. cold start before the first /api/ta poll landed).
        # See regime_platform/services/hmm/ta_extract.py.
        _ta_snapshot = latest_ta(self._hmm_buffer, symbol)

        # ── 4. Direction & trade signal ──
        current_price = float(np_prices[-1])
        # This is the FINAL direction — reads whatever regime_label is
        # bound to at this point (raw today, potentially debounced once
        # regime_label_debounce_mode=live — see the "2c" block above's own
        # comment), unlike _kelly_direction above which deliberately reads
        # the raw label. Deliberately recomputed here rather than reusing
        # _kelly_direction (2026-09-04, worklog
        # 20260904_KELLY_DIRECTION_AWARE_SIZING.md).
        direction = "short" if "bear" in regime_label.lower() else "long"

        # ATR(14) for the bracket-reachability diagnostic (migration 120,
        # worklog 20260825_CALIBRATION_REFRAME_SCOPE.md). Reuses np_prices
        # already in memory for the regime fit — no new fetch. Same
        # definition as the heavy sweep's own ATR (compute_atr).
        atr = compute_atr(np_prices.tolist(), period=14)

        # Kairos v3 P0.5: feed the run tracker on EVERY tick (not only when
        # a signal is captured) so run start/ATR-at-start are exact. Pure
        # bookkeeping — no effect on this tick's decisions.
        if source == "sweep-light":
            try:
                self._kairos_run_state[symbol] = self._kairos_runs.update(
                    symbol, regime_label, time.time(), atr,
                )
            except Exception as _k_exc:
                logger.debug("[kairos] run tracker %s: %s", symbol, _k_exc)

        brick_size, sl_bricks, tp_bricks = await self._load_brick_params(symbol)

        # ── T1/T2 intraday ATR drift scale (2026-09-19) ─────────────
        # Uses nt_brick_params_state.atr_at_write vs live ATR; scales
        # brick_size only (not brick counts). shadow = log; live = apply.
        from regime_platform.core.feature_flags import (
            get_geometry_intraday_scale_mode,
            get_geometry_tuning_min_consecutive,
            get_geometry_tuning_sweep_mode,
            get_dynamic_atr_risk_cap,
            get_light_publish_geometry_gate,
            get_min_p_win_bracket,
        )
        from regime_platform.sweep_orchestrator.geometry_risk import (
            brick_vol_scale,
            fetch_brick_state,
            write_eod_action,
        )
        _geo_scale_mode = get_geometry_intraday_scale_mode()
        _atr_at_write = None
        if _geo_scale_mode != "off":
            try:
                _bstate = await fetch_brick_state(symbol)
                _atr_at_write = float(_bstate.get("atr_at_write")) if _bstate and _bstate.get("atr_at_write") else None
                _vscale = brick_vol_scale(atr, _atr_at_write)
                if _vscale != 1.0:
                    _scaled_brick = brick_size * _vscale
                    logger.info(
                        "[geo:intraday] %s mode=%s atr_now=%.6f atr_write=%s "
                        "scale=%.4f brick %.4f→%.4f",
                        symbol, _geo_scale_mode, atr or 0.0, _atr_at_write,
                        _vscale, brick_size,
                        _scaled_brick if _geo_scale_mode == "live" else brick_size,
                    )
                    if _geo_scale_mode == "live":
                        brick_size = float(_scaled_brick)
            except Exception as exc:
                logger.warning("[geo:intraday] %s failed: %s", symbol, exc)

        # Extract p_up / p_dn from the HMM's 3x3 transition matrix
        #
        # The "markov diag" log line that used to live here (2026-08-22,
        # worklog 20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2 Finding
        # B) was explicitly temporary by its own comment — added to
        # disambiguate current_state flapping vs. transition-matrix
        # noise before the Tier 1/2 root-cause fix existed, to be removed
        # once that mechanism was confirmed. It has been (this session's
        # Tier 1/2 build + real-data verification); removed as log
        # sanitization (2026-09-01, worklog/
        # 20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md) — it fired
        # on every tick for every symbol and was pure log volume by then.
        if _markov_T is not None and _markov_current_state:
            try:
                state_row = _markov_T.get(_markov_current_state, {})
                p_up = float(state_row.get("UP", 0.5))
                p_dn = float(state_row.get("DOWN", 0.5))
            except Exception:
                pass

        trade_signal = _compute_trade_signal(
            regime_label, current_price, brick_size, sl_bricks, tp_bricks,
        )

        # ── R2: p_win_bracket lookup BEFORE EV (2026-09-22) ─────────────
        # p_win_in_ev_mode=live needs p_win_bracket at EV time so the
        # published probability matches the settlement event (R2c). Gate
        # reject/shadow still evaluated later (needs `qualified`).
        from regime_platform.core.feature_flags import (
            get_min_p_win_bracket,
            get_p_win_bracket_mode,
            get_p_win_in_ev_mode,
        )
        p_bracket_mode = get_p_win_bracket_mode()
        p_win_in_ev_mode = get_p_win_in_ev_mode()
        _p_win_bracket = _p_resolve = _p_win_resolve = None
        _bracket_bucket = _bracket_vol_tier = None
        _bracket_gate_shadow: bool | None = None
        _bracket_applied_to_ev = False
        if p_bracket_mode != "off" or p_win_in_ev_mode != "off":
            from regime_platform.sweep_orchestrator.p_win_bracket import (
                lookup_bracket_probs,
            )
            _bp = await lookup_bracket_probs(
                symbol,
                entry_price=trade_signal.get("entry_price"),
                take_profit=trade_signal.get("take_profit"),
                atr=atr,
                regime_label=regime_label,
            )
            _p_win_bracket = _bp.p_win_bracket
            _p_resolve = _bp.p_resolve
            _p_win_resolve = _bp.p_win_resolve
            _bracket_bucket = _bp.bucket
            _bracket_vol_tier = _bp.vol_tier

        # ── 5. EV Engine (v6 — 3-source logit pool, live) ──
        # p_historical (R3, 2026-08-23) was retired as a live blend input
        # 2026-09-09 (worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md):
        # it and calibration_enforcement.apply_calibration_adjustment() were
        # two independently-computed mechanisms both trying to answer "is
        # this symbol's confidence trustworthy," stacked on the same
        # calibration-bias evidence. Consolidated to apply_calibration_adjustment
        # (the hard, post-hoc mechanism, applied after compute_ev() below)
        # as the sole calibration-correction path. compute_ev() still
        # accepts an optional p_historical kwarg (used directly by the
        # ad-hoc renko/router.py /ev/compute endpoint) — simply not fed by
        # this production call site anymore; the blend renormalises over
        # the remaining 3 sources exactly as it already does for any other
        # missing source.
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

        # Tier 2 HMM fix (2026-08-27, worklog
        # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md): same magnitude/
        # sign gate as Tier 1 (trend_transition_matrix_3state), applied
        # to trend_probs itself — which feeds p_regime directly into the
        # live EV blend below. Gated AFTER smoothing, since that's the
        # final distribution about to be used, not the raw per-tick
        # posterior. shadow mode computes both and persists the
        # comparison but does NOT change what compute_ev sees; only
        # live mode substitutes the gated version.
        trend_probs_gate_mode = get_trend_probs_gate_mode()
        # Tier 1/2 outcome-linked evidence (2026-08-31, worklog
        # 20260831_SHADOW_TO_LIVE_ROADMAP.md) — only populated in the
        # "shadow" branch below (both raw and gated genuinely computed
        # side by side); None in "live"/"off" modes, same nullable
        # capture-only-when-computed pattern as atr/macro_label.
        _sim_raw_p_regime: float | None = None
        _sim_gated_p_regime: float | None = None
        if trend_probs_gate_mode in ("shadow", "live"):
            try:
                _gated_trend_probs = model.gated_trend_probs(_smoothed_trend_probs)
                if trend_probs_gate_mode == "shadow":
                    from regime_platform.services.statistics.ev_engine import EVEngine
                    _raw_p_regime = EVEngine.compute_p_regime(_smoothed_trend_probs, direction)
                    _gated_p_regime = EVEngine.compute_p_regime(_gated_trend_probs, direction)
                    logger.info(
                        "%s: trend_probs gate shadow — raw p_regime=%.4f, "
                        "gated p_regime=%.4f (diff=%+.4f)",
                        symbol, _raw_p_regime, _gated_p_regime,
                        _gated_p_regime - _raw_p_regime,
                    )
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        record_trend_probs_gate_shadow,
                    )
                    _sim_raw_p_regime = _raw_p_regime
                    _sim_gated_p_regime = _gated_p_regime
                    asyncio.create_task(record_trend_probs_gate_shadow(
                        symbol, raw_p_regime=_raw_p_regime, gated_p_regime=_gated_p_regime,
                    ))
                elif trend_probs_gate_mode == "live":
                    _smoothed_trend_probs = _gated_trend_probs
            except Exception as exc:
                logger.warning(
                    "%s: trend_probs gate failed (%s: %s), using ungated trend_probs",
                    symbol, type(exc).__name__, exc,
                )

        # R4: apply the non-convergence discount (empty dict = no-op, exact
        # pre-R4 behaviour) around the compute_ev() call itself, not a
        # separately-tracked parallel state.
        with self._ev_engine.temp_weights(_regime_weight_override):
            ev_result = self._ev_engine.compute_ev(
                trend_probs=_smoothed_trend_probs,
                direction=direction,
                transition_matrix=_markov_T,
                current_state=_markov_current_state,
                p_timesfm=p_timesfm,
                brick_size=brick_size,
                sl_bricks=sl_bricks,
                tp_bricks=tp_bricks,
                n_steps=resolve_markov_n_steps(tp_bricks),
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
                base_weight = P_WIN_WEIGHTS_FULL.get("p_timesfm", 0.25)
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
                            transition_matrix=_markov_T,
                            current_state=_markov_current_state,
                            p_timesfm=p_timesfm,
                            brick_size=brick_size,
                            sl_bricks=sl_bricks,
                            tp_bricks=tp_bricks,
                            n_steps=resolve_markov_n_steps(tp_bricks),
                            # Reuse the SAME smoothed values computed above —
                            # not fresh calls — so both compute_ev() calls in
                            # this tick agree on P_markov_hold_N (fix #3a) and
                            # P_regime (R1 fix #3).
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

        # ── R2c/R2e: EV + Kelly use p_win_bracket when p_win_in_ev_mode=live ──
        # Directional p_win is renamed p_dir conceptually; EV/Kelly now price
        # the settlement event. p_win_ok (below) still scores p_win_raw
        # (directional) — different scale from min_p_win_bracket. Unscored
        # bracket fails open (keeps directional EV).
        if p_win_in_ev_mode == "live" and p_bracket_mode != "off":
            from regime_platform.sweep_orchestrator.p_win_bracket import (
                apply_bracket_to_ev,
            )
            _bracket_applied_to_ev = apply_bracket_to_ev(
                ev_result,
                p_win_bracket=_p_win_bracket,
                p_resolve=_p_resolve,
                engine=self._ev_engine,
            )
            if _bracket_applied_to_ev:
                effective_kelly = kelly_f * ev_result.ev_scale
                logger.info(
                    "[sweep:orch] %s: R2 EV/Kelly on p_win_bracket=%.4f "
                    "(p_dir_raw=%.4f, p_resolve=%s, ev=%.4f, kelly=%.6f)",
                    symbol, ev_result.p_win, p_win_raw, _p_resolve,
                    ev_result.ev, effective_kelly,
                )
        elif p_win_in_ev_mode == "shadow" and _p_win_bracket is not None:
            from regime_platform.sweep_orchestrator.p_win_bracket import (
                expire_prob,
            )
            _shadow = self._ev_engine.recompute_ev_fields(
                float(_p_win_bracket), ev_result.profit, ev_result.stake,
                p_expire=expire_prob(_p_resolve),
            )
            logger.info(
                "[sweep:orch] %s: R2 SHADOW would set EV from p_win_bracket="
                "%.4f ev=%.4f (live dir ev=%.4f p_dir=%.4f)",
                symbol, _p_win_bracket or 0.0, _shadow["ev"],
                ev_result.ev, ev_result.p_win,
            )
        calib_adj = None
        if signal_is_qualifiable and settings.EOD_CALIBRATION_ENFORCE:
            from regime_platform.sweep_orchestrator.calibration_enforcement import (
                apply_calibration_adjustment,
            )
            calib_adj = await apply_calibration_adjustment(
                symbol,
                p_win=ev_result.p_win,
                kelly_f=effective_kelly,
                side="buy" if direction == "long" else "sell",
                vol_tier=_bracket_vol_tier,
                distance_atr_bucket=_bracket_bucket,
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

        # ── 5b1. Macro filter (2026-08-28, worklog
        # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md) ──────────────
        # Long-range (multi-month) price-only trend read — completely
        # separate signal from calibration enforcement above (which is
        # outcome-based). Only read when the signal is qualifiable (same
        # guard as calibration enforcement, avoids a Supabase round-trip
        # on neutral ticks). macro_label/macro_strength are captured on
        # nt_signal_sim regardless of mode (feeds the win/loss+PnL
        # evidence diagnostic, macro_edge.py); the Kelly multiplier is
        # only APPLIED when macro_regime_mode=live.
        macro_label: str | None = None
        macro_strength: float | None = None
        if signal_is_qualifiable:
            from regime_platform.core.feature_flags import get_macro_regime_mode
            macro_mode = get_macro_regime_mode()
            if macro_mode != "off":
                from regime_platform.sweep_orchestrator.macro_filter import (
                    get_macro_regime,
                    kelly_macro_multiplier,
                )
                try:
                    macro_row = await get_macro_regime(symbol)
                    if macro_row:
                        macro_label = macro_row.get("macro_label")
                        macro_strength = macro_row.get("macro_strength")
                        if macro_mode == "live" and macro_label is not None:
                            macro_mult = kelly_macro_multiplier(
                                macro_label, macro_strength, trade_signal["signal"],
                            )
                            effective_kelly = effective_kelly * macro_mult
                            logger.info(
                                "[sweep:orch] %s: macro filter LIVE — label=%s "
                                "strength=%.4f mult=%.4f, kelly→%.6f",
                                symbol, macro_label, macro_strength or 0.0,
                                macro_mult, effective_kelly,
                            )
                except Exception as exc:
                    logger.warning(
                        "[sweep:orch] %s: macro filter read failed (%s: %s), skipping",
                        symbol, type(exc).__name__, exc,
                    )

        # ── 5b-2. Trend filter (2026-09-10, worklog
        # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7) ──
        # Medium-term (10-50 day SMA crossover + RSI) trend read — sibling
        # to the macro filter above, separate concern (recent reversal/
        # momentum, not the 200-day structural level), same nullable-
        # capture-regardless-of-mode / apply-only-when-live shape.
        trend_label: str | None = None
        trend_strength: float | None = None
        if signal_is_qualifiable:
            from regime_platform.core.feature_flags import get_trend_regime_mode
            trend_mode = get_trend_regime_mode()
            if trend_mode != "off":
                from regime_platform.sweep_orchestrator.trend_filter import (
                    get_trend_regime,
                    kelly_trend_multiplier,
                )
                try:
                    trend_row = await get_trend_regime(symbol)
                    if trend_row:
                        trend_label = trend_row.get("trend_label")
                        trend_strength = trend_row.get("trend_strength")
                        if trend_mode == "live" and trend_label is not None:
                            trend_mult = kelly_trend_multiplier(
                                trend_label, trend_strength, trade_signal["signal"],
                            )
                            effective_kelly = effective_kelly * trend_mult
                            logger.info(
                                "[sweep:orch] %s: trend filter LIVE — label=%s "
                                "strength=%.4f mult=%.4f, kelly→%.6f",
                                symbol, trend_label, trend_strength or 0.0,
                                trend_mult, effective_kelly,
                            )
                except Exception as exc:
                    logger.warning(
                        "[sweep:orch] %s: trend filter read failed (%s: %s), skipping",
                        symbol, type(exc).__name__, exc,
                    )

        # ── 5b-3. Context-alignment gate — Tier 1, corrected (2026-09-11,
        # worklog 20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md
        # Part 10) ──────────────────────────────────────────────────────
        # An independent 8th gate, computed from the SAME macro_label/
        # trend_label already fetched above — no new Supabase round-trip.
        # Never touches p_win/ev_result (per ev_engine_no_rework); only
        # added to gate_results (and therefore able to affect `qualified`)
        # when the mode is "live". In "shadow", computed and captured on
        # nt_signal_sim for review, but excluded from the qualification
        # decision — same shadow-first discipline as every other mode-
        # gated mechanism this session.
        context_aligned_ok: bool | None = None
        context_alignment_live = False
        if signal_is_qualifiable:
            from regime_platform.core.feature_flags import get_context_alignment_gate_mode
            context_mode = get_context_alignment_gate_mode()
            if context_mode != "off":
                from regime_platform.sweep_orchestrator.context_alignment import (
                    compute_context_alignment,
                )
                context_aligned_ok = compute_context_alignment(
                    trade_signal["signal"], macro_label, trend_label,
                )
                context_alignment_live = context_mode == "live"
                if not context_aligned_ok:
                    logger.info(
                        "[sweep:orch] %s: context-alignment gate %s — "
                        "signal=%s macro=%s trend=%s",
                        symbol,
                        "LIVE REJECT" if context_alignment_live else "would reject (shadow)",
                        trade_signal["signal"], macro_label, trend_label,
                    )

        # ── 5b-4. Dynamic/regime-conditional MIN_RR (2026-09-11, worklog
        # 20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part
        # 11) ────────────────────────────────────────────────────────────
        # Reuses the SAME macro_label/trend_label already fetched above —
        # no new Supabase round-trip. Recomputes ev_result's derived
        # fields via the SAME recompute_ev_fields() pattern the
        # calibration-enforcement block above already uses, with an
        # explicit min_rr override rather than mutating the shared
        # EVEngine instance's own min_rr attribute (would race across
        # concurrently-processed symbols). Never touches p_win or the
        # blend — only the post-blend allow/deny gate.
        if signal_is_qualifiable:
            from regime_platform.core.feature_flags import (
                get_dynamic_min_rr_consolidation_value,
                get_dynamic_min_rr_mode,
            )
            min_rr_mode = get_dynamic_min_rr_mode()
            if min_rr_mode != "off":
                from regime_platform.sweep_orchestrator.dynamic_min_rr import (
                    compute_effective_min_rr,
                )
                effective_min_rr = compute_effective_min_rr(
                    MIN_RR, get_dynamic_min_rr_consolidation_value(),
                    macro_label, trend_label,
                )
                if effective_min_rr != MIN_RR:
                    logger.info(
                        "[sweep:orch] %s: dynamic MIN_RR %s — %.2f→%.2f "
                        "(macro=%s trend=%s)",
                        symbol,
                        "LIVE" if min_rr_mode == "live" else "would apply (shadow)",
                        MIN_RR, effective_min_rr, macro_label, trend_label,
                    )
                    if min_rr_mode == "live":
                        _min_rr_recomputed = self._ev_engine.recompute_ev_fields(
                            ev_result.p_win, ev_result.profit, ev_result.stake,
                            min_rr=effective_min_rr,
                        )
                        ev_result.ev = _min_rr_recomputed["ev"]
                        ev_result.ev_per_dollar = _min_rr_recomputed["ev_per_dollar"]
                        ev_result.rr = _min_rr_recomputed["rr"]
                        ev_result.allowed = _min_rr_recomputed["allowed"]
                        ev_result.aggression = _min_rr_recomputed["aggression"]
                        ev_result.ev_scale = _min_rr_recomputed["ev_scale"]

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
        # When R2 put p_win_bracket on EV, p_win_ok must still score the
        # directional p_win_raw (different scale from min_p_win_bracket).
        _p_win_for_gate = p_win_raw if _bracket_applied_to_ev else ev_result.p_win
        if _p_win_for_gate < P_WIN_HARD_FLOOR:
            p_win_ok = False
            p_win_kelly_shrink = 0.0
        elif _p_win_for_gate < MIN_P_WIN:
            # Soft band — publish but with reduced kelly
            p_win_ok = True  # passes the gate (publish)
            if MIN_P_WIN > P_WIN_HARD_FLOOR:
                p_win_kelly_shrink = (_p_win_for_gate - P_WIN_HARD_FLOOR) / (MIN_P_WIN - P_WIN_HARD_FLOOR)
            else:
                p_win_kelly_shrink = 1.0
        else:
            p_win_ok = True
            p_win_kelly_shrink = 1.0

        p_win_kelly_shrink = max(0.0, min(1.0, p_win_kelly_shrink))
        # Batch D: soft-band honesty — stamp marginal publishes so scorecard
        # can separate "full edge" from "soft band" without changing publish.
        p_win_soft_band = bool(
            p_win_ok
            and P_WIN_HARD_FLOOR <= _p_win_for_gate < MIN_P_WIN
        )

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
        # Only participates in `qualified` (below) when live — see the
        # "5b-3" block above for why shadow mode must not affect it.
        if context_alignment_live:
            gate_results["context_aligned_ok"] = bool(context_aligned_ok)

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
            # Batch D: soft-band stamp (not a gate — informational)
            "p_win_soft_band": p_win_soft_band,
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
        # Kairos G2 (2026-09-24, migration 168): remember the pre-suppression
        # verdict so nt_signal_sim can tell "failed a gate" apart from
        # "passed every gate but was suppressed" (cooldown / dedup / shadow-
        # gate live rejects). Stamp only — never read back into qualification.
        _gates_pass = bool(qualified)
        _failed_gates = sorted(
            k for k, v in gate_results.items()
            if not v and (TIMESFM_GATE_MODE == "hard" or k != "timesfm_available")
        )
        _suppress_reason: str | None = None

        # ── Cooldown check (suppress signal spam) ────────────────────
        # Even if all gates pass, suppress the signal if the same
        # (symbol, direction) was published recently. This prevents the
        # same BUY/SELL from being published every 5 minutes when the
        # regime is stable.
        #
        # 2026-09-08 cooldown-restart fix (worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md, user
        # directive "after a sl/tp hits > the cooldown should restart"):
        # pure wall-clock elapsed time has zero awareness of whether the
        # ORIGINAL signal actually resolved — it could let a brand-new
        # same-direction signal qualify while the original position was
        # still open, tracking toward its own EXPIRY_BARS-hour deadline.
        # Before applying the elapsed-time check, ask
        # `_probe_cooldown_restart` whether the last qualified signal for
        # this (symbol, direction) is still open. If it is, block
        # unconditionally regardless of elapsed time. If it has resolved
        # (DB-recorded outcome, a live TP/SL crossing, or time-based
        # expiry), restart the cooldown clock from the resolution moment
        # instead of the original qualification moment. Best-effort: any
        # probe failure falls back to the original wall-clock-only check.
        cooldown_suppressed = False
        if qualified and SIGNAL_COOLDOWN_MINUTES > 0:
            cooldown_key = (symbol, trade_signal["signal"])
            last_ts = self._last_qualified_signal.get(cooldown_key, 0)

            try:
                restart_probe = await _probe_cooldown_restart(
                    symbol, trade_signal["signal"], current_price,
                )
            except Exception as exc:
                logger.warning(
                    f"[sweep:orch] {symbol}: cooldown-restart probe failed "
                    f"({type(exc).__name__}: {exc}) — falling back to "
                    f"wall-clock-only cooldown"
                )
                restart_probe = CooldownRestartProbe("unknown")

            if restart_probe.status == "open":
                cooldown_suppressed = True
                logger.info(
                    f"[sweep:orch] {symbol}: COOLDOWN SUPPRESSED — "
                    f"{trade_signal['signal']} signal suppressed "
                    f"(prior qualified signal for this direction is still "
                    f"OPEN — neither TP/SL hit nor expired yet)"
                )
                qualified = False
                gate_results["cooldown_ok"] = False
                _suppress_reason = "cooldown_open"
            else:
                if restart_probe.status == "resolved" and restart_probe.resolved_at is not None:
                    # Restart the cooldown clock from the resolution
                    # moment, not the original qualification moment — and
                    # persist it so the elapsed-time check keeps working
                    # unchanged on subsequent ticks.
                    last_ts = restart_probe.resolved_at
                    self._last_qualified_signal[cooldown_key] = last_ts

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
                    _suppress_reason = "cooldown_time"
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
            # Log sanitization (2026-09-01, worklog/
            # 20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md): this
            # fires on nearly every tick for ~13 of 14 symbols (qualified
            # rate is ~1-2%), so the full per-gate dump moved to DEBUG —
            # genuinely useful when actively tuning a specific symbol's
            # qualification path, just not at every-tick volume. The
            # short summary stays at INFO so "is X getting rejected, and
            # by which gates" is still visible without turning on DEBUG.
            logger.info(
                "[sweep:orch] %s: REJECTED — %d gate(s) failed: %s",
                symbol, len(failed_gates), failed_gates,
            )
            logger.debug(
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

        # One arbiter for "is this symbol calibrated" (2026-09-09, worklog
        # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16):
        # classify via eod_correction._classify_bias — the exact,
        # registry-tunable function that actually drives live enforcement
        # — instead of fetch_calibration_bias's own internal, hardcoded
        # 0.05-threshold status, which could and did disagree with the
        # real 0.10-threshold verdict for any |bias| in [0.05, 0.10). Same
        # fix, same reasoning as routers/calibration.py's 2026-08-22 audit
        # fix for the identical duplication.
        calibration_status = None
        if cal is not None:
            from regime_platform.sweep_orchestrator.eod_correction import (
                _classify_bias,
            )
            calibration_status = _classify_bias(cal["bias"])

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
            # v6 (2026-08-23): p_pattern -> p_historical. p_historical was
            # retired as a live blend input 2026-09-09 (this call site no
            # longer feeds compute_ev() a value — see the EV Engine call
            # above), so this field is always None now; left in the
            # payload shape rather than removed since downstream consumers
            # (agent synthesizer) may still read the key.
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
            "calibration_status": calibration_status,
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
                _suppress_reason = "content_dedup"
                logger.info(
                    f"[sweep:orch] {symbol}: DEDUP SUPPRESSED — "
                    f"signal content unchanged (hash={content_hash[:8]}...)"
                )
            else:
                gate_results["content_dedup_ok"] = True
                if content_hash:
                    self._last_signal_hash[dedup_key] = content_hash

            # ── 10a. Bracket evidence gate (R2 + fail-closed) ────────
            # Evaluated BEFORE cooldown stamp / Discord so `qualified` is
            # FINAL for both nt_sweep_result.qualified and nt_signal_sim
            # (G2 gate_outcome). Unscored + fail_closed cannot publish.
            if p_bracket_mode != "off":
                from regime_platform.core.feature_flags import (
                    get_p_win_bracket_unscored_policy as _get_unscored_policy,
                )
                from regime_platform.sweep_orchestrator.p_win_bracket import (
                    BRACKET_REJECT_LOW,
                    BRACKET_REJECT_UNSCORED,
                    bracket_gate_decision,
                )
                _unscored_policy = _get_unscored_policy()
                _bracket_decision = bracket_gate_decision(
                    _p_win_bracket,
                    min_p_win_bracket=get_min_p_win_bracket(),
                    fail_closed=(_unscored_policy == "fail_closed"),
                )
                _bracket_gate_shadow = bool(
                    qualified and _bracket_decision != "pass"
                )
                if _bracket_decision == BRACKET_REJECT_UNSCORED:
                    gate_results["p_win_bracket_ok"] = False
                    if qualified and _unscored_policy == "fail_closed":
                        _suppress_reason = "bracket_unscored"
                        qualified = False
                        logger.info(
                            "[sweep:orch] %s: FAIL-CLOSED unscored p_win_bracket "
                            "(bucket=%s vol=%s n_stored path) — not QUALIFIED "
                            "(policy=fail_closed)",
                            symbol, _bracket_bucket, _bracket_vol_tier,
                        )
                    else:
                        logger.info(
                            "[sweep:orch] %s: bracket unscored (fail-open keep) "
                            "bucket=%s vol=%s qualified=%s",
                            symbol, _bracket_bucket, _bracket_vol_tier, qualified,
                        )
                elif _bracket_decision == BRACKET_REJECT_LOW:
                    gate_results["p_win_bracket_ok"] = False
                    if p_bracket_mode == "live" and qualified:
                        _suppress_reason = "bracket_gate"
                        qualified = False
                        logger.info(
                            "[sweep:orch] %s: REJECTED by p_win_bracket=%.4f "
                            "(min=%.3f, bucket=%s)",
                            symbol, _p_win_bracket or 0.0,
                            get_min_p_win_bracket(), _bracket_bucket,
                        )
                    elif p_bracket_mode == "shadow" and _bracket_gate_shadow:
                        logger.info(
                            "[sweep:orch] %s: bracket gate SHADOW would "
                            "reject p_win_bracket=%.4f (bucket=%s vol=%s) "
                            "— live still qualified=%s",
                            symbol, _p_win_bracket or 0.0, _bracket_bucket,
                            _bracket_vol_tier, qualified,
                        )
                else:
                    gate_results["p_win_bracket_ok"] = True

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
                # Bracket gate already applied (10a, before Discord) so
                # `qualified` / suppress_reason are final for this capture.
                _p_win_dir = p_win_raw

                # ── Tiered ATR geometry / long-lag risk (Phase C2) ──────
                from regime_platform.sweep_orchestrator.geometry_risk import (
                    compute_geometry_metrics,
                    lookup_long_lag,
                )
                _gm = compute_geometry_metrics(
                    entry_price=trade_signal.get("entry_price"),
                    take_profit=trade_signal.get("take_profit"),
                    stop_loss=trade_signal.get("stop_loss"),
                    atr=atr,
                    regime_label=regime_label,
                    atr_at_write=None,
                )
                if _gm.bucket and atr:
                    _exp, _p90 = await lookup_long_lag(
                        symbol, vol_tier=_gm.vol_tier, bucket=_gm.bucket,
                    )
                    _gm = compute_geometry_metrics(
                        entry_price=trade_signal.get("entry_price"),
                        take_profit=trade_signal.get("take_profit"),
                        stop_loss=trade_signal.get("stop_loss"),
                        atr=atr,
                        regime_label=regime_label,
                        pct_expired=_exp,
                        p90_hours=_p90,
                    )
                if _gm.long_lag_risk:
                    logger.info(
                        "[geometry:risk] %s long_lag_hat=%s risk=%s "
                        "tp_atr=%s bucket=%s",
                        symbol, _gm.long_lag_hat, _gm.geometry_risk_hat,
                        _gm.tp_distance_atr, _gm.bucket,
                    )
                # T3: geometry_tuning_sweep enqueue (streak + cooldown in dispatch)
                try:
                    _tune_mode = get_geometry_tuning_sweep_mode()
                    _risk_cap = get_dynamic_atr_risk_cap()
                    _need_tune = bool(
                        _gm.long_lag_risk
                        or (_gm.geometry_risk_hat is not None and float(_gm.geometry_risk_hat) > _risk_cap)
                    )
                    streak = getattr(self, "_geo_tune_streak", None)
                    if streak is None:
                        streak = {}
                        self._geo_tune_streak = streak
                    if _need_tune:
                        streak[symbol] = streak.get(symbol, 0) + 1
                    else:
                        streak[symbol] = 0
                    _min_streak = get_geometry_tuning_min_consecutive()
                    if streak[symbol] >= _min_streak and _tune_mode != "off":
                        detail = {
                            "reason": "geometry_risk_streak",
                            "streak": streak[symbol],
                            "geometry_risk_hat": _gm.geometry_risk_hat,
                            "long_lag_hat": _gm.long_lag_hat,
                            "tp_distance_atr": _gm.tp_distance_atr,
                            "bucket": _gm.bucket,
                            "mode": _tune_mode,
                        }
                        if _tune_mode == "live":
                            ok = await write_eod_action(symbol, "geometry_tuning_sweep", detail)
                            logger.info(
                                "[geo:tuning] %s ENQUEUE geometry_tuning_sweep streak=%d risk=%s ok=%s",
                                symbol, streak[symbol], _gm.geometry_risk_hat, ok,
                            )
                            streak[symbol] = 0
                        else:
                            logger.info(
                                "[geo:tuning] %s SHADOW would enqueue geometry_tuning_sweep "
                                "streak=%d risk=%s tp_atr=%s",
                                symbol, streak[symbol], _gm.geometry_risk_hat, _gm.tp_distance_atr,
                            )
                except Exception as _tune_exc:
                    logger.warning("[geo:tuning] %s: %s", symbol, _tune_exc)

                # ── B: light-path geometry QUALIFIED gate (2026-09-20) ──
                # Geometry-risk ONLY since 2026-09-24 (B5 split): bracket floor
                # is R2's gate (p_win_bracket_mode above), not B's. See
                # worklog/20260924_B5_VETO_SPLIT_SCOPE.md.
                _qualified_geometry_shadow: bool | None = None
                try:
                    from regime_platform.sweep_orchestrator.geometry_risk import (
                        light_geometry_would_reject,
                    )
                    _light_gate = get_light_publish_geometry_gate()
                    _risk_cap_b = get_dynamic_atr_risk_cap()
                    _would_reject = light_geometry_would_reject(
                        _gm.geometry_risk_hat, risk_cap=_risk_cap_b,
                    )
                    # Stamp only when risk is scored (fail-open rows stay NULL)
                    _risk_scored = _gm.geometry_risk_hat is not None
                    if qualified and _light_gate != "off" and _would_reject:
                        if _light_gate == "live":
                            qualified = False
                            _suppress_reason = "geometry_gate"
                            logger.info(
                                "[light:geometry] %s REJECT qualified — risk=%s cap=%s "
                                "tp_atr=%s",
                                symbol, _gm.geometry_risk_hat, _risk_cap_b,
                                _gm.tp_distance_atr,
                            )
                        else:
                            # B2 stamp (2026-09-23): persist for B5 risk-share SQL
                            _qualified_geometry_shadow = True
                            logger.info(
                                "[light:geometry] %s SHADOW would reject — risk=%s "
                                "cap=%s tp_atr=%s (still qualified=%s)",
                                symbol, _gm.geometry_risk_hat, _risk_cap_b,
                                _gm.tp_distance_atr, qualified,
                            )
                    elif qualified and _light_gate == "shadow" and _risk_scored:
                        _qualified_geometry_shadow = False
                except Exception as _lg_exc:
                    logger.warning("[light:geometry] %s: %s", symbol, _lg_exc)

                # Accurate G2 stamps: if bracket failed on an already-failed
                # gate set, name it in failed_gates (gate_fail rows only).
                if (
                    not _gates_pass
                    and gate_results.get("p_win_bracket_ok") is False
                    and "p_win_bracket_ok" not in _failed_gates
                ):
                    _failed_gates = sorted(set(_failed_gates) | {"p_win_bracket_ok"})

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
                    atr=atr,
                    p_win_dir=_p_win_dir,
                    p_win_bracket=_p_win_bracket,
                    p_resolve=_p_resolve,
                    p_win_resolve=_p_win_resolve,
                    bracket_bucket=_bracket_bucket,
                    bracket_vol_tier=_bracket_vol_tier,
                    bracket_gate_shadow=_bracket_gate_shadow,
                    qualified_geometry_shadow=_qualified_geometry_shadow,
                    tp_distance_atr=_gm.tp_distance_atr,
                    sl_distance_atr=_gm.sl_distance_atr,
                    long_lag_hat=_gm.long_lag_hat,
                    geometry_risk_hat=_gm.geometry_risk_hat,
                    long_lag_risk=_gm.long_lag_risk,
                    macro_label=macro_label,
                    macro_strength=macro_strength,
                    trend_label=trend_label,
                    trend_strength=trend_strength,
                    context_aligned_ok=context_aligned_ok,
                    legacy_markov_state=_sim_legacy_markov_state,
                    trend_hmm_markov_state=_sim_trend_hmm_markov_state,
                    raw_p_regime=_sim_raw_p_regime,
                    gated_p_regime=_sim_gated_p_regime,
                    kairos=self._kairos_stamp(symbol, regime_label, atr, source),
                    gate_outcome=(
                        "qualified" if qualified
                        else "suppressed" if _gates_pass
                        else "gate_fail"
                    ),
                    suppress_reason=(
                        _suppress_reason if (_gates_pass and not qualified) else None
                    ),
                    failed_gates=_failed_gates or None,
                )
                if settings.NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY and not qualified:
                    logger.debug(
                        "[sweep:orch] %s: signal-sim capture skipped (qualified-only mode)",
                        symbol,
                    )
                else:
                    asyncio.create_task(log_signal_sim(sim_payload))
                # ── 11a2. Signal-stream meta-regime shadow (2026-09-09) ──
                # Fire-and-forget: append this tick to the local lookback
                # cache and, once signal_stream_meta_model_mode != off,
                # update nt_signal_stream_meta_shadow. Runs on EVERY
                # non-neutral signal (qualified or not), same population
                # as the signal-sim capture above — the meta-model needs
                # the raw stream, not just what happened to qualify. See
                # worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
                # §11 ("what happened to all the shadow mode tracking?" —
                # this call is the piece that was missing).
                from regime_platform.sweep_orchestrator.meta_regime_model import (
                    record_tick_and_update_shadow,
                )
                asyncio.create_task(record_tick_and_update_shadow(
                    symbol=symbol,
                    ts=time.time(),
                    direction=trade_signal["signal"],
                    regime_label=regime_label,
                    regime_conf=regime_conf,
                    p_win=ev_result.p_win,
                    kelly_f=kelly_f,
                ))
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
                #
                # 2026-09-09 real-timestamp fix (worklog
                # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
                # §7, "Finding 1"): `timestamps` (threaded through from
                # `_process_symbol`'s own param, sourced from
                # price_fetcher.fetch_tradingview_batch(include_timestamps=
                # True)) carries the REAL per-bar epoch-seconds for
                # `prices`. Only trust it when it's a complete, same-length,
                # all-non-None array — anything else (None entirely, a
                # partial array, a length mismatch from a fallback fetch
                # path that never populated it) is NOT a valid per-bar time
                # axis, so build_from_prices falls back to its own
                # auto-incrementing index and log_renko_bricks fabricates
                # hourly spacing from sweep_ts instead (see its docstring
                # for exactly when that fallback triggers — it is no longer
                # the silent default).
                try:
                    from renko.brick_engine import RenkoBrickEngine
                    real_timestamps = (
                        timestamps
                        if timestamps is not None
                        and len(timestamps) == len(prices)
                        and all(t is not None for t in timestamps)
                        else None
                    )
                    engine = RenkoBrickEngine(brick_size=brick_size)
                    bricks = engine.build_from_prices(prices, timestamps=real_timestamps)
                    if bricks:
                        asyncio.create_task(log_renko_bricks(
                            symbol=symbol,
                            bricks=[b.to_dict() for b in bricks],
                            brick_size=brick_size,
                            sweep_ts=time.time(),
                            has_real_timestamps=real_timestamps is not None,
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
                    batch, batch_ts = await fetch_candles_for_symbols(
                        [sym], 200, include_timestamps=True,
                    )
                    prices = batch.get(symbol, [])
                    stale_timestamps = batch_ts.get(symbol)
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
                    await self._process_symbol(sym, prices, timestamps=stale_timestamps)
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
        the heavy sweep (daily, unconditional — no TimesFM confidence
        gate as of 2026-09-09) rewrites it with ATR-scaled optimised
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
