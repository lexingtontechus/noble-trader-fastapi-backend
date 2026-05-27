"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     Dynamic Regime Risk Management Platform - FastAPI Service  v7.2.0       ║
║                                                                              ║
║  Stack:  FastAPI · hmmlearn · scipy · numpy · pandas · pydantic v2          ║
║                                                                              ║
║  v2.x Endpoints:                                                             ║
║    POST /regime/detect      - HMM-based vol + trend regime classification    ║
║    POST /size/kelly         - Regime-gated fractional Kelly position sizing  ║
║    POST /risk/analyse       - VaR, CVaR, drawdown, stop/TP suggestions       ║
║    POST /analyse/full       - One-shot: all three in one request             ║
║    POST /simulate/{symbol}  - Markov-chain regime transition simulation      ║
║    GET  /portfolio           - Multi-symbol aggregated regime + risk          ║
║                                                                              ║
║  v3.0 additions:                                                             ║
║    GPU HMM       GET  /gpu/capabilities - pomegranate backend auto-select    ║
║                  POST /gpu/benchmark    - fit/predict latency benchmark      ║
║    Feed adapters POST /feeds/start      - Alpaca / Binance / IB live feeds   ║
║                  POST /feeds/stop       - stop all feed adapters             ║
║                  GET  /feeds/status     - bar counts and adapter health      ║
║    Correlation   POST /correlation/detect - DCC-based portfolio regime       ║
║    Optimisation  POST /optimise           - drawdown-controlled max-Sharpe   ║
║                  POST /optimise/full      - correlation + optimise one-shot  ║
║    Observation   POST /observation/build  - 24-feature obs vector (HMM)     ║
║    Streaming     POST /stream/seed        - seed historical prices           ║
║                  POST /stream/tick        - ingest single tick               ║
║                  GET  /stream/sessions    - list active sessions             ║
║                  WS   /ws/{symbol}        - WebSocket stream                 ║
║                  GET  /sse/{symbol}       - SSE stream                       ║
║                                                                              ║
║  v5.0 Renko HFT Pipeline:                                                    ║
║    POST /renko/tick         - Process single tick through 6-layer pipeline   ║
║    POST /renko/tick/batch   - Batch tick processing (backtest / warm-up)    ║
║    GET  /renko/state        - Current pipeline state                          ║
║    GET  /renko/stats        - Comprehensive pipeline statistics              ║
║    GET  /renko/bricks       - Recent Renko bricks                            ║
║    GET  /renko/classified   - Bricks with swing labels (HH/HL/LH/LL)       ║
║    GET  /renko/signals      - Pattern signals                               ║
║    GET  /renko/trades       - Trade journal                                 ║
║    GET  /renko/swing-points - Detected swing points                         ║
║    POST /renko/regime       - Update HMM regime for gate filter             ║
║    POST /renko/equity       - Update account equity                         ║
║    POST /renko/config       - Update pipeline configuration                 ║
║    POST /renko/reset        - Reset pipeline for a symbol                   ║
║    GET  /renko/backtest/stats - Backtest/trading statistics                 ║
║    POST /renko/backtest/run   - Run isolated Renko backtest                 ║
║    POST /renko/backtest/compare - Compare multiple Renko configs            ║
║    POST /renko/backtest/optimize - Renko parameter sweep (grid search)     ║
║                                                                              ║
║  v5.1 Dynamic Masaniello Pipeline:                                            ║
║    POST /size/masaniello        - Single-trade Masaniello sizing              ║
║    POST /size/masaniello/batch  - Batch Masaniello sizing                    ║
║    POST /optimise/masaniello    - Full two-stage: optimise + Masaniello       ║
║                                                                              ║
║  Auth: Clerk JWT (primary) → local JWT → API key fallback                   ║
║  MCP:   FastAPI-MCP with auth + operation filtering (read-only/analysis)    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
import os
from contextlib import asynccontextmanager

# Load .env file before any other imports that read env vars
from dotenv import load_dotenv
load_dotenv(override=False)  # Don't override already-set env vars (e.g. from Render dashboard)

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

try:
    from fastapi_mcp import FastApiMCP
    from fastapi_mcp.types import AuthConfig
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from regime_platform.auth.jwt_auth import get_authed_user
from regime_platform.auth.jwt_auth import TokenData
from regime_platform.core.gpu_engine import gpu_capabilities
from regime_platform.core.settings import settings
from regime_platform.middleware.rate_limit import limiter, RATE_LIMIT_DEFAULT, RATE_LIMIT_COMPUTE, RATE_LIMIT_WRITE

