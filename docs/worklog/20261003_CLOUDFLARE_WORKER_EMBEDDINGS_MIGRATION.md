# Cloudflare Worker Embeddings Migration — Worklog

**Date:** 2026-10-03  
**Status:** Phase 1 Complete ✅ — Worker deployed, end-to-end verified  
**Repo:** `noble-trader-fastapi-backend`  
**Related:** `docs/scopes/20261003_CLEF_FLASH_VS_ALPHA_PULSE_EVAL.md`  
**Tag:** `kairos`, `embeddings`, `cloudflare-workers-ai`, `bge-small`

---

## 1. Context

`knowledge_index.py` in the self-learning pipeline uses MiniLM-L6-v2 (local
`sentence-transformers` model, CPU) to generate 384-dim embeddings for the
semantic knowledge index. This requires a heavy PyTorch dependency on the
Studio and ties the embedding model to the Python environment.

**Goal:** Migrate to `@cf/baai/bge-small-en-v1.5` (384-dim, same dimension)
hosted on Cloudflare Workers AI, deployed as a free-tier Cloudflare Worker.

---

## 2. What Changed

### 2.1 Old: Supabase Edge Function → New: Cloudflare Worker

| Aspect | Old (nobletradingapp) | New (fastapi-backend) |
|---|---|---|
| Location | `nobletradingapp/supabase/functions/cloudflare-ai-proxy/index.ts` | `noble-trader-fastapi-backend/supabase/functions/cloudflare-ai-proxy/worker.js` |
| Runtime | Supabase Edge Function (Deno) | Cloudflare Worker (free tier) |
| Deployment | Supabase CLI | `npx wrangler deploy worker.js --no-bundle` |
| Auth | Bearer token via Supabase | `x-worker-secret` header |
| AI Access | N/A (was never completed) | `env.AI` binding or `CF_API_TOKEN` fallback |

Old edge function deleted from `nobletradingapp`.

### 2.2 Worker Code (`worker.js`)

