"""data.py — price series loading for the vol backtest.

Sources, in priority order:

  1. ``--data-dir data/vol_backtest/`` — one ``<SYMBOL>.csv`` per symbol with
     columns ``timestamp,close`` (ISO timestamps) or a single ``close``
     column. This is the REAL-data path: export hourly bars from the
     platform's own history store or any market-data vendor.
  2. ``--tdva`` — hourly bars from TDVA (TradingView), the repo's SOLE
     production price source (``price_fetcher.fetch_tradingview_batch``).
     Symbols map through ``nt_symbol.tradingview_symbol``, never a proxy
     table. This is the default research source: a benchmark measured on a
     different feed than the live path measures a different quantity.
  3. ``--synthetic`` — two-state Markov-vol hourly series with the real
     weekend calendar per asset class (smoke tests, CI, methodology demos).

``--yfinance`` is RETIRED (2026-10-21): it pulled Yahoo proxies (GC=F,
JPY=X …) that the live path never sees. It remains importable so old
invocations fail loudly rather than silently re-resolving to TDVA — the
harness must not quietly switch data sources under a recorded baseline.

Series are returned as :class:`SymbolSeries` with optional timestamps; the
harness applies the calendar filter before computing returns.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .realized import SymbolSeries, synthetic_symbol

YAHOO_MAP = {
    "BTCUSD": "BTC-USD",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USDJPY": "JPY=X",
    "USDCHF": "CHF=X",
    "ETHUSD": "ETH-USD",
    "EURUSD": "EURUSD=X",
    "AUDUSD": "AUDUSD=X",
}

THESIS_SYMBOLS = ["BTCUSD", "XAGUSD", "XAUUSD", "USDJPY", "USDCHF"]

# TDVA (TradingView) is the repo's SOLE production price source.  Research
# harnesses read it through regime_platform.services.tdva_feed so the symbol
# mapping (nt_symbol.tradingview_symbol) and the HTTP/429/chunking policy stay
# identical to the live sweep.  2160 = 90 days of 1h bars (the heavy sweep's
# own window); raise via --hours if a longer sample is ever needed, subject
# to the batch endpoint's 5000-bar per-request cap.
TDVA_HOURS = 2160


def load_tdva(symbols: list[str], hours: int = TDVA_HOURS) -> list[SymbolSeries]:
    """Hourly closes from TDVA — the same feed the live path acts on.

    Replaces :func:`load_yfinance`.  Returns one :class:`SymbolSeries` per
    symbol that came back; symbols with no ``tradingview_symbol`` mapping or an
    empty/failed fetch are skipped (see ``tdva_feed.fetch_ohlcv``), so a short
    result is a data problem to surface, not to paper over.
    """
    import asyncio

    import pandas as _pd

    from regime_platform.services.tdva_feed import fetch_ohlcv

    frames = asyncio.run(fetch_ohlcv(symbols, hours=hours))

    out: list[SymbolSeries] = []
    for s in symbols:
        frame = frames.get(s.upper())
        if frame is None or frame.empty:
            continue
        out.append(
            SymbolSeries(
                symbol=s,
                prices=frame["close"].to_numpy(dtype=float),
                timestamps=_pd.to_datetime(frame["timestamp"], utc=True).to_numpy(
                    dtype="datetime64[ns]"
                ),
            )
        )
    return out


def load_symbol_csv(path: Path, symbol: str) -> SymbolSeries:
    """CSV with header ``timestamp,close`` (or bare ``close`` column)."""
    import pandas as _pd

    df = _pd.read_csv(path)
    cols = {c.strip().lower(): c for c in df.columns}
    close_col = cols.get("close") or cols.get("price") or cols.get("adj_close")
    if close_col is None:
        # single numeric column fallback
        num = [c for c in df.columns if _pd.api.types.is_numeric_dtype(df[c])]
        if len(num) != 1:
            raise ValueError(f"{path}: cannot identify close column in {list(df.columns)}")
        close_col = num[0]
    close = df[close_col].astype(float).to_numpy()
    ts = None
    ts_col = cols.get("timestamp") or cols.get("time") or cols.get("date") or cols.get("datetime")
    if ts_col is not None:
        ts = _pd.to_datetime(df[ts_col]).to_numpy(dtype="datetime64[ns]")
    return SymbolSeries(symbol=symbol, prices=close, timestamps=ts)


def load_data_dir(data_dir: Path, symbols: list[str]) -> list[SymbolSeries]:
    out: list[SymbolSeries] = []
    for s in symbols:
        p = Path(data_dir) / f"{s}.csv"
        if p.exists():
            out.append(load_symbol_csv(p, s))
    return out


def load_yfinance(symbols: list[str], period: str = "730d") -> list[SymbolSeries]:
    """RETIRED (2026-10-21) — hourly bars via yfinance (Yahoo proxies).

    Kept only so a stale invocation fails at the import/call site rather than
    being silently re-pointed at TDVA.  Use :func:`load_tdva`: the live
    orchestrator reads TradingView, so a Yahoo-based benchmark measures a
    different quantity than the path it is meant to inform.
    """
    import warnings

    warnings.warn(
        "load_yfinance is retired — use load_tdva (the live path's own feed)",
        DeprecationWarning,
        stacklevel=2,
    )
    import yfinance as yf

    out: list[SymbolSeries] = []
    for s in symbols:
        ticker = YAHOO_MAP.get(s.upper())
        if ticker is None:
            continue
        df = yf.download(ticker, interval="1h", period=period, progress=False, auto_adjust=True)
        if df is None or len(df) < 1000:
            continue
        if isinstance(df.columns, __import__("pandas").MultiIndex):
            df.columns = df.columns.get_level_values(0)
        close = df["Close"].astype(float).to_numpy()
        ts = df.index.to_numpy(dtype="datetime64[ns]")
        out.append(SymbolSeries(symbol=s, prices=close, timestamps=ts))
    return out


def load_synthetic(
    symbols: list[str],
    n_days: int = 365,
    seed: int = 7,
) -> list[SymbolSeries]:
    """Deterministic synthetic series for the 5 thesis symbols (and others)."""
    return [
        synthetic_symbol(s, n_days=n_days, seed=seed + i)
        for i, s in enumerate(symbols)
    ]
