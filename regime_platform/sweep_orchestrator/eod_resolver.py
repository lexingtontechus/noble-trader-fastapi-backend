"""
EOD Signal Resolver — forward-resolution of pending nt_signal_sim rows
═══════════════════════════════════════════════════════════════════════

The signal lookback feature records every qualified sweep signal into
``nt_signal_sim`` (signal_id, symbol, venue, ts, signal, entry/stop/tp,
brick params, p_win, regime context, …) with ``outcome IS NULL`` at write
time. This module is the EOD backfill pass that *forward-resolves* those
pending rows using post-entry TradingView Data API (TDVA) 1h bars:

  • **Bars** — `LIGHT_BARS` (200) post-entry 1h closes per symbol via
    ``fetch_candles_for_symbols`` (the pipeline's standard light-sweep
    fetcher; TDVA is the sole data source after Phase B).
  • **Expiry** — a signal that has not touched either level within
    `EXPIRY_BARS` (48) post-entry 1h bars is marked ``expired``. A signal
    with FEWER than 48 post-entry bars available (entered recently — this
    pass runs once daily, so most pending signals are younger than 48h)
    is left pending instead of force-expired early (2026-08-23 fix — see
    ``_resolve_one``'s minimum-age gate and
    ``worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md`` root cause 3).
    A genuine TP/SL touch still resolves immediately regardless of bar
    count; only a would-be "expired" verdict is deferred.
  • **Resolution** — a *close-walk*: for a BUY, ``tp_hit`` when a close
    ``>= take_profit`` (checked first) and ``sl_hit`` when a close
    ``<= stop_loss``; the bar index that comes first wins. For a SELL
    (TP *below* entry), ``tp_hit`` when a close ``<= take_profit`` and
    ``sl_hit`` when a close ``>= stop_loss``. The fetcher returns closes
    only (no high/low), so this is the pipeline's conservative
    convention — if the fetcher ever gains OHLC, prefer
    ``high >= TP / low <= SL`` (best-effort close-walk is fine today).
  • **Outcomes written back** — ``outcome`` (``tp_hit``/``sl_hit``/
    ``expired``), ``exit_price`` (the triggering close), ``exit_ts``
    (``signal.ts + hold_bars`` hours — *approximate*: the fetcher drops
    bar timestamps, so hold duration is derived from bar count, not wall
    clock), ``hold_bars``, ``mfe``/``mae`` (max favorable/adverse close
    excursion, in price units), ``pnl_bricks``/``pnl_dollars`` and
    ``resolved_at``.
  • **Calibration records** — every ``tp_hit``/``sl_hit`` outcome also
    inserts a ``nt_calibration_records`` row (``win``/``loss``) so the
    per-symbol calibration bias (``fetch_calibration_bias``) reflects
    simulated signal outcomes. **Expired rows get ``outcome='expired'``
    in nt_signal_sim but are deliberately SKIPPED for calibration** —
    a timeout is neither a win nor a loss, and counting it as a loss
    would systematically understate the model's p_win.

Design rules: best-effort only — every per-signal/per-symbol failure is
caught, logged and counted in the summary; this module never raises out
of ``resolve_pending_signals``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.price_fetcher import (
    LIGHT_CANDLE_BARS,
    fetch_candles_for_symbols,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _insert_row,
    _url,
)
from regime_platform.sweep_orchestrator import settlement_contract as sc

logger = logging.getLogger("regime.sweep.eod_resolver")

# ─── Constants ────────────────────────────────────────────────────

EXPIRY_BARS = sc.EXPIRY_BARS   # 48 post-entry 1h bars before a signal is 'expired'
LIGHT_BARS = LIGHT_CANDLE_BARS  # 200 — post-entry bars fetched per symbol
INTERVAL = "1h"           # sweep bar interval used for resolution

# Local whitelist for nt_signal_sim payloads. The parallel agent owns the
# canonical NT_SIGNAL_SIM_COLUMNS in supabase_client.py; if it is not yet
# present at import time we fall back to this exact local set.
NT_SIGNAL_SIM_COLUMNS: frozenset[str] = frozenset({
    "signal_id", "symbol", "venue", "ts", "signal", "entry_price",
    "stop_loss", "take_profit", "brick_size", "sl_bricks", "tp_bricks",
    "p_win", "ev", "regime", "regime_conf", "source", "qualified",
    "outcome", "exit_price", "exit_ts", "hold_bars", "mfe", "mae",
    "pnl_bricks", "pnl_dollars", "resolved_at",
    # drift_*_pct (migration 121, Phase 2): direction-relative post-entry
    # price drift at fixed horizons, independent of trade outcome.
    # 12h/48h added 2026-09-24 (Kairos v3 P0.3, migration 167).
    "drift_1h_pct", "drift_4h_pct", "drift_12h_pct", "drift_24h_pct", "drift_48h_pct",
})

# Columns of nt_calibration_records we write (mirrors renko/trade_journal.py
# + migration 031 venue column).
CALIBRATION_RECORD_COLUMNS: frozenset[str] = frozenset({
    "id", "symbol", "venue", "predicted_p_win", "outcome", "aggression",
    "entry_price", "exit_price", "entered_at", "closed_at",
    "actual_pnl", "error_source",
    # 2026-08-13: pre-enforcement p_win (carried from the sim capture, nullable)
    # + Brier score (predicted-vs-outcome MSE). Both columns exist in the table.
    "predicted_p_win_raw", "calibration_brier",
    # signal_id (migration 121, Phase 2): FK back to the nt_signal_sim row
    # this record was written from — closes the join gap that blocked
    # bracket-reachability-aware calibration analysis. Nullable; rows
    # written before 2026-08-25 predate this column and stay unlinked.
    "signal_id",
})


# ─── Pure resolution logic (network-free, unit-tested) ────────────

def _parse_utc_dt(value: Any) -> datetime | None:
    """Parse ISO-8601 (with/without tz, Z suffix) or epoch into aware UTC."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def anchor_post_entry_closes(
    closes: Sequence[float],
    signal_ts: Any,
    fetch_ts: Any,
    bar_hours: float = 1.0,
) -> list[float] | None:
    """Slice ``closes`` to the post-entry window for one signal.

    The fetched array is ASCENDING (oldest → newest; the 2026-08-11 TDVA
    order fix guarantees this). The signal was captured ``signal_ts``;
    the fetch ran at ``fetch_ts``. Assuming 1h bars ending at ``fetch_ts``,
    the entry bar is ``round((fetch_ts - signal_ts)/bar_hours)`` bars from
    the newest close. Returns ``closes[entry_idx:]`` (the entry bar and
    every bar after it — the only bars the close-walk may legitimately
    inspect), or ``None`` when the entry predates the fetched window
    (cannot resolve against bars that predate the signal).

    Clamps negative offsets (future-dated signal / clock skew) to 0 —
    the signal is treated as captured at the newest bar.
    """
    if not closes:
        return None
    sig_dt = _parse_utc_dt(signal_ts)
    fetch_dt = _parse_utc_dt(fetch_ts)
    if sig_dt is None or fetch_dt is None:
        return None
    offset_hours = (fetch_dt - sig_dt).total_seconds() / 3600.0
    offset = int(round(offset_hours / max(bar_hours, 1e-9)))
    if offset < 0:
        offset = 0
    # The entry bar sits `offset` bars behind the newest close. With a
    # zero-based ascending array, the entry index is len-1-offset (H-fix:
    # the old len-offset was one bar LATE — the close-walk started at the
    # bar AFTER the entry instead of at the entry itself). If the entry is
    # OLDER than the oldest fetched bar (offset >= len), nothing valid to
    # walk. Clamp to 0 only for future-dated/clock-skew (offset 0 → entry
    # at the latest bar).
    if offset >= len(closes):
        return None
    entry_idx = max(len(closes) - 1 - offset, 0)
    return list(closes[entry_idx:])

