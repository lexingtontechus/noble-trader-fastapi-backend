---
title: "Macro Forecast Consumer — wiring nt_macro_forecast into macro_filter.py"
author: "Hermes"
created: "2026-10-04"
status: "scope — build not started"
tags: [kairos, macro-forecast, macro-filter, llm, shadow, scope]
related:
  - docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md
  - regime_platform/services/self_learning/macro_forecast_agent.py
  - regime_platform/sweep_orchestrator/macro_filter.py
---

# Macro Forecast Consumer Scope

## 1. Plain-language framing

`nt_macro_forecast` is a table an LLM writes every night: for each of 33 symbols it reads
a 12-month forecast document and outputs a **strategic** read — `current_macro_label`
(bull/bear/neutral), `current_macro_strength` (0–1), `current_trend_label`, a one-line
`thesis`, a list of `drivers`, a list of `risks`, plus a `structure_bias` (short-term,
computed from the latest sweep's TA, not the LLM). It is clean, validated output.

**Nothing reads it.** The one component that *should* — `macro_filter.py`, the live
long-range read that feeds the `context_alignment` gate and the macro Kelly multiplier —
computes its own `macro_label` from a 200-day SMA of daily closes. The two are
**complementary, not redundant**:

| | `macro_filter.py` (price) | `nt_macro_forecast` (LLM) |
|---|---|---|
| Input | 200-day SMA of daily closes | a 12-month forecast document |
| Horizon | structural level (multi-month) | strategic narrative (quarters ahead) |
| Catches | what price has done | what the narrative expects (rates, supply, geopolitics) |
| Updates | daily, deterministic | daily, LLM |

The LLM sees context the SMA cannot (a named rate path, a supply shock, a policy pivot).
Whether that context **improves discrimination** is an empirical question — this scope
wires it in as a shadow source so the question can be answered, then promotes only on
evidence.

## 2. Current state (live, 2026-10-04)

| Item | Status |
|---|---|
| Writer | `macro_forecast_agent.py`, nightly `45 2 * * *`, flag `macro_forecast_agent_enabled: true` |
| Table | `nt_macro_forecast` — 33 rows / 33 symbols, last write 2026-10-04 02:45 UTC |
| `thesis` / `drivers` populated | 33/33 |
| `structure_bias` populated | 14/33 (TA attach succeeds only when a fresh `sweep-light` row exists) |
| `pivot_label` | 33/33, **all `no_change`** (0 major, 0 incremental) |
| Reader | **none** in `regime_platform/` — verified by grep |
| `macro_filter.py` | computes `nt_macro_regime` from price only; flag `macro_regime_mode: shadow` |
| Gate consumer | `context_alignment.py` (g9, `context_alignment_gate_mode: shadow`) reads `macro_label` |

## 3. Build plan (shadow-first)

### 3.1 Flag

Add to `config/feature_flags.yaml` + `feature_flags.py::_DEFAULTS` + accessor:

```yaml
# macro_forecast_consumer_mode: off | shadow | live
#   shadow: read nt_macro_forecast, log the LLM label beside the price label,
#           do NOT touch kelly or the context gate. Default.
macro_forecast_consumer_mode: shadow
```

### 3.2 Reader (new, small)

`supabase_client.fetch_macro_forecast(symbol)` — one-row read of
`current_macro_label, current_macro_strength, current_trend_label, structure_bias,
thesis, pivot_label, updated_at`. Mirrors `fetch_macro_regime` (15s timeout, one retry,
`None` on failure — never raises).

### 3.3 Wire into `macro_filter.py`

In `get_macro_regime()` (or a sibling `get_macro_forecast_read()`), when
`macro_forecast_consumer_mode != off`:

1. Read `nt_macro_forecast` for the symbol.
2. In **shadow**: log one line — `[macro-fc] SYM price=bull/0.44 llm=bull/0.80 structure=bear agree=yes` —
   and return the price label unchanged. Nothing downstream changes.
3. In **live**: return the LLM label as the `macro_label` the context gate and Kelly
   multiplier consume, falling back to the price label when the forecast row is missing
   or stale (> 36h).

The **stamp on `nt_signal_sim`** must carry both reads so the comparison is possible:
add `macro_forecast_label` / `macro_forecast_strength` to `NT_SIGNAL_SIM_COLUMNS` and the
capture payload (migration adds the two nullable columns). This is the same
shadow-comparison shape `record_markov_shadow_compare` used.

### 3.4 Evidence gate (promotion bar)

Before `live`, pull the outcome evidence for the affected symbols. The comparison is
**aligned vs. opposed expectancy_r** for the LLM label vs. the price label, using the
existing `macro_edge.py` machinery:

```sql
-- LLM-label alignment edge (population: signals with a macro_forecast_label stamped)
SELECT
  CASE WHEN (signal='buy' AND macro_forecast_label='bull')
         OR (signal='sell' AND macro_forecast_label='bear') THEN 'aligned'
       WHEN (signal='buy' AND macro_forecast_label='bear')
         OR (signal='sell' AND macro_forecast_label='bull') THEN 'opposed'
       ELSE 'neutral' END AS alignment,
  count(*) AS n,
  avg(CASE WHEN outcome='tp_hit' THEN 1.0 ELSE 0.0 END) FILTER (WHERE outcome IN ('tp_hit','sl_hit')) AS win_rate,
  avg(CASE WHEN outcome='tp_hit' THEN 1.0 WHEN outcome='sl_hit' THEN -1.0 END) FILTER (WHERE outcome IN ('tp_hit','sl_hit')) AS expectancy_r
FROM nt_signal_sim
WHERE macro_forecast_label IS NOT NULL AND outcome IN ('tp_hit','sl_hit')
GROUP BY 1;
```

**Pass condition:** the LLM label's aligned-vs-opposed `expectancy_r` spread is **wider
than the price label's** on the same population, with `n ≥ 20` per arm per affected
symbol. Otherwise the price label stands and the LLM label stays a display-only field.

## 4. nobletradingapp UX

See `../nobletradingapp/docs/scope-llm-surfaces-dashboard.md` §2 for the full UI spec.
Summary: a **Macro panel** on the Analysis tab reading a new anon view
`v_talaria_macro_forecast` (per-symbol `thesis`, `current_macro_label`,
`current_trend_label`, `structure_bias`, `pivot_label`, `drivers`/`risks` chips).
Fail-open on null. This is the *display* consumer; it does not depend on the shadow→live
promotion above.

## 5. Open questions

1. **Label source when the LLM and price reads disagree** — in live, prefer the LLM label
   outright, or require both to agree (a stricter `context_alignment` variant)? Proposed:
   prefer the LLM label but log every disagreement for the first 7 days.
2. **`structure_bias` 14/33 coverage** — is the missing 19 a TA-fetch timing problem worth
   fixing, or acceptable (the field is short-term and non-authoritative)? Investigate
   before relying on it in the UI.
3. **Staleness threshold** — 36h proposed. Confirm against the nightly cadence
   (`45 2 * * *`) plus one missed-run tolerance.

## 6. Scope boundaries

**In scope:** the flag, the reader, the shadow wiring, the two `nt_signal_sim` stamp
columns, the promotion query, the anon view + macro panel.

**Out of scope:** changing `macro_forecast_agent.py`'s classification (owned by
`20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`); the `context_alignment` gate's own
mode flip (`context_alignment_gate_mode`, separate roadmap item id=45); the 200-day
price read itself.
