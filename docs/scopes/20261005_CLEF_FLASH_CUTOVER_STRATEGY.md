---
title: "Clef-Flash Cutover Strategy — baseline, gate, execute, resolve"
author: "Hermes"
created: "2026-10-05"
status: "scope — the governing plan; supersedes the ad-hoc next-step framing"
tags: [kairos, clef-flash, llm, strategy, cutover, roadmap]
related:
  - docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md
  - docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md
  - docs/scopes/20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md
---

# Clef-Flash Cutover Strategy

This is the governing plan for the Clef-Flash repurpose. It replaces the
"one next step at a time" framing with a single staged programme that has a
defined **start** (Stage 0) and a binary **endpoint** (Stage 3: promoted or
retired). No stage is a discovery that spawns another stage — every stage is
named here up front.

## The reframe that drives the plan

**Clef-Flash replaces a LABEL, not the TEXT.** Both classifier call sites return
prose alongside the categorical output:

| Site | Clef produces | Clef cannot produce |
|---|---|---|
| `kairos_pivot.py::label_event` | label, axis, intensity, surprise | `summary` (one-line reason) |
| `macro_forecast_agent.py::classify_symbol` | trend, direction, strength | `narrative`, `thesis`, `drivers`, `risks` |

Consequence, stated plainly: **the chat LLM (`llm_client`, OpenRouter/NIM) stays
at every site** for the prose. Wiring Clef does NOT remove that dependency. The
gain is narrower and must be justified on its own terms:

1. the **label** becomes parse-proof (no `extract_json()` failure mode), and
2. the label carries a **native confidence** (`max(probabilities)`) a caller can
   gate on.

Both are real. Neither is worth building until the baseline shows the chat
path's label is actually failing.

## Why this order (not "wire macro first")

- **Wiring `macro_forecast_agent` first is backwards.** Its output
  (`nt_macro_forecast`) has no live consumer yet (see
  `20261004_MACRO_FORECAST_CONSUMER_SCOPE.md`), and it is a *hybrid* — Clef does
  3 labels while chat still does the narrative. Building it before knowing the
  pivot site is justified spends effort on the weaker, more complex site.
- **The comparison is the gate, not a side quest.** It is the evidence that
  decides whether *any* cutover happens. Without it, `live` is a guess.
- **The comparison is not step one either.** Step one is cheaper and more
  fundamental: is there a problem at all?

## The programme

### Stage 0 — Baseline (START)

**Question:** does the chat path's label classification actually fail, and how
often?

**Method (read-only, no build):**
1. Grep `orchestrator.log` history for the chat path's own failure signals:
   `llm_client: ... wasn't valid JSON`, `macro_forecast_agent: response missing
   a usable narrative — discarding`, `out-of-vocab trend/direction`, and the
   pivot path's equivalent discards.
2. Cross-check against stored coverage: `nt_macro_forecast` rows per nightly run
   vs the symbol universe (33 expected); `pivot_label` non-null share.
3. If logs are thin (log truncates on restart), a controlled run: 20 real
   calendar events + the forecast doc through the current chat path, count
   discards.

**Gate:** is the failure rate non-trivial (> ~5%)? 
- **No** → there is nothing to fix. Jump to Stage 3 (retire). The repurpose is
  moot; the worker stays for embeddings only.
- **Yes** → Stage 1.

**Produces:** a parse-failure rate + median latency, per site, from real data.

### Stage 1 — Gate (comparison)

**Question:** is Clef-Flash at least as reliable as the chat path?

**Deliverable:** `scripts/replay_clef_vs_chat_classifier.py` — runs both paths on
the same frozen inputs (the Stage-0 event/doc set) and measures:

| Metric | Chat | Clef |
|---|---|---|
| parse-failure rate | `extract_json` None / vocab reject | `classify_choice` None |
| median latency (ms) | measured | measured |
| label agreement | — | arg-max(Clef) vs chat label |

**Gate:** Clef parse-fail **≤** chat **AND** Clef latency **≤** chat **AND**
agreement **≥ 80%**.
- **Fail** → Stage 3 (retire).
- **Pass** → Stage 2.

**Produces:** the comparison report (numbers, per site).

### Stage 2 — Execute (conditional)

**Pivot site (pure label — the clean fit):**
Flip `clef_flash_mode: live`. The label is Clef's; `summary` is either kept from
a chat call or dropped (product decision — see Open questions). Watch 7 days.

**Gate:** no regression in `pivot_label` coverage; no new discards in the logs.

**Macro site (hybrid — decided on its own merits, not automatically):**
Two options, chosen at this stage, not before:
- **Hybrid:** Clef for `trend`/`direction`/`strength`, chat for
  `narrative`/`thesis`/`drivers`/`risks`. Only if Stage-0 showed the macro label
  itself failing.
- **Leave on chat:** the narrative deconstruction is the bulk of that call;
  replacing only its labels may not be worth a second code path.

**Produces:** `clef_flash_mode` value at the end of the 7-day window, per site.

### Stage 3 — Resolve (ENDPOINT)

**Deliverable:** `nt_roadmap` id=89 (`kairos_clef_flash_decision_layer`) →
`resolved`, with one of two `current_state` values:

- **PROMOTED:** "Clef-Flash live at `kairos_pivot` (label), with the Stage-1
  comparison as evidence: parse-fail X%→Y%, latency A→B ms, agreement Z%.
  Macro site: [hybrid | left on chat]. Chat LLM retained for prose."
- **RETIRED:** "No cutover. Stage-0 baseline failure rate W% — below the
  threshold, nothing to fix. `clef_flash_mode: off`; the Cloudflare Worker is
  kept for BGE-small embeddings only."

**No limbo.** Either Clef is live somewhere, or the flag is off and the roadmap
item is closed with the reason. The worker is never left half-used.

## The parallel track (do not conflate)

The **`p_clef` direction stamp** (`p_clef_dir`/`p_clef_wait`/`p_clef_symbol` on
`nt_signal_sim`) is a *different* question — it is the retired EV-source framing,
still running as a research stamp. Its own gate is defined in
`20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md` §6
(`AUC(p_clef_dir) > AUC(p_win_dir)` on ≥200 decided, episode-deduped rows). It is
**not** a promotion candidate to the EV blend — that framing is retired. Its
endpoint: **keep as a research/display column, or drop the stamp.** Resolve it
on the same timeline as Stage 3 so both Clef threads close together.

## Open questions (decided at Stage 2, not now)

1. **Pivot `summary` under Clef** — keep a chat call for the one-line summary
   (defeats part of the point), or drop it from `nt_macro_forecast`? Dropping is
   cleaner if nothing consumes `pivot_summary` beyond display.
2. **Macro hybrid vs leave-on-chat** — decided by whether Stage 0 found the
   macro *label* failing (as opposed to the narrative).

## What is not in scope

- Any change to `p_win` / `ev_result` / `P_WIN_WEIGHTS_FULL` — Clef is never an
  EV source (retired framing).
- Classifying direction/trend/regime from the price series with an LLM
  (`20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md` §1).
- The BGE-small embeddings path (separate; already deployed).
