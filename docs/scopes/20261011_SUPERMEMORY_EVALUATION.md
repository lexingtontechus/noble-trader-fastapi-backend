# Supermemory Evaluation for Kairos — Memory Layer for Adaptive Trading

**Date:** 2026-10-11  
**Type:** Architecture evaluation (theoretical — no code change)  
**Status:** Evaluation complete  
**Relevant:** `docs/scopes/20261004_SELF_LEARNING_TRIAGE_SURFACE_SCOPE.md`, `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md`, `references/llm-decision-layer-integration.md`

---

## 1. Executive Summary

**Recommendation: Do not adopt Supermemory for Kairos's core trading memory.**

Kairos already has a **purpose-built, domain-specific memory system** that is quantitatively superior to a generic vector-graph for trading signal storage. Supermemory is designed for **agent episodic memory** ("user prefers X", "meeting at Y") — not for the statistical, time-series, regime-conditioned outcome data that a trading engine produces.

The real gaps in Kairos are **not memory gaps** — they are:

1. **Consumption gaps** — self-learning outputs (54 triage reports, 33 macro forecasts) have no reader
2. **Strategy rigidity** — one hardcoded signal logic for all 14 symbols and both directions
3. **EV chain blindness** — the fixed-weight logit pool cannot judge its own source coherence

All three are already scoped in existing worklog documents. Adding Supermemory would add a **third unread table** and a parallel memory infrastructure that duplicates what `RankScoreTracker` + `param_store` + `nt_bracket_reachability` already do — with less domain-specific statistical rigor.

---

## 2. What Supermemory Offers

| Capability | Description |
|---|---|
| Vector-graph memory | Facts extracted from content, linked by updates/extends/derives relationships |
| Automatic extraction | One input → many atomic facts, no hand-maintained schema |
| Dreaming | Background processing that groups related content into coherent units |
| Memory types | Facts (persist), preferences (strengthen), episodes (decay) |
| Forgetting | Time-based expiry, contradiction resolution, noise filtering |
| Scoping | `containerTag` for multi-tenancy isolation |
| API | `POST /v3/documents` (write), `POST /v4/search` (search), `POST /v4/profile` (always-on context) |
| Hermes integration | Available (skill + docs MCP) |

Supermemory excels at **narrative memory** — the kind of text-based, episodic, preference-driven knowledge that an AI assistant accumulates across conversations with a user.

---

## 3. What Kairos Already Has (Memory Systems Inventory)

Kairos is **not stateless**. It has six distinct memory/persistence mechanisms, each purpose-built for a specific trading function:

### 3.1 `RankScoreTracker` (kairos_rank.py) — Outcome Memory

```
Trailing decided outcomes → regime-cell-level P(TP|decided) with James-Stein shrinkage
Event-time decay (half-life ~3h for freshness lift)
Transition-type classification (dir_flip / vol_expand / vol_contract / both)
Restart-safe via to_dict/from_dict (regime_state_store)
```

This **is** the "retrieve and reweight past experiences under similar market regimes" the user describes — already implemented with domain-specific statistical rigor. It tracks:
- Per-symbol trailing TP/SL outcomes (exponential decay, 3h half-life)
- Per-(symbol, regime-cell) outcome means with shrinkage to global mean
- Regime transition types and event-time windows (pre/fresh/warm/stale/deep)

### 3.2 `param_store.py` — Parameter Memory

```
config/param_store.sqlite (local authority, WAL mode)
  ↕ parity_report() detects divergence
nt_dynamic_tier_state (Supabase mirror, audit trail)
```

Stores dynamic ATR, dynamic deadline, regime edge, geometry incubation parameters. Enforces single-source-of-truth with a nightly parity check that writes `nt_eod_actions` audit rows on divergence.

### 3.3 `nt_bracket_reachability` — Geometry Memory

