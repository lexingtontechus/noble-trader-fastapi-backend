"""
Supabase REST Client for the Sweep Orchestrator
═════════════════════════════════════════════════

Thin wrapper around httpx for reading/writing Supabase PostgREST tables.
Follows the same pattern as existing backend modules (calibration.py, venue.py).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.sweep.supabase")


# ─── Internal helpers ─────────────────────────────────────────────

def _headers() -> dict[str, str]:
    key = settings.SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _url(table: str) -> str:
    return f"{settings.SUPABASE_URL}/rest/v1/{table}"


# ─── Public API ───────────────────────────────────────────────────


async def fetch_active_symbols() -> list[dict[str, Any]]:
    """Return all rows from ``nt_symbol`` where ``active = true``."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        logger.warning("Supabase not configured — cannot fetch symbols")
        return []

    url = _url("nt_symbol")
    # Phase B: data_source / yahoo_ticker / hyperliquid_coin / alpaca_symbol
    # removed from select — all symbols now route through TDVA via
    # tradingview_symbol. The columns remain in the schema for backward
    # compatibility but are no longer read by the orchestrator.
    #
    # sweep_cron added — heavy sweep (Phase B) groups symbols by this column,
    # mirroring how the light sweep groups by regime_cron. Default is
    # '0 20 * * sun' (Sunday 20:00 UTC, migration 026) when NULL.
    # NOTE: APScheduler uses PYTHON weekday convention (0=Monday); use the
    # 'sun' name (or '6') for Sunday, NOT '0'.
    # plan_ids (UUID[]) added in migration 032 — needed so downstream consumers
    # (sweep orchestrator, future Railway Redis fan-out) can route signals per
    # subscription plan (Signal Scout / Precision Pro).
    params = {
        "select": "symbol,asset_class,exchange,regime_cron,sweep_cron,heartbeat_ttl_seconds,"
                  "tradingview_symbol,plan_ids",
        "active": "eq.true",
        "order": "asset_class.asc,symbol.asc",
    }
    print(f"[sb] Querying {url} with params={params}", flush=True)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()
            logger.info("Fetched %d active symbols from nt_symbol", len(rows))
            print(f"[sb] Got {len(rows)} symbols: {[r.get('symbol') for r in rows]}", flush=True)
            return rows
    except Exception as exc:
        logger.error("Failed to fetch active symbols: %s", exc)
        print(f"[sb] FAILED: {exc}", flush=True)
        return []


