"""
Supabase REST Client for the Sweep Orchestrator
═════════════════════════════════════════════════

Thin wrapper around httpx for reading/writing Supabase PostgREST tables.
Follows the same pattern as existing backend modules (calibration.py, venue.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.sweep.supabase")


# ─── Internal helpers ─────────────────────────────────────────────

def _headers() -> dict[str, str]:
    key = settings.SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _url(table: str) -> str:
    return f"{settings.SUPABASE_URL}/rest/v1/{table}"


# ─── Public API ───────────────────────────────────────────────────


async def fetch_active_symbols() -> list[dict[str, Any]]:
    """Return all rows from ``nt_symbol`` where ``active = true``."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        logger.warning("Supabase not configured — cannot fetch symbols")
        return []

    url = _url("nt_symbol")
    params = {
        "select": "symbol,asset_class,data_source,regime_cron,heartbeat_ttl_seconds,"
                  "yahoo_ticker,hyperliquid_coin,alpaca_symbol",
        "active": "eq.true",
        "order": "asset_class.asc,symbol.asc",
    }
    print(f"[sb] Querying {url} with params={params}", flush=True)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()
            logger.info("Fetched %d active symbols from nt_symbol", len(rows))
            print(f"[sb] Got {len(rows)} symbols: {[r.get('symbol') for r in rows]}", flush=True)
            return rows
    except Exception as exc:
        logger.error("Failed to fetch active symbols: %s", exc)
        print(f"[sb] FAILED: {exc}", flush=True)
        return []


async def log_regime_entry(
    symbol: str,
    source: str,
    regime: str,
    regime_conf: float,
    markov_p_up: float,
    markov_p_dn: float,
    kelly_f: float,
    prev_regime: str | None = None,
    regime_shift: bool = False,
) -> None:
    """Insert a row into ``nt_regime_log``."""
    if not settings.SUPABASE_URL:
        return

    payload = {
        "symbol": symbol,
        "source": source,
        "regime": regime,
        "regime_conf": round(regime_conf, 4),
        "markov_p_up": round(markov_p_up, 4),
        "markov_p_dn": round(markov_p_dn, 4),
        "kelly_f": round(kelly_f, 6),
        "prev_regime": prev_regime,
        "regime_shift": regime_shift,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_regime_log"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=payload,
            )
            if not resp.is_success:
                logger.warning("log_regime_entry %s: HTTP %d", symbol, resp.status_code)
    except Exception as exc:
        logger.warning("log_regime_entry %s failed: %s", symbol, exc)


async def log_sweep_result(payload: dict[str, Any]) -> None:
    """Insert a row into ``nt_sweep_result``."""
    if not settings.SUPABASE_URL:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_sweep_result"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json=payload,
            )
            if not resp.is_success:
                logger.warning("log_sweep_result: HTTP %d", resp.status_code)
    except Exception as exc:
        logger.warning("log_sweep_result failed: %s", exc)


async def log_error(symbol: str, fn_name: str, error_msg: str) -> None:
    """Insert a row into ``nt_errors``."""
    if not settings.SUPABASE_URL:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                _url("nt_errors"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json={
                    "symbol": symbol,
                    "fn_name": fn_name,
                    "error_msg": error_msg[:1000],
                },
            )
            if not resp.is_success:
                logger.warning("log_error %s: HTTP %d", symbol, resp.status_code)
    except Exception as exc:
        logger.warning("log_error %s failed: %s", symbol, exc)


async def fetch_calibration_bias(
    symbol: str,
) -> dict[str, float | str] | None:
    """Query nt_calibration_records and return calibration bias.

    Returns ``{bias: float, status: str}`` or ``None`` on failure.
    """
    if not settings.SUPABASE_URL:
        return None

    url = _url("nt_calibration_records")
    params = {
        "select": "predicted_p_win,outcome",
        "symbol": f"eq.{symbol}",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("fetch_calibration_bias %s failed: %s", symbol, exc)
        return None

    if not isinstance(data, list) or len(data) < 20:
        return None

    n = len(data)
    sum_pred = 0.0
    wins = 0
    for row in data:
        sum_pred += row.get("predicted_p_win", 0.5) or 0.5
        if row.get("outcome") == "win":
            wins += 1

    avg_pred = sum_pred / n
    win_rate = wins / n
    bias = round(avg_pred - win_rate, 4)

    if abs(bias) < 0.05:
        status = "CALIBRATED"
    elif bias >= 0.05:
        status = "OVERCONFIDENT"
    else:
        status = "UNDERCONFIDENT"

    return {"bias": bias, "status": status}