# v2.x routers
from regime_platform.routers import (
    auth_router,
    pipeline,
    portfolio,
    regime,
    risk,
    simulate,
    sizing,
)
# v3.0 routers
from regime_platform.routers import (
    feeds,
    gpu,
    multi_asset,
    observation,
    stream_rest,
    stream_ws,
)
# v4.0 Phase 1 routers
from regime_platform.routers import (
    backtest,
    strategy,
    tda,
)
# Phase 8: Operational Hardening
from regime_platform.routers import operational
# Phase 5: P&L Analytics (intraday, risk dashboard, CSV export, alerts)
from regime_platform.routers import pnl
# Phase 9: Runtime Configuration (admin-adjustable params)
from regime_platform.routers import config as config_router_module
from regime_platform.middleware.kill_switch_gate import KillSwitchGateMiddleware
# v5.0 Renko HFT pipeline
from renko.router import renko_router
from regime_platform.services.redis_persistence import persistence

log = logging.getLogger("regime.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up v7.2.0 ...")
    log.info(f"GPU: {gpu_capabilities()}")

    if persistence.enabled:
        # Start batched XADD flush loop (v7.3: reduces Redis commands ~90%)
        await persistence.start_flush_loop()

        symbols = await persistence.list_persisted_symbols()
        log.info(f"Redis: restoring {len(symbols)} symbol(s)")
        from regime_platform.services.registry import registry

        for symbol in symbols:
            meta = await persistence.load_meta(symbol)
            session = await registry.get_or_create(symbol=symbol, **meta)
            n = await persistence.restore(session)
            if n >= 81:
                import asyncio

                await asyncio.get_event_loop().run_in_executor(None, session._refit)
                log.info(f"  ✓ {symbol}: restored {n} bars")

    # Phase 8: Start operational services
    try:
        from regime_platform.services.operational import kill_switch, audit_logger, reconciler
        await kill_switch.start_refresh()
        log.info("Kill switch service started")

        # Start periodic reconciliation (every 15 minutes)
        import asyncio
        from regime_platform.services.operational.reconciler import reconciler as _reconciler

        async def _periodic_reconciliation():
            """Run reconciliation every 15 minutes."""
            while True:
                try:
                    await asyncio.sleep(900)  # 15 minutes
                    if not _reconciler.is_simulation:
                        results = await _reconciler.reconcile_all_recent()
                        if results:
                            matched = sum(1 for r in results if r.status.value == "MATCHED")
                            critical = sum(1 for r in results if r.status.value in ("MISSED_FILL", "ERROR"))
                            log.info(f"Periodic reconciliation: {len(results)} orders, {matched} matched, {critical} critical")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"Periodic reconciliation error: {e}")

        app.state._recon_task = asyncio.create_task(_periodic_reconciliation())
        log.info("Periodic reconciliation scheduler started (15-min interval)")
    except Exception as e:
        log.warning(f"Kill switch service start failed: {e}")

    # Phase 9: Register runtime config change callbacks
    try:
        from regime_platform.core.runtime_config import runtime_config
        from renko.router import _on_config_changed
        from regime_platform.services.registry import on_stream_config_changed
        runtime_config.register_on_change(_on_config_changed)
        runtime_config.register_on_change(on_stream_config_changed)
        log.info("Pipeline invalidation callbacks registered for runtime config changes (renko + stream)")
    except Exception as e:
        log.warning(f"Runtime config callback registration failed: {e}")

    yield

    # Phase 8: Stop operational services
    try:
        # Cancel periodic reconciliation task
        recon_task = getattr(app.state, '_recon_task', None)
        if recon_task:
            recon_task.cancel()
            try:
                await recon_task
            except asyncio.CancelledError:
                pass
            log.info("Periodic reconciliation task cancelled")

        from regime_platform.services.operational import kill_switch, audit_logger, reconciler, mode_manager
        await kill_switch.stop_refresh()
        await audit_logger.close()
        await reconciler.close()
        await mode_manager.close()
        log.info("Operational services stopped")
    except Exception as e:
        log.warning(f"Operational services shutdown error: {e}")

    # Close Discord notifier HTTP client
    try:
        from notifications.discord import discord_notifier
        await discord_notifier.close()
        log.info("Discord notifier closed")
    except ImportError:
        pass

    # v7.3: Close shared Redis connection pool
    try:
        from regime_platform.services.redis_pool import redis_pool
        await redis_pool.close()
        log.info("Shared Redis connection pool closed")
    except Exception as e:
        log.warning(f"Redis pool shutdown error: {e}")

    # v7.3: persistence.close() now also flushes remaining buffered prices
    await persistence.close()

    # Close Renko L1 Redis cache
    try:
        from renko.redis_cache import renko_cache
        await renko_cache.close()
        log.info("Renko L1 Redis cache closed")
    except Exception as e:
        log.warning(f"Renko L1 cache shutdown error: {e}")

    # Close runtime config HTTP client
    try:
        from regime_platform.core.runtime_config import runtime_config
        await runtime_config.close()
        log.info("Runtime config client closed")
    except Exception as e:
        log.warning(f"Runtime config shutdown error: {e}")

    from regime_platform.routers.feeds import _manager

    if _manager:
        await _manager.stop()
    log.info("Shutdown complete")