def _settlement_mode() -> str:
    """off | shadow | live for the shared settlement contract."""
    try:
        from regime_platform.core.feature_flags import get_settlement_contract_mode
        return get_settlement_contract_mode()
    except Exception:
        return "live"


def _same_bar_policy() -> str:
    try:
        from regime_platform.core.feature_flags import get_settlement_same_bar_policy
        return get_settlement_same_bar_policy()
    except Exception:
        return sc.SAME_BAR_TP_FIRST


def _drift_fields(
    entry_price: float,
    window: Sequence[float],
    is_buy: bool,
) -> dict[str, float | None]:
    """Signed % drift at h ∈ {1,4,12,24,48} bars after the entry bar
    (window[0]). 12h/48h added 2026-09-24 (Kairos v3 P0.3, migration 167).
    ``window`` is ``closes[:EXPIRY_BARS]`` (indices 0..47), so drift_48h is
    always None here — the nightly kairos.backfill_drift_horizons pass fills
    it (and any 12/24h left None by an early TP/SL settle) once the bars
    exist."""
    from regime_platform.sweep_orchestrator.kairos import drift_fields

    return drift_fields(entry_price, window, is_buy)


def resolve_signal_outcome(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    closes: Sequence[float],
    direction: str = "buy",
    expiry_bars: int = EXPIRY_BARS,
) -> dict[str, Any]:
    """Close-walk TP/SL resolution for one signal over post-entry closes.

    Walks the first ``expiry_bars`` closes in order. On each bar the TP
    check runs BEFORE the SL check (same-bar TP priority convention) unless
    ``settlement_same_bar_policy`` is ``sl_first`` — see
    ``settlement_contract.settle``. Whichever level's bar comes first wins;
    if neither is touched within ``expiry_bars`` the signal is ``expired``.

    Implementation delegates to ``settlement_contract`` when
    ``settlement_contract_mode`` is ``live`` (default after Batch A);
    ``off`` keeps the historical inline walk for emergency revert.
    """
    mode = _settlement_mode()
    policy = _same_bar_policy()
    is_buy = str(direction or "buy").strip().lower() not in ("sell", "short", "s")
    window = list(closes or [])[: max(int(expiry_bars or 0), 0)]

    if mode == "off":
        return _resolve_signal_outcome_legacy(
            entry_price, stop_loss, take_profit, closes, direction, expiry_bars
        )

    result = sc.settle_from_closes(
        entry_price, stop_loss, take_profit, window,
        direction=direction, expiry_bars=expiry_bars, same_bar_policy=policy,
    )
    out = result.as_outcome_dict()
    out.update(_drift_fields(float(entry_price), window, is_buy))
    if mode == "shadow":
        legacy = _resolve_signal_outcome_legacy(
            entry_price, stop_loss, take_profit, closes, direction, expiry_bars
        )
        if legacy.get("outcome") != result.outcome:
            logger.info(
                "settlement_contract SHADOW mismatch: legacy=%s contract=%s "
                "policy=%s bars=%d",
                legacy.get("outcome"), result.outcome, policy, len(window),
            )
        out["settlement_shadow_legacy_outcome"] = legacy.get("outcome")
    out["settlement_mode"] = mode
    out["settlement_same_bar_policy"] = policy
    return out


