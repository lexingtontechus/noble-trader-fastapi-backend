"""
Venue Management endpoints.

  GET  /venue/list                - List all venues with active status
  POST /venue/{venue_id}/snapshot - Record an account snapshot
  GET  /venue/{venue_id}/snapshots - Get snapshot history (paginated)
  GET  /venue/{venue_id}/status   - Venue health check

Requires SUPABASE_URL and SUPABASE_KEY to be configured.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth.jwt_auth import get_authed_user, require_write, TokenData
from ..core.settings import settings
from ..middleware.rate_limit import limiter, RATE_LIMIT_DEFAULT, RATE_LIMIT_WRITE

log = logging.getLogger("regime.venue")

router = APIRouter(prefix="/venue", tags=["Venue Management"])


# ─── Supabase REST helpers ─────────────────────────────────────────────


def _supabase_headers() -> dict[str, str]:
    """Build headers for Supabase PostgREST API calls."""
    return {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _supabase_url(table: str) -> str:
    """Build the base Supabase URL for a given table."""
    return f"{settings.SUPABASE_URL}/rest/v1/{table}"


async def _db_available() -> bool:
    """True if Supabase is configured."""
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


# ─── Request/Response Models ───────────────────────────────────────────


class VenueResponse(BaseModel):
    """A trading venue."""
    id: str
    name: str
    venue_type: str
    supports_stops: bool
    max_leverage: float
    is_active: bool


class VenueListResponse(BaseModel):
    """List of venues."""
    venues: list[VenueResponse] = Field(default_factory=list)
    total: int = 0


class SnapshotRequest(BaseModel):
    """Account snapshot data."""
    equity: float = Field(..., description="Total account value")
    cash: Optional[float] = Field(None, description="Cash balance")
    margin_used: Optional[float] = Field(None, description="Margin in use")
    open_positions: int = Field(default=0, ge=0, description="Count of open positions")
    max_drawdown: Optional[float] = Field(None, description="Peak-to-trough drawdown")
    unrealized_pnl: Optional[float] = Field(None, description="Unrealized P&L")


class SnapshotResponse(BaseModel):
    """An account snapshot record."""
    id: str
    venue_id: str
    equity: float
    cash: Optional[float]
    open_positions: int
    snapshot_at: str


class SnapshotListResponse(BaseModel):
    """Paginated snapshot history."""
    snapshots: list[SnapshotResponse] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0


class VenueStatusResponse(BaseModel):
    """Venue health and latest snapshot summary."""
    venue: VenueResponse
    latest_snapshot: Optional[SnapshotResponse] = None
    snapshot_count: int = 0


# ─── Endpoints ─────────────────────────────────────────────────────────


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/list",
    response_model=VenueListResponse,
    summary="List all trading venues",
)
async def list_venues(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """List all configured trading venues with their current status."""
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _supabase_url("nt_venues"),
                headers=_supabase_headers(),
                params={"select": "*", "order": "id.asc"},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch venues: HTTP {resp.status_code}",
                )

            rows = resp.json()
            venues = [
                VenueResponse(
                    id=r["id"],
                    name=r["name"],
                    venue_type=r["venue_type"],
                    supports_stops=r["supports_stops"],
                    max_leverage=r["max_leverage"],
                    is_active=r["is_active"],
                )
                for r in rows
            ]
            return VenueListResponse(venues=venues, total=len(venues))

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")


@limiter.limit(RATE_LIMIT_WRITE)
@router.post(
    "/{venue_id}/snapshot",
    response_model=SnapshotResponse,
    summary="Record an account snapshot",
    status_code=201,
)
async def record_snapshot(
    request: Request,
    venue_id: str,
    req: SnapshotRequest,
    user: TokenData = Depends(require_write),
):
    """
    Record an account snapshot for a venue.

    Snapshot records capture the account state at a point in time,
    enabling drawdown tracking, equity curve analysis, and per-venue
    performance monitoring.
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    # Verify venue exists
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            venue_resp = await client.get(
                _supabase_url("nt_venues"),
                headers=_supabase_headers(),
                params={"id": f"eq.{venue_id}", "select": "id", "limit": "1"},
            )
            if venue_resp.status_code != 200 or not venue_resp.json():
                raise HTTPException(status_code=404, detail=f"Venue '{venue_id}' not found")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Venue lookup failed: {exc}")

    record_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    row = {
        "id": record_id,
        "venue_id": venue_id,
        "equity": req.equity,
        "cash": req.cash,
        "margin_used": req.margin_used,
        "open_positions": req.open_positions,
        "max_drawdown": req.max_drawdown,
        "unrealized_pnl": req.unrealized_pnl,
        "snapshot_at": now,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _supabase_url("nt_account_snapshots"),
                headers=_supabase_headers(),
                json=row,
            )
            if resp.status_code not in (200, 201):
                log.error(
                    "Supabase insert failed: status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to record snapshot: HTTP {resp.status_code}",
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")

    return SnapshotResponse(
        id=record_id,
        venue_id=venue_id,
        equity=req.equity,
        cash=req.cash,
        open_positions=req.open_positions,
        snapshot_at=now,
    )


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/{venue_id}/snapshots",
    response_model=SnapshotListResponse,
    summary="Get snapshot history for a venue",
)
async def get_snapshots(
    request: Request,
    venue_id: str,
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=20, ge=1, le=100, description="Page size"),
    user: TokenData = Depends(get_authed_user),
):
    """Get paginated account snapshot history for a venue."""
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get total count
            count_resp = await client.get(
                _supabase_url("nt_account_snapshots"),
                headers={**_supabase_headers(), "Prefer": "count=exact"},
                params={
                    "venue_id": f"eq.{venue_id}",
                    "select": "id",
                    "limit": "1",
                },
            )
            total = 0
            content_range = count_resp.headers.get("content-range", "")
            if "/" in content_range:
                try:
                    total = int(content_range.split("/")[1].strip())
                except (ValueError, IndexError):
                    pass

            # Get page
            resp = await client.get(
                _supabase_url("nt_account_snapshots"),
                headers=_supabase_headers(),
                params={
                    "venue_id": f"eq.{venue_id}",
                    "select": "id,venue_id,equity,cash,open_positions,snapshot_at",
                    "order": "snapshot_at.desc",
                    "offset": str(offset),
                    "limit": str(limit),
                },
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch snapshots: HTTP {resp.status_code}",
                )

            rows = resp.json()
            snapshots = [
                SnapshotResponse(
                    id=r["id"],
                    venue_id=r["venue_id"],
                    equity=r["equity"],
                    cash=r.get("cash"),
                    open_positions=r["open_positions"],
                    snapshot_at=r["snapshot_at"],
                )
                for r in rows
            ]

            return SnapshotListResponse(
                snapshots=snapshots,
                total=total,
                offset=offset,
                limit=limit,
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/{venue_id}/status",
    response_model=VenueStatusResponse,
    summary="Get venue health and latest snapshot",
)
async def get_venue_status(
    request: Request,
    venue_id: str,
    user: TokenData = Depends(get_authed_user),
):
    """Get venue details along with the latest account snapshot."""
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get venue
            venue_resp = await client.get(
                _supabase_url("nt_venues"),
                headers=_supabase_headers(),
                params={"id": f"eq.{venue_id}", "select": "*", "limit": "1"},
            )
            if venue_resp.status_code != 200 or not venue_resp.json():
                raise HTTPException(status_code=404, detail=f"Venue '{venue_id}' not found")

            venue_row = venue_resp.json()[0]
            venue = VenueResponse(
                id=venue_row["id"],
                name=venue_row["name"],
                venue_type=venue_row["venue_type"],
                supports_stops=venue_row["supports_stops"],
                max_leverage=venue_row["max_leverage"],
                is_active=venue_row["is_active"],
            )

            # Get snapshot count
            count_resp = await client.get(
                _supabase_url("nt_account_snapshots"),
                headers={**_supabase_headers(), "Prefer": "count=exact"},
                params={
                    "venue_id": f"eq.{venue_id}",
                    "select": "id",
                    "limit": "1",
                },
            )
            snapshot_count = 0
            content_range = count_resp.headers.get("content-range", "")
            if "/" in content_range:
                try:
                    snapshot_count = int(content_range.split("/")[1].strip())
                except (ValueError, IndexError):
                    pass

            # Get latest snapshot
            snap_resp = await client.get(
                _supabase_url("nt_account_snapshots"),
                headers=_supabase_headers(),
                params={
                    "venue_id": f"eq.{venue_id}",
                    "select": "id,venue_id,equity,cash,open_positions,snapshot_at",
                    "order": "snapshot_at.desc",
                    "limit": "1",
                },
            )
            latest_snapshot = None
            if snap_resp.status_code == 200 and snap_resp.json():
                s = snap_resp.json()[0]
                latest_snapshot = SnapshotResponse(
                    id=s["id"],
                    venue_id=s["venue_id"],
                    equity=s["equity"],
                    cash=s.get("cash"),
                    open_positions=s["open_positions"],
                    snapshot_at=s["snapshot_at"],
                )

            return VenueStatusResponse(
                venue=venue,
                latest_snapshot=latest_snapshot,
                snapshot_count=snapshot_count,
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")
