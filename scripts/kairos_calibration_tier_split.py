"""Kairos V3 — evidence-tier split for the hourly calibration lever (§14.2).

Question: the hourly resolver mostly speeds up gate-failed evidence (≈80%
of hourly TP/SL). Does evidence from each tier help correct S2 (next
qualified TP/SL signal, same symbol), or is gate-failed evidence noise?

Same data, targets, walk-forward correction and evidence rules as
scripts/kairos_calibration_step2.py §A; the only change is which evidence
rows each rule may use:
  all            — every tier (what production reads today)
  q+s            — qualified + suppressed (passed every gate)
  gate_fail      — failed ≥ 1 gate
Tier = stamped gate_outcome (migration 168) or, for older rows, the
validated reconstruction from scripts/kairos_weekly_check.py
(reconstruct_tier, agreement 1.000 on 910 stamped rows).

Run:  PYTHONPATH="$PWD" python -u scripts/kairos_calibration_tier_split.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


s2 = _load("ks2", "kairos_calibration_step2.py")
cp = s2.cp
wk = _load("kwc", "kairos_weekly_check.py")

EXTRA = "gate_outcome,regime_conf,effective_kelly,ev,p_win_dir,tp_bricks,sl_bricks"


def fetch() -> pd.DataFrame:
    import httpx
    from dotenv import load_dotenv

    for p in (ROOT / ".env.local", ROOT.parents[1] / ".env.local"):
        if p.exists():
            load_dotenv(p)
    from regime_platform.core.settings import Settings
    st = Settings()
    u, k = st.SUPABASE_URL.rstrip("/"), st.SUPABASE_KEY
    h = {"apikey": k, "Authorization": f"Bearer {k}"}
    cols = ("signal_id,symbol,signal,ts,exit_ts,outcome,qualified,p_win,predicted_p_win_raw,"
            "entry_price,stop_loss,take_profit,regime," + EXTRA)
    rows, off = [], 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{u}/rest/v1/nt_signal_sim", headers=h, params={
                "select": cols, "source": "eq.sweep-light", "signal": "in.(buy,sell)",
                "outcome": "in.(tp_hit,sl_hit,expired)", "order": "ts.asc",
                "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows += b
            off += len(b)
            if len(b) < 1000:
                break
    df = pd.DataFrame(rows)
    for c_ in ("p_win", "predicted_p_win_raw", "entry_price", "stop_loss", "take_profit",
               "regime_conf", "effective_kelly", "ev", "p_win_dir", "tp_bricks", "sl_bricks"):
        df[c_] = pd.to_numeric(df[c_], errors="coerce")
    df["ts"] = pd.to_datetime(df.ts, utc=True, format="ISO8601")
    df["exit_ts"] = pd.to_datetime(df.exit_ts, utc=True, format="ISO8601", errors="coerce")
    return df


def add_tier(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    stamped = d.gate_outcome.isin(["qualified", "suppressed", "gate_fail"])
    recon = wk.reconstruct_tier(d)
    d["tier"] = np.where(stamped, d.gate_outcome, recon)
    d["tier_src"] = np.where(stamped, "stamp", "recon")
    return d


def evidence(d: pd.DataFrame, tiers: set[str] | None) -> dict:
    """Per-symbol evidence arrays restricted to ``tiers`` (None = all)."""
    E = {}
    for sym, g in d.groupby("symbol"):
        ev = g[g.resolved & (g.tier.isin(tiers) if tiers else True)]
        ext = s2._hours(ev.exit_ts)
        E[sym] = {"entry": s2._hours(ev.ts), "exit": ext, "err": ev.err.to_numpy(float),
                  "known_nightly": s2.next_resolver_run(ext)}
    return E


def main() -> int:
    t0 = time.time()
    d = add_tier(cp.prepare(fetch()))
    T, _ = s2.build(d)  # targets unchanged: qualified TP/SL (S2)
    groups = {"all tiers (production today)": None, "qualified + suppressed": {"qualified", "suppressed"},
              "gate_fail only": {"gate_fail"}}
    L: list[str] = []
    P = L.append
    res = d[d.resolved]
    P("# Kairos V3: evidence-tier split for the hourly calibration lever\n")
    P(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/kairos_calibration_tier_split.py`. "
      f"Targets (S2): {len(T)} qualified TP/SL signals, {T.symbol.nunique()} symbols, "
      f"{T.ts.min():%Y-%m-%d} → {T.ts.max():%Y-%m-%d}.\n")
    P("## Evidence pool (resolved TP/SL, light sweep)\n")
    P("| tier | n | share | TP rate | mean err (p_win − y) | tier source stamped / reconstructed |")
    P("|---|---|---|---|---|---|")
    for t_ in ("qualified", "suppressed", "gate_fail"):
        g = res[res.tier == t_]
        P(f"| {t_} | {len(g)} | {len(g) / len(res):.1%} | {g.y.mean():.3f} | {g.err.mean():+.3f} | "
          f"{(g.tier_src == 'stamp').sum()} / {(g.tier_src == 'recon').sum()} |")
    P("")

    rules = {}
    for gname, tiers in groups.items():
        E = evidence(d, tiers)
        rules[(gname, "production rule, nightly")] = s2.x_production(T, E, "nightly")
        rules[(gname, "production rule, hourly")] = s2.x_production(T, E, "exit+1h")
        rules[(gname, "3h half-life, hourly")] = s2.x_signal_decay(T, E, 3.0, "exit+1h")

    ok = s2.common_subset(T, rules)
    Tc = T[ok].reset_index(drop=True)
    P("## Walk-forward Δ Brier on S2 (negative = better)\n")
    P("Own = every S2 the rule can score (coverage differs by pool size). Common = the S2 targets every "
      f"rule can score ({int(ok.sum())}), the fair comparison. corr = correlation of the evidence mean "
      "error with S2's error (higher = more informative).\n")
    P("| evidence pool | rule | S2 scorable (own) | Δ Brier own ± SE | Δ Brier common ± SE | corr(x, S2 err) |")
    P("|---|---|---|---|---|---|")
    out = {}
    for (gname, rname), x in rules.items():
        own = s2.walk_forward(T, x)
        com = s2.walk_forward(Tc, x[ok])
        r_ = cp.ols_beta(x, T.target)[1]
        out[f"{gname} | {rname}"] = {"own": own, "common": com, "r": r_}
        P(f"| {gname} | {rname} | {own.get('n', 0)} ({own.get('coverage', 0):.0%}) | "
          f"{own.get('d', np.nan):+.4f} ± {own.get('se', np.nan):.4f} | "
          f"{com.get('d', np.nan):+.4f} ± {com.get('se', np.nan):.4f} | {r_:+.3f} |")
    P(f"\nUncorrected Brier on the common subset: "
      f"{next(iter(out.values()))['common'].get('brier0', np.nan):.4f}.\n")
    P(f"_Runtime {time.time() - t0:.0f}s._")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = ROOT / "worklog" / "kairos" / "reports" / f"{stamp}_calibration_tier_split"
    path.with_suffix(".md").write_text("\n".join(L) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(out, default=float, indent=1), encoding="utf-8")
    print("\n".join(L))
    print(f"\nreport: {path}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
