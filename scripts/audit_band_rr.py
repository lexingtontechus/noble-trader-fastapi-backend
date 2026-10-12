#!/usr/bin/env python3
"""Audit every dynamic_atr band for RR-validity.

A band whose every grid cell fails ``min_rr`` can never produce a pick. Worse,
under G2 the last-resort fallback is the band CENTRE — inside the same dead
range — so the book would ship a bracket that violates its own RR floor.

Found the defect this exists to catch: git's XAGUSD (RR 0.99-1.07) and XAUAUD
(1.23-1.34) bands were both RR-dead on 2026-10-12, leaving 2 of 14 symbols
structurally unreachable.

Usage:
  PYTHONPATH="$PWD" python scripts/audit_band_rr.py            # human table
  PYTHONPATH="$PWD" python scripts/audit_band_rr.py --quiet    # exit code only

Exit code 1 when any band is RR-dead (CI-gateable).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from regime_platform.core.feature_flags import (  # noqa: E402
    get_dynamic_atr_config,
    get_min_rr,
)


def audit_symbol(symbol: str | None, asset_class: str | None, min_rr: float):
    c = get_dynamic_atr_config(asset_class, symbol)
    tp = [float(x) for x in (c.get("tp_atr") or [])]
    sl = [float(x) for x in (c.get("sl_atr") or [])]
    if not tp or not sl:
        return None, None, []
    tmin, tmax = c.get("tp_atr_min"), c.get("tp_atr_max")
    smin, smax = c.get("sl_atr_min"), c.get("sl_atr_max")
    cells = [
        (t, s) for t in tp for s in sl
        if s > 0 and t / s >= min_rr
        and (tmin is None or t >= tmin) and (tmax is None or t <= tmax)
        and (smin is None or s >= smin) and (smax is None or s <= smax)
    ]
    rr = (min(t / s for t in tp for s in sl if s > 0),
          max(t / s for t in tp for s in sl if s > 0))
    return rr, (tp, sl), cells


def main() -> int:
    quiet = "--quiet" in sys.argv
    min_rr = get_min_rr()
    # every symbol the config carries a band for, plus the global fallback
    symbols = ["AAPL", "BTCUSD", "FOREX", "US30USD", "US500",
               "XAGUSD", "XAUAUD", "XAUEUR", "XAUUSD"]
    dead: list[str] = []
    if not quiet:
        print(f"min_rr = {min_rr}")
        print(f"{'band':10} {'RR range':>12} {'valid cells':>12}  state")
    for sym in symbols:
        rr, grid, cells = audit_symbol(sym, None, min_rr)
        if rr is None:
            if not quiet:
                print(f"{sym:10} {'—':>12} {'—':>12}  no band")
            continue
        if not cells:
            dead.append(sym)
        if not quiet:
            print(f"{sym:10} {f'{rr[0]:.2f}-{rr[1]:.2f}':>12} {len(cells):>12}  "
                  f"{'DEAD' if not cells else 'ok'}")
    # global fallback
    rr, _, cells = audit_symbol(None, None, min_rr)
    if rr is not None and not cells:
        dead.append("<GLOBAL>")
        if not quiet:
            print(f"{'<GLOBAL>':10} {f'{rr[0]:.2f}-{rr[1]:.2f}':>12} {0:>12}  DEAD")
    if dead:
        print(f"\nRR-DEAD bands (no cell clears min_rr={min_rr}): {dead}")
        return 1
    if not quiet:
        print("\nno RR-dead bands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
