# 2026-09-07 — Reachability as the sole quality lens, live (non-batch) resolution
# in the signal engine, and a user-facing W/L% + P/L% report

**Status: SCOPE ONLY — not built.** Grounded in real data pulled from production
Supabase (`nt_signal_sim`, `nt_bracket_reachability`, `nt_calibration_bias_by_bucket`,
`nt_heavy_sweep_reachability_shadow`) and direct code reads, same discipline as
`20260825_CALIBRATION_REFRAME_SCOPE.md` (Phases 1-3, which this scope continues
from). Nothing below changes behavior; it's a plan to be reviewed before any of
it is built.

## Explicit framing constraints (from review of this scope)

- **Reachability is the only lens.** This does *not* propose changes to
  `calibration_enforcement.py`, `eod_correction.py`'s bias classification,
  regime labelling, or `MARKOV_SOURCE_MODE`/regime-confidence gating. Those
  stay exactly as they are.
- **No new timeout concept.** `eod_resolver.py`'s `EXPIRY_BARS = 48` (≈48h)
  stays the one and only definition of "too slow to count." Nothing here
  proposes a second timeout, a different deadline, or a "grace period."
  Live resolution (Phase B below) *detects* the same 48h deadline sooner —
  it does not redefine it.
- **Real-time pricing scope = the engine, not a user-facing chart.** This is
  not the Talaria/nobletradingapp "TradingView companion" concept from
  earlier in this thread — that's a separate, unscoped idea. This is about
  the signal engine itself resolving `nt_signal_sim` rows against live TDVA
  prices instead of waiting for the once-daily batch job, so its own
  feedback loop (and the report in Phase C) isn't stale by up to 24h+48h.
- **No new `os.getenv()` flags.** Any mode gate follows the existing
  `config/feature_flags.yaml` pattern (`heavy_sweep_reachability_mode`,
  `TIMESFM_GATE_MODE`, `MARKOV_SOURCE_MODE`) — tri-state `off|shadow|live`,
  read via `feature_flags.py`, not an env var.
- **`compute_ev` is untouched.** Nothing here changes EV/expectancy math,
  Kelly sizing, or the qualification gate stack in `orchestrator.py`.

## How we got here

A signal-quality review (this session) picked XAUUSD as a case study and found
its qualified buy/sell brackets sit almost entirely in the "4+ ATR" distance
bucket, which resolves via TP only ~5-17% of the time and expires ~68-91% of
the time depending on the gold-family symbol. Checking whether this
generalizes surfaced three separate failure modes across the full symbol set
(already documented in `20260825_CALIBRATION_REFRAME_SCOPE.md`): reachability
(XAUUSD/XAUEUR/XAUAUD), directional (USDCAD), and magnitude/confidence
(US500/XAGUSD). Per this thread's explicit steer, only the reachability lens
is in scope going forward — it's the one that's a sizing defect fixable by
changing what brackets get selected, not a claim about model confidence.

Tracing *why* the grid search keeps picking unreachable brackets surfaced the
root mechanism (detail below): the backtest that scores candidate brackets has
no timeout concept and runs over a 90-day window, while live only allows 48h —
an apples-to-oranges comparison that structurally favors wide TPs in
backtest-Sharpe terms regardless of live reachability. A shadow diagnostic for
exactly this already exists and is already confirming the gap in production
(`heavy_sweep_reachability_mode: shadow`, running since 2026-08-28).

Separately, asking "why wait for the daily resolver at all, when TDVA prices
are live" surfaced that TDVA (TradingView Data API, via RapidAPI) already
flows live into `noble-trader-proxy` (persistent WS, 20 watched symbols,
1-10Hz), but nothing in `noble-trader-fastapi-backend` currently consumes it
for signal resolution — `eod_resolver.py` only ever pulls historical candles
once a day.

---

## Phase A — reachability-gated bracket selection (closes the sizing gap)

**Current state** (`heavy_sweep.py`, confirmed by direct read + the 2026-08-25
worklog's live simulation, re-confirmed live this session via
`nt_heavy_sweep_reachability_shadow`):

