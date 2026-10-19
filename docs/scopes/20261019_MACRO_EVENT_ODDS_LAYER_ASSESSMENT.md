# Macro Event-Odds Layer — Assessment & Design (TSFM Event Conditioning, Kalshi/Polymarket Feed, Higher-Frequency LLM Additive)

> **Priority**: P2 proposal · **Status**: assessment complete + §9 Clef-Flash
> integration addendum (same-day operator follow-up); no code changed ·
> **Date**: 2026-10-19 (pre-Fed / Black-Monday-anniversary session) ·
> **Roadmap refs**: `macro_regime_mode`, `macro_forecast_agent_enabled`, `clef_flash_mode`, `kairos_mode`, vol-backtest Phase 0 (`d205d42`/`4fe5203`)

---

## 0. The operator's question

Tomorrow (2026-10-19) pairs a scheduled Fed decision with the Black-Monday
anniversary. Four questions, answered against the code as it exists at HEAD
`4fe5203` — every claim below is file-traced, not assumed:

1. Can the TSFM layer (lookback + forecast) price an event like this?
2. Can the macro layer augment prediction under its current daily method?
3. Is the operator's instinct right — more value in Kalshi/Polymarket as a
   macro filter than in the current LLM narrative layer?
4. How would a more frequent macro-LLM additive be implemented, and what
   values, stored where (`macro_filter.py` is the reference)?

---

## 1. Current state — the four macro pieces (verified at HEAD)

| Piece | Module | Cadence | Storage | Mode |
|---|---|---|---|---|
| Price macro trend (200d SMA, EMA-smoothed) | `macro_filter.py` | pre-sweep grid `5,20,35,50 * * * *`, freshness-guarded → effectively 1×/day per new daily close | `nt_macro_regime` (mig. 124) + `macro_label`/`macro_strength` captured on `nt_signal_sim` | `macro_regime_mode: shadow` (Kelly multiplier live-gated OFF) |
| LLM narrative forecast | `macro_forecast_agent.py` | nightly 02:45 UTC | `nt_macro_forecast` (mig. 181/182: thesis/drivers/risks, structure_*, pivot_*, events JSONB) | shadow-only; **nothing reads it for a live decision** |
| Economic calendar + typed pivots | `kairos.py` calendar cache + `kairos_pivot.py` | refresh when >20 h old (hourly :20 job); window [−3 d, +14 d] | `.cache/kairos/econ_calendar.json` (file, no history) + pivot fields stamped on `nt_macro_forecast` | research/T-layer only |
| Macro edge evidence | `macro_edge.py` | manual/rebuild | `nt_macro_edge_by_symbol` (mig. 125/126) | diagnostic; input to the live-flip decision |

**The structural gaps this exposes on an event day:**

- **The narrative agent's raw material is static.** It classifies today's
  position against hand-written annual docs
  (`docs/LLM/commodity_12m_forecast_model_2027.md`,
  `existing_symbols_12m_forecast_model_2027.md`). Running it more often
  re-reads the same document — frequency cannot manufacture information the
  source does not contain. On the eve of a Fed decision it produces nothing
  new.
- **The calendar layer knows the schedule but not the odds.** It records
  that a policy event exists (importance/policy filters in
  `kairos_pivot.select_pivot_candidates`) and can label surprise only
  **after** release (`actual` vs `forecast`). Pre-event it carries no
  outcome distribution at all — nothing answers "is the hike priced in?"
