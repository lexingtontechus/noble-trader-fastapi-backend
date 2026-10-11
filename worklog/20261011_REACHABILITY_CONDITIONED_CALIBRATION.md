# Reachability-conditioned calibration — reframe + R1/R4/R5 E2E verification

**Date:** 2026-10-11
**Scope:** (1) Reframe calibration + reachability as ONE problem — a reachability-conditioned probability — instead of a direction-only `p_win` times a parallel reachability diagnostic. (2) E2E replay test verifying the R1 router, R4 archetype classifier, R5 stance, and the new reachability-conditioned estimator against era-clean outcomes.
**Status:** Reframe specced; E2E replay test built (offline, deterministic). No live flip.
**Related:** `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md`; roadmap `calibration_reachability_reframe`, `router_clef_stance_agreement`, `mean_reversion_gold_quant`.

---

## 1. The problem is one problem, measured twice

Era-clean (post-2026-10-02, `contaminated=false`), 2026-10-11:

| metric | value |
|---|---|
| WR (closed) | **12.5%** (826/6,586) |
| expectancy_R | **−0.58** |
| avg_p_win | 0.365 (model predicts ~1 in 3, realizes ~1 in 8) |
| per-symbol bias | **+0.095 … +0.334** — every symbol overprices TP |

Reachability (era-clean buckets):

| bucket | TP% | SL% | expired% | med_hours |
|---|---|---|---|---|
| <0.5 | 0% | 100% | 0% | 0.4 |
| 0.5-1 | 7.6% | 92.4% | 0% | 0.5 |
| 1-2 | 9.0% | 91.0% | 0% | 0.5 |
| 2-4 | 17.1% | 82.9% | 0% | 1.6 |
| 4+ | 19.2% | 51.1% | **29.7%** | 12.1 |

The two diagnostics describe the same defect from two angles:

- **Calibration** says the model is overconfident. But it measures a **direction-only** `p_win` — P(this direction resolves TP) — as if every signal were geometrically capable of reaching TP.
- **Reachability** says most brackets **can't** reach TP before SL or expiry — tight brackets stop out 91–100%, and even the wide 4+ bucket expires 30% of the time.

A direction-only probability priced against a book where the bracket is usually unreachable **will** read as overconfident, because the realized win rate is capped by *geometry and time*, not by direction skill. The overconfidence is largely an **artifact of pricing direction independently of reachability.**

## 2. The reframe — reachability as the conditioning variable, not a parallel table

Stop computing `p_win` (direction) and reachability (bucket hit-rate) separately and multiplying/ignoring. Compute **one** probability that already conditions on whether the bracket is reachable:

$$\hat{P} = P(\text{reach TP before SL before expiry} \mid \text{symbol, vol\_tier, distance\_bucket, expiry})$$

- Estimated per `(symbol, vol_tier, distance_atr_bucket)` from **era-clean resolved rows** (the same cells `nt_bracket_reachability` already buckets).
- **Expiry horizon is an explicit term**, not a fixed 48h — a wide bracket's 41h-median/56h-p90 tail is why 30% expire; the probability must know the leash.
- This is the number that enters EV/Kelly, replacing the direction-only `p_win`.

Why this is the right lever and not another gate: every gate candidate this cycle (bracket, reachability, regime_edge, context, clef-direction) failed for the same reason — **the base book is negative and no per-bucket filter separates winners at scale.** Conditioning the probability on reachability moves the *mean* (it prices the geometry honestly), which is what actually fixes a negative-expectancy book. Gates trim; a correct probability reprices.

## 3. What the era-clean data already proves about the shape

The estimator is not speculative — the era-clean cells show the conditioning matters enormously:

- **XAUUSD buy** `trending_down` WR **38.2%** (13/34) vs `consolidating` **8.1%** (11/136) — a 4.7× gap by trend state alone.
- Reachability bucket 4+ TP 19.2% vs <0.5 TP 0% — an unbounded gap by geometry.

A probability that ignores either axis is wrong by multiples. The reframe makes both axes explicit inputs to one number.

## 4. R1/R4/R5 verification (E2E replay)

`tests/test_strategy_structure_e2e.py` reconstructs era-clean-like signal batches and verifies, deterministically and offline:

1. **R1 router** routes the gold-buy counter-trend case to `mean_reversion`, aligned silver/crypto to `momentum`, consolidating/neutral to `stand_by`, and never routes a neutral signal.
2. **R4 archetype classifier** (mocked `/decision`) parses, and `agrees_with` matches/mismatches the R1 router correctly; vocab is a superset of the registry.
3. **R5 stance** (mocked) forms its own direction (no stated signal) and parses buy/sell/hold.
4. **Reachability-conditioned calibration**: a synthetic era-clean cell set reproduces the known bucket ordering (wide > tight TP-rate; the trending_down gold-buy edge) and the estimator's output is monotonic in reachability — i.e. the reframe's core claim (conditioning changes the number in the right direction) is pinned by a test, not just asserted.

## 5. Honest limits

- The E2E test uses **synthetic** era-clean-shaped data, not a live Supabase pull (the suite must run offline). It pins the *logic and shape*, not the live numbers; the live numbers above are from the 2026-10-11 pulse and will drift.
- The reachability-conditioned estimator is **specced and its shape is tested here**; the live build (replacing `p_win` in EV/Kelly) is the next lever, gated on a scope doc + walk-forward — not started.
- R4/R5 discrimination is **not yet established** (R4 has ~1 stamp, R5 has one basket snapshot). The tests pin the plumbing; the AUC/agreement measurement is the roadmap bar.

## 6. Files

- `regime_platform/sweep_orchestrator/strategy_router.py` (R1)
- `regime_platform/services/decision/clef_flash.py` (R4 `classify_archetype`)
- `regime_platform/sweep_orchestrator/clef_stance.py` (R5 `classify_stance`)
- `config/strategy_registry.yaml` (R0 catalog)
- `tests/test_strategy_structure_e2e.py` (this verification)