- `generate_brick_sizes()` (`heavy_sweep.py:161-190`) derives brick size from
  `price × a coarse volatility-tier %`, **not anchored to ATR**. The grid it
  feeds — `[3,5,7] sl_bricks × [4,5,6,8,10,12] tp_bricks` — has no guarantee
  of containing a candidate near the symbol's own ATR.
- `pick_best_combo()` (`heavy_sweep.py:202-223`) filters on `min_rr`/
  `max_sharpe`/`min_trades` only, then takes `max(sharpe)`. No reachability
  or timeout-proportion term anywhere in the score.
- `RenkoPipeline.backtest_with_analytics` (`renko/pipeline.py:836-886`, via
  `backtest()` 787-834 and `_handle_trade_close()` 972-984) has **no
  timeout/mark-to-market concept at all** — a trade still open at the end of
  the backtest window is silently dropped from `total_trades`/`sharpe`, not
  counted as a loss. The grid search feeds it 2,160 hourly bars (90 days,
  `SWEEP_CANDLE_HOURS`, `price_fetcher.py:31`). Live gives the same bracket
  only 48 bars before `eod_resolver.py` calls it expired. A wide-TP combo
  that eventually closes somewhere in a 90-day sample scores a great
  backtest Sharpe using ~45× the runway it actually gets live.
- A shadow fix already exists: `reachability_aware_pick()`
  (`heavy_sweep_reachability.py:73-141`) re-ranks the same quality-filtered
  candidate pool, additionally requiring each combo's distance/ATR bucket to
  show `expectancy_r > 0` (or no data yet) in `nt_calibration_bias_by_bucket`.
  Gated by `heavy_sweep_reachability_mode: shadow` (`config/feature_flags.yaml:106`,
  flipped from `off` on 2026-08-28) — running in production today, changing
  nothing live.
- **Live shadow data pulled this session** (`nt_heavy_sweep_reachability_shadow`,
  accumulated 2026-08-28 → today): every symbol's baseline (Sharpe-only) pick
  lands in the "4+" bucket, with no exceptions across 7 symbols checked. For
  XAUEUR (10/10) and XAUAUD (6/6), **nothing in the grid survives** a
  reachability filter at all — not a worse pick beating a better one, no
  candidate anywhere in the search space clears a positive-expectancy
  bucket. XAUUSD is a mixed case (3/7 nothing survives, 3/7 pick would
  change). USDCHF/USDJPY need no change — their "4+" pick is already fine.
  This matches the 2026-08-25 worklog's one-day simulation almost exactly,
  10+ days later — this is a stable, not a one-off, finding.
- Even flipping `heavy_sweep_reachability_mode` to `live` today would not fix
  gold: the mode's `live` path falls back to the unfiltered Sharpe-only pick
  whenever nothing survives (`heavy_sweep.py:562-573`), which is most of the
  time for XAUEUR/XAUAUD per the shadow data above.

**Proposed Phase A (build, pending review):**

1. **Widen the grid**, not the filter. Add a small number of explicit
   ATR-multiple brick-size candidates (e.g. 0.5×/1×/2× ATR) alongside
   `generate_brick_sizes`'s existing price-%-based candidates, so the search
   space actually contains options in the buckets that resolve reliably
   (per Phase 1's finding: XAUUSD's real edge lives at 0.5-1× ATR, not 4+).
   This is the previously-scoped, not-yet-built "Phase 3b" from the
   2026-08-25 worklog.
2. **Only after Phase A.1 ships and accumulates a few weeks of shadow data**,
   revisit flipping `heavy_sweep_reachability_mode` to `live` — with a
   defined fallback for the (should now be rarer) case where nothing still
   survives (open decision already flagged in the prior worklog: keep
   today's Sharpe-only pick for that cycle vs. skip the sweep entirely via
   the existing `no_combo_passed_filters` path).
3. Do **not** touch `MIN_RR`/`MAX_SHARPE`/`MIN_TRADES` defaults, the EV
   blend, or `_evaluate_heavy_qualification`. Same boundary the prior
   worklog already committed to.

---

## Phase B — live (non-batch) resolution in the signal engine

**Current state:**

