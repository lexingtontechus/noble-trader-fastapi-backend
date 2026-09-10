# 2026-08-25 — Reframing calibration: from "is the raw model calibrated"
# to "is our bracket even reachable" (scope only, not built)

**Status: Phase 1 BUILT (2026-08-25) — migration 120 written but not yet
applied; see §Phase 1 detail for the final shape.** This documents an
investigation thread that started as "run a
48h calibration bias analysis" and ended up surfacing three separate,
compounding problems with what "calibration" currently measures. Related
to Part 1 Change B (`worklog/20260825_CHANGE_B_MARKOV_SHADOW_MODE.md`)
and Part 2 Stream A/C (`.claude/plans/goofy-rolling-swing.md`) but is its
own thread — it reframes the calibration *measurement* itself, not the
Markov derivation or the decay-weighting math.

> **Priority**: P1 · **Task**: Reframe calibration measurement around
> bracket reachability (ATR-bucketed hit-rate/expectancy), Phases 1-3 ·
> **Status**: pending (Phases 1-2 built and deployed; Phase 3 remains
> scope-only, awaiting a build instruction) ·
> **Date**: 2026-08-25

---

## How we got here

1. Asked to analyze calibration bias over the last 48 hours. Found the
   literal window was empty — no calibration record has an `entered_at`
   that recent, because outcomes only get produced by a once-daily
   resolver (00:32 UTC) and a trade can take up to 48h to resolve on top
   of that. **Root cause 1: resolution is batched once a day, so
   "intraday" correction only re-scores yesterday's batch, it never sees
   fresher ground truth.**
2. Asked "what does calibration even mean, and is `nt_signal_sim` the
   right population." Found calibration bias is computed over **every**
   simulated signal the sweep considered, not just the ones that passed
   the 7-gate qualification chain and became a real (paper) position —
   94-98% of the calibration population for the flagged symbols
   (AAPL/US500/USDCAD/XAGUSD/XAUUSD) was signals we never traded.
   **Root cause 2: calibration answers "is the raw model well-calibrated
   across every hypothetical," not "is our actual trading strategy's
   confidence trustworthy."** (Checked whether restricting to
   qualified-only rescues anyone — it doesn't; bias is equal or worse on
   the real, tradeable subset in every case checked.)
3. Asked to refine "directional calibration" (Idea B from the scoping
   discussion) and to show evidence for "does a wide-TP buy signal ever
   hit." Found a third, likely-dominant root cause below.

## Root cause 3 — bracket width, not model confidence, drives most expiries

Bucketing qualified buy signals by TP distance from entry (all symbols
pooled):

| TP distance | n | % hit TP | % expired |
|---|---|---|---|
| <0.5% | 430 | 69.8% | 7.2% |
| 0.5–1% | 126 | 34.1% | 61.9% |
| 1–2% | 652 | 14.3% | **79.0%** |
| 2–4% | 289 | 5.9% | **83.4%** |

57% of all qualified buy signals sit in the 1-4% bucket, where 79-83%
never get a verdict. That's not a directional failure — the model's call
is never even tested for most of these, because the bracket was placed
somewhere the symbol doesn't reliably reach inside the live deadline.

**Raw % distance is meaningless across symbols**, though — same nominal
"4%+ away" bracket:

| Symbol | n | % hit TP |
|---|---|---|
| US500 | 16 | 93.8% |
| XAGUSD | 71 | 76.1% |
| XAUEUR | 27 | 0.0% |
| XAUAUD | 14 | 0.0% |
| BTCUSD | 11 | 0.0% |
| XAUUSD | 9 | 0.0% |

A 4% target is trivial for XAGUSD/US500 right now, essentially
unreachable for XAUEUR/XAUAUD/BTCUSD/XAUUSD in the same window — bracket
width has to be measured against each symbol's own realized volatility,
not a flat percentage.

**When a bracket does resolve, time-to-hit scales sensibly with width**
(pooled, tp_hit/sl_hit only):

| TP distance | n | Median hours | p75 | p90 |
|---|---|---|---|---|
| <0.5% | 609 | 1h | 1h | 1h |
| 0.5–1% | 157 | 1h | 1h | 11h |
| 1–2% | 277 | 2h | 7h | 13h |
| 2–4% | 107 | **8h** | 14h | 20h |

The 48h deadline itself is not the bottleneck — a 2-4% bracket that
resolves at all does so in ~8-20h, comfortably inside the window. The
problem is purely that most brackets in that range never get touched.

(Methodology note: an earlier attempt to estimate "symbol velocity" as
`tp_distance ÷ hours_to_hit` per symbol was tried and rejected — it's
biased, since winning trades disproportionately have tiny targets that
resolve in bar 1 almost by definition, which produced nonsense numbers
like "US500 can move 200% in 48h." The pooled, bucketed view above is the
honest read.)

## Reframing: the ladder approach

Per the discussion, this should not be a fixed global threshold — it's a
**per-symbol matrix, dynamically calculated from real outcomes,
refreshed on a cadence, that improves as more data accumulates.** Same
"ladder" pattern as the 3/7/21-day calibration-horizon idea and the
existing `TIMESFM_GATE_MODE`/`MARKOV_SOURCE_MODE` shadow-mode precedent:
start as a read-only diagnostic, prove it out, then let it gate real
decisions.

Two consumers, once the table exists:

1. **Calibration** (refines "Idea B" from the scoping discussion): split
   "expired because the bracket's reachability was near-zero to begin
   with" (a sizing defect, not a calibration signal) from "expired
   despite a bracket the symbol reaches often" (a genuine weak-edge
   signal). Only the second category should count toward over/under-
   confidence.
