"""Do the free-lattice picks forex 9.75/1.39 and BTCUSD 15.19/2.11 survive?

The reopt report runs with allow_best_effort=True, so the reported EV can be a
cell that does NOT clear the strict bar (both halves positive). This scores the
two exact cells through the same solve with allow_best_effort=False and prints
the full record: does select_tp_sl RETURN them (survive) or return None.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/validate_reopt_picks.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for line in (ROOT / ".env.local").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import logging  # noqa: E402

logging.disable(logging.INFO)

from regime_platform.core.feature_flags import get_dynamic_atr_config, get_min_rr  # noqa: E402
from regime_platform.sweep_orchestrator import atr_reopt as R  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import (  # noqa: E402
    EXPIRY_BARS,
    live_same_bar_policy,
    select_tp_sl,
    walk_envelopes,
)

FOREX = list(R.FOREX)
CASES = [("forex", FOREX, "EURUSD", "forex", 9.75, 1.39),
         ("BTCUSD", ["BTCUSD"], "BTCUSD", None, 15.19, 2.11)]


def record(env, tp, sl, cost):
    pnl, exp = walk_envelopes(env, tp, sl, same_bar_policy=live_same_bar_policy())
    n = len(env)
    mid = n // 2
    net = pnl - cost
    tp_f = (~exp) & (pnl > 0)
    sl_f = (~exp) & (pnl <= 0)
    dec = int(tp_f.sum() + sl_f.sum())
    return {
        "n": n,
        "tp_hits": int(tp_f.sum()),
        "sl_hits": int(sl_f.sum()),
        "expired": int(exp.sum()),
        "wr": (tp_f.sum() / dec) if dec else float("nan"),
        "ev": float(net.mean()),
        "e1": float(net[:mid].mean()),
        "e2": float(net[mid:].mean()),
        "expiry": float(exp.mean()),
    }


async def main():
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    peers = await fetch_active_symbols()
    peers = [p for p in peers if p.get("symbol") and p.get("tradingview_symbol")]
    by_sym = {p["symbol"]: p for p in peers}
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=R.LOOKBACK_DAYS)
    print(f"window {since.date()} .. {until.date()}   min_rr={get_min_rr()}   "
          f"cost={R.COST}\n")
    for label, syms, cmp_symbol, scope_class, tp, sl in CASES:
        members = [by_sym[s] for s in syms if s in by_sym]
        if not members:
            print(f"{label}: no peer rows")
            continue
        env, _ = await R._paths_for(syms, members, since, until)
        cfg = get_dynamic_atr_config(scope_class, cmp_symbol)
        cost = float(cfg["cost_atr"])
        rec = record(env, tp, sl, cost)
        # THE question: does the strict solve return this cell?
        strict = select_tp_sl(
            envelopes=env, tp_grid=[tp], sl_grid=[sl],
            min_rr=get_min_rr(), max_expiry=float(cfg["max_expiry"]),
            cost_atr=cost, min_signals=int(cfg.get("min_signals") or 60),
            same_bar_policy=live_same_bar_policy(), allow_best_effort=False)
        print(f"=== {label}  cell {tp:g}/{sl:g}  (RR {tp/sl:.2f}) ===")
        print(f"  n={rec['n']}  tp_hits={rec['tp_hits']}  sl_hits={rec['sl_hits']}  "
              f"expired={rec['expired']}  wr={rec['wr']:.3f}  expiry={rec['expiry']:.1%}")
        print(f"  EV={rec['ev']:+.3f}   e1(h1)={rec['e1']:+.3f}   e2(h2)={rec['e2']:+.3f}")
        print(f"  STRICT bar (both halves >0): "
              f"{'SURVIVES' if strict is not None else 'FAILS — best-effort only'}")
        print()


asyncio.run(main())
