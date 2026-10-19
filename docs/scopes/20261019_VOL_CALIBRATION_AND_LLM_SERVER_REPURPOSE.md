# Vol-Forecast Calibration + llm_server.py Repurposing — Assessment & Design

> **Priority**: P2 proposal · **Status**: assessment complete, no code changed ·
> **Date**: 2026-10-19 (post-Fed session, HEAD `7d9667d`) ·
> **Roadmap refs**: vol-backtest Phase 0 (`d205d42`/`4fe5203`, runs
> `20261019_005157`/`010230`), `20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md`
> §2.1/§4.3 (calibration doctrine), PBM migs 210/211,
> `scripts/measure_pbm_calibration.py`, `20261019_MACRO_EVENT_ODDS_LAYER_ASSESSMENT.md`
> (§5-§9), README "Classify & Harmonize" bullet (`7d9667d`)

---

## 0. The operator's questions

1. **How to calibrate overconfidence?** — noted correctly as a shared defect
   across BOTH probability domains in the system: signal generation
   (`p_win`) and, newly measured, volatility (Phase 0's coverage80
   0.19–0.44 for Chronos-2 quantile spreads).
2. **How to repurpose `llm_server.py`** to be more impactful than its
   current purpose of LLM narratives?

Both answered against the code at HEAD `7d9667d`, every claim file-traced.

---

## 1. Q1 — Calibrating overconfidence

### 1.1 What "overconfident" means, precisely

The repo's own doctrine (signal-quality thesis §2.1) already states it:

> **Discrimination** — can the score *rank* opportunities? **Calibration**
> — are the stated probabilities *true*? "You need discrimination to have
> edge at all; you need calibration to size correctly. A gate can only
> exploit discrimination; Kelly can only exploit calibration."

Overconfidence is a CALIBRATION defect, not a discrimination defect — and
Phase 0 measured exactly that split for Chronos-2: discrimination is real
(dir_acc 0.57–0.83 vs persistence nulls 0.28–0.52), calibration is broken
(the "80 %" intervals contained reality only 19–44 % of the time — the
spread is 2–4× too narrow). The point forecasts were sometimes competitive
(QLLIKE); the *uncertainty around them* is what cannot be trusted. That is
the specific thing "recalibrate" must fix, because the Kelly vol clamp
and any brick-sizing use consume the SPREAD, not the center.

### 1.2 The p_win side is already handled — and is the template

For signal generation, the machinery exists and follows one discipline
(stamp → backfill → score → refit, per-cell, never in-sample):

- **PBM cell posteriors** (mig 210): decay-weighted Dirichlet on
  (W, L, E) per cell key — empirical probability WITH credible intervals
  and `n_eff`; sparsity shrinks toward the analytic null instead of
  overstating.
- **PBM prediction ledger** (mig 211): point-in-time stamp, outcome
  backfilled offline, log-score replay — era-clean by construction.
- **`measure_pbm_calibration.py`**: reliability curves, Brier/BSS, ECE/MCE,
  bootstrap — the measurement suite.
- **Thesis §4.3**: the recalibration layer is a *monotone map (isotonic,
  or Platt if smooth)* fitted on rolling out-of-fold windows, frozen
  before the next evaluation slice — never fit and scored on the same data.

The remaining honest gap on p_win is cell sparsity, and the Dirichlet
shrinkage IS the remedy. Do not bolt an LLM onto this — the lesson for
the vol side is the PATTERN, not a new mechanism.

### 1.3 The vol recipe (Phase 0.5) — Mincer-Zarnowitz + spread inflation

Continuous target, same doctrine. Per symbol × horizon (never pooled —
the per-symbol discipline the harness already enforces):

**Step 1 — MZ on the center.** Regress realized on forecast in log space:

```
log(RV_realized) = a + b · log(F) + ε        (OLS on rolling train window)
F' = exp(a) · F^b
```

