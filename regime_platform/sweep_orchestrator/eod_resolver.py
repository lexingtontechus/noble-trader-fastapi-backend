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
    `EXPIRY_BARS` (48) post-entry 1h bars is marked ``expired``.
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

logger = logging.getLogger("regime.sweep.eod_resolver")

# ─── Constants ────────────────────────────────────────────────────

EXPIRY_BARS = 48          # post-entry 1h bars before a signal is 'expired'
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
})

# Columns of nt_calibration_records we write (mirrors renko/trade_journal.py
# + migration 031 venue column).
CALIBRATION_RECORD_COLUMNS: frozenset[str] = frozenset({
    "symbol", "venue", "predicted_p_win", "outcome",
    "entry_price", "exit_price", "entered_at", "closed_at",
    "actual_pnl", "error_source",
})


# ─── Pure resolution logic (network-free, unit-tested) ────────────

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
    check runs BEFORE the SL check (same-bar TP priority convention), so:

      * BUY  (TP above entry):  ``tp_hit`` when ``close >= take_profit``,
        ``sl_hit`` when ``close <= stop_loss``.
      * SELL (TP below entry):  ``tp_hit`` when ``close <= take_profit``,
        ``sl_hit`` when ``close >= stop_loss``.

    Whichever level's bar comes first wins; if neither is touched within
    ``expiry_bars`` the signal is ``expired``. Any value of ``direction``
    that is not sell/short is treated as buy (defensive).

    Returns a dict with: ``outcome``, ``exit_price`` (the triggering
    close, ``None`` when expired), ``exit_idx`` (0-based bar index,
    ``None`` when expired), ``hold_bars`` (bars held: exit_idx+1, or the
    walked window length when expired), ``mfe`` / ``mae`` (max favorable
    / max adverse close excursion in price units, >= 0).
    """
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
        else:  # sell — TP is BELOW entry, SL is ABOVE
            if close <= take_profit:
                outcome, exit_price, exit_idx = "tp_hit", close, i
                break
            if close >= stop_loss:
                outcome, exit_price, exit_idx = "sl_hit", close, i
                break

    # Excursion is measured over the bars actually walked (through the
    # exit bar, or the whole window when expired).
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
    }


def compute_pnl(
    outcome: str,
    tp_bricks: float | None,
    sl_bricks: float | None,
    brick_size: float | None,
) -> dict[str, float]:
    """PnL in bricks and dollars for a resolved outcome.

    Convention (matches the pipeline's brick-delta model):
      * ``tp_hit``  → ``pnl_bricks = +tp_bricks``
      * ``sl_hit``  → ``pnl_bricks = -sl_bricks``
      * ``expired`` → ``pnl_bricks = 0``  (no fill, no PnL booked)
    ``abs()`` guards against a future schema that stores negative brick
    counts — a take-profit is always a gain and a stop-loss always a loss.
    ``pnl_dollars = pnl_bricks * brick_size`` (6dp).
    """
    if outcome == "tp_hit":
        pnl_bricks = abs(float(tp_bricks or 0.0))
    elif outcome == "sl_hit":
        pnl_bricks = -abs(float(sl_bricks or 0.0))
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


async def _fetch_pending(limit: int) -> list[dict[str, Any]]:
    """Rows of nt_signal_sim where outcome IS NULL, oldest first."""
    url = _url("nt_signal_sim")
    params = {
        "select": "*",
        "outcome": "is.null",
        "order": "ts.asc",
        "limit": str(int(limit)),
    }
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
        logger.error("eod_resolver: PATCH signal %s failed: %s", signal_id, exc)
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
    payload = _filter_payload({
        "symbol": symbol,
        "venue": sig.get("venue"),
        "predicted_p_win": _as_float(sig.get("p_win")),
        "outcome": outcome,
        "entry_price": _as_float(sig.get("entry_price")),
        "exit_price": resolved.get("exit_price"),
        "entered_at": entered_at,
        "closed_at": shift_ts(sig.get("ts"), resolved.get("hold_bars") or 0),
        "actual_pnl": pnl.get("pnl_dollars"),
        "error_source": "eod_resolver",
    }, CALIBRATION_RECORD_COLUMNS)
    ok = await _insert_row("nt_calibration_records", payload)
    if not ok:
        logger.warning(
            "eod_resolver: calibration record %s for %s failed",
            outcome, symbol,
        )


async def _resolve_one(sig: dict[str, Any], closes: list[float]) -> str | None:
    """Resolve + persist one signal. Returns outcome key, or None on error."""
    entry = _as_float(sig.get("entry_price"))
    stop = _as_float(sig.get("stop_loss"))
    tp = _as_float(sig.get("take_profit"))
    if entry is None or stop is None or tp is None:
        logger.warning(
            "eod_resolver: signal %s missing entry/stop/tp — left pending",
            sig.get("signal_id"),
        )
        return None

    resolved = resolve_signal_outcome(
        entry, stop, tp,
        closes,
        direction=sig.get("signal") or "buy",
        expiry_bars=EXPIRY_BARS,
    )
    pnl = compute_pnl(
        resolved["outcome"],
        sig.get("tp_bricks"), sig.get("sl_bricks"), sig.get("brick_size"),
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
    }, NT_SIGNAL_SIM_COLUMNS)

    if not await _patch_signal(sig.get("signal_id"), patch):
        return None

    if resolved["outcome"] in ("tp_hit", "sl_hit"):
        await _write_calibration_record(sig, resolved, pnl)

    return resolved["outcome"]


# ─── Orchestrator entry point ─────────────────────────────────────

async def resolve_pending_signals(limit: int = 200) -> dict[str, Any]:
    """Forward-resolve up to ``limit`` pending nt_signal_sim rows.

    Best-effort end-to-end:

      1. Query ``nt_signal_sim`` WHERE ``outcome IS NULL`` ORDER BY ts ASC.
      2. Group by symbol and fetch post-entry 1h closes per symbol
         (``fetch_candles_for_symbols``, `LIGHT_BARS` bars).
      3. Close-walk TP/SL per signal (48-bar expiry).
      4. PATCH outcome/exit_*/hold_bars/mfe/mae/pnl_*/resolved_at back.
      5. Insert nt_calibration_records for tp_hit (win) / sl_hit (loss).

    Returns ``{resolved, by_outcome, errors}``. Never raises — every
    failure path is caught and counted in ``errors``.
    """
    summary: dict[str, Any] = {"resolved": 0, "by_outcome": {}, "errors": 0}
    if not settings.SUPABASE_URL:
        logger.warning("eod_resolver: Supabase not configured — nothing to resolve")
        return summary

    try:
        rows = await _fetch_pending(limit)
    except Exception as exc:
        logger.error("eod_resolver: pending query failed: %s", exc)
        summary["errors"] += 1
        return summary
    if not rows:
        logger.info("eod_resolver: no pending signals (limit=%d)", limit)
        return summary

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
    for symbol, sigs in by_symbol.items():
        tv = tv_map.get(symbol)
        if not tv:
            logger.warning(
                "eod_resolver: no tradingview_symbol for %s — %d pending unresolved",
                symbol, len(sigs),
            )
            summary["errors"] += len(sigs)
            continue
        try:
            batch = await fetch_candles_for_symbols(
                [{"symbol": symbol, "tradingview_symbol": tv}],
                n_bars=LIGHT_BARS,
                interval=INTERVAL,
            )
        except Exception as exc:
            logger.error("eod_resolver: candle fetch failed for %s: %s", symbol, exc)
            summary["errors"] += len(sigs)
            continue
        closes = batch.get(symbol, [])
        if not closes:
            logger.warning(
                "eod_resolver: no candles returned for %s — %d pending unresolved",
                symbol, len(sigs),
            )
            summary["errors"] += len(sigs)
            continue

        for sig in sigs:
            try:
                outcome = await _resolve_one(sig, closes)
                if outcome is None:
                    summary["errors"] += 1
                else:
                    summary["resolved"] += 1
                    outcomes[outcome] += 1
            except Exception as exc:
                logger.error(
                    "eod_resolver: signal %s failed: %s",
                    sig.get("signal_id"), exc,
                )
                summary["errors"] += 1

    summary["by_outcome"] = dict(outcomes)
    logger.info(
        "eod_resolver: resolved %d pending (%s), errors=%d",
        summary["resolved"], summary["by_outcome"], summary["errors"],
    )
    return summary


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
