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

## 11. Deployment verified (2026-09-09) — caught and fixed a real regression, plus the actual P0 fix

Studio deployed and restarted. Verified rather than assumed clean:

**Deployment confirmed**: Studio `git log -1` matches local HEAD
(`d4cc31a`) exactly, 0 tracebacks. `heavy_sweep_deadline_aware_scoring_mode:
live` and `signal_stream_meta_model_mode: off` confirmed directly from
the live `config/feature_flags.yaml` on Studio.

**A real regression found and fixed during verification, not assumed
away**: `log_renko_bricks` was failing on every single write since
restart (42 occurrences), Postgres error 42P10 — "no unique or exclusion
constraint matching the ON CONFLICT specification." The constraint from
migration 145 (`brick_size` added to the unique key) was genuinely
present in Postgres (confirmed via `pg_constraint`) — PostgREST's schema
cache simply hadn't picked it up, because the migration was applied via
direct SQL (the Supabase MCP tool) rather than a flow that also triggers
a reload. Fixed with `NOTIFY pgrst, 'reload schema';`, confirmed via a
real insert against production Supabase immediately after (succeeded,
verified the row, cleaned it up).

**The actual P0 fix — TimesFM confidence gate can defer bracket
re-optimization indefinitely.** Investigating the stale
`eurusd_usdchf_qualification_drought` roadmap item with fresh data (not
assuming the earlier "genuinely quiet market" read still held) found a
real, more precise root cause: **exactly 5 of 14 symbols** (EURUSD,
GBPUSD, BTCUSD, USDJPY, XAUAUD) have had their last COMPLETED heavy
sweep stuck at 2026-09-06 20:00 UTC — the last pre-daily-cadence Sunday
run — while the other 9 symbols refreshed hours ago under the new daily
cadence. Confirmed directly (`nt_sweep_result`, `source='sweep-heavy'`):
these 5 have been silently TimesFM-confidence-skipped on every single
daily attempt since. Meaning: two of the biggest fixes from the last two
days (daily cadence, deadline-aware scoring) have delivered **zero**
benefit to these 5 symbols, because the confidence gate — which the
worklog already flagged as answering a different question than "is the
bracket well-suited" — has been quietly vetoing every chance to apply
either fix to them.

**Fix**: `_should_force_refresh` (pure decision function,
`heavy_sweep.py`) — if a symbol's last completed heavy sweep is older
than `heavy_sweep_force_refresh_hours` (new config, default 72h), force
the full re-optimization regardless of TimesFM confidence.
`fetch_last_full_heavy_sweep_ts` (new, `supabase_client.py`) supplies the
staleness check. 72h is a bound, not a guess: the OLD weekly cadence
already tolerated up to 7 days of staleness with no override at all, so
72h can never be worse than what shipped before while still preserving
most of the gate's real compute savings on days a fresh run genuinely
isn't warranted.

9 new tests (`test_heavy_sweep_force_refresh.py`,
`test_fetch_last_full_heavy_sweep_ts.py`) — the decision function tested
in isolation rather than mocking the entire heavy-sweep pipeline. 795
total passing. Smoke-tested against real production data: all 5 stuck
symbols currently read `force_refresh=False` (~57h stale, under the 72h
bound) — correct, not yet due. Honest timing note: their last success
(2026-09-06 20:00:29-20:06:00) lands almost exactly 72h before tonight's
20:00 UTC attempt (71h59m-ish), close enough to the boundary that it may
take one more cycle (tomorrow, ~96h stale) before the override
definitively fires, depending on exact timing — not a bug, just an
honest read of the numbers rather than a claim it fires tonight for
certain.

## 12. Fixed nominal lot-size P/L convention — built (2026-09-09)

