# SSE price streaming vs the 15-minute sweep — does entry latency make a difference?

**Date:** 2026-10-14
**Type:** Design analysis (advisory worklog; no code change).
**Trigger:** operator question — *"does live price monitoring via SSE streaming make a difference vs 15-min sweeps?"* — asked with PBM live in shadow and the entry-timing redesign on the table.
**Base:** HEAD `4953e96` + the PBM working tree (Phases A–E; `pbm_mode: "shadow"` deployed). Every anchor below was re-read this session.
**Companions:** `20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md` (the build), `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` §8 (the selection-gap KPI), the entry-timing advisory in the session worklog (`pbm_gap`-conditioned predicates).

---

## 0. Plain answer

**For the entry policy Kairos runs today: no measurable difference.** The measured problem is policy damage, not transport latency. Entries fire at the first clock slot a state is non-neutral (`orchestrator.py:4073` — the regime label picks direction, the clock picks the bar), which buys local extremes *by construction*: −30 points of selection gap at tight buckets (89.5% SL vs the 59.9% null). Streaming that policy faster delivers the same adverse-timed entries sooner. A direction source measured at AUC 0.499 cannot be improved by latency — a coin flip observed tick-by-tick is still a coin flip.

**For the entry-timing redesign (gap-conditioned predicates): yes — decisively, as enabling infrastructure, not as edge.** A predicate like "enter the dip ≥0.8σ below the 24-bar mean *inside* the bull state" is an intrabar object. At 15-minute granularity it exists only as an approximation, and the approximation tax is quantifiable (§1). Streaming is what makes the predicate executable at the same basis the resolver already uses to *settle* trades. But the order matters: **predicate first, streaming second** — latency purchased before policy is a multiplier applied to zero (§7).

**And a finding that reframes the question: the repo already contains a complete streaming stack, and the live sweep consumes none of it** (§2). This is not a build decision; it is a wiring decision that should wait for a measured reason to exist.

---

## 1. What the 15-minute sweep actually quantizes (verified)

| Fact | Anchor at HEAD |
|---|---|
| Light sweep runs in 15-min slots; cooldown admits one publish per slot | `orchestrator.py:253-256` ("15-min cadence already caps scan noise") |
| The picker's evidence loader dedups sweep repeats per hour bucket | `tiered_atr.py:749` ("15-min sweeps repeat the call") |
| TimesFM forecast cached 15 min | `orchestrator.py:296-304` (`_TIMESFM_CACHE_TTL_S = 900`) |
| Direction is the regime label read at tick time | `orchestrator.py:4073` |
| The resolver settles outcomes on the **intrabar** basis (OHLC wicks, `sl_first`) | `eod_resolver` / worklog `20261008_GEOMETRY_BASIS_DIVERGENCE.md` |

So the platform's *action* layer is quantized to ≤15 min while its *measurement* layer already lives at intrabar truth. Entries are the last clock-quantized surface.

**The quantization tax, quantified.** The cost of sweep granularity is not drift — under the null, 15 minutes of drift is unbiased. The cost is **decay of conditional edge between the predicate becoming true and the entry firing**. If a predicate's edge mean-reverts with half-life τ after trigger, entering Δ late captures `exp(−Δ/τ)` of it:

| reversion half-life τ | mean capture (Δ̄ = 7.5 min) | worst-case capture (Δ = 15 min) |
|---|---|---|
| 60 min | 88% | 78% |
| 45 min | 85% | 72% |
| 30 min | 78% | 61% |
| 15 min | 61% | 37% |

Read: for slow predicates (τ ≥ 1h) the sweep is nearly sufficient; for intraday mean-reversion (τ ≤ 30 min — the physically plausible regime for FX dip-entries) **the 15-minute clock destroys 20–60% of whatever conditional edge exists**. That is the honest size of the prize streaming could capture — *conditional on a predicate surviving measurement in the first place*.

