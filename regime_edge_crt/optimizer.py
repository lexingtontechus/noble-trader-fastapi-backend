"""optimizer.py — walk-forward search + edge-decay replay + deploy mint.

Port of ``crt_ev_optimizer.py`` (CRT_v5_CONT_MT4.zip) with ONLY the
Task-67-sanctioned changes: IO paths, config-driven params, provenance
stamps, structured return value for the CLI/artifact/Discord layers.
The math is the toolkit's math, verbatim — see the fidelity manifest in
worklog/20261115_KAIROS_CRT0_BUILD.md §2.

Method (per setup option = symbol x TF x model x direction x session):
  1. Rebuild the exact triple-barrier outcome (R) for every (entry buffer,
     SL buffer, R, hold) variant from the exported paths. SL wins ties;
     spread/slippage included (same as the EA).
  2. Walk-forward: on each train window choose the config with the highest
     lower confidence bound  LCB = EV - z * SE  (n >= min_train_n). Score it
     on the following, unseen test window.
  3. Concatenate the out-of-sample (OOS) trades -> OOS EV, t, PF, folds
     positive, IS->OOS decay.
  4. Replay the EA's edge-decay monitor (CUSUM + rolling EV) on the OOS
     trades to show what gating would have kept or avoided.
  5. Final config = selection on the most recent train window; enabled only
     if the OOS evidence clears the thresholds.
"""
# ruff: noqa: E501
# The HTML report template below is the MT4 toolkit's template kept
# BYTE-VERBATIM (verification group G3: the port's report is byte-equal to
# the original crt_ev_optimizer.py's on the same corpus). The template's
# long lines are STRING CONTENT — wrapping them would change the report
# bytes and break that parity, so E501 is disabled for this file rather
# than fidelity.
from __future__ import annotations

import html
import itertools
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

from .config import CRTLabError
from .data import load
from .decay import DecayMonitor

SESS = {0: "OUT", 1: "NY", 2: "ASIA"}
VOL_NAMES = {1: "low", 2: "mid", 4: "high"}


# ----------------------------------------------------------------------------
# Outcome reconstruction
# ----------------------------------------------------------------------------
def grid_columns(df):
    rcols = [c for c in df.columns if c.startswith("b_")]
    hcols = [c for c in df.columns if c.startswith("c_")]
    rs = np.array([float(c[2:]) for c in rcols])
    hs = np.array([int(c[2:]) for c in hcols])
    return rcols, hcols, rs, hs


def outcomes(df, rcols, hcols, rs, hs):
    """Return array (rows, nR, nH) of net R results; NaN = not filled or not yet resolved."""
    n = len(df)
    filled = df["filled"].to_numpy() == 1
    ecost = df["entry_cost_r"].to_numpy(dtype=float)
    slip = df["exit_slip_r"].to_numpy(dtype=float)
    avail = df["bars_avail"].to_numpy(dtype=float)
    bsl = df["bars_sl"].to_numpy(dtype=float)
    bsl = np.where(bsl < 0, np.inf, bsl)
    br = df[rcols].to_numpy(dtype=float)
    br = np.where(br < 0, np.inf, br)
    cl = df[hcols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)

    out = np.full((n, len(rs), len(hs)), np.nan, dtype=np.float32)
    sl_r = -1.0 - ecost - slip
    for k, R in enumerate(rs):
        tp_r = R - ecost
        for h, H in enumerate(hs):
            sl_hit = (bsl <= br[:, k]) & (bsl <= H)
            tp_hit = (br[:, k] < bsl) & (br[:, k] <= H)
            time_ok = (~sl_hit) & (~tp_hit) & (avail >= H) & ~np.isnan(cl[:, h])
            r = np.full(n, np.nan)
            r[sl_hit] = sl_r[sl_hit]
            r[tp_hit] = tp_r[tp_hit]
            r[time_ok] = cl[time_ok, h] - ecost[time_ok]
            r[~filled] = np.nan
            out[:, k, h] = r
    return out