Diagnostics fall out for free: `b < 1` → the model over-reacts to recent
input; `b > 1` → under-reacts; intercept absorbs level bias. Mincer &
Zarnowitz (1969) is the canonical reference; in vol-forecast evaluation
this is standard practice alongside QLIKE (Patton & Sheppard — QLIKE is
robust to the RV proxy's noise, which is why it stays the loss of record).

**Step 2 — spread inflation (the actual overconfidence fix).** With the
center corrected, standardize the errors against the model's OWN claimed
spread: `e = (log RV − log F') / σ_f`, where `σ_f` is the model's
log-spread half-width at the target level. If calibrated, `|e|` is
~N(0,1)-scaled and the 80 % interval is `F'·exp(±1.2816·σ_f)`.
Empirically it is 2–4× too big, so:

```
k = quantile_0.80(|e|) / 1.2816              (one number per cell)
calibrated 80% interval = F' · exp(±1.2816 · k · σ_f)
```

One parameter per symbol×horizon fixes the coverage defect directly.
~700 rolling cutoffs per cell already exist from run `005157` — enough
for a stable global `k` per cell; per-regime `k` needs more history
(say so, don't fake it).

**Upgrade paths, in order of need:**
- *Skewed errors* → per-quantile empirical mapping (quantile-to-quantile
  on the pinball grid, or isotonic per quantile level) — fixes asymmetry,
  not just width. `metrics.py` already computes pinball loss per level.
- *Distribution-free belt-and-suspenders* → split conformal on `|e|`:
  finite-sample coverage guarantee with zero assumptions about the error
  distribution, ~20 lines on top of the same residuals.
- *Nonstationarity* → decay-weight the residual window (PBM's
  `2^(−age/τ)` convention) rather than hard-cutting it.

**Step 3 — lockbox validation.** Fit (a, b, k) on train cutoffs, apply to
LATER test cutoffs only. Acceptance (already the thesis band):
`coverage80 ∈ [0.80, 0.95]` AND QLIKE not degraded vs the raw arm. Never
fit and score the same window — the thesis's C1/C6 lesson, restated.

**Step 4 — versioned storage.** The PBM pattern applied to a continuous
target — a small upsert-keyed table:

```sql
-- Migration 2xx: nt_vol_recalib
CREATE TABLE nt_vol_recalib (
  symbol        TEXT NOT NULL,
  horizon_bars  INT  NOT NULL,
  arm           TEXT NOT NULL,          -- 'chronos2' | 'chronos2_cov' | 'har' | ...
  mz_a          DOUBLE PRECISION,       -- center intercept
  mz_b          DOUBLE PRECISION,       -- center slope
  spread_k      DOUBLE PRECISION,       -- inflation factor
  n_fit         INT,
  fit_start     TIMESTAMPTZ,
  fit_end       TIMESTAMPTZ,
  cov_insample  DOUBLE PRECISION,
  cov_oos       DOUBLE PRECISION,       -- the lockbox number that matters
  qlike_raw     DOUBLE PRECISION,
  qlike_cal     DOUBLE PRECISION,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, horizon_bars, arm)
);
```

Refit cadence: weekly (vol regimes drift) — cheaper than PBM's nightly
because the residual stream is batch, not per-signal. Phase 0.5 can start
as a JSON artifact in the run directory (offline, no Supabase dependency)
and graduate to the table when an arm earns it.

**Step 5 — the consumption gate.** ONLY the calibrated spread ever feeds
the Kelly vol clamp / brick sizing. Raw model quantiles never touch a
sizing decision. Same ladder as everything else in this codebase:
shadow stamps → evidence → live.

### 1.4 Why zero-shot quantiles are overconfident (mechanism)

Generic pretraining corpus ≠ finance kurtosis — fat tails are precisely
what a heterogeneous-corpus model under-spreads; the 120M CPU-tier model's
quantile heads were tuned on mixed frequencies/domains, not on hourly
metal/FX RV. This is why "M-Z recalibration mandatory" was already the
first-run verdict, and why recalibration is expected to help the SPREAD a
lot and the CENTER a little.

### 1.5 Calibrate the winner too

HAR's coverage80 was 0.67–0.82 — inside-or-near the band but still
slightly narrow at short horizons. The same (a, b, k) treatment is
near-free for HAR (`k` close to 1, likely) and keeps the reference bar
honest when arms are compared post-calibration.

### 1.6 What recalibration deliberately cannot do

Fix discrimination. If the calibrated arm still loses to calibrated HAR
on QLIKE/DM, the arm does not ship — recalibration repairs stated
uncertainty, it does not conjure information the forecast lacks. (This is
the thesis's trichotomy doing its job.)

### 1.7 Dynamic-tier structure — does it support recalibration? (addendum, file-traced)

**Verdict: yes — the substrate is right — with three named caveats and
one measurement from the Phase 0.5 build itself.**

What the four dynamic tiers get RIGHT for recalibration
(`dynamic_tier_optimizer.py`, `param_store.py`, all file-traced):

1. **Every tier is fitted on REALIZED outcomes, never forecasts.**
   dynamic_atr walks real price paths; dynamic_deadline reads
   reachability percentiles; regime_edge counts resolved `nt_signal_sim`
   outcomes; geometry_incubation mirrors the reconcile job. No tier
   parameter anywhere consumes a Chronos/HAR forecast, so the measured
   forecast-spread overconfidence CANNOT have contaminated the tier
   substrate. Recalibration adds the missing (forecast → realized) map on
   the same realized-outcome discipline — the pipeline ordering is
   correct: tiers condition on state, forecasts get calibrated before
   any consumption.
2. **The tier store is calibration-map-ready.** A `param_store` row is
   `(symbol, tier_type, tier_key, tier_value JSON, n, computed_at)` with
   local-SQLite authority and Supabase as audit mirror — a future
   `vol_recalib` tier (`tier_key = "<H>:<arm>"`) rides the existing
   write/read/mirror machinery with zero schema change. JSON-artifact-
   first (§1.3 step 4) then table-when-earned is exactly this shape.
3. **The fail-open ladders match.** cell → pooled → global → identity in
   `recalib.py` is the same shape as per-symbol → pooled-per-class →
   brick (ATR) and (symbol, vol_tier) → vol_tier-global → conservative
   defaults (deadline). One pattern, no new machinery.

The caveats (what to NOT do yet):

- **(a) vol_tier keys are state estimates, not measurements.** Every
  `vol_tier` in the system is `_vol_tier(regime_label)` — an HMM argmax
  parse (`bracket_reachability.py:134`) of the debounced combined label.
  HMM argmax labels are themselves uncalibrated (soft probabilities
  collapsed to hard labels; nobody has measured P(true state | label)).
  Recalib cells therefore stay keyed `(symbol, horizon, arm)` — NOT
  vol_tier — until the labels earn their own reliability table. Per-regime
  `k` remains the §4 boundary it already was.
- **(b) The tier multipliers are frozen hand priors.** `_VOL_M`
  (`variable_kelly.py`, 1.05/1.00/0.98/0.92) and `VOL_RISK_SCALE`
  (`regime_engine.py`) are hand-set, and the optimizer refreshes tier
  DATA but never these coefficients — the tiers are dynamic in their
  inputs, static in their vol response. That is a deliberate
  measurement-first stance, not a defect, but it means a calibrated
  spread would feed a sizing gate whose vol-tier response is itself
  unmeasured. The evidence-backing path already exists in-repo
  (`regime_edge` computes per-regime win rates; vol_tier can be added as
  a conditioning key the same way) — Phase 2, after Phase 0.5's maps
  prove out.
- **(c) The overconfidence locus is partly the harness's own interval
  construction — and `k` is deliberately agnostic to it.** The Chronos
  arms' claimed interval is `half = Z80·rv/√(2H)` (`model_arms.py` /
  `arms.py`) — a CLT sampling-error band, NOT the model's own quantile
  spread (q05/q95 are fetched and discarded). EWMA, using the IDENTICAL
  construction, scores coverage 0.34 pooled (run 005157) — the same
  defect as Chronos with a different center. `k` standardizes against
  whatever σ_f the arm CLAIMS, so it repairs an understated CLT band and
  a mis-spread empirical band (HAR) with the same machinery; measured
  (Phase 0.5 run `20261019_recalib_phase05`): chronos k ≈ 2.2–2.8,
  HAR k ≈ 0.97–1.21 (the §1.5 prediction), EWMA k up to 10 at H=72.

One measurement from the Phase 0.5 lockbox that sharpens §1.3 step 3:
the thesis band [0.80, 0.95] CANNOT be applied as a point-estimate gate
at lockbox n — a perfectly calibrated cell targets 0.80 nominal, so its
point estimate lands below 0.80 about half the time by sampling noise
alone (proven on synthetic ground truth before touching real data).
`recalib.py` applies the band as a ONE-SIDED Wilson test (reject only
when the lockbox CI rules the band out) and reports the CI next to every
verdict; catastrophes (0.19–0.44) still reject hard.

---

## 2. Q2 — Repurposing `llm_server.py`

### 2.1 What it is today (file-traced)

`llm_server.py` (:8004) — FastAPI hosting Spark-X2.5-1.7B-INT8 behind an
OpenAI-compatible `/v1/chat/completions`, with auth, health, offline HF
cache resolution, transformers-5 compat shims. The server itself is
**generic infrastructure**; the *narratives* come from its callers'
prompts. Its only consumers (via `llm_client`'s ladder: local →
OpenRouter → NVIDIA NIM):

| Consumer | Current use | Status |
|---|---|---|
| `macro_forecast_agent.classify_symbol` (:239) | nightly narrative classification over static annual docs | shadow, inert (odds assessment §1) |
| `kairos_pivot.label_event` (:230-237) | free-text chat fallback when `clef_flash_mode != live` | active fallback |
| `self_learning/triage.py` (:43) | post-mortem summaries | research |

All three go free-text → `extract_json` (first `{` to last `}`) →
validate. The fragility Clef-Flash's `classify_choice` was built to
replace.

### 2.2 The principle (already established, restated)

Every LLM output must be: **structured** (fixed vocabulary), **point-in-time
stored**, **scoreable against an outcome**, and **never an EV source**.
Freshness comes from numeric feeds (odds, vol, realized outcomes) — not
from re-reading static documents. The README's new "Classify & Harmonize
— volatility and macro-economic market information" bullet (`7d9667d`)
names exactly this role.

### 2.3 Three repurposed jobs (ranked)

**A. Structured-choice fallback tier (do first — pure consolidation).**
Generalize `kairos_pivot`'s existing CF-live-first pattern into
`llm_client.classify_choice_local()` mirroring
`clef_flash.classify_choice`'s exact contract — `{instructions, options,
state} → {label: probability}` — via a choice-style prompt, temperature 0,
JSON response, vocabulary-checked and renormalized. The local server
becomes the **second tier of ONE classification ladder**:

```
Clef-Flash (sub-second, structured)  →  llm_server :8004 (structured choice)
→  OpenRouter/NIM (structured choice)  →  None (fail-open)
```

All three existing consumers migrate to it; `extract_json` prose-parsing
retires for classification. The server's availability/offline/no-API-terms
edge is exactly what a fallback tier wants. Offline unit tests with a
stub server (the `test_llm_client.py` pattern already exists).

**B. Event-odds digest engine (the odds assessment §6.2/§9 Shape 2
executor).** Consume `nt_event_odds` snapshots + movement + calendar
context; emit `{stance: risk_on|risk_off|neutral, vol_expansion:
low|medium|high, confidence}` rows into `nt_event_digest` with
`model_provider` stamped. Clef-Flash stays the fast path; the local model
is the deeper/fallback read on the SAME fixed question — two providers
answering one vocab-checked question is a free A/B, and their
disagreement is itself a measurable "ambiguous context" flag.

**C. Narrative → claims issuer (the fix for "static files that are
inert").** Keep the nightly agent and its annual corpus, but change the
OUTPUT from prose to scorable claims:

```sql
-- Migration 2xx: nt_macro_claims — PBM ledger pattern for narrative content
CREATE TABLE nt_macro_claims (
  claim_ts     TIMESTAMPTZ NOT NULL,
  symbol       TEXT,                  -- NULL = cross-asset claim
  driver       TEXT NOT NULL,         -- 'fed_path', 'usd_trend', ...
  direction    TEXT NOT NULL CHECK (direction IN ('up','down','flat')),
  probability  DOUBLE PRECISION,      -- the LLM's stated confidence
  horizon_days INT NOT NULL,
  trigger      TEXT,                  -- falsifiable resolution condition
  model_provider TEXT,
  outcome      TEXT CHECK (outcome IN ('hit','miss','open')),  -- backfilled
  resolved_at  TIMESTAMPTZ,
  PRIMARY KEY (claim_ts, driver, symbol)
);
```

A deterministic backfill job resolves each claim against realized data
(did the move happen by the horizon?). The 12-month view finally gets
MEASURED instead of stored — the narrative becomes a forecast ledger
with a reliability curve, exactly like `p_clef_dir` and PBM. This is the
honest version of "make the narrative layer useful": not fresher text,
scoreable claims.

### 2.4 What to kill / never do

- **No more frequent re-reads of static annual docs** — frequency over a
  static corpus is noise (odds assessment §1, settled).
- **No un-vocab-checked free text on any decision-adjacent path** —
  `extract_json` prose parsing survives only in triage research summaries.
- **Never LLM probability into `p_win`/EV** — one-probability discipline
  (PBM owns the probability object; odds assessment §6.6).
- **Know the model's weight class.** A 1.7B INT8's edge is
  availability/cost/offline, not intelligence. Use it where STRUCTURE
  does the work: classification over fixed vocabularies with numeric
  state — not open-ended forecasting. If it can't hold the vocabulary,
  the A/B stamps will say so (`model_provider` makes that measurable).

### 2.5 Division of labor (one table, whole answer)

| Job | Owner |
|---|---|
| Probability object (`p_win`) | PBM cell posteriors (mig 210/211) — never an LLM |
| Vol forecast + uncertainty | HAR/Chronos arms + MZ/spread recalibration (§1) — never an LLM |
| Categorical compression (stance, vol_expansion, archetype, event labels) | Clef-Flash ladder, local server as tier 2 |
| Narrative corpus → measurable claims | Nightly agent emitting `nt_macro_claims` rows |
| Direction/size decisions | Deterministic engine — unchanged |

---

## 3. Build order

| Step | What | Gate |
|---|---|---|
| 1 | `vol_backtest/recalib.py` (MZ a/b + spread k) + lockbox coverage in harness; run Phase 0.5 | coverage80 ∈ [0.80, 0.95] OOS AND QLIKE not degraded |
| 2 | `llm_client.classify_choice_local()` + migrate 3 consumers | offline unit tests, vocab-validated rows |
| 3 | `event_odds.py` poller + `nt_event_odds` (mig 212) + digest dual-provider + `nt_event_digest` (mig 213) | polls, persists, dedupes; digest rows with model_provider |
| 4 | Claims issuer + `nt_macro_claims` + deterministic backfill scorer | claims resolve; reliability curve renders |
| 5 | `nt_vol_recalib` table + weekly refit job (only if an arm earned it in step 1) | `cov_oos` in band across a full rolling refit cycle |

Step 1 is offline, needs no API, no Supabase, and directly addresses the
measured defect. Steps 2-4 turn every LLM watt in the system into
structured, scoreable rows.

---

## 4. Honest boundaries

- MZ + k fixes stated uncertainty; it cannot rescue an arm that still
  loses on QLIKE after calibration. HAR remains the bar — and HAR itself
  gets recalibrated so the comparison is calibrated-vs-calibrated.
- Per-regime (not just per-symbol×horizon) `k` needs more history than
  one yfinance pull; broker-quality data (already the pending decision-run
  caveat) changes both the arms' and the recalibration's inputs.
- The local 1.7B may fail structured-choice quality — that is a measurable
  outcome, not a guess; `model_provider` columns exist precisely so the
  ladder can be re-weighted on evidence.
- Claims scoring depends on clean resolution rules (`trigger` text must
  be mechanically checkable — the issuer prompt must be constrained to
  price-level/event-print conditions, else the backfill scorer inherits
  narrative ambiguity).
- Nothing here changes any live path. This document proposes; the
  evidence tables dispose.
