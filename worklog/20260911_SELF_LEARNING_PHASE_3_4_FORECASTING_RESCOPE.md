# Self-Learning Phase 3+4 — Rescoped Around Forward/Lookback Forecasting (TimesFM + JinaAI)

> **Status: scope only, nothing built.** Per "scope in worklog, get
> honest review, build only on explicit instruction." This document
> moves Phase 3 ("LLM-assisted triage") and Phase 4 ("explicitly not
> recommended") out of
> `worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md`
> §2 (left there verbatim, now annotated as moved) and widens Phase 3's
> design to combine a **lookback** axis (Phase 1's semantic index +
> Phase 2's detector findings, both already built) with a **forward**
> axis (TimesFM 2.5/3.0 quantitative forecasting + the 12-month
> commodity macro model) and reopens the **JinaAI** retrieval-embedding
> question that was explicitly closed in the opposite direction on
> 2026-09-11 (see §2 below). Phase 1/Phase 2 (`RM-36`, `RM-37`) are
> untouched by this document.

| # | Priority | Task | Status | Date | Roadmap refs |
|---|---|---|---|---|---|
| §1 | P2 | Restate Phase 3/4 as originally scoped, for reference | n/a | 2026-09-11 | RM-36 |
| §2 | P2 | Widen Phase 3: lookback + forward forecasting, reconsider JinaAI | blocked | 2026-09-11 | RM-50 |
| §3 | P2 | Phase 4 — carried forward unchanged | n/a | 2026-09-11 | n/a |
| §4 | P2 | Open questions needing a decision before anything is built | n/a | 2026-09-11 | n/a |

---

## 1. Phase 3/4 as originally scoped (reference only)

Originally scoped in `worklog/20260910_..._SCOPE.md` §2, alongside
Phase 1 (pgvector-style local index — **built**, `RM-36`) and Phase 2
(statistical detectors — **built**, `RM-37`):

- **Phase 3 — LLM-assisted triage, grounded in Phase 1's index, reading
  Phase 2's detector output** (medium risk, output is a written
  report/proposal only, nothing auto-applied). Given a detector firing,
  retrieve related code + worklog context via semantic search and
  generate a written root-cause narrative + a *proposed*, scoped fix
  description. Output lands as a new row in a table (e.g.
  `nt_self_learning_reports`) or a scoped worklog draft — a human still
  reads it and decides whether to approve a build. **No write access to
  any live parameter at this phase.**
- **Phase 4 — explicitly NOT recommended soon.** Any LLM or auto-tuner
  component with *write* access to a live trading/sizing parameter.
  Should only be considered, if ever, after Phases 1-3 have run long
  enough on real data to build trust — and even then through the same
  shadow-mode → real-data comparison → explicit human promotion
  pipeline every other mechanism in this codebase has gone through.

`RM-37`'s own notes already flagged Phase 3 as "the next real milestone
if desired, not yet scoped in detail" — this document is that detailed
scoping, widened per the current request rather than left narrow.

---

## 2. What's widening, and why

### 2.1 The narrow version was retrieval-only over past events

As originally scoped, Phase 3 only looked *backward*: a detector fires
on something that already happened (a regime/price mismatch, a dead
mechanism, a redundant computation), and the LLM's only job is to
explain it using code/worklog history. That's useful, but it can never
anticipate anything — it's diagnosis, not forecasting. The request
this document responds to is to widen that: fold in an actual
**forward-looking numerical forecast** (TimesFM) and a **long-horizon
structured macro narrative** (the 12-month commodity model), and use
both together, alongside the existing lookback machinery, to (a)
produce richer triage narratives and (b) let the written proposal
suggest specific parameter values to tune — not just describe a bug.

### 2.2 Forward axis, source 1 — TimesFM 2.5 (live) / 3.0 (license-blocked)

Two already-scoped, separate pieces of work become inputs to this
Phase 3 rather than parallel unrelated tracks:

- **TimesFM 2.5** is already live in the signal-qualification path
  (`_fetch_timesfm_cached`, univariate, one call per symbol,
  `timesfm_k_adaptive.py` for the sigmoid-k mapping). Its per-symbol
  point/quantile forecast is a forward signal Phase 3 can read
  directly — it already exists, nothing new to build to get access to
  it.
- **TimesFM 3.0** multivariate covariate forecasting is separately
  scoped and marked **"Approved for full implementation"** in
  `worklog/20260915_TIMESFM3_IMPLEMENTATION_SCOPE.md` — full variate
  attention across correlated commodity clusters (energy/agricultural/
  metals), conditioned on the 12-month forecast ranges as static
  covariates, shadow-mode-gated (`timesfm3_mode`), blocked on the
  non-commercial weights license per
  `[[timesfm_3_license_blocker]]`. **That document's implementation
  sequence is independent of this one** — it stands on its own as a
  signal-qualification improvement regardless of whether Phase 3 uses
  its output. This document treats it as an available *input* once
  built: a self-learning triage or tuning proposal grounded in "TimesFM
  says HO and CL diverged from their historical correlation this week"
  is a materially different (and stronger) kind of finding than
  anything Phase 2's detectors can produce today.

### 2.3 Forward axis, source 2 — the 12-month commodity macro model

`docs/LLM/commodity_12m_forecast_model_2027.md` (13 symbols, Q4 2026 →
Q3 2027, structured quarterly ranges + named risk drivers) and its
companion `docs/LLM/commodity_macro_context_scope.md` (assessment-only,
no code) already scope a path to get this content into the live
pipeline as `nt_macro_forecast`, read by `macro_filter.py` and
`context_alignment.py` in shadow mode. That scope is **not itself part
of this rescope** — it's a separate, already-designed piece of work
(own implementation sequence, own open questions, e.g. TradingView
symbol availability for FCPO/VGT/JX/SI). What *is* relevant here: once
`nt_macro_forecast` exists, it's a second forward-looking, long-horizon
input Phase 3 can ground a narrative in — "the detector flagged HO's
regime/price mismatch, and the 12-month model independently predicts
record diesel cracks through Q4" is a genuinely richer triage output
than code/worklog retrieval alone can produce.

### 2.4 Retrieval axis — reopening the JinaAI question

Phase 1's original build (`RM-36`) tried
`jinaai/jina-embeddings-v2-base-code` first, specifically for its
code+docstring domain tuning, then **swapped it out same-day** after
the first real Studio deploy failed: that model's custom
`modeling_bert.py` (loaded via HF's trust-remote-code path) imports
`find_pruneable_heads_and_indices` from `transformers.pytorch_utils`,
which doesn't exist under Studio's `transformers==5.12.1`. The fix was
`sentence-transformers/all-MiniLM-L6-v2` run locally — a real,
already-resolved incompatibility, not a preference.

Bringing JinaAI back in for this rescope means being explicit about
what's actually different this time: the original failure was specific
to loading Jina's *custom code* locally under a fixed transformers
version. A **hosted Jina API call** (their embeddings endpoint,
paid/rate-limited) sidesteps that exact failure mode entirely — no
local model code, no transformers-version coupling — but reopens the
tradeoff this repo's own [[config_centralization_preference]] and
`RM-36`'s notes already closed once, explicitly, in the *opposite*
direction: "we already do this for timesfm, 800mb" (i.e., local/
self-hosted was the user's deliberate call, not an oversight to
correct). Widening Phase 3 to use Jina again — whether for embedding
the forecast narrative text, the commodity model's prose, or news-style
context beyond what's in this repo's own worklogs — is **not a decided
change**; it's flagged here as the concrete tradeoff it actually is
(§4.1).

### 2.5 Rescoped Phase 3 definition

LLM-assisted triage **and tuning-proposal**, grounded in four sources
instead of one:

1. Phase 1's semantic index (code, worklogs, `nt_roadmap.notes`) — lookback, built.
2. Phase 2's detector findings (`nt_self_learning_findings`) — lookback, built.
3. TimesFM 2.5 (live) / 3.0 (blocked) forecasts — forward, quantitative, per-symbol or per-cluster.
4. The 12-month commodity macro model (`nt_macro_forecast`, once built per its own scope) — forward, long-horizon, narrative.

Output is still, unchanged from the original scope, **a written
report/proposal only** — landing as a new row in
`nt_self_learning_reports` or a scoped worklog draft, read and approved
by a human before anything is built. The widening to "tune params"
means the written proposal is now allowed to name a *specific*
parameter and a *specific* proposed value (e.g. "raise
`timesfm_k_floor` from 8 to 10" rather than only "the k-floor may be
too conservative") — this is a change in the *specificity* of the
written output, not a change in write authority. It still requires the
same explicit human read-and-approve step as every other mechanism in
this codebase, and the same Tier-D-only boundary
`auto_tuner.py`/Layer 1 already enforces should be the starting
assumption for what's in-bounds to *propose*, pending §4.3.

---

## 3. Phase 4 — carried forward unchanged

Nothing about widening Phase 3's inputs changes the case against Phase
4. Restated verbatim in spirit: any LLM or auto-tuner component with
*write* access to a live trading/sizing parameter is **explicitly not
recommended soon**, regardless of how good the forecasting/retrieval
inputs get. If ever revisited, it goes through the same shadow-mode →
real-data comparison → explicit human promotion pipeline as every other
mechanism here — a stronger forecast is not an exception.

---

## 4. Open questions needing a decision before any of this is built

1. **JinaAI: local (again) vs hosted API vs stay on MiniLM** — the
   local-custom-code failure that forced the 2026-09-11 swap is
   avoidable via a hosted API call, but that reopens the
   external-API-cost/data-leaves-boundary tradeoff already closed once
   in favor of local-only. No default assumed.
2. **Is TimesFM-3 a hard dependency, or does 2.5 + the static
   12-month-model text suffice to start?** `20260915_TIMESFM3_..._SCOPE.md`
   is separately approved but blocked on the non-commercial license;
   this Phase 3 rescope doesn't need to wait for it — TimesFM 2.5's
   existing per-symbol forecast plus the commodity model's prose is
   enough to prototype the "forward-grounded narrative" idea. Confirm
   whether to proceed on that basis or wait for TimesFM-3.
3. **Scope of "tune params" in the written proposal** — Tier D
   calibration/enforcement constants only (matching `auto_tuner.py`'s
   existing bounded scope), or wider (e.g. gate thresholds, k-adaptive
   floor/ceiling, `min_rr`)? Wider is a bigger trust ask given nothing
   here has write access yet regardless.
4. **Relationship to `nt_macro_forecast`** (from
   `commodity_macro_context_scope.md`) — is building that table a
   prerequisite for Phase 3's forward axis, or should Phase 3 start
   with just TimesFM 2.5 and treat the commodity model as a manually
   pasted-in reference for now, deferring `nt_macro_forecast` to its
   own separate approval?

Nothing above is built. Waiting on direction on these four questions
before any code is written.
