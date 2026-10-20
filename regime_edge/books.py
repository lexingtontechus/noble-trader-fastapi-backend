"""books.py — the decisive gate backtest: Kairos-as-is vs corrected vs null.

Same slot grid (every 24 bars from warmup 720), per-book NO-OVERLAP walk.

kairos (faithful live repro)
    direction = debounced HMM label; gates: HMM conf >= 0.60 AND "EV > 0"
    where p_win = the full-sample repo-style bucket base rate measured on
    this repro's own HMM-direction trades (the measured-table approximation;
    the live gate prices the brick payoff x ATR probability — W-C1 — while we
    price the shipped ATR bracket: both pass ~always at RR ~4-5 and base rate
    ~0.2, which IS the live behavior being proven).  Geometry = current band
    centre, expiry 48.

corrected (the fix)
    direction = sign(er_signed) only when |er_signed| >= tau* else ABSTAIN;
    tau* from a grid chosen on the first 70% of slots by max mean R (train
    split); geometry = the physics-constrained corrected geometry (fit on
    train slots only); publish gate: p_tp*(tp/sl) - p_sl - cost/sl > 0 from
    the walk-forward regime-conditional cells (no lookahead); abstain
    otherwise.

null
    direction = random +-1 (seed 7), corrected geometry, every slot.

DM tests (repo's vol_backtest/dm.py: Newey-West HAC + Holm): kairos vs
corrected and corrected vs null on per-slot loss series (loss = -R; an
abstaining book contributes R=0 at that slot; both books evaluated on the
same slot grid).  Sign convention: dm.a = first-named book; negative
mean_loss_diff => first-named book better.
"""
from __future__ import annotations

import numpy as np

from .brackets import resolve, slot_grid, walk_book
from .edge import _vol_bin

TAU_GRID = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)


def _book_metrics(trades, n_grid):
    """n, outcome shares, mean R, per-trade Sharpe, max drawdown, equity."""
    if not trades:
        return {"n": 0, "tp": 0.0, "sl": 0.0, "exp": 0.0, "mean_r": 0.0,
                "sharpe_r": 0.0, "maxdd_r": 0.0, "equity": [0.0]}
    r = np.array([t["r"] for t in trades], dtype=float)
    outcomes = [t["outcome"] for t in trades]
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if eq.size else 0.0
    sharpe = float(r.mean() / r.std(ddof=1)) if r.size > 1 and r.std(ddof=1) > 0 else 0.0
    # equity downsampled to <= 600 points, 3dp
    if eq.size <= 600:
        pts = eq
    else:
        idx = np.linspace(0, eq.size - 1, 600).astype(int)
        pts = eq[idx]
    return {
        "n": int(r.size),
        "tp": round(float(np.mean([o == "tp" for o in outcomes])), 4),
        "sl": round(float(np.mean([o == "sl" for o in outcomes])), 4),
        "exp": round(float(np.mean([o == "expire" for o in outcomes])), 4),
        "mean_r": round(float(r.mean()), 4),
        "sharpe_r": round(sharpe, 4),
        "maxdd_r": round(dd, 4),
        "equity": [round(float(x), 3) for x in pts],
    }


def _r_series(slots, trades):
    """Per-slot R aligned on the grid (0 where the book abstained/skipped)."""
    m = {t["slot"]: t["r"] for t in trades}
    return np.array([m.get(i, 0.0) for i in slots], dtype=float)


