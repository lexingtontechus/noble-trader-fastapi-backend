"""barrier.py — first-passage physics (W-G2/G3/G4 proof + corrected geometry).

1. Closed-form gambler's ruin (driftless + drift).  Barriers at +tp (favourable)
   and -sl (adverse), sigma = 1 ATR/bar:
       driftless:  P(TP first) = sl/(sl+tp)   ->  pi_SL = tp/(sl+tp)
       drift mu:   P(TP first) = (1 - e^{-k*sl}) / (1 - e^{-k*(sl+tp)}),  k = 2*mu/sigma^2
2. Monte Carlo with expiry, sub-stepped (near-continuous monitoring, the
   repo's own test convention — tests/test_barrier_null.py:111-140): returns
   (pi_tp, pi_sl, pi_e) truncated at expiry_bars.  Same-sub-step double
   touches count as neither (repo convention).

   Monitoring-bias note (measured): discrete sub-step monitoring OVERSTATES
   P(TP first) vs the continuous-monitoring analytic by ~+0.05 at sub=1,
   +0.015 at sub=16, +0.005 at sub=64 (overshoot bias — the absorbing
   boundaries are effectively pushed out by the mean sub-step overshoot).
   The solver grid therefore runs at sub=16 with a -0.01 margin on the
   pi_SL cap, while ALL REPORTED probabilities (crosscheck, per-symbol
   current/corrected pi rows) use REPORT_SUB=64, which lands within ~0.005
   of the repo's continuous-monitoring analytic barrier_null.
3. Cross-check against the repo's own analytic barrier_null
   (regime_platform/sweep_orchestrator/barrier.py:162).
4. CURRENT geometry per symbol parsed from the live feature-flags YAML
   (config/feature_flags.yaml, dynamic_atr.by_symbol, lines 572-639);
   band centre = middle grid value.  EXPIRY_BARS from the repo (=48).
5. CORRECTED geometry solver: coarse (sl, tp, expiry) grid scored by MC under
   the shrunk TREND-regime drift mu_hat, constrained UNDER THE DRIFTLESS NULL
   (pi_SL <= 0.62 - solver margin, pi_E <= 0.25, RR in [1.5, 4]) — the
   corrected geometry must not be an SL-farm under physics even if the drift
   estimate is wrong — while maximising under drift
       EV_R = pi_tp*(tp/sl) - pi_sl - cost_atr/sl
   then fine local refinement.  Under the driftless null every geometry has
   EV <= -cost (a fence cannot manufacture edge) — the solver prices the
   drift the regime values gate ON, not a free lunch.
"""
from __future__ import annotations

import math
import os
import re

import numpy as np

SEED = 7
SUB = 16                 # solver-grid monitoring density (fast)
REPORT_SUB = 64          # reporting-grade monitoring density (~0.005 of analytic)
EXPIRY_CHOICES = (24, 48, 72, 96)
CONSTRAINTS = {"max_pi_sl": 0.62, "max_pi_e": 0.25, "rr_lo": 1.5, "rr_hi": 4.0}
SOLVER_PI_SL_MARGIN = 0.01   # sub=16 overshoot bias head-room vs REPORT_SUB
COST_BY_CLASS = {"crypto": 0.10, "metals": 0.06, "forex": 0.04,
                 "equity": 0.06, "index": 0.05}
# Costs mirror the repo's own live table config/feature_flags.yaml ev_costs.
# cost_atr_by_class (forex 0.04 / metals 0.06 / crypto 0.10 / equity 0.06 /
# index 0.05, unknown 0.05) — the same net=pnl−cost convention EV uses.
SYMBOL_CLASS = {
    "BTCUSD": "crypto",
    "XAUUSD": "metals", "XAGUSD": "metals", "XAUEUR": "metals", "XAUAUD": "metals",
    "USDJPY": "forex", "USDCHF": "forex", "EURUSD": "forex", "GBPUSD": "forex",
    "USDCAD": "forex", "AUDUSD": "forex",
    "AAPL": "equity",
    "US500": "index", "US30USD": "index",
}
# NOTE (20261021, symbol-table CSV): the live registry lists US500/US30USD
# as asset_class=stocks (equity cost 0.06); the repo's own ev_costs table
# carries a distinct index class at 0.05.  This engine prices them as INDEX
# (their economic class per ev_costs' design) — the registry misclassification
# is recorded in 20261021_REOPT_SETTLEMENT_DEADLINE_COHERENCE_AND_SPECIALIST_
# BOOKS.md Part C and costs 0.01 ATR/trade of overcharge live.
FOREX_ALIAS = {"USDJPY": "FOREX", "USDCHF": "FOREX", "EURUSD": "FOREX",
               "GBPUSD": "FOREX", "AUDUSD": "FOREX", "USDCAD": "FOREX"}