Per-(symbol, vol_tier, distance_atr_bucket) bracket resolution statistics:
- `pct_hit_tp`, `pct_hit_sl`, `pct_expired`
- `median_hours_to_resolve`, `p75_hours`, `p90_hours`
- Rebuilt nightly by `_run_bracket_reachability()`

### 3.4 `nt_calibration_bias_by_bucket` — Calibration Memory

Per-cell calibration bias from qualified+supplied outcomes via `v_calibration_records_tiered`. Feeds the drift monitor and the 4h correction passes.

### 3.5 `signal_stream_cache.sqlite` — Dedup/Cooldown Memory

Per-cell content hash + cooldown tracking. Prevents the same signal from re-qualifying within the cooldown window.

### 3.6 `knowledge_index.py` — Codebase Semantic Memory

BGE-small embeddings (384-dim, Cloudflare Workers AI) for semantic retrieval over the codebase. Falls back to local MiniLM on failure. Used by the self-learning triage LLM to ground findings in code context.

### 3.7 `nt_signal_sim` — Shadow Signal Memory

Persists shadow stamps for offline discrimination measurement:
- `kairos_rank_score`, `kairos_rank_gate_shadow`
- `p_clef_dir`, `p_clef_wait` (Clef-Flash stamps)
- `strategy_archetype`, `clef_archetype*` (R4 stamps)
- `contaminated` boolean for era-clean filtering

---

## 4. Gap Analysis: What's Actually Missing

| User's stated need | Already covered by | Actual gap |
|---|---|---|
| Learn from historical market data | `RankScoreTracker`, `nt_bracket_reachability`, `nt_calibration_bias_by_bucket` | ✅ Covered |
| Past decision outcomes | `observe_outcome()`, `nt_signal_sim`, `nt_calibration_records` | ✅ Covered |
| Retrieve/reweight under similar regimes | `regime_cell()` + `shrink_cell_mean()` in `RankScoreTracker` | ✅ Covered |
| Improve directional accuracy | — | ❌ Signal is at/below chance (HMM 0.525 vs 0.5655 base rate) |
| Risk management | Kelly multipliers, dynamic ATR, calibration enforcement | ✅ Covered |
| Interpretability | `nt_macro_forecast` theses, `nt_eod_actions` audit rows | ⚠️ Produced but **not consumed** |
| Volatile environments | Regime detection, dynamic ATR, geometry bands | ✅ Covered |
| Adaptive strategy selection | — | ❌ Hardcoded "follow regime trend" for all symbols |

### The three real gaps (all already scoped):

1. **Consumption gap** — `nt_self_learning_reports` (54 rows) and `nt_macro_forecast` (33 rows) have **no consumer in any repo**. Scoped in `20261004_SELF_LEARNING_TRIAGE_SURFACE_SCOPE.md` and `20261004_MACRO_FORECAST_CONSUMER_SCOPE.md`.

2. **Strategy rigidity** — One signal logic for all assets/directions. Era-clean evidence: realized hit rate spans **3.4% (US30USD) to 23.2% (XAGUSD)** — a 7× spread. Scoped in `20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` → strategy registry + router.

3. **EV chain blindness** — The fixed-weight logit pool produces the same `p_win` from `(0.9, 0.2)` as from `(0.55, 0.55)`. Source disagreement is discarded on the way in. Scoped in `references/llm-decision-layer-integration.md` → coherence judge.

---

## 5. Why Supermemory Is the Wrong Tool

### 5.1 Statistical mismatch

Supermemory's fact extraction and graph relationships are designed for **narrative knowledge** ("Alex works on payments"). Kairos's memory needs are **quantitative and statistical**:

```
# What Kairos needs (RankScoreTracker._cell_p):
shrink_cell_mean(vals, global_mean, k=10.0)  # James-Stein shrinkage
# = (n * cell_mean + k * global_mean) / (n + k)

# What Supermemory would do:
"XAUUSD had 12 TP and 8 SL in med_high_vol_bull regime"  # a fact, not a statistic
```

