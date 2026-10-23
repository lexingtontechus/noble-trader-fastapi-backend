#!/usr/bin/env python
"""export_edge_lab_data.py — build data/regime_edge/{SYM}.csv (1h x N bars).

The edge lab consumes cached OHLCV CSVs (timestamp,open,high,low,close,volume;
UTC ISO-8601 Z strings; 1h bars; futures/forex calendars keep their gaps —
the engine treats the file as a plain bar sequence exactly like the repo's
regime stack).

SOURCE — ``--source tdva`` (default, 2026-10-21)
────────────────────────────────────────────────
``regime_platform.services.tdva_feed.fetch_ohlcv`` — the SAME TradingView
(TDVA) feed the live sweep reads, with symbols mapped through
``nt_symbol.tradingview_symbol`` (authoritative, migration 028).  A benchmark
measured on a different feed than the path it informs measures a different
quantity (docs/scopes/20261019_CONTINUOUS_VOL_TEST_PLAN.md §1).

Under TDVA the two cross-quote metals are REAL symbols, not constructions:
XAUEUR/XAUAUD are rows in nt_symbol, so the old ``GC=F / EURUSD=X`` ratio
synthesis is gone — it existed only because Yahoo has no OANDA:XAUEUR ticker.
That also removes the quote-bar-close wick approximation (the docstring of the
2026-10-21 yfinance export) from the record entirely.

Bar-count ceiling: the TDVA batch endpoint takes a bar COUNT, and
``price_fetcher`` caps it at ``min(hours, 5000)`` — so one fetch yields at most
5000 1h bars (~208 days) versus the ~13–17k bars the 730-day yfinance window
gave.  Expect fewer walk-forward slots and wider CIs; ``--hours`` raises the
request up to that cap.

Every write also refreshes ``SOURCE.json`` in the data dir (source, fetch time,
per-symbol bar counts) so ``run_edge_lab.py`` can state the real data basis in
its artifact notes instead of a hardcoded claim.

SOURCE — ``--source yfinance`` (LEGACY — reproduces the committed baseline only)
──────────────────────────────────────────────────────────────────────────────
The original Yahoo proxy path, retained ONLY so the 5-symbol determinism
witnesses (20261020_053412, 20261020_055039) stay reproducible.  NOT the
production basis — ``data/regime_edge/*.csv`` is TDVA-sourced since 2026-10-21.
Proxies:
    BTCUSD  <- BTC-USD        XAUUSD  <- GC=F          XAGUSD  <- SI=F
    USDJPY  <- JPY=X          USDCHF  <- CHF=X         EURUSD  <- EURUSD=X
    GBPUSD  <- GBPUSD=X       USDCAD  <- USDCAD=X      AUDUSD  <- AUDUSD=X
    AAPL    <- AAPL           US500   <- ES=F          US30USD <- YM=F
    XAUEUR  <- GC=F / EURUSD=X     XAUAUD <- GC=F / AUDUSD=X   (constructed)

Existing CSVs are NOT overwritten unless --force (a resumed/partial export
never silently clobbers a good file).

Usage:
    python3 scripts/export_edge_lab_data.py [--source tdva|yfinance]
        [--symbols SYM ...] [--hours N] [--data-dir DIR] [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DEFAULT_DATA_DIR = os.path.join(REPO_ROOT, "data", "regime_edge")
# Provenance marker written next to the CSVs.  The engine reads it to state
# WHICH feed produced the bars — a hardcoded "yfinance" claim in the run notes
# is exactly the kind of stale assertion this file exists to remove.
PROVENANCE_FILE = "SOURCE.json"

# kairos symbol -> yfinance ticker (direct) — yfinance fallback only.
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
# constructed ratio symbols: (numerator ticker, denominator ticker) — yfinance only.
RATIO_SYMBOLS = {
    "XAUEUR": ("GC=F", "EURUSD=X"),
    "XAUAUD": ("GC=F", "AUDUSD=X"),
}

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ─── yfinance fallback (baseline reproduction) ──────────────────────────────


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


def _yfinance_frame(sym: str) -> pd.DataFrame:
    if sym in RATIO_SYMBOLS:
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


# ─── TDVA (default) ─────────────────────────────────────────────────────────


async def _tdva_frames(symbols: list[str], hours: int) -> dict[str, pd.DataFrame]:
    from regime_platform.services.tdva_feed import fetch_ohlcv

    return await fetch_ohlcv(symbols, hours=hours)


def main(argv=None):
    p = argparse.ArgumentParser(description="export edge-lab OHLCV CSVs")
    p.add_argument("--source", choices=("tdva", "yfinance"), default="tdva",
                   help="tdva (default): the live path's TradingView feed. "
                        "yfinance: Yahoo proxies, for reproducing the committed "
                        "5-symbol baseline only.")
    p.add_argument("--symbols", nargs="+", default=None,
                   help="default: the 14-symbol ACTIVE set")
    p.add_argument("--hours", type=int, default=2160,
                   help="TDVA bars to request (default 2160 = 90d; capped at 5000)")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--force", action="store_true",
                   help="overwrite existing CSVs (default: skip)")
    args = p.parse_args(argv)

    symbols = args.symbols or [
        "BTCUSD", "XAUUSD", "XAGUSD", "XAUEUR", "XAUAUD",
        "USDJPY", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "AUDUSD",
        "AAPL", "US500", "US30USD",
    ]
    os.makedirs(args.data_dir, exist_ok=True)

    if args.source == "tdva":
        frames = asyncio.run(_tdva_frames(symbols, args.hours))
    else:
        frames = {}
        for sym in symbols:
            try:
                frames[sym] = _yfinance_frame(sym)
            except Exception as exc:  # noqa: BLE001 — report and continue
                print(f"[export] {sym}: FAILED ({exc})")

    written = 0
    for sym in symbols:
        path = os.path.join(args.data_dir, f"{sym}.csv")
        frame = frames.get(sym)
        if frame is None:
            print(f"[export] {sym}: no data ({args.source})")
            continue
        if os.path.exists(path) and not args.force:
            print(f"[export] {sym}: exists, skipping ({path})")
            continue
        frame.to_csv(path, index=False)
        written += 1
        ts0, ts1 = frame["timestamp"].iloc[0], frame["timestamp"].iloc[-1]
        basis = "TDVA/TradingView" if args.source == "tdva" else "yfinance proxies"
        print(f"[export] {sym}: {len(frame)} bars {ts0} -> {ts1} ({basis})")

    # Provenance marker — the engine reads this to state the real data basis.
    fetched = dt.datetime.now(dt.timezone.utc).isoformat()
    spans = {
        sym: [str(frames[sym]["timestamp"].iloc[0]), str(frames[sym]["timestamp"].iloc[-1])]
        for sym in symbols if sym in frames
    }
    provenance = {
        "source": args.source,
        "fetched_at": fetched,
        "hours_requested": args.hours,
        "symbols": list(symbols),
        "bars": {sym: int(len(frames[sym])) for sym in symbols if sym in frames},
        "spans": spans,
        "note": (
            "TDVA/TradingView (the live sweep's own feed) — symbols resolved via "
            "nt_symbol.tradingview_symbol."
            if args.source == "tdva" else
            "yfinance proxies — LEGACY reproduction path for the 20261020 baseline "
            "artifacts; NOT the production basis."
        ),
    }
    with open(os.path.join(args.data_dir, PROVENANCE_FILE), "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2)
    print(f"[export] wrote {written} file(s) + {PROVENANCE_FILE} to {args.data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
