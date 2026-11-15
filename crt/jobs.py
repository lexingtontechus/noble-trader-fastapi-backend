"""jobs.py — the CRTOrchestrator job family (CRT-1).

One scheduler job (``crt-engine-tick``) registered inside
``SweepOrchestrator.start()`` (the cheapest of the assessment's two mounts:
reuses the scheduler, the health heartbeat, the process supervision). The
registration is additive and exception-guarded — CRT can never break the
platform's startup. At fire time the job:

1. gates on ``crt_mode`` (off -> return BEFORE any fetch or DB touch — the
   dark contract: byte-identical, zero work);
2. lazily builds/rebuilds the engine registry from ``config/crt/
   crt_engine.yaml`` x the CRT-0 artifact (mtime hot-reload: a re-minted
   artifact rebuilds engines; open positions re-hydrate from the paper
   table);
3. fetches candles per symbol only when a bar boundary passed since the last
   processed bar (wall-clock expectation vs the last SEEN closed bar — TDVA
   lag self-heals on the next minute's retry);
4. refreshes the shared caps snapshot (open CRT positions + today's paper
   PnL — one query set, fail-open);
5. ticks every (symbol, TF) engine with its closed bars.

M30 is derived from the same M15 fetch (2x15m aggregation); H1 is a second
fetch. Symbol lists are chunked by the fetcher (10 per call). Every failure
is caught, logged, and signaled to #crt (fail-open) — a CRT problem never
breaks the orchestrator's loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("crt.jobs")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "crt", "crt_engine.yaml")

# module-level engine registry (the /crt router reads this)
_ENGINES: dict[tuple[str, str], Any] = {}
_META: dict[str, Any] = {}
_LAST_STATS: dict[str, Any] = {"ts": None, "engines": 0, "fills": 0}
_ARTIFACT_MTIME: float | None = None
_CONFIG_MTIME: float | None = None
_LAST_CLOSED: dict[tuple[str, str], float] = {}
_NOTIFIER: Any = None
_NOTIFIER_LOADED = False

# CRT-2 persistence state (throttled flush + daily rolling snapshot)
_MON_FLUSH_TS: float = 0.0
_MON_SIG: dict[tuple[tuple[str, str], tuple[str, int]], tuple] = {}
_ROLL_DAY: str | None = None
_MON_STATUS: dict[str, Any] = {"persist": False, "rows": 0,
                              "last_flush": None, "last_rolling_day": None}

# CRT-3 promotion state (leaderboard-gated live dispatch)
_LIVE_DISPATCH: bool = False          # set per tick when mode=live AND bars met
_LAST_PROMO_ELIGIBLE: bool | None = None   # eligibility-change signal memory

try:  # stdlib-fallback: the engine config is yaml like every CRT knob
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a repo requirement
    yaml = None


def _load_engine_config() -> dict[str, Any]:
    if yaml is None or not os.path.exists(ENGINE_CONFIG_PATH):
        return {}
    try:
        with open(ENGINE_CONFIG_PATH, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001 - fail-open
        return {}


def _get_notifier(cfg: dict[str, Any]):
    """Build the #crt notifier (env-var webhook) once; None when disabled."""
    global _NOTIFIER, _NOTIFIER_LOADED
    if _NOTIFIER_LOADED:
        return _NOTIFIER
    _NOTIFIER_LOADED = True
    try:
        from regime_edge_crt import config as crt_config
        from regime_edge_crt import discord_notify
        base = crt_config.load_discord_config()
        eng = cfg.get("discord") or {}
        if eng.get("username"):
            base["username"] = str(eng["username"])
        if eng.get("label"):
            base["label"] = str(eng["label"])
        for event, enabled in (eng.get("events") or {}).items():
            base.setdefault("events", {})[str(event)] = bool(enabled)
        if not eng.get("enabled", True):
            base["enabled"] = False
        _NOTIFIER = discord_notify.from_config(base)
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("crt.jobs: notifier unavailable: %s", exc)
        _NOTIFIER = None
    return _NOTIFIER


