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
║  Auth: Clerk JWT (primary) -> local JWT -> API key fallback                   ║
║  MCP:   FastAPI-MCP with auth + operation filtering (read-only/analysis)    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import logging
import os
from contextlib import asynccontextmanager

# urllib3<2.0 compatibility shim for alpaca-trade-api
import urllib3_compat  # noqa: F401 -- side-effect shim

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
# Phase 10: TimesFM Calibration Loop + Venue Management
from regime_platform.routers import calibration, venue
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

    # Phase 10: Start L2 order book feeds
    try:
        l2_enabled = await runtime_config.get_bool("l2.enabled", default=True)
        if l2_enabled:
            from regime_platform.services.l2_feed_manager import L2FeedManager
            app.state.l2_feed_manager = L2FeedManager(enabled=True)

            # Subscribe to symbols from the pipeline registry
            symbols = list(getattr(app.state, '_registered_symbols', []))
            if not symbols:
                # Fallback: core symbols from Renko pipeline defaults
                from regime_platform.core.runtime_config import sync_config
                default_sym = sync_config.get_str("renko.default_symbol", "SPY")
                symbols = [default_sym, "QQQ", "BTC", "ETH"]

            await app.state.l2_feed_manager.start(symbols)
            log.info("L2 feed manager started for %d symbol(s): %s", len(symbols), symbols)
        else:
            app.state.l2_feed_manager = None
            log.info("L2 feed manager disabled (l2.enabled=false)")
    except Exception as e:
        log.warning(f"L2 feed manager start failed: {e}")
        app.state.l2_feed_manager = None

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

    # Phase 10: Stop L2 order book feeds
    l2_mgr = getattr(app.state, 'l2_feed_manager', None)
    if l2_mgr:
        try:
            await l2_mgr.stop()
            log.info("L2 feed manager stopped")
        except Exception as e:
            log.warning(f"L2 feed manager stop error: {e}")

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

    for r in [
            auth_router,
            regime,
            sizing,
            risk,
            pipeline,
            stream_rest,
            stream_ws,
            simulate,
            portfolio
    ]:
        app.include_router(r.router)

    app.include_router(gpu.router)
    app.include_router(feeds.router)
    app.include_router(multi_asset.router)
    app.include_router(observation.router)
    app.include_router(strategy.router)
    app.include_router(backtest.router)
    app.include_router(tda.router)

    if renko_router is not None:
        app.include_router(renko_router)

    app.include_router(operational.router)
    app.include_router(pnl.router)
    app.include_router(config_router_module.config_router)
    app.include_router(calibration.router)
    app.include_router(venue.router)

    app.add_middleware(KillSwitchGateMiddleware)

    @app.get("/health", summary="Health check")
    async def health():
        """Return system health status including Renko pipeline mode."""
        from renko.router import _pipelines
        from regime_platform.core.settings import settings
        
        renko_status = {}
        for sym, pipe in _pipelines.items():
            renko_status[sym] = {
                "execution_mode": pipe.executor.execution_mode if hasattr(pipe, 'executor') else "unknown",
                "dry_run": getattr(pipe.executor, 'dry_run', True),
                "live_enabled": getattr(pipe.executor, 'live_enabled', False),
            }
        
        return {
            "status": "ok",
            "version": "7.2.0",
            "mode": "paper" if settings.ALPACA_API_KEY else "live",
            "renko_pipelines": renko_status,
        }

    if MCP_AVAILABLE:
        _mcp_excluded_ops = [
            "clerk_config_auth_clerk_config_get",
            "clerk_me_auth_clerk_me_get",
            "verify_clerk_token_endpoint_auth_clerk_verify_post",
            "me_auth_me_get",
            "refresh_auth_refresh_post",
            "login_auth_token_post",
            "start_feeds_feeds_start_post",
            "stop_feeds_feeds_stop_post",
            "process_tick_renko_tick_post",
            "reset_pipeline_renko_reset_post",
            "update_config_renko_config_post",
            "seed_session_stream_seed_post",
            "ingest_tick_stream_tick_post",
            "ingest_ticks_stream_ticks_post",
            "remove_session_stream_session__symbol__delete",
            "delete_backtest_result_backtest__backtest_id__delete",
        ]
        
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
            """,
            exclude_operations=_mcp_excluded_ops,
            auth_config=_mcp_auth,
            headers=["authorization", "x-api-key"],
        )
        mcp.mount_http()
        log.info(f"MCP mounted with {len(mcp.tools)} tools. Auth: {'enabled' if _mcp_auth else 'DISABLED'}")

    return app



app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=os.getenv("PORT") is None, log_level="info")
