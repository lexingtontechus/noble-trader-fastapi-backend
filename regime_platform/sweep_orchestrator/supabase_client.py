"""
Supabase REST Client for the Sweep Orchestrator
═════════════════════════════════════════════════

Thin wrapper around httpx for reading/writing Supabase PostgREST tables.
Follows the same pattern as existing backend modules (calibration.py, venue.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
    # '0 20 * * *' (daily 20:00 UTC) when NULL — changed from weekly
    # 2026-09-08, migration 142 (see heavy_sweep.py's module docstring).
    # plan_ids (UUID[]) added in migration 032 — needed so the orchestrator's
    # Discord plan-routing can route signals per subscription plan
    # (Signal Scout / Precision Pro).
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
    # 2026-09-23: 5s→15s + one retry (ReadTimeout under light-sweep write
    # pile-up). Log type+repr — ReadTimeout('') stringifies empty.
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
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
                    # 2026-09-24: 502/503 = gateway never reached Postgres →
                    # safe to retry a non-idempotent insert (one AUDUSD row
                    # lost to a 502 on 2026-09-24 21:45).
                    if resp.status_code in (502, 503) and attempt == 1:
                        await asyncio.sleep(1.0)
                        continue
                return
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
    logger.warning(
        "log_regime_entry %s failed: %s: %r",
        symbol, type(last_exc).__name__ if last_exc else "unknown", last_exc,
    )


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
    # atr (migration 120): ATR(14) at signal-capture time, absolute price
    # units — feeds the bracket-reachability diagnostic
    # (bracket_reachability.py). Nullable; rows captured before 2026-08-25
    # have no value.
    "atr",
    # macro_label/macro_strength (migration 124): the macro filter's read
    # for this symbol at capture time — feeds the win/loss+PnL evidence
    # diagnostic (macro_edge.py). Nullable; absent when macro_regime_mode
    # is off or no nt_macro_regime row exists yet.
    "macro_label", "macro_strength",
    # trend_label/trend_strength (2026-09-10, worklog
    # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS7): the
    # medium-term (10-50 day) trend filter's read for this symbol at
    # capture time — see trend_filter.py. Nullable; absent when
    # trend_regime_mode is off or no nt_trend_regime row exists yet.
    "trend_label", "trend_strength",
    # context_aligned_ok (2026-09-11, worklog
    # 20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part
    # 10): Tier 1 gate read at capture time — see context_alignment.py.
    # Nullable; absent when context_alignment_gate_mode is off.
    "context_aligned_ok",
    # legacy_markov_state/trend_hmm_markov_state, raw_p_regime/
    # gated_p_regime (migration 131, 2026-08-31): Tier 1/2's own shadow-
    # comparison values at capture time — feed the outcome-linked
    # accuracy diagnostic (hmm_shadow_accuracy.py). Nullable; only
    # populated during the real shadow-mode comparison.
    "legacy_markov_state", "trend_hmm_markov_state",
    "raw_p_regime", "gated_p_regime",
    # drift_*_pct (migration 121, Phase 2): direction-relative post-entry
    # price drift at fixed horizons, written by eod_resolver.py's PATCH
    # (this constant governs capture-time inserts; eod_resolver.py keeps
    # its own local copy in sync for the outcome patch).
    # 12h/48h added 2026-09-24 (Kairos v3 P0.3, migration 167).
    "drift_1h_pct", "drift_4h_pct", "drift_12h_pct", "drift_24h_pct", "drift_48h_pct",
    # Kairos v3 P0.5 (migration 167): geometry-free market-movement stamps
    # at capture time (kairos.capture_factors). Nullable; only written when
    # kairos_mode != off.
    "rel_volume_1h", "clv_1h", "efficiency_24h", "atr_ratio_run", "spread_bps",
    # Kairos G2 (migration 168): qualified | suppressed | gate_fail
    "gate_outcome", "suppress_reason", "failed_gates",
    # Kairos rank shadow (migration 172): composite rank_score + would-publish
    "kairos_rank_score", "kairos_rank_gate_shadow",
    # Variable binary Kelly shadow (migration 174)
    "kelly_binary_f", "kelly_context_mult", "kelly_effective_shadow",
    # Batch C (migration 154): directional vs bracket-outcome probabilities
    "p_win_dir", "p_win_bracket", "p_resolve", "p_win_resolve",
    "bracket_bucket", "bracket_vol_tier", "bracket_gate_shadow",
    "qualified_geometry_shadow",
    # Batch tiered-ATR (migration 156): geometry / long-lag risk
    "tp_distance_atr", "sl_distance_atr", "long_lag_hat",
    "geometry_risk_hat", "long_lag_risk",
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


async def _insert_row(
    table: str,
    payload: dict[str, Any],
    *,
    idempotent: bool = False,
    drop_on_schema_error: tuple[str, ...] = (),
) -> bool:
    """Insert one row into ``table``. Returns True on success.

    Per-row success is DEBUG (EOD catchup can insert hundreds of
    calibration records in a burst — INFO-per-row drowned real signals).
    Failures stay ERROR. Callers that need a summary log it themselves.

    2026-09-24 transient-failure fix (worklog 20260924_SUPABASE_INSERT_RETRY):
    the 5s timeout lost rows to ReadTimeout under light-sweep write pile-up
    (Supabase 502/504 bursts), and callers then mis-read a timeout as a
    schema problem. Now:

    * timeout 15s (same as log_regime_entry's 2026-09-23 fix);
    * one retry on errors where the first request never reached Postgres
      (connect/pool errors, HTTP 503) — safe for every table, including
      non-idempotent ones like nt_eod_actions / nt_calibration_records
      where a duplicate row would double-apply;
    * ``idempotent=True`` (the payload carries a client-generated primary
      key, e.g. nt_signal_sim.signal_id) additionally retries ReadTimeout /
      HTTP 502 / 504, where the first attempt may have landed — a 409
      duplicate key on the retry therefore means "already written" → success;
    * ``drop_on_schema_error``: only on HTTP 400 PGRST204 (unknown column —
      Studio deployed ahead of a migration) retry once without these keys.
      A timeout never strips columns.
    """
    if not settings.SUPABASE_URL:
        return False
    body = dict(payload)
    dropped = False
    for attempt in (1, 2, 3):
        retry_ok = False
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    _url(table),
                    headers={**_headers(), "Prefer": "return=minimal"},
                    json=body,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            logger.error("%s insert failed (attempt %d): %s: %r", table, attempt, type(exc).__name__, exc)
            retry_ok = True
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            logger.error("%s insert failed (attempt %d): %s: %r", table, attempt, type(exc).__name__, exc)
            retry_ok = idempotent
        except Exception as exc:
            logger.error("%s insert failed: %s: %r", table, type(exc).__name__, exc)
            return False
        else:
            if resp.is_success:
                logger.debug("%s insert OK for %s", table, body.get("symbol", "unknown"))
                return True
            if resp.status_code == 409 and idempotent and attempt > 1:
                # First attempt landed despite the timeout / 504.
                logger.info("%s insert: attempt %d found the row already written (409) — OK", table, attempt)
                return True
            logger.error("%s insert HTTP %d (attempt %d) — %s", table, resp.status_code, attempt, resp.text[:300])
            if resp.status_code == 400 and "PGRST204" in resp.text and drop_on_schema_error and not dropped:
                keys = [k for k in drop_on_schema_error if k in body]
                if keys:
                    for k in keys:
                        body.pop(k, None)
                    dropped = True
                    logger.warning("%s insert: schema lag — retrying without %s", table, keys)
                    continue
            retry_ok = resp.status_code == 503 or (idempotent and resp.status_code in (502, 504))
        if not retry_ok or attempt >= 2 + int(dropped):
            return False
        await asyncio.sleep(1.0)
    return False


async def fetch_recent_decided_signals(limit: int = 500) -> list[dict[str, Any]]:
    """Recent decided TP/SL light-sweep rows for RankScoreTracker ingest.

    Worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md §9.
    Returns [] on any failure (shadow bookkeeping must never break the job).
    """
    return await fetch_all_rows(
        "nt_signal_sim",
        "symbol,regime,outcome,ts,exit_ts",
        filters={
            "source": "eq.sweep-light",
            "signal": "in.(buy,sell)",
            "outcome": "in.(tp_hit,sl_hit)",
        },
        order="exit_ts.desc",
        max_rows=max(1, int(limit)),
    )


async def fetch_all_rows(
    table: str,
    select: str,
    filters: dict[str, str] | None = None,
    order: str | None = None,
    max_rows: int = 5000,
) -> list[dict[str, Any]]:
    """Generic read: rows from ``table`` matching ``filters`` (PostgREST
    operator syntax, e.g. ``{"symbol": "eq.AAPL", "ts": "gt.2026-09-01"}``),
    paginated up to ``max_rows`` (best-effort, returns ``[]`` on any
    failure — PostgREST caps a single response at 1000 rows regardless
    of what's requested, so this loops via the ``Range`` header rather
    than assuming one call returns everything).

    Used by the Phase 2 self-learning detectors
    (regime_platform/services/self_learning/), which read from a
    diverse set of mode-gated shadow/regime tables (small, single-row-
    per-symbol reads) AND large per-tick history (nt_sweep_result) —
    one generic, paginated reader covers both rather than a near-
    duplicate one-off per table (the exact "redundant computation"
    shape detector 3 itself looks for).
    """
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    page_size = 1000
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while len(rows) < max_rows:
                params: dict[str, str] = {"select": select}
                if filters:
                    params.update(filters)
                if order:
                    params["order"] = order
                resp = await client.get(
                    _url(table),
                    headers={
                        **_headers(),
                        "Range-Unit": "items",
                        "Range": f"{offset}-{offset + page_size - 1}",
                    },
                    params=params,
                )
                resp.raise_for_status()
                page = resp.json()
                if not isinstance(page, list) or not page:
                    break
                rows.extend(page)
                if len(page) < page_size:
                    break
                offset += page_size
        return rows[:max_rows]
    except Exception as exc:
        logger.warning("fetch_all_rows: %s: read failed: %s: %r", table, type(exc).__name__, exc)
        return rows


async def insert_self_learning_finding(
    detector: str,
    summary: str,
    symbol: str | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Insert one row into ``nt_self_learning_findings`` (best-effort).
    Diagnostic-only output from the Phase 2 detectors — nothing reads
    this to make a live decision."""
    return await _insert_row("nt_self_learning_findings", {
        "detector": detector,
        "symbol": symbol,
        "summary": summary,
        "details": details,
    })


async def fetch_untriaged_findings(limit: int = 20) -> list[dict[str, Any]]:
    """Rows from ``nt_self_learning_findings`` where ``triaged_at IS
    NULL``, oldest first — feeds the Phase 3 MVP LLM-triage job
    (``regime_platform/services/self_learning/triage.py``, RM-50)."""
    return await fetch_all_rows(
        "nt_self_learning_findings",
        "id,detector,symbol,summary,details,detected_at",
        filters={"triaged_at": "is.null"},
        order="detected_at.asc",
        max_rows=limit,
    )


async def mark_finding_triaged(finding_id: int) -> bool:
    """PATCH one ``nt_self_learning_findings`` row's ``triaged_at`` to
    now, once the Phase 3 triage job has produced a report for it (or
    given up on it) — so the job doesn't reprocess the same row every
    night. Best-effort: returns False on any failure, never raises."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                _url("nt_self_learning_findings"),
                headers={**_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{finding_id}"},
                json={"triaged_at": datetime.now(timezone.utc).isoformat()},
            )
            if not resp.is_success:
                logger.warning(
                    "mark_finding_triaged %s: HTTP %d — %s",
                    finding_id, resp.status_code, resp.text[:200],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("mark_finding_triaged %s failed: %s", finding_id, exc)
        return False


async def insert_self_learning_report(
    finding_id: int,
    narrative: str,
    proposed_param: str | None = None,
    proposed_value: float | None = None,
    current_value: float | None = None,
) -> bool:
    """Insert one row into ``nt_self_learning_reports`` (best-effort).
    Written-report-only output from the Phase 3 MVP LLM-triage job —
    nothing reads this table to make a live decision; a human reads it
    and manually actions any proposed Tier D parameter change."""
    return await _insert_row("nt_self_learning_reports", {
        "finding_id": finding_id,
        "narrative": narrative,
        "proposed_param": proposed_param,
        "proposed_value": proposed_value,
        "current_value": current_value,
    })


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
    # signal_id is client-generated → the insert is idempotent (a retry
    # after a timeout that actually landed returns 409 = already written).
    # Kairos/G2 stamps are dropped ONLY on a PGRST204 schema lag, never on
    # a timeout (2026-09-24: the old "retry without stamps on any failure"
    # stripped gate_outcome from 3 rows that simply timed out).
    await _insert_row(
        "nt_signal_sim", sim_payload,
        idempotent=True, drop_on_schema_error=_KAIROS_CAPTURE_KEYS,
    )


_KAIROS_CAPTURE_KEYS: tuple[str, ...] = (
    "rel_volume_1h", "clv_1h", "efficiency_24h", "atr_ratio_run", "spread_bps",
    "gate_outcome", "suppress_reason", "failed_gates",
    "kairos_rank_score", "kairos_rank_gate_shadow",
    "kelly_binary_f", "kelly_context_mult", "kelly_effective_shadow",
)


NT_RENKO_BRICKS_COLUMNS: frozenset[str] = frozenset({
    "symbol", "direction", "brick_size", "open_price", "close_price",
    "high", "low", "label", "brick_index", "ts",
})


async def _purge_orphan_renko_bricks(
    client: httpx.AsyncClient,
    symbol: str,
    brick_size: float,
    session_date: str,
    max_brick_index: int,
) -> int:
    """Delete leftover high ``brick_index`` rows from a longer prior build.

    ``log_renko_bricks`` upserts indices ``0..N-1`` of the current
    ``build_from_prices`` series. A previous longer build may have written
    indices ``N..M`` on the same ``(symbol, brick_size, session_date)``
    grid; those rows are never overwritten and sort first under
    ``brick_index DESC``, so charts pick stale prices as "last 10".

    Deletes only same-grid orphans (``brick_index > max_brick_index`` with
    brick_size inside the same 0.1% band as retention). Best-effort —
    returns the deleted-row count, never raises.
    """
    tol = max(abs(brick_size) * 1e-3, 1e-9)
    low = brick_size - tol
    high = brick_size + tol
    params = {
        "symbol": f"eq.{symbol}",
        "session_date": f"eq.{session_date}",
        "brick_index": f"gt.{int(max_brick_index)}",
        "and": f"(brick_size.gte.{low:.10f},brick_size.lte.{high:.10f})",
        "select": "brick_id",
    }
    try:
        resp = await client.delete(
            _url("nt_renko_bricks"),
            headers={**_headers(), "Prefer": "count=exact"},
            params=params,
        )
        if not resp.is_success:
            logger.warning(
                "log_renko_bricks: orphan purge HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        content_range = resp.headers.get("content-range") or ""
        if "/" in content_range:
            total = content_range.split("/", 1)[1].strip()
            return int(total) if total.isdigit() else 0
        return 0
    except Exception as exc:
        logger.warning("log_renko_bricks: orphan purge failed: %s", exc)
        return 0


async def log_renko_bricks(
    symbol: str,
    bricks: list[dict[str, Any]],
    brick_size: float | None = None,
    session_date: str | None = None,
    sweep_ts: float | None = None,
    has_real_timestamps: bool = False,
) -> None:
    """Persist one brick per row to ``nt_renko_bricks`` (best-effort).

    Matches the real table schema: brick_id serial PK, unique
    (symbol, brick_index, session_date, brick_size) — brick_size was added
    to the key 2026-09-09 (migration 145,
    worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7
    "Finding 2") so a re-sweep at a NEW brick_size can never silently
    overwrite a row that belongs to a DIFFERENT grid at the same
    (symbol, brick_index, session_date) — each (symbol, brick_size) pair
    now owns its own brick_index/session_date space. Growth from stale
    grids is bounded separately by the nightly ``renko_retention.py`` pass,
    not by this key change. Uses ``Prefer: resolution=merge-duplicates`` +
    ``on_conflict=symbol,brick_index,session_date,brick_size`` so a
    re-sweep of the same symbol+brick_index+brick_size replaces the prior
    brick instead of duplicating rows (the table's PK is the serial
    brick_id, so without the on_conflict hint PostgREST resolves on
    brick_id → always INSERTs → unbounded growth).

    ``session_date`` defaults to CURRENT_DATE (DB default) when omitted.

    ``has_real_timestamps`` (2026-09-09 fix for worklog §7 "Finding 1"):
    when True, each brick dict's own ``"timestamp"`` field (set by
    ``RenkoBrickEngine.build_from_prices(prices, timestamps=...)`` from the
    REAL per-bar epoch the price fetcher returned) is used as-is — this is
    now the expected default path from the live sweep, which threads real
    timestamps through end to end (see orchestrator.py's brick-capture
    block). When False (the caller determined real timestamps were
    unavailable or incomplete for this batch — e.g. the price fetch had no
    parseable time field for some bars, or a caller invoked this with only
    close prices), ``build_from_prices`` fell back to its own bare
    auto-incrementing index (0, 1, 2, ...), so a real epoch cannot be
    recovered from the brick dict — that case falls back to synthesising
    hourly spacing backward from ``sweep_ts`` purely so the admin chart's
    x-axis stays readable. This fallback timing is FABRICATED (uniform
    "1 hour per brick," not real brick-formation timing) and must never be
    treated as a real feature — see the module docstring reference above.

    Never raises: failures are logged and swallowed.
    """
    if not settings.SUPABASE_URL:
        return

    rows = []
    n = len(bricks)
    for i, b in enumerate(bricks):
        if has_real_timestamps:
            ts_epoch = float(b.get("timestamp") or 0)
        elif sweep_ts is not None:
            # Fallback path — triggers ONLY when the caller could not
            # supply real per-bar timestamps (has_real_timestamps=False).
            # build_from_prices was given no timestamps (or an incomplete
            # array), so every brick.timestamp here is a bare
            # auto-incrementing index, not a real epoch. Synthesise hourly
            # spacing backward from sweep_ts instead — a uniform "1 hour
            # per brick" reconstruction, NOT real brick-formation timing.
            ts_epoch = sweep_ts - (n - 1 - i) * 3600.0
        else:
            # Neither real timestamps nor a sweep_ts fallback were
            # supplied — the brick's own field is whatever the caller put
            # there (real epoch if the caller pre-populated it directly,
            # else build_from_prices' bare index).
            ts_epoch = float(b.get("timestamp") or 0)
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

    # Live grid identity for the orphan purge — same symbol/brick_size/
    # session_date as the upserted rows, max index of THIS build.
    idx_vals = [r["brick_index"] for r in rows if r.get("brick_index") is not None]
    max_brick_index = max(int(i) for i in idx_vals) if idx_vals else None
    eff_brick_size = float(brick_size or rows[0].get("brick_size") or 0)
    eff_session_date = (
        session_date
        or rows[0].get("session_date")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_renko_bricks')}?on_conflict=symbol,brick_index,session_date,brick_size",
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
                return
            # Self-heal orphans from a prior longer build on this grid.
            # Upstream only overwrites 0..N-1; rows N..M must go or charts
            # ordered by brick_index DESC plot the stale tail as "latest".
            if max_brick_index is not None and eff_brick_size > 0:
                purged = await _purge_orphan_renko_bricks(
                    client,
                    symbol=symbol,
                    brick_size=eff_brick_size,
                    session_date=eff_session_date,
                    max_brick_index=max_brick_index,
                )
                if purged:
                    logger.info(
                        "log_renko_bricks: purged %d orphan brick(s) for %s "
                        "(bs=%s session=%s index>%d)",
                        purged, symbol, eff_brick_size, eff_session_date, max_brick_index,
                    )
    except Exception as exc:
        logger.error("log_renko_bricks: insert failed: %s", exc)


NT_BRACKET_REACHABILITY_COLUMNS: frozenset[str] = frozenset({
    "symbol", "vol_tier", "distance_atr_bucket", "n",
    "pct_hit_tp", "pct_hit_sl", "pct_expired",
    "median_hours_to_resolve", "p75_hours", "p90_hours",
    "atr_source",
    # Batch B (migration 153): split counts so consumers can quarantine
    # look-ahead proxy ATR without discarding the whole bucket row.
    "n_stored", "n_proxy",
    # Migration 159: buy | sell | all. Consumers of the pooled stats must
    # filter signal='all' (see bracket_reachability.py).
    "signal",
})


async def upsert_bracket_reachability(rows: list[dict[str, Any]]) -> None:
    """Upsert bucket rows into ``nt_bracket_reachability`` (best-effort).

    Keyed on ``(symbol, vol_tier, distance_atr_bucket, signal)`` (2026-09-22,
    migration 159 added the buy/sell/all side dimension; was
    ``(symbol, vol_tier, distance_atr_bucket)`` since 2026-09-13) — a weekly
    refresh REPLACES each bucket's stats rather than accumulating duplicates,
    same ``on_conflict`` + ``Prefer: resolution=merge-duplicates`` pattern as
    ``log_renko_bricks``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_BRACKET_REACHABILITY_COLUMNS) for r in rows]
    # Default to the pooled side so older callers that omit `signal` keep
    # writing the pre-159 aggregate key.
    for r in clean_rows:
        r.setdefault("signal", "all")
    if not clean_rows:
        logger.debug("upsert_bracket_reachability: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_bracket_reachability')}?on_conflict=symbol,vol_tier,distance_atr_bucket,signal",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_bracket_reachability: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_bracket_reachability: insert failed: %s", exc)


NT_CALIBRATION_BIAS_BY_BUCKET_COLUMNS: frozenset[str] = frozenset({
    "symbol", "vol_tier", "distance_atr_bucket", "n",
    "avg_p_win", "win_rate", "bias", "pct_expired",
    "avg_rr_winners", "expectancy_r",
    "median_hours_to_resolve", "daily_expectancy_r",
    "atr_source",
})


async def upsert_calibration_bias_by_bucket(rows: list[dict[str, Any]]) -> None:
    """Upsert bucket rows into ``nt_calibration_bias_by_bucket`` (best-effort).

    Keyed on ``(symbol, vol_tier, distance_atr_bucket)`` (2026-09-13,
    worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md) — same upsert-on-
    conflict shape as ``upsert_bracket_reachability``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_CALIBRATION_BIAS_BY_BUCKET_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_calibration_bias_by_bucket: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_calibration_bias_by_bucket')}?on_conflict=symbol,vol_tier,distance_atr_bucket",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_calibration_bias_by_bucket: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_calibration_bias_by_bucket: insert failed: %s", exc)


NT_CALIBRATION_BIAS_BY_CELL_COLUMNS: frozenset[str] = frozenset({
    "symbol", "side", "vol_tier", "distance_atr_bucket",
    "n", "wins", "losses", "avg_p_win", "win_rate", "bias", "bias_shrunk",
    "expectancy_r",
})


async def upsert_calibration_bias_by_cell(rows: list[dict[str, Any]]) -> None:
    """Upsert side-level cell rows into ``nt_calibration_bias_by_cell`` (R3)."""
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_CALIBRATION_BIAS_BY_CELL_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_calibration_bias_by_cell: no rows — skipping")
        return

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_url('nt_calibration_bias_by_cell')}?on_conflict=symbol,side,vol_tier,distance_atr_bucket",
                    headers={
                        **_headers(),
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=clean_rows,
                )
                if not resp.is_success:
                    logger.error(
                        "upsert_calibration_bias_by_cell: insert HTTP %d — %s",
                        resp.status_code, resp.text[:300],
                    )
                return
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
    logger.error(
        "upsert_calibration_bias_by_cell: insert failed: %s: %r",
        type(last_exc).__name__ if last_exc else "unknown", last_exc,
    )


async def fetch_calibration_bias_cell(
    symbol: str,
    side: str,
    vol_tier: str,
    distance_atr_bucket: str,
) -> dict[str, Any] | None:
    """Fetch one R3 cell row (or None). Used by calibration_enforcement."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None
    params = {
        "select": "symbol,side,vol_tier,distance_atr_bucket,n,wins,losses,avg_p_win,win_rate,bias,bias_shrunk,expectancy_r,computed_at",
        "symbol": f"eq.{symbol}",
        "side": f"eq.{side}",
        "vol_tier": f"eq.{vol_tier}",
        "distance_atr_bucket": f"eq.{distance_atr_bucket}",
        "limit": "1",
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _url("nt_calibration_bias_by_cell"), headers=_headers(), params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            if not isinstance(data, list) or not data:
                return None
            return data[0]
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
    logger.warning(
        "fetch_calibration_bias_cell %s/%s failed: %s: %r",
        symbol, side,
        type(last_exc).__name__ if last_exc else "unknown", last_exc,
    )
    return None


NT_LIVE_EXPECTANCY_DAILY_COLUMNS: frozenset[str] = frozenset({
    "day", "cohort", "n", "n_tp", "n_sl", "n_expired",
    "win_rate_closed", "pct_expired", "expectancy_r",
    "avg_p_dir", "avg_p_win_bracket", "bias_dir",
})


async def upsert_live_expectancy_daily(rows: list[dict[str, Any]]) -> None:
    """Upsert P2 flip-evidence rows into ``nt_live_expectancy_daily``."""
    if not settings.SUPABASE_URL:
        return
    clean_rows = [_filter_payload(r, NT_LIVE_EXPECTANCY_DAILY_COLUMNS) for r in rows]
    if not clean_rows:
        return
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_url('nt_live_expectancy_daily')}?on_conflict=day,cohort",
                    headers={
                        **_headers(),
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=clean_rows,
                )
                if not resp.is_success:
                    logger.error(
                        "upsert_live_expectancy_daily: insert HTTP %d — %s",
                        resp.status_code, resp.text[:300],
                    )
                return
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
    logger.error(
        "upsert_live_expectancy_daily: insert failed: %s: %r",
        type(last_exc).__name__ if last_exc else "unknown", last_exc,
    )


NT_ENTRY_DRIFT_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n",
    "avg_drift_1h_pct", "stdev_drift_1h_pct",
    "avg_drift_4h_pct", "stdev_drift_4h_pct",
    "avg_drift_24h_pct", "stdev_drift_24h_pct",
})


