"""
Upsert nt_roadmap for the claims layer required actions, from
worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md + worklog/20261027_KAIROS_CLAIMS_PHASE_A_BUILD.md
(phases B-E status grounded in the 20261028 build worklogs + live DB probes 2026-11-18).

Live probes behind these rows (2026-11-18, project pcvscowltlrxzgxjurcr):
  * nt_signal_claims: 0 rows (ledger empty).
  * Platform heartbeat DEAD since 2026-10-16 03:07 UTC (last nt_sweep_result
    03:00:52, last nt_signal_sim 03:07:10, 0 sweeps 24h, 0 sim rows 7d).
  * Migrations 218/220/221 applied (arm+calib_level columns, matview claim
    column verified via pg_matviews -- information_schema.columns does NOT
    list matviews, 221 crt table present).

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_claims_20261027.py
(Requires a live SUPABASE_ACCESS_TOKEN; the local .env.local PAT was stale/401
on 2026-11-18 -- the same SQL was applied via the Supabase MCP connector.)
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
  'claims_mode',
  $$Phase A BUILT 2026-10-27 (registration inside the qualified block on light+heavy, resolver scoring inside _resolve_one/_resolve_one_bars via the SAME settlement contract, 14d stale-claim void sweep). claims_mode=shadow shipped; migration 218 applied. LIVE PROBE 2026-11-18: nt_signal_claims EMPTY (0 rows) and the platform heartbeat is DEAD since 2026-10-16 03:07 UTC (last sweep 03:00:52, last sim 03:07:10, 0 sweeps 24h, 0 sim rows 7d). The claims layer cannot accrue until the Studio stack is redeployed/restarted with the claims code.$$,
  now(),
  '2026-11-25 00:00:00+00',
  $$Studio stack restarted with the claims code (start.sh; Studio config/* is LIVE STATE -- verify claims_mode=shadow there, never overwrite from a checkout). Then: first bracket claim at the next qualified signal; first resolver-scored claim ~48h later. Verify via pulse-check liveness gate + nt_signal_claims open rows newer than the restart.$$,
  'blocked',
  'worklog/20261027_KAIROS_CLAIMS_PHASE_A_BUILD.md',
  $$BLOCKER: platform outage since 2026-10-16 03:07 UTC (~33 days at registration). Phase A operator action #1 (apply 218) is DONE; action #2 (nothing else -- shadow picks up on next tick) is moot until the sweep runs again. Fan = house ATR normal-approx (source=atr_normal), the CONTROL arm the Phase D recalibrated fans must beat. No vol_fan claims until claims_vol_fan_mode flips. One claim per signal per kind; re-qualification mints a new signal_id + claim.$$,
  now()
),
(
  'claims_publish',
  $$Phase B BUILT 2026-10-28, ships OFF. Transport verified live 2026-11-18: v_talaria_latest_signals carries the claim JSONB column (219+220 matview rebuild applied; pg_matviews definition joins nt_signal_claims), so the bundle path (/api/dashboard-data + talaria_dashboard_cache, pg_cron refresh) rides with zero code. Broadcast claim object gated on claims_publish=live, fired only AFTER the claim PERSISTS. Phase C SignalContextCard shipped in nobletradingapp dc29cf7 (per Phase E worklog) -- dashboard verification still owed.$$,
  now(),
  '2026-11-25 00:00:00+00',
  $$Flip off->live ONLY after (a) the ledger accrues (claims_mode row) AND (b) SignalContextCard is verified rendering in the dashboard (fan + bracket lines + claim chips). Operator green-light, evidence not calendar. MT5 copy path byte-identical by construction (talaria-copy SIGNAL_COLUMNS allow-list never selects claim fields).$$,
  'pending',
  'worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md',
  $$Design §4 row B + §1 transport contract. Cost when live: one qualified signal emits TWO broadcast events (primary without claim, then payload+claim ~100-500ms later); frontend replaces per-symbol, EA/bridge dedupes on symbol|sweep_timestamp -- benign over-delivery. Primary broadcast latency/shape unchanged in ALL modes. worklog/20261028_KAIROS_CLAIMS_PHASE_BC_BUILD.md §3.$$,
  now()
),
(
  'claims_vol_fan_mode',
  $$Phase D BUILT 2026-10-28, ships OFF; migration 220 applied (arm/calib_level columns verified 2026-11-18). Pre-registered arm = tsfm (T3 flip 2026-10-28, dark; code fallback egarch). PRECONDITIONS: arch>=7.0.0 installed on the deployment (ACTIVE in requirements.txt since Phase E -- Studio install happens at deploy), recalib artifact in place. HONEST GAP (T3 record): the deployed config/claims_vol_fan_recalib.json carries garch/gjr/egarch cells ONLY -- the tsfm arm runs identity calibration (calib_level=none) until the operator mints tsfm cells. The GARCH-era leaderboard baseline has NOT started accruing: the Phase D shadow deploy that starts that clock is still pending.$$,
  now(),
  '2026-12-01 00:00:00+00',
  $$After the claims_mode deploy + arch install: flip off->shadow (mtime hot-reload, next qualified signal). Mint tsfm recalib cells: scripts/run_vol_backtest.py --arms garch,gjr,egarch,tsfm + python -m vol_backtest.recalib. Optional feed-match: re-mint from TDVA (the shipped artifact was fitted on the lab broker CSVs, 5 thesis symbols; other symbols register identity-calibrated, stamped calib_level=none). Verify sibling bracket+vol_fan rows per signal with honest arm/calib_level stamps.$$,
  'pending',
  'worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md',
  $$Design §4 row D (GJR-t/EGARCH-t port via arch + MZ+k recalib). k-transfer is a HYPOTHESIS the ledger tests (k fitted on RV-scale intervals, applied to the price fan log-spread) -- coverage will say if the transfer is wrong; that is the point of the claims layer. Fan asserts zero drift (median=entry at every h). AND-gated on claims_mode. worklog/20261028_KAIROS_CLAIMS_PHASE_D_BUILD.md §4-5 + feature_flags.yaml T3 flip record.$$,
  now()
),
(
  'claims_prior_mode',
  $$Phase E BUILT 2026-10-28, ships OFF (zero I/O, zero arithmetic change -- byte-identical to pre-Phase-E). Bridge: per-symbol resolved-claim coverage evidence modulates BAYES_PRIOR_COUNT in the ONE existing shrink funnel -- calibrated => x0.75 (Wilson floor required), miscalibrated => x1.5, else x1.0. vol_fan preferred once n>=10, bracket control otherwise; TTL-cached 300s; ANY error => None (fail-open standard shrink). Never touches the 0.5 anchor, observation count, or raw p_win.$$,
  now(),
  '2026-12-15 00:00:00+00',
  $$Flip off->live ONLY when GET /claims/leaderboard shows calibrated cells for the symbols in question: pooled coverage80 in the [0.80, 0.95] acceptance band, Wilson-accepted, n_claims floor met (band + floor judged TOGETHER -- pooled-bar Wilson is optimistic under within-claim correlation). Evidence, not calendar. Requires resolved vol_fan claims, i.e. weeks after the claims_vol_fan_mode shadow flip.$$,
  'pending',
  'worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md',
  $$Design §1: claims may INFORM the shrink prior only if coverage proves reliable on era-clean replay -- opt-in, evidence-gated, pbm_* discipline. Gentle bounds by design: fan coverage is ADJACENT evidence for directional p_win, not a measurement of it; the trust bonus (x0.75) is the dangerous direction, hence the Wilson floor. worklog/20261028_KAIROS_CLAIMS_PHASE_E_BUILD.md §3+§5.$$,
  now()
),
(
  'claims_arm11_leaderboard',
  $$Phase E endpoint BUILT 2026-10-28 (read-only, NOT flag-gated -- computes nothing the ledger does not hold; live as soon as the code deploys). EMPTY until claims resolve: first scored claims ~48h after the first post-218 qualified signal; paired-DM needs BOTH siblings resolved per signal (endpoint reports insufficient_pairs until vol_fan shadow runs). Design §6 question: does the registered thesis distribution beat the control on the same cells -- measured with DM p-values, not argued. If not, the thesis layer is decoration on top of vol targeting.$$,
  now(),
  '2026-12-15 00:00:00+00',
  $$Let the ledger accrue (weeks of clock time, not a today-verification). Acceptance bar (design §6): vol-fan claim coverage80 lands in the [0.80, 0.95] Wilson band after Phase D recalibration. Inspect GET /claims/leaderboard: per-arm/calib_level breakdowns + paired DM vs the house ATR control. Record the verdict here; feed claims_prior_mode only on calibrated cells.$$,
  'pending',
  'worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md',
  $$HONEST LIMITS (Phase E §6): the lab join is provenance, not comparison -- claims are 48h-only, lab PASS cells are 6-72h; the paired-DM baseline is the house ATR control, NOT Log-HAR. Beating the lab BTCUSD 6h CRPS-ratio 0.727 in the strict cross-harness sense stays unanswerable until a shared-cell replay harness exists. thesis-kind claims aggregate automatically when a registrant ever lands (nothing writes them today). worklog/20261028_KAIROS_CLAIMS_PHASE_E_BUILD.md §6-7.$$,
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
select id, mechanism, status, review_at::text, left(current_state, 100) as state
from nt_roadmap
where mechanism in ('claims_mode','claims_publish','claims_vol_fan_mode',
                    'claims_prior_mode','claims_arm11_leaderboard')
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
