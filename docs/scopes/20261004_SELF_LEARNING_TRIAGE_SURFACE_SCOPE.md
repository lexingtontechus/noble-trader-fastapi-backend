---
title: "Self-Learning Triage Surface — read path, dedup, and nobletradingapp frontend"
author: "Hermes"
created: "2026-10-04"
status: "scope — build not started"
tags: [kairos, self-learning, triage, llm, dead-mechanism, nobletradingapp, scope]
related:
  - docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md
  - regime_platform/services/self_learning/triage.py
  - regime_platform/services/self_learning/run_detectors.py
---

# Self-Learning Triage Surface Scope

## 1. Plain-language framing

Three nightly jobs run in the 02:00–02:45 UTC window: **detectors** (`run_detectors.py`)
find anomalies and write `nt_self_learning_findings`; **triage** (`triage.py`) reads each
untriaged finding, grounds it in related code/worklog excerpts, and asks an LLM for a
root-cause narrative plus an optional bounded Tier D parameter proposal, writing
`nt_self_learning_reports`; the **macro forecast agent** runs third.

The pipeline is mechanically healthy: **54 findings, 54 triaged, 0 pending**. The value
is near-zero for two independent reasons:

1. **Nobody reads the output.** `nt_self_learning_reports` has no consumer in any repo.
2. **The detector re-fires the same finding every night.** 15 of the 54 findings are the
   *identical* string — `regime_label_debounce_mode: live, nt_regime_debounce_shadow stale
   (last write 2026-09-22...)` — re-detected 2026-09-26 → 2026-10-04. Each one triggers a
   fresh LLM call producing a near-duplicate narrative.

Measured today:

| Metric | Value |
|---|---|
| Findings | 54 (`regime_price_mismatch` 39, `dead_mechanism` 15) |
| Symbols | 6 (findings carry a symbol only for detector 1) |
| Reports | 54 |
| Untriaged | 0 |
| `dead_mechanism` distinct summaries | **1** (repeated 15×) |
| `proposed_param` set | `STAGNATION_EPSILON`, `STAGNATION_TIER1_CYCLES` |
| `current_value` on every proposal | **NULL** — the tuning-registry lookup fails, so proposals are unactionable |

## 2. The two fixes

### 2.1 Fix the detector re-firing (backend, small)

`dead_mechanism` is a plain staleness check (`dead_mechanism_detector.py`). It flags
`regime_label_debounce_mode: live` because `nt_regime_debounce_shadow` last wrote
2026-09-22 — but that is a **known consequence of the mode being `live`**, not a fault.
When `regime_label_debounce_mode == "live"`, the orchestrator writes the debounced label
directly and stops writing the shadow-compare table (`orchestrator.py` §3545, the `else:
# live` branch). The shadow table is *supposed* to go stale when the mode goes live.

**Fix:** in `_DEAD_MECHANISM_REGISTRY`, mark the entries whose shadow table is only
written in `shadow` mode as **shadow-only-staleness** — a `live` mode with a stale shadow
table is expected, not a finding. Add a per-entry `shadow_table_only: true` and have
`detect_dead_mechanism` return `None` when `mode == "live" and shadow_table_only`.

This alone removes 15 of 54 findings and the 15 duplicate LLM calls per day.

### 2.2 Fix the unactionable proposals (backend, small)

`triage.py::_allowed_params_context` reads each param's current value via
`tuning_registry.read_param_async(name)`. Every report has `current_value: NULL`, so the
read is failing (or the params have no file entry). Confirm the failure mode, then either
fix the read or stop emitting a proposal when the current value is unknown — a proposal
with no baseline cannot be actioned.

## 3. The read path (backend → frontend)

### 3.1 An admin view (new)

Add `v_self_learning_triage` joining the two tables:

```sql
CREATE OR REPLACE VIEW v_self_learning_triage AS
SELECT
  r.id             AS report_id,
  f.id             AS finding_id,
  f.detector,
  f.symbol,
  f.summary        AS finding_summary,
  f.detected_at,
  f.triaged_at,
  r.narrative,
  r.proposed_param,
  r.proposed_value,
  r.current_value,
  r.created_at,
  COALESCE(f.reviewed, false)  AS finding_reviewed,
  COALESCE(r.reviewed, false)  AS report_reviewed
FROM nt_self_learning_reports r
JOIN nt_self_learning_findings f ON f.id = r.finding_id
ORDER BY f.detected_at DESC;
```

Grant: **admin only** — do NOT `GRANT SELECT TO anon` (findings name internal mechanisms
and propose parameter changes). The webapp reads it through a server-side route using the
service-role key, exactly like `/api/dashboard-data`.

### 3.2 A "reviewed" action (new)

Both tables already carry a `reviewed boolean NOT NULL` column (verified in
`information_schema`). Add a PATCH route so an admin can dismiss a report, which also
suppresses the finding from the open list.

## 4. nobletradingapp UX

See `../nobletradingapp/docs/scope-llm-surfaces-dashboard.md` §3 for the full UI spec.
Summary: an **admin-only "Self-learning" panel** — a list of findings with their LLM
narrative, the proposed param/value vs. current value, and a dismiss button. Plus a
**dedup rule in the query** (`DISTINCT ON (finding_summary)` or filter on
`finding_reviewed`) so the same stale finding does not fill the list.

## 5. Open questions

1. **Triage audience** — admin-only (recommended) or a member-facing "how the platform
   improves" digest with narratives stripped of internals? The current narratives name
   internal flags; a member digest needs a separate, sanitised prompt.
2. **Should triage write to a proposal queue at all** — given nothing auto-applies and
   the proposals are unactionable while `current_value` is NULL, is the Tier D proposal
   worth keeping, or should triage emit narrative-only until the baseline read works?
3. **Retention** — 54 rows is trivial, but if the dedup fix ships, decide whether to purge
   the 15 historical duplicates.

## 6. Scope boundaries

**In scope:** the detector staleness fix, the proposal-baseline fix, the admin view, the
reviewed PATCH route, the admin frontend panel.

**Out of scope:** adding new detectors; changing the LLM provider for triage (owned by
`20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`); any auto-application of proposals
(structurally forbidden — `triage.py` never imports `tuning_registry.write_param`).