- `eod_resolver.py` is the only place `nt_signal_sim.outcome` gets written.
  It runs once daily (00:32 UTC, `EOD_LOOKBACK_STALE_AFTER` catch-up guard at
  `orchestrator.py:612`), fetches up to `LIGHT_BARS=200` post-entry 1h closes
  per symbol, and close-walks them looking for the first TP/SL touch.
  `exit_ts` is **approximated** as `signal.ts + hold_bars hours`
  (`shift_ts()`), not the real wall-clock touch time, because the resolver
  only has hourly bar closes to work with, not raw ticks.
- TDVA (TradingView Data API) already streams live ticks into
  `noble-trader-proxy` (`proxy/tvda/stream.py`/`client.py`, persistent WS,
  20 watched symbols) and is the sole data source for both sweeps'
  historical candle fetch (`price_fetcher.py:1-8`). Nothing in
  `noble-trader-fastapi-backend` currently consumes this live stream.
- The backend already has one precedent for consuming a proxy-side stream
  cross-service: `l2_imbalance.py`'s docstring describes `p_microstructure`
  being computed in the proxy (`noble-trader-proxy/src/proxy/microstructure/`)
  and consumed via a `proxy_microstructure_client` over the proxy's
  `/sse/alerts`-equivalent stream. **Caveat: grep found no implemented
  `proxy_microstructure_client` module in this repo** — this is a documented
  intended pattern, not proof the cross-service link is live today. Treat it
  as a design precedent to reuse, not existing plumbing to depend on.
- Grep of `noble-trader-proxy/src` found no existing TP/SL matching against
  open positions anywhere in the proxy — this would be new logic, not a
  wire-up of something that already exists.

**Proposed Phase B (build, pending review):**

1. A new lightweight consumer (name TBD, e.g.
   `regime_platform/sweep_orchestrator/live_resolver.py`) that holds the set
   of currently-open (`outcome IS NULL`) `nt_signal_sim` rows in memory per
   symbol, and on each live TDVA price update for that symbol, checks
   whether price has crossed that row's `stop_loss` or `take_profit` —
   exact same buy/sell TP-then-SL comparison `eod_resolver.resolve_signal`
   already does, just evaluated against a live quote instead of an hourly
   close.
2. On a cross, write the same outcome shape `eod_resolver.py` writes today
   (`outcome`, `exit_price`, `exit_ts`, `resolved_at`, `pnl_bricks`,
   `pnl_dollars`) — with one genuine improvement: `exit_ts` becomes the
   **real wall-clock touch time** instead of the `shift_ts()` hour-count
   approximation, since a live tick has a real timestamp.
3. **`eod_resolver.py`'s daily batch stays as the backstop**, unchanged —
   it's what still catches `expired` (the 48h deadline is unchanged, per the
   framing constraints above) and anything the live path missed (proxy
   restart, connectivity gap, etc.). This is additive, not a replacement.
4. Gate under a new `config/feature_flags.yaml` tri-state
   (`live_resolution_mode: off|shadow|live`, default `off`), same ladder
   discipline as every other shadow-mode flag in this codebase. Shadow mode:
   compute what it would have resolved and when, log it, write nothing —
   compare against what the batch resolver eventually says the next day, to
   validate correctness before it's allowed to write.
