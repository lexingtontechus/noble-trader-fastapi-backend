"""
P&L Analytics endpoints.

  GET  /pnl/intraday              - Intraday P&L time-bucketed series
  GET  /pnl/history               - Historical daily P&L with equity curve
  GET  /pnl/export                - CSV export of P&L data
  GET  /pnl/alerts                - List P&L alert thresholds
  POST /pnl/alerts                - Create P&L alert threshold
  DELETE /pnl/alerts/{threshold_id} - Delete P&L alert threshold
  POST /pnl/alerts/check          - Manually evaluate alert thresholds

All endpoints require JWT authentication.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from ..auth.jwt_auth import get_authed_user, TokenData
from ..services.pnl_service import pnl_service, AlertThresholdCreate
from ..middleware.rate_limit import limiter, RATE_LIMIT_DEFAULT
from ..models.schemas import RiskDashboardResponse
from fastapi import Request

log = logging.getLogger("regime.pnl")

router = APIRouter(prefix="/pnl", tags=["P&L Analytics"])


# ─── Request/Response Models ─────────────────────────────────────────────────

class IntradayPnlResponse(BaseModel):
    """Intraday P&L time-bucketed response."""
    timeframe: str
    period: str
    buckets: list[dict] = Field(default_factory=list, description="Time-bucketed P&L data.")
    count: int = 0


class HistoricalPnlResponse(BaseModel):
    """Historical daily P&L with equity curve."""
    period: str
    timestamps: list = Field(default_factory=list)
    equity: list[float] = Field(default_factory=list)
    pnl: list[float] = Field(default_factory=list)
    pnl_pct: list[float] = Field(default_factory=list)
    cumulative_pnl: list[float] = Field(default_factory=list)
    drawdown: list[float] = Field(default_factory=list)
    drawdown_pct: list[float] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    start_equity: float = 0
    end_equity: float = 0


class AlertThresholdRequest(BaseModel):
    """Request to create a P&L alert threshold."""
    metric: str = Field(
        ...,
        description="Metric to monitor: day_pnl, unrealized_pnl, drawdown_pct, var_breach, equity_change_pct",
    )
    operator: str = Field(
        ...,
        description="Comparison operator: lt, gt, lte, gte, crosses_below, crosses_above",
    )
    value: float = Field(..., description="Threshold value to trigger alert.")
    severity: str = Field(default="warning", description="Alert severity: info, warning, critical")
    enabled: bool = Field(default=True)
    cooldown_minutes: int = Field(default=15, ge=1, le=1440, description="Min minutes between repeated alerts")


class AlertThresholdResponse(BaseModel):
    """P&L alert threshold."""
    id: str
    user_id: str
    metric: str
    operator: str
    value: float
    severity: str
    enabled: bool
    cooldown_minutes: int
    created_at: float
    last_triggered: float = 0


class AlertCheckRequest(BaseModel):
    """Request to manually evaluate alert thresholds."""
    current_values: dict[str, float] = Field(
        ...,
        description="Current metric values to check against thresholds.",
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────

@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/intraday",
    response_model=IntradayPnlResponse,
    summary="Intraday P&L time-bucketed series",
)
async def get_intraday_pnl(
    request: Request,
    timeframe: str = Query(default="15Min", description="Time bucket: 5Min, 15Min, 1Hour, 1Day"),
    period: str = Query(default="1D", description="Lookback period: 1D, 1W, 1M, 3M, 6M, 1A, all"),
    user: TokenData = Depends(get_authed_user),
):
    """
    Fetch intraday P&L with fine-grained time buckets from Alpaca portfolio history.

    Useful for intraday equity curves and real-time P&L tracking dashboards.
    Timeframes: 5Min (granular), 15Min (default), 1Hour (summary), 1Day (daily).
    """
    buckets = await pnl_service.get_intraday_pnl(
        user_id=user.sub,
        timeframe=timeframe,
        period=period,
        org_id=user.org_id,
    )

    return IntradayPnlResponse(
        timeframe=timeframe,
        period=period,
        buckets=[
            {
                "timestamp": b.timestamp,
                "date": b.date_str,
                "equity": b.equity,
                "pnl": b.pnl,
                "pnl_pct": b.pnl_pct,
            }
            for b in buckets
        ],
        count=len(buckets),
    )


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/history",
    response_model=HistoricalPnlResponse,
    summary="Historical daily P&L with equity curve and drawdown",
)
async def get_historical_pnl(
    request: Request,
    period: str = Query(default="1M", description="Lookback period: 1W, 1M, 3M, 6M, 1A, all"),
    user: TokenData = Depends(get_authed_user),
):
    """
    Fetch historical daily P&L with equity curve, cumulative P&L, and drawdown.

    Returns a complete daily time series suitable for charting and analysis.
    The equity curve data can be used for risk metrics computation.
    """
    hist = await pnl_service.get_historical_pnl(
        user_id=user.sub,
        period=period,
        org_id=user.org_id,
    )

    return HistoricalPnlResponse(
        period=period,
        timestamps=hist.get("timestamps", []),
        equity=hist.get("equity", []),
        pnl=hist.get("pnl", []),
        pnl_pct=hist.get("pnl_pct", []),
        cumulative_pnl=hist.get("cumulative_pnl", []),
        drawdown=hist.get("drawdown", []),
        drawdown_pct=hist.get("drawdown_pct", []),
        dates=hist.get("dates", []),
        start_equity=hist.get("start_equity", 0),
        end_equity=hist.get("end_equity", 0),
    )


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/export",
    summary="Export P&L data as CSV",
    response_class=StreamingResponse,
)
async def export_pnl_csv(
    request: Request,
    period: str = Query(default="1M", description="Lookback period for equity curve."),
    sections: str = Query(default="all", description="Comma-separated sections: positions, trades, equity_curve, risk_metrics, all"),
    user: TokenData = Depends(get_authed_user),
):
    """
    Export P&L data as a CSV file download.

    Sections (comma-separated):
      - positions: current open positions with P&L
      - trades: recent FILL activities with net amounts
      - equity_curve: daily equity + P&L + drawdown time series
      - risk_metrics: computed Sharpe, Sortino, VaR, etc.
      - all: all of the above (default)
    """
    section_list = [s.strip() for s in sections.split(",")]

    csv_content = await pnl_service.export_pnl_csv(
        user_id=user.sub,
        period=period,
        sections=section_list,
        org_id=user.org_id,
    )

    filename = f"pnl_export_{period}_{user.sub[:8]}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/alerts",
    response_model=list[AlertThresholdResponse],
    summary="List P&L alert thresholds",
)
async def list_alert_thresholds(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """Get all configured P&L alert thresholds for the authenticated user."""
    thresholds = await pnl_service.list_alert_thresholds(user.sub)
    return [
        AlertThresholdResponse(
            id=t.id,
            user_id=t.user_id,
            metric=t.metric,
            operator=t.operator,
            value=t.value,
            severity=t.severity,
            enabled=t.enabled,
            cooldown_minutes=t.cooldown_minutes,
            created_at=t.created_at,
            last_triggered=t.last_triggered,
        )
        for t in thresholds
    ]


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.post(
    "/alerts",
    response_model=AlertThresholdResponse,
    summary="Create P&L alert threshold",
)
async def create_alert_threshold(
    request: Request,
    req: AlertThresholdRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Create a new P&L alert threshold.

    When the specified metric crosses the threshold, an alert will be
    broadcast via SSE and (optionally) Discord.

    Example: {"metric": "drawdown_pct", "operator": "lte", "value": -5.0, "severity": "critical"}
    → Alerts when portfolio drawdown reaches -5%.
    """
    try:
        threshold = await pnl_service.create_alert_threshold(
            user_id=user.sub,
            req=AlertThresholdCreate(
                metric=req.metric,
                operator=req.operator,
                value=req.value,
                severity=req.severity,
                enabled=req.enabled,
                cooldown_minutes=req.cooldown_minutes,
            ),
        )
        return AlertThresholdResponse(
            id=threshold.id,
            user_id=threshold.user_id,
            metric=threshold.metric,
            operator=threshold.operator,
            value=threshold.value,
            severity=threshold.severity,
            enabled=threshold.enabled,
            cooldown_minutes=threshold.cooldown_minutes,
            created_at=threshold.created_at,
            last_triggered=threshold.last_triggered,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.delete(
    "/alerts/{threshold_id}",
    summary="Delete P&L alert threshold",
)
async def delete_alert_threshold(
    request: Request,
    threshold_id: str,
    user: TokenData = Depends(get_authed_user),
):
    """Delete a P&L alert threshold by ID."""
    deleted = await pnl_service.delete_alert_threshold(user.sub, threshold_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Alert threshold '{threshold_id}' not found")
    return {"status": "deleted", "id": threshold_id}


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.post(
    "/alerts/check",
    summary="Manually evaluate P&L alert thresholds",
)
async def check_alerts(
    request: Request,
    req: AlertCheckRequest,
    user: TokenData = Depends(get_authed_user),
):
    """
    Manually evaluate all alert thresholds against provided current values.

    Returns a list of triggered alerts with details.
    This is useful for testing threshold configurations.
    """
    triggered = await pnl_service.check_alerts(user.sub, req.current_values)
    return {"triggered": triggered, "count": len(triggered)}
