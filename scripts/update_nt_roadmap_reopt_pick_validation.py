"""
update_nt_roadmap_reopt_pick_validation.py — record the free-lattice pick
validation as a roadmap followup.

The harmonized reopt (37af9ee) reports its free-lattice picks with
allow_best_effort=True, so a reported EV can belong to a cell that does NOT
clear the strict both-halves bar. Two picks were checked with
allow_best_effort=False (scripts/validate_reopt_picks.py):

  forex  9.75/1.39  EV +0.252  e1 +0.850  e2 -0.346  -> FAILS (best-effort only)
  BTCUSD 15.19/2.11 EV +0.921  e1 +1.180  e2 +0.661  -> SURVIVES

Idempotent: creates the row if absent, PATCHes it if present.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_reopt_pick_validation.py
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

MECHANISM = "reopt_free_lattice_pick_validation"

CURRENT_STATE = (
    "OPEN. The harmonized reopt's free-lattice picks are reported with "
    "allow_best_effort=True, so a reported EV can belong to a cell that does NOT "
    "clear the strict both-halves bar. Scored with allow_best_effort=False on the "
    "same window the reopt used (2026-08-27..2026-10-11, min_rr 1.5, cost 0.1): "
    "BTCUSD 15.19/2.11 SURVIVES (EV +0.921, e1 +1.180, e2 +0.661, n 236, wr 0.185, "
    "expiry 5.9%); forex 9.75/1.39 FAILS (EV +0.252, e1 +0.850, e2 -0.346, n 1041, "
    "wr 0.157, expiry 0.6%) — its whole edge is first-half and it would not be "
    "selected by the strict solve. Neither has forward outcomes yet."
)

REVIEW_CONDITION = (
    "Review when forward outcomes exist for the two cells: the realised win/loss "
    "record of signals stamped tp_distance_atr/sl_distance_atr at BTCUSD 15.19/2.11 "
    "and forex 9.75/1.39 on post-cutover (contaminated=false) rows. Settles whether "
    "a both-halves-positive free-lattice pick holds forward, and whether a "
    "best-effort (e2<0) pick is worth writing at all."
)

NOTES = (
    "Exact record (scripts/validate_reopt_picks.py, window 2026-08-27..2026-10-11, "
    "live intrabar/sl_first basis, min_rr 1.5, cost 0.1):\n"
    "  BTCUSD 15.19/2.11  RR 7.20  n=236  tp_hits=41  sl_hits=181  expired=14  "
    "wr=0.185  expiry=5.9%  EV=+0.921  e1=+1.180  e2=+0.661  -> STRICT BAR SURVIVES\n"
    "  forex  9.75/1.39   RR 7.01  n=1041 tp_hits=162 sl_hits=873  expired=6   "
    "wr=0.157  expiry=0.6%  EV=+0.252  e1=+0.850  e2=-0.346  -> FAILS (best-effort only)\n"
    "Both are the free lattice's max-EV cells after the win-rate filter "
    "(WR_LO/WR_HI 0.40-0.60) was removed from the reopt — that filter was a "
    "candidate FILTER inherited from scripts/vol_atr_solver.py ('same bar as "
    "research'), never the objective; the objective is and was EV. Removing it let "
    "the free lattice select far-TP/tight-SL shapes, which is what these two are. "
    "Drift gate: the reopt only writes clamps when the pick differs >15% from the "
    "shipped band, so the question is whether these two should ever reach the book. "
    "Related: dynamic_atr_vol_ratio_gate, kairos_rank_score (rank ordering, "
    "never gates), entry_timing_conditioning."
)

WORKLOG_REF = (
    "worklog/kairos/reports/20261013_atr_reopt.md "
    "(+ scripts/validate_reopt_picks.py, scripts/run_atr_reopt_once-style report run)"
)

REVIEW_AT = "2026-11-10T00:00:00+00:00"


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
    existing = httpx.get(f"{U}/rest/v1/nt_roadmap", headers=H,
                         params={"select": "mechanism", "mechanism": f"eq.{MECHANISM}"},
                         timeout=20).json()
    if existing:
        r = httpx.patch(f"{U}/rest/v1/nt_roadmap", headers=H,
                        params={"mechanism": f"eq.{MECHANISM}"}, json=payload, timeout=20)
        print(f"PATCH {MECHANISM}: {r.status_code} (row existed)")
    else:
        r = httpx.post(f"{U}/rest/v1/nt_roadmap", headers=H,
                       json={**payload, "set_at": now}, timeout=20)
        print(f"POST {MECHANISM}: {r.status_code} (row created)")

    chk = httpx.get(f"{U}/rest/v1/nt_roadmap", headers=H,
                    params={"select": "mechanism,status,review_at,current_state",
                            "mechanism": f"eq.{MECHANISM}"}, timeout=20).json()
    if not chk:
        print("VERIFY FAILED: row not found after write")
        return 1
    row = chk[0]
    print(f"  status={row['status']}  review_at={row['review_at']}")
    print(f"  current_state: {row['current_state'][:110]}...")
    n = httpx.get(f"{U}/rest/v1/nt_roadmap", headers=H,
                  params={"select": "mechanism", "mechanism": f"eq.{MECHANISM}"},
                  timeout=20).json()
    print(f"  rows with this mechanism: {len(n)} (must be 1)")
    return 0 if len(n) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
