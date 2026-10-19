"""realized.py — realized volatility, calendar handling, annualization.

Phase 0 of the TimesFM refined thesis (`.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md`),
as corrected by the 2026-10-18 review:

  * The forecast target is **realized volatility over the next H bars**
    RV_H = sqrt(sum_{i=1..H} r^2_{t+i})   (log returns, per-bar scale).
    Metrics are computed on this scale. Annualization is for DISPLAY ONLY and
    is per-asset-class (BTC trades 24/7; FX/metals ~120h/week) — a single
    sqrt(365*24) factor is wrong for FX/metals by ~18%.
  * Calendar filter removes market-closed bars (weekends for FX/metals) so
    weekend zero-return bars do not poison RV and the naive baseline.

Conventions: prices are 1-D float arrays, bars are hourly and UNIFORM after
calendar filtering. Timestamps (np.datetime64[ns] or None) are optional and
only used by calendar-aware and covariate code paths.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

# ---------------------------------------------------------------------------
# Calendar configuration (hours in UTC)
# ---------------------------------------------------------------------------

# FX/metals: closed from Fri 22:00 UTC to Sun 22:00 UTC (approximate — good
# enough for bar filtering; exact session edges do not matter for RV).
FOREX_SUNDAY_OPEN_HOUR = 22  # Sunday 22:00 UTC re-open

# Annualization factors (bars per year) — display only.
BARS_PER_YEAR: dict[str, float] = {
    "crypto": 24.0 * 365.0,  # 8760
    "forex": 120.0 * 52.0,   # ~6240 (5 x 24h)
    "metal": 120.0 * 52.0,
    "index": 120.0 * 52.0,
}

ASSET_CLASSES: dict[str, str] = {
    "BTCUSD": "crypto",
    "ETHUSD": "crypto",
    "XAUUSD": "metal",
    "XAUEUR": "metal",
    "XAGUSD": "metal",
    "USDJPY": "forex",
    "USDCHF": "forex",
    "AUDUSD": "forex",
    "EURUSD": "forex",
    "US30USD": "index",
    "US500": "index",
    "US100": "index",
}


def asset_class_for(symbol: str) -> str:
    return ASSET_CLASSES.get(symbol.upper(), "forex")


def bars_per_year(symbol: str) -> float:
    return BARS_PER_YEAR.get(asset_class_for(symbol), BARS_PER_YEAR["forex"])


# ---------------------------------------------------------------------------
# Calendar filter
# ---------------------------------------------------------------------------

def _dayofweek_hour(timestamps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(dayofweek Mon=0..Sun=6, hour 0..23) for datetime64 timestamps."""
    import pandas as _pd  # local import: only needed on the calendar path

    idx = _pd.DatetimeIndex(timestamps)
    return idx.dayofweek.to_numpy(), idx.hour.to_numpy()


def calendar_mask(
    timestamps: np.ndarray,
    symbol: str,
    *,
    respect_calendar: bool = True,
) -> np.ndarray:
    """Boolean mask of bars to KEEP for `symbol`.

    Crypto symbols keep every bar. FX/metals drop Saturday entirely and
    Sunday bars before 22:00 UTC. If `respect_calendar` is False (or the data
    has no usable timestamps), every bar is kept — the caller may already
    have market-calendar-clean data.
    """
    n = len(timestamps) if timestamps is not None else 0
    if not respect_calendar or n == 0:
        return np.ones(n, dtype=bool)

    ts = np.asarray(timestamps)
    if ts.dtype.kind != "M":  # not datetime64 — cannot filter
        return np.ones(n, dtype=bool)

    if asset_class_for(symbol) == "crypto":
        return np.ones(n, dtype=bool)

    dow, hour = _dayofweek_hour(ts)
    keep = np.ones(n, dtype=bool)
    keep[dow == 5] = False                                      # Saturday
    keep[(dow == 6) & (hour < FOREX_SUNDAY_OPEN_HOUR)] = False  # Sunday pre-open
    return keep


# ---------------------------------------------------------------------------
# Returns and realized volatility
# ---------------------------------------------------------------------------

