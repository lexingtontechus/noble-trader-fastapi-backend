// ─── Unified Sweep Edge Function (v1 — Option F) ───────────────
// Self-contained: _shared/utils.ts code inlined for deployment via
// Supabase Management API PATCH endpoint.
//
// Single function that replaces heavy-sweep + light-regime.
// Mode-switched via request body:
//   {"mode":"heavy","symbol":"BTC"}            — single-symbol heavy sweep
//   {"mode":"light","symbol":"BTCUSD"}         — single-symbol light regime
//   {"mode":"light","batch":["BTC","ETH","SOL"]} — batch light regime
//   {"action":"sync-allowlist"}                — admin: sync Redis allowlist
//   {"action":"sync-calibration"}              — admin: compute + write calibration bias for all symbols
//   {"warmup":true}                            — pre-boot the worker
//
// ── Two-Layer Concurrency Control ──
// Layer 1: Per-symbol lock (lock:sweep:{sym})
//   - Heavy TTL: 360s, Light TTL: 90s
//   - Kills same-symbol double-fire (e.g., heavy + light on SOLUSD)
//   - Atomic SET NX EX — first caller wins, others skip instantly
//
// Layer 2: Global concurrency cap (sorted set: sweep:active)
//   - Cap = 4 total concurrent invocations (heavy + light combined)
//   - Stale entries (>300s old) auto-evicted on each acquire
//   - EXPIRE 330s on whole key as safety net
//   - Kills 504 timeouts from edge runtime exhaustion
//
// Deferred jobs (symbol-locked or cap-reached) are picked up by
// the existing retry-stale-symbols cron job (every 5 min).
//
// Trigger: pg_cron via direct net.http_post (see migration 015)
// Or direct: POST with the body shapes above.


// ════════════════════════════════════════════════════════════════
// SECTION 1: Inlined _shared/utils.ts
// ════════════════════════════════════════════════════════════════

// ─── Shared Utilities for Supabase Edge Functions ───────────
// Deno-compatible (no npm imports, uses fetch + Web APIs)
//
// BETA CPU CONSTRAINTS (0.5 CPU Render tier):
//   - Sweep window:  45d (1080 hourly bars) — was 90d
//   - Brick sizes:   3 (1.0×, 1.5×, 2.0×)  — was 5 (0.75×–3.0×)
//   - Combos/symbol: ~72 (3 bricks × 4 SL × 6 TP) — was ~120
//   - Optimize call: ~40-60s on 0.5 CPU — was ~90-120s at 90d/5 bricks
//
//   UPGRADE PATH (≥1 CPU on Render):
//     1. SWEEP_CANDLE_HOURS: 1080 → 2160 (45d → 90d)
//     2. SWEEP_YAHOO_RANGE: "45d" → "90d"
//     3. generateBrickSizes: add 0.75× and 3.0× back (3 → 5 bricks)
//     4. sweep_window labels: "45d" → "90d"
//     5. Optimize timeout: 90s → 120s (more data = longer compute)

// ─── Environment ────────────────────────────────────────────
const UPSTASH_URL = Deno.env.get("UPSTASH_REDIS_REST_URL")!;
const UPSTASH_TOKEN = Deno.env.get("UPSTASH_REDIS_REST_TOKEN")!;
const SUPABASE_URL = Deno.env.get("SB_URL") ?? Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE = Deno.env.get("SB_SERVICE_ROLE_KEY") ?? Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const NOBLE_TRADER_URL = Deno.env.get("NOBLE_TRADER_URL") ?? "https://noble-trader-fastapi-backend.onrender.com";
const TIMESFM_INFERENCE_URL = Deno.env.get("TIMESFM_INFERENCE_URL") ?? "";
const TIMESFM_API_KEYS = Deno.env.get("TIMESFM_API_KEYS") ?? "";

// ─── Types ──────────────────────────────────────────────────
export interface SymbolConfig {
  id: number;
  symbol: string;
  asset_class: string;
  data_source: string;        // "hyperliquid" | "alpaca" | "yahoo"
  yahoo_ticker: string | null;
  hyperliquid_coin: string | null;
  alpaca_symbol: string | null;  // e.g. "AAPL", "BTC/USD", "GLD"
  sweep_cron: string;
  regime_cron: string;
  active: boolean;
  heartbeat_ttl_seconds: number;
}

export interface RedisConfig {
  regime: string;
  regime_conf: number;
  brick_size: number;
  tp_bricks: number;
  sl_bricks: number;
  markov_p_up: number;
  markov_p_dn: number;
  kelly_f: number;
  last_update: number;
  sweep_timestamp?: number;
  sweep_window?: string;
  sweep_sharpe?: number;
  sweep_trades?: number;
  signal?: string;        // "buy" | "sell" | "neutral"
  entry_price?: number;   // Current market price at signal
  stop_loss?: number;     // Price if market goes against signal
  take_profit?: number;   // Price if market goes in favor
  price_decimals?: number; // Tick-size decimal places for this symbol

  // EV Engine outputs (NEW)
  ev?: number;                // Raw EV in price units
  ev_per_dollar?: number;     // Normalized EV per $1 risked
  p_win?: number;             // Blended P_win (0-1)
  p_regime?: number;          // Regime component of P_win
  p_imbalance?: number;       // L2 imbalance component (0.50 if no data)
  p_markov?: number;          // Markov transition component
  aggression?: string;        // "passive" | "mid" | "aggressive"
  ev_scale?: number;          // Kelly scaling factor from EV
  effective_kelly?: number;   // kelly_f * ev_scale
  markov_current_state?: string;  // Current Markov state ("UP"/"DOWN"/"FLAT")

  // Regime Shift fields (v2.3 — Shift-Aware EV)
  regime_shift?: string;      // "true" | "false" — did a shift occur this cycle?
  prev_regime?: string;       // Regime we transitioned FROM (e.g. "low_vol_bull")
  shift_at?: number;          // Unix ms when shift was detected (timestamp approach)
  shifts_24h?: number;        // Count of shifts in last 24h (volatility meta-signal)

  // TimesFM fields (v3)
  p_timesfm?: number;         // Raw TimesFM forecast probability (0-1)
  timesfm_horizon?: string;   // Forecast window label (e.g. "12h")
  timesfm_conf?: number;      // TimesFM model confidence (0-1)

  // Calibration fields (v3)
  calibration_bias?: number;   // avg(predictedPWin) - actual_win_rate
  calibration_status?: string; // "CALIBRATED" | "OVERCONFIDENT" | "UNDERCONFIDENT"

  // Tail risk fields (v3)
  tail_risk_score?: number;    // 0.0 (none) - 1.0 (critical)
  tail_risk_action?: string;   // "none" | "reduce_25" | "reduce_50" | "skip"
}

// Noble Trader /regime/detect actual response
export interface RegimeResult {
  symbol: string;
  vol_probs: Record<string, number>;
  trend_probs: Record<string, number>;
  vol_state: string;           // "low" | "med_low" | "med_high" | "high"
  trend_state: string;         // "strong_bear" | "bear" | "neutral" | "bull" | "strong_bull"
  regime_label: string;        // e.g. "high_vol_strong_bull"
  confidence: number;          // 0.0 - 1.0
  risk_multiplier: number;     // 0.2 - 1.0
  n_bars_fitted: number;
}

// Noble Trader /size/kelly actual response
export interface KellyResult {
  symbol: string;
  full_kelly_f: number;
  fractional_f: number;
  vol_scaled_f: number;
  regime_gated_f: number;
  recommended_f: number;
  kelly_edge: number;
  sharpe_ratio: number;
  regime_label: string;
  regime_multiplier: number;
  fraction_type: string;
  notes: string[];
}

export interface BacktestResult {
  symbol: string;
  n_trades: number;
  win_rate: number;
  total_return_pct: number;
  sharpe_estimate: number;
  max_drawdown_bricks: number;
  total_pnl_bricks: number;
  profit_factor: number;
  avg_pnl_bricks: number;
  avg_win_bricks: number;
  avg_loss_bricks: number;
  kelly_inputs: { win_rate: number; avg_win: number; avg_loss: number; implied_kelly: number };
  raw: Record<string, unknown>;
}

// ─── Price Precision (Tick Sizes) ──────────────────────────
// Ensures entry/SL/TP are rounded to valid exchange price levels.
// MUST be defined before redisSetConfig (used for price formatting).
//
// Hyperliquid perps: max 5 significant figures, max (6 - szDecimals) decimal places
//   https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size
//   Integer prices always allowed regardless of sig figs.
// Stocks: penny ticks (2 decimals)
// Forex: sub-pip (5 decimals)
// Commodities: contract-specific

// Max decimal places per symbol (for toFixed formatting)
export const PRICE_TICK_DECIMALS: Record<string, number> = {
  // ── Hyperliquid perps: max_decimals = 6 - szDecimals ──
  BTC:   1,    // szDecimals=5 → max 1 decimal,  5 sig figs
  ETH:   2,    // szDecimals=4 → max 2 decimals, 5 sig figs
  SOL:   4,    // szDecimals=2 → max 4 decimals, 5 sig figs
  // ── US stocks (penny ticks) ──
  AAPL:  2, AMZN: 2, GOOGL: 2, META: 2,
  MSFT:  2, NVDA:  2, TSLA:  2,
  SPY:   2, QQQ:   2,
  // ── Alpaca crypto spot ──
  BTCUSD: 1,   // same tick as BTC perp
  ETHUSD: 2,   // same tick as ETH perp
  SOLUSD: 4,   // same tick as SOL perp
};

/** Set of Hyperliquid perp symbols for sig-fig rounding */
const HL_PERP_SYMBOLS = new Set(["BTC", "ETH", "SOL"]);

/** Default decimal places by asset class (fallback if symbol not in map) */
const DEFAULT_TICK_DECIMALS: Record<string, number> = {
  crypto:      6,    // Hyperliquid max for low-priced perps
  stocks:      2,
  forex:       5,
  commodities: 2,
};

/**
 * Round a price for a Hyperliquid perp.
 * Rules (from https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size):
 *   - Max 5 significant figures
 *   - Max (6 - szDecimals) decimal places
 *   - Integer prices are always allowed regardless of sig figs
 *
 * Algorithm:
 *   1. Calculate how many decimal places 5 sig figs allows (4 - magnitude)
 *   2. Take the lesser of sig-fig decimals and max_decimals
 *   3. Round to that many decimal places
 */
function roundHyperliquidPrice(price: number, maxDecimals: number): number {
  if (price === 0) return 0;

  // If price is a whole number, return as-is (integer prices always allowed)
  if (Number.isInteger(price)) return price;

  const absPrice = Math.abs(price);

  // Calculate magnitude (position of most significant digit)
  // e.g. 70425 → 4, 1997 → 3, 80 → 1, 0.10162 → -1, 0.001234 → -3
  let magnitude: number;
  if (absPrice >= 1) {
    magnitude = Math.floor(Math.log10(absPrice));
  } else {
    // For small numbers, count leading zeros after decimal point
    magnitude = -1;
    const s = absPrice.toPrecision(20);
    let i = s.indexOf(".") + 1;
    while (i < s.length && s[i] === "0") {
      magnitude -= 1;
      i++;
    }
  }

  // For 5 sig figs: sig_fig_decimals = 4 - magnitude
  // e.g. magnitude=4 (BTC 70425) → 4-4=0 decimals → round to integer
  // e.g. magnitude=3 (ETH 1997)  → 4-3=1 decimal
  // e.g. magnitude=1 (SOL 80)    → 4-1=3 decimals
  // e.g. magnitude=-1 (ARB 0.1)  → 4-(-1)=5 decimals
  const sigFigDecimals = 4 - magnitude;
  const actualDecimals = Math.max(0, Math.min(sigFigDecimals, maxDecimals));

  const factor = Math.pow(10, actualDecimals);
  return Math.round(price * factor) / factor;
}

/**
 * Round a price to the tick-size decimal places for a given symbol.
 * - Crypto (Hyperliquid): 5 significant figures, max (6 - szDecimals) decimals
 * - Other assets: simple decimal rounding
 */
export function roundPrice(price: number, symbol: string, assetClass?: string): number {
  if (price === 0) return 0;

  const maxDecimals = PRICE_TICK_DECIMALS[symbol]
    ?? DEFAULT_TICK_DECIMALS[assetClass ?? ""]
    ?? 2;

  // Hyperliquid perps use sig-fig rules
  if (HL_PERP_SYMBOLS.has(symbol)) {
    return roundHyperliquidPrice(price, maxDecimals);
  }

  // Simple decimal rounding for stocks/forex/commodities
  const factor = Math.pow(10, maxDecimals);
  return Math.round(price * factor) / factor;
}

// ─── CORS Headers ───────────────────────────────────────────
export function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  };
}

// ─── Upstash Redis ──────────────────────────────────────────
async function redisPipeline(commands: unknown[][]): Promise<unknown> {
  const res = await fetch(`${UPSTASH_URL}/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${UPSTASH_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(commands),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Redis pipeline error: ${res.status} — ${text}`);
  }

  return res.json();
}

