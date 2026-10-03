# 2026-10-02 — Pulse Check: 3 Hardening Gaps for Kairos Urgency Gate

## Scope

Follow-up to `docs/worklog-2026-10-02-masaniello-quality-gate.md`.
Three integration gaps were identified during the pulse check that must be
resolved before the gate can move from shadow to enforcement. All are
tracked in `nt_roadmap` as `kairos_masaniello_urgency_gate` (review: 2026-10-04).

## Item 1 — Regime age proxy

**Current**: `check_urgency` computes `regime_age_hours` as the hours spanned
from the oldest resolved `nt_signal_sim` row in the lookback window to now.
This is a coarse proxy: it measures "how old is the oldest sim record," not
"how long has the current regime cell been active."

**Problem**: A regime cell that flipped 1 hour ago will still show a 45-day
age (from the lookback window), causing the gate to use the **aged** threshold
(1.2) instead of the **young** threshold (2.0). This over-triggers during
healthy regime cycling.

**Fix**: Pass live `KairosRunTracker.to_dict()` from the orchestrator tick
into `check_urgency`. The tracker already computes exact per-symbol age
(`float(now_ts) - cur[1]) / 3600.0`). One-line call-site change in
`orchestrator.py` — deferred pending review.

**Severity**: Medium — over-triggers in shadow mode (logs false positives),
but does not affect production since the gate doesn't write
`nt_symbol_calibration_state`.

## Item 2 — Batch boundary reconstruction

**Current**: Neither `nt_signal_sim` nor the quality gate's
`_reconstruct_batch_state` carry a `batch_num`. Batches are reconstructed by
grouping resolved sims in `ts.asc` insertion order into windows of N=5.

**Problem**: Sweep-light generates signals in batch groups (N=5 per symbol
per tick), so insertion-order grouping is correct for live data. But
re-play runs, backfill scripts, or any non-batch-ordered insertion will
produce misaligned batches — `wins_so_far` and `trade_index` will be wrong,
making M_i meaningless.

**Fix**: Add a `batch_num INTEGER` column to `nt_signal_sim` (DDL change —
requires migration). The position sizer already tracks `batch_num` in
`BatchState`; the sim logging just needs to append it. Without this, the
urgency metric is only valid for live sweep data, not historical validation.

**Alternative (no DDL)**: Use the existing `brick_size` + `session_date` +
`ts` clustering to identify batch boundaries heuristically — fragile but
avoids schema changes. Recommended only as a stopgap.

**Severity**: High — the entire urgency metric is batch-dependent. If batches
are misaligned, M_i values are noise. Must be fixed before trusting urgency
correlations.

## Item 3 — Cron scheduling

**Current**: `check_urgency()` is implemented as a standalone async function
with a `run()` CLI entry point, but is not called from any scheduler.

**Problem**: The gate needs regular invocation to populate
`nt_eod_actions` with shadow data for the 2026-10-04 review. Without a cron
hook, data accumulation is manual and sporadic.

**Fix**: Add a call to `check_urgency(active_symbols)` in the nightly
`eod_correction.run()` pass, alongside `drift_monitor.check_drift` and
`calibration_evidence_pool`. Should use the same `best-effort` pattern (try/
except/log/swallow) so a gate hiccup cannot break the nightly pass.

**Severity**: Low — the gate can be run manually for now; the 48-hour
accumulation period is short enough to populate manually if needed.

## Summary

| # | Gap | Severity | Fix | Type |
|---|---|---|---|---|
| 1 | Regime age proxy | Medium | Live KairosRunTracker wiring | Code (1 line) |
| 2 | Batch boundary | High | `batch_num` column on nt_signal_sim | DDL + code |
| 3 | Cron scheduling | Low | Add to nightly eod_correction pass | Code (5 lines) |
