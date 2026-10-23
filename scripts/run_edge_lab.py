#!/usr/bin/env python
"""run_edge_lab.py — one CLI for the whole regime_edge proof engine.

Usage (from the repo root):
    .venv/bin/python scripts/run_edge_lab.py [--symbols BTCUSD XAUUSD ...]
        [--data-dir data/regime_edge] [--out-dir worklog/kairos/reports/regime_edge]
        [--bars-limit N] [--seed 7]

Creates <out-dir>/<UTC runid>/ with edge_lab.json (THE artifact, <= 3MB, all
numbers finite), summary.md (headline verdicts), records.csv (per-slot audit
trail).  Everything seeded (default 7); runtime ~10-15 min for the 5-symbol
test subset, ~30-40 min for the full 14-symbol ACTIVE set (2026-10-21:
DEFAULT_SYMBOLS extended 5 → 14 per the Supabase symbol table; the 5-symbol
run 20261020_055039 remains the comparability baseline).

Long runs survive host restarts: pass --checkpoint-dir <dir> and every
computed symbol is pickled there; re-running the SAME command resumes from
the checkpoints (per-symbol compute is pure + seeded, so a resumed run is
byte-identical to an uninterrupted one).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from regime_edge import ENGINE  # noqa: E402
from regime_edge import barrier as rbar  # noqa: E402
from regime_edge import books as rbooks  # noqa: E402
from regime_edge import edge as redge  # noqa: E402
from regime_edge import features as rfeat  # noqa: E402
from regime_edge import hmm_ref as rhmm  # noqa: E402
from regime_edge import report as rreport  # noqa: E402
from regime_edge import tsfm as rtsfm  # noqa: E402
from regime_edge import values as rvalues  # noqa: E402
from regime_edge.brackets import slot_grid  # noqa: E402
from regime_edge.data import load_all  # noqa: E402

# Full ACTIVE set (14) from the Supabase symbol table (2026-10-21) — the
# original 5 were the test subset.  Cost per symbol via barrier.SYMBOL_CLASS
# → the repo's own ev_costs.cost_atr_by_class table (equity 0.06 / index 0.05
# added for the new names).
DEFAULT_SYMBOLS = list(rbar.ACTIVE_SYMBOLS)
HMM_NOTE = (
    "Cadence approximation: the live orchestrator refits both HMMs from scratch on every "
    "15-minute tick (rolling 200x1h bars); this repro treats one hourly bar as one tick and "
    "refits every 24 bars, re-predicting the label every bar from the most recent fit on the "
    "current trailing window (repo predict() semantics: fit-time standardization).  No EM "
    "warm-start / cross-tick label-continuity matching: each refit uses hmmlearn auto-init "
    "plus the rank-sort label map (regime_engine.py:1155-1156) — the repo's first-fit "
    "behavior and the exact behavior the 20261019 evaluation criticizes."
)


def _data_basis_note(data_dir: str) -> str:
    """State the ACTUAL data basis from ``SOURCE.json`` (written by the export).

    A hardcoded feed claim in the artifact is a lie the moment the export moves
    to another source — the engine must report what it was actually fed.  The
    2026-10-21 baseline hardcoded "yfinance proxies" while the CSVs were being
    regenerated from TDVA; this replaces that with the marker's own record.
    """
    path = os.path.join(data_dir, "SOURCE.json")
    if not os.path.exists(path):
        return ("Data basis UNKNOWN — data/regime_edge/SOURCE.json is missing; run "
                "scripts/export_edge_lab_data.py first.")
    with open(path, encoding="utf-8") as fh:
        prov = json.load(fh)
    src = prov.get("source", "unknown")
    fetched = (prov.get("fetched_at") or "")[:19].replace("T", " ")
    bars = prov.get("bars", {}) or {}
    total = sum(bars.values())
    if src == "tdva":
        detail = ("TDVA/TradingView — the live sweep's own feed, symbols resolved via "
                  "nt_symbol.tradingview_symbol. XAUEUR/XAUAUD are REAL symbols under "
                  "TDVA, so no GC=F/FX ratio construction is involved.")
    else:
        detail = ("yfinance proxies (LEGACY reproduction path) — GC=F/EURUSD=X and "
                  "GC=F/AUDUSD=X constructed ratios for XAUEUR/XAUAUD.")
    return (
        f"Data basis: {src} ({len(bars)} symbols, {total:,} bars, fetched {fetched} UTC). "
        f"{detail} Short series get the engine's adaptive walk-forward schedule "
        f"(first_fit=max(1200, n//2), step=500) with a per-symbol note."
    )


def run_symbol(sym, bars, geo_cur, seed):
    t0 = time.time()
    high, low, close = bars["high"], bars["low"], bars["close"]
    n = bars["n"]
    feats = rfeat.compute_all(high, low, close)
    vals = rvalues.regime_values(feats)
    t_feat = time.time() - t0

    # ── HMM repro (the slow part)
    t0 = time.time()
    hmm_out = rhmm.run_hmm_repro(close, refit_every=24, window=200)
    hmm_m = rhmm.hmm_metrics(close, hmm_out, feats["er"], horizon=24)
    t_hmm = time.time() - t0

    t0 = time.time()
    slots = slot_grid(n, warmup=720, step=24)
    trades_cur = redge.build_trades(slots, feats, vals, geo_cur, high, low, close, feats["atr"])
    surface = redge.edge_surface(trades_cur)
    spearman = redge.spearman_tests(trades_cur, n_perm=2000, seed=seed)
    bucket_cal = redge.repo_bucket_calibration(trades_cur, geo_cur["tp_atr"])
    wf = redge.walkforward_cells(trades_cur, n, first_fit=3000, step=1000)
    calib = redge.calibration_metrics(trades_cur, wf)
    t_edge = time.time() - t0

    return {"feats": feats, "vals": vals, "hmm_out": hmm_out, "hmm_m": hmm_m,
            "slots": slots, "trades_cur": trades_cur, "surface": surface,
            "spearman": spearman, "bucket_cal": bucket_cal, "wf": wf, "calib": calib,
            "timings": {"features": t_feat, "hmm": t_hmm, "edge": t_edge}}


def compute_symbol(sym, bars, geo_cur, cost, seed):
    """The full per-symbol pipeline (pure: same inputs -> same outputs, seeded).
    Returns (S_sym, records, sym_notes) — everything downstream needs, so a
    run can be checkpointed/resumed per symbol (2026-10-21: the 14-symbol
    ACTIVE set needs ~30 min wall; hosts that kill long sessions resume it)."""
    sym_notes = []
    res = run_symbol(sym, bars, geo_cur, seed)
    feats, vals = res["feats"], res["vals"]
    hmm_m = res["hmm_m"]
    print(f"[edge-lab] {sym}: features {res['timings']['features']:.1f}s, "
          f"HMM {res['timings']['hmm']:.1f}s ({res['hmm_out']['n_fits']} refits), "
          f"edge {res['timings']['edge']:.1f}s")
    print(f"[edge-lab] {sym}: HMM acc={hmm_m['acc']:.4f} majority={hmm_m['majority']:.4f} "
          f"skill={hmm_m['skill']:+.4f} (n={hmm_m['n_decided']})")

    # ── corrected geometry (physics solver, train-only drift)
    slots = res["slots"]
    split = max(1, int(0.70 * len(slots)))
    train_slots = slots[:split]
    regime = vals["regime"]
    er_s = feats["er_signed"]
    tr_slots, tr_dirs = [], []
    for i in train_slots:
        if regime[i] in ("trend_up", "trend_down") and np.isfinite(er_s[i]) and er_s[i] != 0:
            tr_slots.append(i)
            tr_dirs.append(1 if er_s[i] > 0 else -1)
    mu_hat, n_trend = rbar.shrunk_trend_drift(bars["close"], feats["atr"], tr_slots, tr_dirs)
    print(f"[edge-lab] {sym}: mu_hat={mu_hat:.5f} ATR/bar (n_trend={n_trend} train TREND slots)")
    t0 = time.time()
    solved = rbar.solve_corrected_geometry(mu_hat, cost, seed=seed)
    geo_cor = {"tp_atr": solved["tp_atr"], "sl_atr": solved["sl_atr"], "expiry": solved["expiry"]}
    print(f"[edge-lab] {sym}: corrected geometry tp={geo_cor['tp_atr']} sl={geo_cor['sl_atr']} "
          f"exp={geo_cor['expiry']} (feasible cells={solved['n_feasible']}, "
          f"ev={solved['ev']:+.3f}) [{time.time() - t0:.1f}s]")

    # barrier report: current & corrected under mu=0 and mu_hat
    cur_null = rbar.geometry_probs(geo_cur["tp_atr"], geo_cur["sl_atr"], geo_cur["expiry"], 0.0, seed=seed)
    cur_drift = rbar.geometry_probs(geo_cur["tp_atr"], geo_cur["sl_atr"], geo_cur["expiry"], mu_hat, seed=seed)
    cor_null = rbar.geometry_probs(geo_cor["tp_atr"], geo_cor["sl_atr"], geo_cor["expiry"], 0.0, seed=seed)
    cor_drift = rbar.geometry_probs(geo_cor["tp_atr"], geo_cor["sl_atr"], geo_cor["expiry"], mu_hat, seed=seed)
    barrier_sym = {
        "current": {"tp_atr": geo_cur["tp_atr"], "sl_atr": geo_cur["sl_atr"], "expiry": geo_cur["expiry"],
                    "pi_tp": round(cur_null[0], 4), "pi_sl": round(cur_null[1], 4),
                    "pi_e": round(cur_null[2], 4),
                    "ev_null": round(rbar.ev_r(cur_null[0], cur_null[1], geo_cur["tp_atr"], geo_cur["sl_atr"], cost), 4),
                    "ev_drift": round(rbar.ev_r(cur_drift[0], cur_drift[1], geo_cur["tp_atr"], geo_cur["sl_atr"], cost), 4)},
        "corrected": {"tp_atr": geo_cor["tp_atr"], "sl_atr": geo_cor["sl_atr"], "expiry": geo_cor["expiry"],
                      "pi_tp": round(cor_null[0], 4), "pi_sl": round(cor_null[1], 4),
                      "pi_e": round(cor_null[2], 4),
                      "ev_null": round(rbar.ev_r(cor_null[0], cor_null[1], geo_cor["tp_atr"], geo_cor["sl_atr"], cost), 4),
                      "ev_drift": round(rbar.ev_r(cor_drift[0], cor_drift[1], geo_cor["tp_atr"], geo_cor["sl_atr"], cost), 4)},
    }
    print(f"[edge-lab] {sym}: barrier current pi_sl={barrier_sym['current']['pi_sl']:.3f} "
          f"ev_null={barrier_sym['current']['ev_null']:+.3f} | corrected pi_sl={barrier_sym['corrected']['pi_sl']:.3f} "
          f"pi_e={barrier_sym['corrected']['pi_e']:.3f} ev_null={barrier_sym['corrected']['ev_null']:+.3f} "
          f"ev_drift={barrier_sym['corrected']['ev_drift']:+.3f}")

    # ── walk-forward cells under corrected geometry (publish gate)
    trades_cor = redge.build_trades(slots, feats, vals, geo_cor, bars["high"], bars["low"],
                                    bars["close"], feats["atr"])
    wf_cor = redge.walkforward_cells(trades_cor, bars["n"], first_fit=3000, step=1000)
    if wf_cor["adaptive_note"]:
        sym_notes.append(f"{sym}: {wf_cor['adaptive_note']}")

    # ── books
    t0 = time.time()
    books_res = rbooks.run_books(sym, {"high": bars["high"], "low": bars["low"],
                                       "close": bars["close"], "atr": feats["atr"], "n": bars["n"]},
                                 feats, vals, res["hmm_out"], geo_cur, geo_cor,
                                 wf_cor["preds"], cost, seed=seed)
    print(f"[edge-lab] {sym}: books [{time.time() - t0:.1f}s] kairos "
          f"n={books_res['books']['kairos']['n']} meanR={books_res['books']['kairos']['mean_r']:+.4f} | "
          f"corrected n={books_res['books']['corrected']['n']} "
          f"meanR={books_res['books']['corrected']['mean_r']:+.4f} (tau*={books_res['tau_star']}) | "
          f"null n={books_res['books']['null']['n']} meanR={books_res['books']['null']['mean_r']:+.4f}")
    gp = books_res["gate_stats"]
    print(f"[edge-lab] {sym}: kairos gates conf_pass={gp['kairos']['conf_pass']}/"
          f"{gp['kairos']['n_slots']} ev_pass={gp['kairos']['ev_pass']} "
          f"(base p_win={gp['base']['p_win']}, RR={gp['rr_current']})")

    # extra numbers for verdicts/summary
    regime_tail = []
    ts = bars["ts"]
    n = bars["n"]
    for i in range(max(0, n - 1500), n):
        regime_tail.append({
            "t": pd.Timestamp(ts[i]).isoformat(),
            "close": round(float(bars["close"][i]), 4),
            "trend": round(float(er_s[i]) if np.isfinite(er_s[i]) else 0.0, 4),
            "chop": round(float(feats["chop"][i]) if np.isfinite(feats["chop"][i]) else 0.0, 4),
            "vol": round(float(feats["vol_pct"][i]) if np.isfinite(feats["vol_pct"][i]) else 0.0, 4),
            "regime": regime[i],
        })
    ready = np.arange(720, n)
    abstain_rate = float(np.mean([regime[i] == "neutral" for i in ready]))
    hmm_decided_frac = float(np.mean([res["hmm_out"]["direction"][i] != 0 for i in ready]))
    best_sp = max(res["spearman"].items(), key=lambda kv: abs(kv[1]["rho"]))
    chop_buckets = res["surface"]["chop"]
    chop_spread = (chop_buckets[-1]["mean_r"] - chop_buckets[0]["mean_r"]) if chop_buckets else 0.0

    S_sym = {
        "n": n,
        "n_labeled": int(n - 199),
        "slots": slots,
        "hmm": hmm_m,
        "hmm_note": HMM_NOTE,
        "hmm_decided_frac": hmm_decided_frac,
        "abstain_rate_neutral": abstain_rate,
        "regime_tail": regime_tail,
        "surface": res["surface"],
        "spearman": res["spearman"],
        "best_spearman": {"feature": best_sp[0], "rho": best_sp[1]["rho"], "p": best_sp[1]["p"]},
        "chop_spread": chop_spread,
        "bucket_calibration": res["bucket_cal"],
        "calibration": res["calib"],
        "barrier": barrier_sym,
        "mu_hat": mu_hat,
        "cost_atr": cost,
        "books": books_res["books"],
        "gate": books_res["gate_stats"],
        "dm": books_res["dm"],
        "tau_star": books_res["tau_star"],
        "_series": books_res["_series"],
        "_trades": books_res["_trades"],
        # checkpoint-only fields (not consumed by report.py; kept so a resumed
        # run rebuilds records.csv / meta notes byte-identically)
        "records": books_res["records"],
        "_notes": sym_notes,
    }
    return S_sym, books_res["records"], sym_notes


def main(argv=None):
    p = argparse.ArgumentParser(description="regime_edge proof engine")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data", "regime_edge"))
    p.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "worklog", "kairos", "reports", "regime_edge"))
    p.add_argument("--bars-limit", type=int, default=None, help="smoke mode: first N bars")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--checkpoint-dir", default=None,
                   help="per-symbol pickle checkpoints: computed symbols are saved and "
                        "re-used across invocations (deterministic, seeded). Re-run the "
                        "same command to resume an interrupted 14-symbol run.")
    p.add_argument("--recompute", action="store_true",
                   help="ignore existing checkpoints for the listed symbols")
    args = p.parse_args(argv)

    t_start = time.time()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(args.out_dir, run_id)
    os.makedirs(out_dir, exist_ok=True)

    data = load_all(args.data_dir, args.symbols, bars_limit=args.bars_limit)
    geo = rbar.load_current_geometry(REPO_ROOT, args.symbols)
    print(f"[edge-lab] geometry source: {geo['source']['file']} lines {geo['source']['lines']}, "
          f"EXPIRY_BARS={geo['source']['expiry_bars']}")
    for s in args.symbols:
        g = geo["geometry"][s]
        print(f"[edge-lab]   {s}: tp={g['tp_atr']} sl={g['sl_atr']} exp={g['expiry']} "
              f"(band tp {g['band']['tp']}, sl {g['band']['sl']}, key={g['yaml_key']})")

    crosscheck = rbar.crosscheck_repo()
    print("[edge-lab] barrier crosscheck vs repo barrier_null:",
          [(c["case"], c["repo_pi"], c["our_mc_pi"], c["abs_diff"]) for c in crosscheck])

    tsfm_obj = rtsfm.reframe(REPO_ROOT)
    print(f"[edge-lab] tsfm reframe: har>{tsfm_obj['vol_backtest']['har_beats_naive_holm_cells']} "
          f"naive; chronos>har sig cells={tsfm_obj['vol_backtest']['chronos_beats_har_sig_cells']}; "
          f"recalib accepted {tsfm_obj['recalib']['accepted']}")

    S = {}
    all_records = []
    notes = [
        HMM_NOTE,
        "Symbol set 2026-10-21: the FULL ACTIVE list (14 of 34 per the Supabase "
        "symbol table) — extends the 5-symbol test run 20261020_055039; per-symbol "
        "costs come from the repo's own ev_costs.cost_atr_by_class (forex 0.04 / "
        "metals 0.06 / crypto 0.10 / equity 0.06 / index 0.05). US500/US30USD are "
        "priced as INDEX, and migration 217 has since moved their nt_symbol."
        "asset_class from 'stocks' to 'indices' (XAU*/XAG* 'commodities' -> "
        "'metals'), so the registry and this engine now agree.",
        _data_basis_note(args.data_dir),
        "kairos book: EV gate priced on the shipped ATR bracket in R units (the live gate prices "
        "the brick payoff x ATR probability — W-C1); p_win is the full-sample measured-table "
        "approximation (repo behavior).",
        "MC first passage: sub-stepped near-continuous monitoring (sub=16/bar; repo test "
        "convention tests/test_barrier_null.py:111-140); same-sub-step double touches count as "
        "neither (repo convention).",
        "mu_hat interpretation: direction-aligned TREND-regime drift (mean of (c[i+24]-c[i])/atr[i] "
        "x sign(er_signed) over TREND-labeled train slots, shrunk n/(n+100)) — the drift the "
        "sign(er_signed) book harvests; equals the literal signed mean when all TREND slots agree.",
        "Entry basis: stale close[i] kept faithfully (repo policy, W-G1) for the head-to-head proof.",
        "MC monitoring convention: the spec's 'hourly steps' MC carries a +0.05 overshoot bias vs "
        "the continuous-monitoring analytic (measured); all reported probabilities therefore use "
        "sub-stepped monitoring at 64 sub-steps/bar (bias ~+0.005, within the 0.01 large-T validation "
        "tolerance), and the corrected-geometry solver grid runs at 16 sub-steps/bar with a 0.01 "
        "margin on the pi_SL cap so reported numbers stay under the published 0.62 cap.",
        "Corrected-geometry constraints are enforced UNDER THE DRIFTLESS NULL (pi_SL at the longest "
        "expiry, pi_E at the chosen expiry) so the corrected bracket is never an SL-farm by physics "
        "even if the drift estimate is wrong; EV is maximised under the drifted MC.",
    ]
    for sym in args.symbols:
        bars = data[sym]
        geo_cur = geo["geometry"][sym]
        cost = rbar.COST_BY_CLASS[rbar.SYMBOL_CLASS[sym]]
        ckpt = None
        if args.checkpoint_dir:
            os.makedirs(args.checkpoint_dir, exist_ok=True)
            ckpt = os.path.join(args.checkpoint_dir, f"{sym}.pkl")
        if ckpt and os.path.exists(ckpt) and not args.recompute:
            import pickle
            with open(ckpt, "rb") as fh:
                S[sym] = pickle.load(fh)
            all_records.extend(S[sym].get("records", []))
            notes.extend(S[sym].get("_notes", []))
            print(f"\n[edge-lab] === {sym} ({S[sym]['n']} bars) — loaded from checkpoint ===")
            print(f"[edge-lab] {sym}: HMM skill={S[sym]['hmm']['skill']:+.4f}, "
                  f"kairos n={S[sym]['books']['kairos']['n']} "
                  f"meanR={S[sym]['books']['kairos']['mean_r']:+.4f}")
            continue
        print(f"\n[edge-lab] === {sym} ({bars['n']} bars) ===")
        S[sym], recs, sym_notes = compute_symbol(sym, bars, geo_cur, cost, args.seed)
        all_records.extend(recs)
        notes.extend(sym_notes)
        if ckpt:
            import pickle
            with open(ckpt, "wb") as fh:
                pickle.dump(S[sym], fh, protocol=pickle.HIGHEST_PROTOCOL)
            print(f"[edge-lab] {sym}: checkpointed -> {ckpt}")

    # ── pooled books + DM, then Holm across the 2×n_symbols per-symbol tests
    pooled = rbooks.pooled_books([S[s] for s in args.symbols])
    raw_ps = []
    for s in args.symbols:
        raw_ps.extend([S[s]["dm"]["kc_p_raw"], S[s]["dm"]["cn_p_raw"]])
    holm = rbooks.run_holm(raw_ps)
    i = 0
    for s in args.symbols:
        S[s]["dm"]["kc_p_holm"] = round(float(holm[i]), 4)
        S[s]["dm"]["cn_p_holm"] = round(float(holm[i + 1]), 4)
        i += 2
    print(f"\n[edge-lab] pooled DM: kc_p={pooled['kc_p']} (mld={pooled['kc_mean_loss_diff']}), "
          f"cn_p={pooled['cn_p']} (mld={pooled['cn_mean_loss_diff']})")

    # ── meta + artifact
    period_start = min(str(pd.Timestamp(data[s]["ts"][0]).isoformat()) for s in args.symbols)
    period_end = max(str(pd.Timestamp(data[s]["ts"][data[s]["n"] - 1]).isoformat()) for s in args.symbols)
    meta = {
            "run_id": run_id,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "engine": ENGINE,
            "symbols": args.symbols,
            "period_start": period_start,
            "period_end": period_end,
            "bars_per_symbol": {s: int(data[s]["n"]) for s in args.symbols},
            "band_source": {"file": geo["source"]["file"], "lines": geo["source"]["lines"]},
            "approximation_notes": notes,
            "fix_manifest_note": "fixes: [] — filled by the Task 40-b coordinator from the repo-side implementation.",
        }
    if args.bars_limit:
        meta["approximation_notes"].append(
            f"SMOKE RUN: --bars-limit {args.bars_limit} (first {args.bars_limit} bars per symbol)")
    doc = rreport.build_edge_lab(meta, S, pooled, crosscheck, tsfm_obj)
    json_path = os.path.join(out_dir, "edge_lab.json")
    doc = rreport.write_json(doc, json_path)
    size = os.path.getsize(json_path) / 1e6
    print(f"[edge-lab] wrote {json_path} ({size:.2f} MB)")

    summary = rreport.build_summary(doc)
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary)
    df = pd.DataFrame(all_records)
    df.to_csv(os.path.join(out_dir, "records.csv"), index=False)
    print(f"[edge-lab] wrote summary.md and records.csv ({len(df)} rows) in {out_dir}")
    print(f"[edge-lab] total wall: {time.time() - t_start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
