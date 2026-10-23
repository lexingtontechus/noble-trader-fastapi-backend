"""
update_nt_roadmap_r1_entry_timing.py — move the mechanism review's R1 (fix entry
timing) onto `nt_roadmap` as a pending item for future review.

Source: docs/scopes/20261016_GEOMETRY_MECHANISM_FORMULATION_REVIEW.md §3 R1 —
"Fix entry timing (the actual WR lever). Condition entries on regime age (the
EDGE_STATE_MATRIX lineage), add a deviation-Z gate (do not enter when extended),
or enter on pullback/brick-reversal rather than label-flip."

Idempotent: creates the row if absent, PATCHes it if present. No duplicate rows.

Recorded honestly, per worklog/20261013_GEOMETRY_REVIEW_VERIFICATION_AND_ERA_CLEAN_REPLAY.md:
R1's premise ("entry timing sits below the random-walk null") is asserted from the
`nt_bracket_reachability` table, which the same review's D6 flags as measuring a
retired (brick-timed) entry process. The current process's own walk is ABOVE the
null in the first half of the window and below in the second — a regime split, not
a structural deficit. So this item is PENDING A CLEAN MEASUREMENT, not pending a
build.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_r1_entry_timing.py
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
K = os.environ["SUPABASE_SECRET_KEY"]
H = {"apikey": K, "Authorization": f"Bearer {K}", "Content-Type": "application/json"}

MECHANISM = "entry_timing_conditioning"

CURRENT_STATE = (
    "No entry-timing conditioning. Entries fire on the regime-label flip, so the label turns "
    "bull after the move has matured and the entry buys into exhaustion. The one proven "
    "counter-trend cell (ID45: fading gold buys wins 38.2% vs following them 6.5%) has no live "
    "consumer: the strategy router that would route to it declines 99.6% of signals "
    "(stand_by 923/927) and ships SHADOW, and its R2 flip gate (>=200 routed across >=8 symbols) "
    "is unmet at routed n=4. No deviation-Z / regime-age / pullback entry predicate exists."
)

REVIEW_CONDITION = (
    "Review when a CLEAN entry-timing measurement exists: the current-process TP rate vs the "
    "random-walk null SL/(TP+SL) per bracket, computed on contaminated=false + post-cutover rows. "
    "The review's premise rests on nt_bracket_reachability, which its own D6 flags as measuring a "
    "retired brick-timed entry process; the current process's walk is ABOVE the null in the first "
    "half of the measured window and below in the second (regime split, not a structural deficit). "
    "Until that measurement exists R1 is unproven, not merely unbuilt."
)

NOTES = (
    "R1 (fix entry timing) moved to the roadmap 2026-10-13 from "
    "docs/scopes/20261016_GEOMETRY_MECHANISM_FORMULATION_REVIEW.md section 3. "
    "R1 IS THE WR-LEVER CANDIDATE, NOT A GEOMETRY FIX: geometry reshapes the loss distribution, it "
    "cannot manufacture a win rate, and at the widened forex band breakeven is RR-derived "
    "p* = 1/(1+1.64) = 37.9% against a random-entry null of SL/(TP+SL) = 37.8% - i.e. the bracket "
    "needs no directional edge to break even, only to not be adverse-timed. "
    "CANDIDATE MECHANISMS (from R1, unranked, none measured): (a) condition on regime AGE "
    "(EDGE_STATE_MATRIX lineage) - do not enter on a fresh label flip; (b) a deviation-Z gate - do "
    "not enter when price is extended from its mean; (c) enter on pullback / brick-reversal instead "
    "of label-flip. The ID45 counter-trend cohort is a ready-made mean-reversion archetype if (c) is "
    "pursued - it already exists as a catalog entry, it just has no live consumer. "
    "EVIDENCE CAVEAT (2026-10-13 verification): the review's crux - measured TP rates below the "
    "random-walk null - is computed from nt_bracket_reachability, whose history the review's own D6 "
    "says is dominated by the OLD brick-timed entry process and therefore measures a selection "
    "effect that no longer exists. The clean re-run of the forex band on contaminated=false + "
    "post-cutover rows (scripts/replay_bands_era_clean.py) gives e1 +0.041 / e2 -0.209 for the "
    "shipped band, i.e. ABOVE the null in the first half and below in the second. So the deficit is "
    "regime-conditional on current evidence, and R1's premise is NOT established. "
    "PREREQUISITE: the same clean measurement this platform already applies elsewhere "
    "(bracket_reachability and dynamic_tier_optimizer filter contaminated=false; atr_reopt clamps "
    "to nt_calibration_cutover) but which _load_class_signals - the geometry walk's own loader - "
    "does not. Fix that before judging any entry-timing hypothesis, or the measurement repeats the "
    "contamination. "
    "RELATED ROWS: strategy_router_r1 / strategy_router_r2_measurement / strategy_live_route_r6 "
    "(the router that would consume the archetype), mean_reversion_gold_quant (the ID45 cell), "
    "tier1_context_alignment_gate_scope, regime_edge_gate."
)

WORKLOG_REF = (
    "worklog/20261013_GEOMETRY_REVIEW_VERIFICATION_AND_ERA_CLEAN_REPLAY.md "
    "(+ docs/scopes/20261016_GEOMETRY_MECHANISM_FORMULATION_REVIEW.md section 3 R1)"
)

REVIEW_AT = "2026-11-10T00:00:00+00:00"   # ~4 weeks: long enough for the clean window to fill


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "mechanism": MECHANISM,
        "status": "pending",
        "current_state": CURRENT_STATE,
        "review_condition": REVIEW_CONDITION,
        "notes": NOTES,
        "worklog_ref": WORKLOG_REF,
        "review_at": REVIEW_AT,
        "updated_at": now,
    }
    existing = httpx.get(
        f"{U}/rest/v1/nt_roadmap", headers=H,
        params={"select": "mechanism", "mechanism": f"eq.{MECHANISM}"}, timeout=20,
    ).json()

    if existing:
        r = httpx.patch(f"{U}/rest/v1/nt_roadmap", headers=H,
                        params={"mechanism": f"eq.{MECHANISM}"}, json=payload, timeout=20)
        print(f"PATCH {MECHANISM}: {r.status_code} (row existed)")
    else:
        r = httpx.post(f"{U}/rest/v1/nt_roadmap", headers=H,
                       json={**payload, "set_at": now}, timeout=20)
        print(f"POST {MECHANISM}: {r.status_code} (row created)")

    chk = httpx.get(
        f"{U}/rest/v1/nt_roadmap", headers=H,
        params={"select": "mechanism,status,review_at,review_condition,worklog_ref,updated_at",
                "mechanism": f"eq.{MECHANISM}"}, timeout=20,
    ).json()
    if not chk:
        print("VERIFY FAILED: row not found after write")
        return 1
    row = chk[0]
    print(f"  status={row['status']}  review_at={row['review_at']}")
    print(f"  worklog_ref={row['worklog_ref']}")
    print(f"  updated_at={row['updated_at']}")

    # idempotency: a second run must not create a duplicate
    n = httpx.get(f"{U}/rest/v1/nt_roadmap", headers=H,
                  params={"select": "mechanism", "mechanism": f"eq.{MECHANISM}"}, timeout=20).json()
    print(f"  rows with this mechanism: {len(n)} (must be 1)")
    return 0 if len(n) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
