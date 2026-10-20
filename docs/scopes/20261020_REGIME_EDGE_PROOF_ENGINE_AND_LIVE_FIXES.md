# Regime-Edge Proof Engine + Live Corrective Fixes — Definitive Remediation

> **Priority**: P0 executed · **Status**: implemented, all gates LIVE (no shadow) ·
> **Date**: 2026-10-20 · **Base**: weaknesses evaluation
> `kairos-review-worklogs/20261019_KAIROS_WEAKNESSES_EVALUATION.md` (audited at
> HEAD `c7568f9`) → fixes at `836b840`, proof engine at `29292bd` ·
> **Roadmap refs**: PBM phases (`20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md`),
> vol-backtest Phase 0/0.5 (`20261019_VOL_CALIBRATION_AND_LLM_SERVER_REPURPOSE.md`),
> geometry coherence (`20261012_GEOMETRY_SINGLE_SOURCE_COHERENT_FLOW.md`)

---

## 0. The operator's mandate

1. **Definitive, mathematical fixes** — measured thresholds, not shadow-mode
   guessing. Every corrective mechanism below ships **live** with a unit test
   pinning its threshold.
2. **Reframe the earlier TSFM + sweep data** — machine-parsed from the
   vol_backtest artifacts (§1.6).
3. **Assign numeric VALUES to regimes across the time series** —
   pattern/trend/volatility as continuous, point-in-time signals — and **measure
   whether they predict the edge** (§1.2).
4. **Do not assume Kairos is right** — prove or disprove each component, then
   implement the correction. Eleven of seventeen components measured
   PROVEN-FAIL (§1.7).

## 1. What was measured (`regime_edge/` proof engine)

New package `regime_edge/` + CLI `scripts/run_edge_lab.py` + tests
`tests/test_regime_edge.py` (19/19). One run, 5 symbols × 2y hourly
(13.7k–17.5k bars each), seeded(7), **467.9 s wall**, fully deterministic
(byte-identical measured sections on re-run; witness kept). Canonical run:
`worklog/kairos/reports/regime_edge/20261020_055039/`
(`edge_lab.json` 0.92 MB · `summary.md` · `records.csv` 9,462 rows).

### 1.1 Regime values (the operator's ask, made concrete)

| Value | Definition | Range |
|---|---|---|
| `trend` | signed 24h efficiency ratio (ER) | [−1, +1] |
| `chop` | Choppiness index / 100 | [0, 1] |
| `vol` | rolling ATR percentile | [0, 1] |
| tri-state label | hysteresis over ER (±0.10 deadband) | UP / DOWN / **NEUTRAL** |

