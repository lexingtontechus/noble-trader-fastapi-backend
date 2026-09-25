"""Kairos V3 step 2 (+ production benchmark) — V3 scope §14.

Builds on scripts/kairos_calibration_persistence.py (step 1): same data,
targets (S2 = next qualified resolved signal per symbol), error definition
(err = p_win − 1{TP first}, + = overconfident) and circular-shift null.

A. Production benchmark and evidence latency
   The live correction (`fetch_calibration_bias`, supabase_client.py) is NOT a
   flat 7-day mean: records in the last EOD_BIAS_WINDOW_DAYS (7) are merged
   into episodes (overlapping [entered_at, closed_at]), each episode is
   weighted 0.5**(age_days / EOD_BIAS_DECAY_HALFLIFE_DAYS = 3) by ENTRY age,
   with ≥ EOD_BIAS_MIN_RECORDS raw rows (Studio 28) and ≥ 2 episodes.
   Records only exist once the resolver has run. It runs about once a day
   (01:00 UTC), so evidence reaches production a median ~11h after the
   outcome. Step 1 assumed evidence is known at exit_ts (instant).
   Rules scored walk-forward on S2 (same β̂-shrink correction as step 1):
     • production: episode + 3d HL + 7d + ≥28 raw + ≥2 ep, known at next 01:00 UTC
     • production rule, known at exit + 1h   (hourly resolver)
     • signal-level 3h HL,  known at next 01:00 UTC
     • signal-level 3h HL,  known at exit + 1h
     • signal-level 3h HL,  known at exit    (step 1, upper bound)
   This separates the two levers: latency vs half-life.

B. Step 2: does carryover survive a state change?
   State = regime label (vol tier × trend × strength, `regime`). For each S2,
   the 0–6h evidence (known at exit) is split three ways and its carryover β
   measured against the null:
     (a) evidence FIRED from the same regime as S2 vs a different one;
     (b) same signal direction as S2 vs opposite (step 1 found same > opp);
     (c) the symbol's regime AT THE EVIDENCE'S RESOLUTION (last signal at or
         before exit_ts) equal to S2's regime vs changed since.
   Also the decayed (h = 3h) version of (a) and (c), and the difference
   β_same − β_diff with its null distribution.

Run:  PYTHONPATH="$PWD" python scripts/kairos_calibration_step2.py [--n-null 300]
Writes worklog/kairos/reports/YYYYMMDD_calibration_step2.{md,json}.
"""

from __future__ import annotations

import argparse
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
_spec = importlib.util.spec_from_file_location("kcp", ROOT / "scripts" / "kairos_calibration_persistence.py")
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

RNG = np.random.default_rng(92602)
H = 3.6e12  # ns per hour
PROD_WINDOW_D, PROD_HL_D, PROD_MIN_RAW, PROD_MIN_EP = 7, 3.0, 28, 2
RESOLVER_HOUR_UTC = 1


def _hours(ts: pd.Series) -> np.ndarray:
    return ts.to_numpy(dtype="datetime64[ns]").astype(np.int64) / H


def next_resolver_run(t_h: np.ndarray) -> np.ndarray:
    """Hours-since-epoch of the first daily resolver run (01:00 UTC) after t."""
    day = np.floor(t_h / 24.0) * 24.0
    run = day + RESOLVER_HOUR_UTC
    return np.where(run > t_h, run, run + 24.0)


