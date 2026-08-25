# 2026-08-25 — US500 brick/time-horizon mismatch + USDCAD closure: investigation scope

**Status:** Root cause confirmed and numerically verified for both. No code
changed in this pass — scoping only, per standing process (scope → plan →
build). Candidate fixes ranked below; none built or approved yet.

---

## US500 — confirmed root cause

### The mismatch

- Live's resolver (`eod_resolver.py`) enforces a hard **48 real-hour**
  resolution deadline (`EXPIRY_BARS=48`), independent of price behavior.
- The weekly/emergency heavy sweep (`heavy_sweep.py`) picks the
  `(brick_size, sl_bricks, tp_bricks)` combo that maximizes backtested
  Sharpe over 90 days (`pick_best_combo`), using `RiskParams`'s default
  `time_stop_bricks=10` as its only give-up condition. That counts **brick
  formations**, not elapsed time — a brick only forms once price moves a
  full `brick_size`, so "10 bricks" takes as long as it takes.
- `force_close` — the one function in `risk_manager.py` that could act as
  a true wall-clock exit — is defined but **never called anywhere in the
  codebase** (confirmed via repo-wide grep). Dead code.
- `RiskManager.evaluate()` (where any time-based check would run) is only
  reached when a new brick actually forms (`renko/pipeline.py`'s
  `process_tick`) — during a quiet stretch with zero brick formation, no
  time-based check runs *at all*, brick-based or otherwise.
- The backtest itself has no wall-clock: `fetch_candles_for_symbols`
  returns closes only, no timestamps; `TradeResult.hold_duration_seconds`
  is computed but never used by `ComboResult`/`pick_best_combo`.
- `MIN_TRADES=10` (the grid search's only trade-count floor) is a weak
  proxy — a combo averaging ~200 real hours per trade can still clear 10
  trades over a 90-day sample.

### Numerical verification (not just theory)

Simulated actual renko brick formation on the real captured price path
(`nt_signal_sim.entry_price`, 5-min cadence, 2026-08-23 05:10 →
2026-08-25 01:00, 385 ticks, ~44 real hours):

```
min=7651.00, max=7685.68, range=34.68 points
brick_size=28.7  (active for nearly all of this window) → 0 bricks closed
brick_size=14.37 (newly re-tuned ~04:30 UTC 08-25)       → 1 brick closed
```

Genuinely zero bricks closed at the brick size that was live for
essentially the entire window — not a resolver bug, not a data artifact,
a real extreme-quiet stretch for this symbol. Confirms neither the
10-brick time-stop nor a TP/SL touch could plausibly fire within 48 hours
at either brick size tested — today's two emergency re-tunes (28.7→14.37)
narrowed the gap but didn't close it (even 14.37 only produced 1 brick in
44h, nowhere near the 3-10 bricks needed to touch SL or TP).

### Candidate fixes, ranked (background research agent, code-traced)

1. **Volatility floor on brick sizing** (recommended first). Constrain
   `generate_brick_sizes`/the grid search's accepted range so brick_size
   can't be sized so large relative to *recent* (24-48h) realized vol —
   not just the 14-period ATR baked in at last sweep time — that a full
   SL/TP round trip becomes structurally implausible within 48h.
   - Effort: small-medium. Files: `heavy_sweep.py` only (`generate_brick_sizes`,
     `compute_atr`, pre-filter in `_renko_grid_search_sync`), maybe one new
     settings knob.
   - Risk: low-medium — only binds when recent vol has collapsed relative
     to the sweep's ATR; fast-moving symbols unaffected.
   - Closes gap: narrows well, doesn't fully close (still a statistical
     proxy, not a guarantee).

