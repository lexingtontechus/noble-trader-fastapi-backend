---
# Barrier physics, gambler's ruin, corrected geometry

`barrier.py` is the proof engine's physics layer. It computes, for any (TP,
SL, expiry) geometry, the probability of each outcome under a Brownian-motion
first-passage model — and checks every one against the repo's OWN analytic
solver (`barrier.barrier_null`). The verdicts W-G2/G3/G4 (the 0.62/0.25 SL-farm
fix) all come from here.

## The math

Sigma = 1 ATR/bar (the repo's convention; ATR is the per-trade risk unit).

**1. Closed-form gambler's ruin** (no deadline, drift-capable):

```
drftless:  P(TP first) = sl/(sl+tp)   ->  pi_SL = tp/(sl+tp)     (ruin form)
drifted mu:  P(TP first) = (1 - e^{-k*sl}) / (1 - e^{-k*(sl+tp)}),  k = 2*mu/sigma^2
```

With drift, `P(SL first)` DECREASES (positive drift makes ruin less likely) —
as it must.

**2. Monte Carlo with expiry** (`mc_first_passage`): sub-stepped Brownian
paths truncated at `expiry_bars * sub` sub-steps. Returns
`(pi_tp, pi_sl, pi_e)`.

- Near-continuous monitoring (`sub=64` for REPORT; `sub=16` for the solver).
- Same-sub-step double touches count as **neither** (repo convention,
  `tests/test_barrier_null.py:111-140`).
- Unresolved at expiry -> `pi_e`.

## Monitoring-bias correction (measured)

Discrete monitoring OVERSTATES P(TP first) vs the continuous analytic — the
boundaries are pushed out by mean sub-step overshoot:

| sub-steps/bar | overshoot bias |
|---|---|
| 1 (hourly steps) | ~+0.05 |
| 16 (solver grid) | ~+0.015 |
| 64 (reporting) | ~+0.005 |

Therefore:

- The solver grid runs at `sub=16` with a **0.01 margin** on the pi_SL cap, so
  REPORT_SUB=64 numbers stay under the published 0.62 cap.
- ALL reported probabilities (crosscheck, per-symbol current/corrected) use
  `REPORT_SUB=64`, landing within ~0.005 of the repo's continuous-monitoring
  `barrier_null`.

## Cross-check vs the repo

`crosscheck_repo()` runs the repo's own `barrier.barrier_null` and the engine's
independent MC (REPORT_SUB=64) on 3 headline cases — max |delta| = 0.0034:

| case | repo pi_SL/pi_E | our MC | abs diff |
|---|---|---|---|
| XAUUSD pi_SL (4.94/1.00, T=48) | 0.8313 | 0.8278 | 0.0034 |
| BTCUSD pi_SL (8.07/1.77, T=48) | 0.7906 | 0.7884 | 0.0022 |
| US500 pi_E (13.23/8.09, T=48) | 0.7042 | 0.7009 | 0.0033 |

## CURRENT geometry (read from live YAML)

Parsed from `config/feature_flags.yaml` -> `dynamic_atr.by_symbol` (lines
572-639). Band centre = middle grid value. `EXPIRY_BARS=48` (from
tiered_atr). The engine reads the *actual* shipped YAML, not a hardcoded table.

## CORRECTED geometry solver (`solve_corrected_geometry`)

A coarse (tp, sl, expiry) grid scored by MC under the shrunk TREND-regime drift
`mu_hat`, with **two hard constraints**:

1. **pi_SL <= 0.62 - 0.01** (solver margin) — checked at the LONGEST expiry
   (pi_SL is non-decreasing in T, so checking once there is sufficient).
2. **pi_E <= 0.25** — checked per candidate expiry (pi_E is non-increasing in T).
3. **RR in [1.5, 4]** — `rr_ok(tp, sl)`.

The constraints are enforced **UNDER THE DRIFTLESS NULL** so the corrected
geometry is never an SL-farm even if the drift estimate is wrong. EV is
maximised on the DRIFTED MC:

```
EV_R = pi_tp * (tp/sl) - pi_sl - cost_atr / sl
```

Monotonicity prunes the grid: pi_SL(null) non-decreasing in expiry -> check
at max; pi_E non-increasing -> check per expiry. After coarse grid, fine
refinement +/-1.0 around the optimum (step 0.25, expiry fixed).

## The finding (W-G2/G3/G4)

| Symbol | Current tp/sl/exp | pi_SL | EV_null | Corrected tp/sl/exp | pi_SL | pi_E |
|---|---|---|---|---|---|---|
| XAUUSD | 4.94 / 1.00 / 48 | 0.828 | <=-cost | 7.00 / 4.50 / 96 | 0.599 | (pi_e) |
| BTCUSD | 8.07 / 1.77 / 48 | 0.788 | <=-cost | 6.25 / 4.00 / — | 0.611 | (pi_e) |

**Every current band centre has EV <= -cost under the driftless null** — the
SL-farm proof. The corrected geometry clears 0.62 only when bands widen enough
to escape the SL-farm (hence XAUUSD/BTCUSD brick fallback in the live fixes).

## Cost table (`COST_BY_CLASS`)

Mirrors the repo's `ev_costs.cost_atr_by_class` exactly:

| class | cost (ATR/trade) |
|---|---|---|--|
| forex | 0.04 | metals | 0.06 | crypto | 0.10 | equity | 0.06 | index | 0.05 |

- US500/US30USD priced as INDEX; migration 217 corrected the registry drift
  (originally `stocks` -> `indices`).

## Fast mode

`EDGE_LAB_FAST=1` halves MC density (sub=8, 4000 coarse/4000 fine vs
16/5000/20000). Used by smoke tests; never for the canonical run.

## Sources

- `regime_edge/barrier.py` — all of the above.
- `regime_platform/sweep_orchestrator/barrier.py:162` — the repo's
  `barrier_null` (physics authority).
- `regime_platform/sweep_orchestrator/barrier_floor.py` — the LIVE stamp/
adjust/reject path that consumes these numbers (W-G2/G4 live fix).
- `tests/test_regime_edge.py` — `test_gamblers_ruin_driftless_vs_mc`,
  `test_positive_drift_raises_tp_first`, `test_barrier_crosscheck_against_repo`.
