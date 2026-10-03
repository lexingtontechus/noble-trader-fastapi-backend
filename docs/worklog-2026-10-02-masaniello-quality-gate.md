# 2026-10-02 — Masaniello Urgency as a Batch-Management Quality Gate

## Context

The repo's `p_win` blend scored against `outcome='tp_hit'` showed AUC ≈ 0.487
(REL 0.062 vs RES 0.001) — below chance on the bracket settlement event.
`calibration_decomp.py` (written 2026-09-24) correctly attributed this to the
**category error (E1)**: `p_win` is a *directional* confidence, but
`tp_hit` is a *bracket/first-passage* event. Scoring `p_win` against
`drift_24h_pct > 0` (the directional label) yields AUC ≈ 0.542 with REL <
RES — the model has real resolution, it was being scored against the wrong
label.

What remained unaddressed: **how does this calibration failure distribute
across Masaniello batch time?** When a batch falls behind on win
accumulation, M_i rises and the sizer increases position size (the
`(0.5 + M_i)` term). If p_win is overconfident *and* the batch is behind,
the two effects compound — inflated confidence × inflated size. We need to
know whether the overconfidence is concentrated in "urgent" batches.

## Reframe

Masaniello's M_i = (W − w) / (N − i + 1) was originally a **sizing**
formula. This work reframes it as a **batch-management** diagnostic:

- **M_i rising** → the batch is "behind" on win accumulation.
- **M_i / M_0 > threshold** → urgency has elevated beyond what the regime
  age justifies.
- **Multiple symbols elevated simultaneously** → systemic degradation, not
  unlucky batches.

The gate is **read-only**: it writes `nt_eod_actions` audit rows
(`regime_degradation_warning`). It does **not** alter the sizer's M_i term,
the Kelly overlay, or any enforcement logic. Masaniello's formula stays
untouched in `position_sizer.py`.

## Changes

### 1. `regime_platform/sweep_orchestrator/calibration_quality_gate.py` (new)

Pure functions (no I/O, unit-testable):

| Function | Purpose |
|---|---|
| `urgency_index(w, i, W, N)` | M_i = (W−w)/(N−i+1), matching `_masaniello_factor` |
| `urgency_drift_ratio(w, l, W, N)` | M_i / M_0 — ratio of current to initial urgency |
| `urgency_threshold(age_hours)` | Bracketed threshold by regime age (young/mature/aged) |
| `check_urgency_batch(states, ages)` | Evaluate all symbols, return `UrgencyReading` list |
| `check_urgency(symbols)` | Async: fetch nt_signal_sim → reconstruct batch → write EOD |