2. **Bracket selection** (Part 2 Stream A, now with a concrete
   mechanism): the heavy sweep's grid search currently picks
   `(brick_size, sl_bricks, tp_bricks)` by maximizing unbounded-backtest
   Sharpe with no connection to live's real deadline. Filtering/
   penalizing combos whose `distance/ATR` ratio has poor historical
   reachability closes that gap directly.

## Proposed phasing

- **Phase 1** (this review): persist ATR at sweep time; build the
  reachability table as a **read-only diagnostic** computed from existing
  `nt_signal_sim` history. Zero behavior change — makes the pattern
  queryable/reportable instead of hand-computed in SQL each time.
- **Phase 2**: wire the table into calibration bias — recompute the
  earlier OVERCONFIDENT findings (AAPL/US500/USDCAD/XAGUSD/XAUUSD) split
  by reachable vs. unreachable brackets, see how much of the verdict
  survives.
- **Phase 3**: wire the table into heavy sweep's combo selection so
  future brackets are chosen with the reachability constraint built in.

Phase 2 and 3 are not scoped in detail yet — deliberately, so Phase 1 can
be reviewed on its own first.

---

## Phase 1 detail (for review)

**Files:**
- `regime_platform/sweep_orchestrator/heavy_sweep.py` — `compute_atr`
  already exists and runs at sweep time but its result isn't persisted
  anywhere; capture it into whatever gets written alongside
  `brick_size`/`sl_bricks`/`tp_bricks` (this gap was already flagged,
  unbuilt, in Part 2 Stream B's design).
- New: a reachability-table builder (working name
  `regime_platform/sweep_orchestrator/bracket_reachability.py`) that
  reads resolved `nt_signal_sim` rows (qualified and unqualified both —
  this stage is purely about "does this bracket width get reached,"
  which unqualified rows can still answer honestly, unlike calibration
  bias itself), computes `distance / ATR` per row, buckets per symbol,
  and outputs hit-rate + time-to-resolution percentiles per bucket —
  the same shape as the tables above, generalized and persisted instead
  of ad-hoc SQL.
- Storage: a new small table (e.g. `nt_bracket_reachability`) keyed by
  `(symbol, distance_atr_bucket)`, refreshed on the same cadence as the
  heavy sweep (weekly) so it reflects current volatility, not a stale
  snapshot.
- No changes to `orchestrator.py`'s live signal path, no changes to
  calibration bias, no changes to heavy sweep's combo selection — this
  phase only produces the table and makes it queryable/reportable.

**Verification plan**: recompute the tables in this worklog directly
from the new persisted table/function and confirm they match the ad-hoc
SQL versions above — real-data validation, not synthetic, same
discipline as the rest of this session.

**Explicitly not in Phase 1**: no enforcement, no change to what feeds
`nt_calibration_records`, no change to bracket selection. Those are
Phase 2/3, gated on this being reviewed and approved first.

## Phase 1 build notes (2026-08-25)

Built as scoped above, with two deviations agreed during implementation:

- ATR is now captured in **both** the heavy-sweep and light-sweep paths
  (`heavy_sweep.py`, `orchestrator.py::_process_symbol`), not heavy-only —
  the light sweep produces the ~94-98% unqualified population the table
  needs to reflect. Additive to the `nt_signal_sim` payload only; no
  qualification/signal-decision code touched (confirmed via diff grep).
- Historical rows (captured before `nt_signal_sim.atr` existed) are
  backfilled with each symbol's **current** ATR(14) as a proxy
  (`atr_source='proxy_current'` vs `'stored'` per bucket row), rather than
  reconstructing point-in-time ATR or excluding them.

Files: `supabase/migrations/120_nt_bracket_reachability.sql` (adds
`nt_signal_sim.atr`, creates `nt_bracket_reachability`, not yet applied —
apply via Supabase CLI or the `mcp-server-supabase` tools when ready),
`regime_platform/sweep_orchestrator/bracket_reachability.py` (new,
dual module/CLI — `python -m regime_platform.sweep_orchestrator.bracket_reachability`
for a standalone report), `supabase_client.py` (`upsert_bracket_reachability`),
`settings.py` (`BRACKET_REACHABILITY_ENABLED`, default on), and a weekly
Sun 07:00 UTC cron job in `orchestrator.py::start()`.

Aggregation logic verified locally with synthetic rows (network calls
stubbed) — bucketing, hit-rate %, and hours-to-resolve percentiles all
compute correctly.

### Live verification (2026-08-25, migration 120 applied)

`python -m regime_platform.sweep_orchestrator.bracket_reachability` run
against production Supabase: 21 bucket rows written across all 14 active
symbols, `atr_source='proxy_current'` for all of them (expected — no row
had a stored `atr` yet at run time). Selected results:

| Symbol | Bucket | n | %TP | %SL | %Exp |
|---|---|---|---|---|---|
| XAUUSD | 0.5-1 | 1660 | 85.9% | 14.1% | 0.0% |
| XAUUSD | 4+ | 2729 | 4.4% | 15.9% | 79.8% |
| XAUEUR | 1-2 | 1662 | 82.7% | 17.3% | 0.0% |
| XAUEUR | 4+ | 2730 | 1.7% | 7.0% | 91.4% |
| XAUAUD | 0.5-1 | 1663 | 85.0% | 15.0% | 0.0% |
| XAUAUD | 4+ | 2812 | 2.0% | 9.9% | 88.1% |
| AAPL | 4+ | 3836 | 5.5% | 49.4% | 45.1% |
| USDCAD | 4+ | 4369 | 0.4% | 39.9% | 59.7% |
| US500 | 1-2 | 117 | 0.0% | 100.0% | 0.0% |
| US500 | 4+ | 3522 | 9.1% | 8.6% | 82.4% |
| XAGUSD | 4+ | 4481 | 31.7% | 36.0% | 32.3% |

Sharpens the raw-% ad-hoc tables above considerably: the gold pairs
(XAUUSD/XAUEUR/XAUAUD) hit 82-86% when the bracket lands within ~0.5-2
ATRs of entry, vs. near-zero (79-91% expired) at 4+ ATRs — confirming
those symbols' brackets are simply oversized relative to their own
volatility. Most other symbols (AAPL, USDCAD, US500, and the FX majors)
sit almost entirely in the 4+ bucket regardless of outcome, meaning
nearly every bracket generated for them today is far outside 1-2x their
own ATR. This result feeds directly into the Phase 2 scoping below.

## Phase 2 scope (2026-08-25)

Phase 2, per the original framing above: "wire the table into calibration
bias — recompute the earlier OVERCONFIDENT findings split by reachable
vs. unreachable brackets, see how much of the verdict survives." This
section scopes that only — nothing below is built yet.

### Mechanism gap found

There is **no join key** between `nt_calibration_records` (the table
calibration bias is actually computed from — `predicted_p_win`, `outcome`
win/loss only, no bracket geometry) and `nt_signal_sim` (has the geometry
+ `atr`, but isn't itself the calibration population).
`eod_resolver.py::_write_calibration_record` mints a fresh `id` per
insert and never carries over `nt_signal_sim.signal_id`. So a proper,
joinable split requires a migration (`nt_calibration_records.signal_id
UUID NULL`, populated going forward at that insert call site — same
"can't backfill history, just start capturing" shape as Phase 1's `atr`
column). For *this* scoping pass, the split below was computed directly
against `nt_signal_sim`'s own win/loss-resolved rows (`p_win`,
`entry_price`, `take_profit`, `atr`, `outcome`) — same underlying data,
no join needed, since every `tp_hit`/`sl_hit` row there is 1:1 in
substance with a calibration record, just not linked by key today.

### Baseline (fresh pull, 2026-08-25, `fetch_calibration_bias_raw`, 7-day decayed window)

| Symbol | n (raw) | avg predicted p_win | win rate | bias | status |
|---|---|---|---|---|---|
| AAPL | 737 | 0.5254 | 4.15% | +0.4838 | OVERCONFIDENT |
| US500 | 63 | 0.6752 | 0.0% | +0.6752 | OVERCONFIDENT |
| USDCAD | 69 | 0.8164 | 0.0% | +0.8164 | OVERCONFIDENT |
| XAGUSD | 970 | 0.4321 | 5.18% | +0.3803 | OVERCONFIDENT |
| XAUUSD | 374 | 0.5404 | 14.32% | +0.3973 | OVERCONFIDENT |

All 5 confirmed OVERCONFIDENT right now — this is the number Phase 2's
split needs to explain, not a stale reading from an earlier session (no
single earlier snapshot covered exactly these 5 — see the investigation
note below the table in this section's predecessor discussion).

### Reachability split (all-time, `nt_signal_sim`, win/loss-resolved rows only)

| Symbol | Bucket | n | avg p_win | win rate | bias |
|---|---|---|---|---|---|
| AAPL | 4+ (only) | 2107 | 0.3354 | 10.06% | +0.2348 |
| US500 | 1-2 | 117 | 0.9837 | 0.0% | **+0.9837** |
| US500 | 4+ | 620 | 0.9588 | 51.45% | +0.4442 |
| USDCAD | 4+ (only) | 1762 | 0.9731 | 0.96% | +0.9635 |
| XAGUSD | 4+ (only) | 3034 | 0.7937 | 46.77% | +0.3260 |
| XAUUSD | 0.5-1 | 1660 | 0.9987 | 85.90% | +0.1396 |
| XAUUSD | 4+ | 552 | 0.6923 | 21.56% | +0.4767 |

**The hypothesis only holds for one of the five symbols.** XAUUSD's bias
drops from +0.4767 (4+ bucket) to +0.1396 (0.5-1 bucket) — a real,
substantial improvement, consistent with the gold-pair pattern in the
Phase 1 table above. The other four don't fit that story:

- **AAPL, USDCAD, XAGUSD have no win/loss-resolved rows outside the 4+
  bucket at all.** Every bracket that ever resolves for these symbols
  today is already 4+ ATRs wide — there is no "reachable" population to
  compare against yet. That's a sizing observation (heavy sweep's grid
  search isn't producing narrower brackets for these symbols), not
  something a calibration-side split can fix — it's Phase 3's territory
  (bracket selection), not Phase 2's.
- **USDCAD's 4+ bucket is 0.4% TP hit vs. 39.9% SL hit** (from the Phase
  1 table) — when it resolves, it's almost always a loss, not a timeout.
  Matches `worklog/20260824_PENDING_ACTIONS_POST_EOD_RUN.md`'s prior note
  that USDCAD's problem "is directional, not expiry" (1,745 sl_hit vs. 17
  tp_hit). Reachability framing doesn't apply here — this is a genuine
  directional miscalibration.
