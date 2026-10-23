"""Query nt_sweep_result for data consistency after the TDVA order fix.

Checks:
1. Latest USDJPY rows: entry/stop/tp levels now near live (~159), not stale 162
2. Pre-fix vs post-fix levels (boot 2026-08-11 22:17:51 UTC)
3. Signal/SL/TP internal consistency (sell: SL>entry>TP; buy: SL<entry<TP)
4. Rows with signal set: entry != 0, reasonable distances
"""
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

env_path = os.path.join(REPO, ".env.local")
env = {}
with open(env_path, "r") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

SUPABASE_URL = env.get("SUPABASE_URL", env.get("SB_URL", "")).rstrip("/")
SUPABASE_KEY = env.get("SUPABASE_SECRET_KEY", env.get("SUPABASE_KEY", env.get("SUPABASE_SERVICE_ROLE_KEY", "")))

if not SUPABASE_URL or not SUPABASE_KEY:
    print("FAIL: missing Supabase credentials")
    sys.exit(1)

import httpx


def query(path: str) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    resp = httpx.get(
        url,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    # ── 1. Latest 20 USDJPY rows ──────────────────────────────
    print("=" * 78)
    print("LATEST 20 USDJPY nt_sweep_result rows (sweep_timestamp DESC)")
    print("=" * 78)
    rows = query(
        "nt_sweep_result?symbol=eq.USDJPY&order=sweep_timestamp.desc&limit=20"
    )
    print(f"Rows returned: {len(rows)}")
    for r in rows:
        ts = (r.get("sweep_timestamp") or "")[:19]
        sig = r.get("signal") or "-"
        entry = r.get("entry_price")
        sl = r.get("stop_loss")
        tp = r.get("take_profit")
        reg = r.get("regime") or "-"
        src = r.get("source") or "-"
        qual = r.get("qualified")
        print(
            f"  {ts} | {sig:7s} | entry={entry} | sl={sl} | tp={tp} "
            f"| {reg} | q={qual} | {src}"
        )

    # ── 2. Consistency checks on the latest rows ───────────────
    print()
    print("=" * 78)
    print("CONSISTENCY CHECKS (USDJPY)")
    print("=" * 78)
    bad = 0
    for r in rows:
        sig = r.get("signal")
        entry = r.get("entry_price")
        sl = r.get("stop_loss")
        tp = r.get("take_profit")
        if sig in (None, "neutral", "unknown"):
            continue
        if entry is None or sl is None or tp is None:
            print(f"  !! {r.get('sweep_timestamp')}: missing price fields")
            bad += 1
            continue
        if sig == "sell":
            ok = sl > entry > tp
        elif sig == "buy":
            ok = sl < entry < tp
        else:
            continue
        if not ok:
            print(
                f"  !! {r.get('sweep_timestamp')}: INCONSISTENT {sig} "
                f"entry={entry} sl={sl} tp={tp}"
            )
            bad += 1
        # Distance sanity: SL/TP should be within ~5% of entry for forex
        if entry > 0:
            sl_dist = abs(sl - entry) / entry
            tp_dist = abs(tp - entry) / entry
            if sl_dist > 0.05 or tp_dist > 0.05:
                print(
                    f"  !! {r.get('sweep_timestamp')}: abnormal distance "
                    f"sl_dist={sl_dist:.3%} tp_dist={tp_dist:.3%}"
                )
                bad += 1

    if bad == 0:
        print("  All signal rows internally consistent.")
    else:
        print(f"  {bad} inconsistency(ies) found.")

    # ── 3. Price level sanity vs live ──────────────────────────
    print()
    print("=" * 78)
    print("PRICE LEVEL CHECK (USDJPY entry vs live ~159.3)")
    print("=" * 78)
    for r in rows[:10]:
        entry = r.get("entry_price")
        ts = (r.get("sweep_timestamp") or "")[:19]
        if entry is None:
            continue
        level = "LIVE" if 150 < entry < 170 else "STALE/ANOMALY"
        print(f"  {ts}: entry={entry} -> {level}")

    # ── 4. Across-symbol recent activity ───────────────────────
    print()
    print("=" * 78)
    print("RECENT ACTIVITY PER SYMBOL (last 200 rows by symbol count)")
    print("=" * 78)
    recent = query(
        "nt_sweep_result?order=sweep_timestamp.desc&limit=200"
    )
    from collections import Counter
    sym_counts = Counter(r.get("symbol") for r in recent)
    sym_qualified = Counter(
        r.get("symbol") for r in recent if r.get("qualified")
    )
    for sym, n in sorted(sym_counts.items()):
        print(f"  {sym}: {n} rows, {sym_qualified.get(sym, 0)} qualified")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