async def upsert_entry_drift(rows: list[dict[str, Any]]) -> None:
    """Upsert per-symbol rows into ``nt_entry_drift`` (best-effort).

    Keyed on ``symbol`` (the table's PK) — same upsert-on-conflict shape
    as ``upsert_bracket_reachability``/``upsert_calibration_bias_by_bucket``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_ENTRY_DRIFT_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_entry_drift: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_entry_drift')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_entry_drift: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_entry_drift: insert failed: %s", exc)


NT_HEAVY_SWEEP_REACHABILITY_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "n_nothing_survived", "n_pick_changed",
    "last_baseline_bucket", "last_baseline_sharpe",
    "last_filtered_bucket", "last_filtered_sharpe", "vol_tier", "updated_at",
})


async def record_heavy_sweep_reachability_shadow(
    symbol: str,
    nothing_survived: bool,
    pick_changed: bool,
    baseline_bucket: str | None,
    baseline_sharpe: float | None,
    filtered_bucket: str | None,
    filtered_sharpe: float | None,
    vol_tier: str | None = None,
) -> None:
    """Accumulate one heavy-sweep's reachability-aware-pick comparison
    into the running per-symbol row in ``nt_heavy_sweep_reachability_shadow``
    (best-effort). Phase 3a of the calibration reframe (worklog
    20260825_CALIBRATION_REFRAME_SCOPE.md).

    ``vol_tier`` (2026-09-13, worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md)
    is the live regime's vol tier at THIS comparison — visibility only,
    not part of the upsert key (this table stays keyed on symbol alone,
    one running row per symbol). Lets a human query "is this symbol's
    nothing-survived pattern concentrated in a specific vol tier" directly.

    Fetch-then-upsert, same shape as ``record_markov_shadow_compare`` —
    ``updated_at`` included in the payload from the start (that
    function's own follow-up fix showed why this matters: an upsert-on-
    conflict never touches a column absent from the payload, so without
    this the timestamp silently freezes at first-insert time forever).
    """
    if not settings.SUPABASE_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_heavy_sweep_reachability_shadow"),
                headers=_headers(),
                params={
                    "select": "n_compared,n_nothing_survived,n_pick_changed",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            prev = data[0] if isinstance(data, list) and data else {}

            row = _filter_payload({
                "symbol": symbol,
                "n_compared": int(prev.get("n_compared", 0)) + 1,
                "n_nothing_survived": int(prev.get("n_nothing_survived", 0)) + (1 if nothing_survived else 0),
                "n_pick_changed": int(prev.get("n_pick_changed", 0)) + (1 if pick_changed else 0),
                "last_baseline_bucket": baseline_bucket,
                "last_baseline_sharpe": round(float(baseline_sharpe), 4) if baseline_sharpe is not None else None,
                "last_filtered_bucket": filtered_bucket,
                "last_filtered_sharpe": round(float(filtered_sharpe), 4) if filtered_sharpe is not None else None,
                "vol_tier": vol_tier,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, NT_HEAVY_SWEEP_REACHABILITY_SHADOW_COLUMNS)

            resp = await client.post(
                f"{_url('nt_heavy_sweep_reachability_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_heavy_sweep_reachability_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_heavy_sweep_reachability_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


NT_HEAVY_SWEEP_ATR_ANCHORED_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "n_changed",
    "last_baseline_bucket", "last_baseline_sharpe",
    "last_widened_bucket", "last_widened_sharpe", "updated_at",
})


async def record_heavy_sweep_atr_anchored_shadow(
    symbol: str,
    changed: bool,
    baseline_bucket: str | None,
    baseline_sharpe: float | None,
    widened_bucket: str | None,
    widened_sharpe: float | None,
) -> None:
    """Accumulate one heavy-sweep's ATR-anchored-bricks comparison into the
    running per-symbol row in ``nt_heavy_sweep_atr_anchored_shadow``
    (best-effort). Phase A of worklog
    20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md.

    Fetch-then-upsert, same shape as
    ``record_heavy_sweep_reachability_shadow`` (its sibling table) —
    ``updated_at`` included in the payload from the start for the same
    reason: an upsert-on-conflict never touches a column absent from the
    payload, so without this the timestamp would silently freeze at
    first-insert time forever.
    """
    if not settings.SUPABASE_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_heavy_sweep_atr_anchored_shadow"),
                headers=_headers(),
                params={
                    "select": "n_compared,n_changed",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            prev = data[0] if isinstance(data, list) and data else {}

            row = _filter_payload({
                "symbol": symbol,
                "n_compared": int(prev.get("n_compared", 0)) + 1,
                "n_changed": int(prev.get("n_changed", 0)) + (1 if changed else 0),
                "last_baseline_bucket": baseline_bucket,
                "last_baseline_sharpe": round(float(baseline_sharpe), 4) if baseline_sharpe is not None else None,
                "last_widened_bucket": widened_bucket,
                "last_widened_sharpe": round(float(widened_sharpe), 4) if widened_sharpe is not None else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, NT_HEAVY_SWEEP_ATR_ANCHORED_SHADOW_COLUMNS)

            resp = await client.post(
                f"{_url('nt_heavy_sweep_atr_anchored_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_heavy_sweep_atr_anchored_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_heavy_sweep_atr_anchored_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


NT_HEAVY_SWEEP_DEADLINE_AWARE_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "n_changed",
    "last_uncapped_sharpe", "last_corrected_sharpe", "updated_at",
})


async def record_heavy_sweep_deadline_aware_shadow(
    symbol: str,
    changed: bool,
    uncapped_sharpe: float | None,
    corrected_sharpe: float | None,
) -> None:
    """Accumulate one heavy-sweep's deadline-aware-scoring comparison into
    the running per-symbol row in ``nt_heavy_sweep_deadline_aware_shadow``
    (best-effort). Root-cause fix, worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.

    Fetch-then-upsert, same shape as its sibling
    ``record_heavy_sweep_atr_anchored_shadow`` — ``updated_at`` included in
    the payload from the start for the same reason: an upsert-on-conflict
    never touches a column absent from the payload.
    """
    if not settings.SUPABASE_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_heavy_sweep_deadline_aware_shadow"),
                headers=_headers(),
                params={
                    "select": "n_compared,n_changed",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            prev = data[0] if isinstance(data, list) and data else {}

            row = _filter_payload({
                "symbol": symbol,
                "n_compared": int(prev.get("n_compared", 0)) + 1,
                "n_changed": int(prev.get("n_changed", 0)) + (1 if changed else 0),
                "last_uncapped_sharpe": round(float(uncapped_sharpe), 4) if uncapped_sharpe is not None else None,
                "last_corrected_sharpe": round(float(corrected_sharpe), 4) if corrected_sharpe is not None else None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, NT_HEAVY_SWEEP_DEADLINE_AWARE_SHADOW_COLUMNS)

            resp = await client.post(
                f"{_url('nt_heavy_sweep_deadline_aware_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_heavy_sweep_deadline_aware_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_heavy_sweep_deadline_aware_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


NT_MACRO_REGIME_COLUMNS: frozenset[str] = frozenset({
    "symbol", "macro_score", "macro_score_smoothed", "macro_label",
    "macro_strength", "sma_window_days", "computed_at",
})


async def upsert_macro_regime(rows: list[dict[str, Any]]) -> None:
    """Upsert per-symbol rows into ``nt_macro_regime`` (best-effort).

    Keyed on ``symbol`` (the table's PK) — a daily refresh REPLACES the
    row rather than accumulating duplicates, same shape as
    ``upsert_bracket_reachability``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_MACRO_REGIME_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_macro_regime: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_macro_regime')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_macro_regime: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_macro_regime: insert failed: %s", exc)


async def fetch_macro_regime(symbol: str) -> dict[str, Any] | None:
    """Single-row read of ``nt_macro_regime`` for one symbol — the live
    per-tick read both sweep paths use right before applying the macro
    Kelly multiplier. Returns ``None`` on any failure or missing row
    (never raises — callers treat that as "no macro read yet, no-op").

    2026-09-20: timeout 5s→15s + one retry (ReadTimeout dropped labels).
    """
    if not settings.SUPABASE_URL:
        return None
    params = {
        "select": "macro_label,macro_strength,macro_score_smoothed,computed_at",
        "symbol": f"eq.{symbol}",
        "limit": "1",
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _url("nt_macro_regime"),
                    headers=_headers(),
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                return data[0] if isinstance(data, list) and data else None
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logger.info("fetch_macro_regime: %s retry (%s)", symbol, type(exc).__name__)
                continue
    logger.warning(
        "fetch_macro_regime: %s: read failed: %s: %r",
        symbol, type(last_exc).__name__ if last_exc else "?", last_exc,
    )
    return None


NT_MACRO_FORECAST_COLUMNS: frozenset[str] = frozenset({
    "symbol", "current_trend_label", "current_macro_label",
    "current_macro_strength", "narrative", "model_provider", "updated_at",
    # P2 T-Layer (migration 181) — same table, same upsert path
    "pivot_label", "pivot_axis", "pivot_intensity", "pivot_surprise",
    "pivot_as_of", "pivot_event_title", "pivot_summary",
    # Uniform multi-asset (migration 182)
    "thesis", "drivers", "risks", "horizon_days",
    "structure_bias", "structure_strength", "structure_note",
    "structure_as_of", "structure_source",
    "events", "source_doc", "asset_class",
})


async def upsert_macro_forecast(rows: list[dict[str, Any]]) -> None:
    """Upsert per-symbol rows into ``nt_macro_forecast`` (best-effort).

    Keyed on ``symbol`` (the table's PK) — a daily refresh REPLACES the
    row rather than accumulating duplicates, same shape as
    ``upsert_macro_regime``. Written by the Phase 3 macro-forecast agent
    (``regime_platform/services/self_learning/macro_forecast_agent.py``,
    worklog 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md) —
    shadow-mode diagnostic output, nothing reads this table to make a
    live decision yet.

    PostgREST bulk upsert (PGRST102) requires every object in the array
    to share the same keys — pad optional 181/182 columns with None so
    partial structure/pivot enrichment cannot fail the whole batch.
    """
    if not settings.SUPABASE_URL:
        return

    # Canonical key set = every allowlisted column actually seen, plus required.
    seen: set[str] = set()
    filtered = []
    for r in rows:
        pr = _filter_payload(r, NT_MACRO_FORECAST_COLUMNS)
        filtered.append(pr)
        seen.update(pr.keys())
    # Always include identity + core vocab so keys are stable across runs.
    seen.update({"symbol", "updated_at"})
    clean_rows = [{k: row.get(k) for k in sorted(seen)} for row in filtered]
    if not clean_rows:
        logger.debug("upsert_macro_forecast: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_macro_forecast')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_macro_forecast: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_macro_forecast: insert failed: %s", exc)


NT_TREND_REGIME_COLUMNS: frozenset[str] = frozenset({
    "symbol", "sma_10", "sma_20", "sma_50", "rsi_14", "rsi_state",
    "days_since_crossover", "last_crossover_direction",
    "trend_label", "trend_strength", "computed_at",
})


async def upsert_trend_regime(rows: list[dict[str, Any]]) -> None:
    """Upsert per-symbol rows into ``nt_trend_regime`` (best-effort).

    Keyed on ``symbol`` (the table's PK) — a daily refresh REPLACES the
    row rather than accumulating duplicates, same shape as
    ``upsert_macro_regime``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_TREND_REGIME_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_trend_regime: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_trend_regime')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_trend_regime: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_trend_regime: insert failed: %s: %r", type(exc).__name__, exc)


async def fetch_trend_regime(symbol: str) -> dict[str, Any] | None:
    """Single-row read of ``nt_trend_regime`` for one symbol — the live
    per-tick read both sweep paths use right before applying the trend
    Kelly multiplier. Returns ``None`` on any failure or missing row
    (never raises — callers treat that as "no trend read yet, no-op").
    """
    if not settings.SUPABASE_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_trend_regime"),
                headers=_headers(),
                params={
                    "select": "trend_label,trend_strength",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data[0] if isinstance(data, list) and data else None
    except Exception as exc:
        logger.warning("fetch_trend_regime: %s: read failed: %s: %r", symbol, type(exc).__name__, exc)
        return None


NT_MACRO_EDGE_BY_SYMBOL_COLUMNS: frozenset[str] = frozenset({
    "symbol", "alignment", "n", "win_rate", "avg_rr_winners", "expectancy_r",
    "median_hours_to_resolve", "p75_hours", "p90_hours",
    # Batch C (2026-09-20): stamp every upsert so stale rows are visible
    "computed_at",
})


async def upsert_macro_edge_by_symbol(rows: list[dict[str, Any]]) -> None:
    """Upsert bucket rows into ``nt_macro_edge_by_symbol`` (best-effort).

    Keyed on ``(symbol, alignment)`` — same upsert-on-conflict shape as
    ``upsert_bracket_reachability``/``upsert_calibration_bias_by_bucket``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_MACRO_EDGE_BY_SYMBOL_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_macro_edge_by_symbol: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_macro_edge_by_symbol')}?on_conflict=symbol,alignment",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_macro_edge_by_symbol: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_macro_edge_by_symbol: insert failed: %s", exc)


NT_TREND_EDGE_BY_SYMBOL_COLUMNS: frozenset[str] = frozenset({
    "symbol", "alignment", "strength_tier", "n", "win_rate", "avg_rr_winners",
    "expectancy_r", "median_hours_to_resolve", "p75_hours", "p90_hours",
})


async def upsert_trend_edge_by_symbol(rows: list[dict[str, Any]]) -> None:
    """Upsert bucket rows into ``nt_trend_edge_by_symbol`` (best-effort).

    Keyed on ``(symbol, alignment, strength_tier)`` — same
    upsert-on-conflict shape as ``upsert_macro_edge_by_symbol``, one
    extra key column since ``trend_edge.py`` also buckets by
    `trend_strength` tier, not just alignment.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_TREND_EDGE_BY_SYMBOL_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_trend_edge_by_symbol: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_trend_edge_by_symbol')}?on_conflict=symbol,alignment,strength_tier",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_trend_edge_by_symbol: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_trend_edge_by_symbol: insert failed: %s: %r", type(exc).__name__, exc)


NT_HMM_SHADOW_ACCURACY_COLUMNS: frozenset[str] = frozenset({
    "symbol", "metric", "n", "coverage", "accuracy", "brier_score", "computed_at",
})


async def upsert_hmm_shadow_accuracy(rows: list[dict[str, Any]]) -> None:
    """Upsert rows into ``nt_hmm_shadow_accuracy`` (best-effort).

    Keyed on ``(symbol, metric)`` — same upsert-on-conflict shape as
    ``upsert_macro_edge_by_symbol``.
    """
    if not settings.SUPABASE_URL:
        return

    clean_rows = [_filter_payload(r, NT_HMM_SHADOW_ACCURACY_COLUMNS) for r in rows]
    if not clean_rows:
        logger.debug("upsert_hmm_shadow_accuracy: no rows — skipping")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_url('nt_hmm_shadow_accuracy')}?on_conflict=symbol,metric",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=clean_rows,
            )
            if not resp.is_success:
                logger.error(
                    "upsert_hmm_shadow_accuracy: insert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("upsert_hmm_shadow_accuracy: insert failed: %s", exc)


async def fetch_max_computed_at(table: str, column: str = "computed_at") -> datetime | None:
    """Latest timestamp column for a diagnostic table (best-effort —
    ``None`` on any failure or an empty table).

    Used by the orchestrator's startup catch-up check for fixed-time-of-
    day/week jobs an in-memory APScheduler job store can silently skip
    across restarts (2026-09-01, worklog/
    20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md): ``add_job()`` on a
    fresh scheduler computes the next fire time strictly forward from
    "now," with no memory that today's/this-week's fire time already
    passed while the process was down. Reading the job's own diagnostic
    table for its last real success avoids needing a second, separately-
    mutated piece of state (a config file) just to answer "did this run
    recently."

    ``column`` defaults to ``"computed_at"`` (the original two callers —
    ``nt_macro_regime``/``nt_bracket_reachability`` — both use that name
    unchanged). Generalized 2026-09-05 (worklog
    20260905_EOD_RESOLVER_SCHEDULER_MISS.md) to also cover
    ``nt_signal_sim.resolved_at`` rather than duplicating this whole
    function a third time for one different column name.

    ``order=...desc.nullslast`` (not just ``desc``): caught live while
    building the ``resolved_at`` case — most ``nt_signal_sim`` rows are
    still-pending (``resolved_at IS NULL``), and Postgres/PostgREST's
    default NULL ordering puts NULLs FIRST on a DESC sort, so a plain
    ``desc`` silently returned a pending row's ``None`` instead of the
    latest real resolution. The original two callers' columns are never
    NULL, so this was latent until a nullable column was ever passed.
    """
    if not settings.SUPABASE_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url(table),
                headers=_headers(),
                params={"select": column, "order": f"{column}.desc.nullslast", "limit": "1"},
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        raw = data[0].get(column)
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception as exc:
        logger.warning("fetch_max_computed_at: %s.%s: read failed: %s", table, column, exc)
        return None


async def fetch_light_swept_symbols_since(since: datetime) -> set[str] | None:
    """Symbols with a ``sweep-light`` ``nt_sweep_result`` row at or after
    ``since`` (2026-09-25 light-sweep startup catch-up). ``None`` on any
    failure — the caller treats that as "unknown, don't re-run". The
    timestamp is sent second-truncated with ``Z`` (PostgREST ``+``/``.``
    pitfalls)."""
    if not settings.SUPABASE_URL:
        return None
    try:
        ts = since.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_sweep_result"),
                headers=_headers(),
                params={
                    "select": "symbol",
                    "source": "eq.sweep-light",
                    "sweep_timestamp": f"gte.{ts}",
                    "limit": "1000",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list):
            return None
        return {str(r.get("symbol")) for r in data if r.get("symbol")}
    except Exception as exc:
        logger.warning("fetch_light_swept_symbols_since: read failed: %s", exc)
        return None


NT_MARKOV_SOURCE_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "n_state_disagree", "sum_abs_p_up_diff",
    "last_legacy_state", "last_trend_hmm_state", "updated_at",
})


async def record_markov_shadow_compare(
    symbol: str,
    state_agree: bool,
    p_up_diff: float,
    legacy_state: str | None,
    trend_hmm_state: str | None,
) -> None:
    """Accumulate one tick's legacy-vs-trend_hmm markov comparison into the
    running per-symbol row in ``nt_markov_source_shadow`` (best-effort).

    Fetch-then-upsert rather than a DB-side atomic increment — this is a
    diagnostic counter (informs a future MARKOV_SOURCE_MODE=live decision,
    not a live trading value), so an occasional missed increment from a
    race between concurrent updates to the SAME symbol is an acceptable
    trade-off for not needing a Postgres RPC function.
    """
    if not settings.SUPABASE_URL:
        return
    # 2026-09-23: 5s→15s + one retry; WARNING (diagnostic, miss is OK).
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _url("nt_markov_source_shadow"),
                    headers=_headers(),
                    params={
                        "select": "n_compared,n_state_disagree,sum_abs_p_up_diff",
                        "symbol": f"eq.{symbol}",
                        "limit": "1",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                prev = data[0] if isinstance(data, list) and data else {}

                row = _filter_payload({
                    "symbol": symbol,
                    "n_compared": int(prev.get("n_compared", 0)) + 1,
                    "n_state_disagree": int(prev.get("n_state_disagree", 0)) + (0 if state_agree else 1),
                    "sum_abs_p_up_diff": round(
                        float(prev.get("sum_abs_p_up_diff", 0.0)) + abs(float(p_up_diff)), 6,
                    ),
                    "last_legacy_state": legacy_state,
                    "last_trend_hmm_state": trend_hmm_state,
                    # updated_at (2026-08-27 fix): the column's DEFAULT now()
                    # only applies on INSERT — an upsert-on-conflict never
                    # touches a column absent from the payload, so without
                    # this the timestamp silently froze at first-insert time
                    # forever, undermining the exact "how fresh is this row"
                    # check this table exists to support. Confirmed live:
                    # n_compared kept incrementing for hours while
                    # updated_at stayed pinned to the very first write.
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, NT_MARKOV_SOURCE_SHADOW_COLUMNS)

                resp = await client.post(
                    f"{_url('nt_markov_source_shadow')}?on_conflict=symbol",
                    headers={
                        **_headers(),
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=row,
                )
                if not resp.is_success:
                    logger.warning(
                        "record_markov_shadow_compare: upsert HTTP %d — %s",
                        resp.status_code, resp.text[:300],
                    )
                return
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                continue
    # Diagnostic only (MARKOV_SOURCE_MODE shadow) — a missed increment is
    # acceptable; do not escalate to ERROR.
    logger.warning(
        "record_markov_shadow_compare: failed for %s: %s: %r",
        symbol, type(last_exc).__name__ if last_exc else "unknown", last_exc,
    )


NT_TREND_PROBS_GATE_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "sum_abs_p_regime_diff",
    "last_raw_p_regime", "last_gated_p_regime", "updated_at",
})


async def record_trend_probs_gate_shadow(
    symbol: str,
    raw_p_regime: float,
    gated_p_regime: float,
) -> None:
    """Accumulate one tick's raw-vs-gated p_regime comparison into the
    running per-symbol row in ``nt_trend_probs_gate_shadow`` (best-effort).

    Fetch-then-upsert, same shape as ``record_markov_shadow_compare`` —
    ``updated_at`` included in the payload from the start this time (see
    that function's own follow-up fix for why this matters).
    """
    if not settings.SUPABASE_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_trend_probs_gate_shadow"),
                headers=_headers(),
                params={
                    "select": "n_compared,sum_abs_p_regime_diff",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            prev = data[0] if isinstance(data, list) and data else {}

            row = _filter_payload({
                "symbol": symbol,
                "n_compared": int(prev.get("n_compared", 0)) + 1,
                "sum_abs_p_regime_diff": round(
                    float(prev.get("sum_abs_p_regime_diff", 0.0))
                    + abs(float(raw_p_regime) - float(gated_p_regime)), 6,
                ),
                "last_raw_p_regime": round(float(raw_p_regime), 4),
                "last_gated_p_regime": round(float(gated_p_regime), 4),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, NT_TREND_PROBS_GATE_SHADOW_COLUMNS)

            resp = await client.post(
                f"{_url('nt_trend_probs_gate_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_trend_probs_gate_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_trend_probs_gate_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


NT_REGIME_DEBOUNCE_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "n_compared", "n_differed",
    "last_raw_label", "last_debounced_label", "updated_at",
})


async def record_regime_debounce_shadow(
    symbol: str,
    raw_label: str,
    debounced_label: str,
) -> None:
    """Accumulate one tick's raw-vs-debounced regime_label comparison into
    the running per-symbol row in ``nt_regime_debounce_shadow`` (best-effort).

    Fetch-then-upsert, same shape as ``record_trend_probs_gate_shadow``.
    """
    if not settings.SUPABASE_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_regime_debounce_shadow"),
                headers=_headers(),
                params={
                    "select": "n_compared,n_differed",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            prev = data[0] if isinstance(data, list) and data else {}

            differed = raw_label != debounced_label
            row = _filter_payload({
                "symbol": symbol,
                "n_compared": int(prev.get("n_compared", 0)) + 1,
                "n_differed": int(prev.get("n_differed", 0)) + (1 if differed else 0),
                "last_raw_label": raw_label,
                "last_debounced_label": debounced_label,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, NT_REGIME_DEBOUNCE_SHADOW_COLUMNS)

            resp = await client.post(
                f"{_url('nt_regime_debounce_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_regime_debounce_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_regime_debounce_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


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


def _parse_iso_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (with/without tz, Z suffix) to aware UTC."""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _count_distinct_days(records: list[dict], key: str = "entered_at") -> int:
    """Count distinct UTC calendar dates among ``records[*][key]``.

    Used to detect a clustered burst (many records, all from a narrow
    window) vs. records genuinely spread across the bias window — a burst
    crammed into a few hours should not be trusted the same as the same
    row count spread over a week (2026-08-25, see
    ``EOD_BIAS_MIN_DISTINCT_DAYS``).
    """
    days: set[str] = set()
    for row in records:
        dt = _parse_iso_dt(row.get(key))
        if dt is not None:
            days.add(dt.date().isoformat())
    return len(days)


def _decay_weighted_bias_stats(
    records: list[dict],
    pred_key: str,
    half_life_days: float,
    now: datetime,
) -> tuple[float, float]:
    """Recency-weighted (avg_pred, win_rate) over ``records``.

    Each record is weighted by ``0.5 ** (age_days / half_life_days)``
    instead of counted equally, so a 7-day-old record contributes less than
    a fresh one and bias doesn't swing sharply the instant a bad stretch
    ages past the window boundary (2026-08-25, see
    ``EOD_BIAS_DECAY_HALFLIFE_DAYS``). Falls back to unweighted (weight=1)
    for any record whose ``entered_at`` is unparseable, rather than
    dropping it.
    """
    total_weight = 0.0
    weighted_pred = 0.0
    weighted_wins = 0.0
    for row in records:
        dt = _parse_iso_dt(row.get("entered_at"))
        if dt is not None:
            age_days = max((now - dt).total_seconds() / 86400.0, 0.0)
            weight = 0.5 ** (age_days / max(half_life_days, 1e-6))
        else:
            weight = 1.0
        pred = row.get(pred_key)
        if pred is None:
            pred = row.get("predicted_p_win", 0.5) or 0.5
        total_weight += weight
        weighted_pred += weight * float(pred)
        if row.get("outcome") == "win":
            weighted_wins += weight
    if total_weight <= 0:
        return 0.5, 0.0
    return weighted_pred / total_weight, weighted_wins / total_weight


def _group_into_episodes(
    records: list[dict], entered_key: str = "entered_at", closed_key: str = "closed_at",
) -> list[list[dict]]:
    """Group calibration records into independent EPISODES by merging
    overlapping ``[entered_at, closed_at]`` windows (2026-09-08, worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md — replaces the
    2026-08-25 distinct-calendar-days discount below).

    Two records whose holding windows overlap in time are riding (at
    least partially) the same underlying price move and are NOT
    independent evidence, regardless of how many raw rows exist between
    them — confirmed live: USDCHF had 294 raw rows entered every ~5
    minutes over ~68 continuous hours, all resolving the same way,
    because each new "record" was really just a fresh snapshot of the
    same still-unresolved move. The distinct-calendar-days discount this
    replaces (``EOD_BIAS_MIN_DISTINCT_DAYS``) missed this case entirely:
    the burst happened to touch exactly 3 calendar dates, which equalled
    its own default threshold and disabled the discount.

    Standard interval-merge, sorted by ``entered_key``: a new episode
    starts whenever a record's ``entered_at`` falls strictly after the
    running maximum ``closed_at`` seen so far among all earlier records
    (NOT just the immediately preceding one — an early long-hold record
    can still overlap a later record even if a short-hold record in
    between has already closed). Records with an unparseable/missing
    ``entered_at`` are dropped (can't be ordered). A missing/unparseable
    ``closed_at`` falls back to ``entered_at`` itself (zero-duration —
    the conservative assumption when hold duration is unknown: it can
    only merge INTO a later record's episode, never absorb one).
    """
    parsed: list[tuple[datetime, datetime, dict]] = []
    for row in records:
        entered = _parse_iso_dt(row.get(entered_key))
        if entered is None:
            continue
        closed = _parse_iso_dt(row.get(closed_key)) or entered
        parsed.append((entered, closed, row))
    parsed.sort(key=lambda t: t[0])

    episodes: list[list[dict]] = []
    running_max_closed: datetime | None = None
    for entered, closed, row in parsed:
        if running_max_closed is None or entered > running_max_closed:
            episodes.append([])
        episodes[-1].append(row)
        running_max_closed = closed if running_max_closed is None else max(running_max_closed, closed)
    return episodes


def _episode_summaries(episodes: list[list[dict]]) -> list[dict]:
    """Collapse each episode's raw rows into ONE representative
    pseudo-record: ``{entered_at, predicted_p_win, predicted_p_win_raw,
    win_fraction, n_raw_present}``.

    Batch B (2026-09-16): ``predicted_p_win_raw`` is averaged only over
    rows that actually carry a non-null raw value — it is NOT fallen back
    to enforced ``predicted_p_win``. ``n_raw_present`` lets callers decide
    whether the episode has enough raw evidence. Enforced averages still
    use all rows.
    """
    summaries: list[dict] = []
    for ep in episodes:
        preds = [float(r.get("predicted_p_win", 0.5) or 0.5) for r in ep]
        preds_raw = [
            float(r["predicted_p_win_raw"])
            for r in ep
            if r.get("predicted_p_win_raw") is not None
        ]
        wins = [1.0 if r.get("outcome") == "win" else 0.0 for r in ep]
        entered_ats = [r.get("entered_at") for r in ep if r.get("entered_at")]
        summaries.append({
            "entered_at": min(entered_ats) if entered_ats else None,
            "predicted_p_win": sum(preds) / len(preds) if preds else 0.5,
            "predicted_p_win_raw": (
                sum(preds_raw) / len(preds_raw) if preds_raw else None
            ),
            "win_fraction": sum(wins) / len(wins) if wins else 0.0,
            "n_raw_present": len(preds_raw),
            "n_rows": len(ep),
        })
    return summaries


def _decay_weighted_episode_stats(
    episode_summaries: list[dict], pred_key: str, half_life_days: float, now: datetime,
    *,
    skip_null_pred: bool = False,
) -> tuple[float, float]:
    """Recency-weighted (avg_pred, win_rate) over EPISODE summaries.

    When ``skip_null_pred`` is True, episodes whose ``pred_key`` is None
    are excluded from the weighted average (and from the returned win_rate
    so numerator/denominator stay on the same population). Batch B: used
    for raw-bias so missing raw never silently becomes 0.5/enforced.
    """
    total_weight = 0.0
    weighted_pred = 0.0
    weighted_wins = 0.0
    for ep in episode_summaries:
        pred = ep.get(pred_key)
        if skip_null_pred and pred is None:
            continue
        if pred is None:
            pred = 0.5
        dt = _parse_iso_dt(ep.get("entered_at"))
        if dt is not None:
            age_days = max((now - dt).total_seconds() / 86400.0, 0.0)
            weight = 0.5 ** (age_days / max(half_life_days, 1e-6))
        else:
            weight = 1.0
        total_weight += weight
        weighted_pred += weight * float(pred)
        weighted_wins += weight * float(ep.get("win_fraction", 0.0))
    if total_weight <= 0:
        return 0.5, 0.0
    return weighted_pred / total_weight, weighted_wins / total_weight


TIERED_CALIBRATION_VIEW = "v_calibration_records_tiered"  # migration 171
_EVIDENCE_TIERS = frozenset({"qualified", "suppressed", "gate_fail", "unknown"})


def _apply_evidence_tiers(params: dict[str, str], tiers: tuple[str, ...] | None) -> None:
    """Add the PostgREST ``evidence_tier=in.(...)`` filter (migration 171).
    Unknown names are dropped; an empty result after filtering is a
    programming error, so it raises instead of silently reading every tier."""
    if not tiers:
        return
    clean = [t for t in tiers if t in _EVIDENCE_TIERS]
    if not clean:
        raise ValueError(f"evidence_tiers has no valid tier: {tiers!r}")
    params["evidence_tier"] = f"in.({','.join(clean)})"


async def fetch_calibration_bias(
    symbol: str,
    venue: str | None = None,
    *,
    window_days: int | None = None,
    min_records: int | None = None,
    min_episodes: int | None = None,
    evidence_tiers: tuple[str, ...] | None = None,
) -> dict[str, float | str] | None:
    """Query nt_calibration_records and return calibration bias.

    ``evidence_tiers`` (2026-09-26, Kairos V3 §14.3, migration 171): when
    set, reads ``v_calibration_records_tiered`` instead of the table and
    keeps only records whose producing signal's tier is in the tuple, e.g.
    ``("qualified", "suppressed")``. Every other step (window, episodes,
    decay, thresholds, classification) is identical. ``None`` (default) =
    the table, every tier — unchanged behaviour for every existing caller.

    Returns ``{bias: float, status: str, n: int, raw_n: int,
    distinct_days: int, win_rate: float, avg_pred: float}`` or ``None`` on
    failure, when fewer than ``min_records`` raw records exist in the
    window, or when they collapse into fewer than ``min_episodes``
    independent episodes (see below — either way, not enough real
    evidence to trust a verdict). If ``venue`` is provided, filters by
    both symbol AND venue (exchange).

    ROLLING WINDOW (2026-08-13, intraday cadence plan): the bias is computed
    over records with ``entered_at`` within the last ``window_days`` days
    (default ``settings.EOD_BIAS_WINDOW_DAYS`` = 7) instead of all-time, so a
    regime shift moves the bias in hours, not days. Pass ``window_days=0``
    for the legacy all-time behaviour. ``min_records`` defaults to
    ``settings.EOD_BIAS_MIN_RECORDS`` (20) — a cheap raw-count floor before
    bothering to compute anything.

    EPISODE COUNTING + DECAY WEIGHTING (2026-09-08, worklog
    20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md): a hard N-day cutoff
    has no protection against a single anomalous burst dominating the
    window — confirmed live TWICE now: USDCAD's ``stagnation_kill_switch``
    (2026-08-25) was driven by n=69 records all written within one 6-hour
    stretch; USDCHF's stuck OVERCONFIDENT penalty (2026-09-08) was driven
    by n=294 records that were really ONE continuous ~68-hour episode
    (entries every ~5 min, each riding the same still-unresolved price
    move) — evading the 2026-08-25 fix (a distinct-calendar-days discount)
    because the burst happened to span exactly its own default threshold
    of 3 calendar dates. Two mitigations, both now episode-based:
      1. ``_group_into_episodes``: records whose ``[entered_at, closed_at]``
         windows overlap in time are merged into ONE episode — the actual
         count of independent evidence, not a proxy for it.
      2. Recency decay (``_decay_weighted_episode_stats``): each EPISODE's
         contribution to ``avg_pred``/``win_rate`` is weighted by
         ``0.5 ** (age_days / EOD_BIAS_DECAY_HALFLIFE_DAYS)`` — same decay
         shape as before, now applied post-episode-collapse so a
         correlated burst contributes one weighted sample, not one per
         raw row within it.
    ``n`` returned is the real episode count (not a scaled approximation);
    ``raw_n``/``distinct_days`` are kept for observability only.
    ``min_episodes`` defaults to ``get_calibration_bias_min_episodes()``
    (config/feature_flags.yaml, default 2).
    """
    if not settings.SUPABASE_URL:
        return None

    from regime_platform.core.feature_flags import get_calibration_bias_min_episodes

    win_days = window_days if window_days is not None else getattr(settings, "EOD_BIAS_WINDOW_DAYS", 7)
    min_n = min_records if min_records is not None else getattr(settings, "EOD_BIAS_MIN_RECORDS", 20)
    half_life = getattr(settings, "EOD_BIAS_DECAY_HALFLIFE_DAYS", 3.0)
    min_eps = min_episodes if min_episodes is not None else get_calibration_bias_min_episodes()

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,outcome,entered_at,closed_at",
        "symbol": f"eq.{symbol}",
    }
    _apply_evidence_tiers(params, evidence_tiers)
    if evidence_tiers:
        url = _url(TIERED_CALIBRATION_VIEW)
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

    raw_n = len(data)
    distinct_days = _count_distinct_days(data)
    episodes = _group_into_episodes(data)
    n_episodes = len(episodes)
    if n_episodes < min_eps:
        return None
    episode_summaries = _episode_summaries(episodes)
    now = datetime.now(timezone.utc)
    avg_pred, win_rate = _decay_weighted_episode_stats(
        episode_summaries, "predicted_p_win", half_life, now,
    )
    bias = round(avg_pred - win_rate, 4)

    # One arbiter for "is this symbol calibrated" (2026-09-09, worklog
    # 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16):
    # classify via eod_correction._classify_bias -- the exact,
    # registry-tunable function that actually drives live enforcement --
    # instead of a hardcoded 0.05 threshold computed independently here.
    # Local import: eod_correction.py imports THIS module's functions the
    # same way (also locally, inside run_eod_correction), so neither
    # module can import the other at module load time -- both already
    # rely on function-local imports to avoid a circular dependency.
    from regime_platform.sweep_orchestrator.eod_correction import _classify_bias
    status = _classify_bias(bias)

    return {
        "bias": bias,
        "status": status,
        "n": n_episodes,
        "raw_n": raw_n,
        "distinct_days": distinct_days,
        "win_rate": round(win_rate, 4),
        "avg_pred": round(avg_pred, 4),
    }


async def fetch_calibration_bias_raw(
    symbol: str,
    venue: str | None = None,
    *,
    window_days: int | None = None,
    min_records: int | None = None,
    min_episodes: int | None = None,
    evidence_tiers: tuple[str, ...] | None = None,
) -> dict[str, float | str | int] | None:
    """Query nt_calibration_records and return calibration bias computed from
    ``predicted_p_win_raw`` (the PRE-enforcement p_win) rather than the
    enforced ``predicted_p_win``.

    ``evidence_tiers``: same as ``fetch_calibration_bias`` (migration 171
    view, tier filter; ``None`` = unchanged).

    This is the raw-model-bias variant used by the 4h intraday correction
    pass (``run_eod_correction(raw_bias_mode=True)``). It separates model
    miscalibration from enforcement compensation — the enforced p_win has
    been shrunk toward 0.5 by the Bayesian penalty, so reading it directly
    would measure the correction, not the model error.

    Batch B (2026-09-16): **no silent fallback to enforced p_win.** Episodes
    without any non-null ``predicted_p_win_raw`` are excluded from the raw
    average and from raw win_rate. If fewer than ``min_episodes`` remain,
    returns ``None`` (insufficient raw evidence). When
    ``settings.NOBLE_TRADER_CALIBRATION_RAW_REQUIRED`` is False, the old
    per-row fallback is restored for emergency use only.

    Returns the same shape as ``fetch_calibration_bias`` plus
    ``avg_pred_raw`` and ``raw_episodes``:
    ``{bias, status, n, raw_n, distinct_days, win_rate, avg_pred, avg_pred_raw, raw_episodes}``.

    The bias thresholds here are wider (``EOD_BIAS_RAW_OVERCONFIDENT`` /
    ``EOD_BIAS_RAW_UNDERCONFIDENT``) because raw model output is naturally
    more extreme. Classification is done here, via
    ``eod_correction._classify_bias_raw`` (2026-09-09) — the same function
    every other consumer of a raw-bias verdict calls, so this can never
    silently disagree with what's actually enforced.

    EPISODE COUNTING + DECAY WEIGHTING (2026-09-08): same mitigations as
    ``fetch_calibration_bias`` — see that function's docstring. This is the
    variant the 4h intraday correction pass (and therefore the stagnation
    streak/kill-switch logic) actually reads, so the fix applies to exactly
    the path that misfired for both USDCAD (2026-08-25) and USDCHF
    (2026-09-08).
    """
    if not settings.SUPABASE_URL:
        return None

    from regime_platform.core.feature_flags import get_calibration_bias_min_episodes

    win_days = window_days if window_days is not None else getattr(settings, "EOD_BIAS_WINDOW_DAYS", 7)
    min_n = min_records if min_records is not None else getattr(settings, "EOD_BIAS_MIN_RECORDS", 20)
    half_life = getattr(settings, "EOD_BIAS_DECAY_HALFLIFE_DAYS", 3.0)
    min_eps = min_episodes if min_episodes is not None else get_calibration_bias_min_episodes()

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,predicted_p_win_raw,outcome,entered_at,closed_at",
        "symbol": f"eq.{symbol}",
    }
    _apply_evidence_tiers(params, evidence_tiers)
    if evidence_tiers:
        url = _url(TIERED_CALIBRATION_VIEW)
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

    raw_n = len(data)
    distinct_days = _count_distinct_days(data)
    episodes = _group_into_episodes(data)
    n_episodes = len(episodes)
    if n_episodes < min_eps:
        return None
    episode_summaries = _episode_summaries(episodes)
    now = datetime.now(timezone.utc)
    raw_required = bool(getattr(settings, "NOBLE_TRADER_CALIBRATION_RAW_REQUIRED", True))
    skip_null = raw_required
    if not skip_null:
        # Emergency opt-out: restore historical per-row stand-in
        for ep in episode_summaries:
            if ep.get("predicted_p_win_raw") is None:
                ep["predicted_p_win_raw"] = ep.get("predicted_p_win", 0.5)

    raw_episodes = [
        ep for ep in episode_summaries
        if ep.get("predicted_p_win_raw") is not None
    ]
    if skip_null and len(raw_episodes) < min_eps:
        logger.info(
            "fetch_calibration_bias_raw %s: only %d/%d episodes carry raw p_win "
            "(min_episodes=%d) — returning None (no enforced fallback)",
            symbol, len(raw_episodes), n_episodes, min_eps,
        )
        return None

    avg_pred_enforced, _ = _decay_weighted_episode_stats(
        episode_summaries, "predicted_p_win", half_life, now,
    )
    avg_pred_raw, win_rate = _decay_weighted_episode_stats(
        episode_summaries, "predicted_p_win_raw", half_life, now,
        skip_null_pred=skip_null,
    )
    # Bias is computed from the RAW (pre-enforcement) p_win so it reflects
    # the model's true miscalibration, not the enforcement compensation.
    bias = round(avg_pred_raw - win_rate, 4)

    from regime_platform.sweep_orchestrator.eod_correction import _classify_bias_raw
    status = _classify_bias_raw(bias)

    return {
        "bias": bias,
        "status": status,
        "n": len(raw_episodes) if skip_null else n_episodes,
        "raw_n": raw_n,
        "distinct_days": distinct_days,
        "win_rate": round(win_rate, 4),
        "avg_pred": round(avg_pred_enforced, 4),
        "avg_pred_raw": round(avg_pred_raw, 4),
        "raw_episodes": len(raw_episodes),
    }


# _clear_stale_eod_actions -- REMOVED 2026-09-09 (worklog
# 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16, item #6).
# Deleted enforcement-state rows from nt_eod_actions at the start of each
# 4h intraday cycle to reset stale state before re-evaluating. That whole
# "clear, then maybe re-add" dance is now unnecessary: upsert_calibration_state
# fully overwrites the single current-state row every cycle a verdict is
# computed (see eod_correction.run_eod_correction's CALIBRATED and
# OVERCONFIDENT/UNDERCONFIDENT branches), and the one case that used to
# need an explicit clear even without a computed verdict -- insufficient
# calibration data -- now gets its own explicit upsert_calibration_state
# (action="none") call at that exact point instead. nt_eod_actions itself
# is untouched by any of this and no longer has anything deleted from it
# for enforcement purposes -- it is a genuinely permanent audit log now.


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
    symbol: str, action: str = "bayesian_penalty", limit: int = 8,
    source: Optional[str] = None,
) -> list[float]:
    """Return the most recent ``limit`` bias values (newest first) written
    for ``action`` on ``symbol`` in ``nt_eod_actions``.

    Used by the stagnation breaker (``eod_correction._stagnation_streak``,
    2026-08-22 audit fix #3c) to detect a symbol whose raw bias has stopped
    moving across consecutive 4h correction cycles — the audit trail
    (``nt_eod_actions``) already carries this history, so no separate
    accumulator/counter table is needed.

    ``source``, when given, additionally filters on ``detail->>source``
    (2026-08-24 fix) — ``drift_monitor.py`` writes its OWN ``bayesian_penalty``
    rows (different window/threshold, tagged ``detail.source="drift_monitor"``)
    to the same table. Without this filter, those rows interleave with
    ``eod_correction.py``'s own (tagged ``detail.source="eod_correction"``)
    and dilute the "same 4h cycle, unmoved" signal the stagnation streak is
    meant to measure. Rows written before this fix shipped have no
    ``detail.source`` key at all and won't match either filter value — a
    one-time, acceptable reset of accumulated streak history, not a bug.

    Best-effort: returns ``[]`` on any failure or when Supabase is not
    configured (never raises).
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return []
    url = _url("nt_eod_actions")
    params: dict[str, str] = {
        "select": "bias,ts",
        "symbol": f"eq.{symbol}",
        "action": f"eq.{action}",
        "order": "ts.desc",
        "limit": str(int(limit)),
    }
    if source:
        params["detail->>source"] = f"eq.{source}"
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


# ── Calibration current-state (2026-09-09, worklog
# 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16, item #6:
# nt_eod_actions state/history split) ───────────────────────────────────
#
# ``nt_eod_actions`` stays exactly what it always was: a permanent AUDIT
# log, multiple writers (eod_correction nightly/intraday, drift_monitor,
# orchestrator._write_regime_shift_alert), multiple readers (including
# _stagnation_streak's own history lookup above via
# fetch_recent_action_bias) — nothing about that role changes here.
#
# ``nt_symbol_calibration_state`` is the new, separate answer to a
# DIFFERENT question: "what is the CURRENTLY enforceable calibration
# verdict for symbol X, right now." One row per symbol, one writer.
# ``upsert_calibration_state`` below is THE ONLY function anywhere in
# this codebase permitted to write to this table — every verdict-
# computing pass (eod_correction nightly, eod_correction intraday,
# drift_monitor) calls INTO it rather than writing directly. This is
# what actually prevents the clashing/contamination the old design was
# vulnerable to: not locking or precedence math, but the simple fact
# that there is exactly one code path capable of performing the write,
# so every writer produces a row of the same consistent shape, and a
# reader never has to disambiguate between rows from different sources
# — there is only ever the one, current row.


async def upsert_calibration_state(
    symbol: str,
    *,
    action: str,
    bias: Optional[float],
    status: Optional[str],
    n_records: Optional[int],
    source: str,
    detail: Optional[dict[str, Any]] = None,
    kelly_reduction_override: Optional[float] = None,
    stagnation_tier: int = 0,
) -> bool:
    """THE SOLE WRITER to ``nt_symbol_calibration_state`` — see module
    note above. Every caller passes its own ``source`` tag
    (``"eod_correction_nightly"``, ``"eod_correction_intraday"``, or
    ``"drift_monitor"``) purely for observability; this function does not
    arbitrate between sources — each caller already decides FOR ITSELF
    whether to write this cycle (e.g. drift_monitor's own "don't churn"
    freshness guard, unchanged), so by the time this is called, the
    caller's verdict is authoritative and simply overwrites the row.

    ``action`` must be one of ``"none"`` (no active penalty — CALIBRATED,
    or a cycle reset when calibration data is insufficient),
    ``"bayesian_penalty"`` (the canonical name for an active shrink
    penalty — enforcement treats ``bayesian_penalty``/``kelly_reduction``/
    ``emergency_heavy_sweep`` identically, see
    ``calibration_enforcement.apply_calibration_adjustment``; those three
    distinct labels still get written to ``nt_eod_actions`` for audit,
    since ``emergency_heavy_sweep`` also drives the SAME-CYCLE, in-memory
    emergency-sweep dispatch in ``orchestrator.py`` — a different
    consumer entirely, unaffected by this table), or
    ``"stagnation_kill_switch"`` (Tier 2 hard block).

    Returns ``True`` on success, ``False`` on any failure — best-effort,
    never raises (a failed state write should not break the sweep tick;
    the next cycle's write will self-heal it).
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return False
    row = {
        "symbol": symbol,
        "action": action,
        "bias": bias,
        "status": status,
        "n_records": n_records,
        "kelly_reduction_override": kelly_reduction_override,
        "stagnation_tier": stagnation_tier,
        "source": source,
        "detail": detail or {},
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        url = _url("nt_symbol_calibration_state") + "?on_conflict=symbol"
        headers = {**_headers(), "Prefer": "resolution=merge-duplicates"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=headers, json=row)
            if resp.status_code not in (200, 201, 204):
                logger.warning(
                    "upsert_calibration_state %s: HTTP %d — %s",
                    symbol, resp.status_code, resp.text[:200],
                )
                return False
            return True
    except Exception as exc:
        logger.warning("upsert_calibration_state %s failed: %s", symbol, exc)
        return False


async def fetch_calibration_state(symbol: str) -> dict[str, Any] | None:
    """Fetch ``symbol``'s single current-state row from
    ``nt_symbol_calibration_state``. Returns ``None`` if the symbol has
    never had a verdict computed, or on any failure — never raises.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None
    try:
        url = _url("nt_symbol_calibration_state")
        params = {"symbol": f"eq.{symbol}", "select": "*", "limit": "1"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_calibration_state %s failed: %s: %r", symbol, type(exc).__name__, exc)
        return None
    if not isinstance(data, list) or len(data) == 0:
        return None
    return data[0]


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


async def fetch_last_full_heavy_sweep_ts(symbol: str) -> Optional[datetime]:
    """Timestamp of the most recent COMPLETED heavy sweep for ``symbol``
    (``source='sweep-heavy'`` rows in ``nt_sweep_result``), regardless of
    what triggered it — the regular daily cron or an emergency dispatch.

    Used by ``orchestrator._execute_emergency_heavy_sweeps`` (2026-09-09,
    worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16)
    as a durable, source-agnostic cooldown check: reconciling the
    emergency-heavy-sweep dispatch against the now-unconditional daily
    cadence found the nightly path had NO cooldown at all (could
    re-trigger a full re-optimization on the same day's data every
    single night for a chronically-biased symbol) and the intraday path's
    cooldown lived only in an in-memory dict, tracking emergency sweeps
    only — invisible to the regular cron's own completions, and wiped on
    every Studio restart. This function replaces both with one real
    question: "has ANY heavy sweep completed for this symbol recently,
    from any source" — not a resurrection of the removed TimesFM-gate
    force-refresh mechanism (SS13), a structurally different use: that one
    used staleness to override a decision that shouldn't have existed;
    this one uses recency to avoid two heavy sweeps doing the same
    90-day-window optimization twice in one day.

    Returns ``None`` if this symbol has never had a completed heavy
    sweep, or on any failure — never raises. Callers should treat
    ``None`` as "no prior sweep, nothing to cool down against."
    """
    try:
        url = _url("nt_sweep_result")
        params = {
            "select": "sweep_timestamp",
            "symbol": f"eq.{symbol}",
            "source": "eq.sweep-heavy",
            "order": "sweep_timestamp.desc",
            "limit": "1",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_last_full_heavy_sweep_ts %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None
    return _parse_iso_dt(data[0].get("sweep_timestamp"))


async def fetch_latest_qualified_signal(symbol: str, direction: str) -> dict[str, Any] | None:
    """Most recent QUALIFIED ``nt_signal_sim`` row for ``(symbol, direction)``.

    Cooldown-restart fix (2026-09-08, worklog
    20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md, user directive
    "after a sl/tp hits > the cooldown should restart"): before letting a
    new signal qualify, the orchestrator's cooldown gate needs to know
    whether the LAST qualified signal in this same direction has actually
    resolved yet (TP/SL hit, or expired) rather than trusting elapsed
    wall-clock time alone. This returns the raw row — ``ts``,
    ``entry_price``, ``stop_loss``, ``take_profit``, ``signal``,
    ``outcome``, ``exit_ts``, ``resolved_at`` — so the caller can apply
    ``eod_resolver.resolve_signal_outcome``'s own TP/SL-crossing logic for
    a live (pre-batch-resolution) check.

    Best-effort: returns ``None`` on any failure or when no qualified
    signal exists yet for this pair — the caller must fall back to the
    existing wall-clock-only cooldown behavior in that case, never raise.
    """
    try:
        url = _url("nt_signal_sim")
        params = {
            "select": "signal_id,ts,entry_price,stop_loss,take_profit,signal,outcome,exit_ts,resolved_at",
            "symbol": f"eq.{symbol}",
            "signal": f"eq.{direction}",
            "qualified": "eq.true",
            "order": "ts.desc",
            "limit": "1",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning(
            "fetch_latest_qualified_signal %s/%s failed: %s: %r",
            symbol, direction, type(exc).__name__, exc,
        )
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None

    return data[0]


# ─── Expiry-rate alert (priority #3, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md) ──


async def fetch_symbol_scorecard_7d() -> list[dict[str, Any]]:
    """Read ``v_symbol_signal_scorecard_7d`` (migration 140) — the rolling
    7-day per-symbol W/L/PnL scorecard. Used by
    ``expiry_rate_alert.check_expiry_rate_alerts()``. Returns ``[]`` on any
    failure (never raises)."""
    if not settings.SUPABASE_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("v_symbol_signal_scorecard_7d"),
                headers=_headers(),
                params={"select": "symbol,resolved,expired,expired_pct"},
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("fetch_symbol_scorecard_7d failed: %s", exc)
        return []


async def fetch_expiry_rate_alert_state(symbol: str) -> Optional[dict[str, Any]]:
    """Last-alert state for ``symbol`` from ``nt_expiry_rate_alerts``
    (migration 141), or ``None`` if never alerted / on failure."""
    if not settings.SUPABASE_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                _url("nt_expiry_rate_alerts"),
                headers=_headers(),
                params={
                    "select": "symbol,last_alert_at,expired_pct,resolved_n",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("fetch_expiry_rate_alert_state %s failed: %s", symbol, exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    return data[0]


NT_EXPIRY_RATE_ALERTS_COLUMNS: frozenset[str] = frozenset({
    "symbol", "last_alert_at", "expired_pct", "resolved_n", "updated_at",
})


async def record_expiry_rate_alert(symbol: str, expired_pct: float, resolved_n: int) -> None:
    """Upsert ``nt_expiry_rate_alerts`` after sending an alert for
    ``symbol`` — same fetch-free upsert-on-conflict shape as
    ``record_heavy_sweep_deadline_aware_shadow`` (best-effort, never
    raises)."""
    if not settings.SUPABASE_URL:
        return
    try:
        row = _filter_payload({
            "symbol": symbol,
            "last_alert_at": datetime.now(timezone.utc).isoformat(),
            "expired_pct": round(float(expired_pct), 2),
            "resolved_n": int(resolved_n),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, NT_EXPIRY_RATE_ALERTS_COLUMNS)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_url('nt_expiry_rate_alerts')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_expiry_rate_alert: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_expiry_rate_alert: failed for %s: %s: %r", symbol, type(exc).__name__, exc)


NT_SIGNAL_STREAM_META_SHADOW_COLUMNS: frozenset[str] = frozenset({
    "symbol", "state", "coherent_prob", "noisy_prob", "ticks_in_current_state",
    "since_ts", "n_transitions_total", "n_ticks_fit", "updated_at",
})


async def record_signal_stream_meta_shadow(symbol: str, result: dict[str, Any]) -> None:
    """Upsert the meta-regime model's current read for ``symbol`` into
    ``nt_signal_stream_meta_shadow`` (migration 147) — shadow tracking,
    worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §11
    (direct response to "what happened to all the shadow mode tracking" —
    this is the table that was missing). ``result`` is whatever
    ``meta_regime_model.update_shadow_for_symbol`` returns (a merge of
    ``predict_latest``'s and ``summarize_since_last_transition``'s output
    plus ``n_ticks_fit``). Best-effort, never raises."""
    if not settings.SUPABASE_URL:
        return
    try:
        row = _filter_payload({
            "symbol": symbol,
            "state": result.get("state"),
            "coherent_prob": result.get("coherent_prob"),
            "noisy_prob": result.get("noisy_prob"),
            "ticks_in_current_state": result.get("ticks_in_current_state"),
            "since_ts": result.get("since_ts"),
            "n_transitions_total": result.get("n_transitions_total"),
            "n_ticks_fit": result.get("n_ticks_fit"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, NT_SIGNAL_STREAM_META_SHADOW_COLUMNS)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_url('nt_signal_stream_meta_shadow')}?on_conflict=symbol",
                headers={
                    **_headers(),
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=row,
            )
            if not resp.is_success:
                logger.error(
                    "record_signal_stream_meta_shadow: upsert HTTP %d — %s",
                    resp.status_code, resp.text[:300],
                )
    except Exception as exc:
        logger.error("record_signal_stream_meta_shadow: failed for %s: %s: %r", symbol, type(exc).__name__, exc)
