"""
TimesFM z-map — per-symbol standardized forecast-error → probability
═════════════════════════════════════════════════════════════════════

worklog/20260917_TIMESFM_ZMAP_FIX.md

Live probes showed TimesFM forecasts differ from last close by ~0.01–0.1%,
which a global k-sigmoid maps to p≈0.5. This module converts rel_change to
a probability via the symbol's own rolling σ of historical rel_change:

    z = rel_change / max(σ, σ_floor)
    p = Φ(z)

Local SQLite under data/ so the distribution survives process restarts
(same pattern as signal_stream_cache). Pure math is offline-testable.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("regime.sweep.timesfm_zmap")

# Minimum samples before z-map is trusted; below this return None (use sigmoid).
MIN_SAMPLES = 30
# Floor on σ so a near-zero historical spread cannot explode z.
SIGMA_FLOOR = 1e-5
# Cap |z| so Φ(z) stays in (eps, 1-eps) numerically.
Z_CAP = 5.0

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "timesfm_relchange.sqlite"

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[Path] = None


def normal_cdf(z: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _ensure_conn(db_path: Path | None = None) -> sqlite3.Connection:
    global _conn, _db_path
    path = db_path or _DEFAULT_DB
    if _conn is not None and _db_path == path:
        return _conn
    with _lock:
        if _conn is not None and _db_path == path:
            return _conn
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timesfm_relchange (
                symbol TEXT NOT NULL,
                ts REAL NOT NULL,
                rel_change REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timesfm_rc_symbol_ts "
            "ON timesfm_relchange (symbol, ts)"
        )
        conn.commit()
        _conn = conn
        _db_path = path
        return conn


def record_rel_change(symbol: str, rel_change: float, *, db_path: Path | None = None) -> None:
    if not symbol or rel_change is None:
        return
    try:
        conn = _ensure_conn(db_path)
        with _lock:
            conn.execute(
                "INSERT INTO timesfm_relchange (symbol, ts, rel_change) VALUES (?, ?, ?)",
                (symbol, time.time(), float(rel_change)),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("timesfm_zmap: record failed for %s: %s", symbol, exc)


def symbol_stats(
    symbol: str,
    *,
    max_rows: int = 2000,
    db_path: Path | None = None,
) -> tuple[int, float]:
    """Return (n, sample_stdev of rel_change) from the last max_rows samples."""
    try:
        conn = _ensure_conn(db_path)
        with _lock:
            rows = conn.execute(
                "SELECT rel_change FROM timesfm_relchange "
                "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, int(max_rows)),
            ).fetchall()
    except Exception as exc:
        logger.warning("timesfm_zmap: stats failed for %s: %s", symbol, exc)
        return 0, 0.0
    vals = [float(r[0]) for r in rows]
    n = len(vals)
    if n < 2:
        return n, 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return n, math.sqrt(max(var, 0.0))


def zmap_probability(
    symbol: str,
    rel_change: float,
    *,
    min_samples: int = MIN_SAMPLES,
    sigma_floor: float = SIGMA_FLOOR,
    db_path: Path | None = None,
) -> Optional[float]:
    """Φ(rel_change / max(σ, floor)) or None if insufficient history."""
    n, sigma = symbol_stats(symbol, db_path=db_path)
    if n < min_samples:
        return None
    s = max(sigma, sigma_floor)
    z = max(-Z_CAP, min(Z_CAP, float(rel_change) / s))
    return normal_cdf(z)


def reset_connection() -> None:
    """Test helper — drop the cached SQLite handle."""
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _db_path = None
