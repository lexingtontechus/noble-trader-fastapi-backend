# Kairos reachability vs regime life (2026-09-25). Run from the repo worktree:
#   PYTHONPATH="$PWD" python scripts/kairos_reachability.py
# Output recorded in worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §13.3.
"""Reachability vs regime life. (a) live resolved signals: TP/SL distance in
sweep-ATR, outcome, hours to resolve. (b) history (recon 1h OHLC, 5 symbols):
first-passage of the live-median bracket (TP a·ATR, SL b·ATR from entry,
label direction), by hour, and whether TP is hit before the regime ends."""
import importlib.util, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd, httpx
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('../../.env.local')
from regime_platform.core.settings import Settings
s = Settings(); u = s.SUPABASE_URL.rstrip('/'); k = s.SUPABASE_SECRET_KEY
h = {"apikey": k, "Authorization": f"Bearer {k}"}

# (a) live
rows, off = [], 0
while True:
    r = httpx.get(f"{u}/rest/v1/nt_signal_sim", headers=h, timeout=60, params={
        "select": "symbol,signal,ts,resolved_at,outcome,entry_price,stop_loss,take_profit,atr,qualified",
        "source": "eq.sweep-light", "ts": "gte.2026-09-12T00:00:00Z", "outcome": "not.is.null",
        "signal": "in.(buy,sell)", "order": "ts.asc", "limit": "1000", "offset": str(off)}).json()
    rows += r; off += 1000
    if len(r) < 1000 or off >= 30000: break
L = pd.DataFrame(rows)
for c in ("entry_price", "stop_loss", "take_profit", "atr"):
    L[c] = pd.to_numeric(L[c], errors="coerce")
L = L[(L.atr > 0) & L.stop_loss.notna() & L.take_profit.notna()]
L["tp_atr"] = (L.take_profit - L.entry_price).abs() / L.atr
L["sl_atr"] = (L.stop_loss - L.entry_price).abs() / L.atr
L["hrs"] = (pd.to_datetime(L.resolved_at) - pd.to_datetime(L.ts)).dt.total_seconds() / 3600
print(f"LIVE resolved buy/sell since 09-12: {len(L)}")
print("outcome share:", L.outcome.value_counts(normalize=True).round(3).to_dict())
print("TP/ATR quantiles:", L.tp_atr.quantile([.25, .5, .75]).round(2).to_dict(), " SL/ATR:", L.sl_atr.quantile([.25, .5, .75]).round(2).to_dict())
for o in ("tp_hit", "sl_hit"):
    x = L[L.outcome == o].hrs
    print(f"hours to {o}: p25 {x.quantile(.25):.1f} · median {x.median():.1f} · p75 {x.quantile(.75):.1f} · p90 {x.quantile(.9):.1f} (n {len(x)})")
x = L[L.outcome.isin(["tp_hit", "sl_hit"])]
print("resolved within 6h/12h/24h (share of all):", [round(float(((L.outcome.isin(['tp_hit','sl_hit'])) & (L.hrs <= t)).mean()), 3) for t in (6, 12, 24)])
print("per symbol median TP/ATR & tp_hit%:\n", L.groupby("symbol").agg(tp_atr=("tp_atr", "median"), sl_atr=("sl_atr", "median"),
      tp=("outcome", lambda o: (o == "tp_hit").mean()), exp=("outcome", lambda o: (o == "expired").mean()), n=("outcome", "size")).round(2).to_string())
TP, SL = float(L.tp_atr.median()), float(L.sl_atr.median())