The tri-state label supplies the **neutral state the live HMM vocabulary lacks**
(`regime_engine.py`'s 4-state labels force a direction every tick — W-X1).

### 1.2 Does the edge surface predict the edge? — honest-weak

Decile edge surfaces with bootstrap CIs, Spearman + permutation flatness tests
across 5 feature families × 5 symbols: **2/25 significant** (XAUUSD-ADX
ρ=−0.1165 p=0.006; USDJPY-Hurst ρ=+0.0797 p=0.030; ~1.3 expected by chance).
`er_abs` nowhere significant. **A measured-if-thin edge surface replaces a
negative-skill direction label** — that is the honest verdict, recorded
verbatim in the artifact.

### 1.3 HMM direction skill (Kairos-as-is, faithful repro)

24h direction skill **negative on all 5 symbols**: BTCUSD −0.0190, XAUUSD
−0.0492, XAGUSD −0.0410, USDJPY −0.0713, USDCHF −0.0416 (mean **−0.0444**;
the repo's own live migration-191 number was −0.0404 — reproduced). Confidence
0.63–0.67 straight through the choppiest (bottom-quartile-ER) windows; raw flip
rate higher in low-ER windows on all 5 (W-X5 PROVEN-FAIL).

### 1.4 Three-book gate backtest (same slot grid, DM/HAC/Holm)

| Book | Trades | R/trade (pooled) |
|---|---|---|
| kairos-as-is | 462 | **+0.2567** |
| corrected geometry | 121 | +0.1052 |
| driftless null | 1303 | −0.0159 |

Pooled DM kairos-vs-corrected p=0.0163 **favouring kairos in this drift-heavy
2y window** — its earnings are wide-TP/tight-SL payoff × realised drift with
negative direction skill (the W-G2 risk inheritance), **not direction alpha**;
per-symbol Holm p=1.0 (no decisive per-symbol edge either way).
Corrected-vs-null p=0.43. **New finding: EV-gate starvation — XAGUSD and
USDCHF published ZERO kairos trades in 2 years** (gate decision constant per
symbol/geometry).

### 1.5 Calibration (lockbox, walk-forward)

Regime-conditional AUC **0.5328 mean** (BTC 0.558 / XAG 0.605 / CHF 0.526 /
XAU 0.531 / JPY 0.444) vs base-rate 0.43–0.49. The repo-style bucket table's
0.55–0.64 AUC is **vol-tier (market-state) information only** — level-only for
the trade being placed (repo's own live-measured 0.4869).

### 1.6 TSFM + sweep reframe (machine-parsed from vol_backtest artifacts)

HAR beats naive **16/20** Holm-significant cells; HAR-EWMA **18/20**;
**Chronos-2 zero-shot beats HAR 0/20**; QLIKE-best arm: `har_ewma` 12 / `har`
6 / `chronos2_cov` 2. Phase 0.5 recalibration accepted **46/100** (mean b
0.7166, k 2.2506). Conclusion: **zero-shot TSFMs carry no directional edge;
their role is calibrated vol spreads for sizing/geometry** — unchanged
consumption gate: only calibrated spreads may feed Kelly vol clamp / brick
sizing.

### 1.7 Barrier physics (cross-checked against the repo's own solver)

`regime_edge/barrier.py` (gambler's ruin closed form + drift + expiry MC) vs
`barrier.barrier_null`: max |Δ| **0.0034** (XAUUSD π_SL 0.8313 vs 0.8278;
BTCUSD 0.7906 vs 0.7884; US500 π_E 0.7009 vs 0.7042). **Every configured
band-centre has EV ≤ −cost under the driftless null** — the SL-farm proof
(W-G2/G4). Corrected centres that clear the caps:

| Symbol | Current (tp/sl ATR, 48h) | π_SL | Corrected | π_SL |
|---|---|---|---|---|
| XAUUSD | 4.94 / 1.00 | 0.828 | 7.00 / 4.50, 96h | **0.599** |
| BTCUSD | 8.07 / 1.77 | 0.788 | 6.25 / 4.00 | **0.611** |

Verdict tally: **17 verdicts — 11 PROVEN-FAIL, 6 MEASURED** (full matrix in
`edge_lab.json` → `verdicts`; rendered in the Edge Lab dashboard).

## 2. The seven live fixes (commit `836b840`, 62/62 new tests)

| ID | Fix | Mechanism (threshold) | Tests |
|---|---|---|---|
| W-C1 (P0) | EV prices the SHIPPED bracket | `apply_bracket_to_ev(tp_dist_price, sl_dist_price)`; shared `orchestrator._shipped_bracket_distances` on light+heavy; rr gate tests the real ratio | 5 |
| W-C2 (P0) | Heavy sweep fails closed | honours `p_win_bracket_unscored_policy: fail_closed`, EV hard floor, per-class `min_p_win_bracket_by_asset_class` | 8 |
| W-C3 (P0) | Published-book era predicate | migration `216_calibration_published_cutover_predicate.sql` — **inert until marker row exists** | 8 |
| W-C4 (HIGH) | Cost-aware EV | `ev_costs.cost_atr_by_class` (forex 0.04 / metals 0.06 / crypto 0.10 / equity 0.06 / index 0.05) × ATR, charged ONCE; `cost_price` stamp survives reprices | 12 |
| W-X3 (P1) | Direction stability | 6h cross-direction cooldown + `shifts_24h ≥ 6` flip-rate breaker (reasons `cross_direction_cooldown` / `flip_rate_breaker`) | 8 |
| W-X4 (P1) | Chop gate | `efficiency_24h < 0.30` ⇒ suppress `chop_gate_low_er`; fail-open stamp `skip_no_er` on cold start | 8 |
| W-G2/G4 (P2→LIVE) | Barrier floor | `sweep_orchestrator/barrier_floor.py`: π stamped on every pick; π_SL > 0.62 ⇒ in-band adjust else reject `barrier_floor_sl_cap`; π_E > 0.25 warn-only | 13 |

Full per-fix engineering notes: `kairos-review-worklogs/task_40b_notes.md`
(out-of-repo) — every wiring anchor re-verified in source; 5 pre-existing
tests legitimately updated (2 stub signatures, 3 floored band-centre
expectations); full suite **zero regressions vs HEAD** (24 failures proven
pre-existing by HEAD overlay); ruff clean on touched lines.

## 3. Consequences operators must know

1. **XAUUSD / BTCUSD tiered-ATR picks now REJECT** (band centres π_SL
   0.83/0.79 > 0.62 cap; no in-band widening clears it) ⇒ those symbols
   publish from **brick geometry** until re-banded. Corrected centres that
   clear the cap: XAUUSD 7.00/4.50/96h (π_SL 0.599), BTCUSD 6.25/4.00
   (π_SL 0.611). Re-banding is an operator decision — the floor will enforce
   whatever bands are configured.
2. **Expect fewer, more honest signals**: unscored brackets no longer pass on
   a directional 0.55; EV now carries round-trip cost; the chop gate suppresses
   consolidation windows that previously shipped conf 0.63–0.67.
3. **XAGUSD / USDCHF EV-gate starvation is now a known, measured defect**
   (zero trades / 2y) — the gate's threshold, not the market, is the binding
   constraint. Separate remediation, not addressed here.
4. **The corrected book did NOT significantly out-earn kairos-as-is
   per-symbol** (Holm p=1.0). The pooled DM p=0.016 favouring kairos is drift
   inheritance with negative direction skill — exactly the W-G2 risk. The
   definitive wins are coherence (C1–C4), the physics floor, and the live
   chop/neutral gate.
5. **US500 expiry starvation is documented, not fixed**: π_E 0.70 vs the
   48h global deadline; `pi_e_cap` is stamp+warn only (changing the deadline
   is a product decision).

## 4. Operator actions (in order)

1. `git push origin main` (publishes the local commits incl. this doc set).
2. **Apply `supabase/migrations/215_dst_safe_heavy_sweep_cron.sql` to prod
   Supabase BEFORE 2026-11-01** (carried over from the 2026-10-19 delivery —
   still time-sensitive).
3. Migration `216_…cutover_predicate.sql` is **inert by design** — apply it
   any time; it only activates when the `nt_calibration_cutover` marker row
   is inserted (era-clean cut of the published calibration book).
4. Watch XAUUSD/BTCUSD publish behaviour after deploy: brick-geometry
   brackets with honest EV/cost gates are the expected state, not a bug.

## 5. Reproduce / verify

```bash
PYTHONPATH=$PWD/src .venv/bin/pytest tests/test_regime_edge.py -q          # 19 passed
PYTHONPATH=$PWD/src .venv/bin/pytest tests/test_w_c1_shipped_ev.py \
  tests/test_w_c2_heavy_fail_closed.py tests/test_w_c3_view_predicate.py \
  tests/test_w_c4_ev_cost.py tests/test_w_x3_direction_stability.py \
  tests/test_w_x4_chop_gate.py tests/test_w_g2_barrier_floor.py -q         # 62 passed
PYTHONPATH=$PWD/src .venv/bin/python scripts/run_edge_lab.py               # ~8 min, seeded(7)
```

Determinism: two consecutive runs produce byte-identical measured sections
(witness run `20261020_053412`).

## 6. Artifact map

| What | Where |
|---|---|
| Canonical run (json/md/csv) | `worklog/kairos/reports/regime_edge/20261020_055039/` |
| Determinism witness | `worklog/kairos/reports/regime_edge/20261020_053412/` |
| Cached OHLCV (730d hourly, UTC) | `data/regime_edge/` (git-ignored) |
| Engine source | `regime_edge/` (11 modules) |
| CLI | `scripts/run_edge_lab.py` |
| Fixes source | `regime_platform/sweep_orchestrator/{barrier_floor,orchestrator,heavy_sweep,kairos,p_win_bracket,tiered_atr}.py`, `ev_engine.py`, `config/feature_flags.yaml` |
| Migration | `supabase/migrations/216_calibration_published_cutover_predicate.sql` |
| Dashboard (companion app) | Kairos Edge Lab — single-page Next.js `/` reading `edge_lab.json` |
| Session evidence trail | `kairos-review-worklogs/task_40{a,b,c}_notes.md` (out-of-repo) |
