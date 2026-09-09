"""
Signal-Stream Local Lookback Cache — SQLite
════════════════════════════════════════════

Worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §6.

The meta-regime model (``meta_regime_model.py``) needs a long, continuous
per-symbol history of signal-stream features (regime label, confidence,
p_win) to fit a sequential model on — much more data than any one sweep
tick has in scope, and far more than it's reasonable to re-query from
Supabase on every 5-15 minute tick. This module is a local, rolling cache
of that history, so the backend reads its own recent past from disk
instead of round-tripping to Supabase for it.

**Format: SQLite, not CSV/JSONL.** The access pattern is a genuinely
rolling window (prune old, append new, range-query for fitting) — SQLite
supports targeted DELETE/INSERT without rewriting the whole file, plus
indexed range queries (`WHERE symbol = ? AND ts > ?`), both of which
CSV/JSONL lack. No new dependency: Python's stdlib `sqlite3`.

**Location, and why it will survive Studio restarts**: stored at
``config/signal_stream_cache.sqlite`` — the SAME convention already used
for other local-mutated runtime state (``config/tuning_registry.yaml``,
``config/brick_params.yaml``; see ``.gitignore``'s comments on both).
Verified via an ALREADY-RELIED-ON precedent rather than a fresh guess:
``HF_HOME=/teamspace/studios/this_studio/hf_cache`` already stores an
883MB TimesFM model checkpoint that survives Studio's frequent process
restarts today (worklog 20260831_TIMESFM_3_UPGRADE_SCOPE.md) — the repo
checkout itself lives on that same persistent Studio volume, so a
relative path within it (like every other ``config/*.yaml`` local-state
file) persists the same way, with no special absolute-path handling
needed.

Blocking (stdlib `sqlite3`, not async-native) — every public function
wraps its DB work in ``asyncio.to_thread`` so callers on the event loop
never block on file I/O, mirroring how this codebase already off-loads
CPU-bound Renko backtests (see worklog 20260908's GIL/ProcessPoolExecutor
lesson — this module doesn't need multiprocessing, sqlite I/O is fast,
but the loop-blocking discipline is the same).

Best-effort throughout: every public function catches and logs, never
raises into the sweep tick — consistent with this module's siblings.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("regime.sweep.signal_stream_cache")

_DB_PATH = Path(__file__).resolve().parents[2] / "config" / "signal_stream_cache.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_stream (
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,
    regime_label TEXT,
    regime_conf REAL,
    p_win REAL,
    kelly_f REAL
);
CREATE INDEX IF NOT EXISTS idx_signal_stream_symbol_ts ON signal_stream(symbol, ts);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _append_tick_sync(row: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO signal_stream (symbol, ts, direction, regime_label, "
            "regime_conf, p_win, kelly_f) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["symbol"], float(row["ts"]), row["direction"],
                row.get("regime_label"), row.get("regime_conf"),
                row.get("p_win"), row.get("kelly_f"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def append_tick(row: dict[str, Any]) -> None:
    """Append one signal-stream observation (best-effort, never raises).

    ``row`` must have ``symbol``, ``ts`` (epoch seconds), ``direction``
    ("buy"/"sell"), and should have ``regime_label``/``regime_conf``/
    ``p_win``/``kelly_f`` (any of these may be ``None``).
    """
    try:
        await asyncio.to_thread(_append_tick_sync, row)
    except Exception as exc:
        logger.warning("signal_stream_cache: append_tick failed for %s: %s", row.get("symbol"), exc)


def _fetch_history_sync(symbol: str, since_ts: float | None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        if since_ts is not None:
            cur = conn.execute(
                "SELECT ts, direction, regime_label, regime_conf, p_win, kelly_f "
                "FROM signal_stream WHERE symbol = ? AND ts >= ? ORDER BY ts",
                (symbol, since_ts),
            )
        else:
            cur = conn.execute(
                "SELECT ts, direction, regime_label, regime_conf, p_win, kelly_f "
                "FROM signal_stream WHERE symbol = ? ORDER BY ts",
                (symbol,),
            )
        cols = ["ts", "direction", "regime_label", "regime_conf", "p_win", "kelly_f"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


async def fetch_history(symbol: str, since_ts: float | None = None) -> list[dict[str, Any]]:
    """Read this symbol's cached tick history (optionally since ``since_ts``,
    epoch seconds), ordered oldest-first. Returns ``[]`` on any failure —
    never raises. A caller needing to fit a model on empty history should
    treat that as "not enough data yet," not an error."""
    try:
        return await asyncio.to_thread(_fetch_history_sync, symbol, since_ts)
    except Exception as exc:
        logger.warning("signal_stream_cache: fetch_history failed for %s: %s", symbol, exc)
        return []


def _prune_older_than_sync(cutoff_ts: float) -> int:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM signal_stream WHERE ts < ?", (cutoff_ts,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


async def prune_older_than(cutoff_ts: float) -> int:
    """Delete cached rows older than ``cutoff_ts`` (epoch seconds).
    Returns the number of rows deleted, or 0 on failure (never raises)."""
    try:
        return await asyncio.to_thread(_prune_older_than_sync, cutoff_ts)
    except Exception as exc:
        logger.warning("signal_stream_cache: prune_older_than failed: %s", exc)
        return 0


def _row_count_sync(symbol: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM signal_stream WHERE symbol = ?", (symbol,))
        return int(cur.fetchone()[0])
    finally:
        conn.close()


async def row_count(symbol: str) -> int:
    """Cheap count of cached rows for ``symbol`` — used to decide whether
    there's enough local history to fit on before bothering. Returns 0 on
    failure (never raises)."""
    try:
        return await asyncio.to_thread(_row_count_sync, symbol)
    except Exception as exc:
        logger.warning("signal_stream_cache: row_count failed for %s: %s", symbol, exc)
        return 0


async def resync_from_supabase(symbol: str, days: int = 90) -> int:
    """Self-heal the local cache from Supabase's ``nt_signal_sim`` after a
    wipe (e.g. a fresh Studio volume) or on first run for a symbol.
    Supabase remains the durable source of truth; this cache is a fast
    local working set that can always be rebuilt from it. Returns the
    number of rows pulled in, or 0 on failure (never raises).
    """
    try:
        import httpx
        from datetime import datetime, timedelta, timezone

        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "symbol,ts,signal,regime,regime_conf,p_win,kelly_f",
                    "ts": f"gte.{cutoff}",
                    "symbol": f"eq.{symbol}",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list) or not data:
            return 0

        def _write_all() -> int:
            conn = _connect()
            try:
                conn.executemany(
                    "INSERT INTO signal_stream (symbol, ts, direction, regime_label, "
                    "regime_conf, p_win, kelly_f) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            row["symbol"],
                            datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")).timestamp(),
                            row.get("signal"), row.get("regime"),
                            row.get("regime_conf"), row.get("p_win"), row.get("kelly_f"),
                        )
                        for row in data
                    ],
                )
                conn.commit()
                return len(data)
            finally:
                conn.close()

        return await asyncio.to_thread(_write_all)
    except Exception as exc:
        logger.warning("signal_stream_cache: resync_from_supabase failed for %s: %s", symbol, exc)
        return 0
