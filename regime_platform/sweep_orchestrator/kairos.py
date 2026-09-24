"""
kairos.py — Kairos v3 P0 capture helpers (docs/scopes/20260924_KAIROS_V3_SCOPE.md).

Geometry-free, read-only-for-trading helpers. Nothing here changes
qualification, sizing, SL/TP or delivery; it only computes values that are
STAMPED on nt_signal_sim (shadow) and caches the economic calendar.

Frozen definitions (scope §6 P0.1, frozen 2026-09-24). The research scripts
(scripts/kairos_discovery.py) import these same functions so live stamps and
offline panels can never drift apart:

  bars           1h OHLCV dicts {"t","o","h","l","c","v"}, ascending. The LAST
                 element is the still-forming bar in the live sweep; every
                 factor uses COMPLETED bars only (index -2 and earlier).
  cell           vol tier × direction from the HMM regime label, e.g.
                 "med_high_vol|bear" (strength collapsed).
  clv            ((C−L) − (H−C)) / (H−L) of the last completed bar; 0 when H=L.
  efficiency_24  |C_t − C_{t−24}| / Σ|ΔC| over the last 24 completed bars
                 (Kaufman efficiency ratio); None if < 25 completed bars.
  rel_volume     volume of the last completed bar ÷ median volume of the
                 completed bars in the window with the same UTC hour-of-day
                 (excluding itself); None if < 3 same-hour samples.
  atr_ratio_run  ATR now ÷ ATR at the start of the current cell run (in-memory
                 run tracker; resets to 1.0 on a process restart).
  spread_bps     (ask − bid)/mid × 1e4 from the /api/quote/batch call the HMM
                 already makes each sweep (no extra API call).
  drift_h        signed close move h completed bars after the entry bar, % of
                 entry (eod_resolver._drift_fields; h ∈ 1, 4, 12, 24, 48).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

DRIFT_HORIZONS: tuple[int, ...] = (1, 4, 12, 24, 48)
KAIROS_CAPTURE_COLUMNS: tuple[str, ...] = (
    "rel_volume_1h", "clv_1h", "efficiency_24h", "atr_ratio_run", "spread_bps",
)
KAIROS_DRIFT_COLUMNS: tuple[str, ...] = ("drift_12h_pct", "drift_48h_pct")


# ─── Frozen definitions: pure functions ──────────────────────────────────


def regime_cell(label: str | None) -> str | None:
    """HMM regime label → "vol_tier|direction" (strength collapsed)."""
    if not isinstance(label, str) or "_vol_" not in label:
        return None
    vol = label.split("_vol_")[0] + "_vol"
    low = label.lower()
    direction = "bull" if "bull" in low else ("bear" if "bear" in low else "flat")
    return f"{vol}|{direction}"


def _completed(bars: Sequence[dict[str, Any]], forming_last: bool) -> list[dict[str, Any]]:
    return list(bars[:-1]) if forming_last and len(bars) > 0 else list(bars)


def clv(bar: dict[str, Any]) -> float | None:
    """Close-location value of one bar in [-1, 1]."""
    try:
        h, low, c = float(bar["h"]), float(bar["l"]), float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    rng = h - low
    if not math.isfinite(rng) or rng < 0:
        return None
    if rng == 0:
        return 0.0
    return ((c - low) - (h - c)) / rng


def efficiency_ratio(closes: Sequence[float], n: int = 24) -> float | None:
    """|C_t − C_{t−n}| / Σ|ΔC| over the last n steps; None if too short."""
    if len(closes) < n + 1:
        return None
    w = [float(x) for x in closes[-(n + 1):]]
    path = sum(abs(b - a) for a, b in zip(w[:-1], w[1:]))
    if path <= 0:
        return 0.0
    return abs(w[-1] - w[0]) / path


def relative_volume(bars: Sequence[dict[str, Any]], min_samples: int = 3) -> float | None:
    """Last bar's volume ÷ median volume of earlier bars at the same UTC hour."""
    if len(bars) < 2:
        return None
    last = bars[-1]
    try:
        v_last = float(last.get("v"))
        hour = datetime.fromtimestamp(float(last["t"]), tz=timezone.utc).hour
    except (TypeError, ValueError, KeyError):
        return None
    same = []
    for b in bars[:-1]:
        try:
            if datetime.fromtimestamp(float(b["t"]), tz=timezone.utc).hour == hour and b.get("v") is not None:
                same.append(float(b["v"]))
        except (TypeError, ValueError, KeyError):
            continue
    if len(same) < min_samples:
        return None
    med = statistics.median(same)
    if med <= 0 or not math.isfinite(v_last):
        return None
    return v_last / med


