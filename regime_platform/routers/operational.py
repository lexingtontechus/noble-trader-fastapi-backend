"""
Operational Hardening Router — Phase 8 P0 endpoints.

Kill Switch:
  GET  /operational/kill-switch/status       — Get active halts
  POST /operational/kill-switch/activate     — Activate a halt (admin only check)
  POST /operational/kill-switch/deactivate   — Deactivate a halt
  POST /operational/kill-switch/cancel-all   — Cancel all open orders
  POST /operational/kill-switch/close-all    — Close all open positions

Audit Log:
  GET  /operational/audit-log               — Query audit events (paginated)
  GET  /operational/audit-log/summary        — Event count summary
  GET  /operational/audit-log/export         — Export audit events as CSV

Mode Manager:
  GET  /operational/mode                     — Get current trading mode
  POST /operational/mode/request             — Request mode change (returns token)
  POST /operational/mode/confirm             — Confirm mode change with token
  GET  /operational/mode/health              — Health check current Alpaca environment

Executor:
  GET  /operational/executor/status          — Executor mode + health check

Reconciliation:
  GET  /operational/reconcile/status         — Last reconciliation status
  POST /operational/reconcile/run            — Run reconciliation now
  GET  /operational/reconcile/positions      — Position reconciliation
"""

from __future__ import annotations
import logging
import csv
import io
from fastapi import APIRouter, Depends, Query, Request, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from pydantic import BaseModel