def vol_bit(p, lo, hi):
    b = np.full(len(p), 2, dtype=int)          # unknown -> mid
    b[(p >= 0) & (p < lo)] = 1
    b[(p >= lo) & (p < hi)] = 2
    b[p >= hi] = 4
    return b


# ----------------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------------
def nanstats(x):
    """x: (n, ...) -> n, mean, sd along axis 0 (NaN ignored)."""
    ok = ~np.isnan(x)
    n = ok.sum(axis=0)
    s = np.nansum(x, axis=0)
    s2 = np.nansum(x * x, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n > 0, s / np.maximum(n, 1), np.nan)
        var = np.where(n > 1, (s2 - n * mean * mean) / np.maximum(n - 1, 1), np.nan)
    sd = np.sqrt(np.clip(var, 0, None))
    return n, mean, sd


def series_stats(r):
    r = np.asarray(r, dtype=float)
    n = len(r)
    if n == 0:
        return dict(n=0, ev=np.nan, sd=np.nan, se=np.nan, t=np.nan, pf=np.nan,
                    win=np.nan, total=0.0, maxdd=0.0)
    ev = r.mean()
    sd = r.std(ddof=1) if n > 1 else np.nan
    se = sd / math.sqrt(n) if n > 1 and sd > 0 else np.nan
    gw = r[r > 0].sum()
    gl = -r[r <= 0].sum()
    eq = np.cumsum(r)
    dd = (np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq).max() if n else 0.0
    return dict(n=n, ev=ev, sd=sd, se=se, t=(ev / se if se and se > 0 else np.nan),
                pf=(gw / gl if gl > 0 else (np.inf if gw > 0 else np.nan)),
                win=(r > 0).mean(), total=r.sum(), maxdd=dd)


# ----------------------------------------------------------------------------
# Walk-forward
# ----------------------------------------------------------------------------
def make_folds(t0, t1, train_m, test_m, anchored):
    folds = []
    start = pd.Timestamp(t0).normalize().replace(day=1)
    end = pd.Timestamp(t1)
    s = start
    while True:
        tr0 = start if anchored else s
        tr1 = s + pd.DateOffset(months=train_m)
        te1 = tr1 + pd.DateOffset(months=test_m)
        if tr1 >= end:
            break
        te_end = min(te1, end + pd.Timedelta(seconds=1))
        if te1 > end and (te_end - tr1) < pd.Timedelta(days=28):
            break                                    # skip a stub test window (< 4 weeks)
        folds.append((tr0, tr1, te_end))
        if te1 > end:
            break
        s = s + pd.DateOffset(months=test_m)
    return folds


def filter_space(stage, model):
    if stage == 1:
        return [(0.0, 0, 7, 0)]
    sweeps = [0.0, 1.0, 0.75, 0.5]
    chops = [0, 1]
    vols = [7, 3, 6, 1, 2, 4]
    c3d = [0, 1] if model == "C3C" else [0]
    return list(itertools.product(sweeps, chops, vols, c3d))


def filter_mask(feat, f, ):
    max_sweep, chop, vmask, c3dir = f
    m = np.ones(len(feat["sweep"]), dtype=bool)
    if max_sweep > 0:
        m &= feat["sweep"] <= max_sweep
    if chop:
        m &= feat["chop"] == 1
    if vmask != 7:
        m &= (feat["vbit"] & vmask) > 0
    if c3dir:
        m &= feat["c3dir"] == 1
    return m


def best_config(blocks, idx_mask_fn, rs, hs, a):
    """blocks: list of (variant, filter, R_mat(n,nR,nH), mask(n)).
    idx_mask_fn(block) -> boolean row mask for the window. Returns best dict or None."""
    best = None
    for vb in blocks:
        rows = vb["mask"] & idx_mask_fn(vb)
        if rows.sum() < a.min_train_n:
            continue
        n, mean, sd = nanstats(vb["R"][rows])
        with np.errstate(invalid="ignore", divide="ignore"):
            lcb = mean - a.z * sd / np.sqrt(np.maximum(n, 1))
        lcb = np.where(n >= a.min_train_n, lcb, -np.inf)
        lcb = np.nan_to_num(lcb, nan=-np.inf)
        k, h = np.unravel_index(np.argmax(lcb), lcb.shape)
        if best is None or lcb[k, h] > best["lcb"]:
            best = dict(block=vb, k=int(k), h=int(h), lcb=float(lcb[k, h]), n=int(n[k, h]),
                        ev=float(mean[k, h]), sd=float(sd[k, h]), R=float(rs[k]), H=int(hs[h]))
    return best