def capture_factors(
    bars: Sequence[dict[str, Any]] | None,
    *,
    atr_now: float | None,
    atr_run_start: float | None,
    spread_bps: float | None,
    forming_last: bool = True,
) -> dict[str, float | None]:
    """The five P0.5 stamps. Every value is None when it can't be computed —
    callers drop None keys so a missing input never writes a fake 0."""
    out: dict[str, float | None] = {k: None for k in KAIROS_CAPTURE_COLUMNS}
    done = _completed(bars or [], forming_last)
    if done:
        c = clv(done[-1])
        out["clv_1h"] = round(c, 4) if c is not None else None
        er = efficiency_ratio([b["c"] for b in done if b.get("c") is not None], 24)
        out["efficiency_24h"] = round(er, 4) if er is not None else None
        rv = relative_volume(done)
        out["rel_volume_1h"] = round(rv, 4) if rv is not None else None
    if atr_now and atr_run_start and atr_now > 0 and atr_run_start > 0:
        out["atr_ratio_run"] = round(float(atr_now) / float(atr_run_start), 4)
    if spread_bps is not None and math.isfinite(spread_bps) and spread_bps > 0:
        out["spread_bps"] = round(float(spread_bps), 3)
    return out


def drift_fields(entry_price: float, closes: Sequence[float], is_buy: bool,
                 horizons: Sequence[int] = DRIFT_HORIZONS) -> dict[str, float | None]:
    """Signed % move of close[h] vs entry (closes[0] is the entry bar)."""
    out: dict[str, float | None] = {}
    for h in horizons:
        if h >= len(closes) or not entry_price:
            out[f"drift_{h}h_pct"] = None
            continue
        c = float(closes[h])
        signed = (c - entry_price) if is_buy else (entry_price - c)
        out[f"drift_{h}h_pct"] = round(signed / entry_price * 100.0, 4)
    return out


# ─── Run tracker (in-memory, per symbol) ─────────────────────────────────


class KairosRunTracker:
    """Tracks the current regime-cell run per symbol: start time and ATR at
    start. In-memory only (same lifecycle as RegimeLabelDebouncer): a restart
    starts every symbol's run afresh, which v_kairos_regime_runs flags as
    ``starts_after_gap`` from the DB side."""

    def __init__(self) -> None:
        self._runs: dict[str, tuple[str, float, float | None]] = {}

    def update(self, symbol: str, regime_label: str | None, now_ts: float,
               atr: float | None) -> tuple[float | None, float | None]:
        """Feed this tick's final regime label. Returns (age_hours, atr_at_start)."""
        cell = regime_cell(regime_label)
        if cell is None:
            return None, None
        cur = self._runs.get(symbol)
        if cur is None or cur[0] != cell:
            self._runs[symbol] = (cell, float(now_ts), float(atr) if atr else None)
            cur = self._runs[symbol]
        elif cur[2] is None and atr:
            self._runs[symbol] = (cell, cur[1], float(atr))
            cur = self._runs[symbol]
        return (float(now_ts) - cur[1]) / 3600.0, cur[2]


# ─── Economic calendar cache (P0.6, daily) ───────────────────────────────