5. **Open architecture question, not resolved by this scope**: does the
   live consumer run inside `noble-trader-fastapi-backend` (subscribing to
   whatever stream the proxy exposes) or inside `noble-trader-proxy` itself
   (which already holds the live quotes, and would need to write to
   `nt_signal_sim` in Supabase — a new capability for that service)? Given
   TDVA is a single data plane already centralized in the proxy
   (`l2_imbalance.py:19`'s stated reasoning for consolidating there), the
   proxy-side option avoids a second live data plane in the backend, but
   moves signal-resolution logic (currently 100% backend-owned) into a
   service that today only relays market data. This needs a decision before
   Phase B.1 is built, not during.

---

## Phase C — user-facing signal-quality report (W/L% + P/L%, no resolution jargon)

**The ask**: "past 7 days, 1000 signals generated, W/L% + P/L%." Users don't
see or care about `expired`/pending states — they want to know the signals
are accurate and can make money.

**Why this can't be built honestly today, and what has to land first:**

- Right now, a naive 7-day report computed straight from `nt_signal_sim`
  would be a biased sample, not a clean one. For XAUUSD-family symbols,
  ~68-91% of signals in their dominant bucket never resolve within the
  window at all — at any given moment, a large chunk of "the last 7 days'
  signals" simply have no verdict yet. A report that silently drops
  unresolved rows (to avoid showing "expired" to the user) would skew
  toward whichever outcome happens to resolve fastest, which is exactly the
  kind of self-inflicted bias the 2026-08-25 worklog's methodology notes
  already warn about (the rejected "symbol velocity" metric, for the same
  reason).
- **Phase A (reachability-gated brackets) is what actually makes this report
  honest**, not a reporting-layer trick: once brackets are chosen from
  buckets that reliably resolve inside the live 48h window, the fraction of
  any 7-day cohort still unresolved at report time shrinks to close to zero,
  and "W/L% over the last 7 days' signals" stops needing an asterisk.
  **Phase B (live resolution) makes it current** — a report run mid-day
  reflects that morning's outcomes, not yesterday's batch.
- **`pnl_bricks`/`pnl_dollars` are deterministic from the pre-chosen bracket
  geometry, not realized price action** (`eod_resolver.py::compute_pnl`): a
  win is always exactly `+tp_bricks`, a loss always exactly `-sl_bricks`.
  "P/L%" for the report should be presented as **realized expectancy**
  (win-rate-weighted R, already computed as `expectancy_r` in
  `nt_calibration_bias_by_bucket`'s methodology) rather than an average
  dollar P/L, which would just restate the pre-declared reward:risk ratio
  and tell the user nothing new about whether the system is actually
  working.
- **Known statistical wrinkle to flag, not fix in this phase**: qualified
  signals are not always independent draws. Checked this session — XAUUSD
  has re-fired the exact same `(entry, stop_loss, take_profit)` bracket up
  to 16 times in a 1-2 day span during a persistent regime (only 246 of 307
  qualified XAUUSD signals have a distinct bracket). A "1000 signals"
  headline count is honest as "1000 things the engine published," but isn't
  1000 independent bets — worth a footnote in the report design, not a
  fix here (fixing it would mean changing the cooldown/re-qualification
  logic, which is out of scope).

**Proposed Phase C (build, pending review, sequenced after A+B):**

1. A new read-only view/report over `nt_signal_sim`, scoped to `qualified =
   true`, `ts` in the trailing N days, restricted to rows that carry a
   settled economic outcome. Given the constraint against surfacing
   "expired"/"pending" language to the user: fold `expired` into the loss
   side of W/L% (it cost the same as a hold with nothing to show for it —
   0 realized R, not a win) rather than excluding it, so every signal in
   the window counts toward the headline number and nothing needs to be
   silently dropped. This is a reporting-presentation choice, not a change
   to what `eod_resolver.py` calls an outcome internally.
2. Metrics: signal count, W/L% (tp_hit vs. sl_hit+expired), realized
   expectancy (`expectancy_r`), and daily expectancy (`expectancy_r` /
   median hours-to-resolve) — reusing `nt_calibration_bias_by_bucket`'s
   existing formulas, not new math.
3. Explicitly NOT in Phase C: no per-user/per-plan personalization, no
   capital or position-sizing claims (P/L% is a per-signal R-multiple
   statistic, not a portfolio return — consistent with this project's
   standing scope boundary that expectancy/reachability metrics are
   signal-level only, with no capital or concurrency modeling), no new
   table — a view over `nt_signal_sim` is sufficient.

---

## Sequencing

Phase A → Phase C is a hard dependency (the report is misleading without it).
Phase B is independent of A/C and can proceed in parallel, but its main
payoff (a *current*, not yesterday's, report) only matters once C exists.
Recommended order: **A.1 (widen grid) → let it accumulate shadow data →
B (shadow) → C (report, once A's effect on expiry rate is confirmed in
production) → A.2 (flip reachability mode to live) → B (live)**.

## Phase A.1 build notes (2026-09-07)

Built as scoped above, with one addition agreed during implementation: a
dedicated `heavy_sweep_atr_anchored_bricks_mode` gate (`off|shadow|live`,
`config/feature_flags.yaml`, default `off`) rather than widening the grid
unconditionally. The scope text above only said "widen the grid," but
`config/feature_flags.yaml`'s own governing comment on
`heavy_sweep_reachability_mode` already called Phase 3b (this work)
"a separate, not-yet-built follow-up **gated** on shadow data accumulating" —
and an ungated widening would have let `pick_best_combo`'s plain Sharpe-max
immediately change every symbol's live bracket the moment this shipped, with
no observation window, which is exactly the kind of change this codebase's
own history (the 2026-08-13/2026-09-04 `MIN_RR` incidents) says not to ship
bare. Same ladder discipline as every other mode flag here, not a scope
change.

**Files:**
- `config/feature_flags.yaml` — new `heavy_sweep_atr_anchored_bricks_mode`
  key, default `off`, full off/shadow/live semantics documented inline.
- `regime_platform/core/feature_flags.py` — `get_heavy_sweep_atr_anchored_bricks_mode()`
  getter + `_DEFAULTS` entry, same shape as every other mode getter in this
  module.
- `regime_platform/sweep_orchestrator/heavy_sweep.py`:
  - `generate_atr_anchored_brick_sizes(atr)` — new pure function, 3
    candidates at 0.5×/1×/2× ATR, deduplicated, `[]` when `atr <= 0`.
  - `select_best_combo_with_atr_anchoring(grid_results, original_brick_sizes,
    mode, ...)` — new pure function (no I/O), same shape as
    `pick_best_combo`/`heavy_sweep_reachability.reachability_aware_pick`,
    so the off/shadow/live selection logic is unit-testable without mocking
    Supabase/RenkoPipeline. Returns `chosen` (what production actually
    uses) plus `baseline`/`widened`/`changed` for logging — `chosen` is
    restricted to the original 5-candidate set in `shadow` mode (zero
    live-behavior change) and becomes the widened pick in `live` mode
    (falling back to baseline when nothing ATR-anchored survives the
    filters, or rescuing the sweep when only an ATR-anchored candidate
    does).
  - `run_heavy_sweep_for_symbol` (step 3): generates the ATR-anchored
    candidates only when the mode isn't `off`, backtests the combined set
    once (no duplicate backtesting), and logs the baseline-vs-widened
    comparison whenever the mode is `shadow`/`live`.
  - Module header docstring updated (step 4/5 counts: up to 8 brick sizes
    × 3 SL × 6 TP = up to 144 combos, up from a fixed 90, when the mode
    is on).
- `tests/test_heavy_sweep_atr_anchored_bricks.py` (new) — 8 tests: brick-size
  generation (zero/negative ATR, the 0.5/1/2× values), and all three modes'
  selection semantics including the "live mode rescues a symbol that would
  otherwise fail `no_combo_passed_filters`" case (the exact scenario
  `nt_heavy_sweep_reachability_shadow` shows happening today for
  XAUEUR/XAUAUD).

**Verification:** full existing suite (`pytest tests/`) — 704 passed, 21
skipped (pre-existing, unrelated), 0 failed, confirming `mode="off"` (the
shipped default) is behaviorally identical to before this change. New tests
verified in isolation with explicit `min_rr`/`max_sharpe`/`min_trades`
(not the module's live `config/feature_flags.yaml`-derived values), so they
don't drift if that config changes.

**Explicitly not touched**: `pick_best_combo`'s own signature/filters,
`heavy_sweep_reachability_mode`'s existing gate/behavior (untouched —
Phase A.2's later decision to flip it stays a separate, future step),
`compute_ev`, qualification gates, `MIN_RR`/`MAX_SHARPE`/`MIN_TRADES`
defaults, `eod_resolver.py`, calibration bias, regime labelling. Confirmed
via diff — only the four files listed above changed.

**Still not built** (per this scope's own sequencing): Phase A.2 (flipping
`heavy_sweep_atr_anchored_bricks_mode` to `shadow` then `live`, after a real
observation window against production shadow logs), Phase B (live
resolution), Phase C (the user-facing report). `heavy_sweep_atr_anchored_bricks_mode`
ships at `off` — this commit changes zero live trading behavior on its own;
someone still has to deliberately flip it.

## Observation window, detailed (2026-09-07), then closed out (2026-09-07)

**Gap found while detailing this, now closed**: Phase A.1 as originally
built only logged the baseline-vs-widened comparison via `logger.info` — it
did not persist anything queryable. Every other shadow mechanism in this
codebase (`nt_heavy_sweep_reachability_shadow`, `nt_markov_source_shadow`,
`nt_trend_probs_gate_shadow`, `nt_regime_debounce_shadow`) persists a small
per-symbol row instead, precisely so a flip-to-live decision can be made
from a SQL query, not by grepping logs across however many weeks. **Built**:
migration `135_nt_heavy_sweep_atr_anchored_shadow.sql` (applied to
production), `record_heavy_sweep_atr_anchored_shadow()` in
`supabase_client.py`, and the wiring in `heavy_sweep.py` that fires it
whenever `atr_anchored_mode in ("shadow", "live")` — same shape as
`nt_heavy_sweep_reachability_shadow`/`record_heavy_sweep_reachability_shadow`,
reusing `heavy_sweep_reachability._bucket_for_combo` for the bucket math
rather than re-deriving it. See "Phase A.2 build notes" below for the full
file list.

**Also resolved**: the mode flag stays **global** (all symbols at once) —
not a per-symbol override dict. This changes the Stage 1 framing below:
since there's no way to flip just the symbols that are ready, "properly
complete" means reviewing `nt_heavy_sweep_atr_anchored_shadow` across ALL
symbols together before any flip, not clearing a per-symbol bar one at a
time. The per-symbol day-counts in the table below are still useful — they
tell you which symbols are the long pole for that joint review — but they
no longer double as individual go/no-go gates.

### Why this needs two stages, not one

Shadow mode (`select_best_combo_with_atr_anchoring`, mode="shadow") only
answers "would the picker choose something different." It cannot answer
"is that different bracket actually good" — because in shadow mode nothing
gets persisted to `brick_params.yaml`, so no real qualified signal is ever
generated from an ATR-anchored bracket, so nothing ever resolves against
it in `nt_signal_sim`. Those are two separate questions requiring two
separate kinds of evidence:

- **Stage 1 (selection-level, shadow-only, no live risk)**: does the
  widened grid, when it picks something different, pick something that
  ALREADY has good historical evidence per the existing
  `nt_calibration_bias_by_bucket`/`nt_bracket_reachability` tables? This is
  checkable immediately, per comparison, with zero waiting — it doesn't
  need new outcomes, just a bucket lookup against data that already exists.
- **Stage 2 (outcome-level, requires a live flip)**: once actually
  selected, do ATR-anchored brackets resolve the way the bucket history
  predicted — real win rate, real expectancy, real `pct_expired`? This
  requires flipping to `live` for at least one symbol, then waiting through
  the full pipeline lag (see below) for real resolved trades to accumulate.

Conflating these two would be the mistake — a good Stage 1 read is
necessary but not sufficient; Stage 2 is where the fix actually gets
proven or disproven.

### Stage 1 — selection-level shadow validation

**Per-comparison check** (mechanical, doable the moment a row lands):
compute the widened pick's own distance/ATR ratio
(`widened.tp_bricks * widened.brick_size / atr`), bucket it the same way
`bracket_reachability._bucket_for` does, and look up that
`(symbol, bucket)` in the existing `nt_calibration_bias_by_bucket`. Pass
condition: the widened pick's bucket shows `expectancy_r > 0` (or has no
data yet — same fail-open rule `reachability_aware_pick` already uses),
and — the point of this whole exercise — a **narrower** bucket than the
symbol's current baseline (e.g. XAUUSD moving from "4+" toward "0.5-1"/"1-2").

**Sample-size threshold before treating a symbol's pattern as real, not
noise**: propose **n_compared >= 10** per symbol. Reasoning: below this
codebase's own `heavy_sweep_grid.min_trades` bar (5, the minimum backtested
trade count before a single *combo* is trusted at all) would be too low
for a cross-week comparison; above `ev.calibration_min_records` (20, the
bar for trusting a *calibration bias* read) is stricter than this
selection-level check needs, since Stage 1 isn't measuring calibration —
it's checking "does the picker consistently prefer a better bucket," a
coarser signal. 10 sits deliberately between the two.

**Real cadence, pulled from the sibling shadow table today** (both shadow
mechanisms fire from the exact same heavy-sweep call site, so
`nt_heavy_sweep_reachability_shadow`'s `n_compared` — accumulated
2026-08-28 → 2026-09-07, 10 calendar days — is a direct proxy for how fast
the new table would fill):

| Symbol | n_compared (10 days) | Days to reach n=10 at this rate |
|---|---|---|
| AUDUSD, USDCHF | 24 | already past — ~4 days |
| XAGUSD | 23 | ~4 days |
| AAPL | 16 | ~6 days |
| US500 | 13 | ~8 days |
| USDCAD | 12 | ~8 days |
| XAUEUR | 11 | ~9 days |
| US30USD | 10 | ~10 days (at threshold today) |
| **XAUUSD** | 8 | ~13 days |
| XAUAUD | 6 | ~17 days |
| USDJPY | 3 | ~33 days |
| GBPUSD, EURUSD, BTCUSD | 2 | ~50 days |

This is much faster than the module docstring's theoretical "~0.6 runs/
week/symbol" (which only accounts for the weekly cron minus the TimesFM
skip) — the observed rate is higher because `nt_eod_actions`-driven
emergency heavy sweeps (`emergency_heavy_sweep`, triggered by
`bayesian_penalty`/`degeneracy_breaker`/`stagnation_kill_switch`) also fire
outside the weekly cron. **Use the observed per-symbol rate, not the
theoretical one, to decide when each symbol is ready** — this is inherently
per-symbol, not one global date; USDJPY/GBPUSD/EURUSD/BTCUSD will lag the
gold pairs by weeks.