def run(args):
    """The walk-forward. ``args`` carries the toolkit's argparse fields
    (csv/stage/train_months/.../out); returns the structured result for the
    artifact mint + Discord signal layers."""
    df, metas = load(args.csv)
    if args.start:
        df = df[df["c3_time"] >= pd.Timestamp(args.start)].reset_index(drop=True)
        print(f"using setups from {args.start} onward: {len(df):,} rows")
    if args.models:
        keep = [m.strip().upper() for m in args.models.split(",") if m.strip()]
        df = df[df["model"].isin(keep)].reset_index(drop=True)
        print(f"models {keep}: {len(df):,} rows")
        if df.empty:
            raise CRTLabError(
                "no rows for the requested models (was the research export run with them enabled?)")
    rcols, hcols, rs, hs = grid_columns(df)
    print(f"rows {len(df):,} | R grid {list(rs)} | holds {list(hs)}")
    R_all = outcomes(df, rcols, hcols, rs, hs)
    df["vbit"] = vol_bit(df["atr_pct"].to_numpy(dtype=float), args.vol_low, args.vol_high)

    t_min, t_max = df["c3_time"].min(), df["c3_time"].max()
    folds = make_folds(t_min, t_max, args.train_months, args.test_months, args.anchored)
    if not folds:
        raise CRTLabError("data span shorter than one train window; lower --train-months")
    print(f"data {t_min:%Y-%m-%d} -> {t_max:%Y-%m-%d} | {len(folds)} walk-forward folds")

    deploy_rows, fold_rows, report_buckets = [], [], []
    keys = ["symbol", "tf", "dir", "session", "model"]

    for key, g in df.groupby(keys, sort=True):
        sym, tf, d, sess, model = key
        variants = []
        for (eb, sb), gv in g.groupby(["entry_buf", "sl_buf"]):
            gv = gv.sort_values("c3_time")
            idx = gv.index.to_numpy()
            feat = dict(sweep=gv["sweep_atr"].to_numpy(float), chop=gv["chop_pass"].to_numpy(int),
                        vbit=gv["vbit"].to_numpy(int), c3dir=gv["c3_dir_ok"].to_numpy(int))
            t = gv["c3_time"].to_numpy()
            Rm = R_all[idx]
            for f in filter_space(args.stage, model):
                variants.append(dict(eb=float(eb), sb=float(sb), f=f, R=Rm, t=t, mask=filter_mask(feat, f)))
        n_configs = len(variants) * len(rs) * len(hs)
        n_setups = g[["c3_time"]].drop_duplicates().shape[0]

        oos_r, oos_t, gated_w, states, is_lcbs, chosen = [], [], [], [], [], []
        for fi, (tr0, tr1, te1) in enumerate(folds):
            tr0n, tr1n, te1n = np.datetime64(tr0), np.datetime64(tr1), np.datetime64(te1)
            best = best_config(variants, lambda vb: (vb["t"] >= tr0n) & (vb["t"] < tr1n), rs, hs, args)
            if best is None or best["lcb"] == -np.inf:
                fold_rows.append(dict(symbol=sym, tf=tf, dir=d, session=SESS.get(sess, sess), model=model, fold=fi,
                                      train_start=tr0.date(), train_end=tr1.date(), test_end=te1.date(),
                                      chosen="none (insufficient n)"))
                continue
            vb = best["block"]
            te_rows = vb["mask"] & (vb["t"] >= tr1n) & (vb["t"] < te1n)
            r = vb["R"][te_rows, best["k"], best["h"]]
            tt = vb["t"][te_rows]
            ok = ~np.isnan(r)
            r, tt = r[ok].astype(float), tt[ok]
            mon = DecayMonitor(best["lcb"] if args.baseline == "lcb" else best["ev"], best["sd"], args)
            w = []
            for x in r:
                st = mon.state                         # decision made before the trade
                w.append(1.0 if st == 0 else (args.caution_mult if st == 1 else 0.0))
                states.append(st)
                mon.update(x)
            oos_r.extend(r)
            oos_t.extend(tt)
            gated_w.extend(w)
            is_lcbs.append(best["lcb"])
            cfg = (vb["eb"], vb["sb"], best["R"], best["H"]) + tuple(vb["f"])
            chosen.append(cfg)
            fs = series_stats(r)
            fold_rows.append(dict(symbol=sym, tf=tf, dir=d, session=SESS.get(sess, sess), model=model, fold=fi,
                                  train_start=tr0.date(), train_end=tr1.date(), test_end=te1.date(),
                                  chosen=cfg_str(model, cfg), is_n=best["n"], is_ev=round(best["ev"], 4),
                                  is_lcb=round(best["lcb"], 4), oos_n=fs["n"], oos_ev=round(fs["ev"], 4) if fs["n"] else None,
                                  oos_total=round(fs["total"], 2)))

        oos = series_stats(oos_r)
        gw = np.array(gated_w)
        gated = np.array(oos_r) * gw if len(oos_r) else np.array([])
        g_total = gated.sum() if len(gated) else 0.0
        g_units = gw.sum() if len(gw) else 0.0
        folds_pos = np.mean([fr["oos_ev"] > 0 for fr in fold_rows
                             if fr.get("oos_ev") is not None and (fr["symbol"], fr["tf"], fr["dir"], fr["session"], fr["model"])
                             == (sym, tf, d, SESS.get(sess, sess), model)]) if chosen else np.nan
        stab = (max(chosen.count(c) for c in set(chosen)) / len(chosen)) if chosen else np.nan

        # Final selection on the most recent train window
        f0 = t_min if args.anchored else (t_max - pd.DateOffset(months=args.train_months))
        f0n, f1n = np.datetime64(f0), np.datetime64(t_max + pd.Timedelta(seconds=1))
        final = best_config(variants, lambda vb: (vb["t"] >= f0n) & (vb["t"] < f1n), rs, hs, args)

        enabled = bool(final is not None and final["lcb"] > -np.inf
                       and oos["n"] >= args.min_oos_n and oos["ev"] > args.min_oos_ev
                       and (oos["t"] if not np.isnan(oos["t"]) else -9) >= args.min_oos_t
                       and (final["lcb"] > 0 or not args.require_final_lcb))
        reason = []
        if final is None or final["lcb"] == -np.inf:
            reason.append("final window n too small")
        else:
            if args.require_final_lcb and final["lcb"] <= 0:
                reason.append("recent LCB <= 0")
        if oos["n"] < args.min_oos_n:
            reason.append(f"OOS n {oos['n']} < {args.min_oos_n}")
        elif oos["ev"] <= args.min_oos_ev:
            reason.append("OOS EV <= min")
        elif np.isnan(oos["t"]) or oos["t"] < args.min_oos_t:
            reason.append("OOS t < min")

        b = dict(symbol=sym, tf=tf, dir=d, session=SESS.get(sess, sess), session_id=int(sess), model=model,
                 n_setups=n_setups, n_configs=n_configs, oos=oos, gated_total=g_total, gated_units=g_units,
                 gated_ev=(g_total / g_units if g_units > 0 else np.nan), folds_pos=folds_pos, stab=stab,
                 is_lcb_avg=(np.mean(is_lcbs) if is_lcbs else np.nan), final=final, enabled=enabled,
                 reason="; ".join(reason) if not enabled else "", oos_r=np.array(oos_r), gated=gated,
                 states=np.array(states), off_share=(np.mean(np.array(states) == 2) if states else np.nan))
        report_buckets.append(b)

    # Pick the better model per symbol x tf x dir x session
    picked = {}
    for b in report_buckets:
        k = (b["symbol"], b["tf"], b["dir"], b["session_id"])
        score = (1 if b["enabled"] else 0,
                 b["oos"]["ev"] - args.z * (b["oos"]["se"] if not np.isnan(b["oos"]["se"]) else 9) if b["oos"]["n"] else -9)
        if k not in picked or score > picked[k][0]:
            picked[k] = (score, b)

    gen = datetime.now().strftime("%Y.%m.%d %H:%M")
    train_end = t_max.strftime("%Y.%m.%d %H:%M")
    for k, (score, b) in sorted(picked.items()):
        b["deployed"] = True
        fin = b["final"]
        if fin is None:
            continue
        vb = fin["block"]
        ms, chop, vm, c3d = vb["f"]
        deploy_rows.append(dict(symbol=b["symbol"], tf=b["tf"], dir=b["dir"], session=b["session_id"], model=b["model"],
                                entry_buf=vb["eb"], sl_buf=vb["sb"], r=fin["R"], hold=fin["H"], max_sweep=ms, chop=chop,
                                vol_mask=vm, c3_dir=c3d, enabled=int(b["enabled"]),
                                base_ev=round(b["oos"]["ev"], 4) if b["oos"]["n"] else 0.0,
                                base_sd=round(b["oos"]["sd"], 4) if b["oos"]["n"] > 1 and not np.isnan(b["oos"]["sd"]) else round(fin["sd"], 4),
                                oos_n=b["oos"]["n"], oos_t=round(b["oos"]["t"], 3) if not np.isnan(b["oos"]["t"]) else 0.0,
                                train_end=train_end, generated=gen))

    print("\nSETUP OPTION                 STATUS   OOS n   OOS EV   OOS SumR  Gated SumR  OFF%   reason")
    for b in sorted(report_buckets, key=lambda b: (b["symbol"], b["tf"], b["dir"], b["session"], b["model"])):
        if not b.get("deployed"):
            continue
        o = b["oos"]
        print(f"{b['symbol']:>7} {b['tf']:>3} {b['dir']:<4} {b['session']:<4} {b['model']:<4}  "
              f"{'ON ' if b['enabled'] else 'off'}   {o['n']:>6}  {f2(o['ev'],3,True):>7}  {f2(o['total'],1,True):>8}  "
              f"{f2(b['gated_total'],1,True):>9}  {f2(b['off_share']*100 if not np.isnan(b['off_share']) else None,0):>4}   {b['reason']}")
    print()
    os.makedirs(args.out, exist_ok=True)
    dep = pd.DataFrame(deploy_rows)
    if dep.empty:
        print("no setup options deployed (nothing cleared the pick — empty deploy set)")
    else:
        for sym, gsym in dep.groupby("symbol"):
            p = os.path.join(args.out, f"{args.prefix}{sym}.csv")
            gsym.to_csv(p, index=False, lineterminator="\r\n")   # MQL4 FILE_ANSI parity
            print(f"wrote {p}  ({int(gsym['enabled'].sum())}/{len(gsym)} setup options enabled)")
    pd.DataFrame(fold_rows).to_csv(os.path.join(args.out, "CRT_EVOPT_folds.csv"), index=False)
    write_report(os.path.join(args.out, "CRT_EVOPT_report.html"), args, report_buckets, folds, t_min, t_max, len(df), rs, hs)
    print(f"wrote {os.path.join(args.out, 'CRT_EVOPT_report.html')}")

    return dict(
        deploy_rows=deploy_rows, fold_rows=fold_rows, report_buckets=report_buckets,
        folds=[(str(tr0.date()), str(tr1.date()), str(te1.date())) for tr0, tr1, te1 in folds],
        t_min=str(t_min), t_max=str(t_max), n_rows=int(len(df)),
        r_grid=[float(r) for r in rs], holds=[int(h) for h in hs],
        train_end=train_end, generated=gen, metas=metas,
    )