Batch-state reconstruction reads resolved `nt_signal_sim` rows (outcome in
`tp_hit`/`sl_hit`), takes the last N=5 per symbol, counts wins/losses, and
computes `(wins_so_far, losses_so_far, trade_index)`. Regime age is
proxied by the hours span from the oldest resolved sim in the lookback
window (best available without live KairosRunTracker state — documented in
the worklog's open items).

Thresholds:
- **Young** (age ≤ 3h): 2.0 — tolerate noise in a fresh batch
- **Mature** (3h–12h): 1.5 — moderate sensitivity
- **Aged** (>12h): 1.2 — tighten, since edge decay is expected

### 2. `scripts/kairos_masaniello_brier_decomp.py` (new)

Standalone script that combines `calibration_decomp`'s Murphy
decomposition with the urgency lens:

1. Fetches resolved `nt_signal_sim` rows (last 45 days by default).
2. Reconstructs per-trade batch state via `_reconstruct_batches` (groups
   into batches of 5, computes M_i and drift per trade).
3. Scores `p_win`, `predicted_p_win_raw`, `p_win_bracket`, `p_win_dir`
   against both `directional` (drift_24h_pct > 0) and `bracket`
   (outcome == tp_hit) labels using `calibration_decomp.decompose`.
4. Stratifies Brier bins by high-urgency vs on-track batches.
5. Writes markdown report to
   `worklog/kairos/reports/YYYYMMDD_masaniello_brier_decomp.md`.

### 3. No DDL changes

Both artifacts are pure diagnostics. No new tables, no INSERTs into
production schemas. `nt_eod_actions` is an existing audit log table
(multiple writers by design — see `eod_correction.py` and
`drift_monitor.py`).

**Pulse check fix**: the `nt_eod_actions` CHECK constraint (migration 102)
requires `action ∈ {emergency_heavy_sweep, bayesian_penalty, kelly_reduction,
parameter_decay_check}`. The initial implementation wrote `regime_degradation_warning`,
which would be rejected by the constraint. Switched to `parameter_decay_check`
(semantically correct: urgency drift = win-rate decaying faster than the
regime-age bracket justifies) — no DDL change needed.

## Verification

All tests pass (run against project venv on 2026-10-02):

```
1. Pure math tests: PASS
   urgency_index(0, 1, 3, 5) = 0.6    # M_0 — first trade, all wins needed
   urgency_index(1, 2, 3, 5) = 0.5    # 1 win accumulated, 2 needed / 4 left
   urgency_index(2, 3, 3, 5) = 0.333  # 2 wins, 1 needed / 3 left
   urgency_index(3, 3, 3, 5) = 0.0    # target met, no urgency
   urgency_index(0, 5, 3, 5) = 1.5    # 4 losses, 5th trade: 3/1 → clamped to 1.5
   urgency_index(0, 6, 3, 5) = 0.0    # batch exhausted → 0.0

   urgency_drift_ratio(0, 0, 3, 5) = 1.0  # on track
   urgency_drift_ratio(0, 4, 3, 5) = 2.5  # behind (M_i=1.5, M_0=0.6)
   urgency_drift_ratio(3, 0, 3, 5) = 0.0  # target met
   urgency_drift_ratio(0, 5, 3, 5) = 0.0  # batch exhausted

   urgency_threshold(1.0)  = 2.0   # young
   urgency_threshold(6.0)  = 1.5   # mature
   urgency_threshold(15.0) = 1.2  # aged

2. check_urgency_batch tests: PASS
   AAPL (0W/4L, age=1h): drift=2.5, threshold=2.0 → triggered
   TSLA (0W/3L, age=6h): drift=2.5, threshold=1.5 → triggered
   NVDA (3W/0L, age=15h): drift=0.0, threshold=1.2 → not triggered

3. Script batch reconstruction tests: PASS
   9 trades → 2 batches (5+4), wins/losses tracked correctly per trade

4. Brier decomposition identity test: PASS
   brier ≈ rel − res + unc (Murphy identity holds)

5. Label function tests: PASS
   directional: drift_24h_pct > 0 → correct (float(pred), bool)
   bracket: outcome == tp_hit → correct (float(pred), bool)

6. Report generation (synthetic 750-row test): PASS
   All sections present: urgency state, Brier decomp per label,
   stratified by urgency, aggregate verdict → "no better than a constant"
```

Lint: `ruff check` passes on both files.

## What's left (open items)

1. **Regime age source**: Currently proxied by lookback-span from `nt_signal_sim`.
   The in-memory `KairosRunTracker` has the ground-truth run age but is
   per-tick state. Wiring the quality gate into the orchestrator tick (to
   pass live `KairosRunTracker.to_dict()` results) is a one-line change
   at the call site — deferred for review.

2. **Batch boundary reconstruction**: `nt_signal_sim` has no `batch_num`
   column. The script groups by insertion order (ts.asc) into batches of 5.
   This is correct for sweep-light (signals are generated in batch groups)
   but not guaranteed for re-play runs. Adding a `batch_num` column to
   `nt_signal_sim` would make this exact — flagged as a future schema
   question, not implemented (views-only preference).

3. **Cron scheduling**: The async `check_urgency` can be called from the
   nightly cron job alongside `eod_correction` and `drift_monitor`. No
   cron changes were made — the caller decides cadence.
