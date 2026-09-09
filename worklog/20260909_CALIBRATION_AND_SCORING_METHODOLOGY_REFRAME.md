# Calibration + Scoring Methodology Reframe — Scope Only, Nothing Built

Continuation of `worklog/20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md`. That
document fixed real bugs (mark-to-market expiry P&L, deadline-aware sweep
scoring, episode-counted calibration bias) but a fresh-eyes review of the
whole EOD/calibration pipeline (same day) concluded the calibration-bias
*architecture* has a conceptual defect predating those fixes: two overlapping
correction mechanisms, evaluated far faster than evidence refreshes, capable
of starving their own evidence supply. The user paused further deployment to
rethink the whole thing from first principles. This document is that rethink.
**No code has been written from this document — it is scope only, per
"scope in worklog, get honest review, build only on explicit instruction."**

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
