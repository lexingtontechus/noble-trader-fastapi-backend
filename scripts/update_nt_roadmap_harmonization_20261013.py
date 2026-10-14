"""update_nt_roadmap_harmonization_20261013.py — record the HARM-01..06
harmonization findings on nt_roadmap.

Idempotent: PATCHes notes + current_state, always the same text. Run from the
project venv:

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_harmonization_20261013.py

Measured 2026-10-13 (pulse check 20:15 UTC + scripts/replay_router_harmonization.py):

  HARM-01 FIXED   Asset-class vocabulary split. nt_symbol.asset_class='commodities'
                  for the four golds -> asset_class_key() 'commodity', but no enabled
                  archetype listed 'commodity'. Router replayed 0/225. Fixed: 'commodity'
                  added to momentum + mean_reversion asset_classes. Replay 19->59 routed
                  (+40, all metals; mean_reversion now fires on 13 XAUAUD/XAGUSD rows).
                  13 E2E tests. Full suite 2069 passed / 22 skipped / 0 failed.
  HARM-02 FIXED   Deadline floors declared but unenforced. min_soft_deadline_hours 12.0 /
                  max_hard_deadline_hours 120.0 returned by
                  get_dynamic_deadline_config_for_symbol() but never applied in
                  _compute_dynamic_deadline. AUDUSD shipped soft=0.5h vs 2.62h avg
                  settle; 118/225 rows breached their own soft deadline. Fixed: clamp
                  sourced from the same config, mirroring dynamic_deadline.derive_deadline_tier.
  HARM-03 VERIFIED Reachability staleness guard (6.5d) vs weekly build interval (7d) —
                  correct, pinned by test.
  HARM-04 OPEN    BINDING CONSTRAINT. macro_label/trend_label refresh daily (06:00/06:10
                  UTC) while signals fire every 15 min -> tri-state alignment up to 24h
                  stale. Zero rows in 3 days with both reads directional (last
                  2026-10-09 06:00Z) -> alignment frozen at 'consolidating' -> stand_by.
                  AS-SHIPPED replay after HARM-01: still 0/206. Fixing the asset-class
                  plumbing alone does not make the router route. This is the frequency
                  defect. Recommendation: move the label refreshes onto the existing
                  pre-sweep '5,20,35,50 * * * *' dynamic-tier-optimization cadence —
                  same dataset, no new computation. NOT done: changes which signals
                  route live.
  HARM-05 PINNED  mean_reversion.regime_vol [low_vol, med_low_vol] excludes the measured
                  best cell (med_high_vol 52.9% WR n=17 vs low_vol 27.3% n=11). Widening
                  is a strategy decision needing the B1 evidence bar.
  HARM-06 VERIFIED Forex majors intentionally uncovered — B1 measured 0 false-cohort rows
                  across ~3,300 era-clean resolved forex signals; ma_trend stays disabled.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
_env = ROOT / ".env.local"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

U = os.environ["SUPABASE_URL"].rstrip("/")
K = os.environ["SUPABASE_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}", "Content-Type": "application/json"}

MECHANISM = "harmonization_single_source_of_truth"
REVIEW_AT = "2026-10-17T00:00:00+00:00"
WORKLOG_REF = "worklog/20261013_HARMONIZATION_SINGLE_SOURCE_OF_TRUTH.md"

REVIEW_CONDITION = (
    "Decide HARM-04 (the binding constraint): move macro_label/trend_label refresh onto "
    "the pre-sweep '5,20,35,50 * * * *' cadence, or compute alignment at signal time. "
    "Settled by re-running scripts/replay_router_harmonization.py — the AS-SHIPPED arm "
    "must exceed 0/206, and every newly-routed cell must clear tp>14.3% and avgR>0 (the "
    "2026-10-13 measured floor for routed cells). Separately decide HARM-05: widen "
    "mean_reversion.regime_vol to include med_high_vol only if the B1 bar is met "
    "(era-clean XAUUSD-buy false-cohort n>=40 AND held-out ratio>=1.5x)."
)

CURRENT_STATE = (
    "HARM-01 + HARM-02 FIXED 2026-10-13; HARM-04 OPEN AND BINDING. "
    "(1) HARM-01 asset-class vocabulary split: nt_symbol.asset_class='commodities' for "
    "the four golds -> asset_class_key() 'commodity', but no enabled archetype listed "
    "'commodity', so every metal routed stand_by — router replay 0/225. Fixed by adding "
    "'commodity' to momentum + mean_reversion asset_classes in config/strategy_registry.yaml "
    "(vocabulary widened, 'metal' kept). Replay 19->59 routed, +40 all metals; "
    "mean_reversion now fires on 13 XAUAUD/XAGUSD buy rows it never reached before. "
    "(2) HARM-02 deadline floors declared but unenforced: min_soft_deadline_hours 12.0 / "
    "max_hard_deadline_hours 120.0 were returned by get_dynamic_deadline_config_for_symbol() "
    "but never applied in dynamic_tier_optimizer._compute_dynamic_deadline — AUDUSD shipped "
    "soft_deadline_hours=0.5 against a 2.62h average settle, and 118 of 225 resolved rows "
    "breached their own soft deadline. Fixed: clamp now sourced from the same config and "
    "mirrors dynamic_deadline.derive_deadline_tier. (3) HARM-04 IS THE BINDING CONSTRAINT "
    "AND IS OPEN: macro_label/trend_label refresh daily (06:00/06:10 UTC) while signals "
    "emit every 15 min, so the router's tri-state alignment is up to 24h stale — zero rows "
    "in 3 days with both reads directional (last 2026-10-09 06:00Z), alignment frozen at "
    "'consolidating'. AS-SHIPPED replay after the HARM-01 fix is still 0/206: fixing the "
    "asset-class plumbing alone does not make the router route. Recommendation: move the "
    "label refreshes onto the existing pre-sweep '5,20,35,50 * * * *' "
    "dynamic-tier-optimization cadence (same dataset, no new computation). NOT done "
    "because it changes which signals route live. (4) HARM-05 pinned: "
    "mean_reversion.regime_vol [low_vol, med_low_vol] excludes the measured best cell "
    "(med_high_vol 52.9% WR n=17 vs low_vol 27.3% n=11) — widening needs the B1 evidence "
    "bar. (5) HARM-03 + HARM-06 verified correct and pinned by tests. "
    "Router mode is shadow, so HARM-01 changes what is measured, not what is traded; "
    "dynamic_deadline_mode is also shadow, so HARM-02 bounds a diagnostic only."
)

NOTES = (
    "HARMONIZATION 2026-10-13 (worklog/20261013_HARMONIZATION_SINGLE_SOURCE_OF_TRUTH.md, "
    "tests/test_harmonization_20261013.py, scripts/replay_router_harmonization.py). "
    "Trigger: pulse check router replay returned routed 0/225, stand_by 100%. "
    "HARM-01 FIXED — asset-class vocabulary split, three vocabularies for one concept: "
    "nt_symbol.asset_class='commodities' (DB), asset_class_key() -> 'commodity' "
    "(normaliser), registry asset_classes: [metal] (allow-list). A gold row could never "
    "match a gold archetype regardless of its macro/trend reads. Fix: 'commodity' added to "
    "momentum + mean_reversion in config/strategy_registry.yaml; 'metal' kept so a literal "
    "metal class still routes. 13 E2E tests, 8 written RED first and watched fail for the "
    "expected reason. Full suite 2069 passed / 22 skipped / 0 failed. Replay BEFORE "
    "(pre-fix vocabulary) 19/206 vs AFTER 59/206 with the HARM-04 blocker relaxed in both "
    "arms so the two differ only by the vocabulary — +40 rows, all metals; "
    "mean_reversion fires on 13 XAUAUD/XAGUSD buy rows. "
    "HARM-02 FIXED — deadline floors declared in feature_flags.yaml "
    "(min_soft_deadline_hours 12.0, max_hard_deadline_hours 120.0) and returned by "
    "get_dynamic_deadline_config_for_symbol() but never applied in "
    "dynamic_tier_optimizer._compute_dynamic_deadline, which wrote raw p75/p90. Measured "
    "consequence: AUDUSD soft_deadline_hours=0.5 vs 2.62h average settle; 118/225 resolved "
    "rows breach soft, 55 breach hard. Fix clamps from the same config and mirrors "
    "dynamic_deadline.derive_deadline_tier:116-117, preserving hard > soft (otherwise "
    "DeadlineTier.decay() divides by a non-positive span). "
    "HARM-04 OPEN, BINDING — the frequency defect the harmonization principle names. "
    "macro_label/trend_label refresh daily (06:00/06:10 UTC) while signals emit every 15 "
    "min; the tri-state alignment is therefore up to 24h stale by construction. Zero rows "
    "in 3 days with both reads directional (last 2026-10-09 06:00Z) — alignment frozen at "
    "'consolidating' for whole days. AS-SHIPPED replay after HARM-01 is still 0/206, i.e. "
    "the plumbing fix alone does not make the router route. Three levers considered; "
    "relaxing the tri-state was MEASURED AND REJECTED (counterfactual C/D route the book's "
    "worst cells — every routed cell tp<=14.3%, avgR<=0). Recommendation: lever 1, move the "
    "label refreshes onto the existing pre-sweep '5,20,35,50 * * * *' "
    "dynamic-tier-optimization job — same dataset, no new computation. Deliberately not "
    "done here: it changes which signals route live and needs its own decision. "
    "HARM-05 PINNED — mean_reversion.regime_vol [low_vol, med_low_vol] excludes the "
    "measured best cell (med_high_vol 52.9% WR n=17 > low_vol 27.3% n=11); widening is a "
    "strategy change requiring the B1 evidence bar, pinned by a test that asserts the "
    "contradiction so it cannot be widened silently. "
    "HARM-03 VERIFIED — BRACKET_REACHABILITY_STALE_AFTER (6.5d) is correctly shorter than "
    "the 7d weekly build interval; pinned by test. "
    "HARM-06 VERIFIED — the 6 forex majors are intentionally uncovered (B1 measured 0 "
    "false-cohort rows across ~3,300 era-clean resolved forex signals; ma_trend stays "
    "disabled); pinned by an explicit INTENTIONALLY_UNCOVERED allow-list so adding a "
    "symbol there requires its evidence. "
    "Live impact: NONE. Router mode is shadow (stamp-only) and dynamic_deadline_mode is "
    "shadow, so HARM-01 changes what is measured and HARM-02 bounds a diagnostic."
)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()

    r = httpx.patch(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"mechanism": f"eq.{MECHANISM}"},
        json={
            "current_state": CURRENT_STATE,
            "status": "pending",
            "review_at": REVIEW_AT,
            "notes": NOTES,
            "updated_at": now,
        },
        timeout=20,
    )
    print(f"PATCH {MECHANISM}: HTTP {r.status_code}")

    # Create-then-patch if the row does not exist yet (idempotent either way).
    r2 = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"select": "mechanism,status,review_at,notes", "mechanism": f"eq.{MECHANISM}"},
        timeout=20,
    )
    rows = r2.json()
    if not rows:
        print("row absent — creating")
        rc = httpx.post(
            f"{U}/rest/v1/nt_roadmap",
            headers=H,
            json={
                "mechanism": MECHANISM,
                "current_state": CURRENT_STATE,
                "review_condition": REVIEW_CONDITION,
                "status": "pending",
                "review_at": REVIEW_AT,
                "notes": NOTES,
                "worklog_ref": "worklog/20261013_HARMONIZATION_SINGLE_SOURCE_OF_TRUTH.md",
                "updated_at": now,
            },
            timeout=20,
        )
        print(f"POST: HTTP {rc.status_code}")
        rows = httpx.get(
            f"{U}/rest/v1/nt_roadmap",
            headers=H,
            params={
                "select": "mechanism,status,review_at,notes",
                "mechanism": f"eq.{MECHANISM}",
            },
            timeout=20,
        ).json()

    row = rows[0]
    print(f"  status      : {row['status']}")
    print(f"  review_at   : {row['review_at']}")
    print(f"  notes chars : {len(row['notes'] or '')}")
    print(f"  notes head  : {(row['notes'] or '')[:90]}")

    cnt = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"select": "count", "mechanism": f"eq.{MECHANISM}"},
        timeout=20,
    ).json()
    print(f"  row count   : {cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
