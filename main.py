"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     Dynamic Regime Risk Management Platform — FastAPI Service  v3.0.0       ║
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
║                                                                              ║
║  Auth: Clerk JWT (primary) → local JWT → API key fallback                   ║
║  MCP:   FastAPI-MCP exposes all endpoints as Model Context Protocol tools   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from fastapi_mcp import FastApiMCP
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from regime_platform.core.gpu_engine import gpu_capabilities

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
# v5.0 Renko HFT pipeline
from renko.router import renko_router
from regime_platform.services.redis_persistence import persistence

log = logging.getLogger("regime.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up v5.0.0 ...")
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

    yield

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
```
        """,
        version="5.0.0",
        contact={
            "name": "Noble Trader Dynamic Regime Risk Platform",
            "email": "nobletrader@0xdweb.com",
        },
        license_info={"name": "MIT"},
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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

    # ─── MCP (Model Context Protocol) ──────────────────────────────────────────
    # Expose all FastAPI endpoints as MCP tools so AI agents can discover
    # and call them via the MCP protocol at /mcp
    if MCP_AVAILABLE:
        mcp = FastApiMCP(
            app,
            name="Noble Trader MCP",
            description="""Noble Trader Dynamic Regime Risk Management Platform — MCP Server

Exposes all trading risk management endpoints as MCP tools:
- Regime detection (HMM-based vol + trend classification)
- Position sizing (regime-gated fractional Kelly)
- Risk analysis (VaR, CVaR, drawdown, stop/TP suggestions)
- Full analysis pipeline (regime + sizing + risk in one call)
- Monte Carlo simulation (Markov-chain regime transitions)
- Portfolio aggregation (multi-symbol regime + risk)
- Correlation detection (DCC-based portfolio regime)
- Portfolio optimisation (drawdown-controlled max-Sharpe)
- Observation building (24-feature HMM observation vector)
- GPU HMM capabilities and benchmarks""",
        )
        mcp.mount_http()
        log.info("MCP server mounted at /mcp")
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
                },
            }
        )

    @app.get("/health", tags=["Health"])
    async def health():
        from regime_platform.services.registry import registry

        symbols = await registry.list_symbols()
        caps = gpu_capabilities()
        return {
            "status": "ok",
            "version": "5.0.0",
            "sessions": len(symbols),
            "redis": persistence.enabled,
            "gpu_backend": caps["hmm_backend"],
            "device": caps["active_device"],
        }

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
