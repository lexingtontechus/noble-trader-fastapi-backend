"""TSFM lab R2 publish — the backend-owned TDVA→R2 refresh job.

Scope: docs/scopes/20261118_TSFM_R2_BACKEND_PUBLISH_SCOPE.md (2026-11-18).
Replaces the retired ``tsfm-refresh`` Cloudflare cron Worker design: the sweep's
OWN TDVA client (``fetch_tradingview_batch``) fetches the lab symbols' 5000-bar
1h window (``ohlcv_out`` yields ascending {t,o,h,l,c,v} rows — the lab CSV schema
directly) and publishes CSVs + ``manifest.json`` to R2 through the secret-gated
PUT route on the existing ``nt-tsfm-data`` bridge Worker. One fetch
implementation, one credential store, one batch HTTP call per run.

Modes (``tsfm_r2_publish_mode``, read per run — flip needs no restart):
  off    — full no-op (no roster read, no fetch).
  shadow — fetch + validate + serialize + log; ZERO uploads.
  live   — validate, then PUT each healthy symbol's CSV + the manifest.

Fail-loud per symbol: a failed validation or PUT keeps the previous CSV in R2
and records the reason in ``manifest.errors`` — never a partial/empty write.
Best-effort: never raises into the scheduler. Freshness couples to Studio
uptime by design (the accepted dedup trade-off); the lab serves the last
vintage with a ``dataVintage`` age stamp during outages, and
``scripts/publish_tsfm_r2.py`` runs THIS module locally as the bootstrap /
outage bridge — one implementation, two runners.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from regime_platform.core.feature_flags import (
    get_tsfm_r2_publish_mode,
    get_tsfm_r2_publish_symbols,
)
from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

logger = logging.getLogger("tsfm_r2_publish")

# The lab window: ~5000 hourly bars (208 days). TDVA's batch cap is exactly
# min(hours, 5000) (price_fetcher), so one request per symbol covers it.
FETCH_BARS = 5000
MIN_ROWS = 1000          # a truncated response is not a lab window
MAX_LAST_BAR_AGE_S = 7200  # 2h — the sweep staleness convention, one notch relaxed
CSV_HEADER = "timestamp,open,high,low,close,volume"

# Bootstrap-only roster for the LOCAL runner (scripts/publish_tsfm_r2.py,
# roster_fallback=True): used ONLY when the nt_symbol read fails/returns empty
# (e.g. stale local Supabase creds during a Studio outage). Verified against
# nt_symbol 2026-11-18 (all active). The Studio job never uses this — it fails
# loud on a roster error, because Supabase is reachable there.
BOOTSTRAP_ROSTER = [
    {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"},
    {"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"},
    {"symbol": "USDCHF", "tradingview_symbol": "OANDA:USDCHF"},
    {"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"},
    {"symbol": "XAGUSD", "tradingview_symbol": "OANDA:XAGUSD"},
]


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_rows(rows: list[dict[str, Any]] | None, now: float) -> str | None:
    """Return an error reason, or None when the rows are publishable."""
    if not rows:
        return "no rows returned"
    if len(rows) < MIN_ROWS:
        return f"rows {len(rows)} < {MIN_ROWS}"
    prev_t: float | None = None
    for r in rows:
        t = r.get("t")
        if t is None:
            return "null timestamp bar (never invent times)"
        if prev_t is not None and t <= prev_t:
            return "timestamps not strictly ascending"
        prev_t = t
        for k in ("o", "h", "l", "c"):
            v = r.get(k)
            if v is None or not (v > 0) or v != v or v in (float("inf"), float("-inf")):
                return f"non-finite/non-positive {k}"
        v = r.get("v")
        if v is None or v < 0 or v != v or v in (float("inf"), float("-inf")):
            return "non-finite/negative volume"
    age = now - float(rows[-1]["t"])
    if age > MAX_LAST_BAR_AGE_S:
        return f"last bar age {age / 3600:.1f}h > {MAX_LAST_BAR_AGE_S / 3600:.0f}h"
    return None


def _rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    """Exact lab schema (data/tsfm/*.csv): ISO-Z timestamps, full float repr."""
    lines = [CSV_HEADER]
    for r in rows:
        lines.append(
            f"{_iso(r['t'])},{r['o']!r},{r['h']!r},{r['l']!r},{r['c']!r},{r['v']!r}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


async def publish_tsfm_prices(
    mode: str | None = None, roster_fallback: bool = False
) -> dict[str, Any]:
    """Fetch → validate → serialize → PUT (live) the lab CSVs + manifest.

    Returns a run summary; never raises. ``mode`` defaults to the flag value.
    ``roster_fallback`` (local runner only) substitutes BOOTSTRAP_ROSTER when
    the nt_symbol read fails or returns empty — loud, never silent.
    """
    mode = (mode or get_tsfm_r2_publish_mode()).lower()
    summary: dict[str, Any] = {
        "mode": mode, "published": 0, "skipped": 0,
        "errors": {}, "symbols": {}, "manifest_written": False,
    }
    if mode == "off":
        return summary

    wanted = get_tsfm_r2_publish_symbols()
    try:
        active = await fetch_active_symbols()
    except Exception as exc:  # noqa: BLE001 — best-effort job
        active = []
        summary["errors"]["_roster"] = str(exc)[:200]
    if not active:
        if roster_fallback:
            active = BOOTSTRAP_ROSTER
            summary["roster"] = "bootstrap-fallback (nt_symbol unreachable)"
            summary["errors"].pop("_roster", None)
            logger.warning(
                "[tsfm-r2-publish] nt_symbol read failed/empty — using the "
                "pinned bootstrap roster (local runner only)"
            )
        else:
            summary["errors"].setdefault("_roster", "empty active roster")
            return summary
    sym_rows = [
        r for r in active
        if r.get("symbol") in wanted and r.get("tradingview_symbol")
    ]
    for m in sorted(set(wanted) - {r["symbol"] for r in sym_rows}):
        summary["errors"][m] = "not active in nt_symbol / no tradingview_symbol"
        summary["skipped"] += 1
    if not sym_rows:
        return summary

    if mode == "live" and not settings.TSFM_R2_WRITE_SECRET:
        summary["errors"]["_secret"] = "TSFM_R2_WRITE_SECRET not set — fail closed"
        return summary

    ohlcv: dict[str, list[dict[str, Any]]] = {}
    try:
        await fetch_tradingview_batch(
            sym_rows, hours=FETCH_BARS, interval="1h",
            include_timestamps=True, ohlcv_out=ohlcv,
        )
    except Exception as exc:  # noqa: BLE001
        summary["errors"]["_fetch"] = str(exc)[:200]
        return summary

    now = time.time()
    payloads: dict[str, bytes] = {}
    for r in sym_rows:
        sym = r["symbol"]
        rows = ohlcv.get(sym)
        err = _validate_rows(rows, now)
        if err:
            summary["errors"][sym] = err
            summary["skipped"] += 1
            continue
        body = _rows_to_csv(rows)
        payloads[sym] = body
        summary["symbols"][sym] = {
            "rows": len(rows), "first": _iso(rows[0]["t"]),
            "last": _iso(rows[-1]["t"]), "bytes": len(body),
            "md5": hashlib.md5(body).hexdigest(),
        }

    if mode == "shadow":
        summary["note"] = "shadow: validated + serialized, zero uploads"
        logger.info(
            "[tsfm-r2-publish] shadow: %d valid, %d skipped, errors=%s",
            len(payloads), summary["skipped"], summary["errors"] or "{}",
        )
        return summary

    bridge = settings.TSFM_R2_BRIDGE_URL.rstrip("/")
    headers = {"X-Write-Secret": settings.TSFM_R2_WRITE_SECRET}
    published: list[str] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for sym, body in payloads.items():
            try:
                resp = await client.put(
                    f"{bridge}/tsfm/{sym}.csv", content=body,
                    headers={**headers, "Content-Type": "text/csv"},
                )
                if resp.status_code == 200:
                    published.append(sym)
                else:
                    summary["errors"][sym] = f"PUT HTTP {resp.status_code}: {resp.text[:120]}"
            except Exception as exc:  # noqa: BLE001
                summary["errors"][sym] = f"PUT failed: {str(exc)[:160]}"
        summary["published"] = len(published)
        manifest = {
            "updated_at": _iso(now), "publisher": "backend", "source": "tdva",
            "symbols": {
                s: {k: summary["symbols"][s][k] for k in ("rows", "first", "last")}
                for s in published
            },
            "errors": summary["errors"],
        }
        try:
            resp = await client.put(
                f"{bridge}/tsfm/manifest.json",
                content=json.dumps(manifest).encode("utf-8"),
                headers={**headers, "Content-Type": "application/json"},
            )
            summary["manifest_written"] = resp.status_code == 200
            if resp.status_code != 200:
                summary["errors"]["_manifest"] = f"PUT HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            summary["errors"]["_manifest"] = str(exc)[:160]

    logger.info(
        "[tsfm-r2-publish] live: published=%d skipped=%d manifest=%s errors=%s",
        summary["published"], summary["skipped"],
        summary["manifest_written"], summary["errors"] or "{}",
    )
    return summary
