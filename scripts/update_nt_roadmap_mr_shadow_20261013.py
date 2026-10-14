"""update_nt_roadmap_mr_shadow_20261013.py — record the 2026-10-13 MR shadow-mode
measurement on the `mean_reversion_gold_quant` roadmap row.

Idempotent: PATCHes notes + current_state, always the same text (no appends
that compound). Run from the project venv:

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_mr_shadow_20261013.py

Findings recorded (all measured 2026-10-13 ~20:15 UTC, scripts/_tmp_replay.py +
scripts/kairos_mr_cohort_walkforward.py):

  (1) B1 false-cohort walk-forward, era-clean (post-2026-10-02 cutover,
      contaminated=false, tp/sl only), split 2026-10-07T14:46Z:
        XAUUSD buy  n_false=34  WR_false 0.382  WR_pass 0.065  ratio 5.86x
                    held-out ratio 7.32x but n_false_h2=34 < 40 -> FAIL ON SAMPLE
        XAGUSD buy  n_false=155 WR_false 0.155  WR_pass 0.247  ratio 0.63x REVERSED
        XAUAUD/XAUEUR n_false=0  NO_DATA (gate never opposes those cohorts)
      Last new false-cohort stamp: 2026-10-09 01:34Z — 4 days, zero accumulation.

  (2) ROUTER REPLAY over all 225 last-12h resolved rows (route() called with the
      row's own macro_label/trend_label/regime): routed 0/225. Two independent
      blockers, both confirmed:
        (a) ASSET-CLASS KEY MISMATCH. nt_symbol.asset_class for the golds is
            'commodities' -> asset_class_key() -> 'commodity', but the registry's
            mean_reversion block gates asset_classes: [metal]. So the gold MR
            archetype can NEVER match a gold row. momentum's list
            [crypto, metal, equity, index, etf] also excludes 'commodity', so
            metals route to neither. Counterfactual B (commodity->metal): still
            0 routed, because blocker (b) dominates.
        (b) TRI-STATE ALIGNMENT IS NEVER 'opposes'/'aligned' IN THE CURRENT
            REGIME. In the last 3 days there are ZERO rows with both macro
            directional AND trend directional (last such row 2026-10-09 06:00Z;
            trend reads 'consolidating', macro 'neutral'/'bear'). So
            _alignment() returns None -> stand_by for 100% of signals. This is
            commit 434a775's correct-by-design behaviour: a consolidating trend
            is UNKNOWN and must claim neither archetype. Counterfactual C
            (neutral-tolerating alignment): 19/225 routed (BTCUSD buy momentum
            13 @ 0% tp, AAPL buy momentum 6 @ 0% tp). Counterfactual D (B+C):
            63/225 routed but EVERY routed cell has tp <= 14.3% and avgR <= 0
            (XAGUSD sell momentum 13.3%, XAUUSD sell momentum 14.3%,
            XAGUSD/XAUAUD buy mean_reversion 0.0%) — i.e. unblocking the router
            routes the WORST cells in the book.
        (c) The registry's mean_reversion regime_vol: [low_vol, med_low_vol]
            contradicts its own evidence: era-clean false-cohort WR by vol tier
            is med_high_vol 52.9% (n=17) > low_vol 27.3% (n=11) > high_vol 16.7%
            (n=6). The allow-list excludes the best cell.
        (d) 4 legacy 'momentum' stamps exist (BTCUSD buy, 2026-10-10/11, all
            sl_hit) from BEFORE commit 434a775 landed 2026-10-11 13:50 PDT —
            they are pre-tri-state-fix artifacts, not current-router behaviour.

  VERDICT: HOLD. Do not flip. The effect is real on the stamped cohort (5.86x)
  but (i) the router cannot reach it, (ii) unblocking the router routes
  negative-expectancy cells, (iii) the archetype's own vol gate excludes its
  best evidence. Bar unchanged: era-clean XAUUSD-buy false-cohort n>=40 AND
  held-out ratio>=1.5x. Currently n=34 (all in H2, H1 n=0) with a degenerate
  split — needs 6 more false-cohort rows, and zero have arrived in 4 days.
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

MECHANISM = "mean_reversion_gold_quant"
REVIEW_AT = "2026-10-17T00:00:00+00:00"

CURRENT_STATE = (
    "HOLD 2026-10-13 (router replay + B1 re-run): the MR edge is real on the stamped "
    "cohort (XAUUSD buy false-cohort WR 38.2% vs pass 6.5%, 5.86x; held-out 7.32x) but "
    "the router CANNOT reach it and unblocking it routes negative-expectancy cells. "
    "Replay of all 225 last-12h resolved rows: routed 0/225, stand_by 100%. Two "
    "independent blockers: (a) asset-class key mismatch — nt_symbol.asset_class "
    "'commodities' -> asset_class_key() 'commodity', but registry gates "
    "asset_classes: [metal], so a gold row can never match the gold archetype; "
    "(b) tri-state alignment is never 'opposes' in the current regime — zero rows in "
    "3 days with both macro AND trend directional (last 2026-10-09 06:00Z; trend "
    "reads 'consolidating'), so _alignment()=None -> stand_by (commit 434a775, "
    "correct by design). Counterfactual D (both blockers removed): 63/225 routed but "
    "every routed cell tp<=14.3%, avgR<=0. Registry's regime_vol [low_vol, "
    "med_low_vol] also contradicts its evidence: era-clean false-cohort WR by vol "
    "tier is med_high_vol 52.9% (n=17) > low_vol 27.3% (n=11) > high_vol 16.7% (n=6). "
    "Bar unchanged: era-clean XAUUSD-buy false-cohort n>=40 AND held-out ratio>=1.5x. "
    "Currently n=34 (all in H2, H1 n=0 — degenerate split), zero new false-cohort "
    "rows in 4 days (last stamp 2026-10-09 01:34Z)."
)

NOTES = (
    "B1 WALK-FORWARD RUN 2026-10-13 (scripts/kairos_mr_cohort_walkforward.py, era-clean "
    "post-2026-10-02 cutover, contaminated=false, tp/sl only, split 2026-10-07T14:46Z): "
    "(1) FOREX COHORT = NO_DATA. The context_alignment gate NEVER opposes any forex "
    "signal (0 false-cohort rows across ~3,300 era-clean resolved signals, all 6 "
    "majors). The ID45 inversion is NOT a forex phenomenon - the engine only generates "
    "forex signals aligned with the regime/trend read, so macro+trend never both "
    "oppose. There is no FX mean-reversion cohort to validate on this platform. "
    "(2) XAUUSD remains the ONLY true inversion: buys, WR_false 38.2% (13/34) vs "
    "WR_pass 6.5% (15/230), 5.86x; held-out half 7.32x. n=34 < the 40 bar AND all 34 "
    "are post-split, so the walk-forward split is degenerate (H1 false n=0) -> B1 "
    "FAILS ON SAMPLE, not on effect. (3) XAGUSD (0.63x, n_false=155) is REVERSED - "
    "the gate is correct there (trend-continues). (4) XAUAUD/XAUEUR n_false=0 -> "
    "NO_DATA. (5) Pooling commodities is a Simpson's-paradox trap - evaluate "
    "PER-SYMBOL only, never pooled. (6) Last new false-cohort stamp 2026-10-09 "
    "01:34Z - 4 days, zero accumulation. "
    "ROUTER REPLAY 2026-10-13 (route() called on each of the 225 last-12h resolved "
    "rows with its own macro_label/trend_label/regime): routed 0/225, stand_by 100%. "
    "Two independent blockers, both confirmed by counterfactual: (a) ASSET-CLASS KEY "
    "MISMATCH — nt_symbol.asset_class='commodities' for the golds -> asset_class_key() "
    "-> 'commodity', but the registry's mean_reversion block gates asset_classes: "
    "[metal]; momentum's [crypto, metal, equity, index, etf] also excludes "
    "'commodity', so metals match neither enabled archetype. Counterfactual B "
    "(commodity->metal) alone: still 0 routed. (b) TRI-STATE ALIGNMENT NEVER FIRES — "
    "zero rows in the last 3 days with both macro directional AND trend directional "
    "(last such row 2026-10-09 06:00Z; trend reads 'consolidating', macro "
    "'neutral'/'bear'), so _alignment() returns None -> stand_by (commit 434a775's "
    "tri-state fix, correct by design: a consolidating trend is UNKNOWN). "
    "Counterfactual C (neutral-tolerating alignment): 19/225 routed — BTCUSD buy "
    "momentum 13 @ 0.0% tp, AAPL buy momentum 6 @ 0.0% tp. Counterfactual D (B+C "
    "combined): 63/225 routed, but EVERY routed cell is tp<=14.3% and avgR<=0 "
    "(XAGUSD sell momentum 13.3% / -0.556R, XAUUSD sell momentum 14.3% / -0.143R, "
    "XAGUSD buy mean_reversion 0.0% / -1.000R, XAUAUD buy mean_reversion 0.0% / "
    "-1.000R) — i.e. unblocking the router routes the WORST cells in the book. "
    "(c) The registry's mean_reversion regime_vol: [low_vol, med_low_vol] contradicts "
    "its own evidence: era-clean false-cohort WR by vol tier is med_high_vol 52.9% "
    "(n=17) > low_vol 27.3% (n=11) > high_vol 16.7% (n=6) — the allow-list excludes "
    "the best cell. (d) The 4 legacy 'momentum' stamps (BTCUSD buy, 2026-10-10/11, all "
    "sl_hit) predate commit 434a775 (2026-10-11 13:50 PDT) — pre-tri-state-fix "
    "artifacts, not current-router behaviour. VERDICT: HOLD, do not flip. Bar "
    "unchanged: era-clean XAUUSD-buy false-cohort n>=40 AND held-out ratio>=1.5x."
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

    r2 = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={
            "select": "mechanism,status,review_at,current_state,notes",
            "mechanism": f"eq.{MECHANISM}",
        },
        timeout=20,
    )
    rows = r2.json()
    if not rows:
        print("NO ROW FOUND — mechanism name mismatch?")
        return 1
    row = rows[0]
    print(f"  status       : {row['status']}")
    print(f"  review_at    : {row['review_at']}")
    print(f"  updated_at   : {row.get('updated_at')}")
    print(f"  notes chars  : {len(row['notes'] or '')}")
    print(f"  state chars  : {len(row['current_state'] or '')}")
    print(f"  notes head   : {(row['notes'] or '')[:100]}")

    r3 = httpx.get(
        f"{U}/rest/v1/nt_roadmap",
        headers=H,
        params={"select": "count", "mechanism": f"eq.{MECHANISM}"},
        timeout=20,
    )
    print(f"  row count    : {r3.json()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
