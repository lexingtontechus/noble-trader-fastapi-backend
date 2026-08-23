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
from datetime import datetime, timedelta, timezone
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
    # L4: debug-level logging (was print() — stdout noise every 5-min
    # symbol-refresh tick; print bypasses log levels and pollutes logs).
    logger.debug("Querying %s with params=%s", url, params)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()
            logger.info("Fetched %d active symbols from nt_symbol", len(rows))
            logger.debug("Symbols: %s", [r.get("symbol") for r in rows])
            return rows
    except Exception as exc:
        logger.error("Failed to fetch active symbols: %s", exc)
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
    # Failure trace (2026-08-09): nullable error reason so a failed or
    # no-combo heavy sweep still leaves a row (observability, not silent
    # absence). Written only when the per-symbol sweep did not complete.
    "error",
})

NT_SIGNAL_SIM_COLUMNS: frozenset[str] = frozenset({
    # Fields populated at signal-capture time (light/heavy sweep).
    "signal_id",              # PK — normally DB-generated (uuid); capture omits it
    "symbol", "venue", "ts", "signal",
    "entry_price", "stop_loss", "take_profit",
    "brick_size", "sl_bricks", "tp_bricks",
    "p_win", "ev", "regime", "regime_conf",
    # predicted_p_win_raw (2026-08-13): pre-enforcement p_win, persisted only
    # when NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW is enabled (column added by
    # supabase/migrations/114_add_predicted_p_win_raw.sql). Absent from
    # the payload when disabled, so the default path is byte-identical.
    "predicted_p_win_raw",
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
                    "%s insert HTTP %d — %s",
                    table, resp.status_code, resp.text[:300],
                )
                return False
            logger.info(
                "%s insert OK for %s",
                table, payload.get("symbol", "unknown"),
            )
            return True
    except Exception as exc:
        logger.error("%s insert failed: %s", table, exc)
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


NT_RENKO_BRICKS_COLUMNS: frozenset[str] = frozenset({
    "symbol", "direction", "brick_size", "open_price", "close_price",
    "high", "low", "label", "brick_index", "ts",
})


async def log_renko_bricks(
    symbol: str,
    bricks: list[dict[str, Any]],
    brick_size: float | None = None,
    session_date: str | None = None,
    sweep_ts: float | None = None,
) -> None:
    """Persist one brick per row to ``nt_renko_bricks`` (best-effort).

    Matches the real table schema: brick_id serial PK, unique
    (symbol, brick_index, session_date). Uses ``Prefer:
    resolution=merge-duplicates`` + ``on_conflict=symbol,brick_index,
    session_date`` so a re-sweep of the same symbol+brick_index replaces
    the prior brick instead of duplicating rows (the table's PK is the
    serial brick_id, so without the on_conflict hint PostgREST resolves on
    brick_id → always INSERTs → unbounded growth).

    ``session_date`` defaults to CURRENT_DATE (DB default) when omitted.
    When ``sweep_ts`` is provided, brick timestamps are synthesised as
    hourly epochs backward from it (the sweep passes only close prices, no
    per-brick timestamps; this keeps the chart's time axis sane).

    Never raises: failures are logged and swallowed.
    """
    if not settings.SUPABASE_URL:
        return

    rows = []
    n = len(bricks)
    for i, b in enumerate(bricks):
        ts_epoch = float(b.get("timestamp") or 0)
        if sweep_ts is not None:
            # The sweep passes only close prices, so build_from_prices
            # auto-increments timestamps from 0 (→ 1970). Synthesise hourly
            # spacing backward from the sweep time instead.
            ts_epoch = sweep_ts - (n - 1 - i) * 3600.0
        row = {
            "symbol": symbol,
            "direction": b.get("direction"),
            "brick_size": float(b.get("brick_size", brick_size or 0)),
            "open_price": float(b.get("open_price", 0)),
            "close_price": float(b.get("close_price", 0)),
            "high": float(b.get("high", b.get("close_price", 0))),
            "low": float(b.get("low", b.get("open_price", 0))),
            "brick_index": b.get("index"),
            "ts": datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat(),
        }
        if session_date:
            row["session_date"] = session_date
        rows.append(row)

    if not rows:
        logger.debug("log_renko_bricks: no bricks for %s — skipping", symbol)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_renko_bricks')}?on_conflict=symbol,brick_index,session_date",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=rows,
            )
            if not resp.is_success:
                logger.error(
                    "log_renko_bricks: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("log_renko_bricks: insert failed: %s", exc)


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
    *,
    window_days: int | None = None,
    min_records: int | None = None,
) -> dict[str, float | str] | None:
    """Query nt_calibration_records and return calibration bias.

    Returns ``{bias: float, status: str, n: int, win_rate: float,
    avg_pred: float}`` or ``None`` on failure (or when fewer than
    ``min_records`` records exist in the window — ``n`` is too small for
    the bias to be meaningful). If ``venue`` is provided, filters by both
    symbol AND venue (exchange).

    ROLLING WINDOW (2026-08-13, intraday cadence plan): the bias is computed
    over records with ``entered_at`` within the last ``window_days`` days
    (default ``settings.EOD_BIAS_WINDOW_DAYS`` = 7) instead of all-time, so a
    regime shift moves the bias in hours, not days. Pass ``window_days=0``
    for the legacy all-time behaviour. ``min_records`` defaults to
    ``settings.EOD_BIAS_MIN_RECORDS`` (20).
    """
    if not settings.SUPABASE_URL:
        return None

    win_days = window_days if window_days is not None else getattr(settings, "EOD_BIAS_WINDOW_DAYS", 7)
    min_n = min_records if min_records is not None else getattr(settings, "EOD_BIAS_MIN_RECORDS", 20)

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,outcome",
        "symbol": f"eq.{symbol}",
    }
    if venue:
        params["venue"] = f"eq.{venue}"
    if win_days and win_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=win_days)).isoformat()
        params["entered_at"] = f"gte.{cutoff}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_calibration_bias %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) < min_n:
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