# The full ACTIVE symbol set (Supabase symbol table 2026-10-21: 14 active of
# 34) — the 5-symbol v1 run was the test subset.
ACTIVE_SYMBOLS = [
    "BTCUSD", "XAUUSD", "XAGUSD", "XAUEUR", "XAUAUD",
    "USDJPY", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "AUDUSD",
    "AAPL", "US500", "US30USD",
]


# ── 1. closed form ─────────────────────────────────────────────────────────
def p_tp_first_closed(sl_atr: float, tp_atr: float, mu: float = 0.0, sigma: float = 1.0):
    """P(hit TP before SL), no deadline. Returns (p_tp, p_sl)."""
    a, b = float(sl_atr), float(tp_atr)
    if a <= 0 or b <= 0:
        return 0.5, 0.5
    if abs(mu) < 1e-12:
        p = a / (a + b)
        return p, 1.0 - p
    k = 2.0 * mu / (sigma * sigma)
    try:
        num = 1.0 - math.exp(-k * a)
        den = 1.0 - math.exp(-k * (a + b))
        if den == 0.0:
            p = 0.5
        else:
            p = num / den
    except OverflowError:
        p = 1.0 if k > 0 else 0.0
    return min(max(p, 0.0), 1.0), 1.0 - min(max(p, 0.0), 1.0)


# ── 2. Monte Carlo with expiry ─────────────────────────────────────────────
def mc_first_passage(tp_atr, sl_atr, expiry_bars, mu=0.0, sigma=1.0,
                     n_paths=20000, seed=SEED, sub=SUB):
    """Sub-stepped BM first passage truncated at expiry.  Returns (pi_tp, pi_sl, pi_e)."""
    rng = np.random.default_rng(seed)
    total = int(expiry_bars) * sub
    x = np.zeros(n_paths)
    ta = np.full(n_paths, total + 1, dtype=np.int64)
    tb = np.full(n_paths, total + 1, dtype=np.int64)
    tp, sl = float(tp_atr), float(sl_atr)
    step_sd = sigma / math.sqrt(sub)
    drift = mu / sub
    for i in range(total):
        x += drift + step_sd * rng.standard_normal(n_paths)
        na = (x >= tp) & (ta > total)
        ta[na] = i
        nb = (x <= -sl) & (tb > total)
        tb[nb] = i
    won = (ta < tb) & (ta <= total)
    los = (tb < ta) & (tb <= total)
    pi_tp = float(won.mean())
    pi_sl = float(los.mean())
    return pi_tp, pi_sl, max(0.0, 1.0 - pi_tp - pi_sl)


def _sim_paths(n_paths: int, total_sub: int, mu: float, seed: int, sub: int) -> np.ndarray:
    """Cumulative sub-stepped BM paths (float32, chunked) with drift mu/bar."""
    rng = np.random.default_rng(seed)
    out = np.empty((n_paths, total_sub), dtype=np.float32)
    step_sd = 1.0 / math.sqrt(sub)
    drift = mu / sub
    chunk = max(1, min(4000, n_paths))
    for s in range(0, n_paths, chunk):
        e = min(n_paths, s + chunk)
        steps = drift + step_sd * rng.standard_normal((e - s, total_sub))
        out[s:e] = np.cumsum(steps, axis=1, dtype=np.float32)
    return out


def _mc_from_paths(paths: np.ndarray, tp: float, sl: float, expiry_bars: int, sub: int):
    T = int(expiry_bars) * sub
    P = paths[:, :T]
    A = P >= tp
    B = P <= -sl
    ta = np.where(A.any(axis=1), A.argmax(axis=1), T + 1)
    tb = np.where(B.any(axis=1), B.argmax(axis=1), T + 1)
    won = (ta < tb) & (ta <= T)
    los = (tb < ta) & (tb <= T)
    pi_tp = float(won.mean())
    pi_sl = float(los.mean())
    return pi_tp, pi_sl, max(0.0, 1.0 - pi_tp - pi_sl)


def _fast_mode() -> bool:
    return os.environ.get("EDGE_LAB_FAST", "") == "1"


