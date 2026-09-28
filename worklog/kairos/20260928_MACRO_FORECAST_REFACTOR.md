# nt_macro_forecast refactor — uniform multi-asset LLM store

**Date:** 2026-09-28 · **Type:** assessment + build · **Parent:** `20260928_P2_PIVOTS_COMPLETE.md`  
**Decision:** refactor **now** (migrations 181–182) so the table is one LLM surface for every `nt_symbol`, not a commodity-only forecast dump.

---

## 0. Assessment (as of 2026-09-28, pre-refactor)

| Issue | Evidence | Risk if not fixed |
|---|---|---|
| **Commodity-only coverage** | 12 rows: B, RB, VGT, FCPO, CT, HO, JX, SB, RR, ZC, ZW, SI | FX/equity/index/crypto (`XAUUSD`, `EURUSD`, `US500`, …) have **no** row |
| **Narrative is prose-only** | `narrative` free text; no `drivers` / `risks` / `thesis` | Every consumer re-parses with another LLM |
| **Strategic vs short-term mixed** | One `current_macro_label` (12m) | No clean “structure now” vs “macro theme” split |
| **Single latest pivot** | 181 `pivot_*` scalars | No event memory for lead-lag |
| **No asset_class / source_doc** | — | Multi-source docs hard to audit |
| **Prompt is classify-only** | `_SYSTEM_PROMPT` trend/direction/strength | Does not deconstruct narrative |

**What is already right:** one table keyed by `symbol` FK `nt_symbol`; one upsert; `ForecastSource` registry; `active` irrelevant to classification; calendar raw in `.cache/kairos/econ_calendar.json` (not a second LLM store).

---

## 1. Refactor shipped (code)

| Piece | Change |
|---|---|
| `supabase/migrations/182_nt_macro_forecast_uniform.sql` | `thesis`, `drivers`, `risks`, `horizon_days`, `structure_*`, `events`, `source_doc`, `asset_class` |
| `macro_forecast_agent` | Prompt **deconstructs** narrative → structured JSON; `_clean_drivers` / `_clean_risks`; `_attach_market_structure` from latest `nt_sweep_result` TA; `source_doc` on each row |
| `kairos_pivot.build_pivot_prompt` | Optional **market-structure note** in pivot LLM prompt (TA cannot alone force `major_pivot`) |
| `supabase_client` allowlist | All 182 columns |
| Tests | 32 green (`test_macro_forecast_agent`, `test_kairos_pivot`) |

### Target row shape (uniform for every `nt_symbol`)

```text
symbol, asset_class, source_doc, model_provider, updated_at
── strategic (12m LLM) ──────────────────────────
current_trend_label, current_macro_label, current_macro_strength
narrative, thesis, drivers[], risks[], horizon_days
── short-term (computed / TA) ───────────────────
structure_bias, structure_strength, structure_note, structure_as_of, structure_source
── events (calendar + LLM pivots) ───────────────
pivot_*, events[]
```

Same columns for forex, metals, indices, equities, crypto. Only the **doc** (`ForecastSource`) and optional `asset_class` differ.

---

## 2. Market-structure × macro LLM (the augment you asked for)

| Layer | Input | Output | Cadence |
|---|---|---|---|
| Strategic | `docs/LLM/*_12m_*.md` + date | `current_*`, `thesis`, `drivers`, `risks` | Daily agent |
| Structure | `nt_sweep_result` TA (RSI, MACD, ADX) | `structure_bias` / note | Same job, cheap compute |
| Events | TDVA calendar + structure note | `pivot_*`, `events[]` | Same job, few LLM calls |

**LLM still required for narrative deconstruction** — structure is numeric and stays non-LLM. Pivot labels use structure as **context** so routine prints in calm tape stay `no_change`.

---

## 3. Gaps still open

1. **Universe:** need `ForecastSource` docs (or a generic “symbol brief”) for **XAU/XAG/FX/US500/US30/AAPL/…** — 21 active + metals. Until then those rows stay empty.
2. **`asset_class` fill:** join `nt_symbol` at upsert (not coded yet — add one select).
3. **`events[]` history:** currently one latest pivot; append last N on each run (safe merge by `as_of`).
4. **Migration apply:** **181 + 182** must run on Supabase before next agent write.
5. **OpenRouter 402** — NIM-only until credits.

---

## 4. What’s next (recommended order)

| # | Action | Status |
|---|---|---|
| **1** | Apply migrations 181 + 182 | **Done (user 2026-09-28)** |
| **2** | ForecastSource for non-commodity actives | **Done** — `docs/LLM/existing_symbols_12m_forecast_model_2027.md` + registry |
| **3** | P3 R-Layer ranker | **Done** — base `rank_score` wins within-cell; `timesfm_gap` optional |
| **4** | Narrative lead-lag vs `dir_flip` | Open |
| **5** | `fetch_macro_forecast` / context_alignment consumers | Open |

**Do not** add a third LLM table, side-car JSON, or per-asset-class schemas. Extend this one.

---

## 5. Stage map (updated)

### 5.1 Ops: Studio schedule (Lightning “agent” = script)

Per [Lightning Agents](https://lightning.ai/docs/platform/build/agents.md): Deploy → frequency + command. Not an autonomous trader.

```bash
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/run_macro_forecast_job.py
```

| Env | Role |
|---|---|
| `LOCAL_LLM_MODEL` or `LOCAL_LLM_BASE_URL` | **Preferred** local instruct (LFM / Qwen 1–7B) |
| `NVIDIA_NIM_API_KEY` | Cloud fallback |
| `OPENROUTER_API_KEY` | Optional (often 402 without credits; `P2_SKIP_OPENROUTER=1` default) |
| `SUPABASE_URL` / `SUPABASE_KEY` | Upsert `nt_macro_forecast` |

Also scheduled in-process at 02:45 UTC (`orchestrator` → `_run_macro_forecast_agent`). Studio Deploy is a **backup/standalone** path.

**Also filled:** `asset_class` (join `nt_symbol`) · `events[]` (last 8 pivot records).

| Phase | Status |
|---|---|
| P0 half-life | Done (`dir_flip` h≈6h) |
| P1 TimesFM / covariates | P1.1–1.2 done; quantiles optional |
| P2 pivots | **Schema + pipeline done**; stamps gated; universe coverage open |
| P3 ranker | **Next build** after migrations |
| P4 LLM extract / news | Later |
