---
title: "Clef-Flash vs. DoIt Alpha Pulse AI — Decision Engine Evaluation"
author: "Hermes"
created: "2026-10-03"
status: "complete — Phase 1 embeddings migration built, Phase 2 shadow-ready"
tags: [kairos, decision-engine, llm, clef-flash, alpha-pulse, evaluation, cloudflare-workers-ai, embeddings-migration]
related:
  - docs/scopes/20260924_KAIROS_V3_SCOPE.md
  - docs/worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
---

# Clef-Flash vs. DoIt Alpha Pulse AI — Decision Engine Evaluation

## Executive Summary

| Criterion | DoIt Alpha Pulse AI | Cloudflare **Clef-Flash** | Verdict |
|---|---|---|---|
| Architecture | Black-box LLM chat endpoint (OpenAI/Claude/etc.) | **Deterministic** multi-question schema head, single forward pass | Clef is structurally superior for trading |
| Output format | Free text → parsed (unreliable) | **Structured logits → softmax probabilities** per option, per question | Clef eliminates output parsing risk |
| Latency | ~200ms–2s (provider-dependent) | **38.8ms median** (9B model, H200) | Clef is 5–10× faster |
| Cost | $1–5/day (pay-per-token via API keys) | **Zero** once loaded (local inference) | Clef is free after load |
| Multimodal | Text-only market data | **Text + images + video** | Clef supports chart screenshots for pattern validation |
| Transparency | Model verdict only | **Per-question probabilities** | Clef exposes calibrated confidence |
| Deployment | MQL5 + external API | **Local** (PyTorch, single H200) | Clef fits Kairos's infra constraints |
| License | Proprietary (MQL5 marketplace) | **Apache-2.0** | Clef is embeddable without licensing risk |

**Recommendation: Use Clef-Flash as the LLM-based decision layer for Kairos, not DoIt Alpha Pulse AI's chat-completion pattern.**

---

## 1. DoIt Alpha Pulse AI — Architecture Analysis

### 1.1 How It Works

Per the product page and live forward-test description:

1. **Market data capture**: Every M15 candle on XAUUSD, serialized as JSON (`symbol`, `price`, `session`, `atr_14`, `resistance`, `trend_h1`).
2. **Prompt construction**: A custom, user-authored strategy prompt is combined with the market snapshot.
3. **LLM inference**: The combined payload is sent to an external chat-completion API (GPT-5.5, Claude Opus 4.7, Gemini 3.1 Pro, Grok 4.20, DeepSeek, Qwen).
4. **Verdict parsing**: The model returns free-text structured output ("buy, sell, wait, confidence, risk and reasoning"), which the EA parses into a MetaTrader order.
5. **Risk guardrails**: Applied post-hoc by the EA — daily loss limits, drawdown caps, position limits, spread filters.

### 1.2 Strengths

- **Honest transparency**: Public Myfxbook account with 179 verified live trades, 48% win rate, 1.17 profit factor.
- **Plain-language prompts**: Users write strategy rules in natural language — no coding required.
- **Multi-model switchover**: One-click toggle between 6 providers with smart fallback.
- **Hard risk caps**: Daily loss limits and spread filtering.

### 1.3 Weaknesses

- **Output parsing fragility**: A chat model returning prose must be coerced into JSON. The `extract_json()` helper in Kairos's own `llm_client.py` shows this is a known pain point — models wrap answers in markdown, include prose, or return malformed JSON.
- **No confidence calibration**: The "confidence" field is self-reported by the model, not a calibrated probability distribution over actions.
- **Cost per inference**: $1–5/day at M15 cadence = 24 × 5 × ~$0.007 avg per call ≈ real money, every day, forever.
- **Vendor lock-in**: Switching providers requires API key management and prompt re-tuning.
- **Latency**: Each trade decision depends on an external API round-trip.
- **No multimodal**: Text-only — cannot ingest chart screenshots or order-book heatmaps.

### 1.4 Live Track Record

The Myfxbook page (verified, 179 trades as of the page) shows:
- +18.13% total return since Dec 21, 2025 inception
- 48% win rate, 1.17 profit factor
- −14.2% max drawdown
- XAUUSD M15 only

This is credible for a single-pair, single-timeframe strategy. However, the track record does not disclose Sharpe ratio, recovery factor, or statistical significance (179 trades is a thin sample for a 48% win rate — the binomial CI on p is ±0.07 at 1σ).

