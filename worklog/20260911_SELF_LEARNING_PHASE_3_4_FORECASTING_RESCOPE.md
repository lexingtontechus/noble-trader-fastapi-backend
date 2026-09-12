# Self-Learning Phase 3 — MVP LLM-Assisted Triage

> **Status: scope revised 2026-09-11, then built same day.** Per "scope
> in worklog, get honest review, build only on explicit instruction."
> This document moves Phase 3 ("LLM-assisted triage") and Phase 4
> ("explicitly not recommended") out of
> `worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md`
> §2 (left there verbatim, now annotated as moved). Phase 1/Phase 2
> (`RM-36`, `RM-37`) are untouched by this document.
>
> **Revision history**: the first version of this file (same day)
> widened Phase 3 with a TimesFM 2.5/3.0 forward-forecasting axis and
> reconsidered JinaAI for retrieval. **JinaAI was a mistake — the user
> flagged it and asked for all references removed**, and on reflection
> the forecasting axis was scope creep for a first cut. This version
> replaces that draft entirely with an **MVP**: minimum-effort LLM
> triage over what Phase 1/2 already produce, no new retrieval infra,
> no forecasting integration. The forecasting idea isn't wrong, just
> not v1 — noted at the end as a possible follow-up, not built.

| # | Priority | Task | Status | Date | Roadmap refs |
|---|---|---|---|---|---|
| §1 | P2 | Restate Phase 3/4 as originally scoped, for reference | n/a | 2026-09-11 | RM-36 |
| §2 | P2 | MVP design: LLM triage + bounded param-tuning proposal | resolved — built | 2026-09-11 | RM-50 |
| §3 | P2 | Phase 4 — carried forward unchanged | n/a | 2026-09-11 | n/a |
| §4 | P3 | Deferred: forecasting-grounded triage (not in MVP) | partially built, see §6 | 2026-09-11 | n/a |
| §6 | P2 | Provider correction (OpenRouter/NVIDIA NIM, not Anthropic) + macro-forecast agent | resolved — built | 2026-09-11 | RM-50 |

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

`RM-37`'s own notes flagged Phase 3 as "the next real milestone if
desired, not yet scoped in detail" — this document is that scoping,
kept deliberately narrow.

---

## 2. MVP design: LLM triage + bounded param-tuning proposal

### 2.1 Goal