- **US500's "reachable" bucket (1-2 ATR, n=117) is bias +0.9837 — worse
  than its 4+ bucket (+0.4442).** The model predicts ~96-98% win
  probability in both buckets regardless of bracket width; the 4+
  bucket's realized win rate (51.45%) is roughly fair, but the stated
  confidence is absurd either way. This is a magnitude/confidence
  miscalibration, not a reachability one — narrowing the bracket doesn't
  help because the problem is the claimed certainty itself.
- **XAGUSD's single bucket shows a roughly balanced win rate (46.77%)**
  against a much higher predicted p_win (79.37%) — same magnitude-
  miscalibration shape as US500, distinct from USDCAD's directional
  shape and AAPL's partial version of both.

So "OVERCONFIDENT" today is masking at least three distinct failure
modes: **reachability-driven** (XAUUSD, and by the Phase 1 fingerprint
probably XAUEUR/XAUAUD too), **directional** (USDCAD, partly AAPL), and
**magnitude/confidence** (US500, XAGUSD). A single reachable/unreachable
split recovers the picture for one mode and would silently misattribute
the other two if presented as *the* explanation.

### Proposed Phase 2 (still read-only diagnostic — ladder discipline continues)

1. **Migration**: add nullable `nt_calibration_records.signal_id UUID`;
   populate it going forward at `eod_resolver.py::_write_calibration_record`
   (one-line payload addition). Historical rows stay unlinked — same
   backfill trade-off Phase 1 made for `atr`.
