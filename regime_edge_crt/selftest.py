"""selftest.py — deterministic synthetic compact-path corpus (seed 7).

Smoke the whole CRT-0 lab WITHOUT MT4 data: generates ``CRT_CONT_RESEARCH_
<SYMBOL>.csv`` files in the EXACT ``CRT_EV_Research_Export.mq4`` v1.10 schema
(header, ``#meta`` cost line, CRLF, compact-path b_/c_ columns), seeded so
two runs are byte-identical.

HONESTY: this corpus is synthetic. Outcomes are drawn per variant row from
injected per-bucket win probabilities (not from a coherent price path, and
variant rows of one setup do not share a path) — it exercises the pipeline's
mechanics (schema ingest, folds, LCB selection, decay replay, thresholds,
mint, Discord) and NOTHING about it is evidence of a real CRT edge. The real
corpus is the operator's MT4 export. The injected edge (win_p by session/TF)
exists so the enable gate has something to find.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import numpy as np

EB_GRID = (0.0, 0.05, 0.10)
SB_GRID = (0.0, 0.10, 0.20)
R_GRID = (1.0, 1.5, 2.0)
HOLD_GRID = (12, 24, 48)

SYMBOLS = {
    "XAUUSD": dict(point=0.01, digits=2, spread_pts=20, risk_lo=600.0, risk_hi=3000.0),
    "EURUSD": dict(point=0.00001, digits=5, spread_pts=2, risk_lo=90.0, risk_hi=400.0),
}
TF_CADENCE = {"H1": (60, 10), "M30": (30, 20), "M15": (15, 40)}   # (minutes, every-N-bars)

HEADER = ("symbol,tf,dir,c2_time,c3_time,session,sweep_atr,chop_wick,chop_overlap,"
          "dir_changes,chop_pass,atr_pts,atr_pct,c1_swing,swept_pd,c3_body,c3_dir_ok,"
          "model,entry_buf,sl_buf,filled,risk_pts,entry_cost_r,exit_slip_r,bars_avail,"
          "bars_sl," + ",".join(f"b_{r:.2f}" for r in R_GRID) + ","
          + ",".join(f"c_{h}" for h in HOLD_GRID))


def _session_of(hour: int) -> int:
    if 9 <= hour < 12:          # NY 09:30-12:00 ET bucket (hour granularity)
        return 1
    if 20 <= hour < 22:         # ASIA 20:00-22:00 ET bucket
        return 2
    return 0


def _win_p(symbol: str, tf: str, session: int) -> float:
    """The injected edge (SYNTHETIC — see module docstring)."""
    p = 0.40
    if session == 1:
        p += 0.07
    if tf == "H1":
        p += 0.04
    if tf == "M15":
        p -= 0.06
    if symbol == "XAUUSD":
        p += 0.02
    return p


def _fmt_c(v: float) -> str:
    return f"{v:.4f}"


def _variant_row(rng, common, eb: float, sb: float) -> str:
    win_p = common["win_p"]
    # small config-sensitivity so selection is not degenerate
    win_p += 0.015 if eb == 0.0 else (0.005 if eb == 0.05 else 0.0)
    win_p += 0.010 if sb == 0.10 else 0.0
    filled = 1 if rng.random() < 0.92 else 0
    model = "CONT"
    prefix = (f"{common['symbol']},{common['tf']},{common['dir']},{common['c2_time']},{common['c3_time']},"
              f"{common['session']},{common['sweep_atr']:.3f},{common['chop_wick']:.3f},"
              f"{common['chop_overlap']:.3f},{common['dir_changes']},{common['chop_pass']},"
              f"{common['atr_pts']:.1f},{common['atr_pct']:.3f},{common['c1_swing']},"
              f"{common['swept_pd']},{common['c3_body']:.3f},{common['c3_dir_ok']},"
              f"{model},{eb:.3f},{sb:.3f},")
    if not filled:
        tail = "0,0,0,0,0,-1" + ",-1" * len(R_GRID) + "," * len(HOLD_GRID)
        return prefix + tail
    risk_pts = round(float(rng.uniform(
        SYMBOLS[common["symbol"]]["risk_lo"], SYMBOLS[common["symbol"]]["risk_hi"])), 1)
    ecost = round(float(rng.uniform(0.004, 0.020)), 4)
    slip = round(float(rng.uniform(0.002, 0.010)), 4)
    avail = 48 + int(rng.integers(0, 7))          # closed bars after C3

    kind = rng.random()
    if kind < win_p:                               # TP reached
        bars_sl = -1
        bars_r = [int(3 + 2 * i + rng.integers(0, 4)) for i in range(len(R_GRID))]
        c_vals = {}
        for h in HOLD_GRID:
            i15 = 1                               # R=1.5 index
            if h >= bars_r[i15]:
                c_vals[h] = 1.5 * float(rng.uniform(0.72, 1.0))
            else:
                c_vals[h] = float(rng.uniform(-0.2, 0.8))
    elif kind < win_p + 0.12:                     # neither barrier (expiry)
        bars_sl = -1
        bars_r = [-1] * len(R_GRID)
        c_vals = {h: float(rng.uniform(-0.4, 0.4)) for h in HOLD_GRID}
    else:                                         # SL hit
        bars_sl = int(1 + rng.integers(0, 20))
        bars_r = [-1] * len(R_GRID)
        c_vals = {}
        for h in HOLD_GRID:
            if h >= bars_sl:
                c_vals[h] = -float(rng.uniform(0.4, 1.0))
            else:
                c_vals[h] = float(rng.uniform(-0.5, 0.8))

    tail = (f"1,{risk_pts},{ecost},{slip},{avail},{bars_sl},"
            + ",".join(str(b) for b in bars_r)
            + "," + ",".join(_fmt_c(c_vals[h]) for h in HOLD_GRID))
    return prefix + tail


def generate_symbol_csv(out_dir: str, symbol: str, months: int, seed: int) -> str:
    """One CRT_CONT_RESEARCH_<SYMBOL>.csv (header + #meta + CRLF rows)."""
    sy = SYMBOLS[symbol]
    rng = np.random.default_rng([seed, sum(map(ord, symbol)) % (2 ** 31), months])
    start = datetime(2023, 3, 1)
    end = start + timedelta(days=30 * months)
    lines = [HEADER]
    lines.append(
        f"#meta,point={sy['point']},digits={sy['digits']},spread_pts={sy['spread_pts']},"
        f"slip_pts=0,clock=auto,gmt_off=2,nyclose=0"
    )
    for tf, (mins, every) in TF_CADENCE.items():
        step = timedelta(minutes=mins)
        t = start + timedelta(minutes=mins * 260)          # warmup
        while t < end - timedelta(minutes=2 * mins):
            if rng.random() < 1.0 / every:                # setup cadence
                c3 = t + step
                common = dict(
                    symbol=symbol, tf=tf, dir="BUY" if rng.random() < 0.5 else "SELL",
                    c2_time=t.strftime("%Y.%m.%d %H:%M"),
                    c3_time=c3.strftime("%Y.%m.%d %H:%M"),
                    session=_session_of(c3.hour),
                    sweep_atr=float(rng.uniform(0.1, 1.5)),
                    chop_wick=float(rng.uniform(0.3, 0.9)),
                    chop_overlap=float(rng.uniform(0.3, 0.9)),
                    dir_changes=int(rng.integers(0, 7)),
                    chop_pass=1 if rng.random() < 0.55 else 0,
                    atr_pts=float(rng.uniform(80.0, 260.0)) if symbol == "XAUUSD"
                            else float(rng.uniform(6.0, 18.0)),
                    atr_pct=float(rng.uniform(0.05, 0.95)),
                    c1_swing=1 if rng.random() < 0.3 else 0,
                    swept_pd=1 if rng.random() < 0.3 else 0,
                    c3_body=float(rng.uniform(0.0, 1.0)),
                    c3_dir_ok=1 if rng.random() < 0.5 else 0,
                    win_p=_win_p(symbol, tf, _session_of(c3.hour)),
                )
                for eb in EB_GRID:
                    for sb in SB_GRID:
                        lines.append(_variant_row(rng, common, eb, sb))
            t += step
    path = os.path.join(out_dir, f"CRT_CONT_RESEARCH_{symbol}.csv")
    with open(path, "w", encoding="latin-1", newline="") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    return path


def generate_corpus(out_dir: str, symbols, months: int = 41, seed: int = 7) -> list:
    """The full selftest corpus. Returns the CSV paths (deterministic)."""
    os.makedirs(out_dir, exist_ok=True)
    return [generate_symbol_csv(out_dir, s, months, seed) for s in symbols]