Let the user actually run and evaluate LLM-assisted triage — "so we
can test/backtest, possibly tune a limited set of params" — with the
least new infrastructure possible. Concretely: one nightly job reads
whatever Phase 2 hasn't triaged yet, asks an LLM for a short narrative
and (optionally) a bounded parameter-change suggestion, and writes both
to a new table for a human to read. Nothing new to build for retrieval
(reuses Phase 1's existing `semantic_search`), nothing new for
detection (reuses Phase 2's existing `nt_self_learning_findings`), and
the tunable surface is not a new decision — it's the exact
`TIER_D_PILOT_PARAMS` allowlist `auto_tuner.py` (Layer 1) already uses
and enforces via `tuning_registry.PARAM_BOUNDS`.

### 2.2 What's genuinely new

1. **`nt_self_learning_findings.triaged_at`** (new nullable column) —
   marks a finding as already sent through triage, so the nightly job
   doesn't reprocess the same rows forever.
2. **`nt_self_learning_reports`** (new table) — one row per triage
   result: `finding_id` (FK), `narrative`, `proposed_param` (nullable,
   must be in `TIER_D_PILOT_PARAMS` or it's discarded before storage),
   `proposed_value` (nullable, clamped into `PARAM_BOUNDS` before
   storage), `current_value` (nullable snapshot of the param's live
   value at triage time, for human comparison), `reviewed` (bool,
   default false, mirrors `nt_self_learning_findings`).
3. **`regime_platform/services/self_learning/triage.py`** — the actual
   LLM call. One finding in, one report dict out (or `None` on any
   failure — best-effort, matches `run_detectors.py`'s per-item
   pattern). Steps per finding:
   - `semantic_search(finding["summary"], k=3)` — reuse Phase 1
     unchanged, small `k` to keep the prompt short/cheap.
   - Build a prompt: the finding's `summary`/`details`, the 3 retrieved
     chunks, and the full `TIER_D_PILOT_PARAMS` list with each
     parameter's current value (`tuning_registry.read_param_async`)
     and bounds (`PARAM_BOUNDS`). System prompt states plainly: this is
     diagnostic only, nothing is applied automatically, propose a
     parameter change **only** from the given list or leave it null.
   - ~~Raw `httpx` POST to the Anthropic Messages API~~ — **corrected
     2026-09-11, see §6**: this repo does not use Anthropic. LLM calls
     go through a shared `llm_client.py` (OpenRouter primary, NVIDIA
     NIM fallback — the exact pattern already live in
     `nobletradingapp`'s `eod-report-generate` edge function), asking
     for a JSON-only response:
     `{"narrative": str, "proposed_param": str|null, "proposed_value": number|null}`.
   - Parse the JSON leniently (first `{` to last `}`); on any parse
     failure, missing key, or HTTP error, log and return `None` for
     that finding rather than raising.
   - **Validate before storing, not just before applying**: if
     `proposed_param` isn't in `TIER_D_PILOT_PARAMS`, drop it (log a
     warning — the model proposed something out of scope) and null out
     `proposed_value` too. If it is in scope, clamp `proposed_value`
     into `PARAM_BOUNDS[proposed_param]` via `tuning_registry.clamp()`
     before storing — belt-and-suspenders even though nothing reads
     this table to make a live decision.
4. **`supabase_client.py`** additions — `fetch_untriaged_findings()`,
   `mark_finding_triaged()`, `insert_self_learning_report()`, following
   the exact `_insert_row`/`fetch_all_rows`/PATCH-by-id patterns
   already in that file (e.g. `update_tuning_param_cycle_result`).
5. **New job**: `self-learning-triage-nightly`, 02:30 UTC (after the
   02:00 UTC detectors job, so today's findings are already written),
   gated by `self_learning_triage_enabled` (default `true`, mirrors
   `self_learning_detectors_enabled` — same risk class: read-only
   diagnostic output, no live effect regardless of on/off).

### 2.3 What's explicitly NOT in the MVP

- No TimesFM forecast grounding, no commodity macro model input — see
  §4.
- No new embedding model/provider — Phase 1's existing local
  `sentence-transformers/all-MiniLM-L6-v2` index is reused as-is.
- No auto-apply path of any kind — `proposed_param`/`proposed_value`
  are written for a human to read and manually action (e.g. by hand-
  editing `config/tuning_registry.yaml`, same as any other Tier D
  change today). This module has no import of, or call into,
  `tuning_registry.write_param()`.
- No retry/backoff logic, no rate limiting, no streaming — a single
  `httpx` call per finding with a plain timeout, best-effort.

TIER_D_PILOT_PARAMS = ("EOD_BIAS_ENFORCED_THRESHOLD", "EOD_BIAS_RAW_OVERCONFIDENT",
    "EOD_BIAS_RAW_UNDERCONFIDENT", "EOD_DRIFT_THRESHOLD", "BAYES_PRIOR_COUNT",
    "KELLY_REDUCTION", "STAGNATION_EPSILON", "STAGNATION_TIER1_CYCLES",
    "STAGNATION_TIER2_CYCLES", "STAGNATION_RESET_CYCLES", "STAGNATION_KELLY_REDUCTION_TIER1")

### 2.4 A real, unavoidable data-boundary note (not the same as the Jina question)

Unlike the Jina retrieval question — which had a genuine local
alternative and was reopened/closed as an actual choice — LLM-assisted
triage has no local alternative that fits "minimum effort": the entire
point of Phase 3 is a model call. Concretely, this means the finding's
`summary`/`details` and up to 3 short code/worklog excerpts (from this
repo's own semantic index) get sent to whichever LLM provider answers
(OpenRouter or NVIDIA NIM, see §6) per triage run. No trading
credentials, prices, or account data are in that payload — only the
same code/worklog text already sitting in the local knowledge index —
but it is a new category of external call this repo didn't have
before, worth naming plainly rather than leaving implicit.

### 2.5 New Studio secrets required

~~`ANTHROPIC_API_KEY`~~ — **corrected 2026-09-11, see §6**:
`OPENROUTER_API_KEY` and/or `NVIDIA_NIM_API_KEY`, read directly via
`os.getenv()` inside the shared `llm_client.py` (same pattern as
`TIMESFM_API_KEYS` in `orchestrator.py`), **not** added to
`settings.py`. Not set by this session — the user needs to add at
least one to Lightning Studio's secret manager for the job to actually
call an LLM. Missing keys are handled the same way missing
`TIMESFM_API_KEYS` already is: a clear one-time warning log, then the
job no-ops for that run instead of crashing.

---

## 3. Phase 4 — carried forward unchanged

Nothing here changes the case against Phase 4: any LLM or auto-tuner
component with *write* access to a live trading/sizing parameter is
**explicitly not recommended soon**, MVP or not. `triage.py` never
imports `tuning_registry.write_param()` — enforced structurally, not
just by convention.

---

## 4. Deferred: forecasting-grounded triage — **partially built same day, see §6**

The first draft of this document proposed grounding Phase 3 in TimesFM
2.5 (live)/3.0 (separately approved, license-blocked per
`worklog/20260915_TIMESFM3_IMPLEMENTATION_SCOPE.md`) forecasts and the
12-month commodity macro model
(`docs/LLM/commodity_12m_forecast_model_2027.md`,
`docs/LLM/commodity_macro_context_scope.md`). Deferred here as
"bigger than minimum effort" — but the user asked the same day to
resume it. §6 builds the commodity-macro-model half (the
`commodity_macro_context_scope.md` §3.3 "inline LLM agent," in
structured-output form) as a standalone agent, not folded into
`triage.py`. **TimesFM forecast grounding is still not built** — that
remains the actual deferred piece.

---

## 5. Built 2026-09-11

- Migration: `nt_self_learning_findings.triaged_at` column added;
  `nt_self_learning_reports` table created.
- `regime_platform/services/self_learning/triage.py` — `triage_finding()`,
  `run_triage()`, prompt construction, JSON extraction, Tier D
  allowlist validation + bounds clamping.
- `supabase_client.py` — `fetch_untriaged_findings()`,
  `mark_finding_triaged()`, `insert_self_learning_report()`.
- `feature_flags.py`/`feature_flags.yaml` — `self_learning_triage_enabled`
  (default `true`).
- `orchestrator.py` — `self-learning-triage-nightly` job (02:30 UTC) +
  `_run_self_learning_triage()`.
- Tests: `tests/test_self_learning_triage.py`, 26 new tests (JSON
  extraction, Tier D allowlist validation/clamping, prompt building,
  mocked LLM call success/HTTP-failure/network-failure/missing-key
  paths, `triage_finding`/`run_triage` best-effort orchestration,
  orchestrator wiring). Full suite: 920 passed, 21 skipped (was 894
  before this change).
- **Not yet verified live** — needs an LLM provider key added to
  Studio secrets, then a real deploy + first real nightly run to
  confirm end-to-end (mirrors how `self-learning-detectors-nightly`
  was verified after its own first deploy). Superseded same day —
  see §6 for the corrected provider.

---

## 6. Follow-up, same day: provider correction + the macro-forecast agent

User feedback, two items:

1. **"we are not using anthropic - ask before assumptions are made."**
   §2/§5 above shipped calling the Anthropic Messages API — a real
   mistake, not caught before this turn. Corrected by extracting a
   shared `regime_platform/services/self_learning/llm_client.py`
   (`call_llm`/`call_llm_with_provider`) implementing **OpenRouter
   primary, NVIDIA NIM fallback** — the exact pattern already live and
   verified in the sibling `nobletradingapp` repo's
   `supabase/functions/eod-report-generate/index.ts`
   (`generateCommentary`/`callChatCompletions`): same two base URLs
   (`openrouter.ai/api/v1`, `integrate.api.nvidia.com/v1`), same
   OpenAI-compatible `/chat/completions` schema, same env-var-
   overridable model defaults (`minimax/minimax-m3:free` /
   `meta/llama-3.2-11b-vision-instruct`, verified working 2026-09-05),
   same two Studio-secret names (`OPENROUTER_API_KEY`,
   `NVIDIA_NIM_API_KEY`). `triage.py` now calls this shared client
   instead of Anthropic directly — no other change to its behavior.
2. **"we still want to build the long term forecasting"** + **"can we
   enhance the llm response to include json like
   [trend:"",direction:""] ... The semantic output is ok for human
   users, not for system automation."** Per user's explicit answers to
   3 clarifying questions (asked rather than assumed, per item 1):
   build the `commodity_macro_context_scope.md` §3.3 inline agent
   (not a triage-schema widening), with structured output matching
   **both** `trend_filter.py`'s 5-value vocab (`current_trend_label`)
   and `macro_filter.py`'s 3-value vocab (`current_macro_label`, the
   "direction"), reusing the same OpenRouter/NVIDIA NIM models as (1).

New: `regime_platform/services/self_learning/macro_forecast_agent.py`
— for each of the 12 commodities in `commodity_macro_context_scope.md`
§1.1 (DAP excluded), sends the full 12-month forecast doc + today's
date to the LLM, asks it to determine the current quarter and
classify `{narrative, trend, direction, strength}`. Validates against
the exact enum vocab (out-of-vocab labels are discarded, not stored)
and clamps `strength` to `[0, 1]`. Upserts to a new `nt_macro_forecast`
table (`symbol` PK, `current_trend_label`, `current_macro_label`,
`current_macro_strength`, `narrative`, `model_provider`,
`updated_at`) — **shadow-mode diagnostic only**, matching
`commodity_macro_context_scope.md`'s own design: nothing reads this
table to make a live decision yet. Blending it into
`macro_filter.py`/`context_alignment.py` (that doc's §3.4/§3.5,
"Option A") is separate, not-yet-approved work.

Wired as `macro-forecast-agent-nightly` (02:45 UTC, same self-learning
window as the other two jobs), gated by `macro_forecast_agent_enabled`
(default `true`, same risk class as the other self-learning flags).

Built 2026-09-11:
- `llm_client.py` (new, shared) + `triage.py` (provider swap only).
- `macro_forecast_agent.py` (new).
- Migration: `nt_macro_forecast` table.
- `supabase_client.py` — `upsert_macro_forecast()`.
- `feature_flags.py`/`feature_flags.yaml` — `macro_forecast_agent_enabled`.
- `orchestrator.py` — `macro-forecast-agent-nightly` job + `_run_macro_forecast_agent()`.
- Tests: `tests/test_llm_client.py` (10), `tests/test_macro_forecast_agent.py`
  (15), `tests/test_self_learning_triage.py` updated (provider-swap
  tests replaced, count unchanged net). Full suite: 943 passed, 21
  skipped.
- **Still not verified live** — needs `OPENROUTER_API_KEY` and/or
  `NVIDIA_NIM_API_KEY` added to Studio secrets (separate secret store
  from `nobletradingapp`'s Supabase Edge Function secrets — these are
  Lightning Studio env vars), then a real deploy + first nightly run
  for all three jobs (02:00/02:30/02:45 UTC).
- **Still deferred, not built**: TimesFM forecast grounding for
  `triage.py` (the other half of §4's original idea) and blending
  `nt_macro_forecast` into the live `macro_filter.py` pipeline.