async function redisCommand(cmd: string, ...args: (string | number)[]): Promise<unknown> {
  const res = await fetch(`${UPSTASH_URL}/${cmd}/${args.join("/")}`, {
    headers: {
      Authorization: `Bearer ${UPSTASH_TOKEN}`,
    },
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Redis ${cmd} error: ${res.status} — ${text}`);
  }

  const data = await res.json();
  return data.result;
}

export async function redisSetConfig(symbol: string, config: RedisConfig): Promise<void> {
  // Determine tick-size decimals for this symbol (for price formatting)
  const decimals = config.price_decimals
    ?? PRICE_TICK_DECIMALS[symbol]
    ?? DEFAULT_TICK_DECIMALS["crypto"] // fallback
    ?? 2;

  // ── NaN Guard: never write NaN to Redis ──
  // NaN in Redis creates a feedback loop — "NaN" is truthy so
  // parseFloat("NaN") returns NaN but the fallback never triggers.
  // Replace NaN with sensible defaults before writing.
  const safeConfig = {
    regime: config.regime || "unknown",
    regime_conf: Number.isNaN(config.regime_conf) ? 0 : config.regime_conf,
    brick_size: Number.isNaN(config.brick_size) ? 0.5 : config.brick_size,
    tp_bricks: Number.isNaN(config.tp_bricks) ? 5 : config.tp_bricks,
    sl_bricks: Number.isNaN(config.sl_bricks) ? 3 : config.sl_bricks,
    markov_p_up: Number.isNaN(config.markov_p_up) ? 0.5 : config.markov_p_up,
    markov_p_dn: Number.isNaN(config.markov_p_dn) ? 0.5 : config.markov_p_dn,
    kelly_f: Number.isNaN(config.kelly_f) ? 0 : config.kelly_f,
    last_update: Number.isNaN(config.last_update) ? Date.now() : config.last_update,
  };

  const fields: (string | number)[] = [];
  const entries: [string, string | number][] = [
    ["regime", safeConfig.regime],
    ["regime_conf", safeConfig.regime_conf],
    ["brick_size", safeConfig.brick_size],
    ["tp_bricks", safeConfig.tp_bricks],
    ["sl_bricks", safeConfig.sl_bricks],
    ["markov_p_up", safeConfig.markov_p_up],
    ["markov_p_dn", safeConfig.markov_p_dn],
    ["kelly_f", safeConfig.kelly_f],
    ["last_update", safeConfig.last_update],
    ["price_decimals", decimals],
  ];

  if (config.sweep_timestamp !== undefined) {
    // Guard against NaN / negative / zero timestamps — replace with Date.now()
    const ts = config.sweep_timestamp;
    entries.push(["sweep_timestamp", (Number.isNaN(ts) || ts <= 0) ? Date.now() : ts]);
  }
  if (config.sweep_window !== undefined) {
    entries.push(["sweep_window", config.sweep_window]);
  }
  if (config.sweep_sharpe !== undefined) {
    entries.push(["sweep_sharpe", config.sweep_sharpe]);
  }
  if (config.sweep_trades !== undefined) {
    entries.push(["sweep_trades", config.sweep_trades]);
  }
  if (config.signal !== undefined) {
    entries.push(["signal", config.signal]);
  }
  // Format prices with explicit decimal places (e.g. BTC 70942 → "70942.0")
  if (config.entry_price !== undefined) {
    entries.push(["entry_price", config.entry_price.toFixed(decimals)]);
  }
  if (config.stop_loss !== undefined) {
    entries.push(["stop_loss", config.stop_loss.toFixed(decimals)]);
  }
  if (config.take_profit !== undefined) {
    entries.push(["take_profit", config.take_profit.toFixed(decimals)]);
  }

  // EV Engine fields
  if (config.ev !== undefined) {
    entries.push(["ev", Number.isNaN(config.ev) ? 0 : config.ev]);
  }
  if (config.ev_per_dollar !== undefined) {
    entries.push(["ev_per_dollar", Number.isNaN(config.ev_per_dollar) ? 0 : config.ev_per_dollar]);
  }
  if (config.p_win !== undefined) {
    entries.push(["p_win", Number.isNaN(config.p_win) ? 0 : config.p_win]);
  }
  if (config.p_regime !== undefined) {
    entries.push(["p_regime", Number.isNaN(config.p_regime) ? 0 : config.p_regime]);
  }
  if (config.p_imbalance !== undefined) {
    entries.push(["p_imbalance", Number.isNaN(config.p_imbalance) ? 0 : config.p_imbalance]);
  }
  if (config.p_markov !== undefined) {
    entries.push(["p_markov", Number.isNaN(config.p_markov) ? 0 : config.p_markov]);
  }
  if (config.aggression !== undefined) {
    entries.push(["aggression", config.aggression]);
  }
  if (config.ev_scale !== undefined) {
    entries.push(["ev_scale", Number.isNaN(config.ev_scale) ? 0 : config.ev_scale]);
  }
  if (config.effective_kelly !== undefined) {
    entries.push(["effective_kelly", Number.isNaN(config.effective_kelly) ? 0 : config.effective_kelly]);
  }
  if (config.markov_current_state !== undefined) {
    entries.push(["markov_current_state", config.markov_current_state]);
  }

  // Regime Shift fields (v2.3)
  if (config.regime_shift !== undefined) {
    entries.push(["regime_shift", config.regime_shift]);
  }
  if (config.prev_regime !== undefined) {
    entries.push(["prev_regime", config.prev_regime]);
  }
  if (config.shift_at !== undefined) {
    entries.push(["shift_at", Number.isNaN(config.shift_at) || config.shift_at <= 0 ? 0 : config.shift_at]);
  }
  if (config.shifts_24h !== undefined) {
    entries.push(["shifts_24h", Number.isNaN(config.shifts_24h) ? 0 : config.shifts_24h]);
  }

  // TimesFM fields (v3)
  if (config.p_timesfm !== undefined) {
    entries.push(["p_timesfm", Number.isNaN(config.p_timesfm) ? 0 : config.p_timesfm]);
  }
  if (config.timesfm_horizon !== undefined) {
    entries.push(["timesfm_horizon", config.timesfm_horizon]);
  }
  if (config.timesfm_conf !== undefined) {
    entries.push(["timesfm_conf", Number.isNaN(config.timesfm_conf) ? 0 : config.timesfm_conf]);
  }

  // Calibration fields (v3)
  if (config.calibration_bias !== undefined) {
    entries.push(["calibration_bias", Number.isNaN(config.calibration_bias) ? 0 : config.calibration_bias]);
  }
  if (config.calibration_status !== undefined) {
    entries.push(["calibration_status", config.calibration_status]);
  }

  // Tail risk fields (v3)
  if (config.tail_risk_score !== undefined) {
    entries.push(["tail_risk_score", Number.isNaN(config.tail_risk_score) ? 0 : config.tail_risk_score]);
  }
  if (config.tail_risk_action !== undefined) {
    entries.push(["tail_risk_action", config.tail_risk_action]);
  }

  for (const [k, v] of entries) {
    fields.push(k, String(v));
  }

  await redisPipeline([
    ["HSET", `trading:config:${symbol}`, ...fields],
  ]);
}

export async function redisSetHeartbeat(symbol: string, ttlSeconds: number): Promise<void> {
  await redisPipeline([
    ["SET", `trading:heartbeat:${symbol}`, String(Date.now()), "EX", String(ttlSeconds)],
  ]);
}

// ─── Regime Cache (single key, 1 GET instead of N HGETALL) ──────────
// The regime_cache key stores the last-known regime + brick params for ALL
// active symbols in a single JSON string. The edge function does 1 GET at
// the start and 1 SET at the end, rather than HGETALL per symbol per cycle.
// Schema: { "BTC": { "regime":"high_vol_bull", "brick_size":0.005, ... }, "ETH": {...} }

export interface RegimeCacheEntry {
  regime: string;
  regime_conf: number;
  brick_size: number;
  sl_bricks: number;
  tp_bricks: number;
  kelly_f: number;
  last_update: number;
  signal: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  price_decimals: number;

  // EV Engine outputs (NEW)
  ev?: number;
  ev_per_dollar?: number;
  p_win?: number;
  p_regime?: number;
  p_imbalance?: number;
  p_markov?: number;
  aggression?: string;
  ev_scale?: number;
  effective_kelly?: number;
  markov_current_state?: string;

  // Regime Shift fields (v2.3 — Shift-Aware EV)
  regime_shift?: string;      // "true" | "false" — persisted to Redis
  prev_regime?: string;       // Regime transitioned FROM
  shift_at?: number;          // Unix ms when shift detected
  shifts_24h?: number;        // Count of shifts in last 24h

  // TimesFM fields (v3)
  p_timesfm?: number;
  timesfm_horizon?: string;
  timesfm_conf?: number;

  // Calibration fields (v3)
  calibration_bias?: number;
  calibration_status?: string;

  // Tail risk fields (v3)
  tail_risk_score?: number;
  tail_risk_action?: string;
}

export interface RegimeCache {
  [symbol: string]: RegimeCacheEntry | undefined;
}

const REGIME_CACHE_KEY = "trading:regime_cache";

export async function redisLoadRegimeCache(): Promise<RegimeCache> {
  try {
    const result = await redisCommand("GET", REGIME_CACHE_KEY);
    if (result && typeof result === "string") {
      return JSON.parse(result) as RegimeCache;
    }
  } catch (err) {
    console.warn(`[redisLoadRegimeCache] Failed to load cache: ${err}`);
  }
  return {};
}

export async function redisSaveRegimeCache(cache: RegimeCache): Promise<void> {
  try {
    await redisPipeline([
      ["SET", REGIME_CACHE_KEY, JSON.stringify(cache)],
    ]);
  } catch (err) {
    console.warn(`[redisSaveRegimeCache] Failed to save cache: ${err}`);
  }
}

/**
 * Update only the price-dependent fields (signal, entry, stop_loss, take_profit)
 * for a symbol. Called every cycle to keep DOOM's entry prices fresh even when
 * the regime hasn't shifted. Uses a targeted HSET with just 4 fields — much
 * cheaper than rewriting the full hash.
 */
export async function redisUpdateSignal(
  symbol: string,
  signal: string,
  entryPrice: number,
  stopLoss: number,
  takeProfit: number,
  decimals: number,
  evFields?: {
    ev?: number;
    ev_per_dollar?: number;
    p_win?: number;
    aggression?: string;
    ev_scale?: number;
    effective_kelly?: number;
  },
  shiftFields?: {
    regime_shift?: string;
    prev_regime?: string;
    shift_at?: number;
    shifts_24h?: number;
  },
  timesfmFields?: {
    p_timesfm?: number;
    timesfm_horizon?: string;
    timesfm_conf?: number;
  },
  calibrationFields?: {
    calibration_bias?: number;
    calibration_status?: string;
  },
  tailRiskFields?: {
    tail_risk_score?: number;
    tail_risk_action?: string;
  },
): Promise<void> {
  const fields: (string | number)[] = [
    "signal", signal,
    "entry_price", entryPrice.toFixed(decimals),
    "stop_loss", stopLoss.toFixed(decimals),
    "take_profit", takeProfit.toFixed(decimals),
    "last_update", String(Date.now()),
    "sweep_timestamp", String(Date.now()),  // Refresh on every light-regime cycle too
  ];

  // Add EV fields if provided
  if (evFields) {
    if (evFields.ev !== undefined) fields.push("ev", String(evFields.ev));
    if (evFields.ev_per_dollar !== undefined) fields.push("ev_per_dollar", String(evFields.ev_per_dollar));
    if (evFields.p_win !== undefined) fields.push("p_win", String(evFields.p_win));
    if (evFields.aggression !== undefined) fields.push("aggression", evFields.aggression);
    if (evFields.ev_scale !== undefined) fields.push("ev_scale", String(evFields.ev_scale));
    if (evFields.effective_kelly !== undefined) fields.push("effective_kelly", String(evFields.effective_kelly));
  }

  // Add TimesFM fields if provided
  if (timesfmFields) {
    if (timesfmFields.p_timesfm !== undefined) fields.push("p_timesfm", String(timesfmFields.p_timesfm));
    if (timesfmFields.timesfm_horizon !== undefined) fields.push("timesfm_horizon", timesfmFields.timesfm_horizon);
    if (timesfmFields.timesfm_conf !== undefined) fields.push("timesfm_conf", String(timesfmFields.timesfm_conf));
  }

  // Add calibration fields if provided
  if (calibrationFields) {
    if (calibrationFields.calibration_bias !== undefined) fields.push("calibration_bias", String(calibrationFields.calibration_bias));
    if (calibrationFields.calibration_status !== undefined) fields.push("calibration_status", calibrationFields.calibration_status);
  }

  // Add tail risk fields if provided
  if (tailRiskFields) {
    if (tailRiskFields.tail_risk_score !== undefined) fields.push("tail_risk_score", String(tailRiskFields.tail_risk_score));
    if (tailRiskFields.tail_risk_action !== undefined) fields.push("tail_risk_action", tailRiskFields.tail_risk_action);
  }

  // Add regime shift fields if provided
  if (shiftFields) {
    if (shiftFields.regime_shift !== undefined) fields.push("regime_shift", shiftFields.regime_shift);
    if (shiftFields.prev_regime !== undefined) fields.push("prev_regime", shiftFields.prev_regime);
    if (shiftFields.shift_at !== undefined) fields.push("shift_at", String(shiftFields.shift_at));
    if (shiftFields.shifts_24h !== undefined) fields.push("shifts_24h", String(shiftFields.shifts_24h));
  }

  await redisPipeline([
    [
      "HSET",
      `trading:config:${symbol}`,
      ...fields,
    ],
  ]);
}

export async function redisGetConfig(symbol: string): Promise<Record<string, string> | null> {
  const result = await redisCommand("HGETALL", `trading:config:${symbol}`);
  if (!result || !Array.isArray(result) || result.length === 0) return null;

  const obj: Record<string, string> = {};
  for (let i = 0; i < result.length; i += 2) {
    obj[result[i]] = result[i + 1];
  }
  return obj;
}

export async function redisPushError(symbol: string, fnName: string, errorMsg: string): Promise<void> {
  const entry = JSON.stringify({
    ts: Date.now(),
    fn: fnName,
    error: errorMsg,
  });

  await redisPipeline([
    ["LPUSH", `trading:errors:${symbol}`, entry],
    ["LTRIM", `trading:errors:${symbol}`, "0", "9"],
  ]);
}

// ─── Supabase PostgREST ────────────────────────────────────
// ─── Count regime shifts in last 24h from nt_regime_log ───────
// Returns 0 if query fails (fail-open, never block the pipeline).
export async function countShifts24h(symbol: string): Promise<number> {
  try {
    const twentyFourHoursAgo = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const data = await supabaseRest(
      "nt_regime_log",
      "GET",
      undefined,
      `symbol=eq.${symbol}&regime_shift=eq.true&detected_at=gte.${twentyFourHoursAgo}&select=id`,
    );
    if (Array.isArray(data)) return data.length;
  } catch (err) {
    console.warn(`[countShifts24h] ${symbol}: failed — ${err}`);
  }
  return 0;
}

// ─── TimesFM Inference ─────────────────────────────────────
export async function timesfmPost(
  symbolsAndPrices: { symbol: string; prices: number[] }[],
  horizon = 12,
): Promise<Map<string, number | null>> {
  if (!TIMESFM_INFERENCE_URL || !TIMESFM_API_KEYS) {
    return new Map();
  }

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10_000);
    const res = await fetch(`${TIMESFM_INFERENCE_URL}/forecast/batch`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${TIMESFM_API_KEYS}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ symbols: symbolsAndPrices, horizon }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    if (!res.ok) {
      console.warn(`[timesfm] HTTP ${res.status} — ${await res.text().catch(() => "")}`);
      return new Map();
    }

    const data = await res.json() as { results?: Array<{ symbol: string; p_timesfm: number }> };
    const map = new Map<string, number | null>();
    for (const item of data.results ?? []) {
      map.set(item.symbol, item.p_timesfm ?? null);
    }
    return map;
  } catch (err) {
    console.warn(`[timesfm] Request failed: ${err instanceof Error ? err.message : String(err)}`);
    return new Map();
  }
}

// ─── Calibration Bias ──────────────────────────────────────
export async function computeCalibrationBias(
  symbol: string,
): Promise<{ bias: number; status: string } | null> {
  try {
    const data = await supabaseRest(
      "nt_calibration_records",
      "GET",
      undefined,
      `symbol=eq.${symbol}&select=predicted_p_win,outcome`,
    );
    if (!Array.isArray(data) || data.length < 20) {
      return null;
    }

    const n = data.length;
    let sumPred = 0;
    let wins = 0;
    for (const r of data) {
      sumPred += r.predicted_p_win ?? 0.5;
      if (r.outcome === "win") wins++;
    }
    const avgPred = sumPred / n;
    const winRate = wins / n;
    const bias = avgPred - winRate;

    let status: string;
    if (Math.abs(bias) < 0.05) status = "CALIBRATED";
    else if (bias >= 0.05) status = "OVERCONFIDENT";
    else status = "UNDERCONFIDENT";

    return { bias: Math.round(bias * 10000) / 10000, status };
  } catch (err) {
    console.warn(`[calibration] ${symbol}: failed — ${err}`);
    return null;
  }
}

// ─── Supabase PostgREST ────────────────────────────────────
export async function supabaseRest(table: string, method: string, body?: unknown, query?: string): Promise<unknown> {
  const url = `${SUPABASE_URL}/rest/v1/${table}${query ? `?${query}` : ""}`;
  const headers: Record<string, string> = {
    apikey: SUPABASE_SERVICE_ROLE,
    Authorization: `Bearer ${SUPABASE_SERVICE_ROLE}`,
    "Content-Type": "application/json",
  };

  if (method === "POST") {
    headers["Prefer"] = "return=minimal";
  }

  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Supabase ${table} error: ${res.status} — ${text}`);
  }

  if (method === "POST") return null;
  return res.json();
}

/**
 * Same as supabaseRest but with 1 retry on transient errors (503, connection reset).
 * Used for symbol lookups where a momentary PostgREST blip causes false 404s.
 */
async function supabaseRestWithRetry(table: string, method: string, body?: unknown, query?: string, retries = 1): Promise<unknown> {
  try {
    return await supabaseRest(table, method, body, query);
  } catch (err) {
    if (retries <= 0) throw err;
    const msg = err instanceof Error ? err.message : String(err);
    // Only retry on transient errors (503, connection, network), not 404/400
    if (msg.includes("503") || msg.includes("connection") || msg.includes("network") || msg.includes("reset")) {
      console.warn(`[supabaseRestWithRetry] ${table} transient error, retrying: ${msg}`);
      await sleep(1000);
      return supabaseRest(table, method, body, query);
    }
    throw err;
  }
}

export async function getSymbolsByAssetClass(assetClass: string): Promise<SymbolConfig[]> {
  const data = await supabaseRestWithRetry(
    "nt_symbol",
    "GET",
    undefined,
    `asset_class=eq.${assetClass}&active=eq.true&select=*`,
  );
  return data as SymbolConfig[];
}

export async function getSymbolByName(symbol: string): Promise<SymbolConfig | null> {
  const data = await supabaseRestWithRetry(
    "nt_symbol",
    "GET",
    undefined,
    `symbol=eq.${symbol}&active=eq.true&select=*`,
  );
  const arr = data as SymbolConfig[];
  return arr.length > 0 ? arr[0] : null;
}

export async function getAllActiveSymbols(): Promise<SymbolConfig[]> {
  const data = await supabaseRest(
    "nt_symbol",
    "GET",
    undefined,
    "active=eq.true&select=*&order=asset_class.asc,symbol.asc",
  );
  return data as SymbolConfig[];
}

export async function logSweepResult(result: Record<string, unknown>): Promise<void> {
  await supabaseRest("nt_sweep_results", "POST", result);
}

export async function logRegimeEntry(entry: Record<string, unknown>): Promise<void> {
  await supabaseRest("nt_regime_log", "POST", entry);
}

export async function logError(symbol: string, fnName: string, errorMsg: string): Promise<void> {
  await supabaseRest("nt_errors", "POST", {
    symbol,
    fn_name: fnName,
    error_msg: errorMsg.substring(0, 1000),
  });
}

// ─── Noble Trader Backend ───────────────────────────────────
// IMPORTANT: Supabase Edge Functions have a hard 150s timeout.
// We must complete all work within that limit. The old settings
// (3 retries × 65s 429 waits × 120s timeouts) could take up to
// 765s per symbol — way past the limit.
//
// New strategy: fail fast, don't retry 429s (the next cron cycle
// will try again in 15 min anyway). 1 retry on transient errors
// only, with short backoff.

async function nobleTraderPost(path: string, body: unknown, timeoutMs = 30000): Promise<unknown> {
  const maxRetries = 2;          // 1 initial + 1 retry (down from 3)
  const baseDelayMs = 2000;      // 2s base (down from 5s)
  let lastError: Error | null = null;

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch(`${NOBLE_TRADER_URL}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      if (res.status === 429) {
        // Rate limited — DO NOT wait 65s. Supabase will kill us at 150s.
        // Just throw immediately. The next cron cycle will retry.
        const retryAfter = res.headers.get("Retry-After");
        lastError = new Error(
          `Noble Trader ${path}: 429 rate limited (attempt ${attempt + 1}/${maxRetries}). ` +
          `Skipping — next cron cycle will retry. Retry-After: ${retryAfter ?? "unknown"}`
        );
        console.warn(lastError.message);
        clearTimeout(timer);
        // No sleep, no retry on 429 — bail immediately
        break;
      }

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Noble Trader ${path}: ${res.status} — ${text}`);
      }

      return await res.json();
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
      if (attempt < maxRetries - 1) {
        const delayMs = baseDelayMs * Math.pow(2, attempt);
        console.warn(
          `[nobleTraderPost] ${path}: attempt ${attempt + 1} failed, retrying in ${delayMs}ms — ${lastError.message}`
        );
        await sleep(delayMs);
        continue;
      }
      break;
    } finally {
      clearTimeout(timer);
    }
  }

  throw lastError || new Error(`Noble Trader ${path}: all ${maxRetries} retries exhausted`);
}