**Stage 1 exit criterion, now that the flag is confirmed global**: not
"each symbol clears `n>=10` independently," but a single joint review,
timed to when the **slowest symbol that actually shows `changed=True`
activity** reaches `n>=10` — a symbol that never shows `changed=True`
doesn't gate anything (flipping is a no-op for it regardless of sample
size), so it drops out of the critical path entirely. Concretely: watch
`nt_heavy_sweep_atr_anchored_shadow.n_changed` per symbol as `n_compared`
climbs; the review date is set by whichever symbol is both (a) actively
changing picks and (b) slowest to accumulate — per the table above, that's
plausibly USDJPY (~33 days) or GBPUSD/EURUSD/BTCUSD (~50 days) *if* they
turn out to be in the "changes often" group, or as early as ~13-17 days
(XAUUSD/XAUAUD) if the FX majors and BTCUSD mostly show `changed=False`
(plausible — `nt_heavy_sweep_reachability_shadow` already shows USDCHF/
USDJPY's baseline pick landing in a bucket that's fine as-is, so
ATR-anchoring may simply have nothing to improve there). **This can only be
answered by watching the new table accumulate — not assumed up front.**

**The joint review itself**: for every symbol where `n_changed` is a
meaningful fraction of `n_compared`, confirm `last_widened_bucket` checks
out against `nt_calibration_bias_by_bucket` (per the per-comparison check
above). A global flip should not proceed while any frequently-changing
symbol fails that check — with a single flag, there's no way to carve out
just the symbols that are ready and exclude the ones that aren't. If one
symbol fails while the rest pass, the options are: wait longer / investigate
that symbol specifically (e.g. does it need different ATR multipliers than
0.5x/1x/2x), or accept the flip with that symbol under tight Stage 2
monitoring, or don't flip yet. This is the real cost of "global, not
per-symbol": one bad-fit symbol can hold back thirteen ready ones, and
there's no cheaper structural fix for that within the current flag shape.

