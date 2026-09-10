# 2026-08-25 — Full pipeline helicopter view: price fetch → EV → signal →
# analysis → feedback into EV (scope only, nothing built)

**Status: SCOPED.** Companion to `worklog/20260825_CALIBRATION_REFRAME_SCOPE.md`
(Phases 1-3: bracket reachability, calibration-bias-by-bucket, heavy-sweep
combo selection). That thread found the EV formula itself has no concept
of a trade expiring (`ev_engine.py:670`: `ev = p_win*profit - (1-p_win)*stake`,
a pure two-outcome model), and that `p_win`'s four blended sources are
mostly directional-confidence estimates with little-to-no awareness of
whether the chosen bracket is geometrically reachable at all. This
document maps the **entire** pipeline end to end — price fetch through
every feedback path that currently reaches back into live decisions — so
that decision sits in context, not in isolation.

Built from three parallel investigations (two completed in full, one hit
a session limit partway through signal-generation/qualification and was
finished directly by re-reading `orchestrator.py` — the two full reports
are the depth level throughout).

> **Worklog header note**: this file bundles several distinct build
> efforts worked over multiple days under one running thread, so each
> major `##` section below carries its own header block instead of one
> file-level header (following the same per-section convention used in
> `20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md`). Sections
> below without a header are connective narrative/checkpoint content, not
> standalone deliverables.

> **Priority**: P1 · **Task**: Map the full price-fetch-to-EV pipeline
> end to end and identify the core feedback-loop gap ·
> **Status**: n/a (pure investigation/mapping, no build) ·
> **Date**: 2026-08-25

---

## The pipeline, stage by stage

### 1. Price fetch — `price_fetcher.py`

