#!/usr/bin/env python3
"""
Noble Trader — 24hr Calibration Report

Consolidated assessment covering the past 24 hours:
  1. EV fix monitor history (from eval_24h.py logic — delivery log + results JSON)
  2. v_eod_calibration_bias view (post-enforcement p_win vs realized)
  3. Raw vs enforced p_win from nt_calibration_records (masked-calibration check)
  4. Calibration record health (counts, outcome mix, entry validity)
  5. Failed heavy sweep traces (no_combo_passed_filters)
  6. Enforcement activity (nt_eod_actions — bayesian_penalty / kelly_reduction / emergency_heavy_sweep)

Usage:
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/noble_calibration.py

Output: consolidated text report to stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Any

import httpx

# ─── Paths ──────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DELIVERY_LOG = REPO_ROOT / "scripts" / "monitor_ev_fix_delivery.log"
RESULTS_FILE = REPO_ROOT / "scripts" / "monitor_ev_fix_results.json"

# ─── Symbols ────────────────────────────────────────────────

SYMBOLS = [
    "AUDUSD", "BTCUSD", "EURUSD", "GBPUSD", "USDCAD",
    "USDCHF", "USDJPY", "US30USD", "US500",
    "XAGUSD", "XAUAUD", "XAUEUR", "XAUUSD", "AAPL",
]

# ─── Supabase helpers ───────────────────────────────────────

def _get_supabase_config() -> tuple[str | None, str | None]:
    try:
        from regime_platform.core.settings import settings as s
        return s.SUPABASE_URL, s.SUPABASE_KEY
    except Exception:
        return os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")


def _headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}"}


async def _get(client: httpx.AsyncClient, base: str, path: str,
                params: dict | None = None) -> list | dict:
    url = f"{base}/{path}"
    resp = await client.get(url, headers=_headers(KEY), params=params)
    if resp.status_code == 200:
        return resp.json()
    return [] if resp.status_code != 200 else {}


# ─── Section 1: EV fix monitor history (eval_24h logic) ────

def parse_delivery_log() -> list[dict]:
    entries = []
    if not DELIVERY_LOG.exists():
        return entries
    with open(DELIVERY_LOG) as f:
        for line in f:
            line = line.strip()
            if "|statuses=" not in line:
                continue
            parts = line.split("|", 1)
            ts = parts[0]
            statuses_json = parts[1].split("=", 1)[1]
            try:
                statuses = json.loads(statuses_json)
            except json.JSONDecodeError:
                continue
            entries.append({"timestamp": ts, "statuses": statuses})
    return entries


def parse_results_json() -> dict:
    if not RESULTS_FILE.exists():
        return {}
    with open(RESULTS_FILE) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {}
    if isinstance(data, list):
        return data[-1] if data else {}
    return data


def section_ev_fix_monitor() -> str:
    """Section 1 — EV fix monitor 24hr history."""
    entries = parse_delivery_log()
    if not entries:
        return "  [No EV fix monitor delivery log found — cron may not have run yet]\n\n"

    first_ts = entries[0]["timestamp"]
    last_ts = entries[-1]["timestamp"]
    now = datetime.now(timezone.utc)
    # Filter to last 24h
    cutoff = now - timedelta(hours=24)
    recent = [e for e in entries if _parse_ts(e["timestamp"]) >= cutoff]

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 1 — EV Fix Monitor (predicted_p_win_raw) — Past 24 Hours")
    lines.append("=" * 90)
    lines.append(f"  Monitoring period: {first_ts} to {last_ts}")
    lines.append(f"  Checks in last 24h: {len(recent)} (total: {len(entries)})")
    lines.append(f"  Symbols: {len(SYMBOLS)}")
    lines.append("")

    if not recent:
        lines.append("  [No checks in the last 24 hours]\n")
        return "\n".join(lines)

    # Status distribution
    all_statuses: dict[str, int] = defaultdict(int)
    for e in recent:
        for sym, status in e["statuses"].items():
            all_statuses[status] += 1

    total = sum(all_statuses.values())
    lines.append("  Status distribution (all symbols × all checks in 24h):")
    for s in sorted(all_statuses.keys()):
        pct = all_statuses[s] / total * 100 if total else 0
        lines.append(f"    {s}: {all_statuses[s]} ({pct:.1f}%)")
    lines.append("")

    # Per-symbol breakdown
    lines.append(f"  {'Symbol':<10} {'DEGEN':>6} {'STRONG':>6} {'NEG':>6} {'MOD':>6} {'MAR':>6} {'FAIL':>6} {'total':>6}")
    lines.append("  " + "-" * 60)
    for sym in SYMBOLS:
        counts: dict[str, int] = defaultdict(int)
        for e in recent:
            status = e["statuses"].get(sym, "NO_DATA")
            counts[status] += 1
        total_sym = sum(counts.values())
        lines.append(
            f"  {sym:<10} "
            f"{counts.get('FIX_WORKING_DEGENERATE', 0):>6} "
            f"{counts.get('FIX_WORKING_STRONG', 0):>6} "
            f"{counts.get('FIX_WORKING_NEGATIVE', 0):>6} "
            f"{counts.get('FIX_WORKING_MODERATE', 0):>6} "
            f"{counts.get('FIX_WORKING_MARGINAL', 0):>6} "
            f"{counts.get('FIX_NOT_WORKING', 0):>6} "
            f"{total_sym:>6}"
        )
    lines.append("")

    # Regressions
    regressions = []
    for i, e in enumerate(recent):
        for sym in SYMBOLS:
            status = e["statuses"].get(sym, "")
            if status == "FIX_NOT_WORKING":
                prev = recent[i - 1]["statuses"].get(sym, "") if i > 0 else ""
                regressions.append({
                    "timestamp": e["timestamp"],
                    "symbol": sym,
                    "from": prev,
                    "to": status,
                })

    if regressions:
        lines.append(f"  === REGRESSIONS DETECTED: {len(regressions)} ===")
        for r in regressions:
            lines.append(f"    {r['timestamp']}  {r['symbol']}: {r['from']} → {r['to']}")
    else:
        lines.append("  === REGRESSIONS: NONE ===")
        lines.append(f"  0 FIX_NOT_WORKING events across {len(recent)} checks × {len(SYMBOLS)} symbols")
    lines.append("")

    # Latest snapshot from results JSON
    results = parse_results_json()
    latest_statuses = recent[-1]["statuses"] if recent else {}
    lines.append("  === Latest Monitor Snapshot ===")
    lines.append(f"  {'Symbol':<10} {'raw_p_win':>10} {'cal_p_win':>10} {'qualified':>10} {'bias':>8} {'status':<25}")
    lines.append("  " + "-" * 75)
    for sym in SYMBOLS:
        info = {}
        if isinstance(results, dict) and "symbols" in results:
            info = results.get("symbols", {}).get(sym, {})
        elif isinstance(results, dict):
            info = results.get(sym, {})
        raw = info.get("raw_p_win")
        cal = info.get("calibrated_p_win")
        qual = info.get("qualified")
        bias_val = info.get("bias_estimate")
        status = info.get("fix_status", latest_statuses.get(sym, "N/A"))

        raw_str = f"{raw:.4f}" if isinstance(raw, (int, float)) else "N/A"
        cal_str = f"{cal:.4f}" if isinstance(cal, (int, float)) else "N/A"
        bias_str = f"{bias_val:+.4f}" if isinstance(bias_val, (int, float)) else "N/A"
        qual_str = "✓" if qual else "✗"

        lines.append(f"  {sym:<10} {raw_str:>10} {cal_str:>10} {qual_str:>10} {bias_str:>8} {status:<25}")
    lines.append("")

    # Notable trends
    lines.append("  === Notable Trends (status changes in 24h) ===")
    found_trend = False
    for sym in SYMBOLS:
        statuses_over_time = [e["statuses"].get(sym, "") for e in recent]
        unique = set(statuses_over_time)
        unique.discard("NO_DATA")
        if len(unique) > 1:
            found_trend = True
            lines.append(f"    {sym}: {statuses_over_time[0]} → {statuses_over_time[-1]}")
            transitions = []
            for i in range(1, len(statuses_over_time)):
                if statuses_over_time[i] != statuses_over_time[i - 1]:
                    transitions.append(
                        f"{statuses_over_time[i-1]}→{statuses_over_time[i]} "
                        f"@{recent[i]['timestamp']}"
                    )
            if transitions:
                lines.append(f"      transitions: {', '.join(transitions[:3])}"
                             f"{'...' if len(transitions) > 3 else ''}")
    if not found_trend:
        lines.append("    No status transitions in the last 24 hours.")
    lines.append("")

    return "\n".join(lines)


def _parse_ts(ts_str: str) -> datetime:
    """Parse a timestamp string from the delivery log."""
    # Format: "2026-08-19 01:03:22 UTC"
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


# ─── Section 2: v_eod_calibration_bias view ────────────────

async def section_calibration_bias_view() -> str:
    """Section 2 — v_eod_calibration_bias (post-enforcement)."""
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=30) as client:
        # Query the view directly
        resp = await client.get(
            f"{base}/v_eod_calibration_bias",
            headers=_headers(KEY),
            params={
                "day": f"gte.{_date_str(days=7)}",
                "order": "day.desc",
            },
        )
        if resp.status_code != 200:
            return f"  [Failed to query v_eod_calibration_bias: HTTP {resp.status_code}]\n\n"

        rows = resp.json()
        if not isinstance(rows, list):
            return "  [Unexpected response from v_eod_calibration_bias]\n\n"

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 2 — v_eod_calibration_bias (POST-ENFORCEMENT p_win vs realized)")
    lines.append("=" * 90)
    lines.append("  Source: nt_signal_sim.p_win (AFTER Bayesian shrink + Kelly enforcement)")
    lines.append("  Answers: 'Is the deployed p_win still overconfident after enforcement?'")
    lines.append("  Does NOT answer: 'Is the raw model still miscalibrated?'")
    lines.append("")

    # Group by symbol, take most recent day
    by_symbol: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol", "")
        if sym not in by_symbol or row.get("day", "") > by_symbol[sym].get("day", ""):
            by_symbol[sym] = row

    if not by_symbol:
        lines.append("  [No rows in view]\n")
        return "\n".join(lines)

    lines.append(f"  {'Symbol':<10} {'day':<12} {'avg_pred_p_win':>14} {'realized_wr':>12} {'bias':>8} {'status':<15}")
    lines.append("  " + "-" * 75)
    for sym in SYMBOLS:
        row = by_symbol.get(sym, {})
        day = row.get("day", "N/A")
        avg_pred = row.get("avg_predicted_p_win")
        realized = row.get("realized_win_rate")
        bias = row.get("bias")
        status = row.get("status", "N/A")

        avg_str = f"{avg_pred:.4f}" if isinstance(avg_pred, (int, float)) else "N/A"
        real_str = f"{realized:.4f}" if isinstance(realized, (int, float)) else "N/A"
        bias_str = f"{bias:+.4f}" if isinstance(bias, (int, float)) else "N/A"

        lines.append(f"  {sym:<10} {str(day):<12} {avg_str:>14} {real_str:>12} {bias_str:>8} {status:<15}")
    lines.append("")

    # Summary
    overconfident = [s for s, r in by_symbol.items() if r.get("status") == "OVERCONFIDENT"]
    underconfident = [s for s, r in by_symbol.items() if r.get("status") == "UNDERCONFIDENT"]
    calibrated = [s for s, r in by_symbol.items() if r.get("status") == "CALIBRATED"]

    lines.append(f"  Summary (most recent day):")
    lines.append(f"    OVERCONFIDENT: {len(overconfident)} symbols — {', '.join(overconfident) or 'none'}")
    lines.append(f"    UNDERCONFIDENT: {len(underconfident)} symbols — {', '.join(underconfident) or 'none'}")
    lines.append(f"    CALIBRATED: {len(calibrated)} symbols — {', '.join(calibrated) or 'none'}")
    lines.append("")
    return "\n".join(lines)


def _date_str(days: int = 7) -> str:
    """Return ISO date string N days ago for PostgREST filter."""
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


# ─── Section 3: Raw vs enforced p_win ──────────────────────

async def section_raw_vs_enforced() -> str:
    """Section 3 — nt_calibration_records: raw vs enforced p_win."""
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=30) as client:
        all_rows: list[dict] = []
        offset = 0
        while True:
            resp = await client.get(
                f"{base}/nt_calibration_records",
                headers=_headers(KEY),
                params={
                    "entered_at": f"gte.{_iso_str(days=7)}",
                    "select": "symbol,predicted_p_win,predicted_p_win_raw,outcome",
                    "order": "entered_at.desc",
                    "limit": "1000",
                    "offset": str(offset),
                },
            )
            if resp.status_code != 200:
                return f"  [Failed to query nt_calibration_records: HTTP {resp.status_code}]\n\n"
            rows = resp.json()
            if not isinstance(rows, list) or len(rows) == 0:
                break
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000

        if not all_rows:
            return "  [No records in nt_calibration_records (last 7 days)]\n\n"

        # Aggregate per symbol
        by_sym: dict[str, dict] = {}
        for row in all_rows:
            sym = row.get("symbol", "")
            if sym not in by_sym:
                by_sym[sym] = {
                    "n": 0,
                    "raw_sum": 0.0,
                    "enforced_sum": 0.0,
                    "wins": 0,
                    "losses": 0,
                    "raw_n": 0,
                }
            by_sym[sym]["n"] += 1
            raw = row.get("predicted_p_win_raw")
            enf = row.get("predicted_p_win")
            if isinstance(raw, (int, float)):
                by_sym[sym]["raw_sum"] += raw
                by_sym[sym]["raw_n"] += 1
            if isinstance(enf, (int, float)):
                by_sym[sym]["enforced_sum"] += enf
            outcome = row.get("outcome", "")
            if outcome == "tp_hit":
                by_sym[sym]["wins"] += 1
            elif outcome == "sl_hit":
                by_sym[sym]["losses"] += 1

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 3 — Raw vs Enforced p_win (masked-calibration check)")
    lines.append("=" * 90)
    lines.append("  Source: nt_calibration_records (last 7 days, predicted_p_win_raw IS NOT NULL)")
    lines.append("  raw_bias          = avg(predicted_p_win_raw)  - realized_win_rate  ← model miscalibration")
    lines.append("  enforced_bias     = avg(predicted_p_win)     - realized_win_rate  ← what the view sees")
    lines.append("  avg_shrink        = avg(predicted_p_win) - avg(predicted_p_win_raw) ← enforcement magnitude")
    lines.append("")

    if not by_sym:
        lines.append("  [No records with predicted_p_win_raw in the last 7 days]\n")
        return "\n".join(lines)

    lines.append(f"  {'Symbol':<10} {'n':>5} {'raw_p_win':>10} {'enf_p_win':>10} {'shrink':>8} {'realized':>10} {'raw_bias':>9} {'enf_bias':>9} {'status':<12}")
    lines.append("  " + "-" * 95)
    for sym in SYMBOLS:
        d = by_sym.get(sym, {})
        n = d.get("n", 0)
        if n == 0:
            lines.append(f"  {sym:<10} {n:>5} {'—':>10} {'—':>10} {'—':>8} {'—':>10} {'—':>9} {'—':>9} {'no_data':<12}")
            continue

        raw_avg = d["raw_sum"] / max(d["raw_n"], 1)
        enf_avg = d["enforced_sum"] / n
        realized = d["wins"] / max(d["wins"] + d["losses"], 1)
        shrink = enf_avg - raw_avg
        raw_bias = raw_avg - realized
        enf_bias = enf_avg - realized

        status = "?"
        if abs(enf_bias) < 0.10:
            status = "CALIBRATED"
        elif enf_bias > 0.10:
            status = "OVERCONF (view)"
        elif enf_bias < -0.10:
            status = "UNDERCONF (view)"

        lines.append(
            f"  {sym:<10} {n:>5} "
            f"{raw_avg:.4f}     {enf_avg:.4f}     {shrink:+.4f} "
            f"{realized:.4f}   {raw_bias:+.4f}   {enf_bias:+.4f}   {status:<12}"
        )
    lines.append("")

    # Interpretation
    lines.append("  Interpretation:")
    lines.append("    - If raw_bias >> enf_bias: enforcement is doing most of the compensating")
    lines.append("    - If both are small: the model itself is well-calibrated")
    lines.append("    - If raw_bias > 0.10 and enf_bias < 0.10: enforcement is masking true overconfidence")
    lines.append("")

    return "\n".join(lines)


def _iso_str(days: int = 7) -> str:
    """Return ISO timestamp string N days ago for PostgREST filter (Z suffix)."""
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Section 4: Calibration record health ──────────────────

async def section_record_health() -> str:
    """Section 4 — nt_calibration_records health check."""
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=30) as client:
        all_rows: list[dict] = []
        offset = 0
        while True:
            resp = await client.get(
                f"{base}/nt_calibration_records",
                headers=_headers(KEY),
                params={
                    "entered_at": f"gte.{_iso_str(days=7)}",
                    "select": "symbol,outcome,entered_at",
                    "order": "entered_at.desc",
                    "limit": "1000",
                    "offset": str(offset),
                },
            )
            if resp.status_code != 200:
                return f"  [Failed to query nt_calibration_records: HTTP {resp.status_code}]\n\n"
            rows = resp.json()
            if not isinstance(rows, list) or len(rows) == 0:
                break
            all_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000

        if not all_rows:
            return "  [No records in nt_calibration_records (last 7 days)]\n\n"

        by_sym: dict[str, dict] = {}
        for row in all_rows:
            sym = row.get("symbol", "")
            if sym not in by_sym:
                by_sym[sym] = {"n": 0, "wins": 0, "losses": 0, "expired": 0, "oldest": None, "newest": None}
            by_sym[sym]["n"] += 1
            outcome = row.get("outcome", "")
            if outcome == "tp_hit":
                by_sym[sym]["wins"] += 1
            elif outcome == "sl_hit":
                by_sym[sym]["losses"] += 1
            elif outcome == "expired":
                by_sym[sym]["expired"] += 1
            entered = row.get("entered_at", "")
            if entered:
                if by_sym[sym]["oldest"] is None or entered < by_sym[sym]["oldest"]:
                    by_sym[sym]["oldest"] = entered
                if by_sym[sym]["newest"] is None or entered > by_sym[sym]["newest"]:
                    by_sym[sym]["newest"] = entered

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 4 — Calibration Record Health (last 7 days)")
    lines.append("=" * 90)
    lines.append("  Threshold: >= 20 records per symbol (fetch_calibration_bias requirement)")
    lines.append("  Degenerate flag: min(wins, losses) == 0 → suspect artifact / untrustworthy")
    lines.append("")

    lines.append(f"  {'Symbol':<10} {'n':>5} {'wins':>5} {'losses':>6} {'expired':>7} {'oldest':<25} {'newest':<25} {'status':<20}")
    lines.append("  " + "-" * 105)
    for sym in SYMBOLS:
        d = by_sym.get(sym, {})
        n = d.get("n", 0)
        wins = d.get("wins", 0)
        losses = d.get("losses", 0)
        expired = d.get("expired", 0)
        oldest = d.get("oldest", "N/A")
        newest = d.get("newest", "N/A")

        if n == 0:
            lines.append(f"  {sym:<10} {n:>5} {'—':>5} {'—':>6} {'—':>7} {'—':<25} {'—':<25} {'no_data':<20}")
            continue

        # Status
        if n < 20:
            status = "BELOW THRESHOLD"
        elif min(wins, losses) == 0 and n >= 20:
            status = "DEGENERATE — investigate"
        else:
            wr = wins / (wins + losses) if (wins + losses) > 0 else 0
            if wr >= 0.95 or wr <= 0.05:
                status = "EXTREME — verify entries"
            else:
                status = "healthy"

        old_str = str(oldest)[:24] if oldest else "N/A"
        new_str = str(newest)[:24] if newest else "N/A"

        lines.append(
            f"  {sym:<10} {n:>5} {wins:>5} {losses:>6} {expired:>7} "
            f"{old_str:<25} {new_str:<25} {status:<20}"
        )
    lines.append("")

    # Aggregate
    total_n = sum(d.get("n", 0) for d in by_sym.values())
    total_wins = sum(d.get("wins", 0) for d in by_sym.values())
    total_losses = sum(d.get("losses", 0) for d in by_sym.values())
    below_threshold = [s for s, d in by_sym.items() if d.get("n", 0) < 20]

    lines.append("  Aggregate (7-day window):")
    lines.append(f"    Total records: {total_n}")
    lines.append(f"    Win rate: {total_wins}/{total_wins + total_losses} = "
                 f"{total_wins / max(total_wins + total_losses, 1):.1%}")
    lines.append(f"    Symbols below 20-record threshold: {', '.join(below_threshold) or 'none'}")
    lines.append("")

    return "\n".join(lines)


# ─── Section 5: Failed heavy sweep traces ──────────────────

async def section_failed_sweeps() -> str:
    """Section 5 — nt_sweep_result failed heavy sweeps."""
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base}/nt_sweep_result",
            headers=_headers(KEY),
            params={
                "source": "eq.sweep-heavy",
                "qualified": "eq.false",
                "sweep_timestamp": f"gte.{_iso_str(days=7)}",
                "select": "symbol,sweep_timestamp,n_combos_tested,regime,regime_conf,kelly_f,sweep_duration_ms,error",
                "order": "sweep_timestamp.desc",
                "limit": "1000",
            },
        )
        if resp.status_code != 200:
            return f"  [Failed to query nt_sweep_result: HTTP {resp.status_code}]\n\n"
        rows = resp.json()
        if not isinstance(rows, list):
            return "  [Unexpected response]\n\n"

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 5 — Failed Heavy Sweep Traces (no_combo_passed_filters)")
    lines.append("=" * 90)
    lines.append("  Source: nt_sweep_result WHERE source='sweep-heavy' AND qualified=false")
    lines.append("  n_combos_tested = how many of 90 combos survived the MIN_RR pre-filter")
    lines.append("")

    if not rows:
        lines.append("  [No failed heavy sweep traces in the last 7 days]\n")
        return "\n".join(lines)

    lines.append(f"  {'Symbol':<10} {'sweep_ts':<25} {'combos_tested':>13} {'regime':<12} {'regime_conf':>10} {'kelly_f':>8} {'duration_ms':>11} {'error':<30}")
    lines.append("  " + "-" * 120)
    for row in rows[:20]:  # cap at 20 most recent
        sym = row.get("symbol", "")
        ts = str(row.get("sweep_timestamp", ""))[:24]
        combos = row.get("n_combos_tested", 0)
        regime = row.get("regime", "") or ""
        conf = row.get("regime_conf")
        kelly = row.get("kelly_f")
        dur = row.get("sweep_duration_ms")
        error = (row.get("error") or "")[:29]

        conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "N/A"
        kelly_str = f"{kelly:.4f}" if isinstance(kelly, (int, float)) else "N/A"
        dur_str = f"{dur}" if isinstance(dur, (int, float)) else "N/A"

        lines.append(f"  {sym:<10} {ts:<25} {combos:>13} {regime:<12} {conf_str:>10} {kelly_str:>8} {dur_str:>11} {error:<30}")
    if len(rows) > 20:
        lines.append(f"  ... and {len(rows) - 20} more (cap at 20 shown)")
    lines.append("")

    # Analysis
    by_sym: dict[str, list] = defaultdict(list)
    for row in rows:
        by_sym[row.get("symbol", "")].append(row)

    lines.append("  Analysis:")
    for sym in SYMBOLS:
        sweeps = by_sym.get(sym, [])
        if not sweeps:
            continue
        last = sweeps[0]
        combos = last.get("n_combos_tested", 0)
        error = (last.get("error") or "")
        lines.append(f"    {sym}: {len(sweeps)} failed sweep(s), last tested {combos} combos")
        if "MIN_RR=2.0" in error and combos <= 25:
            lines.append(f"      → Likely MIN_RR=2.0 filter artifact (few combos survived pre-filter)")
            lines.append(f"      → Aug 17 fix (MIN_RR shared env var, now 1.5) should expand grid ~2x")
        elif combos > 30:
            lines.append(f"      → Many combos tested, none passed — likely genuine 'no geometry works'")
            lines.append(f"      → Consider signal reduction or different brick params for this symbol")
    lines.append("")

    return "\n".join(lines)


# ─── Section 6: Enforcement activity ───────────────────────

async def section_enforcement_activity() -> str:
    """Section 6 — nt_eod_actions enforcement stamps."""
    base = f"{URL}/rest/v1"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{base}/nt_eod_actions",
            headers=_headers(KEY),
            params={
                "ts": f"gte.{_iso_str(days=1)}",
                "select": "symbol,action,ts",
                "order": "ts.desc",
                "limit": "1000",
            },
        )
        if resp.status_code != 200:
            return f"  [Failed to query nt_eod_actions: HTTP {resp.status_code}]\n\n"
        rows = resp.json()
        if not isinstance(rows, list):
            return "  [Unexpected response]\n\n"

    by_action: dict[str, int] = defaultdict(int)
    by_sym: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        action = row.get("action", "unknown")
        sym = row.get("symbol", "unknown")
        by_action[action] += 1
        by_sym[sym][action] += 1

    lines = []
    lines.append("=" * 90)
    lines.append("  SECTION 6 — Enforcement Activity (nt_eod_actions, last 24 hours)")
    lines.append("=" * 90)
    lines.append("  Actions written by EOD correction passes (nightly 00:30 + 4h intraday)")
    lines.append("  Applied live by every 5-min light sweep (Bayesian p_win shrink + Kelly x0.5)")
    lines.append("")

    if not rows:
        lines.append("  [No enforcement actions in the last 24 hours]\n")
        return "\n".join(lines)

    lines.append(f"  Total actions: {len(rows)}")
    lines.append("")
    lines.append("  By action type:")
    for action in sorted(by_action.keys()):
        lines.append(f"    {action}: {by_action[action]}")
    lines.append("")

    lines.append("  By symbol:")
    lines.append(f"  {'Symbol':<10} {'bayesian_penalty':>16} {'kelly_reduction':>15} {'emergency_heavy_sweep':>22} {'total':>6}")
    lines.append("  " + "-" * 75)
    for sym in SYMBOLS:
        d = by_sym.get(sym, {})
        bp = d.get("bayesian_penalty", 0)
        kr = d.get("kelly_reduction", 0)
        ehs = d.get("emergency_heavy_sweep", 0)
        total = bp + kr + ehs
        if total == 0:
            continue
        lines.append(f"  {sym:<10} {bp:>16} {kr:>15} {ehs:>22} {total:>6}")
    lines.append("")

    return "\n".join(lines)


# ─── Main ───────────────────────────────────────────────────

async def main() -> int:
    global URL, KEY
    URL, KEY = _get_supabase_config()
    if not URL or not KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set.")
        print("  In local dev: set env vars or ensure .env.local is loaded")
        print("  In Studio: secrets are configured automatically")
        return 1

    print(f"\n{'#' * 90}")
    print(f"#  Noble Trader — 24-Hour Calibration Report")
    print(f"#  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"#  Supabase: {URL}")
    print(f"{'#' * 90}\n")

    # Section 1: EV fix monitor (sync — reads local files)
    print(section_ev_fix_monitor())

    # Sections 2-6: Supabase queries (async)
    print(await section_calibration_bias_view())
    print(await section_raw_vs_enforced())
    print(await section_record_health())
    print(await section_failed_sweeps())
    print(await section_enforcement_activity())

    print("=" * 90)
    print("  END OF REPORT")
    print(f"  Run again: PYTHONPATH=\"$PWD/src\" ./.venv/Scripts/python.exe scripts/noble_calibration.py")
    print("=" * 90)

    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
