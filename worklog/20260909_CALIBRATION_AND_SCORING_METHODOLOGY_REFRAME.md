# Calibration + Scoring Methodology Reframe

> **This file is a narrative record of how we got here — not the status
> tracker.** For current priority/status/what's-next, query `nt_roadmap`
> directly (Supabase project `pcvscowltlrxzgxjurcr`):
> ```sql
> select mechanism, status, current_state, review_at, review_condition
> from nt_roadmap where status not in ('resolved','superseded')
> order by review_at nulls last;
> ```
> Every mechanism below has a matching `nt_roadmap` row kept in sync as
> work happens — this document explains *why*, the roadmap tracks *where
> things stand right now*. Sections 1-8 were written when nothing had
> been built yet (hence the original title); section 9 (built the same
> day) and section 10 (v2 fix, one day later) are additions, not a
> rewrite — read in order for the actual chronology.

Continuation of `worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`. That
document fixed real bugs (mark-to-market expiry P&L, deadline-aware sweep
scoring, episode-counted calibration bias) but a fresh-eyes review of the
whole EOD/calibration pipeline (same day) concluded the calibration-bias
*architecture* has a conceptual defect predating those fixes: two overlapping
correction mechanisms, evaluated far faster than evidence refreshes, capable
of starving their own evidence supply. The user paused further deployment to
rethink the whole thing from first principles. This document is that rethink.
**Sections 1-8: no code has been written yet at this point — scope only, per
"scope in worklog, get honest review, build only on explicit instruction."
Section 9 onward: built, per explicit approval — see that section.**

## 1. Calibration bias vs. scoring bias — the corrected mental model

The user's framing, which is sharper than anything the codebase itself
currently expresses:

- **Calibration bias** = is the raw signal itself honest — is the regime
  call right, is the stated confidence (`p_win`) well-formed. This is about
  the model's self-knowledge at the moment a signal is generated.
- **Scoring bias** = given a signal (or a bracket choice), is our prediction
  of its resolution accurate — does the backtest/selection process reflect
  real accuracy and precision. This is about whether we're choosing well
  among the things we generate.
- **They are coupled, one direction**: if calibration is off (the model's
  stated confidence doesn't mean what it claims), then scoring built on top
  of that confidence is measuring something meaningless — you can't validate
  a prediction's precision if the prediction itself wasn't honestly formed.

The codebase does not currently express this cleanly. The fresh-eyes review
(2026-09-08) found `p_historical` (a continuous, soft correction blended into
`compute_ev`) and `apply_calibration_adjustment` (a separate, hard, post-hoc
correction) both independently trying to answer "is this symbol's confidence
trustworthy," computed two different ways, stacked on the same small pool of
evidence. That overlap is the direct cause of the user's stated confusion —
it isn't one clean calibration-bias concept, it's two, undeclared, doing
overlapping work.

## 2. The deeper root cause: raw signals are treated as independent trials, and they aren't

Direct quote, because it is the single most important sentence in this
rethink: **"Treating the raw signal as independent only works in binary
(polymarket) or HighFrequencyTrades as one-offs."**

This is the same root mistake found in three different places today, at
three different layers of the system:

1. **Heavy-sweep backtest scoring** (fixed 2026-09-08): scored a simulated
   trade as a win no matter how long it took to resolve — implicitly treating
   "did this trade eventually resolve favorably" as the only question that
   matters, ignoring that a live signal only gets 48 hours, i.e. ignoring the
   temporal structure of the process.
2. **Calibration-bias counting** (fixed 2026-09-08): counted every
   `nt_calibration_records` row as one independent trial, when many of them
   were the same directional bet re-sampled every 5 minutes across a single,
   continuous multi-hour price move (USDCHF: 294 rows, 1 real episode).
3. **Raw signal qualification itself (not yet touched by anything)**: every
   5-minute sweep tick is evaluated as if it were a fresh, independent
   observation — but it's a sample of a continuous, autocorrelated regime
   process. This is the one still causing confusion, because nothing has
   named it as the same category of bug as #1 and #2 yet.

### Concrete evidence, pulled from production (not hypothetical)

`nt_signal_sim` captures every non-neutral raw signal every 5 minutes,
qualified or not (`NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY` defaults to
`False`). Pulling XAUUSD's raw signal stream for 2026-09-07, 11:00–16:00:

| Time | Regime | Confidence | p_win | Qualified? | Outcome |
|---|---|---|---|---|---|
| 11:01–11:56 (12 ticks) | `med_low_vol_bull` | steady ~0.90 | 0.41–0.45 | never | — |
| 12:01–12:56 (12 ticks) | `med_low_vol_strong_bear` | noisy, 0.45–0.53 | climbing 0.26→0.55 | never | — |
| **13:01:32** | same | **spikes to 0.896** (from 0.477) | 0.562 | **qualifies** | `sl_hit` (loss) |
| 13:06–13:56 (11 ticks) | same | stays high, 0.88–0.96 | keeps climbing 0.54→0.62 | blocked by cooldown | — |
| 14:01–14:56 | flips twice more (bull 1 tick, then bear 45 min) | declining | 0.36–0.49 | never | — |
| **15:06:31** | `low_vol_strong_bear` | jumps to 0.806 | 0.507 | **qualifies** | `tp_hit` (win) |
| 15:11–15:56 (9 ticks) | same, stable at 0.807 | 0.48–0.56 | blocked by cooldown | — |

`nt_renko_bricks` for the **same window**: every brick, on every brick-size
grid present, is `direction: "down"` — a single, unbroken downward drift the
entire 5 hours. The regime-HMM's directional *call* flip-flopped four times
in that same window; the actual realized price movement never did.

This is not a subtle argument. The brick-level ground truth was one coherent
episode; the qualification mechanism sampled it four different ways and got
two different-looking "signals" out of it, one of which happened to look
like a loss and one like a win — not because the underlying process changed,
but because a noisy confidence readout crossed an arbitrary threshold at two
different, essentially interchangeable moments.

## 3. Proposed reframe: a meta-state model over the signal-generation stream

This is not a new technique for this codebase — `regime_engine.py`'s
`RegimeHMM` already is a `hmm.GaussianHMM` (via `hmmlearn`), fitting two
4-state Gaussian HMMs (volatility, trend) fresh every 5-minute tick. It is
pointed at raw price-derived features (rolling-window return statistics).
**The proposal is to point the same class of tool at a different layer**:
the model's *own output stream* (regime-label stability, `p_win` trajectory,
brick-direction persistence) rather than raw price, to detect a genuinely
different thing — not "what's the market regime," but "is my own
signal-generation process currently coherent or currently noisy."

