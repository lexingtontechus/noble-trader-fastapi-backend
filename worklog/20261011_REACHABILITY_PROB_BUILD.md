# Reachability-conditioned probability — build + live E2E replay

**Date:** 2026-10-11
**Scope:** Build `reachability_prob.py` — the calibration reframe from `20261011_REACHABILITY_CONDITIONED_CALIBRATION.md` — and verify it end-to-end: unit/E2E tests on era-clean-shaped cells + a **live replay against real `nt_bracket_reachability` rows**. Same E2E + replay discipline as the strategy-structure builds.
**Status:** Module built, tested, live replay confirms the reframe. Shadow — NOT yet wired into EV/Kelly (that's the gated flip).
**Roadmap:** `calibration_reachability_reframe`.

---

## 1. The reframe, in one line

Calibration and reachability are one problem: a **direction-only `p_win` priced against a geometrically-unreachable book reads as overconfident**, because realized win rate is capped by *geometry and time*, not direction skill. The fix conditions the probability on reachability:

$$P_{reach} = P(\text{reach TP before SL before expiry} \mid \text{symbol, vol\_tier, distance\_bucket, expiry})$$

## 2. What was built — `regime_platform/sweep_orchestrator/reachability_prob.py`

`reachability_conditioned_probability(reach_rows, symbol, vol_tier, distance_bucket, expiry_hours, direction_edge, signal, min_n)` returns a `ReachProbability`:

- **Base rate** = the era-clean cell's `pct_hit_tp` for that `(symbol, vol_tier, distance_atr_bucket, signal)`. This already nets out expiry — it is NOT re-discounted.
- **Expiry factor** = `min(1, expiry_hours / p90_hours)` — a monotonic haircut: a leash shorter than the cell's p90 cuts the tail that would have resolved later; never inflates.
- **Direction adjustment** = a light logit-space nudge for within-cell direction edge (centred on the cell base rate, weight 0.5), so the trending_down-vs-consolidating gap survives WITHOUT letting a direction-only signal re-introduce overconfidence.
- **Fail-open**: cold cell (`n < 20`) → `matched=False`; no matching cell → `None`. Never invents a probability, never raises.
- `compare_to_blend(p_reach, p_win_blend)` — the diagnostic: positive delta = the blend overprices vs reachability.

Side-specific rows (buy/sell, migration 159) are preferred, pooled `all` as fallback.

## 3. E2E + replay results

### Unit/E2E (`tests/test_reachability_prob.py`, 12 tests, all pass)
Encodes era-clean-shaped cells and verifies: base rate reproduces the cell at a covering leash; expiry is monotonic and cuts the tail; tight bracket prices ~0; direction edge modulates but **cannot dominate** (a strong edge can't push a 9% tight cell to 50% — geometry binds); cold/missing cells fail open; `compare_to_blend` flags overpricing.

### Live replay — real `nt_bracket_reachability` (17 cells, `signal='all'`, stored-ATR, `n≥20`), leash 48h:

| symbol | vol_tier | bucket | n | cell_tp | exp_f | **P_reach** |
|---|---|---|---|---|---|---|
| BTCUSD | med_low_vol | 2-4 | 144 | 0.417 | 1.00 | **0.417** |
| BTCUSD | high_vol | 2-4 | 77 | 0.416 | 1.00 | **0.416** |
| BTCUSD | med_high_vol | 2-4 | 99 | 0.293 | 1.00 | **0.293** |
| XAUUSD | high_vol | 2-4 | 68 | 0.279 | 1.00 | **0.279** |
| USDCHF | med_high_vol | 2-4 | 75 | 0.280 | 1.00 | **0.280** |
| XAUUSD | med_high_vol | 4+ | 56 | 0.232 | 1.00 | **0.232** |
| XAUUSD | low_vol | 2-4 | 61 | 0.197 | 1.00 | **0.197** |
| USDCAD | low_vol | 4+ | 72 | 0.181 | 1.00 | **0.181** |
| XAGUSD | med_high_vol | 2-4 | 86 | 0.186 | 1.00 | **0.186** |
| XAUUSD | med_high_vol | 2-4 | 62 | 0.177 | 1.00 | **0.177** |
| XAUUSD | low_vol | 4+ | 79 | 0.051 | **0.90** | **0.045** |
| US30USD | high_vol | 4+ | 67 | 0.015 | 1.00 | **0.015** |
| AUDUSD | high_vol | 1-2 | 154 | 0.078 | 1.00 | **0.078** |
| EURUSD | low_vol | 1-2 | 101 | 0.089 | 1.00 | **0.089** |
| EURUSD | med_low_vol | 1-2 | 77 | 0.000 | 1.00 | **0.000** |
| USDJPY | high_vol / med_low_vol | 4+ | 64/61 | 0.000 | 1.00 | **0.000** (100% expired) |

**The core diagnostic (blend avg_p_win = 0.365 vs P_reach):**
- **15 of 17 cells: the blend OVERPRICES.** Mean delta **+0.195**, median **+0.184**.
- **2 cells underprice** — both BTCUSD 2-4 (P_reach 0.42), a genuinely reachable, high-edge cell the current blend *under*-prices.

**This reproduces the era-clean calibration bias (+0.10…+0.33) from first principles** — the overconfidence was largely the artifact of pricing direction independently of reachability, and conditioning on reachability recovers it. The two directions are the actionable part:
- **Overpriced cells** (forex/metals tight + wide-expiry): the blend should be pulled *down* toward P_reach — most of these are the negative-expectancy bulk.
- **Underpriced cells** (BTCUSD 2-4): a real edge the blend is *missing* — conditioning would pull it *up*.

## 4. What this is NOT (yet)

- **Shadow.** `reachability_prob.py` computes and returns P_reach; it is **not wired into EV/Kelly** as the live `p_win`. That flip is the gated next step, one lever, behind a flag, validated walk-forward on whether bias→0 and expectancy improves.
- **The `direction_edge` input is optional and unestimated here.** The within-cell direction skill (e.g. trending_down gold-buy edge) needs its own era-clean estimator per (regime, direction) before the logit nudge carries real weight; for now it defaults to base-rate-only (edge=None), which is the conservative direction.
- **Live numbers drift.** The replay is a 2026-10-11 snapshot; `nt_bracket_reachability` refreshes, so P_reach moves with it — that's the point (it's conditioned on current geometry), but any decision needs a fresh pull.

## 5. Files

- `regime_platform/sweep_orchestrator/reachability_prob.py` — the estimator (new).
- `tests/test_reachability_prob.py` — 12 E2E tests (new).
- `worklog/20261011_REACHABILITY_CONDITIONED_CALIBRATION.md` — the reframe (prior).
- Roadmap `calibration_reachability_reframe` — build bar.

## 6. Next (gated)

Wire P_reach into EV/Kelly behind a `p_win_reachability_mode: off|shadow|live` flag (shadow = stamp `p_reach` + `compare_to_blend` delta on `nt_signal_sim` for measurement first), estimate the `direction_edge` term from era-clean (regime × direction) cells, then validate: does per-symbol bias move toward 0 and expectancy improve, era-clean walk-forward? Only then flip live, one symbol cohort at a time.

---

# Shadow stamp build (2026-10-11)

**Scope:** Wire P_reach into the sweep as a **shadow stamp** so the live-vs-blend delta starts accumulating on `nt_signal_sim` for the walk-forward validation. Gate `p_win_reachability_mode` ships **`off`**; EV/Kelly unchanged until the gated `live` flip.
**Status:** Built, migration 208 applied, E2E tests green, deploy pending full suite.

## What was wired

- **`get_p_win_reachability_mode()`** (`feature_flags.py`): `off | shadow | live`, default `off`, unknown → `off` (fail-safe).
- **`lookup_reach_prob()`** (`reachability_prob.py`): async fetch of the matching `(symbol, vol_tier, bucket, signal)` reachability row (incl. `p90_hours` for the expiry factor), computes P_reach. Best-effort — never raises into the sweep tick (same contract as `lookup_bracket_probs`).
- **Orchestrator stamp block** (`_process_symbol`, before the `nt_signal_sim` payload): when mode ≠ `off` and a bracket bucket exists, calls `lookup_reach_prob`, computes `compare_to_blend(p_reach, ev_result.p_win)`, stamps `p_reach` / `p_reach_delta`. Fail-open on any miss.
- **Payload** (`_build_signal_sim_payload`): adds `p_reach` / `p_reach_delta` only when a read exists.
- **Allowlist** (`supabase_client.NT_SIGNAL_SIM_COLUMNS`): `p_reach`, `p_reach_delta`.
- **Migration 208**: nullable `nt_signal_sim.p_reach`, `nt_signal_sim.p_reach_delta` + partial index. Applied. No backfill.

## Shadow contract (unchanged EV/Kelly)

The stamp is **measurement-only**. `ev_result.p_win` still flows into EV/Kelly/qualified/kelly/SL/TP exactly as before. The shadow stamp adds two columns so the validation can answer, per resolved signal: *does P_reach (and replacing p_win with it) reduce the blend's overpricing bias toward 0 and improve expectancy, era-clean?* Only after that walk-forward passes does `p_win_reachability_mode=live` flip P_reach into EV/Kelly — one symbol cohort at a time.

## E2E + replay (`tests/test_reachability_shadow_stamp.py`, 7 tests)

Mode ships `off`; `lookup_reach_prob` computes from a fetched row and fails open on empty/error; the stamped delta matches the live-replay shape (overpriced cells → +delta, BTCUSD 2-4 → −delta); the payload builder carries `p_reach`/`p_reach_delta` only when present. Plus the 12 base-estimator tests (`test_reachability_prob.py`). 17 reachability tests green.

## Live replay reference (from §3)

17 era-clean cells, leash 48h: **15/17 overpriced**, mean +0.195 / median +0.184; 2 underpriced (BTCUSD 2-4). The shadow stamp now records exactly this delta per live signal.

## Next (gated)

Flip `p_win_reachability_mode=shadow` on the Studio → let P_reach/p_reach_delta accumulate on `nt_signal_sim` → resolve → measure bias→0 + expectancy, era-clean walk-forward → only then `live`, one cohort at a time.
