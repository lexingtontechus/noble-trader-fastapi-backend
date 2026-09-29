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
| **5** | `fetch_macro_forecast` / context_alignment consumers | **Explained** — see §6; not wired into gates |

### 6. Optional consumers (what “reading the table” means)

These do **not** require new LLM datasets. They only **read** `nt_macro_forecast`.

| Consumer | Reads | What it would do | Status |
|---|---|---|---|
| **`context_alignment`** (8th gate, shadow) | `current_macro_label` + `trend_label` | Already live as shadow using **price** `macro_label`/`trend_label`. Optional: also take **LLM** `current_macro_label` / `current_trend_label` as a third longitudinal source (only oppose when *all* available reads oppose). | Shadow only; EV untouched |
| **Strategic vs structure split** | `current_macro_label` vs `structure_bias` | Disagreement = regime tension (e.g. long-term bull + short-term bear on XAUUSD). Feature / size multiplier, not a hard veto. | Research |
| **`macro_edge` / `trend_edge`** | captured `macro_label` on `nt_signal_sim` | Already measures aligned vs opposed expectancy. Could add a join to `nt_macro_forecast.thesis`/`drivers` for explanations. | Read-only diagnostics |
| **P3 R-Layer** | `pivot_label`, `timesfm_gap`, P0 τ | Optional rank features after P2.3 lead-lag clears. | Base `rank_score` shipped |
| **Kelly context mult** | `structure_bias` + strength | Same pattern as existing macro Kelly mult — boost/cut when structure agrees with signal. | Not wired |

**Rule:** optional consumers are **reads only**. They never fork LLM writes. Missing `structure_*` stays null (do not treat as neutral).

### 7. P2.3 (2026-09-28) — machinery live, **underpowered**

`scripts/kairos_p23_narrative_leadlag.py` → `reports/20260928_p23_narrative_leadlag.md`

Only **2 unique labeled events** in `events[]` so far (both `no_change`) — the store is new and shallow. Rate tables will not be informative until `major_pivot` events accumulate (or a historical label backfill). **P2.3 stamps stay gated.** Re-run weekly as `events[]` grows.

### 8. Local LLM — correct secret setup (2026-09-28)

`llm_client` prefers **one** of two local modes (never both required):

| Mode | Secrets | HF download to `hf_cache`? |
|---|---|---|
| **A. HTTP server** (llama.cpp / vLLM / TGI) | `LOCAL_LLM_BASE_URL=http://127.0.0.1:<port>` (full URL). `LOCAL_LLM_MODEL` = name **the server** was started with (e.g. `LiquidAI/LFM2.5-1.2B-Instruct` or `local`). | **No** in our process — the **server** owns the weights. |
| **B. In-process transformers** | **Unset** `LOCAL_LLM_BASE_URL`. `LOCAL_LLM_MODEL=LiquidAI/LFM2.5-1.2B-Instruct` | **Yes** — `from_pretrained` pulls into `HF_HOME` / `hf_cache` on first run. |

**Studio pattern (same as TimesFM `server.py` :8002):** run **`llm_server.py` :8004** (orchestrator owns 8003).

```bash
# on_start.sh / start.sh / background.sh now launch this
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe llm_server.py
# default model: XHToken/Spark-X2.5-1.7B-INT8  (override LOCAL_LLM_MODEL)
```

| Secret | Value |
|---|---|
| `LOCAL_LLM_BASE_URL` | `http://127.0.0.1:8004` |
| `LOCAL_LLM_MODEL` | `XHToken/Spark-X2.5-1.7B-INT8` (8-bit JSON-safe; Q4 GGUF only if RAM tight) |
| `LOCAL_LLM_API_KEY` | optional shared secret (`X-API-Key`) |
| `HF_HOME` | Studio `…/hf_cache` — **honored**, same as TimesFM (do not force project `.cache/huggingface`) |

Weights download once into Studio `hf_cache`. `trust_remote_code=1` for Spark.  
**Keep** `models--sentence-transformers--all-MiniLM-L6-v2` — `knowledge_index.py` still uses it.

Weights download once into `.cache/huggingface` (same as TimesFM HF_HOME).

**Wrong (as of 2026-09-28):** `LOCAL_LLM_BASE_URL=localhost` (bare host — code now **ignores** it) and `LOCAL_LLM_MODEL=LFM-2-1.2B` (not an HF id). Correct ids: `LiquidAI/LFM2.5-1.2B-Instruct` (or GGUF + llama.cpp server).

Smoke: `scripts/local_llm_smoke.py`. Fallback remains NIM if local fails.

### 9. Schedules — who runs what

| Job | Who | When | Command / id |
|---|---|---|---|
| **Macro classify (full universe)** | **APScheduler in orchestrator** (primary) | **Daily 02:45 UTC** | `_run_macro_forecast_agent` |
| Macro classify (backup) | Studio **Deploy** schedule (optional) | Daily 02:45 UTC | `scripts/run_macro_forecast_job.py` |
| P2.3 narrative lead-lag | **Not in APScheduler** | Manual or Studio Deploy **weekly** | `scripts/kairos_p23_narrative_leadlag.py` |
| Light sweeps / EOD / kairos-daily | APScheduler | existing crons | unchanged |

You do **not** need Studio Deploy if the orchestrator is running 24/7 — APScheduler already fires 02:45. Deploy is for when the Studio box is stopped (Lightning auto-starts to run the command).

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
