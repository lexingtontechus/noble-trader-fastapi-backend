---
title: "Clef-Flash as an EV-Chain Decision Layer — defining the state and the questions"
author: "Hermes"
created: "2026-10-05"
status: "scope — DEFINITION. The value is a hypothesis to measure, not a claim."
tags: [kairos, clef-flash, jev, decision-model, ev-engine, scope]
related:
  - docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md
  - docs/scopes/20261005_CLEF_FLASH_CUTOVER_STRATEGY.md
  - regime_platform/services/statistics/ev_engine.py
---

# Clef-Flash as an EV-Chain Decision Layer

## 0. Why the earlier work missed

Clef-Flash (and Jev, its reference implementation) is a **decision model**: you send
a `state` and typed `questions` (`noul` / `choice` / `score`), and it returns a
calibrated probability per option in one forward pass. The reference projects use it
exactly that way — `sosopop/jev_stock` feeds a full past-only market state and asks
`up`/`flat`/`down` as a `choice`; `Gamma-Software/jev-signals-lab` sends one snapshot
and **12 independent typed questions**, then branches on the answers in a rule engine.

Everything I built so far used it as a **text classifier** — feeding it a signal's own
context and asking it to *re-state* a label the pipeline already computed. That is not
a decision; it is a redundant re-derivation, and it correctly measured as worthless.

**The gap:** the EV engine is a **fixed-weight logit pool**. It averages sources. It
structurally **cannot** express a judgment *about* those sources — whether they agree,
whether the combination is coherent, whether the geometry fits the regime. That is a
decision, and it is the one thing a weighted average cannot produce.

## 1. What the EV chain already computes (do not ask Clef for these)

From `ev_engine.py::compute_ev` + the orchestrator's post-blend layer, per signal:

| Output | Source |
|---|---|
| `p_win` | logit pool of `p_regime` (5/14), `p_markov_hold_n` (4/14), `p_timesfm` (5/14), renormalised over available |
| `p_regime`, `p_timesfm`, `p_markov_hold_n` | the three source values |
| `p_win_dir`, `p_resolve`, `p_win_resolve` | bracket-probability layer |
| `ev`, `ev_per_dollar`, `rr`, `aggression` | geometry + p_win |
| `regime_label`, `regime_conf`, `macro_label`, `trend_label` | longitudinal filters |
| `kairos_rank_score`, `event_window`, `transition` | Kairos stamps |
| `tp_distance_atr`, `sl_distance_atr`, `bracket_bucket`, `bracket_vol_tier` | geometry |

**None of these may be the Clef question.** Asking "will it win?" re-derives `p_win`;
asking "what's the direction?" re-derives the HMM. Both are already computed, and both
already measured at/below the naive base rate (`20261004_LLM_VALUE_AUDIT…` §1).

## 2. The one thing the chain cannot produce: **coherence**

A fixed-weight logit pool with `p_regime=0.9, p_timesfm=0.2` produces the *same* `p_win`
as `p_regime=0.55, p_timesfm=0.55` when the weights are equal. It has **no read on
disagreement**. And the documented failure — `p_win_dir` top decile predicting ~0% TP,
bracket AUC 0.486 — is exactly what you'd expect if the blend is confidently averaging
sources that don't agree.

**Coherence is the decision.** It is not a number the chain has, it is a judgment *about*
the chain's own inputs, and it is the natural thing a decision model reads from a state.

## 3. The state (the entire chain, as one JSON object)