def resolve_signal_outcome_ohlc(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    bars: Sequence[dict[str, Any]],
    direction: str = "buy",
    expiry_bars: int = EXPIRY_BARS,
) -> dict[str, Any]:
    """**First-touch** TP/SL resolution over intrabar OHLC.

    The dependency Phase 1 needs (worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.1,
    quant-audit finding R1: *"close-only ≠ first-touch"*).

    ``resolve_signal_outcome`` walks **closes**, which is optimistic: a bar whose
    close sits between SL and TP is scored as "no touch" even when its wick
    crossed a level. This walks the intrabar ``high``/``low`` instead, so a level
    is touched if price traded through it at any point in the bar — which is the
    same basis the ``mae_*_pct`` / ``mfe_*_pct`` columns already use, so a
    survivable label computed from this resolver is internally consistent with
    them (the caveat that made the 57.7% ghost-edge figure an upper bound).

    ``bars`` are ``{"h": high, "l": low, "c": close}`` dicts in chronological
    order (the shape ``price_fetcher.fetch_candles_for_symbols(ohlcv_out=...)``
    emits). Bars missing ``h``/``l`` fall back to their ``c``, so a partial
    series degrades to a close-walk rather than being dropped.

    **Same-bar both-touched** is reported, not silently resolved: ``ambiguous``
    is True and the outcome follows ``sl_first`` (the conservative choice — a
    stop is assumed filled before the target when a single bar spans both). The
    audit's live/backtest split is exactly this case (live assumes SL first,
    the old close-walk assumed TP first), so the flag makes the disagreement
    visible instead of picking silently.

    Returns the same keys as ``_resolve_signal_outcome_legacy`` plus
    ``ambiguous`` and ``touch_bar``.
    """
    is_buy = str(direction or "buy").strip().lower() not in ("sell", "short", "s")
    window = list(bars or [])[: max(int(expiry_bars or 0), 0)]

    outcome = "expired"
    exit_price: float | None = None
    exit_idx: int | None = None
    ambiguous = False

    for i, bar in enumerate(window):
        try:
            hi = float(bar.get("h") if bar.get("h") is not None else bar.get("c"))
            lo = float(bar.get("l") if bar.get("l") is not None else bar.get("c"))
            cl = float(bar.get("c"))
        except (AttributeError, TypeError, ValueError):
            continue
        if is_buy:
            tp_touch, sl_touch = hi >= take_profit, lo <= stop_loss
        else:
            tp_touch, sl_touch = lo <= take_profit, hi >= stop_loss

        if tp_touch and sl_touch:
            # One bar spans both levels — order is unknowable from OHLC.
            ambiguous = True
            outcome = "sl_hit"          # conservative: assume the stop filled
            exit_price, exit_idx = stop_loss, i
            break
        if sl_touch:
            outcome, exit_price, exit_idx = "sl_hit", stop_loss, i
            break
        if tp_touch:
            outcome, exit_price, exit_idx = "tp_hit", take_profit, i
            break
        # neither touched; remember the close in case we expire
        exit_price = cl

    if outcome == "expired" and window:
        try:
            exit_price = float(window[-1].get("c"))
        except (AttributeError, TypeError, ValueError):
            exit_price = None

    walked = window if exit_idx is None else window[: exit_idx + 1]
    highs, lows = [], []
    for b in walked:
        try:
            highs.append(float(b.get("h") if b.get("h") is not None else b.get("c")))
            lows.append(float(b.get("l") if b.get("l") is not None else b.get("c")))
        except (AttributeError, TypeError, ValueError):
            continue
    if is_buy:
        mfe = max((h - entry_price for h in highs), default=0.0)
        mae = max((entry_price - l for l in lows), default=0.0)
    else:
        mfe = max((entry_price - l for l in lows), default=0.0)
        mae = max((h - entry_price for h in highs), default=0.0)
    # Excursions are magnitudes — floor at 0 so a favourable-only path cannot
    # report a negative MAE. (The legacy close-walk can: it takes the max over
    # the walked path without a floor, so an all-up path yields mae < 0. That is
    # a pre-existing quirk of the close-walk, not replicated here.)
    mfe, mae = max(0.0, mfe), max(0.0, mae)

    # Drift columns stay CLOSE-based by design (direction-relative price drift),
    # so they are computed from the bars' closes — same basis as the legacy
    # resolver, keeping the two comparable on those columns.
    closes = []
    for b in window:
        try:
            closes.append(float(b.get("c")))
        except (AttributeError, TypeError, ValueError):
            continue

    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_idx": exit_idx,
        "hold_bars": (exit_idx + 1) if exit_idx is not None else len(window),
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "ambiguous": ambiguous,
        "touch_bar": exit_idx,
        **_drift_fields(float(entry_price), closes, is_buy),
    }


def _resolve_signal_outcome_legacy(
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    closes: Sequence[float],
    direction: str = "buy",
    expiry_bars: int = EXPIRY_BARS,
) -> dict[str, Any]:
    """Historical inline close-walk (tp_first). Kept for mode=off revert."""
    is_buy = str(direction or "buy").strip().lower() not in ("sell", "short", "s")
    window = list(closes or [])[: max(int(expiry_bars or 0), 0)]

    outcome = "expired"
    exit_price: float | None = None
    exit_idx: int | None = None

    for i, close in enumerate(window):
        if is_buy:
            if close >= take_profit:
                outcome, exit_price, exit_idx = "tp_hit", close, i
                break
            if close <= stop_loss:
                outcome, exit_price, exit_idx = "sl_hit", close, i
                break
        else:
            if close <= take_profit:
                outcome, exit_price, exit_idx = "tp_hit", close, i
                break
            if close >= stop_loss:
                outcome, exit_price, exit_idx = "sl_hit", close, i
                break

    if outcome == "expired" and window:
        exit_price = float(window[-1])

    walked = window if exit_idx is None else window[: exit_idx + 1]
    if is_buy:
        mfe = max((float(c) - entry_price for c in walked), default=0.0)
        mae = max((entry_price - float(c) for c in walked), default=0.0)
    else:
        mfe = max((entry_price - float(c) for c in walked), default=0.0)
        mae = max((float(c) - entry_price for c in walked), default=0.0)

    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_idx": exit_idx,
        "hold_bars": (exit_idx + 1) if exit_idx is not None else len(window),
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        **_drift_fields(float(entry_price), window, is_buy),
    }


def compute_pnl(
    outcome: str,
    tp_bricks: float | None,
    sl_bricks: float | None,
    brick_size: float | None,
    entry_price: float | None = None,
    exit_price: float | None = None,
    direction: str = "buy",
) -> dict[str, float]:
    """PnL in bricks and dollars for a resolved outcome.

    Convention (matches the pipeline's brick-delta model) — implemented in
    ``settlement_contract.compute_bricks_pnl`` so live resolver, paper book,
    and scorecard share one formula. See that module for expired MTM.
    ``pnl_dollars = pnl_bricks * brick_size`` (6dp).
    """
    mode = _settlement_mode()
    if mode == "off":
        return _compute_pnl_legacy(
            outcome, tp_bricks, sl_bricks, brick_size,
            entry_price=entry_price, exit_price=exit_price, direction=direction,
        )
    pnl_bricks, dollars = sc.compute_bricks_pnl(
        outcome,
        tp_bricks=tp_bricks,
        sl_bricks=sl_bricks,
        brick_size=brick_size,
        entry_price=entry_price,
        exit_price=exit_price,
        direction=direction,
    )
    return {"pnl_bricks": pnl_bricks, "pnl_dollars": round(dollars, 6)}


def _compute_pnl_legacy(
    outcome: str,
    tp_bricks: float | None,
    sl_bricks: float | None,
    brick_size: float | None,
    entry_price: float | None = None,
    exit_price: float | None = None,
    direction: str = "buy",
) -> dict[str, float]:
    if outcome == "tp_hit":
        pnl_bricks = abs(float(tp_bricks or 0.0))
    elif outcome == "sl_hit":
        pnl_bricks = -abs(float(sl_bricks or 0.0))
    elif (
        outcome == "expired"
        and entry_price is not None
        and exit_price is not None
        and brick_size
    ):
        is_buy = str(direction or "buy").strip().lower() not in ("sell", "short", "s")
        delta = (
            float(exit_price) - float(entry_price) if is_buy
            else float(entry_price) - float(exit_price)
        )
        pnl_bricks = delta / float(brick_size)
    else:
        pnl_bricks = 0.0
    dollars = pnl_bricks * float(brick_size or 0.0)
    return {"pnl_bricks": pnl_bricks, "pnl_dollars": round(dollars, 6)}


def shift_ts(ts: Any, hours: int) -> str | None:
    """Approximate ``exit_ts = signal.ts + hold_bars`` hours as ISO UTC.

    Accepts ISO-8601 strings (with/without tz, ``Z`` suffix) or epoch
    floats/ints. Returns ``None`` when unparseable — resolution still
    proceeds, the column just stays NULL.
    """
    if ts is None or hours is None:
        return None
    try:
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            base = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        else:
            text = str(ts).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            base = datetime.fromisoformat(text)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        return (base + timedelta(hours=int(hours))).isoformat()
    except Exception:
        logger.debug("shift_ts: unparseable ts=%r", ts)
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ─── Supabase REST helpers (mirror supabase_client pattern) ───────

