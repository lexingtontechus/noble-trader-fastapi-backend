"""data.py — price series loading for the vol backtest.

Sources, in priority order:

  1. ``--data-dir data/vol_backtest/`` — one ``<SYMBOL>.csv`` per symbol with
     columns ``timestamp,close`` (ISO timestamps) or a single ``close``
     column. This is the REAL-data path: export hourly bars from the
     platform's own history store or any market-data vendor.
  2. ``--yfinance`` — hourly bars via yfinance (in requirements.txt for
     warmup): BTCUSD->BTC-USD, XAUUSD->GC=F, XAGUSD->SI=F, USDJPY->JPY=X,
     USDCHF->CHF=X (proxy instruments — good enough for RV evaluation;
     document any basis risk). 1h interval covers ~730 days.
  3. ``--synthetic`` — two-state Markov-vol hourly series with the real
     weekend calendar per asset class (smoke tests, CI, methodology demos).

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
    """Hourly bars via yfinance (guarded import; network required)."""
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