### Why this is the right tool, not a stretch

An HMM is explicitly designed to model a sequence of *correlated*
observations by inferring a small number of hidden states and the
transition dynamics between them. That is precisely the structure the
5-minute signal stream has (long same-state runs punctuated by real
transitions) and precisely what raw-row bias-counting got wrong by assuming
i.i.d. samples. Applying a sequential model here is the statistically
correct response to "each 5-minute sweep is independent but we're trying to
sequence them together" — not a novel idea, a standard regime-switching
pattern (used in quant work to gate strategy activation on "trending vs.
choppy" market states), just not yet applied at this layer here.

### Where its output would plug in

- As a genuine fifth logit-pooled source into `compute_ev`, alongside
  `p_regime`/`p_markov`/`p_timesfm`/`p_historical` — the model's own
  assessment of "how coherent is the evidence feeding this signal right
  now."
- And/or as a pre-qualification gate: require the meta-state posterior for
  "coherent" to clear some floor before a signal can qualify at all — a
  principled replacement for the ad hoc noise the cooldown timer currently
  fights after the fact.
- This is a more information-rich version of the fresh-eyes review's own
  proposal (a Beta-posterior/credible-interval approach to calibration
  enforcement) — same underlying need, but built from the full observation
  sequence instead of just post-hoc win/loss counts. These should be treated
  as one unified redesign, not two competing ones.

## 4. How many bars/bricks are needed to learn a pattern

Two different questions get asked here, and conflating them is exactly the
trap that produced the original bug:

**Wrong question: "how many independent samples do we need?"** This is the
right question for a point-estimate statistic (a win-rate, a bias number) —
and the honest answer there, given the platform-wide measurement that most
symbols see only 1-7 independent episodes per 7-day window, is "not enough,
for a long time, no matter how the window is drawn." This is the sparse-data
wall the calibration-bias enforcement mechanism keeps hitting.

**Right question for a sequential model: "how many total observations, and
how many real state transitions, does the sequence contain?"** An HMM does
not need independent samples — it needs enough *sequence* to estimate
per-state emission parameters and the transition matrix. The existing
`RegimeHMM` already answers this empirically for itself:
`min_bars = max(50, 4 * n_components * 5)` — 80 bars for its current 4-state
setup, 50 bars for a simpler 2-state ("coherent" / "noisy") meta-model,
reusing a precedent already validated in this exact codebase rather than
inventing a new number.

The practical resolution: **fit the meta-HMM on a long, continuous history**
(mirroring the existing 90-day/2160-hourly-bar window heavy-sweep already
fetches per symbol) using every 5-minute tick as a sequential observation —
correctly leaning into the autocorrelation instead of fighting it — and use
a *separate, much shorter* window purely for **inference/reporting**: "what
state is this symbol in right now, and what fraction of the recent period
was spent in each state." Fitting fresh on a single short block (4 hours, 48
ticks) would hit the same sparse-transition problem episode-counting just
exposed one layer down — a short block might contain zero or one genuine
state change, nowhere near enough to fit a transition matrix. Long-history
fit, short-window read is the shape that avoids that.

