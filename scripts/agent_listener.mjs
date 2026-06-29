#!/usr/bin/env node
/**
 * Agent Pub/Sub Listener
 * Subscribe to agent:* channels and prints heartbeats and True Signals.
 *
 * Usage:
 *   node scripts/agent_listener.mjs
 *   # Or with specific symbol:
 *   node scripts/agent_listener.mjs BTC
 *
 * Depends on: npm install redis
 */

import { createClient } from "redis";

const symbolFilter = process.argv[2]?.toUpperCase(); // optional: filter by symbol

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

const channel = symbolFilter ? `agent:${symbolFilter}` : "agent:*";
console.log(`\nSubscribing to ${channel}...`);
console.log("Waiting for messages (Ctrl+C to stop)...\n");

const subscriber = client.duplicate();
await subscriber.connect();

await subscriber.pSubscribe(channel, (message, channel) => {
  try {
    const data = JSON.parse(message);
    const ts = new Date(data.ts).toISOString();
    
    if (data.type === "heartbeat") {
      console.log(
        `[${ts}] ❤️  ${data.symbol} | ${data.regime} (conf=${data.regime_conf})`
      );
    } else if (data.type === "full" || data.signal) {
      // True Signal (full config payload)
      console.log(
        `[${ts}] 🚀 ${data.symbol} | SIGNAL=${data.signal} | EV=${
          data.ev_per_dollar
        } | ${data.regime} (conf=${data.regime_conf})` +
          (data.aggression ? ` | ${data.aggression}` : "")
      );
    } else {
      console.log(`[${ts}] 📡 ${channel}:`, JSON.stringify(data).slice(0, 200));
    }
  } catch {
    console.log(`[${new Date().toISOString()}] 📡 ${channel}: ${message.slice(0, 200)}`);
  }
});

// Keep alive
process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  await subscriber.quit();
  await client.quit();
  process.exit(0);
});