- **The price macro label is structurally inert on the event timescale.**
  A 200-day SMA moves ~0.1 % on any single day; bear/bull/neutral cannot
  change intraday by design (EMA half-life smoothing exists precisely so
  one print can't flip it). Correct for regime, useless for tomorrow.
- **The TSFM input is univariate price.** `server.py` (:8002) — TimesFM
  2.5-200M, `MAX_CONTEXT=1024`, `MAX_HORIZON=256`, no covariate input.
  `p_timesfm` = Φ(z) of forecast rel-change under the per-symbol z-map
  (`timesfm_zmap.py`). The event is not representable in its input.

## 2. The macro layer's own evidence — why `macro_regime_mode` is still shadow

The 2026-09-14 alignment backtests (worklog
`20260914_MACRO_ALIGNMENT_DISTANCE_BACKTEST_SCOPE.md` and the same-day
reliability-filtered resolution) are the controlling evidence for anything
that would turn a macro read into a live multiplier:

- Plain aggregate: "5 of 6 measurable symbols invert the macro filter's
  assumption."
- Reliability-filtered (n ≥ 20 both sides): 1 symbol reverses entirely
  (XAGUSD — aligned actually wins), 1 largely disappears (US30USD), 2
  survive inverted (BTCUSD, US500), 1 rests on a single extreme cell (AAPL),
  1 confirms on thin data (XAUAUD). **8 of 14 symbols have zero usable
  data** — their 200 d read never left `neutral`.
- Consequence already applied in-repo: AAPL is per-symbol macro-disabled
  (constant bull — flag `macro.symbols_disabled`, 2026-10-05 reframe).

Any new event layer must inherit this lesson: **shadow stamps first,
evidence table second, live flip only on a cleared bar.** The macro filter
never got its live flip; the event layer should expect the same scrutiny.

## 3. Q1 — TSFM lookback & event-impact forecasting

**What lookback is today:** 1024 hourly bars ≈ 6–9 weeks of market time
(FX calendars gap weekends; crypto runs continuous). That typically spans
**at most one prior FOMC meeting** — the context contains one example of
the event class we care about. The forecast side (k-bar horizon, z-mapped
to `p_timesfm`) inherits this blindness.

**What a univariate TSFM cannot do:** anticipate a *scheduled*
discontinuity. Vol models without event indicators famously underpredict
announcement-day vol (Ederington & Lee 1993 is the canonical event-study
reference); our own Phase 0 first zero-shot run (`4fe5203`, run
`20261019_010230`) adds the calibration dimension: Chronos-2's native
80 % intervals covered realized vol only 0.19–0.42 — badly too narrow,
Mincer-Zarnowitz recalibration mandatory. Neither zero-shot TSFM "knows"
a Fed decision is tomorrow.

**The three mechanisms that DO work, ranked by cost/evidence:**

1. **Event dummies in HAR/Log-HAR (cheapest, do first).** The Phase 0 bar
   (`vol_backtest/arms.py`) is the exact harness for this: add
   `is_event_day` (or `hours_to_event` decay) as a HAR regressor — standard
   announcement-vol practice, one arm of code, judged by the existing
   gate (beat plain Log-HAR on QLIKE, DM/Holm-significant).
2. **Known-future covariates in Chronos-2 (the endgame arm already has the
   plumbing).** `Chronos2Covariate.forecast()` already builds a future
   frame of genuinely-known-in-advance calendar features
   (`vol_backtest/model_arms.py:336-347`). Adding `is_fomc_hour`,
   `hours_to_scheduled_event` — pure functions of the calendar — is a
   few-line extension, and *odds-derived* features (§5) slot into the same
   frame. This is the only one of the two TSFMs whose API can express
   "the event is tomorrow" at all (TimesFM 2.5 is univariate by
   architecture; the universal TimesFM is license-blocked — settled in
   worklog Task 28).
3. **Longer context only if fine-tuning happens.** Chronos-2's 8192-bar
   context ≈ 11 months ≈ 7–8 FOMC cycles vs ~1 at 1024. But at zero-shot
   the benchmark cost doubles (0.13 s → 0.38 s/forecast, context 512 →
   1024) for unproven gain — keep 512–1024 zero-shot, revisit context
   length as a fine-tune hyperparameter, not a zero-shot knob.

**Lookback summary:** lookback is not the lever; *event representation* is.
A model with one prior FOMC in context and an event covariate beats a
model with eight FOMCs and none, because the latter has to *infer* the
event class from price alone.

## 4. Q2 — can the macro layer augment prediction (daily method)?

Three channels, in increasing order of intrusion — the first two are
testable **now** in the Phase 0 harness, the third is the existing
sizing channel that stays gated:

1. **Macro covariate for the vol forecast** (research arm): pass
   `macro_score_smoothed` history as a past covariate in
   `Chronos2Covariate` and/or as a HAR regressor. Vol dynamics differ by
   regime (leverage effect — bear regimes run hotter); whether the signal
   survives DM/Holm per-symbol is exactly what the harness exists to
   answer. Sample-size caveat from §2: 8/14 symbols live in permanent
   `neutral`, so for them this covariate is a constant — the arm will
   simply learn nothing (harmless, honest).
2. **Regime-split HAR**: estimate Log-HAR per `macro_label` bucket.
   Cheaper than it sounds and mostly degenerate for the same permanent-
   neutral reason. Priority below the covariate arm.
3. **Kelly multiplier** (existing, `kelly_macro_multiplier`, clamp
   [0.25, 1.5]): the *sizing* channel, not a forecast augmentation. Stays
   shadow until the alignment evidence says otherwise (§2 — it currently
   does not).

**Boundary:** none of these makes the daily macro label *faster*. It is a
regime read; its information content is multi-month. The event-day gap is
not a macro-filter bug to fix — it is a missing layer (§5).

## 5. Q3 — Kalshi/Polymarket as the macro filter: the instinct is right, and the repo's own record says why

**Assessment: correct, with a precise role.** Prediction-market prices are
the cleanest continuously-updated, forward-looking macro probability feed
available to this system:

- **They compress exactly what the current layer lacks.** Market-implied
  P(hike) at 0.94 with a 0.06 tail is (a) an outcome distribution
  *before* release, (b) a time series of how the distribution moved on
  news, (c) free of narrative lag. Wolfers & Zitzewitz (2004) is the
  standard result: prediction-market prices ≈ mean beliefs under mild
  assumptions; Kalshi (CFTC-regulated, binary contracts) and Polymarket
  both expose public market-data APIs sufficient for probability
  snapshots.
- **The repo's LLM layer cannot converge on this information by
  re-running.** Its source document is annual and static; its freshest
  input is the 20 h-old calendar cache with post-hoc surprise. An LLM
  polled more often over the same corpus is confident noise. The odds feed
  is the *new information*; the LLM's honest role shrinks to
  interpreting odds *movements* (§6) — which is exactly the operator's
  phrasing: "kalshi+polymarket **as macro filters**."
- **Calibration provenance:** unlike an LLM narrative, odds carry their own
  audit trail — `implied_prob` time series can be scored against outcomes
  (did the 0.94 favorite win?) the same way `nt_calibration_records` scores
  `p_win`. A macro layer that can be scored is a macro layer this
  codebase can trust; one that can't (narrative text) never earns a live
  flag.
- **What they will NOT price:** the Black-Monday *anniversary* itself.
  Anniversary effects are narrative risk, not scheduled information; no
  liquid market prices "crash because it's the 19th." The system's honest
  treatment of the anniversary is elevated baseline vol-risk for the
  session, not a directional signal. The Fed decision, by contrast, IS
  priced — and that's the asymmetry that makes the odds feed worth
  building.

## 6. Q4 — Design: the event-odds layer + higher-frequency LLM additive

Design follows the repo's own conventions everywhere: separate concerns,
shadow-before-live, fail-open, evidence tables, one flag per layer,
`[0.25, 1.5]` clamp band for any multiplier, point-in-time capture on
`nt_signal_sim`.

### 6.1 New feed module — `event_odds.py` (sibling of `kairos.py` calendar)

- **Poll** Kalshi (`/trade-api/v2/markets`, public market data) and
  Polymarket (gamma + CLOB endpoints) for a **curated series list**
  (`FOMC`, `CPI`, `NFP`, plus operator-chosen geopolitical markers).
  Curated, not firehose — the same discipline as `CALENDAR_CURRENCIES`.
- **Cadence — tiered, mirroring the freshness-guard philosophy** (spend
  bandwidth only when information can change): hourly baseline; 15-min
  (light-sweep grid) when any tracked event is within T−24 h. Persist on
  the sweep scheduler like HARM-04's macro refresh, not a second clock.
- **Dedupe/persist rule:** write a poll row when any outcome's
  `implied_prob` moved ≥ 0.5 pp since the last stored row, or on a 30-min
  heartbeat (a flat odds series is itself evidence; full 96/day × series
  would be ~35 k rows/month otherwise).
- **Derived fields** (pure functions, offline-testable — `compute_macro_regime`
  precedent): `consensus_outcome` (argmax), `surprise_potential = 1 −
  max(p)` (probability mass NOT on consensus — the pre-event analogue of
  `pivot_surprise`, which is post-hoc), `prob_change_24h`.

### 6.2 Storage — the crux of "what/where"

**Append-only history table** (not a JSON file — the econ_calendar.json
pattern is fine for a raw schedule, but odds history is the evidence
substrate; every other decision layer here has a table for exactly this
reason):

```sql
-- Migration 2xx: nt_event_odds — market-implied event probabilities
CREATE TABLE nt_event_odds (
  id            BIGSERIAL PRIMARY KEY,
  poll_ts       TIMESTAMPTZ NOT NULL,
  source        TEXT NOT NULL CHECK (source IN ('kalshi','polymarket')),
  event_key     TEXT NOT NULL,              -- 'FOMC_2026-10-19'
  event_ts      TIMESTAMPTZ NOT NULL,       -- scheduled decision time
  market_id     TEXT,
  outcome       TEXT NOT NULL,              -- 'hike_25bp' | 'hold' | ...
  implied_prob  DOUBLE PRECISION,
  bid           DOUBLE PRECISION,
  ask           DOUBLE PRECISION,
  volume        DOUBLE PRECISION,
  open_interest DOUBLE PRECISION
);
CREATE INDEX nt_event_odds_event_poll_idx ON nt_event_odds (event_key, poll_ts DESC);
-- "latest state" = max(poll_ts) per event_key/outcome — a view, not a table.
```

Style matches migrations 124/181/182 (`nt_` prefix, TIMESTAMPTZ, DOUBLE
PRECISION, service-role RLS, CHECK-constrained vocabularies). Append-only
like `nt_signal_sim`; no upsert-merge, because odds history is never
revised.

**Signal-time capture (the macro_filter.py pattern, verbatim):**

```sql
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS hours_to_event DOUBLE PRECISION;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS event_surprise_potential DOUBLE PRECISION;
```

Nullable, stamped regardless of mode — migration 124's
`macro_label`/`macro_strength` precedent: capture first so the evidence
layer can accumulate, apply never until a flag says so.

**LLM digest (the "more frequent macro LLM additive") — event-triggered,
not schedule-triggered.** Do NOT re-run the narrative agent more often
(§1: same static doc). Run a **cheap, narrow digest** only when
`event_odds` fires a trigger — event within T−24 h, or ≥ 5 pp probability
move in 6 h. Input: the odds snapshot + movement + calendar context.
Output (structured, vocabulary-checked — `_validate_classification`
precedent): `{stance: risk_on|risk_off|neutral, vol_expansion:
low|medium|high, confidence: 0-1}` via Clef-Flash (sub-second, already
wired, `clef_flash_mode: shadow`) with the OpenRouter/NIM client as
fallback. Store:

```sql
CREATE TABLE nt_event_digest (
  digest_ts     TIMESTAMPTZ NOT NULL,
  event_key     TEXT NOT NULL,
  odds_snapshot JSONB NOT NULL,
  stance        TEXT CHECK (stance IN ('risk_on','risk_off','neutral')),
  vol_expansion TEXT CHECK (vol_expansion IN ('low','medium','high')),
  confidence    DOUBLE PRECISION,
  model_provider TEXT,
  PRIMARY KEY (digest_ts, event_key)
);
```

Cross-asset by nature (one row per event, not per symbol) — deliberately
NOT jammed into `nt_macro_forecast`, which is per-symbol and refreshed
nightly; stale odds in a nightly table would defeat the point.

### 6.3 Flags (feature_flags.yaml, one per layer)

```yaml
event_odds_mode: shadow        # off: no polling at all. shadow: poll+persist+stamp. live: + kelly_event_multiplier
event_odds:
  baseline_poll_min: 60
  fast_poll_min: 15
  event_window_h: 24           # fast cadence + signal stamping window
  surprise_potential_floor: 0.15
  prob_move_trigger_pp: 5.0    # LLM digest trigger (6h window)
  tracked_series: [FOMC, CPI, NFP]
llm_event_digest_mode: shadow  # off | shadow (never live in v1)
```

### 6.4 Consumption ladder (shadow → evidence → live)

1. **Shadow stamps (v1)**: `hours_to_event` / `event_surprise_potential`
   on every qualifiable signal — placed in `_process_symbol` directly
   after the macro-filter block (orchestrator ~line 4806, same
   `signal_is_qualifiable` guard, same fail-open try/except).
2. **Evidence (v2)**: an `event_edge.py` diagnostic in the
   `macro_edge.py` mold — bucket resolved outcomes by
   (hours_to_event × surprise_potential): do signals taken within T−2 h
   of a ≥ 0.15-surprise-potential event underperform? That is the
   measurable form of "should we have stood aside before the Fed?"
3. **Live application (only if the evidence clears a bar)**:
   `kelly_event_multiplier` — penalty scaled by `surprise_potential`
   within the event window, same `[0.25, 1.5]` clamp, same
   neutral-is-a-no-op convention. AND/OR the vol side: `hours_to_event`
   + `surprise_potential` as known-future covariates in the
   `chronos2_cov` arm and an `is_event_day` dummy in Log-HAR — judged by
   the existing Phase 0 gate, nothing custom.

### 6.5 Cost

Kalshi/Polymarket market-data endpoints are free; polling as specced is
~24–96 GETs/day. The LLM digest fires ~4–12 times on an event day at
Clef-Flash/short-prompt cost — negligible against the nightly narrative
agent's ~44 symbol calls. No new paid dependency.

### 6.6 What this design deliberately refuses to do

- **No odds → `p_win`/EV injection.** `p_win` is a barrier-hit
  probability calibrated on outcomes; a market-implied *policy-outcome*
  probability is a different quantity. Blending them double-counts and
  violates the one-probability discipline this codebase just finished
  imposing (PBM). Odds enter as *filters/covariates/multipliers*, never
  as an EV term.
- **No faster narrative agent.** Frequency over a static corpus is noise
  (§1). The additive's freshness comes from the odds feed; the LLM
  interprets, it does not originate.
- **No live multiplier without the evidence table** (§2's lesson — the
  macro filter's own live flip was never earned; expect no better here).
- **No anniversary effect, no directional bet.** The system's whole
  event-day posture is: vol-risk flag + (eventually) a sizing penalty —
  direction remains the price layer's job.

---

## 7. Build order (if adopted)

| Step | What | Where | Gate to proceed |
|---|---|---|---|
| 1 | Log-HAR `is_event_day` arm + Chronos-2 `hours_to_event` future covariate | `vol_backtest/` | beats plain Log-HAR on QLIKE, DM/Holm — existing Phase 0 gate |
| 2 | `event_odds.py` poller + `nt_event_odds` + flags | new module + migration | polls, persists, dedupes; offline unit tests |
| 3 | `nt_signal_sim` capture stamps + orchestrator hook | orchestrator §5b1-adjacent | stamps accumulate in shadow |
| 4 | `nt_event_digest` + Clef digest on triggers | self_learning | vocabulary-validated rows appear |
| 5 | `event_edge.py` evidence diagnostic | macro_edge mold | n ≥ 20 cells per bucket (the 2026-09-14 bar) |
| 6 | `kelly_event_multiplier` live flip | feature flag | step 5 evidence + per-symbol reliability |

Step 1 requires **no new data dependency at all** (calendar features
only) and is the cheapest falsifiable test of whether event information
helps the vol forecast — do it first; it may make steps 2–6 optional.

---

## 8. Honest boundaries

- Zero-shot Chronos-2 currently **loses** to Log-HAR (8/10 cells) with
  0.19–0.42 interval coverage — event covariates ride on an arm that has
  not yet earned its place; the covariate test is as much a test of the
  arm as of the feature.
- Prediction-market coverage of *this* repo's symbol set (metals/FX
  majors) is indirect: Fed/CPI/NFP odds are USD-wide, not per-symbol.
  The layer is a risk filter, not a per-symbol alpha source.
- Kalshi/Polymarket API terms change; the poller must fail-open (§6.4's
  try/except pattern) and the flags must be able to turn the layer off
  without leaving stale stamps (`event_odds_mode: off` → no writes, reads
  return None → multiplier no-ops — the `macro_regime_mode` contract).
- Nothing here is a live-path change. This document proposes; the
  evidence tables dispose.

---

## 9. Addendum (2026-10-19, operator follow-up): should the odds layer feed into Clef-Flash?

Operator's follow-up: the LLM-narrative method is static files that are
inert — a fair description, and §1 already concluded the same (annual
source docs, shadow-only output, "nothing reads it for a live decision").
Calendar events and news modulate volatility. The specific question:
**does/should the odds layer be fed into Clef-Flash?** Answered against
the code at HEAD `b509865`, every claim file-traced.

### 9.1 What Clef-Flash is (verified)

`regime_platform/services/decision/clef_flash.py` + `cloudflare/worker.js`:

- `@cf/cloudflare/clef-flash` — a **9B structured-decision model** on
  Cloudflare Workers AI, reached via the deployed Worker `POST /decision`
  (worker.js:207). Contract (worker.js:230-235, verified live 2026-10-05):
  `{state, questions}` in; **softmax-normalised probabilities over a fixed
  option set** out. `choice` → `{choice, probabilities, confidence}`;
  `noul` → `P(true)`; `score` is rejected by the model.
- **The Worker does not whitelist `state` fields** — worker.js:226/240
  destructure and pass `{state, questions}` straight to the model. New
  state fields are a Python-side change only; **no Worker redeploy**.
- Four callers exist today — all shadow, all fail-open, never an EV
  source, never a gate (`clef_flash_mode: shadow`, feature_flags.yaml:1418):
  1. per-signal **direction stamp** (buy/sell/wait) — `_clef_flash_stamp`
     (orchestrator.py:2852, called :5794), stamped `p_clef_dir` /
     `p_clef_wait` / `p_clef_symbol` onto `nt_signal_sim` (:6024-6026);
  2. per-signal **archetype stamp** (R4) — `classify_archetype`
     (clef_flash.py:446, orchestrator :5820-5839);
  3. per-symbol **stance batch** (R5) — `clef_stance.py::classify_stance`,
     hourly `:48` job (orchestrator :1768, :3028-3094), upserts
     `nt_clef_stance` (mig. 207);
  4. **macro coherence** — `macro_filter.py::_clef_macro_coherence`
     (:154-225): feeds the *narrative text* (thesis/drivers/risks) plus the
     price label, asks agree/disagree, stamps `nt_macro_forecast.coherence`
     — never read back into a decision.

### 9.2 Does the odds layer feed Clef-Flash today?

**No.** Every state builder — `build_direction_state` (clef_flash.py:81),
`build_archetype_state` (:387), `build_stance_state` (clef_stance.py:62),
and the coherence state (macro_filter.py:185) — carries only slow reads:
`macro_label` (200d SMA), `trend_label`, `regime`, indicator ratios, or
the annual narrative text. On the eve of a Fed decision, **every
Clef-Flash call in the system is blind to the event**. The one existing
marriage of LLM and macro (the coherence stamp) joins the two slowest
layers in the pipeline — narrative text against a 200-day SMA.

### 9.3 Should it? Yes — Clef-Flash is the only LLM surface here where odds become measurable value instead of inert text

- **Structured by construction.** A choice question returns a probability
  distribution over a fixed vocabulary — storable, joinable, scoreable.
  The exact inversion of the narrative layer's free text: no parsing, no
  `extract_json`, no inert file.
- **Already shadow + fail-open + never-EV.** Odds enter with zero
  live-path risk; the §6.4 consumption ladder is untouched.
- **Already running at the right cadences.** 15-min light-sweep per-signal
  stamps + hourly `:48` stance batch — matching §6.1's tiered polling
  (hourly baseline, 15-min inside T−24 h). The "more frequent macro LLM
  additive" the operator asked for **already exists as infrastructure;
  it is blind, not absent**.
- **The 9B model reads numeric state.** An odds snapshot —
  `{outcomes: [{label, prob, prob_change_24h}], hours_to_event}` — is
  small structured context, exactly what a structured-decision model
  consumes.
- **`classify_choice` (clef_flash.py:286) was built for this.** It already
  replaced the fragile chat classification at
  `macro_forecast_agent.classify_symbol` and `kairos_pivot.label_event`.

### 9.4 Design — two shapes, both inside the existing surface

**Shape 1 — event fields on the existing state blocks (v1, rides the odds
store, no new flag):** `build_direction_state` / `build_archetype_state` /
`build_stance_state` gain optional `hours_to_event` and
`event_surprise_potential` (floats). The **None-drop convention already in
the builders** (clef_flash.py:117/420, clef_stance.py:88) means payloads
outside event windows stay byte-identical to today's — the ongoing
shadow discrimination measurements do not break — while inside T−24 h the
two fields appear, and the event-aware vs event-blind contrast *becomes
the measurement*. Call sites (orchestrator :5794/:5839/:3065) read the
latest odds state, fail-open to None. The fields appear iff
`event_odds_mode != off` — one flag per layer (§6.3 discipline).

**Shape 2 — the dedicated event digest (the odds-first LLM surface;
supersedes "run the narrative agent faster"):** one `decide()` call per
odds trigger with **two questions in the existing questions-object
contract**: `stance ∈ {risk_on, risk_off, neutral}` and
`vol_expansion ∈ {low, medium, high}` — both distributions returned in a
single sub-second call. `state` = the odds snapshot itself (event_key,
event_ts, hours_to_event, per-outcome probs + 24 h movement, consensus,
surprise_potential). Trigger = §6.1's events (T−24 h window entry, ≥5 pp
move in 6 h) — **event-triggered, not schedule-triggered**, ~4-12 calls
on an event day. Store → `nt_event_digest` (§6.2 schema stands).
**The code template is already in the file the operator pointed at:**
`_clef_macro_coherence` (macro_filter.py:154-225) — same module
placement, same fail-open, same `clef_flash_mode` gate, same
stamp-not-decide docstring. Copy it; swap the narrative-text state for
the odds state; swap the `noul` question for the two `choice` questions.

