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

from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    build_envelopes,
    live_basis,
    live_same_bar_policy,
    select_tp_sl,
    walk_envelopes,
)

logger = logging.getLogger("regime.sweep.atr_reopt")

COST = 0.1
LOOKBACK_DAYS = 45
FOREX = ("AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY")
# NOTE: this module carries NO acceptance rule of its own. min_rr, the expiry
# bar, cost, the sample floor and the both-halves gate all come from the same
# place the live picker gets them (config + tiered_atr.select_tp_sl). The old
# WR_LO/WR_HI band and MIN_PATHS/MIN_DEC floors were a second, divergent
# methodology — removed 2026-10-13.
# Material drift bar: propose when new TP/SL falls outside current clamp
# or sits >15% away from the published pick.
DRIFT_REL = 0.15

# Vol-urgency (2026-09-30, revised 2026-10-01): the 4h job IS the clock.
# Cooldown floors are durable (nt_roadmap) — never in-memory only — so a
# Studio restart cannot double-fire or look "stale".
_URGENCY_STATE_MECH = "atr_reopt_urgency"


async def _load_urgency_state() -> dict[str, Any]:
    """Durable last-run/last-apply stamps from nt_roadmap (survives restart)."""
    try:
        import httpx

        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_roadmap"),
                headers=_headers(),
                params={"select": "current_state,notes", "mechanism": f"eq.{_URGENCY_STATE_MECH}"},
            )
        rows = resp.json() or []
        if not rows:
            return {}
        raw = rows[0].get("current_state") or ""
        # current_state = "last_run <ts> · {json}"
        if "·" in raw:
            raw = raw.split("·", 1)[1].strip()
        import json

        return json.loads(raw) if raw.startswith("{") else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:urg] state load failed: %s", exc)
        return {}


async def _save_urgency_state(state: dict[str, Any]) -> None:
    try:
        from regime_platform.sweep_orchestrator.kairos import write_job_heartbeat

        await write_job_heartbeat(_URGENCY_STATE_MECH, state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:urg] state save failed: %s", exc)