async def log_regime_entry(
    symbol: str,
    source: str,
    regime: str,
    regime_conf: float,
    markov_p_up: float,
    markov_p_dn: float,
    kelly_f: float,
    prev_regime: str | None = None,
    regime_shift: bool = False,
    venue: str | None = None,
    # ── TA indicators (migration 036) ───────────────────────────────
    # The 5 TA values from the HMM's feature buffer at the sweep tick.
    # All optional — best-effort, may be None if the TA fetch failed
    # or the buffer was empty (e.g. on cold start before the first
    # /api/ta poll landed).
    ta_adx: float | None = None,
    ta_ao: float | None = None,
    ta_bb_power: float | None = None,
    ta_macd: float | None = None,
    ta_rsi: float | None = None,
) -> None:
    """Insert a row into ``nt_regime_log``.

    The 5 ta_* kwargs (migration 036) persist the HMM's latest TA
    observation alongside the regime transition, so regime analytics
    can be run join-free against nt_regime_log alone.
    """
    if not settings.SUPABASE_URL:
        return

    payload = {
        "symbol": symbol,
        "source": source,
        "regime": regime,
        "regime_conf": round(regime_conf, 4),
        "markov_p_up": round(markov_p_up, 4),
        "markov_p_dn": round(markov_p_dn, 4),
        "kelly_f": round(kelly_f, 6),
        "prev_regime": prev_regime,
        "regime_shift": regime_shift,
        "ta_adx": ta_adx,
        "ta_ao": ta_ao,
        "ta_bb_power": ta_bb_power,
        "ta_macd": ta_macd,
        "ta_rsi": ta_rsi,
    }
    if venue:
        payload["venue"] = venue
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_regime_log"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=payload,
            )
            if not resp.is_success:
                logger.warning(
                    "log_regime_entry %s: HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.warning("log_regime_entry %s failed: %s", symbol, exc)


# ─── Schema whitelists ───────────────────────────────────────────
# PostgREST will HTTP 400 (PGRST204) if the payload contains a key that
# doesn't map to a real column. Whitelist the known columns per table so
# callers can pass in a fat dict and we filter to the right shape per table.
#
# Sources: supabase/functions/sweep/index.deployed.ts (the TS edge function
# is the source of truth — it writes sweep results to nt_sweep_result and
# regime shifts to nt_regime_log as two separate calls).

NT_REGIME_LOG_COLUMNS: frozenset[str] = frozenset({
    "symbol", "source", "regime", "regime_conf",
    "markov_p_up", "markov_p_dn", "kelly_f",
    "prev_regime", "regime_shift",
    "venue",  # exchange name (e.g., Coinbase, OANDA, Hyperliquid)
    # TA indicators (migration 036) — mirror of nt_sweep_result.ta_*
    # so regime analytics can run join-free.
    "ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi",
})

NT_SWEEP_RESULT_COLUMNS: frozenset[str] = frozenset({
    # Original fields
    "symbol", "asset_class", "brick_size", "sl_bricks", "tp_bricks",
    "sharpe", "total_return", "annual_return", "max_drawdown_pct",
    "win_rate", "n_trades", "profit_factor",
    "regime", "regime_conf", "kelly_f",
    "markov_p_up", "markov_p_dn",
    "sweep_window", "sweep_duration_ms", "n_combos_tested",
    "source",
    # New fields (migration 029) — enriched sweep result
    "effective_kelly",        # Kelly after EV scaling + markov multiplier
    "signal",                 # buy / sell / neutral
    "ev",                     # expected value
    "p_win",                  # blended probability of winning
    "qualified",              # whether signal passed all gates
    "p_timesfm",              # TimesFM probability (null if unavailable)
    "entry_price",            # trade entry price
    "stop_loss",              # stop loss price
    "take_profit",            # take profit price
    "aggression",             # passive / mid / aggressive
    "venue",                  # exchange name (e.g., Coinbase, OANDA, Hyperliquid)
    # Additional fields (migration 030) — regime context + sizing
    "regime_shift",           # whether regime changed from previous sweep
    "prev_regime",            # previous regime label (empty if first sweep)
    "size_mult",              # TimesFM confidence-based size multiplier (0.6/1.0/1.2)
    # TA indicators (migration 036) — HMM's latest TA observation per sweep.
    # All nullable; populated from FeatureBuffer.latest() via the
    # regime_platform.services.hmm.latest_ta helper.
    "ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi",
})

NT_SIGNAL_SIM_COLUMNS: frozenset[str] = frozenset({
    # Fields populated at signal-capture time (light/heavy sweep).
    "signal_id",              # PK — normally DB-generated (uuid); capture omits it
    "symbol", "venue", "ts", "signal",
    "entry_price", "stop_loss", "take_profit",
    "brick_size", "sl_bricks", "tp_bricks",
    "p_win", "ev", "regime", "regime_conf",
    "source", "qualified",
    # Part II paper-portfolio sizing inputs (migration 105). Nullable;
    # filled at capture when kelly is available.
    "kelly_f", "effective_kelly",
    # Fields filled later by the EOD signal resolver. They stay NULL at
    # capture time but are part of the schema so a resolver insert is
    # a plain filtered insert into the same table.
    "outcome", "exit_price", "exit_ts", "hold_bars",
    "mfe", "mae", "pnl_bricks", "pnl_dollars", "resolved_at",
})


def _filter_payload(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Return a copy of ``payload`` containing only keys in ``allowed``.

    Logs any dropped fields at DEBUG level so unexpected schema drift is
    visible without being noisy.
    """
    clean = {k: v for k, v in payload.items() if k in allowed}
    dropped = set(payload) - set(clean)
    if dropped:
        logger.debug(
            "Filtered payload — dropped %d non-schema field(s): %s",
            len(dropped), sorted(dropped),
        )
    return clean


async def _insert_row(table: str, payload: dict[str, Any]) -> bool:
    """Insert one row into ``table``. Returns True on success."""
    if not settings.SUPABASE_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url(table),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=payload,
            )
            if not resp.is_success:
                logger.error(
                    "log_sweep_result: %s insert HTTP %d — %s",
                    table, resp.status_code, resp.text[:300],
                )
                return False
            logger.info(
                "log_sweep_result: %s insert OK for %s",
                table, payload.get("symbol", "unknown"),
            )
            return True
    except Exception as exc:
        logger.error("log_sweep_result: %s insert failed: %s", table, exc)
        return False


async def log_sweep_result(payload: dict[str, Any]) -> None:
    """Persist sweep KPIs to ``nt_sweep_result``.

    IMPORTANT: This function writes ONLY to ``nt_sweep_result`` (the sweep
    KPI table — sharpe, annual_return, win_rate, n_trades, sweep_window,
    sweep_duration_ms, etc.). It does NOT write to ``nt_regime_log``.

    The regime entry (regime, regime_conf, kelly_f, prev_regime,
    regime_shift) is written separately by ``log_regime_entry()``, which
    is called from inside ``_process_symbol()`` at the point where the
    prev_regime / regime_shift values are actually known. Writing regime
    fields from here would duplicate those rows.

    Previous behaviour (caused duplicate nt_regime_log rows):
      This function split its payload across BOTH nt_sweep_result AND
      nt_regime_log. But log_regime_entry() was ALSO being called from
      _process_symbol(), so nt_regime_log got TWO rows per sweep —
      one from here (with prev_regime=null, because the caller doesn't
      have that info) and one from log_regime_entry() (with the real
      prev_regime). The ~250ms gap between them is the time it takes
      for _process_symbol() to finish and call log_regime_entry().
    """
    if not settings.SUPABASE_URL:
        return

    sweep_payload = _filter_payload(payload, NT_SWEEP_RESULT_COLUMNS)

    # Best-effort insert. Failures are logged but don't propagate.
    await _insert_row("nt_sweep_result", sweep_payload)


async def log_signal_sim(payload: dict[str, Any]) -> None:
    """Persist one generated signal to ``nt_signal_sim`` (best-effort).

    Captures EVERY generated non-neutral signal (qualified or not) at
    sweep time. Outcome/exit/PNL fields (outcome, exit_price, exit_ts,
    hold_bars, mfe, mae, pnl_bricks, pnl_dollars, resolved_at) are left
    NULL here — the EOD signal resolver fills them later against the
    same table via a plain filtered insert (they are included in
    ``NT_SIGNAL_SIM_COLUMNS`` so the resolver needs no schema changes).

    Never raises: a missing Supabase config or a failed insert is
    logged and swallowed, exactly like ``log_sweep_result``.
    """
    if not settings.SUPABASE_URL:
        return

    sim_payload = _filter_payload(payload, NT_SIGNAL_SIM_COLUMNS)
    # nt_signal_sim.signal_id is UUID PRIMARY KEY with no DB default —
    # the capture path must supply it. Generated here (not in the payload
    # builder) so every call site gets a fresh id.
    sim_payload["signal_id"] = str(uuid.uuid4())

    # Best-effort insert. Failures are logged but don't propagate.
    await _insert_row("nt_signal_sim", sim_payload)


async def log_error(symbol: str, fn_name: str, error_msg: str) -> None:
    """Insert a row into ``nt_errors``."""
    if not settings.SUPABASE_URL:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_errors"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json={
                    "symbol": symbol,
                    "fn_name": fn_name,
                    "error_msg": error_msg[:1000],
                },
            )
            if not resp.is_success:
                logger.warning("log_error %s: HTTP %d", symbol, resp.status_code)
    except Exception as exc:
        logger.warning("log_error %s failed: %s", symbol, exc)


async def fetch_calibration_bias(
    symbol: str,
    venue: str | None = None,
) -> dict[str, float | str] | None:
    """Query nt_calibration_records and return calibration bias.

    Returns ``{bias: float, status: str, n: int, win_rate: float,
    avg_pred: float}`` or ``None`` on failure (or when fewer than 20
    records exist — ``n`` is too small for the bias to be meaningful).
    If ``venue`` is provided, filters by both symbol AND venue (exchange).
    This ensures calibration is tracked per exchange, not mixed across
    exchanges for the same symbol.
    """
    if not settings.SUPABASE_URL:
        return None

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,outcome",
        "symbol": f"eq.{symbol}",
    }
    if venue:
        params["venue"] = f"eq.{venue}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_calibration_bias %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) < 20:
        return None

    n = len(data)
    sum_pred = 0.0
    wins = 0
    for row in data:
        sum_pred += row.get("predicted_p_win", 0.5) or 0.5
        if row.get("outcome") == "win":
            wins += 1

    avg_pred = sum_pred / n
    win_rate = wins / n
    bias = round(avg_pred - win_rate, 4)

    if abs(bias) < 0.05:
        status = "CALIBRATED"
    elif bias >= 0.05:
        status = "OVERCONFIDENT"
    else:
        status = "UNDERCONFIDENT"

    return {
        "bias": bias,
        "status": status,
        "n": n,
        "win_rate": round(win_rate, 4),
        "avg_pred": round(avg_pred, 4),
    }
