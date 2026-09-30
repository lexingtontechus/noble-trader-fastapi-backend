"""Diagnostic on vol_regime cells: unconstrained EV + loose OOS + legacy TP."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS, walk
from vol_atr_solver import (
    COST, MIN_DEC, build_paths_meta, eval_cell, load_signals, tercile_masks,
)

TARGETS = ["BTCUSD", "XAUUSD"]


def best_unconstrained(paths, tp_max=20.0, sl_max=12.0):
    """Max EV ignoring wr band / both-halves / expiry. Diagnostic only."""
    best = None
    for tp in np.round(np.arange(0.25, tp_max + 0.01, 0.25), 2):
        for sl in np.round(np.arange(0.25, sl_max + 0.01, 0.25), 2):
            if tp / sl < 1.0:
                continue
            pnl, exp = walk(paths, float(tp), float(sl))
            ev = float((pnl - COST).mean())
            wr_dec = (~exp) & (pnl > 0)
            sl_dec = (~exp) & (pnl <= 0)
            dec = int(wr_dec.sum() + sl_dec.sum())
            wr = float(wr_dec.sum() / dec) if dec else float("nan")
            sc = {"tp": float(tp), "sl": float(sl), "ev": ev, "wr": wr, "e": float(exp.mean()), "n": len(paths), "dec": dec}
            if best is None or ev > best["ev"]:
                best = sc
    return best


def eval_fixed(paths, tp, sl, min_dec=MIN_DEC):
    pnl, exp = walk(paths, float(tp), float(sl))
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    dec = int(tp_hit.sum() + sl_hit.sum())
    if dec < min_dec:
        return {"n": len(paths), "dec": dec, "ev": float((pnl - COST).mean()), "wr": np.nan, "e": float(exp.mean())}
    mid = len(paths) // 2
    net = pnl - COST
    return {
        "n": len(paths), "dec": dec, "ev": float(net.mean()),
        "wr": float(tp_hit.sum() / dec),
        "e": float(exp.mean()),
        "ev_h1": float(net[:mid].mean()) if mid else np.nan,
        "ev_h2": float(net[mid:].mean()) if mid else np.nan,
    }


async def main():
    peers = {p["symbol"]: p for p in await fetch_active_symbols() if p.get("symbol")}
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=45)
    out = ROOT / "worklog" / "kairos" / "reports" / "20260930_vol_regime_diag.md"
    lines = ["# Vol-regime diagnostics (unconstrained / legacy TP / loose OOS)\n"]

    cases = {
        "BTCUSD": ["post_absmax", "post_range", "vol_ratio", "disp_atr"],
        "XAUUSD": ["vol_ratio", "post_range", "range_atr"],
    }
    legacy = {"BTCUSD": (0.95, 0.15), "XAUUSD": (0.91, 0.71)}

    for sym in TARGETS:
        p = peers[sym]
        sig = await load_signals([sym], since, until)
        bars = {}
        await fetch_tradingview_batch(
            [{"symbol": sym, "tradingview_symbol": p["tradingview_symbol"]}],
            hours=24 * 30, interval="1h", ohlcv_out=bars,
        )
        bars = {s: [b for b in rows if b.get("t") and b.get("c")] for s, rows in bars.items()}
        paths, meta = build_paths_meta(sig, bars)
        print(f"\n=== {sym} n={len(meta)} ===", flush=True)
        lines.append(f"\n## {sym} n={len(meta)}\n")

        lt, ls = legacy[sym]
        for feat in cases[sym]:
            vals = np.array([m[feat] for m in meta], dtype="float64")
            masks = tercile_masks(vals)
            if not masks:
                continue
            lines.append(f"\n### `{feat}`\n")
            lines.append("| cell | bestEV TP/SL | EV | wr | e | n | legacy TP/SL EV | wr |\n|---|---|---:|---:|---:|---:|---|---:|\n")
            for name in ("low", "mid", "high"):
                m = masks[name]
                sub = paths[m]
                if len(sub) < 30:
                    lines.append(f"| {name} | thin | | | | {len(sub)} | | |\n")
                    continue
                best = best_unconstrained(sub)
                leg = eval_fixed(sub, lt, ls, min_dec=1)
                row = (
                    f"| {name} | {best['tp']:.2f}/{best['sl']:.2f} | {best['ev']:+.3f} | "
                    f"{best['wr']:.3f} | {best['e']:.3f} | {best['n']} | "
                    f"{leg['ev']:+.3f} | {leg['wr']:.3f} |"
                )
                print(row, flush=True)
                lines.append(row + "\n")

            # loose OOS on high cell unconstrained best
            if "high" in masks:
                m = masks["high"]
                idx = np.where(m)[0]
                if len(idx) >= 60:
                    best = best_unconstrained(paths[m])
                    cut = int(len(idx) * 0.7)
                    oos = eval_fixed(paths[idx[cut:]], best["tp"], best["sl"], min_dec=10)
                    is_e = eval_fixed(paths[idx[:cut]], best["tp"], best["sl"], min_dec=10)
                    lines.append(
                        f"\nhigh unconstrained {best['tp']:.2f}/{best['sl']:.2f} → "
                        f"IS EV {is_e['ev']:+.3f} wr {is_e['wr']:.3f} n={is_e['n']}; "
                        f"OOS EV {oos['ev']:+.3f} wr {oos['wr']:.3f} n={oos['n']}\n"
                    )
                    print(f"  OOS high: IS {is_e} OOS {oos}", flush=True)

        # XAU vol_ratio high: constrained candidate OOS loose
        if sym == "XAUUSD":
            vals = np.array([m["vol_ratio"] for m in meta], dtype="float64")
            masks = tercile_masks(vals)
            m = masks["high"]
            idx = np.where(m)[0]
            cut = int(len(idx) * 0.7)
            for tp, sl, tag in ((1.33, 0.20, "constrained"), (1.33, 0.20, "same")):
                is_e = eval_fixed(paths[idx[:cut]], tp, sl, min_dec=10)
                oos = eval_fixed(paths[idx[cut:]], tp, sl, min_dec=10)
                lines.append(
                    f"\nXAU vol_ratio.high {tag} {tp}/{sl} → "
                    f"IS {is_e['ev']:+.3f}/{is_e['wr']:.3f}/n{is_e['n']} · "
                    f"OOS {oos['ev']:+.3f}/{oos['wr']:.3f}/n{oos['n']}\n"
                )
                print(f"  XAU gate OOS: {is_e} {oos}", flush=True)
                break

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
