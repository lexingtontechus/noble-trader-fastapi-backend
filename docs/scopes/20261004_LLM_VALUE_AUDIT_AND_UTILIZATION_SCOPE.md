---
title: "LLM Value Audit and Utilization — Clef-Flash, BGE-small, Macro Forecast, Triage"
author: "Hermes"
created: "2026-10-04"
status: "scope — assessment complete, builds not started"
tags: [kairos, llm, clef-flash, bge-small, macro-forecast, self-learning, categorization, scope]
related:
  - docs/scopes/20261003_CLEF_FLASH_VS_ALPHA_PULSE_EVAL.md
  - docs/worklog/20261003_CLOUDFLARE_WORKER_EMBEDDINGS_MIGRATION.md
  - worklog/kairos/reports/20261001_rm1_markov_source_eval.md
  - worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md
---

# LLM Value Audit and Utilization

## 0. Executive summary

Four LLM-adjacent surfaces exist in Kairos. Measured against live data today, three of
them produce output that **nothing reads**, and one (Clef-Flash) has no output at all:

| Surface | Writer | Reader | Live data | Live value |
|---|---|---|---|---|
| Clef-Flash (`@cf/cloudflare/clef-flash`) | Worker `/decision` route | **nobody** | **0 episodes** — no `p_clef` column, no caller | **none** |
| BGE-small (`@cf/baai/bge-small-en-v1.5`) | Worker `/embeddings` route | `knowledge_index.py` (shadow) | deployed, retrieval quality **unverified** | **potential** |
| Macro forecast agent | `macro_forecast_agent.py` | **nobody** | 33 rows / 33 symbols, daily | **diagnostic** |
| Self-learning triage | `triage.py` | **nobody** | 54 findings / 54 reports, 100% triaged | **marginal** |

The rule this document enforces: **every LLM output needs a live consumer, or it is waste.**
Each surface below gets one of three dispositions — *wire it in (with a real gate)*,
*repurpose it*, or *kill it*.

## 1. The Clef-Flash categorizer question, answered directly

> **Q.** What true value can Clef-Flash add as a *categorizer* — reading renko or
> walk-forward data to classify direction, or to agree/disagree on a market shift or
> trend up/down?

**A. None, and the platform has already paid for this lesson twice.** Clef-Flash must
**not** be used to derive direction / trend / regime from price data. The evidence is
already in the repo:

| Model on Kairos price data | Accuracy | Naive majority baseline | Skill |
|---|---|---|---|
| Legacy 5-state HMM (fitted state model) | 0.5251 | 0.5655 | **−0.0404** |
| trend_hmm (RegimeHMM-derived) | 0.3230 | 0.5655 | −0.2426 |
| first-passage, direct price-path walk (Phase 2) | — | 0.7041 base rate | **−0.0691** |

Sources: `worklog/kairos/reports/20261001_rm1_markov_source_eval.md`,
`worklog/20261001_PHASE2_FIRST_PASSAGE_RESULT.md`.

Two independent *fitted probabilistic models* over the same numeric series measured
**below a constant**. The HMM→first-passage pivot (2026-10-01) was the platform
learning exactly this: the discretised state abstraction added nothing over the naive
base rate, and the honest answer is the direct price-path walk — which itself did not
beat the base rate on this corpus. On 87,187 stored signals across 14 symbols, the
directional signal is **at or below chance**.

An LLM re-reading the same series would be a strictly worse copy of that signal:

- **Non-deterministic** — temperature/sampling; two identical inputs can disagree.
- **Non-reproducible** — logits are not auditable the way `walk()` arithmetic is.
- **Slower** — network round-trip per symbol vs. in-process numpy over a 48-bar window.
- **More expensive** — $0.09/M tokens × 14 symbols × 288 sweeps/day.
- **Uncalibrated against the base rate** — it inherits the same sub-chance information
  ceiling, with none of the deterministic engine's reproducibility.

**Do not add `p_clef` as a 4th `P_WIN_WEIGHTS_FULL` source.** That is re-deriving a
probability the price path already answers, from a signal that is already below the
naive baseline. Adding a 4th correlated source to an overconfident blend (bracket
AUC 0.486, `docs/worklog/20261003_CLOUDFLARE_WORKER_EMBEDDINGS_MIGRATION.md` §8) makes
the blend worse, not better.

### 1.1 Where Clef-Flash *does* add value

Clef-Flash's one structural advantage over the chat-completion LLMs already in the
codebase is **structured logits instead of free text** — no `extract_json()`, no
markdown-wrapping failures, no provider-retirement churn (the `minimax/m3:free`
incident, `nt_roadmap.llm_client_openrouter_free_model_retired`). That advantage is
real **only where the input is text**, which is where Kairos currently has a fragility
it would like to remove:

| Consumer | Input | Today | Clef-Flash value |
|---|---|---|---|
| `kairos_pivot.py::label_event` | economic-calendar event text | chat LLM + `extract_json()` | **Reliability** — structured `choice`, no parse layer, no OpenRouter/NIM key |
| `macro_forecast_agent.py::classify_symbol` | 12-month forecast doc | chat LLM + `extract_json()` | **Reliability** — same |
| `triage.py` | detector finding + code excerpts | chat LLM, free-text narrative | **Partial** — can emit a structured *category*, cannot emit prose |