## 5. Not a fixed 4-hour block — a configurable aggregation period, decoupled from training

Per the correction: the earlier "4hr block" framing was a specific example,
not the design. The right shape has two independently-configurable
durations that should not be conflated:

- **Training/fit window**: long (weeks-to-months), because the model needs
  real transitions to learn from, not because it needs "a block."
- **Aggregation/reporting window**: the period over which "what fraction of
  recent time was spent in the coherent vs. noisy state" gets summarized and
  fed forward as a calibration input for the *next* period. This should be a
  tunable parameter (config/feature_flags.yaml, consistent with this
  session's config-centralization discipline) — not hardcoded to 4 hours,
  and not necessarily fixed-width at all; it could reasonably be redefined
  as "since the last detected state transition" rather than a wall-clock
  duration, which would make it self-adjusting to how fast the market is
  actually moving.

## 6. Local lookback dataset — should the backend cache instead of querying Supabase every time

Yes, worth building, with a real caveat flagged rather than assumed away.

**Why it's needed**: fitting a meta-HMM on a long history means pulling a
much larger, higher-frequency dataset (5-minute-tick-level signal features
across weeks, times 14 symbols) than anything currently cached locally.
Today, nothing is cached — `heavy_sweep.py`/`price_fetcher.py` fetch fresh
from TDVA every run, and no local lookback file exists anywhere in this
codebase. Re-querying Supabase's REST API for this volume on every tick (or
even every aggregation-window boundary) would add real latency and load that
doesn't exist today.

**Format recommendation: SQLite over CSV/JSONL**, for one concrete reason —
this needs a genuinely *rolling* window (prune old, append new), and SQLite
supports targeted `DELETE`/`INSERT` without rewriting the whole file, plus
indexed range queries (`WHERE symbol = ? AND ts > ?`) for fast reads when
fitting. CSV/JSONL are fine for simple append-only logs but require a full
rewrite to prune, and have no native indexing — worse on both axes for this
specific access pattern. Python's stdlib `sqlite3` needs no new dependency.

**The caveat, not yet verified**: Lightning AI Studio's orchestrator process
restarts every 2-6 hours (well-established this session, e.g. the
`_catch_up_stale_jobs` mechanism exists because of it). Whether the local
filesystem itself persists across those restarts, or whether it's a fresh
container each time, has not been confirmed — it matters enormously for
whether a local SQLite cache is durable or gets silently wiped and rebuilt
constantly. This needs a direct check (e.g., SSH in, write a test file,
force/wait for a restart, check if it survived) before committing to a
local-cache design. Recommended shape regardless of the answer: treat
Supabase as the durable source of truth, SQLite as a fast local working set,
with a periodic full resync from Supabase so a wiped cache self-heals rather
than silently going stale or empty.

## 7. Brick-data hygiene — dug in, and it's a bigger problem than "hygiene"

Asked to investigate why `nt_renko_bricks` showed multiple overlapping
`brick_index` sequences for the same symbol in the same time window. Root
cause, confirmed by reading the actual code paths (not inferred):