# ─── App Factory ──────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(
        title="Noble Trader Dynamic Regime Risk Management Platform",
        description="""
## Overview

A quantitative trading risk management web service built on:

| Component | Library | Purpose |
|-----------|---------|---------|
| **Regime Detection** | `hmmlearn` GaussianHMM | Classify market into volatility x trend regimes |
| **Position Sizing** | `scipy` + Kelly formula | Fractional Kelly with vol-scaling & regime gating |
| **Risk Analysis** | `numpy` / `scipy.stats` | VaR, CVaR, drawdown, Sortino, Calmar |
| **Correlation** | `hmmlearn` + DCC | Portfolio-level correlation regime detection |
| **Optimisation** | `scipy.optimize` | Drawdown-controlled max-Sharpe portfolio |
| **Dynamic Masaniello** | Adaptive batch sizing | Progress-dependent position sizing with regime quality overlay |
| **GPU HMM** | `pomegranate` (optional) | 10-50x HMM speedup on CUDA/MPS |
| **API Framework** | `FastAPI` + `pydantic v2` | Typed, validated, auto-documented REST API |

## Regime Labels

The HMM produces a **2D regime** combining:
- **Volatility**: `low` / `med_low` / `med_high` / `high`
- **Trend**: `strong_bear` / `bear` / `bull` / `strong_bull`

Combined label example: `low_vol_bull`, `high_vol_bear`, etc.

## Risk Multiplier

Each regime maps to a **risk_multiplier** [0.10 – 1.75] that scales the Kelly fraction:
- `low_vol_strong_bull` -> 1.75x (max risk-on)
- `high_vol_strong_bear` -> 0.14x (max defensive)

## Authentication

Clerk JWT (primary) -> local JWT -> API key fallback.

## Recommended Workflow

```
POST /analyse/full  ->  regime + sizing + risk in one call
POST /correlation/detect  ->  portfolio-level correlation regime
POST /optimise/full  ->  correlation + optimisation one-shot
POST /optimise/masaniello  ->  portfolio optimise + per-asset Masaniello sizing
```

## Dynamic Masaniello (v5.1)

Two-stage pipeline connecting portfolio optimisation to per-trade position sizing:

1. **DrawdownOptimiser** → regime-adjusted portfolio weights (what to hold)
2. **DynamicMasanielloSizer** → per-asset contract sizes (how much to hold)

Formula: `f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i`

- M_i = urgency (wins needed / trades left)
- Q_i = quality (prob × regime × confidence)
- DD_i = drawdown scaling (floor 0.25)
- V_i = volatility adjustment (ATR-based)

app = FastAPI(
    title="Noble Trader Dynamic Regime Risk Management Platform",
    description="""
