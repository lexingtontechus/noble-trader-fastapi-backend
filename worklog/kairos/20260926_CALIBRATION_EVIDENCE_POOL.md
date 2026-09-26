# Calibration evidence pool (Kairos V3 §14.3)

> **Date:** 2026-09-26 · **Branch:** `kairos` → `main`
> **Scope:** `worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md` §14.3 · **Evidence:** `reports/20260926_calibration_tier_split.md`
> **Flag:** `calibration_evidence_pool_mode` (`off | shadow | live`), ships **shadow**; `calibration_evidence_tiers: [qualified, suppressed]`
> **Migration:** `171_calibration_records_tiered.sql`: **view only**, applied 2026-09-26

---

## 1. Why

`eod_correction`'s bias (`fetch_calibration_bias` / `_raw`) counts every TP/SL outcome in
`nt_calibration_records`, whichever tier the signal came from. Gate-failed signals make up 84% of those
outcomes. They win about as often as the other tiers (TP 0.27 vs 0.25–0.26), but their p_win is much
lower, so their error is +0.15 against +0.34 (qualified) and +0.39 (suppressed). Mixing them in
understates how overconfident qualified signals are.

In walk-forward on S2 (the next qualified signal), a qualified + suppressed pool gives Δ Brier −0.118,
against −0.043 for all tiers. It holds in both halves of the month (§14.3).

## 2. View join vs extra column: decision

| | **View join on `signal_id`** (chosen) | Tier column on `nt_calibration_records` (stamped at insert) | Read `nt_sweep_result` |
|---|---|---|---|
| Schema change | view only | `ALTER TABLE` + write-path change + backfill `UPDATE` | none |
| Old rows | covered now: stamp where available, validated reconstruction otherwise | need a backfill | n/a |
| Has the tier? | yes: `nt_signal_sim.gate_outcome` (migration 168) | yes | **no**: it has `qualified` but no `gate_outcome` / suppressed. Per-sweep summary, not per-signal |
| Cost | index lookup on `nt_signal_sim_pkey`: 9.7 ms for one symbol, 7 days (EXPLAIN ANALYZE) | none | – |
| Risk | lost if `nt_signal_sim` rows are deleted before calibration rows | stays even if sim rows are deleted | – |

Coverage check (7 days, 2,711 records):
- **100% have `signal_id`.**
- **100% join to an `nt_signal_sim` row.** Of those, 152 have a stamped tier and 2,557 are
  reconstructed.
- The SQL reconstruction agrees with **2,093 / 2,093** stamped rows.
- Sources: 2,666 light, 45 heavy.

The retention risk doesn't apply here: sim rows are kept for 120 days
(`signal_sim_retention_resolved_days`), and the bias window is 7 days.

**Don't use `nt_sweep_result`.** It has no gate tier, so suppressed can't be separated from gate-failed.
That split is the whole point.

## 3. What changed

| File | Change |
|---|---|
| `supabase/migrations/171_calibration_records_tiered.sql` | `v_calibration_records_tiered` (`security_invoker`): `nt_calibration_records.*` + `evidence_tier` (qualified / suppressed / gate_fail / unknown) + `tier_src` (stamp / recon / missing) + `sim_source`. No table, column or insert. |
| `regime_platform/sweep_orchestrator/supabase_client.py` | `fetch_calibration_bias` and `_raw` gain `evidence_tiers=None`. When set, they read the view with an `evidence_tier=in.(…)` filter. Everything else is identical: window, episodes, decay, floors, classification. `None` keeps the current behaviour for every existing caller. |
| `regime_platform/sweep_orchestrator/eod_correction.py` | `_apply_evidence_pool()` after the all-tier fetch, in both the nightly and the 4h raw passes. **off:** nothing. **shadow:** also compute the pooled bias and log `[eod:pool]`; the all-tier bias still enforces. **live:** the pooled bias enforces, falling back to all-tier when the pool is INSUFFICIENT. Never raises. Comparison rows go to `summary["evidence_pool"]`. |
| `regime_platform/core/feature_flags.py`, `config/feature_flags.yaml` | `calibration_evidence_pool_mode: shadow`, `calibration_evidence_tiers: [qualified, suppressed]`. Invalid tiers fall back to the default, never to all tiers. |
| `tests/test_calibration_evidence_pool.py` | 26 tests. `tests/test_raw_bias_cycle_reset.py` pins pool mode to off, because those tests count fetcher calls on the all-tier path. |

Out of scope, and unchanged: `drift_monitor`, `heavy_sweep`, the orchestrator's per-signal
`fetch_calibration_bias`, and `v_eod_calibration_bias_latest` (the EV blend's `p_historical`). They move
only once the pool goes live and proves itself.

## 4. Smoke test (live DB, raw bias as the 4h pass reads it, 2026-09-26 ~01:05 UTC)

| symbol | all tiers | qualified + suppressed |
|---|---|---|
| XAGUSD | OVERCONFIDENT +0.49 (3 episodes, 517 raw) | OVERCONFIDENT **+0.68** (2 ep, 79 raw) |
| **AAPL** | **CALIBRATED −0.17** (7 ep, 437 raw) | **OVERCONFIDENT +0.43** (8 ep, 44 raw) |
| XAUAUD / XAUUSD / BTCUSD / US500 | OVERCONFIDENT +0.35 to +0.46 | INSUFFICIENT (< 20 raw) |
| USDJPY | CALIBRATED +0.28 | INSUFFICIENT |
| 6 FX majors (5 insufficient) · XAUEUR · US30USD | INSUFFICIENT | INSUFFICIENT |

- **Direction matches the offline finding.** Where the pool has enough evidence, it reads more
  overconfident.
- **AAPL flips bias status.** Gate-failed outcomes are hiding a +0.43 overconfidence in its qualified and
  suppressed signals.
- **The pool is thin.** Only 2/14 symbols pass the 20-raw / 2-episode floor. The Studio floor is
  `EOD_BIAS_MIN_RECORDS=28`, which is stricter still.
- **In live mode today, 12/14 symbols would fall back to all tiers.** This is the same volume problem as
  FX expiry: qualified and suppressed signals resolve rarely under wide brackets. Tighter geometry (§5)
  is what grows this pool.

## 5. Promotion bar (shadow → live)

- **≥ 3 days of `[eod:pool]` lines**, spanning the 09-26 heavy sweep (first live hybrid geometry).
- **Status disagreements are reviewed per symbol** (the AAPL type), with the pooled bias status checked
  against realised qualified outcomes.
- **Pool coverage grows:** ≥ 6/14 symbols are not INSUFFICIENT in the pool. Otherwise live is mostly fallback, and
  flipping it changes little.
- **One lever at a time:** don't flip this in the same read window as `calibration_resolve_hourly_mode`
  or `dynamic_atr_mode`.

**Rollback:** set the flag to `shadow` / `off`. The view is read-only, and dropping it has no effect
while the mode is off or shadow, because the pooled fetch failure is caught.

## 6. Verify after deploy

```bash
ssh noble-trader-unified 'cd /teamspace/studios/this_studio/noble-trader;
  grep -a "\[eod:pool\]" logs/orchestrator.log | tail -14'   # after the next 4h pass (04:30 UTC) or nightly
```