Closes `pnl_methodology_three_conventions` in `nt_roadmap`: of the
convention proposals discussed there, the user confirmed the intended
shape is a **fixed nominal lot size** (0.01 lot / micro-lot, a realistic
minimum retail position), applied as a constant per-symbol multiplier —
not dynamic equal-risk position sizing (which would size every trade so
its SL loss equals a fixed dollar amount, a materially different and
more involved model the user explicitly ruled out: "which is why I said
nominal lot size, eg. 0.01").

**Design**: `pnl_notional_1unit` (migration 138/146, unchanged) already
encodes P/L as `pnl_bricks * brick_size` — a notional 1-unit-of-underlying
price delta, with NO lot/contract sizing baked in, using the same formula
for every symbol regardless of what "1 unit" means for that instrument's
quoting convention. `pnl_001_lot` is the same underlying number scaled by
a real per-symbol multiplier, so it reflects what an actual 0.01-lot
position would have made — and, unlike `pnl_notional_1unit`, is genuinely
comparable *across* symbols, not just within one over time.

**The multiplier isn't uniform, and isn't even one direction** — grounded
in each symbol's real venue (`nt_symbol.exchange`) and what "1 unit"
already means in the current formula for that quoting convention:

| Asset class | Symbols | "1 unit" today | 0.01 lot | Multiplier | Confidence |
|---|---|---|---|---|---|
| Forex | AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY | 1 unit of base currency | 1,000 units (OANDA: 1.0 lot = 100,000) | **×1000** (scale up) | high |
| Gold | XAUUSD, XAUEUR, XAUAUD | 1 troy oz | 1 oz (OANDA: 1.0 lot = 100 oz) | **×1** (no change) | high |
| Silver | XAGUSD | 1 troy oz | 50 oz (OANDA: 1.0 lot = 5,000 oz) | **×50** (scale up) | high |
| Index | US30USD (OANDA), US500 (PEPPERSTONE) | already ≈$1/point (no multiplier in current formula) | $0.01/point | **×0.01** (scale down) | low-medium (broker-variable) |
| Stock | AAPL (NASDAQ) | 1 share | 1 share (common: 1.0 lot = 100 shares) | **×1** (no change) | medium (common, not universal) |
| Crypto | BTCUSD (COINBASE, spot feed) | 1 BTC | 0.01 BTC (common CFD convention: 1.0 lot = 1 BTC) | **×0.01** (scale down) | medium (overlays a CFD convention on a spot feed) |

Forex/gold/silver multipliers are high confidence because they match
OANDA's actually-published contract specs for the exact venue
`nt_symbol.exchange` names for those symbols. Index/stock/crypto are
lower confidence by nature — broker-variable or convention-based, not
each venue's individually verified live spec sheet — and are labeled as
such in the data itself (`nt_symbol_lot_multiplier.confidence`), not
just in this doc.

**Built**: migration 148 — `nt_symbol_lot_multiplier` (symbol PK,
asset_class_label, lot_unit_description, multiplier, confidence CHECK IN
`('high','medium','low-medium')`), a real reviewable table rather than a
CASE expression buried in a view, populated for all 14 active symbols,
RLS-enabled with a service-role write policy and `SELECT` granted to
`anon`/`authenticated`. Migration 149 — `pnl_001_lot` added to
`v_symbol_signal_scorecard`, `_7d`, and `_monthly` via a `LEFT JOIN` on
symbol, summing `pnl_dollars * multiplier` directly per resolved row
(not multiplying the already-rounded `pnl_notional_1unit`, to avoid
compounding rounding error). `pnl_notional_1unit` is kept alongside it
unchanged — additive only, consistent with this session's "never
silently remove/rename an already-exposed column" discipline.

**A real mistake caught immediately, not shipped**: the first attempt at
migration 149 placed `pnl_001_lot` in the middle of each view's column
list (right after `pnl_notional_1unit`) and failed outright —
`ERROR: 42P16: cannot change name of view column "last_qualified_ts" to
"pnl_001_lot"`. `CREATE OR REPLACE VIEW` requires a strictly
column-order-preserving replacement; a new column must be appended at
the end of the SELECT list, not inserted anywhere else. Fixed by moving
`pnl_001_lot` to the last position in all three views' column lists,
re-applied cleanly.

**Verified against real production data** (not just that the migration
applied) across every asset class, confirming both magnitude and
direction:

| Symbol | pnl_notional_1unit | pnl_001_lot | Multiplier applied |
|---|---|---|---|
| AAPL | -61.62 | -61.62 | ×1 (unchanged, as expected) |
| XAUUSD | -636.63 | -636.63 | ×1 (unchanged, as expected) |
| EURUSD | 0.05 | 48.00 | ×1000 (scaled up, as expected) |
| XAGUSD | 284.77 | 14238.73 | ×50 (scaled up, as expected) |
| BTCUSD | 47158.13 | 471.58 | ×0.01 (scaled down, as expected) |
| US30USD | -1807.85 | -18.08 | ×0.01 (scaled down, as expected) |

Every row matches the derivation table exactly — no sign flips, no
unexpected magnitude. `pnl_methodology_three_conventions` marked
`resolved` in `nt_roadmap`, with the note that it should only reopen if a
new symbol is added to `nt_symbol` without a matching
`nt_symbol_lot_multiplier` row (currently no enforcement of that at
insert time — a future consideration, not built now, since `nt_symbol`
inserts are rare and manual today).

## 13. The TimesFM confidence gate — a sinking foundation, traced and rescoped (2026-09-09)

**SCOPE ONLY — nothing built yet, per this session's standing discipline.**
User pushback, verbatim: "this is a flawed methodology using time to
hopefully correct a technical/process issue... a poor decision that is
causing the problem patched with another poor decision." Correct call.
Full history traced (git log, migration headers, worklogs, `nt_roadmap`)
before proposing anything, rather than patching the patch again.

### The sequence, in full

1. **2026-06-20/21 (origin)** — `supabase/migrations/022_reduce_heavy_with_timesfm.sql`.
   The gate was invented to solve an **infrastructure** problem: Edge
   Function 150s timeouts colliding with Render cold starts under burst
   load (84 heavy invocations/day across 14 symbols → silent 5xx
   storms, per migration 018's own incident writeup). The fix bundled
   two unrelated levers together — cadence reduction (daily → 2-3x/week)
   *and* a new "TimesFM confidence gate": skip the heavy sweep outright
   whenever `abs(p_timesfm - 0.5) > 0.10`. Migration 022's own header
   states the goal directly: "Heavy invocations/day: 39 → 16 (-59%).
   Plus ~70% of those will be TimesFM-skipped → ~5 actual runs/day."
   **This was a compute-cost lever from day one, never a signal-quality
   claim** — the "TimesFM handles non-stationarity 24/7" framing was
   asserted, not measured, and (see #5) never checked against a real
   number until 11 weeks later.
2. **2026-07-25** — ported verbatim into `heavy_sweep.py` during the
   Python-port squash commit, rationale and all, unexamined.
3. **2026-08-28** — `heavy_sweep_reachability_mode` added (shadow): a
   different corrective layer entirely (re-rank the Sharpe-only pick by
   realized bucket expectancy). Never reconciled against the gate;
   still open today (see below).
4. **2026-09-07** — `heavy_sweep_atr_anchored_bricks_mode` added,
   found within 24 hours to solve the wrong variable (candidate *set*,
   not scoring), honestly retired to `superseded` the same day. **Proof
   the team can and does kill an invalidated mechanism fast, when it's
   checked.**
5. **2026-09-08** — real cost measurement finally run
   (`worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`): actual
   heavy-sweep cost is ~29s/symbol average, ~93s worst case, ~7 minutes
   for a full 14-symbol pass. **This directly falsifies migration 022's
   entire founding premise** — there was never a real compute problem
   large enough to justify skipping anything. Weekly→daily cadence was
   fixed as a result (migration 142). **But the confidence-gate skip
   itself — the actual "don't spend compute today" mechanism — was left
   completely untouched**, even though its own justification had just
   been disproven in the same worklog. The current module docstring
   even names the conceptual error explicitly ("conflates... 'is the
   DIRECTIONAL forecast already confident' [with] 'is the currently-
   selected brick/SL/TP GEOMETRY still well-matched'") — the diagnosis
   was written down and then not acted on.
6. **2026-09-08, later same day** — deadline-aware scoring shipped
   live, a genuine, validated, root-cause fix to the Sharpe formula
   itself (70-anchor real backtest, win% 58.5→74.5). Good, independent
   work — unaffected by anything in this section.
7. **2026-09-09, early** — 5 symbols (EURUSD, GBPUSD, BTCUSD, USDJPY,
   XAUAUD) found stuck 2+ days behind on pre-fix brackets, because the
   still-untouched gate had skipped every single daily attempt for all
   five. Correctly diagnosed as a real defect.
8. **2026-09-09** — instead of removing the gate whose founding premise
   had already been falsified the day before, `heavy_sweep_force_refresh_hours`
   (72h) was added **on top of it**: a countdown timer that forces a
   refresh once staleness crosses a bound, while leaving the invalid
   skip condition itself fully intact for every case that doesn't cross
   that bound. A patch on a patch — treating "the gate makes an
   unjustified decision sometimes" by capping how long the unjustified
   decision is tolerated, instead of removing the decision.
9. **2026-09-09, this conversation** — the 72h patch turns out to
   almost exactly coincide with the daily cron's own fire time for
   these 5 symbols (last success landed at ~20:00-20:06 UTC, the same
   time the cron fires). Computed precisely: tonight's attempt lands
   EURUSD at 71h59m31s — **29 seconds short of its own 72h threshold**.
   A timer built to route around a timing problem has its own timing
   problem. This is the concrete illustration of "sinking foundation":
   each layer inherited the fragility of the one below it.

### "What does 'bracket is stale' mean?" — honest answer: it was never defined

Searched the entire codebase for any signal that compares "the
volatility/regime the active bracket was optimized under" against
"the volatility/regime right now" — **no such comparison exists
anywhere.** The closest analogs (`bracket_reachability.py`'s ATR
backfill, the light-sweep regime-shift alert) both operate at the wrong
layer: one is a resolution-time data-quality shim using *current* ATR
as a stand-in for a missing historical value, the other is a tick-to-
tick (minutes) detection-only alert that never feeds back into bracket
selection. "Staleness" has only ever meant, operationally, "wall-clock
time since the last completed run" — which is exactly why every attempt
to fix this reached for a wall-clock threshold instead of a real
measurement. There was nothing else to reach for, because nothing else
was ever built.

### Untangling: what's sound vs. what's a patch on an invalid foundation

**Sound, independent, unaffected by this section:**
- Daily cadence (migration 142) — justified by real cost data, holds.
- `heavy_sweep_deadline_aware_scoring_mode` (live) — root-cause scoring
  fix, validated on 70 real anchors.
- `heavy_sweep_atr_anchored_bricks_mode` (superseded) — honestly killed
  within a day of being found wrong. No action needed.

**Invalid at the root, everything built on it inherits the defect:**
- The TimesFM confidence gate itself (`TIMESFM_CONFIDENT_THRESHOLD`,
  `heavy_sweep.py`) — its only justification (compute cost) was
  measured and falsified on 2026-09-08. The signal it gates on
  (directional confidence) was never causally connected to the thing
  it's supposed to protect (bracket-geometry fit) in the first place —
  two independent, sufficient reasons to remove it, not patch it.
- `heavy_sweep_force_refresh_hours` / `_should_force_refresh` — exists
  solely to bound the damage from the gate above. If the gate goes,
  this has nothing left to bound.

**Open, unresolved, adjacent (not part of this fix, flagged for a
separate decision):**
- `heavy_sweep_reachability_mode` (shadow since 2026-08-28) — its own
  `nt_roadmap` notes already say "possibly superseded by
  heavy_sweep_deadline_aware_scoring_mode... not yet determined." Same
  category of dangling loose end, smaller blast radius, deserves its
  own explicit check rather than being silently swept into this fix.
- `orchestrator.py:3717-3725`'s `_load_brick_params` docstring still
  says "the heavy sweep (weekly) rewrites it" — stale wording left over
  from before migration 142, harmless but worth correcting while in the
  area.
- There has never been an `nt_roadmap` row for the TimesFM confidence
  gate itself, unlike every other mode-gated mechanism this session —
  the gate was only ever referenced inline inside *other* mechanisms'
  notes. That's a process gap: the one mechanism doing the most
  consequential (indefinite deferral) damage was the one mechanism
  never given its own tracked review checkpoint.

### Proposed resolution — remove, don't bound

1. **Delete the TimesFM confidence gate outright** — both
   `TIMESFM_CONFIDENT_THRESHOLD` and the skip branch in
   `heavy_sweep.py`. Every symbol runs its full daily re-optimization,
   unconditionally, every day. No remaining justification exists for
   skipping any of them.
2. **Delete `_should_force_refresh` and `heavy_sweep_force_refresh_hours`
   with it** — a bypass for a gate that no longer exists has nothing to
   bypass. Remove the config flag, the pure function, and its tests.
3. This makes the 71h59m31s edge case moot by removing the premise that
   made timing matter at all — not by making the timer smarter.
4. Close `heavy_sweep_force_refresh_override` in `nt_roadmap` as
   **superseded by removal**, not resolved-by-waiting. Simplify
   `heavy_sweep_daily_cadence_log_confirmation`'s remaining job: once
   skip is not a possible outcome, "did the cron fire and complete" is
   the only thing left to confirm.
5. Add a new `nt_roadmap` row for the gate's removal itself, carrying
   this full history, so the same compute-saving idea doesn't get
   silently reinvented later without institutional memory of why it was
   killed.
6. Separately, explicitly deferred: fix the `heavy_sweep_reachability_mode`
   open question and the stale "weekly" docstring — related, smaller,
   not bundled into this change.
7. Explicitly **not** proposed now: a real event-driven re-optimization
   trigger (e.g., "ATR jumped 40% intraday, don't wait for tonight's
   cron") would be a legitimate *future* enhancement built on an actual
   measured signal (ATR/regime now vs. ATR/regime at last optimization)
   — but it's additive new scope, not a fix for today's problem, and
   building it now would repeat exactly the mistake being corrected
   here: reaching for a plausible-sounding mechanism before confirming
   there's still a problem left to solve once the invalid gate is gone.

Waiting on explicit go-ahead to build steps 1-5 above.

### Built (2026-09-09, same day, following explicit approval)

User confirmed the missing piece of context that makes this an even
easier call: the original 2026-06 justification (Supabase Edge Function
150s timeouts colliding with Render cold starts under burst load) was
an infrastructure constraint of the OLD deployment — the decision to
migrate off Supabase Edge Functions onto Lightning AI Studio already
resolved that constraint entirely, independent of and prior to anything
in this section. So the gate's founding premise was dead on **two
separate counts**: the infrastructure problem it solved no longer
exists (migration to Lightning), and even before that migration, the
2026-09-08 real-cost measurement had already falsified it on its own
terms. Explicit instruction: "streamline/optimize the codebase. Stop
adding time as a fix > it is not a solution! build this worklog."

Steps 1-5 built exactly as scoped, nothing expanded beyond it:

1. **`heavy_sweep.py`**: removed `TIMESFM_CONFIDENT_THRESHOLD` and the
   entire skip branch. TimesFM's forecast is still fetched
   (`_fetch_timesfm_cached`) and still feeds the EV blend downstream —
   it simply no longer gates whether the sweep runs. Module docstring
   rewritten: removed the false "TimesFM confidence gate still skips
   ~70% of scheduled runs... remains useful for its OWN purpose" claim,
   replaced with a full "TimesFM confidence gate — removed" section
   carrying the complete history (infra origin, falsified premise,
   category error, the 71h59m31s near-miss) so this can't get silently
   reinvented later without the context for why it was killed.
2. **Deleted `_should_force_refresh`** (the pure decision function) —
   nothing left to bypass once there's no gate.
3. **Deleted `get_heavy_sweep_force_refresh_hours`** (`core/feature_flags.py`)
   and its `_DEFAULTS` entry, and **`fetch_last_full_heavy_sweep_ts`**
   (`supabase_client.py`) — confirmed zero remaining callers of either
   before deleting (`_should_force_refresh` was its only caller).
4. **`config/feature_flags.yaml`**: removed the `heavy_sweep_force_refresh_hours: 72.0`
   value, replaced the block with a short "REMOVED" pointer to this
   section rather than deleting the historical trail outright.
5. **Deleted both test files** for the removed mechanism
   (`test_heavy_sweep_force_refresh.py`, 6 tests;
   `test_fetch_last_full_heavy_sweep_ts.py`, 3 tests) — testing code
   that no longer exists isn't coverage, it's clutter. Full suite: 791
   passed, 21 skipped (was 800 passed before this change; -9 for the
   deleted tests, 0 new failures, 0 regressions).
6. **In-area honesty fixes** (flagged in scope as related, done while
   already touching this code): `orchestrator.py`'s `_refresh_symbols`
   and `_load_brick_params` docstrings both still said "the heavy sweep
   (weekly)" — stale since migration 142 (2026-09-08) changed cadence to
   daily. Corrected both to reflect current, accurate behavior.
7. **`nt_roadmap`**: `heavy_sweep_force_refresh_override` marked
   `superseded` (not `resolved` — it wasn't validated, it was deleted).
   New row `heavy_sweep_confidence_gate_removed` added (`resolved`, no
   review needed) carrying the full history so the same compute-saving
   idea can't get reinvented later without institutional memory of why
   it failed twice. `heavy_sweep_daily_cadence_log_confirmation`
   simplified — "was this symbol skipped by the gate" is no longer a
   variable to control for when checking tonight's cron completion.

**Explicitly not done, per the scope's own step 6/7** (separate
decisions, not silently folded in): `heavy_sweep_reachability_mode`'s
open "possibly superseded by deadline-aware scoring" question is still
unresolved, still `shadow` since 2026-08-28 — untouched. No new
event-driven re-optimization trigger was built — the scope document is
explicit that building a replacement mechanism before confirming a
problem still exists once the invalid gate is gone would repeat the
same mistake this section corrects.

Not yet deployed to Studio — pushed to git only, per this session's
established push/notify pattern.

## 14. TradeJournal.record — the deprecation gate itself deprecated (2026-09-09)

User spotted a second instance of the same pattern this section just
corrected, from the post-restart orchestrator log:

```
[WARNING] regime_platform.services.execution_deprecation: Execution gate
BLOCKED — context=TradeJournal.record. noble-trader-fastapi-backend is
not a trade executor. Use hermes agent (noble-trader-agent) for order
placement.
```

**What "Trade closed"/"Position opened" logging is**: `renko/pipeline.py`'s
`RenkoPipeline` logs one INFO line per simulated position
open/close (`_handle_trade_close`, ~line 1071: `"Trade closed: %s
PnL=%.2f bricks ($%.2f)"`; the open-side equivalent a few lines above).
This is **backtest-internal logging, not live trading** — the sole
caller of `RenkoPipeline` in the live pipeline is
`heavy_sweep.py:_renko_grid_search_sync`'s grid search (up to 90 combos
per symbol: 5 brick sizes × 3 SL × 6 TP), which constructs a fresh
`RenkoPipeline` and replays up to 2160 hourly bars (90 days) through it
per combo, purely to score which combo wins by Sharpe. Every simulated
trade inside every combo's replay logs one of these two lines. **These
messages "appearing new" is a direct, expected consequence of §13's own
fix, not a separate issue**: yesterday's TimesFM-gate removal means all
14 symbols now run their full grid search daily instead of ~9 (5 were
being skipped) — same per-symbol log volume, ~1.5x more symbols
actually running it.

**Why the WARNING fired on every one of those simulated trades**: each
simulated trade close calls `pipeline.journal.record(record)` →
`TradeJournal.record()`, which (before this fix) attempted to schedule
a live `nt_calibration_records` Supabase write on every call, gated
behind `assert_execution_enabled()`. Traced fully before touching
anything: `NOBLE_TRADER_DISABLE_EXECUTION` is permanently true for this
backend (P5 architecture — hermes agent is the sole live executor and
journal, not a temporary state awaiting re-enablement), and confirmed
via search that **no caller anywhere in this repo** ever sets it to
false, so this gate had been unconditionally, permanently blocking on
every single call since P5 (2026-07-23) — an exception constructed and
caught, and two WARNING log lines emitted, for something that can
structurally never succeed. Separately confirmed the real, live
`nt_calibration_records` writer for actual production signals is
`eod_resolver.py` (self-documented as "mirrors renko/trade_journal.py")
— this module's write path was always a parallel, backtest-only
convenience, now provably dead weight, not a load-bearing calibration
input.

**Same root lesson as §13, explicitly named by the user**: "optimize &
fully deprecate this including the warning msg" — a permanently-true
condition doesn't need a runtime gate re-checked and logged on every
call (here: potentially thousands of times per symbol per day across a
90-combo grid search × 14 symbols), it needs the dead branch deleted.

**Built** (`renko/trade_journal.py`):
- `TradeJournal.record()` reduced to its actual job — append to the
  in-memory deque, increment the counter. No gate check, no import, no
  exception construction, no warning, on any call.
- Deleted outright as unreachable: `_schedule_calibration_write`,
  `_run_calibration_in_new_loop`, `_log_calibration_record` (the
  ~100-line Supabase-write implementation, including its schema-drift
  fallback logic), `_PENDING_CALIBRATION_TASKS`, `_normalize_aggression`
  / `_NT_CALIBRATION_VALID_AGRESSIONS` (only ever called from the
  deleted writer).
- Cleaned up now-unused imports left behind by the deletion: `asyncio`,
  `uuid`, `datetime`/`timezone`, `logging` (and its module-level `log`),
  plus a pre-existing (unrelated to this change) unused `time` import
  found while already touching the import block.
- Module docstring rewritten: the old "deprecated, gated, still useful
  for a research harness" framing replaced with a direct statement that
  the write path was removed entirely and why.
- `execution_deprecation.py` itself is untouched — its gate is still
  genuinely load-bearing for its other 5 call sites (kill_switch,
  reconciler, executor_factory, mode_manager, the operational HTTP
  routes), all of which guard an actual attempted order/position action,
  a fundamentally different kind of call site than a backtest journal
  entry. Only `TradeJournal.record`'s use of it was the category error.
- Full suite: 791 passed, 21 skipped, 0 failures, 0 tests referenced the
  removed behavior (none existed to update or delete this time).

**Not changed, flagged as a related but separate, smaller optimization**:
the "Trade closed"/"Position opened" INFO lines themselves are still
emitted at their original volume (now higher, per the explanation
above) — they're harmless (no gate, no exception, no Supabase call
behind them) but are pure per-simulated-trade noise inside a tight
backtest loop with no operational reader. Worth considering a demotion
to DEBUG level or removal in a future pass; not done here since it
wasn't part of what was asked and doesn't share the "permanently-false
gate" defect this section actually corrects.
