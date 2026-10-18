"""
Mark Phase 1 of the HMM->first-passage programme as BUILT (2026-10-01).

Phase 1a: skill diagnostic (base_rate + skill on nt_hmm_shadow_accuracy)
Phase 1b: first_passage.py primitive (offline only)

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_hmm_phase1_1001.py
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
  'bolster_legacy_hmm',
  $$PHASE 1 BUILT 2026-10-01. (1a) Skill diagnostic: compute_tier1_accuracy now also returns base_rate + skill (= accuracy - the symbol's own majority-direction share over ALL decided rows), persisted via migration 191 (2 new nullable columns on nt_hmm_shadow_accuracy). Validated against live data: the shipped function reproduces mean legacy skill -0.0622, positive on 5/14 symbols, exactly matching the hand-rolled RM-1 analysis. (1b) first_passage.py: P(TP before SL) over direction-signed ATR-scaled paths, reusing tiered_atr.walk()'s first-touch rule, PLUS the D5 regime-transition overlay (transition_carry = floor + (ceiling-floor) x sqrt(freshness_score x type_score)). OFFLINE ONLY -- nothing imported by the sweep path.$$,
  now(),
  '2026-10-15 00:00:00+00',
  $$Phase 2 offline evaluation: first-passage vs shrunk base-rate control (0.5898) vs legacy HMM (incumbent) on decided rows with a completed 48-bar window, scored on scorecard D (DeltaD accuracy-vs-no-skill, gated vs rest), era halves, symbol-day clustered CIs, episode unit (symbol x direction x hour). The overlay is scored as an INCREMENT on each arm, never alone.$$,
  'pending',
  'worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md',
  $$Build artefacts: regime_platform/sweep_orchestrator/first_passage.py (new, pure, no I/O); hmm_shadow_accuracy.py extended (base_rate/skill + wider CLI report); supabase_client.py allowlist + migration 191. Tests: tests/test_first_passage.py (34) + 8 new skill tests in tests/test_hmm_shadow_accuracy.py. Frozen weights FP_OVERLAY_W = {floor: 0.25, ceiling: 1.00} declared BEFORE scoring (same discipline as rank_score). Carry is GEOMETRIC not arithmetic -- fresh x vol_contract carries strictly less than averaging the two would, because fresh AND dir_flip is the supported cell and either alone is weaker. Floor is deliberately > 0: the level stays evidence even under the weakest timing prior. Surprise stays REJECTED (Spearman 0.004). Bar unchanged: B1-B7, B5 (beats p_regime+p_timesfm alone on DeltaD) is the real gate. Kill -> option F by 2026-11-01.$$,
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
select mechanism, status, review_at::text, left(current_state, 100) as state
from nt_roadmap where mechanism = 'bolster_legacy_hmm';
"""


def main() -> int:
    import httpx

    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    # Ref parsed from SUPABASE_URL (the variable .env.local carries).
    ref = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
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
