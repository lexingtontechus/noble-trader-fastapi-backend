"""
EV Engine Fix Monitor — Background Watch

Monitors the next 4h calibration correction pass and subsequent light sweeps
to verify the p_regime cap + Markov degenerate fix are working correctly.

Polls nt_signal_sim for post-fix sweep results and tracks:
  - raw_p_win (pre-calibration, directly from EV engine)
  - calibrated p_win (after calibration enforcement)
  - qualified status
  - kelly_f
  - rolling bias

Usage:
  # Run as one-shot check:
  PYTHONPATH="$PWD/src" .venv/Scripts/python.exe scripts/monitor_ev_fix.py --once

  # Run as background monitor for 6 hours:
  PYTHONPATH="$PWD/src" .venv/Scripts/python.exe scripts/monitor_ev_fix.py --hours 6

  # Use with --watch for continuous polling:
  PYTHONPATH="$PWD/src" .venv/Scripts/python.exe scripts/monitor_ev_fix.py --hours 6 --interval 30

Output:
  - Console: real-time table of symbol → metrics
  - JSON: scripts/monitor_ev_fix_results.json (updated each poll)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

# ─── Configuration ──────────────────────────────────────────
SYMBOLS = [
    "AUDUSD", "BTCUSD", "EURUSD", "GBPUSD", "USDCAD",
    "USDCHF", "USDJPY", "US30USD", "US500",
    "XAGUSD", "XAUAUD", "XAUEUR", "XAUUSD", "AAPL",
]

# Post-fix deployment timestamp (Studio restart at 00:26:52 UTC on 2026-08-19)
# Use 'Z' suffix — PostgREST URL encoding breaks on +00:00 (see AGENTS.md pitfall #3)
POST_FIX_TS = "2026-08-19T00:26:52Z"

# ─── Supabase helpers ───────────────────────────────────────

def _get_supabase_config() -> tuple[str | None, str | None]:
    """Read Supabase credentials from settings or environment."""
    try:
        from regime_platform.core.settings import settings
        return settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY
    except Exception:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SECRET_KEY")
        return url, key

def _headers(key: str) -> dict[str, str]:
    return {"apikey": key, "Authorization": f"Bearer {key}"}

async def fetch_latest_signals(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    since_ts: str,
) -> dict[str, dict]:
    """Fetch the latest nt_signal_sim row for each symbol since a timestamp."""
    results: dict[str, dict] = {}
    for sym in SYMBOLS:
        try:
            resp = await client.get(
                f"{base_url}/nt_signal_sim",
                headers=headers,
                params={
                    "symbol": f"eq.{sym}",
                    "ts": f"gte.{since_ts}",
                    "order": "ts.desc",
                    "limit": "1",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    results[sym] = data[0]
        except httpx.HTTPError as e:
            print(f"  ⚠️  {sym}: HTTP error: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ⚠️  {sym}: error: {type(e).__name__}: {e}")
    return results

async def fetch_eod_actions(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    symbol: str,
) -> list[dict]:
    """Fetch recent nt_eod_actions for a symbol."""
    qurl = (
        f"{base_url}/rest/v1/nt_eod_actions"
        f"?symbol=eq.{symbol}"
        f"&order=ts.desc"
        f"&limit=5"
    )
    try:
        resp = await client.get(qurl, headers=headers)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []

# ─── Monitoring logic ───────────────────────────────────────

class EVMonitor:
    """Monitors the EV engine fix across symbol sweeps."""

    def __init__(self, supabase_url: str, supabase_key: str):
        self.base_url = f"{supabase_url}/rest/v1"
        self.headers = _headers(supabase_key)
        self.seen_hashes: dict[str, str] = {}
        self.raw_history: dict[str, list[float]] = {}  # symbol → [raw_p_win values]
        self.bias_history: dict[str, list[float]] = {}

    async def poll(self) -> dict[str, Any]:
        """Poll for latest signal data. Returns a structured report."""
        async with httpx.AsyncClient(timeout=30) as client:
            signals = await fetch_latest_signals(
                client, self.base_url, self.headers, POST_FIX_TS
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        report: dict[str, Any] = {
            "timestamp": now,
            "symbols": {},
        }

        for sym in SYMBOLS:
            s = signals.get(sym, {})
            if not s:
                report["symbols"][sym] = {"status": "no_data"}
                continue

            raw_pwin = s.get("predicted_p_win_raw")
            cal_pwin = s.get("p_win")
            qualified = s.get("qualified")
            kelly = s.get("effective_kelly") or s.get("kelly_f", 0)
            ts = s.get("ts", "")
            regime = s.get("regime", "")
            bias = self._compute_bias(raw_pwin, cal_pwin)

            # Track history
            if raw_pwin is not None:
                self.raw_history.setdefault(sym, []).append(float(raw_pwin))
                if len(self.raw_history[sym]) > 20:
                    self.raw_history[sym] = self.raw_history[sym][-20:]

            if bias is not None:
                self.bias_history.setdefault(sym, []).append(bias)
                if len(self.bias_history[sym]) > 20:
                    self.bias_history[sym] = self.bias_history[sym][-20:]

            # Classify the fix state
            fix_status = self._classify(sym, raw_pwin, cal_pwin, bias)

            report["symbols"][sym] = {
                "ts": ts,
                "regime": regime,
                "raw_p_win": raw_pwin,
                "calibrated_p_win": cal_pwin,
                "qualified": qualified,
                "kelly_f": kelly,
                "bias_estimate": bias,
                "fix_status": fix_status,
            }

        return report

    def _compute_bias(self, raw_pwin: float | None, cal_pwin: float | None) -> float | None:
        """Estimate calibration bias from raw vs calibrated p_win.

        This is a proxy — true bias uses rolling 7-day outcomes.
        A large gap between raw and calibrated indicates the calibration
        enforcement is actively correcting overconfidence.
        """
        if raw_pwin is None or cal_pwin is None:
            return None
        return float(raw_pwin) - float(cal_pwin)

    def _classify(self, sym: str, raw: float | None, cal: float | None,
                  bias: float | None) -> str:
        """Classify the fix status for a symbol."""
        if raw is None:
            return "no_data"

        raw = float(raw)

        if raw <= 0.01:
            return "FIX_WORKING_DEGENERATE"  # Markov absorbing fixed
        elif raw <= 0.10:
            return "FIX_WORKING_STRONG"      # Strong corrective action
        elif raw <= 0.95:
            if raw < 0.5:
                return "FIX_WORKING_NEGATIVE"  # p_markov dragging negative
            return "FIX_WORKING_MODERATE"      # Regime cap reducing confidence
        elif raw <= 0.98:
            return "FIX_WORKING_MARGINAL"      # Barely under cap
        else:
            return "FIX_NOT_WORKING"           # p_win near 1.0 despite fix

    def print_report(self, report: dict[str, Any]) -> None:
        """Print a formatted report to console."""
        ts = report["timestamp"]
        print(f"\n{'=' * 90}")
        print(f"  EV Engine Fix Monitor — {ts}")
        print(f"  Post-fix sweep threshold: {POST_FIX_TS}")
        print(f"{'=' * 90}")
        print(f"  {'Symbol':<10} {'raw_p_win':>10} {'cal_p_win':>10} {'qualified':>10} {'kelly':>8} {'bias':>8} {'status':<25}")
        print(f"  {'─' * 80}")

        for sym in SYMBOLS:
            data = report["symbols"].get(sym, {})
            if data.get("status") == "no_data":
                print(f"  {sym:<10} {'—':>10} {'—':>10} {'—':>10} {'—':>8} {'—':>8} {'no_data':<25}")
                continue

            raw = data.get("raw_p_win")
            cal = data.get("calibrated_p_win")
            qual = data.get("qualified", False)
            kelly = data.get("kelly_f")
            bias = data.get("bias_estimate")
            status = data.get("fix_status", "")

            raw_str = f"{raw:.4f}" if raw is not None else "NULL"
            cal_str = f"{cal:.4f}" if cal is not None else "NULL"
            qual_str = "✓ True" if qual else "✗ False"
            kelly_str = f"{kelly:.4f}" if kelly is not None else "NULL"
            bias_str = f"{bias:+.4f}" if bias is not None else "N/A"

            print(f"  {sym:<10} {raw_str:>10} {cal_str:>10} {qual_str:>10} {kelly_str:>8} {bias_str:>8} {status:<25}")

        print(f"  {'─' * 80}")

        # Summary stats
        stats = self._summarize(report)
        print(f"  Summary:")
        print(f"    Fix working (degenerate/absorbing): {stats['degenerate_ok']} symbols")
        print(f"    Fix working (capped):               {stats['capped_ok']} symbols")
        print(f"    Fix NOT working (raw > 0.98):       {stats['not_working']} symbols")
        print(f"    Qualified signals:                  {stats['qualified']} symbols")
        print()

    def _summarize(self, report: dict[str, Any]) -> dict[str, int]:
        """Compute summary statistics."""
        degenerate_ok = 0
        capped_ok = 0
        not_working = 0
        qualified = 0

        for sym in SYMBOLS:
            data = report["symbols"].get(sym, {})
            if data.get("status") == "no_data":
                continue
            status = data.get("fix_status", "")
            if status == "FIX_WORKING_DEGENERATE":
                degenerate_ok += 1
            elif status in ("FIX_WORKING_STRONG", "FIX_WORKING_MODERATE",
                            "FIX_WORKING_NEGATIVE", "FIX_WORKING_MARGINAL"):
                capped_ok += 1
            elif status == "FIX_NOT_WORKING":
                not_working += 1

            if data.get("qualified"):
                qualified += 1

        return {
            "degenerate_ok": degenerate_ok,
            "capped_ok": capped_ok,
            "not_working": not_working,
            "qualified": qualified,
        }

# ─── Main ───────────────────────────────────────────────────

async def main_async(args: argparse.Namespace) -> None:
    supabase_url, supabase_key = _get_supabase_config()

    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set.")
        print("  In local dev: set env vars")
        print("  In Studio: secrets are configured automatically")
        return

    print(f"Supabase URL: {supabase_url}")
    print(f"Monitoring {len(SYMBOLS)} symbols since {POST_FIX_TS}")
    print(f"Mode: {'single check' if args.once else f'continuous ({args.hours}h, interval={args.interval}s)'}")

    monitor = EVMonitor(supabase_url, supabase_key)

    if args.once:
        report = await monitor.poll()
        monitor.print_report(report)

        # Save results
        output_path = os.path.join(os.path.dirname(__file__), "monitor_ev_fix_results.json")
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n  Results saved to: {output_path}")
        return

    # Continuous mode
    end_time = time.time() + (args.hours * 3600)
    all_reports: list[dict] = []
    results_path = os.path.join(os.path.dirname(__file__), "monitor_ev_fix_results.json")

    while time.time() < end_time:
        report = await monitor.poll()
        monitor.print_report(report)
        all_reports.append(report)

        # Save cumulative results
        with open(results_path, "w") as f:
            json.dump(all_reports, f, indent=2, default=str)

        if time.time() + args.interval >= end_time:
            break

        print(f"  Next poll in {args.interval} seconds...\n")
        await asyncio.sleep(args.interval)

    print(f"\n{'=' * 90}")
    print("  Monitoring complete!")
    print(f"  Total samples collected: {len(all_reports)}")
    print(f"  Results saved to: {results_path}")

    # Print final summary
    if all_reports:
        final = all_reports[-1]
        monitor.print_report(final)

def main():
    parser = argparse.ArgumentParser(
        description="Monitor the EV engine fix (p_regime cap + Markov degenerate) "
                    "across light sweeps and the next 4h calibration correction pass."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single check and exit"
    )
    parser.add_argument(
        "--hours", type=float, default=6.0,
        help="Duration to monitor (default: 6 hours)"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds (default: 60)"
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