... (truncated for brevity) ...
    """,
    version="7.2.0",
    contact={
        "name": "Noble Trader Dynamic Regime Risk Platform",
        "email": "nobletrader@0xdweb.com",
    },
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error(f"FATAL ERROR during request {request.method} {request.url}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "detail": str(exc), "type": exc.__class__.__name__},
    )

    # CORS - restricted to configured origins (never allow_origins=["*"] in production)
    cors_origins = [
        o.strip()
        for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]
    _deploy_env = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
    if _deploy_env in ("production", "staging") and "*" in cors_origins:
        log.error("FATAL: CORS wildcard not allowed in production/staging. Set CORS_ALLOWED_ORIGINS explicitly.")
        raise RuntimeError("CORS wildcard not allowed in production/staging")
    log.info(f"CORS allowed origins: {cors_origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # v2.x routers
    for r in [
        auth_router,
        regime,
        sizing,
        risk,
        pipeline,
        stream_rest,
        stream_ws,
        simulate,
        portfolio,
    ]:
        app.include_router(r.router)

    # v3.0 routers
    app.include_router(gpu.router)
    app.include_router(feeds.router)
    app.include_router(multi_asset.router)
    app.include_router(observation.router)

    # v4.0 Phase 1 routers
    app.include_router(strategy.router)
    app.include_router(backtest.router)
    app.include_router(tda.router)

    # v5.0 Renko HFT pipeline (14 endpoints under /renko/*)
    if renko_router is not None:
        app.include_router(renko_router)

    # Phase 8: Operational Hardening (13 endpoints under /operational/*)
    app.include_router(operational.router)

    # Phase 5: P&L Analytics (intraday, risk dashboard, CSV export, alerts)
    app.include_router(pnl.router)

    # Phase 9: Runtime Configuration (admin-adjustable params)
    app.include_router(config_router_module.config_router)

    # Kill Switch Gate Middleware (blocks order execution when halted)
    app.add_middleware(KillSwitchGateMiddleware)

    # ─── MCP (Model Context Protocol) ──────────────────────────────────────────
    # Expose READ-ONLY / analysis FastAPI endpoints as MCP tools so AI agents
    # can discover and call them via the MCP protocol at /mcp.
    #
    # Security measures:
    #   1. AuthConfig with get_authed_user dependency - MCP endpoint requires
    #      JWT / API-key auth; unauthenticated connections get 401
    #   2. exclude_operations - dangerous write / infrastructure / auth-circular
    #      endpoints are NOT exposed as MCP tools
    #   3. headers=["authorization", "x-api-key"] - forwarded to REST calls so
    #      per-route Depends() auth is also enforced
    if MCP_AVAILABLE:
        # Operation IDs to EXCLUDE from MCP exposure
        #
        # v7.1 - Full agent autonomy: MCP agents can now warmup, restore,
        # set regime, set equity, run backtests, switch sizing models,
        # and toggle live execution. Only genuinely dangerous or
        # infrastructure-only operations remain excluded.
        #
        # Promoted to MCP (v7.1):
        #   - process_batch: agent needs warmup & backtest
        #   - set_regime: agent needs HMM regime sync
        #   - set_equity: agent needs portfolio sync
        #   - warmup_pipeline: agent needs autonomous cold-start
        #   - save_snapshot: agent needs to persist state
        #   - restore_snapshot: agent needs cold-start recovery
        #   - set_sizing_model: agent needs Kelly ↔ Masaniello switching
        #   - toggle_live: agent needs execution mode control
        #   - run_backtest / optimize: agent needs strategy testing
        _mcp_excluded_ops = [
            # ── Auth (circular - MCP clients should already have tokens) ──
            "clerk_config_auth_clerk_config_get",
            "clerk_me_auth_clerk_me_get",
            "verify_clerk_token_endpoint_auth_clerk_verify_post",
            "me_auth_me_get",
            "refresh_auth_refresh_post",
            "login_auth_token_post",
            # ── Feed adapters (infrastructure control) ──
            "start_feeds_feeds_start_post",
            "stop_feeds_feeds_stop_post",
            # ── Single tick injection (manipulation risk - use batch/warmup instead) ──
            "process_tick_renko_tick_post",
            # ── Pipeline reset (destructive - loses all brick history & trades) ──
            "reset_pipeline_renko_reset_post",
            # ── Config update (destructive - rebuilds pipeline from scratch) ──
            "update_config_renko_config_post",
            # ── Stream data injection / teardown ──
            "seed_session_stream_seed_post",
            "ingest_tick_stream_tick_post",
            "ingest_ticks_stream_ticks_post",
            "remove_session_stream_session__symbol__delete",
            # ── Backtest delete (destructive - MCP should run/read, not delete) ──
            "delete_backtest_result_backtest__backtest_id__delete",
        ]

        # Build AuthConfig: require JWT / API-key on the MCP endpoint itself
        # Only include issuer if MCP_ISSUER is set (avoids empty-string validation issues)
        if settings.MCP_AUTH_ENABLED:
            _mcp_auth_kwargs = dict(dependencies=[Depends(get_authed_user)])
            _mcp_issuer = settings.MCP_ISSUER
            if _mcp_issuer:
                _mcp_auth_kwargs["issuer"] = _mcp_issuer
            _mcp_auth = AuthConfig(**_mcp_auth_kwargs)
        else:
            _mcp_auth = None

        mcp = FastApiMCP(
            app,
            name="Noble Trader MCP",
            description="""Noble Trader Dynamic Regime Risk Management Platform - MCP Server

