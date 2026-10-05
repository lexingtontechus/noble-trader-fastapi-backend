# Clef-Flash Shadow Stamp — build worklog

**Date:** 2026-10-04
**Status:** BUILT — shadow, fail-open, migration applied, tests green
**Scope:** `docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`,
`docs/scopes/20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md`
**Roadmap:** `nt_roadmap` id=89 (`kairos_clef_flash_decision_layer`) — repurposed

## What was built

Clef-Flash (`@cf/cloudflare/clef-flash`, Cloudflare Workers AI) is repurposed from a
retired EV-blend source to a **shadow research stamp**. It is a structured-decision
model: one logit per option per question, softmax-normalised — no free text, no
`extract_json()` fragility.

| Artefact | Path |
|---|---|
| Migration | `supabase/migrations/194_nt_signal_sim_p_clef.sql` (applied) |
| Caller | `regime_platform/services/decision/clef_flash.py` (new package) |
| Flag | `config/feature_flags.yaml` + `feature_flags.py` `_DEFAULTS` + `get_clef_flash_mode()` |
| Schema whitelist | `supabase_client.py::NT_SIGNAL_SIM_COLUMNS` + `_KAIROS_CAPTURE_KEYS` |
| Stamp | `orchestrator.py::_clef_flash_stamp` + `_build_signal_sim_payload` params + call site |
| Tests | `tests/test_clef_flash.py` (23 tests) |

## Columns (migration 194, applied)

| Column | Type | Meaning |
|---|---|---|
| `p_clef_dir` | `double precision` | P(model agrees with the signal's direction) — the discrimination column |
| `p_clef_wait` | `double precision` | P(model says "wait") — the abstention column |
| `p_clef_symbol` | `text` | model slug used (provenance) |

All nullable, no default. Verified via `information_schema.columns` + column comments.

## Contract enforced

- **Research only.** Never added to `P_WIN_WEIGHTS_FULL`, never passed to `compute_ev()`,
  never touches `qualified` / `published` / `gate_outcome` / Kelly / SL / TP. The stamp
  is written and never read back into a decision.
- **Context only.** The `state` block carries the signal's already-computed context
  (symbol/direction/regime/bracket/macro/trend) — never raw candles or a renko series.
  The model is not asked to re-derive anything the deterministic engine produced.
- **Fail-open.** `decide()` returns `None` on missing config, non-2xx, timeout, or
  malformed body; the stamp returns all-None. `clef_flash_mode: off` → no HTTP call.
- **Light sweep only.** `source != "sweep-light"` → no stamp (heavy sweep excluded).
- **Guarded HTTP.** 10s timeout; a hung worker cannot stall the sweep. Symbols process
  sequentially (verified — no unbounded concurrency fan-out), so at most one Clef-Flash
  call is in flight per sweep tick.

## Question schema

Three-way `choice(buy, sell, wait)` (not binary agree/disagree — a binary cannot express
"the opposite is favoured"). `p_clef_dir` is direction-relative: `probabilities[signal_side]`
so buy and sell rows are comparable. No `score(confidence)` question — the choice
distribution is already the confidence signal, and adding a second question doubles the
token cost for a research stamp with no consumer yet.

## The categorizer primitive + first call-site wiring

`clef_flash.classify_choice(instructions, options, state)` → `{label: probability}` — the
generic structured-label primitive. It is the same call shape as the direction stamp but
returns the whole distribution for any fixed label set.

**First call site wired:** `kairos_pivot.py::label_event`. When
`clef_flash_mode == "live"`, the structured-logit path (`_label_event_clef`) is tried
first — label = arg-max, `intensity` = its probability — through the **same
`validate_pivot`** the chat path uses. Any failure (None, missing config, unknown label)
falls through to the existing free-text `call_llm_with_provider` path unchanged. In
`shadow` (the default) the classifier path is **not** touched — the flag value is the
cutover switch, and the shadow stamp is independent of it.

**Verified against the live Worker** (2026-10-05): an FOMC-style event returns
`{major_pivot: 0.077, incremental: 0.085, no_change: 0.838}`, `confidence: 0.574` — a
proper distribution, no parsing. The second site (`macro_forecast_agent.py::classify_symbol`)
is the identical shape and is the next wiring.

## Flag

```yaml
clef_flash_mode: shadow   # off | shadow | live
```

`shadow` (default) → stamp. `off` → no HTTP call. `live` → reserved for the classifier
cutover (does not change the stamp). Unknown → `shadow`. Requires `CF_EMBEDDINGS_URL`
(Worker base URL) or `CF_DECISION_URL`; no-ops without either.

## Verification

- `tests/test_clef_flash.py`: **23 passed** — state/questions builders, the
  direction-answer parser (buy/sell sides, missing shape, wait default, model slug),
  fail-open (no config / HTTP error / exception), end-to-end `decide_direction`, the flag
  accessor (default/unknown/off), whitelist presence, payload stamp/omit, and the two
  orchestrator stamp guards (non-light source, off mode).
- Full suite: see changelog entry.

## Next (not this build)

- `clef_flash_mode: live` cutover at `kairos_pivot.py` / `macro_forecast_agent.py` —
  gated on the parse-failure-rate + latency comparison.
- Evaluation gate: `AUC(p_clef_dir) > AUC(p_win_dir)` on ≥200 decided, episode-deduped
  rows. Fail → keep display-only.
