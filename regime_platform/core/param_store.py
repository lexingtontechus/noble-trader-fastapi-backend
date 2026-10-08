"""
param_store.py — Local parameter store: the single source of truth for
runtime-mutated parameters.
═══════════════════════════════════════════════════════════════════════════

Worklog 20261008_PARAM_SOURCE_OF_TRUTH.md §3.

THE PROBLEM THIS SOLVES
-----------------------
Optimization processes were retrieving their own freshly-computed
parameters back out of Supabase to run. `dynamic_tier_optimizer.py`
computed dynamic ATR / deadline / regime-edge / incubation parameters and
wrote them to the `nt_dynamic_tier_state` table — a network round-trip to
a DB that the sweep then had to read back, and (as measured 2026-10-08)
never actually did. Parameters are *local runtime state*, not remote data:
the process that computes them and the process that consumes them run in
the SAME Studio process, on the SAME persistent volume.

THE RULE (single source of truth)
---------------------------------
  • `config/*` (YAML + SQLite) is the RUNTIME AUTHORITY for parameters.
    Hot paths read local files/SQLite — never Supabase — to run.
  • Supabase tables are the DURABLE AUDIT TRAIL + cross-instance fallback.
    A mirror, never the read path for an optimization process.
  • The mirror is best-effort: a Supabase outage never blocks a sweep.
  • `parity_report()` detects divergence between local and Supabase so
    "single source of truth" is *enforced*, not merely asserted.

This is the SAME shape already relied on elsewhere in this codebase
(`config/signal_stream_cache.sqlite`, `config/tuning_registry.yaml`,
`config/brick_params.yaml`): a local, gitignored, hot-reloadable working
set with Supabase as the durable record. This module generalizes it for
the dynamic-tier family.

STORAGE
-------
`config/param_store.sqlite` — stdlib `sqlite3`, no new dependency. Same
persistence argument as `signal_stream_cache.sqlite`: the repo checkout
lives on Studio's persistent volume, so a relative path within it survives
the frequent process restarts. Gitignored (live-mutated runtime state).

Blocking sqlite I/O is off-loaded via `asyncio.to_thread`, mirroring
`signal_stream_cache.py`. Best-effort throughout: every public function
catches and logs, never raises into the sweep tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("regime.core.param_store")

_DB_PATH = Path(__file__).resolve().parents[2] / "config" / "param_store.sqlite"

# Tier types written by dynamic_tier_optimizer.py (kept here so the
# optimizer, the sweep reader and the parity check agree on the vocabulary).
TIER_DYNAMIC_ATR = "dynamic_atr"
TIER_DYNAMIC_DEADLINE = "dynamic_deadline"
TIER_REGIME_EDGE = "regime_edge"
TIER_GEOMETRY_INCUBATION = "geometry_incubation"
# Brick-state tier (2026-10-08, FU1): the heavy sweep's
# nt_brick_params_state payload (brick_size/sl/tp + atr_at_write) — read by
# the light path's geometry_intraday_scale (live), previously a per-signal
# Supabase round-trip. Keyed on tier_key="latest".
TIER_BRICK_STATE = "brick_state"
ALL_TIER_TYPES = (
    TIER_DYNAMIC_ATR,
    TIER_DYNAMIC_DEADLINE,
    TIER_REGIME_EDGE,
    TIER_GEOMETRY_INCUBATION,
    TIER_BRICK_STATE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS params (
    symbol      TEXT NOT NULL,
    tier_type   TEXT NOT NULL,
    tier_key    TEXT NOT NULL,
    tier_value  TEXT,
    n           INTEGER,
    computed_at TEXT,
    source      TEXT,
    PRIMARY KEY (symbol, tier_type, tier_key)
);
CREATE INDEX IF NOT EXISTS idx_params_symbol_type ON params(symbol, tier_type);
"""