```json
{
  "signal": {
    "symbol": "XAUUSD", "direction": "sell",
    "entry_price": 2650.12, "stop_loss": 2670.44, "take_profit": 2601.10,
    "tp_bricks": 6, "sl_bricks": 3, "brick_size": 0.5
  },
  "ev_chain": {
    "p_win": 0.4505, "p_win_dir": 0.2857, "p_resolve": 1.0, "p_win_resolve": 0.2857,
    "ev": 0.31, "ev_per_dollar": 0.10, "rr": 2.0, "aggression": "mid",
    "sources": { "p_regime": 0.81, "p_timesfm": 0.22, "p_markov_hold_n": null }
  },
  "regime": {
    "regime_label": "med_high_vol_strong_bear", "regime_conf": 0.81,
    "macro_label": "bear", "macro_strength": 0.44,
    "trend_label": "trending_down", "trend_strength": 0.10
  },
  "geometry": {
    "tp_distance_atr": 2.40, "sl_distance_atr": 0.90,
    "bracket_bucket": "2-4", "bracket_vol_tier": "med_high_vol"
  },
  "market_context": {
    "rel_volume_1h": 1.3, "clv_1h": 0.62, "efficiency_24h": 0.41,
    "atr_ratio_run": 1.05, "spread_bps": 4.2
  },
  "rank": { "kairos_rank_score": 0.61, "event_window": "fresh", "transition": "dir_flip" }
}
```

Every field is already on `nt_signal_sim` at capture time — no new computation.

## 4. The questions (typed, decision-shaped, none a re-derivation)

Following the `jev-signals-lab` multi-question pattern — several independent typed
questions over one state, answers consumed by a rule/modifier in code:

| # | Type | Question | Returns | Used for |
|---|---|---|---|---|
| Q1 | `noul` | "Do the EV sources (regime, TimesFM, bracket) agree on this setup's direction and strength?" | P(true) | **coherence read** — the core |
| Q2 | `score` 0–3 | "How well does this bracket geometry fit this regime?" | expected score + dist | geometry-fit modifier |
| Q3 | `choice` | "What is the dominant risk here?" `{source_conflict, geometry_mismatch, regime_fresh, none}` | probabilities | risk tag / routing |
| Q4 | `noul` | "Would a disciplined trader act on this signal as-is, or stand aside?" | P(true) | the abstain decision |

**Q1 is the primary.** Q2–Q4 are the supporting set (the `jev-signals-lab` shape); they
are cheap (one call, ≤20 questions) and give the rule engine material to branch on.

## 5. The return, and what it is FOR

The answers do **not** replace `p_win`. They feed a **modifier**, exactly like the
existing calibration/rank machinery:

```
coherence   = answers.Q1.noul                      # 0-1
geometry_fit = answers.Q2.score / 3                 # 0-1
stand_aside  = answers.Q4.noul                      # 0-1

# candidate modifier (shadow first):
p_win_adjusted = p_win * f(coherence, geometry_fit)  # shrink when sources conflict
```

The hypothesis: **low-coherence rows underperform their `p_win`** — i.e. coherence
discriminates the outcomes `p_win_dir` fails to. If true, it is a calibration input the
fixed-weight pool cannot compute. If false, it is retired.

## 5a. Where `nt_macro_forecast` fits — the narrative half of the state

**Live data (2026-10-05), all 14 active symbols have both tables:**

| Symbol | `nt_macro_forecast` (LLM, 12m) | `nt_macro_regime` (price, 200d) | agree |
|---|---|---|---|
| XAUUSD | bull | bear | ✗ |
| XAGUSD | bull | bear | ✗ |
| XAUAUD | bull | bear | ✗ |
| AAPL | neutral | bull | ✗ |
| US30USD | bull | neutral | ✗ |
| US500 | neutral | bull | ✗ |
| BTCUSD | bull | bull | ✓ |
| EURUSD/GBPUSD/USDCAD/USDCHF/USDJPY | neutral | neutral | ✓ |

**6 of 14 disagree, persistently** — and nothing consumes the disagreement today.

The EV chain is **numbers**; `nt_macro_forecast` is **text** (`thesis`, `drivers`,
`risks`). A fixed-weight logit pool cannot read prose. **A decision model can** — it is
the only component in the platform that can consume the narrative. Three roles:

1. **State content.** The forecast's `thesis`/`drivers`/`risks`/`current_macro_label`
   join the state next to the numeric chain. Clef then judges a **cross-domain** question
   the pool cannot: *does the strategic story match the tape?* ("Gold closes above 2800 on
   real-rate cuts" vs `p_regime` = `med_high_vol_strong_bear`).
2. **A third longitudinal read.** `context_alignment.py` already hand-codes a crude
   version — it rejects only when **both** `macro_label` **and** `trend_label` oppose
   (binary, hardcoded). The decision layer generalizes it: the forecast adds an
   independent strategic read that can conflict with the price-macro and the trend.
3. **The reader `nt_macro_forecast` never had.** `20261004_MACRO_FORECAST_CONSUMER_SCOPE.md`
   proposed wiring it into `macro_filter.py` (numeric→numeric) — the wrong socket. The
   decision layer is the natural consumer: it is the only thing that can read the
   narrative. This resolves the "no consumer" problem without a numeric integration.

**State block added:**
```json
"macro_narrative": {
  "forecast_label": "bull", "forecast_strength": 0.80, "forecast_trend": "trending_up",
  "thesis": "XAUUSD will close above 2,800 by end of Q4 2026",
  "drivers": [{"name": "real-rate cuts", "stance": "bull", "confidence": 0.8}],
  "risks": [{"name": "real-yield spike", "severity": 0.4, "direction": "bear"}],
  "pivot_label": "no_change", "updated_at": "2026-10-04T02:45Z",
  "price_macro_label": "bear"
}
```

**Question added — Q5 (`noul`):** *"Does the strategic narrative agree with the realized
price regime and the current trade direction?"* → P(agree). The text-vs-tape read a
numeric engine fundamentally cannot produce.

**Risks, stated:**
- **Slow-moving** — daily forecast vs per-tick signal. Strategic context, not a timing read.
- **Partial circularity** — both forecast and Clef are language models. The value is *not*
  "two LMs agree"; it is **text vs numbers** (does the story match `p_regime`/`trend`/
  `p_timesfm`). That comparison is real and cross-domain.
- **`pivot_label` is `no_change` on all 33** today — the pivot axis is uninformative; the
  `current_macro_label` disagreement is the live signal.

## 6. The endpoint (binary, same discipline as the cutover strategy)

| Stage | What | Gate |
|---|---|---|
| **D0** | Build `clef_flash.decide_state(state, questions)`; shadow-stamp `p_clef_coherence` + `p_clef_stand_aside` + `p_clef_geometry_fit` on `nt_signal_sim` | — |
| **D1** | ≥200 decided, episode-deduped rows. Split by coherence tertile; compare realized TP rate | does low-coherence underperform? |
| **D2** | If yes → shadow modifier `p_win_adjusted`; measure Brier/ΔD vs raw | does it improve calibration? |
| **D3** | **PROMOTE** (modifier live) or **RETIRE** (`clef_flash_mode: off`) | — |

**Endpoint:** either coherence is a live calibration modifier with measured Brier
improvement, or the whole Clef thread is retired and the worker kept for embeddings.
No limbo. This **supersedes** the classifier-cutover strategy
(`20261005_CLEF_FLASH_CUTOVER_STRATEGY.md`) — that path answers "is the label parse-proof",
which is a reliability question; this one asks the value question, which is the one you
actually want answered.

## 7. What is genuinely uncertain (state it, don't hide it)

- **Coherence may not discriminate.** The HMM and first-passage both measured below the
  base rate. A decision model reading a state that *contains* those sub-base-rate sources
  may inherit the same ceiling. That is the risk, and D1 is what settles it.
- **The state may be too thin.** Coherence needs the sources to actually conflict often;
  if they rarely do, Q1 has nothing to read.
- **Cost.** One call per qualifiable signal (~24/day at light-sweep cadence) — negligible.

## 8. Not in scope

- Replacing `p_win` / `ev` / any gate — Clef is a modifier candidate, never a source.
- Asking Clef for direction/trend/regime (re-derivation — retired).
- The `p_clef_dir` research stamp already shipped (separate, keep as-is).