### Stage 2 — outcome-level live validation

Only after the joint Stage 1 review above passes. Flip
`heavy_sweep_atr_anchored_bricks_mode` to `live` (single YAML edit,
`config/feature_flags.yaml`) — applies to all symbols simultaneously.

**Pipeline lag once live** (all pre-existing, unchanged by this work):
a newly-selected ATR-anchored bracket only produces evidence once it (a)
actually qualifies a real signal (still subject to the full gate stack,
untouched by Phase A), (b) that signal resolves — up to `EXPIRY_BARS=48`
hours, plus however long until the once-daily 00:32 UTC batch resolver
picks it up (Phase B, not yet built, is what would shorten this), and (c)
the weekly `bracket_reachability`/`calibration_bias_by_bucket` cron folds
the new resolved row into the aggregate bucket stats it's checked against.
Realistically: first real evidence ~1-2 weeks after a symbol's first
ATR-anchored qualified signal; a trustworthy post-flip readout (proposing
the same n>=10 resolved-signal bar, this time on actual `tp_hit`/`sl_hit`
outcomes for that bucket) is a 4-8 week affair per symbol, gated by how
often that symbol actually qualifies a signal at all, not just how often
the grid runs.

**Rollback trigger**: flip back to `shadow` (or `off`) immediately if
**any** live symbol's realized `expectancy_r` for its newly-selected bucket
comes in worse than its previous baseline bucket's historical number — cheap
and fully reversible, no migration or data loss, since `brick_params.yaml`
gets overwritten by the very next heavy sweep regardless of mode. Because
the flag is global, a rollback triggered by one symbol reverts everyone —
same asymmetry as the Stage 1 joint-review gate above, which is exactly why
that gate is conservative by design.

