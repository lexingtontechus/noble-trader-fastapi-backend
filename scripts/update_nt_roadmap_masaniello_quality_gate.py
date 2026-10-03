"""
Upsert nt_roadmap for the Masaniello urgency quality gate (2026-10-02).

Registers the calibration_quality_gate as a shadow-mode mechanism with a
48-hour review window for data accumulation + pulse check.

Run:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_masaniello_quality_gate.py

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
  'kairos_masaniello_urgency_gate',
  $$shadow: check_urgency() → nt_eod_actions parameter_decay_check, pure funcs verified$$,
  now(),
  '2026-10-04 18:00:00+00',
  $$Shadow done + pulse-check verified: (1) >=100 quality_gate rows
  in nt_eod_actions, (2) drift ratios correlate with calibration_bias_bucket,
  (3) zero false multi-symbol triggers during normal regime cycling.
  Fail → keep shadow, lower thresholds.$$,
  'pending',
  'docs/worklog-2026-10-02-masaniello-quality-gate.md',
  $$M_i=(W-w)/(N-i+1) reframed: sizing → batch-management health check.
  Pure funcs: urgency_index, urgency_drift_ratio, urgency_threshold,
  check_urgency_batch. Async: check_urgency(symbols) reads nt_signal_sim
  (resolved: tp_hit/sl_hit), reconstructs batch state (last N=5),
  compares urgency drift to regime-age brackets. Writes
  parameter_decay_check (nt_eod_actions CHECK constraint, no DDL).
  Audit-only: does NOT write nt_symbol_calibration_state. Gaps:
  (1) age proxied by lookback span not live KairosRunTracker;
  (2) nt_signal_sim lacks batch_num → insertion-order grouping,
  sweep-light only; (3) not wired to cron.
  See worklog/2026-10-02-pulse-check.md.$$,
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
"""

CHECK = """
select mechanism, status, review_at::timestamptz as review,
       left(current_state, 80) as state
from nt_roadmap
where mechanism = 'kairos_masaniello_urgency_gate'
order by mechanism;
"""


def main() -> int:
    import httpx

    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    ref = os.environ.get("SUPABASE_PROJECT_REF") or "pcvscowltlrxzgxjurcr"
    if not token:
        print(SQL)
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