**Finding 1 — brick timestamps are fabricated, not real.**
`orchestrator.py:3290-3298` builds the brick series by calling
`RenkoBrickEngine.build_from_prices(prices)` with **only the price list, no
timestamps** — even though `build_from_prices` explicitly supports a
`timestamps=` parameter (`renko/brick_engine.py:355-383`) and the sweep
*already has* the real per-bar timestamps from the price fetch; they're just
never threaded through. Every brick's internal timestamp therefore defaults
to a bare index (0, 1, 2…). Then `log_renko_bricks`
(`supabase_client.py:384-389`) papers over this by fabricating plausible
looking timestamps: `ts_epoch = sweep_ts - (n - 1 - i) * 3600.0` — a uniform
"1 hour per brick" backward reconstruction from the current moment,
regardless of how long each brick actually took to form in reality (real
bricks form in minutes during volatile stretches, days during quiet ones).
The code is honest about why: "the sweep passes only close prices... this
keeps the chart's time axis sane" — it was built to make an admin chart look
reasonable, not as a data source for analysis. Nothing currently stops it
from being reused as one, which is exactly the trap here.

**Finding 2 — the table is a "latest full reconstruction" cache, not an
event log.** Every light-sweep tick rebuilds the ENTIRE brick series from
the current price window from scratch and upserts it via
`(symbol, brick_index, session_date)` — there's no append-only accumulation
of real brick-formation events over time, it's a rolling redraw. Confirmed
live for XAUUSD: `brick_size` changes almost daily now (tracking the
now-daily heavy-sweep re-optimization) — 16.7 → 17.3 → 8.2 → 11.1 → 16.2 →
8.3 → 0.89 → 0.735 across the last month — and every time it changes, a
fresh `brick_index` sequence starts from 0 under the new size, while rows
from every *previous* size/session_date combination remain in the table
indefinitely (no retention policy). This is exactly why the same symbol
showed multiple concurrent-looking index sequences: they're not concurrent,
they're **orphaned historical grids from different bracket-geometry
regimes, accumulating forever**, made worse now that heavy sweep churns
brick_size daily instead of weekly.

**Bottom line: `nt_renko_bricks`, as currently populated, cannot be trusted
as a brick-formation-timing feature source without real rework** — real
per-bar timestamps need to flow from the price fetcher through
`build_from_prices(prices, timestamps=...)` into what gets persisted, and
the table needs either a retention policy or a clear way to identify "the
currently-active brick_size grid" vs. historical ones. This is a concrete,
scoped, buildable fix (not a redesign) — flagged here, not yet built.

## 8. Where this leaves things — open decisions, nothing built

1. Calibration-bias architecture: pick one location for "distrust an
   overconfident symbol" to live (soft EV-blend input vs. hard post-hoc
   enforcement), not both, independently thresholded (fresh-eyes review,
   2026-09-08).
