---
# 17 verdicts + 7 live fixes

## The verdict tally

**17 verdicts — 11 PROVEN-FAIL, 6 MEASURED.** Every verdict text is data-driven
off the measured numbers (DM direction, calibration AUC direction, EV-gate
pass rates) — no presumed outcome. Full matrix in `edge_lab.json` ->
`verdicts`; rendered in the Edge Lab dashboard.

The 7 fixes that shipped LIVE (no shadow) are W-C1..W-C4, W-X3, W-X4, W-G2/G4.
Each fix has a unit test pinning its threshold.

## The 17 verdicts

### Calibration / EV

| id | verdict | one-line evidence |
|---|---|---|
| W-C1 | PROVEN-FAIL | EV gate prices a bracket that is never shipped (brick payoff x ATR prob), passes as a per-symbol constant, starves XAGUSD/USDCHF to ZERO trades |
| W-C2 | PROVEN-FAIL | Heavy sweep fails open on unscored brackets (directional p_win ~0.55 vs 0.20 floor passes by construction) |
| W-C3 | MEASURED | Calibration loop scores a mixed-semantics, survivor-biased target (expired excluded) |
| W-C4 | PROVEN-FAIL | Cost-blind, expiry-zero EV with single-gate dependency; under null every band is EV <= -cost |
| W-C5 | MEASURED | Nine p_win variants with crossing semantics; consumers re-blending get unrelated numbers |
| W-C6 | MEASURED | Scale-crossing enforcement + path asymmetries (Kelly reads raw, direction reads debounced) |

### Direction / regime

| id | verdict | one-line evidence |
|---|---|---|
| W-X1 | PROVEN-FAIL | 4-state rank HMM has no NEUTRAL; decided fraction 100%; tri-state `er` label abstains on a measured fraction |
| W-X2 | PROVEN-FAIL | HMM direction skill negative on 14/14 symbols (mean -0.0425, US500 -0.1019); worse than majority baseline |
| W-X3 | PROVEN-FAIL | Debounce converts whipsaw into stale-direction lock-in + 3h adoption lag (flip rate high in low-ER) |
| W-X4 | PROVEN-FAIL | Zero live chop gates; efficiency_24h etc. all shadow/stamp-only |
| W-X5 | PROVEN-FAIL | regime_conf >= 0.60 does not brake chop; mean conf low-ER >= 0.60 on all symbols |
| W-X6 | MEASURED | HMM neutrality doesn't reach published EV; trend features 30-bar vs sub-30h chop cycles |

### Geometry / barrier physics

| id | verdict | one-line evidence |
|---|---|---|
| W-G1 | MEASURED | Entry is stale close under clock-slot policy, uncompensated (repo's own SSE: -30pt gap) |
| W-G2 | PROVEN-FAIL | Tight-SL bands are SL-farms by physics (XAUUSD pi_SL 0.83 / BTCUSD 0.79); every band EV<=-cost under null |
| W-G3 | PROVEN-FAIL | 48h fixed deadline vs wide bands: expiry-farm (US500 pi_E=0.70) + starvation |
| W-G4 | PROVEN-FAIL | Nothing physics-based constrains the published bracket; corrected solver enforces pi_SL<=0.62, pi_E<=0.25 |
| W-G5 | MEASURED | Evidence-free geometry paths (n=0 band-centre picks, absolute-unit cold start) |

## The 7 live fixes (commit `836b840`, 62/62 tests)

| ID | Fix (priority) | Mechanism (threshold) | Tests |
|---|---|---|---|
| W-C1 (P0) | EV prices the SHIPPED bracket | `apply_bracket_to_ev(tp_dist_price, sl_dist_price)`; shared `orchestrator._shipped_bracket_distances` on light+heavy; rr gate tests real ratio | 5 |
| W-C2 (P0) | Heavy sweep fails closed | `p_win_bracket_unscored_policy: fail_closed`, EV hard floor, per-class `min_p_win_bracket_by_asset_class` | 8 |
| W-C3 (P0) | Published-book era predicate | migration `216_calibration_published_cutover_predicate.sql` — inert until marker row exists | 8 |
| W-C4 (HIGH) | Cost-aware EV | `ev_costs.cost_atr_by_class` (forex 0.04/metals 0.06/crypto 0.10/equity 0.06/index 0.05) x ATR, charged ONCE; `cost_price` stamp survives reprices | 12 |
| W-X3 (P1) | Direction stability | 6h cross-direction cooldown + `shifts_24h >= 6` flip-rate breaker (reasons: `cross_direction_cooldown`/`flip_rate_breaker`) | 8 |
| W-X4 (P1) | Chop gate | `efficiency_24h < 0.30` => suppress `chop_gate_low_er`; fail-open `skip_no_er` on cold start | 8 |
| W-G2/G4 (P2->LIVE) | Barrier floor | `barrier_floor.py`: pi stamped on every pick; pi_SL>0.62 => in-band adjust else reject `barrier_floor_sl_cap`; pi_E>0.25 warn-only | 13 |

## Consequences operators must know

1. XAUUSD/BTCUSD tiered-ATR picks now REJECT (pi_SL 0.83/0.79 > 0.62 cap; no
in-band widening clears it) -> brick geometry until re-banded. Corrected bands
   that clear: XAUUSD 7.00/4.50/96h (pi_SL 0.599), BTCUSD 6.25/4.00 (pi_SL 0.611).
2. Expect fewer, more honest signals: unscored brackets no longer pass on 0.55;
   EV carries round-trip cost; chop gate suppresses consolidation windows.
3. **8 of 14 symbols (incl. both indices) now starve** — EV-gate starvation is a
   known, measured defect (zero trades / 2y); the gate's threshold, not the
   market, is the binding constraint. US500 pi_E=0.70 in-symbol.
4. Corrected did NOT significantly out-earn kairos-as-is per-symbol (Holm
   p=1.0); **pooled p=0.0101** is drift inheritance with negative direction skill.
   Wins are coherence (C1-C4), the physics floor, and the live chop/neutral gate.
5. US500 expiry starvation documented, not fixed: pi_E 0.70 vs 48h global
   deadline; `pi_e_cap` is stamp+warn only.

## The verdicts are in the artifact

`edge_lab.json` -> `verdicts` (17 records, each with id/title/verdict/evidence/numbers/source).
summary.md renders the verdict table section 7. The Edge Lab dashboard shows
them with the fix_metadata (empty for MEASURED-ONLY verdicts).

## Sources

- `docs/scopes/20261020_REGIME_EDGE_PROOF_ENGINE_AND_LIVE_FIXES.md` — primary.
- `regime_edge/report.py` — `build_verdicts()` (data-driven, no presumed outcome).
- `kairos-review-worklogs/task_40b_notes.md` — full per-fix engineering notes.
- Tests: `tests/test_w_c1_shipped_ev.py`, `test_w_c2_heavy_fail_closed.py`,
  `test_w_c3_view_predicate.py`, `test_w_c4_ev_cost.py`,
  `test_w_x3_direction_stability.py`, `test_w_x4_chop_gate.py`,
  `test_w_g2_barrier_floor.py`.
