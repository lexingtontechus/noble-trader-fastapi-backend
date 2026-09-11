# Longitudinal Context Enrichment & Brick Geometry Review

> **Status: REVIEW ONLY, Parts 1-8 — Part 9 corrects a factual error in
> Part 5, Part 10 is a new build-ready scope. Nothing built yet.**
> Per "scope in worklog, get honest review, build only on explicit
> instruction." Parts 1-8 map current data flows and propose a
> longitudinal enrichment framework; Part 9 (2026-09-11) verifies Part
> 5's central claim against the live code and finds it does not hold;
> Part 10 (2026-09-11) fully scopes Part 3's Tier 1 (the highest-leverage
> item that survives the correction) as a concrete, buildable gate.

| # | Priority | Task | Status | Date | Roadmap refs |
|---|---|---|---|---|---|
| P1-8 | P1 | Longitudinal enrichment framework + brick geometry review | n/a (review only) | 2026-09-10 | — |
| P9 | P1 | Verify Part 5's brick-geometry-mismatch claim against live code | resolved — claim does not hold | 2026-09-11 | — |
| P10 | P1 | Scope Tier 1 (context-alignment gate) as a buildable, EV-engine-safe mechanism | blocked (scope only, awaiting approval) | 2026-09-11 | RM-45 |
| P11 | P2 | Scope dynamic/regime-conditional MIN_RR | blocked (scope only, awaiting approval) | 2026-09-11 | RM-46 |
| P12 | P2 | Scope TimesFM k-adaptive dampening | blocked (scope only, awaiting approval; real sequencing complexity found) | 2026-09-11 | RM-47 |
| P13 | P1 | Build the Part 10 context-alignment gate | resolved (built, tested, shipped off) | 2026-09-11 | RM-45 |

## Session Summary

A three-part theoretical discussion covering: (1) how longitudinal data
(90-day lookback, daily heavy sweep, medium/long-term trend filters) can
enrich the 5-minute light sweep when the market is choppy or consolidating;
(2) how that enrichment impacts calibration and scoring accuracy for
entry/SL/TP price determination; (3) the critical brick geometry mismatch
between heavy-sweep-optimized parameters and light-sweep execution.

---

## Part 1 — Current Architecture Snapshot

### Signal pipeline (as documented in AGENTS.md and confirmed in code)

```
TradingView batch (1h candles, 168 bars) → Renko bricks → HMM regime → TimesFM forecast → EV Engine v5 → 7 gates → QUALIFIED
   ──► Discord (direct delivery)
```

### The EV Engine v5 blend

4-source logit-pooled `P_win` (per `ev_engine.py`):
- `p_regime` = 0.25 — HMM 4-state regime posterior
- `p_markov_hold_n` = 0.20 — T^N multi-step hold probability
- `p_timesfm` = 0.25 — TimesFM foundation model forecast
- `p_historical` = 0.30 — Bayesian-shrunk realized win rate (NOT fed by any production caller as of 2026-09-09; renormalizes out)

When a source is unavailable, the engine renormalizes remaining weights to
sum to 1.0 — it does NOT substitute 0.5 (which would dilute signal).

### The 7-gate qualification chain (in `orchestrator.py:_process_symbol`)

1. `signal_qualifiable` — trade_signal is not "neutral"/"unknown"
2. `regime_conf_ok` — `regime_conf >= MIN_REGIME_CONF`
3. `kelly_min_ok` — `effective_kelly >= MIN_KELLY`
4. `timesfm_available` — p_timesfm is not None (informational in soft/off)
5. `timesfm_confidence_ok` — TIMESFM_GATE_MODE-dependent
6. `ev_allowed` — `effective_ev_result.allowed`
7. `p_win_ok` — p_win >= MIN_P_WIN (soft band 0.50–MIN_P_WIN publishes with kelly shrink)

### Data timeframes in the system

| Component | Timeframe | Bars | Frequency | File/Function |
|---|---|---|---|---|
| Light sweep | 1h candles | 200 (`LIGHT_CANDLE_BARS`) | Every 5 min | `price_fetcher.py:fetch_tradingview_batch(n_bars=200)` |
| Heavy sweep | 1h candles | 2160 (`SWEEP_CANDLE_HOURS = 90d × 24h`) | Daily 20:00 UTC | `heavy_sweep.py` |
| Trend filter | Daily bars | ~200 (SMA-50 + buffer) | Daily refresh | `trend_filter.py:compute_trend_regime()` |
| Macro filter | Daily bars | ~200+ (SMA-200 + buffer) | Daily refresh | `macro_filter.py:compute_macro_regime()` |
| EOD resolver | 1h bars | 48 (`EXPIRY_BARS`) | Daily 00:30 UTC | `eod_resolver.py` |
| EOD correction | Rolling bias | 7 days, min 20 records | 4h passes | `eod_correction.py` |
| Drift monitor | Rolling bias | 48h rolling | Every light sweep | `drift_monitor.py` |

### Brick parameter flow

1. Heavy sweep runs grid search over 8 brick sizes × 3 SL × 6 TP (144 combos)
   using `RenkoPipeline.backtest_with_analytics()` on 2160 hourly bars
2. `pick_best_combo()` selects the best combo by Sharpe (MIN_RR/MAX_SHARPE/MIN_TRADES filters)
3. Result written to `config/brick_params.yaml` via `_persist_brick_params_to_yaml()`
4. Light sweep reads brick params from YAML at `_load_brick_params()` (line 3904-3916)
5. These brick params feed `_process_symbol()` at line 2732:
   `brick_size, sl_bricks, tp_bricks = await self._load_brick_params(symbol)`

### Key constants (confirmed in code)

- `LIGHT_CANDLE_BARS = 200` (price_fetcher.py line 33)
- `SWEEP_CANDLE_HOURS = 2160` (price_fetcher.py line 31)
- `EXPIRY_BARS = 48` (eod_resolver.py line 78) — post-entry 1h bars before signal expires
- `MIN_RR = 1.5` (user-configured, env override — currently the active value)
- `MIN_P_WIN = 0.55` (default, via `get_min_p_win()`)

### TimesFM probability tuning (from AGENTS.md)

- `TIMESFM_SIGMOID_K` (default 20.0)
- `TIMESFM_CLAMP_MIN/MAX` (default 0.001/0.999)
- Recommended production: k=10.0, clamp=[0.05, 0.95]

---

## Part 2 — The Problem: Choppy Markets and Signal Quality

### The structural blind spot in choppy/consolidating regimes