2. **Adaptive per-signal expiry** (recommended second, paired with #1).
   `resolve_signal_outcome` already accepts a parametrized `expiry_bars`
   (`eod_resolver.py:173`) — compute a per-symbol value at signal-push
   time instead of the fixed 48 constant.
   - Effort: medium. Files: `eod_resolver.py` (`_resolve_one`,
     `NT_SIGNAL_SIM_COLUMNS`, `resolve_pending_signals`), signal-sim
     payload builders, a Supabase migration (new column).
   - Risk: medium — changes the calibration timing/denominator for every
     symbol, not just quiet ones; needs care around `EOD_RETENTION_STALE_DAYS`
     semantics (7-day cleanup assumes 48-bar window is a small fraction of
     that).
   - Closes gap: fully closes the "expired without real cause" symptom,
     but is the inverse of #3 below — best paired with #1 so combo
     selection doesn't keep picking structurally-slow combos in the first
     place.

3. **Full wall-clock backtest/live parity** (structurally correct,
   defer). Feed real timestamps through `fetch_candles_for_symbols` →
   `RenkoPipeline.backtest_with_analytics`, and move risk-check evaluation
   from brick-triggered to tick-triggered so a genuine wall-clock time-stop
   can run even when no brick has formed.
   - Effort: medium-large. Files: `price_fetcher.py`, `renko/pipeline.py`
     (`process_tick`, `backtest`), `renko/risk_manager.py`
     (`_check_time_stop`), `heavy_sweep.py` (`ComboResult`,
     `pick_best_combo`).
   - Risk: medium-high — touches the core live/backtest-must-behave-
     identically pipeline (an explicit design invariant in
     `pipeline.py`'s docstring). Bug here risks changing live risk
     behavior for every symbol, not just quiet ones.
   - Closes gap: fully — the only option that makes backtest selection
     structurally identical to what live enforces. Right long-term target,
     wrong thing to lead with on an actively-trading system.

4. **Volatility-drift-triggered re-sweeps** (supplementary only). Trigger
   an emergency sweep when realized vol drops sharply below what was
   assumed at last sweep, rather than waiting for the weekly cron.
   - Effort: medium. Doesn't fix the selection logic itself — just reacts
     faster. Not a substitute for 1-3.

**Not yet decided**: whether to build 1+2 now or wait for more of Change
A's stability window to close first (same "don't compound fixes" scoping
discipline as the rest of this session).

---

## USDCAD — historical episode explained, no new fix needed

Two separate threads were originally tangled together as "USDCAD ~1% win
rate":

### Thread 1: Aug 6-12 catastrophe (historical, closed)

1,413 `buy` signals resolved 100% as `sl_hit` (zero `tp_hit`) over a full
week, despite averaging 98% model-predicted win probability. Regime data
for the same window:

```
Aug 06-11: regime=high_vol_strong_bull, confidence=1.0 (pinned), 1,000+ signals
Aug 12:    regime flips to high_vol_strong_bear, confidence=1.0 (pinned)
```

Confidence pinned at the maximum for 6 straight days while price actually
fell — the textbook degenerate/non-convergent HMM signature (model locked
in the wrong regime at artificially maximal confidence). This is the same
failure class **Change A** (RegimeHMM feature standardization, commit
`5bf94b0`, deployed 2026-08-24) was built to fix, ~12-18 days before that
fix shipped. No new fix needed — this predates Change A and is explained
by it retroactively. Action: a quick spot-check once Change A's
observation window closes, confirming this exact "pinned confidence for
days, wrong direction" pattern hasn't recurred anywhere in the fleet —
not urgent, not blocking.

### Thread 2: current flag (tonight, folds into US500's fix)

The `n=69, OVERCONFIDENT, tier=1` flag from tonight's 04:30 UTC cycle is
driven by a **frozen snapshot**: 69 losses, zero wins, all dated
2026-08-23 00:40-06:35 — 22+ hours stale, zero new calibration records of
any outcome since. This is the same expired-dominant, brick-timeout
pattern as US500, not an active directional-accuracy bug. **No separate
fix track required** — whatever fix ships for US500 (candidates above)
should be evaluated against USDCAD too before/alongside deployment, since
both symbols hit the same structural gap.
