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
    venue: Optional[str] = Field(None, description="Exchange name (e.g., Coinbase, OANDA)")
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
    """Per-symbol calibration bias.

    2026-08-22 (audit fix #2): this now mirrors the SAME windowed query and
    thresholds the live enforcement loop uses (``fetch_calibration_bias`` /
    ``fetch_calibration_bias_raw`` in ``sweep_orchestrator/supabase_client.py``,
    classified via ``eod_correction._classify_bias`` /
    ``_classify_bias_raw``) instead of a separately-implemented all-time
    query with its own hardcoded ±0.05 threshold. Both the enforced view
    (``bias``/``status``, tight ±0.10 band, what's actually live) and the
    raw view (``bias_raw``/``status_raw``, wide +0.30/−0.20 band, true model
    drift) are returned side by side, labeled, instead of one ambiguous
    number — see worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md.
    """
    symbol: str
    window_days: int = 7
    # ── Enforced view (predicted_p_win — what's actually live) ──────────
    avg_predicted_p_win: float = 0.0
    actual_win_rate: float = 0.0
    bias: float = 0.0  # positive = overconfident
    n_records: int = 0
    status: Literal["CALIBRATED", "OVERCONFIDENT", "UNDERCONFIDENT", "INSUFFICIENT_DATA"] = "INSUFFICIENT_DATA"
    # ── Raw view (predicted_p_win_raw — true model drift, pre-enforcement) ──
    avg_predicted_p_win_raw: Optional[float] = None
    bias_raw: Optional[float] = None
    status_raw: Literal["CALIBRATED", "OVERCONFIDENT", "UNDERCONFIDENT", "INSUFFICIENT_DATA"] = "INSUFFICIENT_DATA"
    # ── Thresholds actually applied (for callers that want to display them) ──
    threshold_enforced: float = 0.10
    threshold_raw_overconfident: float = 0.30
    threshold_raw_underconfident: float = -0.20


class CalibrationStatusResponse(BaseModel):
    """Aggregated calibration status across symbols."""
    results: list[CalibrationBiasResponse] = Field(default_factory=list)
    total_symbols: int = 0


# ─── Bias computation ───────────────────────────────────────────────────
# 2026-08-22 (audit fix #2): thresholds and windowing are no longer defined
# here — they come from settings.EOD_BIAS_ENFORCED_THRESHOLD /
# EOD_BIAS_RAW_OVERCONFIDENT / EOD_BIAS_RAW_UNDERCONFIDENT / EOD_BIAS_WINDOW_DAYS
# / EOD_BIAS_MIN_RECORDS, and classification reuses
# eod_correction._classify_bias / _classify_bias_raw — the exact functions
# the live enforcement loop calls. This endpoint can no longer silently
# diverge from what's actually being enforced.