def _signal_sync(event: str, title: str, body: str) -> None:
    notifier = _NOTIFIER
    if notifier is None or not notifier.enabled:
        return
    try:
        notifier.notify(event, title, body)
    except Exception:  # noqa: BLE001 - fail-open by contract
        pass


async def _signal(event: str, title: str, body: str) -> None:
    """Fire-and-forget #crt signal off the event loop (the sender is sync)."""
    try:
        await asyncio.to_thread(_signal_sync, event, title, body)
    except Exception:  # noqa: BLE001 - fail-open by contract
        pass


def _signal_fn(cfg: dict[str, Any]):
    """The engines' sync signal callback (spawns a task; never blocks).

    Titles are prefixed ``[LIVE]`` while the CRT-3 promotion gate holds
    (``_LIVE_DISPATCH`` — read at call time so a bar flip mid-process
    changes the stream immediately)."""
    def fn(event: str, title: str, body: str) -> None:
        if _LIVE_DISPATCH:
            title = f"[LIVE] {title}"
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_signal(event, title, body))
        except RuntimeError:
            _signal_sync(event, title, body)
    return fn


def _signal_promotion_sync(title: str, body: str) -> None:
    """CRT-3 promotion notices — labeled CRT-3 through the same sender."""
    notifier = _NOTIFIER
    if notifier is None or not notifier.enabled:
        return
    if not notifier.events.get("promotion", True):
        return
    try:
        from regime_edge_crt.discord_notify import event_message
        notifier.send(event_message("promotion", title, body, label="CRT-3"))
    except Exception:  # noqa: BLE001 - fail-open by contract
        pass


async def _signal_promotion(title: str, body: str) -> None:
    try:
        await asyncio.to_thread(_signal_promotion_sync, title, body)
    except Exception:  # noqa: BLE001 - fail-open by contract
        pass


async def _apply_promotion(mode: str, stats: dict[str, Any]) -> None:
    """CRT-3 enforcement: ``live`` runs paper semantics until every
    pre-registered bar holds; bars met set the live-dispatch flag (the #crt
    stream becomes the actionable surface). The refusal is never silent —
    recorded on the tick stats AND signaled to #crt on eligibility change
    (bounded: state-change transitions only)."""
    global _LIVE_DISPATCH, _LAST_PROMO_ELIGIBLE
    _LIVE_DISPATCH = False
    if mode != "live":
        _LAST_PROMO_ELIGIBLE = None    # shadow/off never carry a live verdict
        return
    from crt import promotion
    verdict = await promotion.current_verdict(engines=list(_ENGINES.values()))
    eligible = bool(verdict.get("eligible"))
    _LIVE_DISPATCH = eligible
    if eligible:
        stats["live"] = True
    else:
        stats["live_refused"] = verdict.get("reasons") or ["no evidence"]
    if _LAST_PROMO_ELIGIBLE is None or _LAST_PROMO_ELIGIBLE != eligible:
        ev = verdict.get("evidence") or {}
        head = (f"PROMOTED to live dispatch — bars met: n {ev.get('n')} / "
                f"EV {ev.get('ev_r', 0):+.3f}R / t {ev.get('t_stat', 0):.2f} / "
                f"LCB {ev.get('lcb_r', 0):+.3f}R"
                if eligible else
                "live REFUSED — bars unmet, paper semantics continue")
        await _signal_promotion(head, "reasons: " + ("; ".join(
            verdict.get("reasons") or ["eligible"])[:6] if not eligible
            else "evidence holds; demotion is automatic on evidence decay"))
    _LAST_PROMO_ELIGIBLE = eligible


def _rebuild_needed(cfg: dict[str, Any]) -> bool:
    global _ARTIFACT_MTIME, _CONFIG_MTIME
    artifact_path = os.path.join(
        _REPO_ROOT, str((cfg.get("artifact") or {}).get("path") or "config/crt_cont.json"))
    try:
        a_mtime = os.path.getmtime(artifact_path) if os.path.exists(artifact_path) else -1.0
        c_mtime = os.path.getmtime(ENGINE_CONFIG_PATH) if os.path.exists(ENGINE_CONFIG_PATH) else -1.0
    except OSError:
        return False
    changed = (a_mtime != _ARTIFACT_MTIME) or (c_mtime != _CONFIG_MTIME)
    _ARTIFACT_MTIME, _CONFIG_MTIME = a_mtime, c_mtime
    return changed