# (b) history
spec = importlib.util.spec_from_file_location("kr", Path("scripts/kairos_repeatability.py"))
kr = importlib.util.module_from_spec(spec); spec.loader.exec_module(kr)
A, R, MU = kr.build(2)
import json
C = kr._cache_dir()
HMAX = 48
def passage(sym, tp_k, sl_k):
    a = A[sym]; bars = pd.DataFrame(json.loads((C / f"recon_{sym}_bars_1h.json").read_text()))
    hi, lo, c = bars.h.to_numpy(float), bars.l.to_numpy(float), a["c"]
    n = a["n"]; out = []
    run_end = np.r_[np.flatnonzero(np.diff(R[sym]["rid"]) != 0), n - 1]
    end_of = run_end[np.searchsorted(run_end, np.arange(n))]
    for t in range(0, n - HMAX - 1, 3):   # every 3rd bar (overlap thinning)
        d, atr = a["d"][t], a["atr"][t]
        if d == 0 or not np.isfinite(atr) or R[sym]["left_cens"][t]:
            continue
        e = c[t]; tp = e + d * tp_k * atr; sl = e - d * sl_k * atr
        hit_tp = hit_sl = None
        for j in range(1, HMAX + 1):
            up, dn = hi[t + j], lo[t + j]
            tpj = (up >= tp) if d > 0 else (dn <= tp)
            slj = (dn <= sl) if d > 0 else (up >= sl)
            if slj:            # conservative: SL first if both in one bar
                hit_sl = j; break
            if tpj:
                hit_tp = j; break
        out.append((sym, t, hit_tp, hit_sl, end_of[t] - t + 1, a["age_h"][t] if "age_h" in a else R[sym]["age_h"][t]))
    return out
res = []
for sym in A:
    res += passage(sym, TP, SL)
P = pd.DataFrame(res, columns=["symbol", "t", "tp", "sl", "regime_left_h", "age"])
n = len(P)
print(f"\nHISTORY first-passage, bracket TP {TP:.2f} / SL {SL:.2f} sweep-ATR, {n} signals (every 3rd bar), SL-first on same-bar ties")
rows = []
for T in (2, 4, 6, 8, 12, 24, 48):
    rows.append({"within_h": T, "P(TP)": (P.tp <= T).mean(), "P(SL)": (P.sl <= T).mean(),
                 "P(open)": 1 - (P.tp <= T).mean() - (P.sl <= T).mean()})
print(pd.DataFrame(rows).round(3).to_string(index=False))
tp_h = P.tp.dropna(); sl_h = P.sl.dropna()
print(f"hours to TP: median {tp_h.median():.0f} · p75 {tp_h.quantile(.75):.0f} · p90 {tp_h.quantile(.9):.0f}; to SL median {sl_h.median():.0f}")
print(f"regime remaining at entry: median {P.regime_left_h.median():.0f}h · p75 {P.regime_left_h.quantile(.75):.0f}h")
P["tp_in_regime"] = P.tp.notna() & (P.tp <= P.regime_left_h)
P["sl_in_regime"] = P.sl.notna() & (P.sl <= P.regime_left_h)
print(f"TP hit while the entry regime is still alive: {P.tp_in_regime.mean():.3f} of signals · {P.tp_in_regime.sum() / max(P.tp.notna().sum(), 1):.2f} of all TP hits")
print(f"SL hit while regime alive: {P.sl_in_regime.mean():.3f}")
# scale: what bracket size is reachable within median regime life (~7h)?
print("\nbracket scale sweep (TP=SL=k sweep-ATR, symmetric), P(TP first) within 8h / 24h / 48h:")
for kk in (1, 2, 3, 5, 8, 12):
    rr = []
    for sym in A:
        rr += passage(sym, kk, kk)
    Q = pd.DataFrame(rr, columns=["symbol", "t", "tp", "sl", "rl", "age"])
    print(f"  k={kk:>2}: " + " · ".join(f"{T}h TP {((Q.tp <= T)).mean():.2f}/SL {((Q.sl <= T)).mean():.2f}" for T in (8, 24, 48))
          + f" · median hrs to first touch {np.nanmedian(np.fmin(Q.tp.fillna(99), Q.sl.fillna(99))):.0f}")