- Sole source: TradingView Data API via RapidAPI (`fetch_tradingview_batch`).
- **Close-only** — no OHLC, no volume, timestamps dropped downstream. Every
  "ATR"/"true range" computation in this codebase (Phase 1's `compute_atr`,
  the legacy HMM's ADX approximation) is a close-to-close proxy for lack
  of real high/low data.
- Light sweep: 200 bars, 1h interval. Heavy sweep: 2160 bars (90d), 1h
  interval. 10-symbols-per-request TV limit, auto-chunked.
- One bounded 429 retry per chunk (capped 10s); a chunk that still fails
  just **drops those symbols for the tick** — no fallback price, silent
  gap self-healing on the next 5-15min tick.
- Historical bug (fixed): TDVA returns newest-first; every consumer
  assumes oldest→newest. A missed reversal here would have silently fed
  an 8-12-day-old close in as "current price" — worth remembering as the
  class of bug this pipeline is generally exposed to (silent staleness,
  not a loud failure).

### 2. Regime / feature detection — **two independent HMMs**, both refit every tick

- **`RegimeHMM`** (`core/regime_engine.py`) — produces `trend_probs` fed
  straight into `compute_ev`. Fit from scratch every ~5min tick on the
  200-bar window, no persisted state across ticks (docstring is explicit
  about this). Cross-tick label continuity is preserved only by matching
  new component means against the previous tick's label means, not by
  keeping any model state. Features are pure log-return statistics
  (rolling std, cumulative sum) — no TA indicators.
- **`HmmModel`** (`services/hmm/model.py`) — the transition-matrix source.
  Reads a rolling 300-bar `FeatureBuffer` (persists across ticks) built
  from log-return + microstructure (spread/imbalance, quote-API-dependent)
  + 5 locally-computed TA indicators (ADX/AO/BB-Power/MACD/RSI, all
  close-only approximations, ADX explicitly documented as "rough"). Also
  refit every tick, from the rolling window.
- Both models needed anti-degeneracy patches after going live: `RegimeHMM`
  needed feature z-scoring (2026-fix #3); `HmmModel` needed an `lr_std`
  floor raised from `1e-8`→`1e-4` after compressed-FX-pair state collapse,
  plus a uniform-prior fallback on transition-matrix collapse.

### 3. Markov transition matrix + `current_state`

- Live path: `HmmModel.emit_markov(symbol)` — matrix re-estimated every
  tick from the 300-bar buffer; `current_state` re-classified every tick
  from the buffer's single latest bar.
- **Confirmed noisy at tick granularity**: near-deterministic transition
  rows + a `current_state` that flips at the UP/FLAT classification
  boundary produced a documented live case (AAPL, 2026-08-22) where the
  raw `T^N` hold-probability swung between near-0 and near-1 tick to
  tick with no actual price move. **Deliberately addressed** by
  EMA-smoothing the output (`p_markov_hold_n`, α=0.30) rather than the
  re-estimation itself — confirmed (2026-08-26) this was the intended
  fix, not a stopgap; see "Candidate intervention points" below, which
  originally mis-flagged this as still-open.
- `_build_markov_chain` (the "obvious" markov function name) is dead
  code — kept only for a legacy `/ev/compute` REST endpoint, not on the
  live sweep path. Easy to mis-trace if you go looking for "the" markov
  function without checking call sites.
- An alternate source (`RegimeHMM`'s own transition matrix,
  `MARKOV_SOURCE_MODE=trend_hmm_shadow/live`) exists specifically because
  the legacy `HmmModel` was producing live `NaN`/state-collapse warnings
  — currently shadow-logged only, not the default.

### 4. EV computation — `services/statistics/ev_engine.py::compute_ev`

4-source logit-pool blend (`p_historical`, `p_regime`, `p_markov_hold_n`,
`p_timesfm`), missing sources renormalized out rather than defaulted to
0.5. Refresh cadence per source (this is the piece that most needed
mapping — none of the 4 are pinned to the weekly heavy-sweep cycle):

| Source | Refresh | Notes |
|---|---|---|
| `trend_probs` (→p_regime) | Every tick, EMA-smoothed (α=0.30) before use | Fresh-in-name, damped-in-practice |
| `p_markov_hold_n` | Every tick, EMA-smoothed (α=0.30) | Same — the smoothing exists because the raw signal is too noisy to use directly |
| `p_historical` | 15min TTL cache over `fetch_historical_p_win` | Per-**symbol** pooled realized win rate, Bayesian-shrunk. Not geometry-aware — see §Core finding. |
| `p_timesfm` | 15min TTL cache, external HTTP forecast | Not a native probability — a 1-bar-ahead price forecast run through a manual sigmoid (`k=20` tunable); fails open (`None`) on any of 5 distinct error paths. Each call is per-symbol, but the model is a foundation model pretrained across a broad pool of series — this is the pipeline's intended source of broader-than-one-symbol robustness in the probability forecast, not something absent (corrected 2026-08-26, see "Candidate intervention points") |

Then: `ev = (p_win * profit) - ((1.0 - p_win) * stake)`, `profit =
tp_bricks*brick_size`, `stake = sl_bricks*brick_size`. **No expire
branch.** Whenever a trade doesn't win, this formula assumes the full
stake was lost — but Phase 1/2's data shows the dominant "didn't win"
outcome for many symbol/bucket combinations is a zero-P&L expiry, not a
stop-out. `p_markov_hold_n` is the only one of the 4 sources with any
notion of time/geometry at all, and it uses `n_steps = tp_bricks` under
a hard-coded "1 brick = 1 bar" velocity assumption — Phase 1 showed real
time-to-resolve for wide brackets is 40-130+ hours, not `tp_bricks` hours.

### 5. Signal generation + qualification gates — `orchestrator.py`

- `_compute_trade_signal`: `regime_label` (does it contain "bear"?) +
  `current_price` + `brick_size`/`sl_bricks`/`tp_bricks` → buy/sell/neutral
  + entry/stop/take-profit prices. Purely geometric — no EV/confidence
  input at this stage.
- Brick geometry itself reaches the light sweep via a **pure YAML
  round-trip**: heavy sweep (weekly) writes `config/brick_params.yaml`;
  light sweep (every 5-15min) reads it via `_load_brick_params` — no DB,
  no cache invalidation signal. A symbol can run for up to a week on
  brick geometry chosen against last week's volatility regime.
- Light-sweep qualification (`orchestrator.py` gate_results dict) is an
  **all-of** over: `signal_qualifiable` (non-neutral), `regime_conf_ok`
  (≥`MIN_REGIME_CONF`), `kelly_min_ok` (≥`MIN_KELLY`, checked on the
  **post-enforcement** effective kelly, not raw), `timesfm_confidence_ok`,
  `ev_allowed` (`ev>min_ev and rr>=min_rr`, post-enforcement), and a
  **soft `p_win` gate** (hard floor 0.50 = never publish; 0.50-`MIN_P_WIN`
  = publish at linearly-shrunk kelly; above `MIN_P_WIN` = full kelly).
  `timesfm_available` is informational unless `TIMESFM_GATE_MODE=hard`.
  Heavy-sweep qualification (`_evaluate_heavy_qualification`) is the same
  shape, simpler: non-neutral + `ev_result.allowed` + `p_win≥MIN_P_WIN` +
  `regime_conf≥MIN_REGIME_CONF` + `kelly_f≥MIN_KELLY`.
- **None of these gates reference bracket-width/ATR/reachability at
  all** — confirmed by reading the full gate list. A signal with a
  4+-ATR bracket and one with a 0.5-ATR bracket pass through exactly the
  same qualification checks.

### 6. Delivery + outcome tracking

- Both light- and heavy-sweep **qualified** signals open a kelly-sized
  paper position (`paper_book.open_position`, fire-and-forget) — confirmed
  both paths call it, not just heavy.
- Every non-neutral signal (qualified or not) is captured to
  `nt_signal_sim` regardless — this is the population Phase 1/2 build
  their diagnostics from.
- `eod_resolver.py` (nightly) forward-resolves pending signals via a
  close-walk against post-entry candles, writes `outcome`/`mfe`/`mae`/
  `pnl_*`/(now, Phase 2) `drift_*_pct`, and inserts a `win`/`loss`
  calibration record for `tp_hit`/`sl_hit` only — `expired` never
  produces a calibration record at all (by design, not a bug: "a timeout
  is neither a win nor a loss").

### 7. Analysis layer — Phases 1-3 (this session)

`nt_bracket_reachability` (hit-rate/expiry per symbol×ATR-bucket),
`nt_calibration_bias_by_bucket` (bias/expectancy-in-R/daily-expectancy
per bucket, joined with reachability), `nt_entry_drift` (post-entry
price drift). All read-only, weekly refresh, **nothing downstream reads
any of them yet** — confirmed via grep across `regime_platform/routers/`
and everywhere else.

### 8. Feedback into EV — what's alive vs. dead-end

This is the part worth mapping precisely, because "does anything feed
back" turned out to have a more nuanced answer than expected.

**Alive** (measured outcome → real live-behavior change, with a traced
mechanism):

| Measurement | Path | Live effect |
|---|---|---|
| Calibration bias (per-symbol, pooled across all buckets) | `eod_correction.py` (4h/nightly) or `drift_monitor.py` (per-tick, overconfident-only) → `nt_eod_actions` → `calibration_enforcement.apply_calibration_adjustment` (every tick, qualifiable signals only) | Mutates `p_win` (Bayesian shrink toward 0.5) and `effective_kelly` (×0.5, or ×0.25 under Tier-1 stagnation, or ×0.0 hard kill under Tier-2) in place; `ev`/`rr`/`allowed`/`aggression` recomputed from the shrunk `p_win` |
| Realized win rate (per-symbol) | `calibration_enforcement.fetch_historical_p_win` → `p_historical` | Feeds `compute_ev`'s blend **directly**, independent of the post-hoc shrink above — a second, separate measured→p_win path |
| `degeneracy_breaker`/`emergency_heavy_sweep` recommendation | → `_execute_emergency_heavy_sweeps` | Re-runs the actual brick-geometry optimization for that symbol — the one feedback path that changes *inputs* rather than just correcting `p_win` after the fact |
| Mean \|raw bias\| system health | `auto_tuner.py` (daily) → `tuning_registry` | **Contradicts its own docstring** ("nothing downstream reads these yet") — confirmed live via `eod_correction.py`/`calibration_enforcement.py`/`drift_monitor.py`'s `_settings_number` helper, which checks the registry FIRST. An explicit end-to-end test suite (`tests/test_e2e_registry_wiring.py`) proves all 11 tuned params reach live behavior. The comment is stale, not the mechanism. |

**Dead-end** (written, nothing live reads it): `parameter_decay_check`,
`regime_shift_alert` ("Layer 2," explicitly documented as
detection-only, never built), and this session's three new tables.

**Magnitude check on the Bayesian shrink** (from the feedback-layer
investigation): at the minimum qualifying sample (`n=20`,
`BAYES_PRIOR_COUNT=10`), the shrink moves `p_win` by ~5 points — a real
effect. By `n=100`, the same shrink moves it by ~1.4 points — functionally
a no-op. So the mechanism is strongest exactly when a symbol has *just*
been flagged with a thin sample, and decays toward irrelevance for
symbols that have been chronically miscalibrated with a large accumulated
record count — arguably backwards from where you'd want the strongest
correction. `KELLY_REDUCTION` (a flat ×0.5/×0.25/×0.0 cut) doesn't decay
this way and is the more decisive lever in practice.

---

## Cadence timeline

| Cadence | What happens |
|---|---|
| Every 5-15 min (per-symbol `regime_cron`) | Light sweep: price fetch → both HMMs refit → EV computed → qualification gates → (if qualified) paper position + Discord + drift-monitor check |
| Every 15 min (TTL) | `p_historical`/`p_timesfm` caches refresh |
| Every 4h | Intraday EOD correction pass (raw-bias mode, wider thresholds) |
| Nightly, 00:32 UTC | `eod_resolver` (forward-resolve pending signals) → `paper_book.reconcile` → `eod_correction` (enforced-bias mode) |
| Daily, 01:30 UTC | Tier D auto-tuner (one parameter nudged, evaluated against previous cycle) |
| Weekly, per-symbol `sweep_cron` | Heavy sweep: brick-geometry grid search → YAML write → (this session) bracket-reachability + calibration-bias-by-bucket + entry-drift refresh, Sun 07:00 UTC |

---

## The core finding: the existing feedback loop is symbol-level, not bracket-level

Section 8 shows real feedback already exists and is genuinely live — this
isn't a system with no learning loop. But every alive path shares the
same shape: it measures and corrects **`p_win` pooled per symbol**
(`fetch_calibration_bias`, `fetch_historical_p_win`), then applies a
uniform shrink/reduction to whatever signal that symbol generates next —
completely independent of which bracket-width bucket that specific
signal falls into.

That's exactly the blind spot Phase 2 found and had to build around: a
symbol's pooled calibration bias is dominated by whatever bucket has the
most volume (usually the widest, least-reachable one, per Phase 1's own
numbers), so the "correction" the existing enforcement layer applies is
tuned to the wrong population most of the time. A symbol like XAUUSD
gets the *same* Bayesian shrink and Kelly reduction whether the specific
signal about to be qualified sits in its well-calibrated 0.5-1 ATR bucket
(+1.58 expectancy) or its poorly-calibrated 4+ ATR bucket (-0.21
expectancy) — the enforcement layer has no way to tell the two apart,
because it was never given bucket-level data to work with.

So the honest framing: Phases 1-3 didn't discover a system with *no*
feedback — they discovered that the one feedback axis that exists
(per-symbol `p_win` correction) is orthogonal to the one that actually
matters most for a large share of the miscalibration (per-bracket-width
reachability/expectancy). Both axes are real; only one is closed today.

---

## Candidate intervention points — corrected 2026-08-26 after user review

The original four-item list below was reviewed and corrected. Two items
were wrong or incomplete as first framed; recorded here rather than
silently edited, since the correction is itself worth remembering.

**#4 (Markov/regime re-estimation stability) was not an open gap.** The
EMA smoothing on `p_markov_hold_n`/`trend_probs` (α=0.30) is the
deliberate, considered fix for the tick-to-tick noise this document
described — not a patch standing in for a real fix still owed. Removed
from the candidate list.

**TimesFM's role is broader than "one more blended source."** It's
intended to guide the probability forecast using signal from the wider
symbol pool, not just the one symbol's own price history — the
robustness a cross-sectional/market-wide view provides comes from
TimesFM's pretraining breadth, even though each call is per-symbol. This
document previously implied that kind of broader-than-one-symbol
awareness was entirely absent from the pipeline; it isn't — it's TimesFM's
job, by design.

**`compute_ev`'s formula (old #3) is off the table, not just high-risk —
and there's a direct precedent.** `docs/mse-implementation-plan.md`
(2026-08-08, status PLANNED — not built) scoped exactly this kind of
EV-engine change: a Market Sync Engine producing a 6-theme-basket
correlation score, with two integration options — **Method A**, a Gate 8
qualification check (doesn't touch `compute_ev` at all), and **Method
B**, a 5th blended EV source (`p_mse`, does touch the formula). MSE was
never built, and reworking the EV engine is the reason — the same
conclusion reached independently here. Not re-opening this.

**Enforcement-layer bucket-awareness (old #2) has a real risk this
document missed**: per-(symbol, bucket) historical stats are backward-
looking and uncorrelated across symbols. If the *entire market* shifts
character (a broad vol regime change, not a single symbol drifting), a
bucket-level correction has no way to notice — it would keep applying a
now-stale historical pattern with more granular, and therefore more
confidently wrong, precision than today's per-symbol pooled correction
does. This is precisely the gap MSE's basket-correlation signal was
designed to cover (Method A specifically — a qualification gate, not an
EV-formula change, so it doesn't conflict with the point above). Without
some market-wide-shift detector in place first, making the enforcement
layer bucket-aware trades one blind spot (bracket-width) for exposure to
another (systemic regime shift) that nothing currently guards against.

### Where this leaves the list

1. **Heavy sweep's grid search (Phase 3, already scoped)** — unaffected
   by any of the above; still the lowest-risk, already-detailed option.
2. **Enforcement-layer bucket-awareness** — real value, but per the
   correction above should not ship without a market-wide-shift
   safeguard alongside it. MSE Gate 8 (Method A, never built, no EV
   engine involvement) is the previously-scoped candidate for exactly
   that safeguard — worth treating as a prerequisite or companion, not
   a separate unrelated idea.
3. **`compute_ev` formula rework** — closed. Same conclusion the MSE
   review already reached; not revisiting.

## Explicitly not decided here

Whether to scope #2 (and/or dust off MSE Method A as its companion
safeguard) is an open question for review, not a recommendation made
here.

## New section: long-range macro-regime filter (scoped 2026-08-26)

Prompted by a direct question: what in this pipeline would notice a
sustained, multi-week-to-multi-month bear market, as opposed to
tick-level noise? Answer, checked against the code rather than assumed:
**nothing does, anywhere, including MSE if it were built.**

### Why nothing catches this today

Every regime-sensing component operates on a short window and is refit
from scratch each cycle — none persist a belief across cycles:

| Component | Window | Persists across cycles? |
|---|---|---|
| `RegimeHMM` (trend_probs → EV) | 200 bars ≈ 8 days | No — refit from scratch every 5-min tick |
| `HmmModel` (Markov matrix) | 300-bar buffer ≈ 12.5 days | No — refit every tick from the rolling buffer |
| MSE's theme-correlation window (unbuilt) | 200 bars ≈ 8 days | Would be the same order of magnitude as the above — **MSE would not close this gap even if built** |
| TimesFM | 1 bar ahead | No trend memory at all |
| Heavy sweep's backtest window | 2160 bars = 90 days (the longest lookback anywhere) | Used only to grid-search brick geometry for Sharpe — never classifies or persists a regime label, and re-runs from scratch weekly |

Everything in this pipeline fetches **1h bars, always** — confirmed
`price_fetcher.py`'s `fetch_tradingview_batch` is hardcoded `interval="1h"`
at every call site. A genuine multi-month filter needs a much longer
calendar span than hourly resolution can cheaply provide (a year in 1h
bars is ~8760 bars vs. today's 2160-bar max).

**Good news found while checking feasibility**: `fetch_tradingview_batch`
already supports a daily interval — `interval_map` includes `"1D"/"1d" →
"D"` (`price_fetcher.py:98-101`), just never called with it anywhere in
the sweep path. Fetching a year of daily bars (~250-365 bars) is cheap —
no new API integration, no new rate-limit exposure, just a new call site
using an interval the fetcher already understands.

### Proposed filter (simple and interpretable, matching this codebase's
own stated preference — see `auto_tuner.py`'s docstring on deliberately
avoiding black-box optimizers)

- New daily-cadence fetch (`interval="1D"`, ~250-365 bars) per symbol —
  separate from both the light-sweep (1h/200) and heavy-sweep (1h/2160)
  paths.
- A slow, persisted scalar: e.g. `(price - SMA_200d) / SMA_200d`,
  EMA-smoothed with a long half-life, classified into
  `bear|neutral|bull` via fixed thresholds plus a `macro_strength` in
  [0,1]. Deliberately not a fresh HMM re-fit each cycle — the whole
  point is a state that changes slowly and remembers, unlike everything
  else in this pipeline.
- Refreshed daily (matches its own data resolution) and persisted (new
  small table, e.g. `nt_macro_regime`, one row per symbol) — same shape
  as this session's other diagnostic tables.

### How it reaches the EV engine — without reworking it

This is the part that has to be gotten right given the standing
constraint (see [[ev_engine_no_rework]]): the mechanism must use the EV
engine's *existing*, already-live extension points, not add new ones.
Confirmed two such points exist and are already proven in production:

1. **`temp_weights()`** (`ev_engine.py:351-...`) — a context manager that
   temporarily overrides the blend weights for one `compute_ev()` call,
   guaranteed to restore afterward. **Only accepts the four existing
   source keys** (`p_historical`, `p_regime`, `p_markov_hold_n`,
   `p_timesfm`) — there is no path through this API to add a 5th source,
   which is exactly the boundary that keeps this from becoming MSE's
   rejected Method B under a different name. Already used live today for
   the TimesFM soft gate and the R4 trend-HMM non-convergence discount
   (`heavy_sweep.py`: `with self._ev_engine.temp_weights(_regime_weight_override):
   ev_result = self._ev_engine.compute_ev(...)`). A macro-bear reading
   could discount `p_timesfm`/`p_markov_hold_n` (the noisier, faster
   sources) toward `p_historical`, proportional to `macro_strength`,
   right before `compute_ev()` runs — "the EV engine reads it before it
   computes," as asked for, with zero changes to `compute_ev` itself.
2. **`recompute_ev_fields(p_win, profit, stake)`** (`ev_engine.py:719-...`)
   — the sanctioned post-hoc path, already used by
   `calibration_enforcement.apply_calibration_adjustment` for the
   Bayesian p_win shrink and Kelly reduction. A macro-bear reading could
   damp `p_win` toward 0.5 and/or multiply `kelly_f` when the trade's
   direction fights the macro trend, then call this to keep
   `ev`/`rr`/`allowed`/`aggression` consistent — exactly the same shape
   as the calibration-enforcement step that already runs on every
   qualifiable tick today.

Both are non-negotiable boundaries, not a style choice: neither touches
`compute_ev`'s formula, the `EVFullResult`/`ComboResult` dataclasses, or
`P_WIN_WEIGHTS_FULL`'s key set. **Recommendation: option 2**, since it's
the more direct match for "tempers or heightens p_win, kelly" as stated,
and it reuses the identical pattern already proven safe in production
(`calibration_enforcement.py`) rather than introducing a new use of
`temp_weights()` for a different purpose than its two existing callers.
Open for review, not decided here.

### Relationship to MSE

This is a sibling to MSE, not a piece of it — MSE's correlation window
(~8 days) and this filter's window (~1 year) sit at genuinely different
timescales, addressing different questions (moment-of-entry
synchronization vs. sustained trend). It doesn't need MSE to exist first,
and arguably addresses a bigger risk than MSE's own gate does (a
multi-month mispriced bear market is a larger exposure than a single
desynchronized week) — worth weighing which to prioritize if only one
gets built.

### Mode gate (same ladder discipline as everything else this session)

Propose `MACRO_REGIME_MODE = off | shadow | live` (default `off`),
matching `TIMESFM_GATE_MODE`/`MARKOV_SOURCE_MODE`'s existing naming
convention. Shadow mode: compute and log what the adjustment would have
been, change nothing. This touches every live tick's `p_win`/`kelly` the
same way `calibration_enforcement` already does — same blast-radius
tier as that mechanism, so it earns the same shadow-first staging, not
Phase 3's lighter weekly-cadence treatment.

### Explicitly not in this scope

No change to `compute_ev`'s formula or dataclasses. No new
`P_WIN_WEIGHTS_FULL` key. No change to MSE's own (unbuilt) design. No
decision yet on temp_weights vs. recompute_ev_fields, or on the exact
SMA window/thresholds — those are implementation-detail decisions for
when/if this gets built.

## Live 12h check (2026-08-26) + path-forward assessment

Studio logs were not reachable from this environment (no local `logs/`
directory — `lightningstudio/logs.sh` expects a locally-running service;
the live instance runs on Lightning AI's cloud). Database checked
directly instead, window = trailing 12h from ~21:20 UTC 2026-08-26:

- **`nt_calibration_records` (new win/loss): 0.** Expected, not a fault —
  the nightly EOD resolver (00:32 UTC) was the last batch to add any,
  ~21h before the check; the next is ~3h out. A 12h window will almost
  always straddle zero resolution batches unless timed around 00:32 UTC.
- **`nt_signal_sim` resolved: 0`** — same reason.
- **`nt_eod_actions`: 184 rows, 165 of them `regime_shift_alert`.** That's
  a lot of "large regime-posterior shift" detections for 12 hours across
  ~14 symbols — and every one is a dead-end (§8 above: nothing reads
  this action type). Confirmed the `bias` field on these rows is actually
  shift *magnitude*, not calibration bias (`orchestrator.py:1475`,
  `shift_info["magnitude"]`) — XAUUSD's 0.97 reading is a near-maximal
  posterior swing, not a miscalibration score; don't conflate the two.
  This volume is concrete, live evidence for the "reading data without
  understanding it" diagnosis — `regime_shift_alert` watches `RegimeHMM`'s
  `trend_probs`/`regime_label` specifically (set right after the alert
  write, `orchestrator.py:2391-2392`), so this points at `RegimeHMM`'s
  tick-to-tick instability in particular, not `HmmModel`.
- The 4h correction pass fired on schedule (20:30 UTC) and re-penalized
  USDCAD/AAPL/US500 with `bias` values matching numbers already seen
  earlier this session (US500 still exactly `0.6752`) — concrete
  confirmation these symbols are being re-corrected on the same stale
  evidence cycle after cycle, exactly the risk this document already
  described in the abstract.

### Path forward: HMM consolidation vs. the macro filter — not either/or, but a sequencing call

They fix different problems and aren't in competition, but if choosing
what to build first: **consolidate the HMMs first.** Reasoning:
- The regime-shift noise just measured is real, current, and free to fix
  in the sense that a fix already exists half-built: `MARKOV_SOURCE_MODE
  = trend_hmm_shadow/live` (`orchestrator.py:149`) was added specifically
  because the legacy `HmmModel` was producing live NaN/state-collapse
  warnings — it lets `RegimeHMM`'s own transition matrix
  (`trend_transition_matrix_3state()`) replace `HmmModel`'s as the markov
  source. Promoting this from shadow to live (after reviewing the shadow
  log) consolidates two refit-every-tick models into one, directly
  addressing a real, currently-firing instability — without new
  engineering.
- This does **not** fix `RegimeHMM`'s own instability (the 165
  shift-alerts are driven by `RegimeHMM`, not `HmmModel`) — that's a
  separate, harder problem (still an ~8-day refit-from-scratch window).
  Consolidating removes one noise source and duplicate compute, not all
  of it.
- The macro filter solves a different problem entirely (multi-month
  blindness) that HMM consolidation does not touch — even one clean HMM
  is still only looking at ~8-12 days. Building it against a more
  stable, single-engine foundation is cleaner than building it on top of
  today's dual-engine setup.

### Does the macro filter consider qualified-signal win/loss + PnL?

**No, as scoped.** The macro filter (previous section) is purely
price-based — a long moving-average/trend-slope read on daily bars, with
zero input from `nt_signal_sim`/`nt_calibration_records`/`paper_book`
outcomes. That's by design, not an oversight: it's answering a different
question than the calibration-bias work.

### Confirmed: this is two separate parts, not one metric

- **Part 1 — macro**: is the broad market trend bullish/bearish/neutral,
  independent of any specific trade. Price-only. The filter scoped above.
- **Part 2 — signal quality**: given this symbol's own history, is *this
  specific bracket* (its distance/ATR bucket) one that actually wins,
  and by how much. Outcome-only (win/loss/PnL via `expectancy_r`,
  `pct_expired`). Phases 1-3's `nt_calibration_bias_by_bucket`.

Both are real and both matter, but they answer different questions and
should stay separate signals — not blended into one number — reaching
`compute_ev`/Kelly via their own paths (temp_weights/recompute_ev_fields
for macro; a bucket-aware read in `calibration_enforcement.py` for
signal quality, per candidate #2 above), same non-EV-rework boundary for
both.

## HMM consolidation — build notes (2026-08-26)

> **Priority**: P1 · **Task**: Consolidate the dual HMMs — default
> `MARKOV_SOURCE_MODE` to `trend_hmm_shadow`, fix `heavy_sweep.py`'s
> missing mode branch ·
> **Status**: pending (built and verified, shadow mode; live cutover not
> yet decided) · **Date**: 2026-08-26

Built the "retire HMM + shadow mode" half of the path-forward decision.
Files:

- `orchestrator.py:149` — `MARKOV_SOURCE_MODE` default changed
  `legacy_hmm` → `trend_hmm_shadow`. Zero live-behavior change (shadow
  never substitutes into the blend) — just starts real comparison data
  accumulating immediately instead of sitting off.
- `orchestrator.py` (light-sweep fit loop, ~1651) — the legacy
  `HmmModel.fit()` loop is now skipped entirely when
  `MARKOV_SOURCE_MODE == "trend_hmm_live"` (confirmed safe: TA/
  microstructure features are read directly from the `FeatureBuffer`,
  populated independently by the quote/TA fetchers, not from the fitted
  model — so retiring `fit()` doesn't break TA persistence to
  `nt_sweep_result`/`nt_signal_sim`).
- `orchestrator.py` (markov derivation block, ~1923) — only calls
  `self._hmm_model.emit_markov(symbol)` when NOT in live mode; live mode
  computes straight from `RegimeHMM.trend_transition_matrix_3state()`
  with a neutral uniform-matrix + FLAT fallback if that raises (there's
  no legacy value left to fall back to once its fit() is skipped). Shadow
  mode now also fire-and-forgets a call to the new
  `record_markov_shadow_compare()`.
- **Found and fixed heavy_sweep.py's missing branch** — it previously
  used the legacy `HmmModel` unconditionally, completely ignoring
  `MARKOV_SOURCE_MODE`, exactly the "gate added to one path, missed on
  the other" bug the MSE doc explicitly warns about (§7 risk #3).
  `_regime_sync` now returns `(model, regime)` instead of just `regime`
  so the caller can call `trend_transition_matrix_3state()`; the same
  branch (shadow log + persist, live substitute + fallback) is mirrored
  in `heavy_sweep.py`.
- New `supabase_client.record_markov_shadow_compare()` + migration `122_
  nt_markov_source_shadow.sql` — persists a running per-symbol
  disagreement-rate/avg-diff aggregate (fetch-then-upsert, best-effort)
  instead of the previous log-line-only comparison, specifically because
  this session found studio logs aren't reachable from this environment.

**Verified**: all 616 existing tests pass (`pytest tests/ -p
no:cacheprovider`, per the MSE doc's noted flag), both modules import
cleanly with no circular-import issue from the lazy
`MARKOV_SOURCE_MODE` import in `heavy_sweep.py`, and the shadow-compare
running-total arithmetic was dry-run against a stubbed httpx client
(3 synthetic observations → exactly 3 compared / 2 disagreed / 0.35
summed diff, matching hand calculation).

**Not done**: `MARKOV_SOURCE_MODE=trend_hmm_live` is not the default —
that's the follow-up decision once real shadow data has accumulated
(query `nt_markov_source_shadow`, same review-before-flip discipline as
everything else this session). Migration 122 not yet applied.

## Requirements scope: macro filter + signal quality (2026-08-26)

Formalizing both halves of the "2 parts" model from the previous
exchange into build-ready requirements. Still scope only.

### Part 1 — Macro filter requirements

**Data**: daily-interval bars (`fetch_tradingview_batch(interval="1D")`,
confirmed already supported, see earlier section), ~250-365 bars per
symbol, fetched on its own daily cadence — separate from both existing
sweep paths.

**Computation**:
- `macro_score = (last_close - SMA_200d) / SMA_200d`.
- EMA-smoothed with a multi-day half-life (daily bars still carry
  single-day noise; the whole point is a state that doesn't flip on one
  bad print).
- `macro_label = bear if macro_score < -threshold, bull if > +threshold,
  else neutral` (threshold TBD, candidate 0.05-0.10, env-tunable —
  matches this codebase's convention of hardcoded-but-tunable gates,
  e.g. `MIN_RR`, `EOD_DRIFT_THRESHOLD`).
- `macro_strength = min(1.0, abs(macro_score) / normalization_factor)`
  — a 0-1 magnitude, not just a label.

**Storage**: new table, one row per symbol (`symbol` PK) — e.g.
`nt_macro_regime(symbol, macro_score, macro_label, macro_strength,
sma_window_days, computed_at)`. Refreshed daily.

**Integration**: mirrored in *both* sweep paths (learned that lesson
today) — read right before `compute_ev()`, applied via
`recompute_ev_fields()` (recommended per the earlier scope) as a Kelly
multiplier: dampened when the trade's direction opposes `macro_label`
(proportional to `macro_strength`), and — since the ask was explicitly
"tempers **or heightens**" — a symmetric boost when direction agrees,
e.g. `kelly_macro_mult = 1.0 ± (macro_strength * MACRO_ADJUST_SCALE)`,
sign depending on agree/disagree, clamped to a sane range (e.g.
`[0.25, 1.5]`) so this can't alone zero out or double a position.

**Mode gate**: `MACRO_REGIME_MODE = off | shadow | live`, default `off`.

### Part 2 — Signal-quality requirements (formalizing what's already built)

**Data**: already exists — `nt_calibration_bias_by_bucket`
(`expectancy_r`, `pct_expired`, `daily_expectancy_r` per symbol ×
distance/ATR bucket, Phases 1-2). No new migration needed.

**New requirement — bucket lookup at signal time**: at the point
`compute_ev` is about to run, the specific signal's own `distance/ATR`
ratio is already computable (`atr`, `brick_size`, `tp_bricks` are all in
scope before `compute_ev` is called, per Phase 1's own capture). Bucket
it the same way `bracket_reachability.py` does, and look up *that*
bucket's row — not a pooled per-symbol number — mirrored in both sweep
paths.

**Consumption**: same shape as Part 1 — a `recompute_ev_fields()`-based
Kelly multiplier, this time driven by `expectancy_r`/`daily_expectancy_r`
for the matched bucket: strongly discount (or gate to zero) when
`expectancy_r <= 0` with `n` above a minimum sample threshold (matches
`EOD_BIAS_MIN_RECORDS`'s existing convention — don't act on a thin
bucket); a mild boost when strongly positive. Below the minimum sample
threshold: no adjustment (insufficient evidence, not evidence of
mediocrity).

**Mode gate**: a second, independent gate — e.g.
`SIGNAL_QUALITY_GATE_MODE = off | shadow | live`, default `off`.

### How the two parts combine — kept separate, composed multiplicatively

Per the prior conclusion (two dials, not one blended metric): apply each
as its own independent Kelly multiplier, then compose by multiplication
— `kelly_final = kelly_base * kelly_macro_mult * kelly_quality_mult *
(existing calibration-enforcement shrink) * (existing p_win soft-gate
shrink)`. This preserves the "macro says X, signal quality says Y" as
two legible, separately-inspectable numbers (both worth exposing in the
`gate_results`/`passed_gates` telemetry dict, same as every other factor
already surfaced there) rather than collapsing them into one score that
hides which axis is actually driving the outcome.

**Named risk, not new**: the MSE doc's own §7 risk list already flagged
"Kelly double-counting... stacking an MSS risk-scaler needs care to
avoid triple-penalising (pattern → EV → MSE)" for exactly this
multiplicative-stacking pattern. We're now up to *four* multiplicative
Kelly dampers if both parts ship (calibration-enforcement shrink,
p_win soft-gate shrink, macro, signal-quality) — worth a combined-effect
sanity check (e.g. worst-case product across all four) before flipping
any of them live, not just each in isolation.

### Explicitly not decided here

Exact thresholds (`macro` bear/bull cutoff, `MACRO_ADJUST_SCALE`, the
minimum-sample gate for signal-quality bucket lookups), and whether both
parts ship together or staged one at a time. Scope only — awaiting a
build instruction, same discipline as everything else this session.

## Shadow-mode verification (2026-08-26) — corrected after review

First pass on this (below the line) stopped at one symptom (FLAT never
appearing) and treated it as the explanation. That was an incomplete
review, called out directly and correctly. Redone properly with real
data before scoping anything.

**Mechanism check**: 873 real comparisons accumulated within ~15 minutes
across all 14 symbols, correct arithmetic, no errors — the shadow
pipeline itself works. Overall disagreement rate: **83.2%** (54.0% to
96.9% by symbol) — worse than the ~66.7% two independent random
3-state classifiers would produce by chance, meaning the two engines
aren't just noisy relative to each other, something structural is wrong.

**Root cause, found by actually fitting `RegimeHMM` on real recent price
data (200 bars) and inspecting the raw component means, not just the
final collapsed label** — `regime_engine.py::_label_states` assigns
`strong_bear/bear/bull/strong_bull` by **pure rank order** among whichever
4 components got fit that tick (`_label_states:668-685`, rank-sort with
no previous-fit continuity; the continuity path just nearest-matches to
a previous label's mean — neither path has an absolute magnitude or sign
check anywhere). Real output, `feature std` = the trend feature's own
noise level that tick:

| Symbol | strong_bear | bear | bull | strong_bull | feature std |
|---|---|---|---|---|---|
| AAPL | -2.30sd | -2.09sd | **-2.09sd** | +0.14sd | 0.0433 |
| US500 | -2.01sd | -0.62sd | **-0.23sd** | +0.07sd | 0.0046 |
| AUDUSD | -0.88sd | **+0.54sd** | +1.11sd | +1.30sd | 0.0036 |
| EURUSD | -0.46sd | **+0.11sd** | +0.43sd | +2.42sd | 0.0033 |
| XAUUSD | -0.36sd | +0.49sd | +1.53sd | +2.04sd | 0.0143 |

AAPL's component labeled **"bull" is 2.09 standard deviations *below*
zero** — a strongly bearish reading, called bullish purely because it
ranks 3rd-lowest of 4. AUDUSD's "bear" is **positive** (+0.54sd). This is
not a missing-FLAT edge case — the labeling scheme always forces a full
bear→bull spread onto whatever 4 components got fit, real trend or not,
with zero check on whether any of them are actually near zero or even on
the correct side of it. Legacy's 5-state model does the opposite:
`threshold = STATE_MAP_THRESHOLD_STDS * lr_std`
(`model.py:198`) — a component only gets UP/DOWN if its mean *clears* an
absolute, noise-scaled threshold; otherwise it's FLAT. `RegimeHMM`'s
trend labeling has no equivalent anywhere. (Also checked: the 3-label
variant, `["bear","neutral","bull"]` for `n_components=3`, uses the exact
same rank-sort function — switching to it would not fix this; "neutral"
would just become "whichever component ranks in the middle," with the
same blindness to actual magnitude.)

**This is bigger than the markov shadow comparison.** The same label map
built by `_label_states` is what produces `trend_probs`, which feeds
`p_regime` directly into `compute_ev` — **already live**, unrelated to
anything built this session. If a symbol's 4 trend components cluster on
one side of zero (AAPL, US500, AUDUSD, EURUSD above all show this
pattern in real, current data), `p_regime`'s directional read may
already be corrupted in production, independent of which
`MARKOV_SOURCE_MODE` is active. This deserves its own explicit attention
— flagged here, not fixed here, and not bundled into the narrower fix
below.

### Fix scope — two tiers, deliberately separated by risk

**Tier 1 (narrower, lower risk) — fix the 3-state markov derivation only.**
Add a magnitude gate to `trend_transition_matrix_3state()`
(`regime_engine.py:427`) itself: before mapping a labeled component to
UP/DOWN via `_TREND_TO_3STATE`, check `abs(mean) / feature_std` against a
threshold (same shape as legacy's `STATE_MAP_THRESHOLD_STDS`); below
threshold, map to FLAT regardless of the component's rank-assigned label.
This fixes the markov-comparison/emission path specifically (what
`MARKOV_SOURCE_MODE=trend_hmm_shadow/live` actually uses) **without**
touching `_label_states`, `TREND_LABELS`, or `trend_probs` — i.e. without
touching the EV blend's already-live `p_regime` input at all. Threshold
goes on the git-tracked config file (see below), not a bare constant.
Re-run the shadow comparison after this ships and check whether the
disagreement rate drops toward something explicable (ideally well under
the ~67% random-chance baseline) before revisiting `trend_hmm_live`.

**Tier 2 (broader, higher risk, flagged not fixed) — `_label_states`
itself.** The rank-only assignment feeding `trend_probs`/`p_regime` is
the more consequential bug, since it's already live. A real fix means
giving components an absolute magnitude/sign gate at the source, which
changes what `trend_probs` means for every symbol whose components
cluster on one side of zero — a change to an EV-blend input that's been
running in production, not a shadow-mode addition. This needs its own
deliberate review before touching it, separate from Tier 1 and from
everything else in this document — not something to fix as a drive-by
alongside the markov-shadow work.

## Tier 1 fix + config file migration — build notes (2026-08-26)

> **Priority**: P1 · **Task**: Add a magnitude gate to RegimeHMM's 3-state
> markov derivation; migrate mode/threshold flags to git-tracked
> `config/feature_flags.yaml` ·
> **Status**: pending (built and verified against real data, shadow mode
> only) · **Date**: 2026-08-26

Built both in one pass since the fix's own threshold needed a config
home anyway. Files:

- **New `config/feature_flags.yaml`** (git-tracked) + **new
  `regime_platform/core/feature_flags.py`** — holds `markov_source_mode`
  and `markov_flat_gate_threshold_stds`. Hot-reloads on the file's mtime
  (verified: edited the file on disk, the very next call picked up the
  new value with no process restart, then reverted). Deliberately
  consults no environment variable — a leftover Studio Secret now has
  zero effect on these two values.
- **`regime_engine.py::trend_transition_matrix_3state`** — the Tier 1
  gate. Computes the labeled component's mean against
  `markov_flat_gate_threshold_stds * sigma` (`sigma` from `self._trend_scale`,
  the same fit-time noise scale already computed for feature
  standardization — no new computation, reused what was already there).
  A component only keeps its rank-assigned UP/DOWN label if it clears
  the threshold in that direction; otherwise FLAT. Applied to both the
  full `T3` matrix and `current_state` (previously `current_state` was
  derived by mapping the label directly, which would have bypassed the
  gate for the one reading that matters most).
- **`orchestrator.py` + `heavy_sweep.py`** — `MARKOV_SOURCE_MODE` no
  longer read from `os.getenv()`; both now call
  `feature_flags.get_markov_source_mode()` fresh each tick (cheap,
  mtime-cached). `heavy_sweep.py`'s lazy import changed from
  `orchestrator.MARKOV_SOURCE_MODE` to `feature_flags.get_markov_source_mode`
  directly — simpler, and removes a dependency on `orchestrator.py`
  entirely for this value.
- **`tests/test_regime_stability.py`** — `_make_fitted_stub` extended to
  accept `means`/`sigma` (previously the stub didn't set `means_` at
  all, since the pre-fix code never read it). Default means are set
  comfortably beyond the gate threshold so the 3 existing tests that
  predate the fix keep exercising the aggregation math unchanged. Added
  two new regression tests reproducing the exact real-data failures
  found: a component on the wrong side of zero (AUDUSD's "bear" at
  +0.54std) and a near-zero component regardless of sign (AAPL's "bull"
  pattern) — both now correctly gate to FLAT.

**Verified against the real data that exposed the bug**: re-ran the
gated derivation live for AAPL/US500/AUDUSD/EURUSD/XAUUSD. AAPL, US500,
AUDUSD, and EURUSD all still produce confident directional readings
(their components genuinely clear the threshold). **XAUUSD's
current rank-label is "strong_bear," but the gate correctly downgrades
it to FLAT** — its component doesn't clear the noise threshold right
now, exactly the kind of overconfident misclassification this fix
exists to catch. Not over-suppressing everything to FLAT, just the
genuinely weak cases.

All 618 tests pass (`pytest tests/ -p no:cacheprovider`), including the
2 new regression tests.

**Not done**: Tier 2 (the `_label_states`/`trend_probs`/live `p_regime`
issue) — flagged above, deliberately not touched. `MARKOV_SOURCE_MODE`
in `config/feature_flags.yaml` ships at `trend_hmm_shadow` (unchanged
from where it already stood) — this build does not flip it to live.

## 12h data assessment (2026-08-27, before Tier 2)

Studio logs still unreachable from this environment (no local log
access, no connection path to the Studio instance) — data only.
1808 signals captured, 0 errors, 44 qualified (2.4%), all pending
(expected — nightly-batch resolution, nothing in a 12h window should
have resolved yet). Markov shadow (Tier 1) over a longer accumulation
window: disagreement down to **67.9%** (from 82-83%) over 3491 compared,
`trend_hmm` now showing FLAT on 8/14 symbols (up from 4/14) — converging
toward the ~66.7% "just noise between two classifiers" baseline, real
confirmation the Tier 1 fix keeps working as more data accumulates.
`regime_shift_alert` volume also dropped (165→147 per 12h) but that
alert is driven by `regime_label`/`trend_probs` continuity, a different
code path than Tier 1 touched — noted as a correlation, not claimed as
caused by it.

## Tier 2 HMM fix — build (2026-08-27)

> **Priority**: P1 · **Task**: Apply the same magnitude/sign gate to
> `trend_probs`, feeding the already-live `p_regime` EV-blend input ·
> **Status**: pending (shadow-gated; not yet flipped live) ·
> **Date**: 2026-08-27

Applies the identical magnitude/sign gate from Tier 1 to the OTHER
consumer of `_label_states`' rank-only assignment: `trend_probs` itself,
which feeds `p_regime` directly into the **already-live** EV blend via
`ev_engine.EVEngine.compute_p_regime`. This is the bigger, higher-stakes
half flagged and deliberately not touched in the Tier 1 build — built
now with the same shadow-first discipline, not flipped live.

### Design — stays entirely out of `ev_engine.py`

`compute_p_regime(trend_probs, direction)` already handles a
`"neutral"` key correctly: `p = p_bullish + 0.5 * p_neutral` — that
mechanism exists for the 3-state label variant (`["bear","neutral","bull"]`)
but is unused by the live 4-state config, which has no `"neutral"` label
at all. New `RegimeHMM.gated_trend_probs()` (`regime_engine.py`) reuses
this: for each of the 4 raw posterior probabilities, if the labeled
component doesn't clear the same `markov_flat_gate_threshold_stds` gate
Tier 1 uses, its mass moves into an explicit `"neutral"` key instead of
staying under a directional label it doesn't deserve. Returns a strict
superset of what `compute_p_regime` already expects — **zero changes to
`ev_engine.py`**, respecting the standing "no EV engine rework"
boundary the same way Tier 1 did.

### Wiring — both sweep paths, shadow-gated

- `orchestrator.py::_process_symbol` — gate applied to
  `_smoothed_trend_probs` (the EMA-smoothed distribution, not the raw
  per-tick posterior — gating the final distribution about to be used,
  consistent with Tier 1's "gate at the point of consumption").
  `shadow` mode computes both raw and gated `p_regime` via the
  unchanged, pure `compute_p_regime` staticmethod and persists the
  comparison; only `live` mode substitutes the gated `trend_probs` into
  the real `compute_ev()` call (both call sites in this function share
  the same `_smoothed_trend_probs` variable, so gating it once covers
  both).
- `heavy_sweep.py` — mirrored, same shape, using `regime_model`
  (available since Tier 1's `_regime_sync` return-shape change).
- New `trend_probs_gate_mode` in `config/feature_flags.yaml` (git-tracked,
  same file as Tier 1's threshold) — defaults to `shadow`, same
  reasoning as `markov_source_mode`'s default (shadow mode is provably
  zero-live-impact, so defaulting to it just starts collecting real
  comparison data immediately; `live` is a separate, deliberate decision).
- New `nt_trend_probs_gate_shadow` table (migration 123) + `supabase_client.
  record_trend_probs_gate_shadow()` — same running-aggregate shape as
  `nt_markov_source_shadow`, **`updated_at` included in the payload from
  the start this time** (learned from the follow-up fix needed on the
  Tier 1 table).

### Verified — and the real magnitude is bigger than expected

3 new regression tests (pass-through when nothing needs gating, mass
correctly moved to neutral for a mislabeled component, `RuntimeError`
before fit) — all pass, plus the full suite (621 passed, 0 failed).

Real data against the exact symbols that exposed the bug:

| Symbol | direction | raw p_regime | gated p_regime | diff |
|---|---|---|---|---|
| AAPL | long | 0.9700 | 0.4998 | **-0.4702** |
| US500 | short | 0.9700 | 0.5000 | **-0.4700** |
| AUDUSD | short | 0.8947 | 0.8947 | -0.0000 |
| EURUSD | long | 0.8313 | 0.9157 | +0.0843 |
| XAUUSD | short | 0.8281 | 0.4140 | **-0.4140** |

AAPL and US500's `p_regime` was pinned at the 0.97 cap — the blend
essentially certain of a direction — purely because the model's
posterior mass concentrated on a component that was mislabeled
"strong_bull"/"bear" by rank while its actual mean sat within noise
(AAPL) or not measured at all confidently enough to warrant that
certainty. Gated, both correct to ~0.50 (genuinely neutral). XAUUSD
drops from 0.83 to 0.41 for the same reason. AUDUSD is essentially
unchanged — its dominant component's label was correct, confirming the
gate isn't indiscriminately suppressing everything, only the genuinely
mislabeled cases. EURUSD *increases* (0.83→0.92) — a "bear" component
that didn't deserve the label was dragging the long-direction score
down; removing it from the directional pool (into neutral, which
contributes half-weight) raised the score. `p_regime` carries a 0.25
weight in the 4-source blend — corrections of this size are not noise;
they're the difference between "confidently one-sided" and "actually
uncertain" for at least 3 of the 5 symbols checked.

**Recommendation, not decided here**: let `nt_trend_probs_gate_shadow`
accumulate for a period (same review-before-flip discipline as
`markov_source_mode`) before considering `trend_probs_gate_mode=live`
— but given the magnitude found, this is a strong candidate for
prioritized review, not a low-urgency backlog item.

## Macro filter — full build scope (2026-08-26)

Confirmed as the right next build regardless of the HMM finding above —
different problem, doesn't depend on resolving it. Full scope, superseding
the earlier requirements sketch with build-ready detail.

### Data

- `fetch_tradingview_batch(symbols, hours=300, interval="1D")` — already
  supported, confirmed earlier. 300 daily bars (~14 months) gives a full
  200-day SMA plus buffer, per symbol.
- New fetch, own cadence: **once daily**, not on the light/heavy sweep
  ticks. Time TBD but should land before both the light-sweep day starts
  and the existing 07:00 UTC Sunday reachability job — candidate `06:00
  UTC daily`.

### Computation

```
macro_score          = (last_close - SMA_200d) / SMA_200d
macro_score_smoothed = EMA(macro_score, half_life_days=10)
macro_label           = "bear" if macro_score_smoothed < -BEAR_BULL_THRESHOLD
                         "bull" if macro_score_smoothed > +BEAR_BULL_THRESHOLD
                         else "neutral"
macro_strength        = min(1.0, abs(macro_score_smoothed) / STRENGTH_SATURATION)
```
Candidate defaults: `BEAR_BULL_THRESHOLD = 0.05` (5% off the 200d SMA),
`STRENGTH_SATURATION = 0.15` (full strength at 15%+ off). Both tunable,
not hardcoded — see Config below. EMA-smoothing `macro_score` (not the
SMA itself) matters because a single day's close is noisy relative to a
200-day average; the whole point of this filter is a state that doesn't
flip on one print.

### Storage

New table, one row per symbol (`symbol` PK): `nt_macro_regime(symbol,
macro_score, macro_score_smoothed, macro_label, macro_strength,
sma_window_days, computed_at)`. Refreshed daily, upsert-on-conflict, same
shape as every other diagnostic table this session.

### Config — corrected 2026-08-26: config params belong in git, not a runtime-only file

The previous version of this section proposed `tuning_registry.py`'s
YAML rail — but that file is deliberately `.gitignore`d, for a specific,
different reason (its own comment, `.gitignore:115-121`): it's
**live-mutated runtime state** written by the auto-tuner, and the repo
already learned the hard way that a *tracked* file which something else
keeps rewriting just goes stale (`config/brick_params.yaml` "is tracked
in git but drifts... the tracked file just goes stale"). That's the
right call for values the *app* writes automatically. It is the wrong
model for `MACRO_REGIME_MODE` and its thresholds, which are values a
*person* sets deliberately — exactly the same category as
`MARKOV_SOURCE_MODE` is today, just on a Studio Secret instead of a file.
Corrected position: **a real secret (API keys, credentials) belongs in
Studio Secrets. A config parameter — mode flags, thresholds, anything a
person deliberately sets — belongs in the repo,** reviewable in a diff,
with no separate platform UI required to know what's actually running.

**Proposed**: a new, git-tracked file — e.g. `config/feature_flags.yaml`
— holding exactly this class of setting:
```yaml
markov_source_mode: trend_hmm_shadow   # off | legacy_hmm | trend_hmm_shadow | trend_hmm_live
markov_flat_gate_threshold_stds: 0.5   # Tier 1 HMM fix, above

macro_regime_mode: off                 # off | shadow | live
macro:
  bear_bull_threshold: 0.05
  strength_saturation: 0.15
  boost_scale: 0.3
  penalty_scale: 0.5
```
Loaded via a small, dedicated reader (sync file read, safe to call from
hot per-tick paths, same performance shape as `tuning_registry.read_param`
but without its float-only/auto-tune machinery) — no Supabase fallback
needed here since nothing but a human commit ever changes these values,
unlike the auto-tuner's registry. `MARKOV_SOURCE_MODE` itself should
migrate onto this same file rather than staying on the env-var/Studio-
Secret path that caused this session's incident — one file, one
mechanism, for every deliberately-set flag in this family, not two
different systems depending on which feature added the flag first.

This directly targets the actual problem: a Studio Secret is invisible
outside the platform UI and can silently diverge from what the code
appears to default to. A committed YAML file has neither property — its
current value is exactly what's in the last commit, visible to anyone
with repo access, changed the same way every other code change is
changed.
about rather than overselling the fix.

### Integration

Mirrored in **both** sweep paths (the exact lesson from the heavy_sweep.py
gap found this session) — read `nt_macro_regime` for the symbol right
before `compute_ev()` runs in `orchestrator.py::_process_symbol` and
`heavy_sweep.py::run_heavy_sweep_for_symbol`. Applied via
`recompute_ev_fields()` (the existing, already-live post-hoc mechanism —
see the earlier scope note on why this over `temp_weights()`) as a Kelly
multiplier:

```
if macro_label == "neutral":
    kelly_macro_mult = 1.0
elif direction agrees with macro_label:
    kelly_macro_mult = 1.0 + macro_strength * MACRO_BOOST_SCALE   # heightens
else:
    kelly_macro_mult = 1.0 - macro_strength * MACRO_PENALTY_SCALE  # tempers
kelly_macro_mult = clamp(kelly_macro_mult, 0.25, 1.5)
```
Candidate defaults: `MACRO_BOOST_SCALE = 0.3` (max +30% at full strength),
`MACRO_PENALTY_SCALE = 0.5` (max -50% at full strength) — both on the
same YAML rail as the thresholds above. The `[0.25, 1.5]` clamp is a hard
safety band independent of the formula — this mechanism alone can never
zero out or double a position; that's reserved for the more decisive
existing kill-switch-style mechanisms (`stagnation_kill_switch`, the
`p_win` hard floor).

**Telemetry**: surface `macro_label`, `macro_strength`, and
`kelly_macro_mult` in the `gate_results`/`passed_gates` dict alongside
every other qualification factor already exposed there — keeps the "two
separate dials" principle visible in the actual signal payload, not just
in an internal variable.

**Mode gate**: `MACRO_REGIME_MODE = off | shadow | live`, default `off`.
Shadow: compute and log/persist what `kelly_macro_mult` would have been,
apply nothing. Same blast-radius tier as `calibration_enforcement` (runs
every qualifiable tick) — earns the same shadow-first staging as
everything else, not Phase 3's lighter weekly-cadence treatment.

### Verification plan

1. Compute the filter over historical daily data for all 14 symbols and
   spot-check `macro_label`/`macro_strength` against known periods (e.g.
   does it correctly read "bear" through a symbol's own historically
   worst drawdown window) before wiring it to anything live.
2. Shadow mode: run for at least 1-2 weeks, then pull
   `nt_macro_regime` history and check `kelly_macro_mult`'s distribution
   — is it mostly near 1.0 (rare, meaningful signal) or constantly
   swinging (threshold too tight, needs retuning before going live)?
3. Confirm both sweep paths read the same table/row consistently (the
   heavy_sweep.py mirroring lesson, checked explicitly this time before
   shipping, not found after the fact).

### Explicitly not in this scope

No change to `compute_ev`'s formula/dataclasses (uses the same
`recompute_ev_fields()` boundary as everything else). No decision yet on
extending `tuning_registry.py` for string values beyond what's described
above. Exact threshold/scale defaults are candidates, not final — tune
during the shadow-mode observation window, not before.

## 48h data review + outcome-validation-layer scope (2026-08-28)

Studio logs still unreachable from this environment — data only, same
limitation noted throughout.

### 48h findings

6948 signals captured, 0 errors, 2.4% qualified. 6459 resolved: 71.8%
expired, 16.8% sl_hit, 11.4% tp_hit — same expiry-dominated pattern the
whole calibration-reframe thread addresses. First 48h window to catch
real resolution data: BTCUSD (n=114, bias -0.016) and XAUAUD (n=68,
bias +0.041) both read as genuinely well-calibrated right now; XAGUSD
(n=26) and US500 (n=18) still show real bias (+0.28, +0.31) but on
small samples. Nothing built this session is live yet, so this isn't
evidence of anything we shipped helping — most likely just this
window's market behavior.

**Tier 1**: disagreement now **61.3%** over 6749 comparisons — continued
dropping (82→68→61%) and has moved *below* the ~66.7% "two independent
classifiers" baseline. **Tier 2**: with ~220+ observations per symbol,
the correction is confirmed persistent, not a one-off — avg |diff|
ranges 0.02 (USDCAD) to 0.47 (AAPL), most symbols in between.

Both shadow tables now answer "how much do these differ" convincingly.
Neither answers "which one is right" — that's what's scoped below.

### Outcome-validation-layer — full scope

**Goal**: join the existing shadow comparisons against REALIZED outcomes
(already sitting in `nt_signal_sim`, filled in nightly by
`eod_resolver.py`) so a live-mode decision is based on evidence of
accuracy, not just evidence of disagreement.

**Mechanism — link via `signal_id`, capture per-observation instead of
only updating the running aggregate.**

1. **New table `nt_hmm_shadow_outcomes`**: `signal_id` (PK, FK →
   `nt_signal_sim.signal_id`), `symbol`, `legacy_markov_state`,
   `trend_hmm_markov_state`, `raw_p_regime`, `gated_p_regime`,
   `created_at`. One row per non-neutral signal tick — same population
   as `nt_signal_sim` itself, since the shadow diagnostics are already
   computed at that exact point in both sweep paths.

2. **Threading a shared ID** (the one real code change this needs,
   verified feasible against the actual code before writing this scope):
   - `log_signal_sim(payload, signal_id: str | None = None)` gains an
     optional override — uses it if given, generates its own otherwise.
     100% backward compatible: confirmed via
     `tests/test_signal_sim_capture.py::test_log_signal_sim_adds_signal_id`,
     which only checks a valid UUID ends up in the row, not where it
     came from.
   - `_build_signal_sim_payload` itself stays **unchanged** —
     `test_payload_has_no_signal_id` explicitly protects it from gaining
     this field, so the ID threads around that function, not through
     it: the caller (`_process_symbol`/`run_heavy_sweep_for_symbol`)
     pre-generates `signal_id = str(uuid.uuid4())` (a trivial, synchronous
     call, no async plumbing needed) before building the payload, passes
     it to `log_signal_sim`, and reuses the exact same value for the new
     `nt_hmm_shadow_outcomes` insert — both writes are fire-and-forget,
     same as everything else in this capture path.
   - The existing running-aggregate tables (`nt_markov_source_shadow`,
     `nt_trend_probs_gate_shadow`) are unaffected — this adds a
     per-observation record alongside them, doesn't replace them.

3. **New analysis module `hmm_validation.py`** (same dual module/CLI
   shape as `bracket_reachability.py`/`calibration_bias_bucket.py`):
   fetches `nt_hmm_shadow_outcomes` JOIN `nt_signal_sim` on `signal_id`
   WHERE `outcome IN ('tp_hit','sl_hit')` — expired excluded, same rule
   as everywhere else this session (a timeout is neither a win nor a
   loss).
   - **Tier 1 scoring**: ground truth `realized_direction` = which way
     price actually went relative to entry (derived from `signal`
     buy/sell + `outcome` tp_hit/sl_hit). For each engine, compute
     hit-rate against `realized_direction` **only on its own non-FLAT
     calls** — FLAT is "no answer," not a wrong answer, so it's excluded
     from the accuracy tally and reported separately as a coverage/
     abstention rate instead.
   - **Tier 2 scoring**: Brier score, `(p_regime - realized)^2` with
     `realized ∈ {0,1}` (tp_hit=1, sl_hit=0), averaged separately for
     raw vs. gated per symbol — reusing the exact scoring convention
     `eod_resolver.py::_write_calibration_record` already uses for
     `calibration_brier`, not a new metric invented for this.
   - Output: per-symbol AND aggregate comparison, with a minimum-sample
     gate (matching the `EOD_BIAS_MIN_RECORDS=20` convention already
     used elsewhere) before a symbol's result is treated as meaningful.

4. **Migration**: standard shape — new table, service-role-only RLS,
   same convention as every other table this session.

**Explicitly not in this phase**: no live-mode decision made here — this
produces evidence, not a switch flip. Neither existing shadow table's
behavior changes. No per-symbol override for `markov_source_mode`/
`trend_probs_gate_mode` (currently global-only settings) — worth
flagging since prior data (AAPL/US500 badly mislabeled, USDCAD/AUDUSD
fine) suggests the right answer may end up being per-symbol, not
global, but that's a bigger config-system change not scoped here.

**Verification plan**: once shipped, let it accumulate (48h resolved
count across all symbols was ~1822 in the review above, so a
multi-day wait is likely needed for adequate per-symbol samples above
the min-n gate), then re-run and sanity-check against what the earlier
live simulations already suggested (e.g. expect AAPL/US500 to show
`gated`/`trend_hmm` clearly outperforming `raw`/`legacy`; expect
USDCAD/AUDUSD to show near-parity, consistent with those symbols'
labels already being largely correct).

## Still not built

The outcome-validation-layer scope above is investigation + synthesis
only, same discipline as the rest of this session — no code or migration
changes for it. Superseded in practice by the root-cause build below,
per the explicit instruction not to keep tacking on more gates/tables:
dig into WHY the labels disagree instead of adding a third layer to
adjudicate between two already-disagreeing ones.

## Root-cause classifier — build (2026-08-28)

> **Priority**: P1 · **Task**: Replace rank-based HMM component labeling
> with a direct occupancy + cross-feature-sign classifier (root causes A
> and B) ·
> **Status**: pending (shadow only, no live-mode change) ·
> **Date**: 2026-08-28

**Why this replaces Tier 1 + Tier 2's internals rather than adding a
third gate.** Digging into AAPL/US500 specifically (per instruction) to
find where the platform's regime label and the "correct" label actually
diverge turned up two distinct, previously-unaddressed root causes, not
one:

- **Root cause A**: `_label_states` ranks the 4 fitted trend components
  using ONLY feature index 2 (30-bar cumulative return) — the other two
  fitted features (1-bar return, 10-bar cumulative return) are computed
  and fitted but never consulted for labeling. Real data showed several
  symbols with a component whose 3 features actively disagree in sign
  (e.g. a strongly-positive 30-bar return sitting on a component whose
  1-bar/10-bar features are negative) — an internally contradictory
  regime read that Tier 1/2's magnitude-only gates couldn't see, since
  they also only looked at feature 2.
- **Root cause B**: some fits show near-total single-component Viterbi
  occupancy collapse (e.g. AAPL, one component absorbing ~100% of the
  fit window's bars in the original investigation) — the other
  components' means are fit on almost no real observations and are
  statistical noise dressed up as a regime label, regardless of their
  magnitude. Nothing before this checked occupancy at all.

Confirmed (via `git grep` on `RegimeHMM.TREND_LABELS`) that `_label_states`
and the `TREND_LABELS`/`TREND_LABELS_BY_N` constants have a wide blast
radius — `gpu_engine.py`'s parallel GPU HMM, `simulator.py`,
`backtester.py`, and multiple external-facing routers
(`routers/regime.py`, `routers/pipeline.py`, `routers/stream_ws.py`,
`routers/stream_rest.py`, `renko/router.py`) all depend on the current
4-label shape. Widening the label set (a "true" 5th neutral state) would
touch all of them for a rank-based scheme that still wouldn't fix A or
B. Instead, the fix classifies components directly from the trend
model's own fitted parameters — bypassing the rank-assigned label
entirely — inside the two functions that already sit between the trend
model and the live EV blend (`trend_transition_matrix_3state` for Tier
1, `gated_trend_probs` for Tier 2). `_label_states`, `TREND_LABELS`, and
every consumer above are untouched.

**`RegimeHMM._classify_trend_components()`** (new, `regime_engine.py`) —
called once per Tier 1/2 read, returns `{component_index: "UP"|"DOWN"|"FLAT"}`
computed directly from the fit, in order:
1. Viterbi occupancy (bars actually assigned to this component during
   `.fit()`, via `.predict()` on the standardized features) must clear
   `markov_min_occupancy` (default 0.10) — else FLAT (root cause B).
2. All 3 raw fitted features must agree in sign — else FLAT (root cause
   A).
3. Feature 2's magnitude must clear `markov_flat_gate_threshold_stds *
   sigma` (same threshold Tier 1/2 already used) in either direction —
   UP if positive, DOWN if negative, else FLAT.

This is a stronger read than the old gates: it doesn't just ask "does
this component support ITS RANK-ASSIGNED label," it determines the
component's real direction independent of that label. A component
rank-labeled "bear" that's actually +0.54std now reports UP (not just
"gated to FLAT") — real signal the old approach discarded.
`gated_trend_probs` was updated to match: a component's probability mass
stays under its rank-assigned label only if the classifier's direct
verdict AGREES with what that label implies; a real UP/DOWN verdict that
CONTRADICTS the rank label is treated the same as FLAT (moved to
`neutral`) rather than being silently left under the wrong label.

**New config** (`config/feature_flags.yaml`): `markov_min_occupancy:
0.10` — the occupancy threshold from root cause B, same
git-tracked/hot-reload convention as the existing `markov_source_mode`/
`markov_flat_gate_threshold_stds`/`trend_probs_gate_mode` keys. No new
tables, no new params beyond this one threshold — the fix is a logic
change inside two existing functions plus one new classifier method.

**Tests** (`tests/test_regime_stability.py`): `_make_fitted_stub` extended
to accept per-component `feature_means` (all 3 features, defaulting to
matching values so cross-feature consistency passes trivially unless a
test wants to exercise it) and `occupancy` (defaulting to uniform `1/n`,
clearing the default threshold unless a test wants to exercise it). Two
new regression tests added directly for root causes A and B
(`test_trend_transition_matrix_3state_flat_when_features_disagree_in_sign`,
`test_trend_transition_matrix_3state_flat_when_occupancy_below_minimum`).
The pre-existing "wrong side of zero" test's expectation changed from
FLAT to UP, reflecting the classifier's stronger direct-reclassification
behavior described above — not a weakened test, a corrected one. Full
suite: 623 passed, 21 skipped (pre-existing/unrelated), 0 failed.

**Real-data verification**: re-ran the same per-component diagnostic
(occupancy + cross-feature sign check + classifier verdict) against a
fresh fit on live TDVA data for all 14 active symbols (56 components
total):
- Root cause A (cross-feature inconsistency) present in 8 of 56
  components, across 7 of 14 symbols (XAGUSD, XAUAUD, XAUUSD, AUDUSD ×2,
  EURUSD, GBPUSD, US500) — all correctly forced to FLAT.
- Root cause B (occupancy < 10%) present in 14 of 56 components (25%) —
  including AAPL's comp3 at 9.96% occupancy, the same symbol flagged in
  the original investigation — all correctly forced to FLAT regardless
  of magnitude.

Both root causes are real and current, not artifacts of the original
investigation's specific market moment, and both are now caught. No
live-mode change from this build — `markov_source_mode` stays
`trend_hmm_shadow` and `trend_probs_gate_mode` stays `shadow`; this only
changes what the shadow computation itself produces, so its effect will
show up in the existing `nt_markov_source_shadow`/
`nt_trend_probs_gate_shadow` comparisons going forward.

## Shadow-table reset — clean baseline for the root-cause fix (2026-08-28)

Studio was updated and restarted with the root-cause classifier above.
Checking `nt_markov_source_shadow`/`nt_trend_probs_gate_shadow` post-
restart confirmed it's running (fresh `updated_at`, and the latest tick
per symbol showed the classifier visibly firing — AAPL raw 0.970 →
gated 0.500, US500 raw 0.825 → gated 0.529, US30USD raw 0.969 → gated
0.666, BTCUSD raw 0.970 → gated 0.745, everyone else unchanged that
tick). But both tables are running lifetime accumulators (`n_compared`
counts up, never resets) — every count and average logged since Tier
1/2 first shipped, which pre-dates today's root-cause fix. Reading them
now would blend pre-fix and post-fix ticks into one average, which
isn't a clean read on what the fix actually changed.

**Pre-reset snapshot (for the record — final blended lifetime totals,
now superseded)**:

`nt_markov_source_shadow` (Tier 1): 7113 comparisons across 14 symbols,
61.5% overall disagreement. Per-symbol n ranged 505-517 (all started
together, same cron cadence), disagreement 43.6% (AAPL) - 74.7%
(XAUAUD).

`nt_trend_probs_gate_shadow` (Tier 2): 3496 comparisons, avg |raw -
gated| ranged 0.019 (USDCAD) - 0.470 (AAPL).

**Action taken**: both tables' rows deleted entirely via Supabase REST
(`DELETE ... WHERE symbol IS NOT NULL`) at **2026-08-28T23:19:51Z**.
Confirmed safe before doing it: both tables are `symbol TEXT PRIMARY
KEY` with no FK pointing at them from anywhere else, and both are
already self-healing fetch-then-upsert accumulators (`record_markov_
shadow_compare`/`record_trend_probs_gate_shadow` in
`supabase_client.py` already handle "no existing row" as `n_compared =
0 + 1`) — the next tick per symbol just re-creates a fresh row, no code
change needed, no risk of the app erroring on a missing row.

**Baseline marker**: any review from this point forward should treat
**2026-08-28T23:19:51Z** as the start of the clean, post-root-cause-fix
observation window — `n_compared` and the averages now reflect only
ticks emitted by the fixed classifier. The 48-72h wait recommended in
the "studio updated & restarted" review above should be measured from
this timestamp, not the original Tier 1/2 ship date.

## Macro filter — build, with a w/l+PnL evidence extension (2026-08-28)

> **Priority**: P1 · **Task**: Build the long-range macro-regime filter
> (200d-SMA bull/bear/neutral + Kelly multiplier) and its win/loss+PnL
> evidence layer ·
> **Status**: pending (migrations not yet applied; `macro_regime_mode`
> ships at `shadow`) · **Date**: 2026-08-28

Built the macro filter scoped earlier (2026-08-26), plus a new
win/loss+PnL evidence layer the user specifically asked to have detailed
and factored in. One real correction surfaced while re-verifying the
original scope against actual code before building it: the original
text assumed `recompute_ev_fields()` would carry a Kelly multiplier —
it doesn't (it only recomputes `p_win`-derived EV fields: ev/
ev_per_dollar/rr/allowed/aggression/ev_scale; no `kelly_f` anywhere in
its signature or return). The real, already-established pattern for a
post-hoc Kelly adjustment is `calibration_enforcement.py`'s
`apply_calibration_adjustment` → `effective_kelly = calib_adj["kelly_f"]`
mutation in both sweep paths — the macro filter mirrors that exact
pattern instead.

**Why a w/l+PnL layer, given the earlier scope explicitly said "no, as
scoped"?** That answer was about NOT blending macro (price-only) and
signal-quality (`nt_calibration_bias_by_bucket`, outcome-only) into one
number — a design decision that stands, unchanged. What's new here is
using realized win/loss+PnL as EVIDENCE for whether the macro filter's
own boost/penalty constants are pointed the right way, without touching
the live formula's mechanism itself (still pure price, still a human
sets `boost_scale`/`penalty_scale`, no auto-tuning added).

### Exact calculations

**Price/label (unchanged from the 2026-08-26 scope):**
```
macro_score          = (last_close - SMA_200d) / SMA_200d
macro_score_smoothed = prev_smoothed + alpha * (macro_score - prev_smoothed)
                        (alpha = 1 - 0.5**(1/ema_half_life_days); bootstraps
                        to macro_score itself on a symbol's first-ever read)
macro_label           = "bull" if macro_score_smoothed > +bear_bull_threshold
                        "bear" if macro_score_smoothed < -bear_bull_threshold
                        else "neutral"
macro_strength        = min(1.0, abs(macro_score_smoothed) / strength_saturation)
```

**Kelly multiplier (corrected mechanism — mutates `effective_kelly`/
`result.kelly_f` directly, not via `recompute_ev_fields()`):**
```
implied = "bull" if direction == "buy" else "bear"
if macro_label is None or macro_label == "neutral": mult = 1.0
elif macro_label == implied: mult = 1.0 + macro_strength * boost_scale
else: mult = 1.0 - macro_strength * penalty_scale
mult = clamp(mult, 0.25, 1.5)
```
Only APPLIED when `macro_regime_mode == "live"`; computed and captured
(`macro_label`/`macro_strength` on `nt_signal_sim`, migration 124) in
`shadow` mode too, so the evidence layer below can accumulate before any
live decision.

**Win/loss+PnL evidence — the new part, `macro_edge.py`:**
```
implied_direction(signal) = "bull" if signal == "buy" else "bear"
alignment = "neutral" if macro_label == "neutral"
            "aligned" if macro_label == implied_direction(signal)
            "opposed" otherwise

# per (symbol, alignment), over win/loss-resolved nt_signal_sim rows
# (outcome IN tp_hit/sl_hit; expired excluded, same rule as everywhere
# else this session):
win_rate       = n_tp_hit / n
avg_rr_winners = mean(tp_bricks / sl_bricks) over tp_hit rows
expectancy_r   = win_rate * (avg_rr_winners or 0.0) - (1 - win_rate) * 1.0
```
`expectancy_r`'s formula is copy-identical to `calibration_bias_bucket.py`'s
already-bug-fixed one (2026-08-25 fix: well-defined at zero wins, not
gated on `avg_rr_winners`) — reused, not reinvented. Output:
`nt_macro_edge_by_symbol(symbol, alignment, n, win_rate, avg_rr_winners,
expectancy_r)` (migration 125). This is diagnostic evidence for a human
to read before setting `boost_scale`/`penalty_scale` — it does not feed
back into live sizing itself.

### Files

- **Migrations 124/125**: `nt_macro_regime` (symbol PK, daily-refreshed
  score/label/strength) + `nt_signal_sim.macro_label`/`macro_strength`
  (nullable capture columns, same precedent as `atr`, migration 120);
  `nt_macro_edge_by_symbol` ((symbol, alignment) PK, the evidence table
  above). Not yet applied — same convention as every other migration
  this session, left for the user to run.
- **`config/feature_flags.yaml` / `feature_flags.py`**: `macro_regime_mode`
  (off|shadow|live, default `shadow` — same shadow-first default as
  `markov_source_mode`/`trend_probs_gate_mode`, a deliberate deviation
  from the original scope's "default off," since shadow costs nothing
  and starts the evidence accumulating immediately) + `macro.{sma_window_days,
  ema_half_life_days,bear_bull_threshold,strength_saturation,boost_scale,
  penalty_scale}`.
- **`macro_filter.py`** (new): `compute_macro_regime` (pure calc),
  `refresh_macro_regime` (daily job — fetches ~300 daily bars/symbol via
  `fetch_tradingview_batch(interval="1D")`, reads each symbol's previous
  `macro_score_smoothed` for real EMA continuity, upserts), `get_macro_regime`
  (per-tick read), `kelly_macro_multiplier` (pure fn). Dual module/CLI
  shape, same as `bracket_reachability.py`.
- **`macro_edge.py`** (new): the evidence table builder above. Same
  dual module/CLI shape, own local `_fetch_all` pagination helper
  (mirrors `calibration_bias_bucket.py`'s own local copy — existing
  convention, not shared).
- **`supabase_client.py`**: `upsert_macro_regime`, `fetch_macro_regime`,
  `upsert_macro_edge_by_symbol` — mirror `upsert_bracket_reachability`'s
  shape. `macro_label`/`macro_strength` added to `NT_SIGNAL_SIM_COLUMNS`.
- **`orchestrator.py`**: `_build_signal_sim_payload` gains
  `macro_label`/`macro_strength` kwargs (nullable, mirrors `atr`). Light
  sweep reads `nt_macro_regime` right after the calibration-enforcement
  block (same `signal_is_qualifiable` guard, avoids a round-trip on
  neutral ticks), mutates `effective_kelly` in place when `live`.
  Scheduler: new daily job (06:00 UTC) for `_run_macro_regime_refresh`;
  `_run_bracket_reachability`'s existing weekly job now also calls
  `macro_edge.build_macro_edge_table()` at the end — no new weekly cron
  slot, same resolved-`nt_signal_sim` population and cadence as the two
  diagnostics already chained there.
- **`heavy_sweep.py`**: mirrors the light-sweep wiring exactly (the
  "gate added to one path, missed on the other" bug this session already
  found twice) — reads macro state right after `result.qualified` is
  set, mutates `result.kelly_f` in place when live, which automatically
  propagates to `_push_heavy_signal`, the `nt_sweep_result` payload, and
  the sim-capture payload since all three already read from
  `result.kelly_f`/derive from it.
- **`settings.py`**: `MACRO_REGIME_ENABLED` (kill switch for both
  background jobs; live-behavior gating is the separate
  `macro_regime_mode`).

### Tests + verification

`tests/test_macro_filter.py` (new, 15 tests): hand-computed SMA/EMA/
score/label/strength cases for `compute_macro_regime` (insufficient
history, flat/bootstrap/continuity, bear threshold, strength saturation
and linear scaling); `kelly_macro_multiplier` (neutral/missing-label
no-op, aligned buy/sell boost, opposed buy/sell penalty, internal
strength clamp, and — via `monkeypatch` on an artificially large boost/
penalty scale, since the real 0.3/0.5 defaults never reach it on their
own — confirmed the `[0.25, 1.5]` hard clamp actually engages). Full
suite: 638 passed, 21 skipped (pre-existing/unrelated), 0 failed (up
from 623 before this build).

Real-data check: ran `macro_filter.py` standalone against live TDVA
daily bars for all 14 active symbols. Labels look directionally sane —
AAPL/BTCUSD/US500/US30USD read `bull` with real conviction (strength
0.51-0.95), XAGUSD reads `bear` (0.38), FX pairs mostly `neutral`
(strength 0.01-0.21, consistent with FX trading closer to fair value
than equities/commodities do). The upsert itself 404'd, as expected —
migrations 124/125 aren't applied yet.

### Not done

Migrations 124/125 not yet applied (user's step, as always). No live
decision made — `macro_regime_mode` ships at `shadow`, computing and
capturing but not touching Kelly sizing. `nt_macro_edge_by_symbol` needs
real accumulation time (weeks, same as the original scope's own
verification plan) before its evidence is meaningful for a
`boost_scale`/`penalty_scale` review.

## Macro-edge time aspect + admin views (2026-08-28)

Two additions on top of the macro filter build above.

**Time-to-resolve on `nt_macro_edge_by_symbol`** (migration 126): the
win/loss+PnL evidence answered "does trading with the macro trend win
more" but not "does it also resolve faster" — capital turnover matters
at equal win rate too. Added `median_hours_to_resolve`/`p75_hours`/
`p90_hours`, computed identically to `bracket_reachability.py`'s own
percentile approach (reused `_hours_between` directly, not
reimplemented), keyed by `alignment` instead of distance/ATR bucket.
`macro_edge.py` now also selects `ts`/`resolved_at` from `nt_signal_sim`
to feed this.

**Admin-viewable summary views** (migration 127): both admin surfaces
under consideration (nobletradingapp, the Hermes plugin) render generic
Supabase table/view results directly via the anon key — same mechanism
already proven for the Talaria plugin (migration 110). That meant no
FastAPI router work was needed, just the views themselves — but also
surfaced a real prerequisite checked against migration 107 before
writing this: a Postgres view still needs the `anon` role to hold its
own RLS policy + `GRANT SELECT` on every base table it reads, not only
a grant on the view. None of the four base tables involved
(`nt_bracket_reachability`, `nt_calibration_bias_by_bucket`,
`nt_macro_regime`, `nt_macro_edge_by_symbol`) had ever been anon-granted
— they've been service-role-only diagnostics until now — so the
migration adds that first, identical shape to migration 107's own anon
policies.

Two views:
- `v_bracket_performance` — one row per `(symbol, distance_atr_bucket)`,
  `nt_bracket_reachability` LEFT JOINed with
  `nt_calibration_bias_by_bucket`. Deliberately LEFT JOIN, not INNER: an
  all-expired bucket has no win/loss rows and so never appears in the
  bias-by-bucket table, but should still show up here (with those
  columns NULL) rather than silently disappearing. The two tables' `n`
  counts are exposed separately as `n_all` (any outcome) vs `n_resolved`
  (win/loss only, a subset) rather than picked-one/conflated, since they
  measure genuinely different populations.
- `v_macro_filter_summary` — one row per symbol: `nt_macro_regime`'s
  current price read, plus `nt_macro_edge_by_symbol`'s three alignment
  rows pivoted into `aligned_*`/`opposed_*`/`neutral_*` columns via
  `FILTER` (same idiom `v_talaria_signal_health`, migration 110, already
  uses for its tp_hit/sl_hit split).

**Verified**: full suite still 638 passed/0 failed after these changes.
Ran `macro_edge.py` standalone against live Supabase — query executes
cleanly (200 OK, correct new `ts`/`resolved_at` columns in the select),
returns zero rows currently (expected: no live signal has captured
`macro_label` yet since Studio hasn't been restarted with this code).
Migrations 126/127 not yet applied.

## What's still outstanding across this whole thread (2026-08-28 review)

Asked directly "did I miss anything" — full inventory:

- **Phase 3 of the original calibration reframe** (worklog
  20260825_CALIBRATION_REFRAME_SCOPE.md, "Phase 3 scope") — wiring
  bracket reachability into heavy-sweep's own combo selection (grid
  search), so it prefers ATR-reachable brackets instead of just
  reporting on them after the fact. Scoped in real detail (3a shadow-log
  diagnostic, 3b actual grid widening gated on 3a's evidence) but never
  built — no "build phase 3" instruction was ever given. Still open if
  wanted.
- **Migrations 124-127 not yet applied** — 124/125 confirmed applied by
  you already; 126 (macro-edge hours) and 127 (anon views) from this
  message are new and still need a run.
- **Studio hasn't been restarted with the macro-filter code yet** (the
  original macro build, or this hours/views follow-up) — nothing in
  `nt_macro_regime`/`nt_macro_edge_by_symbol` will start populating from
  live ticks until it has.
- **No admin view yet for `nt_entry_drift`** (Phase 2, migration 121 —
  per-symbol post-entry price drift at 1h/4h/24h) — same "nothing reads
  it yet" state as the others were in before this message. Not asked
  for; flagging since it's the one Phase-2 diagnostic without an
  admin-facing view now that the other two have one.
- **The actual admin-app-side work** (adding a table/view entry in
  nobletradingapp or Hermes pointing at `v_bracket_performance`/
  `v_macro_filter_summary`) is outside this repo — this session only
  produced the Supabase-side half.
- **Both new shadow mechanisms are intentionally still shadow-only**,
  not a gap: `markov_source_mode`/`trend_probs_gate_mode` (accumulating
  since the 2026-08-28T23:19:51Z reset) and `macro_regime_mode`
  (essentially zero real accumulation yet, pending the Studio restart
  above) both need real observation windows before any live-mode
  decision — that's the design, not something left unfinished.
- **The earlier outcome-validation-layer scope is superseded**, not
  outstanding — explicitly dropped in favor of the root-cause classifier
  fix per the "no more tacking on weight" rule; not a gap to fill.

## Phase 3a build + v_entry_drift admin view (2026-08-28)

> **Priority**: P2 · **Task**: Build the reachability-aware heavy-sweep
> combo shadow pick (Phase 3a) and the `v_entry_drift` admin view ·
> **Status**: pending (migrations not applied;
> `heavy_sweep_reachability_mode` stays `off`) · **Date**: 2026-08-28

Built the two items flagged in the "what's outstanding" review above.

### Phase 3a — reachability-aware heavy-sweep combo pick (shadow only)

Built exactly the ladder the worklog's own "Phase 3 scope" section
prescribed — 3a (shadow) only, 3b (grid widening) still deliberately
not built, since it's explicitly gated on 3a's real data accumulating
over weekly cycles, not something to jump ahead on.

**`heavy_sweep_reachability.py`** (new): `reachability_aware_pick(combos,
atr, bias_by_bucket)` — a pure function. Mirrors `pick_best_combo`'s own
`MIN_RR`/`MAX_SHARPE`/`MIN_TRADES` filter internally (a local copy, not
a shared import — same "mirror, don't share" convention
`calibration_enforcement.py` already uses for `BAYES_PRIOR_COUNT`/
`KELLY_REDUCTION`, and it keeps this module free of any import-time
dependency on `heavy_sweep.py`, avoiding a circular import since
`heavy_sweep.py` is the one importing this module). Over that SAME
quality-filtered pool, it additionally requires each combo's distance/
ATR bucket (`bracket_reachability.py`'s buckets) to show
`expectancy_r > 0` in `nt_calibration_bias_by_bucket`, OR have no
calibration data yet for that bucket — an unscored bucket isn't
evidence against a combo, only a scored-negative one is. `atr` falsy
(missing/zero) fails open the same way — disables the reachability leg
entirely rather than gating blindly on absent data. Combos are
duck-typed (only `.brick_size`/`.sl_bricks`/`.tp_bricks`/`.sharpe`/
`.total_trades` read), not `heavy_sweep.ComboResult` directly, for the
same circular-import reason.

**Config** (`config/feature_flags.yaml`): `heavy_sweep_reachability_mode:
off` (off|shadow|live) — default `off`, not `shadow`, because unlike
the HMM/macro shadow mechanisms this one still needs its underlying
price-only assumption checked against real weekly cycles before even
passive logging is worth the noise; matches the doc's own proposed
default.

**Migration 128**: `nt_heavy_sweep_reachability_shadow` — running
per-symbol comparison (`n_compared`, `n_nothing_survived`,
`n_pick_changed`, last baseline/filtered bucket+sharpe), same shape as
`nt_markov_source_shadow`, `updated_at` included in the upsert payload
from the start (learned from that table's own bug, not repeated here).
Service-role-only RLS — a backend diagnostic, not admin-viewable, same
tier as the other shadow tables.

**Wiring** (`heavy_sweep.py`): right after `pick_best_combo` produces
the baseline pick, gated on `heavy_sweep_reachability_mode != "off"`:
fetches `nt_calibration_bias_by_bucket` for the symbol, runs
`reachability_aware_pick` against the full `grid_results` already in
scope, logs + persists the comparison (fire-and-forget). In `live`
mode, overrides `best`/`result.best_combo` with the filtered pick when
one survives; when nothing survives, falls back to the baseline
Sharpe-only pick — the "safest" option the worklog's own "live-mode
fallback" note left as an open decision, implemented now as a
provisional default since `live` mode needs to be well-defined code-
wise even though it isn't the shipped default, clearly commented as
provisional pending real 3a data.

**Tests** (`tests/test_heavy_sweep_reachability.py`, 7 new): good-bucket
pick unchanged, bad-bucket pick changes to a reachable alternative,
nothing-survives when every bucket scores negative, an unscored bucket
is kept not excluded, missing `atr` disables the reachability leg
entirely, the quality filter is correctly mirrored (a combo failing
`MIN_TRADES` never appears despite a great sharpe/bucket), and an empty
combo list is a safe no-op. Full suite: 645 passed, 21 skipped
(pre-existing/unrelated), 0 failed (up from 638).

**Real-data verification**: ran `fetch_bias_by_bucket` against live
Supabase for the 5 symbols from the original Phase 3 live simulation.
Values match that simulation exactly — XAUUSD `4+`: -0.2106 (doc:
-0.211), AAPL `4+`: -0.6977 (doc: -0.698), US500 `4+`: +1.2296 (doc:
+1.230), XAGUSD `4+`: +0.5063 (doc: +0.506) — confirming the fetch
reads real, current calibration-bias-by-bucket data correctly. Also
reconfirmed XAUUSD's known-good `0.5-1` bucket (+1.577 expectancy) is
sitting there unused, exactly the "grid never generates a candidate in
the good bucket" finding the original simulation made — this is the
concrete evidence Phase 3b would eventually act on.

**Not done**: Phase 3b (grid widening) — deliberately, per the doc's own
gating. Migration 128 not yet applied. `heavy_sweep_reachability_mode`
stays `off` — no shadow data has started accumulating yet.

### v_entry_drift admin view

Migration 129: closes the one asymmetry flagged in the "what's
outstanding" review — `nt_entry_drift` (Phase 2, migration 121) was the
only Phase-2 diagnostic without an admin view once
`v_bracket_performance`/`v_macro_filter_summary` shipped. Same exact
pattern as migration 127 (anon read policy + grant on the base table,
then the view, then grant on the view). `nt_entry_drift` is already a
clean one-row-per-symbol shape, so `v_entry_drift` is a plain
passthrough — no join/pivot needed, kept as a view anyway so every
admin-surfaced object follows the same "view, not raw table" convention
consistently. Migration 129 not yet applied.

## 36h checkpoint review + MIN_RR/MAX_SHARPE/MIN_TRADES config fix (2026-08-30)

> **Priority**: P1 · **Task**: Fix `MIN_RR`/`MAX_SHARPE`/`MIN_TRADES`
> config divergence risk between `orchestrator.py` and `heavy_sweep.py`
> (previously caused a live `no_combo_passed_filters` incident) ·
> **Status**: pending (fix written, not yet pushed) ·
> **Date**: 2026-08-30

Ran a full data check (studio logs still unreachable — see below;
Supabase data only) covering signal-sim health, Tier 1/2 HMM shadow,
macro filter, calibration bias freshness, and the just-flipped Phase 3a
shadow.

**Findings**: 5,197 signals in 36h, 100% with `macro_label` captured
(confirms the macro-filter deploy is genuinely live). Tier 1
disagreement is 75.6%, up from the 61.5% pre-reset baseline — plausibly
the classifier fix working as intended (it no longer defaults uncertain
components to FLAT, so it disagrees with legacy's FLAT-heavy reads more
often precisely because it's now doing something legitimately
different), but flagged as worth continued watching given the
magnitude (up to 98.7% on XAUAUD). `nt_macro_regime` only shows ONE
`computed_at` (2026-08-29) — today's 06:00 UTC refresh didn't fire;
most likely an innocent misfire-grace-window skip from a same-day
restart, not a bug — confirm after tomorrow's window. Phase 3a is
already producing real signal on day one: XAUUSD's baseline pick (`4+`
bucket, sharpe 1.13) got correctly redirected to a `2-4` bucket pick
(sharpe 0.52) both times it's run so far — the mechanism working
exactly as designed on live data.

**Studio logs — re-verified, not just re-asserted.** Grepped the entire
repo for any Lightning AI Studio log-access code (zero matches) and
re-checked `.env.example`'s own note that `LIGHTNING_APIKEY`/
`LIGHTNING_USER_ID`/`LIGHTNING_URL` are deprecated with "no code
consumers" (2026-08-12 audit). Studio's env/secrets are configured
entirely through the Lightning AI UI, separate from this sandbox — "the
studio is operational" and "this sandbox can read its logs" are
independent facts; the first doesn't change the second. Not inventing a
new Lightning API integration to guess around this — no documentation
for it exists in this repo.

**Real bug found while auditing Phase 3a's own correctness**:
`heavy_sweep_reachability.py`'s `reachability_aware_pick()` mirrored
`heavy_sweep.py`'s `MIN_RR`/`MAX_SHARPE`/`MIN_TRADES` filter using its
own hardcoded copy (2.0/10.0/10) instead of reading `heavy_sweep.py`'s
actual values — which were themselves `os.getenv()`-sourced (i.e.,
Studio-Secret-overridable). Grepped every usage of these three names
across the codebase before fixing anything, which surfaced something
more important than the immediate bug: **`MIN_RR` is deliberately
SHARED** between `orchestrator.py` (feeds `EVEngine.min_rr`, the live
light-sweep qualification gate) and `heavy_sweep.py` (grid search
filter) — and the two had **already diverged once in production**
(2026-08-13: EURUSD/GBPUSD/USDCAD emergency heavy sweeps failed with
`no_combo_passed_filters` because `heavy_sweep.py`'s hardcoded
`MIN_RR=2.0` disagreed with `orchestrator.py`'s env-var override
`MIN_RR=1.5`). `position_sizer.py` also has an unrelated `min_rr` (its
own `sizing.min_rr`, auto-tuned via `tuning_registry.yaml`, default
2.50) — confirmed this is a completely separate concept sharing only a
name, and left untouched (it's already correctly homed in the
auto-tuned config system, not this one).

**Fix**: moved `MIN_RR` (shared), `MAX_SHARPE`, `MIN_TRADES` off env
vars/Studio Secrets onto `config/feature_flags.yaml` (`min_rr` top-level
— it's cross-cutting, not heavy-sweep-specific — plus a
`heavy_sweep_grid: {max_sharpe, min_trades}` section). Both
`orchestrator.py` and `heavy_sweep.py` now call the same
`get_min_rr()`; `heavy_sweep.py`'s wiring call to
`reachability_aware_pick()` now passes `min_rr=MIN_RR,
max_sharpe=MAX_SHARPE, min_trades=MIN_TRADES` explicitly (its own
already-in-scope, now-config-sourced constants) instead of letting
`heavy_sweep_reachability.py` re-derive an independent copy — this is
what actually eliminates the divergence risk structurally, not just the
source-of-truth change. `heavy_sweep_reachability.py`'s own hardcoded
defaults stay only as a standalone/test fallback, clearly documented as
such now.

**Verified**: all three getters resolve to the same defaults as before
(2.0/10.0/10) — confirms no Studio Secret was actually overriding these
in practice, so this was a structural/future-proofing fix, not a
correction to currently-wrong shadow data. Full suite: 645 passed, 21
skipped (pre-existing/unrelated), 0 failed — unchanged, as expected for
a same-value config-source migration. Not yet pushed.