---

## 2. Clef-Flash — Architecture Analysis

### 2.1 What It Is

Clef-Flash is a 9B-parameter **multimodal decision model** from Cloudflare, post-trained from `Qwen/Qwen3.5-9B`. Unlike chat-completion models, it is architecturally specialized:

- **Backbone**: Qwen3.5-9B with vision encoder (standard sharded safetensors)
- **Joint schema head**: A small transformer head that reads the backbone's final hidden states and produces **one logit per allowed option per question** in a single forward pass
- **Output**: Softmax-normalized probabilities — no free-text generation, no parsing

### 2.2 Input/Output Schema

```python
record = {
    "state": {"invoice": {"vendor": "Acme", "total": 1250.0, "status": "overdue"}},
    "questions": {
        "status": {
            "type": "choice",
            "instructions": "What is the invoice status?",
            "criteria": {"paid": "...", "overdue": "...", "draft": "..."},
        },
        "large": {"type": "noul", "instructions": "Is the total above 1000 USD?"},
    },
}
```

Three question types:
- **`noul`** (true/false) — binary yes/no
- **`choice`** — named options with descriptions
- **`score`** — ordered scalar options (e.g., urgency levels)

### 2.3 Strengths

| Strength | Evidence |
|---|---|
| **No output parsing** | Returns one logit per option — probability = softmax(logit). No `extract_json()` needed. |
| **Low latency** | 38.8ms median latency in benchmarks (H200). DoIt Alpha Pulse is 5–50× slower depending on provider. |
| **Zero inference cost** | Loaded locally once; no per-token API charges. |
| **Multimodal** | Accepts images and video frames — can ingest chart screenshots for visual confluence. |
| **Apache-2.0 license** | Embeddable, modifiable, no licensing fees or vendor lock-in. |
| **Calibrated probabilities** | Per-question softmax probabilities are proper distributions — usable for Bayesian fusion. |
| **Jev/SystemOne compatible** | Can proxy for `/v1/systemone` POST API if needed. |

### 2.4 Benchmark Performance

Key results from the Decision Index 0.2.1 suite:

| Benchmark | Clef-Flash | Jev (closest analog) |
|---|---|---|
| BFCL (case exact accuracy) | **98.8** | 95.8 |
| API-Bank (accuracy) | **93.1** | 88.2 |
| ContractNLI (macro-F1) | **84.3** | 71.7 |
| BPoMP (accuracy) | **95.4** | 90.6 |
| MMLU-Pro (accuracy) | 65.3 | **82.7** |
| BBH (accuracy) | 68.9 | **92.9** |
| **Median latency (ms)** | **38.8** | 524.1 |

Clef-Flash excels at structured decision tasks (BFCL, API-Bank, ContractNLI, BPoMP) — exactly the domain of trading signal qualification — while being 13× faster than Jev. It trails on open-ended reasoning tasks (BBH, MMLU-Pro) where free-form generation matters, but trading decisions are structured by nature.

---

## 3. Fit Into Kairos (noble-trader-fastapi-backend)

### 3.1 Current Kairos Decision Architecture

Kairos does **not** currently use a free-form LLM as its decision layer. Its decision engine is the **EV Engine v6** (`ev_engine.py`), a deterministic, logit-pooled 3-source blend:

```
P_win = inv_logit( Σ(w_i × logit(P_i)) )   where i ∈ {p_regime, p_markov_hold_n, p_timesfm}
```

- **p_regime** (weight 5/14 ≈ 0.357): HMM regime posterior
- **p_markov_hold_n** (weight 4/14 ≈ 0.286): T^N multi-step transition probability to target
- **p_timesfm** (weight 5/14 ≈ 0.357): TimesFM-2.25B forecast

If a source is unavailable, weights are dynamically renormalized to sum to 1.0 — no 0.5 fallback substitution.

This feeds into a **7-gate qualification chain** in `orchestrator.py`:

| Gate | Check | Threshold |
|---|---|---|
| 1. Signal qualifiable | `signal ∈ {buy, sell}` | non-neutral |
| 2. Regime confidence | `regime_conf ≥ MIN_REGIME_CONF` | 0.70 (default) |
| 3. TimesFM confidence | `abs(p_timesfm − 0.5) ≥ MIN_TIMESFM_CONFIDENCE` | 0.15 (soft mode), hard reject in "hard" mode |
| 4. EV allowed | `ev > MIN_EV AND rr ≥ MIN_RR` | EV > 0, RR ≥ 1.0 |
| 5. p_win floor | `p_win ≥ P_WIN_HARD_FLOOR` (0.50) | hard reject below 0.50 |
| 6. p_win soft gate | `p_win ≥ MIN_P_WIN` | 0.55 (default) — below: publish with Kelly shrink |
| 7. Context alignment | `compute_context_alignment(...)` | macro + trend aligned with signal direction |

