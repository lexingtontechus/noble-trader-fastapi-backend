"""tdva_feed.py — shared TDVA (TradingView) OHLCV loader for research tooling.

Why this module exists
──────────────────────
Two research harnesses need hourly OHLCV and both used to pull a DIFFERENT
price feed than production:

  • ``regime_edge/`` (the edge lab)   → yfinance proxies (GC=F, SI=F, ES=F …)
  • ``vol_backtest/`` (continuous vol test) → yfinance too

A benchmark that measures a different feed than the live path measures a
different quantity — the live orchestrator's sole price source is TDVA
(``price_fetcher.fetch_tradingview_batch``), so the HAR bar / regime values
must be computed on the same bars the live sweep would act on.  That is
exactly the argument in ``docs/scopes/20261019_CONTINUOUS_VOL_TEST_PLAN.md``
§1 ("yfinance is not the repo's data source").  This module is the single
TDVA read path that replaces both.

What it guarantees
──────────────────
* **One symbol mapping.** Symbols resolve through ``nt_symbol.tradingview_symbol``
  (authoritative, migration 028) via ``supabase_client.fetch_active_symbols`` —
  never a hardcoded proxy table.  A symbol with no TV mapping is reported and
  skipped, not silently substituted.
* **One HTTP client.** The fetch goes through the orchestrator's own
  ``fetch_tradingview_batch``, so auth, 429 retry, chunking and the
  ascending-order normalisation are identical to the live sweep.  No second
  implementation of the rate-limit/retry policy can drift from it.
* **One output convention.** Frames come back in the repo's cached-CSV shape
  (``timestamp,open,high,low,close,volume`` — UTC ISO-8601 ``Z`` strings, 1h
  bars, ascending), so ``regime_edge/data.py`` and the vol-backtest CSV path
  consume TDVA and CSV identically and the engine never knows the source.

Known limitation — bar-count ceiling
────────────────────────────────────
TDVA's batch endpoint takes a bar COUNT (``range``), not a date window, and
``price_fetcher`` caps it at ``min(hours, 5000)``.  One call therefore yields at
most **5000 1h bars ≈ 208 days**.  The edge lab's yfinance baseline used a
730-day window (~13–17k bars), so a TDVA run is a materially shorter sample —
expect fewer walk-forward slots and a wider CI.  ``hours`` is a parameter so a
future paginated/wider fetch can raise it without touching callers; do NOT
raise the shared ``min(hours, 5000)`` cap in ``price_fetcher`` for this, that
cap also bounds the live sweep's request size.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger("regime.tdva_feed")

# TDVA's batch endpoint returns at most this many bars per request (see
# price_fetcher.range_bars = min(hours, 5000)).
MAX_TDVA_BARS = 5000

# The cached-CSV convention shared with data/regime_edge/*.csv and
# data/vol_backtest/*.csv.  Keep in sync with regime_edge/data.py's reader.
OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _iso_utc(epoch_seconds: float) -> str:
    """Unix seconds → ``YYYY-MM-DDTHH:MM:SSZ`` (the cached-CSV convention)."""
    return (
        dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


async def load_symbol_configs(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Return ``nt_symbol`` configs for ``symbols`` (default: all active).

    Each dict carries at least ``symbol`` and ``tradingview_symbol``.  Symbols
    that are inactive or have no TV mapping are logged and omitted — the caller
    decides whether an empty result is fatal.
    """
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    rows = await fetch_active_symbols()
    if not rows:
        return []

    wanted = {s.upper() for s in symbols} if symbols else None
    configs: list[dict[str, Any]] = []
    for row in rows:
        sym = (row.get("symbol") or "").upper()
        tv = row.get("tradingview_symbol")
        if wanted is not None and sym not in wanted:
            continue
        if not tv:
            logger.warning("tdva_feed: %s has no tradingview_symbol — skipped", sym)
            continue
        configs.append({"symbol": sym, "tradingview_symbol": tv})
    return configs


def _rows_to_frame(symbol: str, rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Turn ``ohlcv_out[symbol]`` (``{t,o,h,l,c,v}`` dicts) into a CSV-convention frame.

    Bars with no parseable timestamp are dropped (the engine treats the file as
    a plain bar sequence, so a bar without a time is unusable); OHLC fields are
    coerced with ``pd.to_numeric`` so a ``None`` becomes ``NaN`` rather than
    poisoning the dtype.
    """
    kept = [r for r in rows if r.get("t") is not None]
    if not kept:
        return pd.DataFrame(columns=OHLCV_COLUMNS)

    frame = pd.DataFrame(
        {
            "timestamp": [_iso_utc(float(r["t"])) for r in kept],
            "open": [r.get("o") for r in kept],
            "high": [r.get("h") for r in kept],
            "low": [r.get("l") for r in kept],
            "close": [r.get("c") for r in kept],
            "volume": [r.get("v") for r in kept],
        }
    )
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["volume"] = frame["volume"].fillna(0.0)
    return frame[OHLCV_COLUMNS].sort_values("timestamp").reset_index(drop=True)


async def fetch_ohlcv(
    symbols: list[str],
    hours: int = MAX_TDVA_BARS,
    interval: str = "1h",
) -> dict[str, pd.DataFrame]:
    """Fetch hourly OHLCV for ``symbols`` from TDVA.

    Returns ``{internal_symbol: DataFrame[timestamp,open,high,low,close,volume]}``
    in the cached-CSV convention.  Symbols that fail (no mapping, API error,
    empty history) are absent from the result — callers must handle a short
    dict rather than assuming the requested set came back.
    """
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

    requested = [s.upper() for s in symbols]
    configs = await load_symbol_configs(requested)
    if not configs:
        logger.error(
            "tdva_feed: no usable TDVA symbol configs for %s — nothing fetched",
            requested,
        )
        return {}

    ohlcv: dict[str, list[dict[str, Any]]] = {}
    await fetch_tradingview_batch(
        configs,
        hours=min(hours, MAX_TDVA_BARS),
        interval=interval,
        include_timestamps=False,
        ohlcv_out=ohlcv,
    )

    out: dict[str, pd.DataFrame] = {}
    for sym in requested:
        rows = ohlcv.get(sym)
        if not rows:
            logger.warning("tdva_feed: %s returned no OHLCV bars", sym)
            continue
        frame = _rows_to_frame(sym, rows)
        if frame.empty:
            logger.warning("tdva_feed: %s had bars but none with a timestamp", sym)
            continue
        out[sym] = frame
    return out


def write_cached_csvs(frames: dict[str, pd.DataFrame], data_dir: str) -> list[str]:
    """Write ``{SYM: frame}`` to ``<data_dir>/<SYM>.csv`` (the cached-CSV convention).

    Returns the paths written.  Callers decide whether to overwrite an existing
    file — the 5-symbol edge-lab baseline is a committed determinism witness
    and must not be silently replaced.
    """
    import os

    os.makedirs(data_dir, exist_ok=True)
    written: list[str] = []
    for sym, frame in frames.items():
        path = os.path.join(data_dir, f"{sym}.csv")
        frame.to_csv(path, index=False)
        written.append(path)
    return written
