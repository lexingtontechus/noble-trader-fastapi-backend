"""Monitor paper book health — cron-ready, stdlib-only (no Django/apscheduler deps).

Checks:
  1. Supabase query health (all 200 OK) for paper book endpoints
  2. Open paper position count (nt_paper_positions with status=open)
  3. Closed paper position count (status=closed, last 24h)
  4. Orphan count — open positions whose signal_id is already resolved
     (tp_hit/sl_hit/expired in nt_signal_sim) but position was never closed

Designed to run from cron (30 min) via Hermes cronjob as a script-only job.
Silent when healthy; prints alert block to Discord when issues detected.

Usage:
    python scripts/monitor_paper_book.py
    python scripts/monitor_paper_book.py --check  (exit 0 always, just prints)

Env vars:
    SUPABASE_URL, SUPABASE_KEY  — paper book database credentials
    MONITOR_DISCORD_WEBHOOK     — optional Discord webhook URL for alerts
    MONITOR_OPEN_THRESHOLD      — alert if open positions exceed this (default: 2000)
    MONITOR_ORPHAN_THRESHOLD    — alert if orphan count exceeds this (default: 0)
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("monitor.paper_book")

# ─── Metrics storage (for cronjob context_from chaining) ──────────────
# Writes metrics to a JSON file so downstream cron jobs / dashboards can
# read the last-known state without re-querying Supabase.
METRICS_PATH = os.environ.get(
    "PAPER_BOOK_METRICS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "paper_book_metrics.json"),
)


async def _fetch_json(url: str, headers: dict[str, str]) -> tuple[int, list[dict[str, Any]] | dict[str, Any]]:
    """Fetch a Supabase REST endpoint, return (status_code, json_body)."""
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        try:
            body = resp.json()
        except Exception:
            body = {"_raw": resp.text[:500]}
        return resp.status_code, body


async def _fetch_count(url: str, headers: dict[str, str]) -> tuple[int, int]:
    """Fetch a Supabase REST endpoint with Prefer: count=exact, return (status, count)."""
    import httpx
    hdrs = dict(headers)
    hdrs["Prefer"] = "count=exact"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=hdrs)
        count_str = resp.headers.get("X-Total-Count", "0") or "0"
        count = int(count_str) if count_str.isdigit() else 0
        return resp.status_code, count


def _sb_headers() -> dict[str, str]:
    key = os.environ.get("SUPABASE_SECRET_KEY", "[REDACTED]")
    url = os.environ.get("SUPABASE_URL", "https://pcvscowltlrxzgxjurcr.supabase.co")
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }


async def check_paper_book_health(
    open_threshold: int = 2000,
    orphan_threshold: int = 0,
) -> dict[str, Any]:
    """Check paper book health and return a metrics dict.

    Returns a dict with keys:
        - timestamp: ISO string
        - supabase_ok: bool (all queries returned 200)
        - open_positions: int
        - closed_24h: int
        - orphan_count: int
        - alerts: list of alert strings (empty = healthy)
    """
    url = os.environ.get("SUPABASE_URL", "https://pcvscowltlrxzgxjurcr.supabase.co")
    headers = _sb_headers()
    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()[:19]
    cutoff_14d = (now - timedelta(days=14)).isoformat()[:19] + "Z"

    metrics: dict[str, Any] = {
        "timestamp": now.isoformat(),
        "supabase_ok": True,
        "open_positions": 0,
        "closed_24h": 0,
        "orphan_count": 0,
        "alerts": [],
    }

    try:
        # ── 1. Supabase health: open positions count ──────────────────
        open_url = (
            f"{url}/rest/v1/nt_paper_positions"
            "?select=position_id&status=eq.open"
        )
        open_status, open_count = await _fetch_count(open_url, headers)
        if open_status != 200:
            metrics["supabase_ok"] = False
            metrics["alerts"].append(
                f"Supabase query FAILED for open positions: HTTP {open_status}"
            )
        else:
            metrics["open_positions"] = open_count

        # ── 2. Closed positions (last 24h) ─────────────────────────────
        closed_url = (
            f"{url}/rest/v1/nt_paper_positions"
            f"?select=position_id&status=eq.closed&close_ts=gte.{cutoff_24h}"
        )
        closed_status, closed_count = await _fetch_count(closed_url, headers)
        if closed_status != 200:
            metrics["supabase_ok"] = False
            metrics["alerts"].append(
                f"Supabase query FAILED for closed positions: HTTP {closed_status}"
            )
        else:
            metrics["closed_24h"] = closed_count

        # ── 3. Orphan count: open positions whose signal_id is resolved ─
        #   Fetch open positions' signal_ids (paginated)
        open_positions: list[dict[str, Any]] = []
        offset = 0
        page_size = 1000
        while True:
            pos_url = (
                f"{url}/rest/v1/nt_paper_positions"
                f"?select=position_id,signal_id&status=eq.open"
                f"&limit={page_size}&offset={offset}"
            )
            pos_status, pos_rows = await _fetch_json(pos_url, headers)
            if pos_status != 200:
                metrics["supabase_ok"] = False
                metrics["alerts"].append(
                    f"Supabase query FAILED for open positions (page {offset}): HTTP {pos_status}"
                )
                break
            if not isinstance(pos_rows, list):
                break
            open_positions.extend(pos_rows)
            if len(pos_rows) < page_size:
                break
            offset += page_size

        open_sids = {
            pos.get("signal_id")
            for pos in open_positions
            if pos.get("signal_id")
        }

        # Fetch resolved signals (outcome set) with resolved_at >= cutoff_14d
        # (paginated)
        resolved_signals: list[dict[str, Any]] = []
        offset = 0
        while True:
            sig_url = (
                f"{url}/rest/v1/nt_signal_sim"
                "?select=signal_id,outcome&outcome=in.(tp_hit,sl_hit,expired)"
                f"&resolved_at=gte.{cutoff_14d}&order=resolved_at.desc"
                f"&limit={page_size}&offset={offset}"
            )
            sig_status, sig_rows = await _fetch_json(sig_url, headers)
            if sig_status != 200:
                metrics["supabase_ok"] = False
                metrics["alerts"].append(
                    f"Supabase query FAILED for resolved signals (page {offset}): HTTP {sig_status}"
                )
                break
            if not isinstance(sig_rows, list):
                break
            resolved_signals.extend(sig_rows)
            if len(sig_rows) < page_size:
                break
            offset += page_size

        resolved_sids = {
            sig.get("signal_id")
            for sig in resolved_signals
            if sig.get("signal_id")
        }

        overlap = open_sids & resolved_sids
        metrics["orphan_count"] = len(overlap)
        metrics["open_signal_ids"] = len(open_sids)
        metrics["resolved_signal_ids"] = len(resolved_sids)

        # ── 4. Threshold alerts ───────────────────────────────────────
        if metrics["open_positions"] > open_threshold:
            metrics["alerts"].append(
                f"Open positions ({metrics['open_positions']}) exceeds threshold ({open_threshold})"
            )
        if metrics["orphan_count"] > orphan_threshold:
            metrics["alerts"].append(
                f"Orphan count ({metrics['orphan_count']}) exceeds threshold ({orphan_threshold})"
            )

    except Exception as exc:
        metrics["supabase_ok"] = False
        metrics["alerts"].append(f"Exception during health check: {type(exc).__name__}: {exc}")

    # ─── Write metrics file for downstream consumers ───────────────────
    try:
        os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
        with open(METRICS_PATH, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Could not write metrics file %s: %s", METRICS_PATH, exc)

    return metrics


def format_alert(metrics: dict[str, Any]) -> str:
    """Format metrics into a Discord alert block (or stdout summary)."""
    icon = "\u2705" if metrics["supabase_ok"] else "\u274c"
    status = "healthy" if metrics["supabase_ok"] else "FAILED"
    lines = [
        f"**Paper Book Health Check** \u2014 {metrics['timestamp']}",
        "",
        f"{icon} Supabase: {status}",
        f"\U0001f4ca Open positions: {metrics['open_positions']}",
        f"\U0001f4c8 Closed (24h): {metrics['closed_24h']}",
        f"\u26a0\ufe0f  Orphan count: {metrics['orphan_count']}",
        f"   (open signal_ids: {metrics.get('open_signal_ids', 'N/A')}",
        f"   resolved signal_ids: {metrics.get('resolved_signal_ids', 'N/A')})",
    ]
    if metrics["alerts"]:
        lines.append("")
        lines.append("**Alerts:**")
        for alert in metrics["alerts"]:
            lines.append(f"- {alert}")
    else:
        lines.append("")
        lines.append("\u2705 All metrics within thresholds.")
    return "\n".join(lines)


async def _send_discord(webhook: str, message: str) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(webhook, json={"content": message})


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor paper book health \u2014 counts open/closed positions, detects orphans."
    )
    parser.add_argument(
        "--open-threshold",
        type=int,
        default=int(os.environ.get("MONITOR_OPEN_THRESHOLD", "2000")),
        help="Alert if open positions exceed this count (default: 2000)",
    )
    parser.add_argument(
        "--orphan-threshold",
        type=int,
        default=int(os.environ.get("MONITOR_ORPHAN_THRESHOLD", "0")),
        help="Alert if orphan count exceeds this (default: 0)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 always (just print metrics)",
    )
    args = parser.parse_args()

    metrics = await check_paper_book_health(
        open_threshold=args.open_threshold,
        orphan_threshold=args.orphan_threshold,
    )
    report = format_alert(metrics)
    print(report)

    if metrics["alerts"]:
        # Send to Discord if webhook configured
        webhook = os.environ.get("MONITOR_DISCORD_WEBHOOK", "[REDACTED]")
        if webhook and webhook != "[REDACTED]":
            try:
                await _send_discord(webhook, report)
            except Exception as exc:
                logger.warning("Discord webhook send failed: %s", exc)
        if not args.check:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
