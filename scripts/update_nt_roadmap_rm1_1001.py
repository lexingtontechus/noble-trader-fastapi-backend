"""
Upsert nt_roadmap for 2026-10-01 RM-1 early decision:
  * markov_source_mode  -> legacy_hmm (trend_hmm_shadow retired)
  * p_markov_hold_n_mode -> off (source withdrawn from the EV blend)
  * bolster_legacy_hmm  -> new research programme (scope doc)

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_rm1_1001.py
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
  'markov_source_mode',
  $$SHADOW RETIRED 2026-10-01 (early decision). markov_source_mode -> legacy_hmm. The trend_hmm_shadow comparison ran 2026-08-25 -> 2026-10-01 and answered negatively: legacy won accuracy on 13/14 symbols (weighted 0.5274 vs 0.3191; USDJPY +0.0007 is a tie), and state disagreement was 65.9% over 63,919 compares vs a 66.7% 3-class random baseline -- statistically indistinguishable from two independent random classifiers, with no convergence over 5 weeks. trend_hmm_shadow/trend_hmm_live code paths LEFT IN PLACE (dormant); a re-evaluation needs only a YAML edit.$$,
  now(),
  NULL,
  $$Resolved. Reopen only if a NEW pre-registration proposes a different trend-state derivation (not a re-run of this comparison).$$,
  'resolved',
  'worklog/kairos/reports/20261001_rm1_markov_source_eval.md',
  $$Tier 1/2 magnitude gate itself was VALIDATED (Brier improved on 14/14 symbols, e.g. AAPL 0.7064->0.3420) and stays live for trend_probs_gate_mode. Only the legacy-vs-trend_hmm horse race is closed.$$,
  now()
),
(
  'p_markov_hold_n_mode',
  $$WITHDRAWN from the EV blend 2026-10-01 (early decision). p_markov_hold_n_mode -> "off". Sweep now passes no transition_matrix / current_state / p_markov_override to compute_ev(), so the engine's existing missing-source renormalisation drops it: blend is p_regime 0.5 / p_timesfm 0.5. P_WIN_WEIGHTS_FULL keeps its 5:4:5 relative weights, so flipping back to "live" restores the prior blend exactly with no migration.$$,
  now(),
  '2026-11-01 00:00:00+00',
  $$Re-entry bar (docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md SS3): accuracy >= 0.64 (shrunk base rate 0.5898 + 0.05), coverage >= 0.30, positive skill on >= 2/3 of symbols, holds in both era halves, AND beats p_regime+p_timesfm alone on DeltaD. Failing that by 2026-11-01, retire the model permanently (option F).$$,
  'pending',
  'docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md',
  $$Why: neither candidate source was informative. On 31,328 decided rows the legacy HMM's own directional-call accuracy is 0.5251 -- BELOW the naive 'always DOWN' baseline 0.5655 and well below a shrunk per-symbol base rate 0.5898. Mean per-symbol skill -0.0622 (positive 5/14). Its calls are also mis-mixed: it calls UP 17.6% / DOWN 17.4% against a realized population that is 43.5/56.5 DOWN. FLAT (65% of rows) is indistinguishable from the base rate. The legacy HmmModel is NOT deleted -- it still fits each tick and feeds the TA buffer + nt_sweep_result's legacy markov_p_up/markov_p_dn columns, which the bolster programme measures against.$$,
  now()
),
(
  'bolster_legacy_hmm',
  $$PROPOSED 2026-10-01. Research programme to decide whether the legacy HMM earns blend weight, or should be replaced by the first-passage machinery that already answers its question exactly. Nothing built yet; the only proposed build is a per-symbol skill diagnostic (SS5.1) so future reviews see 'skill vs baseline' on the table instead of a bare accuracy number.$$,
  now(),
  '2026-10-08 00:00:00+00',
  $$User decision on scope SS6: (Q1) build the skill diagnostic now? (Q2) Phase-1 candidates A standardize-features and/or D debounce-state? (Q3) is E (first-passage replaces the HMM) the real target? (Q4) confirm the 2026-11-01 retirement deadline.$$,
  'pending',
  'docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md',
  $$Options ranked: A standardize the 9 unscaled features (~1d, hygiene, mirrors the Change A fix that worked for RegimeHMM); D debounce markov_current_state (~1d, root fix for per-tick flapping, pattern already in-repo); B re-label states by realized first-passage outcome (~2-3d, strongest single change); C asymmetric fit/prior (~1d, risks era overfit); E replace with direct first-passage from tiered_atr.walk() (~3-4d, strategic -- the platform ALREADY answers P(reach TP before SL) exactly, so the HMM is a noisy 3-state detour); F retire permanently (zero cost, reachable now that the blend no longer depends on it).$$,
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
from nt_roadmap
where mechanism in ('markov_source_mode','p_markov_hold_n_mode','bolster_legacy_hmm')
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
