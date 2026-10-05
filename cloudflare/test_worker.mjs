// Syntax + contract smoke test for worker.js (no network).
// Run: node cloudflare/test_worker.mjs
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

// ── routing: unknown path returns the usage payload ──────────────────────
const usage = await worker.fetch(new Request("https://x/"), {}, {});
check("unknown path -> 200", usage.status === 200);
const usageBody = await usage.json();
check("usage lists /decision", "POST /decision" in usageBody.routes);

// ── missing questions -> 400 JSON (not a 500) ────────────────────────────
const bad = await worker.fetch(
  new Request("https://x/decision", {
    method: "POST",
    body: JSON.stringify({ state: { a: 1 } }),
  }),
  {},
  {},
);
check("missing questions -> 400", bad.status === 400);
check("missing questions -> JSON content-type",
  (bad.headers.get("content-type") || "").includes("application/json"));

// ── upstream failure is caught and surfaced as structured JSON ───────────
// env has neither AI nor CF_API_TOKEN -> _callAi throws -> handler must
// return JSON, proving the await + try/catch wiring.
const noCfg = await worker.fetch(
  new Request("https://x/decision", {
    method: "POST",
    body: JSON.stringify({ state: { a: 1 }, questions: { Q: { type: "noul" } } }),
  }),
  {},
  {},
);
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
