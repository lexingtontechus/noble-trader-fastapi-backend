"""
Monthly ATR re-optimum — propose by_symbol clamps; never auto-write YAML.
══════════════════════════════════════════════════════════════════════════

worklog/kairos/20260930_MONTHLY_ATR_REOPT.md

Heavy sweep is the wrong clock for rediscovery: 10d window, fixed YAML
grid, no vol split, no OOS. This job is the research clock:

  1. 45d free lattice (0.25 → 0.05 → 0.01), wr∈[0.40,0.60], exp≤0.20,
     cost 0.1, both-halves EV>0 (same bar as vol_atr_solver).
  2. Optional vol_ratio terciles (live-gateable entry feature).
  3. Compare to current ``dynamic_atr.by_symbol`` — flag drift.
  4. Write report + nt_roadmap heartbeat. YAML stays operator-owned.

Cadence: APScheduler monthly, 1st 03:30 UTC (orchestrator).
Flag: ``atr_reopt_enabled`` (simple on/off, proposal-only).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS, build_paths

logger = logging.getLogger("regime.sweep.atr_reopt")

COST = 0.1
WR_LO, WR_HI = 0.40, 0.60
EXP_MAX = 0.20
MIN_PATHS = 80
MIN_DEC = 40
LOOKBACK_DAYS = 45
FOREX = ("AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY")
# Material drift bar: propose when new TP/SL falls outside current clamp
# or sits >15% away from the published pick.
DRIFT_REL = 0.15


def eval_cell(paths: np.ndarray, tp: float, sl: float, cost: float = COST) -> Optional[dict[str, Any]]:
    from regime_platform.sweep_orchestrator.tiered_atr import walk

    if paths is None or len(paths) == 0:
        return None
    pnl, exp = walk(paths, float(tp), float(sl))
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    dec = int(tp_hit.sum() + sl_hit.sum())
    if dec < MIN_DEC:
        return None
    wr = float(tp_hit.sum() / dec)
    net = pnl - cost
    mid = len(paths) // 2
    return {
        "tp": float(tp),
        "sl": float(sl),
        "wr": wr,
        "exp": float(exp.mean()),
        "ev": float(net.mean()),
        "ev_h1": float(net[:mid].mean()) if mid else float("nan"),
        "ev_h2": float(net[mid:].mean()) if mid else float("nan"),
        "n": int(paths.shape[0]),
        "dec": dec,
    }


def _ok(sc: Optional[dict[str, Any]]) -> bool:
    if not sc:
        return False
    if sc["exp"] > EXP_MAX or not (WR_LO <= sc["wr"] <= WR_HI):
        return False
    return bool(sc["ev_h1"] > 0 and sc["ev_h2"] > 0)


def solve_free(paths: np.ndarray) -> Optional[dict[str, Any]]:
    """Coarse 0.25 → 0.05 → 0.01. Both halves EV>0. Same bar as research."""
    if paths is None or len(paths) < MIN_PATHS:
        return None

    def scan(tps, sls):
        best = None
        for tp in tps:
            for sl in sls:
                if tp / sl < 1.0:
                    continue
                sc = eval_cell(paths, tp, sl)
                if _ok(sc) and (best is None or sc["ev"] > best["ev"]):
                    best = sc
        return best

    best = scan(np.round(np.arange(0.25, 16.01, 0.25), 2), np.round(np.arange(0.25, 10.01, 0.25), 2))
    if best is None:
        return None
    best = scan(
        np.round(np.arange(max(0.20, best["tp"] - 0.30), best["tp"] + 0.30, 0.05), 2),
        np.round(np.arange(max(0.20, best["sl"] - 0.30), best["sl"] + 0.30, 0.05), 2),
    ) or best
    best = scan(
        np.round(np.arange(max(0.15, best["tp"] - 0.08), best["tp"] + 0.08, 0.01), 2),
        np.round(np.arange(max(0.15, best["sl"] - 0.08), best["sl"] + 0.08, 0.01), 2),
    ) or best
    return best


def _rel_drift(new: float, old: float) -> float:
    if old is None or old == 0:
        return 1.0
    return abs(new - old) / abs(old)


def compare_to_current(symbol: str, sc: dict[str, Any] | None, asset_class: str | None) -> dict[str, Any]:
    """Flag material drift vs dynamic_atr.by_symbol (operator-owned YAML)."""
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    cfg = get_dynamic_atr_config(asset_class, symbol)
    tps = list(cfg.get("tp_atr") or [])
    sls = list(cfg.get("sl_atr") or [])
    cur_tp = float(tps[len(tps) // 2]) if tps else None
    cur_sl = float(sls[len(sls) // 2]) if sls else None
    out = {
        "current_tp": cur_tp,
        "current_sl": cur_sl,
        "tp_atr_min": cfg.get("tp_atr_min"),
        "tp_atr_max": cfg.get("tp_atr_max"),
        "sl_atr_min": cfg.get("sl_atr_min"),
        "sl_atr_max": cfg.get("sl_atr_max"),
        "vol_ratio_max": cfg.get("vol_ratio_max"),
        "vol_ratio_min": cfg.get("vol_ratio_min"),
        "drift": False,
        "reasons": [],
    }
    if not sc:
        out["reasons"].append("no +EV cell in free lattice")
        return out
    if cur_tp is not None and cur_sl is not None:
        if _rel_drift(sc["tp"], cur_tp) > DRIFT_REL or _rel_drift(sc["sl"], cur_sl) > DRIFT_REL:
            out["drift"] = True
            out["reasons"].append(
                f"new {sc['tp']:.2f}/{sc['sl']:.2f} vs current {cur_tp:.2f}/{cur_sl:.2f} (>15%)"
            )
    tmin, tmax = out["tp_atr_min"], out["tp_atr_max"]
    smin, smax = out["sl_atr_min"], out["sl_atr_max"]
    if tmin is not None and sc["tp"] < float(tmin):
        out["drift"] = True
        out["reasons"].append(f"tp {sc['tp']:.2f} below clamp {tmin}")
    if tmax is not None and sc["tp"] > float(tmax):
        out["drift"] = True
        out["reasons"].append(f"tp {sc['tp']:.2f} above clamp {tmax}")
    if smin is not None and sc["sl"] < float(smin):
        out["drift"] = True
        out["reasons"].append(f"sl {sc['sl']:.2f} below clamp {smin}")
    if smax is not None and sc["sl"] > float(smax):
        out["drift"] = True
        out["reasons"].append(f"sl {sc['sl']:.2f} above clamp {smax}")
    return out


async def _load_signals(symbols: list[str], since: datetime, until: datetime) -> list[dict]:
    import httpx

    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    rows: list[dict] = []
    off = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "symbol,signal,ts,entry_price,atr",
                    "source": "eq.sweep-light",
                    "signal": "in.(buy,sell)",
                    "symbol": f"in.({','.join(symbols)})",
                    "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                    "order": "ts.asc",
                    "limit": "1000",
                    "offset": str(off),
                },
            )
            resp.raise_for_status()
            batch = resp.json() or []
            rows += batch
            off += len(batch)
            if len(batch) < 1000:
                break
    out, seen = [], set()
    for r in rows:
        try:
            atr = float(r.get("atr") or 0)
            entry = float(r["entry_price"])
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        if atr <= 0:
            continue
        key = (r["symbol"], r["signal"], int(ts // 3600))
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": r["symbol"], "signal": r["signal"], "entry_price": entry, "atr": atr, "_ts": ts})
    return out


async def _paths_for(symbols: list[str], members: list[dict], since: datetime, until: datetime):
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

    signals = await _load_signals(symbols, since, until)
    if len(signals) < MIN_PATHS:
        return np.empty((0, EXPIRY_BARS)), []
    bars: dict[str, list[dict]] = {}
    await fetch_tradingview_batch(
        [{"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]} for m in members],
        hours=24 * (LOOKBACK_DAYS + 5),
        interval="1h",
        ohlcv_out=bars,
    )
    bars = {
        s: [b for b in rows if b.get("t") is not None and b.get("c") is not None]
        for s, rows in bars.items()
    }
    return build_paths(signals, bars), signals


def _fmt_sc(sc: dict[str, Any] | None) -> str:
    if not sc:
        return "—"
    return (
        f"{sc['tp']:.2f}/{sc['sl']:.2f} · RR {sc['tp']/sc['sl']:.2f} · "
        f"wr {sc['wr']:.3f} · EV {sc['ev']:+.3f} · n {sc['n']}"
    )


async def run_atr_reopt(*, lookback_days: int = LOOKBACK_DAYS) -> dict[str, Any]:
    """Free-lattice re-solve per symbol + forex pool. Proposal-only."""
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    peers = await fetch_active_symbols()
    peers = [p for p in peers if p.get("symbol") and p.get("tradingview_symbol")]
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=lookback_days)

    lines = [
        f"# Monthly ATR re-optimum — {since.date()} → {until.date()}",
        "",
        "Free lattice · wr∈[0.40,0.60] · exp≤0.20 · cost 0.1 · both halves EV>0",
        "Proposals only — YAML is operator-owned. Heavy sweep keeps local clamps.\n",
    ]
    proposals: list[dict[str, Any]] = []

    fx = [p for p in peers if p["symbol"] in FOREX]
    scopes: list[tuple[str, list[dict], list[str]]] = []
    if fx:
        scopes.append(("forex", fx, [p["symbol"] for p in fx]))
    for p in peers:
        if p["symbol"] not in FOREX:
            scopes.append((p["symbol"], [p], [p["symbol"]]))

    for scope, members, symbols in scopes:
        paths, _ = await _paths_for(symbols, members, since, until)
        sc = solve_free(paths)
        asset = members[0].get("asset_class")
        cmp = compare_to_current(scope if scope != "forex" else None, sc, asset if scope != "forex" else "forex")
        row = f"| {scope} | {_fmt_sc(sc)} | drift={cmp['drift']} | {'; '.join(cmp['reasons']) or '—'} |"
        print(row, flush=True)
        lines.append(row)
        if sc and cmp["drift"]:
            proposals.append({"scope": scope, "pick": sc, "compare": cmp})

    lines.append("")
    lines.append(f"proposals: {len(proposals)}")
    for p in proposals:
        lines.append(
            f"- **{p['scope']}**: propose {_fmt_sc(p['pick'])} — {'; '.join(p['compare']['reasons'])}"
        )

    out_dir = Path(__file__).resolve().parents[2] / "worklog" / "kairos" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = out_dir / f"{stamp}_atr_reopt.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "window": f"{since.date()}..{until.date()}",
        "scopes": len(scopes),
        "proposals": len(proposals),
        "report": str(out_path.name),
    }
    try:
        from regime_platform.sweep_orchestrator.kairos import write_job_heartbeat

        await write_job_heartbeat("atr_monthly_reopt", summary)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:reopt] heartbeat failed: %s", exc)

    logger.info("[atr:reopt] done %s proposals=%d report=%s", summary["window"], len(proposals), out_path)
    return {"summary": summary, "proposals": proposals, "report": str(out_path)}