def _filter_payload(payload: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Drop keys outside ``allowed`` so PostgREST never sees PGRST204."""
    return {k: v for k, v in payload.items() if k in allowed}


async def _fetch_pending(
    limit: int, min_ts: str | None = None, offset: int = 0,
) -> list[dict[str, Any]]:
    """Rows of nt_signal_sim where outcome IS NULL, oldest first.

    ``min_ts`` (ISO UTC string) bounds the fetch to signals whose entry
    falls inside the candle window the resolver will fetch — older signals
    cannot be resolved against bars that predate them (window-anchoring
    fix 2026-08-12), so they stay pending and are handled by retention
    (expired after EOD_RETENTION_STALE_DAYS).

    ``offset`` pages past rows already seen. Only the read-only dry run
    uses it (its rows never leave the ``outcome IS NULL`` pool, so without
    paging it would re-read the same oldest batch forever).
    """
    url = _url("nt_signal_sim")
    params = {
        "select": "*",
        "outcome": "is.null",
        "order": "ts.asc",
        "limit": str(int(limit)),
    }
    if offset:
        params["offset"] = str(int(offset))
    if min_ts:
        params["ts"] = f"gte.{min_ts}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


async def _patch_signal(signal_id: Any, patch: dict[str, Any]) -> bool:
    """PATCH one nt_signal_sim row by signal_id. True on success."""
    if not settings.SUPABASE_URL or signal_id is None:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={"signal_id": f"eq.{signal_id}"},
                json=patch,
            )
            if not resp.is_success:
                logger.error(
                    "eod_resolver: PATCH signal %s HTTP %d — %s",
                    signal_id, resp.status_code, resp.text[:300],
                )
                return False
            return True
    except Exception as exc:
        logger.error("eod_resolver: PATCH signal %s failed: %s: %r", signal_id, type(exc).__name__, exc)
        return False


async def _write_calibration_record(
    sig: dict[str, Any],
    resolved: dict[str, Any],
    pnl: dict[str, float],
) -> None:
    """Insert a win/loss calibration record for a tp_hit/sl_hit signal.

    Expired signals are intentionally NOT written here — a timeout is
    neither a win nor a loss, and counting it as a loss would bias the
    calibration record set against the model.
    """
    symbol = sig.get("symbol")
    outcome = "win" if resolved["outcome"] == "tp_hit" else "loss"
    entered_at = shift_ts(sig.get("ts"), 0) or datetime.now(timezone.utc).isoformat()
    # id is TEXT PRIMARY KEY NOT NULL — generate one here (the migration 100
    # table has no DB default). aggression is NOT NULL CHECK in
    # ('passive','mid','aggressive') — take it from the signal, default 'mid'.
    aggression = str(sig.get("aggression") or "mid").lower()
    if aggression not in ("passive", "mid", "aggressive"):
        aggression = "mid"
    # Brier score = (predicted_prob - outcome)^2; outcome ∈ {1, 0}.
    # Batch B (2026-09-16): measurement honesty — Brier uses RAW only.
    # When raw is missing we do NOT silently score the enforced p_win
    # (that would make enforcement look like model accuracy). brier=None
    # and error_source marks the gap.
    raw_required = bool(getattr(settings, "NOBLE_TRADER_CALIBRATION_RAW_REQUIRED", True))
    pred_raw = _as_float(sig.get("predicted_p_win_raw"))
    pred_enforced = _as_float(sig.get("p_win"))
    error_source = "eod_resolver"
    if pred_raw is not None:
        brier = round((1.0 - pred_raw) ** 2 if outcome == "win" else pred_raw ** 2, 6)
    elif raw_required:
        brier = None
        error_source = "raw_missing"
        logger.warning(
            "eod_resolver: calibration record for %s has no predicted_p_win_raw — "
            "Brier skipped (raw required); enforced p_win not used as stand-in",
            symbol,
        )
    else:
        # Legacy opt-out path only
        pred_for_brier = pred_enforced or 0.5
        brier = round(
            (1.0 - pred_for_brier) ** 2 if outcome == "win" else pred_for_brier ** 2, 6
        )
        error_source = "enforced_fallback"
    payload = _filter_payload({
        "id": str(uuid.uuid4()),
        "signal_id": sig.get("signal_id"),
        "symbol": symbol,
        "venue": sig.get("venue"),
        "predicted_p_win": pred_enforced,
        "predicted_p_win_raw": pred_raw,
        "calibration_brier": brier,
        "outcome": outcome,
        "aggression": aggression,
        "entry_price": _as_float(sig.get("entry_price")),
        "exit_price": resolved.get("exit_price"),
        "entered_at": entered_at,
        "closed_at": shift_ts(sig.get("ts"), resolved.get("hold_bars") or 0),
        "actual_pnl": pnl.get("pnl_dollars"),
        "error_source": error_source,
    }, CALIBRATION_RECORD_COLUMNS)
    ok = await _insert_row("nt_calibration_records", payload)
    if not ok:
        logger.warning(
            "eod_resolver: calibration record %s for %s failed",
            outcome, symbol,
        )


def anchor_post_entry_bars(
    bars: Sequence[dict[str, Any]],
    signal_ts: Any,
    fetch_ts: Any,
    bar_hours: float = 1.0,
) -> list[dict[str, Any]] | None:
    """OHLC twin of :func:`anchor_post_entry_closes`.

    Same windowing rule (the entry bar is ``len-1-offset``; ``None`` when the
    entry predates the fetched window), applied to the ``{"t","o","h","l","c","v"}``
    rows ``fetch_candles_for_symbols(ohlcv_out=...)`` emits. Kept as a separate
    function rather than a generic over both shapes so the index arithmetic is
    duplicated *visibly* — if one drifts, a test catches it rather than a silent
    shared-helper change.
    """
    if not bars:
        return None
    sig_dt = _parse_utc_dt(signal_ts)
    fetch_dt = _parse_utc_dt(fetch_ts)
    if sig_dt is None or fetch_dt is None:
        return None
    offset_hours = (fetch_dt - sig_dt).total_seconds() / 3600.0
    offset = int(round(offset_hours / max(bar_hours, 1e-9)))
    if offset < 0:
        offset = 0
    if offset >= len(bars):
        return None
    entry_idx = max(len(bars) - 1 - offset, 0)
    return [b for b in bars[entry_idx:] if isinstance(b, dict)]


def _judge_bars(
    sig: dict[str, Any], bars: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None]:
    """Intrabar twin of :func:`_judge` — same contract, OHLC basis.

    Shared by the writing resolver and the read-only hourly dry run so both
    judge identically under ``eod_resolver_basis: intrabar``. The minimum-age
    gate is preserved verbatim: an "expired" verdict with fewer than
    ``EXPIRY_BARS`` bars available means "not checked long enough yet", not
    "timed out" — deferring is what keeps a young signal out of a permanent
    wrong terminal state.
    """
    entry = _as_float(sig.get("entry_price"))
    stop = _as_float(sig.get("stop_loss"))
    tp = _as_float(sig.get("take_profit"))
    if entry is None or stop is None or tp is None:
        return None, None

    resolved = resolve_signal_outcome_ohlc(
        entry, stop, tp,
        bars,
        direction=sig.get("signal") or "buy",
        expiry_bars=EXPIRY_BARS,
    )
    if resolved["outcome"] == "expired" and len(bars) < EXPIRY_BARS:
        return "pending", resolved
    return resolved["outcome"], resolved


def _judge(
    sig: dict[str, Any], closes: list[float],
) -> tuple[str | None, dict[str, Any] | None]:
    """Pure verdict for one pending signal — no I/O.

    Returns ``(None, None)`` when entry/stop/tp is missing, ``("pending",
    resolved)`` when the close-walk would only say "expired" because fewer
    than EXPIRY_BARS closes exist yet (minimum-age gate below), otherwise
    ``(outcome, resolved)``. Shared by the writing resolver and the
    read-only hourly dry run so both judge identically.
    """
    entry = _as_float(sig.get("entry_price"))
    stop = _as_float(sig.get("stop_loss"))
    tp = _as_float(sig.get("take_profit"))
    if entry is None or stop is None or tp is None:
        return None, None

    resolved = resolve_signal_outcome(
        entry, stop, tp,
        closes,
        direction=sig.get("signal") or "buy",
        expiry_bars=EXPIRY_BARS,
    )

    # ── Minimum-age gate (2026-08-23 fix) ──────────────────────────────
    # resolve_signal_outcome only ever walks `closes[:EXPIRY_BARS]` — if
    # fewer than EXPIRY_BARS closes were even available yet (a signal
    # entered a few bars ago, not 48), an "expired" verdict here means
    # "hasn't been checked long enough," not "genuinely timed out."
    # Without this gate, every signal newer than EXPIRY_BARS bars gets
    # force-resolved as 'expired' at whatever partial bar count this daily
    # run happens to catch it at (confirmed live: US500 hold_bars 2-3,
    # AAPL 1-9, nowhere near 48) — and since outcome is no longer NULL,
    # it's permanently excluded from every future resolution attempt and
    # never produces a calibration record (tp_hit/sl_hit only). A TP/SL
    # touch still resolves immediately regardless of bar count — only a
    # would-be "expired" verdict is deferred. See
    # worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md root cause 3 and
    # worklog/20260823_CALIBRATION_BIAS_CHECK_AUDIT.md.
    if resolved["outcome"] == "expired" and len(closes) < EXPIRY_BARS:
        return "pending", resolved
    return resolved["outcome"], resolved


async def _resolve_one(sig: dict[str, Any], closes: list[float]) -> str | None:
    """Resolve + persist one signal. Returns outcome key, ``"pending"`` if
    it's not old enough to judge yet (see the minimum-age gate in
    ``_judge``), or None on error."""
    verdict, resolved = _judge(sig, closes)
    if verdict is None or resolved is None:
        logger.warning(
            "eod_resolver: signal %s missing entry/stop/tp — left pending",
            sig.get("signal_id"),
        )
        return None
    if verdict == "pending":
        return "pending"
    entry = _as_float(sig.get("entry_price"))
    pnl = compute_pnl(
        resolved["outcome"],
        sig.get("tp_bricks"), sig.get("sl_bricks"), sig.get("brick_size"),
        entry_price=entry,
        exit_price=resolved["exit_price"],
        direction=sig.get("signal") or "buy",
    )

    patch = _filter_payload({
        "outcome": resolved["outcome"],
        "exit_price": resolved["exit_price"],
        "exit_ts": shift_ts(sig.get("ts"), resolved["hold_bars"]),
        "hold_bars": resolved["hold_bars"],
        "mfe": resolved["mfe"],
        "mae": resolved["mae"],
        "pnl_bricks": pnl["pnl_bricks"],
        "pnl_dollars": pnl["pnl_dollars"],
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "drift_1h_pct": resolved.get("drift_1h_pct"),
        "drift_4h_pct": resolved.get("drift_4h_pct"),
        "drift_12h_pct": resolved.get("drift_12h_pct"),
        "drift_24h_pct": resolved.get("drift_24h_pct"),
        "drift_48h_pct": resolved.get("drift_48h_pct"),
    }, NT_SIGNAL_SIM_COLUMNS)

    if not await _patch_signal(sig.get("signal_id"), patch):
        return None

    if resolved["outcome"] in ("tp_hit", "sl_hit"):
        await _write_calibration_record(sig, resolved, pnl)

    return resolved["outcome"]


async def _resolve_one_bars(sig: dict[str, Any], bars: list[dict[str, Any]]) -> str | None:
    """Intrabar twin of :func:`_resolve_one` — same persistence, OHLC verdict.

    Identical patch payload; only the judge differs (high/low instead of
    closes). Kept as a separate function so the close-walk path stays
    byte-identical when the basis flag is untouched.
    """
    verdict, resolved = _judge_bars(sig, bars)
    if verdict is None or resolved is None:
        logger.warning(
            "eod_resolver: signal %s missing entry/stop/tp — left pending",
            sig.get("signal_id"),
        )
        return None
    if verdict == "pending":
        return "pending"
    entry = _as_float(sig.get("entry_price"))
    pnl = compute_pnl(
        resolved["outcome"],
        sig.get("tp_bricks"), sig.get("sl_bricks"), sig.get("brick_size"),
        entry_price=entry,
        exit_price=resolved["exit_price"],
        direction=sig.get("signal") or "buy",
    )

    patch = _filter_payload({
        "outcome": resolved["outcome"],
        "exit_price": resolved["exit_price"],
        "exit_ts": shift_ts(sig.get("ts"), resolved["hold_bars"]),
        "hold_bars": resolved["hold_bars"],
        "mfe": resolved["mfe"],
        "mae": resolved["mae"],
        "pnl_bricks": pnl["pnl_bricks"],
        "pnl_dollars": pnl["pnl_dollars"],
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "drift_1h_pct": resolved.get("drift_1h_pct"),
        "drift_4h_pct": resolved.get("drift_4h_pct"),
        "drift_12h_pct": resolved.get("drift_12h_pct"),
        "drift_24h_pct": resolved.get("drift_24h_pct"),
        "drift_48h_pct": resolved.get("drift_48h_pct"),
    }, NT_SIGNAL_SIM_COLUMNS)

    if not await _patch_signal(sig.get("signal_id"), patch):
        return None

    if resolved["outcome"] in ("tp_hit", "sl_hit"):
        await _write_calibration_record(sig, resolved, pnl)

    return resolved["outcome"]


def _resolver_basis() -> str:
    """``close_walk`` | ``intrabar`` — the resolver's TP/SL basis.

    Fails closed to ``close_walk`` (the shipped default) so a flag-read error
    can never silently change outcome semantics.
    """
    try:
        from regime_platform.core.feature_flags import get_eod_resolver_basis
        return get_eod_resolver_basis()
    except Exception:
        return "close_walk"


# ─── Orchestrator entry point ─────────────────────────────────────
async def resolve_pending_signals(limit: int = 200, max_total: int = 0) -> dict[str, Any]:
    """Forward-resolve pending nt_signal_sim rows in batches.

    Batches the fetch+resolve loop until the in-window pending backlog is
    drained (or ``max_total`` rows have been attempted). Previously a single
    fetch of ``limit`` rows per daily run — with ~3,200 signals captured/day
    vs a 1,000/day resolve limit the backlog grew monotonically (H4).

    Per batch:
      1. Query ``nt_signal_sim`` WHERE ``outcome IS NULL`` ORDER BY ts ASC,
         bounded to signals whose entry falls inside the candle window the
         resolver is about to fetch (``ts >= now() - LIGHT_BARS hours`` —
         window-anchoring fix 2026-08-12; older signals stay pending and are
         handled by retention, which expires them after
         EOD_RETENTION_STALE_DAYS).
      2. Group by symbol and fetch post-entry 1h closes per symbol
         (``fetch_candles_for_symbols``, `LIGHT_BARS` bars).
      3. Per signal, slice the fetched closes to the POST-ENTRY window via
         ``anchor_post_entry_closes`` (the fetched array is ascending, so
         ``closes[-(offset):]`` starts at the entry bar). Signals whose
         entry predates the window are skipped (``windowed_out``) — never
         resolved against pre-entry bars.
      4. Close-walk TP/SL per signal (48-bar expiry).
      5. PATCH outcome/exit_*/hold_bars/mfe/mae/pnl_*/resolved_at back.
      6. Insert nt_calibration_records for tp_hit (win) / sl_hit (loss).

    Returns ``{resolved, by_outcome, windowed_out, insufficient_bars, errors,
    batches}``. ``insufficient_bars`` counts signals left pending because
    they haven't had their full EXPIRY_BARS window yet (not a failure —
    they'll be re-attempted on a later run once more bars accumulate).
    Never raises — every failure path is caught and counted in ``errors``.
    """
    summary: dict[str, Any] = {
        "resolved": 0, "by_outcome": {}, "windowed_out": 0, "errors": 0,
        "batches": 0, "insufficient_bars": 0,
        # intrabar_fallback (2026-10-02): signals judged by the CLOSE-WALK while
        # eod_resolver_basis=intrabar, because OHLC was unavailable for their
        # symbol. Non-zero means those rows carry the OLD basis, so a systematic
        # shortfall would silently mix labels — hence it is counted, not hidden.
        "intrabar_fallback": 0,
    }
    if not settings.SUPABASE_URL:
        logger.warning("eod_resolver: Supabase not configured — nothing to resolve")
        return summary

    fetch_ts = datetime.now(timezone.utc)
    # Only resolve signals whose entry falls inside the candle window the
    # resolver is about to fetch. Anything older cannot be scored against
    # post-entry bars (the close-walk would inspect pre-entry action).
    min_ts = (fetch_ts - timedelta(hours=LIGHT_BARS)).isoformat()
    attempted_total = 0

    # ── Full-queue scan (2026-09-26, worklog/kairos/
    # 20260926_RESOLVER_HEAD_OF_LINE_FIX.md) ──────────────────────────────
    # Found live: the pending query is oldest-first, and the loop used to
    # stop at the first batch that resolved nothing. The oldest `limit` rows
    # are mostly signals still inside their 48-bar window, so every TP/SL
    # already decided further down the queue (72 on 2026-09-26 01:25 UTC,
    # queue positions 1,024–1,893) waited for the head to expire — and the
    # nightly wrote 15 TP / 21 SL while 12 TP / 83 SL were decided. Losses
    # were under-written, skewing the calibration pool toward wins.
    # `full` (default) pages with an offset past rows that stay pending
    # (they never leave the `outcome IS NULL` pool, so the offset only needs
    # to skip those) and scans to the end of the queue; `head` restores the
    # old stop-at-first-empty-batch behaviour (rollback).
    from regime_platform.core.feature_flags import (
        get_eod_resolver_page_pause_s,
        get_eod_resolver_scan_mode,
    )
    full_scan = get_eod_resolver_scan_mode() == "full"
    page_pause_s = get_eod_resolver_page_pause_s()
    skip = 0  # rows at the head of the pool this run has seen and left pending
    summary["scan_mode"] = "full" if full_scan else "head"

    while True:
        if max_total > 0 and attempted_total >= max_total:
            logger.info("eod_resolver: reached max_total=%d — stopping", max_total)
            break
        if summary["batches"] and page_pause_s > 0:
            # Space the per-batch TradingView candle fetches (the 2026-08-23/24
            # 429 incidents were back-to-back fetches with no delay).
            await asyncio.sleep(page_pause_s)
        try:
            rows = await _fetch_pending(limit, min_ts=min_ts, offset=skip if full_scan else 0)
        except Exception as exc:
            logger.error("eod_resolver: pending query failed: %s", exc)
            summary["errors"] += 1
            break
        if not rows:
            logger.info("eod_resolver: no more pending signals (limit=%d)", limit)
            break
        attempted_total += len(rows)
        summary["batches"] += 1

        # Map symbol -> tradingview_symbol (active symbols first; fall back to
        # whatever the row itself carries).
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_active_symbols,
        )
        tv_map: dict[str, Any] = {}
        for sym in await fetch_active_symbols():
            tv_map.setdefault(sym.get("symbol"), sym.get("tradingview_symbol"))
        for row in rows:
            tv_map.setdefault(row.get("symbol"), row.get("tradingview_symbol"))

        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_symbol.setdefault(row.get("symbol"), []).append(row)

        outcomes: Counter[str] = Counter()

        # ── Batched candle fetch (2026-08-24 fix) ──────────────────────
        # Previously fetched one symbol at a time — N sequential HTTP
        # requests with no delay between them. That collided with the
        # light sweep's own concurrent TradingView batch call at the same
        # cron tick and tripped 429s on whichever symbols happened to be
        # later in the loop (confirmed live 2026-08-24: 4 consecutive
        # single-symbol requests all 429'd within ~100ms, wiping out that
        # day's only resolution attempt for those symbols — not a real
        # data gap, self-inflicted rate limiting). Fetch every symbol in
        # this batch in ONE call; fetch_candles_for_symbols auto-chunks
        # into ceil(N/10) HTTP requests per TradingView's batch limit,
        # the same pattern the light sweep already uses.
        symbol_specs: list[dict[str, Any]] = []
        for symbol, sigs in by_symbol.items():
            tv = tv_map.get(symbol)
            if not tv:
                logger.warning(
                    "eod_resolver: no tradingview_symbol for %s — %d pending unresolved",
                    symbol, len(sigs),
                )
                summary["errors"] += len(sigs)
                continue
            symbol_specs.append({"symbol": symbol, "tradingview_symbol": tv})

        # Basis (2026-10-02, worklog/20261002_SIGNAL_QUALITY_PLAN.md §3.1):
        # "intrabar" fetches OHLC as well and judges on high/low; "close_walk"
        # is byte-identical to the historical behaviour.
        _basis_intrabar = _resolver_basis() == "intrabar"
        _ohlcv_out: dict[str, list[dict[str, Any]]] = {}
        try:
            if _basis_intrabar:
                _res = await fetch_candles_for_symbols(
                    symbol_specs, n_bars=LIGHT_BARS, interval=INTERVAL,
                    ohlcv_out=_ohlcv_out,
                )
                # Defensive: accept either the (closes, ohlcv) tuple or a bare
                # closes dict. A caller/mock that ignores ohlcv_out must not
                # crash the resolve pass — it degrades to closes-only below.
                if isinstance(_res, tuple):
                    candles_by_symbol, _ohlcv_out = _res
                else:
                    candles_by_symbol = _res
            else:
                candles_by_symbol = await fetch_candles_for_symbols(
                    symbol_specs, n_bars=LIGHT_BARS, interval=INTERVAL,
                )
        except Exception as exc:
            logger.error("eod_resolver: candle batch fetch failed: %s", exc)
            for spec in symbol_specs:
                summary["errors"] += len(by_symbol[spec["symbol"]])
            symbol_specs = []

        for spec in symbol_specs:
            symbol = spec["symbol"]
            sigs = by_symbol[symbol]
            closes = candles_by_symbol.get(symbol, [])
            if not closes:
                logger.warning(
                    "eod_resolver: no candles returned for %s — %d pending unresolved",
                    symbol, len(sigs),
                )
                summary["errors"] += len(sigs)
                continue

            for sig in sigs:
                try:
                    # Window anchoring: only walk bars AFTER the signal's entry.
                    # Without this, a backlogged signal (entry days ago) would be
                    # scored against the FIRST bars of the fetched window, which
                    # predate the entry (2026-08-12 root-cause fix).
                    post = anchor_post_entry_closes(closes, sig.get("ts"), fetch_ts)
                    if post is None:
                        summary["windowed_out"] += 1
                        continue
                    if _basis_intrabar:
                        _bars = anchor_post_entry_bars(
                            _ohlcv_out.get(symbol) or [], sig.get("ts"), fetch_ts,
                        )
                        if _bars:
                            outcome = await _resolve_one_bars(sig, _bars)
                        else:
                            # OHLC unavailable for this symbol (partial response,
                            # or a caller that ignored ohlcv_out). Fall back to
                            # the close-walk for this signal rather than
                            # silently dropping it — and count it, so a
                            # systematic shortfall is visible rather than
                            # looking like a quiet data gap.
                            summary["intrabar_fallback"] += 1
                            outcome = await _resolve_one(sig, post)
                    else:
                        outcome = await _resolve_one(sig, post)
                    if outcome is None:
                        summary["errors"] += 1
                    elif outcome == "pending":
                        summary["insufficient_bars"] += 1
                    else:
                        summary["resolved"] += 1
                        outcomes[outcome] += 1
                except Exception as exc:
                    logger.error(
                        "eod_resolver: signal %s failed: %s",
                        sig.get("signal_id"), exc,
                    )
                    summary["errors"] += 1

        # Fold this batch's outcome counters into the summary.
        batch_outcomes = dict(outcomes)
        for k, v in batch_outcomes.items():
            summary["by_outcome"][k] = summary["by_outcome"].get(k, 0) + v

        # ── No-forward-progress guard (2026-08-23, found live) ────────────
        # Only a successful terminal PATCH (tp_hit/sl_hit/expired-at-full-
        # window) removes a row from the `outcome IS NULL` pool. `pending`
        # (insufficient_bars), `windowed_out`, and `errors` all leave it in
        # the pool — so if this batch resolved zero rows to a terminal
        # state, the NEXT `_fetch_pending` call returns the exact same rows
        # (deterministic close-walk over unchanged candle data → identical
        # result), forever. Confirmed live: after the minimum-age gate
        # shipped, a fresh backlog (mostly signals too young to judge yet)
        # made this loop re-fetch + re-fetch-candles-for-all-symbols in a
        # tight cycle with no delay, hitting TradingView 429 rate limits
        # within seconds. Stop as soon as a batch makes no progress —
        # there's nothing more this invocation can do; a later run (once
        # signals mature or the cron fires again) will pick it back up.
        progressed = sum(outcomes.values())
        if full_scan:
            # Rows that reached a terminal outcome left the pool; everything
            # else in this batch is still there, ahead of the unseen rows.
            skip += len(rows) - progressed
            if len(rows) < limit and progressed == 0:
                logger.info(
                    "eod_resolver: reached end of pending queue (%d scanned past, "
                    "%d insufficient_bars / %d windowed_out / %d errors)",
                    skip, summary["insufficient_bars"], summary["windowed_out"],
                    summary["errors"],
                )
                break
            continue

        if progressed == 0:
            logger.info(
                "eod_resolver: batch made no forward progress (0 resolved this "
                "batch) — stopping to avoid refetching the same unresolvable "
                "rows; %d insufficient_bars / %d windowed_out / %d errors so far",
                summary["insufficient_bars"], summary["windowed_out"], summary["errors"],
            )
            break

    summary["by_outcome"] = dict(summary.get("by_outcome") or {})
    logger.info(
        "eod_resolver: resolved %d pending in %d batch(es) (%s), "
        "windowed_out=%d, insufficient_bars=%d, errors=%d",
        summary["resolved"], summary["batches"], summary["by_outcome"],
        summary["windowed_out"], summary["insufficient_bars"], summary["errors"],
    )
    return summary


# ─── Hourly calibration resolve (Kairos V3 §14.2 lever 1, 2026-09-25) ──
#
# worklog/kairos/20260926_HOURLY_CALIBRATION_RESOLVE.md. The nightly
# eod-lookback (01:17 UTC) is the only writer of nt_calibration_records,
# so a TP/SL outcome waits a median ~11h before fetch_calibration_bias can
# see it. `calibration_resolve_hourly_mode` (config/feature_flags.yaml):
#   shadow → dry_run_pending_signals(): read-only, same judge (_judge) and
#            the same windowing as the writing resolver, reports what would
#            resolve now + the evidence-hours saved vs the next nightly.
#   live   → the orchestrator calls resolve_pending_signals() hourly (no
#            new write path — the same function the nightly runs).

NIGHTLY_RESOLVE_UTC = (1, 17)  # eod-lookback cron "17 1 * * *"


def next_nightly_resolve(now: datetime) -> datetime:
    """First nightly eod-lookback fire strictly after ``now`` (UTC)."""
    h, m = NIGHTLY_RESOLVE_UTC
    cand = now.astimezone(timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def evidence_hours_saved(now: datetime) -> float:
    """Hours an outcome resolved at ``now`` reaches nt_calibration_records
    earlier than it would at the next nightly run."""
    return round((next_nightly_resolve(now) - now).total_seconds() / 3600.0, 2)


async def dry_run_pending_signals(
    limit: int = 1000, max_total: int = 12000, now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only twin of ``resolve_pending_signals``: same pending query,
    same candle window + post-entry anchoring, same ``_judge``. Writes
    nothing (no PATCH, no calibration record). Pages with ``offset``
    because judged rows stay in the ``outcome IS NULL`` pool.

    Returns ``{would_resolve, by_outcome, by_symbol, qualified_tp_sl,
    insufficient_bars, windowed_out, errors, scanned, hours_saved}``.
    ``hours_saved`` = evidence latency removed for every TP/SL found now
    (each would otherwise wait for the next 01:17 UTC nightly run).
    Never raises.
    """
    now = now or datetime.now(timezone.utc)
    out: dict[str, Any] = {
        "would_resolve": 0, "by_outcome": {}, "by_symbol": {}, "by_gate": {}, "qualified_tp_sl": 0,
        "insufficient_bars": 0, "windowed_out": 0, "errors": 0, "scanned": 0,
        "hours_saved": evidence_hours_saved(now),
    }
    if not settings.SUPABASE_URL:
        return out
    min_ts = (now - timedelta(hours=LIGHT_BARS)).isoformat()
    rows: list[dict[str, Any]] = []
    try:
        offset = 0
        while offset < max_total:
            batch = await _fetch_pending(limit, min_ts=min_ts, offset=offset)
            rows.extend(batch)
            offset += len(batch)
            if len(batch) < limit:
                break
    except Exception as exc:
        logger.warning("eod_resolver: dry-run pending query failed: %s", exc)
        out["errors"] += 1
        return out
    out["scanned"] = len(rows)
    if not rows:
        return out

    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    tv_map: dict[str, Any] = {}
    try:
        for sym in await fetch_active_symbols():
            tv_map.setdefault(sym.get("symbol"), sym.get("tradingview_symbol"))
    except Exception as exc:
        logger.warning("eod_resolver: dry-run active symbols failed: %s", exc)
    by_symbol_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tv_map.setdefault(row.get("symbol"), row.get("tradingview_symbol"))
        by_symbol_rows.setdefault(row.get("symbol"), []).append(row)
    specs = [{"symbol": s, "tradingview_symbol": tv_map[s]} for s in by_symbol_rows if tv_map.get(s)]
    out["errors"] += sum(len(v) for s, v in by_symbol_rows.items() if not tv_map.get(s))
    try:
        if _resolver_basis() == "intrabar":
            _dry_ohlcv: dict[str, list[dict[str, Any]]] = {}
            _dres = await fetch_candles_for_symbols(
                specs, n_bars=LIGHT_BARS, interval=INTERVAL, ohlcv_out=_dry_ohlcv,
            ) if specs else ({}, {})
            # Same defensive unpack as the writing resolver: accept either the
            # (closes, ohlcv) tuple or a bare closes dict.
            if isinstance(_dres, tuple):
                candles, _dry_ohlcv = _dres
            else:
                candles = _dres
        else:
            candles = await fetch_candles_for_symbols(specs, n_bars=LIGHT_BARS, interval=INTERVAL) if specs else {}
            _dry_ohlcv = {}
    except Exception as exc:
        logger.warning("eod_resolver: dry-run candle fetch failed: %s", exc)
        out["errors"] += sum(len(by_symbol_rows[s["symbol"]]) for s in specs)
        return out

    outcomes: Counter[str] = Counter()
    for spec in specs:
        symbol = spec["symbol"]
        closes = candles.get(symbol, [])
        if not closes:
            out["errors"] += len(by_symbol_rows[symbol])
            continue
        for sig in by_symbol_rows[symbol]:
            post = anchor_post_entry_closes(closes, sig.get("ts"), now)
            if post is None:
                out["windowed_out"] += 1
                continue
            if _resolver_basis() == "intrabar":
                _b = anchor_post_entry_bars(
                    _dry_ohlcv.get(symbol) or [], sig.get("ts"), now,
                )
                # Same fallback as the writing resolver: no OHLC -> judge on
                # closes rather than dropping the signal, so the dry run's counts
                # stay comparable to what the writing pass would do.
                verdict, _ = _judge_bars(sig, _b) if _b else _judge(sig, post)
            else:
                verdict, _ = _judge(sig, post)
            if verdict is None:
                out["errors"] += 1
            elif verdict == "pending":
                out["insufficient_bars"] += 1
            else:
                outcomes[verdict] += 1
                if verdict in ("tp_hit", "sl_hit"):
                    sym_c = out["by_symbol"].setdefault(symbol, {"tp_hit": 0, "sl_hit": 0})
                    sym_c[verdict] += 1
                    if sig.get("qualified"):
                        out["qualified_tp_sl"] += 1
                    # Kairos G2 tier (migration 168): qualified | suppressed |
                    # gate_fail. Suppressed = passed every gate but was held
                    # back (suppress_reason); gate_fail = failed ≥ 1 gate.
                    # NULL on pre-G2 rows → "unknown".
                    tier = str(sig.get("gate_outcome") or "unknown")
                    tier_c = out["by_gate"].setdefault(tier, {"tp_hit": 0, "sl_hit": 0})
                    tier_c[verdict] += 1
    out["by_outcome"] = dict(outcomes)
    out["would_resolve"] = sum(outcomes.values())
    return out


async def main() -> None:
    """CLI entry: resolve pending signals and print the summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = await resolve_pending_signals()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
