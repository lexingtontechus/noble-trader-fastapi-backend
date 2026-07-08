"""
Calibration Loop endpoints.

  POST /calibration/log         - Insert a calibration record (called on trade close)
  GET  /calibration/bias/{symbol} - Compute calibration bias per symbol
  GET  /calibration/status      - Daily CALIBRATED / OVERCONFIDENT / UNDERCONFIDENT label

The Calibration Loop pairs predicted P_win at trade entry with the actual
outcome, enabling bias detection and dynamic weight adjustment for the EV Engine.

Requires SUPABASE_URL and SUPABASE_KEY to be configured.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth.jwt_auth import get_authed_user, require_write, TokenData
from ..core.exceptions import raise_http_exception
from ..core.settings import settings
from ..middleware.rate_limit import limiter, RATE_LIMIT_DEFAULT, RATE_LIMIT_WRITE

log = logging.getLogger("regime.calibration")

router = APIRouter(prefix="/calibration", tags=["Calibration"])


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


class CalibrationLogRequest(BaseModel):
    """Record a trade outcome for calibration bias analysis."""
    symbol: str = Field(..., description="Trading symbol (e.g. BTC, SPY)")
    predicted_p_win: float = Field(..., ge=0, le=1, description="Blended P_win at moment of entry")
    outcome: Literal["win", "loss"] = Field(..., description="Trade outcome")
    aggression: Literal["passive", "mid", "aggressive"] = Field(
        ..., description="Execution strategy used"
    )
    actual_pnl: Optional[float] = Field(None, description="Final realized P&L")
    error_source: Optional[str] = Field(
        None, description="E.g. regime_shift_mid_trade, signal_flip"
    )
    entry_price: Optional[float] = Field(None, description="Price at entry")
    exit_price: Optional[float] = Field(None, description="Price at exit")
    p_regime: Optional[float] = Field(None, ge=0, le=1, description="P_regime breakdown")
    p_ext_signal: Optional[float] = Field(None, ge=0, le=1, description="External signal vote breakdown")
    p_markov: Optional[float] = Field(None, ge=0, le=1, description="P_markov breakdown")
    p_timesfm: Optional[float] = Field(None, ge=0, le=1, description="TimesFM forecast at entry")


class CalibrationLogResponse(BaseModel):
    """Confirmation of a logged calibration record."""
    id: str
    symbol: str
    outcome: str
    created_at: str


class CalibrationBiasResponse(BaseModel):
    """Per-symbol calibration bias."""
    symbol: str
    avg_predicted_p_win: float = 0.0
    actual_win_rate: float = 0.0
    bias: float = 0.0  # positive = overconfident
    n_records: int = 0
    status: Literal["CALIBRATED", "OVERCONFIDENT", "UNDERCONFIDENT", "INSUFFICIENT_DATA"] = "INSUFFICIENT_DATA"


class CalibrationStatusResponse(BaseModel):
    """Aggregated calibration status across symbols."""
    results: list[CalibrationBiasResponse] = Field(default_factory=list)
    total_symbols: int = 0


# ─── Bias computation thresholds (configurable) ────────────────────────

CALIBRATION_MIN_RECORDS = 20       # Minimum records before bias is meaningful
CALIBRATION_OVER_THRESHOLD = 0.05  # |bias| >= this → OVERCONFIDENT / UNDERCONFIDENT


# ─── Endpoints ─────────────────────────────────────────────────────────


@limiter.limit(RATE_LIMIT_WRITE)
@router.post(
    "/log",
    response_model=CalibrationLogResponse,
    summary="Log a calibration record",
    status_code=201,
)
async def log_calibration(
    request: Request,
    req: CalibrationLogRequest,
    user: TokenData = Depends(require_write),
):
    """
    Insert a calibration record linking a predicted P_win to its actual outcome.

    Called by the Renko pipeline's trade journal when a trade closes.
    Over time this data reveals whether the EV Engine is overconfident,
    underconfident, or well-calibrated for each symbol.
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    record_id = str(uuid.uuid4())
    # Use timezone-naive datetime for Postgres compatibility
    now = datetime.now(timezone.utc).astimezone().replace(tzinfo=None).isoformat()

    row = {
        "id": record_id,
        "symbol": req.symbol,
        "predicted_p_win": req.predicted_p_win,
        "outcome": req.outcome,
        "aggression": req.aggression,
        "actual_pnl": req.actual_pnl,
        "error_source": req.error_source,
        "entry_price": req.entry_price,
        "exit_price": req.exit_price,
        "p_regime": req.p_regime,
        "p_ext_signal": req.p_ext_signal,
        "p_markov": req.p_markov,
        "p_timesfm": req.p_timesfm,
        "entered_at": now,
        "closed_at": now,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = _supabase_url("nt_calibration_records")
            resp = await client.post(url, headers=_supabase_headers(), json=row)

            # ── Schema drift fallback: p_ext_signal vs p_imbalance ─────
            # Migration 100 originally named this column 'p_imbalance'.
            # Migration 024 renamed it to 'p_ext_signal'. If 024 hasn't
            # been applied on this Supabase, the insert fails with
            # PGRST204 mentioning 'p_ext_signal'. Retry once with the
            # legacy column name so the API doesn't hard-fail.
            if resp.status_code == 400 and "p_ext_signal" in resp.text:
                log.warning(
                    "log_calibration: 'p_ext_signal' column not found — "
                    "retrying with legacy 'p_imbalance' name. "
                    "Apply migration 024 to fix."
                )
                row["p_imbalance"] = row.pop("p_ext_signal")
                resp = await client.post(url, headers=_supabase_headers(), json=row)

            if resp.status_code not in (200, 201):
                log.error(
                    "Supabase insert failed: status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to log calibration record: HTTP {resp.status_code}",
                )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")

    return CalibrationLogResponse(
        id=record_id,
        symbol=req.symbol,
        outcome=req.outcome,
        created_at=now,
    )


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/bias/{symbol}",
    response_model=CalibrationBiasResponse,
    summary="Get calibration bias for a symbol",
)
async def get_calibration_bias(
    request: Request,
    symbol: str,
    user: TokenData = Depends(get_authed_user),
):
    """
    Compute calibration bias for a single symbol.

    Bias = AVG(predicted_p_win) - actual_win_rate.

    A positive bias means the system is overconfident (predicts higher
    win rates than it achieves). A negative bias means underconfident.
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch all records for this symbol
            resp = await client.get(
                _supabase_url("nt_calibration_records"),
                headers=_supabase_headers(),
                params={
                    "symbol": f"eq.{symbol}",
                    "select": "predicted_p_win,outcome",
                },
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch calibration records: HTTP {resp.status_code}",
                )

            rows = resp.json()
            if not rows:
                return CalibrationBiasResponse(
                    symbol=symbol,
                    status="INSUFFICIENT_DATA",
                )

            n = len(rows)
            avg_pred = sum(r["predicted_p_win"] for r in rows) / n
            wins = sum(1 for r in rows if r["outcome"] == "win")
            win_rate = wins / n
            bias = round(avg_pred - win_rate, 4)

            if n < CALIBRATION_MIN_RECORDS:
                status = "INSUFFICIENT_DATA"
            elif abs(bias) < CALIBRATION_OVER_THRESHOLD:
                status = "CALIBRATED"
            elif bias >= CALIBRATION_OVER_THRESHOLD:
                status = "OVERCONFIDENT"
            else:
                status = "UNDERCONFIDENT"

            return CalibrationBiasResponse(
                symbol=symbol,
                avg_predicted_p_win=round(avg_pred, 4),
                actual_win_rate=round(win_rate, 4),
                bias=bias,
                n_records=n,
                status=status,
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/status",
    response_model=CalibrationStatusResponse,
    summary="Get daily calibration status for all symbols",
)
async def get_calibration_status(
    request: Request,
    user: TokenData = Depends(get_authed_user),
):
    """
    Get calibration status labels for all symbols that have records.

    Each symbol is labelled as:
      - CALIBRATED         — predictions match outcomes (|bias| < 5%)
      - OVERCONFIDENT      — predictions too optimistic (bias >= +5%)
      - UNDERCONFIDENT     — predictions too pessimistic (bias <= -5%)
      - INSUFFICIENT_DATA  — fewer than 20 records
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _supabase_url("nt_calibration_records"),
                headers=_supabase_headers(),
                params={
                    "select": "symbol,predicted_p_win,outcome",
                    "order": "entered_at.desc",
                },
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to fetch calibration records: HTTP {resp.status_code}",
                )

            rows = resp.json()
            if not rows:
                return CalibrationStatusResponse(results=[])

            # Group by symbol
            from collections import OrderedDict

            grouped: dict[str, dict] = OrderedDict()
            for r in rows:
                sym = r["symbol"]
                if sym not in grouped:
                    grouped[sym] = {"preds": [], "wins": 0, "total": 0}
                grouped[sym]["preds"].append(r["predicted_p_win"])
                grouped[sym]["total"] += 1
                if r["outcome"] == "win":
                    grouped[sym]["wins"] += 1

            results = []
            for sym, data in grouped.items():
                n = data["total"]
                avg_pred = sum(data["preds"]) / n
                win_rate = data["wins"] / n
                bias = round(avg_pred - win_rate, 4)

                if n < CALIBRATION_MIN_RECORDS:
                    status = "INSUFFICIENT_DATA"
                elif abs(bias) < CALIBRATION_OVER_THRESHOLD:
                    status = "CALIBRATED"
                elif bias >= CALIBRATION_OVER_THRESHOLD:
                    status = "OVERCONFIDENT"
                else:
                    status = "UNDERCONFIDENT"

                results.append(
                    CalibrationBiasResponse(
                        symbol=sym,
                        avg_predicted_p_win=round(avg_pred, 4),
                        actual_win_rate=round(win_rate, 4),
                        bias=bias,
                        n_records=n,
                        status=status,
                    )
                )

            return CalibrationStatusResponse(
                results=results,
                total_symbols=len(results),
            )

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")