CALENDAR_CURRENCIES = frozenset({"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "CNY"})
DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[2] / ".cache" / "kairos" / "econ_calendar.json"


def _calendar_keep(ev: dict[str, Any]) -> bool:
    try:
        return int(ev.get("importance", -1)) >= 0 and str(ev.get("currency") or "").upper() in CALENDAR_CURRENCIES
    except (TypeError, ValueError):
        return False


def _calendar_slim(ev: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "date", "country", "currency", "title", "indicator", "category", "importance",
            "actual", "forecast", "previous", "actualRaw", "forecastRaw", "previousRaw", "period")
    return {k: ev.get(k) for k in keys}


def merge_calendar(existing: list[dict[str, Any]], fetched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge by event id; the newer fetch wins (actuals arrive after release)."""
    by_id = {str(e.get("id")): e for e in existing if e.get("id") is not None}
    for e in fetched:
        if e.get("id") is not None and _calendar_keep(e):
            by_id[str(e["id"])] = _calendar_slim(e)
    return sorted(by_id.values(), key=lambda e: (str(e.get("date") or ""), str(e.get("id"))))


async def fetch_calendar_window(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """GET /api/calendar/economic in ≤7-day chunks (unix seconds — date
    strings silently return status=no_data)."""
    import httpx

    from regime_platform.core.settings import settings

    headers = {"x-rapidapi-key": settings.TRADINGVIEW_API_KEY,
               "x-rapidapi-host": settings.TRADINGVIEW_API_HOST}
    url = f"{settings.TRADINGVIEW_BASE_URL}/api/calendar/economic"
    out: list[dict[str, Any]] = []
    cur = start
    async with httpx.AsyncClient(timeout=30.0) as client:
        while cur < end:
            nxt = min(cur + timedelta(days=7), end)
            resp = await client.get(url, headers=headers,
                                    params={"from": int(cur.timestamp()), "to": int(nxt.timestamp())})
            resp.raise_for_status()
            data = (resp.json() or {}).get("data") or {}
            out.extend(data.get("result") or [])
            cur = nxt
    return out


async def refresh_calendar_cache(path: Path | None = None, *, days_back: int = 3,
                                 days_ahead: int = 14,
                                 start: datetime | None = None) -> dict[str, int]:
    """Pull [now−days_back, now+days_ahead] (or from ``start``) and merge into
    the JSON cache. Returns counts. Never raises."""
    path = path or DEFAULT_CALENDAR_PATH
    now = datetime.now(timezone.utc)
    s = start or (now - timedelta(days=days_back))
    try:
        fetched = await fetch_calendar_window(s, now + timedelta(days=days_ahead))
        existing = json.loads(path.read_text()) if path.exists() else []
        merged = merge_calendar(existing, fetched)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged))
        logger.info("[kairos:calendar] fetched %d, cache now %d events (%s)",
                    len(fetched), len(merged), path)
        return {"fetched": len(fetched), "cached": len(merged)}
    except Exception as exc:
        logger.warning("[kairos:calendar] refresh failed: %s: %s", type(exc).__name__, exc)
        return {"fetched": 0, "cached": -1}


# ─── Drift-horizon backfill (P0.3) ───────────────────────────────────────


def anchor_by_timestamp(bar_ts: Sequence[float | None], signal_epoch: float) -> int | None:
    """Index of the bar containing ``signal_epoch`` (open ≤ t < open+3600).
    Timestamp-based, so it is correct across FX weekend gaps."""
    for i in range(len(bar_ts) - 1, -1, -1):
        t = bar_ts[i]
        if t is None:
            continue
        if t <= signal_epoch < t + 3600:
            return i
        if t < signal_epoch:
            return None
    return None


async def backfill_drift_horizons(max_rows: int = 5000, concurrency: int = 8) -> dict[str, int]:
    """Fill NULL drift_{1,4,12,24,48}h_pct on resolved nt_signal_sim rows
    whose entry is 49–199h old (the resolver often settles a TP/SL touch
    before 24/48 bars exist, leaving later horizons NULL). Only NULL fields
    are written — existing values are never overwritten. Timestamp-anchored.
    Never raises."""
    import asyncio

    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers,
        _url,
        fetch_active_symbols,
    )

    summary = {"candidates": 0, "patched": 0, "no_bars": 0, "errors": 0}
    if not settings.SUPABASE_URL:
        return summary
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lo = (now - timedelta(hours=199)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (now - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = "signal_id,symbol,signal,ts,entry_price," + ",".join(f"drift_{h}h_pct" for h in DRIFT_HORIZONS)
    rows: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            off = 0
            while len(rows) < max_rows:
                resp = await client.get(_url("nt_signal_sim"), headers=_headers(), params=[
                    ("select", cols), ("outcome", "not.is.null"), ("drift_48h_pct", "is.null"),
                    ("ts", f"gte.{lo}"), ("ts", f"lte.{hi}"), ("order", "ts.asc"),
                    ("limit", "1000"), ("offset", str(off))])
                resp.raise_for_status()
                page = resp.json() or []
                rows += page
                if len(page) < 1000:
                    break
                off += 1000
    except Exception as exc:
        logger.warning("[kairos:drift-backfill] query failed: %s", exc)
        summary["errors"] += 1
        return summary
    rows = rows[:max_rows]
    summary["candidates"] = len(rows)
    if not rows:
        return summary

    tv = {s.get("symbol"): s.get("tradingview_symbol") for s in await fetch_active_symbols()}
    specs = [{"symbol": s, "tradingview_symbol": tv[s]} for s in sorted({r["symbol"] for r in rows}) if tv.get(s)]
    try:
        closes, stamps = await fetch_candles_for_symbols(specs, n_bars=240, include_timestamps=True)
    except Exception as exc:
        logger.warning("[kairos:drift-backfill] candle fetch failed: %s", exc)
        summary["errors"] += 1
        return summary

    patches: list[tuple[str, dict[str, float]]] = []
    for r in rows:
        cs, ts = closes.get(r["symbol"]), stamps.get(r["symbol"])
        if not cs or not ts:
            summary["no_bars"] += 1
            continue
        try:
            sig_epoch = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
            entry = float(r["entry_price"])
        except (TypeError, ValueError):
            summary["errors"] += 1
            continue
        i = anchor_by_timestamp(ts, sig_epoch)
        if i is None:
            summary["no_bars"] += 1
            continue
        post = cs[i:-1] if len(cs) - 1 > i else []          # drop the forming bar
        vals = drift_fields(entry, post, str(r.get("signal") or "buy").lower() not in ("sell", "short"))
        fill = {k: v for k, v in vals.items() if v is not None and r.get(k) is None}
        if fill:
            patches.append((r["signal_id"], fill))

    sem = asyncio.Semaphore(concurrency)

    async def _patch(client, sid, body):
        async with sem:
            try:
                resp = await client.patch(_url("nt_signal_sim"), headers=_headers(),
                                          params={"signal_id": f"eq.{sid}"}, json=body)
                if resp.is_success:
                    summary["patched"] += 1
                else:
                    summary["errors"] += 1
            except Exception:
                summary["errors"] += 1

    async with httpx.AsyncClient(timeout=10.0) as client:
        await asyncio.gather(*(_patch(client, sid, body) for sid, body in patches))
    logger.info("[kairos:drift-backfill] %s", summary)
    return summary


def epoch_now() -> float:
    return time.time()