def log_returns(prices: np.ndarray) -> np.ndarray:
    """Log returns r_t = ln(P_t / P_{t-1}). Length N-1."""
    p = np.asarray(prices, dtype=np.float64)
    if len(p) < 2:
        return np.zeros(0)
    return np.diff(np.log(p))


def rv_trailing(r2: np.ndarray, horizon: int) -> np.ndarray:
    """Trailing RV over the last `horizon` bars ending at each return index t
    (inclusive). NaN before index horizon-1. Inputs series for naive/HAR."""
    n = len(r2)
    out = np.full(n, np.nan)
    if n == 0 or horizon <= 0:
        return out
    c = np.concatenate(([0.0], np.cumsum(r2)))
    # RV_t = sqrt(c[t+1] - c[t+1-horizon]) for t in [horizon-1, n-1]
    out[horizon - 1:] = np.sqrt(c[horizon:] - c[: n - horizon + 1])
    return out


def forward_rv(r2: np.ndarray, cutoff: int, horizon: int) -> float:
    """Backtest target: RV over the NEXT `horizon` bars after `cutoff`.

    `r2` is squared per-bar returns; `cutoff` is a return-series index such
    that only r2[:cutoff] is visible to the forecaster (no lookahead).
    """
    if cutoff < 0 or cutoff + horizon > len(r2):
        return float("nan")
    return float(np.sqrt(np.sum(r2[cutoff:cutoff + horizon])))


def annualize_horizon(rv_horizon: float, horizon: int, symbol: str) -> float:
    """Annualized vol from an H-bar RV (DISPLAY ONLY):
    sigma_ann = RV_H / sqrt(H) * sqrt(bars_per_year)."""
    if horizon <= 0 or not np.isfinite(rv_horizon):
        return float("nan")
    return float(rv_horizon) / np.sqrt(horizon) * np.sqrt(bars_per_year(symbol))


# ---------------------------------------------------------------------------
# Symbol container + synthetic data
# ---------------------------------------------------------------------------

@dataclass
class SymbolSeries:
    symbol: str
    prices: np.ndarray
    timestamps: np.ndarray | None

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.prices)


def synthetic_symbol(
    symbol: str,
    n_days: int = 365,
    seed: int = 0,
    *,
    annual_vol_low: float = 0.08,
    annual_vol_high: float = 0.45,
    p_stay: float = 0.995,
    include_calendar: bool = True,
) -> SymbolSeries:
    """Synthetic hourly prices with a two-state Markov volatility regime.

    Smoke tests and CI: the harness must produce finite metrics, and Log-HAR
    must beat naive on persistent vol (the implementation sanity check).
    Calibers default to FX-low / crypto-high.

    Calendar: FX/metals skip Saturday + Sunday-before-22:00 UTC bars; crypto
    trades every hour, so both calendar paths are exercised.
    """
    rng = np.random.default_rng(seed)
    cls = asset_class_for(symbol)

    hours_total = n_days * 24
    base = np.datetime64("2025-10-01T00:00:00", "ns")
    ts_all = base + np.arange(hours_total).astype("timedelta64[h]")

    if include_calendar and cls != "crypto":
        dow, hour = _dayofweek_hour(ts_all)
        keep = ~((dow == 5) | ((dow == 6) & (hour < FOREX_SUNDAY_OPEN_HOUR)))
    else:
        keep = np.ones(hours_total, dtype=bool)

    ts = ts_all[keep]
    n = len(ts)

    bpv = BARS_PER_YEAR.get(cls, BARS_PER_YEAR["forex"])
    sigma_low = annual_vol_low / np.sqrt(bpv)
    sigma_high = annual_vol_high / np.sqrt(bpv)

    state = 0
    sig = np.empty(n)
    stay = float(p_stay)
    flips = rng.random(n) > stay
    for i in range(n):
        if flips[i]:
            state = 1 - state
        sig[i] = sigma_low if state == 0 else sigma_high
    rets = rng.normal(0.0, sig)
    prices = 100.0 * np.exp(np.cumsum(rets))
    return SymbolSeries(symbol=symbol, prices=prices, timestamps=ts)