def db_path() -> Path:
    """Resolve the SQLite path (env override for tests / alternate volumes)."""
    import os

    env_path = os.getenv("PARAM_STORE_SQLITE")
    if env_path:
        return Path(env_path)
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _normalise_value(value: Any) -> str:
    """Serialize a tier value to TEXT (the column is TEXT; floats round-trip)."""
    if isinstance(value, (int, float)):
        return repr(float(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return "" if value is None else str(value)


# ── Writes (local authority) ─────────────────────────────────────────────

def _write_params_sync(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    conn = _connect()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = []
        for r in rows:
            symbol = r.get("symbol")
            tier_type = r.get("tier_type")
            tier_key = r.get("tier_key")
            if not symbol or not tier_type or tier_key is None:
                continue
            payload.append((
                str(symbol), str(tier_type), str(tier_key),
                _normalise_value(r.get("tier_value")),
                int(r.get("n") or 0),
                r.get("computed_at") or now_iso,
                r.get("source") or "param_store",
            ))
        if not payload:
            return 0
        conn.executemany(
            "INSERT INTO params (symbol, tier_type, tier_key, tier_value, n, "
            "computed_at, source) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, tier_type, tier_key) DO UPDATE SET "
            "tier_value=excluded.tier_value, n=excluded.n, "
            "computed_at=excluded.computed_at, source=excluded.source",
            payload,
        )
        conn.commit()
        return len(payload)
    finally:
        conn.close()


async def write_params(rows: list[dict[str, Any]]) -> int:
    """Write parameter rows to the LOCAL store — the runtime authority.

    This is the write the sweep and the optimizer must go through. The
    Supabase mirror is a separate, best-effort call (`sync_to_supabase`).
    Returns the number of rows written (best-effort: 0 on failure).
    """
    try:
        return await asyncio.to_thread(_write_params_sync, rows)
    except Exception as exc:
        logger.warning("param_store: write_params failed: %s", exc)
        return 0


# ── Reads (local authority) ──────────────────────────────────────────────

def _read_all_sync() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT symbol, tier_type, tier_key, tier_value, n, computed_at, "
            "source FROM params ORDER BY symbol, tier_type, tier_key"
        )
        return [
            {
                "symbol": row[0], "tier_type": row[1], "tier_key": row[2],
                "tier_value": row[3], "n": row[4], "computed_at": row[5],
                "source": row[6],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def read_all() -> list[dict[str, Any]]:
    """Read every stored parameter (best-effort: [] on failure)."""
    try:
        return _read_all_sync()
    except Exception as exc:
        logger.warning("param_store: read_all failed: %s", exc)
        return []


def read_tier(symbol: str, tier_type: str) -> dict[str, dict[str, Any]]:
    """Read one tier for one symbol: ``{tier_key: row}``.

    The sweep-facing read. Local-only — never touches the network.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in read_all():
        if row["symbol"] == symbol and row["tier_type"] == tier_type:
            out[str(row["tier_key"])] = row
    return out


def read_value(symbol: str, tier_type: str, tier_key: str) -> Any | None:
    """Read a single parameter value (float where numeric, else raw TEXT).

    Local-only. Returns ``None`` when the key is absent — callers fall
    back to their own computation (fail-open), never to a DB round-trip.
    """
    rows = read_tier(symbol, tier_type)
    row = rows.get(str(tier_key))
    if row is None:
        return None
    raw = row.get("tier_value")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def read_dict(symbol: str, tier_type: str, tier_key: str) -> dict[str, Any] | None:
    """Read a parameter stored as a JSON object (e.g. regime_edge carries
    both ``win_rate`` and ``expectancy_r``). Local-only; ``None`` when the
    key is absent or not a JSON object."""
    rows = read_tier(symbol, tier_type)
    row = rows.get(str(tier_key))
    if row is None:
        return None
    raw = row.get("tier_value")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_float(symbol: str, tier_type: str, tier_key: str) -> float | None:
    """Like ``read_value`` but only returns a float (None for non-numeric)."""
    v = read_value(symbol, tier_type, tier_key)
    return float(v) if isinstance(v, float) else None


def stats() -> dict[str, Any]:
    """Row counts by tier type + newest computed_at (diagnostics)."""
    rows = read_all()
    by_type: dict[str, int] = {}
    newest = None
    for r in rows:
        by_type[r["tier_type"]] = by_type.get(r["tier_type"], 0) + 1
        ts = r.get("computed_at")
        if ts and (newest is None or ts > newest):
            newest = ts
    return {"total": len(rows), "by_tier_type": by_type, "newest_computed_at": newest}


# ── Supabase mirror (audit + cross-instance fallback) ────────────────────

async def sync_to_supabase() -> dict[str, Any]:
    """Mirror the local store into ``nt_dynamic_tier_state`` (best-effort).

    This is the AUDIT path — it must never be the read path for an
    optimization. A failure here is logged and does not affect the local
    authority, matching every other best-effort write in this codebase.
    """
    rows = read_all()
    if not rows:
        return {"mirrored": 0, "ok": True, "note": "empty_local_store"}
    # nt_dynamic_tier_state.tier_value is TEXT — normalise any JSON-object
    # values (regime_edge carries win_rate + expectancy_r) to a JSON string
    # before the PostgREST upsert, which rejects a dict for a text column.
    mirror_rows = [
        {**r, "tier_value": _normalise_value(r.get("tier_value"))}
        for r in rows
    ]
    try:
        from regime_platform.sweep_orchestrator.supabase_client import (
            upsert_dynamic_tier_state,
        )

        await upsert_dynamic_tier_state(mirror_rows)
        return {"mirrored": len(mirror_rows), "ok": True}
    except Exception as exc:
        logger.warning("param_store: sync_to_supabase failed: %s", exc)
        return {"mirrored": 0, "ok": False, "error": str(exc)}


async def _fetch_supabase_rows(symbols: list[str] | None) -> list[dict[str, Any]]:
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    if not settings.SUPABASE_URL:
        return []
    params: dict[str, str] = {
        "select": "symbol,tier_type,tier_key,tier_value,n,computed_at,source",
        "order": "symbol.asc",
        "limit": "2000",
    }
    if symbols:
        params["symbol"] = f"in.({','.join(symbols)})"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            _url("nt_dynamic_tier_state"), headers=_headers(), params=params
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def sync_from_supabase(symbols: list[str] | None = None) -> int:
    """Self-heal the local store from the Supabase mirror (best-effort).

    Cold-boot path only: a fresh Studio instance whose ``param_store.sqlite``
    does not exist yet (or a first-ever deploy) can recover the last known
    parameters from the audit mirror rather than recomputing from scratch.
    Mirrors ``signal_stream_cache.resync_from_supabase``.
    """
    try:
        rows = await _fetch_supabase_rows(symbols)
    except Exception as exc:
        logger.warning("param_store: sync_from_supabase fetch failed: %s", exc)
        return 0
    return await write_params(rows)


# ── Parity enforcement (single source of truth, detectable divergence) ───

async def parity_report(symbols: list[str] | None = None) -> dict[str, Any]:
    """Compare the local authority against the Supabase mirror.

    Returns a report with the key sets that disagree and the values that
    differ. A non-empty ``missing_in_mirror`` / ``missing_local`` /
    ``value_mismatch`` means the two stores have diverged — the local
    store remains authoritative, and the operator should re-run
    ``sync_to_supabase()`` (or investigate a concurrent writer).
    """
    local = {
        (r["symbol"], r["tier_type"], str(r["tier_key"])): r
        for r in read_all()
    }
    try:
        remote_rows = await _fetch_supabase_rows(symbols)
    except Exception as exc:
        logger.warning("param_store: parity_report fetch failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "local_keys": len(local),
            "missing_in_mirror": [],
            "missing_local": [],
            "value_mismatch": [],
        }

    remote = {
        (r["symbol"], r["tier_type"], str(r["tier_key"])): r
        for r in remote_rows
    }

    local_keys = set(local)
    remote_keys = set(remote)
    missing_in_mirror = sorted(local_keys - remote_keys)
    missing_local = sorted(remote_keys - local_keys)

    def _norm(v: Any) -> str:
        try:
            return repr(float(v))
        except (TypeError, ValueError):
            return "" if v is None else str(v)

    value_mismatch = []
    for key in sorted(local_keys & remote_keys):
        if _norm(local[key].get("tier_value")) != _norm(remote[key].get("tier_value")):
            value_mismatch.append({
                "key": list(key),
                "local": local[key].get("tier_value"),
                "mirror": remote[key].get("tier_value"),
            })

    return {
        "ok": not (missing_in_mirror or missing_local or value_mismatch),
        "local_keys": len(local),
        "mirror_keys": len(remote),
        "missing_in_mirror": [list(k) for k in missing_in_mirror[:50]],
        "missing_local": [list(k) for k in missing_local[:50]],
        "value_mismatch": value_mismatch[:50],
        "counts": {
            "missing_in_mirror": len(missing_in_mirror),
            "missing_local": len(missing_local),
            "value_mismatch": len(value_mismatch),
        },
    }


# ── Test helpers ─────────────────────────────────────────────────────────

def clear() -> None:
    """Delete every row (tests only)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM params")
        conn.commit()
    finally:
        conn.close()


# ── Scheduled parity enforcement (nightly EOD, FU2 2026-10-08) ───────────

async def run_parity_check() -> dict[str, Any]:
    """Nightly enforcement wrapper: parity_report() + an nt_eod_actions audit
    row when the local authority and the Supabase mirror have diverged.

    Turns the single-source-of-truth rule into a MONITORED signal instead of
    an on-demand check. Best-effort: never raises. The audit row uses the
    ``parameter_decay_check`` action type (the nt_eod_actions CHECK
    constraint's only audit-only value) and carries the divergence counts +
    sample keys in ``detail``.

    One row per diverged key is NOT written — parity is a system-level
    property, so a single row keyed on a sentinel symbol ``__param_store__``
    carries the whole report (the idx_eod_actions_symbol_ts index still
    works; audit-only writes never trigger enforcement, see Known Pitfall 17).
    """
    report = await parity_report()
    if report.get("ok"):
        logger.info("[param-store] parity OK — local and mirror agree (%d keys)",
                    report.get("local_keys", 0))
        return report

    counts = report.get("counts") or {}
    logger.warning(
        "[param-store] parity DIVERGENCE — missing_in_mirror=%d missing_local=%d "
        "value_mismatch=%d",
        counts.get("missing_in_mirror", 0),
        counts.get("missing_local", 0),
        counts.get("value_mismatch", 0),
    )
    try:
        from regime_platform.sweep_orchestrator.geometry_risk import write_eod_action

        await write_eod_action(
            "__param_store__",
            "parameter_decay_check",
            {
                "source": "param_store_parity",
                "local_keys": report.get("local_keys"),
                "mirror_keys": report.get("mirror_keys"),
                "missing_in_mirror": counts.get("missing_in_mirror", 0),
                "missing_local": counts.get("missing_local", 0),
                "value_mismatch": counts.get("value_mismatch", 0),
                "sample_missing_in_mirror": report.get("missing_in_mirror", [])[:5],
                "sample_value_mismatch": report.get("value_mismatch", [])[:5],
                "error": report.get("error"),
            },
        )
    except Exception as exc:
        logger.warning("param_store: parity audit row failed: %s", exc)
    return report