def cfg_str(model, cfg):
    eb, sb, R, H, ms, chop, vm, c3d = cfg
    s = (f"eb{eb:.2f} " if model in ("STOP", "CONT") else "") + f"sb{sb:.2f} {R:g}R h{H}"
    if ms > 0:
        s += f" sweep<={ms:g}"
    if chop:
        s += " chop"
    if vm != 7:
        s += " vol=" + "+".join(VOL_NAMES[b] for b in (1, 2, 4) if vm & b)
    if c3d:
        s += " c3dir"
    return s


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------
def svg_curve(r, gated, w=260, h=60):
    if len(r) < 2:
        return ""
    a, b = np.cumsum(r), np.cumsum(gated)
    lo, hi = min(a.min(), b.min(), 0), max(a.max(), b.max(), 0)
    span = (hi - lo) or 1.0

    def pts(y):
        xs = np.linspace(2, w - 2, len(y))
        ys = h - 2 - (y - lo) / span * (h - 4)
        return " ".join(f"{x:.1f},{v:.1f}" for x, v in zip(xs, ys))

    zero = h - 2 - (0 - lo) / span * (h - 4)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<line x1="0" x2="{w}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="#ccc" stroke-dasharray="2,2"/>'
            f'<polyline fill="none" stroke="#9aa3ad" stroke-width="1.2" points="{pts(a)}"/>'
            f'<polyline fill="none" stroke="#1f6feb" stroke-width="1.6" points="{pts(b)}"/></svg>')


