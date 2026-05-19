"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     Dynamic Regime Risk Management Platform — FastAPI Service  v5.1.0       ║
║                                                                              ║
║  Stack:  FastAPI · hmmlearn · scipy · numpy · pandas · pydantic v2          ║
║                                                                              ║
║  v2.x Endpoints:                                                             ║
║    POST /regime/detect      — HMM-based vol + trend regime classification    ║
║    POST /size/kelly         — Regime-gated fractional Kelly position sizing  ║
║    POST /risk/analyse       — VaR, CVaR, drawdown, stop/TP suggestions       ║
║    POST /analyse/full       — One-shot: all three in one request             ║
║    POST /simulate/{symbol}  — Markov-chain regime transition simulation      ║
║    GET  /portfolio           — Multi-symbol aggregated regime + risk          ║
║                                                                              ║
║  v3.0 additions:                                                             ║
║    GPU HMM       GET  /gpu/capabilities — pomegranate backend auto-select    ║
║                  POST /gpu/benchmark    — fit/predict latency benchmark      ║
║    Feed adapters POST /feeds/start      — Alpaca / Binance / IB live feeds   ║
║                  POST /feeds/stop       — stop all feed adapters             ║
║                  GET  /feeds/status     — bar counts and adapter health      ║
║    Correlation   POST /correlation/detect — DCC-based portfolio regime       ║
║    Optimisation  POST /optimise           — drawdown-controlled max-Sharpe   ║
║                  POST /optimise/full      — correlation + optimise one-shot  ║
║    Observation   POST /observation/build  — 24-feature obs vector (HMM)     ║
║    Streaming     POST /stream/seed        — seed historical prices           ║
║                  POST /stream/tick        — ingest single tick               ║
║                  GET  /stream/sessions    — list active sessions             ║
║                  WS   /ws/{symbol}        — WebSocket stream                 ║
║                  GET  /sse/{symbol}       — SSE stream                       ║
║                                                                              ║
║  v5.0 Renko HFT Pipeline:                                                    ║
║    POST /renko/tick         — Process single tick through 6-layer pipeline   ║
║    POST /renko/tick/batch   — Batch tick processing (backtest / warm-up)    ║
║    GET  /renko/state        — Current pipeline state                          ║
║    GET  /renko/stats        — Comprehensive pipeline statistics              ║
║    GET  /renko/bricks       — Recent Renko bricks                            ║
║    GET  /renko/classified   — Bricks with swing labels (HH/HL/LH/LL)       ║
║    GET  /renko/signals      — Pattern signals                               ║
║    GET  /renko/trades       — Trade journal                                 ║
║    GET  /renko/swing-points — Detected swing points                         ║
║    POST /renko/regime       — Update HMM regime for gate filter             ║
║    POST /renko/equity       — Update account equity                         ║
║    POST /renko/config       — Update pipeline configuration                 ║
║    POST /renko/reset        — Reset pipeline for a symbol                   ║
║    GET  /renko/backtest/stats — Backtest/trading statistics                 ║
║    POST /renko/backtest/run   — Run isolated Renko backtest                 ║
║    POST /renko/backtest/compare — Compare multiple Renko configs            ║
║    POST /renko/backtest/optimize — Renko parameter sweep (grid search)     ║
║                                                                              ║
║  v5.1 Dynamic Masaniello Pipeline:                                            ║
║    POST /size/masaniello        — Single-trade Masaniello sizing              ║
║    POST /size/masaniello/batch  — Batch Masaniello sizing                    ║
║    POST /optimise/masaniello    — Full two-stage: optimise + Masaniello       ║
║                                                                              ║
║  Auth: Clerk JWT (primary) → local JWT → API key fallback                   ║
║  MCP:   FastAPI-MCP with auth + operation filtering (read-only/analysis)    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
from contextlib import asynccontextmanager

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
from regime_platform.middleware.kill_switch_gate import KillSwitchGateMiddleware
# v5.0 Renko HFT pipeline
from renko.router import renko_router
from regime_platform.services.redis_persistence import persistence

