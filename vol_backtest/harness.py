"""harness.py — rolling-origin (walk-forward) backtest for RV forecasts.

The design invariants (thesis Phase 0, corrected):

  1. **Frozen cutoffs.** At return-index cutoff t, arms see ONLY
     prices[:t+1] / timestamps[:t+1]; the target is RV over r2[t+1 : t+1+H]
     (returns from bars t+1..t+H). The harness enforces this by slicing
     before calling the arm — an arm cannot look ahead even by accident
     (verified by a sentinel test).
  2. **Cutoff spacing** defaults to 24 bars (one forecast per day, matching
     the thesis's daily refresh cadence); forecasts at spacing < horizon
     OVERLAP, which is why the DM test carries a Newey-West HAC lag of
     ``horizon // spacing - 1``.
  3. **Calendar filter** runs BEFORE returns are computed, per asset class.
  4. Metrics are per (symbol, horizon, arm) — never pooled across symbols
     (BTC's vol scale would dominate); the summary reports per-symbol rows
     and counts how many symbols each arm wins.

Outputs: ``BacktestRecord`` rows (one per cutoff per arm) and aggregated
``metric_rows`` as list[dict] ready for a DataFrame / CSV.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time

import numpy as np

from .arms import Arm
from .realized import (
    SymbolSeries,
    calendar_mask,
    log_returns,
    rv_trailing,
)
from . import metrics as M
from .dm import DMResult, dm_test, holm_adjust


@dataclass
class BacktestRecord:
    symbol: str
    horizon: int
    cutoff: int
    arm: str
    forecast: float          # NaN when the arm failed at this cutoff
    actual: float
    lo80: float
    hi80: float
    prev_rv: float           # trailing RV over H bars at cutoff (context)
    prev2_rv: float          # trailing RV one spacing earlier (direction null)


@dataclass
class ArmRunStats:
    n_calls: int = 0
    n_success: int = 0
    seconds: float = 0.0


@dataclass
class BacktestResult:
    records: list[BacktestRecord] = field(default_factory=list)
    arm_stats: dict[str, ArmRunStats] = field(default_factory=dict)
    skipped_arms: dict[str, str] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    horizons: list[int] = field(default_factory=list)


def backtest_symbol(
    series: SymbolSeries,
    arms: list[Arm],
    horizons: list[int],
    *,
    spacing: int = 24,
    min_train_bars: int = 720,
    max_cutoffs: int | None = None,
    respect_calendar: bool = True,
) -> BacktestResult:
    """Rolling-origin backtest for one symbol across all arms/horizons."""
    res = BacktestResult(symbols=[series.symbol], horizons=list(horizons))

    # calendar filter -> effective series
    if series.timestamps is not None:
        mask = calendar_mask(series.timestamps, series.symbol, respect_calendar=respect_calendar)
        prices = np.asarray(series.prices)[mask]
        ts = np.asarray(series.timestamps)[mask]
    else:
        prices = np.asarray(series.prices)
        ts = None

    r = log_returns(prices)
    r2 = r * r
    n = len(r2)

    for arm in arms:
        if not arm.available:
            res.skipped_arms[arm.name] = arm.unavailable_reason or "unavailable"
    active_arms = [a for a in arms if a.available]

    for H in horizons:
        # trailing RV series for context/direction nulls
        rv_h = rv_trailing(r2, H)
        # first cutoff: needs HAR min_train + H future bars + one spacing of history
        first = max(min_train_bars, 24 * 7, H + spacing)
        last = n - H  # target needs H bars after cutoff index
        cutoffs = list(range(first, last + 1, spacing))
        if max_cutoffs is not None and len(cutoffs) > max_cutoffs:
            stride = int(np.ceil(len(cutoffs) / max_cutoffs))
            cutoffs = cutoffs[::stride]

        for t in cutoffs:
            # ---- target (strictly future) ----
            actual = float(np.sqrt(np.sum(r2[t:t + H])))
            if not np.isfinite(actual):
                continue
            hist_prices = prices[:t + 1]     # bars up to and including cutoff bar
            hist_ts = ts[:t + 1] if ts is not None else None

            prev_rv = float(rv_h[t - 1]) if (t - 1) >= 0 else float("nan")
            prev2_idx = t - 1 - spacing
            prev2_rv = float(rv_h[prev2_idx]) if prev2_idx >= 0 else float("nan")

            for arm in active_arms:
                st = res.arm_stats.setdefault(arm.name, ArmRunStats())
                st.n_calls += 1
                t0 = time.perf_counter()
                try:
                    f = arm.forecast(hist_prices, hist_ts, H)
                except Exception:
                    f = None
                st.seconds += time.perf_counter() - t0
                if f is not None and np.isfinite(f.rv) and f.rv > 0:
                    st.n_success += 1
                    fc = float(f.rv)
                    lo = float(f.lo80) if f.lo80 is not None and np.isfinite(f.lo80) else float("nan")
                    hi = float(f.hi80) if f.hi80 is not None and np.isfinite(f.hi80) else float("nan")
                else:
                    fc, lo, hi = float("nan"), float("nan"), float("nan")
                res.records.append(BacktestRecord(
                    symbol=series.symbol, horizon=H, cutoff=t, arm=arm.name,
                    forecast=fc, actual=actual, lo80=lo, hi80=hi,
                    prev_rv=prev_rv, prev2_rv=prev2_rv,
                ))
    return res


def aggregate_metrics(res: BacktestResult) -> list[dict]:
    """Per (symbol, horizon, arm) metric rows — the results.csv content."""
    import pandas as pd

    df = pd.DataFrame([r.__dict__ for r in res.records])
    rows: list[dict] = []
    if df.empty:
        return rows
    for (sym, H, arm), g in df.groupby(["symbol", "horizon", "arm"]):
        ok = np.isfinite(g["forecast"].to_numpy())
        n_ok = int(ok.sum())
        f = g["forecast"].to_numpy()
        a = g["actual"].to_numpy()
        prev = g["prev_rv"].to_numpy()
        prev2 = g["prev2_rv"].to_numpy()
        row = {
            "symbol": sym, "horizon": int(H), "arm": arm,
            "n": n_ok,
            "n_total": len(g),
            "qlike": M.qlike(f, a),
            "mae_log": M.mae_log(f, a),
            "rmse_log": M.rmse_log(f, a),
            "mae": M.mae(f, a),
            "dir_acc": M.directional_accuracy(f, a, prev),
            "dir_null_persistence": M.persistence_direction_accuracy(a, prev, prev2),
            "coverage80": M.interval_coverage(
                g["lo80"].to_numpy(), g["hi80"].to_numpy(), a
            ),
            "mean_forecast": float(np.nanmean(f)) if n_ok else float("nan"),
            "mean_actual": float(np.mean(a)),
        }
        rows.append(row)
    return rows


def dm_family(
    res: BacktestResult,
    reference: str = "har",
    second_reference: str = "naive",
) -> list[dict]:
    """DM tests of every arm vs `reference` (and vs `second_reference`),
    per (symbol, horizon), QLIKE loss, HAC lag = horizon//spacing - 1.

    Holm-adjusted p-values are added per (symbol, horizon, reference) family.
    """
    import pandas as pd

    df = pd.DataFrame([r.__dict__ for r in res.records])
    out: list[dict] = []
    if df.empty:
        return out
    # spacing recovered from the cutoff grid of each (symbol, horizon)
    for (sym, H), g in df.groupby(["symbol", "horizon"]):
        cuts = np.unique(g["cutoff"].to_numpy())
        spacing = int(np.min(np.diff(cuts))) if len(cuts) > 1 else 24
        hac_lag = max(0, int(H) // spacing - 1)

        # per-record QLIKE contribution, aligned by cutoff across arms
        losses: dict[str, np.ndarray] = {}
        for arm, ga in g.groupby("arm"):
            ga = ga.sort_values("cutoff")
            f = ga["forecast"].to_numpy()
            a = ga["actual"].to_numpy()
            # per-record QLIKE contribution
            ok = np.isfinite(f) & np.isfinite(a) & (f > 0)
            ll = np.full(len(f), np.nan)
            ratio = np.maximum(a[ok], M.EPS) / np.maximum(f[ok], M.EPS)
            ll[ok] = ratio - np.log(ratio) - 1.0
            losses[arm] = ll

        for ref in {reference, second_reference}:
            if ref not in losses:
                continue
            fam: list[dict] = []
            for arm in sorted(losses):
                if arm == ref:
                    continue
                dmr: DMResult = dm_test(
                    losses[arm], losses[ref], hac_lag=hac_lag,
                    name_a=arm, name_b=ref,
                )
                fam.append(dmr.as_dict())
            if not fam:
                continue
            padj = holm_adjust(np.array([r["p_value"] for r in fam]))
            for r, pa in zip(fam, padj):
                r["p_holm"] = float(pa) if np.isfinite(pa) else float("nan")
                r["significant_holm"] = bool(
                    np.isfinite(pa) and pa < 0.05 and r["mean_loss_diff"] < 0
                )
                r["symbol"] = sym
                r["horizon"] = int(H)
                r["hac_lag"] = hac_lag
                out.append(r)
    return out