2. **New diagnostic table**, e.g. `nt_calibration_bias_by_bucket`
   (`symbol`, `distance_atr_bucket`, `n`, `avg_p_win`, `win_rate`,
   `bias`) — same shape as `nt_bracket_reachability`, but scoped to the
   win/loss-only calibration population (mirrors this section's table
   above, generalized to all symbols and refreshed on a cadence). Builder
   can read directly from `nt_signal_sim` as this scoping pass did — the
   `signal_id` FK is worth adding for future joinability but isn't a hard
   blocker for computing the split itself.
   - **Broadened (decision below): carry `pct_expired` across from
     `nt_bracket_reachability`'s matching `(symbol, bucket)` row into the
     same output row**, rather than inventing new derived
     "directional"/"magnitude" columns. One row then shows all three
     lenses at once without a hardcoded classifier: `pct_expired` high →
     reachability story (bucket rarely resolves at all); `win_rate` near
     0 (or 1) → directional story (resolves, but the call is wrong);
     `win_rate` near 0.5 with `bias` still large → magnitude/confidence
     story (roughly fair outcomes, absurd stated confidence). This is
     just a join of the two tables' existing columns — no new
     classification logic, so it doesn't lock in a reachable/unreachable
     cutoff or a mode-detection rule the codebase has no precedent for
     yet (see the "no existing cutoff precedent" gap found earlier).
3. **Explicitly NOT in Phase 2**: no change to `eod_correction.py`'s
   `_classify_bias`/`_classify_bias_raw` or the actions it writes
   (`bayesian_penalty`, `kelly_reduction`, `emergency_heavy_sweep`,
   `stagnation_kill_switch`), no change to `auto_tuner.py`'s health
   metric, no change to the migration-119 SQL views. Given the mixed
   result above — the split explains one symbol's overconfidence and not
   the other four's — enforcement shouldn't change until a human reviews
   the per-bucket table across the full symbol set, not just these 5.
4. **Known desync risk to flag, not fix, in Phase 2**: migration 119's
   `v_eod_calibration_bias_latest` SQL view hardcodes the same
   threshold constants as `settings.py` with no automated sync (its own
   comment already says so). If a later phase changes what "bias"
   means, that view needs a manual matching edit or it'll silently
   disagree with the Python path.

### Decision (2026-08-25)

Broaden now rather than ship the narrow reachability-only table — see
the "Broadened" note under item 2 above. Rationale: reachability alone
demonstrably explains only 1 of the 5 flagged symbols in this sample, so
shipping the narrow version would almost certainly need an immediate
follow-up once it's reviewed against the full symbol set. Still scope
only (not built) — awaiting a build instruction, same as Phase 1.

## Phase 2 broadened further — user-facing trade-quality metrics (2026-08-25)

Reframing from "is the model calibrated" to three trader-facing
questions: was the entry good, did it win/lose and by how much, and how
fast. Investigated exactly how `mfe`/`mae`/`pnl_bricks`/`pnl_dollars`
(written by `eod_resolver.py::resolve_signal_outcome`/`compute_pnl`) and
paper-portfolio sizing (`paper_book.py`) actually work before designing
anything, since two of the three requested metrics turn out to already
be answerable from existing columns and one genuinely isn't.

### 1. "Was the entry price optimal" — needs new capture

`mfe`/`mae` (max favorable/adverse excursion) already exist on
`nt_signal_sim`, but as two independent, **unordered** maxima over the
walked price path — there's no record of *when* each extremum happened,
so "did price dip against us before it worked out" cannot be answered
from today's columns. (Side finding: migration 102's column comment says
`mfe`/`mae` are "in bricks" — the actual code computes raw price-unit
deltas. Worth a one-line comment fix regardless of what else ships.)

This metric needs new capture, but cheaply — `eod_resolver.py` already
fetches the full post-entry candle path per signal and discards it after
computing the two maxima. Proposed: at the same point, snapshot
close-vs-entry `%` at 3 fixed short horizons (e.g. 1h/4h/24h) into new
nullable `nt_signal_sim` columns (`drift_1h_pct`, `drift_4h_pct`,
`drift_24h_pct`). Aggregated per symbol (mean = "does price typically
run in our favor or against us right after entry," stdev = the
"variance %" asked for) this becomes a genuine entry-timing/fill-quality
diagnostic, and it doubles as a live check on Phase 1's ATR assumption —
does realized short-horizon volatility actually match the ATR the
bracket was sized against.