---

## 2. The repo already contains the streaming stack — and the sweep consumes none of it

Verified this session, not assumed:

| Component | What it is | Anchor |
|---|---|---|
| `adapters/feed_adapters.py` | Alpaca / Binance / IB WebSocket OHLCV adapters + `FeedManager` (reconnect, multi-symbol routing) feeding `StreamSession.tick()` | module header; `:250` `websockets.connect` |
| `adapters/l2_adapters.py` | L2 order-book snapshots | `:342` |
| `services/stream_session.py` | per-symbol stateful streaming engine: rolling price deque, **incremental HMM predict on every tick**, regime-transition alerts, sizing, risk — emits `StreamTick` (regime_label, suggested_stop, suggested_tp, recommended_f) | module docstring + `StreamTick` |
| `services/l2_feed_manager.py`, `routers/feeds.py` | feed lifecycle endpoints; "adapters stream bars directly into seeded StreamSessions" | `routers/feeds.py:1-10` |

And the decisive grep: **`orchestrator.py` contains zero references to `StreamSession`, `FeedManager`, or the L2 stack.** The sweep's price universe is TradingView 1h OHLC (`fetch_tradingview_batch`, `tiered_atr.py:961-963`); the streaming universe is Alpaca/Binance/IB. They share no data path, no state, no geometry.

This is the same architectural disease the whole review documented — two probabilities, two EVs, two geometries, two calibration paths — now seen at the top level: **two entry engines, one tick-driven and disconnected, one clock-driven and live.** `StreamSession` even has its own per-symbol HMM, sizer, and suggested TP/SL: a complete parallel brain the product doesn't consult. (Whether any live deployment exposes it is not verifiable from this sandbox — stated as a boundary, not a claim.)

Two consequences:

1. **The marginal cost of streaming is wiring, not greenfield.** FeedManager already solves reconnect/gap/multi-symbol routing — the ugliest 20% of streaming infrastructure.
2. **The symbol universes don't overlap.** The sweep trades FX majors, metals, indices via TradingView; the adapters cover equities, crypto, and IB-routable FX. Streaming the *sweep's* symbols needs a new feed source regardless — which strengthens the "predicate before pipe" sequencing (§7), because the pipe itself is a purchase.

---

## 3. Why streaming does not fix the measured damage

- **The −30pt gap is policy damage.** Entries fire on the state, at the clock's pleasure, after the move that created the state. Streaming shaves ≤15 min off the *observation* of the same condition; it does nothing to the condition. You would be earlier into the same wrong-timed entry.
- **There is no listener for ticks.** The current entry vocabulary has no within-state price condition — nothing a tick could trigger. SSE delivers events to a policy with no event handler.
- **Latency is a multiplier on policy quality.** Measured policy quality of the direction layer: AUC 0.499. The product of 0.499-skill and any latency is still no skill.

---

## 4. What streaming enables once predicates exist