## Phase A.2 build notes (2026-09-07) — closing the observation-window gap

Built same day as the detail above, once the persisted-table gap was
identified as the one remaining piece of code (not calendar time, not a
human decision) standing between Phase A.1 and a properly reviewable
observation window.

**Files:**
- `supabase/migrations/135_nt_heavy_sweep_atr_anchored_shadow.sql` (new,
  applied to production) — same column shape as migration 128's
  `nt_heavy_sweep_reachability_shadow`: `symbol` PK, `n_compared`,
  `n_changed`, `last_baseline_bucket`/`last_baseline_sharpe`,
  `last_widened_bucket`/`last_widened_sharpe`, `updated_at`.
- `regime_platform/sweep_orchestrator/supabase_client.py` —
  `record_heavy_sweep_atr_anchored_shadow()`, fetch-then-upsert, byte-for-
  byte the same pattern as `record_heavy_sweep_reachability_shadow`
  (including `updated_at` in the payload from the start, per that
  function's own documented lesson about upsert-on-conflict never
  touching absent columns).
- `regime_platform/sweep_orchestrator/heavy_sweep.py` — the existing
  shadow/live logging block now also fires
  `asyncio.create_task(record_heavy_sweep_atr_anchored_shadow(...))`,
  computing `last_baseline_bucket`/`last_widened_bucket` via
  `heavy_sweep_reachability._bucket_for_combo(combo, atr)` (reused, not
  re-derived — same cross-module private-helper import pattern
  `calibration_bias_bucket.py` already uses against `bracket_reachability.py`).