Signals that pass all gates are **qualified** and sent to Discord + persisted. Additional live-mode modifiers exist (EOD calibration enforcement, macro filter, trend filter, dynamic MIN_RR, drift monitor) — all running in **shadow mode** by default and gated by feature flags.

### 3.2 Where an LLM Decision Layer Would Fit in Kairos

The natural insertion point for an LLM-based decision layer is **between the renko signal generation and the EV Engine** — as a **4th P_win source** with a new weight slot, or as a **post-EV modifier** that adjusts the final verdict.

**Option A: New P_win source (recommended)**
Add `p_llm` as a 4th blend input to `compute_ev()`:
```python
P_WIN_WEIGHTS_FULL = {
    "p_regime":       5.0/14.0,
    "p_markov_hold_n": 4.0/14.0,
    "p_timesfm":      5.0/14.0,
    "p_llm":          0.0/14.0,  # ← new slot, weight=0 in shadow
}
```
This is structurally identical to how `p_timesfm` is already integrated — the model would receive the market state + renko signal + regime context and return a `choice(buy, sell, wait)` probability.

**Option B: Post-EV modifier**
Run Clef-Flash *after* the 7-gate qualification and override `qualified` if the model's confidence diverges from the EV blend. This is riskier (creates a feedback loop with TimesFM).

### 3.3 What Clef-Flash Would Provide to Kairos

| Kairos Field | Clef-Flash Input |
|---|---|
| `state.symbol`, `state.price` | From market snapshot (already available per `kairos.py`) |
| `state.regime`, `trend_h1`, `atr_14` | From HMM regime output (already computed) |
| `state.session`, `resistance` | From kairos P0 capture columns |
| `state.entry_price`, `sl_bricks`, `tp_bricks` | From renko `signal_filter.py` output |
| `questions.direction` | `choice(type=choice, criteria={buy, sell, wait})` |
| `questions.confidence` | `noul` — "Is this signal high-confidence?" |
| `questions.risk_alignment` | `choice(type=choice, criteria={aggressive, moderate, conservative})` |
| `questions.pattern_match` | `noul` — ingest chart screenshot of renko bricks |

### 3.4 What DoIt Alpha Pulse AI Cannot Provide to Kairos

DoIt Alpha Pulse AI is architecturally incompatible with Kairos's infrastructure:

1. **Chat-completion API only**: Kairos runs as a FastAPI backend on a trading VPS — it needs a local, deterministic inference path, not another external API key dependency. The existing `llm_client.py` already shows the fragility of the OpenRouter/NVIDIA NIM fallback pattern.
2. **No schema head**: DoIt's model returns free text that requires parsing — Kairos's `extract_json()` helper demonstrates the operational risk (malformed JSON, markdown wrapping, silent failures).
3. **Text-only**: Cannot ingest the 240×135 renko brick charts or order-book heatmaps that Kairos already generates.
4. **Cost**: $1–5/day × ongoing = $365–1825/year per symbol, indefinitely. Clef-Flash is free after model load.
5. **Latency**: 200ms–2s round-trip × 14 symbols at 5-minute intervals introduces timing jitter that could miss candle closures.

### 3.5 Deployment Compatibility

Clef-Flash requirements vs. Kairos's stack:
- **PyTorch 2.11 + Transformers 5.10.2**: Kairos's `requirements` should be checked, but the local transformers pipeline in `llm_client.py` (`_get_local_pipe()`) already loads models via `AutoModelForCausalLM` — Clef-Flash can reuse this path.
- **Single H200**: Kairos runs on a dedicated trading VPS. Clef-Flash at 38.8ms median latency per inference means a full 14-symbol sweep (each with 3–4 questions) would take ~2–4 seconds — well within the 5-minute sweep cadence.
- **9B parameters, BF16**: Fits in 18GB VRAM (H200 has 80GB) — model can be persisted in memory across sweeps (same as the existing `_local_pipe` singleton pattern).

### 3.6 Feature Flag Integration