def _build_engines(cfg: dict[str, Any]) -> None:
    """(Re)build the (symbol, tf) engine registry from config x artifact."""
    global _ENGINES, _META, _MON_SIG
    from crt import config_loader
    from crt.engine import SymbolTFEngine

    artifact_path = os.path.join(
        _REPO_ROOT, str((cfg.get("artifact") or {}).get("path") or "config/crt_cont.json"))
    meta, options = config_loader.load_artifact(artifact_path)
    _META = {"path": artifact_path, "rows": len(options),
             "train_end": meta.get("train_end"), "runid": meta.get("runid")}
    by_stf = config_loader.options_by_symbol_tf(options)
    symbols = cfg.get("symbols") or []
    engine_cfg = {k: v for k, v in cfg.items()
                  if k in ("chop", "sessions", "rolling", "decay", "sizing",
                           "claims")}
    engine_cfg["artifact_runid"] = meta.get("runid")   # CRT-2 mint stamp
    engines: dict[tuple[str, str], Any] = {}
    for entry in symbols:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        spread = float(entry.get("spread_price") or 0.0)
        slip = float(entry.get("slip_price") or 0.0)
        for tf in cfg.get("timeframes") or ("M15", "M30", "H1"):
            opts = by_stf.get((symbol, str(tf).upper()))
            if not opts:
                continue
            engines[(symbol, str(tf).upper())] = SymbolTFEngine(
                symbol, str(tf).upper(), opts, engine_cfg, spread, slip,
                signal=_signal_fn(cfg))
    _ENGINES = engines
    _LAST_CLOSED.clear()
    _MON_SIG = {}    # engine rebuild: monitor signatures start fresh
    logger.info("crt.jobs: %d engine(s) from %s (%d option rows)",
                len(engines), artifact_path, len(options))


async def _preregister_monitors(cfg: dict[str, Any]) -> None:
    """CRT-2 config-deploy pre-registration: one nt_crt_monitor_state row
    per option — the artifact's PROMISED numbers (oos_ev/sd/t/n, train_end)
    plus the deploy-time gate thresholds, before any live evidence exists.
    The pre-registration precedes the evidence by construction (the claim
    discipline); fail-open, throttled to rebuilds.
    """
    if not (cfg.get("monitors") or {}).get("persist", False) or not _ENGINES:
        return
    try:
        from dataclasses import asdict

        from crt import state_store
        rows = []
        for (sym, tf), engine in _ENGINES.items():
            for opt in engine.options.values():
                rows.append(state_store.build_monitor_row(
                    opt, decay_cfg=engine.decay_cfg,
                    roll_cfg=asdict(engine.roll),
                    artifact_runid=_META.get("runid")))
        n = await state_store.upsert_monitor_rows(rows)
        if n:
            _MON_STATUS.update({"persist": True, "rows": n,
                                "last_flush": state_store.now_iso()})
            logger.info("crt.jobs: pre-registered %d CRT option row(s)", n)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.jobs: monitor pre-registration failed: %s", exc)


async def _flush_monitors_if_due(cfg: dict[str, Any], now: float) -> None:
    """Throttled monitor flush: upsert state rows when ANY option's
    signature changed (a decay/rolling transition) or the TTL elapsed —
    bounded writes, never per-tick chatter. Fail-open."""
    global _MON_FLUSH_TS, _MON_SIG
    mcfg = cfg.get("monitors") or {}
    if not mcfg.get("persist", False) or not _ENGINES:
        return
    try:
        from crt import state_store
        sig = {}
        for key, engine in _ENGINES.items():
            for opt in engine.options.values():
                sig[(key, opt.key)] = state_store.monitor_signature(opt)
        ttl = float(mcfg.get("flush_seconds", 3600))
        if sig == _MON_SIG and (now - _MON_FLUSH_TS) < ttl:
            return
        from dataclasses import asdict
        rows = []
        for (sym, tf), engine in _ENGINES.items():
            for opt in engine.options.values():
                rows.append(state_store.build_monitor_row(
                    opt, decay_cfg=engine.decay_cfg,
                    roll_cfg=asdict(engine.roll),
                    artifact_runid=_META.get("runid")))
        n = await state_store.upsert_monitor_rows(rows)
        if n:
            _MON_SIG, _MON_FLUSH_TS = sig, now
            _MON_STATUS.update({"persist": True, "rows": n,
                                "last_flush": state_store.now_iso()})
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.jobs: monitor flush failed: %s", exc)


