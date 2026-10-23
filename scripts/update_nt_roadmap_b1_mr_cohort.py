"""
update_nt_roadmap_b1_mr_cohort.py — record the B1 walk-forward result on the
`mean_reversion_gold_quant` roadmap row.

Idempotent: re-running PATCHes the same notes text (no duplicate rows, no
appends that compound). Run from the project venv.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_b1_mr_cohort.py

Findings recorded (see scripts/kairos_mr_cohort_walkforward.py for the runner):
  - FOREX COHORT = NO_DATA — the context_alignment gate never opposes any forex
    signal (0 false-cohort rows across ~3,300 era-clean resolved signals).
  - XAUUSD is the only true inversion: buys, WR_false 38.2% (13/34) vs pass 7.7%,
    5.0x. n=34 < the 40 bar and all post-Oct-07 → degenerate walk-forward → FAIL
    on sample size, not on effect.
  - XAGUSD (0.65x) and BTCUSD (0.44x) are REVERSED — the gate is correct there.
  - Pooling commodities is a Simpson's-paradox trap (1.52x pooled).
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

MECHANISM = "mean_reversion_gold_quant"

NOTES = (
    "B1 WALK-FORWARD RUN 2026-10-12 (scripts/kairos_mr_cohort_walkforward.py): "
    "(1) FOREX COHORT = NO_DATA. The context_alignment gate NEVER opposes any forex signal "
    "(0 false-cohort rows across ~3,300 era-clean resolved signals, all 6 majors). The ID45 inversion "
    "is NOT a forex phenomenon - the engine only generates forex signals aligned with the regime/trend "
    "read, so macro+trend never both oppose. There is no FX mean-reversion cohort to validate on this "
    "platform. (2) XAUUSD remains the ONLY true inversion: buys, WR_false 38.2% (13/34) vs WR_pass 7.7%, "
    "5.0x. n=34 < the 40 bar AND all 34 are post-Oct-07, so the walk-forward split is degenerate (no "
    "in-sample half) -> B1 FAILS ON SAMPLE, not on effect. (3) XAGUSD (0.65x) and BTCUSD (0.44x) are "
    "REVERSED - the gate is correct there (trend-continues). (4) Pooling commodities is a Simpson-paradox "
    "trap (1.52x pooled vs XAUUSD 2.92x + XAGUSD 0.65x) - evaluate PER-SYMBOL only, never pooled. "
    "Re-run when the XAUUSD false-cohort reaches n>=40."
)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    r = httpx.patch(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"mechanism": f"eq.{MECHANISM}"},
        json={"notes": NOTES, "updated_at": now},
        timeout=20,
    )
    print(f"PATCH {MECHANISM}: {r.status_code}")

    r2 = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"select": "mechanism,notes,updated_at", "mechanism": f"eq.{MECHANISM}"},
        timeout=20,
    )
    row = r2.json()[0]
    print(f"updated_at: {row['updated_at']}")
    print(f"notes length: {len(row['notes'] or '')}")
    print(f"notes head: {(row['notes'] or '')[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
