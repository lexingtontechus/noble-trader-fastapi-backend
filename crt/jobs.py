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
    """The engines' sync signal callback (spawns a task; never blocks)."""
    def fn(event: str, title: str, body: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_signal(event, title, body))
        except RuntimeError:
            _signal_sync(event, title, body)
    return fn


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
    global _ENGINES, _META
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
                  if k in ("chop", "sessions", "rolling", "decay", "sizing")}
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
    logger.info("crt.jobs: %d engine(s) from %s (%d option rows)",
                len(engines), artifact_path, len(options))


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
        if _ENGINES:
            await _signal("handshake", "CRT-1 engine online",
                          f"mode {mode} | {len(_ENGINES)} engine(s) | "
                          f"artifact {_META.get('runid') or 'n/a'} "
                          f"({(_META.get('rows') or 0)} rows, train_end "
                          f"{_META.get('train_end') or 'n/a'})")
    if not _ENGINES:
        return {"mode": mode, "engines": 0}

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
        return {"mode": mode, "engines": len(_ENGINES)}

    from crt.candles import aggregate_m30, closed_bars, expected_closed_open

    def _need(sym: str, tf: str, tf_seconds: int) -> bool:
        expected = expected_closed_open(now, tf_seconds)
        return (sym, tf) in _ENGINES and _LAST_CLOSED.get((sym, tf), -1.0) < expected

    need_m15 = any(_need(e["symbol"], "M15", 900) or _need(e["symbol"], "M30", 1800)
                   for e in fetch_syms)
    need_h1 = any(_need(e["symbol"], "H1", 3600) for e in fetch_syms)
    if not (need_m15 or need_h1):
        return {"mode": mode, "engines": len(_ENGINES), "idle": True}

    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols

    stats = {"mode": mode, "engines": len(_ENGINES), "fills": 0, "resolved": 0}
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
    }
