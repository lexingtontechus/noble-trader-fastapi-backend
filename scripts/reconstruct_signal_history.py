#!/usr/bin/env python3
"""
reconstruct_signal_history.py — rebuild light-sweep-style signals for ONE
symbol from TDVA 1h OHLC history (~21 months; TDVA serves 1h bars back to
2025-01-01) and run the pre-registered Kairos tests H1–H4 on them.

Read-only against production: nothing is written to Supabase. Outputs go to
.cache/ (gitignored). Scope: worklog/kairos/scopes/20260923_KAIROS_EDGE_STATE_SCOPE.md.

What is reproduced exactly (production code, not re-implementations):
  • regime label  — RegimeHMM (runtime config) fit+predict on the trailing
                    200 closes, label-continuity means carried bar to bar
                    (orchestrator.py _process_symbol §1; the light sweep
                    fetches 200 bars, orchestrator.py:2569)
  • direction     — sell iff "bear" in label (_compute_trade_signal)
  • ATR           — heavy_sweep.compute_atr(closes, 14) (orchestrator.py:3135)
  • settlement    — settlement_contract.settle, 48-bar expiry, tp_first,
                    close-walk from the entry bar (eod_resolver parity)

What is NOT reproduced (so this is the FULL-STREAM cohort, not qualified):
  • geometry      — the heavy sweep re-picks brick/SL/TP daily; replaying 21
                    months of heavy sweeps is out of scope. Fixed geometry:
                    brick = K × ATR14, K = the symbol's live median brick/ATR
                    from the 30-day cache, SL/TP bricks = the live modal combo.
  • gates         — kelly / p_win / TimesFM / calibration state have no
                    history; TimesFM forecasts cannot be regenerated.
  • tick cadence  — one decision per completed 1h bar (live: every 15 min
                    on a forming bar). Hourly labels are comparable to the
                    3-tick debounced live labels, not to raw tick labels.

R convention (matches the persistence script): tp → tp_d/sl_d, sl → −1,
expired → 0. Knob variants are path-exact re-settlements (not MFE/MAE
shortcuts), under close-walk and OHLC (wick) touch rules.

Usage:
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/reconstruct_signal_history.py \
      --symbol EURUSD [--refresh-bars] [--refresh-labels] [--workers 10] [--n-perm 40]
"""

from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")   # one BLAS thread per worker
os.environ.setdefault("MKL_NUM_THREADS", "1")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

WINDOW = 200          # light sweep bar count (orchestrator.py:2569)
EXPIRY = 48           # settlement_contract.EXPIRY_BARS
WARM = 48             # continuity warm-up fits before each worker chunk
HMM_KEYS = ("n_components", "n_iter", "covariance_type", "vol_window_long",
            "vol_window_short", "trend_window_short", "trend_window_long", "random_state")
CACHE = ROOT / ".cache"
QUADS = ["edge_rising", "edge_decaying", "edge_recovering", "no_edge"]
RNG = np.random.default_rng(11)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# ─── 1. bars ───────────────────────────────────────────────────────────


async def _tv_symbol(symbol: str) -> str:
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
    for s in await fetch_active_symbols():
        if s["symbol"] == symbol and s.get("tradingview_symbol"):
            return s["tradingview_symbol"]
    raise SystemExit(f"{symbol}: no tradingview_symbol in nt_symbol — pass --tv-symbol")


async def _fetch_bars(tv_symbol: str) -> list[dict]:
    """1h OHLC via the same TDVA endpoint the sweeps use (price_fetcher.py),
    requesting more bars than exist so the server returns its full depth."""
    import httpx

    from regime_platform.core.settings import settings as s
    headers = {"x-rapidapi-key": s.TRADINGVIEW_API_KEY, "x-rapidapi-host": s.TRADINGVIEW_API_HOST,
               "Content-Type": "application/json"}
    body = {"requests": [{"symbol": tv_symbol, "timeframe": "60", "range": 20000}]}
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{s.TRADINGVIEW_BASE_URL}/api/price/batch", headers=headers, json=body)
    r.raise_for_status()
    item = r.json()["data"]["data"][0]
    hist = item.get("data", item).get("history", [])
    now = time.time()
    out = {}
    for b in hist:
        t = float(b["time"])
        t = t / 1000 if t > 1e11 else t
        if t + 3600 > now:        # bar still forming — not a completed close
            continue
        out[int(t)] = {"t": int(t), "o": float(b["open"]), "h": float(b["max"]),
                       "l": float(b["min"]), "c": float(b["close"])}
    # TDVA history is DESCENDING (AGENTS.md pitfall 1) — sort ascending here.
    return [out[k] for k in sorted(out)]


# ─── 2. regime labels (production RegimeHMM, parallel chunks) ──────────


def _label_chunk(args) -> list[tuple[int, str, float, bool]]:
    """Cold fit per bar: a fresh RegimeHMM on the trailing 200 closes with
    no carried label means. Measured on the 30-day overlap (EURUSD, 120
    sampled hours): cold fits match the live label on 82% of hours for
    direction / 77% vol / 58% 16-cell, while carrying continuity means
    bar-to-bar across 10k bars drifts to chance (52% / 24% / 5%)."""
    closes, start, end, kw = args
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)
    from regime_platform.core.regime_engine import RegimeHMM

    arr = np.asarray(closes, dtype=float)
    out = []
    for t in range(max(WINDOW - 1, start), end):
        w = arr[t - WINDOW + 1: t + 1]
        try:
            m = RegimeHMM(**kw)
            m.fit(w)
            snap = m.predict(w)
        except Exception:
            continue
        if snap.regime_label:
            out.append((t, snap.regime_label, float(snap.confidence),
                        bool(getattr(snap, "trend_converged", True))))
    return out


async def _hmm_kwargs() -> dict:
    from regime_platform.core.regime_engine import RegimeHMM
    m = await RegimeHMM.from_runtime_config()
    return {k: getattr(m, k) for k in HMM_KEYS if hasattr(m, k)}


