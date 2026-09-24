#!/usr/bin/env python3
"""
edge_state_gate_test.py — small pre-registered test for scope item #3:
"which params should be tuned in each edge state?" (SL/TP excluded — that
is geometry).

Read-only. Uses the live nt_signal_sim cache built by
regime_cell_edge_persistence.py (.cache/regime_cell_edge_persistence_sim.json).

Pre-registered (2026-09-23, before the first run):
  population  live sweep-light episodes (1 row per symbol × side × hour),
              stamped with their point-in-time edge state (same panel as
              quadrant_panel: 14d level vs 7d trend of within-cell R,
              episodes resolved before the day starts)
  params      the four light gates that carry a value on every row:
              p_win ≥ MIN_P_WIN, regime_conf ≥ MIN_REGIME_CONF,
              effective_kelly ≥ MIN_KELLY, ev > MIN_EV (current config values)
  statistic   separation = mean R(pass) − mean R(fail), R demeaned within
              symbol; symbol-day cluster bootstrap
  hypothesis  G1: for some (param, edge state) the separation differs from
              that param's pooled separation → that param wants a
              different threshold in that state
  family      4 params × 4 edge states = 16 tests, Holm; pass = adj p < 0.05
  secondary   (descriptive, not tested) pooled separation per param — is
              the gate useful at all right now; win-rate version.

Edge-state names (scope rename; quadrant names in parentheses):
  edge_rising (Leader)       level > 0, trend ≥ level
  edge_decaying (Fading)     level > 0, trend < level
  edge_recovering (Emerging) level ≤ 0, trend > level
  no_edge (Dead)             level ≤ 0, trend ≤ level

Usage:
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/edge_state_gate_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import regime_cell_edge_persistence as P  # noqa: E402

STATES = ["edge_rising", "edge_decaying", "edge_recovering", "no_edge"]
RNG = np.random.default_rng(23)
N_BOOT = 4000


def _thresholds() -> dict[str, tuple[str, float]]:
    from regime_platform.core import feature_flags as f
    return {"p_win": (">=", f.get_min_p_win()),
            "regime_conf": (">=", f.get_min_regime_conf()),
            "effective_kelly": (">=", f.get_min_kelly()),
            "ev": (">", f.get_min_ev())}


def _sep_boot(s: pd.DataFrame, col: str, mask: pd.Series | None):
    """Bootstrap (over symbol-day clusters) of mean(col|pass) − mean(col|fail)
    within `mask`, and of the same quantity minus the pooled value."""
    m = mask if mask is not None else pd.Series(True, index=s.index)
    g = pd.DataFrame({"clu": s.clu, "x": s[col], "p": s["pass"].astype(float), "m": m.astype(float)})
    g["xp_m"] = g.x * g.p * g.m
    g["xf_m"] = g.x * (1 - g.p) * g.m
    g["np_m"] = g.p * g.m
    g["nf_m"] = (1 - g.p) * g.m
    g["xp"] = g.x * g.p
    g["xf"] = g.x * (1 - g.p)
    g["nf"] = 1 - g.p
    A = g.groupby("clu")[["xp_m", "np_m", "xf_m", "nf_m", "xp", "p", "xf", "nf"]].sum().to_numpy()

    def stat(T):
        sub = T[..., 0] / np.maximum(T[..., 1], 1) - T[..., 2] / np.maximum(T[..., 3], 1)
        pool = T[..., 4] / np.maximum(T[..., 5], 1) - T[..., 6] / np.maximum(T[..., 7], 1)
        return sub, sub - pool

    est_sub, est_diff = stat(A.sum(0))
    idx = RNG.integers(0, len(A), (N_BOOT, len(A)))
    b_sub, b_diff = stat(A[idx].sum(1))

    def summ(est, b):
        lo, hi = np.percentile(b, [2.5, 97.5])
        return float(est), float(lo), float(hi), float(min(1.0, 2 * min((b <= 0).mean(), (b >= 0).mean())))

    npass = int((g.p * g.m).sum())
    nfail = int(((1 - g.p) * g.m).sum())
    return summ(est_sub, b_sub), summ(est_diff, b_diff), npass, nfail


def _f(t):
    return f"{t[0]:+.3f} [{t[1]:+.3f},{t[2]:+.3f}] p={t[3]:.3f}"


def holm(ps: dict[str, float]) -> dict[str, float]:
    items = sorted(ps.items(), key=lambda kv: kv[1])
    m, run, out = len(items), 0.0, {}
    for i, (k, p) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def main() -> int:
    cache = ROOT / ".cache" / "regime_cell_edge_persistence_sim.json"
    rows = json.loads(cache.read_text())
    ep = P.episodes(rows, expired_mtm=False)
    t0, t1 = ep.ts.min().normalize(), ep.ts.max().normalize()
    s = P.quadrant_panel(ep, t0, t1 - pd.Timedelta(days=2))
    s["state"] = s.quad
    s["day"] = pd.to_datetime(s.day)
    s["clu"] = s.symbol + "|" + s.day.dt.strftime("%Y-%m-%d")
    s["r_x"] = s.r - s.groupby("symbol").r.transform("mean")
    s["win_x"] = s.win - s.groupby("symbol").win.transform("mean")
    print(f"live episodes {len(ep)} ({t0.date()}..{t1.date()}), stamped panel {len(s)} across "
          f"{s.symbol.nunique()} symbols, {s.clu.nunique()} symbol-days")
    print("edge-state share: " + ", ".join(f"{k} {v:.0%}" for k, v in
                                          s.state.value_counts(normalize=True).reindex(STATES).items()))
    print("NOTE: every live row predates the 2026-09-22 debounce go-live — one label convention.\n")

    ps: dict[str, float] = {}
    for col, (op, thr) in _thresholds().items():
        x = pd.to_numeric(s[col], errors="coerce")
        s["pass"] = (x >= thr) if op == ">=" else (x > thr)
        pooled, _, npz, nfz = _sep_boot(s, "r_x", None)
        wpool, _, _, _ = _sep_boot(s, "win_x", None)
        print(f"── {col} {op} {thr:g}: pass {s['pass'].mean():.0%} · pooled R(pass)−R(fail) {_f(pooled)} · "
              f"win-rate {_f(wpool)}")
        for st in STATES:
            m = s.state == st
            sub, diff, npass, nfail = _sep_boot(s, "r_x", m)
            flag = "" if min(npass, nfail) >= 100 else "  (thin: <100 on one side)"
            print(f"     {st:16s} sep {_f(sub)} · vs pooled {_f(diff)} · n pass/fail {npass}/{nfail}{flag}")
            ps[f"{col} × {st}"] = diff[3]
        print()

    adj = holm(ps)
    print("Holm-adjusted G1 family (param × edge state separation ≠ pooled; pass = adj p < 0.05):")
    for k in sorted(ps, key=lambda k: ps[k]):
        print(f"  {k:36s} raw {ps[k]:.4f} → Holm {adj[k]:.4f}{'  PASS' if adj[k] < 0.05 else ''}")
    out = {"n_panel": len(s), "p_raw": ps, "p_holm": adj}
    (ROOT / ".cache" / "edge_state_gate_test.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