1. **Executable intrabar triggers.** Dip-stretch, level-touch-with-persistence, distance-from-flip — actable at price granularity instead of approximated at slot granularity (the §1 tax refunded).
2. **Action/settlement basis symmetry.** The resolver already settles intrabar `sl_first`; streamed entries act on the same basis. This is the entry-layer instance of the basis-divergence discipline the repo learned the hard way for geometry (`20261008_GEOMETRY_BASIS_DIVERGENCE.md` — close-walk picks vs intrabar reality).
3. **Event-driven episodes.** One entry per predicate trigger + refractory period replaces clock-driven repeats. Cleaner episode structure → less serial correlation per cell → honest `n_eff` for the posterior (the bootstrap-correlation caveat in `scripts/measure_pbm_drift_auc.py` exists precisely because clock-driven sampling correlates rows).
4. **Continuous covariates for gap conditioning.** deviation-Z, distance-to-barrier, and time-in-state measured at the entry *instant* become stampable on `nt_pbm_shadow` — richer conditioning than hourly-label + slot-time.
5. **The HMM cadence problem is already solved in the streaming universe.** `StreamSession` runs incremental per-tick predict — the pattern for state-at-tick without waiting for the hourly bar. (Unifying it with the sweep's label source is its own project; the bones exist.)

## 5. What streaming does NOT change

- **Geometry, pricing, posterior cells, Phases B/C/D.** Cells are keyed `(symbol, side, vol_tier, bucket, deadline_class, era)`; entry frequency is not in the key. The barrier null is untouched by transport.
- **Settlement.** Already intrabar; the sim platform marks to market — there is no execution slippage to improve, and if live execution ever arrives, slippage modeling is a new cost that streaming *adds*, not removes.
- **The honesty.** μ=0 streamed is μ=0. A faster clock on a no-edge policy is no-edge, faster — with more rows to store.

## 6. Risks and costs (if wired)

- **Episode explosion.** If every tick becomes a signal candidate, `nt_signal_sim` floods and posterior cells fill with highly correlated samples — the exact pathology the AUC harness flags. Cooldown must become per-predicate refractory, not clock slots.
- **The backtest basis gap.** There is no historical tick archive for the sweep's symbols (1h OHLC via TradingView is the ledger's basis). Predicate backtests must start OHLC-bounded — design predicates whose triggers are *visible on hourly bars* (touch + close-back), then let streaming refine. Otherwise the platform re-creates the basis-divergence bug class for entries.
- **Feed gap discipline.** A reconnect gap during a burst = missed trigger or phantom level. `FeedManager`'s reconnect handling is the right bone; gaps must still be *recorded* so predicates can refuse to fire on incomplete windows.
- **Never let a tick imply state.** Predicates bridge price and state (`state ∧ price-condition`); a tick must never re-derive regime on its own or the two-universes disease gets worse, not better.

## 7. Sequencing — the actual decision

1. **Now, zero infra:** offline predicate discovery on the shadow ledger — join `nt_pbm_shadow` × `nt_signal_sim`, condition `gap` on deviation-Z / time-in-state / hour / side. Design predicates that degrade gracefully at 15-min granularity.
2. **Next, zero new infra:** sweep-granularity live **qualifier** — skip entries when the predicate is absent. A measurable book change using the existing 15-minute machinery; the posterior prices the new population honestly (it already does).
3. **Then, conditional purchase:** stream only the symbols where a predicate has **measured** `gap > 0` at sweep granularity *and* a decay signature (edge present but fading intraday). **The decay signature is the business case for streaming.** Without it, streaming is latency without edge.

Expected honest outcome, given the measured record: most predicates STAY-OFF, the streaming purchase may never trigger — and that is the correct result, not a failure. The 15-minute clock is currently the most robust component in the whole stack; do not spend its reliability on a multiplier for zero.

## 8. Honest boundaries

- The §1 decay table assumes mean-reverting conditional edge — a modeling assumption, not a measurement. The measurement (`gap` per cell) is what decides whether any of it applies.
- `StreamSession`'s tick-HMM was not evaluated for quality here; only its existence and disconnection were verified.
- Nothing in this worklog changes live behaviour; it informs sequencing of the entry-layer redesign already advised (predicate → qualifier → trigger).

## 9. Files / lineage

- Anchors at HEAD `4953e96` (re-read this session): `orchestrator.py:253-256/:296-304/:4073`, `tiered_atr.py:749/:961-963`, `adapters/feed_adapters.py` (header, `:250`), `adapters/l2_adapters.py:342`, `services/stream_session.py` (docstring, `StreamTick`), `routers/feeds.py:1-70`.
- Companions: `20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md`; `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` §8; `scripts/measure_pbm_drift_auc.py` (bootstrap correlation note); worklog `20261008_GEOMETRY_BASIS_DIVERGENCE.md` (the basis-discipline precedent).
