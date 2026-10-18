"""
Record the Phase 2 result for the HMM->first-passage programme (2026-10-01).

Verdict: 1/5 bar items pass. Kill criteria met.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_phase2_result_1001.py
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
  $$PHASE 2 RESULT 2026-10-01: first-passage does NOT clear the bar -- 1/5 items pass. B1 FAIL (skill -0.0691 vs base rate 0.7041), B2 PASS (coverage 0.997), B3 FAIL (3/14 symbols positive), B4 FAIL (wrong-signed in the first half), B5 FAIL (the D5 regime-transition overlay makes it WORSE: DeltaD +0.0236 vs bare +0.0502, delta -0.0265). Population: 6,904 decided light episodes, 45d, one closed 48-bar path per signal, episode-deduped. THE HONEST FLOOR: a bracket-blind trailing per-symbol base rate scores DeltaD +0.059 -- ABOVE first-passage's +0.050 -- so the bracket conditioning bought nothing, and the trivial base-rate arm (+0.076 DeltaD, 100% coverage, zero fitting) is now the best-scoring candidate on the table.$$,
  now(),
  '2026-11-01 00:00:00+00',
  $$DECISION REQUESTED (kill criteria met, 3 weeks early). Option 1 (recommended): take F now -- ship the calibrated base rate as the replacement level, retire the legacy HMM (delete services/hmm/model.py + feature_buffer.py + hmm_quote_fetcher call + MARKOV_SOURCE_MODE/p_markov_hold_n_mode; keep hmm_ta_fetcher/latest_ta for nt_regime_log.ta_*). Option 2 (time-boxed): one more formulation -- drop the overlay and condition the LEVEL on the regime cell (the cell_p shape already scoring +0.145 IC) instead of a geometry bucket. Option 3: bank the finding, no further action.$$,
  'pending',
  'worklog/20261001_PHASE2_FIRST_PASSAGE_RESULT.md',
  $$WHAT SURVIVES: the SS2.4 thesis (walk() still answers the Markov question exactly; the HMM still scores below the naive baseline) and the D1/D2 retirement (p_markov_hold_n_mode stays "off", markov_source_mode stays legacy_hmm -- nothing here argues for turning the HMM back on). WHAT DIES: option E as specified, and the D5 overlay in its current form (measured, and it subtracts). Three bugs were caught in the build that would each have produced a confident wrong answer: (1) look-ahead -- scoring a signal on its own path is a binary outcome, rebuilt as a walk-forward trailing rate; (2) a hand-rolled regime parser that did not match the raw label format, making the overlay a silent no-op that still reported as measured (now delegates to kairos.regime_cell); (3) B5 scored on sign-accuracy, which is structurally blind to a shrink-toward-0.5 transform -- moved to DeltaD. Plus the honest floor (trailing_base_rate, bracket-blind) so a geometry arm cannot claim credit for momentum. Tests: 48 in test_fp_eval.py; full suite 1491 passed.$$,
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
select mechanism, status, review_at::text, left(current_state, 120) as state
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