async def _snapshot_rolling_if_new_day(cfg: dict[str, Any], now: float) -> None:
    """Daily rolling snapshot: one nt_crt_rolling_daily row per option per
    UTC day (idempotent upsert — fires on day change AND once per process
    start; the migration-163 daily-upsert discipline). Fail-open."""
    global _ROLL_DAY
    mcfg = cfg.get("monitors") or {}
    if not mcfg.get("rolling_daily", False) or not _ENGINES:
        return
    try:
        from crt import state_store
        day = state_store.utc_day(now)
        if _ROLL_DAY == day:
            return
        rows = []
        for (sym, tf), engine in _ENGINES.items():
            pool = state_store.tf_pool(list(engine.options.values()))
            for opt in engine.options.values():
                rows.append(state_store.build_rolling_row(
                    opt, day=day, tf_pool=pool,
                    open_positions=len(engine.positions)))
        n = await state_store.upsert_rolling_rows(rows)
        if n:
            _ROLL_DAY = day
            _MON_STATUS.update({"persist": True, "last_rolling_day": day})
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.warning("crt.jobs: rolling snapshot failed: %s", exc)


async def crt_engine_tick() -> dict[str, Any]:
    """The per-minute engine tick (registered as the crt-engine-tick job)."""
    from regime_platform.core.feature_flags import get_crt_mode

    mode = get_crt_mode()
    if mode == "off":
        return {"mode": "off"}          # dark: zero fetches, zero DB reads
    cfg = _load_engine_config()
    if not cfg.get("enabled", True):
        return {"mode": "disabled-by-config"}
    if _rebuild_needed(cfg) or not _ENGINES:
        _build_engines(cfg)
        await _preregister_monitors(cfg)   # CRT-2 deploy pre-registration
        if _ENGINES:
            await _signal("handshake", "CRT-1 engine online",
                          f"mode {mode} | {len(_ENGINES)} engine(s) | "
                          f"artifact {_META.get('runid') or 'n/a'} "
                          f"({(_META.get('rows') or 0)} rows, train_end "
                          f"{_META.get('train_end') or 'n/a'})")
    if not _ENGINES:
        return {"mode": mode, "engines": 0}

    stats = {"mode": mode, "engines": len(_ENGINES), "fills": 0, "resolved": 0}

    # CRT-3: the leaderboard-gated live rung (shadow passes through — the
    # verdict is surfaced at /crt/promotion + /crt/state either way)
    await _apply_promotion(mode, stats)

    now = time.time()
    tick_cfg = cfg.get("candles") or {}
    m15_bars = int(tick_cfg.get("m15_bars", 4800))
    h1_bars = int(tick_cfg.get("h1_bars", 1700))
    symbols = [e for e in (cfg.get("symbols") or [])
               if isinstance(e, dict) and e.get("symbol")]
    fetch_syms = []
    for entry in symbols:
        sym = str(entry["symbol"]).upper()
        wants = any((sym, tf) in _ENGINES for tf in ("M15", "M30", "H1"))
        if wants:
            fetch_syms.append({"symbol": sym,
                               "tradingview_symbol": str(entry.get("tradingview_symbol") or "")})
    if not fetch_syms:
        return stats

    from crt.candles import aggregate_m30, closed_bars, expected_closed_open

    def _need(sym: str, tf: str, tf_seconds: int) -> bool:
        expected = expected_closed_open(now, tf_seconds)
        return (sym, tf) in _ENGINES and _LAST_CLOSED.get((sym, tf), -1.0) < expected

    need_m15 = any(_need(e["symbol"], "M15", 900) or _need(e["symbol"], "M30", 1800)
                   for e in fetch_syms)
    need_h1 = any(_need(e["symbol"], "H1", 3600) for e in fetch_syms)
    if not (need_m15 or need_h1):
        stats["idle"] = True
        return stats

    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols

    ohlcv_15: dict[str, list[dict[str, Any]]] = {}
    ohlcv_1h: dict[str, list[dict[str, Any]]] = {}
    try:
        if need_m15:
            await fetch_candles_for_symbols(
                fetch_syms, m15_bars, "15m", include_timestamps=True,
                ohlcv_out=ohlcv_15)
        if need_h1:
            await fetch_candles_for_symbols(
                fetch_syms, h1_bars, "1h", include_timestamps=True,
                ohlcv_out=ohlcv_1h)
    except Exception as exc:  # noqa: BLE001 - fail-open
        logger.warning("crt.jobs: candle fetch failed: %s", exc)
        await _signal("error", "candle fetch failed", f"```\n{exc!r}\n```")
        return stats

    from crt import paper as crt_paper
    risk_state = await crt_paper.refresh_risk_state(now)

    for entry in fetch_syms:
        sym = str(entry["symbol"]).upper()
        raw15 = ohlcv_15.get(sym) or []
        raw1h = ohlcv_1h.get(sym) or []
        m15 = closed_bars(raw15, 900, now)
        m30 = aggregate_m30(m15)
        h1 = closed_bars(raw1h, 3600, now)
        for tf, bars in (("M15", m15), ("M30", m30), ("H1", h1)):
            engine = _ENGINES.get((sym, tf))
            if engine is None or not bars:
                continue
            try:
                tick = await engine.on_candles(bars, now, risk_state)
                stats["fills"] += tick.get("fills_emitted", 0)
                stats["resolved"] += tick.get("positions_resolved", 0)
                newest = bars[-1]["t"]
                prev = _LAST_CLOSED.get((sym, tf), -1.0)
                if newest > prev:
                    _LAST_CLOSED[(sym, tf)] = newest
            except Exception as exc:  # noqa: BLE001 - fail-open per engine
                logger.error("crt.jobs: %s %s tick failed: %s", sym, tf, exc)
                await _signal("error", f"{sym} {tf} engine tick failed",
                              f"```\n{exc!r}\n```")
    # CRT-2 persistence: throttled monitor flush + daily rolling snapshot
    await _flush_monitors_if_due(cfg, now)
    await _snapshot_rolling_if_new_day(cfg, now)
    _LAST_STATS.update({"ts": now, "engines": len(_ENGINES),
                        "fills": stats.get("fills", 0)})
    return stats


