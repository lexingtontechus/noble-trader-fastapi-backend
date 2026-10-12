# Asset-class pooled ATR solve + scorecard published-book filter

**Date:** 2026-10-12
**Scope:** Two harmony fixes. (1) The per-symbol ATR solve fails for 13/14 symbols (2-3 paths each < `min_signals` 80), so the dynamic-ATR geometry never ships — fall back to an **asset-class pooled** pick that solves once per class and each symbol inherits. (2) The scorecard / calibration bias reads the **whole** population (962 suppressed : 38 published); make the **published book** (`qualified=true`) the standard, keep the full population as a sanity stratifier.
**Status:** Built, E2E + replay, deployed.
**Roadmap:** geometry harmony (B1) + scorecard standard (B2).

---

## 1. Asset-class pooled ATR (B1)

### The gap (proven by replay)
`_refresh_dynamic_atr` (dynamic_tier_optimizer.py:182) calls `dynamic_atr_pick(asset_class, symbol=symbol)` — **per-symbol** — for every peer. `dynamic_atr_pick` already supports pooling (when `symbol=None` it walks the whole class, line 810), but the refresh never uses it. Result: each symbol solves on 2-3 paths, below `min_signals` (80), so 13/14 return `None` → the pick store stays empty → the harmony fix (ATR as single source of truth) fails open to brick for almost every symbol.

### The fix
When the per-symbol pick is `None`, fall back to the **asset-class pooled** pick (`dynamic_atr_pick(asset_class, symbol=None, use_store=False)`), solved **once per class** and cached, then written under each symbol in that class. The pooled solve has enough paths (forex ~7 symbols × signals) to clear `min_signals`. Source is tagged so the store shows `pooled:<class>` vs per-symbol. Fail-open chain preserved: per-symbol → pooled → brick.

### What it does NOT fix
Pooling assumes the class shares a bracket geometry. Forex majors are similar; metals-vs-equity are not pooled (separate classes). The pooled pick is a **fallback**, not a replacement for per-symbol once volume grows. The artifact gates (consistency, walk-forward, clamps) still apply to the pooled pick — a 12:1 pooled artifact is rejected, failing open to brick.

## 2. Scorecard published-book filter (B2)

### The gap (validated)
`nt_calibration_records` is fed by the resolver's `_fetch_pending` which queries `outcome IS NULL` with **no `qualified` filter**, and `log_signal_sim` writes every signal. So the bias/scorecard reads the whole population: 962 suppressed : 38 published (validated 2026-10-12). The published book is the standard users experience; the full population is a diagnostic.

### The fix
The calibration **bias** computation (`fetch_calibration_bias`) and the scorecard read `qualified=true` records as the standard. The full population stays available as a stratifier (the sanity check that finds underlying issues). This is a read-path change — no new writes; the resolver still resolves everything, but the *standard metric* filters to published.

Note: `nt_calibration_records.signal_id → nt_signal_sim.qualified` linkage was found broken (join returns None), so the filter joins through `nt_signal_sim` on `signal_id`. Fixed as part of this build.

## 3. E2E + replay

`tests/test_pooled_atr_fallback.py`: per-symbol None → pooled pick returned and stored; pooled solved once per class (cache); per-symbol pick preferred when it exists; artifact gates still apply to pooled. `tests/test_scorecard_published_filter.py`: bias reads qualified=true only; full population available as stratifier; the signal_id→qualified join resolves.

## 4. Files

- `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py` — pooled fallback in `_refresh_dynamic_atr`.
- `regime_platform/sweep_orchestrator/supabase_client.py` — `fetch_calibration_bias` published-book filter + join fix.
- `tests/test_pooled_atr_fallback.py`, `tests/test_scorecard_published_filter.py`.
- `worklog/20261012_POOLED_ATR_AND_SCORECARD_FILTER.md` — this.
