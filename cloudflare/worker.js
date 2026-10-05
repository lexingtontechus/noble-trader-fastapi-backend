// =============================================================================
// Cloudflare Worker: embeddings-worker (nobletradingapp)
// =============================================================================
// Free-tier Cloudflare Worker that proxies embedding & decision inference to
// Workers AI via the env.AI binding (when available) or via the Cloudflare
// API with a token stored as a secret (fallback). Replaces the local
// MiniLM-L6-v2 embedding model in knowledge_index.py with
// @cf/baai/bge-small-en-v1.5 (384-dim, exact dimension match → no index rebuild).
//
// DEPLOYMENT:
//   npx wrangler deploy worker.js --no-bundle
//   npx wrangler secret put CF_WORKER_SECRET       # optional auth for the Studio caller
//   npx wrangler secret put CF_API_TOKEN           # Cloudflare API token (fallback if no AI binding)
//
// Worker URL after deploy: https://nobletradingapp.lexington.workers.dev
// Set that as CF_EMBEDDINGS_URL on the Studio (Studio Secret).
//
// The worker auto-detects whether the env.AI binding is available:
//   - If env.AI is set  → use it (no token needed, true free-tier edge compute)
//   - If env.AI is NOT set but CF_API_TOKEN is → call Cloudflare API directly
//   - If neither is available → return 503 with a helpful error
//
// ROUTES:
//   POST /embeddings
//     Body: { "input": string | string[], "model"?: string }
//     → Cloudflare AI: @cf/baai/bge-small-en-v1.5 (384-dim)
//     Returns: { "data": [...], "model": string, "usage": {...} }
//
//   POST /decision
//     Body: { "state": {...}, "questions": {...} }
//     → Cloudflare AI: @cf/cloudflare/clef-flash
//     Returns: { "answers": {...}, "model": string, "usage": {...} }
// =============================================================================

const CLOUDFLARE_ACCOUNT_ID = "e787a0b54e75a41d1c25d8a9bc9f8809";
const CF_API_BASE = `https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/run`;