def _ts_or_none(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _cell(env: np.ndarray, tp: float, sl: float, cost: float) -> dict[str, Any]:
    """Reporting dict for one (tp, sl) cell, measured on the LIVE envelope."""
    pnl, exp = walk_envelopes(env, float(tp), float(sl),
                              same_bar_policy=live_same_bar_policy())
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    dec = int(tp_hit.sum() + sl_hit.sum())
    net = pnl - cost
    mid = len(env) // 2
    return {
        "tp": float(tp),
        "sl": float(sl),
        "wr": float(tp_hit.sum() / dec) if dec else float("nan"),
        "exp": float(exp.mean()),
        "ev": float(net.mean()),
        "ev_h1": float(net[:mid].mean()) if mid else float("nan"),
        "ev_h2": float(net[mid:].mean()) if mid else float("nan"),
        "n": int(env.shape[0]),
        "dec": dec,
    }


def _select(env, tp_grid, sl_grid, *, cfg, clamps=None, allow_best_effort=False):
    """The ONE solve: ``tiered_atr.select_tp_sl``.

    The reopt must not carry a second acceptance rule. This passes the caller's
    grid + clamps straight into the picker's own solve, so min_rr, the
    both-halves gate, the expiry bar, cost, the sample floor and the
    best-effort fallback are all identical by construction. Only the grid and
    the clamps differ between the live picker and the reopt.
    """
    from regime_platform.core.feature_flags import get_min_rr

    c = clamps or {}
    return select_tp_sl(
        envelopes=env,
        tp_grid=[float(x) for x in tp_grid],
        sl_grid=[float(x) for x in sl_grid],
        min_rr=get_min_rr(),
        max_expiry=float(cfg["max_expiry"]),
        cost_atr=float(cfg["cost_atr"]),
        min_signals=int(cfg.get("min_signals") or 60),
        tp_atr_min=c.get("tp_atr_min"),
        tp_atr_max=c.get("tp_atr_max"),
        sl_atr_min=c.get("sl_atr_min"),
        sl_atr_max=c.get("sl_atr_max"),
        same_bar_policy=live_same_bar_policy(),
        allow_best_effort=allow_best_effort,
    )


def eval_cell(env: np.ndarray, tp: float, sl: float, cost: float = COST) -> Optional[dict[str, Any]]:
    """Score ONE (tp, sl) cell through the picker's own solve.

    Unit-test entry point. It defers to ``_select`` (a 1x1 grid, no clamps) so a
    cell can never be judged by a rule the live picker does not apply.
    """
    if env is None or len(env) == 0:
        return None
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    cfg = dict(get_dynamic_atr_config(None, None))
    cfg["cost_atr"] = cost
    pick = _select(env, [float(tp)], [float(sl)], cfg=cfg, allow_best_effort=True)
    return _cell(env, tp, sl, cost) if pick is not None else None


# The free lattice — the reopt's SEARCH SPACE, and the only thing that differs
# from the live picker. The picker is bound by the shipped clamps; the reopt
# searches freely because proposing new clamps is its job.
FREE_TP = tuple(float(x) for x in np.round(np.arange(0.25, 16.01, 0.25), 2))
FREE_SL = tuple(float(x) for x in np.round(np.arange(0.25, 10.01, 0.25), 2))


def solve_free(
    env: np.ndarray,
    *,
    symbol: str | None = None,
    asset_class: str | None = None,
    allow_best_effort: bool = False,
) -> Optional[dict[str, Any]]:
    """Re-solve on the free lattice using the PICKER's solve.

    Identical methodology to ``tiered_atr.select_tp_sl`` as the live picker calls
    it — same walk, basis, min_rr, cost, expiry bar, both-halves gate, sample
    floor and fallback. The conditional input is the clamps: ``None`` here
    (free), the shipped band's there.
    """
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    cfg = get_dynamic_atr_config(asset_class, symbol)
    if env is None or len(env) < int(cfg.get("min_signals") or 60):
        return None
    pick = _select(env, FREE_TP, FREE_SL, cfg=cfg, allow_best_effort=allow_best_effort)
    if pick is None:
        return None
    # Refine around the coarse optimum — a SEARCH step, not a second rule.
    for span, step in ((0.30, 0.05), (0.08, 0.01)):
        tp_g = np.round(np.arange(max(0.15, pick.tp_atr - span), pick.tp_atr + span, step), 2)
        sl_g = np.round(np.arange(max(0.15, pick.sl_atr - span), pick.sl_atr + span, step), 2)
        p2 = _select(env, tp_g, sl_g, cfg=cfg, allow_best_effort=allow_best_effort)
        if p2 is not None and p2.exp_atr > pick.exp_atr:
            pick = p2
    return _cell(env, pick.tp_atr, pick.sl_atr, float(cfg["cost_atr"]))


def band_health(env: np.ndarray, *, symbol: str | None, asset_class: str | None) -> dict[str, Any]:
    """Score the SHIPPED band with the picker's own solve — the invalidity path.

    ``solve_free`` can only propose a REPLACEMENT, so a band that fails on the
    live basis while no better cell exists is invisible to it (that is exactly
    XAUUSD: 1.0 ATR solved on the retired close-walk basis, EV<=0 on intrabar,
    nothing qualifying to replace it). Here the shipped band's own grid + clamps
    go through the SAME solve the live picker runs, so a non-positive expectancy
    marks the band as broken regardless of whether a replacement exists.
    """
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    cfg = get_dynamic_atr_config(asset_class, symbol)
    if env is None or len(env) < int(cfg.get("min_signals") or 60):
        return {"band_ev": None, "invalid": False, "reason": "insufficient sample"}
    clamps = {k: cfg.get(k) for k in
              ("tp_atr_min", "tp_atr_max", "sl_atr_min", "sl_atr_max")}
    pick = _select(env, cfg["tp_atr"], cfg["sl_atr"], cfg=cfg, clamps=clamps,
                   allow_best_effort=True)
    if pick is None:
        return {"band_ev": None, "invalid": True, "reason": "no in-band cell for the shipped band"}
    invalid = pick.exp_atr <= 0
    return {
        "band_ev": float(pick.exp_atr),
        "band_tp": float(pick.tp_atr),
        "band_sl": float(pick.sl_atr),
        "invalid": invalid,
        "reason": (f"shipped band EV {pick.exp_atr:+.3f} <= 0 on the live basis"
                   if invalid else ""),
    }


def _rel_drift(new: float, old: float) -> float:
    if old is None or old == 0:
        return 1.0
    return abs(new - old) / abs(old)


def compare_to_current(
    symbol: str,
    sc: dict[str, Any] | None,
    asset_class: str | None,
    health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flag material drift vs dynamic_atr.by_symbol (operator-owned YAML).

    ``health`` is ``band_health()``'s verdict on the SHIPPED band, scored by the
    same solve the live picker runs. Drift alone is a REPLACEMENT signal — it
    cannot express "this band is broken", which is how a band sits EV-negative on
    the live basis while every report reads drift=False.
    """
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
        "invalid": False,
        "band_ev": (health or {}).get("band_ev"),
        "reasons": [],
    }
    # INVALIDITY first: a band that fails on the live basis is a finding whether
    # or not a replacement exists. This is the path drift cannot express.
    if health and health.get("invalid") and health.get("reason"):
        out["invalid"] = True
        out["reasons"].append(health["reason"])
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


async def _fetch_cutover_ts() -> Optional[float]:
    """Resolver-basis cutover as a unix ts (nt_calibration_cutover), or None.

    Same marker the calibration/reachability era filters use. Reopt trains on
    signal ENTRY time, so clamping the window to >= cutover means every signal
    in the solve was entered (and thus resolved) under the intrabar/sl_first
    basis — otherwise the pre-cutover close-walk era contaminates the solve
    (worklog 20261010_REOPT_CLAMP_HARMONIZATION.md Fix B). None → no clamp
    (fail-open to the full lookback).
    """
    try:
        import httpx

        from regime_platform.core.settings import settings
        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url
        if not settings.SUPABASE_URL:
            return None
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_calibration_cutover"),
                headers=_headers(),
                params={"select": "cutover_at", "limit": "1"},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            raw = rows[0].get("cutover_at")
            if not raw:
                return None
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[atr:reopt] cutover marker fetch failed: %s", exc)
        return None


async def _load_signals(symbols: list[str], since: datetime, until: datetime) -> list[dict]:
    import httpx

    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    # Fix B (2026-10-10, worklog 20261010_REOPT_CLAMP_HARMONIZATION.md): clamp
    # the window to the resolver-basis cutover so the solve only sees signals
    # entered under intrabar/sl_first. The 45d lookback otherwise spans the
    # pre-2026-10-02 close-walk era (n was 6506, ~all pre-cutover), which made
    # the wide 4.52/2.75 solve era-contaminated. Fail-open: no marker → full
    # lookback (prior behaviour).
    cutover_ts = await _fetch_cutover_ts()
    if cutover_ts is not None and cutover_ts > since.timestamp():
        logger.info(
            "[atr:reopt] era-clamp: lookback %s → %s (post-cutover only)",
            since.date(), datetime.fromtimestamp(cutover_ts, tz=timezone.utc).date(),
        )
        since = datetime.fromtimestamp(cutover_ts, tz=timezone.utc)

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
    # Same sample floor the picker uses (config min_signals), not a private one.
    from regime_platform.core.feature_flags import get_dynamic_atr_config

    floor = int(get_dynamic_atr_config(None, symbols[0] if symbols else None).get("min_signals") or 60)
    if len(signals) < floor:
        return np.empty((0, EXPIRY_BARS, 2)), []
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
    # 2026-10-08 (worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md): the reopt
    # proposes the SAME clamps the live picker uses, so it must measure on the
    # SAME basis — the live resolver's intrabar/sl_first rule. Building close
    # paths here would re-propose the tight stop the resolver keeps stopping out.
    return build_envelopes(signals, bars, basis=live_basis()), signals


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
        "Free lattice · the picker's OWN solve (tiered_atr.select_tp_sl) — only the clamps differ",
        "Proposals only — YAML is operator-owned. Heavy sweep keeps local clamps.\n",
    ]
    proposals: list[dict[str, Any]] = []
    invalid_bands: list[dict[str, Any]] = []

    fx = [p for p in peers if p["symbol"] in FOREX]
    scopes: list[tuple[str, list[dict], list[str]]] = []
    if fx:
        scopes.append(("forex", fx, [p["symbol"] for p in fx]))
    for p in peers:
        if p["symbol"] not in FOREX:
            scopes.append((p["symbol"], [p], [p["symbol"]]))

    for scope, members, symbols in scopes:
        env, _ = await _paths_for(symbols, members, since, until)
        asset = members[0].get("asset_class")
        # Config-layer parity (worklog 20261010_REOPT_CLAMP_HARMONIZATION.md):
        # compare against the SAME dynamic_atr layer the picker applies. For the
        # pooled forex scope the picker resolves by_symbol.FOREX (via any major
        # symbol, feature_flags.py:848-851), so pass a forex member as `symbol`
        # here — otherwise get_dynamic_atr_config(symbol=None) falls through to
        # the wide GLOBAL clamps and reopt's solve never flags drift vs the
        # tight FOREX band the picker is actually bound by.
        cmp_symbol = members[0]["symbol"]  # forex: a major resolves by_symbol.FOREX
        scope_class = asset if scope != "forex" else "forex"
        # Same solve the live picker runs; only the clamps differ (free here).
        # allow_best_effort mirrors the picker's G2 contract: the solve ALWAYS
        # returns geometry, so drift is always evaluated and a drifted band is
        # always reconditioned — never left silent behind a None.
        sc = solve_free(env, symbol=cmp_symbol, asset_class=scope_class,
                        allow_best_effort=True)
        # The shipped band scored by that same solve — the invalidity path.
        health = band_health(env, symbol=cmp_symbol, asset_class=scope_class)
        cmp = compare_to_current(cmp_symbol, sc, scope_class, health)
        bev = health.get("band_ev")
        row = (
            f"| {scope} | {_fmt_sc(sc)} | drift={cmp['drift']} | "
            f"band_ev={'—' if bev is None else f'{bev:+.3f}'} | "
            f"{'INVALID · ' if cmp.get('invalid') else ''}"
            f"{'; '.join(cmp['reasons']) or '—'} |"
        )
        print(row, flush=True)
        lines.append(row)
        if cmp.get("invalid"):
            invalid_bands.append({"scope": scope, "compare": cmp})
        if sc and cmp["drift"]:
            proposals.append({"scope": scope, "pick": sc, "compare": cmp})

    lines.append("")
    lines.append(f"proposals: {len(proposals)}")
    for p in proposals:
        lines.append(
            f"- **{p['scope']}**: propose {_fmt_sc(p['pick'])} — {'; '.join(p['compare']['reasons'])}"
        )
    lines.append("")
    lines.append(f"invalid bands: {len(invalid_bands)}")
    for b in invalid_bands:
        lines.append(f"- **{b['scope']}**: {b['compare']['reasons'][0]}")

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


def apply_symbol_clamps(symbol: str, pick: dict[str, Any], *, path: Any = None) -> bool:
    """Patch ``dynamic_atr.by_symbol.<SYMBOL>`` clamp lines in feature_flags.yaml.

    Line-level edit so comments/structure survive (no yaml.dump). Only
    touches tp/sl lattice + clamp keys under that symbol block. Creates the
    symbol block after ``by_symbol:`` if missing.

    ``path`` overrides the config file (tests pass a temp copy). The write is
    scoped to ``dynamic_atr``'s ``by_symbol`` — the config has four such blocks
    and AAPL also appears in ``macro.by_symbol``.
    """
    from pathlib import Path

    if path is None:
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
    section = ""
    in_by_symbol = False
    found = False
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip("\n")
        # Track the top-level section. The config carries FOUR `  by_symbol:`
        # blocks (macro, dynamic_atr, dynamic_deadline, geometry_band_urgency)
        # and AAPL appears in macro.by_symbol — so an unscoped anchor rewrites
        # the WRONG block. That is the bug that wrote six ATR keys into
        # macro.by_symbol.AAPL. Only dynamic_atr's by_symbol is ours.
        if stripped and not stripped.startswith((" ", "\t")):
            section = stripped.split(":", 1)[0]
        if section == "dynamic_atr" and stripped.startswith("  by_symbol:"):
            in_by_symbol = True
            out.append(raw)
            i += 1
            continue
        if in_by_symbol and stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
            in_by_symbol = False
        if in_by_symbol and re.match(rf"^    {re.escape(symbol)}:\s*$", stripped):
            found = True
            out.append(raw)
            i += 1
            # rewrite known keys until next symbol / dedent
            while i < len(lines):
                s2 = lines[i].rstrip("\n")
                if re.match(r"^    \S", s2) or (s2 and not s2.startswith(" ")):
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
        # Scope the insertion point to dynamic_atr's by_symbol too — the bare
        # `^  by_symbol:` matched macro's block first and appended ATR keys there.
        sec = re.search(r"(?m)^dynamic_atr:\s*$", text2)
        if not sec:
            return False
        m = re.compile(r"(?m)^  by_symbol:\s*$").search(text2, sec.end())
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
    """Unattended vol-urgency on a **4h cycle** (restart-safe floors).

    2026-10-01: drop the 48h in-memory cooldown (wiped on Studio restart →
    double-fire / stale look). The 4h job is the clock:

    1. Every 4h: measure ``vol_ratio`` per symbol.
    2. If any ≥ ``elevated_ratio`` and durable ``last_reopt_at`` is older
       than ``min_reopt_hours`` (default **4**), run the free lattice.
    3. If ``auto_apply_clamps`` and drift, patch ``by_symbol`` — same 4h
       floor on ``last_apply_at`` so a restart cannot re-write clamps.
    4. Discord is FYI of the action taken.

    State lives in ``nt_roadmap(atr_reopt_urgency)`` — survives restarts.
    Returns ``{enabled, checked, alerted, reopt, applied}``. Never raises.
    """
    import time as _time

    from regime_platform.core.feature_flags import get_atr_reopt_urgency_config

    cfg = get_atr_reopt_urgency_config()
    if not cfg["enabled"]:
        return {"enabled": False, "checked": 0, "alerted": []}

    threshold = float(cfg["elevated_ratio"])
    # 4h cycle floor (was 48h in-memory — ineffective across restarts).
    min_reopt_h = float(cfg.get("min_reopt_hours") or cfg.get("cooldown_hours") or 4.0)
    if min_reopt_h > 24:
        min_reopt_h = 4.0
    auto_apply = bool(cfg.get("auto_apply_clamps", True))
    vol_short = int(cfg.get("vol_ratio_short") or 24)
    vol_long = int(cfg.get("vol_ratio_long") or 168)
    min_bars = int(vol_long) + 2

    state = await _load_urgency_state()
    last_reopt = _ts_or_none(state.get("last_reopt_at"))
    last_apply = _ts_or_none(state.get("last_apply_at"))
    alerted_at = state.get("last_alerted_at") or {}
    now = _time.time()

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
        # per-symbol Discord floor — durable across restarts
        last_alert = _ts_or_none(alerted_at.get(sym))
        if last_alert is not None and (now - last_alert) < min_reopt_h * 3600.0:
            continue
        spiked.append((sym, vr))

    if not spiked:
        return {"enabled": True, "checked": checked, "alerted": [], "reopt": None, "applied": []}

    reopt_result: dict[str, Any] | None = None
    applied: list[str] = []
    ran = False
    can_run = last_reopt is None or (now - last_reopt) >= min_reopt_h * 3600.0
    can_apply = last_apply is None or (now - last_apply) >= min_reopt_h * 3600.0
    if can_run:
        try:
            reopt_result = await run_atr_reopt()
            ran = True
            state["last_reopt_at"] = _iso(now)
        except Exception as exc:  # noqa: BLE001
            logger.error("[atr:urg] auto re-opt failed: %s", exc)
        if ran and auto_apply and reopt_result and can_apply:
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
            if applied:
                state["last_apply_at"] = _iso(now)
                state["last_applied"] = applied

    for sym, vr in spiked:
        alerted_at[sym] = _iso(now)
        try:
            from notifications.discord import discord_notifier

            action = "free lattice + clamps applied" if applied else (
                "free lattice ran (no drift / no apply)" if ran else (
                    "re-opt skipped (4h floor — last_run %s)" % state.get("last_reopt_at", "—")
                    if not can_run else "re-opt floor"
                )
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
                    "Discord is FYI only — 4h cycle, durable floor in nt_roadmap."
                ),
                data={
                    "vol_ratio": f"{vr:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "auto_action": action,
                    "applied": ", ".join(applied) if applied else "none",
                    "cycle_hours": min_reopt_h,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[atr:urg] %s Discord send failed: %s", sym, exc)

    state["last_alerted_at"] = alerted_at
    await _save_urgency_state(state)
    logger.warning(
        "[atr:urg] spiked=%s ran_reopt=%s applied=%s floor=%sh",
        [s for s, _ in spiked], ran, applied, min_reopt_h,
    )
    return {
        "enabled": True,
        "checked": checked,
        "alerted": [s for s, _ in spiked],
        "reopt": (reopt_result or {}).get("summary") if reopt_result else None,
        "applied": applied,
        "cycle_hours": min_reopt_h,
    }