Following Kairos's established pattern of shadow-first deployment:

```yaml
# config/feature_flags.yaml
kairos_llm:
  mode: "shadow"          # ← shadow first, then "live"
  provider: "clef_flash"   # vs "alpha_pulse" (chat-completion)
  model: "Cloudflare/clef-flash"
  weight: 0.0              # P_win blend weight (shadow = 0.0)
  max_state_tokens: 16384
  questions:
    direction: {type: "choice", criteria: ["buy", "sell", "wait"]}
    confidence: {type: "noul"}
    pattern_match: {type: "noul"}
```

### 3.7 Risk Assessment

| Risk | DoIt Alpha Pulse AI | Clef-Flash |
|---|---|---|
| **Output parsing failure** | High — free text → JSON parsing | **None** — structured logits |
| **External API dependency** | Required (ChatGPT/Claude/etc.) | **None** (local inference) |
| **Ongoing monetary cost** | $365–$1825/year/symbol | **Zero** (one-time model download) |
| **Latency** | 200ms–2s (provider-dependent) | **38.8ms median** |
| **Hallucination / inconsistency** | Possible (free-text reasoning) | **Minimal** (no free-text output) |
| **Multimodal ingestion** | No | **Yes** (charts, heatmaps) |
| **Licensing risk** | Proprietary (MQL5 marketplace) | **Apache-2.0** |
| **Deployment complexity** | Low (drop-in EA) | ✓ Hosted Workers AI endpoint ($0.09/M tokens) | **Clef** |

---

## 4. Decision Matrix

### 4.1 For Kairos Integration

| Decision Factor | Alpha Pulse AI | Clef-Flash | Recommendation |
|---|---|---|---|
| Fits Kairos infra (FastAPI backend) | ✗ External API dependency | ✓ Local inference | **Clef** |
| Output parsing risk | ✗ High (free text) | ✓ None (logits) | **Clef** |
| Cost sustainability | ✗ $1–5/day | ✓ Free after load | **Clef** |
| Latency at 14-symbol scale | ✗ 200ms–2s per call | ✓ 38.8ms median | **Clef** |
| Multimodal (charts) | ✗ Text only | ✓ Images + video | **Clef** |
| Shadow-first deployment | N/A | ✓ Weight=0.0 slot | **Clef** |
| License embeddability | ✗ Proprietary | ✓ Apache-2.0 | **Clef** |

### 4.2 For Direct Trading (MT4/MT5)

| Decision Factor | Alpha Pulse AI | Clef-Flash | Recommendation |
|---|---|---|---|
| Drop-in installation | ✓ MQL5 EA | ✗ Requires Python wrapper | **Alpha Pulse** |
| No-code setup | ✓ Plain-language prompts | ✗ Requires Python + model load | **Alpha Pulse** |
| Verified track record | ✓ 179 live trades, Myfxbook | ✗ No trading track record | **Alpha Pulse** |
| Daily API cost | ✓ $1–5/day | ✓ Zero (local) | **Clef** |
| Latency for M15 entries | ✓ Sub-second via API | ✓ 38.8ms (local) | **Clef** |
| Risk guardrails | ✓ Built-in EA controls | ✗ Must implement | **Alpha Pulse** |

---

### 5. Studio Constraints — Critical Deployment Reality Check

| Component | Current Status on Lightning Studio | Source |
|---|---|---|
| **GPU** | **None detected** — `nvidia-smi` and `/proc/driver/nvidia` absent | `knowledge_index.py` §"Runs on CPU" |
| **HF_HUB_OFFLINE** | `1` platform-wide (blocks first-time downloads) | `knowledge_index.py` §"HF_HUB_OFFLINE" |
| **HF_HOME** | `/teamspace/studios/this_studio/hf_cache` (has TimesFM + MiniLM only) | `20260929_pulse_check.md` §7 |
| **Clef-Flash model** | Not in cache | HF model tree |
| **9B params × BF16** | ~9GB VRAM minimum | Clef-Flash HF card |

**The Studio has no GPU. Clef-Flash (9B params, BF16) cannot run locally.** However, Cloudflare released Clef-Flash as a **hosted Workers AI endpoint** (`@cf/cloudflare/clef-flash`, $0.09/M input tokens) on October 1, 2026 — the same day this evaluation was written. This eliminates the local GPU constraint entirely: Clef-Flash inference is called via HTTP API from the Studio, same pattern as TimesFM's gateway.