Exposes trading risk management endpoints as MCP tools for full agent autonomy:
- Regime detection (HMM-based vol + trend classification)
- Position sizing (regime-gated fractional Kelly + Dynamic Masaniello)
- Risk analysis (VaR, CVaR, drawdown, stop/TP suggestions)
- Full analysis pipeline (regime + sizing + risk in one call)
- Monte Carlo simulation (Markov-chain regime transitions)
- Portfolio aggregation (multi-symbol regime + risk)
- Correlation detection (DCC-based portfolio regime)
- Portfolio optimisation (drawdown-controlled max-Sharpe)
- Dynamic Masaniello two-stage pipeline
- Observation building (24-feature HMM observation vector)
- GPU HMM capabilities and benchmarks
- Backtesting (walk-forward HMM with 30+ metrics), backtest history, comparison
- Strategy signals, TDA features
- Renko pipeline: full autonomy (warmup, restore, regime, equity, sizing, backtest, live toggle)
- Snapshot management (save/restore pipeline state)

Agent autonomy (v7.2):
- POST /renko/warmup - autonomous pipeline warmup (Yahoo fetch + snapshot save)
- POST /renko/snapshot/save - persist pipeline state (Redis L1 + Supabase L2 dual-write)
- POST /renko/snapshot/restore - recover from cold start (Redis L1 → Supabase L2 tiered restore)
- POST /renko/regime - update HMM regime (auto-saves on change)
- POST /renko/equity - update account equity (auto-saves on change)
- POST /renko/tick/batch - batch tick processing (warmup/backtest)
- POST /renko/sizing/model - switch Kelly ↔ Masaniello (auto-saves on change)
- POST /renko/live/toggle - toggle live execution (auto-saves on change)
- POST /renko/backtest/run - run isolated backtests
- POST /renko/backtest/optimize - parameter sweep
- Auto-save on: position open/close, regime/equity change, sizing/toggle change, every 100th tick

