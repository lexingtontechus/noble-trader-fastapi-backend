#!/usr/bin/env python3
"""
Historical remediation for the 2026-09-08 eod_resolver.py fix
(worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md, "Root cause 2").

Every `nt_signal_sim` row with outcome='expired' was written with
exit_price=NULL and pnl_bricks/pnl_dollars=0 — a fixed fiction, not the
real mark-to-market outcome, because resolve_signal_outcome/compute_pnl
never captured or scored the real price at the 48h cutoff. That's now
fixed going forward (eod_resolver.py). This script repairs the existing
59,913 contaminated rows in place, using the SAME two functions
(resolve_signal_outcome's fix already applied at write time — here we
only need compute_pnl, since we compute exit_price ourselves from real
history) — real historical TDVA prices, not synthetic.

Confirmed before writing this (not assumed): all 59,913 expired rows are
within TDVA's ~208-day history depth (earliest 2026-08-06, latest
2026-09-06 — zero unrecoverable). Deleting them would discard recoverable
real data; this recomputes them instead.

Method: per symbol, fetch one broad historical hourly price series
(covers every that symbol's expired signals' 48h cutoff), then for each
row find the bar closest to (ts + 48h) and use its close as the real
exit_price, feeding it through the same compute_pnl() the live resolver
now uses. Patches exit_price/pnl_bricks/pnl_dollars only — outcome,
hold_bars, exit_ts, resolved_at are untouched (the resolver's original
"did it expire" determination is unaffected by this fix; only the
financial consequence of expiring was wrong).

Usage: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/backfill_expired_mark_to_market.py
"""
from __future__ import annotations

import asyncio
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

EXPIRY_HOURS = 48          # must match eod_resolver.EXPIRY_BARS
FETCH_HOURS = 950          # covers earliest expired ts (33d ago) + 48h buffer
PATCH_CONCURRENCY = 40     # network-bound (Supabase REST), not CPU-bound
PAGE_SIZE = 1000


async def _fetch_all_expired(client, url: str, headers: dict) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    select = "signal_id,symbol,signal,ts,entry_price,tp_bricks,sl_bricks,brick_size"
    while True:
        resp = await client.get(
            url,
            headers=headers,
            params={
                "select": select,
                "outcome": "eq.expired",
                "order": "ts.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


async def main() -> None:
    import httpx
    from regime_platform.sweep_orchestrator.eod_resolver import compute_pnl
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import (
        _headers, _url, fetch_active_symbols,
    )

    signal_sim_url = _url("nt_signal_sim")
    headers = _headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("Fetching all expired nt_signal_sim rows...", flush=True)
        expired = await _fetch_all_expired(client, signal_sim_url, headers)
        print(f"Found {len(expired)} expired rows to remediate.", flush=True)
        if not expired:
            return

        by_symbol: dict[str, list[dict]] = {}
        for r in expired:
            by_symbol.setdefault(r["symbol"], []).append(r)

        active = await fetch_active_symbols()
        active_by_symbol = {s["symbol"]: s for s in active}
        missing = [s for s in by_symbol if s not in active_by_symbol]
        if missing:
            print(f"WARNING: {len(missing)} symbols with expired rows are not in "
                  f"nt_symbol (no tradingview_symbol mapping) — skipping: {missing}", flush=True)

        symbols_to_fetch = [active_by_symbol[s] for s in by_symbol if s in active_by_symbol]
        print(f"Fetching {FETCH_HOURS}h of history for {len(symbols_to_fetch)} symbols...", flush=True)
        fetch_time = datetime.now(timezone.utc)
        price_batch = await fetch_candles_for_symbols(symbols_to_fetch, n_bars=FETCH_HOURS, interval="1h")

        def _price_at(symbol: str, target_ts: datetime) -> float | None:
            prices = price_batch.get(symbol)
            if not prices:
                return None
            hours_ago = (fetch_time - target_ts).total_seconds() / 3600.0
            idx = round((len(prices) - 1) - hours_ago)
            idx = max(0, min(len(prices) - 1, idx))
            return float(prices[idx])

        patches: list[tuple[str, dict]] = []  # (signal_id, patch_body)
        skipped_no_price = 0
        skipped_missing_fields = 0
        for symbol, rows in by_symbol.items():
            if symbol not in active_by_symbol:
                continue
            for r in rows:
                entry = r.get("entry_price")
                brick_size = r.get("brick_size")
                if entry is None or not brick_size:
                    skipped_missing_fields += 1
                    continue
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                target_ts = ts + timedelta(hours=EXPIRY_HOURS)
                exit_price = _price_at(symbol, target_ts)
                if exit_price is None:
                    skipped_no_price += 1
                    continue
                pnl = compute_pnl(
                    "expired",
                    r.get("tp_bricks"), r.get("sl_bricks"), brick_size,
                    entry_price=entry, exit_price=exit_price,
                    direction=r.get("signal") or "buy",
                )
                patches.append((r["signal_id"], {
                    "exit_price": round(exit_price, 6),
                    "pnl_bricks": pnl["pnl_bricks"],
                    "pnl_dollars": pnl["pnl_dollars"],
                }))

        print(f"Computed {len(patches)} real mark-to-market patches "
              f"(skipped {skipped_missing_fields} missing entry/brick_size, "
              f"{skipped_no_price} with no price data).", flush=True)

        semaphore = asyncio.Semaphore(PATCH_CONCURRENCY)
        done = 0
        failed = 0
        t_start = time.time()

        async def _patch_one(signal_id: str, body: dict):
            nonlocal done, failed
            async with semaphore:
                resp = await client.patch(
                    signal_sim_url,
                    headers={**headers, "Prefer": "return=minimal"},
                    params={"signal_id": f"eq.{signal_id}"},
                    json=body,
                )
                if resp.is_success:
                    done += 1
                else:
                    failed += 1
                    if failed <= 5:
                        print(f"  PATCH failed for {signal_id}: {resp.status_code} {resp.text[:200]}", flush=True)
                if (done + failed) % 5000 == 0:
                    print(f"  {done + failed}/{len(patches)} ({time.time() - t_start:.0f}s elapsed)", flush=True)

        await asyncio.gather(*(_patch_one(sid, body) for sid, body in patches))
        print(f"\nDone: {done} patched, {failed} failed, in {time.time() - t_start:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