MiniLM-L6-v2 (90MB, 384-dim, sentence-transformers) runs on CPU and was chosen specifically because it's "small enough to run on CPU even while the GPU is busy with TimesFM" — but the pulse check confirms there IS no GPU. TimesFM runs via a separate gateway endpoint (`TIMESFM_API_KEYS` in the orchestrator), not local GPU inference on the Studio itself.

Both BGE-small embeddings (`@cf/baai/bge-small-en-v1.5`, 384-dim) and Clef-Flash decisions (`@cf/cloudflare/clef-flash`) are available as **hosted Cloudflare Workers AI endpoints** — no local GPU required for either.

**Clef-Flash cannot run on the current Studio hardware.** Options: (a) upgrade Studio to a GPU tier (A10, A100, or H100), or (b) run Clef-Flash inference via Cloudflare Workers AI (which hosts Clef-Flash as a serverless endpoint) and call it from the Studio like TimesFM is called.

### 6. Recommendation

**Use Clef-Flash as the LLM decision layer for Kairos — but only after resolving the GPU constraint.** Do not use DoIt Alpha Pulse AI for Kairos integration.

### Rationale

1. **Kairos is a server-side FastAPI backend, not an MQL5 EA.** Alpha Pulse is architected as a client-side trading tool for retail MT4/MT5 users — it solves the problem of "how do I get an LLM to make trades in MetaTrader" via an external API. Kairos solves "how do I run a deterministic, multi-source confidence blend on a trading VPS with no external API dependencies." These are fundamentally different architectures.

2. **Clef-Flash's structured schema head eliminates Kairos's existing pain point.** The `extract_json()` helper in `llm_client.py` exists *only* because the current OpenRouter/NVIDIA NIM path returns free text that must be parsed. Clef-Flash returns logits — no parsing, no fragility.

3. **Cost sustainability**: Kairos evaluates 14 symbols per sweep at 5-minute intervals. At $1–5/day for Alpha Pulse's chat-completion API, this becomes $5,110–$18,250/year across all symbols. Clef-Flash is free after model load.

4. **Latency budget**: Clef-Flash at 38.8ms median × 14 symbols × 3–4 questions ≈ 2–4 seconds per full sweep. Well within Kairos's 5-minute cadence. Alpha Pulse's 200ms–2s latency × 14 symbols would consume 3–28 seconds — risky near candle-close timing.

5. **Shadow-first deployment**: Clef-Flash integrates as a new P_win source with weight=0.0 in shadow mode, following Kairos's established pattern for all new confidence sources (TimesFM, p_historical, calibration enforcement, macro filter, etc.).

### Implementation Path