export async function runRenkoBacktest(
  prices: number[],
  symbol: string,
  brickSize: number,
  slBricks: number,
  tpBricks: number,
  initialCapital = 100000,
): Promise<BacktestResult> {
  const raw = await nobleTraderPost("/renko/backtest/run", {
    prices,
    symbol,
    brick_size: brickSize,
    sl_bricks: slBricks,
    tp_bricks: tpBricks,
    initial_capital: initialCapital,
    commission_bps: 5,
    slippage_bps: 2,
  }) as Record<string, unknown>;

  const stats = (raw as any).stats ?? {};
  const journal = stats.journal ?? {};
  const dollarStats = journal.dollar_stats ?? {};

  return {
    symbol,
    n_trades: journal.total_trades ?? stats.state?.total_trades ?? 0,
    win_rate: journal.win_rate ?? 0,
    total_return_pct: dollarStats.net_return_pct ?? 0,
    sharpe_estimate: journal.sharpe_estimate ?? -999,
    max_drawdown_bricks: journal.max_drawdown_bricks ?? 0,
    total_pnl_bricks: journal.total_pnl_bricks ?? stats.state?.total_pnl_bricks ?? 0,
    profit_factor: journal.profit_factor ?? 0,
    avg_pnl_bricks: journal.avg_pnl_bricks ?? 0,
    avg_win_bricks: journal.avg_win_bricks ?? 0,
    avg_loss_bricks: journal.avg_loss_bricks ?? 0,
    kelly_inputs: journal.kelly_inputs ?? { win_rate: 0, avg_win: 0, avg_loss: 0, implied_kelly: 0 },
    raw,
  };
}

// ─── Optimize Result (from /renko/backtest/optimize) ────────
// Single API call that replaces 60× individual /run calls.
// Critical for Render memory stability — 1 call vs 60.

export interface OptimizeComboResult {
  brick_size: number;
  sl_bricks: number;
  tp_bricks: number;
  total_trades: number;
  win_rate: number;
  sharpe: number;
  profit_factor: number;
  total_pnl_bricks: number;
  max_drawdown_bricks: number;
  cost_summary: Record<string, number>;
  dollar_stats: Record<string, number>;
  raw_p_value?: number;
  bonferroni_adjusted_p?: number;
  bh_adjusted_p?: number;
}

export interface OptimizeResult {
  n_combinations: number;
  results: OptimizeComboResult[];
  best: OptimizeComboResult | null;
  multiple_testing: Record<string, unknown>;
  deflated_sharpe: Record<string, unknown>;
}

/**
 * Run parameter sweep optimization via Noble Trader /renko/backtest/optimize
 *
 * This replaces the old pattern of calling /renko/backtest/run 60× per symbol
 * with a single API call that runs the full grid on the server.
 *
 * Benefits:
 *   - 60× fewer API calls → massive Render memory relief
 *   - Server-side grid execution → consistent results
 *   - Built-in statistical rigor (DSR, Bonferroni, BH corrections)
 *   - min_rr filter reduces noise combos before ranking
 */
export async function runRenkoOptimize(
  prices: number[],
  symbol: string,
  brickSizes: number[],
  slBricksRange: number[],
  tpBricksRange: number[],
  minRR = 2.0,
): Promise<OptimizeResult> {
  // Pre-filter: only send combos with R:R >= minRR
  // The MCP server doesn't have min_rr yet, so we filter client-side
  const filteredSl = slBricksRange;
  const filteredTp = tpBricksRange.filter(tp => {
    // At least one SL in range produces R:R >= minRR with this TP
    return filteredSl.some(sl => tp / sl >= minRR);
  });

  if (filteredTp.length === 0) {
    // Fallback: use full range if filter eliminated everything
    return {
      n_combinations: 0,
      results: [],
      best: null,
      multiple_testing: { summary: { raw_significant: 0 } },
      deflated_sharpe: { dsr: 0.0, interpretation: "No combos with R:R >= " + minRR },
    };
  }

  const raw = await nobleTraderPost("/renko/backtest/optimize", {
    prices,
    symbol,
    brick_sizes: brickSizes,
    sl_bricks_range: filteredSl,
    tp_bricks_range: filteredTp,
    min_rr: minRR,  // Server-side R:R filter (new field)
    commission_bps: 5,
    slippage_bps: 2,
    spread_bps: 1,
    confidence_level: 0.95,
    multiple_testing_alpha: 0.05,
  }, 90000) as OptimizeResult;  // 90s timeout — optimize is the bottleneck, runs in parallel with regime/kelly

  return raw;
}

/**
 * Detect regime via Noble Trader /regime/detect
 * 
 * Response structure:
 *   regime_label: "high_vol_strong_bull"  (the primary regime identifier)
 *   confidence: 1.0                       (0-1, probability of dominant state)
 *   vol_state: "high"                     (volatility regime)
 *   trend_state: "strong_bull"            (trend regime)
 *   risk_multiplier: 0.4                  (position size scaling)
 */
export async function detectRegime(
  prices: number[],
  symbol: string,
): Promise<RegimeResult> {
  return nobleTraderPost("/regime/detect", {
    prices,
    symbol,
    window: 200,
    kelly_fraction: 0.5,
    target_vol: 0.15,
    base_risk_limit: 0.02,
  }, 20000) as Promise<RegimeResult>;  // 20s (down from 30s — parallel with optimize)
}

/**
 * Compute Kelly fraction via Noble Trader /size/kelly
 * 
 * Response structure:
 *   recommended_f: 0.0     (final Kelly fraction after all adjustments)
 *   full_kelly_f: 0.0      (raw Kelly)
 *   fractional_f: 0.0      (half-Kelly)
 *   vol_scaled_f: 0.0      (vol-adjusted)
 *   regime_gated_f: 0.0    (regime-adjusted)
 *   sharpe_ratio: -0.54    (signal quality)
 *   regime_label: "..."    (current regime)
 *   regime_multiplier: 0.4 (regime scaling)
 */
export async function computeKelly(
  prices: number[],
  symbol: string,
): Promise<KellyResult> {
  return nobleTraderPost("/size/kelly", {
    prices,
    symbol,
    kelly_fraction: 0.5,
    target_vol: 0.15,
    use_regime: true,
  }, 20000) as Promise<KellyResult>;  // 20s (down from 30s — parallel with optimize)
}

// ─── Data Sources ───────────────────────────────────────────

// Fetch HyperLiquid candles (crypto perps) — MAINNET
// Switched from testnet to mainnet for real market prices.
// HL mainnet has all 9 crypto perps (BTC, ETH, SOL, AVAX, ARB, DOGE, LINK, XRP, WIF)
// but does NOT have forex perps (EURUSD etc.) — those use Yahoo Finance.
// Rate limit: ~60 candleSnapshot calls/min per IP (weight 20 each, 1200/min cap)
export async function fetchHyperLiquidCandles(
  coin: string,
  hours = 2160,
  interval = "1h",
): Promise<number[]> {
  const startTime = Date.now() - hours * 3600000;
  const HL_MAINNET_URL = Deno.env.get("HL_API_URL") || "https://api.hyperliquid.xyz";

  const res = await fetch(`${HL_MAINNET_URL}/info`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "candleSnapshot",
      req: { coin, interval, startTime, endTime: Date.now() },
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HyperLiquid candle fetch failed for ${coin}: ${res.status} — ${text.substring(0, 200)}`);
  }

  const candles = await res.json();
  if (!Array.isArray(candles) || candles.length === 0) {
    throw new Error(`HyperLiquid returned no candles for ${coin}`);
  }
  return candles
    .map((c: Record<string, unknown>) => parseFloat(c.c as string))
    .filter((p: number) => !isNaN(p));
}

// Fetch Yahoo Finance candles (forex — fallback for symbols not on HL or Alpaca)
// Uses v8 chart API with multiple User-Agent strategies to avoid blocking
// Retries with backoff on 401/429, falls back to daily interval if hourly fails
export async function fetchYahooCandles(
  ticker: string,
  range = "90d",
  interval = "1h",
): Promise<number[]> {
  // Try with different User-Agent strings (Yahoo blocks some patterns)
  const userAgents = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  ];

  // Try both hourly and daily intervals (daily is more reliable for Yahoo)
  const intervalsToTry = interval === "1h" ? ["1h", "1d"] : [interval];

  let lastError: Error | null = null;

  for (const tryInterval of intervalsToTry) {
    // Adjust range for daily interval
    const tryRange = tryInterval === "1d" ? "1y" : range;
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?range=${tryRange}&interval=${tryInterval}`;

    for (const ua of userAgents) {
      try {
        const res = await fetch(url, {
          headers: {
            "User-Agent": ua,
          },
        });

        if (res.status === 429 || res.status === 401) {
          // Rate limited or blocked — try next user agent
          console.warn(`[yahoo] ${res.status} for ${ticker} (${tryInterval}) with UA ${ua.substring(0, 30)}... — trying next`);
          lastError = new Error(`Yahoo Finance ${res.status} for ${ticker} (${tryInterval}) with UA ${ua.substring(0, 30)}...`);
          // Small delay before trying next UA
          await sleep(500);
          continue;
        }

      if (!res.ok) {
        throw new Error(`Yahoo Finance fetch failed for ${ticker} (${tryInterval}): ${res.status}`);
      }

      const data = await res.json();

      // Check for Yahoo API error
      if (data?.chart?.error) {
        throw new Error(`Yahoo API error for ${ticker} (${tryInterval}): ${data.chart.error.description || JSON.stringify(data.chart.error)}`);
      }

      const quotes = data?.chart?.result?.[0]?.indicators?.quote?.[0];
      if (!quotes?.close) {
        throw new Error(`No close prices returned from Yahoo for ${ticker} (${tryInterval})`);
      }

      const prices = quotes.close.filter((p: number | null) => p !== null && !isNaN(p as number));

      if (prices.length > 0) {
        console.log(`[yahoo] ${ticker}: Got ${prices.length} prices (${tryInterval}, UA=${ua.substring(0, 20)}...)`);
        return prices;
      }

      lastError = new Error(`Yahoo returned 0 valid prices for ${ticker} (${tryInterval})`);
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
    }
  } // end UA loop
  } // end interval loop

  throw lastError || new Error(`Yahoo Finance fetch failed for ${ticker} after all retries (UAs + intervals)`);
}

// Fetch Alpaca Market Data bars (stocks, ETFs, crypto spot, commodities ETFs)
// Uses Alpaca's data API which is more reliable than scraping Yahoo Finance.
// Requires ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.
// Falls back to Yahoo Finance if credentials not set.
//
// Symbol mapping: our internal names → Alpaca format
//   AAPL → AAPL, GOLD → GLD, BTCUSD → BTC/USD, etc.
const ALPACA_DATA_URL = "https://data.alpaca.markets";
const ALPACA_SYMBOL_MAP: Record<string, string> = {
  // Crypto spot
  BTCUSD: "BTC/USD",
  ETHUSD: "ETH/USD",
  SOLUSD: "SOL/USD",
  // Equities
  AAPL: "AAPL", AMZN: "AMZN", GOOGL: "GOOGL", META: "META",
  MSFT: "MSFT", NVDA: "NVDA", TSLA: "TSLA", SPY: "SPY", QQQ: "QQQ",
};

function isAlpacaCrypto(alpacaSymbol: string): boolean {
  return alpacaSymbol.includes("/");  // e.g. "BTC/USD"
}

export async function fetchAlpacaCandles(
  symbol: string,
  alpacaSymbol: string,
  hours = 1080,
  interval = "1h",
): Promise<number[]> {
  const apiKey = Deno.env.get("ALPACA_API_KEY");
  const secretKey = Deno.env.get("ALPACA_SECRET_KEY");

  if (!apiKey || !secretKey) {
    console.warn(`[alpaca] No API credentials set, falling back to Yahoo for ${symbol}`);
    // Fall back to Yahoo — derive the yahoo ticker from the symbol
    const yahooTicker = symbol === "BTCUSD" ? "BTC-USD" :
                        symbol === "ETHUSD" ? "ETH-USD" :
                        symbol === "SOLUSD" ? "SOL-USD" :
                        symbol;
    return fetchYahooCandles(yahooTicker, hours <= 1080 ? "45d" : "90d", interval);
  }

  const headers: Record<string, string> = {
    "APCA-API-KEY-ID": apiKey,
    "APCA-API-SECRET-KEY": secretKey,
  };

  const startTime = new Date(Date.now() - hours * 3600000).toISOString();
  const endTime = new Date().toISOString();
  const timeframe = interval === "1h" ? "1Hour" : interval === "1d" ? "1Day" : "1Hour";

  try {
    if (isAlpacaCrypto(alpacaSymbol)) {
      // Crypto bars: /v1beta3/crypto/us/bars
      const url = `${ALPACA_DATA_URL}/v1beta3/crypto/us/bars?symbols=${encodeURIComponent(alpacaSymbol)}&timeframe=${timeframe}&start=${startTime}&end=${endTime}&limit=10000`;
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`Alpaca crypto bars failed for ${alpacaSymbol}: ${res.status} — ${text.substring(0, 200)}`);
      }
      const data = await res.json() as { bars?: Record<string, Array<{ c: number }>> };
      const bars = data?.bars?.[alpacaSymbol];
      if (!bars || bars.length === 0) {
        throw new Error(`Alpaca returned no crypto bars for ${alpacaSymbol}`);
      }
      const prices = bars.map((b) => parseFloat(String(b.c))).filter((p) => !isNaN(p));
      console.log(`[alpaca-crypto] ${alpacaSymbol}: Got ${prices.length} prices`);
      return prices;
    } else {
      // Stock/ETF bars: /v2/stocks/{symbol}/bars
      const url = `${ALPACA_DATA_URL}/v2/stocks/${encodeURIComponent(alpacaSymbol)}/bars?timeframe=${timeframe}&start=${startTime}&end=${endTime}&limit=10000&adjustment=raw`;
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`Alpaca stock bars failed for ${alpacaSymbol}: ${res.status} — ${text.substring(0, 200)}`);
      }
      const data = await res.json() as { bars?: Array<{ c: number }> };
      const bars = data?.bars;
      if (!bars || bars.length === 0) {
        throw new Error(`Alpaca returned no stock bars for ${alpacaSymbol}`);
      }
      const prices = bars.map((b) => parseFloat(String(b.c))).filter((p) => !isNaN(p));
      console.log(`[alpaca-stock] ${alpacaSymbol}: Got ${prices.length} prices`);
      return prices;
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`[alpaca] ${symbol}: ${msg} — falling back to Yahoo`);
    // Fall back to Yahoo
    const yahooTicker = symbol === "BTCUSD" ? "BTC-USD" :
                        symbol === "ETHUSD" ? "ETH-USD" :
                        symbol === "SOLUSD" ? "SOL-USD" :
                        symbol;
    return fetchYahooCandles(yahooTicker, hours <= 1080 ? "45d" : "90d", interval);
  }
}

// Fetch full history for sweep.
// BETA: Using 45d (1080 hourly bars) instead of 90d to reduce CPU load on
// Render's 0.5 CPU tier. The /optimize endpoint does HMM fitting + backtesting
// for each brick×SL×TP combo, which is CPU-bound. On 0.5 CPU this takes 60-90s
// for 45d; 90d would exceed Supabase's edge function timeout.
// v2: Upgraded lookback 45d → 90d for more statistical significance.
const SWEEP_CANDLE_HOURS = 2160; // 90 days × 24h
const SWEEP_YAHOO_RANGE = "90d";

export async function fetchSweepCandles(
  symbolConfig: SymbolConfig,
): Promise<number[]> {
  if (symbolConfig.data_source === "hyperliquid") {
    return fetchHyperLiquidCandles(symbolConfig.hyperliquid_coin!, SWEEP_CANDLE_HOURS, "1h");
  } else if (symbolConfig.data_source === "alpaca") {
    const alpacaSymbol = symbolConfig.alpaca_symbol || ALPACA_SYMBOL_MAP[symbolConfig.symbol] || symbolConfig.symbol;
    return fetchAlpacaCandles(symbolConfig.symbol, alpacaSymbol, SWEEP_CANDLE_HOURS, "1h");
  } else {
    return fetchYahooCandles(symbolConfig.yahoo_ticker!, SWEEP_YAHOO_RANGE, "1h");
  }
}

// Fetch latest N candles for regime detection (lighter)
// Falls back to daily interval if hourly returns insufficient data
export async function fetchLatestCandles(
  symbolConfig: SymbolConfig,
  nBars = 200,
): Promise<number[]> {
  if (symbolConfig.data_source === "hyperliquid") {
    const hours = Math.min(nBars, 500);
    return fetchHyperLiquidCandles(symbolConfig.hyperliquid_coin!, hours, "1h");
  } else if (symbolConfig.data_source === "alpaca") {
    const alpacaSymbol = symbolConfig.alpaca_symbol || ALPACA_SYMBOL_MAP[symbolConfig.symbol] || symbolConfig.symbol;
    return fetchAlpacaCandles(symbolConfig.symbol, alpacaSymbol, Math.min(nBars, 500), "1h");
  } else {
    // Try hourly first (more granular for regime detection)
    const range = nBars <= 390 ? "5d" : "90d";
    let prices = await fetchYahooCandles(symbolConfig.yahoo_ticker!, range, "1h");

    // Noble Trader /regime/detect requires >= 81 prices.
    // Fallback to daily interval if hourly returned insufficient data
    // (common outside market hours for stocks/ETFs/commodities)
    if (prices.length < 100) {
      console.log(`[fetchLatestCandles] ${symbolConfig.symbol}: hourly returned ${prices.length} bars (need >=81 for regime), falling back to daily`);
      prices = await fetchYahooCandles(symbolConfig.yahoo_ticker!, "1y", "1d");
    }

    // Final safety: if daily also failed, try 2y daily for more history
    if (prices.length < 100) {
      console.log(`[fetchLatestCandles] ${symbolConfig.symbol}: daily 1y returned ${prices.length} bars, trying 2y daily`);
      prices = await fetchYahooCandles(symbolConfig.yahoo_ticker!, "2y", "1d");
    }

    return prices;
  }
}