def build(d: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Target rows + per-symbol evidence arrays (resolved signals, any tier)."""
    ev_by_sym, targets = {}, []
    for sym, g in d.groupby("symbol"):
        g = g.sort_values("ts")
        allt = _hours(g.ts)
        allreg = g.regime.fillna("?").to_numpy()
        ev = g[g.resolved].copy()
        ent = _hours(ev.ts)
        ext = _hours(ev.exit_ts)
        # symbol regime at evidence resolution = last signal at/before exit
        j = np.searchsorted(allt, ext, side="right") - 1
        reg_at_exit = np.where(j >= 0, allreg[np.clip(j, 0, None)], "?")
        ev_by_sym[sym] = {
            "entry": ent, "exit": ext, "err": ev.err.to_numpy(float),
            "regime": ev.regime.fillna("?").to_numpy(), "dir": ev.signal.to_numpy(),
            "reg_at_exit": reg_at_exit, "known_nightly": next_resolver_run(ext),
        }
        for r in g[g.qualified.astype(bool) & g.resolved].itertuples():
            targets.append({"symbol": sym, "ts": r.ts, "t": r.ts.value / H, "day": r.ts.floor("D"),
                            "target": r.err, "p_win": r.p_win, "y": r.y,
                            "regime": r.regime or "?", "dir": r.signal})
    return pd.DataFrame(targets).sort_values("ts").reset_index(drop=True), ev_by_sym


# ─── A. evidence rules ───────────────────────────────────────────────────


def x_signal_decay(T, E, hl_h, latency):
    out = np.full(len(T), np.nan)
    for i, r in enumerate(T.itertuples()):
        e = E[r.symbol]
        known = {"exit": e["exit"], "exit+1h": e["exit"] + 1.0, "nightly": e["known_nightly"]}[latency]
        m = known < r.t
        if m.sum() < cp.MIN_EVID:
            continue
        age = r.t - e["exit"][m]
        w = np.power(2.0, -age / hl_h)
        out[i] = float(np.sum(w * e["err"][m]) / np.sum(w))
    return out


def x_production(T, E, latency):
    """Replicates fetch_calibration_bias: 7d entry window, episodes by
    overlapping [entry, exit], episode weight 0.5**(entry_age_d/3)."""
    out = np.full(len(T), np.nan)
    for i, r in enumerate(T.itertuples()):
        e = E[r.symbol]
        known = e["known_nightly"] if latency == "nightly" else e["exit"] + 1.0
        m = (known < r.t) & (e["entry"] >= r.t - PROD_WINDOW_D * 24)
        if m.sum() < PROD_MIN_RAW:
            continue
        ent, ext, err = e["entry"][m], e["exit"][m], e["err"][m]
        o = np.argsort(ent)
        ent, ext, err = ent[o], ext[o], err[o]
        ep_id = np.zeros(len(ent), int)
        run_max = ext[0]
        for k in range(1, len(ent)):
            ep_id[k] = ep_id[k - 1] + (1 if ent[k] > run_max else 0)
            run_max = max(run_max, ext[k])
        n_ep = ep_id[-1] + 1
        if n_ep < PROD_MIN_EP:
            continue
        ep_err = np.bincount(ep_id, weights=err) / np.bincount(ep_id)
        ep_ent = np.array([ent[ep_id == q].min() for q in range(n_ep)])
        w = np.power(0.5, ((r.t - ep_ent) / 24.0) / PROD_HL_D)
        out[i] = float(np.sum(w * ep_err) / np.sum(w))
    return out


def walk_forward(T, x):
    """Same correction as step 1: p' = clip(p − β̂x), β̂ ∈ [0,1] from earlier targets."""
    p0, y = T.p_win.to_numpy(float), T.y.to_numpy(float)
    e = p0 - y
    b0, b1, used = [], [], 0
    for i in range(len(T)):
        ok = np.isfinite(x[:i])
        if ok.sum() < 30 or not np.isfinite(x[i]):
            continue
        xp, ep = x[:i][ok], e[:i][ok]
        b = np.cov(xp, ep, bias=True)[0, 1] / np.var(xp) if np.var(xp) > 0 else 0.0
        b = float(np.clip(b, 0.0, 1.0))
        pp = float(np.clip(p0[i], .01, .99))
        p1 = float(np.clip(p0[i] - b * x[i], .01, .99))
        b0.append((pp - y[i]) ** 2)
        b1.append((p1 - y[i]) ** 2)
        used += 1
    if not used:
        return {"n": 0}
    dd = np.array(b1) - np.array(b0)
    return {"n": used, "coverage": used / len(T), "brier0": float(np.mean(b0)), "brier1": float(np.mean(b1)),
            "d": float(dd.mean()), "se": float(dd.std(ddof=1) / np.sqrt(used)) if used > 1 else np.nan}


def common_subset(T, xs):
    """Score every rule on targets all rules can score (fair comparison)."""
    ok = np.all([np.isfinite(x) for x in xs.values()], axis=0)
    return ok


# ─── B. state split ──────────────────────────────────────────────────────


def x_split(T, E, key, lo=0.0, hi=6.0, hl=None):
    """Mean 0–6h evidence error (known at exit), split same / different
    state. key ∈ {regime, dir, reg_at_exit}. Returns (x_same, x_diff)."""
    xs, xd = np.full(len(T), np.nan), np.full(len(T), np.nan)
    for i, r in enumerate(T.itertuples()):
        e = E[r.symbol]
        age = r.t - e["exit"]
        m = (e["exit"] < r.t) & (age >= lo) & (age < hi)
        tgt_state = r.dir if key == "dir" else r.regime
        same = m & (e[key] == tgt_state)
        diff = m & (e[key] != tgt_state)
        for msk, arr in ((same, xs), (diff, xd)):
            if msk.sum() >= cp.MIN_EVID:
                if hl is None:
                    arr[i] = float(e["err"][msk].mean())
                else:
                    w = np.power(2.0, -age[msk] / hl)
                    arr[i] = float(np.sum(w * e["err"][msk]) / np.sum(w))
    return xs, xd


def beta(x, y):
    return cp.ols_beta(x, y)


def shifted(T):
    parts = []
    for _, g in T.groupby("symbol"):
        g = g.copy()
        if len(g) > 2:
            o = int(RNG.integers(1, len(g)))
            for c in ("target", "p_win", "y"):
                g[c] = np.roll(g[c].to_numpy(), o)
        parts.append(g)
    return pd.concat(parts).loc[T.index]


# ─── main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-null", type=int, default=300)
    a = ap.parse_args()
    t0 = time.time()
    d = cp.prepare(cp.fetch())
    T, E = build(d)
    L: list[str] = []
    P = L.append
    P("# Kairos V3 step 2 + production benchmark: calibration carryover\n")
    P(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/kairos_calibration_step2.py` (scope §14). "
      f"Targets: {len(T)} qualified TP/SL signals (S2), {T.symbol.nunique()} symbols, "
      f"{T.ts.min():%Y-%m-%d} → {T.ts.max():%Y-%m-%d}. Evidence: same symbol, any tier, TP/SL.\n")

    # A
    rules = {
        "production rule, nightly resolver (today)": x_production(T, E, "nightly"),
        "production rule, hourly resolver": x_production(T, E, "exit+1h"),
        "3h half-life, nightly resolver": x_signal_decay(T, E, 3.0, "nightly"),
        "3h half-life, hourly resolver": x_signal_decay(T, E, 3.0, "exit+1h"),
        "3h half-life, instant (step 1 bound)": x_signal_decay(T, E, 3.0, "exit"),
        "24h half-life, hourly resolver": x_signal_decay(T, E, 24.0, "exit+1h"),
    }
    P("## A. Production benchmark vs latency vs half-life (walk-forward Δ Brier on S2; negative = better)\n")
    P("Production rule = `fetch_calibration_bias`: 7d entry window, episode merge, 3-day half-life on entry age, "
      "≥ 28 raw records, ≥ 2 episodes. Nightly = evidence usable only after the next 01:00 UTC resolver run.\n")
    P("| rule | S2 scorable (own) | Δ Brier own ± SE | Δ Brier on common subset ± SE | corr(x, S2 err) |")
    P("|---|---|---|---|---|")
    ok = common_subset(T, rules)
    Tc = T[ok].reset_index(drop=True)
    resA = {}
    for name, x in rules.items():
        own = walk_forward(T, x)
        com = walk_forward(Tc, x[ok])
        r_ = cp.ols_beta(x, T.target)[1]
        resA[name] = {"own": own, "common": com, "r": r_}
        P(f"| {name} | {own.get('n', 0)} ({own.get('coverage', 0):.0%}) | "
          f"{own.get('d', np.nan):+.4f} ± {own.get('se', np.nan):.4f} | "
          f"{com.get('d', np.nan):+.4f} ± {com.get('se', np.nan):.4f} | {r_:+.3f} |")
    P(f"\nCommon subset: {int(ok.sum())} targets. Uncorrected Brier on it: "
      f"{resA[next(iter(rules))]['common'].get('brier0', np.nan):.4f}.\n")

    # B
    P("## B. Step 2: does carryover survive a state change? (0–6h evidence, known at exit)\n")
    P("β = slope of S2's error on the evidence mean error. Null = S2 targets circularly shifted within symbol "
      f"({a.n_null} draws). p = share of null draws with β_same − β_diff ≥ observed.\n")
    P("| split | β same [n] | β different [n] | β same − β diff | null diff median [2.5, 97.5] | p (diff) | p (β same > null) |")
    P("|---|---|---|---|---|---|---|")
    splits = {
        "fired from same regime as S2": ("regime", None),
        "same signal direction as S2": ("dir", None),
        "symbol regime unchanged since evidence resolved": ("reg_at_exit", None),
        "same regime, decayed h=3h": ("regime", 3.0),
        "regime unchanged since resolved, decayed h=3h": ("reg_at_exit", 3.0),
    }
    resB = {}
    xsplit = {name: x_split(T, E, key, hl=hl) for name, (key, hl) in splits.items()}
    null_rows = {name: [] for name in splits}
    for _ in range(a.n_null):
        Tn = shifted(T)
        for name, (xs, xd) in xsplit.items():
            bs, bd = beta(xs, Tn.target)[0], beta(xd, Tn.target)[0]
            null_rows[name].append((bs, bs - bd))
    for name, (xs, xd) in xsplit.items():
        bs, _, ns = beta(xs, T.target)
        bd, _, nd = beta(xd, T.target)
        diff = bs - bd
        nr = np.array([r for r in null_rows[name] if np.all(np.isfinite(r))])
        if len(nr):
            p_diff = float((nr[:, 1] >= diff).mean())
            p_same = float((nr[:, 0] >= bs).mean())
            q = np.percentile(nr[:, 1], [50, 2.5, 97.5])
        else:
            p_diff = p_same = np.nan
            q = [np.nan] * 3
        resB[name] = {"beta_same": bs, "n_same": ns, "beta_diff": bd, "n_diff": nd, "diff": diff,
                      "p_diff": p_diff, "p_same": p_same}
        P(f"| {name} | {bs:+.3f} [{ns}] | {bd:+.3f} [{nd}] | {diff:+.3f} | {q[0]:+.3f} [{q[1]:+.3f}, {q[2]:+.3f}] | "
          f"{p_diff:.3f} | {p_same:.3f} |")

    # regime persistence context: how often is S2's regime equal to the evidence's?
    share = []
    for r in T.itertuples():
        e = E[r.symbol]
        m = (e["exit"] < r.t) & (r.t - e["exit"] < 6)
        if m.sum():
            share.append(((e["regime"][m] == r.regime).mean(), (e["reg_at_exit"][m] == r.regime).mean()))
    if share:
        s_ = np.array(share)
        P(f"\nContext: of 0–6h evidence, {s_[:, 0].mean():.0%} was fired from S2's regime; for "
          f"{s_[:, 1].mean():.0%} the symbol's regime at resolution already equalled S2's.\n")

    P(f"_Runtime {time.time() - t0:.0f}s._")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = ROOT / "worklog" / "kairos" / "reports" / f"{stamp}_calibration_step2"
    out.with_suffix(".md").write_text("\n".join(L) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps({"A": resA, "B": resB}, default=float, indent=1), encoding="utf-8")
    print("\n".join(L))
    print(f"\nreport: {out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