The shrinkage estimator, event-time decay, and regime-cell bucketing are **domain-specific statistical operations** that a generic fact-extraction pipeline cannot replicate. Supermemory would store the raw outcomes; `RankScoreTracker` already stores the **statistically rigorous aggregation** of those outcomes.

### 5.2 Retrieval mismatch

Supermemory retrieves by **semantic similarity** ("find memories about gold trading"). Kairos needs retrieval by **exact regime cell** (`(symbol, vol_tier, direction)`) with **statistical shrinkage** toward the global mean. A vector search for "similar market regimes" would return semantically-related-but-statistically-different cells — the opposite of what a trading engine needs.

### 5.3 Infrastructure redundancy

Adding Supermemory would introduce:
- A new external API dependency (Supermemory API or self-hosted)
- A new `containerTag` scoping concept that doesn't map to Kairos's `(symbol, regime_cell)` keying
- A parallel write path that duplicates what `param_store` + `nt_*` tables already do
- A new failure mode (Supermemory outage → memory loss) that the existing local-first architecture is designed to avoid

### 5.4 The "stateless" misconception

The user's framing — "moving beyond stateless analysis to adaptive, context-aware trading" — implies Kairos is currently stateless. It is not. The `RankScoreTracker` + `regime_state_persist_mode` + `param_store` stack is a **stateful, restart-safe, parity-enforced** memory system. The problem is not lack of state — it's that the state is **not consumed** by the decision layer.

---

## 6. Where Supermemory *Could* Add Value

There is one narrow use case where Supermemory's capabilities align with Kairos's needs:

### 6.1 Narrative/Worklog Memory

Kairos produces narrative content that is currently **write-only**:
- `nt_macro_forecast.thesis/drivers/risks` — daily macro narratives (33 symbols, no reader)
- `nt_self_learning_reports.narrative` — LLM-generated root-cause analyses (54 reports, no reader)
- `nt_roadmap` — mechanism tracking with `review_condition` and `worklog_ref`

This is **textual, episodic, preference-adjacent** knowledge — exactly what Supermemory is designed for. A `containerTag` per symbol could store:
- "Gold's ID45 inversion pattern (counter-trend buys 38.2% vs pass 6.5%) was confirmed in era-clean data 2026-10-02"
- "regime_label_debounce_mode went live 2026-09-26; shadow table staleness is expected, not a fault"
- "Clef-Flash p_clef_dir measured at chance (AUC 0.506) — do not add as 4th P_win source"

**But**: Kairos already has `knowledge_index.py` (BGE-small embeddings) for semantic retrieval over text. The triage LLM already uses it to ground findings in code context. Adding Supermemory would be a **second** semantic retrieval system for the same corpus.

### 6.2 Cross-Session Parameter Change Memory

When a parameter is flipped (e.g., `dynamic_atr_mode: shadow → live`), the outcome of that change is currently tracked in `nt_roadmap` + `nt_eod_actions` but not in a **retrievable, decay-aware** format. Supermemory's episodic memory with time-based forgetting could track:
- "Flipped kairos_rank_weight to 0.28 on 2026-09-28; rank score IC improved from 0.042 to 0.067 over 200 episodes"
- "p_win_in_ev_mode went live 2026-10-03; bracket AUC degraded from 0.665 to 0.486 on raw rows"

**But**: This is a **nice-to-have**, not a critical gap. The existing `nt_roadmap` + worklog files already provide this audit trail in human-readable form.

---

## 7. The Right Path Forward

The user's goals — "improve directional accuracy, risk management, and interpretability in volatile environments" — are **already scoped** in existing worklog documents. The path forward does not require a new memory system:

### Priority 1: Fix the consumption gap (already scoped)