def label_bars(bars: list[dict], kw: dict, workers: int) -> list[tuple[int, str, float, bool]]:
    closes = [b["c"] for b in bars]
    n = len(closes)
    starts = np.linspace(WINDOW - 1, n, workers * 3 + 1).astype(int)
    jobs = [(closes, int(a), int(b), kw) for a, b in zip(starts[:-1], starts[1:]) if b > a]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        parts = list(ex.map(_label_chunk, jobs))
    labels = sorted(x for p in parts for x in p)
    print(f"labelled {len(labels)} bars in {time.time() - t0:.0f}s ({workers} workers)")
    return labels


# ─── 3. signals + settlement ───────────────────────────────────────────


def build_signals(bars, labels, symbol, k_atr, sl_b, tp_b,
                  alt_geoms: tuple[tuple[int, int], ...] = ()) -> list[dict]:
    from regime_platform.sweep_orchestrator import settlement_contract as sc
    from regime_platform.sweep_orchestrator.heavy_sweep import compute_atr

    closes = [b["c"] for b in bars]
    rows = []
    for t, label, conf, conv in labels:
        if t + EXPIRY >= len(bars):
            continue
        sig = "sell" if "bear" in label.lower() else "buy"
        sgn = 1.0 if sig == "buy" else -1.0
        atr = compute_atr(closes[t - WINDOW + 1: t + 1], period=14)
        if atr <= 0:
            continue
        brick = k_atr * atr
        entry = closes[t]
        sl_d, tp_d = brick * sl_b, brick * tp_b
        win_c = closes[t: t + EXPIRY]      # eod_resolver: walk starts at the entry bar
        win_o = [{"close": b["c"], "high": b["h"], "low": b["l"]} for b in bars[t + 1: t + EXPIRY]]

        def settle(bars_, sd, td):
            res = sc.settle(entry_price=entry, stop_loss=entry - sgn * sd, take_profit=entry + sgn * td,
                            bars=bars_, direction=sig, expiry_bars=EXPIRY,
                            same_bar_policy=sc.SAME_BAR_TP_FIRST)
            r = td / sd if res.outcome == sc.OUTCOME_TP else (-1.0 if res.outcome == sc.OUTCOME_SL else 0.0)
            return res, r

        base, r_c = settle(win_c, sl_d, tp_d)
        _, r_o = settle(win_o, sl_d, tp_d)
        _, r_tsl_c = settle(win_c, 0.5 * sl_d, tp_d)
        _, r_ttp_c = settle(win_c, sl_d, 0.5 * tp_d)
        _, r_tsl_o = settle(win_o, 0.5 * sl_d, tp_d)
        _, r_ttp_o = settle(win_o, sl_d, 0.5 * tp_d)
        exit_bar = t + (base.exit_idx if base.exit_idx is not None else EXPIRY - 1)
        resolved = bars[exit_bar]["t"] + 3600

        def drift(k):
            return round(sgn * (win_c[k] - entry) / entry * 100.0, 6) if k < len(win_c) else None

        # Alternate geometries (same brick, different SL/TP brick counts):
        # R under close and OHLC touch rules, plus the close-walk resolution
        # time — the quadrant lag windows must use each geometry's OWN
        # resolved_at (no-lookahead rule).
        alt = {}
        for g_sl, g_tp in alt_geoms:
            g_res, g_rc = settle(win_c, brick * g_sl, brick * g_tp)
            _, g_ro = settle(win_o, brick * g_sl, brick * g_tp)
            g_exit = t + (g_res.exit_idx if g_res.exit_idx is not None else EXPIRY - 1)
            alt[f"r_close_g{g_sl}x{g_tp}"] = g_rc
            alt[f"r_ohlc_g{g_sl}x{g_tp}"] = g_ro
            alt[f"res_g{g_sl}x{g_tp}"] = bars[g_exit]["t"] + 3600

        rows.append({
            "signal_id": f"recon-{symbol}-{bars[t]['t']}", "symbol": symbol, "signal": sig,
            "ts": _iso(bars[t]["t"] + 3600), "resolved_at": _iso(resolved), "exit_ts": _iso(resolved),
            "bar_open": bars[t]["t"], "regime": label, "regime_conf": conf, "trend_converged": conv,
            "outcome": base.outcome, "pnl_bricks": (tp_b if base.outcome == "tp_hit" else
                                                    -sl_b if base.outcome == "sl_hit" else
                                                    sgn * (win_c[-1] - entry) / brick),
            "sl_bricks": sl_b, "tp_bricks": tp_b, "qualified": None, "source": "recon-1h",
            "entry_price": entry, "stop_loss": entry - sgn * sl_d, "take_profit": entry + sgn * tp_d,
            "exit_price": base.exit_price, "mfe": base.mfe, "mae": base.mae, "hold_bars": base.hold_bars,
            "drift_4h_pct": drift(4), "drift_24h_pct": drift(24), "atr": atr,
            "effective_kelly": None, "ev": None, "p_win": None, "p_win_dir": None,
            "r_close": r_c, "r_ohlc": r_o, "r_tsl_close": r_tsl_c, "r_ttp_close": r_ttp_c,
            "r_tsl_ohlc": r_tsl_o, "r_ttp_ohlc": r_ttp_o, **alt,
        })
    return rows


# ─── 4. validation against the live 30-day overlap ─────────────────────