**Verification**: full suite re-run after this change — 704 passed, 21
skipped (pre-existing), 0 failed. Migration applied directly to the
production Supabase project (`pcvscowltlrxzgxjurcr`) and its column shape
confirmed via `information_schema.columns` — additive-only (`CREATE TABLE
IF NOT EXISTS`), no existing table/column touched.

**Phase A is now feature-complete** in the sense that matters: `off` mode
ships with zero behavior change (unchanged from A.1), and `shadow` mode (once
someone flips the flag) will actually populate a queryable table instead of
only log lines. What's left is exclusively non-code: the calendar wait for
`nt_heavy_sweep_atr_anchored_shadow` to accumulate, and the joint human
review described above before any flip to `live`.

### Not decided here

- Whether to add a standalone/manual diagnostic runner (mirroring
  `bracket_reachability.py`'s/`calibration_bias_bucket.py`'s
  `python -m ... ` CLI pattern) to accelerate Stage 1 for whichever symbols
  turn out to be the long pole, by running the grid search out-of-band
  against current price data without waiting on the cron — same real-data
  methodology the original Phase 3 worklog used for its one-off live
  simulation, just repeated daily instead of once. Not built; would only be
  worth it once the shadow table shows which specific symbols are actually
  slow AND actively changing (the two-condition critical path above) —
  building an accelerator before that's known risks optimizing the wrong
  symbols.

## Explicitly not in this scope

`calibration_enforcement.py`, `eod_correction.py`'s bias classification,
regime labelling/`MARKOV_SOURCE_MODE`, `compute_ev`/Kelly sizing, `MIN_RR`/
`MAX_SHARPE`/`MIN_TRADES` defaults, `EXPIRY_BARS`, the light-sweep
qualification gate stack, and any user-facing real-time chart/dashboard
(Talaria/nobletradingapp companion — separate, unscoped idea raised earlier
in this thread but deliberately not part of this document).
