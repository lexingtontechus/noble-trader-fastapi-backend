"""
Upsert nt_roadmap for the 2026-10-01 bolster_legacy_hmm DECISIONS:
option E (first-passage replaces the HMM) agreed as the target, reinforced
by the regime-transition overlay. See worklog/20261001_HMM_FIRST_PASSAGE_
AND_REGIME_TRANSITION.md.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_hmm_e_1001.py
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
  $$DECIDED 2026-10-01: option E AGREED as the real target -- first-passage replaces the HMM. Scope SS2.4 accepted: tiered_atr.walk() already answers P(reach TP before SL) exactly on the price path, so the HMM is a noisy 3-state detour. A/B/C/D demoted to optional hygiene (only if a discrete-state regime read is independently wanted). REINFORCED with regime transition (D5): Kairos-proven freshness (+0.117 fresh-deep) x transition type (dir_flip the only non-negative type) conditions the first-passage level by WHEN the regime last changed. Build plan: 4 phases, pre-registered bar, kill criteria.$$,
  now(),
  '2026-10-08 00:00:00+00',
  $$Phase 1 built (skill diagnostic + p_first_passage primitive, both offline). Then Phase 2 offline eval: first-passage vs shrunk base-rate control (0.5898) vs legacy HMM (incumbent), on scorecard D (DeltaD accuracy-vs-no-skill, gated vs rest), era halves, symbol-day clustered CIs, episode unit.$$,
  'pending',
  'worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md',
  $$Bar (B1-B7): B1 beat the base-rate control; B2 coverage >= 0.30; B3 positive skill on >= 2/3 symbols; B4 holds both era halves; B5 INCREMENTAL -- beats p_regime+p_timesfm alone on DeltaD with CI > 0 (the real gate); B6 episode unit (symbol x direction x hour, deduped); B7 symbol-day clustered CIs. Kill criteria -> option F (ship calibrated base rate, delete services/hmm/model.py + feature_buffer.py + hmm_quote_fetcher call + MARKOV_SOURCE_MODE/p_markov_hold_n_mode; keep hmm_ta_fetcher/latest_ta for nt_regime_log.ta_*). Risks: R1 look-ahead (offline on resolved rows only; trailing paths for any live stamp), R2 first-passage may duplicate dynamic_atr_pick (B5 settles it), R3 base rate is era-dependent (2026 window 56.5% DOWN), R4 cell_p persistence inflates DeltaD, R5 overlay overfits the 09-26 mixed-era discovery window, R6 post-ATR n=43 is thin.$$,
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
select mechanism, status, review_at::text, left(current_state, 110) as state
from nt_roadmap
where mechanism in ('bolster_legacy_hmm','p_markov_hold_n_mode','markov_source_mode')
order by mechanism;
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