### 2. Win/loss + PnL ratio — already computable, real numbers below

`pnl_bricks` on `nt_signal_sim` is **deterministic from the pre-chosen
geometry**, not from realized price action — a win is always exactly
`+tp_bricks`, a loss always exactly `-sl_bricks` (`eod_resolver.py::compute_pnl`).
So a naive "avg win $ / avg loss $" would just recover the bracket's
pre-declared reward:risk, telling you nothing new. The informative
version is **expectancy in R**: `win_rate × avg_RR_of_winners − (1 −
win_rate) × 1` (a loss is always exactly −1R by construction) — this
asks whether the system's wins are cheap/easy geometry while losses are
expensive, or vice versa, which is the real "PnL ratio" question.
Computed now, all-time, all 14 active symbols, win/loss-resolved rows
only:

| Symbol | n | win% | avg RR (winners) | expectancy (R) |
|---|---|---|---|---|
| USDJPY | 1490 | 91.8% | 2.25 | **+1.983** |
| USDCHF | 1314 | 91.6% | 2.00 | +1.747 |
| US500 | 737 | 43.3% | 3.33 | +0.876 |
| XAUEUR | 1898 | 74.9% | 2.00 | +1.246 |
| XAUAUD | 1998 | 73.6% | 1.99 | +1.198 |
| XAUUSD | 2212 | 69.8% | 2.05 | +1.131 |
| XAGUSD | 3034 | 46.8% | 2.00 | +0.403 |
| GBPUSD | 1505 | 43.3% | 2.00 | +0.298 |
| AUDUSD | 1060 | 35.5% | 2.48 | +0.233 |
| EURUSD | 1416 | 40.5% | 2.00 | +0.216 |
| BTCUSD | 2501 | 38.2% | 2.04 | +0.162 |
| US30USD | 691 | 30.4% | 2.00 | -0.088 |
| AAPL | 2107 | 10.1% | 2.00 | **-0.698** |
| USDCAD | 1762 | 1.0% | 2.00 | **-0.971** |

Confirms the Phase 2 finding above from a different angle: AAPL and
USDCAD are strongly negative-expectancy, matching their directional-
miscalibration diagnosis. **Caveat, in the same spirit as the rest of
this worklog**: USDCHF/USDJPY's 91%+ win rates over this window are
unusually high for a live signal and should be treated as "this
historical window," not assumed durable, until checked for a one-way
trending period inflating it — this is exactly the kind of number this
new metric is meant to surface for review, not a result to act on
directly.

### 3. ROI over an effective timeframe — already computable, real numbers below

`bracket_reachability.py` already computes wall-clock hours-to-resolve
per bucket (more accurate than `hold_bars`, which is an approximate
bar-count). Recasting the same underlying data as "% resolved within N
days" instead of percentiles, plus dividing expectancy by the typical
hold time, gives a capital-efficiency view raw expectancy alone hides:

| Symbol | expectancy (R) | median hours to resolve | daily expectancy (R/day) |
|---|---|---|---|
| USDJPY | +1.983 | 101.1 | +0.471 |
| USDCHF | +1.747 | 106.8 | +0.393 |
| XAGUSD | +0.403 | **23.7** | **+0.408** |
| XAUUSD | +1.131 | 71.4 | +0.380 |
| XAUEUR | +1.246 | 83.6 | +0.358 |
| XAUAUD | +1.198 | 80.5 | +0.357 |
| US500 | +0.876 | 75.8 | +0.277 |
| BTCUSD | +0.162 | 54.1 | +0.072 |
| GBPUSD | +0.298 | 100.6 | +0.071 |
| AUDUSD | +0.233 | 102.8 | +0.054 |
| EURUSD | +0.216 | 103.0 | +0.050 |
| US30USD | -0.088 | 77.8 | -0.027 |
| USDCAD | -0.971 | 89.8 | -0.260 |
| AAPL | -0.698 | 43.3 | -0.386 |

The reranking is the point: XAGUSD's raw per-trade edge (+0.403R) is
roughly a third of the gold pairs' (+1.1-1.25R), but it resolves 3-4x
faster (median 23.7h vs. 71-84h) — on a per-day basis it's competitive
with, not far behind, the slower gold pairs. A symbol/bucket ranking by
raw expectancy alone would under-rate it.

### Additional metrics assessed

Ranked by how directly they extend what's already captured:

- **Expectancy in R and in $** (item 2 above) — the one number that
  actually answers "is this worth trading," combining win rate and
  payoff ratio into a single sign.
- **Daily/annualized expectancy** (item 3 above) — capital-efficiency
  correction to expectancy; already demonstrated to reorder the ranking.
- **Opportunity cost of expired brackets** — `mfe` is already captured
  for `expired` rows too (not just tp_hit/sl_hit), so
  `avg(mfe / atr)` on expired rows directly answers "how close did the
  price get to a TP it never technically hit" — a $-relevant companion
  to Phase 1's reachability table. Attempted to pull this now; came back
  empty for every symbol because `atr` on `nt_signal_sim` only started
  being populated a few hours ago (this session) and no expired signal
  has had time to resolve since — re-run in ~2 days once enough
  post-migration signals have resolved.
