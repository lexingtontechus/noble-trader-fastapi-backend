#!/usr/bin/env python3
"""
Real-data verification for the 2026-08-24 RegimeHMM feature-standardization
fix (see regime_platform/core/regime_engine.py's module docstring, fix #3).

Not a pytest test (like scripts/validate_r1_regime_fix_live_data.py, this
pulls live TDVA data — real network, not deterministic/CI-safe). Run this
after any further change to RegimeHMM's fitting to confirm it still
converges on real data, not just synthetic test fixtures.

History: this script originally (2026-08-23) tested three fix hypotheses
against real data — dropping the collinear trend feature, z-score
standardization, and 5-restart-best-of fitting. Standardization was the
only one that reliably fixed convergence across all symbols tested;
dropping the collinear feature made it WORSE in 3 of 4 symbols, and
multi-restart fixed only 1 of 4. That result is why fix #3 is
standardization, not feature reduction or restarts — see
worklog/20260823_CALIBRATION_BIAS_CHECK_AUDIT.md and the regime_engine.py
module docstring. This version verifies the RESULT: the real (fixed)
RegimeHMM.fit() against real current market data.

Usage: PYTHONPATH="$PWD" ./.venv/bin/python scripts/diagnose_hmm_convergence.py
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np

warnings.filterwarnings("ignore")

SYMBOLS = ["EURUSD", "XAUUSD", "BTCUSD", "AAPL", "US500"]
N_BARS = 200


async def main() -> None:
    from regime_platform.core.regime_engine import RegimeHMM
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    active = await fetch_active_symbols()
    tv_map = {s["symbol"]: s.get("tradingview_symbol") for s in active}

    results = []
    for symbol in SYMBOLS:
        tv = tv_map.get(symbol)
        if not tv:
            print(f"{symbol}: skip — no tradingview_symbol")
            continue
        batch = await fetch_candles_for_symbols([{"symbol": symbol, "tradingview_symbol": tv}], n_bars=N_BARS)
        prices = np.array(batch.get(symbol, []), dtype=float)
        if len(prices) < 100:
            print(f"{symbol}: skip — only {len(prices)} bars")
            continue

        model = RegimeHMM(n_components=4)
        model.fit(prices)
        snap = model.predict(prices)
        results.append((symbol, snap.trend_converged, snap.vol_converged))
        print(
            f"{symbol}: trend_converged={snap.trend_converged} "
            f"vol_converged={snap.vol_converged} regime={snap.regime_label} "
            f"conf={snap.confidence}"
        )

    n_failed = sum(1 for _, t, v in results if not (t and v))
    print(f"\n{len(results)} symbols checked, {n_failed} with any non-convergence.")


if __name__ == "__main__":
    asyncio.run(main())