| Output | Consumer to build | Scope doc |
|---|---|---|
| `nt_macro_forecast` (33 rows) | `macro_filter.py` shadow source | `20261004_MACRO_FORECAST_CONSUMER_SCOPE.md` |
| `nt_self_learning_reports` (54 rows) | `v_self_learning_triage` view + admin panel | `20261004_SELF_LEARNING_TRIAGE_SURFACE_SCOPE.md` |
| `p_clef_wait` (AUC 0.61) | Suppress gate (shadow) | `references/llm-decision-layer-integration.md` |

### Priority 2: Build the strategy router (already scoped)

`config/strategy_registry.yaml` + `strategy_router.py` — replaces the hardcoded "follow regime trend with symmetric bracket" with a per-signal-selected archetype. Build sequence R0–R6 in `20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md`.

### Priority 3: Wire the EV coherence judge (already scoped)

Clef-Flash `noul` question over the EV chain's individual sources — "do the sources agree?" — as a **modifier**, not a source. Defined in `references/llm-decision-layer-integration.md`.

### Priority 4: If narrative memory is needed, extend `knowledge_index.py`

The existing BGE-small embedding infrastructure can be extended to index worklog files, macro theses, and triage narratives — without adding a new external API dependency.

---

## 8. Decision Matrix

| Criterion | Supermemory | Existing Kairos stack | Winner |
|---|---|---|---|
| Domain-specific statistics (shrinkage, decay, regime bucketing) | ❌ Generic fact extraction | ✅ `RankScoreTracker` purpose-built | **Kairos** |
| Retrieval by exact regime cell | ❌ Semantic similarity | ✅ Exact key lookup | **Kairos** |
| Restart-safe persistence | ❌ External API | ✅ Local SQLite + Supabase mirror | **Kairos** |
| Parity/divergence detection | ❌ None | ✅ `param_store.parity_report()` | **Kairos** |
| Narrative/text memory | ✅ Designed for this | ⚠️ `knowledge_index.py` (codebase only) | **Supermemory** (narrow) |
| Episodic decay | ✅ Time-based forgetting | ⚠️ `RankScoreTracker` half-life (domain-specific) | **Tie** |
| Cross-instance portability | ✅ Cloud-hosted | ✅ Supabase tables | **Tie** |
| Infrastructure complexity | ❌ New API dependency | ✅ Already deployed | **Kairos** |
| Alignment with existing conventions | ❌ New containerTag concept | ✅ Matches `(symbol, regime_cell)` keying | **Kairos** |

---

## 9. Verdict

**Do not adopt Supermemory for Kairos's core trading memory.**

The existing `RankScoreTracker` + `param_store` + `nt_bracket_reachability` + `nt_calibration_bias_by_bucket` stack is a **superior, purpose-built memory system** for quantitative trading signal storage. It already implements the "retrieve and reweight past experiences under similar market regimes" the user describes — with James-Stein shrinkage, event-time half-life decay, and exact regime-cell keying that a generic vector-graph cannot replicate.

The real gaps — consumption, strategy rigidity, EV chain blindness — are **already scoped** in existing worklog documents and do not require a new memory system to address.

**If narrative memory is needed** (worklog indexing, macro thesis retrieval, triage narrative search), extend the existing `knowledge_index.py` (BGE-small embeddings) rather than adding Supermemory as a parallel system.

---

## 10. References

- `docs/scopes/20261004_SELF_LEARNING_TRIAGE_SURFACE_SCOPE.md` — consumption gap for triage reports
- `docs/scopes/20261004_LLM_VALUE_AUDIT_AND_UTILIZATION_SCOPE.md` — LLM surface audit
- `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` — strategy router scope
- `references/llm-decision-layer-integration.md` — EV coherence judge + Clef-Flash evaluation
- `regime_platform/sweep_orchestrator/kairos_rank.py` — `RankScoreTracker` (outcome memory)
- `regime_platform/core/param_store.py` — parameter memory with parity enforcement
- `regime_platform/services/self_learning/knowledge_index.py` — codebase semantic retrieval
- `supermemory.ai` — evaluated product
