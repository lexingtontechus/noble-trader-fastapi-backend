#!/usr/bin/env python3
"""
Trigger an emergency heavy sweep for specified symbols.

Usage (on Studio):
  PYTHONPATH='' ./.venv/Scripts/python.exe scripts/run_emergency_heavy_sweep.py [SYMBOL ...]

With no arguments, runs for all active symbols.
"""
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

async def run(symbols: list[str] | None = None) -> None:
    from regime_platform.sweep_orchestrator.heavy_sweep import HeavySweepMixin
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    active = await fetch_active_symbols()
    sym_map = {s["symbol"]: s for s in active}

    if symbols is None:
        targets = [s["symbol"] for s in active]
    else:
        targets = [s.upper() for s in symbols]

    mixin = HeavySweepMixin()
    # _sweep_locks and _sizer are defined on SweepOrchestrator, not the
    # mixin — initialize them so run_heavy_sweep_for_symbol works standalone.
    mixin._sweep_locks = {}
    from regime_platform.core.position_sizer import PositionSizer
    mixin._sizer = PositionSizer()
    for sym in targets:
        s = sym_map.get(sym)
        if not s:
            print(f"{sym}: not in active symbols — skipped")
            continue
        print(f"Running emergency heavy sweep for {sym}...", flush=True)
        try:
            result = await mixin.run_heavy_sweep_for_symbol(s)
            print(f"{sym}: skip={result.skip_reason}, error={result.error}", flush=True)
        except Exception as exc:
            print(f"{sym}: FAILED — {exc}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:] if len(sys.argv) > 1 else None
    asyncio.run(run(args))
