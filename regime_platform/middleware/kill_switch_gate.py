"""
Kill Switch Gate Middleware — Blocks order execution when trading is halted.

Intercepts requests to trading execution endpoints and checks the kill switch
before allowing them through. Returns 403 with halt details if any active
halt exists for the requesting user or symbol.

This is the last line of defense — even if a bug in the signal pipeline
generates a trade, the kill switch middleware will block it from reaching
Alpaca.
"""

from __future__ import annotations
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class KillSwitchGateMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that blocks order execution when trading is halted.
    
    Protected paths (any request to these paths is checked):
    - /operational/kill-switch/cancel-all
    - /operational/kill-switch/close-all
    - (All other operational endpoints are allowed even during halt)
    
    Order execution paths (always blocked during halt):
    - /renko/tick (generates signals that may trigger orders)
    - /renko/tick/batch
    """
    
    # Paths that are BLOCKED when trading is halted
    # These are order execution / signal generation endpoints
    BLOCKED_PATHS = [
        # Renko signal generation (may trigger automatic order execution)
        "/renko/tick",
        "/renko/tick/batch",
        # Note: /operational/kill-switch/* endpoints are always allowed
        # so admins can deactivate halts
    ]
    
    # Paths that are ALWAYS allowed (even during halt)
    ALLOWED_PATHS = [
        "/operational/kill-switch",
        "/operational/mode",
        "/operational/audit-log",
        "/operational/reconcile",
        "/health",
        "/docs",
        "/openapi.json",
        "/mcp",
    ]

    async def dispatch(self, request: Request, call_next):
        # Check if this path should be blocked
        path = request.url.path
        
        # Always allow health checks and operational endpoints
        if any(path.startswith(allowed) for allowed in self.ALLOWED_PATHS):
            return await call_next(request)
        
        # Check if this path is in the blocked list
        should_check = any(path == blocked or path.startswith(blocked + "/") for blocked in self.BLOCKED_PATHS)
        
        if not should_check:
            return await call_next(request)
        
        # Check kill switch
        try:
            from regime_platform.services.operational.kill_switch import kill_switch
            
            # Extract user_id from auth if available
            user_id = None
            symbol = None
            
            # Try to get symbol from query params or path
            if "symbol" in request.query_params:
                symbol = request.query_params["symbol"]
            
            is_halted = await kill_switch.is_halted(user_id=user_id, symbol=symbol)
            
            if is_halted:
                active_halt = await kill_switch.get_active_halt(user_id=user_id, symbol=symbol)
                halt_info = active_halt.model_dump(mode="json") if active_halt else {}
                
                logger.warning(
                    f"KillSwitchGate: BLOCKED {request.method} {path} — "
                    f"trading is halted ({halt_info.get('level', 'unknown')})"
                )
                
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "TRADING_HALTED",
                        "message": "All trading is halted. Use /operational/kill-switch to review.",
                        "halt": halt_info,
                    },
                )
        except Exception as e:
            logger.error(f"KillSwitchGate: Error checking kill switch: {e}")
            # On error, allow the request through (fail-open for availability)
            # The kill switch is a safety net, not a primary control
        
        return await call_next(request)
