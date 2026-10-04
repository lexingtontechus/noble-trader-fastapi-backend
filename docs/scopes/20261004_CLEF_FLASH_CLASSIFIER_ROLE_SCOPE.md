---
title: "Clef-Flash Classifier Role — repurpose from EV source to text categorizer"
author: "Hermes"
created: "2026-10-04"
status: "scope — build not started"
tags: [kairos, clef-flash, llm, pivot, macro-forecast, cloudflare, scope]
related:
  - docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md
  - docs/scopes/20261003_CLEF_FLASH_VS_ALPHA_PULSE_EVAL.md
  - regime_platform/sweep_orchestrator/kairos_pivot.py
---

# Clef-Flash Classifier Role Scope

## 1. Decision

**Clef-Flash is repurposed from a proposed EV-blend source to a structured text
categorizer.** The `p_clef` 4th-source plan is retired; the roadmap gate that depends on
it is unmeetable and must be rewritten.

### Why the EV-source plan is retired

`nt_roadmap` id=89 (`kairos_clef_flash_decision_layer`, review 2026-10-10) gates on
"≥200 post-shadow episodes with `p_clef` stamped AND `correlation(p_clef, p_win_bracket)`".
Measured today:

- `p_clef` **does not exist** as a column in `nt_signal_sim` (SQL: `column "p_clef" does not exist`).
- **No `ClefFlashCaller`** exists anywhere in the codebase (grep: 0 matches).
- **0 episodes** — nothing stamps it.

The gate can never fire. And the underlying premise is wrong: §1 of
`20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md` shows the price-derived directional
signal is already at/below the naive base rate, so a 4th correlated source cannot help.

## 2. What Clef-Flash is repurposed *to*

Clef-Flash returns **one logit per option per question, softmax-normalised** — no free
text, no `extract_json()`. Its value is therefore **operational reliability** at the two
call sites where Kairos already depends on a fragile free-text chat LLM.

### 2.1 Target call sites

| Site | Current | Clef-Flash question schema |
|---|---|---|
| `kairos_pivot.py::label_event` | chat LLM (`call_llm_with_provider`) + `extract_json()` | `choice(label: major_pivot/incremental/no_change)`, `choice(axis: reaction_function/tool/trajectory/none)`, `score(intensity)`, `score(surprise)` |
| `macro_forecast_agent.py::classify_symbol` | chat LLM + `extract_json()` | `choice(direction: bull/bear/neutral)`, `choice(trend: trending_up/…/consolidating)`, `score(strength)` |
| `triage.py` (optional) | chat LLM, free-text narrative | `choice(category)` over detector root-cause classes — **cannot** produce the narrative prose, so keep the chat path for prose and add the category as a structured extra |

### 2.2 The genuinely new capability (shadow research only)

**Narrative-vs-tape agreement.** Given the platform's own `nt_macro_forecast.thesis` +
`drivers` (text) and the deterministic `regime_label` / `trend_label` (price), ask
Clef-Flash whether the strategic narrative **agrees with the realized regime shift**:

```
questions: {
  "agrees_with_regime": { type: "noul",
    instructions: "Does the narrative thesis agree with the realized regime direction?" }
}
```

This is text-vs-label classification — orthogonal to the HMM, requires no price
re-derivation, and belongs in the same shadow-only class as `context_alignment.py` (g9).
It **never** touches `p_win`, `ev_result`, or any gate.

## 3. Build plan (shadow-first)

### 3.1 Flag

```yaml
# clef_flash_classifier_mode: off | shadow | live
#   shadow: call Clef-Flash alongside the chat path, log both, use the chat result.
#           Measures parse-failure rate + latency of the two paths side by side.
clef_flash_classifier_mode: shadow
```

### 3.2 Caller

`regime_platform/services/decision/clef_flash.py` — a thin async HTTP client to the
deployed Worker's `/decision` route (`https://nobletradingapp.lexington.workers.dev`),
auth via `CF_WORKER_SECRET`, reading `CF_EMBEDDINGS_URL` for the base. Mirrors the
existing `_embed_texts_cf` HTTP pattern in `knowledge_index.py`. Never raises — returns
`None` on any failure so the caller falls back to the chat path.

### 3.3 Call-site wiring (fail-open)

In `kairos_pivot.py::label_event` and `macro_forecast_agent.py::classify_symbol`, wrap the
new path in `try/except` and fall back to the existing chat call on any exception or
`None`. In shadow, log both outcomes on one line so the comparison is measurable.

### 3.4 Promotion gate (replaces id=89's unmeetable gate)

| Metric | Chat path (today) | Clef-Flash | Pass condition |
|---|---|---|---|
| Parse-failure rate | measure | measure | Clef-Flash ≤ chat |
| Median latency | measure | measure | Clef-Flash ≤ chat |
| Label agreement (shadow) | — | — | ≥ 80% agreement on a shared sample before cutover |

Only when Clef-Flash matches the chat path on reliability does `live` replace the call.
The value is a **removed dependency** (no OpenRouter/NIM key, no model-retirement churn),
not a new signal.

## 4. Cloudflare Worker readiness

Already deployed and tested (`docs/worklog/20261003_CLOUDFLARE_WORKER_EMBEDDINGS_MIGRATION.md`):
`POST /decision` → `{answers: {direction: {choice, probabilities}}, model, usage}`.
Confirmed test: `{choice: "buy", probabilities: {buy: 0.853, sell: 0.040, wait: 0.107}}`.

Two known pitfalls to respect (from the migration worklog):
- The REST-API fallback body needs `model: "clef-flash"` (short name), not the full
  `@cf/cloudflare/clef-flash` path.
- The `[ai]` binding in `wrangler.toml` does not auto-apply in Wrangler 4.x — it was
  applied via an API PATCH; re-verify `ai_binding: true` on the worker root route.

## 5. Open questions

1. **Pivot classification worth** — `pivot_label` is `no_change` on 33/33 symbols today.
   Is the pivot classifier actually discriminating, or is the calendar quiet? Check the
   `no_change` rate over a longer window before treating this as a valued call site.
2. **Keep the chat LLM at all** — if Clef-Flash matches reliability at both sites, the
   chat path (`llm_client.py`, OpenRouter + NIM) becomes dead code for classification and
   survives only for triage prose. Decide whether to consolidate.
3. **Worker cost at 14-symbol cadence** — the classifier runs nightly (2 calls × 33
   symbols), not per-sweep, so cost is negligible. Confirm no accidental per-tick wiring.

## 6. Scope boundaries

**In scope:** the flag, the caller, the two call-site swaps (fail-open), the shadow
comparison, the `nt_roadmap` id=89 gate rewrite, the narrative-vs-tape shadow question.

**Out of scope:** any `P_WIN_WEIGHTS_FULL` change; any new EV source; the BGE-small
embeddings path; using Clef-Flash to classify direction/trend/regime from price data
(explicitly rejected — `20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md` §1).

## 7. Roadmap correction required

Rewrite `nt_roadmap` id=89:

- `mechanism`: unchanged (`kairos_clef_flash_decision_layer`).
- `status`: `pending`.
- `current_state`: "Repurposed from EV-blend source (retired) to structured text
  categorizer. `p_clef` column never existed; no caller ever built."
- `review_condition`: "Clef-Flash parse-failure rate and median latency vs. the chat path
  at `kairos_pivot.py`/`macro_forecast_agent.py`, ≥80% label agreement on a shared shadow
  sample."
- `review_at`: 7 days after the caller ships.
