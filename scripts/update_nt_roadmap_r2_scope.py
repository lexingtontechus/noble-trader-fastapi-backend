"""
update_nt_roadmap_r2_scope.py — point the strategy-router roadmap rows at the
new R2 measurement scope doc, and record the current R1 sample reality.

Idempotent: re-running PATCHes the same text. Run from the project venv.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_r2_scope.py

Writes to nt_roadmap:
  strategy_router_r2_measurement — worklog_ref -> docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md,
    current_state + review_condition updated to the doc's bar (ROUTED episodes, not total stamps).
  strategy_router_r1 — notes record the stand_by dominance (923/927) and link the R2 doc.
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

DOC = "docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md"

R2_STATE = (
    "R2 SCOPE DRAFTED 2026-10-12 -> docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md. "
    "R2 = the gate that decides whether routing helps at all. NOT STARTED (no measurement script yet). "
    "Sample reality 2026-10-12: 927 stamped / 769 closed, but 923 stand_by / 4 momentum / 0 mean_reversion "
    "- the router is functionally a decline-all (predicates need BOTH macro and trend definite AND in "
    "agreement, so ~99.6% of signals fall to stand_by). The old bar (>=200 stamped across >=8 symbols) is "
    "MET on raw count but ~2% met on routed episodes."
)

R2_CONDITION = (
    "Bar is ROUTED (non-stand_by) episodes, NOT total stamps: >=200 routed across >=8 symbols AND >=40 in "
    "the smallest archetype evaluated. Test H1 routed vs stand_by expectancy, H2 per-archetype vs uniform "
    "baseline, H3 stand_by-as-suppress (descriptive only, 99.6% of book). Era-clean (post-2026-10-02), "
    "walk-forward both halves. Verdict: ROUTING HELPS / INERT / ANTI-PREDICTIVE / UNDECIDABLE. Build R2a "
    "script scripts/kairos_r2_router_measure.py (runnable now, will report UNDECIDABLE)."
)

R2_NOTES = (
    "R2a (measurement script) can be built and run immediately - it will report UNDECIDABLE given the "
    "current sample. R2b: do NOT loosen predicates to manufacture a sample (in-sample-overfit trap); a "
    "looser predicate is a separate pre-registered hypothesis with its own walk-forward. R3 is gated on an "
    "R2 'helps' verdict; R4 (Clef-Flash Role A) is measured against R2's router."
)

R1_NOTES = (
    "2026-10-12: R1 sample reality measured - 927 stamped / 769 closed, of which 923 stand_by / 4 momentum "
    "/ 0 mean_reversion. The router declines ~99.6% of signals because its predicates require BOTH "
    "macro_label AND trend_label to be definite and in agreement; a neutral macro or a consolidating trend "
    "returns alignment=None -> stand_by. This is a predicate-strictness finding, not a wiring failure. R2's "
    "bar must therefore count ROUTED episodes, not total stamps - see " + DOC + "."
)


def patch(mechanism: str, payload: dict) -> None:
    payload = {**payload, "updated_at": datetime.now(timezone.utc).isoformat()}
    r = httpx.patch(
        f"{U}/rest/v1/nt_roadmap", headers=H, params={"mechanism": f"eq.{mechanism}"}, json=payload, timeout=20
    )
    print(f"PATCH {mechanism}: {r.status_code}")


def main() -> int:
    patch(
        "strategy_router_r2_measurement",
        {
            "current_state": R2_STATE,
            "review_condition": R2_CONDITION,
            "notes": R2_NOTES,
            "worklog_ref": DOC,
        },
    )
    patch("strategy_router_r1", {"notes": R1_NOTES, "worklog_ref": DOC})

    r = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={
            "select": "mechanism,current_state,review_at,status,worklog_ref",
            "mechanism": "in.(strategy_router_r1,strategy_router_r2_measurement)",
        },
        timeout=20,
    )
    for row in r.json():
        print(f"\n{row['mechanism']}  status={row['status']}  review_at={row['review_at']}")
        print(f"  worklog_ref: {row['worklog_ref']}")
        print(f"  state: {row['current_state'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