async def fetch_calibration_bias_raw(
    symbol: str,
    venue: str | None = None,
    *,
    window_days: int | None = None,
    min_records: int | None = None,
) -> dict[str, float | str | int] | None:
    """Query nt_calibration_records and return calibration bias computed from
    ``predicted_p_win_raw`` (the PRE-enforcement p_win) rather than the
    enforced ``predicted_p_win``.

    This is the raw-model-bias variant used by the 4h intraday correction
    pass (``run_eod_correction(raw_bias_mode=True)``). It separates model
    miscalibration from enforcement compensation — the enforced p_win has
    been shrunk toward 0.5 by the Bayesian penalty, so reading it directly
    would measure the correction, not the model error.

    Falls back to ``predicted_p_win`` when ``predicted_p_win_raw IS NULL``
    (e.g. before migration 114 or when the capture flag was off), so the
    behaviour is identical to ``fetch_calibration_bias`` on old data.

    Returns the same shape as ``fetch_calibration_bias`` plus
    ``avg_pred_raw`` (the average of the raw column) for observability:
    ``{bias, status, n, win_rate, avg_pred, avg_pred_raw}``.

    The bias thresholds here are wider (``EOD_BIAS_RAW_OVERCONFIDENT`` /
    ``EOD_BIAS_RAW_UNDERCONFIDENT``) because raw model output is naturally
    more extreme. Classification is done by the caller (``run_eod_correction``
    in raw mode) using those settings.
    """
    if not settings.SUPABASE_URL:
        return None

    win_days = window_days if window_days is not None else getattr(settings, "EOD_BIAS_WINDOW_DAYS", 7)
    min_n = min_records if min_records is not None else getattr(settings, "EOD_BIAS_MIN_RECORDS", 20)

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,predicted_p_win_raw,outcome",
        "symbol": f"eq.{symbol}",
    }
    if venue:
        params["venue"] = f"eq.{venue}"
    if win_days and win_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=win_days)).isoformat()
        params["entered_at"] = f"gte.{cutoff}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_calibration_bias_raw %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) < min_n:
        return None

    n = len(data)
    sum_pred_enforced = 0.0
    sum_pred_raw = 0.0
    wins = 0
    for row in data:
        enforced = row.get("predicted_p_win", 0.5) or 0.5
        raw = row.get("predicted_p_win_raw")
        if raw is None:
            # Fallback: use enforced p_win when raw wasn't captured.
            raw = enforced
        sum_pred_enforced += float(enforced)
        sum_pred_raw += float(raw)
        if row.get("outcome") == "win":
            wins += 1

    avg_pred_enforced = sum_pred_enforced / n
    avg_pred_raw = sum_pred_raw / n
    win_rate = wins / n
    # Bias is computed from the RAW (pre-enforcement) p_win so it reflects
    # the model's true miscalibration, not the enforcement compensation.
    bias = round(avg_pred_raw - win_rate, 4)

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
        "avg_pred": round(avg_pred_enforced, 4),       # enforced average (for stamping)
        "avg_pred_raw": round(avg_pred_raw, 4),        # raw average (for bias)
    }