In consolidation, all four EV blend sources converge toward 0.5 simultaneously:
- HMM regime posteriors flatten (regime_conf degrades)
- TimesFM p_timesfm collapses to ~0.5 (no directional forecast)
- p_markov_hold_n → 0.5 (T^N approaches random walk)
- p_historical reverts to long-run mean

The logit-pool blend correctly produces p_win ≈ 0.5, but the 7-gate chain has
no mechanism to distinguish **"genuinely uncertain"** from **"genuinely edge-less."**
Both look identical: p_win hovering near the qualification threshold, signal
firing randomly.

The XAUUSD case documented on 2026-09-07 (brick-level price holding one unbroken
trend while regime-HMM flip-flopped 4x in 5 hours) is the canonical example —
the 5-min window is too short to see the forest for the trees.

### What longitudinal data currently provides (but doesn't feed the light sweep)

1. **Trend filter** (10-50d SMA crossover + RSI) — daily refresh, medium-term
   - Outputs `trend_label`, `trend_strength`, `rsi_state`, `days_since_crossover`
   - Feeds `kelly_trend_multiplier()` — Kelly sizing only (live mode)
   - Does NOT modulate EV blend or qualification gates

2. **Macro filter** (200-day SMA, EMA-smoothed) — daily refresh, long-range
   - Outputs `macro_label`, `macro_strength`
   - Feeds `kelly_macro_multiplier()` — Kelly sizing only (live mode)
   - Does NOT modulate EV blend or qualification gates

3. **Macro edge by symbol** — outcome-based win/loss evidence
   - `macro_edge.py` computes `win_rate`, `avg_rr_winners`, `expectancy_r` per macro bucket
   - Pure diagnostic — nothing downstream reads `nt_macro_edge_by_symbol`

4. **Meta-regime model** — coherence/noisy detection of signal-generation stream
   - Fits GaussianHMM over the model's own output stream (regime-label stability)
   - Currently informational — not a gating signal

5. **Heavy sweep** — 90-day grid search for optimal brick/SL/TP geometry
   - Optimizes for Sharpe over the full 90-day window
   - Writes optimized params to `brick_params.yaml`
   - These params are read by the light sweep as-is

---

## Part 3 — Longitudinal Enrichment Framework

### Tier 1 — Pre-blend context scalar (highest leverage)

**Concept:** A `trend_context_scalar` ∈ [0.25, 1.5] that modulates the EV blend
*before* the p_win threshold gate, derived from the longitudinal state.

**Derivation:**
```
if signal aligns with non-zero macro_dir and medium-term isn't opposing:
    trend_context_scalar = 1.0 + trend_strength * boost_scale  # up to ~1.5
elif no macro trend (macro_dir == 0):
    trend_context_scalar = 0.25  # no edge to ride
else:  # counter-trend
    trend_context_scalar = 0.25  # suppress counter-trend signals
```

