"""Restore heavy geometry on light-sweep rows written while payoff_geometry_mode was live.

Window: 2026-09-26 05:00-05:17 UTC (Studio 6045c92; flag live at the 04:57 restart,
set to off on the Studio at 05:29:58). 13 symbols x 05:00/05:15 = 26 nt_signal_sim
+ 26 nt_sweep_result rows carry the payoff bracket. BTCUSD was never overridden.
All 26 are open (outcome NULL) and none qualified, so no Discord / paper / broadcast
side effects -- only the bracket columns and what is derived from them are wrong.

Method (no model re-run): the heavy geometry did not change across the window
(config/brick_params.yaml mtime 01:26), so the same symbol's 04:45 and 05:30 rows
hold the bracket the 05:00/05:15 ticks would have written. For each symbol:
  * require 04:45 == 05:30 on every geometry column (else stop, nothing written);
  * copy those columns onto the 05:00/05:15 rows;
  * recompute ev / effective_kelly / ev_allowed / kelly_min_ok from the row's own
    p_win with the heavy profit/stake (binary EV, as stored);
  * p_win: keep the row's value when its calibration cell bucket is unchanged,
    else take the neighbour's calibration state (p_win = raw when the neighbour
    was not corrected).
Control: the same recompute applied to the 04:45 and 05:30 rows must reproduce
their stored ev / effective_kelly / failed_gates exactly.

nt_eod_actions: 05:15 geometry_tuning_sweep rows get the heavy tp_distance_atr;
rows the heavy geometry would not have enqueued (neighbour risk below cap) are deleted.

Dry-run by default; --apply writes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, ".")
for line in Path("../../.env.local").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from regime_platform.core.feature_flags import (  # noqa: E402
    get_dynamic_atr_risk_cap, get_min_ev, get_min_kelly, get_min_rr,
)
from regime_platform.core.settings import settings  # noqa: E402
from regime_platform.services.statistics.ev_engine import EVEngine  # noqa: E402

U = settings.SUPABASE_URL.rstrip("/") + "/rest/v1/"
H = {"apikey": settings.SUPABASE_KEY, "Authorization": f"Bearer {settings.SUPABASE_KEY}",
     "Content-Type": "application/json", "Prefer": "return=minimal"}
ENGINE = EVEngine(min_ev=get_min_ev(), min_rr=get_min_rr())

GEOMETRY = ["brick_size", "sl_bricks", "tp_bricks", "stop_loss", "take_profit",
            "tp_distance_atr", "sl_distance_atr", "bracket_bucket", "bracket_vol_tier",
            "p_win_bracket", "p_resolve", "p_win_resolve",
            "geometry_risk_hat", "long_lag_hat", "long_lag_risk"]
DERIVED = ["p_win", "ev", "effective_kelly", "failed_gates"]


def get(table: str, params: dict) -> list[dict]:
    r = httpx.get(U + table, headers=H, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def rows(lo: str, hi: str) -> list[dict]:
    return get("nt_signal_sim", {"select": "*", "source": "eq.sweep-light",
                                 "and": f"(ts.gte.{lo},ts.lt.{hi})", "order": "ts"})


def eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(1e-6, 1e-4 * abs(b))
    return a == b


def recompute(row: dict, geo: dict, ref: dict) -> dict:
    """ev / kelly / gates for ``row`` under geometry ``geo``; ``ref`` = heavy neighbour."""
    raw = float(row["predicted_p_win_raw"])
    p = float(row["p_win"])
    if geo["bracket_bucket"] != row["bracket_bucket"]:        # calibration cell changes
        p = raw if eq(float(ref["p_win"]), float(ref["predicted_p_win_raw"])) else p
    profit = geo["tp_bricks"] * geo["brick_size"]
    stake = geo["sl_bricks"] * geo["brick_size"]
    f_new = ENGINE.recompute_ev_fields(p, profit, stake)
    f_old = ENGINE.recompute_ev_fields(float(row["p_win"]), row["tp_bricks"] * row["brick_size"],
                                       row["sl_bricks"] * row["brick_size"])
    ek_old = float(row["effective_kelly"])
    if f_old["ev_scale"] > 0:
        ek = ek_old / f_old["ev_scale"] * f_new["ev_scale"]
    else:
        ek = float(row["kelly_f"]) * f_new["ev_scale"]
    failed = set(filter(None, (row.get("failed_gates") or "").split(","))) - {"ev_allowed", "kelly_min_ok"}
    if not f_new["allowed"]:
        failed.add("ev_allowed")
    if ek < get_min_kelly():
        failed.add("kelly_min_ok")
    return {"p_win": round(p, 4), "ev": round(f_new["ev"], 4), "effective_kelly": round(ek, 6),
            "failed_gates": ",".join(sorted(failed)) or None, "_aggression": f_new["aggression"]}


def main(apply: bool) -> None:
    by: dict[str, list[dict]] = {}
    for r in rows("2026-09-26T04:44:00Z", "2026-09-26T05:32:00Z"):
        by.setdefault(r["symbol"], []).append(r)
    plan, problems = [], []
    for sym, rs in sorted(by.items()):
        if sym == "BTCUSD":
            continue
        pre = [r for r in rs if r["ts"] < "2026-09-26T04:50"]
        post = [r for r in rs if r["ts"] >= "2026-09-26T05:29"]
        bad = [r for r in rs if "2026-09-26T05:00" <= r["ts"] < "2026-09-26T05:17"]
        if len(pre) != 1 or len(post) != 1 or len(bad) != 2:
            problems.append(f"{sym}: rows pre/post/bad = {len(pre)}/{len(post)}/{len(bad)}")
            continue
        a, b = pre[0], post[0]
        drift = [k for k in GEOMETRY if not eq(a[k], b[k])]
        if drift:
            problems.append(f"{sym}: 04:45 vs 05:30 geometry differs on {drift}")
            continue
        for c in (a, b):   # control: recompute with own geometry reproduces the stored row
            rec = recompute(c, {k: c[k] for k in GEOMETRY}, c)
            # ev tolerance 0.2%: stored p_win is rounded to 4dp, ev was computed unrounded
            miss = {k: (c[k], rec[k]) for k in DERIVED
                    if not (abs(rec[k] - c[k]) <= 2e-3 * abs(c[k]) if k == "ev" else eq(rec[k], c[k]))}
            if miss:
                problems.append(f"{sym} control {c['ts'][11:16]}: {miss}")
        geo = {k: a[k] for k in GEOMETRY}
        for r in bad:
            if r["outcome"] is not None or r["gate_outcome"] not in ("gate_fail", "suppressed"):
                problems.append(f"{sym} {r['ts'][11:16]}: outcome={r['outcome']} gate={r['gate_outcome']}")
                continue
            rec = recompute(r, geo, a)
            if r["gate_outcome"] == "suppressed" and rec["failed_gates"]:
                problems.append(f"{sym} {r['ts'][11:16]}: suppressed row would gate-fail {rec['failed_gates']}")
            if r["gate_outcome"] == "gate_fail" and not rec["failed_gates"]:
                problems.append(f"{sym} {r['ts'][11:16]}: gate_fail row would pass")
            plan.append((r, {**geo, **{k: rec[k] for k in DERIVED}}, rec["_aggression"]))
            print(f"{r['ts'][11:16]} {sym:8} tp/sl {r['tp_bricks']}/{r['sl_bricks']}->{geo['tp_bricks']}/{geo['sl_bricks']}"
                  f"  tp_atr {r['tp_distance_atr']}->{geo['tp_distance_atr']}  ev {r['ev']}->{rec['ev']}"
                  f"  ek {r['effective_kelly']}->{rec['effective_kelly']}  p {r['p_win']}->{rec['p_win']}"
                  f"  gates {r['failed_gates']}->{rec['failed_gates']}")

    acts = get("nt_eod_actions", {"select": "action_id,symbol,ts,detail", "action": "eq.geometry_tuning_sweep",
                                  "and": "(ts.gte.2026-09-26T05:15:00Z,ts.lt.2026-09-26T05:17:00Z)"})
    cap = get_dynamic_atr_risk_cap()
    act_plan = []
    for x in acts:
        if x["symbol"] == "BTCUSD":
            continue
        a = by[x["symbol"]][0]
        risk = a.get("geometry_risk_hat")
        if risk is not None and float(risk) > cap:
            act_plan.append(("patch", x, {**x["detail"], "tp_distance_atr": a["tp_distance_atr"],
                                          "geometry_risk_hat": risk, "long_lag_hat": a["long_lag_hat"],
                                          "bucket": a["bracket_bucket"]}))
        else:
            act_plan.append(("delete", x, None))
        print(f"eod_action {act_plan[-1][0]:6} {x['symbol']:8} heavy risk={risk} cap={cap}")

    print(f"\nplan: {len(plan)} nt_signal_sim + {len(plan)} nt_sweep_result rows, {len(act_plan)} nt_eod_actions")
    if problems:
        print("PROBLEMS:\n  " + "\n  ".join(problems))
    if not apply:
        print("dry-run: nothing written")
        return
    if problems:
        print("REFUSING to apply")
        return
    with httpx.Client(headers=H, timeout=60) as c:
        for r, new, aggr in plan:
            c.patch(U + "nt_signal_sim", params={"signal_id": f"eq.{r['signal_id']}"}, json=new).raise_for_status()
            sr = c.get(U + "nt_sweep_result", params={
                "select": "id", "symbol": f"eq.{r['symbol']}", "source": "eq.sweep-light",
                "and": f"(sweep_timestamp.gte.{r['ts'][:16]}:00Z,sweep_timestamp.lt.{r['ts'][:15]}9:59Z)"}).json()
            if len(sr) != 1:
                print("nt_sweep_result not unique", r["symbol"], r["ts"], sr)
                continue
            c.patch(U + "nt_sweep_result", params={"id": f"eq.{sr[0]['id']}"}, json={
                "brick_size": new["brick_size"], "sl_bricks": new["sl_bricks"], "tp_bricks": new["tp_bricks"],
                "stop_loss": new["stop_loss"], "take_profit": new["take_profit"], "ev": new["ev"],
                "p_win": new["p_win"], "effective_kelly": new["effective_kelly"], "aggression": aggr,
            }).raise_for_status()
        for op, x, d in act_plan:
            if op == "delete":
                c.delete(U + "nt_eod_actions", params={"action_id": f"eq.{x['action_id']}"}).raise_for_status()
            else:
                c.patch(U + "nt_eod_actions", params={"action_id": f"eq.{x['action_id']}"},
                        json={"detail": d}).raise_for_status()
    print("applied")


if __name__ == "__main__":
    main("--apply" in sys.argv)
