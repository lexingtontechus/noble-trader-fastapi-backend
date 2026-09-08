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
    # drift_*_pct (migration 121, Phase 2): direction-relative post-entry
    # price drift at fixed horizons, independent of trade outcome.
    "drift_1h_pct", "drift_4h_pct", "drift_24h_pct",
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
    / max adverse close excursion in price units, >= 0), and
    ``drift_1h_pct``/``drift_4h_pct``/``drift_24h_pct`` (direction-
    relative price drift at fixed post-entry horizons, as % of
    entry_price — positive means the market moved in the signal's
    favor. Computed over the FULL fetched window, not the outcome-
    truncated walk, since this measures post-entry market behavior
    independent of when/whether the trade itself exited. ``None`` when
    fewer than that many post-entry bars were available.
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

    # 2026-09-08 fix: an expired signal is still open at the last walked
    # close — that price was always available here (it's how mfe/mae
    # below get computed) but was previously discarded, leaving
    # exit_price permanently NULL for every expired row (confirmed live:
    # 59,913 of 59,913 expired signals had exit_price=NULL). Capture it
    # so compute_pnl can score a real mark-to-market outcome instead of a
    # fixed 0 — "expired" still means "never touched TP/SL," it no longer
    # means "financially nothing happened."
    if outcome == "expired" and window:
        exit_price = float(window[-1])

    # Excursion is measured over the bars actually walked (through the
    # exit bar, or the whole window when expired).
    walked = window if exit_idx is None else window[: exit_idx + 1]
    if is_buy:
        mfe = max((float(c) - entry_price for c in walked), default=0.0)
        mae = max((entry_price - float(c) for c in walked), default=0.0)
    else:
        mfe = max((entry_price - float(c) for c in walked), default=0.0)
        mae = max((float(c) - entry_price for c in walked), default=0.0)

    def _drift_at(bar_offset: int) -> float | None:
        # window[0] is the entry bar's own close (anchor_post_entry_closes
        # includes it); window[bar_offset] is `bar_offset` hours later.
        if bar_offset >= len(window) or entry_price == 0:
            return None
        close = float(window[bar_offset])
        signed = (close - entry_price) if is_buy else (entry_price - close)
        return round(signed / entry_price * 100.0, 4)

    return {
        "outcome": outcome,
        "exit_price": exit_price,
        "exit_idx": exit_idx,
        "hold_bars": (exit_idx + 1) if exit_idx is not None else len(window),
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "drift_1h_pct": _drift_at(1),
        "drift_4h_pct": _drift_at(4),
        "drift_24h_pct": _drift_at(24),
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

    Convention (matches the pipeline's brick-delta model):
      * ``tp_hit``  → ``pnl_bricks = +tp_bricks``
      * ``sl_hit``  → ``pnl_bricks = -sl_bricks``
      * ``expired`` → mark-to-market, not a fixed 0 (2026-09-08 fix). The
        position was still open at ``exit_price`` (the last walked close
        — see ``resolve_signal_outcome``'s 2026-09-08 fix) when the 48h
        window ran out; scoring it as a flat 0 regardless of whether it
        was actually up or down was fiction, not a "no fill" case (no
        signal here is ever unfilled — entry is the live market price at
        signal time). ``pnl_bricks = signed(exit_price - entry_price) /
        brick_size`` — same brick-unit convention as tp_hit/sl_hit, so all
        three outcome types stay directly summable downstream. Falls back
        to the old ``0.0`` only when price/brick_size data is missing
        (defensive — should not happen for any row resolved after this
        fix; historical expired rows resolved before it have no
        exit_price to compute from).
    ``abs()`` guards against a future schema that stores negative brick
    counts — a take-profit is always a gain and a stop-loss always a loss.
    ``pnl_dollars = pnl_bricks * brick_size`` (6dp).
    """
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


async def _fetch_pending(limit: int, min_ts: str | None = None) -> list[dict[str, Any]]:
    """Rows of nt_signal_sim where outcome IS NULL, oldest first.

    ``min_ts`` (ISO UTC string) bounds the fetch to signals whose entry
    falls inside the candle window the resolver will fetch — older signals
    cannot be resolved against bars that predate them (window-anchoring
    fix 2026-08-12), so they stay pending and are handled by retention
    (expired after EOD_RETENTION_STALE_DAYS).
    """
    url = _url("nt_signal_sim")
    params = {
        "select": "*",
        "outcome": "is.null",
        "order": "ts.asc",
        "limit": str(int(limit)),
    }
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
    # id is TEXT PRIMARY KEY NOT NULL — generate one here (the migration 100
    # table has no DB default). aggression is NOT NULL CHECK in
    # ('passive','mid','aggressive') — take it from the signal, default 'mid'.
    aggression = str(sig.get("aggression") or "mid").lower()
    if aggression not in ("passive", "mid", "aggressive"):
        aggression = "mid"
    # Brier score = (predicted_prob - outcome)^2; outcome ∈ {1, 0}. Uses the
    # RAW (pre-enforcement) p_win when the capture flag carried it, else the
    # stored (possibly enforced) p_win. Measures probability calibration per
    # record so the bias loop can track model accuracy independent of the
    # enforcement shrink (2026-08-13).
    pred_for_brier = _as_float(sig.get("predicted_p_win_raw"))
    if pred_for_brier is None:
        pred_for_brier = _as_float(sig.get("p_win")) or 0.5
    brier = round((1.0 - pred_for_brier) ** 2 if outcome == "win" else pred_for_brier ** 2, 6)
    payload = _filter_payload({
        "id": str(uuid.uuid4()),
        "signal_id": sig.get("signal_id"),
        "symbol": symbol,
        "venue": sig.get("venue"),
        "predicted_p_win": _as_float(sig.get("p_win")),
        "predicted_p_win_raw": _as_float(sig.get("predicted_p_win_raw")),
        "calibration_brier": brier,
        "outcome": outcome,
        "aggression": aggression,
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
    """Resolve + persist one signal. Returns outcome key, ``"pending"`` if
    it's not old enough to judge yet (see minimum-age gate below), or None
    on error."""
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
        return "pending"

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
        "drift_24h_pct": resolved.get("drift_24h_pct"),
    }, NT_SIGNAL_SIM_COLUMNS)

    if not await _patch_signal(sig.get("signal_id"), patch):
        return None

    if resolved["outcome"] in ("tp_hit", "sl_hit"):
        await _write_calibration_record(sig, resolved, pnl)

    return resolved["outcome"]


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

    while True:
        if max_total > 0 and attempted_total >= max_total:
            logger.info("eod_resolver: reached max_total=%d — stopping", max_total)
            break
        try:
            rows = await _fetch_pending(limit, min_ts=min_ts)
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

        try:
            candles_by_symbol = (
                await fetch_candles_for_symbols(
                    symbol_specs, n_bars=LIGHT_BARS, interval=INTERVAL,
                )
                if symbol_specs else {}
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
        if sum(outcomes.values()) == 0:
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
