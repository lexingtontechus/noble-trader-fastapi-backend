"""
kairos.py — Kairos v3 P0 capture helpers (worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md).

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
  atr_ratio_run  ATR now ÷ ATR at the start of the current cell run (run
                 tracker, persisted across restarts by regime_state_store
                 since 2026-09-25; cold only after a > max-age outage).
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
KAIROS_RANK_COLUMNS: tuple[str, ...] = ("kairos_rank_score", "kairos_rank_gate_shadow")
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


PATH_HORIZONS: tuple[int, ...] = (4, 12, 24, 48)
PATH_COLUMNS: tuple[str, ...] = tuple(f"{k}_{h}h_pct" for k in ("mae", "mfe") for h in PATH_HORIZONS)


def path_excursions(entry_price: float, bars: Sequence[dict[str, Any]], is_buy: bool,
                    horizons: Sequence[int] = PATH_HORIZONS) -> dict[str, float | None]:
    """Fixed-horizon max adverse / favourable excursion, % of entry, ≥ 0
    (migration 170, worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md).

    ``bars[0]`` is the entry bar (excluded: its range includes prices from
    before the signal); the window is bars 1..h, ending on the same close as
    ``drift_fields``' drift_h, so drift_h ∈ [−mae_h, +mfe_h]. Independent of
    TP/SL: every bar is walked no matter where a bracket would have closed.
    A missing high/low on any bar in the window makes that horizon None
    (an under-stated excursion would read as safer than it was)."""
    out: dict[str, float | None] = {f"{k}_{h}h_pct": None for k in ("mae", "mfe") for h in horizons}
    if not entry_price or entry_price <= 0:
        return out
    for h in horizons:
        if h >= len(bars):
            continue
        win = bars[1:h + 1]
        try:
            hi = max(float(b["h"]) for b in win)
            lo = min(float(b["l"]) for b in win)
        except (TypeError, ValueError, KeyError):
            continue
        up, down = max(0.0, hi - entry_price), max(0.0, entry_price - lo)
        fav, adv = (up, down) if is_buy else (down, up)
        out[f"mae_{h}h_pct"] = round(adv / entry_price * 100.0, 4)
        out[f"mfe_{h}h_pct"] = round(fav / entry_price * 100.0, 4)
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

    def to_dict(self) -> dict[str, list]:
        return {s: [c, t, a] for s, (c, t, a) in self._runs.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KairosRunTracker":
        obj = cls()
        for s, v in (d or {}).items():
            try:
                obj._runs[s] = (str(v[0]), float(v[1]), float(v[2]) if v[2] else None)
            except (TypeError, ValueError, IndexError):
                continue
        return obj


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


async def backfill_drift_horizons(max_rows: int = 12000, concurrency: int = 8,
                                  min_age_h: int = 2, max_age_h: int = 199) -> dict[str, int]:
    """Fill NULL drift_{1,4,12,24,48}h_pct on light-sweep buy/sell rows whose
    entry is ``min_age_h``–``max_age_h`` old, for every horizon whose bar
    already exists — resolved OR still pending (2026-09-25 accelerated G2:
    the resolver only writes drift when a signal settles, so young rows had
    drift only when TP/SL hit early, a geometry-selected subset). Only NULL
    fields are written — existing values are never overwritten, and the
    only other drift consumer (calibration_bias_bucket) filters
    ``outcome IS NOT NULL``. Timestamp-anchored. Never raises."""
    import asyncio

    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers,
        _url,
        fetch_active_symbols,
    )

    summary = {"candidates": 0, "patched": 0, "no_bars": 0, "errors": 0, "path_cols": 1}
    if not settings.SUPABASE_URL:
        return summary
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lo = (now - timedelta(hours=max_age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (now - timedelta(hours=min_age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = "signal_id,symbol,signal,ts,entry_price," + ",".join(f"drift_{h}h_pct" for h in DRIFT_HORIZONS)

    async def _query(with_path: bool) -> list[dict[str, Any]]:
        cols = base + ("," + ",".join(PATH_COLUMNS) if with_path else "")
        null_filter = (("or", "(drift_48h_pct.is.null,mae_48h_pct.is.null)") if with_path
                       else ("drift_48h_pct", "is.null"))
        out: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            off = 0
            while len(out) < max_rows:
                resp = await client.get(_url("nt_signal_sim"), headers=_headers(), params=[
                    ("select", cols), null_filter,
                    ("signal", "in.(buy,sell)"), ("source", "eq.sweep-light"),
                    ("ts", f"gte.{lo}"), ("ts", f"lte.{hi}"), ("order", "ts.asc"),
                    ("limit", "1000"), ("offset", str(off))])
                resp.raise_for_status()
                page = resp.json() or []
                out += page
                if len(page) < 1000:
                    break
                off += 1000
        return out

    try:
        try:
            rows = await _query(True)
        except httpx.HTTPStatusError as exc:
            # Migration 170 not applied yet (unknown column) → drift only.
            if exc.response.status_code != 400:
                raise
            summary["path_cols"] = 0
            rows = await _query(False)
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
    ohlc: dict[str, list[dict[str, Any]]] = {}
    try:
        closes, stamps = await fetch_tradingview_batch(specs, hours=240, include_timestamps=True, ohlcv_out=ohlc)
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
        is_buy = str(r.get("signal") or "buy").lower() not in ("sell", "short")
        post = cs[i:-1] if len(cs) - 1 > i else []          # drop the forming bar
        vals = drift_fields(entry, post, is_buy)
        bars = ohlc.get(r["symbol"]) or []
        if summary["path_cols"] and len(bars) == len(cs):
            vals.update(path_excursions(entry, bars[i:-1] if len(bars) - 1 > i else [], is_buy))
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


# ─── Job heartbeat (2026-09-25) ──────────────────────────────────────────


def heartbeat_state(summary: dict[str, Any], now: datetime | None = None) -> str:
    """One-line current_state for the nt_roadmap heartbeat row."""
    now = now or datetime.now(timezone.utc)
    return f"last_run {now.strftime('%Y-%m-%dT%H:%M:%SZ')} · " + json.dumps(summary, default=str)[:900]


async def write_job_heartbeat(mechanism: str, summary: dict[str, Any]) -> bool:
    """PATCH nt_roadmap(mechanism).current_state/updated_at — no new table.
    The row is created once by hand (worklog/kairos/…_ACCELERATED_G2.md); a
    missing row just returns False. Never raises."""
    try:
        import httpx

        from regime_platform.core.settings import settings
        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url
        if not settings.SUPABASE_URL:
            return False
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                _url("nt_roadmap"), headers={**_headers(), "Prefer": "return=minimal"},
                params={"mechanism": f"eq.{mechanism}"},
                json={"current_state": heartbeat_state(summary, now), "updated_at": now.isoformat()})
        return resp.is_success
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kairos] heartbeat %s failed: %s", mechanism, exc)
        return False