### 9.5 What happens to the narrative layer: demote, don't delete

The nightly agent (02:45 UTC) keeps producing structured labels and
pivot merges (`kairos_pivot.py:152` writes `pivot_*` onto the same table).
But its *text's* only programmatic consumer — the coherence stamp —
measures agreement between the two slowest layers in the pipeline. Once
Shape 2 ships, retire the narrative-coherence question (or repurpose it:
odds-stance vs price-macro is the live version of the same question). The
12-month docs remain an annual research corpus, not a decision input.
Nothing re-runs the narrative agent faster — §1's conclusion stands:
frequency over a static corpus is noise. The additive's freshness comes
from the odds feed; the LLM interprets, it does not originate.

### 9.6 Division of labor — where "calendar events & news modulate volatility" actually lands

| Job | Owner | Form |
|---|---|---|
| Numeric vol modulation | Log-HAR `is_event_day` dummy + `hours_to_event` known-future covariate in `chronos2_cov` (§3, build-order step 1) | regressor / covariate — judged by the Phase 0 gate |
| Categorical compression | Clef-Flash Shapes 1-2 | probability distributions — shadow-stamped, scoreable |
| Sizing penalty (post-evidence) | `kelly_event_multiplier` (§6.4) | multiplier, clamp [0.25, 1.5] |
| Never | — | odds or Clef-Flash output into `p_win`/EV (§6.6, one-probability discipline) |