**Philosophy:** Identical to `kelly_trend_multiplier` and `kelly_macro_multiplier`
(the user's own convention) — but applied to *qualification*, not just sizing.
A signal counter-trend to the macro should fail `ev_allowed` or `p_win_ok`
regardless of what the 5-min EV blend says.

### Tier 2 — TimesFM k-adaptive (dampening in consolidation)

**Concept:** The static `TIMESFM_SIGMOID_K` (default 20.0) maps TimesFM's raw
price forecast to a probability. In consolidation, this produces noise.

**Adaptation:** `k_adaptive = k_base / (trend_strength + epsilon)`
- Strong trend → lower k → more aggressive probability mapping (trust the model)
- Consolidation → higher k → probabilities compressed toward 0.5 (don't trust noise)

This is the inverse of the current clamp behavior — the clamp preserves extreme
values, which is wrong when the market doesn't support directional conviction.

### Tier 3 — Meta-regime as a gating signal

**Concept:** Make the meta-regime's "coherent/noisy" output an 8th gate.
- meta-regime = "coherent" AND macro_dir ≠ 0 → proceed normally
- meta-regime = "noisy" AND macro_dir == 0 → fail (no edge, don't manufacture one)
- meta-regime = "noisy" AND macro_dir ≠ 0 → Tier 1 scalar applies

### Tier 4 — Heavy-sweep geometry fit score

**Concept:** The heavy sweep's `sweep_sharpe` and `n_trades` for the current
brick config serve as a consolidation detector. If best Sharpe < 1.0 and
trade count < 10, the geometry doesn't fit the current regime.

**Feeds:** `geometry_fit_score` ∈ [0, 1] modulates the effective `MIN_P_WIN`.
Poor geometry fit → higher p_win threshold to qualify.

---

## Part 4 — Impact on Calibration & Scoring Accuracy

### Entry price accuracy

The light sweep entry price is the next Renko brick boundary from the current
price. In a trending regime, entry precision is *less critical* — the move
carries you. In consolidation, entry precision *is* the edge — a ±0.3%
slippage eats 30-60% of a 0.5-1.0% oscillation.

**Regime-adaptive entry:** The brick size should scale with `trend_strength`:
- Trending → accept wider brick entries (the trend carries)
- Consolidating → demand tighter brick entries

### SL/TP calibration — the ATR problem

Current SL/TP uses `compute_atr(prices, period=14)` on hourly bars — a short-window
ATR that adapts to recent volatility but has no memory of whether recent volatility
is anomalous. In consolidation, ATR contracts (low volatility → tight SL). In a
trend, ATR may expand. The problem: **a tight ATR-based SL in consolidation gets
stopped out by normal noise**, producing the exact pattern the calibration system
flags as "loss" when it should be classified as "regime noise — not a signal failure."

**Longitudinal fix — regime-weighted ATR:**
```
atr_short = ATR(14) on hourly (current)
atr_regime = percentile(atr_90d, 50) from heavy sweep's 90-day history
atr_expansion_ratio = atr_short / atr_regime

# If ratio << 1: ATR is abnormally compressed → SL is too tight
# If ratio >> 1: ATR is abnormally expanded → SL is too wide
```

### Calibration bias: regime-conditioned estimation

The EOD recalibration loop computes `bayesian_penalty` from rolling bias.
Currently it's a single bias number per symbol per day. With longitudinal enrichment,
**bias becomes regime-conditional:**

Instead of `bias = mean(predicted_p_win - realized_win_rate)`:
```
bias_regime = {
    "trend": mean(predicted_p_win - realized_win_rate | regime == "trending"),
    "consolidation": mean(predicted_p_win - realized_win_rate | regime == "consolidating"),
    "reversal": mean(predicted_p_win - realized_win_rate | regime == "reversal")
}
```

The same p_win model can be well-calibrated in trends and overconfident in consolidation.
A single blended bias number masks this.

### MIN_RR interaction

At `MIN_RR = 1.5` (the user's current active value), the system is already below
the default 2.0 floor to generate signals. This means:

- **Without regime context:** grid search optimizes combos passing MIN_RR = 1.5,
  light sweep qualifies them, many get stopped out by consolidation noise.
- **With regime context:** effective MIN_RR rises to ~2.0-2.5 in consolidation,
  filtering for wider-SL combos or suppressing entirely.

The effective MIN_RR must propagate into `heavy_sweep.py:737`'s grid-search
pre-filter and `pick_best_combo()` — otherwise the heavy sweep optimizes brick
geometry against MIN_RR = 1.5 globally while the light sweep rejects those
same geometries locally.

### The honest caveat

None of this creates edge from nothing. In a *truly* random walk with no macro or
medium-term structure, the longitudinal layer will correctly identify "no edge"
and suppress signals — which is the desired behavior, but means fewer signals
overall. The design goal isn't more signals in choppy markets; it's **fewer false
positives in choppy markets and more confidence in trending markets**.

---

## Part 5 — The Brick Geometry Mismatch: Root Cause of "Expired" Signals

### The critical finding

**The SL/TP gap is too wide and the W/L + P/L rate is low while "expired" signals
are high because SL/TP never hits.** This is not a calibration problem — it is a
**brick geometry timescale mismatch** between the heavy sweep's optimization window
and the light sweep's execution window.

### The mismatch in numbers

| Component | Timeframe | Execution bars |
|---|---|---|
| Heavy sweep optimization | 2160 hourly bars (90 days) | Each "bar" = 1 hour |
| Light sweep execution | 200 hourly bars (~8 days) | Signal lives for EXPIRY_BARS = 48 hours |
| Renko brick execution | 5-minute bricks | Signal lives for ~48 hours = ~576 five-minute bars |

The heavy sweep's `pick_best_combo()` returns optimal `brick_size`, `sl_bricks`,
`tp_bricks` calibrated to **hourly** price movement. When the light sweep applies
those same brick counts to 5-minute Renko bricks, the SL/TP price distances are
an order of magnitude too wide for what the 5-minute window actually traverses.

### Concrete example

Heavy sweep optimizes: `brick_size = 0.50 ATR`, `sl_bricks = 4`, `tp_bricks = 8`
→ SL = 2.0 ATR, TP = 4.0 ATR

In the 5-minute window, the asset moves maybe 0.3 ATR per session. Neither SL
nor TP is ever reached → **expired signal, P/L = 0 (mark-to-market), W/L rate collapses.**

### Evidence from the codebase

The `eod_resolver.py` confirms this pattern:
- `EXPIRY_BARS = 48` — post-entry 1h bars before a signal is "expired"
- `resolve_signal_outcome` walks 48 hourly closes. If neither TP nor SL is hit
  within those 48 bars, outcome = "expired"
- `compute_pnl` for expired now uses mark-to-market (2026-09-08 fix) — the position
  was still open at `exit_price` (the last walked close) when the window ran out
- From `20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`: 59,913 expired rows,
  all with `exit_price IS NULL` before the fix. 52.3% showed `abs(mfe) > abs(mae)` —
  plausibly sitting in profit when the clock ran out and recorded as flat zero

### The `_renko_grid_search_sync` backtest problem

From `20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`:
- `RenkoPipeline.backtest_with_analytics` has **no concept of a deadline**
- It lets a simulated trade run until it closes, however long that takes, over a
  90-day window
- `eod_resolver.py` gives every real signal exactly 48 hours
- A bracket whose backtested "wins" mostly take 100+ hours to close looks great
  to the selector and mostly times out live
- Proven (`scripts/sim_deadline_aware_scoring.py`): XAUUSD's pick produces
  10 trades, 0 wins, 0 losses, **10 expired, $0** — every "win" takes longer than 48h
- Fix: same 5 candidates, deadline-aware scoring → 15 trades, 13 wins, 2 losses,
  **0 expired**, 86.7% win rate, +$468.70. **One variable changed.**

### The `_check_position` mechanism (risk_manager.py)

The SL/TP check runs per-brick in `_check_stop_loss` and `_check_take_profit`:
- `_check_stop_loss`: checks if `brick.close_price <= pos.current_sl_price`
- `_check_take_profit`: checks if `brick.close_price >= pos.tp_price`
- Position entry is `brick.close_price` from the 5-minute Renko brick stream
- SL/TP prices are computed at entry: `entry_price ± sl_bricks * brick_size`
- **Both SL and TP are evaluated against 5-minute brick prices**

The brick_size is loaded from `config/brick_params.yaml` — which the heavy sweep
writes with ATR-scaled values optimized on **hourly** data. The SL and TP prices
are therefore set in **hourly ATR multiples** but checked against **5-minute** prices.

### Three concrete fixes (ranked by implementation leverage)

#### Fix A: Scale heavy-sweep geometry to light-sweep timeframe (highest leverage)

Derive the ratio of `hourly_range / 5min_range` empirically from the 90-day lookback
data the heavy sweep already fetches. Apply that ratio to the heavy sweep's optimal
`brick_size`. The light sweep then uses geometry proportional to its actual price range.

Implementation:
```python
# In heavy_sweep or a new brick_scale module
# Compute from the same 2160-bar history used for grid search
hourly_atr = compute_atr(hourly_prices, period=14)
five_min_range = max(prices[-200:]) - min(prices[-200:])  # last 200 5-min bars
scale_factor = five_min_range / (len(prices[-200:]) * hourly_atr / 14)

# Apply to heavy sweep's output
scaled_brick_size = heavy_sweep_best_brick_size * scale_factor
# sl_bricks and tp_bricks counts stay the same — only brick_size scales
```

#### Fix B: Re-optimize heavy sweep on 5-minute data

Heavier (more compute), but eliminates the mismatch entirely. The 90-day lookback
becomes 5-minute bars instead of hourly. This changes the grid search's entire
combinatorial space and may not converge in the same runtime budget.

#### Fix C: Light sweep constructs independent Renko at scaled geometry

The heavy sweep identifies the optimal *hourly* brick size. The light sweep divides
it by √12 (≈3.46, the random-walk scaling from hourly to 5-min) to get the
5-minute brick size, then applies the same `sl_bricks`/`tp_bricks` counts. Same as
Fix A but derived from first principles rather than empirical ratio.

**Fix A is the pragmatic path** — reuses the heavy sweep's grid-search optimization,
just rescales the output. Fix B is the "correct" answer but likely too expensive.
Fix C is a middle ground that may work without empirical calibration.

### The EOD resolver's 48-hour window is also a mismatch

The `eod_resolver.py` walks 48 **1-hour** bars (`LIGHT_BARS = LIGHT_CANDLE_BARS = 200`).
But the signal executes on 5-minute Renko bricks. A signal that enters at 10:00 UTC
and expires at 10:00 UTC two days later has 576 five-minute bars of price action —
but the resolver only sees 48 hourly closes. **The resolver's "expired" verdict is
based on hourly price checkpoints that may miss intraday TP/SL crossings entirely.**

This means:
1. A TP could be hit at 14:30 UTC on a 5-minute brick, but the 1-hour close at
   15:00 UTC might be back below TP — the resolver records "expired" instead of "tp_hit"
2. An SL could be hit at 09:15 UTC on a 5-minute brick, but the 1-hour close at
   10:00 UTC might be above SL — same false "expired"

The resolver's `window = list(closes or [])[:max(int(expiry_bars or 0), 0)]` at
line 211 takes the first 48 elements of the hourly close array. These are
1-hour candles, not 5-minute bricks. **The resolution mechanism is operating on
a different granularity than the execution mechanism.**

---

## Part 6 — Proposed Architecture (Enrichment Pipeline)

```
Light sweep (5-min) produces raw EV blend → p_win_raw
                                               │
Longitudinal enrichment layer:
  1. Macro_dir, medium_dir from trend/macro filters (daily)
  2. trend_context_scalar ∈ [0.25, 1.5] — direction alignment
  3. TimesFM k-adaptive — dampen in consolidation
  4. Meta-regime coherence gate — "noisy" + no macro → suppress
  5. Heavy-sweep geometry_fit_score — adjust MIN_P_WIN dynamically
  6. Regime-adaptive p_win baseline — edge = p_win - baseline(regime)
  7. BRICK GEOMETRY SCALE — scale heavy-sweep brick_size to 5-min timeframe
                                               │
Adjusted p_win_adjusted = p_win_raw * trend_context_scalar
Adjusted brick_size = heavy_sweep_brick_size * scale_factor
Final qualification: p_win_adjusted > baseline_p_win(regime) * threshold
Final SL/TP: entry_price ± sl_bricks * adjusted_brick_size, tp_bricks * adjusted_brick_size
```

### The heaviest lifting comes from:

1. **Fix A (brick geometry scale)** — Without this, all other enrichment is
   polishing a broken instrument. SL/TP still won't be hit because the
   fundamental unit of measurement is calibrated to a 12x slower timeframe
   than execution.

2. **Tier 1 (context scalar on qualification)** — Makes the 7-gate chain
   context-aware rather than threshold-blind.

3. **Regime-adaptive p_win baseline** — Edge = p_win - baseline(regime)
   makes the scoring meaningful rather than absolute.

---

## Part 7 — Risks and Dependencies

### What needs to be true for this to work

1. **The heavy sweep's 90-day lookback must contain the current regime**
   — If the market just entered a new regime (e.g., transition from trend
   to consolidation 2 days ago), the 90-day history is still mostly old-regime
   data. The trend/macro filters' daily refresh cadence mitigates this, but
   there's a lag of up to 24 hours.

2. **The scale factor must be stable** — If the hourly-to-5-min range ratio
   varies wildly between symbols (crypto vs. equities vs. forex), a single
   global scale factor won't work. Symbol-specific calibration is needed,
   which is doable but requires a per-symbol historical ratio table.

3. **The resolver's 48-hour window granularity** — If Fix A is applied but the
   eod_resolver still walks hourly bars, the "expired" verdict remains
   inaccurate for 5-minute execution signals. Fix A alone improves brick
   geometry but doesn't fix the resolution timestamp mismatch.

### What is NOT in scope

- TimesFM model retraining or architecture changes (out of scope for this review)
- HMM regime detection changes (regime_engine.py, regime label stability —
  already addressed by warm-start fix per 20260901_REGIME_LABEL_STABILITY_SCOPE.md)
- Supabase schema changes (the user's preference is views over new tables)
- New tables or INSERTs into production (per user's stated preferences)

---

## Part 8 — Summary

| Problem | Root Cause | Fix Category |
|---|---|---|
| Choppy markets produce random qualified signals | No longitudinal context in light sweep | Tier 1-4 enrichment framework |
| Calibration bias masks regime-specific errors | Single rolling bias per symbol | Regime-conditional bias estimation |
| TimesFM overconfident in consolidation | Static k=20.0 | k-adaptive dampening |
| W/L and P/L rate low | Brick geometry mismatch (hourly optimization, 5-min execution) | **Fix A: scale brick_size** |
| High "expired" rate (SL/TP never hits) | SL/TP distances set in hourly ATR multiples, checked against 5-min prices | **Fix A + resolver granularity fix** |
| MIN_RR = 1.5 produces noise | Already below default floor; no regime filter | Dynamic effective MIN_RR |
| Calibration data polluted by noise stop-outs | ATR-based SL too tight in consolidation | Regime-weighted ATR |
| Signal precision matters less in trends | Fixed brick geometry | Regime-adaptive brick sizing |
| Resolver records expired when TP/SL was hit | 48 hourly checkpoints vs 576 five-minute bars | Resolver granularity fix |

The brick geometry scale fix (Fix A) is the single highest-impact change
because it addresses the root cause of the "expired" signal problem — which
is the most visible and operationally damaging symptom the user is currently
experiencing. Everything else (Tier 1-4 enrichment, regime-conditional
calibration, k-adaptive TimesFM) improves the *quality* of signals that
actually fire, but doesn't fix the fact that SL/TP never reaches the price
the system thinks it will.

---

## References

- `worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md` — deadline-unaware
  backtest scoring (proven in simulation), expired-signal PnL fabrication fix
- `worklog/20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md`
  — 4+ ATR distance bucket, 5-17% TP resolution, 68-91% expiry for gold family
- `worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md` — calibration
  bias vs scoring bias, p_historical retirement, EV engine v6 architecture
- `worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md` — HMM regime label flapping,
  warm-start fix, label continuity
- `worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md` — trend
  filter module, regime staleness, self-learning direction
- `worklog/20260904_SIGNAL_ENGINE_MATH_TRIAGE.md` — Masaniello exponent inverted,
  fractional kelly cap bug, kelly_edge unbounded, orchestrator _mu direction-blind
- `regime_platform/core/regime_engine.py` — RegimeHMM, RegimeSnapshot, 4-state
  dual GaussianHMM (volatility × trend)
- `regime_platform/sweep_orchestrator/orchestrator.py` — `_process_symbol()` (line
  2404+), 7-gate qualification, EV engine integration, brick params loading
- `regime_platform/sweep_orchestrator/heavy_sweep.py` — grid search, `pick_best_combo()`
  (line 333+), `select_best_combo_deadline_aware()` (line 452)
- `regime_platform/services/statistics/ev_engine.py` — EV Engine v6, 4-source logit pool
- `regime_platform/sweep_orchestrator/trend_filter.py` — medium-term SMA crossover
  + RSI trend regime
- `regime_platform/sweep_orchestrator/macro_filter.py` — long-range 200-day SMA
- `regime_platform/sweep_orchestrator/macro_edge.py` — win/loss + PnL evidence per macro bucket
- `regime_platform/sweep_orchestrator/meta_regime_model.py` — coherence/noisy detection
- `regime_platform/sweep_orchestrator/price_fetcher.py` — `LIGHT_CANDLE_BARS = 200`,
  `SWEEP_CANDLE_HOURS = 2160`
- `regime_platform/sweep_orchestrator/eod_resolver.py` — `EXPIRY_BARS = 48`,
  `resolve_signal_outcome()`, `compute_pnl()` (mark-to-market expired fix)
- `regime_platform/config/brick_params.py` — YAML-backed brick parameter loader
- `renko/risk_manager.py` — `ActivePosition`, `_check_stop_loss()`, `_check_take_profit()`,
  `SL/TP price = entry_price ± sl_bricks * brick_size`
- `renko/pipeline.py` — `RenkoPipeline.backtest()`, `backtest_with_analytics()`,
  no deadline concept in `backtest()`
- `regime_platform/sweep_orchestrator/calibration_enforcement.py` — live calibration
  enforcement, `apply_calibration_adjustment()`
- `regime_platform/sweep_orchestrator/drift_monitor.py` — rolling bias drift detection
- `regime_platform/core/feature_flags.py` — `get_min_p_win()`, `MIN_RR` access

---

## Part 9 — Assessment: Part 5's central claim does not hold (2026-09-11)

> **Priority**: P1 · **Task**: Verify Part 5's brick-geometry-mismatch
> diagnosis against the live code before treating it as a build
> direction · **Status**: resolved (verified false, evidence below) ·
> **Date**: 2026-09-11 · **Roadmap refs**: none (finding, not a
> tracked mechanism)

Part 5's whole diagnosis rests on one falsifiable claim: heavy sweep
optimizes brick/SL/TP against **hourly** bars while light sweep
executes against **5-minute** Renko bricks (checked every 5 minutes),
and `risk_manager.py`'s `_check_stop_loss`/`_check_take_profit`
evaluate SL/TP at that 5-minute granularity. This was checked directly
against the running code before accepting it, not assumed correct
because the framing was plausible.

**What's actually true, verified with file/line references:**

1. **Light sweep cadence is stale in the doc.** `regime_cron` default
   is `*/15 * * * *` (`orchestrator.py:1370`); migration
   `144_light_sweep_15min_cadence.sql` moved every live `nt_symbol` row
   from `*/5 * * * *` to `*/15 * * * *` on 2026-09-09 — one day before
   this document. The "every 5 min" framing describes the pre-migration
   cadence.

2. **Light sweep and heavy sweep use the SAME bar granularity — hourly,
   not 5-minute.** `price_fetcher.py:355` states explicitly: light
   sweep fetches `n_bars=200, interval="1h"`. The live brick-capture
   block (`orchestrator.py:3724-3734`) calls
   `RenkoBrickEngine(...).build_from_prices(prices, ...)` using that
   exact 200-bar/1h array. Heavy sweep uses 2160 hourly bars — same
   interval, just more of them. **There is no 5-minute price feed
   anywhere in `regime_platform/` or `renko/`** — grepped for one
   explicitly, found none in live use. No timeframe mismatch of the
   kind Part 5 describes exists between the two sweep paths.

3. **`risk_manager.py`'s SL/TP checks are not part of the live sweep
   loop at all.** `ActivePosition`/`_check_stop_loss`/
   `_check_take_profit` (`renko/risk_manager.py`) operate on
   `renko/pipeline.py`'s `RenkoPipeline` — a self-contained, tick-driven
   HFT module exposed only via `renko_router` (`main.py:438-439`, a
   separate FastAPI router for backtest/simulation endpoints). Grepped
   `orchestrator.py`/`heavy_sweep.py`: zero references to
   `risk_manager`, `RiskManager`, or `ActivePosition`. Citing this code
   as evidence for how live SL/TP checks work describes a different,
   disconnected module tree.

4. **"5-minute Renko brick" is a category error independent of the
   above.** `RenkoBrickEngine.process_tick` (`renko/brick_engine.py:
   114-138`) emits a brick only when price crosses a `brick_size`
   threshold — bricks are price-driven, not time-based. No brick has a
   fixed duration; framing execution as "5-minute bricks" conflates
   input sampling rate with brick formation.

**Conclusion**: Fix A (scale `brick_size` for an hourly/5-minute ratio)
solves a mismatch that does not exist in the current live system — both
sweep paths already share the same hourly granularity. This is not a
judgment call; it's a direct code-level contradiction of Part 5's
premise.

**What the "expired" symptom actually is, re-grounded**: the real,
already-partially-addressed version of "optimization window vs.
execution window" is the gap between heavy sweep's 90-day backtest
(no deadline) and a live signal's actual 48-hour life
(`eod_resolver.py`'s `EXPIRY_BARS = 48`) — which is exactly what
deadline-aware scoring (`worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`)
was built to fix, not a new, undiscovered problem. Checked the real
number as of this writing (pulse-check, 2026-09-11): **810/2526 = 32%**
of the last 24h's resolved signals expired — elevated, but down from
~45% two days prior. The symptom is real and still worth attention; the
mechanism Part 5 proposed for it is not the explanation.

**What survives the correction, unaffected**: Parts 1-4's core
observation — `trend_label`/`macro_label` are computed daily and
captured on `nt_signal_sim`, but only ever multiply Kelly *sizing*,
never influence whether a signal *qualifies* in the first place — is
independently true and verified (see Part 10's own code trace). The
Tier 1-4 framework's motivating problem is real; only Part 5's specific
brick-geometry mechanism is not.

## Part 10 — Tier 1 scope: a context-alignment gate, not an EV-blend scalar (2026-09-11)

> **Priority**: P1 · **Task**: Fully scope Part 3's Tier 1 (pre-blend
> context scalar) as a concrete, buildable mechanism · **Status**:
> blocked (scope only, nothing built, awaiting approval) · **Date**:
> 2026-09-11 · **Roadmap refs**: RM-45

### 10.1 Why not build Tier 1 exactly as Part 3 describes it

Part 3's literal proposal — `p_win_adjusted = p_win_raw *
trend_context_scalar`, applied "before the p_win threshold gate" — is a
change to the EV blend's own arithmetic. Reworking `compute_ev()` /
`ev_engine.py` is off the table per
[[ev_engine_no_rework]] (the MSE precedent; EMA smoothing/TimesFM
already address noise/pool-robustness). Tier 1 needs a design that
achieves the same *effect* Part 3 wants — a counter-trend signal should
not sail through on the strength of a 5-minute EV blend that has no
memory of the last 10-50 days — without touching `p_win` or
`ev_result` at all.

### 10.2 Where the real gap is — traced precisely, not assumed

Had the exact gate-evaluation order in `_process_symbol` checked before
designing anything:

- `effective_kelly` is set (`orchestrator.py:2851`, refined at `:2962`/
  `:2993`), **then** the macro filter (`:3016-3056`) and trend filter
  (`:3058-3094`) blocks each multiply it in place
  (`effective_kelly = effective_kelly * macro_mult` at `:3045`, same
  pattern at `:3083`), **then** `kelly_min_ok` reads that same
  `effective_kelly` at `:3149`.
- So **`kelly_min_ok` already IS context-aware** — a strongly
  counter-trend signal whose Kelly gets multiplied down by both
  penalties can already fail this gate today, in `live` mode. This is
  better than Part 3 assumed.
- **`p_win_ok`** (`:3127-3139`, from `ev_result.p_win`) and
  **`ev_allowed`** (`effective_ev_result.allowed`, set at `:2837-2848`/
  recomputed `:3000-3008`) are the real gap — neither formula
  references Kelly or the macro/trend multipliers *at all*. A
  confidently counter-trend signal can clear both of these purely on
  the 5-minute EV blend's own say-so, with zero longitudinal input.

### 10.3 Design — a new, independent 8th gate

`context_aligned_ok`, added to the same `gate_results` dict
`kelly_min_ok` already lives in (`orchestrator.py:3143-3159`), computed
from the SAME `macro_label`/`trend_label` reads the two existing blocks
already fetch — no new Supabase round-trip, no new I/O.

```python
def compute_context_alignment(
    signal: str,               # "buy" | "sell"
    macro_label: str | None,
    trend_label: str | None,
) -> bool:
    """True (pass) unless BOTH available longitudinal reads confidently
    oppose the trade direction. Requiring both to agree in opposition
    (not just one) is deliberate: a single noisy source disagreeing
    should not veto a signal on its own — that's what the existing
    Kelly multipliers already handle proportionally. This gate targets
    only the case both Part 3 and the real p_win_ok/ev_allowed gap
    describe: everything says no, yet the 5-min blend still fired.
    """
    if signal not in ("buy", "sell"):
        return True
    implied = "bull" if signal == "buy" else "bear"

    macro_opposes = macro_label is not None and macro_label not in ("neutral", implied)

    trend_bearish = trend_label in ("reversal_down", "trending_down")
    trend_bullish = trend_label in ("reversal_up", "trending_up")
    trend_opposes = (implied == "bull" and trend_bearish) or (implied == "bear" and trend_bullish)

    return not (macro_opposes and trend_opposes)
```

Deliberately excludes `consolidating`/`neutral` trend reads from
"opposing" — matching the existing multipliers' own no-op-when-neutral
convention (`kelly_macro_multiplier`/`kelly_trend_multiplier` both
return `1.0` on a neutral/consolidating read, never treated as
evidence against a trade).

### 10.4 Feature flag — `context_alignment_gate_mode`, same 3-stage discipline

`off` (default) → the gate always evaluates `True`, zero behavior
change. `shadow` → compute and capture `context_aligned_ok` on
`nt_signal_sim` (a new nullable column, same pattern as
`raw_p_regime`/`gated_p_regime`) WITHOUT including it in the
`qualified = all(gate_results.values())` calculation — so it can be
reviewed against real outcomes before it ever rejects anything. `live`
→ included in the gate calculation for real.

### 10.5 Test plan

Pure-function unit tests for `compute_context_alignment` (mirrors
`test_trend_filter.py`'s style): both-oppose → `False`; only one
opposes → `True` (the "single noisy source" case this gate deliberately
does not veto); both-agree → `True`; either/both neutral/consolidating
→ `True`; neutral `trade_signal` → `True` no-op. Shadow-mode validation
before `live`: compare `context_aligned_ok=False` days against what
`p_win_ok`/`ev_allowed` alone would have qualified, over a real trailing
window — same "does this correctly suppress bad signals without
starving good ones" question the 14-symbol trend-filter backtest asked,
applied here as a gate-agreement check instead of a directional
hit-rate.

### 10.6 What this deliberately does NOT do

Does not touch `ev_engine.py`, `compute_ev()`, `p_win`, or
`ev_result.allowed` in any way — those stay exactly as they are, per
[[ev_engine_no_rework]]. Does not replace or duplicate
`kelly_min_ok`'s existing (already-working) context-awareness via the
Kelly multipliers — this is a second, independent check catching the
gap those multipliers cannot reach. Not built — scope only, awaiting
approval.

## Part 11 — Dynamic/regime-conditional MIN_RR, scoped (2026-09-11)

> **Priority**: P2 · **Task**: Fully scope Part 4's "MIN_RR interaction"
> proposal as a concrete, buildable mechanism · **Status**: blocked
> (scope only, nothing built, awaiting approval) · **Date**: 2026-09-11
> · **Roadmap refs**: RM-46

### 11.1 Where MIN_RR actually lives — traced, not assumed

Part 4 cited `heavy_sweep.py:737` as the grid-search pre-filter — that
line is actually just the `asyncio.to_thread` dispatch call; the real
filter is `if tp / sl < MIN_RR: continue` at **`heavy_sweep.py:1423`**
(stale line reference in the source doc, corrected here).

Full call-site map (`get_min_rr()`, `feature_flags.py:271`):
- `orchestrator.py:138` — `MIN_RR = get_min_rr()`, read once at module
  load.
- `orchestrator.py:1096` — `self._ev_engine.min_rr = MIN_RR`, set once
  at `EVEngine` construction, never touched again.
- `heavy_sweep.py:137` — same module-load pattern; `:1423` the grid
  pre-filter; `:335/384/455` default params threaded into
  `pick_best_combo()`.
- `heavy_sweep_reachability.py:61,77,109` — a **separate, hardcoded**
  `_MIN_RR_DEFAULT = 2.0`, not wired to `get_min_rr()` at all — a
  second, independent MIN_RR source already silently diverged from the
  configured value if that module is live. Worth fixing regardless of
  whether this section gets built (see 11.5).

**Confirmed independent of the EV blend**: `min_rr` is consulted only
in `compute_ev()`'s Step 6 allow/deny gate (`ev_engine.py:708-715`,
`allowed = (ev > self.min_ev) and (rr >= self.min_rr)`) and in
`recompute_ev_fields()` — both strictly after and structurally separate
from the Steps 1-3 logit-pool blend. Changing how `min_rr` is *chosen*
per-tick does not touch blend logic.

**Confirmed real limitation Part 4 flagged**: the light sweep has no
independent RR check of its own — `gate_results["ev_allowed"]` is a
pass-through of `effective_ev_result.allowed`, which was computed
against whatever brick geometry heavy sweep already picked. The light
sweep cannot reject a geometry for insufficient RR; it can only
allow/deny the single geometry it inherited. So yes — heavy sweep and
light sweep genuinely can (and currently do) operate against the same
static `MIN_RR` value with no regime awareness in either, exactly as
Part 4 describes. This part of Part 4's diagnosis holds up, unlike
Part 5.

### 11.2 Design — an explicit parameter, not shared mutable state

The tempting implementation — mutate `self._ev_engine.min_rr` per-tick
right before each symbol's `compute_ev()` call — is a real hazard: if
`_process_symbol` calls ever run concurrently across symbols sharing
one `EVEngine` instance (needs confirming, but the risk is real enough
not to build around it), a per-tick mutation of shared state is a race
condition — symbol A's freshly-set `min_rr` could be overwritten by
symbol B before A's `compute_ev()` call actually executes.

**Safe alternative**: add an optional parameter to `compute_ev()`'s
signature — `min_rr: float | None = None`, falling back to
`self.min_rr` when not supplied — used only inside the existing Step 6
gate check, never touching the blend. This is an additive,
backward-compatible signature change to the gate step, not a rework of
`compute_ev()`'s actual logic, and eliminates the shared-state race
entirely: each call passes its own resolved value.

### 11.3 Computing the effective MIN_RR — reuse, don't refetch

Reuses the SAME `macro_label`/`trend_label` already fetched for the
Kelly multipliers and (if built) Part 10's gate — no new Supabase
round-trip.

```python
def compute_effective_min_rr(
    base_min_rr: float,
    consolidation_min_rr: float,
    macro_label: str | None,
    trend_label: str | None,
) -> float:
    """Raise the RR floor when there is no directional edge to ride;
    never lower it below base_min_rr. Deliberately one-directional —
    a confirmed trend does not relax risk controls, it only avoids
    tightening them unnecessarily."""
    no_macro_edge = macro_label is None or macro_label == "neutral"
    no_trend_edge = trend_label is None or trend_label == "consolidating"
    if no_macro_edge and no_trend_edge:
        return max(base_min_rr, consolidation_min_rr)
    return base_min_rr
```

Applied in two places, closing the exact gap Part 4 identified:
1. **Light sweep** (`orchestrator.py`): resolve `effective_min_rr` at
   the same point macro/trend context is already fetched, pass it into
   `compute_ev(..., min_rr=effective_min_rr)`.
2. **Heavy sweep** (`heavy_sweep.py`): at grid-search time, look up the
   symbol's current `nt_macro_regime`/`nt_trend_regime` rows (already
   fetched elsewhere in this file for the macro-filter block) and use
   the same `compute_effective_min_rr()` to set that day's grid
   pre-filter threshold at line 1423, instead of the flat module-level
   `MIN_RR` constant.

### 11.4 Feature flag — `dynamic_min_rr_mode`, same 3-stage discipline

`off` (default) → `effective_min_rr == base_min_rr` always, byte-
identical to today. `shadow` → compute and log/capture what the
effective value WOULD have been (e.g. on `nt_sweep_result`, a nullable
column) without actually passing it to `compute_ev()`/the grid filter.
`live` → actually applied in both call sites above.

### 11.5 Also worth fixing regardless: `heavy_sweep_reachability.py`'s orphaned default

Independent of whether this section gets built, `_MIN_RR_DEFAULT = 2.0`
in `heavy_sweep_reachability.py` not being wired to `get_min_rr()` is
the same "two independent sources of truth for one threshold" shape as
the 2026-08-13 incident referenced in that module's own comments — a
one-line fix (read `get_min_rr()` instead of a hardcoded literal)
whenever this file is next touched, but not scoped as its own thing
here.

### 11.6 Test plan

Pure-function tests for `compute_effective_min_rr` (both-neutral →
raised; either has an edge → base; strength/label combinations mirror
`test_trend_filter.py`'s style). Integration: confirm
`compute_ev(min_rr=X)` produces the exact same `allowed` result as
manually setting `self.min_rr = X` beforehand — a direct regression
test that the new parameter path is equivalent to the old
attribute-mutation path it replaces, not a silent behavior change.

## Part 12 — TimesFM k-adaptive dampening, scoped (2026-09-11)

> **Priority**: P2 · **Task**: Fully scope Part 3 Tier 2's TimesFM
> sigmoid-k adaptation as a concrete, buildable mechanism · **Status**:
> blocked (scope only, nothing built, awaiting approval; a real
> sequencing complexity found, see 12.2) · **Date**: 2026-09-11 ·
> **Roadmap refs**: RM-47

### 12.1 Where the sigmoid mapping actually lives — confirmed upstream of the blend

`orchestrator.py`'s `_fetch_timesfm_cached` (defined line 270, sigmoid
+ clamp at lines 461-478) computes `p_timesfm` from TimesFM's raw
forecast using `TIMESFM_SIGMOID_K` (sourced line 255), then
`TIMESFM_CLAMP_MIN`/`MAX`. The result is passed into
`self._ev_engine.compute_ev(..., p_timesfm=p_timesfm, ...)` at line
2837 as an already-computed, opaque float —
`compute_ev()` (`ev_engine.py:656,668-669`) never sees the raw
forecast or `k` at all. **Confirmed safe**: an adaptive-k change is
entirely upstream of the blend; `ev_engine.py` needs zero edits.

The clamp is separate code applied after the sigmoid, in the same
function — an adaptive k wouldn't require changing the clamp itself,
though clamp-hit frequency may shift empirically once k varies (a
smaller adaptive-k in a strong trend could push more values toward the
clamp's edges) — worth watching in shadow mode, not a code dependency.

### 12.2 The real complexity: sequencing, not the EV blend

`_fetch_timesfm_cached` runs early in `_process_symbol` (feeding the EV
blend inputs); the trend-filter block (`orchestrator.py:3058-3094`)
that produces `trend_strength` runs much later, right before gate
evaluation. **`trend_strength` is not yet available at the point the
TimesFM sigmoid currently runs.** Making k adaptive to it requires
hoisting the trend-context fetch earlier in `_process_symbol` — before
the TimesFM call — not just adding an isolated new block the way every
other mechanism built this session has (`trend_filter.py`,
`macro_filter.py`'s own integration, Part 10's proposed gate). This is
a real, moderate reordering of existing control flow, not a pure
addition, and is the main reason this section is scored P2/more
cautious than Part 10 or Part 11.

**Mitigation**: fetch `nt_trend_regime` once, early, and thread that
SAME already-fetched row through to both the (now-earlier) TimesFM
k-adaptation and the later Kelly-multiplier/gate logic that already
reads it today — a net-zero change in the number of Supabase round-trips
per tick (one fetch, moved earlier and reused, not a second fetch
added).

### 12.3 Design

```python
def compute_adaptive_timesfm_k(
    base_k: float,
    trend_strength: float | None,
    floor_k: float,
    ceiling_k: float,
) -> float:
    """Stronger trend -> lower k -> sharper, more confident probability
    mapping (trust the forecast). No trend context (None) or a
    consolidating/zero-strength read -> higher k -> compress toward
    0.5 (don't manufacture conviction from noise). Clamped to
    [floor_k, ceiling_k] so a near-zero trend_strength can't blow k up
    to an extreme that behaves pathologically in the sigmoid."""
    strength = trend_strength if trend_strength is not None else 0.0
    epsilon = 0.05  # prevents divide-by-near-zero when strength -> 0
    k = base_k / (strength + epsilon)
    return max(floor_k, min(ceiling_k, k))
```

### 12.4 Feature flag — `timesfm_k_adaptive_mode`, same 3-stage discipline

`off` (default) → `k == TIMESFM_SIGMOID_K` always, byte-identical to
today (also the fallback if `nt_trend_regime` has no row yet for a
symbol — never worse than today's static behavior). `shadow` → compute
`k_adaptive` and capture the resulting `p_timesfm_adaptive` alongside
the real `p_timesfm` for comparison (a new nullable field, same pattern
as `raw_p_regime`/`gated_p_regime`), without it affecting the actual EV
blend input. `live` → the adaptive value is what actually feeds
`compute_ev()`.

### 12.5 Test plan

Pure-function tests for `compute_adaptive_timesfm_k` (strong trend →
low k within floor; no/neutral trend → high k at ceiling; the
floor/ceiling clamp itself). Shadow-mode validation: compare
`p_timesfm` vs `p_timesfm_adaptive` against realized outcomes over a
real trailing window — same shape of validation as the trend-filter's
own 14-symbol backtest, checking whether the adaptive version is
better *calibrated* (Brier score), not just directionally different.

### 12.6 What both Part 11 and Part 12 deliberately do NOT do

Neither touches `ev_engine.py`'s blend logic (Steps 1-3, the
logit-pooling/renormalization) — confirmed by tracing both proposals to
their exact call sites before writing this scope, not assumed safe
because the framing sounded plausible (the same discipline applied to
Part 5, which did NOT survive that check). Not built — both are scope
only, awaiting approval.

## Part 13 — Tier 1 context-alignment gate — built (2026-09-11, explicit approval: "build code")

> **Priority**: P1 · **Task**: Build the Part 10 context-alignment gate
> · **Status**: resolved (built, tested, shipped `off`; not yet
> deployed to Studio) · **Date**: 2026-09-11 · **Roadmap refs**: RM-45

Built exactly to Part 10's spec, no scope drift:

- **`regime_platform/sweep_orchestrator/context_alignment.py`** (new) —
  `compute_context_alignment(signal, macro_label, trend_label) -> bool`,
  the pure function from 10.3, unchanged from the scoped design. Never
  touches `p_win`/`ev_result`.
- **`context_alignment_gate_mode`** (`off`/`shadow`/`live`, shipped
  `off`) in `feature_flags.py`/`feature_flags.yaml`.
- **`nt_signal_sim.context_aligned_ok`** (new nullable column).
- **Wired into `orchestrator.py`** right after the trend-filter block:
  computed from the SAME `macro_label`/`trend_label` already fetched
  (zero new Supabase round-trips), added to the existing
  `gate_results` dict **only when `live`** so it participates in
  `qualified = all(...)` — in `off`/`shadow` the dict is untouched,
  so `qualified`'s computation is byte-identical to before this change.
- **Wired into `heavy_sweep.py`** too, but deliberately **capture-only**
  there: tracing the code while wiring this up found that heavy
  sweep's own `qualified` is decided (~line 1130) *before* any of the
  macro/trend/context blocks run — meaning the existing
  `kelly_macro_multiplier`/`kelly_trend_multiplier` calls already only
  ever affect `result.kelly_f` there, never `result.qualified`, today.
  Making the new gate capture-only in this file preserves that existing
  behavior rather than introducing a new asymmetry — light sweep (the
  path that actually re-evaluates every 15 minutes against live
  conditions) is where this gate can actually reject a signal.

**Verified**: 10 new unit tests (`tests/test_context_alignment.py`) —
both-oppose fails, single-source-opposes still passes (the deliberate
"don't veto on one noisy source" design), neutral/consolidating/missing
reads never treated as opposition, neutral trade signal is always a
no-op, return-type check (plain `bool`, not `numpy.bool_`/`None`, since
this feeds a boolean DB column and a dict consumed by `all()`). 841
tests passing, 0 regressions (831 baseline + 10 new).

Shipped `off` by default — no live behavior change until `shadow`
(then `live`) is explicitly turned on, same rollout discipline as
every other mode-gated mechanism this session. Not deployed to Studio
yet (code-complete vs. deployed gate).
