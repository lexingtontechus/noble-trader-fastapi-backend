#!/usr/bin/env node
/**
 * Agent Signal Stream Listener
 * Subscribe to the per-plan Redis streams `signals:signal_scout` and
 * `signals:precision_pro` and print heartbeats + True Signals as they arrive.
 *
 * Fix #18 (2026-07-22): migrated off the deprecated `agent:*` pub/sub channel.
 *   - Old: `PSUBSCRIBE agent:*` (pub/sub — fire-and-forget, no replay, no
 *     per-plan routing).
 *   - New: `XREAD` from `signals:signal_scout` + `signals:precision_pro`
 *     (Redis Streams — the per-plan fan-out written by the StreamSplitter
 *     service wired in C4). Each stream entry wraps the original agent
 *     payload in a `payload` field and adds `plan_id` / `plan_name` /
 *     `split_at` / `source_entry_id` routing metadata.
 *
 * This is a debug/monitoring tool — it tail-reads NEW entries only
 * (XREAD BLOCK $ ...), so it does not replay history and does not
 * XACK (it doesn't join a consumer group; multiple listeners can run
 * concurrently without stealing each other's messages).
 *
 * Usage:
 *   node scripts/agent_listener.mjs                 # all symbols, both plans
 *   node scripts/agent_listener.mjs BTC             # filter to BTC only
 *   node scripts/agent_listener.mjs BTC signal_scout  # filter symbol + plan
 *
 * Env vars:
 *   UPSTASH_REDIS_REST_URL     e.g. https://<name>.upstash.io
 *   UPSTASH_REDIS_REST_TOKEN   <token>
 *
 * Depends on: npm install redis
 */

import { createClient } from "redis";

const symbolFilter = process.argv[2]?.toUpperCase(); // optional: filter by symbol
const planFilter = process.argv[3]?.toLowerCase();   // optional: signal_scout | precision_pro

// Per-plan streams written by StreamSplitter (see regime_platform/services/stream_splitter.py).
const ALL_STREAMS = [
  { key: "signals:signal_scout",   plan: "signal_scout"   },
  { key: "signals:precision_pro",  plan: "precision_pro"  },
];

const streams = planFilter
  ? ALL_STREAMS.filter(s => s.plan === planFilter)
  : ALL_STREAMS;

if (streams.length === 0) {
  console.error(`Unknown plan filter: '${planFilter}'. Valid: signal_scout, precision_pro`);
  process.exit(1);
}

// Upstash Redis RESP connection (construct from REST URL)
// REST: https://{name}.upstash.io
// RESP: rediss://default:{TOKEN}@{name}.upstash.io:6379
const REST_URL = process.env.UPSTASH_REDIS_REST_URL;
const TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN;

if (!REST_URL || !TOKEN) {
  console.error("Missing UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN");
  console.error("Set env vars or create a .env file with:");
  console.error("  UPSTASH_REDIS_REST_URL=https://<name>.upstash.io");
  console.error("  UPSTASH_REDIS_REST_TOKEN=<token>");
  process.exit(1);
}

const name = REST_URL.replace("https://", "").replace(".upstash.io", "");
const RESP_URL = `rediss://default:${TOKEN}@${name}.upstash.io:6379`;

const client = createClient({ url: RESP_URL });

client.on("error", (err) => console.error("Redis error:", err.message));
client.on("connect", () => console.log(`Connected to ${name}.upstash.io`));

await client.connect();

console.log(
  `\nTail-reading streams (new entries only, no replay):\n  ${streams.map(s => s.key).join("\n  ")}` +
  (symbolFilter ? `\n  symbol filter: ${symbolFilter}` : "") +
  `\nWaiting for messages (Ctrl+C to stop)...\n`
);

// XREAD BLOCK on multiple streams. We start each stream at "$" (latest only)
// — this is a debug tool, not a durable consumer, so we don't replay history.
// Use a long block (5s) so we can react to Ctrl+C promptly.
const BLOCK_MS = 5000;
const streamKeys = streams.map(s => s.key);
const planByKey = Object.fromEntries(streams.map(s => [s.key, s.plan]));

async function loop() {
  // Cursor per stream — "$" means "only entries that arrive after I connect".
  const ids = streamKeys.map(() => "$");

  while (true) {
    let resp;
    try {
      // xReadBlock(keyArray, idArray, blockMs) — node-redis v4+ API.
      resp = await client.xReadBlock(streamKeys, ids, BLOCK_MS);
    } catch (err) {
      console.error("XREAD error:", err.message);
      await new Promise(r => setTimeout(r, 1000));
      continue;
    }

    if (!resp) continue; // timed out with no new entries

    for (const entry of resp) {
      const streamKey = entry.name;     // stream name (e.g. "signals:signal_scout")
      const messages = entry.messages;  // [{ id, message: { field: value } }, ...]
      const planLabel = planByKey[streamKey] ?? "?";

      for (const m of messages) {
        // Advance cursor past this entry so the next XREAD gets the next one.
        const idx = streamKeys.indexOf(streamKey);
        if (idx >= 0) ids[idx] = m.id;

        // StreamSplitter writes the original agent payload as a JSON string
        // in the `payload` field. Other fields (plan_id, plan_name,
        // split_at, source_entry_id) are routing metadata.
        const fields = m.message ?? {};
        const payloadStr = fields.payload;
        let data;
        try {
          data = JSON.parse(payloadStr);
        } catch {
          console.log(
            `[${new Date().toISOString()}] 📡 ${streamKey} ${m.id}: ` +
            `(unparseable payload) ${String(payloadStr ?? "").slice(0, 200)}`
          );
          continue;
        }

        // Optional symbol filter
        if (symbolFilter && data.symbol && data.symbol.toUpperCase() !== symbolFilter) {
          continue;
        }

        const ts = data.ts ? new Date(data.ts).toISOString() : new Date().toISOString();
        const planTag = `[${planLabel}]`;

        if (data.type === "heartbeat") {
          console.log(
            `[${ts}] ${planTag} ❤️  ${data.symbol} | ${data.regime} (conf=${data.regime_conf})`
          );
        } else if (data.type === "full" || data.signal) {
          // True Signal (full config payload)
          console.log(
            `[${ts}] ${planTag} 🚀 ${data.symbol} | SIGNAL=${data.signal} | EV=${
              data.ev_per_dollar
            } | ${data.regime} (conf=${data.regime_conf})` +
              (data.aggression ? ` | ${data.aggression}` : "")
          );
        } else {
          console.log(
            `[${ts}] ${planTag} 📡 ${streamKey} ${m.id}:`,
            JSON.stringify(data).slice(0, 200)
          );
        }
      }
    }
  }
}

// Keep alive
process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  await client.quit();
  process.exit(0);
});

loop().catch((err) => {
  console.error("Listener loop crashed:", err);
  process.exit(1);
});