def f2(v, d=2, plus=False):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "-"
    return f"{v:+.{d}f}" if plus else f"{v:.{d}f}"


def write_report(path, a, buckets, folds, t0, t1, nrows, rs, hs):
    buckets = sorted(buckets, key=lambda b: (b["symbol"], b["tf"], b["dir"], b["session"], b["model"]))
    rows = []
    for b in buckets:
        o, fin = b["oos"], b["final"]
        status = "ENABLED" if b["enabled"] else "off"
        cls = "on" if b["enabled"] else "off"
        dep = "&#9733;" if b.get("deployed") else ""
        cfg = cfg_str(b["model"], (fin["block"]["eb"], fin["block"]["sb"], fin["R"], fin["H"]) + tuple(fin["block"]["f"])) if fin else "-"
        decay = (b["is_lcb_avg"] - o["ev"]) if o["n"] else np.nan
        rows.append(
            f'<tr class="{cls}"><td>{dep}</td><td>{b["symbol"]}</td><td>{b["tf"]}</td><td>{b["dir"]}</td><td>{b["session"]}</td>'
            f'<td>{b["model"]}</td><td><b>{status}</b><div class="why">{html.escape(b["reason"])}</div></td>'
            f'<td>{html.escape(cfg)}</td><td>{fin["n"] if fin else "-"}</td><td>{f2(fin["lcb"] if fin else None, 3, True)}</td>'
            f'<td>{o["n"]}</td><td>{f2(o["ev"], 3, True)}</td><td>{f2(o["t"])}</td><td>{f2(o["pf"])}</td>'
            f'<td>{f2(o["win"]*100 if o["n"] else None, 0)}</td><td>{f2(o["total"], 1, True)}</td><td>{f2(o["maxdd"], 1)}</td>'
            f'<td>{f2(b["folds_pos"]*100 if not np.isnan(b["folds_pos"]) else None, 0)}</td>'
            f'<td>{f2(b["stab"]*100 if not np.isnan(b["stab"]) else None, 0)}</td>'
            f'<td>{f2(decay, 3, True)}</td>'
            f'<td>{f2(b["gated_total"], 1, True)}</td><td>{f2(b["gated_ev"], 3, True)}</td>'
            f'<td>{f2(b["off_share"]*100 if not np.isnan(b["off_share"]) else None, 0)}</td>'
            f'<td>{svg_curve(b["oos_r"], b["gated"])}</td></tr>')

    tot_u = sum(b["oos"]["total"] for b in buckets if b.get("deployed"))
    tot_g = sum(b["gated_total"] for b in buckets if b.get("deployed"))
    n_en = sum(1 for b in buckets if b.get("deployed") and b["enabled"])
    n_dep = sum(1 for b in buckets if b.get("deployed"))
    fold_txt = ", ".join(f"{x[1]:%Y-%m}" for x in folds)

    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>CRT EV Optimizer report</title>