async def _clear_stale_eod_actions(symbol: str) -> None:
    """DELETE stale enforcement-state actions for ``symbol`` from ``nt_eod_actions``.

    Used at the start of each 4h intraday correction pass (raw-bias mode) to
    reset the enforcement state before re-evaluating from raw p_win. Only
    enforcement actions are cleared; ``parameter_decay_check`` / ``degeneracy_breaker``
    rows are preserved (they are audit-only, not enforcement).

    ``stagnation_kill_switch`` (2026-08-22 audit fix #3c) is included so a
    symbol whose bias has returned to CALIBRATED gets its Tier 2 hard block
    lifted immediately at the next cycle boundary — the stagnation streak
    is recomputed fresh from the (now-improving) bias history, so the kill
    switch is only re-written if the symbol is still genuinely stuck.

    ``bayesian_penalty`` is deliberately NOT included here (2026-08-23 fix,
    see worklog/20260823_CALIBRATION_BIAS_CHECK_AUDIT.md §2). This call runs
    unconditionally every cycle for every symbol; ``bayesian_penalty`` rows
    are also the sole history source ``fetch_recent_action_bias`` reads for
    ``_stagnation_streak`` in the SAME cycle they'd be deleted, which made
    the streak reset to 1 every single cycle — Tier 1/2 escalation could
    never fire. ``_clear_stagnation_history`` below clears bayesian_penalty
    specifically, and only on genuine recovery to CALIBRATED, restoring the
    originally-documented auto-recovery behavior without destroying the
    streak's own history in the process.

    Best-effort: failures are logged, never raised.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return
    url = _url("nt_eod_actions")
    params = {
        "action": "in.(kelly_reduction,emergency_heavy_sweep,stagnation_kill_switch)",
        "symbol": f"eq.{symbol}",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(url, headers=_headers(), params=params)
            if not resp.is_success and resp.status_code != 204:
                logger.warning(
                    "_clear_stale_eod_actions %s: HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
            elif resp.status_code == 204:
                logger.info(
                    "_clear_stale_eod_actions %s: cleared stale enforcement actions",
                    symbol,
                )
    except Exception as exc:
        logger.warning("_clear_stale_eod_actions %s failed: %s", symbol, exc)


async def _clear_stagnation_history(symbol: str) -> None:
    """DELETE ``bayesian_penalty`` rows for ``symbol`` — the stagnation
    breaker's bias history (``_stagnation_streak``, ``eod_correction.py``).

    Call this ONLY when a symbol's bias has just been classified CALIBRATED
    in raw-bias mode — i.e. genuine recovery — so the next time it goes
    OVERCONFIDENT/UNDERCONFIDENT, its stagnation streak starts fresh instead
    of picking up stale pre-recovery history. This is the auto-recovery half
    of fix #3c that ``_clear_stale_eod_actions`` was originally documented as
    providing; splitting it out fixes the 2026-08-23 clear-order bug (see
    ``_clear_stale_eod_actions``'s docstring) without losing the intended
    recovery behavior.

    Best-effort: failures are logged, never raised.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return
    url = _url("nt_eod_actions")
    params = {
        "action": "eq.bayesian_penalty",
        "symbol": f"eq.{symbol}",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(url, headers=_headers(), params=params)
            if not resp.is_success and resp.status_code != 204:
                logger.warning(
                    "_clear_stagnation_history %s: HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
            elif resp.status_code == 204:
                logger.info(
                    "_clear_stagnation_history %s: cleared bayesian_penalty history "
                    "(bias recovered to CALIBRATED)",
                    symbol,
                )
    except Exception as exc:
        logger.warning("_clear_stagnation_history %s failed: %s", symbol, exc)


async def fetch_recent_action_bias(
    symbol: str, action: str = "bayesian_penalty", limit: int = 8
) -> list[float]:
    """Return the most recent ``limit`` bias values (newest first) written
    for ``action`` on ``symbol`` in ``nt_eod_actions``.

    Used by the stagnation breaker (``eod_correction._stagnation_streak``,
    2026-08-22 audit fix #3c) to detect a symbol whose raw bias has stopped
    moving across consecutive 4h correction cycles — the audit trail
    (``nt_eod_actions``) already carries this history, so no separate
    accumulator/counter table is needed.

    Best-effort: returns ``[]`` on any failure or when Supabase is not
    configured (never raises).
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return []
    url = _url("nt_eod_actions")
    params = {
        "select": "bias,ts",
        "symbol": f"eq.{symbol}",
        "action": f"eq.{action}",
        "order": "ts.desc",
        "limit": str(int(limit)),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("fetch_recent_action_bias %s/%s failed: %s", symbol, action, exc)
        return []
    if not isinstance(data, list):
        return []
    out: list[float] = []
    for row in data:
        b = row.get("bias")
        if b is not None:
            try:
                out.append(float(b))
            except (TypeError, ValueError):
                pass
    return out


async def fetch_latest_tuning_param(param_name: str) -> dict[str, Any] | None:
    """Fetch the most recent ``nt_tuning_params`` row for ``param_name``.

    DB fallback for the tuning registry (``regime_platform.core.tuning_registry``,
    2026-08-23, Layer 0) — used when the local config file has no entry
    (cold start, or the file was lost in a Studio rebuild). Returns the row
    dict (``value``, ``previous_value``, ``ts``, ``updated_by``, ``reason``,
    ``clamped``, ``rate_limited``, ``cycle_result``, ``reverted``) or
    ``None`` on failure / no rows. See
    worklog/PARAMETER_TUNING_PLAYBOOK.md §2.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None
    url = _url("nt_tuning_params")
    params = {
        "select": "id,value,previous_value,ts,updated_by,reason,clamped,rate_limited,cycle_result,reverted",
        "param_name": f"eq.{param_name}",
        "order": "ts.desc",
        "limit": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("fetch_latest_tuning_param %s failed: %s", param_name, exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    return data[0]


async def fetch_tuning_param_history(param_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch the most recent ``limit`` ``nt_tuning_params`` rows for
    ``param_name``, newest first — for auto-rollback / streak-style
    decisions (mirrors ``fetch_recent_action_bias``'s role for the
    stagnation breaker, generalized to any registry parameter).

    Best-effort: returns ``[]`` on any failure or when Supabase is not
    configured (never raises).
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return []
    url = _url("nt_tuning_params")
    params = {
        "select": "id,value,previous_value,ts,updated_by,reason,clamped,rate_limited,cycle_result,reverted",
        "param_name": f"eq.{param_name}",
        "order": "ts.desc",
        "limit": str(int(limit)),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("fetch_tuning_param_history %s failed: %s", param_name, exc)
        return []
    return data if isinstance(data, list) else []


async def insert_tuning_param_row(
    param_name: str,
    value: Any,
    previous_value: Any,
    updated_by: str,
    reason: str,
    *,
    clamped: bool = False,
    rate_limited: bool = False,
    cycle_result: str | None = None,
    reverted: bool = False,
) -> bool:
    """Insert one audit row into ``nt_tuning_params``. Returns True on success.

    Best-effort — the config file (written by the caller BEFORE this is
    called) is authoritative for the current process; a failed DB insert
    here is logged, not raised, matching this module's other write paths.
    """
    row = {
        "param_name": param_name,
        "value": value,
        "previous_value": previous_value,
        "updated_by": updated_by,
        "reason": reason,
        "clamped": clamped,
        "rate_limited": rate_limited,
        "cycle_result": cycle_result,
        "reverted": reverted,
    }
    return await _insert_row("nt_tuning_params", row)


async def update_tuning_param_cycle_result(row_id: str, cycle_result: str) -> bool:
    """PATCH one ``nt_tuning_params`` row's ``cycle_result`` by ``id``.

    Used by the Layer 1 auto-tuner (``regime_platform.sweep_orchestrator.auto_tuner``)
    to fill in whether a pending write improved/regressed/stayed neutral,
    once the next cycle's health metric is available — mirrors
    ``eod_resolver.py``'s pattern of writing a row once (``outcome IS
    NULL``) and PATCHing it later once the outcome is known, rather than
    treating this table as strictly insert-only.

    Best-effort: returns False on any failure, never raises.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return False
    url = _url("nt_tuning_params")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                url,
                headers={**_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{row_id}"},
                json={"cycle_result": cycle_result},
            )
            if not resp.is_success:
                logger.warning(
                    "update_tuning_param_cycle_result %s: HTTP %d — %s",
                    row_id, resp.status_code, resp.text[:200],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("update_tuning_param_cycle_result %s failed: %s", row_id, exc)
        return False


async def fetch_latest_sweep(symbol: str) -> dict[str, Any] | None:
    """Fetch the most recent nt_sweep_result row for a symbol.

    Used by the EOD correction loop to detect HMM degeneracy when no
    calibration records exist (bias=None) — if p_win=<0.01, the symbol
    is stuck in the degeneracy deadlock (p_win=0 → qualified=False →
    no resolved outcomes → no calibration records → no EOD correction).

    Returns the full row dict (including p_win, qualified, regime_conf, etc.)
    or None if not found / query fails.
    """
    try:
        url = _url("nt_sweep_result")
        params = {
            "select": "*",
            "symbol": f"eq.{symbol}",
            "order": "sweep_timestamp.desc",
            "limit": "1",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_latest_sweep %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None
    return data[0]
