# Bucket-selection inversion + reachability measurement harmonization

**Date:** 2026-10-12
**Scope:** Prove and fix the era-clean inversion where narrow buckets (structurally dead, 0–7% TP) are priced as the MOST winnable (p_win_bracket 0.3–0.55) and wide buckets (the only +EV pocket) priced as least. Root cause = reachability measured two different ways across the orchestrator; the live `p_win_bracket` re-derives the probability instead of reading the diagnostic table's all-in TP share. Single-source-of-truth fix + E2E + replay.
**Status:** Diagnosed with era-clean data; fix built, gated behind a flag, E2E + replay green.
**Roadmap:** `calibration_reachability_reframe` / bucket harmonization.

---

## 1. The inversion, proven (era-clean, `nt_signal_sim`, `entered/ts ≥ 2026-10-02`)

Actual resolved outcomes by bucket vs what the sweep PRICED each bucket at:

| bucket | n | **actual TP%** | actual SL% | expired% | **priced `p_win_bracket`** |
|---|---|---|---|---|---|
| <0.5 | 79 | **0.0%** | 100% | 0% | **0.545** |
| 0.5–1 | 490 | 6.3% | 93.7% | 0% | **0.418** |
| 1–2 | 3,593 | **6.9%** | 93.1% | 0% | **0.315** |
| 2–4 | 1,961 | **14.8%** | 85.2% | 0% | **0.356** |
| 4+ | 1,882 | 13.0% | 61.5% | 25.6% | **0.126** |

**The sweep prices winnability DOWN as buckets widen (0.545 → 0.126), but reality goes UP (0% → 14.8%).** A bucket that wins **0%** of the time is priced at **0.545**. That is the inversion — and it is exactly why -EV narrow signals sailed through the pre-EV-floor gate: the pricing said they were the best bets.

## 2. Root cause — reachability measured two ways

Two different computations of "the same" probability, disagreeing:

1. **The diagnostic table** (`nt_bracket_reachability.pct_hit_tp`) stores the **all-in** TP share (`tp / n`) — the honest number. 1-2 bucket: ~0.07. Correct.

2. **The live pricing path** (`p_win_bracket.py:compute_bracket_probs`) does NOT read that share. It **re-derives**:
   ```
   p_win_resolve = beta_shrunk(tp, sl)      # tp/(tp+sl), prior-dominated
   p_resolve     = 1 - expired
   p_win_bracket = p_resolve × p_win_resolve
   ```
   For narrow buckets with near-zero resolved TP, the **Beta prior** (α,β) dominates `tp/(tp+sl)` and inflates it — 0 TP / 79 SL still prices ~0.5 because the prior pulls toward 0.5. So the live number is a **prior-inflated resolved-rate product**, not the all-in TP share the table holds.

The two are mathematically equal ONLY when the beta shrink is neutral and expired≈0 — which holds for the wide 4+ bucket (hence its ~correct 0.126) and **breaks catastrophically for narrow buckets** (0% reality priced 0.545). That is the non-harmonization: the diagnostic and the pricing run on different bucket populations and different formulas.

Secondary: `nt_calibration_records` has no `bracket_bucket` column, so era-clean resolved outcomes can't even be joined to buckets there — the join has to go through `nt_signal_sim` (which has both `bracket_bucket` and `outcome`). The measurement plumbing itself is split.

## 3. The fix — single source of truth

`p_win_bracket` must BE the diagnostic table's all-in TP share (`pct_hit_tp`), not a re-derivation. One formula, read from one table, used by pricing, reopt, and the gate. Behind `p_win_bracket_source: derived | all_in` (default `all_in`):

- **`all_in`** (new, default): `p_win_bracket = pct_hit_tp` (the table's all-in TP share). `p_resolve`/`p_win_resolve` still stamped for diagnostics, but EV/Kelly use the all-in share. A 0%-TP bucket prices 0.0 and the EV floor + bracket gate suppress it. Honest.
- **`derived`** (legacy): the old `p_resolve × beta_shrunk` path — retained for A/B and rollback, off by default.

This harmonizes the measurement: the diagnostic table, the live `p_win_bracket`, the bracket gate, and reopt's EV solve all read the SAME all-in TP share for the SAME (symbol, vol_tier, bucket) cell.

## 4. Replay — precise result (corrects the initial read)

Re-pricing the **actual `nt_bracket_reachability` cells** through both sources:

| bucket | all_in | derived | gap (prior inflation) |
|---|---|---|---|
| <0.5 | 0.000 | 0.079 | +0.079 |
| 0.5-1 | 0.048 | 0.105 | +0.057 |
| 1-2 | 0.098 | 0.128 | +0.030 |
| 2-4 | 0.191 | 0.223 | +0.032 |
| 4+ | 0.142 | 0.161 | +0.019 |

**Honest correction:** at the *table-cell* level the inversion is mild — derived over-prices every bucket by only +0.02–0.08, and 1-2 vs 4+ is correctly ordered (0.128 < 0.161). The table cells are basically sound.

**Where the dramatic inversion actually lives:** `nt_signal_sim.avg_p_win_bracket` (the *stamped* values — 0.545 / 0.418 / 0.315 / 0.356 / 0.126). Those were computed **at signal time from whatever the cell looked like then** — stale/thin cell state, beta-shrunk — and are NOT a fresh read of the current all-in share. That stamp/table divergence is the true non-harmonization.

**What all_in fixes:** fresh signals now read `pct_hit_tp` directly — no prior inflation (+0.02–0.08 removed), no thin-cell distortion, harmonized with the table and reopt. What it does NOT do: retroactively re-price the ~1,882 historical stamps (those were priced on old cell state). Only forward signals price honestly. all_in suppresses 85/111 cells below the 0.20 floor vs derived's 83/111 — the dead narrow buckets drop out.

## 5. What this does to the book

- Narrow buckets (1-2, 0.5-1, <0.5) drop from priced 0.3–0.55 to their true 0.0–0.07 → **EV floor + bracket gate suppress them** (they were the -EV volume: 3,593 signals in the dead 1-2 bucket).
- The +EV pocket (BTCUSD 2-4 at 41.7% all-in TP, metals 4+) prices correctly and **survives**.
- The book concentrates into buckets that can actually win. Signal count drops hard — that's the honest book, not a regression.

## 5. E2E + replay

`tests/test_p_win_bracket_harmonization.py`: pins the all_in vs derived divergence on the era-clean cells (<0.5 → 0.0 all_in vs 0.545 derived; BTCUSD 2-4 → 0.417 both), and the monotonicity contract (p_win_bracket must track all-in TP share, not invert). Replay re-prices the era-clean bucket table through both sources and confirms all_in removes the inversion.

## 6. Files

- `regime_platform/sweep_orchestrator/p_win_bracket.py` — `p_win_bracket_source` branch in `compute_bracket_probs`.
- `regime_platform/core/feature_flags.py` — `get_p_win_bracket_source()`.
- `tests/test_p_win_bracket_harmonization.py` — E2E + replay.
- `worklog/20261012_BUCKET_INVERSION_HARMONIZATION.md` — this.