2. Signal-generation layer: build (or don't) a meta-HMM over the raw signal
   stream, per §3-5 above — this is the part directly answering "is it
   adding noise, or indicating movement, or other" per raw signal.
3. Brick data: real-timestamp fix + retention policy (§7) is a genuine
   prerequisite if brick-direction persistence is going to be one of the
   meta-model's input features — otherwise the model would be learning from
   fabricated timing data without knowing it.
4. Local caching (§6): format decided (SQLite) contingent on confirming
   Studio filesystem persistence across restarts — not yet verified.
5. Aggregation window: should be a configurable time period
   (`config/feature_flags.yaml`), decoupled from the (much longer) training
   window — not a hardcoded 4 hours.

No code has been written. Waiting on direction on which of the above to
scope into an actual build plan next.

## 9. Built (2026-09-09, same day, following explicit approval)

All five open decisions above were resolved and built the same day, plus
two more items from a follow-up conversation. Three independent builds ran
in parallel (subagents); the sweep-cadence decision, the SQLite cache, and
the meta-regime model were built directly. Full suite: 784 passed, 21
skipped, 0 failed. Nothing deployed to Studio yet — pushed to git only.

**#1 — calibration-bias architecture, consolidated to the hard mechanism.**
Removed `p_historical` (the soft, continuous EV-blend correction) entirely
from both production call sites (`orchestrator.py`, `heavy_sweep.py`) —
`apply_calibration_adjustment` (hard, post-hoc, already episode-counted
per 2026-09-08's fix) is now the sole calibration correction. Deleted
`fetch_historical_p_win()` and its cache helpers outright once they became
fully dead. Deliberately left `ev_engine.py`'s blend formula and the
separate `/ev/compute` API endpoint (`renko/router.py`, a genuinely
different live consumer of `p_historical`) untouched — narrower blast
radius than gutting the engine, and per [[ev_engine_no_rework]].

**#2 — the meta-HMM over the signal-generation stream: built (v1).**
`meta_regime_model.py`: a 2-state GaussianHMM fit on regime-label
persistence + confidence deviation, reusing `RegimeHMM`'s own
standardization/degenerate-fit-repair statics directly. Validated against
real XAUUSD history (not just synthetic), which surfaced an honest
limitation: the binary label-persistence feature dominates the fit over
the more continuous confidence signal, so it reliably catches label
flip-flopping but under-weights a confidence *spike* without a label
change — exactly the failure mode in the original 13:01 example. Shipped
`signal_stream_meta_model_mode: off` — needs that v2 feature before
shadow, let alone live.

**#3 — brick-data timestamps: fixed and verified against live data.**
Real per-bar timestamps now flow `price_fetcher.py` → `orchestrator.py` →
`log_renko_bricks`, replacing the fabricated `sweep_ts`-backward
reconstruction — the fabrication path is now an explicit, logged fallback
for when real timestamps genuinely aren't available, not a silent
default. Root cause traced one layer deeper than originally scoped:
`price_fetcher.py` was discarding TDVA's real candle timestamps before
they ever reached the brick engine. Also fixed the undiscriminated
`nt_renko_bricks` unique key (migration 145: added `brick_size` to the
key, one-time-pruned 129,583 → 62,530 orphaned-grid rows) plus a new
nightly `renko_retention.py` pass. Verified end-to-end against a live
TDVA fetch after the build (not just unit tests): real timestamps resolve
to correct, sane real-world dates.

**#4 — SQLite local cache: built, persistence verified via a real existing
precedent.** `signal_stream_cache.py` at `config/signal_stream_cache.sqlite`
— same convention as `tuning_registry.yaml`. Persistence across Studio
restarts confirmed via `HF_HOME=/teamspace/studios/this_studio/hf_cache`
(worklog 20260831_TIMESFM_3_UPGRADE_SCOPE.md), which already stores an
883MB TimesFM checkpoint that survives Studio's frequent restarts today —
proof the repo checkout lives on the persistent Studio volume, not
ephemeral container disk, so a relative path within it persists the same
way. `resync_from_supabase()` self-heals the cache from `nt_signal_sim` if
ever wiped.

**#5 — aggregation window: resolved by construction, not a config
knob.** Per the correction that this isn't a fixed 4-hour block:
`summarize_since_last_transition` reports "how long has the current state
held" directly from the fitted Viterbi state sequence — there is no
duration parameter to tune or let drift stale, because the boundary is
the data's own last real transition, not a wall clock.

**Follow-up #1 — signal cooldown now restarts on resolution, not on a
pure wall-clock timer.** The exact fix requested from the XAUUSD example:
a qualified signal can no longer get a same-direction sibling while still
open, regardless of elapsed cooldown minutes. Since the batch resolver
only runs once daily, "has it resolved" is checked live at
qualification time by reusing `eod_resolver.resolve_signal_outcome`
directly (a single-bar call, not a reimplementation) against the current
price, falling back to the DB record or 48h-expiry when neither the live
check nor the DB has an answer yet. Known accepted gap: a brief intraday
spike that touched TP/SL and reverted before the next tick won't be
caught until the nightly batch resolves it — flagged, not solved, since
solving it fully would mean resolving intraday rather than checking live
at qualification time only.

**Follow-up #2 — sweep cadence: 5 → 15 minutes, unified (not
differentiated by asset class).** TimesFM's own cache TTL is 900s (15
min) — at 5-min sweep cadence, 2 of every 3 ticks were silently reusing a
stale TimesFM value while everything else recomputed fresh, manufacturing
tick-to-tick noise with no real new information behind it. Considered and
rejected differentiating cadence for crypto/forex ("faster-moving"): the
real bottleneck for every asset class today is the shared hourly-candle
fetch, not sweep tick rate — a genuine fix there means upgrading data
granularity, a separate unscoped effort. Confirms the 2026-08 cadence
unification (migration 028) was the right call, not something to undo.

**Not addressed this round**: the multivariate cross-symbol
calibration/bucket methodology TimesFM 3.0 would eventually enable is
noted but blocked on the existing non-commercial license restriction
(worklog 20260831_TIMESFM_3_UPGRADE_SCOPE.md) — nothing to build there
until that's resolved. Kept in mind as a reason not to design the
per-symbol meta-model in a way that would need to be thrown away if/when
cross-symbol modeling becomes possible.

## 10. Meta-regime model v2 — the confidence-spike fix, detailed (2026-09-09)

