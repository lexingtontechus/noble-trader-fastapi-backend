# Supabase Timeout/Retry + Log Hygiene (3 fixes + R2) — Build Log

**Date:** 2026-09-23  
**Scope:** Studio `ReadTimeout` / empty `log_regime_entry` warning  
**Status:** Built + tests green. No migration.

## Root cause

Light-sweep fire-and-forget PostgREST writers + **5s** `httpx` timeout.
`ReadTimeout('')` has empty `str(exc)` → `log_regime_entry USDJPY failed: `.
Both paths are diagnostic/analytics (not live gates).

## Fixes

1. **Log format** — `type(exc).__name__: %r` (never bare `%s` on exceptions).
2. **Timeout 5s→15s + one retry** — `log_regime_entry`,
   `record_markov_shadow_compare`, `fetch_calibration_bias_cell`,
   `lookup_bracket_probs`, `upsert_calibration_bias_by_cell`,
   `upsert_live_expectancy_daily` (same pattern as `fetch_macro_regime` 2026-09-20).
3. **Severity** — `record_markov_shadow_compare` failures are **WARNING**
   (diagnostic miss is acceptable), not ERROR. HTTP upsert fail also WARNING.

## R2 paths hardened

- `p_win_bracket.lookup_bracket_probs` — 8s→15s + retry + typed log
- R3 cell fetch + live-expectancy / cell upserts — same

## Tests

`tests/test_supabase_timeout_retry.py` (source guards) + prior R2/R3 suites: **37 passed**.