export default {
  async fetch(request, env, ctx) {
    // ── CORS + auth ──────────────────────────────────────────────────────
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, x-worker-secret",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    // Optional auth: if CF_WORKER_SECRET is set, require matching header
    if (env.CF_WORKER_SECRET) {
      const provided = request.headers.get("x-worker-secret");
      if (provided !== env.CF_WORKER_SECRET) {
        return new Response(JSON.stringify({ error: "Unauthorized" }), {
          status: 401,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/^\//, "");

    try {
      if (path === "embeddings") {
        return await _handleEmbeddings(request, env, corsHeaders);
      }
      if (path === "decision") {
        return await _handleDecision(request, env, corsHeaders);
      }

      // Default: return usage info
      return new Response(
        JSON.stringify({
          status: "ok",
          routes: {
            "POST /embeddings": "BGE-small-en-v1.5 (384-dim, OpenAI-compatible)",
            "POST /decision": "Clef-Flash (9B decision model)",
          },
          default_embedding_model: "@cf/baai/bge-small-en-v1.5",
          default_decision_model: "@cf/cloudflare/clef-flash",
          ai_binding: env.AI ? "enabled" : "not configured (using API fallback)",
        }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      return new Response(JSON.stringify({ error: msg }), {
        status: 500,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    }
  },
};

// ─── Helper: call AI via env.AI binding or Cloudflare API ──────────────────────

async function _callAi(env, model, params) {
  if (env.AI) {
    // Preferred: env.AI binding (no token needed, true edge-free)
    return await env.AI.run(model, params);
  }

  if (env.CF_API_TOKEN) {
    // Fallback: call Cloudflare Workers AI REST API with token
    // The URL uses the full model path, but Clef-Flash expects model="clef-flash" in body
    const apiParams = { ...params };
    if (model === "@cf/cloudflare/clef-flash") {
      apiParams.model = "clef-flash";
    }

    const resp = await fetch(`${CF_API_BASE}/${model}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.CF_API_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(apiParams),
    });

    if (!resp.ok) {
      throw new Error(`Cloudflare AI API returned ${resp.status}: ${await resp.text()}`);
    }
    return await resp.json();
  }

  throw new Error(
    "Neither env.AI binding nor CF_API_TOKEN is configured. " +
    "Add [ai] binding to wrangler.toml (preferred) or set CF_API_TOKEN as a secret.",
  );
}

async function _handleEmbeddings(request, env, corsHeaders) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (!body || !body.input) {
    return new Response(JSON.stringify({ error: "Missing 'input' field" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  const texts = Array.isArray(body.input) ? body.input : [body.input];
  const model = body.model || "@cf/baai/bge-small-en-v1.5";

  // Cloudflare Workers AI: env.AI binding or Cloudflare API via CF_API_TOKEN
  const result = await _callAi(env, model, { text: texts });

  // Normalize Cloudflare response shapes to a flat array of embedding vectors
  // Cloudflare returns: { result: { data: [[...], [...], ...] }, success: true, errors: [] }
  // or via env.AI.run: { data: [[...], [...], ...] }
  let cfResult = result.result || result;
  let embeddings = [];
  if (cfResult.data && Array.isArray(cfResult.data)) {
    if (Array.isArray(cfResult.data[0])) {
      // Batch: [[...], [...]]
      embeddings = cfResult.data;
    } else if (typeof cfResult.data[0] === "number") {
      // Single: [...]
      embeddings = [cfResult.data];
    } else {
      // May be [{ embedding: [...] }, ...]
      embeddings = cfResult.data.map((d) => d.embedding || d);
    }
  } else if (cfResult.shape && Array.isArray(cfResult.shape)) {
    embeddings = [cfResult.data];
  } else if (cfResult.embeddings) {
    embeddings = cfResult.embeddings;
  }

  // L2-normalize (BGE-small doesn't normalize by default; MiniLM did via normalize_embeddings=True)
  embeddings = embeddings.map((vec) => {
    const norm = Math.sqrt(vec.reduce((s, v) => s + v * v, 0));
    return norm > 0 ? vec.map((v) => v / norm) : vec;
  });

  // Return OpenAI shape — single array if input was single, array of arrays if batch
  const data = Array.isArray(body.input)
    ? embeddings.map((e, i) => ({ index: i, embedding: e }))
    : { index: 0, embedding: embeddings[0] };

  return new Response(
    JSON.stringify({
      data,
      model,
      usage: cfResult.usage || {
        prompt_tokens: texts.reduce((s, t) => s + t.length, 0) / 4,
        total_tokens: texts.reduce((s, t) => s + t.length, 0) / 4,
      },
      provider: "cloudflare_workers_ai",
    }),
    { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
}

// ─── Decision handler (Clef-Flash) ─────────────────────────────────────────────

async function _handleDecision(request, env, corsHeaders) {
  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }

  if (!body || !body.state || !body.questions) {
    return new Response(
      JSON.stringify({ error: "Missing 'state' or 'questions' field" }),
      { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  }

  const model = body.model || "@cf/cloudflare/clef-flash";
  const { state, questions } = body;

  // Cloudflare Workers AI Clef-Flash.
  //
  // Verified contract (2026-10-05, live): `questions` is an OBJECT keyed by
  // question id; each value is {type, instructions, criteria?}.
  //   type "choice" → {choice, probabilities{...}, confidence}
  //   type "noul"   → {noul: P(true)}
  //   type "score"  → REJECTED by the model ("Input should be a valid list").
  //                   Do not use `score`; express a rating as `choice`.
  // Wrap the upstream call so an upstream error returns structured JSON with
  // the upstream status, instead of a bare 500.
  let result;
  try {
    result = await _callAi(env, model, { state, questions });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // _callAi throws "Cloudflare AI API returned <status>: <body>" on non-2xx.
    const m = /^Cloudflare AI API returned (\d{3}):/.exec(msg);
    const upstreamStatus = m ? parseInt(m[1], 10) : 502;
    return new Response(
      JSON.stringify({
        error: msg,
        upstream_status: upstreamStatus,
        hint:
          upstreamStatus === 422
            ? "questions must be an object keyed by id; each question needs a " +
              "supported `type` (choice | noul). `score` is rejected by the model."
            : undefined,
      }),
      {
        status: upstreamStatus === 400 || upstreamStatus === 422 ? 400 : 502,
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      },
    );
  }

  // Normalize: Cloudflare API returns { result: { answers: {...} } } or flat { answers: {...} }
  // env.AI.run may return either shape
  const cfResult = result.result || result;
  const answers = cfResult.answers || {};

  return new Response(
    JSON.stringify({
      answers,
      model,
      usage: cfResult.usage || (result.usage) || {},
      provider: "cloudflare_workers_ai",
    }),
    { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } },
  );
}