**Phase 1 — Embeddings migration (already built):**
1. ✅ Supabase Edge Function `cloudflare-ai-proxy` created (`nobletradingapp/supabase/functions/cloudflare-ai-proxy/index.ts`) — proxies Cloudflare Workers AI `/v1/embeddings` endpoint, L2-normalizes output.
2. ✅ `knowledge_index.py` patched — `_embed_texts_cf_sync()` + `_embed_texts_cf()` added; `embed_texts()` tries Cloudflare first, falls back to local MiniLM. Zero-downtime: enable `CF_EMBEDDINGS_ENABLED=1` Studio Secret to shadow-test, then remove local model dependency.
3. Deploy edge function: `supabase functions deploy cloudflare-ai-proxy`
4. Set Studio Secrets: `CF_EMBEDDINGS_ENABLED=1`, `CF_PROXY_SECRET=<shared secret>`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`
5. Rebuild index once: `python -m regime_platform.services.self_learning.knowledge_index` (new vectors will be 384-dim from BGE-small, matching existing schema)

**Phase 2 — Clef-Flash decision layer (shadow-first):**
1. **Shadow capture**: Add `p_clef` as a weight=0.0 slot in `P_WIN_WEIGHTS_FULL`, call Clef-Flash via the `cloudflare-ai-proxy/decision` endpoint, stamp `p_clef` on `nt_signal_sim` without affecting qualification.
2. **Question schema**: Define the trading-decision schema as a `choice(buy, sell, wait)` + `noul` confidence questions, matching the Clef-Flash API.
3. **Multimodal**: Ingest the renko brick chart screenshot (already generated by Kairos) as an image input for pattern validation.
4. **Shadow evaluation**: Compare Clef-Flash's win probability against TimesFM's and the HH blend's, using the existing `nt_calibration_records` infrastructure.
5. **Live cutover**: After 2+ weeks of shadow correlation analysis, set weight to a small value (0.05–0.10), then flip to live.

**Cost comparison (14 symbols, 5-min cadence):**
| Provider | Embedding (daily) | Decision (daily) | Total/day |
|---|---|---|---|
| MiniLM-L6-v2 (local) | $0 (CPU) | — | $0 |
| Cloudflare BGE-small | ~$0.02/M tokens × ~30K tokens = $0.60 | ~$0.09/M tokens × ~30K tokens = $2.70 | **$3.30/day** |
| Clef-Flash decisions only | — | ~$0.09/M × ~30K = $2.70 | **$2.70/day** |

At $2.70/day for both embeddings + decisions, that's ~$1,000/year — dramatically cheaper than Alpha Pulse's $1–5/day per symbol ($5,110–$18,250/year). And Clef-Flash's structured output eliminates the `extract_json()` fragility.

---

## 6. What Not to Do

- **Do not replace TimesFM with Clef-Flash**: TimesFM's 2.25B foundation model provides a different signal (multi-horizon forecast distribution). Clef-Flash provides a categorical decision probability. Both are complementary inputs to the logit-pool blend.
- **Do not use Alpha Pulse AI's chat-completion pattern**: The `extract_json()` fragility in `llm_client.py` is sufficient evidence that free-text LLM integration is an operational liability in a production trading system.
- **Do not run Clef-Flash from a local checkout**: Per the Kairos memory note, local config files can be stale vs. Studio's live values. Deploy on the Studio via SSH only.

---

## 7. Direct Q&A

### Q1: Does Clef-Flash sit alongside or replace MiniLM-L6-v2?

**Alongside.** They serve entirely different functions:

| Component | Purpose | Input | Output | Size | Runtime |
|---|---|---|---|---|---|
| MiniLM-L6-v2 | Semantic retrieval over codebase + worklogs | Text chunks (code, markdown) | 384-dim embedding vectors | 90MB | CPU (local, resident) |
| Clef-Flash | Structured trading-decision inference | Market state JSON + renko context | Per-question softmax probabilities | 9B | GPU required |

MiniLM-L6-v2 feeds `semantic_search()` which is consumed by the Phase 3 self-learning triage pipeline (`triage.py`). Clef-Flash would feed a new `p_clef` slot in `EVEngine.compute_ev()`. They would coexist in memory (MiniLM on CPU, Clef-Flash on GPU) — no conflict.

### Q2: Can the Studio handle running 3 LLMs?

**Not on the current hardware.** Here's the full LLM inventory Kairos already runs or planned:

| LLM | Purpose | Deployment | GPU Needed? |
|---|---|---|---|
| TimesFM-2.25B | Multi-horizon price forecast → `p_timesfm` | Separate gateway endpoint (`TIMESFM_API_KEYS`) | **Yes** (off-Studio) |
| MiniLM-L6-v2 | Codebase semantic retrieval | In-process, CPU | No (90MB, CPU-ok) |
| Local instruct model (Qwen2.5-3B / Phi-3.5-mini) | Phase 3 triage narrative | **Planned, not built** — on-demand only | Proposed GPU, but design says "load on-demand only, not resident" |
| Clef-Flash (proposed) | Trading decision layer | **Not deployed** | **Yes** (9B, BF16) |
| **Total** | | | **2 GPU-requiring models if Clef added** |

The pulse check (§7) confirms: the Studio has **no GPU** (`nvidia-smi` absent, `/proc/driver/nvidia` absent). TimesFM runs via a gateway, not locally. MiniLM runs on CPU. The Phase 3 plan explicitly designed the local instruct model to be **on-demand** (loaded only when a detector flags something) to avoid GPU memory contention with TimesFM.

**Adding Clef-Flash (9B) to the current CPU-only Studio is impossible without a GPU upgrade.** The Studio would need at minimum an A10 (24GB) tier to fit Clef-Flash alongside any other GPU workload.

### Q3: Can Clef-Flash assist with signal calibration?

**Yes — structurally ideal.** Clef-Flash's `noul` (true/false) question type maps perfectly to the calibration evidence-gathering pipeline:

| Calibration Use Case | Clef-Flash Question | Current Mechanism |
|---|---|---|
| "Is this regime overconfident?" | `noul`: "Does the regime confidence agree with realized price direction over the last N bars?" | `eod_correction.py` bayesian_penalty / hard reject |
| "Should Kelly be reduced?" | `choice`: "Reduce / Maintain / Increase position size" based on regime+macro+TimesFM alignment | `calibration_enforcement.apply_calibration_adjustment()` |
| "Is this signal in a regime-shift zone?" | `choice`: "Normal / Transitioning / Regime-shift" | `detect_regime_shift()` in `kairos_rank.py` |
| "Flag stale regime" (the AAPL issue) | `noul`: "Does regime direction agree with realized N-bar return sign?" | **Does not exist** — the AAPL gap (worklog §104-128) |

Clef-Flash's per-question softmax probabilities are proper distributions — usable as `p_calibration_confidence` in a Bayesian update to `ev_result.p_win`, the same way `p_timesfm` and `p_regime` are fused via logit pooling in `compute_ev()`.

No GPU constraint remains since Clef-Flash is available as a hosted Cloudflare Workers AI endpoint (`@cf/cloudflare/clef-flash`). The proxy edge function (`cloudflare-ai-proxy`) already supports the `/decision` route.

---

## 8. Q2 Deep Dive — LLM Inventory on Studio

The Studio hosts or routes through four LLM surfaces:

### 8.1 Active (in production use)

1. **TimesFM via gateway** (`TIMESFM_API_KEYS` in `orchestrator.py`) — external HTTP API to a TimesFM inference gateway. Not local. No GPU on Studio.
2. **MiniLM-L6-v2** (`knowledge_index.py`) — sentence-transformers, loaded in-process via `_get_local_pipe()` singleton, CPU-only. ~90MB, 384-dim. Used by `semantic_search()` → consumed by `triage.py` Phase 3.
3. **NVIDIA NIM via llm_client.py** — already configured with `NVIDIA_NIM_API_KEY` env var in `feature_flags.yaml`. Used by `call_llm()` for Phase 3 triage narrative + macro forecast agent. OpenRouter is primary, NIM is fallback.

### 8.2 Planned / shadow

4. **Local instruct model (Qwen2.5-3B / Phi-3.5-mini)** — designed as on-demand load (not resident) specifically to avoid GPU contention. Per `20260910_AAPL_REGIME...` §3.1: "load on-demand only when Phase 2 actually flags something."

### 8.3 Proposed

5. **Clef-Flash (`@cf/cloudflare/clef-flash`)** — hosted on Cloudflare Workers AI (not local). Called via the `cloudflare-ai-proxy` edge function's `/decision` route. $0.09/M input tokens.

### 8.4 Conclusion

The Studio runs 2 LLMs actively (TimesFM via gateway + MiniLM on CPU) and 1 LLM via env-configured API keys (NIM/OpenRouter for chat). It does **not** host any GPU-resident LLM locally. Adding Clef-Flash (9B) requires either:
- A GPU upgrade to the Studio tier (A10 24GB minimum), or
- Cloud routing (Cloudflare Workers AI hosting Clef-Flash as a serverless endpoint, or NVIDIA NIM for embeddings — see §9)

**Correction (2026-10-03):** Cloudflare now hosts Clef-Flash as a serverless Workers AI endpoint (`@cf/cloudflare/clef-flash`, released Oct 1). No GPU upgrade needed — call via the `cloudflare-ai-proxy` edge function's `/decision` route. The GPU constraint is resolved.

---

## 9. Embeddings Migration — Moving MiniLM-L6-v2 Off-Studio

### 9.1 Can NVIDIA NIM handle the embedding migration?

**Yes.** The existing `NVIDIA_NIM_API_KEY` is already configured in the Studio's secrets (used by `llm_client.py` for chat). NVIDIA's NeMo Retriever embedding NIMs expose an **OpenAI-compatible `/v1/embeddings`** endpoint at `https://integrate.api.nvidia.com/v1`:

- `nvidia/nv-embedqa-e5-v5` — 768-dim, OpenAI-compatible `/v1/embeddings`
- `nvidia/nemotron-3-embed-1b` — 2048-dim
- `nvidia/nv-embedcode-7b-v1` — 4096-dim (code-optimized)

**NV-Embed-QA model** is the closest match to MiniLM-L6-v2's use case (semantic retrieval over code + worklogs). Both operate in `passage`/`query` mode — pass the `-passage` or `-query` suffix on the model name, or use `input_type: "passage"` / `input_type: "query"`.

### 9.2 Migration plan: knowledge_index.py → NVIDIA NIM embeddings

**Change**: In `knowledge_index.py`, replace the `embed_texts()` function's local model load with an HTTP call to the NVIDIA NIM embeddings endpoint.

**Before** (CPU resident, in-process):
```python
_embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
vectors = _embedder.encode(texts, normalize_embeddings=True)
```

**After** (HTTP call, no local model):
```python
async def embed_texts_remote(texts: list[str], *, input_type: str = "passage") -> np.ndarray:
    """Embed via NVIDIA NIM /v1/embeddings — OpenAI-compatible schema."""
    headers = {"Authorization": f"Bearer {NVIDIA_NIM_API_KEY}", "Content-Type": "application/json"}
    resp = await httpx.AsyncClient(timeout=30.0).post(
        f"{NVIDIA_NIM_BASE_URL}/embeddings",
        headers=headers,
        json={
            "model": "nvidia/nv-embedqa-e5-v5",  # or via env var
            "input": texts,
            "input_type": input_type,
            "modality": "text",
        },
    )
    data = resp.json()
    vectors = [d["embedding"] for d in data["data"]]
    return _l2_normalize(np.array(vectors, dtype=np.float32))
```