// ─── ATR Computation ────────────────────────────────────────
export function computeATR(prices: number[], period = 14): number {
  if (prices.length < period + 1) return 0;

  let atrSum = 0;
  for (let i = 1; i <= period; i++) {
    atrSum += Math.abs(prices[i] - prices[i - 1]);
  }

  return atrSum / period;
}

// Generate brick sizes — professional daily Renko standards.
// Validated by professional trader: bricks must be 0.5-1.0% of price
// for stocks/crypto, 0.2-0.35% for forex, to avoid noise over-fitting.
//
// Previous values (0.08-0.35%) were 2-5× too small, causing:
//   - Sharpe anomalies (GOOGL=15,121) from over-fitting noise
//   - Whipsaw stops (WIF $0.00 SL) from tiny brick distances
//   - 600+ trades/90d instead of professional ~200-300
//   - Render memory crashes from excessive backtest API calls
export function generateBrickSizes(atr: number, price: number): number[] {
  if (atr <= 0 || price <= 0) return [0.5];

  // Target brick as % of price, scaled by volatility regime
  // Professional daily Renko: bricks should be ~0.5-1.0% of price
  const volPct = (atr / price) * 100;  // e.g. BTC ~2.7% daily vol
  let baseBrickPct: number;
  if (volPct > 3.0) {
    baseBrickPct = 0.75;   // High vol (crypto, TSLA): 0.75% of price
  } else if (volPct > 1.5) {
    baseBrickPct = 0.60;   // Medium vol (most stocks): 0.60% of price
  } else if (volPct > 0.5) {
    baseBrickPct = 0.40;   // Low vol (indices, commodities): 0.40% of price
  } else {
    baseBrickPct = 0.25;   // Forex: 0.25% of price
  }

  const base = price * baseBrickPct / 100;
  // BETA: 3 brick sizes instead of 5 to reduce CPU load on Render's 0.5 CPU tier.
  // Each brick size × SL×TP combo requires a full backtest in the /optimize endpoint.
  // 3 bricks × 4 SL × 6 TP = 72 combos; 5 bricks would be 120 combos.
  // UPGRADE PATH: When upgrading to ≥1 CPU on Render, add 0.75× and 3.0× back.
  const bricks = [
    Math.round(base * 1.0  * 10000) / 10000,
    Math.round(base * 1.5  * 10000) / 10000,
    Math.round(base * 2.0  * 10000) / 10000,
  ];

  // Deduplicate (forex can produce identical bricks after rounding)
  return [...new Set(bricks)];
}

// ─── Market Hours Check ─────────────────────────────────────
export function isMarketOpen(assetClass: string): boolean {
  const now = new Date();
  const day = now.getUTCDay(); // 0=Sun, 6=Sat
  const hourUTC = now.getUTCHours();
  const minuteUTC = now.getUTCMinutes();
  const timeUTC = hourUTC * 60 + minuteUTC;

  switch (assetClass) {
    case "crypto":
      return true; // 24/7

    case "forex":
      // Forex: Sun 22:00 UTC - Fri 22:00 UTC
      if (day === 0) return timeUTC >= 1320;
      if (day === 6) return false;
      if (day === 5) return timeUTC < 1320;
      return true; // Mon-Thu

    case "stocks":
    case "commodities":
      // US market hours: Mon-Fri 14:30-21:00 UTC (9:30am-4pm ET)
      if (day === 0 || day === 6) return false;
      return timeUTC >= 870 && timeUTC <= 1260;

    default:
      return true;
  }
}

// ─── Regime Parsing Helpers ─────────────────────────────────

/**
 * Extract the primary regime label from the Noble Trader response.
 * The API returns `regime_label` (e.g. "high_vol_strong_bull")
 * Falls back to constructing from vol_state + trend_state if needed.
 */
export function parseRegimeLabel(result: RegimeResult): string {
  if (result.regime_label) return result.regime_label;
  // Fallback: construct from components
  const parts = [];
  if (result.vol_state) parts.push(`${result.vol_state}_vol`);
  if (result.trend_state) parts.push(result.trend_state);
  return parts.length > 0 ? parts.join("_") : "unknown";
}

/**
 * Extract Markov transition probabilities from regime state.
 * These are directional priors the Executor blends with L2 data.
 */
export function computeMarkovProbs(regimeLabel: string): { pUp: number; pDn: number } {
  const lower = regimeLabel.toLowerCase();
  const isBullish = lower.includes("bull");
  const isBearish = lower.includes("bear");
  const isVolatile = lower.includes("high_vol") || lower.includes("volatile");

  let pUp = 0.5;
  let pDn = 0.5;

  if (isBullish) {
    pUp = isVolatile ? 0.55 : 0.65;
    pDn = 1 - pUp;
  } else if (isBearish) {
    pDn = isVolatile ? 0.55 : 0.65;
    pUp = 1 - pDn;
  }

  return { pUp, pDn };
}

// ─── EV Engine (Market Pulse) ──────────────────────────────────
// Computes blended P_win from HMM regime + L2 imbalance + Markov transition
// and derives EV, aggression level, and EV-scaled Kelly.

export interface EVInput {
  // From HMM regime detection (required)
  trendProbs: Record<string, number>;  // e.g. { strong_bull: 0.10, bull: 0.65, bear: 0.15, strong_bear: 0.05, neutral: 0.05 }
  // From L2 imbalance engine (optional — 0.50 default if missing)
  imbalanceSignal?: {
    signal: "long" | "short" | "flat";
    confidence: number;  // 0-1
    persistence: number; // bars
  };
  // From Markov transition matrix (optional — 0.50 default if missing)
  markovTransition?: {
    current_state: "UP" | "DOWN" | "FLAT";
    p_up: number;  // P(UP | current_state)
    p_down: number; // P(DOWN | current_state)
  };
  // Trade parameters (required)
  direction: "long" | "short";
  brickSize: number;
  slBricks: number;
  tpBricks: number;
  // Kelly (optional)
  baseKelly?: number;
  // TimesFM (v3, optional)
  pTimesfm?: number;
}

export interface EVResult {
  p_win: number;
  p_regime: number;
  p_imbalance: number;
  p_markov: number;
  p_timesfm: number;           // v3: TimesFM component (0 if unused)
  ev: number;
  ev_per_dollar: number;
  rr: number;
  profit: number;
  stake: number;
  allowed: boolean;
  aggression: "passive" | "mid" | "aggressive";
  ev_scale: number;
  effective_kelly: number;
  markov_current_state: string;
}

/**
 * Compute P_regime from HMM trend posteriors.
 * P_regime(long)  = P(BULLISH) + 0.5 * P(NEUTRAL)
 * P_regime(short) = P(BEARISH) + 0.5 * P(NEUTRAL)
 */
export function computePRegime(
  trendProbs: Record<string, number>,
  direction: "long" | "short",
): number {
  const bullKeys = ["strong_bull", "bull"];
  const bearKeys = ["strong_bear", "bear"];
  const neutralKeys = ["neutral"];

  let pBull = 0;
  let pBear = 0;
  let pNeutral = 0;

  for (const [key, val] of Object.entries(trendProbs)) {
    const lower = key.toLowerCase();
    if (bullKeys.some(k => lower.includes(k))) pBull += val;
    else if (bearKeys.some(k => lower.includes(k))) pBear += val;
    else if (neutralKeys.some(k => lower.includes(k))) pNeutral += val;
  }

  if (direction === "long") {
    return pBull + 0.5 * pNeutral;
  } else {
    return pBear + 0.5 * pNeutral;
  }
}

/**
 * Compute P_imbalance from L2 order book signal.
 * Aligned: confidence * (0.6 + 0.4 * persistence_bonus)
 * Flat: 0.50
 * Opposed: 1.0 - confidence
 */
export function computePImbalance(
  imbalanceSignal: EVInput["imbalanceSignal"],
  direction: "long" | "short",
): number {
  if (!imbalanceSignal) return 0.50;  // No data → neutral

  const minPersistenceTarget = 5;

  if (imbalanceSignal.signal === direction) {
    const persistenceBonus = Math.min(1.0, imbalanceSignal.persistence / minPersistenceTarget);
    return imbalanceSignal.confidence * (0.6 + 0.4 * persistenceBonus);
  } else if (imbalanceSignal.signal === "flat") {
    return 0.50;
  } else {
    // Opposing signal
    return 1.0 - imbalanceSignal.confidence;
  }
}

/**
 * Compute P_markov from Markov transition matrix.
 * P_markov(long)  = T[current_state][UP]
 * P_markov(short) = T[current_state][DOWN]
 */
export function computePMarkov(
  markovTransition: EVInput["markovTransition"],
  direction: "long" | "short",
): number {
  if (!markovTransition) return 0.50;  // No data → neutral

  if (direction === "long") {
    return markovTransition.p_up;
  } else {
    return markovTransition.p_down;
  }
}

/**
 * Full EV computation — the "Market Pulse" calculator (v3).
 *
 * When ``input.pTimesfm`` is provided, uses a 4-source blend::
 *     P_win = 0.35 * P_regime + 0.20 * P_imbalance + 0.20 * P_markov + 0.25 * P_timesfm
 *
 * When TimesFM is unavailable, falls back to the existing 3-source::
 *     P_win = 0.40 * P_regime + 0.30 * P_imbalance + 0.30 * P_markov
 *
 * EV = (P_win * profit) - (P_loss * stake)
 */
export function computeEV(input: EVInput): EVResult {
  const pRegime = computePRegime(input.trendProbs, input.direction);
  const pImbalance = computePImbalance(input.imbalanceSignal, input.direction);
  const pMarkov = computePMarkov(input.markovTransition, input.direction);
  const pTimesfm = input.pTimesfm ?? null;

  // Blended P_win — 4-source or 3-source fallback
  let pWin: number;
  if (pTimesfm !== null) {
    pWin = 0.35 * pRegime + 0.20 * pImbalance + 0.20 * pMarkov + 0.25 * pTimesfm;
  } else {
    pWin = 0.40 * pRegime + 0.30 * pImbalance + 0.30 * pMarkov;
  }

  // EV computation
  const profit = input.tpBricks * input.brickSize;
  const stake = input.slBricks * input.brickSize;
  const pLoss = 1 - pWin;
  const ev = (pWin * profit) - (pLoss * stake);
  const evPerDollar = stake > 0 ? ev / stake : 0;
  const rr = stake > 0 ? profit / stake : 0;

  // Aggression dispatcher
  let aggression: "passive" | "mid" | "aggressive";
  if (evPerDollar > 0.40) aggression = "passive";
  else if (evPerDollar > 0.15) aggression = "mid";
  else aggression = "aggressive";

  // EV-scaled Kelly
  const evScale = evPerDollar > 0 ? Math.min(1.0, evPerDollar / 0.50) : 0;
  const baseKelly = input.baseKelly ?? 0;
  const effectiveKelly = baseKelly * evScale;

  // Min EV gate
  const MIN_EV = 0.0;   // Accept any positive EV
  const MIN_RR = 2.0;
  const allowed = ev > MIN_EV && rr >= MIN_RR;

  const currentState = input.markovTransition?.current_state ?? "FLAT";

  return {
    p_win: pWin,
    p_regime: pRegime,
    p_imbalance: pImbalance,
    p_markov: pMarkov,
    p_timesfm: pTimesfm ?? 0,
    ev,
    ev_per_dollar: evPerDollar,
    rr,
    profit,
    stake,
    allowed,
    aggression,
    ev_scale: evScale,
    effective_kelly: effectiveKelly,
    markov_current_state: currentState,
  };
}

// ─── Markov Chain (Price State Transitions) ────────────────────

export type PriceState = "UP" | "DOWN" | "FLAT";

export interface MarkovTransitionResult {
  current_state: PriceState;
  p_up: number;
  p_down: number;
  p_flat: number;
  n_observations: number;
}

/**
 * Build a Markov transition matrix from a price series and compute
 * transition probabilities from the current state.
 *
 * States: UP (price > brick_size increase), DOWN (price > brick_size decrease), FLAT (within brick_size)
 */
export function computeMarkovTransition(
  prices: number[],
  brickSize: number,
): MarkovTransitionResult {
  if (prices.length < 10) {
    return { current_state: "FLAT", p_up: 0.33, p_down: 0.33, p_flat: 0.34, n_observations: 0 };
  }

  // Classify each price change into a state
  const states: PriceState[] = [];
  for (let i = 1; i < prices.length; i++) {
    const delta = prices[i] - prices[i - 1];
    if (delta > brickSize) states.push("UP");
    else if (delta < -brickSize) states.push("DOWN");
    else states.push("FLAT");
  }

  if (states.length < 2) {
    return { current_state: states[0] ?? "FLAT", p_up: 0.33, p_down: 0.33, p_flat: 0.34, n_observations: states.length };
  }

  // Count transitions
  const counts: Record<PriceState, Record<PriceState, number>> = {
    UP: { UP: 0, DOWN: 0, FLAT: 0 },
    DOWN: { UP: 0, DOWN: 0, FLAT: 0 },
    FLAT: { UP: 0, DOWN: 0, FLAT: 0 },
  };

  for (let i = 1; i < states.length; i++) {
    counts[states[i - 1]][states[i]]++;
  }

  // Current state is the last classified state
  const currentState = states[states.length - 1];

  // Compute transition probabilities from current state
  const fromCounts = counts[currentState];
  const total = fromCounts.UP + fromCounts.DOWN + fromCounts.FLAT;

  if (total === 0) {
    return { current_state: currentState, p_up: 0.33, p_down: 0.33, p_flat: 0.34, n_observations: states.length - 1 };
  }

  return {
    current_state: currentState,
    p_up: Math.round((fromCounts.UP / total) * 10000) / 10000,
    p_down: Math.round((fromCounts.DOWN / total) * 10000) / 10000,
    p_flat: Math.round((fromCounts.FLAT / total) * 10000) / 10000,
    n_observations: states.length - 1,
  };
}

/**
 * Extract the recommended Kelly fraction from the Noble Trader response.
 * Uses recommended_f (which is regime-gated and vol-scaled).
 * Falls back to fractional_f, then full_kelly_f.
 */
export function parseKellyF(result: KellyResult): number {
  // Cap at 0.5 (half-Kelly max) — full Kelly is too aggressive for live trading
  const KELLY_CAP = 0.5;
  let f = 0;
  if (result.recommended_f > 0) f = result.recommended_f;
  else if (result.regime_gated_f > 0) f = result.regime_gated_f;
  else if (result.fractional_f > 0) f = result.fractional_f;
  else if (result.full_kelly_f > 0) f = result.full_kelly_f * 0.5; // Never use full Kelly raw
  else return 0.1; // fallback minimum
  return Math.min(f, KELLY_CAP);
}

// ─── Minimum SL Distance (per-symbol) ───────────────────────
// Prevents stop-losses so tight they get hit by normal market noise.
// Expressed as minimum % distance from entry price.
const MIN_SL_PCT: Record<string, number> = {
  // Crypto: higher noise, need wider stops
  BTC: 0.5,   // ~$320 at $64k — above 1-min noise
  ETH: 0.5,   // ~$8.50 at $1,700
  SOL: 0.8,
  AVAX: 0.8,
  ARB: 1.0,
  DOGE: 1.0,
  LINK: 0.8,
  XRP: 0.8,
  WIF: 1.0,
  // Stocks: moderate noise
  AAPL: 0.3, AMZN: 0.3, GOOGL: 0.3, META: 0.3,
  MSFT: 0.3, NVDA: 0.5,  TSLA: 0.5,
  SPY: 0.3,  QQQ: 0.3,
  // Forex: tight spreads but still need room
  EURUSD: 0.3, GBPUSD: 0.3, USDJPY: 0.3,
  AUDUSD: 0.3, USDCAD: 0.3,
  // Commodities
  GOLD: 0.3, SILVER: 0.3, OIL: 0.5, COPPER: 0.5,
};
const DEFAULT_MIN_SL_PCT = 0.3;

// ─── Trade Signal Computation ───────────────────────────────

/**
 * Compute trade signal, entry, stop-loss, and take-profit from
 * regime + optimal Renko parameters + current market price.
 *
 * All prices are rounded to the exchange tick size for the symbol.
 *
 * Signal logic:
 *   Bullish regime (contains "bull") → BUY  (green brick)
 *   Bearish regime (contains "bear") → SELL (red brick)
 *   Neutral / unknown / market_closed → neutral (no trade)
 *
 * Stop Loss = entry ± (sl_bricks × brick_size)
 * Take Profit = entry ± (tp_bricks × brick_size)
 *
 * Safety features:
 *   - SL floor at 1% of entry (not $0) — prevents WIF-style zero-SL bug
 *   - Minimum SL distance guard — widens SL if too close to noise
 */
