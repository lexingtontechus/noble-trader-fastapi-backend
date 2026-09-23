# Geometry forward-test / incubation gate — build

**2026-09-23**

## Problem

Heavy sweep picks a `(brick_size, sl_bricks, tp_bricks)` combo from a
single in-sample 90-day grid backtest (`heavy_sweep.py`'s
`_renko_grid_search_sync` → `pick_best_combo`) and writes it straight to
`config/brick_params.yaml` via `write_brick_params()` — documented as
"the SOLE writer" — for the very next light-sweep tick (≤15 min later) to
trade live. There has never been a held-out / out-of-sample check before
promotion. `tests/test_renko_backtest.py` still references a
`walk_forward()` method that was removed in v7.3 and never rebuilt —
confirmed by grep, not replaced by anything since.

This is the platform's most-scoped gap versus e.g. AITradingArena's
"candidate → forward-test on unseen window → then it counts" discipline,
and it is orthogonal to (and should ship ahead of) any position-sizing
work — parked the Masaniello batch-gate idea per user decision to "test
this first and see some results" before adding sizing logic on top.

## What shipped

Same off → shadow → live convention as every other flag in
`config/feature_flags.yaml`. **Landed in `shadow` — zero live behavior
change today.**

1. **Migration 165** (`nt_geometry_incubation`): one row per candidate
   pick. Partial unique index enforces at most one `status='incubating'`
   row per symbol at a time. Tracks `n_resolved/wins/losses/expired`,
   `expectancy_r`, `pct_expired`, and `baseline_pct_expired` (the
   previous live combo's own expiry rate, for the regression check).

2. **`geometry_incubation.py`** — pure math + async orchestration:
   - `compute_forward_stats(rows, sl_bricks, tp_bricks) -> ForwardStats`
     — aggregates forward-RESOLVED `nt_signal_sim` rows (outcome ∈
     tp_hit/sl_hit/expired) into win/loss/expired counts,
     `pct_expired`, and `expectancy_r = win_rate·RR − (1−win_rate)`
     using the candidate's own designed RR (same formula shape as
     `pick_best_combo`'s in-sample scoring, but computed on genuinely
     forward data).
   - `evaluate_promotion(...) -> PromotionVerdict` — pure decision
     function, ordered gates:
     1. `days_elapsed < min_days` (default 7 — a calendar-week floor,
        deliberately independent of sweep cadence: heavy sweep now runs
        DAILY (`nt_symbol.sweep_cron` default `'0 20 * * *'`, changed
        from weekly 2026-09-08 migration 142), so tying incubation to
        "next sweep run" would only be 1 day — too thin a forward
        sample) → hold.
     2. `n_resolved < min_resolved` (default 20 — matches existing
        calibration min-episode conventions) → hold.
     3. `expectancy_r <= 0` on the forward sample → **reject outright**,
        regardless of sample size — a candidate already losing money
        out-of-sample should never wait for more samples to "confirm"
        it.
     4. `pct_expired` regressed more than
        `geometry_incubation_max_expiry_regression` (default 0.10)
        versus the baseline it would replace → **reject** — never trade
        a bracket that resolves worse than the one already live.
     5. Otherwise → **promote**.
   - `start_incubation()` / `reconcile_incubations()` — best-effort
     Supabase I/O, same resilience pattern as `paper_book.py` (never
     raises, per-row try/except, logs and moves on).
   - `fetch_baseline_pct_expired()` — looks up the symbol's *previous*
     live combo's ATR-at-write (`nt_brick_params_state`) → bucket →
     pooled (`signal='all'`) `nt_bracket_reachability.pct_expired`, so
     the regression check has something concrete to compare against.
     Returns `None` (skips the regression gate, never blocks) when
     there's no prior geometry — a brand-new symbol has nothing to
     regress against.

3. **`heavy_sweep.py` wiring** (step 7, right where `write_brick_params`
   was previously called unconditionally):
   - `off`: unchanged, write straight to YAML (available as a full
     kill-switch; **not** the shipped default — see below).
   - `shadow` (**shipped default**, per user's "let's test this first"):
     **still writes straight to YAML immediately** (identical
     live behavior) — additionally calls `start_incubation()` so every
     future heavy-sweep pick accumulates a tracked promote/reject
     verdict for review before anything gates.
   - `live`: a new candidate is held as `incubating` (skips the direct
     YAML write) **unless** the symbol has no prior live geometry at all
     (bootstrap case — nothing to protect, so straight to YAML like
     `off`, otherwise a brand-new symbol would sit on hardcoded defaults
     for a full incubation window with zero downside risk in exchange).
   - Any exception in the incubation path falls back to the direct YAML
     write (fail-open — a forward-test bug must never leave a symbol
     without any live geometry).

4. **Feature flags** (`config/feature_flags.yaml` + `feature_flags.py`
   getters, hot-reloaded on mtime change like every other flag):
   - `geometry_incubation_mode: "shadow"` (shipped default)
   - `geometry_incubation_min_days: 7`
   - `geometry_incubation_min_resolved: 20`
   - `geometry_incubation_max_expiry_regression: 0.10`

## Rollout plan

1. **Now (shadow)**: every heavy-sweep pick — starting with today's
   daily 20:00 UTC run (heavy sweep is DAILY, not weekly — changed
   2026-09-08 migration 142; AGENTS.md's "Sunday 20:00 UTC" line
   predates that migration and is stale) — gets a tracked
   `nt_geometry_incubation` row. YAML writes are completely unaffected.
2. **After ~7-10 days** (long enough for at least one full incubation
   window per symbol to actually clear `min_days`/`min_resolved` against
   daily forward evidence): review the accumulated verdicts —
   `SELECT status, reason, count(*) FROM nt_geometry_incubation GROUP BY
   status, reason` — to sanity check the promote/reject rate isn't
   degenerate (e.g. everything rejecting = thresholds too strict for
   real signal volume; everything promoting instantly = thresholds too
   loose to matter).
3. Only then consider flipping `geometry_incubation_mode: live`,
   symbol-by-symbol or globally, per the same "reject rate review before
   flip" discipline already used for `light_publish_geometry_gate`
   (migration 164's comment: "if reject rate >~0.70 tune the cap rather
   than flipping live").

## Not built (parked per user decision)

- **Masaniello batch-completion as a promotion trigger.** The
  `masaniello_bridge.py` / `position_sizer.py` batch primitives
  (`batch_size`, `target_wins`, `BatchState`) already exist and could in
  principle gate promotion on "N consecutive batches clearing target
  wins" instead of / in addition to the day+sample-count gates built
  here — parked until the day/sample-count version above has real data
  to look at.
- **`heavy_sweep_grid.min_trades` raise (5 → ~100)**: separate, smaller
  lever flagged in the prior comparison writeup (in-sample backtest
  trade-count floor, distinct from this forward-test layer's
  `geometry_incubation_min_resolved`). Not touched in this build — can
  be a one-line follow-up.

## Verification

- New tests: `tests/test_geometry_incubation.py` — 11 tests, pure
  functions only (`compute_forward_stats`, `evaluate_promotion`), no I/O
  mocking needed since the async Supabase helpers are deliberately kept
  separate from the pure math.
- Full suite: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m
  pytest tests/ -q -p no:cacheprovider` → **1139 passed, 21 skipped, 0
  failed** (pre-existing skips unrelated to this change).