def solve_corrected_geometry(mu_hat: float, cost_atr: float, seed: int = SEED):
    """Coarse grid + fine refinement.  Constraints (pi_SL, pi_E, RR) are
    enforced on the DRIFTLESS-NULL MC (the corrected geometry must clear the
    physics caps even if the drift estimate is wrong); EV is maximised on the
    DRIFTED MC.  Returns the best feasible (tp, sl, expiry) with its MC probs
    under mu_hat.

    Monotonicity used to prune: pi_SL(null) is non-decreasing in expiry (more
    time to absorb), so the cap is checked once at the longest expiry; pi_E is
    non-increasing in expiry, so it is checked per candidate expiry.  The
    pi_SL cap carries a 0.01 solver margin so REPORT_SUB=64 numbers stay
    under the published 0.62 cap (sub=16 overshoot bias, measured ~+0.01)."""
    fast = _fast_mode()
    sub = 8 if fast else SUB
    coarse_paths = 1200 if fast else 5000
    fine_paths = 4000 if fast else 20000
    max_exp = max(EXPIRY_CHOICES)
    null_paths = _sim_paths(coarse_paths, max_exp * sub, 0.0, seed + 1, sub)
    drift_paths = _sim_paths(coarse_paths, max_exp * sub, mu_hat, seed, sub)
    cap_sl = CONSTRAINTS["max_pi_sl"] - SOLVER_PI_SL_MARGIN

    def ev(pi_tp, pi_sl, tp, sl):
        return pi_tp * (tp / sl) - pi_sl - cost_atr / sl

    def rr_ok(tp, sl):
        rr = tp / sl
        return CONSTRAINTS["rr_lo"] - 1e-9 <= rr <= CONSTRAINTS["rr_hi"] + 1e-9

    def scan(null_arr, drift_arr, sls, tps, expiries, best):
        n_feas = 0
        for sl in sls:
            for tp in tps:
                if not rr_ok(tp, sl):
                    continue
                # pi_SL cap at the LONGEST expiry (monotone in T)
                _, nsl_long, _ = _mc_from_paths(null_arr, tp, sl, max_exp, sub)
                if nsl_long > cap_sl:
                    continue
                for e in expiries:
                    _, _, ne_e = _mc_from_paths(null_arr, tp, sl, e, sub)
                    if ne_e > CONSTRAINTS["max_pi_e"]:
                        continue
                    pi_tp, pi_sl, pi_e = _mc_from_paths(drift_arr, tp, sl, e, sub)
                    n_feas += 1
                    score = ev(pi_tp, pi_sl, tp, sl)
                    if best is None or score > best[0]:
                        best = (float(score), float(tp), float(sl), int(e),
                                (float(pi_tp), float(pi_sl), float(pi_e)))
        return best, n_feas

    best, n_feas = scan(null_paths, drift_paths,
                        np.arange(0.5, 6.0 + 1e-9, 0.5),
                        np.arange(1.0, 15.0 + 1e-9, 1.0),
                        EXPIRY_CHOICES, None)
    if best is None:
        # relax nothing: fall back to the most defensive feasible-ish corner
        # (documented — only reachable on degenerate synthetic smoke data)
        return {"tp_atr": 3.0, "sl_atr": 2.0, "expiry": 96, "n_feasible": 0,
                "pi_tp": 0.0, "pi_sl": 0.0, "pi_e": 1.0, "ev": -9.9, "mu_hat": float(mu_hat)}
    _, tp0, sl0, exp0, _ = best
    # fine refinement: +/-1.0 around the coarse optimum, step 0.25, expiry fixed
    fine_drift = _sim_paths(fine_paths, max_exp * sub, mu_hat, seed, sub)
    fine_null = _sim_paths(fine_paths, max_exp * sub, 0.0, seed + 1, sub)
    fine_best, n_fine = scan(
        fine_null, fine_drift,
        np.arange(max(0.5, sl0 - 1.0), min(6.0, sl0 + 1.0) + 1e-9, 0.25),
        np.arange(max(1.0, tp0 - 1.0), min(15.0, tp0 + 1.0) + 1e-9, 0.25),
        (exp0,), best)
    score, tp, sl, exp_, probs = fine_best
    return {"tp_atr": round(tp, 2), "sl_atr": round(sl, 2), "expiry": int(exp_),
            "n_feasible": int(n_feas + n_fine), "pi_tp": probs[0], "pi_sl": probs[1],
            "pi_e": probs[2], "ev": score, "mu_hat": float(mu_hat)}


def geometry_probs(tp, sl, expiry, mu, seed=SEED):
    """20k-path reporting-grade MC probs for one geometry under drift mu.

    Uses REPORT_SUB=64 sub-steps/bar (within ~0.005 of the repo's
    continuous-monitoring analytic barrier_null)."""
    fast = _fast_mode()
    sub = 8 if fast else REPORT_SUB
    n = 4000 if fast else 20000
    return mc_first_passage(tp, sl, expiry, mu=mu, sigma=1.0, n_paths=n, seed=seed, sub=sub)


def ev_r(pi_tp, pi_sl, tp, sl, cost_atr):
    return pi_tp * (tp / sl) - pi_sl - cost_atr / sl


