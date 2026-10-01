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
import re
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

# Urgency nudge (2026-09-30): vol_ratio spike → Discord → run reopt now.
# In-memory cooldown (process-lifetime is enough for a nudge; the monthly
# job is still the durable clock).
_LAST_URGENCY_ALERT: dict[str, float] = {}


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


# ─── Vol-urgency → auto re-opt (market-driven, unattended) ────────────────

_LAST_REOPT_RUN: float = 0.0


def _clamp_block(tp: float, sl: float, pad_tp: float = 0.25, pad_sl: float = 0.12) -> dict[str, Any]:
    """Narrow by_symbol lattice around a free-lattice pick (same style as research)."""
    tp, sl = float(tp), float(sl)
    return {
        "tp_atr": [round(max(0.15, tp - pad_tp), 2), round(tp, 2), round(tp + pad_tp, 2)],
        "sl_atr": [round(max(0.15, sl - pad_sl), 2), round(sl, 2), round(sl + pad_sl, 2)],
        "tp_atr_min": round(max(0.15, tp - 2 * pad_tp), 2),
        "tp_atr_max": round(tp + 2 * pad_tp, 2),
        "sl_atr_min": round(max(0.15, sl - 2 * pad_sl), 2),
        "sl_atr_max": round(sl + 2 * pad_sl, 2),
    }


def apply_symbol_clamps(symbol: str, pick: dict[str, Any]) -> bool:
    """Patch ``dynamic_atr.by_symbol.<SYMBOL>`` clamp lines in feature_flags.yaml.

    Line-level edit so comments/structure survive (no yaml.dump). Only
    touches tp/sl lattice + clamp keys under that symbol block. Creates the
    symbol block after ``by_symbol:`` if missing.
    """
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "config" / "feature_flags.yaml"
    text = path.read_text(encoding="utf-8")
    blk = _clamp_block(pick["tp"], pick["sl"])
    # Rebuild the key lines for this symbol (4-space indent under by_symbol)
    new_keys = {
        "tp_atr": f"      tp_atr: [{', '.join(str(x) for x in blk['tp_atr'])}]",
        "sl_atr": f"      sl_atr: [{', '.join(str(x) for x in blk['sl_atr'])}]",
        "tp_atr_min": f"      tp_atr_min: {blk['tp_atr_min']}",
        "tp_atr_max": f"      tp_atr_max: {blk['tp_atr_max']}",
        "sl_atr_min": f"      sl_atr_min: {blk['sl_atr_min']}",
        "sl_atr_max": f"      sl_atr_max: {blk['sl_atr_max']}",
    }
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    in_by_symbol = False
    in_sym = False
    found = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n")
        if stripped.startswith("  by_symbol:"):
            in_by_symbol = True
            out.append(raw)
            i += 1
            continue
        if in_by_symbol and stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
            in_by_symbol = False
            in_sym = False
        if in_by_symbol and re.match(rf"^    {re.escape(symbol)}:\s*$", stripped):
            in_sym = True
            found = True
            out.append(raw)
            i += 1
            # rewrite known keys until next symbol / dedent
            while i < len(lines):
                s2 = lines[i].rstrip("\n")
                if re.match(r"^    \S", s2) or (s2 and not s2.startswith(" ")):
                    in_sym = False
                    break
                m = re.match(r"^      (tp_atr|sl_atr|tp_atr_min|tp_atr_max|sl_atr_min|sl_atr_max):", s2)
                if m and m.group(1) in new_keys:
                    out.append(new_keys[m.group(1)] + "\n")
                    del new_keys[m.group(1)]
                else:
                    out.append(lines[i])
                i += 1
            for k in ("tp_atr", "sl_atr", "tp_atr_min", "tp_atr_max", "sl_atr_min", "sl_atr_max"):
                if k in new_keys:
                    out.append(new_keys[k] + "\n")
                    del new_keys[k]
            continue
        out.append(raw)
        i += 1

    if not found and in_by_symbol is not False:
        # append block at end of by_symbol — locate insertion point
        text2 = "".join(out)
        m = re.search(r"(?m)^  by_symbol:\n", text2)
        if not m:
            return False
        block = f"    {symbol}:\n" + "\n".join(new_keys.values()) + "\n"
        # insert after last symbol under by_symbol (before next top-level key)
        # safer: before the first non-indented line after by_symbol
        rest = text2[m.end():]
        cut = re.search(r"(?m)^(?!\s)", rest)
        insert_at = m.end() + (cut.start() if cut else len(rest))
        text2 = text2[:insert_at] + block + text2[insert_at:]
        path.write_text(text2, encoding="utf-8")
        return True

    path.write_text("".join(out), encoding="utf-8")
    return True


def _symbol_has_vol_gate(symbol: str) -> bool:
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    cfg = get_dynamic_atr_config(None, symbol)
    return cfg.get("vol_ratio_max") is not None or cfg.get("vol_ratio_min") is not None