def validate(recon: pd.DataFrame, bars: list[dict], live_rows: list[dict], symbol: str) -> None:
    from regime_platform.sweep_orchestrator import settlement_contract as sc
    from regime_platform.sweep_orchestrator.bracket_reachability import _vol_tier

    live = pd.DataFrame(live_rows)
    live = live[(live.source == "sweep-light") & (live.symbol == symbol)].copy()
    if live.empty:
        print("  no live rows for this symbol in the cache — skipped")
        return
    live["ts"] = pd.to_datetime(live.ts, utc=True, format="ISO8601")
    live["hr"] = live.ts.dt.floor("h")
    lh = live.groupby("hr").regime.agg(reg=lambda s: s.mode().iat[0], k="nunique")
    rb = pd.Series(recon.regime.values, index=pd.to_datetime(recon.bar_open, unit="s", utc=True))

    def parts(lbl: pd.Series) -> pd.DataFrame:
        vol = lbl.map(lambda x: _vol_tier(x) if isinstance(x, str) else None)
        d = lbl.map(lambda x: ("bull" if "bull" in x else "bear") if isinstance(x, str) else None)
        return pd.DataFrame({"reg": lbl, "vol": vol, "dir": d}, index=lbl.index)

    L = parts(lh.reg)
    print(f"  live hours {len(L)} ({L.index.min().date()}..{L.index.max().date()}); "
          f"hours where live ticks disagreed among themselves: {(lh.k > 1).mean():.0%}")
    for name, shift in (("recon bar open = live hour (forming bar)", 0),
                        ("recon bar open = live hour − 1h (last completed bar)", -1)):
        R = parts(rb.reindex(L.index + pd.Timedelta(hours=shift))).set_axis(L.index)
        ok = R.reg.notna()
        cells = []
        for c in ("reg", "vol", "dir"):
            agree = (L.loc[ok, c] == R.loc[ok, c]).mean()
            p, q = L.loc[ok, c].value_counts(normalize=True), R.loc[ok, c].value_counts(normalize=True)
            chance = float((p * q.reindex(p.index).fillna(0)).sum())
            stable = ok & (lh.k == 1)
            agree_st = (L.loc[stable, c] == R.loc[stable, c]).mean()
            cells.append(f"{c} {agree:.0%} (chance {chance:.0%}; stable-hours {agree_st:.0%})")
        print(f"  {name}: n={int(ok.sum())} · " + " · ".join(cells))

    # settlement parity: live entry/SL/TP re-settled on TDVA closes
    idx = {pd.Timestamp(b["t"], unit="s", tz="UTC"): i for i, b in enumerate(bars)}
    closes = [b["c"] for b in bars]
    ep = live[live.outcome.isin(["tp_hit", "sl_hit", "expired"])].sort_values("ts") \
        .groupby(["signal", "hr"], as_index=False).first()
    same, n = 0, 0
    for r in ep.itertuples():
        i = idx.get(r.hr)
        if i is None or i + EXPIRY > len(closes):
            continue
        res = sc.settle(entry_price=float(r.entry_price), stop_loss=float(r.stop_loss),
                        take_profit=float(r.take_profit), bars=closes[i: i + EXPIRY],
                        direction=r.signal, expiry_bars=EXPIRY, same_bar_policy=sc.SAME_BAR_TP_FIRST)
        n += 1
        same += res.outcome == r.outcome
    print(f"  settlement parity (live entry/SL/TP re-settled on TDVA closes): "
          f"{same}/{n} = {same / max(n, 1):.1%} same outcome")


# ─── 5. hypothesis tests (single symbol) ───────────────────────────────


def _day_boot(s: pd.DataFrame, stat, n: int = 2000) -> tuple[float, float, float, float]:
    """Point estimate, 95% CI and two-sided p from a day-cluster bootstrap.
    `stat(frame)` must return a float."""
    days = s.day.unique()
    groups = {d: g for d, g in s.groupby("day")}
    est = stat(s)
    boots = []
    for _ in range(n):
        pick = RNG.choice(days, size=len(days), replace=True)
        boots.append(stat(pd.concat([groups[d] for d in pick], ignore_index=True)))
    b = np.array([x for x in boots if np.isfinite(x)])
    lo, hi = np.percentile(b, [2.5, 97.5])
    p = min(1.0, 2 * min((b <= 0).mean(), (b >= 0).mean()))
    return est, lo, hi, p


def _fast_boot(s: pd.DataFrame, col: str, mask: pd.Series | None = None, vs_all: bool = False,
               n: int = 4000) -> tuple[float, float, float, float]:
    """Vectorised day-cluster bootstrap for a mean (mask subset) or for
    mean(subset) − mean(all). Returns (est, lo, hi, p two-sided)."""
    m = mask if mask is not None else pd.Series(True, index=s.index)
    g = pd.DataFrame({"day": s.day, "x": s[col], "m": m.astype(float)})
    g["xm"] = g.x * g.m
    agg = g.groupby("day").agg(sx=("x", "sum"), n=("x", "size"), sxm=("xm", "sum"), nm=("m", "sum"))
    A = agg.to_numpy()
    idx = RNG.integers(0, len(A), (n, len(A)))
    S = A[idx].sum(axis=1)
    sub = S[:, 2] / np.maximum(S[:, 3], 1)
    b = sub - S[:, 0] / S[:, 1] if vs_all else sub
    tot = A.sum(axis=0)
    est = tot[2] / max(tot[3], 1) - (tot[0] / tot[1] if vs_all else 0.0)
    lo, hi = np.percentile(b, [2.5, 97.5])
    return est, lo, hi, min(1.0, 2 * min((b <= 0).mean(), (b >= 0).mean()))


def _f(t) -> str:
    est, lo, hi, p = t
    return f"{est:+.3f} [{lo:+.3f},{hi:+.3f}] p={p:.3f}"


def _p_stay(s: pd.DataFrame, h: int = 1) -> pd.Series:
    cq = s.groupby(["vol", "direction", "day"]).quad.first().rename("q").reset_index()
    nxt = cq.assign(day=cq.day - pd.Timedelta(days=h))
    j = cq.merge(nxt, on=["vol", "direction", "day"], suffixes=("", "_n"))
    return (j.q == j.q_n).groupby(j.q).mean().reindex(QUADS)


# ─── vectorised point-in-time quadrant panel (exact; enables 1000+ perms) ──
#
# quadrant_panel() in the persistence script rebuilds cell tables with
# pandas for every day (≈40 s per EURUSD panel). The lag slice it uses is
# {ts ≥ d − w, resolved_at < d}. Because every signal resolves within 48
# bars (< 7 wall-clock days even across weekends — asserted below),
# ts < d − w implies resolved_at < d, so the slice sum is exactly
#     Σ[resolved_at < d] − Σ[ts < d − w],
# two prefix sums + searchsorted. Verified equal to quadrant_panel() in
# `verify_fast_panel`. Shrinkage mirrors cell_table: toward the symbol's own
# mean over the same lag slice, SHRINK_K pseudo-episodes.