The LLM is not the vol model. Event-day vol is a numeric forecast
problem with an existing harness; the LLM's comparative advantage is
compressing messy context into a storable categorical read.

### 9.7 How we'll know it worked

- **Direction stamp:** AUC of `p_clef_dir` vs outcomes, split by
  event-window × fields-present — accumulates automatically once Shape 1
  ships; no extra instrumentation.
- **Digest:** `nt_event_digest.vol_expansion` vs realised RV in the 24 h
  after `event_ts` (point-in-time clean — `nt_event_odds` is append-only);
  `stance` vs next-day macro-label movement. The odds themselves score
  against outcomes (did the 0.94 favourite win) — a macro layer that can
  be scored is a macro layer this codebase can trust (§5).
- **The live bar:** n ≥ 20 cells per bucket, per-symbol reliability — the
  2026-09-14 standard. Until then: stamps only.

### 9.8 Boundaries added by this addendum

- Whether a 9B decision model extracts signal from odds numbers — rather
  than ignoring them — is an empirical question the shadow stamps answer
  for the cost of a few hundred sub-second calls. Shape 2's honest value
  is compression and consistency, not hidden alpha.
- Both shapes depend on the odds store (build-order step 2); step 1
  (event dummies in HAR) remains the cheapest falsifiable test and needs
  no odds at all.
- Migration numbering, now concrete at HEAD: **212** `nt_event_odds`,
  **213** `nt_event_digest`, **214** `nt_signal_sim` event stamps
  (latest is 211 `nt_pbm_shadow`).
- None of this changes the 10/19 session itself — the layer is built for
  the next FOMC/CPI/NFP cycle.
