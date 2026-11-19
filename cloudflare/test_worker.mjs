// Syntax + contract smoke test for worker.js (no network).
// Run: node cloudflare/test_worker.mjs
// Updated 2026-11-18 for the required-auth + model-allowlist security fix:
// CF_WORKER_SECRET is mandatory (fail closed when unset); only allowlisted
// model identifiers may reach the token-bearing Cloudflare API path.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(new URL("./worker.js", import.meta.url), "utf8");

// Strip the ESM export so it can be evaluated in a plain script context.
const body = src.replace(/export default\s*\{/, "const __default = {");
const sandbox = { console, Response, Request, URL, fetch: async () => ({}) };
vm.createContext(sandbox);
new vm.Script(body + "\n;globalThis.__default = __default;", { filename: "worker.js" })
  .runInContext(sandbox);

const worker = sandbox.__default;
const results = [];
const check = (name, cond) => results.push(`${cond ? "PASS" : "FAIL"}  ${name}`);

check("module evaluates (syntax ok)", typeof worker?.fetch === "function");

const SECRET = "s3cret";
const env = { CF_WORKER_SECRET: SECRET };
const authed = (path, bodyObj) =>
  new Request(`https://x${path}`, {
    method: "POST",
    headers: { "x-worker-secret": SECRET, "Content-Type": "application/json" },
    body: JSON.stringify(bodyObj ?? {}),
  });

// ── fail closed when the secret is not provisioned ───────────────────────
const noSecret = await worker.fetch(
  new Request("https://x/decision", { method: "POST", body: "{}" }), {}, {});
check("no secret configured -> 503 (fail closed)", noSecret.status === 503);
const noSecretRoot = await worker.fetch(new Request("https://x/"), {}, {});
check("no secret configured -> usage also gated (503)", noSecretRoot.status === 503);

// ── routing: unknown path returns the usage payload (authed) ─────────────
const usage = await worker.fetch(
  new Request("https://x/", { headers: { "x-worker-secret": SECRET } }), env, {});
check("authed unknown path -> 200", usage.status === 200);
const usageBody = await usage.json();
check("usage lists /decision", "POST /decision" in usageBody.routes);

// ── missing questions -> 400 JSON (not a 500) ────────────────────────────
const bad = await worker.fetch(authed("/decision", { state: { a: 1 } }), env, {});
check("missing questions -> 400", bad.status === 400);
check("missing questions -> JSON content-type",
  (bad.headers.get("content-type") || "").includes("application/json"));

// ── model allowlist: caller-controlled model must not reach the API path ─
const evilEmbed = await worker.fetch(
  authed("/embeddings", { input: "hi", model: "../../accounts/other/ai/run/@cf/evil" }), env, {});
check("non-allowlisted embeddings model -> 400", evilEmbed.status === 400);
const evilDecision = await worker.fetch(
  authed("/decision", { state: { a: 1 }, questions: { Q: { type: "noul" } }, model: "@cf/evil/model" }), env, {});
check("non-allowlisted decision model -> 400", evilDecision.status === 400);

// ── upstream failure is caught and surfaced as structured JSON ───────────
// env has neither AI nor CF_API_TOKEN -> _callAi throws -> handler must
// return JSON, proving the await + try/catch wiring AND that the default
// (allowlisted) model passes validation.
const noCfg = await worker.fetch(
  authed("/decision", { state: { a: 1 }, questions: { Q: { type: "noul" } } }), env, {});
check("upstream failure -> JSON (await wiring works)",
  (noCfg.headers.get("content-type") || "").includes("application/json"));
const noCfgBody = await noCfg.json();
check("upstream failure carries error text", typeof noCfgBody.error === "string");
check("upstream failure is 502 (no AI binding, no token)", noCfg.status === 502);

// ── auth gate ────────────────────────────────────────────────────────────
const denied = await worker.fetch(
  new Request("https://x/decision", { method: "POST", body: "{}" }),
  { CF_WORKER_SECRET: "s3cret" },
  {},
);
check("wrong secret -> 401", denied.status === 401);

console.log(results.join("\n"));
const failed = results.filter((r) => r.startsWith("FAIL")).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
