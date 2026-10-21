#!/usr/bin/env python
"""export_edge_lab_data.py — build data/regime_edge/{SYM}.csv (1h x 730d).

The edge lab consumes cached OHLCV CSVs (timestamp,open,high,low,close,volume;
UTC ISO-8601 Z strings; 1h bars; futures/forex calendars keep their gaps —
the engine treats the file as a plain bar sequence exactly like the repo's
regime stack).

PROXIES (yfinance, same convention as the v1 5-symbol run 20261020_055039 —
sandbox has no broker feed):
    BTCUSD  <- BTC-USD        XAUUSD  <- GC=F          XAGUSD  <- SI=F
    USDJPY  <- JPY=X          USDCHF  <- CHF=X         EURUSD  <- EURUSD=X
    GBPUSD  <- GBPUSD=X       USDCAD  <- USDCAD=X      AUDUSD  <- AUDUSD=X
    AAPL    <- AAPL           US500   <- ES=F          US30USD <- YM=F

CONSTRUCTED (2026-10-21, symbol-set extension to the full ACTIVE 14):
    XAUEUR  <- GC=F / EURUSD=X     XAUAUD <- GC=F / AUDUSD=X
  on INNER-JOINED hourly timestamps.  Rationale: same metal basis as the
  XAUUSD proxy (GC=F), so the only difference vs XAUUSD is the quote
  currency — exactly the quote-noise question the Part-C specialist-books
  review asks of these variants.  Wick approximation: each GC=F bar's
  high/low/open are divided by the SAME bar's EUR/AUD CLOSE (the true
  extremum of the ratio lies inside [GC_low/Q_high, GC_high/Q_low]; the
  close-quotient anchor errs by at most the quote bar's own range, ~0.05%).
  Volume carried from the GC=F leg.

Determinism: yfinance 1h windows roll with `period="730d"` (the repo's own
vol_backtest export used the same); re-runs re-fetch the trailing window.
Existing CSVs are NOT overwritten unless --force (the committed 5-symbol
baseline files stay byte-stable).

Usage:
    python3 scripts/export_edge_lab_data.py [--symbols SYM ...] [--force]
"""
from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data", "regime_edge")

# kairos symbol -> yfinance ticker (direct)
TICKERS = {
    "BTCUSD": "BTC-USD",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "USDJPY": "JPY=X",
    "USDCHF": "CHF=X",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "AAPL": "AAPL",
    "US500": "ES=F",
    "US30USD": "YM=F",
}
# constructed ratio symbols: (numerator ticker, denominator ticker)
RATIO_SYMBOLS = {
    "XAUEUR": ("GC=F", "EURUSD=X"),
    "XAUAUD": ("GC=F", "AUDUSD=X"),
}

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _fetch(ticker: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period="730d", interval="1h", progress=False,
                     auto_adjust=False, multi_level_index=False)
    if df is None or df.empty:
        raise RuntimeError(f"empty download for {ticker}")
    df = df.reset_index()
    # normalise the timestamp column name (Datetime / index)
    tcol = next(c for c in df.columns if str(c).lower().startswith(("datetimetz", "datetime", "date", "index")))
    df = df.rename(columns={tcol: "timestamp"})
    keep = ["timestamp", "Open", "High", "Low", "Close", "Volume"]
    df = df[[c for c in keep if c in df.columns]]
    df.columns = ["timestamp", "open", "high", "low", "close", "volume"][: len(df.columns)]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.dropna(subset=["open", "high", "low", "close"]).sort_values("timestamp")


def _direct(sym: str) -> pd.DataFrame:
    df = _fetch(TICKERS[sym])
    out = pd.DataFrame({
        "timestamp": df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "volume": df["volume"].fillna(0.0).astype(float),
    })
    return out[COLUMNS]


def _ratio(sym: str) -> pd.DataFrame:
    num_t, den_t = RATIO_SYMBOLS[sym]
    num, den = _fetch(num_t), _fetch(den_t)
    j = num.merge(den, on="timestamp", suffixes=("_n", "_d"), how="inner")
    q = j["close_d"].astype(float)  # quote anchor: same bar's close
    out = pd.DataFrame({
        "timestamp": j["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": j["open_n"].astype(float) / q,
        "high": j["high_n"].astype(float) / q,
        "low": j["low_n"].astype(float) / q,
        "close": j["close_n"].astype(float) / q,
        "volume": j["volume_n"].fillna(0.0).astype(float),
    })
    return out[COLUMNS]


def main(argv=None):
    p = argparse.ArgumentParser(description="export edge-lab OHLCV CSVs")
    p.add_argument("--symbols", nargs="+", default=None,
                   help="default: the 9 symbols added by the 2026-10-21 extension")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--force", action="store_true",
                   help="overwrite existing CSVs (default: skip)")
    args = p.parse_args(argv)

    symbols = args.symbols or ["EURUSD", "GBPUSD", "USDCAD", "AUDUSD",
                               "AAPL", "US500", "US30USD", "XAUEUR", "XAUAUD"]
    os.makedirs(args.data_dir, exist_ok=True)
    for sym in symbols:
        path = os.path.join(args.data_dir, f"{sym}.csv")
        if os.path.exists(path) and not args.force:
            print(f"[export] {sym}: exists, skipping ({path})")
            continue
        try:
            df = _ratio(sym) if sym in RATIO_SYMBOLS else _direct(sym)
        except Exception as exc:  # noqa: BLE001 — report and continue with others
            print(f"[export] {sym}: FAILED ({exc})")
            continue
        df.to_csv(path, index=False)
        ts0, ts1 = df["timestamp"].iloc[0], df["timestamp"].iloc[-1]
        basis = (f"{RATIO_SYMBOLS[sym][0]} / {RATIO_SYMBOLS[sym][1]} (constructed ratio)"
                 if sym in RATIO_SYMBOLS else TICKERS[sym])
        print(f"[export] {sym}: {len(df)} bars {ts0} -> {ts1} ({basis})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
