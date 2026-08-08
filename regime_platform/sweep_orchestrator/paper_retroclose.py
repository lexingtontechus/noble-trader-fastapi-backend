"""Retro-close stale paper positions (one-shot, 2026-08-08).

After the close-pass fix in paper_book.py, run this to close every OPEN
paper position whose nt_signal_sim signal has already been resolved
(tp_hit / sl_hit / expired) but was never closed because of the
200-oldest-window bug (closed=0 on every reconcile run).

Usage (dry-run first — no writes):
    PYTHONPATH=src .venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.paper_retroclose --dry-run
Apply:
    PYTHONPATH=src .venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.paper_retroclose --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)
logger = logging.getLogger("paper_retroclose")

from regime_platform.sweep_orchestrator.paper_book import (  # noqa: E402
    close_position,
    _fetch_open_positions,
    _fetch_resolved_signals,
)


async def retro_close(dry_run: bool = True) -> dict[str, Any]:
    """Close open positions whose signal is resolved. Dry-run by default."""
    summary: dict[str, Any] = {"scanned_open": 0, "resolved_match": 0, "closed": 0, "skipped_no_size": 0, "errors": 0}
    try:
        open_positions = await _fetch_open_positions()
        resolved = await _fetch_resolved_signals(limit=5000)
    except Exception as exc:
        logger.error("retroclose: fetch failed: %s", exc)
        summary["errors"] += 1
        return summary

    summary["scanned_open"] = len(open_positions)
    resolved_by_sid = {r.get("signal_id"): r for r in resolved if r.get("signal_id")}

    for pos in open_positions:
        sid = pos.get("signal_id")
        sig = resolved_by_sid.get(sid)
        if not sig:
            continue  # signal not resolved yet — leave open
        summary["resolved_match"] += 1
        size_units = float(pos.get("size_units") or 0.0)
        if size_units <= 0:
            summary["skipped_no_size"] += 1
            logger.warning(
                "retroclose: %s position %s has size_units=0 — skipping (PnL would be $0)",
                pos.get("symbol"), pos.get("position_id"),
            )
            continue
        if dry_run:
            logger.info(
                "[DRY-RUN] would CLOSE %s (%s) signal=%s outcome=%s size_units=%.4f",
                sig.get("symbol"), pos.get("position_id"), sid,
                sig.get("outcome"), size_units,
            )
            continue
        try:
            ok = await close_position(sig, pos.get("position_id"), size_units=size_units)
            if ok:
                summary["closed"] += 1
            else:
                summary["errors"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.error("retroclose: close %s failed: %s", sid, exc)

    logger.info(
        "retroclose %s: scanned_open=%d resolved_match=%d closed=%d skipped_no_size=%d errors=%d",
        "DRY-RUN" if dry_run else "APPLY",
        summary["scanned_open"], summary["resolved_match"],
        summary["closed"], summary["skipped_no_size"], summary["errors"],
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Retro-close stale paper positions")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    parser.add_argument("--apply", action="store_true", help="Actually close positions")
    args = parser.parse_args()

    dry_run = not args.apply
    result = asyncio.run(retro_close(dry_run=dry_run))
    print(f"RETROCLOSE {'DRY-RUN' if dry_run else 'APPLY'} SUMMARY: {result}")


if __name__ == "__main__":
    sys.exit(main())
