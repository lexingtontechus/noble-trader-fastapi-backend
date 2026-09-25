"""Can the 3 stripped Kairos stamp sets be recomputed exactly?

Refetch 1h OHLCV for all 14 symbols (TDVA, same fetcher as the sweep), then
for every row of the 3 affected sweep ticks recompute clv_1h / efficiency_24h
/ rel_volume_1h with kairos.capture_factors using only bars up to that tick.
CONTROL = the 13 healthy rows per tick whose stored stamps we compare against.
atr_ratio_run is checked separately from stored atr + prior-tick ratio.
spread_bps is a live quote snapshot — not recoverable, never backfilled.
"""
import asyncio
import importlib.util
import json
import sys
from datetime import datetime

sys.path.insert(0, ".")
_argv = sys.argv
sys.argv = [sys.argv[0]]
spec = importlib.util.spec_from_file_location(
    "sq", r"C:/Users/aloys/AppData/Local/hermes/profiles/astro/cache/scratch/cellpersist/sq.py")
sq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sq)
sys.argv = _argv

from regime_platform.sweep_orchestrator.kairos import capture_factors  # noqa: E402
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402

OUT = r"C:/Users/aloys/AppData/Local/hermes/profiles/astro/cache/scratch/cellpersist/kairos_recompute.json"
TICKS = [("2026-09-24 15:00", "2026-09-24 15:14"), ("2026-09-24 18:00", "2026-09-24 18:14"),
         ("2026-09-24 21:45", "2026-09-24 21:59")]
LOST = {"6672e6d8-e2f9-4c2f-bfb9-2cb888eb0436", "9e51cac8-5789-4d63-b6ea-ab1d5d1e9434",
        "f1ab4fce-42f3-4446-86ce-2b98c77ece0f"}
F3 = ("clv_1h", "efficiency_24h", "rel_volume_1h")


def ep(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("+00", "+00:00")).timestamp()


async def main():
    syms = sq.q("select symbol, tradingview_symbol from nt_symbol where symbol in "
                "(select distinct symbol from nt_signal_sim where source='sweep-light' and ts > now() - interval '1 day')")
    ohlcv: dict = {}
    await fetch_tradingview_batch(syms, 300, include_timestamps=True, ohlcv_out=ohlcv)
    rows = []
    for a, b in TICKS:
        rows += sq.q(f"""select signal_id, symbol, ts, atr, clv_1h, efficiency_24h, rel_volume_1h, atr_ratio_run
                        from nt_signal_sim where source='sweep-light' and ts >= '{a}' and ts < '{b}' order by ts""")
    res = []
    for r in rows:
        bars = ohlcv.get(r["symbol"]) or []
        t = ep(r["ts"])
        upto = [x for x in bars if x["t"] is not None and x["t"] <= t][-200:]  # sweep fetches 200
        f = capture_factors(upto, atr_now=None, atr_run_start=None, spread_bps=None, forming_last=True)
        rec = {"signal_id": r["signal_id"], "symbol": r["symbol"], "ts": r["ts"], "lost": r["signal_id"] in LOST,
               "last_bar": datetime.utcfromtimestamp(upto[-1]["t"]).isoformat() if upto else None}
        for k in F3:
            rec[k + "_stored"] = r[k]
            rec[k + "_recomp"] = f[k]
        res.append(rec)
    ctl = [x for x in res if not x["lost"] and x["clv_1h_stored"] is not None]
    summ = {}
    for k in F3:
        pairs = [(x[k + "_stored"], x[k + "_recomp"]) for x in ctl if x[k + "_stored"] is not None]
        exact = sum(1 for s, c in pairs if c is not None and abs(float(s) - float(c)) <= 1e-4)
        summ[k] = {"control_n": len(pairs), "exact": exact}
    json.dump({"summary": summ, "rows": res}, open(OUT, "w"), indent=1, default=str)
    print(json.dumps(summ))
    for x in res:
        if x["lost"]:
            print("LOST", x["symbol"], x["ts"], x["last_bar"], {k: x[k + "_recomp"] for k in F3})
    bad = [x for x in ctl if any(x[k + "_stored"] is not None and (x[k + "_recomp"] is None or abs(float(x[k + "_stored"]) - float(x[k + "_recomp"])) > 1e-4) for k in F3)]
    for x in bad[:8]:
        print("MISMATCH", x["symbol"], x["ts"], {k: (x[k + "_stored"], x[k + "_recomp"]) for k in F3})


asyncio.run(main())