<style>
body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:24px;color:#1d2125;font-size:13px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:24px 0 8px}}
table{{border-collapse:collapse;width:100%}} th,td{{border-bottom:1px solid #e3e6ea;padding:4px 6px;text-align:right;vertical-align:middle;white-space:nowrap}}
th{{background:#f4f6f8;position:sticky;top:0;font-weight:600}} td:nth-child(-n+8){{text-align:left}}
tr.off td{{color:#7a828c}} tr.on td b{{color:#137333}} tr.off td b{{color:#a50e0e}} .why{{font-size:11px;color:#a50e0e;white-space:normal;max-width:180px}}
.kpi{{display:flex;gap:28px;margin:12px 0}} .kpi div{{background:#f4f6f8;padding:8px 14px;border-radius:6px}} .kpi b{{display:block;font-size:17px}}
.note{{color:#4a525b;max-width:980px;line-height:1.5}}
</style></head><body>
<h1>CRT EV Optimizer: walk-forward report (Stage {a.stage})</h1>
<div class="note">Data {t0:%Y-%m-%d} &rarr; {t1:%Y-%m-%d} | {nrows:,} variant rows | R grid {', '.join(f'{r:g}' for r in rs)} | holds {', '.join(str(h) for h in hs)} |
{len(folds)} folds ({'anchored' if a.anchored else 'rolling'} {a.train_months}m train / {a.test_months}m test; test windows start {fold_txt}) |
selection = max LCB (EV &minus; {a.z}&middot;SE), train n &ge; {a.min_train_n} | enable if OOS n &ge; {a.min_oos_n}, OOS EV &gt; {a.min_oos_ev}, OOS t &ge; {a.min_oos_t}{', recent LCB &gt; 0' if a.require_final_lcb else ''}.</div>
<div class="kpi"><div>Setup options deployed<b>{n_dep}</b></div><div>Enabled<b>{n_en}</b></div>
<div>OOS total, all deployed (R)<b>{tot_u:+.1f}</b></div><div>OOS total with decay gate (R)<b>{tot_g:+.1f}</b></div></div>
<h2>Setup options</h2>
<table><thead><tr><th>&#9733;</th><th>Sym</th><th>TF</th><th>Dir</th><th>Sess</th><th>Model</th><th>Status</th><th>Deployed config (recent window)</th>
<th>IS n</th><th>IS LCB</th><th>OOS n</th><th>OOS EV</th><th>OOS t</th><th>PF</th><th>Win%</th><th>OOS &Sigma;R</th><th>Max DD R</th>
<th>Folds +%</th><th>Param stab%</th><th>IS&rarr;OOS gap</th><th>Gated &Sigma;R</th><th>Gated EV</th><th>OFF%</th><th>OOS equity (grey = all, blue = gated)</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>How to read this</h2>
<div class="note">
<p><b>&#9733;</b> = the model (STOP or C3C) exported to the EA for that TF &times; direction &times; session. The other model is shown for comparison only.</p>
<p><b>OOS</b> columns use only trades from test windows the selection never saw. This is the honest estimate of the edge. <b>IS LCB</b> is from the most recent train window (what the EA deploys).</p>
<p><b>IS&rarr;OOS gap</b> = average in-sample LCB of the chosen configs minus OOS EV. A large positive gap means the search is fitting noise: widen min n, raise z, or search fewer settings.</p>
<p><b>Param stab%</b> = how often the same config was chosen across folds. Low stability with good OOS EV means the edge is real but the exact settings are not; prefer Stage 1.</p>
<p><b>Gated</b> = the EA's edge-decay monitor replayed on the OOS trades (CAUTION = {a.caution_mult:g}&times; size, OFF = skip). If gated &Sigma;R &ge; ungated &Sigma;R with a smaller drawdown, the gate is earning its keep on this option.</p>
<p>Setups overlap across TFs and can overlap in time within a bucket; the EA's one-position-per-symbol rule will skip some of them, so live totals will be lower than these sums.</p>
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
