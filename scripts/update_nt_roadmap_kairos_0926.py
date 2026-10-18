"""
Upsert nt_roadmap rows for the 2026-09-26 Kairos work:
  - eod_resolver_scan_full          (89e4aec, live)
  - calibration_evidence_pool       (8cc4899, shadow, migration 171 view)
  - geometry_replay_wide_tp_tight_sl (afbf49d, offline finding, V3 §14.5)
and append replay/resolver context to dynamic_atr_mode_flip + calibration_resolve_hourly
(append is idempotent: skipped if the marker is already in notes).

Run:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_kairos_0926.py

Uses SUPABASE_ACCESS_TOKEN (management API); prints SQL if unavailable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_env = Path(__file__).resolve().parents[1] / ".env.local"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SQL = r"""
INSERT INTO nt_roadmap (
  mechanism, current_state, set_at, review_at, review_condition,
  status, worklog_ref, notes, updated_at
) VALUES
(
  'eod_resolver_scan_full',
  $$live (eod_resolver_scan_mode: full, 89e4aec 2026-09-26)$$,
  now(),
  '2026-09-27 04:00:00+00',
  $$Next nightly (03:11 UTC) after Studio deploy: log shows scan_mode: full + "reached end of pending queue"; TP/SL written ~= preceding :38 hourly shadow count (one-time catch-up ~110, mostly SL); no TradingView 429s; runtime < 60s. Pass -> resolved.$$,
  'pending',
  'worklog/kairos/20260926_RESOLVER_HEAD_OF_LINE_FIX.md',
  $$Bug: resolver stopped at first oldest-first batch that resolved nothing; 09-26 nightly wrote 15 TP / 21 SL while 12 TP / 83 SL were decided (72 SL at queue pos 1,024-1,893). Losses under-written -> calibration pool skewed to wins (AAPL read underconfident). Replay: 118 TP/SL vs 23, 11.9s vs 22.5s. Calibration reads before this deploy carry the win skew. Rollback: eod_resolver_scan_mode: head (hot, no restart).$$,
  now()
),
(
  'calibration_evidence_pool',
  $$shadow (calibration_evidence_pool_mode, tiers [qualified, suppressed]; view v_calibration_records_tiered, migration 171)$$,
  now(),
  '2026-09-29 18:00:00+00',
  $$>=3 days of [eod:pool] lines spanning the 09-26 heavy (first live hybrid geometry); per-symbol status disagreements (AAPL type) checked against realised qualified outcomes; >=6/14 symbols not INSUFFICIENT in the pool. Not in the same read window as calibration_resolve_hourly_mode or dynamic_atr_mode flips (one lever at a time).$$,
  'pending',
  'worklog/kairos/20260926_CALIBRATION_EVIDENCE_POOL.md',
  $$Kairos V3 §14.3: q+s evidence triples production bias gain; gate_fail dilutes the slow rule. View-only DDL, recon 2093/2093 vs stamped gate_outcome. Live falls back to all-tier when pool thin. Read only on post-resolver-fix data (eod_resolver_scan_full). Rollback: flag -> shadow/off.$$,
  now()
),
(
  'geometry_replay_wide_tp_tight_sl',
  $$finding (offline replay, 8,252 signals 09-01..09-24, 87.3% parity with live outcomes) -- no flag, no code change$$,
  now(),
  '2026-10-05 18:00:00+00',
  $$Before any geometry lever: re-run scripts/kairos_geometry_replay.py on out-of-sample data (09-25 onward, post-resolver-fix) and confirm TP 9-17 / SL 0.5-2 ATR stays positive; fix close-only pnl overstatement for tight stops (compute_bricks_pnl books nominal bricks). Only gate pass-rate change needs a live shadow.$$,
  'pending',
  'worklog/kairos/reports/20260926_geometry_replay.md',
  $$V3 §14.5. Recorded geometry (TP 15.8 / SL 7.5 ATR): 43% expiry, edge -0.03, -0.87 ATR/signal. TP too wide: confirmed. SL too tight: NOT supported (SL>=TP shapes edge <=0). Dynamic ATR set: 0% expiry, best +0.08 ATR, unstable halves. Best region TP 9-17 / SL 0.5-2: +0.45..+0.56, positive both halves (trend-following payoff). Caveats: overlapping hourly signals, one month, in-sample grid, entries/directions held fixed, q+s tier only 82 signals.$$,
  now()
)
ON CONFLICT (mechanism) DO UPDATE SET
  current_state = EXCLUDED.current_state,
  set_at = EXCLUDED.set_at,
  review_at = EXCLUDED.review_at,
  review_condition = EXCLUDED.review_condition,
  status = EXCLUDED.status,
  worklog_ref = EXCLUDED.worklog_ref,
  notes = EXCLUDED.notes,
  updated_at = EXCLUDED.updated_at;

UPDATE nt_roadmap SET
  notes = notes || $$ | 2026-09-26 replay (V3 §14.5): dynamic-ATR feasible set fixes expiry (0%, decided 2-3h) but not profitability (best 2/0.5 ATR = +0.08 ATR/signal, h1 +0.01 / h2 +0.14); its TP band 0.3-2 ATR excludes the best replay region (TP 9-17 / SL 0.5-2). Live read on 09-29 must beat that prior on expectancy, not just exp%.$$,
  updated_at = now()
WHERE mechanism = 'dynamic_atr_mode_flip'
  AND position('2026-09-26 replay' in coalesce(notes, '')) = 0;

UPDATE nt_roadmap SET
  notes = notes || $$ | 2026-09-26: shadow vs nightly parity gap was the resolver head-of-line bug (fixed, eod_resolver_scan_full), not a shadow/live disagreement. Count shadow-vs-nightly parity only from the first post-fix nightly.$$,
  updated_at = now()
WHERE mechanism = 'calibration_resolve_hourly'
  AND position('head-of-line bug' in coalesce(notes, '')) = 0;
"""

CHECK = """
select mechanism, status, review_at::date::text as review, left(current_state, 70) as state,
       right(notes, 90) as notes_tail
from nt_roadmap
where mechanism in ('eod_resolver_scan_full','calibration_evidence_pool',
  'geometry_replay_wide_tp_tight_sl','dynamic_atr_mode_flip','calibration_resolve_hourly')
order by review_at;
"""


def main() -> int:
    import httpx

    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    # Ref parsed from SUPABASE_URL (the variable .env.local carries).
    ref = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
    if not token:
        print("-- Paste into Supabase SQL editor:\n" + SQL)
        print("Missing SUPABASE_ACCESS_TOKEN — SQL printed above.", file=sys.stderr)
        return 1
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(url, headers=headers, json={"query": SQL})
        print(f"HTTP {resp.status_code} {resp.text[:500]}")
        if resp.status_code >= 400:
            print("-- Paste into Supabase SQL editor:\n" + SQL)
            return 1
        r = client.post(url, headers=headers, json={"query": CHECK})
        print("=== verify ===")
        for row in r.json():
            print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