def run_books(symbol, ohlc, features, values, hmm_out, geo_current, geo_corrected,
              wf_preds_corrected, cost_atr, tau_grid=TAU_GRID, seed=7):
    high, low, close, atr = ohlc["high"], ohlc["low"], ohlc["close"], ohlc["atr"]
    n = ohlc["n"]
    slots = slot_grid(n, warmup=720, step=24)
    er_s = np.asarray(features["er_signed"], float)
    volp = np.asarray(features["vol_pct"], float)
    hmm_dir = hmm_out["direction"]
    hmm_conf = hmm_out["conf"]

    # ── kairos: full-sample repo-style bucket base rate on HMM-direction trades
    hmm_trades = []
    for i in slots:
        d = int(hmm_dir[i]) if np.isfinite(hmm_dir[i]) else 0
        if d == 0:
            continue
        o, r, end = resolve(i, d, geo_current["tp_atr"], geo_current["sl_atr"],
                            geo_current["expiry"], high, low, close, atr)
        hmm_trades.append({"slot": i, "direction": d, "outcome": o, "r": r, "end": end,
                           "vol": float(volp[i])})
    bucket = int(np.floor(geo_current["tp_atr"]))
    cells = {}
    for t in hmm_trades:
        cells.setdefault((_vol_bin(t["vol"]), bucket), []).append(t)
    base = {
        "p_win": float(np.mean([t["outcome"] == "tp" for t in hmm_trades])) if hmm_trades else 0.0,
        "p_sl": float(np.mean([t["outcome"] == "sl" for t in hmm_trades])) if hmm_trades else 0.0,
    }
    cell_stats = {}
    for key, ts in cells.items():
        cell_stats[key] = {
            "p_win": float(np.mean([t["outcome"] == "tp" for t in ts])),
            "p_sl": float(np.mean([t["outcome"] == "sl" for t in ts])),
            "n": len(ts),
        }
    rr = geo_current["tp_atr"] / geo_current["sl_atr"]
    gate_pass = {"conf_pass": 0, "ev_pass": 0, "n_slots": len(slots),
                 "conf_pass_trend": 0, "conf_pass_neutral": 0,
                 "ev_pass_trend": 0, "ev_pass_neutral": 0}

    def decide_kairos(i):
        d = int(hmm_dir[i]) if np.isfinite(hmm_dir[i]) else 0
        if d == 0:
            return None
        c = hmm_conf[i]
        if not np.isfinite(c) or c < 0.60:
            return None
        gate_pass["conf_pass"] += 1
        is_trend = values["regime"][i] in ("trend_up", "trend_down")
        gate_pass["conf_pass_trend" if is_trend else "conf_pass_neutral"] += 1
        st = cell_stats.get((_vol_bin(volp[i]), bucket), base)
        ev = st["p_win"] * rr - st["p_sl"]
        if ev <= 0:
            return None
        gate_pass["ev_pass"] += 1
        gate_pass["ev_pass_trend" if is_trend else "ev_pass_neutral"] += 1
        return d

    def resolve_kairos(i, d):
        return resolve(i, d, geo_current["tp_atr"], geo_current["sl_atr"],
                       geo_current["expiry"], high, low, close, atr)

    kairos_trades = walk_book(slots, decide_kairos, resolve_kairos)

    # ── corrected: tau* on train split (ungated mean R), then gated walk
    split = max(1, int(0.70 * len(slots)))
    train_slots = slots[:split]

    def resolve_corr(i, d):
        return resolve(i, d, geo_corrected["tp_atr"], geo_corrected["sl_atr"],
                       geo_corrected["expiry"], high, low, close, atr)

    best_tau, best_mr = None, -np.inf
    for tau in tau_grid:
        def decide_tau(i, _tau=tau):
            v = er_s[i]
            if not np.isfinite(v) or abs(v) < _tau:
                return None
            return 1 if v > 0 else -1
        tr = walk_book(train_slots, decide_tau, resolve_corr)
        mr = float(np.mean([t["r"] for t in tr])) if tr else float("nan")
        if np.isfinite(mr) and mr > best_mr:
            best_mr, best_tau = mr, tau
    tau_star = float(best_tau if best_tau is not None else tau_grid[0])

    rr_c = geo_corrected["tp_atr"] / geo_corrected["sl_atr"]
    publish_pass = {"n": 0, "ev_positive": 0}

    def decide_corr(i):
        v = er_s[i]
        if not np.isfinite(v) or abs(v) < tau_star:
            return None
        d = 1 if v > 0 else -1
        pred = wf_preds_corrected.get(i)
        if pred is None:  # before the first walk-forward fit -> no publish evidence
            return None
        p_tp, p_sl, _ = pred
        ev = p_tp * rr_c - p_sl - cost_atr / geo_corrected["sl_atr"]
        publish_pass["n"] += 1
        if ev <= 0:
            return None
        publish_pass["ev_positive"] += 1
        return d

    corrected_trades = walk_book(slots, decide_corr, resolve_corr)

    # ── null: random +-1, corrected geometry, every slot
    rng = np.random.default_rng(seed)
    null_dir = {i: int(v) for i, v in zip(slots, rng.integers(0, 2, size=len(slots)) * 2 - 1)}

    def decide_null(i):
        return null_dir[i]

    null_trades = walk_book(slots, decide_null, resolve_corr)

    books = {
        "kairos": _book_metrics(kairos_trades, len(slots)),
        "corrected": _book_metrics(corrected_trades, len(slots)),
        "null": _book_metrics(null_trades, len(slots)),
    }

    # ── DM tests (repo machinery: HAC + Holm)
    from vol_backtest.dm import dm_test
    rk = _r_series(slots, kairos_trades)
    rc = _r_series(slots, corrected_trades)
    rn = _r_series(slots, null_trades)
    max_exp = max(geo_current["expiry"], geo_corrected["expiry"])
    hac_lag = max(0, max_exp // 24 - 1)
    dm_kc = dm_test(-rk, -rc, hac_lag=hac_lag, name_a="kairos", name_b="corrected")
    dm_cn = dm_test(-rc, -rn, hac_lag=hac_lag, name_a="corrected", name_b="null")
    # records for auditability
    records = []
    regime = values["regime"]
    for book, trades in (("kairos", kairos_trades), ("corrected", corrected_trades), ("null", null_trades)):
        tset = {t["slot"]: t for t in trades}
        for i in slots:
            t = tset.get(i)
            records.append({
                "symbol": symbol, "t": int(i), "book": book,
                "direction": t["direction"] if t else 0,
                "outcome": t["outcome"] if t else "abstain",
                "R": round(float(t["r"]) if t else 0.0, 4),
                "trend": round(float(er_s[i]) if np.isfinite(er_s[i]) else 0.0, 4),
                "chop": round(float(features["chop"][i]) if np.isfinite(features["chop"][i]) else 0.0, 4),
                "vol": round(float(volp[i]) if np.isfinite(volp[i]) else 0.0, 4),
                "regime_label": regime[i],
            })

    return {
        "symbol": symbol,
        "slots": slots,
        "tau_star": tau_star,
        "books": books,
        "dm": {"kc_p_raw": round(float(dm_kc.p_value), 4),
               "kc_mean_loss_diff": round(float(dm_kc.mean_loss_diff), 4),
               "cn_p_raw": round(float(dm_cn.p_value), 4),
               "cn_mean_loss_diff": round(float(dm_cn.mean_loss_diff), 4),
               "hac_lag": int(hac_lag)},
        "gate_stats": {"kairos": gate_pass, "corrected_publish": publish_pass,
                       "base": {k: round(v, 4) for k, v in base.items()},
                       "rr_current": round(rr, 4)},
        "records": records,
        "_series": {"kairos": rk, "corrected": rc, "null": rn},
        "_trades": {"kairos": kairos_trades, "corrected": corrected_trades,
                    "null": null_trades},
    }


def pooled_books(results):
    """Pooled-across-symbols books: full book-metrics shape on the
    concatenated trades (symbol-blocked order) + DM tests on the concatenated
    per-slot loss series (abstain slots contribute R=0)."""
    from vol_backtest.dm import dm_test
    out = {}
    series = {}
    for book in ("kairos", "corrected", "null"):
        trades = [t for r in results for t in r["_trades"][book]]
        out[book] = _book_metrics(trades, sum(len(r["slots"]) for r in results))
        series[book] = np.concatenate([r["_series"][book] for r in results])
    hac_lag = 3  # max corrected expiry 96 bars / 24-bar slots - 1
    dm_kc = dm_test(-series["kairos"], -series["corrected"], hac_lag=hac_lag,
                    name_a="kairos", name_b="corrected")
    dm_cn = dm_test(-series["corrected"], -series["null"], hac_lag=hac_lag,
                    name_a="corrected", name_b="null")
    out.update({
        "kc_p": round(float(dm_kc.p_value), 4),
        "kc_mean_loss_diff": round(float(dm_kc.mean_loss_diff), 4),
        "cn_p": round(float(dm_cn.p_value), 4),
        "cn_mean_loss_diff": round(float(dm_cn.mean_loss_diff), 4),
        "hac_lag": hac_lag,
        "note": "trades concatenated symbol-blocked; DM on per-slot series "
                "(abstain/skip slots contribute R=0)",
    })
    return out


def run_holm(raw_ps):
    """Holm-Bonferroni adjusted p-values (repo vol_backtest.dm.holm_adjust)."""
    from vol_backtest.dm import holm_adjust
    return [float(x) for x in holm_adjust(np.asarray(raw_ps, dtype=float))]
