"""
Upsert nt_roadmap for 2026-09-26 evening: G2.2 decision, rank_score shadow stamp,
and Studio restart health after migration 172.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_kairos_rank_0926.py
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
  'kairos_g2',
  $$G2.2 DECIDED NO PASS at power (2026-09-26): Delta m24 demeaned -0.32 [-1.87,+0.82], power bar met. Age/time knobs closed.$$,
  now(),
  '2026-10-10 18:00:00+00',
  $$Only reopen if a NEW pre-registration targets scorecard D (accuracy vs no-skill under live geometry) on >=200 post-ATR episodes. Do not re-run m24 age tests.$$,
  'resolved',
  'worklog/kairos/20260926_LIVE_G22_DECISION_AND_PROMOTION.md',
  $$Primary confirmatory test for gate selection. Point estimate wrong-signed (q+s worse than gate_fail on demeaned m24). Freshness/type rank_score is the replacement programme (worklog 20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md).$$,
  now()
),
(
  'kairos_rank_score',
  $$shadow stamp live in code (migration 172 applied) -- tracker cold until outcomes ingest; Studio must pull c02d6be$$,
  now(),
  '2026-10-03 18:00:00+00',
  $$>=200 post-ATR episodes with rank_score stamped AND DeltaD CI > 0 (shadow gate P60). Then consider one gate lever vs 7-gate on scorecard D. Until stamps appear on nt_signal_sim, treat Studio as not yet on c02d6be.$$,
  'pending',
  'worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md',
  $$Composite 0.35 freshness + 0.25 type + 0.25 recent_tp + 0.15 cell_p. Fail-open stamp kairos_rank_score / kairos_rank_gate_shadow. Never gates signals. Walk-forward (episode-deduped): IC 0.13 all / 0.20 post-debounce; post-ATR n=43 DeltaD CI spans 0. Ingest via kairos-daily fetch_recent_decided_signals.$$,
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
select mechanism, status, left(current_state, 80) as state
from nt_roadmap
where mechanism in ('kairos_g2','kairos_rank_score','kairos_daily_job','dynamic_atr_mode_flip')
order by mechanism;
"""


def main() -> int:
    import httpx

    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    ref = os.environ.get("SUPABASE_PROJECT_REF") or "pcvscowltlrxzgxjurcr"
    if not token:
        print(SQL)
        print("Missing SUPABASE_ACCESS_TOKEN", file=sys.stderr)
        return 1
    url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(url, headers=headers, json={"query": SQL})
        print(f"HTTP {resp.status_code} {resp.text[:400]}")
        if resp.status_code >= 400:
            print(SQL)
            return 1
        r = client.post(url, headers=headers, json={"query": CHECK})
        print("=== verify ===")
        for row in r.json():
            print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
