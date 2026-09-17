# Reachability bootstrap — unblock p_win_bracket shadow

> **Priority**: P0 measurement · **Status**: built (apply migration 155 + redeploy Studio) · **Date**: 2026-09-16  
> **Parent**: `worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md` Batch C follow-up  
> **Trigger**: Post-deploy check showed `p_win_dir`/`predicted_p_win_raw` 100% capture but **`p_win_bracket` always NULL** because `nt_bracket_reachability.n_stored = 0` on all ~93k historical buckets (migration 153 defaults; table never rebuilt after column add).

## Problem

| Layer | State |
|---|---|
| Batch C lookup | Requires `n_stored ≥ 3` before returning a probability |
| Reachability table | `n=93388` but `n_stored=0`, `n_proxy=0` on every row |
| Result | Geometry tags write; bracket probability never computes; shadow gate inert |

Root cause is **bootstrap**, not a Studio crash: migration 153 added `n_stored`/`n_proxy` at 0; the existing bucket rows predate the counters. A rebuild with current code would populate them — it has not run (or did not persist) since 153.

## Scope

1. **Migration 155** — backfill `n_stored = n` where `atr_source = 'stored'` and `n_stored = 0` (legacy rows that were labeled stored).
2. **Rebuild after EOD** — ensure `_run_eod_lookback` always runs `build_reachability_table` after resolve (already scheduled separately — wire explicitly into lookback for ordering).
3. **Shadow lookup fallback** — if `n_stored < min_n_stored` but `n ≥ min_n` and `atr_source = 'stored'`, compute using `n` and stamp `atr_source_used='legacy_stored'`. Never use `proxy_current` for the fallback.

## Non-goals

- No `p_win_bracket_mode` flip to live.
- No change to settlement, gates, or EV blend.

## Proof

- Unit tests for fallback vs stored-only path.
- After apply: live query shows `n_stored > 0` on some buckets; new `nt_signal_sim` rows can get non-null `p_win_bracket` in shadow.

## Build completion (2026-09-16)

| Piece | Path |
|---|---|
| Backfill | `supabase/migrations/155_reachability_n_stored_backfill.sql` |
| Daily rebuild | `_run_eod_lookback` calls `_run_bracket_reachability` after resolve+correction |
| Shadow fallback | `p_win_bracket.compute_bracket_probs` — `legacy_stored` when `n_stored` thin, `n` large, row tagged stored; **never** for `proxy_current` |
| Tests | `tests/test_p_win_bracket.py` |

```
pytest tests/ → 1047 passed, 21 skipped
```

### Deploy steps

1. Apply migration **155** in Supabase SQL editor.
2. Redeploy/restart Studio orchestrator from this commit.
3. Optional immediate rebuild:  
   `python -m regime_platform.sweep_orchestrator.bracket_reachability`  
   or wait for next EOD lookback (01:17 UTC).
4. Verify: `n_stored` sum &gt; 0; new sim rows may get non-null `p_win_bracket`.