Excluded from MCP (use REST API directly):
- Auth endpoints, feed start/stop, single tick injection, pipeline reset, config update""",
            exclude_operations=_mcp_excluded_ops,
            auth_config=_mcp_auth,
            headers=["authorization", "x-api-key"],
        )
        mcp.mount_http()
        _tool_count = len(mcp.tools)
        _excluded_count = len(_mcp_excluded_ops)
        _auth_status = "enabled" if _mcp_auth else "DISABLED (dev mode)"
        log.info(
            f"MCP server mounted at /mcp - "
            f"{_tool_count} tools exposed, {_excluded_count} excluded, "
            f"auth {_auth_status}"
        )
    else:
        log.warning("fastapi-mcp not installed - MCP endpoint disabled")

    @app.get("/", include_in_schema=False)
    async def root():
        caps = gpu_capabilities()
        return JSONResponse(
            {
                "service": "Noble Trader Dynamic Regime Risk Management Platform",
                "version": "7.2.0",
                "docs": "/docs",
                "mcp": "/mcp" if MCP_AVAILABLE else "not installed",
                "gpu": caps,
                "endpoints": {
                    "regime_detect": "POST /regime/detect",
                    "regime_detect_v2": "POST /regime/detect-v2",
                    "kelly_sizing": "POST /size/kelly",
                    "risk_analysis": "POST /risk/analyse",
                    "risk_analyze": "POST /risk/analyze",
                    "full_pipeline": "POST /analyse/full",
                    "strategy_signal": "POST /strategy/signal",
                    "backtest_run": "POST /backtest/run",
                    "tda_features": "POST /tda/features",
                    "observation_build": "POST /observation/build",
                    "observation_build_v2": "POST /observation/build-v2",
                    "simulate": "POST /simulate/{symbol}",
                    "portfolio": "GET /portfolio",
                    "correlation_detect": "POST /correlation/detect",
                    "optimise": "POST /optimise",
                    "optimise_full": "POST /optimise/full",
                    "gpu_capabilities": "GET /gpu/capabilities",
                    "gpu_benchmark": "POST /gpu/benchmark",
                    "feeds_start": "POST /feeds/start",
                    "feeds_status": "GET /feeds/status",
                    "stream_seed": "POST /stream/seed",
                    "stream_sessions": "GET /stream/sessions",
                    "mcp_server": "GET /mcp",
                    # Renko HFT
                    "renko_tick": "POST /renko/tick",
                    "renko_state": "GET /renko/state",
                    "renko_bricks": "GET /renko/bricks",
                    "renko_signals": "GET /renko/signals",
                    "renko_trades": "GET /renko/trades",
                    "renko_config": "POST /renko/config",
                    "renko_warmup": "POST /renko/warmup",
                    "renko_snapshot_save": "POST /renko/snapshot/save",
                    "renko_snapshot_restore": "POST /renko/snapshot/restore",
                    "renko_sizing_model": "GET/POST /renko/sizing/model",
                    "renko_live_toggle": "POST /renko/live/toggle",
                    "renko_backtest_run": "POST /renko/backtest/run",
                    "renko_backtest_compare": "POST /renko/backtest/compare",
                    "renko_backtest_optimize": "POST /renko/backtest/optimize",
                    # v5.1 Dynamic Masaniello
                    "masaniello_sizing": "POST /size/masaniello",
                    "masaniello_batch": "POST /size/masaniello/batch",
                    "portfolio_masaniello": "POST /optimise/masaniello",
                    # Phase 9: Runtime Configuration
                    "config_list": "GET /config/",
                    "config_category": "GET /config/{category}",
                    "config_update": "PATCH /config/key/{key}",
                    "config_reload": "GET /config/reload",
                },
            }
        )

    @app.get("/health", tags=["Health"])
    async def health():
        from regime_platform.services.registry import registry

        symbols = await registry.list_symbols()
        caps = gpu_capabilities()

        # Phase 8: Operational status
        operational_status = {"kill_switch": "unknown", "trading_mode": "unknown"}
        try:
            from regime_platform.services.operational import kill_switch, mode_manager
            is_halted = await kill_switch.is_halted()
            current_mode = await mode_manager.get_current_mode()
            operational_status = {
                "kill_switch": "HALTED" if is_halted else "clear",
                "trading_mode": current_mode.value,
            }
        except Exception:
            pass

        # Discord notification status
        discord_status = {"configured": False, "channels": {}}
        try:
            from notifications.discord import discord_notifier
            discord_status["configured"] = discord_notifier.is_configured
            discord_status["channels"] = {
                "signals": bool(settings.DISCORD_WEBHOOK_SIGNALS),
                "executions": bool(settings.DISCORD_WEBHOOK_EXECUTIONS),
                "status": bool(settings.DISCORD_WEBHOOK_STATUS),
            }
        except ImportError:
            pass

        # MCP security status
        mcp_status = {"available": MCP_AVAILABLE}
        if MCP_AVAILABLE:
            mcp_status["endpoint"] = "/mcp"
            mcp_status["auth"] = "enabled" if settings.MCP_AUTH_ENABLED else "disabled"
            mcp_status["header_forwarding"] = ["authorization", "x-api-key"]

        # Circuit breaker status
        from regime_platform.core.circuit_breaker import all_circuit_stats
        circuit_status = all_circuit_stats()

        # Alpaca stream manager status
        alpaca_stream_status = {"active": False, "users": 0}
        try:
            from regime_platform.services.alpaca_stream import alpaca_stream_manager
            mgr_status = alpaca_stream_manager.status()
            alpaca_stream_status = {
                "active": True,
                "users": mgr_status.get("active_users", 0),
                "details": mgr_status.get("users", []),
            }
        except Exception:
            pass

        return {
            "status": "ok",
            "version": "7.2.0",
            "sessions": len(symbols),
            "redis": persistence.enabled,
            "operational": operational_status,
            "cors_origins": settings.CORS_ORIGINS,
            "discord": discord_status,
            "mcp": mcp_status,
            "circuit_breakers": circuit_status,
            "alpaca_stream": alpaca_stream_status,
            "gpu_backend": caps["hmm_backend"],
            "device": caps["active_device"],
            "rate_limits": {
                "default": RATE_LIMIT_DEFAULT,
                "compute": RATE_LIMIT_COMPUTE,
                "write": RATE_LIMIT_WRITE,
            },
        }

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
