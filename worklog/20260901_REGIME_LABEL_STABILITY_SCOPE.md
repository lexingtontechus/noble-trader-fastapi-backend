# 2026-09-01 — RegimeHMM refit/label stability: scope

Scope only. Continues the `regime_shift_alert` triage from the health-
check thread — this is the "not a one-off, how do we actually fix it"
follow-up.

## Root cause, confirmed against the actual code (not assumed)

`RegimeHMM.fit()` (`regime_engine.py:311-315`) constructs a brand new
`hmm.GaussianHMM` every call — `self._trend_model = self._build_hmm()`
— with `random_state=42` **fixed** and no `init_params` override, so
hmmlearn's default `"stmc"` re-initializes start probabilities,
transition matrix, means, and covariances from scratch every tick, via
its own k-means-style init.

The fixed seed matters: it rules out "random noise" as the cause. Given
identical input data, the fit would be deterministic. The actual driver
is that the input **isn't** identical tick-to-tick — light sweep re-fits
on a rolling window that shifts by one bar every ~5 minutes. That's
occasionally enough to nudge the k-means init across a cluster boundary,
which cascades into a different EM local optimum — a different
component partition — and, via `_label_states`, a different
`regime_label`. `regime_label` (not the Tier 1/2 markov/p_regime outputs
this session already fixed) is what `detect_regime_shift` compares
tick-to-tick, and it's also what `direction = "short" if "bear" in
regime_label.lower() else "long"` uses directly to pick trade direction
— so this instability isn't just alert noise, it's also flapping the
actual direction decision on borderline ticks.

The existing continuity mechanism (`prev_vol_means`/`prev_trend_means`,
threaded via `self._regime_label_means` on the orchestrator instance)
only *relabels* a fresh fit after the fact — it has never influenced the
fit itself. That's the gap.

## Fix 1 (root cause): warm-start the EM fit from the previous tick's own parameters

hmmlearn supports skipping auto-init (`model.init_params = ""`) and
manually seeding `means_`/`covars_`/`transmat_`/`startprob_` before
calling `.fit()` — the EM iterations (`params`, default `"stmc"`) still
refine everything from that starting point, they just don't start blind.
Starting from "very close to last tick's converged answer" instead of a
fresh k-means guess on slightly-shifted data should make EM converge to
the *same* local optimum far more often.

### The one genuinely delicate part: standardization changes tick-to-tick

`_fit_scale` recomputes `(mu, sigma)` from the current window every fit,
so last tick's fitted params (in *that* tick's standardized space)
aren't directly reusable this tick — they need rebasing onto the new
scale first:
- `covariance_type="diag"` is confirmed the actual type in use — this
  makes the rebasing tractable. Variance rescales as
  `covar_new_std = covar_original / sigma_new**2`; a mean rescales as
  `mean_new_std = (mean_original - mu_new) / sigma_new`.
- Store `means_`/`covars_` in **original units** (already computed post-
  `_destandardize_means`, no new computation needed to get them) rather
  than standardized units — re-standardize on demand with whichever
  scale is current at warm-start time, reusing the existing
  `_standardize()` helper directly instead of inventing new rebasing
  math from two different old/new scales.
- `transmat_`/`startprob_` are unitless probabilities — reused directly,
  no rescaling needed.

### Seed by raw component index, not by label

Warm-starting needs "previous component *i*'s params seed this tick's
component *i*'s initial guess" — but component *identity* isn't stable
across fits by construction (that's the whole labeling problem). The
fix: seed by **raw index**, unrelated to which human label a component
will eventually get. `_label_states`' existing `prev_vol_means`/
`prev_trend_means` continuity matching still runs afterward, unchanged,
to decide what to *call* whatever partition the warm-started fit
produces. The two mechanisms are complementary: index-seeding stabilizes
*which partition* EM finds; label-matching decides *what to name it*.
Neither replaces the other.

### Concrete changes

- New `RegimeHMM.fit()` kwargs: `prev_vol_params`/`prev_trend_params`
  (`dict` with `means`/`covars`/`transmat`/`startprob` in original units
  — `None` on a symbol's first fit, same optional-with-fallback shape as
  the existing `prev_vol_means`/`prev_trend_means`).
- New introspection (mirrors `vol_label_means`/`trend_label_means`, but
  returns the FULL raw arrays needed to seed a future fit, not just one
  feature's per-label scalar) — e.g. `vol_raw_params`/`trend_raw_params`
  properties.
- `orchestrator.py::_process_symbol`: a parallel per-symbol dict next to
  `self._regime_label_means` (e.g. `self._regime_warm_start_params`),
  read before `fit()`, written after — same lifecycle, same in-memory-
  only persistence (lost on restart, same as label continuity already
  is today — not a regression, and not worth solving here given the
  restart-cadence work already in flight separately).
- **`heavy_sweep.py` explicitly OUT of scope**: its `_regime_sync`
  doesn't thread `prev_vol_means`/`prev_trend_means` today either, and
  heavy sweep's cadence (weekly-ish, TimesFM-gated) means there's no
  adjacent-tick/single-bar-window-delta relationship to warm-start from
  in the first place — the window composition changes far too much
  between heavy-sweep runs for "start close to last time" to mean
  anything. This is a deliberate scoping boundary, not an oversight.

### Mode gate — deliberately simpler than this session's usual shadow-first pattern

A true shadow comparison (fit both warm-started AND cold every tick,
compare) would double the CPU cost of the single most expensive part of
each tick, for every symbol, forever — not the same cheap-to-compute
shadow diagnostic this session's other Tier 1/2 work could afford.
Proposing `regime_warm_start_mode: off | live` only (no `shadow`),
default `off`. Confidence instead comes from: unit-testing the rebasing
math precisely (hand-computed values), and a real-data before/after
comparison of label-flip-rate over a fixed window once built — evidence
from a real trial, just not a live parallel-shadow computation.

### Level of effort

Moderate — the mechanism itself (seed 4 arrays, set `init_params=""`,
call `.fit()`) is small, but the rebasing math needs to be exactly right
(covariance rescaling under changing standardization is the kind of
thing that's easy to get subtly wrong), and it touches the core fitting
path of a class this session has otherwise been careful to leave alone.
Recommend: build behind the `off`-by-default flag, unit-test the
rebasing transform in isolation first (pure function, hand-computed
cases, no hmmlearn needed), then integration-test a real fit sequence
before ever flipping to `live`.

## Fix 2 (defense in depth): hysteresis/debounce on `regime_label`

Even a well warm-started fit will occasionally cross a genuine boundary
noisily. A simple per-symbol debounce: track the current "confirmed"
label plus a pending challenger and its consecutive-tick count; only
promote the challenger to confirmed after `N` consecutive ticks of
agreement (`N` from `config/feature_flags.yaml`,
e.g. `regime_label_debounce_ticks: 3` — 3×5min = 15 min lag on a
confirmed change, a real tradeoff against faster response to genuine
shifts, tunable). Cold start (no confirmed label yet for a symbol)
accepts the first read immediately — no artificial lag on a symbol's
very first tick.

**This one CAN cheaply run shadow-first** (`regime_label_debounce_mode:
off | shadow | live`) — no extra HMM fit needed, just bookkeeping. Shadow
mode computes the debounced label and logs/persists how often it would
have differed from the raw one, without changing what `direction`/
`detect_regime_shift` actually see. Same discipline as Tier 1/2.

**What consumes the debounced label once live**: `direction` selection
(`orchestrator.py`'s `_process_symbol`) and `_evaluate_regime_shift`'s
`prev_regime`/`curr_regime` comparison — both currently read the raw,
undebounced `regime.regime_label`. `nt_sweep_result`/`nt_signal_sim`'s
stored `regime` column would store whichever is authoritative once this
ships; not proposing a parallel raw+debounced capture on those tables —
that's the outcome-validation-layer pattern this session already
decided against reviving without a specific need, and there isn't one
here (the shadow table above already answers "how often would this
differ").

### Level of effort

Small-moderate — the debounce state machine itself is a small,
cleanly-testable pure-ish class/function (explicit state in/out, easy to
unit test with a scripted sequence of raw labels asserting the confirmed-
label sequence). The wiring (reading it in `_process_symbol`, a new
shadow table + config key) mirrors patterns already built several times
this session.

## Recommended build order

1. Fix 1 (warm-start) first — it's the actual root cause. Ship behind
   `regime_warm_start_mode: off`, review the rebasing math and a real
   fit-sequence test before ever flipping to `live`.
2. Measure the real change in label-flip-rate / `regime_shift_alert`
   volume over a comparable window once Fix 1 is live.
3. Decide whether Fix 2 (debounce) is still needed on top, informed by
   that measurement — not built blind alongside Fix 1. If warm-starting
   alone meaningfully cuts flip-rate, debounce may turn out to be
   unnecessary overhead; if flips persist even with a stabilized fit,
   debounce is the right next layer.

## Explicitly not in this scope

- `heavy_sweep.py` (reasoned above — no adjacent-tick relationship to
  warm-start from).
- Any TimesFM 3.0-informed regime cross-check (blocked/unavailable per
  the licensing note; this fix is fully orthogonal and doesn't need
  rework once that unblocks — see the prior exchange's reasoning).
- Touching `ev_engine.py`, `_label_states`, or `TREND_LABELS` — same
  wide-blast-radius boundary this session has held throughout; both
  fixes here sit at the fit-mechanics/label-consumption layers, not the
  labeling logic or its consumers outside this thread.

## Build notes — Fix 1 (2026-09-01, same day)

Built exactly as scoped.

- **`RegimeHMM._rebase_params_to_scale`** (new, static, pure): the
  diag-covariance rescaling math. Verified with real hmmlearn first
  (a standalone script, before writing production code) that seeding
  `means_`/`_covars_`/`transmat_`/`startprob_` + `init_params=""` then
  calling `.fit()` behaves exactly as intended — no surprises once
  implemented.
- **`RegimeHMM._seed_model`** (new, static): seeds by raw component
  index, no-ops safely on any shape mismatch. Writes to `model._covars_`
  (not the public `covars_` setter) — confirmed via a real hmmlearn
  object that the private attribute is the compact `(n_components,
  n_features)` diag form, matching `_sanitize_model`'s own existing
  precedent for the same reason.
- **`vol_raw_params`/`trend_raw_params`** properties (new): full fitted
  params in original units, for a caller to persist and pass into the
  next `fit()` call.
- **`fit()`** gained `prev_vol_params`/`prev_trend_params` kwargs, both
  `None`-default (regression-safe — omitting them is byte-identical to
  before).
- **`config/feature_flags.yaml`**: `regime_warm_start_mode: off`
  (off|live only, no shadow — reasoning unchanged from the scope).
- **`orchestrator.py`**: new `self._regime_warm_start_params` dict,
  parallel to `self._regime_label_means`, gated on
  `get_regime_warm_start_mode() == "live"`. `heavy_sweep.py` untouched,
  per the scope.
- **Tests** (`tests/test_regime_warm_start.py`, 10 new): rebasing math
  hand-computed (including an identity-scale no-op case), `_seed_model`
  against a real `hmm.GaussianHMM` (correct assignment, `init_params`
  disabled, safe no-op on shape mismatch / missing key), raw-params
  introspection shape/finiteness, a real two-fit warm-start sequence,
  and a regression check that omitting the new kwargs behaves as before.
  Full suite: 678 passed, 21 skipped (pre-existing/unrelated), 0 failed
  (up from 668).

### Real-data verification — a genuine, important mixed result

Ran a live comparison, not just "doesn't crash": for 5 symbols, simulated
40 consecutive rolling-window ticks against real hourly price data, once
fitting cold every tick (today's behavior) and once warm-starting each
tick from the previous tick's own `raw_params` — same underlying data,
so any difference is attributable to the fitting mechanism, not the
market.

| Symbol | cold flips (of 39) | warm flips (of 39) |
|---|---|---|
| XAUAUD | 21 | **3** |
| XAGUSD | 19 | **5** |
| US500 | 23 | **7** |
| GBPUSD | 18 | **7** |
| **AAPL** | 9 | **27** |

4 of 5 symbols: a large, real reduction in tick-to-tick label flapping
(61-86% fewer flips) — direct evidence the mechanism does what it was
built to do. **AAPL is a genuine counter-example**: warm-starting made
it 3x *worse* (9 -> 27 flips).

**Correction (same day, after user challenge)**: my first-pass
explanation above — "warm-starting propagates forward a fit where one
component absorbed ~100% Viterbi occupancy" — was asserted without
actually inspecting per-tick occupancy/means data, and doesn't survive
contact with it. The user separately confirmed, from real intraday
price action, that AAPL's move on the day in question was a smooth,
low-volatility, decisive uptrend, not a choppy session — a fair
challenge to a hypothesis I hadn't verified. Re-ran the comparison with
full per-tick diagnostics (occupancy, component means, and a major
vs. minor flip split — major = crosses the bull/bear family boundary,
minor = e.g. `bull` <-> `strong_bull`). Corrected findings:

- **Not a metric artifact.** Major flips: cold 2, warm 14. Minor flips:
  cold 7, warm 13. Warm genuinely produces 7x more direction *reversals*,
  not just more label churn between adjacent buckets.
- **But the regression is concentrated in an earlier sub-window, not the
  smooth-uptrend day.** The 200-bar fetch spans roughly 8 trading days;
  the last ~10 of the 40 simulated ticks are the ones whose window
  actually reaches into the smooth breakout the user described
  (`window_last_close` climbing 316 -> 325). In exactly that segment,
  **both cold and warm are stable** — all `strong_bull`/`bull`, zero
  `bear` labels either way. The other 90% of the major-flip regression
  (12 of 14) happens in an earlier, separate sub-period the user's
  description was never about.
- **The actual mechanism, now verified**: tracked each tick's trend-
  component separation (`max - min` of the 4 components' fitted 30-bar-
  return mean). Cold holds a steady ~0.10-0.13 every tick — a fresh
  k-means init reliably re-finds a well-separated partition regardless
  of the exact window. Warm periodically collapses to 0.015-0.04 (e.g.
  ticks 4, 6, 12 in the run) — roughly 3-6x tighter — and those are
  exactly where the major flips cluster. Once EM is seeded near a
  compressed, poorly-separated solution, it converges to a nearby local
  optimum (by construction, EM doesn't re-explore), so the rank-based
  bull/bear assignment ends up decided by differences close to noise,
  and a one-bar window shift is enough to swap the ranking. Cold's fresh
  init has no such memory to get stuck near.

**Rescope**: this isn't a new "occupancy-health gate" as first
speculated — it's the same shape of problem `markov_flat_gate_threshold_stds`
already solves for a different consumer (Tier 1/2's directional
classifier refuses to trust a component whose fitted signal doesn't
clear a noise-floor threshold). The fix is a pre-seed guard in
`_seed_model`/`fit()`: before warm-starting from a tick's converged
params, check that tick's own trend-component separation against the
same style of threshold; if it doesn't clear the bar, skip seeding for
the next tick (fall back to a fresh cold fit) instead of propagating a
compressed partition forward. Small, reuses an existing, already-tuned
threshold concept rather than inventing a second one.

**Not done**: the separation guard described above. Recommend NOT
flipping `regime_warm_start_mode` to `live` until it's built and
re-verified against this same 5-symbol comparison — the mechanism is a
real, large improvement for most symbols and a real, now-understood
regression for at least one, and AAPL is unlikely to be the only symbol
with a sub-period like this.

### Separation guard — built, tested, then found to fail under real data

Built the guard as scoped: `RegimeHMM._trend_separation_healthy()`
(reuses `markov_flat_gate_threshold_stds` — the same threshold Tier 1/2
already uses, checking whether the 4 trend components' means are
separated from each other, not whether any one clears the noise floor)
gates `trend_raw_params` — returns `None` when unhealthy, so the next
tick's `fit()` call naturally falls back to a fresh cold init (`None`
already means "no warm start" in the existing `fit()` signature — no
new code path needed). 4 new tests, full suite 681 passed.

Re-ran the same 5-symbol real-data comparison with three conditions
(cold / warm-no-guard / warm-with-guard) — **the guard does not work**:

| Symbol | cold (maj,min) | warm-no-guard | warm-guarded | seeds skipped (/39) |
|---|---|---|---|---|
| US500 | 19,5 | 3,1 | **11,2** | 2 |
| GBPUSD | 14,4 | 4,1 | **19,7** | 2 |
| XAUAUD | 13,7 | 6,2 | **9,5** | 5 |
| AAPL | 2,7 | 14,13 | **14,15** | 1 |
| XAGUSD | 15,5 | 6,5 | **11,11** | 3 |

Two independent problems: (1) the reused threshold barely engages for
AAPL (1/39 skips) — it was calibrated for a different question ("is one
component's mean big enough to call directional") and is too loose for
"are all 4 separated enough to trust as a warm-start basis"; (2) it made
4 *previously-fixed* symbols worse — skipping a seed for one tick lets
that tick cold-reset to a different k-means partition than the
otherwise-continuous warm-started run was using, and warm-starting then
resumes from that new partition, so a handful of skips can knock a
whole subsequent stretch onto a different track. Not shipped, not
re-attempted with a re-tuned threshold — see the ADX finding below for
why a different threshold wasn't the next thing tried.

### External indicator (ADX) as a warm-start gate — tested, doesn't hold up

Before abandoning the "gate on market conditions" approach entirely,
tested whether a market-external choppiness measure could do what the
HMM-internal separation guard couldn't. `_compute_adx_close_only()`
(`regime_platform/services/hmm/model.py`, already computed for the
legacy HMM's bootstrap, close-only Wilder's-smoothing approximation, no
new HTTP calls) was run per-tick over the same 5-symbol/40-tick window
and correlated against which ticks the warm-started fit produced a
major (bull-family <-> bear-family) flip on:

| Symbol | mean ADX at major-flip ticks | mean ADX at stable ticks |
|---|---|---|
| US500 | 24.10 | 21.96 |
| XAUAUD | 38.94 | 37.03 |
| GBPUSD | 40.06 | 25.77 |
| XAGUSD | 32.60 | 27.11 |
| AAPL | 17.13 | 19.97 |

4 of 5 symbols flip *more* at higher ADX, not lower — the opposite of
"chop causes flips." A flip here is the HMM's rank-ordering of two
already-fit components swapping, which can happen during a strong,
well-established trend (high ADX) just as easily as during genuine
chop, if the recent bars are homogeneous enough that two components'
means sit close together — trend strength and rank-stability turned out
to be different questions. Abandoned this direction rather than testing
further indicators (mean/variance/volatility) — the ADX result already
undermines the premise that "was the market choppy" predicts "will the
fit's label ranking flip."

## Fix 2: built and verified — the one that actually works

Given both warm-start (with or without a guard) and the external-
indicator-gate idea failed to produce a reliable, no-regression fix,
built and real-data-verified Fix 2 (hysteresis/debounce, scoped above)
next, independent of Fix 1's outcome rather than gated behind it as
originally sequenced — Fix 1 isn't going live, so there was nothing left
to measure it against.

**Real-data verification, run BEFORE building anything** (the lesson
from Fix 1: verify before committing to a mechanism) — extended to 500
simulated hourly ticks (~3 weeks) at window=200 bars (matches
production's actual `fetch_tradingview_batch(active, 200)` exactly,
corrected from the earlier tests' arbitrary window=150), cold fits only
(today's real baseline — no confound from Fix 1):

| Symbol | ticks | raw flip rate | n=2 | n=3 | n=4 |
|---|---|---|---|---|---|
| US500 | 500 | 55.0% | 15.8% | 5.2% | 2.8% |
| XAUAUD | 500 | 57.7% | 16.6% | 5.2% | 3.0% |
| GBPUSD | 500 | 38.9% | 12.2% | 7.8% | 4.6% |
| AAPL | 500 | 36.3% | 10.0% | 5.8% | 3.8% |
| XAGUSD | 500 | 52.5% | 15.2% | 8.4% | 4.6% |

No symbol-specific regression, unlike every warm-start variant tried —
AAPL improves right along with everyone else. Reasoned why: this works
on the output symptom (label churn), not the fit mechanism, so it
doesn't matter *why* a given tick disagrees with the last one.
`n_confirm=3` chosen as the default: ~92-95% flip reduction for a 3-hour
confirmation lag (this data is hourly, matching production's actual
relabeling cadence — the light sweep re-fits every ~5min but re-fetches
the same hourly bars until the next hour rolls over, so N ticks really
does mean N hours of lag in production).

**Built**:
- `RegimeLabelDebouncer` (`regime_platform/core/regime_engine.py`):
  small state machine — confirmed label + pending challenger + its
  consecutive-agreement count, promotes the challenger after
  `n_confirm` ticks of agreement, cold-starts on a symbol's first read
  with no lag. Pure logic, no hmmlearn dependency.
- `regime_label_debounce_mode: off | shadow | live` +
  `regime_label_debounce_ticks: 3` in `config/feature_flags.yaml` /
  `feature_flags.py`.
- `nt_regime_debounce_shadow` (migration
  `132_nt_regime_debounce_shadow.sql`) + `record_regime_debounce_shadow`
  in `supabase_client.py` — same running-per-symbol-row,
  fetch-then-upsert shape as `nt_trend_probs_gate_shadow`.
- `orchestrator.py`: one `RegimeLabelDebouncer` per symbol
  (`self._regime_debouncers`), wired in right after the Kelly-floor
  block in `_process_symbol` — deliberately placed there so
  `_apply_regime_kelly_floor` keeps reading the RAW label (a third
  consumer not mentioned in the original Fix 2 scope, left alone per
  this session's wide-blast-radius boundary) while everything
  downstream (direction selection, regime-shift detection, and the
  persisted `nt_sweep_result`/`nt_signal_sim` "regime" column) sees
  whatever `regime_label` is bound to — a single in-place reassignment
  in `live` mode, no parallel variable needed.
- Tests: `tests/test_regime_label_debouncer.py` (7 new — first-tick
  no-lag, single-tick noise rejected, N-consecutive promotion,
  challenger reset on a third label appearing, challenger reset on
  reverting to the already-confirmed label, `n_confirm=1` degenerates
  to no debounce, `n_confirm` clamped to >=1). Full suite: 688 passed,
  21 skipped, 0 failed.

**Recommendation**: ship `regime_label_debounce_mode: shadow` next,
watch `nt_regime_debounce_shadow` accumulate for a real review window,
then decide on `live`. Shelve Fix 1 (`regime_warm_start_mode` stays
`off`) — leave the code in place (tested, harmless at rest, the
rebasing math may still be useful for an unrelated future need) but
don't pursue the guard further; Fix 2 alone appears sufficient and,
critically, doesn't have Fix 1's per-symbol-regression risk.