export function computeTradeSignal(
  regime: string,
  currentPrice: number,
  brickSize: number,
  slBricks: number,
  tpBricks: number,
  symbol: string,
  assetClass?: string,
): { signal: string; entry_price: number; stop_loss: number; take_profit: number } {
  const lower = regime.toLowerCase();
  const isBullish = lower.includes("bull");
  const isBearish = lower.includes("bear");

  let slDistance = slBricks * brickSize;
  const tpDistance = tpBricks * brickSize;

  // ── Minimum SL distance guard ──
  // If SL is too tight (< MIN_SL_PCT of price), widen it
  const minSlPct = MIN_SL_PCT[symbol] ?? DEFAULT_MIN_SL_PCT;
  const minSlDistance = currentPrice * minSlPct / 100;
  if (slDistance < minSlDistance) {
    console.warn(
      `[computeTradeSignal] ${symbol}: SL too tight (${(slDistance/currentPrice*100).toFixed(3)}% < ${minSlPct}%), ` +
      `widening from ${slBricks} bricks ($${slDistance.toFixed(2)}) to $${minSlDistance.toFixed(2)} (${Math.ceil(minSlDistance / brickSize)} bricks)`
    );
    slDistance = minSlDistance;
  }

  const entry = roundPrice(currentPrice, symbol, assetClass);

  if (isBullish) {
    return {
      signal: "buy",
      entry_price: entry,
      stop_loss:  roundPrice(Math.max(entry * 0.01, entry - slDistance), symbol, assetClass),  // Floor at 1% of entry, never $0
      take_profit: roundPrice(entry + tpDistance, symbol, assetClass),
    };
  } else if (isBearish) {
    return {
      signal: "sell",
      entry_price: entry,
      stop_loss:  roundPrice(entry + slDistance, symbol, assetClass),
      take_profit: roundPrice(Math.max(entry * 0.01, entry - tpDistance), symbol, assetClass), // Floor at 1% of entry, never $0
    };
  } else {
    // Neutral / unknown / market_closed — still compute entry for reference
    return {
      signal: "neutral",
      entry_price: entry,
      stop_loss:  roundPrice(Math.max(entry * 0.01, entry - slDistance), symbol, assetClass),
      take_profit: roundPrice(entry + tpDistance, symbol, assetClass),
    };
  }
}

// ─── NaN-Safe Parsers ────────────────────────────────────────
// "NaN" is a truthy string in JS, so parseFloat("NaN") returns NaN
// but the truthiness check doesn't catch it. These helpers prevent
// the NaN feedback loop where NaN gets read from Redis, stays NaN,
// and gets written back forever.

export function safeParseFloat(val: string | undefined | null, fallback: number): number {
  if (!val) return fallback;
  const n = parseFloat(val);
  return Number.isNaN(n) ? fallback : n;
}

export function safeParseInt(val: string | undefined | null, fallback: number): number {
  if (!val) return fallback;
  const n = parseInt(val, 10);
  return Number.isNaN(n) ? fallback : n;
}

// ─── Volatility Shift Detection ────────────────────────────
export function isVolatilityShift(prevRegime: string, currentRegime: string): boolean {
  if (!prevRegime || !currentRegime) return false;
  const prevHigh = prevRegime.includes("high_vol");
  const currHigh = currentRegime.includes("high_vol");
  return prevHigh !== currHigh;
}

// ─── Lead/Lag: Correlation Matrix ───────────────────────────
function computeCorrelationMatrix(
  pricesBySymbol: Record<string, number[]>,
): { matrix: Record<string, Record<string, number>>; leaders: Record<string, string[]> } {
  const matrix: Record<string, Record<string, number>> = {};
  const leaders: Record<string, string[]> = {};
  const symbols = Object.keys(pricesBySymbol).filter(
    (s) => (pricesBySymbol[s]?.length ?? 0) >= 20,
  );
  if (symbols.length < 2) return { matrix, leaders };

  const returns: Record<string, number[]> = {};
  for (const sym of symbols) {
    const prices = pricesBySymbol[sym]!;
    const r: number[] = [];
    for (let i = prices.length - 19; i < prices.length; i++) {
      if (prices[i - 1] > 0) r.push(Math.log(prices[i] / prices[i - 1]));
    }
    if (r.length >= 19) returns[sym] = r;
  }

  const validSyms = Object.keys(returns);
  if (validSyms.length < 2) return { matrix, leaders };

  for (const a of validSyms) {
    matrix[a] = {};
    const ra = returns[a]!;
    const meanA = ra.reduce((s, v) => s + v, 0) / ra.length;
    for (const b of validSyms) {
      if (a === b) { matrix[a][b] = 1; continue; }
      const rb = returns[b]!;
      const meanB = rb.reduce((s, v) => s + v, 0) / rb.length;
      let cov = 0, varA = 0, varB = 0;
      for (let i = 0; i < ra.length; i++) {
        const da = ra[i] - meanA;
        const db = rb[i] - meanB;
        cov += da * db;
        varA += da * da;
        varB += db * db;
      }
      const denom = Math.sqrt(varA * varB);
      matrix[a][b] = denom > 0 ? Math.max(-1, Math.min(1, cov / denom)) : 0;
    }
  }

  for (const sym of validSyms) {
    const corrs = Object.entries(matrix[sym]!)
      .filter(([k, v]) => k !== sym && v > 0.3)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 2)
      .map(([k]) => k);
    leaders[sym] = corrs;
  }
  return { matrix, leaders };
}

function adjustLeadLag(
  symbol: string,
  pTimesfmMap: Map<string, number | null>,
  leaders: Record<string, string[]>,
  selfWeight = 0.7,
): number | null {
  const own = pTimesfmMap.get(symbol) ?? null;
  if (own === null) return null;
  const leaderSyms = leaders[symbol] ?? [];
  if (leaderSyms.length === 0) return own;
  let sum = 0, count = 0;
  for (const ls of leaderSyms) {
    const lp = pTimesfmMap.get(ls);
    if (lp !== null) { sum += lp; count++; }
  }
  if (count === 0) return own;
  return selfWeight * own + (1 - selfWeight) * (sum / count);
}

// ─── Tail Risk ──────────────────────────────────────────────
function computeTailRisk(
  pTimesfm: number | null,
  regime: string,
  timesfmConf: number,
): { score: number; action: string; multiplier: number } {
  const result = { score: 0, action: "none" as string, multiplier: 1.0 };
  if (pTimesfm === null || timesfmConf < 0.5) return result;
  const isExtreme = pTimesfm < 0.2 || pTimesfm > 0.8;
  const isHighVol = regime.includes("high_vol");
  const isExtremeConfident = isExtreme && timesfmConf >= 0.7;
  if (isExtremeConfident && isHighVol) { result.score = 0.85; result.action = "skip"; result.multiplier = 0.0; }
  else if (isExtremeConfident) { result.score = 0.60; result.action = "reduce_50"; result.multiplier = 0.5; }
  else if (isExtreme && isHighVol) { result.score = 0.35; result.action = "reduce_25"; result.multiplier = 0.75; }
  return result;
}

// ─── Utility ────────────────────────────────────────────────
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}


// ════════════════════════════════════════════════════════════════
// SECTION 2: Sweep Function Logic
// ════════════════════════════════════════════════════════════════

// ─── Concurrency Control Constants ────────────────────────────
const GLOBAL_CAP = 4;                    // max concurrent sweep invocations
const GLOBAL_STALE_MS = 300_000;         // 5 min — evict stale slots
const GLOBAL_EXPIRE_S = 330;             // safety net EXPIRE on sorted set
const HEAVY_LOCK_TTL_S = 360;            // 6 min — heavy sweep lock
const LIGHT_LOCK_TTL_S = 90;             // 1.5 min — light regime lock

// ─── F-Lite-1: Heavy Hours (UTC) ──────────────────────────────
// Light regime is suppressed during these 6 hours to avoid colliding
// with the heavy sweep cron jobs (which fire at :00/:05/:10/etc).
// Enforced BOTH in the cron schedule AND here (defense in depth).
const HEAVY_HOURS_UTC = [20];  // v2: heavy sweeps run daily at 20:00 UTC only

function isHeavyHour(): boolean {
  return HEAVY_HOURS_UTC.includes(new Date().getUTCHours());
}

// Heavy sweep grid ranges (ported from heavy-sweep v4)
const SWEEP_SL_RANGE = [3, 5, 7];    // v2: wider SL range for 90d lookback
const SWEEP_TP_RANGE = [4, 5, 6, 8, 10, 12];
const MIN_RR = 2.0;
const MAX_SHARPE = 10.0;
const MIN_TRADES = 10;

// ─── Upstash Redis helpers ────────────────────────────────────
// NOTE: UPSTASH_URL and UPSTASH_TOKEN are declared in _shared/utils.ts
// (inlined at deploy time). We reuse them here — no duplicate declaration.

function sweepRedisHeaders(): Record<string, string> {
  return {
    Authorization: `Bearer ${UPSTASH_TOKEN}`,
    "Content-Type": "application/json",
  };
}

async function sweepRedisPipeline(commands: unknown[][]): Promise<unknown[]> {
  const res = await fetch(`${UPSTASH_URL}/pipeline`, {
    method: "POST",
    headers: sweepRedisHeaders(),
    body: JSON.stringify(commands),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Redis pipeline error: ${res.status} — ${text}`);
  }
  const data = await res.json();
  return data as unknown[];
}

async function sweepRedisGet(cmd: string, ...args: (string | number)[]): Promise<unknown> {
  const res = await fetch(`${UPSTASH_URL}/${cmd}/${args.map(String).join("/")}`, {
    headers: { Authorization: `Bearer ${UPSTASH_TOKEN}` },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Redis ${cmd} error: ${res.status} — ${text}`);
  }
  const data = await res.json();
  return data.result;
}

// ─── Layer 1: Per-Symbol Lock ─────────────────────────────────
async function acquireSymbolLock(
  symbol: string,
  mode: "heavy" | "light",
  token: string,
): Promise<boolean> {
  const ttl = mode === "heavy" ? HEAVY_LOCK_TTL_S : LIGHT_LOCK_TTL_S;
  // SET lock:sweep:{symbol} {token} NX EX {ttl}
  const result = await sweepRedisGet(
    "set",
    `lock:sweep:${symbol}`,
    token,
    "NX",
    "EX",
    String(ttl),
  );
  return result === "OK";
}

async function releaseSymbolLock(symbol: string, token: string): Promise<void> {
  // Best-effort release — use pipeline to GET + DEL only if value matches
  // (prevents releasing a lock we no longer own after TTL expiry)
  // For simplicity, just DEL — TTL self-heals if we crash before releasing.
  // The risk: if our lock expired (TTL) and another caller acquired it,
  // DEL would release THEIR lock. Mitigation: TTL >> expected runtime,
  // so this only happens if we're truly stuck.
  try {
    // Lua-style check: only DEL if value === token
    // Upstash /eval POST form: { script, args }
    const script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end";
    await fetch(`${UPSTASH_URL}/eval`, {
      method: "POST",
      headers: sweepRedisHeaders(),
      body: JSON.stringify({
        script,
        args: [`lock:sweep:${symbol}`, token],
      }),
    });
  } catch {
    // Best effort — TTL will clean up
  }
}

// ─── Layer 2: Global Concurrency Cap ──────────────────────────
async function acquireGlobalSlot(token: string): Promise<boolean> {
  const now = Date.now();
  const staleThreshold = now - GLOBAL_STALE_MS;

  // Pipeline: evict stale + check count (1 round trip)
  const checkResult = await sweepRedisPipeline([
    ["ZREMRANGEBYSCORE", "sweep:active", "0", String(staleThreshold)],
    ["ZCARD", "sweep:active"],
  ]);

  const count = (checkResult[1] as { result?: number })?.result ?? 0;
  if (count >= GLOBAL_CAP) {
    return false;
  }

  // Pipeline: add ourselves + set expire (1 round trip)
  await sweepRedisPipeline([
    ["ZADD", "sweep:active", String(now), token],
    ["EXPIRE", "sweep:active", String(GLOBAL_EXPIRE_S)],
  ]);
  return true;
}

async function releaseGlobalSlot(token: string): Promise<void> {
  try {
    await sweepRedisGet("zrem", "sweep:active", token);
  } catch {
    // Best effort
  }
}

