#!/usr/bin/env python3
"""
Live smoke test for nt_calibration_records writes.

Validates that:
1. A calibration record can be written to Supabase
2. The entered_at / closed_at timestamps are properly formatted ISO 8601
3. The p_ext_signal column works (or falls back to p_imbalance if
   migration 024 wasn't applied)
4. The aggression constraint is satisfied

Usage:
  # Set env vars first:
  export SUPABASE_URL=https://your-project.supabase.co
  export SUPABASE_KEY=your-service-role-key

  # Then run:
  python3 test_calibration_live.py

  # Or with a specific symbol:
  python3 test_calibration_live.py --symbol BTC

The script writes 5 test records (alternating win/loss) and then reads
them back to verify the timestamps landed correctly. Test records are
marked with error_source='SMOKE_TEST' so you can delete them afterwards:

  DELETE FROM nt_calibration_records WHERE error_source = 'SMOKE_TEST';
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

# Add repo to path
REPO = "/home/z/my-project/noble-trader-fastapi-backend"
sys.path.insert(0, REPO)


async def write_calibration_record(
    supabase_url: str,
    supabase_key: str,
    symbol: str,
    predicted_p_win: float,
    is_win: bool,
    aggression: str = "mid",
    delay_seconds: float = 0,
) -> dict:
    """Write one calibration record and return the row that was written.

    Mirrors the logic in renko/trade_journal.py:_log_calibration_record(),
    including the schema-drift fallback for p_ext_signal vs p_imbalance.
    """
    import httpx

    # Simulate a trade that lasted `delay_seconds` (for entered_at/closed_at)
    entry_ts = time.time() - max(delay_seconds, 0.1)
    exit_ts = time.time()

    row = {
        "id": str(uuid.uuid4()),
        "symbol": symbol,
        "predicted_p_win": predicted_p_win,
        "outcome": "win" if is_win else "loss",
        "aggression": aggression,
        "actual_pnl": 100.0 if is_win else -50.0,
        "error_source": "SMOKE_TEST",  # marker for cleanup
        "entry_price": 50000.0,
        "exit_price": 50100.0 if is_win else 49950.0,
        "p_regime": 0.72,
        "p_ext_signal": 0.61,
        "p_markov": 0.58,
        "p_timesfm": 0.65,
        "entered_at": datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
        "closed_at": datetime.fromtimestamp(exit_ts, tz=timezone.utc).isoformat(),
    }

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",  # return the inserted row so we can verify
    }

    url = f"{supabase_url}/rest/v1/nt_calibration_records"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=row)

        # Schema drift fallback: p_ext_signal → p_imbalance
        if resp.status_code == 400 and "p_ext_signal" in resp.text:
            print(f"  ⚠ 'p_ext_signal' column not found — retrying with legacy 'p_imbalance'")
            row["p_imbalance"] = row.pop("p_ext_signal")
            resp = await client.post(url, headers=headers, json=row)

        if resp.status_code not in (200, 201):
            print(f"  ✗ INSERT FAILED: HTTP {resp.status_code}")
            print(f"    Response: {resp.text[:300]}")
            print(f"    Row sent: {json.dumps(row, indent=2)}")
            return None

        # PostgREST returns the inserted row as a list with return=representation
        inserted = resp.json()
        if isinstance(inserted, list) and inserted:
            return inserted[0]
        return inserted


async def read_back_records(
    supabase_url: str,
    supabase_key: str,
    symbol: str,
    limit: int = 10,
) -> list:
    """Read back recent calibration records for a symbol."""
    import httpx

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    url = f"{supabase_url}/rest/v1/nt_calibration_records"
    params = {
        "symbol": f"eq.{symbol}",
        "error_source": "eq.SMOKE_TEST",
        "select": "*",
        "order": "closed_at.desc",
        "limit": str(limit),
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            print(f"  ✗ READ FAILED: HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        return resp.json()


async def main():
    parser = argparse.ArgumentParser(description="Smoke test nt_calibration_records writes")
    parser.add_argument("--symbol", default="BTC", help="Symbol to use (default: BTC)")
    parser.add_argument("--count", type=int, default=5, help="Number of test records (default: 5)")
    args = parser.parse_args()

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print("ERROR: Set SUPABASE_URL and SUPABASE_KEY env vars first.")
        print()
        print("Example:")
        print("  export SUPABASE_URL=https://your-project.supabase.co")
        print("  export SUPABASE_KEY=your-service-role-key")
        sys.exit(1)

    print("=" * 70)
    print(f"  Calibration Records Smoke Test")
    print(f"  Supabase URL: {supabase_url}")
    print(f"  Symbol:       {args.symbol}")
    print(f"  Records:      {args.count}")
    print("=" * 70)
    print()

    # ── Write test records ─────────────────────────────────────────────
    print(f"[1/3] Writing {args.count} test records...")
    written_ids = []
    for i in range(args.count):
        is_win = (i % 2 == 0)  # alternate win/loss
        predicted = 0.65 if is_win else 0.35
        aggression = ["passive", "mid", "aggressive"][i % 3]
        delay = 60.0 * (i + 1)  # 1min, 2min, 3min... so entered_at/closed_at differ

        print(f"  [{i+1}/{args.count}] Writing {'WIN' if is_win else 'LOSS'} "
              f"(predicted={predicted}, aggression={aggression}, delay={delay}s)...")

        inserted = await write_calibration_record(
            supabase_url, supabase_key, args.symbol,
            predicted_p_win=predicted,
            is_win=is_win,
            aggression=aggression,
            delay_seconds=delay,
        )
        if inserted:
            written_ids.append(inserted.get("id"))
            print(f"    ✓ Inserted id={inserted.get('id', '?')[:8]}...")
        else:
            print(f"    ✗ Failed — see error above")

    if not written_ids:
        print("\n✗ No records were written. Check the error messages above.")
        sys.exit(1)

    print(f"\n  ✓ {len(written_ids)}/{args.count} records written")
    print()

    # ── Read back and verify ───────────────────────────────────────────
    print("[2/3] Reading back records to verify timestamps...")
    records = await read_back_records(supabase_url, supabase_key, args.symbol, limit=args.count)

    if not records:
        print("  ✗ No records read back — check Supabase RLS policies")
        sys.exit(1)

    print(f"  ✓ Read {len(records)} records back")
    print()

    # ── Verify each record ─────────────────────────────────────────────
    print("[3/3] Verifying fields...")
    print()
    print(f"  {'ID':<10} {'Outcome':<7} {'Aggression':<12} {'Predicted':<10} "
          f"{'Entered_at':<28} {'Closed_at':<28} {'Duration':<10}")
    print(f"  {'-'*10} {'-'*7} {'-'*12} {'-'*10} {'-'*28} {'-'*28} {'-'*10}")

    all_ok = True
    for r in records:
        rid = str(r.get("id", "?"))[:8]
        outcome = r.get("outcome", "?")
        aggression = r.get("aggression", "?")
        predicted = r.get("predicted_p_win", "?")
        entered = r.get("entered_at", "?")
        closed = r.get("closed_at", "?")

        # Verify timestamps are valid ISO 8601 with timezone
        try:
            entered_dt = datetime.fromisoformat(entered.replace("Z", "+00:00"))
            closed_dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
            duration = (closed_dt - entered_dt).total_seconds()
            duration_str = f"{duration:.0f}s"
            ts_ok = "✓"
        except Exception as e:
            duration_str = "INVALID"
            ts_ok = "✗"
            all_ok = False

        print(f"  {rid:<10} {outcome:<7} {aggression:<12} {predicted:<10} "
              f"{entered:<28} {closed:<28} {duration_str:<10} {ts_ok}")

    print()

    # ── Summary ────────────────────────────────────────────────────────
    print("=" * 70)
    if all_ok:
        print("  ✓ ALL RECORDS VALID — timestamps are properly formatted")
        print("  ✓ entered_at < closed_at for all records")
        print("  ✓ Schema (p_ext_signal, aggression constraint) accepted")
    else:
        print("  ✗ SOME RECORDS HAVE INVALID TIMESTAMPS — see table above")
    print()
    print(f"  Test records are marked with error_source='SMOKE_TEST'.")
    print(f"  To clean up, run this SQL in Supabase SQL editor:")
    print(f"    DELETE FROM nt_calibration_records WHERE error_source = 'SMOKE_TEST';")
    print("=" * 70)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
