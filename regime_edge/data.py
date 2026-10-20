"""data.py — cached OHLCV bar loading for the edge lab.

CSVs at data/regime_edge/{SYM}.csv: timestamp,open,high,low,close,volume
(UTC ISO strings, 1h bars, futures/forex calendars have gaps — treated as a
plain bar sequence exactly like the repo's regime stack does).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd


def load_symbol(csv_path: str, bars_limit: int | None = None) -> dict:
    df = pd.read_csv(csv_path)
    ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    if bars_limit is not None and len(df) > bars_limit:
        df = df.iloc[:bars_limit]  # smoke mode: first N bars (deterministic)
        ts = ts.iloc[:bars_limit]
    return {
        "ts": ts.to_numpy(),
        "open": df["open"].to_numpy(float),
        "high": df["high"].to_numpy(float),
        "low": df["low"].to_numpy(float),
        "close": df["close"].to_numpy(float),
        "volume": df["volume"].to_numpy(float),
        "n": len(df),
    }


def load_all(data_dir: str, symbols: list[str], bars_limit: int | None = None) -> dict:
    out = {}
    for s in symbols:
        p = os.path.join(data_dir, f"{s}.csv")
        out[s] = load_symbol(p, bars_limit)
    return out