// ─── Heavy Sweep Logic (ported from heavy-sweep v4) ───────────
async function runHeavySweepForSymbol(
  symbolConfig: SymbolConfig,
): Promise<{ best: OptimizeComboResult | null; regime: unknown; kelly: unknown; duration: number; nCombos: number; error?: string }> {
  const startTime = Date.now();

  try {
    // ── Sweep Fatigue: skip if calibrated and skip_until is in the future ──
    try {
      const prevCfg = await redisGetConfig(symbolConfig.symbol);
      if (prevCfg) {
        const skipUntil = safeParseFloat(prevCfg.sweep_skip_until ?? "0", 0);
        if (skipUntil > Date.now()) {
          console.log(`[sweep:heavy] ${symbolConfig.symbol}: SKIPPED — sweep_skip_until=${new Date(skipUntil).toISOString()} (symbol is well-calibrated)`);
          return {
            best: null, regime: null, kelly: null,
            duration: 0, nCombos: 0,
            error: `Skipped — sweep_skip_until active`,
          };
        }
      }
    } catch { /* best effort — proceed with sweep */ }

    // 1. Fetch candles
    console.log(`[sweep:heavy] Fetching candles for ${symbolConfig.symbol}...`);
    const prices = await fetchSweepCandles(symbolConfig);

    if (prices.length < 100) {
      throw new Error(`Insufficient candle data: ${prices.length} bars (need >= 100)`);
    }

    console.log(`[sweep:heavy] ${symbolConfig.symbol}: ${prices.length} candles loaded`);

    // 2. Compute ATR and generate brick sizes
    const atr = computeATR(prices, 14);
    const currentPrice = prices[prices.length - 1];
    const brickSizes = generateBrickSizes(atr, currentPrice);

    console.log(`[sweep:heavy] ${symbolConfig.symbol}: ATR=${atr.toFixed(4)}, price=${currentPrice.toFixed(2)}, bricks=[${brickSizes.map(b => b.toFixed(4)).join(",")}]`);

    // 3. Run optimize + regime + Kelly IN PARALLEL
    console.log(`[sweep:heavy] ${symbolConfig.symbol}: Running optimize + regime + Kelly in parallel...`);
    const [optResultRaw, regimeResultRaw, kellyResultRaw] = await Promise.allSettled([
      runRenkoOptimize(prices, symbolConfig.symbol, brickSizes, SWEEP_SL_RANGE, SWEEP_TP_RANGE, MIN_RR),
      prices.length >= 81
        ? detectRegime(prices, symbolConfig.symbol)
        : Promise.reject(new Error(`Only ${prices.length} bars (need >=81)`)),
      computeKelly(prices, symbolConfig.symbol),
    ]);

    // ── Graceful fallback: if optimize timed out but regime/kelly succeeded ──
    const optimizeFailed = optResultRaw.status === "rejected";
    const regimeOk = regimeResultRaw.status === "fulfilled";
    const kellyOk = kellyResultRaw.status === "fulfilled";

    if (optimizeFailed) {
      const reason = optResultRaw.reason instanceof Error ? optResultRaw.reason.message : String(optResultRaw.reason);
      console.warn(`[sweep:heavy] ${symbolConfig.symbol}: Optimization failed: ${reason}`);

      let cachedBrick = brickSizes[0];
      let cachedSl = 3;
      let cachedTp = 5;
      let prevPTimesfm: number | undefined = undefined;
      try {
        const cachedCfg = await redisGetConfig(symbolConfig.symbol);
        if (cachedCfg) {
          cachedBrick = safeParseFloat(cachedCfg.brick_size ?? "", brickSizes[0]);
          cachedSl = safeParseInt(cachedCfg.sl_bricks ?? "", 3);
          cachedTp = safeParseInt(cachedCfg.tp_bricks ?? "", 5);
          prevPTimesfm = safeParseFloat(cachedCfg.p_timesfm ?? "", NaN);
          if (isNaN(prevPTimesfm)) prevPTimesfm = undefined;
          console.log(`[sweep:heavy] ${symbolConfig.symbol}: Using cached brick params (brick=${cachedBrick}, sl=${cachedSl}, tp=${cachedTp})`);
        }
      } catch { /* use defaults */ }

      if (!regimeOk) {
        throw new Error(`Optimize + regime both failed. Optimize: ${reason}, Regime: ${regimeResultRaw.status === "rejected" ? regimeResultRaw.reason : "n/a"}`);
      }

      const regimeResult = regimeResultRaw.value as RegimeResult;
      const combinedRegime = parseRegimeLabel(regimeResult);
      const regimeConf = regimeResult.confidence ?? 0;
      const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(combinedRegime);
      const kellyF = kellyOk ? parseKellyF(kellyResultRaw.value as KellyResult) : 0.1;
      const tradeSignal = computeTradeSignal(combinedRegime, currentPrice, cachedBrick, cachedSl, cachedTp, symbolConfig.symbol, symbolConfig.asset_class);
      const direction: "long" | "short" = combinedRegime.toLowerCase().includes("bear") ? "short" : "long";
      const markovTransition = computeMarkovTransition(prices, cachedBrick);
      const evResult = computeEV({
        trendProbs: regimeResult.trend_probs ?? {},
        direction,
        brickSize: cachedBrick,
        slBricks: cachedSl,
        tpBricks: cachedTp,
        baseKelly: kellyF,
        markovTransition: { current_state: markovTransition.current_state, p_up: markovTransition.p_up, p_down: markovTransition.p_down },
      });
      const effectiveKelly = evResult.effective_kelly > 0 ? Math.min(evResult.effective_kelly, 0.5) : kellyF;

      console.log(`[sweep:heavy] ${symbolConfig.symbol}: FALLBACK — regime=${combinedRegime}, cached brick=${cachedBrick}, signal=${tradeSignal.signal}`);

      const redisConfig: RedisConfig = {
        regime: combinedRegime,
        regime_conf: regimeConf,
        brick_size: cachedBrick,
        tp_bricks: cachedTp,
        sl_bricks: cachedSl,
        markov_p_up: markovPUp,
        markov_p_dn: markovPDn,
        kelly_f: kellyF,
        last_update: Date.now(),
        sweep_timestamp: Date.now(),
        sweep_window: "45d (fallback)",
        signal: tradeSignal.signal,
        entry_price: tradeSignal.entry_price,
        stop_loss: tradeSignal.stop_loss,
        take_profit: tradeSignal.take_profit,
        ev: evResult.ev,
        ev_per_dollar: evResult.ev_per_dollar,
        p_win: evResult.p_win,
        p_regime: evResult.p_regime,
        p_imbalance: evResult.p_imbalance,
        p_markov: evResult.p_markov,
        aggression: evResult.aggression,
        ev_scale: evResult.ev_scale,
        effective_kelly: effectiveKelly,
        markov_current_state: evResult.markov_current_state,
	        regime_shift: "false",
	        prev_regime: "",
	        shift_at: 0,
	        shifts_24h: 0,
	        p_timesfm: prevPTimesfm,
	        timesfm_horizon: prevPTimesfm !== undefined ? "12h" : undefined,
	      };
		      await redisSetConfig(symbolConfig.symbol, redisConfig);
		      await redisSetHeartbeat(symbolConfig.symbol, symbolConfig.heartbeat_ttl_seconds);

		      // ── Sweep Fatigue: calibration write for fallback path ──
		      try {
		        const cal = await computeCalibrationBias(symbolConfig.symbol);
		        if (cal) {
		          const additionalFields: (string | number)[] = [
		            "calibration_bias", String(cal.bias),
		            "calibration_status", cal.status,
		          ];
		          if (cal.status === "CALIBRATED") {
		            additionalFields.push("sweep_skip_until", String(Date.now() + 12 * 3600 * 1000));
		          } else {
		            additionalFields.push("sweep_skip_until", "0");
		          }
		          await sweepRedisPipeline([
		            ["HSET", `trading:config:${symbolConfig.symbol}`, ...additionalFields],
		          ]);
		        }
		      } catch { /* best effort */ }

		      try {
		        const cache = await redisLoadRegimeCache();
	        cache[symbolConfig.symbol] = {
          regime: combinedRegime, regime_conf: regimeConf,
          brick_size: cachedBrick, sl_bricks: cachedSl, tp_bricks: cachedTp,
          kelly_f: kellyF, last_update: Date.now(),
          signal: tradeSignal.signal, entry_price: tradeSignal.entry_price,
          stop_loss: tradeSignal.stop_loss, take_profit: tradeSignal.take_profit,
          price_decimals: 2,
          ev: evResult.ev, ev_per_dollar: evResult.ev_per_dollar, p_win: evResult.p_win,
          p_regime: evResult.p_regime, p_imbalance: evResult.p_imbalance,
          p_markov: evResult.p_markov, aggression: evResult.aggression,
          ev_scale: evResult.ev_scale, effective_kelly: effectiveKelly,
          markov_current_state: evResult.markov_current_state,
	          regime_shift: "false", prev_regime: "", shift_at: 0, shifts_24h: 0,
	          p_timesfm: prevPTimesfm,
	          timesfm_horizon: prevPTimesfm !== undefined ? "12h" : undefined,
	        };
        await redisSaveRegimeCache(cache);
      } catch { /* best effort */ }

      await logRegimeEntry({
        symbol: symbolConfig.symbol, source: "sweep-heavy-fallback",
        regime: combinedRegime, regime_conf: regimeConf,
        markov_p_up: markovPUp, markov_p_dn: markovPDn,
        kelly_f: kellyF, prev_regime: null, regime_shift: false,
      });

      return {
        best: null, regime: { regime_label: combinedRegime, confidence: regimeConf },
        kelly: { kelly_f: kellyF }, duration: Date.now() - startTime,
        nCombos: 0, error: `Optimize failed (fallback): ${reason}`,
      };
    }

    const optResult = optResultRaw.value as { n_combinations: number; results: OptimizeComboResult[]; deflated_sharpe?: Record<string, unknown> };
    const nCombos = optResult.n_combinations;
    console.log(`[sweep:heavy] ${symbolConfig.symbol}: Optimization complete — ${nCombos} combinations tested`);

    // 4. Rank results
    const validResults = (optResult.results || []).filter((r: OptimizeComboResult) => {
      if (r.total_trades < MIN_TRADES) return false;
      const rr = r.tp_bricks / r.sl_bricks;
      if (rr < MIN_RR) return false;
      if (r.sharpe > MAX_SHARPE) {
        console.warn(`[sweep:heavy] ${symbolConfig.symbol}: Rejecting combo ${r.brick_size}/${r.sl_bricks}/${r.tp_bricks} — Sharpe ${r.sharpe.toFixed(2)} > ${MAX_SHARPE} (likely overfit)`);
        return false;
      }
      return true;
    });

    let rankedResults = validResults;
    if (rankedResults.length === 0) {
      const anyTrades = (optResult.results || []).filter((r: OptimizeComboResult) =>
        r.total_trades > 0 && r.sharpe <= MAX_SHARPE
      );
      if (anyTrades.length === 0) {
        throw new Error(`No backtest combinations produced valid trades (R:R >= ${MIN_RR}, Sharpe <= ${MAX_SHARPE})`);
      }
      anyTrades.sort((a, b) => b.sharpe - a.sharpe);
      rankedResults = anyTrades;
      console.warn(`[sweep:heavy] ${symbolConfig.symbol}: Strict filter eliminated all results, using best available (R:R=${(rankedResults[0].tp_bricks / rankedResults[0].sl_bricks).toFixed(2)}x)`);
    }

    rankedResults.sort((a, b) => b.sharpe - a.sharpe);
    const best = rankedResults[0];
    const bestRR = best.tp_bricks / best.sl_bricks;

    console.log(
      `[sweep:heavy] ${symbolConfig.symbol} best: brick=${best.brick_size}, sl=${best.sl_bricks}, ` +
      `tp=${best.tp_bricks}, R:R=${bestRR.toFixed(2)}x, sharpe=${best.sharpe.toFixed(2)}, trades=${best.total_trades}`
    );

    const dsr = optResult.deflated_sharpe as Record<string, unknown> | undefined;
    if (dsr && typeof dsr.dsr === "number") {
      console.log(`[sweep:heavy] ${symbolConfig.symbol}: DSR=${dsr.dsr.toFixed(4)} (${dsr.interpretation || ""})`);
    }

    // 5. Unpack regime result
    let combinedRegime = "unknown";
    let regimeConf = 0;
    let riskMultiplier = 1.0;
    let regimeResult: RegimeResult | null = null;
    if (regimeResultRaw.status === "fulfilled") {
      regimeResult = regimeResultRaw.value as RegimeResult;
      combinedRegime = parseRegimeLabel(regimeResult);
      regimeConf = regimeResult.confidence ?? 0;
      riskMultiplier = regimeResult.risk_multiplier ?? 1.0;
    } else {
      console.warn(`[sweep:heavy] ${symbolConfig.symbol} regime detection failed: ${regimeResultRaw.reason}`);
    }

    // GUARD: preserve previous valid regime if detection returned "unknown"
    if (combinedRegime === "unknown") {
      try {
        const prevCfg = await redisGetConfig(symbolConfig.symbol);
        const prevRegime = prevCfg?.regime;
        if (prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed") {
          console.warn(`[sweep:heavy] ${symbolConfig.symbol}: Regime detection returned "unknown" — preserving previous regime "${prevRegime}"`);
          combinedRegime = prevRegime;
          regimeConf = safeParseFloat(String(prevCfg?.regime_conf ?? ""), 0);
        }
      } catch { /* use "unknown" if Redis read fails */ }
    }

    // 6. Unpack Kelly result
    let kellyF = 0.1;
    if (kellyResultRaw.status === "fulfilled") {
      kellyF = parseKellyF(kellyResultRaw.value as KellyResult);
    } else {
      console.warn(`[sweep:heavy] ${symbolConfig.symbol} Kelly computation failed, using default: ${kellyResultRaw.reason}`);
    }

    const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(combinedRegime);
    const direction: "long" | "short" = combinedRegime.toLowerCase().includes("bear") ? "short" : "long";
    const markovTransition = computeMarkovTransition(prices, best.brick_size);

    const evResult = computeEV({
      trendProbs: regimeResult?.trend_probs ?? {},
      direction,
      brickSize: best.brick_size,
      slBricks: best.sl_bricks,
      tpBricks: best.tp_bricks,
      baseKelly: kellyF,
      markovTransition: {
        current_state: markovTransition.current_state,
        p_up: markovTransition.p_up,
        p_down: markovTransition.p_down,
      },
    });

    const effectiveKelly = evResult.effective_kelly > 0 ? Math.min(evResult.effective_kelly, 0.5) : kellyF;

    console.log(
      `[sweep:heavy] ${symbolConfig.symbol}: EV=${evResult.ev.toFixed(2)} ($${evResult.ev_per_dollar.toFixed(3)}/$risk), ` +
      `P_win=${evResult.p_win.toFixed(3)} (regime=${evResult.p_regime.toFixed(3)}, markov=${evResult.p_markov.toFixed(3)}), ` +
      `aggression=${evResult.aggression}, effective_kelly=${effectiveKelly.toFixed(3)}`
    );

    const duration = Date.now() - startTime;

    // 7a. Detect regime shift
    let prevRegime: string | null = null;
    let prevShiftAt = 0;
    let regimeShift = false;
    let prevPTimesfm: number | undefined = undefined;
    try {
      const prevCfg = await redisGetConfig(symbolConfig.symbol);
      if (prevCfg) {
        prevRegime = prevCfg.regime ?? null;
        prevShiftAt = safeParseFloat(prevCfg.shift_at ?? "0", 0);
        prevPTimesfm = safeParseFloat(prevCfg.p_timesfm ?? "", NaN);
        if (isNaN(prevPTimesfm)) prevPTimesfm = undefined;
        if (
          combinedRegime !== "unknown" && combinedRegime !== "market_closed" &&
          prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed" &&
          prevRegime !== combinedRegime
        ) {
          regimeShift = true;
          console.log(`[sweep:heavy] ${symbolConfig.symbol}: REGIME SHIFT ${prevRegime} → ${combinedRegime}`);
        }
      }
    } catch { /* best effort */ }

    const shifts24h = await countShifts24h(symbolConfig.symbol);
    console.log(`[sweep:heavy] ${symbolConfig.symbol}: shifts_24h=${shifts24h}, shift=${regimeShift}`);

    // 8. Compute trade signal
    const tradeSignal = computeTradeSignal(
      combinedRegime,
      currentPrice,
      best.brick_size,
      best.sl_bricks,
      best.tp_bricks,
      symbolConfig.symbol,
      symbolConfig.asset_class,
    );

    console.log(
      `[sweep:heavy] ${symbolConfig.symbol}: Signal=${tradeSignal.signal}, Entry=${tradeSignal.entry_price.toFixed(2)}, ` +
      `SL=${tradeSignal.stop_loss.toFixed(2)}, TP=${tradeSignal.take_profit.toFixed(2)}`
    );

    // 9. Write to Redis
    const redisConfig: RedisConfig = {
      regime: combinedRegime,
      regime_conf: regimeConf,
      brick_size: best.brick_size,
      tp_bricks: best.tp_bricks,
      sl_bricks: best.sl_bricks,
      markov_p_up: markovPUp,
      markov_p_dn: markovPDn,
      kelly_f: kellyF,
      last_update: Date.now(),
      sweep_timestamp: Date.now(),
      sweep_window: "90d",
      sweep_sharpe: best.sharpe,
      sweep_trades: best.total_trades,
      signal: tradeSignal.signal,
      entry_price: tradeSignal.entry_price,
      stop_loss: tradeSignal.stop_loss,
      take_profit: tradeSignal.take_profit,
      ev: evResult.ev,
      ev_per_dollar: evResult.ev_per_dollar,
      p_win: evResult.p_win,
      p_regime: evResult.p_regime,
      p_imbalance: evResult.p_imbalance,
      p_markov: evResult.p_markov,
      aggression: evResult.aggression,
      ev_scale: evResult.ev_scale,
      effective_kelly: effectiveKelly,
      markov_current_state: evResult.markov_current_state,
      regime_shift: regimeShift ? "true" : "false",
      prev_regime: prevRegime ?? "",
      shift_at: regimeShift ? Date.now() : prevShiftAt,
      shifts_24h: shifts24h,
      // Preserve TimesFM fields from previous cycle (heavy sweep does not call TimesFM)
      p_timesfm: prevPTimesfm,
      timesfm_horizon: prevPTimesfm !== undefined ? "12h" : undefined,
    };

    await redisSetConfig(symbolConfig.symbol, redisConfig);

    try {
      const cache = await redisLoadRegimeCache();
      cache[symbolConfig.symbol] = {
        regime: combinedRegime,
        regime_conf: regimeConf,
        brick_size: best.brick_size,
        sl_bricks: best.sl_bricks,
        tp_bricks: best.tp_bricks,
        kelly_f: kellyF,
        last_update: Date.now(),
        signal: tradeSignal.signal,
        entry_price: tradeSignal.entry_price,
        stop_loss: tradeSignal.stop_loss,
        take_profit: tradeSignal.take_profit,
        price_decimals: 4,
        ev: evResult.ev,
        ev_per_dollar: evResult.ev_per_dollar,
        p_win: evResult.p_win,
        p_regime: evResult.p_regime,
        p_imbalance: evResult.p_imbalance,
        p_markov: evResult.p_markov,
        aggression: evResult.aggression,
        ev_scale: evResult.ev_scale,
        effective_kelly: effectiveKelly,
        markov_current_state: evResult.markov_current_state,
        regime_shift: regimeShift ? "true" : "false",
        prev_regime: prevRegime ?? "",
        shift_at: regimeShift ? Date.now() : prevShiftAt,
        shifts_24h: shifts24h,
        p_timesfm: prevPTimesfm,
        timesfm_horizon: prevPTimesfm !== undefined ? "12h" : undefined,
      };
      await redisSaveRegimeCache(cache);
      console.log(`[sweep:heavy] ${symbolConfig.symbol}: regime cache updated with optimized brick params`);
    } catch (err) {
      console.warn(`[sweep:heavy] ${symbolConfig.symbol}: regime cache save failed: ${err}`);
    }

    await redisSetHeartbeat(symbolConfig.symbol, symbolConfig.heartbeat_ttl_seconds);
    console.log(`[sweep:heavy] ${symbolConfig.symbol}: Redis config updated (regime=${combinedRegime}, kelly=${kellyF.toFixed(3)})`);

    // 10. Log to Supabase
    await logSweepResult({
      symbol: symbolConfig.symbol,
      asset_class: symbolConfig.asset_class,
      brick_size: best.brick_size,
      sl_bricks: best.sl_bricks,
      tp_bricks: best.tp_bricks,
      sharpe: best.sharpe,
      total_return: best.dollar_stats?.net_return_pct ?? 0,
      annual_return: best.dollar_stats?.net_return_pct ?? 0,
      max_drawdown_pct: best.max_drawdown_bricks * best.brick_size,
      win_rate: best.win_rate,
      n_trades: best.total_trades,
      profit_factor: best.profit_factor,
      regime: combinedRegime,
      regime_conf: regimeConf,
      kelly_f: kellyF,
      markov_p_up: markovPUp,
      markov_p_dn: markovPDn,
      sweep_window: "90d",
      sweep_duration_ms: duration,
      n_combos_tested: nCombos,
    });

    await logRegimeEntry({
      symbol: symbolConfig.symbol,
      source: "sweep-heavy",
      regime: combinedRegime,
      regime_conf: regimeConf,
      markov_p_up: markovPUp,
      markov_p_dn: markovPDn,
      kelly_f: kellyF,
      prev_regime: prevRegime,
      regime_shift: regimeShift,
    });

	    console.log(`[sweep:heavy] ${symbolConfig.symbol}: Complete in ${duration}ms (${nCombos} combos, 1 API call)`);

	    // 11. Query calibration bias and write to Redis (v3)
	    try {
	      const cal = await computeCalibrationBias(symbolConfig.symbol);
	      if (cal) {
	        await sweepRedisPipeline([
	          [
	            "HSET", `trading:config:${symbolConfig.symbol}`,
	            "calibration_bias", String(cal.bias),
	            "calibration_status", cal.status,
	          ],
	        ]);
	        console.log(`[sweep:heavy] ${symbolConfig.symbol}: calibration_bias=${cal.bias} (${cal.status})`);
	      }
	    } catch (err) {
	      console.warn(`[sweep:heavy] ${symbolConfig.symbol}: calibration write failed: ${err}`);
	    }

	    return { best, regime: { regime_label: combinedRegime, confidence: regimeConf, risk_multiplier: riskMultiplier }, kelly: { kelly_f: kellyF }, duration, nCombos };

  } catch (err) {
    const duration = Date.now() - startTime;
    const errMsg = err instanceof Error ? err.message : String(err);

    console.error(`[sweep:heavy] ${symbolConfig.symbol} FAILED: ${errMsg}`);
    await redisPushError(symbolConfig.symbol, "sweep-heavy", errMsg);
    await logError(symbolConfig.symbol, "sweep-heavy", errMsg);

    return { best: null, regime: null, kelly: null, duration, nCombos: 0, error: errMsg };
  }
}