def _build_bias_response(
    symbol: str,
    enforced: dict | None,
    raw: dict | None,
    window_days: int,
) -> "CalibrationBiasResponse":
    """Assemble a CalibrationBiasResponse from the enforced/raw fetch results.

    Shared by ``get_calibration_bias`` and ``get_calibration_status`` so the
    two endpoints can never compute this differently.
    """
    from regime_platform.sweep_orchestrator.eod_correction import (
        _classify_bias,
        _classify_bias_raw,
        _settings_number,
    )

    # Registry-tunable (Layer 1, 2026-08-23) — read the SAME way
    # _classify_bias/_classify_bias_raw do, so what's displayed here can
    # never disagree with what's actually enforced. See
    # worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.2.1.
    fields: dict = {
        "symbol": symbol,
        "window_days": window_days,
        "threshold_enforced": _settings_number(
            "EOD_BIAS_ENFORCED_THRESHOLD", settings.EOD_BIAS_ENFORCED_THRESHOLD
        ),
        "threshold_raw_overconfident": _settings_number(
            "EOD_BIAS_RAW_OVERCONFIDENT", getattr(settings, "EOD_BIAS_RAW_OVERCONFIDENT", 0.30)
        ),
        "threshold_raw_underconfident": _settings_number(
            "EOD_BIAS_RAW_UNDERCONFIDENT", getattr(settings, "EOD_BIAS_RAW_UNDERCONFIDENT", -0.20)
        ),
    }
    if enforced is not None:
        fields.update(
            avg_predicted_p_win=enforced["avg_pred"],
            actual_win_rate=enforced["win_rate"],
            bias=enforced["bias"],
            n_records=enforced["n"],
            status=_classify_bias(enforced["bias"]),
        )
    if raw is not None:
        fields.update(
            avg_predicted_p_win_raw=raw.get("avg_pred_raw"),
            bias_raw=raw["bias"],
            status_raw=_classify_bias_raw(raw["bias"]),
        )
        fields.setdefault("n_records", raw["n"])
    return CalibrationBiasResponse(**fields)


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
        "venue": req.venue,
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
    venue: Optional[str] = None,
    window_days: Optional[int] = None,
    user: TokenData = Depends(get_authed_user),
):
    """
    Compute calibration bias for a single symbol — using the SAME rolling
    window and thresholds the live enforcement loop uses, so this endpoint
    can never silently disagree with what's actually being enforced.

    Returns both views:
      - Enforced (``bias``/``status``): reads ``predicted_p_win`` (the
        deployed, possibly-Bayesian-shrunk value), tight ±0.10 band.
      - Raw (``bias_raw``/``status_raw``): reads ``predicted_p_win_raw``
        (the pre-enforcement model output), wide +0.30/−0.20 band — this is
        "is the model itself still miscalibrated," independent of
        enforcement compensation.

    ``window_days`` defaults to ``settings.EOD_BIAS_WINDOW_DAYS`` (7 days,
    the same window the enforcement loop uses). Pass ``0`` for legacy
    all-time (unwindowed) behavior.
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_calibration_bias,
        fetch_calibration_bias_raw,
    )

    win_days = window_days if window_days is not None else settings.EOD_BIAS_WINDOW_DAYS

    try:
        enforced = await fetch_calibration_bias(symbol, venue=venue, window_days=win_days)
        raw = await fetch_calibration_bias_raw(symbol, venue=venue, window_days=win_days)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Supabase request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Supabase request failed: {exc}")

    return _build_bias_response(symbol, enforced, raw, win_days)


@limiter.limit(RATE_LIMIT_DEFAULT)
@router.get(
    "/status",
    response_model=CalibrationStatusResponse,
    summary="Get daily calibration status for all symbols",
)
async def get_calibration_status(
    request: Request,
    window_days: Optional[int] = None,
    user: TokenData = Depends(get_authed_user),
):
    """
    Calibration status for every active symbol — same windowed query and
    thresholds as ``get_calibration_bias`` (see its docstring). Each
    result carries both the enforced view (``status``) and the raw view
    (``status_raw``):
      - CALIBRATED         — within the calibrated band
      - OVERCONFIDENT      — predictions too optimistic
      - UNDERCONFIDENT     — predictions too pessimistic
      - INSUFFICIENT_DATA  — fewer than ``settings.EOD_BIAS_MIN_RECORDS``
        records in the window
    """
    if not await _db_available():
        raise HTTPException(status_code=503, detail="Supabase not configured")

    from regime_platform.sweep_orchestrator.supabase_client import (
        fetch_active_symbols,
        fetch_calibration_bias,
        fetch_calibration_bias_raw,
    )

    win_days = window_days if window_days is not None else settings.EOD_BIAS_WINDOW_DAYS

    try:
        symbols = await fetch_active_symbols()
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch active symbols: {exc}")

    results: list[CalibrationBiasResponse] = []
    for sym in symbols:
        symbol = sym.get("symbol") if isinstance(sym, dict) else None
        if not symbol:
            continue
        try:
            enforced = await fetch_calibration_bias(symbol, window_days=win_days)
            raw = await fetch_calibration_bias_raw(symbol, window_days=win_days)
        except (httpx.TimeoutException, httpx.RequestError):
            enforced = raw = None
        results.append(_build_bias_response(symbol, enforced, raw, win_days))

    return CalibrationStatusResponse(results=results, total_symbols=len(results))