log = logging.getLogger("regime.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up v5.1.0 ...")
    log.info(f"GPU: {gpu_capabilities()}")

    if persistence.enabled:
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

    await persistence.close()
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
- **Volatility**: `low` / `medium` / `high`
- **Trend**: `bear` / `neutral` / `bull`

Combined label example: `low_vol_bull`, `high_vol_bear`, etc.

## Risk Multiplier

Each regime maps to a **risk_multiplier** [0.25 - 1.50] that scales the Kelly fraction:
- `low_vol_bull` -> 1.5x (max risk-on)
- `high_vol_bear` -> 0.25x (max defensive)

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

Endpoints:
- `POST /size/masaniello` — single-trade Masaniello sizing
- `POST /size/masaniello/batch` — batch Masaniello sizing
- `POST /optimise/masaniello` — full two-stage pipeline
        """,
        version="5.0.0",
        contact={
            "name": "Noble Trader Dynamic Regime Risk Platform",
            "email": "nobletrader@0xdweb.com",
        },
        license_info={"name": "MIT"},
        lifespan=lifespan,
    )

    # ── Rate Limiting (SlowAPI) ───────────────────────────────────────────────
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from fastapi import Request as _Request

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── Global Exception Handlers ────────────────────────────────────────────
    # Proper HTTP status codes instead of default 422 for everything

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Return 422 for genuine validation errors with structured detail."""
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(), "body": exc.body},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        """Return 400 for bad input values (invalid params, out-of-range, etc.)."""
        log.warning(f"400 Bad Request: {request.method} {request.url.path} — {exc}")
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc)},
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError):
        """Return 400 for missing required fields."""
        log.warning(f"400 Bad Request: {request.method} {request.url.path} — missing key: {exc}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Missing required field: {exc}"},
        )

    @app.exception_handler(FileNotFoundError)
    async def not_found_error_handler(request: Request, exc: FileNotFoundError):
        """Return 404 for resource not found."""
        log.warning(f"404 Not Found: {request.method} {request.url.path} — {exc}")
        return JSONResponse(
            status_code=404,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ConnectionError)
    async def connection_error_handler(request: Request, exc: ConnectionError):
        """Return 502 for upstream/downstream service failures."""
        log.error(f"502 Bad Gateway: {request.method} {request.url.path} — {exc}")
        return JSONResponse(
            status_code=502,
            content={"detail": f"Upstream service error: {str(exc)}"},
        )

    @app.exception_handler(TimeoutError)
    async def timeout_error_handler(request: Request, exc: TimeoutError):
        """Return 504 for upstream timeouts."""
        log.error(f"504 Gateway Timeout: {request.method} {request.url.path} — {exc}")
        return JSONResponse(
            status_code=504,
            content={"detail": "Upstream service timeout"},
        )

    @app.exception_handler(ImportError)
    async def import_error_handler(request: Request, exc: ImportError):
        """Return 503 when an optional dependency is missing."""
        log.error(f"503 Service Unavailable: {request.method} {request.url.path} — {exc}")
        return JSONResponse(
            status_code=503,
            content={"detail": f"Required dependency not available: {str(exc)}"},
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        """Catch-all: return 500 for unexpected errors (never leak tracebacks)."""
        log.exception(f"500 Internal Server Error: {request.method} {request.url.path}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # CORS — locked to specific origins via env var
    # CORS_ORIGINS="https://your-app.vercel.app,http://localhost:3000"
    # Falls back to localhost-only in development when not set.
    _cors_origins = settings.cors_origins_list
    if not _cors_origins:
        # Dev fallback: only localhost
        _cors_origins = [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    log.info(f"CORS origins: {_cors_origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type", "Accept"],
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

    # Kill Switch Gate Middleware (blocks order execution when halted)
    app.add_middleware(KillSwitchGateMiddleware)

    # ─── MCP (Model Context Protocol) ──────────────────────────────────────────
    # Expose READ-ONLY / analysis FastAPI endpoints as MCP tools so AI agents
    # can discover and call them via the MCP protocol at /mcp.
    #
    # Security measures:
    #   1. AuthConfig with get_authed_user dependency — MCP endpoint requires
    #      JWT / API-key auth; unauthenticated connections get 401
    #   2. exclude_operations — dangerous write / infrastructure / auth-circular
    #      endpoints are NOT exposed as MCP tools
    #   3. headers=["authorization", "x-api-key"] — forwarded to REST calls so
    #      per-route Depends() auth is also enforced
    if MCP_AVAILABLE:
        # Operation IDs to EXCLUDE from MCP exposure
        # Rationale:
        #   - Auth endpoints: circular (MCP shouldn't issue/refresh tokens)
        #   - Feed start/stop: infrastructure control (not for AI agents)
        #   - Renko write ops: pipeline mutation (tick, batch, reset, config, equity, regime)
        #   - Stream seed/tick: data injection endpoints
        #   - Stream session delete: resource teardown
        _mcp_excluded_ops = [
            # ── Auth (circular — MCP clients should already have tokens) ──
            "clerk_config_auth_clerk_config_get",
            "clerk_me_auth_clerk_me_get",
            "verify_clerk_token_endpoint_auth_clerk_verify_post",
            "me_auth_me_get",
            "refresh_auth_refresh_post",
            "login_auth_token_post",
            # ── Feed adapters (infrastructure control) ──
            "start_feeds_feeds_start_post",
            "stop_feeds_feeds_stop_post",
            # ── Renko write / mutation (pipeline manipulation) ──
            "process_tick_renko_tick_post",
            "process_batch_renko_tick_batch_post",
            "update_config_renko_config_post",
            "set_equity_renko_equity_post",
            "set_regime_renko_regime_post",
            "reset_pipeline_renko_reset_post",
            # ── Stream data injection / teardown ──
            "seed_session_stream_seed_post",
            "ingest_tick_stream_tick_post",
            "ingest_ticks_stream_ticks_post",
            "remove_session_stream_session__symbol__delete",
            # ── Backtest delete (destructive — MCP should run, read, compare, not delete) ──
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
            description="""Noble Trader Dynamic Regime Risk Management Platform — MCP Server

Exposes trading risk management endpoints as MCP tools (read-only / analysis):
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
- Renko read-only: state, stats, bricks, signals, trades, swing-points

Excluded from MCP (use REST API directly):
- Auth endpoints, feed start/stop, Renko write ops, stream data injection, backtest delete""",
            exclude_operations=_mcp_excluded_ops,
            auth_config=_mcp_auth,
            headers=["authorization", "x-api-key"],
        )
        mcp.mount_http()
        _tool_count = len(mcp.tools)
        _excluded_count = len(_mcp_excluded_ops)
        _auth_status = "enabled" if _mcp_auth else "DISABLED (dev mode)"
        log.info(
            f"MCP server mounted at /mcp — "
            f"{_tool_count} tools exposed, {_excluded_count} excluded, "
            f"auth {_auth_status}"
        )
    else:
        log.warning("fastapi-mcp not installed — MCP endpoint disabled")

    @app.get("/", include_in_schema=False)
    async def root():
        caps = gpu_capabilities()
        return JSONResponse(
            {
                "service": "Noble Trader Dynamic Regime Risk Management Platform",
                "version": "5.0.0",
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
                    "renko_backtest_run": "POST /renko/backtest/run",
                    "renko_backtest_compare": "POST /renko/backtest/compare",
                    "renko_backtest_optimize": "POST /renko/backtest/optimize",
                    # v5.1 Dynamic Masaniello
                    "masaniello_sizing": "POST /size/masaniello",
                    "masaniello_batch": "POST /size/masaniello/batch",
                    "portfolio_masaniello": "POST /optimise/masaniello",
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

        return {
            "status": "ok",
            "version": "5.4.0",
            "sessions": len(symbols),
            "redis": persistence.enabled,
            "operational": operational_status,
            "cors_origins": settings.CORS_ORIGINS,
            "discord": discord_status,
            "mcp": mcp_status,
            "circuit_breakers": circuit_status,
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