from ..auth.jwt_auth import get_authed_user, TokenData
from ..core.exceptions import raise_http_exception
from ..middleware.rate_limit import limiter, RATE_LIMIT_WRITE
from ..services.operational.kill_switch import (
    kill_switch, HaltLevel, HaltReason, HaltRecord,
)
from ..services.operational.audit_logger import (
    audit_logger, AuditEventType,
)
from ..services.operational.mode_manager import (
    mode_manager, TradingMode,
)
from ..services.operational.reconciler import (
    reconciler,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/operational", tags=["Operational"])


# ── Pydantic Request/Response Models ─────────────────────────────────────

class HaltActivateRequest(BaseModel):
    level: HaltLevel
    reason: HaltReason
    scope: Optional[str] = None
    notes: Optional[str] = None

class HaltDeactivateRequest(BaseModel):
    level: HaltLevel
    scope: Optional[str] = None

class ModeChangeRequestModel(BaseModel):
    target_mode: TradingMode

class ModeConfirmRequest(BaseModel):
    confirmation_token: str
    acknowledgment_text: str

class ReconcileOrderRequest(BaseModel):
    order_id: str
    expected_price: Optional[float] = None
    expected_quantity: Optional[float] = None
    direction: Optional[str] = None
    symbol: Optional[str] = None


# ── Kill Switch Endpoints ─────────────────────────────────────────────────

@router.get(
    "/kill-switch/status",
    summary="Get all active trading halts",
)
async def get_kill_switch_status(
    user: TokenData = Depends(get_authed_user),
):
    """Returns all currently active halts (global, user, symbol)."""
    try:
        halts = await kill_switch.get_all_active_halts()
        is_halted = await kill_switch.is_halted(user_id=user.sub)
        return {
            "is_halted": is_halted,
            "active_halts": [h.model_dump(mode="json") for h in halts],
            "total_halts": len(halts),
        }
    except Exception as e:
        raise_http_exception(e, "kill_switch_status")


@router.post(
    "/kill-switch/activate",
    summary="Activate a trading halt",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def activate_halt(
    request: Request,
    req: HaltActivateRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Activate a halt at the specified level.
    
    Levels:
    - global_halt: Stops ALL trading (nuclear option)
    - user_halt: Stops trading for a specific user (scope = user_id)
    - symbol_halt: Stops trading for a specific symbol (scope = "AAPL")
    """
    try:
        record = await kill_switch.activate_halt(
            level=req.level,
            reason=req.reason,
            triggered_by=user.sub,
            scope=req.scope,
            notes=req.notes,
        )
        # Audit log
        await audit_logger.log_event(
            event_type=AuditEventType.HALT_ACTIVATED,
            user_id=user.sub,
            metadata={
                "level": req.level.value,
                "reason": req.reason.value,
                "scope": req.scope,
            },
        )
        return {"status": "activated", "halt": record.model_dump(mode="json")}
    except Exception as e:
        raise_http_exception(e, "kill_switch_activate")


@router.post(
    "/kill-switch/deactivate",
    summary="Deactivate a trading halt",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def deactivate_halt(
    request: Request,
    req: HaltDeactivateRequest,
    user: TokenData = Depends(get_authed_user),
):
    """Deactivate a halt at the specified level."""
    try:
        success = await kill_switch.deactivate_halt(
            level=req.level,
            scope=req.scope,
            deactivated_by=user.sub,
        )
        # Audit log
        await audit_logger.log_event(
            event_type=AuditEventType.HALT_DEACTIVATED,
            user_id=user.sub,
            metadata={
                "level": req.level.value,
                "scope": req.scope,
            },
        )
        return {"status": "deactivated", "level": req.level.value, "scope": req.scope}
    except Exception as e:
        raise_http_exception(e, "kill_switch_deactivate")


@router.post(
    "/kill-switch/cancel-all",
    summary="Cancel all open orders (emergency action)",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def cancel_all_orders(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """Cancel ALL open orders across all symbols. Use in emergencies only."""
    try:
        result = await kill_switch.cancel_all_orders(user_id=user.sub)
        await audit_logger.log_event(
            event_type=AuditEventType.KILL_SWITCH_CANCEL_ALL,
            user_id=user.sub,
            metadata=result,
        )
        return result
    except Exception as e:
        raise_http_exception(e, "cancel_all_orders")


@router.post(
    "/kill-switch/close-all",
    summary="Close all open positions (emergency action)",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def close_all_positions(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """Close ALL open positions. Use in emergencies only."""
    try:
        result = await kill_switch.close_all_positions(user_id=user.sub)
        await audit_logger.log_event(
            event_type=AuditEventType.KILL_SWITCH_CLOSE_ALL,
            user_id=user.sub,
            metadata=result,
        )
        return result
    except Exception as e:
        raise_http_exception(e, "close_all_positions")


# ── Audit Log Endpoints ───────────────────────────────────────────────────

@router.get(
    "/audit-log",
    summary="Query audit events with pagination and filters",
)
async def query_audit_log(
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    user: TokenData = Depends(get_authed_user),
):
    """Query the append-only audit trail with filters."""
    try:
        result = await audit_logger.query_events(
            user_id=user.sub,
            event_type=event_type,
            symbol=symbol,
            limit=limit,
            offset=offset,
            start_date=start_date,
            end_date=end_date,
        )
        return result
    except Exception as e:
        raise_http_exception(e, "audit_log_query")


@router.get(
    "/audit-log/summary",
    summary="Get audit event count summary for 24h/7d/30d",
)
async def get_audit_summary(
    user: TokenData = Depends(get_authed_user),
):
    """Returns event counts by type for 24h, 7d, and 30d windows."""
    try:
        return await audit_logger.get_summary(user_id=user.sub)
    except Exception as e:
        raise_http_exception(e, "audit_summary")


@router.get(
    "/audit-log/export",
    summary="Export audit events as CSV",
)
async def export_audit_log(
    event_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
    user: TokenData = Depends(get_authed_user),
):
    """Export audit events as CSV file download."""
    try:
        result = await audit_logger.query_events(
            user_id=user.sub,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=0,
        )
        
        output = io.StringIO()
        writer = csv.writer(output)
        # Header
        writer.writerow([
            "id", "event_type", "user_id", "symbol", "order_id",
            "direction", "quantity", "price", "order_type", "regime",
            "strategy", "signal_score", "created_at",
        ])
        # Rows
        for event in result.get("events", []):
            writer.writerow([
                event.get("id", ""),
                event.get("event_type", ""),
                event.get("user_id", ""),
                event.get("symbol", ""),
                event.get("order_id", ""),
                event.get("direction", ""),
                event.get("quantity", ""),
                event.get("price", ""),
                event.get("order_type", ""),
                event.get("regime", ""),
                event.get("strategy", ""),
                event.get("signal_score", ""),
                event.get("created_at", ""),
            ])
        
        content = output.getvalue()
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=audit_log.csv",
            },
        )
    except Exception as e:
        raise_http_exception(e, "audit_export")


# ── Mode Manager Endpoints ────────────────────────────────────────────────

@router.get(
    "/mode",
    summary="Get current trading mode (simulation/paper/live)",
)
async def get_trading_mode(
    user: TokenData = Depends(get_authed_user),
):
    """Returns the current trading mode and last change details."""
    try:
        state = await mode_manager.get_mode_state()
        return state.model_dump(mode="json")
    except Exception as e:
        raise_http_exception(e, "mode_get")


@router.post(
    "/mode/request",
    summary="Request a trading mode change (returns confirmation token)",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def request_mode_change(
    request: Request,
    req: ModeChangeRequestModel,
    user: TokenData = Depends(get_authed_user),
):
    """
    Request a mode change. Returns a confirmation token that must be
    confirmed within 5 minutes via /mode/confirm.
    
    For LIVE mode, the confirmation requires typing "I UNDERSTAND THE RISKS".
    """
    try:
        change_request = await mode_manager.request_mode_change(
            target_mode=req.target_mode,
            requested_by=user.sub,
        )
        return {
            "confirmation_token": change_request.confirmation_token,
            "target_mode": change_request.target_mode.value,
            "requested_at": change_request.requested_at.isoformat(),
            "expires_in_seconds": mode_manager.TOKEN_TTL,
            "acknowledgment_required": (
                "I UNDERSTAND THE RISKS"
                if req.target_mode == TradingMode.LIVE
                else None
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise_http_exception(e, "mode_request")


@router.post(
    "/mode/confirm",
    summary="Confirm a trading mode change with token and acknowledgment",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def confirm_mode_change(
    request: Request,
    req: ModeConfirmRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Confirm a mode change. For LIVE mode, acknowledgment_text must be
    exactly "I UNDERSTAND THE RISKS".
    """
    try:
        new_state = await mode_manager.confirm_mode_change(
            confirmation_token=req.confirmation_token,
            acknowledged_by=user.sub,
            acknowledgment_text=req.acknowledgment_text,
        )
        return new_state.model_dump(mode="json")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise_http_exception(e, "mode_confirm")


# ── Reconciliation Endpoints ──────────────────────────────────────────────

@router.get(
    "/reconcile/status",
    summary="Get last reconciliation status and results",
)
async def get_reconciliation_status(
    user: TokenData = Depends(get_authed_user),
):
    """Returns the last reconciliation run timestamp and results summary."""
    try:
        return await reconciler.get_status()
    except Exception as e:
        raise_http_exception(e, "reconcile_status")


@router.post(
    "/reconcile/run",
    summary="Run reconciliation on all recent orders",
)
@limiter.limit(RATE_LIMIT_WRITE)
async def run_reconciliation(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """
    Trigger a reconciliation of all orders from the last 24 hours.
    Compares internal expectations against Alpaca's reported fills.
    """
    try:
        results = await reconciler.reconcile_all_recent()
        
        # Summary counts
        matched = sum(1 for r in results if r.status.value == "MATCHED")
        warnings = sum(1 for r in results if r.status.value in ("PARTIAL_FILL", "PRICE_DEVIATION"))
        critical = sum(1 for r in results if r.status.value in ("MISSED_FILL", "ERROR"))
        
        return {
            "total_reconciled": len(results),
            "matched": matched,
            "warnings": warnings,
            "critical": critical,
            "results": [r.model_dump(mode="json") for r in results[:50]],
        }
    except Exception as e:
        raise_http_exception(e, "reconcile_run")


@router.post(
    "/reconcile/order",
    summary="Reconcile a single order by ID",
)
async def reconcile_single_order(
    req: ReconcileOrderRequest,
    user: TokenData = Depends(get_authed_user),
):
    """Reconcile a specific order by checking its fill status against expectations."""
    try:
        result = await reconciler.reconcile_order(
            order_id=req.order_id,
            expected_price=req.expected_price,
            expected_quantity=req.expected_quantity,
            direction=req.direction,
            symbol=req.symbol,
        )
        return result.model_dump(mode="json")
    except Exception as e:
        raise_http_exception(e, "reconcile_order")


@router.get(
    "/reconcile/positions",
    summary="Compare DB positions against Alpaca's reported positions",
)
async def reconcile_positions(
    user: TokenData = Depends(get_authed_user),
):
    """Returns position count comparison between our DB and Alpaca."""
    try:
        result = await reconciler.reconcile_positions(user_id=user.sub)
        return result.model_dump(mode="json")
    except Exception as e:
        raise_http_exception(e, "reconcile_positions")


# ── Executor Status Endpoints ─────────────────────────────────────────────

@router.get(
    "/mode/health",
    summary="Health check the current Alpaca trading environment",
)
async def check_mode_health(
    user: TokenData = Depends(get_authed_user),
):
    """
    Runs a health check against the current Alpaca API environment.
    Verifies that API keys are valid and the account is accessible.
    """
    try:
        from ..services.operational.executor_factory import executor_factory
        health = await executor_factory.health_check()
        return health
    except Exception as e:
        raise_http_exception(e, "mode_health")


@router.get(
    "/executor/status",
    summary="Get executor mode and health status",
)
async def get_executor_status(
    user: TokenData = Depends(get_authed_user),
):
    """Returns the current executor mode, simulation status, and Alpaca connectivity."""
    try:
        from ..services.operational.executor_factory import executor_factory
        executor = executor_factory.get_executor()
        return {
            "execution_mode": executor.execution_mode,
            "is_simulation": executor.is_simulation,
            "base_url": executor.base_url,
            "has_api_keys": bool(executor.api_key and executor.api_secret),
            "order_history_count": len(executor.order_history),
            "circuit_breaker": executor.circuit_breaker_status,
        }
    except Exception as e:
        raise_http_exception(e, "executor_status")
