"""One-off empirical validation for R1 (worklog/
20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md) — NOT a pytest test, not part
of the permanent suite. Run manually, read the printed output, then delete
or keep for reference.

Pulls REAL current AAPL/US500 hourly closes (same TDVA feed + same 200-bar
window the live light sweep uses) and runs them through OLD (pre-fix) vs
NEW (post-fix) RegimeHMM code to empirically check the two fixes actually
change behavior on the two symbols the whole investigation was about —
not just on synthetic data shaped like them.

Two checks, matching each symbol's distinct symptom:

  1. AAPL (flip-flopping): refit repeatedly on the SAME real window with a
     tiny perturbation on just the last bar each time (simulating the
     live-price wobble between 5-minute ticks within the same still-
     forming hourly bar). Count how many times regime_label CHANGES
     between consecutive fits, OLD vs NEW.
  2. US500 (pinned overconfidence): fit ONCE on the real window, OLD vs
     NEW, and compare the raw confidence value directly — no perturbation
     needed since the symptom is "confidently wrong," not "flip-flopping."

OLD = blind rank-sort labeling (previous_means=None) + hmmlearn's default
min_covar (1e-3, unscaled to the symbol's own feature variance) — recreated
here via a subclassed RegimeHMM that overrides _build_hmm(), rather than
by checking out the pre-fix commit, so both variants run against the
IDENTICAL current codebase's feature engineering / label ordering logic
and differ ONLY in the two things R1 actually changed.
"""
import asyncio
import copy

import numpy as np

from regime_platform.core.regime_engine import RegimeHMM
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

SYMBOLS = [
    {"symbol": "AAPL", "tradingview_symbol": "NASDAQ:AAPL"},
    {"symbol": "US500", "tradingview_symbol": "PEPPERSTONE:US500"},
]


class OldRegimeHMM(RegimeHMM):
    """Recreates the pre-R1 behavior: fixed min_covar, no label continuity
    (this class simply never receives previous_means from the harness
    below, so _label_states always takes the blind-rank-sort branch)."""

    def _build_hmm(self, min_covar: float = 1e-3):
        # Ignore the data-relative floor entirely -- always hmmlearn's
        # unscaled default, exactly as before fix #1.
        return super()._build_hmm(min_covar=1e-3)


def perturb_last_bar(prices: np.ndarray, frac: float, rng: np.random.RandomState) -> np.ndarray:
    """Nudge only the last bar by +/- frac (default tiny, e.g. 0.02%) --
    simulating the live/still-forming bar's price ticking between two
    5-minute sweep ticks within the same hour."""
    out = prices.copy()
    out[-1] *= 1.0 + rng.uniform(-frac, frac)
    return out


def run_flip_flop_check(symbol: str, prices: list[float], n_ticks: int = 20) -> None:
    prices = np.array(prices, dtype=float)
    rng = np.random.RandomState(42)

    print(f"\n=== {symbol}: flip-flop check over {n_ticks} simulated ticks "
          f"(tiny last-bar perturbation, +/-0.02%) ===")

    for label, cls, use_continuity in (
        ("OLD (pre-fix)", OldRegimeHMM, False),
        ("NEW (post-fix)", RegimeHMM, True),
    ):
        flips = 0
        prev_label = None
        prev_vol_means = None
        prev_trend_means = None
        confidences = []
        for i in range(n_ticks):
            window = perturb_last_bar(prices, 0.0002, rng)
            model = cls(n_components=4)
            model.fit(
                window,
                prev_vol_means=prev_vol_means if use_continuity else None,
                prev_trend_means=prev_trend_means if use_continuity else None,
            )
            snap = model.predict(window)
            confidences.append(snap.confidence)
            if prev_label is not None and snap.regime_label != prev_label:
                flips += 1
            prev_label = snap.regime_label
            if use_continuity:
                prev_vol_means = model.vol_label_means
                prev_trend_means = model.trend_label_means
        print(
            f"  {label}: {flips}/{n_ticks - 1} tick-to-tick regime_label "
            f"flips, mean confidence={np.mean(confidences):.4f}"
        )


def run_pinned_confidence_check(symbol: str, prices: list[float]) -> None:
    prices = np.array(prices, dtype=float)
    print(f"\n=== {symbol}: pinned-overconfidence check (one fit, no perturbation) ===")
    for label, cls in (("OLD (pre-fix)", OldRegimeHMM), ("NEW (post-fix)", RegimeHMM)):
        model = cls(n_components=4)
        model.fit(prices)
        snap = model.predict(prices)
        print(
            f"  {label}: regime_label={snap.regime_label!r} "
            f"confidence={snap.confidence:.4f} "
            f"trend_probs={[round(p, 4) for p in snap.trend_probs]}"
        )


async def main() -> None:
    batch = await fetch_tradingview_batch(SYMBOLS, hours=200, interval="1h")
    for sym in SYMBOLS:
        symbol = sym["symbol"]
        prices = batch.get(symbol)
        if not prices or len(prices) < 81:
            print(f"{symbol}: insufficient data ({len(prices) if prices else 0} bars) -- skipping")
            continue
        print(f"\n{'#'*70}\n{symbol}: {len(prices)} real hourly closes pulled from TDVA "
              f"(last close={prices[-1]})\n{'#'*70}")
        run_flip_flop_check(symbol, prices)
        run_pinned_confidence_check(symbol, prices)


if __name__ == "__main__":
    asyncio.run(main())
