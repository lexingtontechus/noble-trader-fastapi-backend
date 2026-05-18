"""
Backtest result persistence layer.

Stores backtest results in Supabase (ta_backtest_result table) for
history retrieval, comparison, and audit trail.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..core.settings import settings


# ---------------------------------------------------------------------------
# Supabase REST helper
# ---------------------------------------------------------------------------

def _supabase_headers() -> dict[str, str]:
    """Build headers for Supabase PostgREST API calls."""
    return {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _supabase_url() -> str:
    """Build the base Supabase URL for the backtest table."""
    return f"{settings.SUPABASE_URL}/rest/v1/ta_backtest_result"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def save_backtest_result(
    user_id: str,
    result_dict: dict,
    config_used: dict,
    symbol: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Persist a backtest result to Supabase.

    Returns (record_id, created_at) on success, (None, None) on failure.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        # Supabase not configured — skip persistence silently
        return None, None

    record_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Separate heavy data into JSONB columns to keep the row lean
    row = {
        "id": record_id,
        "user_id": user_id,
        "symbol": symbol,
        "n_trades": result_dict.get("n_trades", 0),
        "win_rate": result_dict.get("win_rate", 0.0),
        "total_return": result_dict.get("total_return", 0.0),
        "annual_return": result_dict.get("annual_return", 0.0),
        "sharpe_ratio": result_dict.get("sharpe_ratio", 0.0),
        "sortino_ratio": result_dict.get("sortino_ratio", 0.0),
        "calmar_ratio": result_dict.get("calmar_ratio", 0.0),
        "max_drawdown": result_dict.get("max_drawdown", 0.0),
        "profit_factor": result_dict.get("profit_factor", 0.0),
        "n_hmm_states": config_used.get("n_hmm_states", 4),
        "config_used": json.dumps(config_used),
        "summary_metrics": json.dumps({
            k: v for k, v in result_dict.items()
            if k not in ("equity_curve", "drawdown_curve", "trade_log",
                         "regime_distribution", "trades_by_regime", "config_used")
        }),
        "regime_distribution": json.dumps(result_dict.get("regime_distribution", {})),
        "trades_by_regime": json.dumps(result_dict.get("trades_by_regime", {})),
        "equity_curve": json.dumps(result_dict.get("equity_curve", [])),
        "drawdown_curve": json.dumps(result_dict.get("drawdown_curve", [])),
        "trade_log": json.dumps(result_dict.get("trade_log", [])),
        "created_at": now,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _supabase_url(),
                headers=_supabase_headers(),
                json=row,
            )
            if resp.status_code in (200, 201):
                # Prefer: return=representation returns the inserted row
                try:
                    inserted = resp.json()
                    if isinstance(inserted, list) and inserted:
                        created_at = inserted[0].get("created_at", now)
                    elif isinstance(inserted, dict):
                        created_at = inserted.get("created_at", now)
                    else:
                        created_at = now
                except Exception:
                    created_at = now
                return record_id, created_at
            # Log but don't crash
            print(f"[backtest_store] Supabase insert failed: {resp.status_code} {resp.text[:200]}")
            return None, None
    except Exception as exc:
        print(f"[backtest_store] Supabase save error: {exc}")
        return None, None


async def get_backtest_history(
    user_id: str,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """
    Fetch paginated backtest history for a user.

    Returns {"results": [...], "total": int, "offset": int, "limit": int}.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return {"results": [], "total": 0, "offset": offset, "limit": limit}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get total count
            count_resp = await client.get(
                _supabase_url(),
                headers={**_supabase_headers(), "Prefer": "count=exact"},
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "id",
                    "order": "created_at.desc",
                    "limit": 1,
                },
            )
            total = int(count_resp.headers.get("content-range", "*/0").split("/")[1].strip())

            # Get page
            resp = await client.get(
                _supabase_url(),
                headers=_supabase_headers(),
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "id,symbol,n_trades,win_rate,total_return,annual_return,sharpe_ratio,max_drawdown,profit_factor,n_hmm_states,created_at",
                    "order": "created_at.desc",
                    "offset": offset,
                    "limit": limit,
                },
            )
            rows = resp.json() if resp.status_code == 200 else []
            return {"results": rows, "total": total, "offset": offset, "limit": limit}
    except Exception as exc:
        print(f"[backtest_store] Supabase history error: {exc}")
        return {"results": [], "total": 0, "offset": offset, "limit": limit}


async def get_backtest_detail(backtest_id: str, user_id: str) -> Optional[dict]:
    """
    Fetch a single backtest result by ID (scoped to user).

    Returns the full result dict or None.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _supabase_url(),
                headers=_supabase_headers(),
                params={
                    "id": f"eq.{backtest_id}",
                    "user_id": f"eq.{user_id}",
                    "limit": 1,
                },
            )
            rows = resp.json() if resp.status_code == 200 else []
            if not rows:
                return None
            row = rows[0]
            # Parse JSONB columns back
            for key in ("config_used", "summary_metrics", "regime_distribution",
                        "trades_by_regime", "equity_curve", "drawdown_curve", "trade_log"):
                if key in row and isinstance(row[key], str):
                    row[key] = json.loads(row[key])
            # Merge summary_metrics into the top level
            if "summary_metrics" in row and isinstance(row["summary_metrics"], dict):
                row.update(row["summary_metrics"])
                del row["summary_metrics"]
            return row
    except Exception as exc:
        print(f"[backtest_store] Supabase detail error: {exc}")
        return None


async def delete_backtest(backtest_id: str, user_id: str) -> bool:
    """
    Delete a backtest result by ID (scoped to user).

    Returns True if deleted, False otherwise.
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(
                _supabase_url(),
                headers=_supabase_headers(),
                params={
                    "id": f"eq.{backtest_id}",
                    "user_id": f"eq.{user_id}",
                },
            )
            return resp.status_code in (200, 204)
    except Exception as exc:
        print(f"[backtest_store] Supabase delete error: {exc}")
        return False