- **Regime-conditioned performance** — `nt_signal_sim.regime`/`regime_conf`
  are already captured per row; slicing expectancy/win-rate by regime
  label answers "does the edge hold in trending vs. ranging markets,"
  a natural question given the system's own regime-detection framing.
- **Max drawdown / losing-streak length** — expectancy alone hides
  sequencing risk; derivable from `v_paper_equity`'s `cumulative_pnl`
  (migration 105) or directly from the outcome sequence per symbol.
- **Sharpe-like consistency** (`mean_R / stdev_R` per symbol/bucket) —
  distinguishes "small reliable edge" from "big edge with wild variance,"
  neither of which expectancy alone reveals.
- **Time-of-day / session effects** — `ts` is already captured;
  bucketing by entry hour could reveal session-dependent edge (e.g. FX
  pairs around London/NY open), directly actionable for entry timing.
- **Sized (kelly-weighted) PnL and return-%** — `nt_signal_sim.pnl_dollars`
  is unsized (same for every signal with the same geometry, regardless of
  position size); a true $ / % return needs `nt_paper_positions.realized_pnl`
  and `size_notional` (already both persisted, just never divided into a
  `realized_pnl / size_notional` return-% today) — narrower population
  (qualified heavy signals only), so best treated as a supplementary
  view rather than the primary one.

### Review outcome (2026-08-25) — all 3 confirmed, with two corrections

- **Idea 3 (ROI over an effective timeframe): go, as scoped.** Daily
  expectancy in R, not literal capital ROI — that distinction stands.
- **Idea 2 (win/loss + PnL ratio → expectancy-in-R): go.** "Edge" is the
  right term (confirmed over "PnL ratio"/"ROI"). The concurrency/capital-
  redeployment caveat raised above is **explicitly out of scope, by
  design, not a gap to fix**: this calibration/expectancy layer scores
  each signal as an independent, mutually-exclusive one-to-one event —
  it does not model or claim anything about portfolio-level capital
  competition between simultaneously-open positions. That's a separate
  concern (paper-book/portfolio sizing), not something expectancy-in-R
  is trying to answer. Worth remembering as a standing scope boundary
  for this whole calibration/expectancy line of work, not just this
  metric.
- **Idea 1 (entry drift/variance): go, correction to my own framing.**
  I'd argued this was weaker because the paper system models zero
  slippage, so "was the entry optimal" has no execution-quality answer
  to compare against. That's true as far as it goes, but it doesn't
  weaken the underlying metric — whether price runs favorably or
  reverses immediately after a signal fires is a signal-*timing*
  question, independent of fill mechanics. Slippage/execution quality is
  a real, separate consideration that matters once/if this compares
  against live fills, not a reason to discount the drift/variance
  diagnostic now. Corrected: idea 1 stands on the timing-quality
  question alone; slippage is a caveat to flag when live execution
  enters the picture, not a flaw in the concept today.

### Still not built

Everything above is scoping + real-data grounding, same discipline as
the rest of this worklog. Nothing new has been written to the schema or
`eod_resolver.py`/`bracket_reachability.py` yet.

## Phase 2 build notes (2026-08-25)

Built as scoped above. Files:

- `supabase/migrations/121_nt_calibration_bias_by_bucket.sql` — not yet
  applied (apply via Supabase CLI or the `mcp-server-supabase` tools when
  ready). Adds: `nt_calibration_records.signal_id` (nullable FK to
  `nt_signal_sim.signal_id`, closes the join gap found during scoping);
  `nt_signal_sim.drift_1h_pct`/`drift_4h_pct`/`drift_24h_pct` (nullable);
  a corrected `COMMENT ON COLUMN` for `mfe`/`mae` (price units, not
  bricks — the migration-102 doc bug found during scoping); the
  `nt_calibration_bias_by_bucket` table (bias, `pct_expired`,
  `avg_rr_winners`, `expectancy_r`, `median_hours_to_resolve`,
  `daily_expectancy_r` per symbol/bucket); the `nt_entry_drift` table
  (mean/stdev of drift per symbol).
- `eod_resolver.py::resolve_signal_outcome` — now also returns
  `drift_1h_pct`/`drift_4h_pct`/`drift_24h_pct`, direction-relative %
  drift at fixed post-entry horizons, computed from the full fetched
  window (not the outcome-truncated walk, since drift measures market
  behavior independent of when the trade itself exited). Verified by
  hand-computed buy/sell cases — both directions produce the expected
  signed %.
- `eod_resolver.py::_write_calibration_record` — now carries
  `sig["signal_id"]` into the `nt_calibration_records` payload.
- New `regime_platform/sweep_orchestrator/calibration_bias_bucket.py` —
  dual module/CLI (same shape as `bracket_reachability.py`):
  `build_calibration_bias_by_bucket()` (win/loss-resolved population,
  joins `pct_expired` from `nt_bracket_reachability`) and
  `build_entry_drift()` (all-outcome population, per symbol). Both
  verified against hand-computed synthetic data (network stubbed) —
  bias, expectancy-in-R, median-hours, daily-expectancy, and
  drift mean/stdev all matched by-hand arithmetic exactly.
- `orchestrator.py::_run_bracket_reachability` — extended to call both
  new builders right after Phase 1's reachability table refresh (same
  weekly job, so the `pct_expired` join reads a fresh table), gated by
  its own `CALIBRATION_BIAS_BY_BUCKET_ENABLED` flag so Phase 1's table
  still refreshes even if Phase 2 is disabled.
- `settings.py` — `CALIBRATION_BIAS_BY_BUCKET_ENABLED` (default on).