def register_crt_jobs(orchestrator: Any) -> None:
    """Register the CRT job family on the orchestrator's scheduler.

    Additive + guarded: a CRT failure can never break platform startup.
    Cadence: every ``tick_seconds`` (default 60) — boundary-gated inside, so
    the effective fetch cadence is the TF grid (15m/30m/1h).
    """
    cfg = _load_engine_config()
    tick_seconds = int((cfg.get("candles") or {}).get("tick_seconds", 60))
    scheduler = getattr(orchestrator, "_scheduler", None)
    if scheduler is None:
        logger.warning("crt.jobs: no scheduler on orchestrator — CRT stays dark")
        return
    scheduler.add_job(
        crt_engine_tick,
        trigger="interval",
        seconds=max(15, tick_seconds),
        id="crt-engine-tick",
        name="CRT engine tick (CRT-1, crt_mode-gated)",
        misfire_grace_time=30,
        coalesce=True,
        max_instances=1,
    )
    logger.info("crt.jobs: crt-engine-tick registered (every %ss, gated on "
                "crt_mode != off)", max(15, tick_seconds))


def engine_snapshots() -> dict[str, Any]:
    """The /crt router's state view."""
    return {
        "engines": [e.snapshot() for e in _ENGINES.values()],
        "artifact": _META,
        "last_tick": _LAST_STATS,
        "claims": {"kind": "crt_option",
                   "enabled": any(e.claims_enabled for e in _ENGINES.values())},
        "monitors": _MON_STATUS,
    }