async def check_atr_reopt_urgency() -> dict[str, Any]:
    """Unattended vol-urgency: elevated ``vol_ratio`` → **auto-run** free lattice.

    2026-09-30 redesign (operator cannot watch Discord 24/7):

    1. ``vol_ratio >= elevated_ratio`` on any symbol (4h job).
    2. Free lattice runs immediately (``run_atr_reopt``), 48h run-cooldown.
    3. If ``auto_apply_clamps`` and a proposal drifted, patch ``by_symbol``
       clamps in ``feature_flags.yaml`` (hot-reload).
    4. Discord ``#system-status`` is **FYI of the action taken** — never a
       to-do for a human.

    Returns ``{enabled, checked, alerted, reopt, applied}``. Never raises.
    """
    import time as _time

    from regime_platform.core.feature_flags import get_atr_reopt_urgency_config

    cfg = get_atr_reopt_urgency_config()
    if not cfg["enabled"]:
        return {"enabled": False, "checked": 0, "alerted": []}

    threshold = float(cfg["elevated_ratio"])
    cooldown_h = float(cfg["cooldown_hours"])
    auto_apply = bool(cfg.get("auto_apply_clamps", True))
    vol_short = int(cfg.get("vol_ratio_short") or 24)
    vol_long = int(cfg.get("vol_ratio_long") or 168)
    min_bars = int(vol_long) + 2

    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
    from regime_platform.sweep_orchestrator.tiered_atr import compute_vol_ratio

    try:
        peers = [p for p in await fetch_active_symbols() if p.get("symbol") and p.get("tradingview_symbol")]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:urg] peers fetch failed: %s", exc)
        return {"enabled": True, "checked": 0, "alerted": []}

    bars: dict[str, list[dict]] = {}
    try:
        await fetch_tradingview_batch(
            [{"symbol": p["symbol"], "tradingview_symbol": p["tradingview_symbol"]} for p in peers],
            hours=vol_long * 2 + 48,
            interval="1h",
            ohlcv_out=bars,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:urg] bar fetch failed: %s", exc)
        return {"enabled": True, "checked": 0, "alerted": []}

    checked = 0
    spiked: list[tuple[str, float]] = []
    now = _time.time()
    for p in peers:
        sym = p["symbol"]
        rows = [b for b in (bars.get(sym) or []) if b.get("c") is not None]
        if len(rows) < min_bars:
            continue
        closes = np.array([float(b["c"]) for b in rows], dtype="float64")
        vr = compute_vol_ratio(closes, len(closes) - 1, vol_short, vol_long)
        if not np.isfinite(vr):
            continue
        checked += 1
        if vr < threshold:
            continue
        last = _LAST_URGENCY_ALERT.get(sym)
        if last is not None and (now - last) < cooldown_h * 3600.0:
            continue
        spiked.append((sym, vr))

    if not spiked:
        return {"enabled": True, "checked": checked, "alerted": [], "reopt": None, "applied": []}

    global _LAST_REOPT_RUN
    reopt_result: dict[str, Any] | None = None
    applied: list[str] = []
    ran = False
    if now - _LAST_REOPT_RUN >= cooldown_h * 3600.0:
        try:
            reopt_result = await run_atr_reopt()
            _LAST_REOPT_RUN = now
            ran = True
        except Exception as exc:  # noqa: BLE001
            logger.error("[atr:urg] auto re-opt failed: %s", exc)
        if ran and auto_apply and reopt_result:
            for prop in reopt_result.get("proposals") or []:
                scope = prop.get("scope")
                pick = prop.get("pick") or {}
                if not scope or not pick.get("tp"):
                    continue
                # Do not clobber a vol-gated cell with a pooled free-lattice
                # pick (BTC 8.07/1.77 @ vol_ratio≤0.71 vs pooled 6.32/3.17).
                if scope not in ("forex",) and _symbol_has_vol_gate(scope):
                    logger.info(
                        "[atr:urg] skip apply %s — vol_ratio gate present "
                        "(pooled lattice is not the gated cell)",
                        scope,
                    )
                    continue
                try:
                    if apply_symbol_clamps(scope, pick):
                        applied.append(f"{scope} {pick['tp']:.2f}/{pick['sl']:.2f}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[atr:urg] apply %s failed: %s", scope, exc)

    for sym, vr in spiked:
        _LAST_URGENCY_ALERT[sym] = now
        try:
            from notifications.discord import discord_notifier

            action = "free lattice + clamps applied" if applied else (
                "free lattice ran (no drift / no apply)" if ran else "re-opt still cooling down"
            )
            await discord_notifier.send_risk_alert(
                symbol=sym,
                risk_type="atr_reopt_urgency",
                message=(
                    f"{sym}: vol_ratio {vr:.2f} ≥ {threshold:.2f} "
                    f"(+{(vr - 1.0) * 100:.0f}% vs 1-week baseline). "
                    f"AUTO: {action}. "
                    + (f"Applied: {', '.join(applied)}. " if applied else "")
                    + (f"Proposals={len((reopt_result or {}).get('proposals') or [])}. "
                       if reopt_result else "")
                    + f"Report: {(reopt_result or {}).get('summary', {}).get('report', '—')}. "
                    "Discord is FYI only — no operator action required unless you "
                    "want to override clamps."
                ),
                data={
                    "vol_ratio": f"{vr:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "auto_action": action,
                    "applied": ", ".join(applied) if applied else "none",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[atr:urg] %s Discord send failed: %s", sym, exc)

    logger.warning(
        "[atr:urg] spiked=%s ran_reopt=%s applied=%s",
        [s for s, _ in spiked], ran, applied,
    )
    return {
        "enabled": True,
        "checked": checked,
        "alerted": [s for s, _ in spiked],
        "reopt": (reopt_result or {}).get("summary") if reopt_result else None,
        "applied": applied,
    }