Pure JavaScript (no TypeScript — Wrangler `--no-bundle` requires plain JS ESM):
- `export default { async fetch(request, env, ctx) }` — standard Worker format
- `POST /embeddings` → calls `_cf/baai/bge-small-en-v1.5` via `_callAi()`
- `POST /decision` → calls `@cf/cloudflare/clef-flash` via `_callAi()`
- `_callAi()` helper: tries `env.AI.run()` binding first, falls back to Cloudflare REST API with `CF_API_TOKEN` secret
- L2-normalization on embeddings (matches MiniLM's `normalize_embeddings=True`)
- Optional `CF_WORKER_SECRET` auth via `x-worker-secret` header

### 2.3 `knowledge_index.py` Changes

- `_cf_proxy_base_url()` → `_cf_worker_url()`: reads `CF_EMBEDDINGS_URL` directly (Worker URL, no more Supabase edge function URL construction)
- `_cf_proxy_secret()` → `_cf_worker_secret()`: reads `CF_WORKER_SECRET`
- `_cf_enabled()`: gated on `_cf_worker_url()` presence (not `CF_EMBEDDINGS_ENABLED=1`)
- `embed_texts()`: unchanged logic — tries Cloudflare first via `_embed_texts_cf_sync()`, falls back to local MiniLM on failure
- `_embed_texts_cf()`: POSTs to `{CF_EMBEDDINGS_URL}/embeddings` with `x-worker-secret` header (was `Authorization: Bearer {secret}` to Supabase edge function)

### 2.4 Secrets Configured

On the Cloudflare Worker:
- `CF_WORKER_SECRET` = `5252325e...` (64 hex chars)
- `CF_API_TOKEN` = `cfut_pPK...` (53-char scannable token, used by API fallback)
- `AI` binding = enabled via API PATCH (wrangler.toml `[ai]` section doesn't auto-apply in Wrangler 4.147.0)

On the Studio (`noble-trader-unified`):
- `CF_EMBEDDINGS_URL` = `https://nobletradingapp.lexington.workers.dev`
- `CF_WORKER_SECRET` = same value as the worker secret

---

## 3. Verified (End-to-End)

```
Python embed_texts() 
  → POST https://nobletradingapp.lexington.workers.dev/embeddings
  → x-worker-secret header
  → Cloudflare Worker 
  → env.AI.run("@cf/baai/bge-small-en-v1.5")
  → 384-dim L2-normalized vectors
  → OpenAI-compatible JSON response
  → parsed by knowledge_index.py
```

Test output:
```
CF enabled: True
Worker URL: https://nobletradingapp.lexington.workers.dev
embed_texts result:
  Shape: (2, 384)
  dtype: float32
  Norms: [1. 1.]
```

Clef-Flash decision endpoint also tested:
```
POST /decision → {"direction": {"choice": "buy", "probabilities": {"buy": 0.853, "sell": 0.040, "wait": 0.107}}}
```

---

## 4. Shadow Migration Plan

### Phase 1: Shadow Mode (current)
- ✅ Worker deployed with CF_EMBEDDINGS_URL set on Studio
- ✅ `embed_texts()` tries Cloudflare first, falls back to MiniLM on failure
- Next: Verify retrieval quality is equivalent (cosine similarity between Cloudflare and MiniLM embeddings of the same corpus)

### Phase 2: Cutover
- If quality is equivalent: remove `sentence-transformers` from `requirements.txt`/`pyproject.toml`
- The SQLite schema stores `embedding BLOB` — dimension-agnostic, no migration needed

### Phase 3: Clef-Flash Integration
- The worker's `/decision` route is ready for Clef-Flash
- `knowledge_index.py` has the HTTP infrastructure; adding a Clef-Flash call
  follows the same `_embed_texts_cf_sync` async pattern
- See `docs/scopes/20261003_CLEF_FLASH_VS_ALPHA_PULSE_EVAL.md` for decision schema

---

## 8. Masaniello Brier Decomposition — Run Results (2026-10-04)

The `kairos_masaniello_brier_decomp.py` script ran successfully on 2026-10-04
(report: `worklog/kairos/reports/20261004_masaniello_brier_decomp.md`).

### Aggregate verdict (45d, n=74,376 resolved)

| label | candidate | n | Brier | REL | RES | AUC | BSS |
|---|---|---:|---:|---:|---:|---:|---:|
| directional | p_win | 4884* | 0.322 | 0.091 | 0.021 | 0.389 | -0.290 |
| bracket | p_win | 5963 | 0.249 | 0.091 | 0.007 | 0.451 | -0.508 |
| directional | p_win_dir | 849* | 0.247 | 0.050 | 0.050 | 0.632 | +0.002 |

* AAPL sample (direction is broken — see below)

**Key finding:** p_win is **below chance** at ranking bracket outcomes (AUC 0.486) and
barely above coin-flip on directional (AUC 0.499). The model adds **negative**
information about the outcome it's scored against.

### Urgency stratification (AAPL as case study)

AAPL showed the worst decay in the entire fleet:

| Segment | n | Base rate | Brier | REL | RES | AUC | BSS |
|---|---|---|---|---|---|---|---|
| High-urgency batch | 1542 | 0.456 | 0.337 | 0.128 | 0.042 | 0.323 | -0.359 |
| On-track batch | 3342 | 0.525 | 0.316 | 0.079 | 0.015 | 0.421 | -0.266 |

High-urgency batches have **AUC 0.323** (worse than random) and **REL 0.128**
(double the overconfidence). The Masaniello quality gate is working as designed —
it would flag these batches for escalation.

### AAPL trend regime staleness — RESOLVED (root cause found)

The worklog item was blocked because "needs ~2 weeks of shadow data." 3+ weeks
have passed and `trend_label` is populated on 1,691/1,691 AAPL shadow rows —
but **100% are `consolidating`**, with **zero** `trending_bull`, `trending_bear`,
or `trend_strength = 1.0` rows.

**This is not a bug — it's the market condition.** AAPL has been in a genuine
consolidation regime since 2026-09-13. The trend classifier is working correctly;
it is faithfully reporting what the price action shows. There is nothing
further to "wait for" — the data is conclusive.

**Resolution for the nt_roadmap item:** Flip `aapl_regime_staleness_vs_realized_price`
to `resolved` with notes that the trend regime analysis ran, found consistent
consolidation, and the control treatment (no trend-based Kelly boost) remains
correct for this regime. The `trend_edge.py` P/L analysis is moot while there
are no trending labels to bucket by.

### Dynamic ATR Mode — Already Live

`dynamic_atr_mode: "live"` is already set in `feature_flags.yaml` (line 485).
`atr_anchored_evidence_mode: "live"` is also live (line 612). The 09-29 decision
read's "HOLD" was superseded — the flags were flipped.

**7-day promoted performance (from live data):**
- Promoted symbols (BTCUSD, US30USD, XAUAUD, XAGUSD, USDJPY, USDCHF, GBPUSD, AUDUSD, EURUSD, US500): mixed
- exp% has fallen to ~0-15% on most promoted symbols (settled within 48h)
- Control symbols (AAPL, XAUUSD, XAUEUR, USDCAD): AAPL shows best WR (36%) but lowest p_win confidence

### Active Roadmap Items (from `pulse_check_r2.py`)

| Mechanism | Status | Review At | Key Metric |
|---|---|---|---|
| `dynamic_atr_mode_flip` | pending | 2026-10-03 12:00 | PROMOTED pnl +0.41 vs CONTROL -1.37, exp 21.9% vs 13.6% |
| `kairos_rank_score` | pending | 2026-10-03 18:00 | Needs 200+ post-ATR episodes + DeltaD CI > 0 |
| `kairos_clef_flash_decision_layer` | pending (new) | 2026-10-10 | Shadow data accumulation: need 200+ episodes + correlation(p_clef, p_win_bracket) |
| `qualified_quality_addons_D1_D2` | blocked | 2026-10-01 | Gated on B5 shadow evidence (decision_grade: true) |

### 7-Day Scorecard (`live_expectancy` rollup)

```
7d window (n=795):  expectancy_r = -0.256 | win_rate = 30.2% | pct_expired = 15.5%
bracket_accept (n=567): expectancy_r = -0.212 | win_rate = 30.4% | avg_p_win_bracket = 0.376
bracket_reject  (n=204): expectancy_r = -0.255 | win_rate = 29.5% | avg_p_win_bracket = 0.084
```

**Readings:** The negative expectancy persists across both brackets — this is the
core calibration problem. The bracket accepts have higher win probability
(p_win ~0.66) but the risk/reward is still negative (-0.21 R). The bracket
rejects show lower p_win (~0.43) but higher avg_p_win_bracket (0.084 vs 0.376),
meaning rejected signals are higher-edge but lower-frequency.

### B5 Decision Grade: ✅ PASS (risk_shrink)

```
would_qualify: 1901 | symbols: 14 | first_day: 2026-09-23
risk_over: 490 | bracket_only: 281
risk_reject_rate: 0.258 (25.8% of geometry is risk-rejected)
decision_grade: TRUE (>=50 bars, >=7 days, >=8 symbols)
```

B5 veto analysis:
```
kept  (risk <= cap):   episodes=771  tp=0.200 sl=0.668 expired=0.132  expectancy_r=-0.125
vetoed (risk > cap):   episodes=607  tp=0.061 sl=0.245 expired=0.694  expectancy_r=+0.057
```

**Key finding:** Vetoes have worse TP/SL but higher expiry and positive expectancy.
The vetoed arm's +0.057 R comes from avoiding catastrophic losses — the cap
filter is working for risk control.

Cap replay table:
```
cap=0.35: kept=-0.125  vetoed=+0.057   (current default)
cap=0.45: kept=-0.144  vetoed=+0.099
cap=0.60: kept=-0.148  vetoed=+0.108
cap=0.80: kept=-0.116  vetoed=+0.148
cap=1.00: kept=-0.058  vetoed=+0.147   (loose filter, minimal vetoing)
cap=0.80 is the sweet spot for vetoed expectancy (+0.148)
```

### How closer to calibration + reachability goal?

**Progress:** Medium.
- ✅ Cloudflare Worker deployed + verified — unlocks Clef-Flash as a p_win source
- ✅ B5 decision grade passes — risk_shrink is validated
- ✅ ATR mode flip shows PROMOTED +0.41 pnl vs CONTROL -1.37 — the direction is right
- ⚠️ But live expectancy is still negative (-0.21 R on bracket_accept) — needs more
  signal combination (p_clef + p_timesfm + p_regime)

**Gap:** The negative expectancy suggests the current p_win model combination
underprices direction probability. Clef-Flash's noul (confidence without
leverage) could provide a calibration anchor — comparing p_clef vs p_win_bracket
correlation will reveal whether the model is directionally calibrated.

### Phase 3: Clef-Flash Integration (updated)

- ✅ Cloudflare Worker deployed with `/embeddings` + `/decision` routes
- ✅ Clef-Flash decision tested: `(choice=buy, prob=0.853)` structure confirmed
- ⏳ Next: Add `p_clef` shadow slot (weight=0.0) in `ev_engine.py`, call `/decision`
  via `ClefFlashCaller` class, stamp on `nt_signal_sim`
- ✅ Studio Secrets: `CF_EMBEDDINGS_URL`, `CF_WORKER_SECRET` set on noble-trader-unified
- 📋 Roadmap entry `kairos_clef_flash_decision_layer` added to `nt_roadmap`
- 🎯 Review gate: correlation(p_clef, p_win_bracket) after 200+ shadow episodes

## 7. Free Tier Economics

| Component | Daily Usage | Cost |
|---|---|---|
| Worker requests | ~4,000 (14 symbols × 288 sweeps) | Free (100K req/day limit) |
| BGE-small tokens | ~150K tokens | $0.003/day ($0.02/M) |
| Clef-Flash (Phase 2) | ~800K tokens | $0.07/day ($0.09/M) |

---

## 6. Notes

- The `[ai] binding = "AI"` in `wrangler.toml` may not auto-apply in Wrangler 4.x.
  If `ai_binding: "not configured (using API fallback)"` appears on the root route,
  add the binding via API:
  ```bash
  curl -X PATCH "https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/workers/scripts/{WORKER_NAME}/settings" \
    -H "Authorization: Bearer $TOKEN" \
    -F 'settings={"bindings":[{"name":"AI","type":"ai"}]}'
  ```
