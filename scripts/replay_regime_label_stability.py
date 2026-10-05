"""Regime-label stability replay — the runnable artifact for the 500-tick,
5-symbol flip-rate sweep recorded in
``worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md`` (that run was ad hoc and
never kept as a script).

WHAT IT MEASURES
────────────────
``RegimeHMM.fit()`` rebuilds a fresh ``GaussianHMM`` every tick and re-inits
from k-means (``init_params="stmc"``). The light sweep re-fits on a rolling
200-bar window that shifts by one bar every ~5 min, which is occasionally
enough to nudge the init across a cluster boundary → a different EM optimum →
a different ``regime_label``. ``regime_label`` picks trade direction
(``"short" if "bear" in regime_label``) and drives ``detect_regime_shift``, so
this instability is a live decision input, not just alert noise.

The replay cold-fits the SAME rolling window tick-by-tick (today's production
baseline — no warm-start, which is shelved) and measures the **tick-to-tick
flip rate** at three levels, each with the ``RegimeLabelDebouncer`` applied at
n_confirm = 2/3/4 in parallel:

  1. **full label**  — ``low_vol_bull`` → ``low_vol_strong_bull`` etc.
  2. **cell**        — ``vol_tier|direction`` (``low_vol|bull``) via
     ``kairos.regime_cell`` — collapses strength, the coarser read the Kairos
     pipeline actually keys on.
  3. **trend filter** — ``trend_filter.compute_trend_regime`` on the same
     window (``trending_* / reversal_* / consolidating``). This is the
     platform's own *momentum / trend* read — chop and consolidation are not
     new regime labels, they are this layer recognising that momentum has
     faded (no fresh crossover + neutral RSI → ``consolidating``). Its flip
     rate is the "is the platform recognising regime momentum" question.

USAGE
─────
    PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m scripts.replay_regime_label_stability
    # options:
    #   --ticks 500 --window 200 --symbols AAPL,US500,XAUAUD,GBPUSD,XAGUSD
    #   --interval 1h --json out.json

On the Studio (live TDVA credentials) run it there; locally it needs
``.env.local`` (TDVA key) and prints a clear message if the fetch is empty.

NOT a pytest test — it hits the live price feed. The pure core
(``sweep_symbol``) takes an injected fetch callable so it can be unit-tested
without network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from regime_platform.core.regime_engine import RegimeHMM, RegimeLabelDebouncer
from regime_platform.sweep_orchestrator.kairos import regime_cell
from regime_platform.sweep_orchestrator.trend_filter import compute_trend_regime

# Production default (matches the light sweep's fetch_tradingview_batch(active, 200)).
DEFAULT_WINDOW = 200
DEFAULT_TICKS = 500
DEFAULT_SYMBOLS = ["US500", "XAUAUD", "GBPUSD", "AAPL", "XAGUSD"]
N_CONFIRM_LEVELS = (2, 3, 4)


# ─── Flip-rate math (pure) ──────────────────────────────────────────────────


def flip_rate(seq: list[Optional[str]]) -> float:
    """Share of consecutive tick pairs whose label CHANGED.

    ``None`` entries (e.g. the trend filter returning ``None`` on
    insufficient history) are skipped — a missing read is not a flip.
    Returns 0.0 for fewer than 2 comparable pairs.
    """
    pairs = 0
    flips = 0
    prev: Optional[str] = None
    for v in seq:
        if v is None:
            continue
        if prev is not None:
            pairs += 1
            if v != prev:
                flips += 1
        prev = v
    return (flips / pairs) if pairs else 0.0


def major_minor_flips(seq: list[Optional[str]]) -> tuple[int, int]:
    """Split flips into MAJOR (bull-family ↔ bear-family / direction change)
    vs MINOR (adjacent buckets, e.g. ``bull`` ↔ ``strong_bull``).

    Operates on the full regime label: the family is bull/bear/flat via
    ``regime_cell``. A change whose direction component differs is major.
    """
    major = minor = 0
    prev: Optional[str] = None
    prev_dir: Optional[str] = None
    for v in seq:
        if v is None:
            continue
        d = None
        cell = regime_cell(v)
        if cell and "|" in cell:
            d = cell.split("|", 1)[1]
        if prev is not None and v != prev:
            if d != prev_dir:
                major += 1
            else:
                minor += 1
        prev, prev_dir = v, d
    return major, minor


@dataclass
class DebouncedSeries:
    """One debouncer level's output over a symbol's ticks."""

    n_confirm: int
    labels: list[Optional[str]] = field(default_factory=list)


@dataclass
class SymbolResult:
    symbol: str
    n_ticks: int
    raw_full: list[Optional[str]] = field(default_factory=list)
    raw_cell: list[Optional[str]] = field(default_factory=list)
    raw_trend: list[Optional[str]] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    debounced: dict[int, DebouncedSeries] = field(default_factory=dict)
    # How often the trend filter read consolidating (momentum faded).
    consolidating_share: float = 0.0

    def summary(self) -> dict[str, Any]:
        raw_major, raw_minor = major_minor_flips(self.raw_full)
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "n_ticks": self.n_ticks,
            "raw_full_flip": round(flip_rate(self.raw_full), 4),
            "raw_cell_flip": round(flip_rate(self.raw_cell), 4),
            "raw_trend_flip": round(flip_rate(self.raw_trend), 4),
            "raw_major_flips": raw_major,
            "raw_minor_flips": raw_minor,
            "consolidating_share": round(self.consolidating_share, 4),
            "mean_confidence": round(statistics.fmean(self.confidences), 4)
            if self.confidences else None,
        }
        for n, series in self.debounced.items():
            out[f"full_flip_n{n}"] = round(flip_rate(series.labels), 4)
        return out


# ─── Core sweep (pure — fetch is injected) ──────────────────────────────────


def sweep_symbol(
    symbol: str,
    closes: list[float],
    *,
    window: int = DEFAULT_WINDOW,
    ticks: int = DEFAULT_TICKS,
    n_components: int = 4,
    structure_note_fn: Optional[Callable[[list[float]], Optional[str]]] = None,
) -> SymbolResult:
    """Cold-fit the rolling window ``ticks`` times; return the flip series.

    Mirrors production exactly: a fresh ``RegimeHMM`` per tick, fit on
    ``closes[i : i + window]``, the SAME window the light sweep uses. The
    debouncers are fed the raw label tick-by-tick (their normal online use),
    one instance per level so all levels see identical input.
    """
    prices = np.asarray(closes, dtype=float)
    if len(prices) < window + 1:
        raise ValueError(
            f"{symbol}: need > {window} closes for a rolling sweep, got {len(prices)}"
        )

    # Rolling window: the last `ticks` positions whose full window fits.
    max_start = len(prices) - window
    starts = list(range(max(0, max_start - ticks + 1), max_start + 1))

    debouncers = {n: RegimeLabelDebouncer(n_confirm=n) for n in N_CONFIRM_LEVELS}
    res = SymbolResult(symbol=symbol, n_ticks=len(starts))
    res.debounced = {n: DebouncedSeries(n_confirm=n) for n in N_CONFIRM_LEVELS}

    consolidating = 0
    for i in starts:
        win = prices[i : i + window]
        model = RegimeHMM(n_components=n_components)
        model.fit(win)
        snap = model.predict(win)

        raw = snap.regime_label
        res.raw_full.append(raw)
        res.raw_cell.append(regime_cell(raw))
        res.confidences.append(float(snap.confidence))

        # Trend filter — the platform's momentum/trend read on the SAME window.
        tf = compute_trend_regime(list(win))
        tlabel = tf.get("trend_label") if tf else None
        res.raw_trend.append(tlabel)
        if tlabel == "consolidating":
            consolidating += 1

        for n, d in debouncers.items():
            res.debounced[n].labels.append(d.update(raw))

    res.consolidating_share = consolidating / res.n_ticks if res.n_ticks else 0.0
    return res


# ─── I/O + reporting ────────────────────────────────────────────────────────


def _load_env_local() -> None:
    """Best-effort: load .env.local into os.environ (setdefault, strip quotes)."""
    for cand in (Path(".env.local"), Path("../../.env.local")):
        if cand.exists():
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def _symbol_defs(symbols: list[str]) -> list[dict[str, str]]:
    """Map bare tickers to the TDVA ``EXCHANGE:SYMBOL`` form the sweep uses.

    Mirrors orchestrator._PRICE_DECIMALS' roster prefixes: OANDA for FX/metals,
    COINBASE for crypto, the named index feeds otherwise.
    """
    exchange = {
        "BTCUSD": "COINBASE", "ETHUSD": "COINBASE",
        "XAUUSD": "OANDA", "XAUAUD": "OANDA", "XAUEUR": "OANDA", "XAGUSD": "OANDA",
        "EURUSD": "OANDA", "GBPUSD": "OANDA", "USDJPY": "OANDA",
        "USDCHF": "OANDA", "USDCAD": "OANDA", "AUDUSD": "OANDA",
        "US500": "PEPPERSTONE", "US30USD": "PEPPERSTONE",
        "AAPL": "NASDAQ", "MSFT": "NASDAQ", "NVDA": "NASDAQ", "TSLA": "NASDAQ",
    }
    return [
        {"symbol": s, "tradingview_symbol": f"{exchange.get(s, 'OANDA')}:{s}"}
        for s in symbols
    ]


def _print_report(results: list[SymbolResult], window: int, interval: str) -> None:
    print("=" * 96)
    print(f" REGIME-LABEL STABILITY REPLAY — rolling window={window} bars, {interval} bars")
    print("=" * 96)
    print()
    hdr = (f"{'Symbol':<9} {'ticks':>6} {'raw_full':>9} {'cell':>7} {'trend':>7} "
           f"{'n2':>7} {'n3':>7} {'n4':>7} {'consol':>7} {'conf':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        s = r.summary()
        print(
            f"{s['symbol']:<9} {s['n_ticks']:>6} "
            f"{s['raw_full_flip']*100:>8.1f}% {s['raw_cell_flip']*100:>6.1f}% "
            f"{s['raw_trend_flip']*100:>6.1f}% "
            f"{s['full_flip_n2']*100:>6.1f}% {s['full_flip_n3']*100:>6.1f}% "
            f"{s['full_flip_n4']*100:>6.1f}% "
            f"{s['consolidating_share']*100:>6.1f}% "
            f"{(s['mean_confidence'] or 0):>6.3f}"
        )
    print("-" * len(hdr))
    print("  raw_full/cell/trend = tick-to-tick flip rate BEFORE debounce")
    print("  n2/n3/n4            = full-label flip rate WITH the debouncer")
    print("  consol              = share of ticks the trend filter read 'consolidating'")
    print()
    # Aggregate
    if results:
        def _avg(key: str) -> float:
            vals = [r.summary()[key] for r in results if r.summary()[key] is not None]
            return statistics.fmean(vals) if vals else 0.0
        print(f"  MEAN raw_full flip : {_avg('raw_full_flip')*100:5.1f}%")
        print(f"  MEAN debounced n=3 : {_avg('full_flip_n3')*100:5.1f}%")
        print(f"  MEAN cell  flip    : {_avg('raw_cell_flip')*100:5.1f}%")
        print(f"  MEAN trend flip    : {_avg('raw_trend_flip')*100:5.1f}%")
        print(f"  MEAN consolidating : {_avg('consolidating_share')*100:5.1f}%")
        print()
        print("  Major/minor split (full label, direction-family vs adjacent bucket):")
        for r in results:
            s = r.summary()
            print(f"    {s['symbol']:<9} major={s['raw_major_flips']:>3} "
                  f"minor={s['raw_minor_flips']:>3}")
    print()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--json", default=None, help="write results to this JSON path")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    _load_env_local()

    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

    # Need window + ticks bars for a full rolling sweep.
    want = args.window + args.ticks + 10
    sym_defs = _symbol_defs(symbols)
    print(f"fetching {want} × {args.interval} bars for {len(symbols)} symbols…")
    batch = await fetch_tradingview_batch(sym_defs, want, args.interval)

    results: list[SymbolResult] = []
    for sym in symbols:
        closes = batch.get(sym)
        if not closes or len(closes) < args.window + 1:
            print(f"  {sym}: insufficient data ({len(closes) if closes else 0} bars) — skipping")
            continue
        try:
            r = sweep_symbol(sym, closes, window=args.window, ticks=args.ticks)
        except Exception as exc:
            print(f"  {sym}: sweep failed ({type(exc).__name__}: {exc}) — skipping")
            continue
        results.append(r)
        print(f"  {sym}: {r.n_ticks} ticks swept")

    if not results:
        print("\nNo symbols swept — check TDVA credentials / .env.local, or run on the Studio.")
        return

    _print_report(results, args.window, args.interval)

    if args.json:
        payload = {
            "window": args.window,
            "interval": args.interval,
            "ticks_requested": args.ticks,
            "n_confirm_levels": list(N_CONFIRM_LEVELS),
            "symbols": [r.summary() for r in results],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