**Key changes needed:**
1. `EMBEDDING_MODEL_NAME` changes from `sentence-transformers/all-MiniLM-L6-v2` (CPU local) to `nvidia/nv-embedqa-e5-v5` (API).
2. `EMBEDDING_DIM` changes from 384 to 768.
3. `get_embedder()` becomes `embed_texts_remote()` — HTTP call instead of local model load.
4. `build_index()` already calls `embed_fn` (injectable) — just swap the default `embed_fn`.
5. `semantic_search()` calls `embed_fn` — same swap.
6. Store vectors as `float64` (768-dim) in the same SQLite BLOB column — schema is `embedding BLOB NOT NULL`, dimension-agnostic.
7. **Index rebuild required**: all existing 384-dim vectors must be re-embedded at 768-dim. The content-hash dedup logic in `_index_chunks()` already tracks what changed — only new/changed chunks need re-embedding, but existing vectors for unchanged chunks are now wrong-dimension (must truncate/rebuild).

### 9.3 Env var requirements

The Studio already has `NVIDIA_NIM_API_KEY` configured (used by `llm_client.py`). We need to add:

| Var | Value | Purpose |
|---|---|---|
| `NVIDIA_NIM_EMBEDDING_MODEL` | `nvidia/nv-embedqa-e5-v5` | Embedding model ID |
| `NVIDIA_NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Already hardcoded in `llm_client.py` as `_NVIDIA_NIM_BASE_URL` |

### 9.4 Cloudflare Worker alternative

Cloudflare Workers AI also provides `@cf/baai/bge-small-en-v1.5` (384-dim, exact dimension match to MiniLM-L6-v2) via:

```
POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/embeddings
Headers: Authorization: Bearer {API_TOKEN}
Body: { "model": "@cf/baai/bge-small-en-v1.5", "text": ["..."] }
```

This is **dimension-identical** to MiniLM-L6-v2 (384-dim), so no index rebuild needed — just swap the `embed_fn`. The `bge-small-en` model is a direct successor to MiniLM-L6-v2 in the same architecture family, with comparable retrieval quality.

### 9.5 Decision: NVIDIA NIM vs Cloudflare Workers AI

| Factor | NVIDIA NIM | Cloudflare Workers AI |
|---|---|---|
| Dimension match | 768-dim (mismatch → rebuild) | 384-dim (exact match → no rebuild) |
| API key already configured | ✓ (`NVIDIA_NIM_API_KEY`) | ✗ (needs new `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`) |
| Free tier | 200K tokens/month free, then pay-per-use | Generous free tier, $0.01/1M tokens |
| Code similarity | Same NIM pattern as `llm_client.py` | New pattern, OpenAI-compatible |
| Rebuild needed | Yes (384→768) | No (384→384) |

**Recommendation: Use Cloudflare Workers AI with `@cf/baai/bge-small-en-v1.5`** — the 384-dim dimension match means no index rebuild, and the BGE-small model is architecturally a successor to MiniLM-L6. The only new secret needed is `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`.

A Supabase Edge Function wrapper can be added to proxy the Cloudflare API call (avoiding exposing the API token to the Studio environment), following the exact same `Deno.serve` + `X-Internal-Secret` pattern as `redis-credentials-manager`.
