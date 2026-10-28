"""
kairos-claims Phase E endpoints — the "arm 11" leaderboard.

  GET /claims/leaderboard — resolved-claim calibration cells (pooled
  coverage + Wilson bands, mean CRPS/pinball/Brier), the PAIRED
  bracket-vs-vol_fan Diebold–Mariano test, and the committed lab arm
  table join (config/claims_lab_arm_reference.json).

Read-only by construction: one REST SELECT over ``nt_signal_claims`` plus
a mtime-cached local JSON read. The READ is not flag-gated — the ledger is
shadow-first evidence (design doc §1) and this endpoint computes nothing
the ledger doesn't already hold; the WRITE path stays gated by
``claims_mode`` / ``claims_vol_fan_mode`` exactly as in Phases A/D, and
the claims→calibration bridge stays gated by ``claims_prior_mode``.
Requires SUPABASE_URL and SUPABASE_SECRET_KEY to be configured.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.api_key import require_api_key
from ..core.settings import settings
from ..middleware.rate_limit import RATE_LIMIT_DEFAULT, limiter

log = logging.getLogger("regime.claims")

router = APIRouter(prefix="/claims", tags=["Claims"])


def _db_available() -> bool:
    """True if Supabase is configured."""
    return bool(settings.SUPABASE_URL and settings.SUPABASE_SECRET_KEY)


class ClaimsLeaderboardResponse(BaseModel):
    """The arm-11 leaderboard document (loose payloads: cells mirror the
    claims_leaderboard module's dicts; outcome fields are JSONB)."""

    generated_at: str
    n_resolved_total: int = 0
    fetch_limit: int = 5000
    window: dict[str, Any] = Field(default_factory=dict)
    cells: list[dict[str, Any]] = Field(default_factory=list)
    paired_dm: Optional[dict[str, Any]] = None
    lab_reference: dict[str, Any] = Field(default_factory=dict)
    gates: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/leaderboard",
    response_model=ClaimsLeaderboardResponse,
    summary="Arm-11 leaderboard: resolved kairos claims vs the lab arm table",
)
async def get_claims_leaderboard(
    request: Request,
    limit: int = 5000,
    _: None = Depends(require_api_key),
):
    """Aggregate every RESOLVED claim in ``nt_signal_claims`` into the
    Phase E leaderboard: per-(symbol, kind) coverage/CRPS cells with
    lab-style verdicts, the paired sibling-claim DM test, and the lab
    arm-table join. Read-only; no flags; no writes; no resolver.
    """
    if not _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    from regime_platform.sweep_orchestrator.claims_leaderboard import (
        build_leaderboard,
        fetch_resolved_claims,
    )

    try:
        safe_limit = max(1, min(int(limit), 20000))
    except (TypeError, ValueError):
        safe_limit = 5000

    try:
        rows = await fetch_resolved_claims(safe_limit)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Supabase error: {exc.response.status_code}"
        )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail=f"Supabase timeout: {exc}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch claims: {exc}")

    doc = build_leaderboard(rows, limit=safe_limit)
    return ClaimsLeaderboardResponse(**doc)
