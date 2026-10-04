# Cloudflare Workers AI — Embeddings + Clef-Flash Deployment Guide

## Overview

This guide covers deploying a **Cloudflare Worker** (free tier) that proxies
embedding and decision inference requests to Cloudflare Workers AI,
replacing the local MiniLM-L6-v2 embedding model in `knowledge_index.py`
with `@cf/baai/bge-small-en-v1.5` (384-dim, exact dimension match).

## Key Facts

| Item | Value |
|---|---|
| **Worker model** | Cloudflare Worker (free tier: 100K req/day, 1M neutrons/day) |
| **Embedding model** | `@cf/baai/bge-small-en-v1.5` (384-dim, $0.02/M tokens) |
| **Decision model** | `@cf/cloudflare/clef-flash` (9B, $0.09/M input tokens) |
| **Auth** | `env.AI` binding (preferred) or `CF_API_TOKEN` secret (fallback) |
| **Python caller** | `knowledge_index.py` via `CF_EMBEDDINGS_URL` Studio Secret |
| **Dimension match** | 384-dim (same as MiniLM-L6-v2 → **no index rebuild**) |

## Step 1: Deploy the Cloudflare Worker

```bash
cd noble-trader-fastapi-backend/supabase/functions/cloudflare-ai-proxy/
CLOUDFLARE_ACCOUNT_ID=e787a0b54e75a41d1c25d8a9bc9f8809 npx wrangler deploy worker.js --no-bundle
```

**Deployed URL:** `https://nobletradingapp.lexington.workers.dev`

### Set secrets on the worker

```bash
# Optional: shared secret for auth between Studio and Worker
echo "<strong-random-hex>" | npx wrangler secret put CF_WORKER_SECRET

# Required (fallback if env.AI binding is not available): Cloudflare API token
echo "$CLOUDFLARE_API_TOKEN" | npx wrangler secret put CF_API_TOKEN
```

### Workers AI binding

The worker tries `env.AI` binding first (preferred, no token needed). If the `[ai]` binding
isn't available on your account (the token may lack `Account.Workers AI` permission),
the worker automatically falls back to the CF_API_TOKEN secret to call the Cloudflare
REST API directly.

The `[ai] binding = "AI"` line in `wrangler.toml` attempts to enable the binding at deploy.
If the deploy log shows `ai_binding: "not configured (using API fallback)"` on the root
route, the API fallback path is active — which is fully functional.

## Step 2: Add Studio Secrets

Set these as **Studio Secrets** (not env vars in `feature_flags.yaml`):

| Secret | Value | Purpose |
|---|---|---|
| `CF_EMBEDDINGS_URL` | `https://nobletradingapp.lexington.workers.dev` | Worker endpoint URL |
| `CF_WORKER_SECRET` | `5252325e6363ddff9f38bd590d047b8fd2a51e59ea5ffa97` (optional) | Auth header for the worker |

> These are read by `knowledge_index.py` via `os.getenv()` — same pattern as `NVIDIA_NIM_API_KEY`, `SUPABASE_URL`, etc.

## Current Deployment Status (as of 2026-10-03)

| Component | Status |
|---|---|
| Worker deployed | ✅ `nobletradingapp` at `https://nobletradingapp.lexington.workers.dev` |
| CF_WORKER_SECRET | ✅ Set: `5252325e6363ddff9f38bd590d047b8fd2a51e59ea5ffa97` |
| CF_API_TOKEN | ✅ Set as worker secret (fallback for AI binding) |
| env.AI binding | ✅ Enabled (configured via API PATCH) |
| /embeddings route | ✅ Tested: 384-dim, L2-normalized vectors |
| /decision (Clef-Flash) | ✅ Tested: structured answers with probabilities |

### Shadow Test

With `CF_EMBEDDINGS_URL` set on the Studio but before committing to the migration:

```bash
# Build the index — should use Cloudflare, fall back to local if it fails
python -m regime_platform.services.self_learning.knowledge_index
```

Check logs for:
- `knowledge_index: embedding via Cloudflare Workers AI (BGE-small)` — Cloudflare path active
- `knowledge_index: Cloudflare embedding worker returned HTTP 5xx — fallback` — failure, falling back

The `embed_texts()` function automatically tries Cloudflare first, then falls
back to local MiniLM if the worker returns an error. No downtime — the system
degrades gracefully.

## Step 4: Full Migration (optional)

Once you've verified Cloudflare embeddings produce equivalent retrieval results:

1. Verify no dimension mismatch: `EMBEDDING_DIM = 384` (unchanged — BGE-small is also 384-dim)
2. The SQLite schema stores `embedding BLOB NOT NULL` — dimension-agnostic, no migration needed
3. Remove the `sentence-transformers` dependency from `requirements.txt` / `pyproject.toml` (after verifying the index rebuild works with Cloudflare-only)

## Clef-Flash Decision Layer (Phase 2)

The same worker exposes a `/decision` route for Clef-Flash:

```bash
curl https://nobletradingapp.lexington.workers.dev/decision \
  -H "x-worker-secret: 5252325e6363ddff9f38bd590d047b8fd2a51e59ea5ffa97" \
  -d '{
    "state": {"symbol": "XAUUSD", "price": 3247.82, "regime": "high_vol_bull", "session": "London/NY overlap", "atr_14": 4.18, "resistance": 3251.40, "trend_h1": "bullish"},
    "questions": {
      "direction": {"type": "choice", "instructions": "Trade direction", "criteria": {"buy": "Bullish setup", "sell": "Bearish setup", "wait": "No clear signal"}},
      "overconfident": {"type": "noul", "instructions": "Is the regime overconfident?"}
    }
  }'
```

This returns structured probabilities — no text parsing needed. The `knowledge_index.py`
already has the infrastructure for HTTP-based embedding; adding Clef-Flash decision
calls follows the same `_embed_texts_cf_sync` pattern (async → sync wrapper).

## Free Tier Economics

Cloudflare Workers free tier: 100,000 requests/day.

At 14 symbols × 5-min cadence = 4,032 embedding calls/day (14 per sweep × 288 sweeps).
Well within the free tier. BGE-small embedding cost: $0.02/M tokens × ~150K tokens/day
(code + worklogs corpus) = **$0.003/day** — effectively free.

Clef-Flash decisions (Phase 2): 14 symbols × 288 sweeps × ~2K tokens/state = ~800K tokens/day.
At $0.09/M = **$0.07/day**. Still negligible.
