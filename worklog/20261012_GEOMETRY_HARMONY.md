# Geometry harmony — ATR pick as single source of truth, brick retired to shadow

**Date:** 2026-10-12
**Scope:** Collapse the two parallel geometry systems (brick-quantised vs reopt-ATR) into ONE. The replay proved 40/40 signals ship brick geometry while reopt optimizes ATR geometry that never reaches the signal — two TP/SL, two buckets, two reachability reads, two EVs, diverging on every tick. This build makes the ATR pick the single source of truth, with a consistency gate that rejects artifact brackets, and demotes brick to a shadow comparison.
**Status:** Built, E2E + replay, deployed behind the consistency gate.
**Roadmap:** geometry harmony (supersedes the brick/ATR split).

---

## 1. The disharmony, proven by replay (2026-10-12)

Reconstructing all 40 resolved signals: brick bracket vs reopt-ATR bracket vs shipped:

| finding | result |
|---|---|
| Shipped bracket matches **BRICK** (brick_size × tp/sl_bricks) | **40 / 40** |
| Shipped bracket matches reopt **ATR** pick | **0 / 40** |
| Bucket would **flip** if ATR geometry were used | **40 / 40** |
| reopt pick store populated | **1 / 14 symbols** (XAGUSD only) |
| That one pick flagged by its own consistency check | every cycle |

**Two geometries, neither harmonized:**
- `_compute_trade_signal` (orchestrator.py:687) builds TP/SL as `entry ± brick_size × tp/sl_bricks`.
- reopt / `select_tp_sl` (tiered_atr.py:479) solves TP/SL in **ATR units** (`entry ± tp_atr/sl_atr × atr`).
- The bridge (`dynamic_atr_light_mode`, orchestrator.py:4157) is **`shadow`** — computes the ATR bracket, logs "would apply," **keeps brick**.

So the bucket (`_bucket_for(tp_distance/atr)`), the reachability cell, `p_win_bracket`, and EV are all computed on the **brick** geometry, while reopt optimizes a **different** bracket that ships to nothing. Every downstream metric inherits whichever geometry happened to be priced. That is the disharmony — one symbol, two truths.

## 2. Why the pick is mostly absent (and why that's not a toggle)

`solve_free`/`select_tp_sl` need `min_signals` (80) resolvable paths per symbol. The book yields 2-3 per symbol (only 40 of 400 signals have 48 forward bars). So **13/14 symbols produce no pick** → fail-open to brick → brick ships by default, not by choice. The ATR path was never actually reachable at current volume.

## 3. The artifact problem (why "just promote to live" is unsafe)

The one symbol that solves (BTCUSD) produces `tp=9.5 ATR, sl=0.75 ATR` — a **12:1 reward:risk**, wr 0.42, ev +3.5 on the intrabar envelope. A 0.75-ATR stop almost never trips intrabar-first, so the envelope reports a WR real fills never see. The solver exploits `sl_first` + intrabar wicks. `select_tp_sl` has clamp bands (`tp_atr_min/max`, `sl_atr_min/max`) that would reject this — but the **store-write path doesn't apply them**, so the artifact lands in the store. The consistency check (`CONSISTENCY shadow — pick contradicts measured`) flags it every cycle and does nothing.

## 4. The harmony fix

**One geometry source: the ATR pick. Brick becomes shadow-only.**

1. **Apply the clamp bands at the store-write** (`write_pick_to_store`): reject any pick outside `[tp_atr_min, tp_atr_max] × [sl_atr_min, sl_atr_max]` before it persists. The 12:1 artifact (tp 9.5) fails `tp_atr_max` → never stored → symbol fails-open cleanly instead of shipping an untradeable bracket.
2. **Promote `dynamic_atr_light_mode` to `live`**: when a (clamped, consistent) pick exists, the signal ships the ATR bracket. When absent, fail-open to brick — but now brick is the *documented fallback*, not a silent parallel system.
3. **Consistency gate blocks, not logs**: a pick whose walk-forward WR contradicts the measured bucket by > 0.20 is rejected before store-write (the check already computes the delta; it now returns None instead of stamping).
4. **One bucket, one reachability, one EV**: because the signal's TP/SL is now the ATR bracket, `_bucket_for`, the reachability lookup, `p_win_bracket`, and EV all read the SAME geometry the signal trades. The 40/40 bucket-flip divergence disappears.

Fail-open is preserved and now honest: no valid pick → brick fallback, logged as fallback (not "shadow would-apply"). The replay becomes the acceptance test: after the fix, shipped bracket should match the ATR pick wherever a pick exists, and the bucket should not flip.

## 5. What this does NOT fix (stated plainly)

- **The underlying no-edge problem.** Both brick and ATR geometry lose at ~10-19% WR on this book. Harmonizing the measurement makes the two systems agree; it does not create edge. The replay showed the ATR solver, when it runs, produces artifacts — so the honest near-term outcome is *more* fail-open to brick, not a sudden +EV book.
- **Per-symbol solve feasibility.** 80 paths/symbol is unreachable at current volume. The clamp gate makes the failure clean (reject, don't store garbage); pooling to asset-class is a separate lever (out of scope here, noted for the roadmap).

## 6. E2E + replay

`tests/test_geometry_harmony.py`: the clamp gate rejects the 12:1 artifact; a sane pick (tp 2-4 / sl 1-2) passes and persists; the consistency gate rejects a pick whose measured WR diverges > 0.20; fail-open returns None (brick fallback) when no valid pick. Replay (offline, `nt_signal_sim`): shipped-bracket-matches-ATR where a pick exists, bucket no longer flips.

## 7. Files

- `regime_platform/sweep_orchestrator/tiered_atr.py` — clamp bands + consistency gate in `write_pick_to_store`.
- `regime_platform/core/feature_flags.py` — `dynamic_atr_light_mode` default `live`.
- `tests/test_geometry_harmony.py` — E2E.
- `worklog/20261012_GEOMETRY_HARMONY.md` — this.