These are **replacements of an existing fragile call**, not new capability. The value is
operational (fewer silent parse failures, one fewer API dependency), and it is
measurable: parse-failure rate and median latency of the current chat path vs. Clef-Flash.

### 1.2 The one genuinely new categorical capability

The only *new* thing a text classifier can add here is **narrative-vs-tape agreement**:
given the platform's own `thesis`/`drivers` text (already in `nt_macro_forecast`) and
the deterministic `regime_label` / `trend_label` (price-derived), ask Clef-Flash whether
the strategic narrative **agrees with the realized regime shift**. This is text-vs-label
classification, requires no price re-derivation, and is genuinely orthogonal to the HMM.

This is a **shadow research input at most** — it never touches `p_win`, `ev_result`, or
any gate. It belongs in the same class as `context_alignment.py` (g9): a shadow-only
agreement read, measured for discrimination before any promotion is considered.

## 2. Recommended dispositions

### 2.1 Clef-Flash — repurpose, do not blend

- **Kill** the "4th P_win source" plan. Rewrite `nt_roadmap` id=89
  (`kairos_clef_flash_decision_layer`) — its current gate
  (`correlation(p_clef, p_win_bracket)` after ≥200 shadow episodes) is **unmeetable**:
  `p_clef` does not exist as a column and no caller stamps it. A gate that can never
  fire is not a gate.
- **New gate:** parse-failure rate and median latency of the Clef-Flash pivot/macro
  classifier vs. the current chat path. Ship Clef-Flash behind a `clef_flash_classifier_mode`
  flag (off/shadow/live), shadow-first, and only replace the chat call when it is at
  least as reliable. See `20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`.

### 2.2 BGE-small — finish the verification, then cut the local model

The migration is deployed and falls back to MiniLM on any failure, but the **retrieval
quality comparison was never run** — the whole reason the migration exists (offload CPU
inference off the Studio) is unproven until cosine similarity between BGE and MiniLM
embeddings of the same corpus is measured. See §4.3.

### 2.3 Macro forecast — wire into `macro_filter.py` (shadow)

`nt_macro_forecast` (33 symbols, daily, clean structured output) has no reader. Its
`current_macro_label` + `current_macro_strength` are the strategic counterpart to
`macro_filter.py`'s 200-day price read. Wire it in as a shadow source, compare against
outcomes, promote only on evidence. See `20261004_MACRO_FORECAST_CONSUMER_SCOPE.md`.

### 2.4 Self-learning triage — surface it, stop re-triaging the same finding

54 reports, 100% triaged, **15 of them the identical `regime_label_debounce` stale
finding re-detected daily 2026-09-26 → 2026-10-04**. The pipeline works; the value is
zero because (a) nobody reads the table and (b) the detector re-fires a known-stale
mechanism every night. Surface the reports in nobletradingapp **and** fix the detector.
See `20261004_SELF_LEARNING_TRIAGE_SURFACE_SCOPE.md`.

## 3. Value matrix (measured, 2026-10-04)

| Surface | Data produced | Live impact | Quality | Disposition |
|---|---|---|---|---|
| Clef-Flash | 0 episodes | none | untested | **Repurpose** → text classifier |
| BGE-small | embeddings (unverified) | none (shadow) | unverified | **Verify** → then cut MiniLM |
| Macro forecast | 33 symbols daily | none (shadow) | good structure | **Wire** → macro_filter shadow |
| Triage | 54 reports | none (shadow) | repetitive | **Surface** → frontend + dedup |

## 4. Open questions / decisions needed

1. **Clef-Flash disposition** — repurpose to the text classifier (recommended), or
   retire the worker entirely? Repurpose costs one flag + one call-site swap and removes
   the OpenRouter/NIM dependency from two jobs.
2. **BGE-small cutover** — is a CPU-offload of ~90MB MiniLM worth the added network
   dependency, given the Studio has spare CPU? If retrieval quality is equivalent and
   the CPU is not contended, the local model is simpler. Measure first.
3. **Macro-forecast promotion bar** — what discrimination improvement over the pure-price
   `macro_label` justifies a live `kelly_macro_multiplier` from the LLM label? Proposed:
   the same bar `macro_edge.py` already uses (aligned vs. opposed expectancy_r sign
   consistency across the affected symbols).
4. **Triage surface audience** — admin-only (recommended; the findings name internal
   mechanisms and propose Tier D parameter changes) or a member-facing "platform
   improvements" digest?

## 5. What is and is not in scope

**In scope:** the four dispositions above; the nobletradingapp UX surfaces in the two
consumer scope docs; the `nt_roadmap` gate corrections.

**Out of scope:** any change to `p_win` / `ev_result` / `P_WIN_WEIGHTS_FULL`; any
direction/trend/regime classification from price data by an LLM (explicitly rejected §1);
the `legacy_hmm` retirement decision (owned by `nt_roadmap` id=84); the TimesFM path.