def _win(ts, res, R, days, w):
    """Count and per-row sum of R over {ts ≥ d−w, res < d} for each d.
    R is (P, n); returns n (D,), sums (P, D)."""
    o_ts, o_res = np.argsort(ts, kind="stable"), np.argsort(res, kind="stable")
    c_ts = np.concatenate([np.zeros((R.shape[0], 1)), np.cumsum(R[:, o_ts], axis=1)], axis=1)
    c_res = np.concatenate([np.zeros((R.shape[0], 1)), np.cumsum(R[:, o_res], axis=1)], axis=1)
    i_res = np.searchsorted(res[o_res], days, "left")
    i_ts = np.searchsorted(ts[o_ts], days - w, "left")
    return i_res - i_ts, c_res[:, i_res] - c_ts[:, i_ts]


class FastPanel:
    """Quadrant stamps for every episode, for any number of R vectors."""

    DAY = np.int64(86_400 * 10**9)

    def __init__(self, ep: pd.DataFrame, t0, last_fwd):
        import regime_cell_edge_persistence as P
        self.K, self.min_lag = P.SHRINK_K, P.MIN_LAG
        self.ep = ep.reset_index(drop=True)
        self.ts = self.ep.ts.astype("datetime64[ns, UTC]").astype("int64").to_numpy()
        self.res = self.ep.resolved_at.astype("datetime64[ns, UTC]").astype("int64").to_numpy()
        hold = (self.res - self.ts).max() / self.DAY
        assert hold < 7, f"max hold {hold:.1f} d breaks the prefix-sum identity (needs < 7 d)"
        days = pd.date_range(t0 + pd.Timedelta(days=14), last_fwd, freq="D", inclusive="left")
        self.days = days.astype("datetime64[ns, UTC]").astype("int64").to_numpy()
        day_of = self.ep.ts.dt.floor("D").astype("datetime64[ns, UTC]").astype("int64").to_numpy()
        di = np.searchsorted(self.days, day_of)
        self.valid = (di < len(self.days)) & (self.days[np.minimum(di, len(self.days) - 1)] == day_of)
        self.di = np.minimum(di, len(self.days) - 1)
        self.sym_idx = {s: np.flatnonzero(self.ep.symbol.to_numpy() == s) for s in self.ep.symbol.unique()}
        cell = (self.ep.vol + "|" + self.ep.direction).to_numpy()
        self.cells = {s: {c: ix[cell[ix] == c] for c in np.unique(cell[ix])} for s, ix in self.sym_idx.items()}
        # (symbol, cell, day) keys that carry ≥1 episode, and the day→day+1 pairs, for P(stay)
        key = pd.Series(list(zip(self.ep.symbol, cell, self.di)))
        first = key[self.valid].drop_duplicates()
        self.key_first = first.index.to_numpy()
        pos = {k: i for i, k in enumerate(first.to_numpy())}
        pa, pb = [], []
        for k, i in pos.items():
            j = pos.get((k[0], k[1], k[2] + 1))
            if j is not None:
                pa.append(i)
                pb.append(j)
        self.pa, self.pb = np.array(pa, int), np.array(pb, int)

    def stamp(self, R: np.ndarray) -> np.ndarray:
        """R (P, n) → quad codes (P, n): 0 edge_rising, 1 edge_decaying, 2 edge_recovering,
        3 no_edge, −1 unstamped (cell below the episode floors / outside panel)."""
        R = np.atleast_2d(R)
        out = np.full(R.shape, -1, np.int8)
        d14, d7 = 14 * self.DAY, 7 * self.DAY
        for s, six in self.sym_idx.items():
            nL_s, sL_s = _win(self.ts[six], self.res[six], R[:, six], self.days, d14)
            nS_s, sS_s = _win(self.ts[six], self.res[six], R[:, six], self.days, d7)
            mL, mS = sL_s / np.maximum(nL_s, 1), sS_s / np.maximum(nS_s, 1)
            for ix in self.cells[s].values():
                nL, sL = _win(self.ts[ix], self.res[ix], R[:, ix], self.days, d14)
                nS, sS = _win(self.ts[ix], self.res[ix], R[:, ix], self.days, d7)
                lvl = (sL + self.K * mL) / (nL + self.K)
                sht = (sS + self.K * mS) / (nS + self.K)
                q = np.select([(lvl > 0) & (sht >= lvl), (lvl > 0) & (sht < lvl), (lvl <= 0) & (sht > lvl)],
                              [0, 1, 2], 3).astype(np.int8)
                q[:, ~((nL >= self.min_lag) & (nS >= 5))] = -1
                v = ix[self.valid[ix]]
                out[:, v] = q[:, self.di[v]]
        return out

    def dead_minus_all(self, R, Q) -> np.ndarray:
        st = Q >= 0
        dead = Q == 3
        return (np.where(dead, R, 0).sum(1) / np.maximum(dead.sum(1), 1)
                - np.where(st, R, 0).sum(1) / np.maximum(st.sum(1), 1))

    def quad_minus_all(self, R, Q, code) -> np.ndarray:
        st, m = Q >= 0, Q == code
        return (np.where(m, R, 0).sum(1) / np.maximum(m.sum(1), 1)
                - np.where(st, R, 0).sum(1) / np.maximum(st.sum(1), 1))

    def p_stay(self, Q) -> np.ndarray:
        """(P, 4) 1-day P(stay) per quadrant over (symbol, cell) day pairs."""
        kq = Q[:, self.key_first]
        qa, qb = kq[:, self.pa], kq[:, self.pb]
        ok = (qa >= 0) & (qb >= 0)
        out = np.full((Q.shape[0], 4), np.nan)
        for c in range(4):
            m = ok & (qa == c)
            out[:, c] = (m & (qb == c)).sum(1) / np.where(m.sum(1) > 0, m.sum(1), np.nan)
        return out

    def perms(self, r: np.ndarray, n: int, chunk: int = 50, mode: str = "circ"):
        """Yield null R matrices, per symbol.

        circ (primary): rotate each symbol's time-ordered R by a random offset
          of ≥ 14 days' worth of episodes. Keeps R's autocorrelation (hourly
          signals share most of their 48-bar path, so R comes in clumps) and
          only breaks R's alignment with the cell labels.
        iid (reference): shuffle R within symbol. Destroys autocorrelation →
          anti-conservative for anything built on rolling windows."""
        if not hasattr(self, "_order"):
            self._order = {s: six[np.argsort(self.ts[six], kind="stable")] for s, six in self.sym_idx.items()}
        done = 0
        while done < n:
            k = min(chunk, n - done)
            R = np.tile(r, (k, 1))
            for six in self._order.values():
                m = len(six)
                if mode == "iid":
                    R[:, six] = R[:, six][np.arange(k)[:, None], np.argsort(RNG.random((k, m)), axis=1)]
                else:
                    lo = min(14 * 24, m // 4)
                    off = RNG.integers(lo, m - lo, size=k)
                    idx = (np.arange(m)[None, :] - off[:, None]) % m
                    R[:, six] = r[six][idx]
            done += k
            yield R


def verify_fast_panel(ep: pd.DataFrame, t0, last_fwd) -> None:
    import regime_cell_edge_persistence as P
    slow = P.quadrant_panel(ep, t0, last_fwd)
    fp = FastPanel(ep, t0, last_fwd)
    q = fp.stamp(ep.r.to_numpy()[None, :])[0]
    fast = pd.Series(np.array(QUADS + ["-"])[q], index=fp.ep.signal_id)
    fast = fast[fast != "-"]
    sl = slow.set_index("signal_id").quad
    same = sl.index.equals(fast.sort_index().index) or set(sl.index) == set(fast.index)
    agree = (fast.reindex(sl.index) == sl).mean()
    print(f"  fast panel check vs quadrant_panel(): stamped {len(fast)} vs {len(sl)} · same set {same} · "
          f"quad agreement {agree:.4f}")
    if not same or agree < 0.9999:
        raise SystemExit("fast panel diverges from quadrant_panel() — do not trust the nulls")


def stamped_frame(fp: FastPanel, Q0: np.ndarray) -> pd.DataFrame:
    """Observed panel as a DataFrame shaped like quadrant_panel()'s output."""
    import regime_cell_edge_persistence as P
    s = fp.ep[Q0 >= 0].copy()
    s["quad"] = np.array(QUADS)[Q0[Q0 >= 0]]
    s["day"] = s.ts.dt.floor("D").dt.tz_localize(None)
    for c in P.NUM:
        if c in s:
            s[c] = pd.to_numeric(s[c], errors="coerce")
    s["d24"] = s.drift_24h_pct / 100 * s.entry_price / s.atr
    return s


def geometry_check(rows: list[dict], alt_geoms, base_geom, n_perm: int) -> dict:
    """Does the quadrant result depend on the fixed 3/5-brick choice? For each
    alt SL/TP (same brick, same labels), rebuild the panel on that geometry's
    own R and resolution times; report no_edge/edge_rising edge and P(stay) vs null."""
    import regime_cell_edge_persistence as P
    out = {}
    print(f"\n  Geometry robustness (same labels & brick; each geometry scored on its own R and resolved_at; "
          f"{n_perm} within-symbol perms)")
    for g_sl, g_tp in [base_geom] + list(alt_geoms):
        tag = f"g{g_sl}x{g_tp}"
        if (g_sl, g_tp) == base_geom:
            rr = rows
        else:
            rr = [dict(r_, outcome=("tp_hit" if r_[f"r_close_{tag}"] > 0 else
                                    "sl_hit" if r_[f"r_close_{tag}"] < 0 else "expired"),
                       sl_bricks=g_sl, tp_bricks=g_tp,
                       resolved_at=_iso(r_[f"res_{tag}"])) for r_ in rows if f"r_close_{tag}" in r_]
        ep = P.episodes(rr, expired_mtm=False, source="recon-1h")
        t0 = ep.ts.min().normalize()
        last = ep.groupby("symbol").ts.max().min().normalize() - pd.Timedelta(days=2)
        fp = FastPanel(ep, t0, last)
        r = fp.ep.r.to_numpy()
        Q0 = fp.stamp(r[None, :])
        obs_d, obs_l = fp.quad_minus_all(r[None], Q0, 3)[0], fp.quad_minus_all(r[None], Q0, 0)[0]
        obs_st = fp.p_stay(Q0)[0]
        nd, nl, ns = [], [], []
        for R in fp.perms(r, n_perm):
            Q = fp.stamp(R)
            nd.append(fp.quad_minus_all(R, Q, 3))
            nl.append(fp.quad_minus_all(R, Q, 0))
            ns.append(fp.p_stay(Q))
        nd, nl, ns = np.concatenate(nd), np.concatenate(nl), np.vstack(ns)
        pdd = (1 + np.sum(np.abs(nd) >= abs(obs_d))) / (1 + n_perm)
        pll = (1 + np.sum(np.abs(nl) >= abs(obs_l))) / (1 + n_perm)
        pst = [(1 + np.sum(ns[:, i] >= obs_st[i])) / (1 + n_perm) for i in range(4)]
        print(f"    SL {g_sl}/TP {g_tp} (RR {g_tp / g_sl:.2f}){' [base]' if (g_sl, g_tp) == base_geom else ''}: "
              f"all R {r[Q0[0] >= 0].mean():+.3f} · no_edge−all {obs_d:+.3f} p={pdd:.3f} · "
              f"edge_rising−all {obs_l:+.3f} p={pll:.3f} · P(stay) "
              + " ".join(f"{q}={obs_st[i]:.2f}/{np.nanmean(ns[:, i]):.2f} p={pst[i]:.3f}"
                         for i, q in enumerate(QUADS)))
        out[tag] = {"all_r": float(r[Q0[0] >= 0].mean()), "dead_minus_all": float(obs_d), "p_dead": float(pdd),
                    "leader_minus_all": float(obs_l), "p_leader": float(pll),
                    "p_stay": [float(x) for x in obs_st], "p_stay_null": [float(x) for x in np.nanmean(ns, 0)],
                    "p_stay_p": [float(x) for x in pst]}
    return out


def holm(ps: dict[str, float]) -> dict[str, float]:
    items = sorted(ps.items(), key=lambda kv: kv[1])
    m, run, out = len(items), 0.0, {}
    for i, (k, p) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def analyse(rows: list[dict], n_perm: int, tag: str) -> dict:
    """Pre-registered H1–H4 on one or more symbols.

    Multi-symbol conventions: bootstrap clusters are symbol-days; quadrant
    effects are measured on R demeaned WITHIN symbol (so a symbol that is
    mostly no_edge and also weak cannot masquerade as a no_edge effect); nulls
    shuffle R within symbol and rebuild the whole point-in-time panel
    (FastPanel, verified exact against quadrant_panel)."""
    import regime_cell_edge_persistence as P

    ep = P.episodes(rows, expired_mtm=False, source="recon-1h")
    for c in ("r_close", "r_ohlc", "r_tsl_close", "r_ttp_close", "r_tsl_ohlc", "r_ttp_ohlc"):
        ep[c] = pd.to_numeric(ep[c], errors="coerce")
    syms = sorted(ep.symbol.unique())
    t0, t1 = ep.ts.min().normalize(), ep.ts.max().normalize()
    last_fwd = t1 - pd.Timedelta(days=2)
    print(f"\n== {tag}: {len(ep)} episodes, {len(syms)} symbol(s) {t0.date()}..{t1.date()} · mean R "
          f"{ep.r.mean():+.3f} (close) / {ep.r_ohlc.mean():+.3f} (OHLC) · tp {ep.win.mean():.0%} sl "
          f"{(ep.outcome == 'sl_hit').mean():.0%} expired {ep.expired.mean():.0%} ==")
    reg = ep.drop_duplicates(["symbol", "hr"]).set_index(["symbol", "hr"]).regime
    for h in (1, 24, 48):
        key = pd.MultiIndex.from_arrays([ep.symbol, ep.hr + pd.Timedelta(hours=h)])
        fut = pd.Series(reg.reindex(key).to_numpy(), index=ep.index)
        ok = fut.notna()
        same16 = (ep.regime[ok] == fut[ok]).mean()
        dsame = (ep.regime[ok].str.contains("bull") == fut[ok].str.contains("bull")).mean()
        print(f"  P(same after {h:>2}h): 16-cell {same16:.2f} · direction {dsame:.2f}")

    fp = FastPanel(ep, t0, last_fwd)
    r = fp.ep.r.to_numpy()
    sym_arr = fp.ep.symbol.to_numpy()
    sym_mean = pd.Series(r).groupby(sym_arr).transform("mean").to_numpy()
    Q0 = fp.stamp(r[None, :])
    s = stamped_frame(fp, Q0[0])
    s["clu"] = s.symbol + "|" + s.day.dt.strftime("%Y-%m-%d")
    for c in ("r", "r_ohlc", "r_close"):
        s[f"{c}_x"] = s[c] - s.groupby("symbol")[c].transform("mean")
    print(f"  quadrant panel: {len(s)} stamped episodes, {s.clu.nunique()} symbol-days, share "
          + ", ".join(f"{q} {v:.0%}" for q, v in s.quad.value_counts(normalize=True).reindex(QUADS).items()))

    def boot(frame, col, mask=None, vs_all=False):
        return _fast_boot(frame.assign(day=frame.clu), col, mask, vs_all)

    ps: dict[str, float] = {}

    # H1 — no_edge forward edge (within-symbol R)
    print("\n  H1 no_edge has above-average edge (R demeaned within symbol; symbol-day cluster bootstrap)")
    for qd in QUADS:
        t = boot(s, "r_x", s.quad == qd, vs_all=True)
        print(f"    same-day {qd:9s} R − all: {_f(t)}   (raw R {s[s.quad == qd].r.mean():+.3f}, "
              f"n={int((s.quad == qd).sum())})")
        if qd == "no_edge":
            ps["H1 no_edge same-day"] = t[3]
    t = boot(s, "r_ohlc_x", s.quad == "no_edge", vs_all=True)
    print(f"    same-day no_edge − all, OHLC settlement: {_f(t)}")

    # nulls — whole point-in-time panel rebuilt for every null draw.
    # PRIMARY = circular shift (keeps R autocorrelation); iid shown for reference.
    t_null = time.time()
    obs = {c: fp.quad_minus_all((r - sym_mean)[None], Q0, c)[0] for c in range(4)}
    obs_stay = fp.p_stay(Q0)[0]
    nulls = {}
    for mode in ("circ", "iid"):
        nq, nst = [], []
        for R in fp.perms(r, n_perm, mode=mode):
            Q = fp.stamp(R)
            Rx = R - sym_mean          # both nulls preserve each symbol's mean
            nq.append(np.stack([fp.quad_minus_all(Rx, Q, c) for c in range(4)], axis=1))
            nst.append(fp.p_stay(Q))
        nulls[mode] = (np.vstack(nq), np.vstack(nst))
    null_q, null_stay = nulls["circ"]
    print(f"    null draws ({n_perm} per mode, {time.time() - t_null:.0f}s) — circ = circular shift of each "
          f"symbol's R vs its labels (primary), iid = within-symbol shuffle (reference):")
    for c, qd in enumerate(QUADS):
        line = []
        for mode in ("circ", "iid"):
            nq_ = nulls[mode][0][:, c]
            p = (1 + np.sum(np.abs(nq_) >= abs(obs[c]))) / (1 + n_perm)
            lo, hi = np.percentile(nq_, [2.5, 97.5])
            line.append(f"{mode} [{lo:+.3f},{hi:+.3f}] p={p:.4f}")
            if qd == "no_edge" and mode == "circ":
                ps["H1 no_edge vs shift null"] = p
        print(f"      {qd:9s} − all observed {obs[c]:+.3f} · " + " · ".join(line))

    # H2 — stickiness beyond the rolling-window mechanics
    print("\n  H2 edge_rising & no_edge stickier than rolling windows + R autocorrelation (1-day P(stay) vs null)")
    for i, qd in enumerate(QUADS):
        p = (1 + np.sum(null_stay[:, i] >= obs_stay[i])) / (1 + n_perm)
        ns_iid = nulls["iid"][1][:, i]
        p_iid = (1 + np.sum(ns_iid >= obs_stay[i])) / (1 + n_perm)
        print(f"    {qd:9s} observed {obs_stay[i]:.2f} · circ null {np.nanmean(null_stay[:, i]):.2f} p={p:.4f} · "
              f"iid null {np.nanmean(ns_iid):.2f} p={p_iid:.4f}")
        if qd in ("edge_rising", "no_edge"):
            ps[f"H2 {qd} stay"] = p

    # H3 — reversal (user hypothesis: no_edge|edge_rising predict reversal)
    print("\n  H3 reversal, excess over the symbol's base rate (within-symbol, symbol-day cluster bootstrap)")
    dirmap = reg.str.contains("bull")
    s["rev_px"] = (s.d24 < 0).astype(float).where(s.d24.notna())
    key = pd.MultiIndex.from_arrays([s.symbol, s.hr + pd.Timedelta(hours=24)])
    fut = pd.Series(dirmap.reindex(key).to_numpy(), index=s.index)
    s["rev_hmm"] = (fut.astype("boolean") != (s.direction == "bull")).astype(float).where(fut.notna())
    for col, name in (("rev_px", "price 24h against signal"), ("rev_hmm", "HMM direction flipped by +24h")):
        x = s[s[col].notna()].copy()
        x[f"{col}_x"] = x[col] - x.groupby("symbol")[col].transform("mean")
        print(f"    {name}: base {x[col].mean():.2f}")
        for qd in QUADS:
            t = boot(x, f"{col}_x", x.quad == qd, vs_all=True)
            print(f"      {qd} {x[x.quad == qd][col].mean():.2f} ({_f(t)})")
            if qd in ("no_edge", "edge_rising"):
                ps[f"H3 {qd} {col}"] = t[3]

    # H4 — knob response depends on quadrant (path-exact re-settlement)
    for mode in ("ohlc", "close"):
        s[f"tsl_{mode}"] = s[f"r_tsl_{mode}"] - s[f"r_{mode}"]
        s[f"ttp_{mode}"] = s[f"r_ttp_{mode}"] - s[f"r_{mode}"]
        for k in ("tsl", "ttp"):
            s[f"{k}_{mode}_x"] = s[f"{k}_{mode}"] - s.groupby("symbol")[f"{k}_{mode}"].transform("mean")
        prim = mode == "ohlc"
        pooled_sl, pooled_tp = _f(boot(s, f"tsl_{mode}")), _f(boot(s, f"ttp_{mode}"))
        print(f"\n  H4 knob × quadrant, {mode.upper()} touch rule{' (primary)' if prim else ' (sensitivity)'}: "
              f"pooled tight SL {pooled_sl} · pooled tight TP {pooled_tp}")
        for qd in QUADS:
            m = s.quad == qd
            a_, b_ = boot(s, f"tsl_{mode}_x", m), boot(s, f"ttp_{mode}_x", m)
            print(f"    {qd:9s} tightSL − symbol pooled {_f(a_)} · tightTP − symbol pooled {_f(b_)}")
            if prim:
                ps[f"H4 {qd} tightSL"] = a_[3]
                ps[f"H4 {qd} tightTP"] = b_[3]

    # stability across chronological blocks
    print("\n  stability — same-day quadrant − all (within-symbol R) by chronological block")
    edges = pd.date_range(s.day.min(), s.day.max() + pd.Timedelta(days=1), periods=5)
    for a, b in zip(edges[:-1], edges[1:]):
        blk = s[(s.day >= a) & (s.day < b)]
        if blk.clu.nunique() < 20:
            continue
        t = boot(blk, "r_x", blk.quad == "no_edge", vs_all=True)
        lt = boot(blk, "r_x", blk.quad == "edge_rising", vs_all=True)
        print(f"    {a.date()}..{(b - pd.Timedelta(days=1)).date()}: all R {blk.r.mean():+.3f} · "
              f"no_edge − all {_f(t)} · edge_rising − all {_f(lt)}")

    adj = holm(ps)
    print("\n  Holm-adjusted p across the pre-registered family (pass = adj p < 0.05):")
    for k in sorted(ps, key=lambda k: ps[k]):
        print(f"    {k:28s} raw {ps[k]:.4f} → Holm {adj[k]:.4f}{'  PASS' if adj[k] < 0.05 else ''}")
    return {"episodes": len(ep), "panel": len(s), "symbols": syms, "p_raw": ps, "p_holm": adj,
            "quad_minus_all": {QUADS[c]: float(v) for c, v in obs.items()},
            "p_stay": dict(zip(QUADS, map(float, obs_stay))),
            "p_stay_null": dict(zip(QUADS, map(float, np.nanmean(null_stay, 0))))}


# ─── main ──────────────────────────────────────────────────────────────


def _live_geometry(live_rows: list[dict], symbol: str, days: int = 7) -> tuple[float, int, int]:
    """Current production geometry: brick/ATR median and modal SL/TP over
    the last `days` of live light-sweep rows (the heavy sweep re-picks
    daily, so the 30-day mode can be a combo that is no longer live)."""
    d = pd.DataFrame(live_rows)
    d = d[(d.source == "sweep-light") & (d.symbol == symbol)].copy()
    d["ts"] = pd.to_datetime(d.ts, utc=True, format="ISO8601")
    d = d[d.ts >= d.ts.max() - pd.Timedelta(days=days)]
    for c in ("entry_price", "stop_loss", "atr"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    k = ((d.entry_price - d.stop_loss).abs() / d.sl_bricks / d.atr).median()
    sl, tp = d.groupby(["sl_bricks", "tp_bricks"]).size().idxmax()
    return float(k), int(sl), int(tp)


ALT_GEOMS = ((2, 4), (3, 9), (5, 5))   # RR 2.0 / 3.0 / 1.0 around the live 3/5 (RR 1.67)


def build_symbol(sym: str, a) -> tuple[list[dict], tuple[int, int]]:
    """Fetch/label/settle one symbol; returns its signal rows and base geometry."""
    bars_f, labels_f = CACHE / f"recon_{sym}_bars_1h.json", CACHE / f"recon_{sym}_labels.json"
    live_f = CACHE / "regime_cell_edge_persistence_sim.json"
    print(f"\n################ {sym} ################")
    if a.refresh_bars or not bars_f.exists():
        tv = (a.tv_symbol if a.tv_symbol and len(a.symbols) == 1 else None) or asyncio.run(_tv_symbol(sym))
        bars = asyncio.run(_fetch_bars(tv))
        bars_f.write_text(json.dumps(bars))
    bars = json.loads(bars_f.read_text())
    gaps = np.diff([b["t"] for b in bars]) / 3600
    print(f"{sym}: {len(bars)} completed 1h bars {_iso(bars[0]['t'])[:16]} .. {_iso(bars[-1]['t'])[:16]} "
          f"· gaps >1h: {(gaps > 1).sum()} (max {gaps.max():.0f}h, weekends/holidays)")

    lab = json.loads(labels_f.read_text()) if labels_f.exists() else None
    stale = lab is None or lab["n_bars"] != len(bars) or lab["last_t"] != bars[-1]["t"]
    if a.refresh_labels or stale:
        kw = asyncio.run(_hmm_kwargs())
        print(f"RegimeHMM runtime config: {kw}")
        labels = label_bars(bars, kw, a.workers)
        labels_f.write_text(json.dumps({"n_bars": len(bars), "last_t": bars[-1]["t"], "hmm": kw,
                                        "labels": labels}))
        lab = json.loads(labels_f.read_text())
    labels = [tuple(x) for x in lab["labels"]]

    live_rows = json.loads(live_f.read_text()) if live_f.exists() else []
    k, sl, tp = _live_geometry(live_rows, sym) if live_rows else (6.0, 3, 10)
    k, sl, tp = a.k_atr or k, a.sl or sl, a.tp or tp
    print(f"geometry: brick = {k:.2f} × ATR14 (close-to-close), SL {sl} / TP {tp} bricks (RR {tp / sl:.2f})")

    alts = tuple(g for g in ALT_GEOMS if g != (sl, tp))
    rows = build_signals(bars, labels, sym, k, sl, tp, alt_geoms=alts)
    (CACHE / f"recon_{sym}_signals.json").write_text(json.dumps(rows))
    recon = pd.DataFrame(rows)
    print(f"signals: {len(rows)} (one per completed bar with a full 48-bar forward window) · "
          f"trend HMM non-converged {(~recon.trend_converged).mean():.1%}")
    print("\n== validation vs live light sweep (30-day overlap) ==")
    validate(recon, bars, live_rows, sym)
    return rows, (sl, tp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="single symbol (kept for compatibility)")
    ap.add_argument("--symbols", help="comma-separated; each is analysed alone, then pooled")
    ap.add_argument("--tv-symbol")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--geom-perm", type=int, default=300, help="perms per geometry in the robustness check")
    ap.add_argument("--refresh-bars", action="store_true")
    ap.add_argument("--refresh-labels", action="store_true")
    ap.add_argument("--k-atr", type=float, help="brick = K × ATR14 (default: live median)")
    ap.add_argument("--sl", type=int)
    ap.add_argument("--tp", type=int)
    ap.add_argument("--build-only", action="store_true", help="build/validate signals, skip analysis")
    ap.add_argument("--analyse-only", action="store_true", help="use cached signals")
    a = ap.parse_args()
    a.symbols = [x.strip() for x in (a.symbols or a.symbol or "EURUSD").split(",") if x.strip()]
    CACHE.mkdir(exist_ok=True)

    per: dict[str, list[dict]] = {}
    geoms: dict[str, tuple[int, int]] = {}
    for sym in a.symbols:
        f = CACHE / f"recon_{sym}_signals.json"
        if a.analyse_only:
            per[sym] = json.loads(f.read_text())
            geoms[sym] = (int(per[sym][0]["sl_bricks"]), int(per[sym][0]["tp_bricks"]))
        else:
            per[sym], geoms[sym] = build_symbol(sym, a)
    if a.build_only:
        return 0

    summary = {}
    for sym, rows in per.items():
        print(f"\n################ analysis: {sym} ################")
        summary[sym] = analyse(rows, a.n_perm, f"{sym} reconstruction")
        summary[sym]["geometry"] = geometry_check(rows, [g for g in ALT_GEOMS if g != geoms[sym]],
                                                  geoms[sym], a.geom_perm)
        (CACHE / f"recon_{sym}_summary.json").write_text(json.dumps(summary[sym], default=float, indent=1))
    if len(per) > 1:
        print("\n################ analysis: POOLED ################")
        pooled = [r_ for rows in per.values() for r_ in rows]
        summary["POOLED"] = analyse(pooled, a.n_perm, "pooled " + "+".join(per))
        (CACHE / "recon_POOLED_summary.json").write_text(json.dumps(summary["POOLED"], default=float, indent=1))

        print("\n################ scoreboard (Holm within each run; pass = adj p < 0.05) ################")
        keys = list(next(iter(summary.values()))["p_raw"])
        cols = list(summary)

        def cell(s_, k_):
            v = summary[s_]["p_holm"].get(k_, float("nan"))
            return f"{v:>9.4f}{' PASS' if v < 0.05 else '     '}  "

        print(f"  {'test':28s} " + " ".join(f"{k:>16s}" for k in cols))
        for k in keys:
            print(f"  {k:28s} " + " ".join(cell(s, k) for s in cols))
        for q in ("no_edge", "edge_rising"):
            print(f"  {q + ' − all (within-sym R)':28s} "
                  + " ".join(f"{summary[s]['quad_minus_all'][q]:>+16.3f}" for s in cols))
        for q in QUADS:
            print(f"  {'P(stay) ' + q + ' obs/null':28s} " + " ".join(
                f"{summary[s]['p_stay'][q]:>8.2f}/{summary[s]['p_stay_null'][q]:.2f}  " for s in summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