// ─── Light Regime Logic (ported from light-regime v2.2) ────────
async function runLightRegimeForSymbol(
  symbolConfig: SymbolConfig,
  cacheEntry: RegimeCacheEntry | undefined,
  prefetchedTimesfm?: number,
): Promise<{ regime: string; regimeShift: boolean; entry: RegimeCacheEntry | null; error?: string }> {
  try {
    const marketOpen = isMarketOpen(symbolConfig.asset_class);

    // 1. Fetch latest candles
    let prices: number[] = [];
    try {
      prices = await fetchLatestCandles(symbolConfig, 200);
    } catch (err) {
      console.warn(`[sweep:light] ${symbolConfig.symbol} candle fetch failed: ${err}`);
    }

    if (prices.length >= 81) {
      // 2. Detect regime
      console.log(`[sweep:light] ${symbolConfig.symbol}: Detecting regime (${prices.length} bars)...`);
      const regimeResult: RegimeResult = await detectRegime(prices, symbolConfig.symbol);
      const combinedRegime = parseRegimeLabel(regimeResult);
      const regimeConf = regimeResult.confidence ?? 0;

      // 3. Compute Kelly
      let kellyF = cacheEntry?.kelly_f ?? 0.1;
      try {
        const kellyResult: KellyResult = await computeKelly(prices, symbolConfig.symbol);
        kellyF = parseKellyF(kellyResult);
      } catch (err) {
        console.warn(`[sweep:light] ${symbolConfig.symbol} Kelly failed: ${err}`);
      }

      // 3b. TimesFM inference (v3) — use prefetched value when available
      let pTimesfm: number | null = prefetchedTimesfm ?? null;
      if (pTimesfm === null && TIMESFM_INFERENCE_URL && TIMESFM_API_KEYS) {
        try {
          const timesfmMap = await timesfmPost([
            { symbol: symbolConfig.symbol, prices: prices.slice(-500) },
          ]);
          pTimesfm = timesfmMap.get(symbolConfig.symbol) ?? null;
          if (pTimesfm !== null) {
            console.log(`[sweep:light] ${symbolConfig.symbol}: TimesFM=${pTimesfm.toFixed(4)} (direct call)`);
          }
        } catch (err) {
          console.warn(`[sweep:light] ${symbolConfig.symbol} TimesFM failed: ${err}`);
        }
      } else if (pTimesfm !== null) {
        console.log(`[sweep:light] ${symbolConfig.symbol}: TimesFM=${pTimesfm.toFixed(4)} (vectorized)`);
      }

      // 4. Detect regime shift from cache entry
      const prevRegime = cacheEntry?.regime ?? null;
      let effectiveRegime = combinedRegime;
      let effectiveRegimeConf = regimeConf;
      let regimeDegraded = false;
      if (combinedRegime === "unknown" && prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed") {
        console.warn(`[sweep:light] ${symbolConfig.symbol}: Regime detection returned "unknown" — falling back to previous regime "${prevRegime}" for downstream computations`);
        effectiveRegime = prevRegime;
        effectiveRegimeConf = cacheEntry?.regime_conf ?? 0;
        regimeDegraded = true;
      }
      const regimeShift = !regimeDegraded && prevRegime !== null && prevRegime !== "unknown" && prevRegime !== "market_closed" && prevRegime !== combinedRegime;

      if (regimeShift) {
        console.log(`[sweep:light] ${symbolConfig.symbol}: REGIME SHIFT ${prevRegime} → ${combinedRegime}`);

        // ── Shift-Aware Wakeup: trigger emergency heavy sweep on vol shift ──
        if (isVolatilityShift(prevRegime ?? "", combinedRegime)) {
          console.log(`[sweep:light] ${symbolConfig.symbol}: Volatility shift detected — triggering emergency heavy sweep`);
          try {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 5_000);
            await fetch(`${SUPABASE_URL}/functions/v1/sweep`, {
              method: "POST",
              headers: {
                Authorization: `Bearer ${SUPABASE_SERVICE_ROLE}`,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({ mode: "heavy", symbol: symbolConfig.symbol }),
              signal: controller.signal,
            });
            clearTimeout(timer);
          } catch (err) {
            console.warn(`[sweep:light] Emergency heavy sweep trigger failed: ${err}`);
          }
        }
      }

      // 5. Compute Markov probabilities
      const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(effectiveRegime);
      const riskMultiplier = regimeResult.risk_multiplier ?? 1.0;

      // 6. Load brick params — prefer cache, fall back to Redis config
      let brickSize = cacheEntry?.brick_size ?? NaN;
      let slBricks  = (cacheEntry?.sl_bricks as number) ?? NaN;
      let tpBricks  = (cacheEntry?.tp_bricks as number) ?? NaN;
      if (isNaN(brickSize) || isNaN(slBricks) || isNaN(tpBricks)) {
        try {
          const fullCfg = await redisGetConfig(symbolConfig.symbol);
          if (fullCfg) {
            brickSize = safeParseFloat(String(fullCfg.brick_size ?? ""), 0.5);
            slBricks  = safeParseInt(String(fullCfg.sl_bricks ?? ""), 3);
            tpBricks  = safeParseInt(String(fullCfg.tp_bricks ?? ""), 5);
            console.log(`[sweep:light] ${symbolConfig.symbol}: cold cache — loaded brick params from Redis config (brick=${brickSize}, sl=${slBricks}, tp=${tpBricks})`);
          } else {
            brickSize = 0.5; slBricks = 3; tpBricks = 5;
          }
        } catch {
          brickSize = 0.5; slBricks = 3; tpBricks = 5;
        }
      }

      // 6b. Compute EV
      const direction: "long" | "short" = effectiveRegime.toLowerCase().includes("bear") ? "short" : "long";
      const markovTransition = computeMarkovTransition(prices, brickSize);

      const evResult = computeEV({
        trendProbs: regimeResult.trend_probs ?? {},
        direction,
        brickSize: brickSize,
        slBricks: slBricks,
        tpBricks: tpBricks,
        baseKelly: kellyF,
        markovTransition: {
          current_state: markovTransition.current_state,
          p_up: markovTransition.p_up,
          p_down: markovTransition.p_down,
        },
        pTimesfm: pTimesfm ?? undefined,
      });

      const effectiveKelly = evResult.effective_kelly > 0 ? Math.min(evResult.effective_kelly, 0.5) : kellyF;

      console.log(
        `[sweep:light] ${symbolConfig.symbol}: EV=${evResult.ev.toFixed(2)} ($${evResult.ev_per_dollar.toFixed(3)}/$risk), ` +
        `P_win=${evResult.p_win.toFixed(3)}${pTimesfm !== null ? `, timesfm=${pTimesfm?.toFixed(3)}` : ""}, ` +
        `aggression=${evResult.aggression}, effective_kelly=${effectiveKelly.toFixed(3)}`
      );

      // 6d. Tail risk assessment (v3)
      const tailRisk = computeTailRisk(
        pTimesfm,
        effectiveRegime,
        evResult.p_timesfm > 0 ? 0.8 : 0,
      );
      if (tailRisk.score > 0) {
        console.log(`[sweep:light] ${symbolConfig.symbol}: tail_risk=${tailRisk.score.toFixed(2)} (${tailRisk.action})`);
      }

      // 6c. Compute shifts_24h
      let shifts24h = cacheEntry?.shifts_24h ?? 0;
      if (regimeShift) {
        shifts24h = await countShifts24h(symbolConfig.symbol);
        console.log(`[sweep:light] ${symbolConfig.symbol}: shifts_24h=${shifts24h} (fresh count)`);
      }

      // 7. Compute trade signal
      const currentPrice = prices[prices.length - 1];
      const tradeSignal = computeTradeSignal(effectiveRegime, currentPrice, brickSize, slBricks, tpBricks, symbolConfig.symbol, symbolConfig.asset_class);

      const decimals = cacheEntry?.price_decimals
        ?? PRICE_TICK_DECIMALS[symbolConfig.symbol]
        ?? (symbolConfig.symbol === "BTC" ? 1 : 2);
      await redisUpdateSignal(
        symbolConfig.symbol,
        tradeSignal.signal,
        tradeSignal.entry_price,
        tradeSignal.stop_loss,
        tradeSignal.take_profit,
        decimals,
        {
          ev: evResult.ev,
          ev_per_dollar: evResult.ev_per_dollar,
          p_win: evResult.p_win,
          aggression: evResult.aggression,
          ev_scale: evResult.ev_scale,
          effective_kelly: effectiveKelly,
        },
        {
          regime_shift: regimeShift ? "true" : "false",
          prev_regime: prevRegime ?? "",
          shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
          shifts_24h: shifts24h,
        },
        pTimesfm !== null ? {
          p_timesfm: pTimesfm,
          timesfm_horizon: "12h",
        } : undefined,
        tailRisk.score > 0 ? {
          tail_risk_score: tailRisk.score,
          tail_risk_action: tailRisk.action,
        } : undefined,
      );

      // 8. Build new cache entry
	      const newEntry: RegimeCacheEntry = {
	        regime: effectiveRegime,
	        regime_conf: effectiveRegimeConf,
	        brick_size: brickSize,
	        sl_bricks: slBricks,
	        tp_bricks: tpBricks,
	        kelly_f: kellyF,
	        last_update: Date.now(),
	        signal: tradeSignal.signal,
	        entry_price: tradeSignal.entry_price,
	        stop_loss: tradeSignal.stop_loss,
	        take_profit: tradeSignal.take_profit,
	        price_decimals: decimals,
	        ev: evResult.ev,
	        ev_per_dollar: evResult.ev_per_dollar,
	        p_win: evResult.p_win,
	        p_regime: evResult.p_regime,
	        p_imbalance: evResult.p_imbalance,
	        p_markov: evResult.p_markov,
	        aggression: evResult.aggression,
	        ev_scale: evResult.ev_scale,
	        effective_kelly: effectiveKelly,
	        markov_current_state: evResult.markov_current_state,
	        regime_shift: regimeShift ? "true" : "false",
	        prev_regime: prevRegime ?? "",
	        shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
	        shifts_24h: shifts24h,
		        p_timesfm: pTimesfm ?? undefined,
		        timesfm_horizon: pTimesfm !== null ? "12h" : undefined,
		        tail_risk_score: tailRisk.score > 0 ? tailRisk.score : undefined,
		        tail_risk_action: tailRisk.score > 0 ? tailRisk.action : undefined,
		      };

	      // 9. Write full config + cache only on regime shift or initial population
      const isInitialWrite = cacheEntry === undefined || cacheEntry.regime === "unknown" || cacheEntry.regime === "market_closed";
      if (regimeShift || isInitialWrite) {
        const redisConfig: RedisConfig = {
          regime: effectiveRegime,
          regime_conf: effectiveRegimeConf,
          brick_size: brickSize,
          tp_bricks: tpBricks,
          sl_bricks: slBricks,
          markov_p_up: markovPUp,
          markov_p_dn: markovPDn,
          kelly_f: kellyF,
          last_update: Date.now(),
          sweep_timestamp: Date.now(),
          signal: tradeSignal.signal,
          entry_price: tradeSignal.entry_price,
          stop_loss: tradeSignal.stop_loss,
          take_profit: tradeSignal.take_profit,
          ev: evResult.ev,
          ev_per_dollar: evResult.ev_per_dollar,
          p_win: evResult.p_win,
          p_regime: evResult.p_regime,
          p_imbalance: evResult.p_imbalance,
          p_markov: evResult.p_markov,
          aggression: evResult.aggression,
          ev_scale: evResult.ev_scale,
          effective_kelly: effectiveKelly,
          markov_current_state: evResult.markov_current_state,
	          regime_shift: regimeShift ? "true" : "false",
	          prev_regime: prevRegime ?? "",
	          shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
	          shifts_24h: shifts24h,
	          p_timesfm: pTimesfm ?? undefined,
	          timesfm_horizon: pTimesfm !== null ? "12h" : undefined,
	          tail_risk_score: tailRisk.score > 0 ? tailRisk.score : undefined,
	          tail_risk_action: tailRisk.score > 0 ? tailRisk.action : undefined,
	        };
	        await redisSetConfig(symbolConfig.symbol, redisConfig);

        await logRegimeEntry({
          symbol: symbolConfig.symbol,
          source: regimeDegraded ? "sweep-light-degraded" : "sweep-light",
          regime: effectiveRegime,
          regime_conf: effectiveRegimeConf,
          markov_p_up: markovPUp,
          markov_p_dn: markovPDn,
          kelly_f: kellyF,
          prev_regime: prevRegime,
          regime_shift: true,
        });
      }

      console.log(
        `[sweep:light] ${symbolConfig.symbol}: regime=${effectiveRegime}${regimeDegraded ? " (degraded)" : ""}, conf=${effectiveRegimeConf.toFixed(2)}, ` +
        `kelly=${kellyF.toFixed(3)}, risk_mult=${riskMultiplier}, shift=${regimeShift}`,
      );

      return { regime: effectiveRegime, regimeShift, entry: newEntry };

	    } else {
	      // No data available or cold start
	      if (prices.length >= 20 && pTimesfm !== null && TIMESFM_INFERENCE_URL) {
	        // ── Cold-start: TimesFM only (not enough bars for HMM) ──
	        const currentPrice = prices[prices.length - 1];
	        const brickSize = cacheEntry?.brick_size ?? 0.5;
	        const slBricks = cacheEntry?.sl_bricks ?? 3;
	        const tpBricks = cacheEntry?.tp_bricks ?? 5;
	        const tradeSignal = computeTradeSignal("warming_up", currentPrice, brickSize, slBricks, tpBricks, symbolConfig.symbol, symbolConfig.asset_class);
	        const decimals = PRICE_TICK_DECIMALS[symbolConfig.symbol] ?? 2;

	        const redisConfig: RedisConfig = {
	          regime: "warming_up",
	          regime_conf: 0,
	          brick_size: brickSize,
	          tp_bricks: tpBricks,
	          sl_bricks: slBricks,
	          markov_p_up: 0.5,
	          markov_p_dn: 0.5,
	          kelly_f: 0.05,
	          last_update: Date.now(),
	          sweep_timestamp: Date.now(),
	          signal: tradeSignal.signal,
	          entry_price: tradeSignal.entry_price,
	          stop_loss: tradeSignal.stop_loss,
	          take_profit: tradeSignal.take_profit,
	          p_timesfm: pTimesfm,
	          timesfm_horizon: "12h",
	        };
	        await redisSetConfig(symbolConfig.symbol, redisConfig);
	        await redisSetHeartbeat(symbolConfig.symbol, symbolConfig.heartbeat_ttl_seconds);
	        console.log(`[sweep:light] ${symbolConfig.symbol}: warm_up — TimesFM=${pTimesfm.toFixed(4)} (${prices.length} bars, need >=81 for HMM)`);

	        const newEntry: RegimeCacheEntry = {
	          regime: "warming_up",
	          regime_conf: 0,
	          brick_size: brickSize,
	          sl_bricks: slBricks,
	          tp_bricks: tpBricks,
	          kelly_f: 0.05,
	          last_update: Date.now(),
	          signal: tradeSignal.signal,
	          entry_price: tradeSignal.entry_price,
	          stop_loss: tradeSignal.stop_loss,
	          take_profit: tradeSignal.take_profit,
	          price_decimals: decimals,
	          p_timesfm: pTimesfm,
	          timesfm_horizon: "12h",
	        };
	        return { regime: "warming_up", regimeShift: cacheEntry?.regime !== "warming_up", entry: newEntry };
	      }

	      if (!cacheEntry) {
	        console.log(`[sweep:light] ${symbolConfig.symbol}: Initial market_closed write`);
        const brickSize = safeParseFloat(undefined, 0.5);
        const slBricks = safeParseInt(undefined, 3);
        const tpBricks = safeParseInt(undefined, 5);
        const redisConfig: RedisConfig = {
          regime: "market_closed",
          regime_conf: 0,
          brick_size: brickSize,
          tp_bricks: tpBricks,
          sl_bricks: slBricks,
          markov_p_up: 0.5,
          markov_p_dn: 0.5,
          kelly_f: 0,
          last_update: Date.now(),
          signal: "neutral",
        };
        await redisSetConfig(symbolConfig.symbol, redisConfig);
      } else {
        console.log(`[sweep:light] ${symbolConfig.symbol}: market_closed — FILTERED (already exists)`);
      }
      return { regime: "market_closed", regimeShift: false, entry: null };
    }

  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[sweep:light] ${symbolConfig.symbol} FAILED: ${errMsg}`);

    try {
      if (!cacheEntry) {
        const brickSize = safeParseFloat(undefined, 0.5);
        const slBricks = safeParseInt(undefined, 3);
        const tpBricks = safeParseInt(undefined, 5);
        const redisConfig: RedisConfig = {
          regime: "unknown",
          regime_conf: 0,
          brick_size: brickSize,
          tp_bricks: tpBricks,
          sl_bricks: slBricks,
          markov_p_up: 0.5,
          markov_p_dn: 0.5,
          kelly_f: 0,
          last_update: Date.now(),
          signal: "neutral",
        };
        await redisSetConfig(symbolConfig.symbol, redisConfig);
      }
    } catch { /* Best effort */ }

    await redisPushError(symbolConfig.symbol, "sweep-light", errMsg);
    await logError(symbolConfig.symbol, "sweep-light", errMsg);
    return { regime: "error", regimeShift: false, entry: null, error: errMsg };
  }
}

// ─── Noble Trader Backend Warmup ──────────────────────────────
async function warmupNobleTrader(): Promise<void> {
  const url = Deno.env.get("NOBLE_TRADER_URL") ?? "https://noble-trader-fastapi-backend.onrender.com";
  try {
    console.log("[sweep] Warming up Noble Trader backend...");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    const res = await fetch(`${url}/health`, { signal: controller.signal });
    clearTimeout(timer);
    if (res.ok) {
      console.log("[sweep] Noble Trader backend is warm");
    } else {
      console.warn(`[sweep] Noble Trader warmup returned ${res.status}`);
    }
  } catch (err) {
    console.warn(`[sweep] Noble Trader warmup failed (cold start likely): ${err instanceof Error ? err.message : String(err)}`);
  }
}

// ─── Sync Allowlist (ported from heavy-sweep) ─────────────────
async function syncAllowlist(): Promise<Response> {
  try {
    const activeSymbols = await getAllActiveSymbols();
    const hlSymbols: string[] = [];
    const alpacaSymbols: string[] = [];
    const venueMapCommands: unknown[][] = [];

    const allSymbols = await supabaseRest("nt_symbol", "GET", undefined, "select=symbol,active") as { symbol: string; active: boolean }[];
    const deactivated = allSymbols.filter(s => !s.active);
    for (const s of deactivated) {
      venueMapCommands.push(["DEL", `trading:venue_map:${s.symbol}`]);
    }

    for (const sym of activeSymbols) {
      if (sym.data_source === "hyperliquid") {
        hlSymbols.push(sym.symbol);
        venueMapCommands.push([
          "HSET", `trading:venue_map:${sym.symbol}`,
          "venueId", "hyperliquid",
          "assetClass", sym.asset_class,
        ]);
      } else if (sym.data_source === "alpaca") {
        alpacaSymbols.push(sym.symbol);
        venueMapCommands.push([
          "HSET", `trading:venue_map:${sym.symbol}`,
          "venueId", "alpaca",
          "assetClass", sym.asset_class,
          ...(sym.alpaca_symbol ? ["alpacaSymbol", sym.alpaca_symbol] : []),
        ]);
      }
    }

    const pipeline: unknown[][] = [
      ["DEL", "trading:allowlist:hyperliquid"],
      ["SADD", "trading:allowlist:hyperliquid", ...hlSymbols],
      ["DEL", "trading:allowlist:alpaca"],
      ["SADD", "trading:allowlist:alpaca", ...alpacaSymbols],
      ...venueMapCommands,
    ];

    let totalCommands = 0;
    for (let i = 0; i < pipeline.length; i += 10) {
      const chunk = pipeline.slice(i, i + 10);
      const res = await fetch(`${UPSTASH_URL}/pipeline`, {
        method: "POST",
        headers: sweepRedisHeaders(),
        body: JSON.stringify(chunk),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Redis pipeline error: ${res.status} — ${text}`);
      }
      totalCommands += chunk.length;
    }

    return new Response(
      JSON.stringify({
        success: true,
        action: "sync-allowlist",
        hl_symbols: hlSymbols,
        alpaca_symbols: alpacaSymbols,
        venue_map_entries: venueMapCommands.length,
        deactivated_cleaned: deactivated.length,
        total_redis_commands: totalCommands,
      }),
      { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    return new Response(
      JSON.stringify({ error: errMsg }),
      { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }
}

// ─── Startup Validation ──────────────────────────────────────
const REQUIRED_ENV_VARS = [
  "UPSTASH_REDIS_REST_URL",
  "UPSTASH_REDIS_REST_TOKEN",
  "SB_URL",
  "SB_SERVICE_ROLE_KEY",
];

const MISSING_ENV_VARS = REQUIRED_ENV_VARS.filter((v) => !Deno.env.get(v));
if (MISSING_ENV_VARS.length > 0) {
  console.error(
    `[sweep] FATAL: Missing required env vars: ${MISSING_ENV_VARS.join(", ")}. ` +
    `Set them in Supabase Dashboard → Edge Functions → Secrets.`
  );
}

// ─── Main Handler ───────────────────────────────────────────
Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders() });
  }

  const body = await req.json().catch(() => ({}));
  const { mode, symbol, batch, symbols, warmup, action } = body;

  // ── Warmup ping: boot the worker, return immediately ──
  if (warmup === true) {
    await warmupNobleTrader();
    return new Response(
      JSON.stringify({ status: "warmup", message: "Worker booted + Noble Trader warmed", timestamp: Date.now() }),
      { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── Sync allowlist (admin action) ──
  if (action === "sync-allowlist") {
    return syncAllowlist();
  }

  // ── Sync calibration (admin action, v3) ──
  if (action === "sync-calibration") {
    try {
      const activeSymbols = await getAllActiveSymbols();
      let updated = 0;
      for (const sym of activeSymbols) {
        const cal = await computeCalibrationBias(sym.symbol);
        if (cal) {
          await sweepRedisPipeline([
            [
              "HSET", `trading:config:${sym.symbol}`,
              "calibration_bias", String(cal.bias),
              "calibration_status", cal.status,
            ],
          ]);
          updated++;
        }
      }
      return new Response(
        JSON.stringify({ status: "ok", symbols_checked: activeSymbols.length, symbols_updated: updated }),
        { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return new Response(
        JSON.stringify({ error: msg }),
        { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    }
  }

  // ── Env var check ──
  if (MISSING_ENV_VARS.length > 0) {
    return new Response(
      JSON.stringify({
        error: `Missing required env vars: ${MISSING_ENV_VARS.join(", ")}`,
        hint: "Set them in Supabase Dashboard → Edge Functions → Secrets",
      }),
      { status: 503, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── Validate mode ──
  if (mode !== "heavy" && mode !== "light") {
    return new Response(
      JSON.stringify({ error: "Provide 'mode' (heavy|light), 'action', or 'warmup' in request body" }),
      { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── F-Lite-1: Suppress light regime during heavy hours ──
  // Defense in depth — the cron schedule already avoids firing light
  // during these hours, but this catches any caller that bypasses the
  // schedule (retry-stale-symbols pre-migration-017, manual curl, etc).
  if (mode === "light" && isHeavyHour()) {
    const hourUTC = new Date().getUTCHours();
    const syms = symbol ?? (Array.isArray(batch) ? batch.join(",") : (Array.isArray(symbols) ? symbols.join(",") : "?"));
    console.log(`[sweep] mode=light SUPPRESSED — heavy hour ${hourUTC} UTC (F-Lite-1). symbols=[${syms}]`);
    return new Response(
      JSON.stringify({
        status: "suppressed",
        reason: "f-lite-1",
        hour: hourUTC,
        symbols: syms,
        message: "Light regime suppressed during heavy sweep hour — defer to heavy sweep cron",
        timestamp: Date.now(),
      }),
      { status: 202, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── Resolve symbols from symbol, batch, or symbols array ──
  // batch and symbols are aliases (batch is the new name; symbols kept for back-compat)
  const batchArray = Array.isArray(batch) ? batch : (Array.isArray(symbols) ? symbols : null);
  const isBatch = batchArray !== null && batchArray.length > 0;

  let symbolsToProcess: SymbolConfig[];
  if (symbol) {
    const found = await getSymbolByName(symbol);
    if (!found) {
      return new Response(
        JSON.stringify({ error: `Symbol ${symbol} not found in registry` }),
        { status: 404, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    }
    symbolsToProcess = [found];
  } else if (isBatch) {
    const found: SymbolConfig[] = [];
    for (const sym of batchArray!) {
      const s = await getSymbolByName(sym);
      if (s) found.push(s);
      else console.warn(`[sweep] Symbol ${sym} not found, skipping`);
    }
    if (found.length === 0) {
      return new Response(
        JSON.stringify({ error: "None of the provided symbols were found in registry" }),
        { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    }
    symbolsToProcess = found;
  } else {
    return new Response(
      JSON.stringify({ error: "Provide 'symbol' or 'batch' in request body" }),
      { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── Acquire global concurrency slot (Layer 2) ──
  const invocationToken = crypto.randomUUID();
  const globalSlotAcquired = await acquireGlobalSlot(invocationToken);
  if (!globalSlotAcquired) {
    console.warn(`[sweep] mode=${mode} symbols=[${symbolsToProcess.map(s => s.symbol).join(",")}]: DEFERRED — global cap (${GLOBAL_CAP}) reached. retry-stale-symbols will pick this up.`);
    return new Response(
      JSON.stringify({
        status: "deferred",
        reason: "concurrency-cap",
        cap: GLOBAL_CAP,
        mode,
        symbols: symbolsToProcess.map(s => s.symbol),
        message: `Global concurrency cap (${GLOBAL_CAP}) reached. retry-stale-symbols will re-invoke shortly.`,
        timestamp: Date.now(),
      }),
      { status: 202, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  try {
    // ── Warm up Noble Trader backend (avoid cold start during sweep) ──
    if (mode === "heavy") {
      await warmupNobleTrader();
    }

    console.log(`[sweep] mode=${mode} processing ${symbolsToProcess.length} symbols: [${symbolsToProcess.map(s => s.symbol).join(",")}]`);

    // ── Heavy mode: process sequentially with deadline ──
    if (mode === "heavy") {
      const DEADLINE_MS = 120_000;
      const startTime = Date.now();
      const results: Record<string, unknown> = {};

      for (let i = 0; i < symbolsToProcess.length; i++) {
        const elapsed = Date.now() - startTime;
        if (elapsed > DEADLINE_MS) {
          console.warn(`[sweep:heavy] Deadline exceeded (${elapsed}ms > ${DEADLINE_MS}ms). Skipping remaining.`);
          for (let j = i; j < symbolsToProcess.length; j++) {
            results[symbolsToProcess[j].symbol] = { error: "Deadline exceeded — skipped" };
          }
          break;
        }

        const sym = symbolsToProcess[i];

        // ── Layer 1: Per-symbol lock ──
        const symbolLockToken = `${invocationToken}:${sym.symbol}`;
        const lockAcquired = await acquireSymbolLock(sym.symbol, "heavy", symbolLockToken);
        if (!lockAcquired) {
          const skipMsg = "skipped: symbol-locked by concurrent invocation (likely retry-stale light during heavy hour)";
          console.warn(`[sweep:heavy] ${sym.symbol}: SKIPPED — symbol locked by another invocation`);
          // Fix C: log skip to nt_errors for dashboard visibility
          try {
            await logError(sym.symbol, "sweep-heavy", skipMsg);
          } catch { /* best effort — don't let logging fail the sweep */ }
          results[sym.symbol] = { skipped: true, reason: "symbol-locked", message: skipMsg };
          continue;
        }

        try {
          console.log(`[sweep:heavy] Starting sweep for ${sym.symbol} (${i + 1}/${symbolsToProcess.length}, ${elapsed}ms elapsed)...`);
          const result = await runHeavySweepForSymbol(sym);
          results[sym.symbol] = result;
        } finally {
          await releaseSymbolLock(sym.symbol, symbolLockToken);
        }

        if (i < symbolsToProcess.length - 1) {
          await sleep(1000);
        }
      }

      const successCount = Object.values(results).filter((r: any) => !r.error && !r.skipped).length;
      const failCount = Object.values(results).filter((r: any) => r.error).length;
      const skipCount = Object.values(results).filter((r: any) => r.skipped).length;

      return new Response(
        JSON.stringify({
          success: true,
          mode: "heavy",
          symbols_processed: symbolsToProcess.length,
          successes: successCount,
          failures: failCount,
          skipped: skipCount,
          results,
        }),
        { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    }

    // ── Light mode: process synchronously (returns actual results) ──
    const batchResults = await processLightBatch(symbolsToProcess, invocationToken);

    return new Response(
      JSON.stringify({
        status: "completed",
        mode: "light",
        message: `Processed ${symbolsToProcess.length} symbols`,
        symbols: symbolsToProcess.map(s => s.symbol),
        results: batchResults,
        timestamp: Date.now(),
      }),
      { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );

  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[sweep] Fatal error: ${errMsg}`);
    return new Response(
      JSON.stringify({ error: errMsg }),
      { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  } finally {
    // ── Release global slot (Layer 2) ──
    // NOTE: For light mode, we return 202 immediately but background processing
    // continues. The global slot is released here (before background work finishes).
    // This is acceptable because:
    // 1. The per-symbol lock (Layer 1) still protects against same-symbol double-fire
    // 2. The global slot's purpose is to prevent 504s from too many SIMULTANEOUS
    //    invocations arriving at the edge runtime — once we've returned 202, the
    //    edge runtime is no longer blocked on this invocation
    // 3. Background work happens in the Deno isolate which stays alive independently
    await releaseGlobalSlot(invocationToken);
  }
});

// ─── Light Batch Background Processor ─────────────────────────
async function processLightBatch(
  symbolsToProcess: SymbolConfig[],
  invocationToken: string,
): Promise<{ processed: number; skipped: number; errors: number; cacheChanged: boolean; symbols: Record<string, { regime: string; error?: string }> }> {
  const result = { processed: 0, skipped: 0, errors: 0, cacheChanged: false, symbols: {} as Record<string, { regime: string; error?: string }> };
  try {
    const regimeCache = await redisLoadRegimeCache();
    const DEADLINE_MS = 110_000;
    const PER_SYMBOL_DEADLINE_MS = 40_000;
    const startTime = Date.now();
    const RATE_LIMIT_DELAY_MS = 8000;
    let cacheChanged = false;

    // ── Vectorized TimesFM: fetch prices + one batched call ──
    let timesfmMap = new Map<string, number | null>();
    const pricesBySymbol: Record<string, number[]> = {};
    if (TIMESFM_INFERENCE_URL && TIMESFM_API_KEYS) {
      try {
        const priceInputs = await Promise.all(
          symbolsToProcess.map(async (sym) => {
            let prices: number[] = [];
            try {
              prices = await fetchLatestCandles(sym, 200);
            } catch { /* best effort */ }
            pricesBySymbol[sym.symbol] = prices;
            return { symbol: sym.symbol, prices: prices.slice(-500) };
          }),
        );
        timesfmMap = await timesfmPost(priceInputs);
        console.log(`[sweep:light] Vectorized TimesFM: ${timesfmMap.size} symbols processed`);

        // ── Lead/Lag: correlation adjustment ──
        const { leaders } = computeCorrelationMatrix(pricesBySymbol);
        const hasAdjustments = Object.values(leaders).some((l) => l.length > 0);
        if (hasAdjustments) {
          let adjustedCount = 0;
          for (const sym of symbolsToProcess) {
            const adjusted = adjustLeadLag(sym.symbol, timesfmMap, leaders);
            if (adjusted !== null && adjusted !== timesfmMap.get(sym.symbol)) {
              timesfmMap.set(sym.symbol, adjusted);
              adjustedCount++;
            }
          }
          if (adjustedCount > 0) {
            console.log(`[sweep:light] Lead/Lag adjusted ${adjustedCount}/${symbolsToProcess.length} symbols`);
          }
        }
      } catch (err) {
        console.warn(`[sweep:light] Vectorized TimesFM call failed: ${err}`);
      }
    }

    for (let i = 0; i < symbolsToProcess.length; i++) {
      const elapsed = Date.now() - startTime;
	      if (elapsed > DEADLINE_MS) {
	        console.warn(`[sweep:light] Deadline exceeded (${elapsed}ms). Skipping remaining ${symbolsToProcess.length - i} symbols.`);
	        result.errors += symbolsToProcess.length - i;
	        break;
      }

      const sym = symbolsToProcess[i];
      const cacheEntry = regimeCache[sym.symbol];
      console.log(`[sweep:light] Processing ${sym.symbol} (${i + 1}/${symbolsToProcess.length}, cache=${cacheEntry ? "hit" : "miss"}, ${elapsed}ms elapsed)...`);

      // ── Layer 1: Per-symbol lock ──
      const symbolLockToken = `${invocationToken}:${sym.symbol}`;
      const lockAcquired = await acquireSymbolLock(sym.symbol, "light", symbolLockToken);
	      if (!lockAcquired) {
	        const skipMsg = "skipped: symbol-locked by concurrent invocation (heavy sweep holding lock)";
	        console.warn(`[sweep:light] ${sym.symbol}: SKIPPED — symbol locked by another invocation`);
	        result.skipped++;
	        result.symbols[sym.symbol] = { regime: "skipped_locked", error: skipMsg };
	        // Fix C: log skip to nt_errors for dashboard visibility
        try {
          await logError(sym.symbol, "sweep-light", skipMsg);
        } catch { /* best effort */ }
        continue;
      }

      try {
        // Race: process symbol vs per-symbol deadline
        const symbolResult = await Promise.race([
          runLightRegimeForSymbol(sym, cacheEntry, timesfmMap.get(sym.symbol) ?? undefined),
          sleep(PER_SYMBOL_DEADLINE_MS).then(() => ({
            regime: "skipped_timeout" as const,
            regimeShift: false,
            entry: null as RegimeCacheEntry | null,
            error: `Per-symbol deadline exceeded (${PER_SYMBOL_DEADLINE_MS}ms)`,
          })),
        ]);

	        if (symbolResult.entry) {
	          regimeCache[sym.symbol] = symbolResult.entry;
	          if (symbolResult.regimeShift) cacheChanged = true;
	        }

	        if (symbolResult.error) {
	          result.errors++;
	          result.symbols[sym.symbol] = { regime: symbolResult.regime, error: symbolResult.error };
	        } else {
	          result.processed++;
	          result.symbols[sym.symbol] = { regime: symbolResult.regime };
	        }
	      } finally {
        await releaseSymbolLock(sym.symbol, symbolLockToken);
      }

      if (i < symbolsToProcess.length - 1) {
        await sleep(RATE_LIMIT_DELAY_MS);
      }
    }

    if (cacheChanged) {
      await redisSaveRegimeCache(regimeCache);
      console.log(`[sweep:light] Regime cache saved (1 SET)`);
    } else {
      console.log(`[sweep:light] Regime cache unchanged — no write needed`);
    }

    result.cacheChanged = cacheChanged;
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[sweep:light] Background processing error: ${errMsg}`);
    result.errors += symbolsToProcess.length;
  }

  return result;
}