**Explicitly not touched** (confirmed via diff + grep): `eod_correction.py`,
`calibration_enforcement.py`, `auto_tuner.py`, and the migration-119 SQL
views. All 32 existing `tests/test_eod_resolver.py` tests still pass.

### Live verification (2026-08-25, migration 121 applied)

First run surfaced a real bug: buckets with zero winners (US500's `1-2`,
US30USD's `<0.5`, both `win_rate=0.0%`) printed `expectancy_r` as
undefined instead of the correct `-1.0` — the formula incorrectly gated
`expectancy_r` on `avg_rr_winners` being non-null, when expectancy is
well-defined at zero wins regardless (the win term is 0 either way).
Fixed in `calibration_bias_bucket.py` and re-run; both buckets now
correctly show `expectancy_r=-1.000`, everything else unchanged.

`nt_entry_drift` came back with 0 rows — expected, not a bug: unlike
`atr`, drift has no valid backfill. It's specific to one signal's exact
post-entry price path, not a symbol-level average that a "current"
snapshot can proxy. It will only start populating as the EOD resolver
resolves new signals going forward.

Selected `nt_calibration_bias_by_bucket` results for the 5 originally-
flagged symbols, confirming the three-failure-mode read from the earlier
scoping pass, now with `pct_expired` alongside in the same row:

| Symbol | Bucket | n | bias | %Exp | expectancy_r | daily_expectancy_r |
|---|---|---|---|---|---|---|
| AAPL | 4+ (only) | 2107 | +0.235 | 45.1% | -0.698 | -0.386 |
| USDCAD | 4+ (only) | 1762 | +0.964 | 59.7% | -0.971 | -0.260 |
| US500 | 1-2 | 117 | +0.984 | 0.0% | **-1.000** | -0.234 |
| US500 | 4+ | 620 | +0.444 | 82.4% | +1.230 | +0.416 |
| XAGUSD | 4+ (only) | 2494 | +0.291 | 32.3% | +0.506 | +0.243 |
| XAUUSD | 0.5-1 | 1660 | +0.140 | 0.0% | +1.577 | +0.403 |
| XAUUSD | 4+ | 552 | +0.477 | 79.8% | -0.211 | -0.223 |

US500's `1-2` bucket is the sharpest confirmation yet that its
overconfidence is a magnitude problem, not reachability: `pct_expired`
is 0% (every one of these brackets gets touched) and it's still a clean
-1.0 expectancy — narrowing the bracket didn't help because the model's
stated confidence itself is wrong, not the bracket width. XAUUSD's two
buckets tell the opposite, reachability-driven story cleanly: 0% expired
/ +1.58 expectancy in the near bucket vs. 79.8% expired / -0.21
expectancy in the far one — same symbol, same model, opposite verdict
purely as a function of bracket width. Having `pct_expired` and
`expectancy_r` in the same row (the "broadened" decision from scoping)
is what makes this readable at a glance instead of needing two separate
tables cross-referenced by hand.

All 32 `tests/test_eod_resolver.py` tests still pass after the fix.
Nothing in `eod_correction.py`, `calibration_enforcement.py`,
`auto_tuner.py`, or the migration-119 views was touched — confirmed via
diff + grep, unchanged from the build notes above.

## Phase 3 scope (2026-08-25)

Phase 3, per the original framing: "wire the table into heavy sweep's
combo selection so future brackets are chosen with the reachability
constraint built in." Investigated the actual grid search
(`heavy_sweep.py`) before designing anything, then ran it live against
real price data for 5 symbols to see what a reachability/expectancy-aware
filter would actually change — same discipline as Phase 1/2.

### Mechanism found

`_renko_grid_search_sync` (`heavy_sweep.py:737-796`) iterates
`brick_sizes × [3,5,7] sl × [4,5,6,8,10,12] tp` (up to 5 brick sizes from
`generate_brick_sizes`, pre-filtered by `MIN_RR`) and calls
`renko/pipeline.py`'s `RenkoPipeline.backtest_with_analytics` per combo
— **not** `regime_platform/core/backtester.py`, which is an unrelated
engine used only by the `/backtest` router. Each `ComboResult` carries
`brick_size`/`sl_bricks`/`tp_bricks`/`sharpe`/`total_trades`/`win_rate`/
`profit_factor` but no distance or ATR field — trivially derivable
(`distance = tp_bricks * brick_size`, same shape `bracket_reachability.py`
already uses) since `atr` is computed earlier in
`run_heavy_sweep_for_symbol` and stays in scope through
`pick_best_combo`'s call site with no plumbing needed.
`pick_best_combo` (`heavy_sweep.py:193-214`) filters by `MIN_RR`/
`MAX_SHARPE`/`MIN_TRADES` then takes `max(..., key=sharpe)` — the caller
holds the full unfiltered `grid_results` list right before this call, so
a reachability-aware filter/re-rank slots in there with zero signature
changes needed downstream.

**Important finding**: `generate_brick_sizes` (`heavy_sweep.py:161-190`)
is *not* ATR-anchored the way this needs — `brick_size` comes from a
`price × base_brick_pct%` formula where `base_brick_pct` is picked from
a coarse `atr/price` volatility band, not scaled to `atr` directly. So
nothing in the grid's own generation keeps `tp_bricks × brick_size`
within a reachable ATR multiple — confirmed this has to be a downstream
concern, not something already implicit in how bricks are seeded.

`nt_sweep_result` stores only the single winning combo per sweep, not
the other ~89 considered and discarded — there is no historical record
to query retroactively. Any evaluation has to re-run the grid search
live.

### Live simulation: today's pick vs. a reachability-aware pick

Ran the actual grid search against real current price data for 5
symbols, computed each candidate's `distance/ATR` bucket, looked up
`nt_calibration_bias_by_bucket` for that bucket, and compared today's
Sharpe-only winner against the best Sharpe combo that also has
`expectancy_r > 0` (or no data yet) in its bucket:

| Symbol | Baseline pick's bucket | expectancy_r | pct_expired | Reachability-aware result |
|---|---|---|---|---|
| XAUUSD | 4+ | -0.211 | 79.8% | **no candidate survives** |
| AAPL | 4+ | -0.698 | 45.1% | **no candidate survives** |
| US500 | 4+ | **+1.230** | 82.4% | unchanged — baseline already fine |
| XAGUSD | 4+ | +0.506 | 32.3% | unchanged — baseline already fine |
| USDCAD | — | — | — | baseline itself found no viable combo (unrelated `no_combo_passed_filters` case, pre-existing failure path) |

Two things this validates, one thing it complicates:

1. **Confirms the mechanism works correctly where a good option exists.**
   US500's bucket is flagged OVERCONFIDENT by the calibration-bias
   formula (avg predicted 95.9% vs. win rate 51.4%) — a naive filter
   keyed on calibration *status* would have rejected this bucket. But
   its realized `expectancy_r` is a strongly positive +1.23R (the payoff
   when it wins, 3.33R, more than compensates for the coinflip win
   rate), and the reachability-aware filter correctly leaves the pick
   unchanged. This is a direct, live confirmation that Phase 2's choice
   to gate on `expectancy_r` rather than on `pct_expired` or bias status
   was the right call — a status-based filter would have made this
   symbol's selection worse, not better.
2. **XAGUSD needed no change** — its current pick already lands in a
   historically-decent bucket, so the filter is a no-op there, as it
   should be.
3. **XAUUSD and AAPL break the "just filter the existing grid" premise.**
   For both, *none* of the current grid's candidates land in a bucket
   with non-negative expectancy — not because a bad one was chosen over
   a good one, but because **the grid never generates a candidate in
   the good bucket at all**. XAUUSD's actual edge lives at 0.5-1 ATR
   (+1.58 expectancy, per the Phase 2 table); nothing in `[3,5,7] sl ×
   [4,5,6,8,10,12] tp` combined with this week's brick sizes produces a
   combo anywhere near that. A pure post-hoc filter has nothing to fall
   back to for 2 of the 5 symbols tested — a real, load-bearing finding,
   not a hypothetical edge case.

### Proposed Phase 3 — two sub-phases, ladder discipline continues

**Phase 3a (shadow only)**: add the reachability/expectancy-aware
re-rank as a **shadow computation** inside `run_heavy_sweep_for_symbol`,
gated by a new tri-state env var following the `TIMESFM_GATE_MODE`/
`MARKOV_SOURCE_MODE` naming convention (e.g.
`HEAVY_SWEEP_REACHABILITY_MODE = off | shadow | live`, default `off`).
In `shadow` mode: compute what the filter would have picked, log both
picks and whether anything survived, change nothing about
`pick_best_combo`'s actual return value. This is deliberately the *only*
thing Phase 3a does — it's a diagnostic that runs against the live grid,
week over week, to measure how often (and for which symbols) "nothing
survives" happens in practice, before committing to a fix. This
simulation found it for 2/5 symbols on one day; a shadow-mode log
running for a few weekly cycles will show whether that's typical or a
one-off snapshot.

**Phase 3b (grid widening, gated separately, only after 3a data exists)**:
since 3a will likely keep finding "nothing survives" for the same
class of symbol, the fix isn't a better filter — it's giving the grid
somewhere good to point the filter at. Add a small number of explicit
ATR-multiple brick sizes (e.g. 0.25×/0.5×/1×/2× ATR) alongside
`generate_brick_sizes`'s existing price-%-based candidates, so the
search space actually contains options in the buckets Phase 1/2 found
performant. Only then does flipping `HEAVY_SWEEP_REACHABILITY_MODE` to
`live` make sense — before 3b, `live` mode would just mean "no combo
qualifies" for symbols like XAUUSD/AAPL, which is worse than today's
behavior, not better.

**Live-mode fallback (design decision, not yet made)**: when even the
widened grid has nothing that clears the expectancy bar, live mode needs
a defined fallback — keep today's Sharpe-only pick for that cycle
(safest, but reintroduces the original problem for that one cycle), or
skip the sweep and let `_log_heavy_failure_trace`'s existing
`no_combo_passed_filters` path handle it (consistent with how a
`MIN_RR`/`MAX_SHARPE`/`MIN_TRADES` shutout is already handled today, but
would make some symbols go a full week without a fresh combo). Left
open deliberately — this is exactly the kind of decision 3a's shadow-mode
data should inform, not something to guess at now.

### Explicitly not in Phase 3

No change to `MIN_RR`/`MAX_SHARPE`/`MIN_TRADES` defaults, no change to
the EV blend or qualification gates (`_evaluate_heavy_qualification`),
no change to the light sweep's YAML-read path
(`_load_brick_params`) — Phase 3a only adds a shadow log inside the
heavy sweep; nothing about what gets written to `brick_params.yaml`
changes until 3b + a live-mode decision are both made.

### Still not built

Scoping + a real live simulation only, same discipline as Phases 1/2.
Nothing written to `heavy_sweep.py`, `settings.py`, or any migration for
Phase 3 yet.
