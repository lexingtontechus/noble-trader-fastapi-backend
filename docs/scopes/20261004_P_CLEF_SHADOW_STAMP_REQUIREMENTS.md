---
title: "p_clef Shadow-Stamp Requirements"
author: "Hermes"
created: "2026-10-04"
status: "scope — requirements for the shadow stamp build"
tags: [kairos, clef-flash, llm, p_clef, shadow, schema, scope]
related:
  - docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md
  - docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md
---

# p_clef Shadow-Stamp Requirements

Companion to `20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`. That doc decides *why* and
*where*; this one fixes the **exact contract** the build implements.

## 1. What `p_clef` is — and is not

**Is:** a **shadow research stamp** recording what the Clef-Flash structured decision
model answers for a signal's market state, so its discrimination can be measured offline.

**Is not:**
- an EV source — it is **never** added to `P_WIN_WEIGHTS_FULL`, never passed to
  `compute_ev()`, never touches `ev_result`.
- a gate — it never affects `qualified`, `published`, `gate_outcome`, Kelly, or SL/TP.
- a directional classifier over price — it consumes the signal's **own** context
  (symbol, direction, regime, bracket), not a re-derivation of direction from the series.

The single rule: **`p_clef` is written and never read back into any decision.**

## 2. Column contract

Migration adds three nullable columns to `nt_signal_sim` (all research-only, no
constraints beyond type):