# ── 3. cross-check against the repo's own analytic null ────────────────────
def crosscheck_repo(cases=(("XAUUSD pi_SL", 4.94, 1.00, 48),
                           ("BTCUSD pi_SL", 8.07, 1.77, 48),
                           ("US500 pi_E", 13.23, 8.09, 48))):
    """Run the repo's barrier_null and our independent MC (REPORT_SUB=64) for
    the 3 headline cases; the repo's own measured values are 0.831 / 0.791 /
    0.701."""
    from regime_platform.sweep_orchestrator.barrier import barrier_null
    out = []
    for name, tp, sl, t in cases:
        b = barrier_null(tp, sl, t_bars=t)
        ours = mc_first_passage(tp, sl, t, mu=0.0, n_paths=20000, seed=SEED, sub=REPORT_SUB)
        if "pi_E" in name:
            repo_pi, our_pi = float(b.p_expire), ours[2]
        else:
            repo_pi, our_pi = float(b.p_loss), ours[1]
        out.append({"case": name, "repo_pi": round(repo_pi, 4),
                    "our_mc_pi": round(our_pi, 4),
                    "abs_diff": round(abs(repo_pi - our_pi), 4)})
    return out


# ── 4. current geometry from the live YAML ─────────────────────────────────
def load_current_geometry(repo_root: str, symbols: list[str]) -> dict:
    """Parse config/feature_flags.yaml dynamic_atr.by_symbol (band grids).

    Band centre = the middle grid value (for 3-value grids this equals the
    band midpoint to within the grid step).  Returns per-symbol geometry +
    provenance (file + line span).
    """
    import yaml
    path = os.path.join(repo_root, "config", "feature_flags.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    data = yaml.safe_load(text)
    bysym = data["dynamic_atr"]["by_symbol"]
    # locate the by_symbol line span for provenance
    dyn_i = next(i for i, l in enumerate(lines) if re.match(r"^dynamic_atr:\s*$", l))
    by_i = next(i for i in range(dyn_i, len(lines))
                if re.match(r"^  by_symbol:\s*$", lines[i]))
    end_i = by_i + 1
    while end_i < len(lines):
        l = lines[end_i]
        if l.strip() and not l.startswith("    ") and not l.lstrip().startswith("#") and (len(l) - len(l.lstrip())) <= 2:
            break
        end_i += 1
    try:
        from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS
    except Exception:
        EXPIRY_BARS = 48
    geo = {}
    for sym in symbols:
        key = FOREX_ALIAS.get(sym, sym)
        cfg = bysym.get(key) or {}
        tp_grid = cfg.get("tp_atr") or []
        sl_grid = cfg.get("sl_atr") or []
        tp = float(tp_grid[(len(tp_grid) - 1) // 2]) if len(tp_grid) % 2 == 1 else \
            float(cfg.get("tp_atr_min", 1) + cfg.get("tp_atr_max", 1)) / 2.0
        sl = float(sl_grid[(len(sl_grid) - 1) // 2]) if len(sl_grid) % 2 == 1 else \
            float(cfg.get("sl_atr_min", 1) + cfg.get("sl_atr_max", 1)) / 2.0
        geo[sym] = {
            "tp_atr": tp, "sl_atr": sl, "expiry": int(EXPIRY_BARS),
            "band": {"tp": [float(cfg.get("tp_atr_min", float("nan"))), float(cfg.get("tp_atr_max", float("nan")))],
                     "sl": [float(cfg.get("sl_atr_min", float("nan"))), float(cfg.get("sl_atr_max", float("nan")))]},
            "yaml_key": key,
        }
    return {"geometry": geo,
            "source": {"file": "config/feature_flags.yaml",
                       "lines": f"{by_i + 1}-{end_i}",
                       "expiry_bars": int(EXPIRY_BARS)}}


def shrunk_trend_drift(closes, atr, slots, directions, horizon: int = 24, n0: int = 100):
    """mu_hat (ATR/bar): |shrunk mean of direction-aligned 24h ATR-normalised
    moves| over TREND-labeled slots (train split only, caller's duty).

    Direction-aligned: (c[i+h]-c[i])/atr[i] * dir_i, i.e. the drift the
    sign(er_signed) book actually harvests (equals the literal signed mean
    when every TREND slot points the same way).  Shrinkage n/(n+n0).
    """
    closes = np.asarray(closes, float)
    atr = np.asarray(atr, float)
    vals = []
    for i, d in zip(slots, directions):
        if d == 0 or i + horizon >= closes.size:
            continue
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        vals.append((closes[i + horizon] - closes[i]) / atr[i] * d)
    vals = np.asarray(vals, float)
    n = vals.size
    if n == 0:
        return 0.0, 0
    raw = float(np.mean(vals))
    shrunk = raw * (n / (n + n0))
    return abs(shrunk) / horizon, int(n)
