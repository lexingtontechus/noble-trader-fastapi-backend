# Reopt ↔ clamp harmonization — the forex band is gated by a stale clamp

**Date:** 2026-10-10
**Scope:** `atr_reopt` proposes a wide forex bracket (`tp=4.52/sl=2.75`) but marks it `drift=False`, so it never reaches the proposal/apply path. The picker meanwhile is bound by the tight `by_symbol.FOREX` clamp (`tp_max 1.85`) and returns `None`. The two read **different config layers** for forex.
**Status:** Fix A + Fix B BUILT, deployed, tests pass. Apply-target still open.

---

## 1. Symptom (live, 2026-10-10 04:00 UTC reopt)

```
| forex | 4.52/2.75 · RR 1.64 · wr 0.404 · EV +0.086 · n 6506 | drift=False | — |
```

Reopt's free-lattice solve on the **live intrabar basis** (n=6506) finds forex's best cell is **wide** (`tp=4.52/sl=2.75`), positive-EV, wr 0.404. But `drift=False` → not proposed → the tight `by_symbol.FOREX` band stays frozen → the picker (bound by that band) returns `None` for every forex symbol → the walk-forward gate has nothing to validate.

## 2. Root cause — TWO divergent forex bands; reopt and picker read different ones

First, clearing a wrong turn: reopt **already measures on the live basis** (`atr_reopt._paths_for` line 300-304 builds envelopes via `build_envelopes(..., basis=live_basis())`, intrabar/sl_first — fixed in Stage 1). And the tight band is **not** a close-walk artifact. Both prior hypotheses were wrong.

The real defect, confirmed live on the Studio (2026-10-10):

| call | resolves | forex tp_max |
|---|---|---|
| `get_dynamic_atr_config('forex', None)` (reopt, `compare_to_current`) | `by_class.forex` | **5.02** (wide) |
| `get_dynamic_atr_config('forex', 'USDJPY')` (picker, `dynamic_atr_pick`) | `by_symbol.FOREX` | **1.85** (tight) |

There are **two forex bands in the config that disagree**: `by_class.forex` (wide, `tp_max 5.02`) and `by_symbol.FOREX` (tight, `tp_max 1.85`). The precedence chain is `by_symbol → by_class → global`, so:
- the **picker** (always passes a symbol) hits `by_symbol.FOREX` → tight 1.85 → returns `None` for every forex symbol,
- **reopt**'s forex scope passes `symbol=None` → falls to `by_class.forex` → wide 5.02 → the wide solve `4.52` reads as in-band → `drift=False` → never proposed.

So reopt's solve (4.52/2.75) looks "stable" against the wide class band while the picker is strangled by the tight symbol band. **Two bands, two readers, no agreement.** (Note: locally the two happen to resolve to the same 1.85, which is why a naive local test can't reproduce it — the divergence is in the Studio's `by_class.forex` block, line 641.)

## 3. Why it matters (the "same values read consistently" question)

Your question — "is this a harmonization issue, same values read consistently through the orchestrator?" — **yes, and this is precisely it.** The `dynamic_atr` config has three layers (global / by_class / by_symbol) with a precedence chain, and reopt was comparing against the wrong layer for the pooled forex scope. The fix is to make reopt read the **same layer the picker applies** for forex — i.e. resolve the FOREX override in `compare_to_current`.

## 4. Secondary finding — the stale-clamp hypothesis (partially right)

The prior turn's hypothesis ("tight forex was optimal under the old 48h close-walk; now stale") is **directionally right but not the mechanism**. The `by_symbol.FOREX` band (`tp_max 1.85`) was set in an earlier era and has not moved because reopt's drift check never compared against it. Whether 1.85 or 4.52 is *actually* better on the clean post-cutover basis is an open empirical question — reopt's own solve says **4.52 (EV +0.086, wr 0.404)** on n=6506. But n=6506 spans the pre-cutover era, so that number is itself era-contaminated (same lesson as the calibration/reachability fix). The honest next step is to re-solve forex on the **era-clean** window before trusting 4.52.

## 5. Prescribed + built fix

**Fix A — config-layer parity (BUILT, 2026-10-10):**
`run_atr_reopt`'s forex scope now passes a forex member symbol to `compare_to_current` (`cmp_symbol = members[0]["symbol"]`), so `get_dynamic_atr_config` resolves the **`by_symbol.FOREX`** override — the same band the picker applies — instead of falling through to the wider `by_class.forex`. Verified live: with a forex member, the `4.52/2.75` solve now flags `drift=True` with `tp 4.52 above clamp 1.85`. Reopt and the picker now read the same forex band by construction.

**Fix B — era-clean the reopt window (BUILT, 2026-10-10):**
Added `_fetch_cutover_ts()` helper and era-clamp in `_load_signals`. When the cutover marker (`nt_calibration_cutover.cutover_at`) is newer than the lookback start, the window is clamped to the cutover date. This ensures the free-lattice solve only sees signals entered under the intrabar/sl_first basis. The 45d lookback that spanned pre-cutover (n=6506, ~all contaminated) is now bounded by the 2026-10-02 cutover. Implemented as fail-open: no cutover marker → full lookback (no silent shrink). **Fix applied, shadow observation pending.**

**Apply target (OPEN, unchanged):** `apply_symbol_clamps` writes `by_symbol.<SYMBOL>`, but forex is pooled under the `FOREX` key. A forex proposal must write the `FOREX` block, not 7 per-symbol blocks. Fix A+B fixes *detection*; the apply half needs the pooled-key target confirmed before reopt can auto-write a forex band.

## 5b. Build record (2026-10-10)

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/atr_reopt.py` | Fix A: forex scope passes `members[0]["symbol"]` to `compare_to_current`. Fix B: added `_fetch_cutover_ts()` + era-clamp in `_load_signals` |
| `tests/test_atr_reopt.py` | `test_forex_drift_flags_against_for_override` — pins the wide solve flags drift vs the FOREX override |
| `worklog/20261010_REOPT_CLAMP_HARMONIZATION.md` | this |

Verified on the Studio: reopt's real `solve_free` → `4.52/2.75`; `compare(symbol=USDJPY)` → `drift=True, tp_max 1.85`. Tests 7/7 pass. **Detection fixed, era-clamp built, apply-target still open.**

## 6. Remaining open question

Fix A makes reopt *propose* the wide forex band. But proposal ≠ apply: `apply_symbol_clamps` writes `by_symbol.<SYMBOL>`, and forex is pooled under the `FOREX` key, not per-symbol. So a forex proposal needs to write the `FOREX` block, not 7 per-symbol blocks. Confirm the intended apply target for the pooled forex scope before building Fix A's apply half.