| Column | Type | Meaning | Range |
|---|---|---|---|
| `p_clef_dir` | `double precision` | P(model agrees with the signal's direction) = `probabilities[signal_side]` | 0–1, NULL when unavailable |
| `p_clef_wait` | `double precision` | P(model says "wait") = `probabilities.wait` | 0–1, NULL when unavailable |
| `p_clef_symbol` | `text` | model slug actually used (e.g. `@cf/cloudflare/clef-flash`) | NULL when unavailable |

**Why these three, not one:**
- `p_clef_dir` is the **discrimination** column — it is the one compared against
  `p_win_dir` (AUC on bracket outcome). It must be direction-relative so buy and sell
  signals are comparable.
- `p_clef_wait` is the **abstention** column — a decision model that says "wait" on most
  signals is not discriminating, and without this column that would be invisible.
- `p_clef_symbol` is **provenance** — the model slug, so a future model swap is
  distinguishable in the data without a schema change.

**Deliberately NOT stamped:** the full probability vector (a JSONB blob). The two
scalars above are what the eval needs; a blob invites later ad-hoc reads that drift from
the contract.

**Nullable, no default.** Rows captured before the build, or when the mode is `off` or
the call fails, carry NULL — the same convention every other shadow stamp in this table
uses (`raw_p_regime`, `timesfm_q_*`, …). The eval query filters `WHERE p_clef_dir IS NOT NULL`.

## 3. Question schema (what is sent)

Clef-Flash returns **one logit per option per question, softmax-normalised** — no text.
The request body is `{state, questions}`:

```json
{
  "state": {
    "symbol": "XAUUSD",
    "signal": "buy",
    "regime": "med_high_vol_bull",
    "regime_conf": 0.81,
    "entry_price": 2650.12,
    "stop_loss": 2630.44,
    "take_profit": 2698.10,
    "tp_distance_atr": 4.2,
    "sl_distance_atr": 3.0,
    "macro_label": "bull",
    "trend_label": "trending_up",
    "p_win_dir": 0.34
  },
  "questions": {
    "direction": {
      "type": "choice",
      "instructions": "Given this signal's own context, does the setup favour the stated direction, the opposite, or neither?",
      "criteria": { "buy": "…", "sell": "…", "wait": "…" }
    },
    "confidence": {
      "type": "score",
      "instructions": "How confident is the direction read? (0 = none, 1 = high)"
    }
  }
}
```

**Mapping to columns:**
- `p_clef_dir = answers.direction.probabilities[state.signal]` — the probability mass on
  the signal's **own** side (buy→`buy`, sell→`sell`). Direction-relative by construction.
- `p_clef_wait = answers.direction.probabilities.wait`.
- `p_clef_symbol = model`.

**Why `choice(buy, sell, wait)` and not `noul`:** a binary agree/disagree cannot express
"the opposite is favoured", which is the informative case. Three-way choice gives the eval
a proper distribution, and `wait` is a real answer, not a parse failure.

**Input discipline:** the `state` block carries the signal's **already-computed** context
only. No raw candle array, no renko brick series — the model is not asked to re-derive
anything the deterministic engine already produced. This is the §1 rule
(`20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md`) enforced at the payload level.

## 4. Stamp path (fail-open, shadow-only)

```
orchestrator._process_symbol  (signal_is_qualifiable, source == "sweep-light")
   └─ self._clef_flash_stamp(symbol, trade_signal, regime_label, regime_conf, _gm, macro_label, trend_label, _p_win_dir)
        └─ if get_clef_flash_mode() == "off": return (None, None, None)
        └─ clef_flash.decide(state)  ──► None on ANY failure
        └─ return (p_clef_dir, p_clef_wait, model)
   └─ _build_signal_sim_payload(..., p_clef_dir=…, p_clef_wait=…, p_clef_symbol=…)
```

**Guards (identical to every other shadow stamp):**
1. `source != "sweep-light"` → skip (heavy sweep does not stamp).
2. `get_clef_flash_mode() == "off"` → skip (no HTTP call).
3. Wrap in `try/except`; on any exception or `None` return → the three values are None.
4. The HTTP client returns `None` on non-2xx, timeout, or malformed body — the caller
   never raises.

**Async:** the stamp is awaited before the payload is built (the values must be in the
payload), but it is **not** on the qualification critical path — it runs after the gate
outcome is known and cannot change it. A slow Clef-Flash call delays the capture write,
not the signal. Timeout is short (10s) so a hung worker cannot stall the sweep.

## 5. Feature flag

```yaml
# clef_flash_mode: off | shadow | live
#   shadow (default): call /decision for qualifiable light-sweep signals, stamp
#           p_clef_dir/p_clef_wait/p_clef_symbol on nt_signal_sim. Research only —
#           never in P_WIN_WEIGHTS_FULL, never touches ev_result/qualified/kelly.
#   live:   reserved for the classifier cutover (kairos_pivot/macro_forecast call
#           sites). Does NOT change the shadow stamp — the stamp is always shadow.
#   off:    no HTTP call, no stamp.
clef_flash_mode: shadow
```

`_DEFAULTS["clef_flash_mode"] = "shadow"`, `get_clef_flash_mode()` resolves
`off|shadow|live` with an unknown value falling back to `shadow` (same shape as
`get_kairos_mode`).

## 6. Evaluation gate (what the stamp is FOR)

Run after ≥200 stamped rows:

```sql
-- Does p_clef_dir discriminate bracket outcomes better than p_win_dir?
SELECT
  count(*) FILTER (WHERE p_clef_dir IS NOT NULL) AS n_clef,
  count(*) FILTER (WHERE p_clef_dir IS NOT NULL AND outcome IN ('tp_hit','sl_hit')) AS n_decided
FROM nt_signal_sim
WHERE ts > '2026-10-04';

-- AUC(p_clef_dir → tp_hit) vs AUC(p_win_dir → tp_hit), episode-deduped
-- (first row per symbol/side/hour — the nt_signal_sim independence rule).
```

**Pass condition:** `AUC(p_clef_dir) > AUC(p_win_dir)` on the same decided, episode-deduped
population, with a bootstrap CI that excludes 0.5.

**Fail → keep display-only.** A decision model that cannot beat the existing bracket
probability on the same population is not worth a live slot — the exact standard the
HMM and first-passage both failed.

## 7. What the build delivers

| Artefact | Path |
|---|---|
| Migration | `supabase/migrations/194_nt_signal_sim_p_clef.sql` |
| Caller | `regime_platform/services/decision/clef_flash.py` |
| Flag | `config/feature_flags.yaml` + `feature_flags.py` `_DEFAULTS` + `get_clef_flash_mode()` |
| Schema whitelist | `supabase_client.py::NT_SIGNAL_SIM_COLUMNS` (+3) |
| Stamp | `orchestrator.py::_clef_flash_stamp` + `_build_signal_sim_payload` params + call site |
| Tests | `tests/test_clef_flash.py` |

**Out of scope for this build:** the classifier cutover at `kairos_pivot.py` /
`macro_forecast_agent.py` (that is `clef_flash_mode: live`, a later step gated on §6);
the narrative-vs-tape shadow question; any UI.