**The bug, precisely**: v1's real-data validation found it correctly
called the 2026-09-07 13:01 XAUUSD moment "coherent" (100% `coherent_prob`)
when it should have flagged it as noisy — that was the exact tick whose
confidence spike produced a losing qualified signal. Root cause,
re-examined carefully (an earlier draft of this explanation was
imprecise): the spike that actually happened at 13:01 was in
**`regime_conf`** (0.477 → 0.896 in one tick, staying elevated 0.88-0.96
for the next 11 ticks), **not** in `p_win` (which moved only 0.546→0.562).
v1's feature set had no column that measured *change* in `regime_conf` at
all — only its instantaneous value, mixed in with the rest of a 1000-tick
history where regime_conf's overall distribution overlaps enough between
calm and volatile stretches that absolute level alone doesn't cleanly
separate two states. `label_persistence` — a clean, always-exactly-0-or-1
column — ended up carrying nearly all of the fit's discriminating power
by default, and it had nothing to say about a spike that happened without
a label change.

**The fix**: a 4th feature, `regime_conf_volatility` — the rolling
standard deviation of `regime_conf` (not `p_win`) over the trailing
`CONFIDENCE_VOLATILITY_WINDOW=4` ticks. This gives the model a second,
independent, near-binary discriminating signal (elevated during and
briefly after a spike, ~0 in a calm stretch) instead of relying on
`label_persistence` to carry the whole fit alone. `_label_components`'s
ranking was updated to `regime_conf + label_persistence -
regime_conf_volatility` (subtracted, since it's a noisiness measure).

**Verified two ways, not just unit tests:**
1. Real-data re-validation on the exact XAUUSD case: v2 now calls the
   2026-09-07 13:01 moment 97.9% `noisy` (was 100% `coherent` under v1),
   while still correctly calling a genuinely calm moment (11:30, mid a
   stable bull run) 100% `coherent`. A complete reversal of the wrong
   call, on the real data that exposed it.
2. Regression tests (`tests/test_meta_regime_model.py`):
   - `test_build_features_regime_conf_spike_has_elevated_volatility` —
     feature-level, using the real recorded XAUUSD `regime_conf`
     sequence from the worklog table directly (not round synthetic
     numbers), proving the volatility column is correctly keyed off
     `regime_conf` and flags the spike tick.
   - `test_model_distinguishes_stable_from_oscillating_confidence_under_constant_label`
     — full HMM regression: `regime_label` is held constant across the
     *entire* synthetic history (zero discriminating power from
     `label_persistence`, by construction) and both segments share
     nearly the same mean `regime_conf` (~0.7), differing only in
     stability vs. irregular jitter — the model still correctly calls
     the stable segment coherent and the jittering one noisy, proving
     the fix works even when the OTHER v1 features are deliberately
     uninformative.

**A test-construction lesson worth keeping**: the first attempt at test
2 used a strict period-2 alternation (`0.5, 0.9, 0.5, 0.9, ...`) for the
"noisy" segment and got the assertion backwards — the model confidently
called it *coherent*, not noisy. Investigated rather than just flipping
the assertion: a perfectly periodic 2-value alternation is exactly what
a 2-state Markov chain represents best (it's maximally predictable, so
genuinely not "noisy" from the model's perspective) — real noise needs
to be irregular/unpredictable to be a valid test case. Switched to a
fixed-seed random jitter (`random.Random(42).uniform(0.4, 0.95)`), which
produced the expected result immediately. Kept the failed version's
lesson in the test's own docstring so it isn't rediscovered the hard way
again.

**One honest open item, investigated not just noted**: fitting on the
real 1000-tick history logs hmmlearn's "Model is not converging" warning
(log-likelihood dips slightly, 71.01→70.61, at iteration 6). Checked
whether raising `n_iter` (100→200→300→500) fixes it — it does not; EM
still stops at iteration 6 regardless of budget, because hmmlearn's
monotonicity check triggers on the small negative delta itself, not on
running out of iterations. Not chased further: `_sanitize_model` already
guards the degenerate-output case, and the prediction quality on the
real validated example was excellent despite the warning. Left as a
monitoring item for if/when this moves past `off` — not blocking, since
nothing consumes this model's output yet.

`config/feature_flags.yaml`'s `signal_stream_meta_model_mode` comment
updated to reflect the v2 fix; the v1-limitation caveat there is now
resolved, replaced with the convergence-monitoring note above.
