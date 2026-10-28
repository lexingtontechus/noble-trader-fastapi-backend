"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     Dynamic Regime Risk Management Platform - FastAPI Service  v7.7.2       ║
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

# Load .env file before any other imports that read env vars
from dotenv import load_dotenv
load_dotenv(override=False)  # Don't override already-set env vars (e.g. from Render dashboard)

# Configure sensitive data filtering for logging
from regime_platform.utils.log_filter import configure_logging_filter
configure_logging_filter()

# urllib3<2.0 compatibility shim for alpaca-trade-api
import urllib3_compat  # noqa: F401 -- side-effect shim

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exceptions import HTTPException

try:
    from fastapi_mcp import FastApiMCP
    from fastapi_mcp.types import AuthConfig
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from regime_platform.auth.api_key import require_api_key
from regime_platform.core.gpu_engine import gpu_capabilities
from regime_platform.core.settings import settings
from regime_platform.middleware.rate_limit import limiter, RATE_LIMIT_DEFAULT, RATE_LIMIT_COMPUTE, RATE_LIMIT_WRITE
from regime_platform.middleware.input_validation import InputValidationMiddleware

# v2.x routers
from regime_platform.routers import (
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
from regime_platform.routers import calibration, claims, venue
# Phase 9: Runtime Configuration (admin-adjustable params)
from regime_platform.routers import config as config_router_module
# Phase 11: Price Data Proxy (Lightning AI, replaces Yahoo scraping)
from regime_platform.routers import prices as prices_router_module
from regime_platform.middleware.kill_switch_gate import KillSwitchGateMiddleware
# v5.0 Renko HFT pipeline
from renko.router import renko_router

log = logging.getLogger("regime.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting up v7.7.2 ...")
    log.info(f"GPU: {gpu_capabilities()}")

    # Phase 8: Start operational services
    try:
        from regime_platform.services.operational import kill_switch, audit_logger, reconciler
        await kill_switch.start_refresh()
        log.info("Kill switch service started")
        
        # Start enhanced Telegram notifications if configured
        if settings.telegram_configured:
            try:
                from notifications.telegram import telegram_notifier
                # Test the connection with a simple message
                try:
                    test_result = await telegram_notifier._send_message("🔔 Telegram signal notifications active")
                    if test_result:
                        log.info("Enhanced Telegram notifications service initialized")
                    else:
                        log.warning("Telegram notifications service failed to initialize")
                except Exception as e:
                    log.warning(f"Telegram test message failed: {e}")
            except ImportError:
                log.warning("Telegram notifications module not available")
            except Exception as e:
                log.error(f"Failed to initialize Telegram notifications: {e}")

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
    # ── HIGH #11 (audit 2026-07-23): L2 imbalance dead-signal ──────────
    # The L2FeedManager starts Alpaca + HL WebSocket adapters per symbol
    # and feeds snapshots into L2ImbalanceComputer.compute(), but the
    # v5 EV engine no longer accepts `external_signal` — the computed
    # imbalance value is discarded. P3.5 deprecated L2 entirely; HIGH #9
    # wired the proxy's SSE stream as the replacement microstructure
    # source. Running the WS adapters wastes 1 connection per symbol +
    # CPU on snapshot processing.
    #
    # Default: NOBLE_TRADER_DISABLE_L2_FEED=true (see settings.py:71-85).
    # Set to False ONLY for research/simulation harnesses that consume
    # L2 via a downstream path (e.g. /api/ta/{symbol}/indicators?source=l2).
    # The L2FeedManager code is preserved for that path.
    try:
        from regime_platform.core.settings import settings as _settings
        l2_hard_disabled = bool(getattr(_settings, "NOBLE_TRADER_DISABLE_L2_FEED", True))
        if l2_hard_disabled:
            app.state.l2_feed_manager = None
            log.info(
                "L2 feed manager DISABLED via NOBLE_TRADER_DISABLE_L2_FEED=true "
                "(audit HIGH #11 — L2 imbalance dead signal; v5 EV engine no "
                "longer accepts external_signal; SSE consumer is the replacement). "
                "Set NOBLE_TRADER_DISABLE_L2_FEED=false to re-enable for research."
            )
        else:
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
        version="8.2.0",
        contact={
            "name": "Noble Trader Dynamic Regime Risk Platform",
            "email": "nobletrader@0xdweb.com",
        },
        license_info={"name": "MIT"},
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        _deploy_env = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        
        # Log the full error with stack trace in all environments
        log.error(f"FATAL ERROR during request {request.method} {request.url}: {exc}", exc_info=True)
        
        # Sanitize error response for production/staging
        if _deploy_env in ("production", "staging"):
            return JSONResponse(
                status_code=500,
                content={"error": "Internal Server Error", "detail": "An unexpected error occurred"},
            )
        else:
            # Development environment can show more details
            return JSONResponse(
                status_code=500,
                content={"error": "Internal Server Error", "detail": str(exc), "type": exc.__class__.__name__},
            )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Handler for FastAPI request validation errors."""
        _deploy_env = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        
        # Log validation errors in all environments
        log.warning(f"Validation error: {exc}")
        
        # Sanitize error response for production/staging
        if _deploy_env in ("production", "staging"):
            return JSONResponse(
                status_code=422,
                content={"error": "Invalid input", "detail": "Please check your request data"},
            )
        else:
            # Development environment can show more details
            return JSONResponse(
                status_code=422,
                content={"error": "Invalid input", "detail": exc.detail},
            )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """Handler for HTTP exceptions."""
        _deploy_env = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        
        # Log HTTP exceptions in all environments
        log.warning(f"HTTP {exc.status_code}: {exc.detail}")
        
        # Sanitize error details for production/staging
        if _deploy_env in ("production", "staging") and exc.status_code >= 500:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "Internal Server Error", "detail": "An unexpected error occurred"},
            )
        else:
            # Keep original details for development and non-5xx errors
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.detail},
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
    app.include_router(claims.router)
    app.include_router(venue.router)
    app.include_router(prices_router_module.router)

    # Add input validation middleware (before other middleware)
    app.add_middleware(InputValidationMiddleware)

    # Kill-switch gate middleware (M5): blocks /renko/tick* when a halt is
    # active. Previously imported but never registered — the advertised
    # admin risk control did nothing. On-demand Supabase read (no background
    # pub/sub), fail-open on errors.
    app.add_middleware(KillSwitchGateMiddleware)

    # ── TimesFM forecast proxy ──────────────────────────────────
    # The proxy.py routes /forecast → TimesFM on port 8002, but
    # Lightning AI's public URL may map to this port (8001) instead
    # of the proxy port (8000). This endpoint ensures /forecast
    # works regardless of which port is publicly exposed.
    import urllib.request as _urllib
    import asyncio as _asyncio

    @app.post("/forecast", include_in_schema=False)
    async def forecast_proxy(
        request: Request,
        _: None = Depends(require_api_key),
    ):
        """Pass-through to the TimesFM inference server on port 8002.

        Authenticated (require_api_key): the sweep orchestrator calls this
        with X-API-Key. Blocking anonymous callers closes the open-proxy
        (C2): no unauthenticated GPU burn / internal probing.
        """
        log.info(f"TimesFM proxy: forwarding POST /forecast to port 8002")
        timesfm_url = "http://127.0.0.1:8002/forecast"
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}

        def _sync_post():
            req = _urllib.Request(timesfm_url, data=body, headers=headers, method="POST")
            with _urllib.urlopen(req, timeout=90) as resp:
                return (resp.status, resp.read(), dict(resp.headers))

        try:
            status, content, resp_headers = await _asyncio.get_event_loop().run_in_executor(None, _sync_post)
        except _urllib.HTTPError as exc:
            status, content, resp_headers = exc.code, exc.read(), dict(exc.headers)
        except _urllib.URLError as exc:
            log.error(f"TimesFM proxy: connection to port 8002 failed: {exc.reason}")
            return JSONResponse(status_code=502, content={"error": "TimesFM upstream unreachable"})

        log.info(f"TimesFM proxy: port 8002 returned {status}")
        return Response(content=content, status_code=status, headers=resp_headers)

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
            "version": "8.2.0",
            "mode": "paper" if settings.ALPACA_API_KEY else "live",
            "renko_pipelines": renko_status,
        }

    if MCP_AVAILABLE:
        _mcp_excluded_ops = [
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
        
        _mcp_auth = AuthConfig(dependencies=[Depends(require_api_key)])

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
    import socket as _socket

    _preferred = int(os.getenv("PORT", "8000"))
    _ports_to_try = [_preferred] + [p for p in [8000, 8001, 8080, 9000] if p != _preferred]
    _bound_port = None

    for _port in _ports_to_try:
        try:
            _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            _sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            _sock.bind(("0.0.0.0", _port))
            _sock.listen(2048)
            _bound_port = _port
            log.info(f"Bound to port {_bound_port} (requested={_preferred})")
            break
        except OSError:
            log.warning(f"Port {_port} unavailable, trying next...")
            continue

    if _bound_port is None:
        log.critical("No available port found!")
        raise SystemExit(1)

    uvicorn.run("main:app", host="0.0.0.0", port=_bound_port, fd=_sock.fileno(),
                reload=os.getenv("PORT") is None, log_level="info")
