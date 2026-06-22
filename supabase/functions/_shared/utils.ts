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
// v2: Upgraded lookback 45d → 90d for more statistical significance.
//     3. generateBrickSizes: add 0.75× and 3.0× back (3 → 5 bricks)
//     4. sweep_window labels: "45d" → "90d"
//     5. Optimize timeout: 90s → 120s (more data = longer compute)

// ─── Environment ────────────────────────────────────────────
const UPSTASH_URL = Deno.env.get("UPSTASH_REDIS_REST_URL")!;
const UPSTASH_TOKEN = Deno.env.get("UPSTASH_REDIS_REST_TOKEN")!;
const SUPABASE_URL = Deno.env.get("SB_URL") ?? Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE =
  Deno.env.get("SB_SERVICE_ROLE_KEY") ??
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const NOBLE_TRADER_URL = Deno.env.get("NOBLE_TRADER_URL") ?? "";
const TIMESFM_INFERENCE_URL =
  Deno.env.get("TIMESFM_INFERENCE_URL") ?? (NOBLE_TRADER_URL || "");
const TIMESFM_API_KEYS = Deno.env.get("TIMESFM_API_KEYS") ?? "";

// ─── Types ──────────────────────────────────────────────────
export interface SymbolConfig {
  id: number;
  symbol: string;
  asset_class: string;
  data_source: string; // "hyperliquid" | "alpaca" | "yahoo"
  yahoo_ticker: string | null;
  hyperliquid_coin: string | null;
  alpaca_symbol: string | null; // e.g. "AAPL", "BTC/USD", "GLD"
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
  signal?: string; // "buy" | "sell" | "neutral"
  entry_price?: number; // Current market price at signal
  stop_loss?: number; // Price if market goes against signal
  take_profit?: number; // Price if market goes in favor
  price_decimals?: number; // Tick-size decimal places for this symbol

  // EV Engine outputs (NEW)
  ev?: number; // Raw EV in price units
  ev_per_dollar?: number; // Normalized EV per $1 risked
  p_win?: number; // Blended P_win (0-1)
  p_regime?: number; // Regime component of P_win
  p_imbalance?: number; // L2 imbalance component (0.50 if no data)
  p_markov?: number; // Markov transition component
  aggression?: string; // "passive" | "mid" | "aggressive"
  ev_scale?: number; // Kelly scaling factor from EV
  effective_kelly?: number; // kelly_f * ev_scale
  markov_current_state?: string; // Current Markov state ("UP"/"DOWN"/"FLAT")

  // Regime Shift fields (v2.3 — Shift-Aware EV)
  regime_shift?: string; // "true" | "false" — did a shift occur this cycle?
  prev_regime?: string; // Regime we transitioned FROM (e.g. "low_vol_bull")
  shift_at?: number; // Unix ms when shift was detected (timestamp approach)
  shifts_24h?: number; // Count of shifts in last 24h (volatility meta-signal)

  // TimesFM fields (v3)
  p_timesfm?: number; // Raw TimesFM forecast probability (0-1)
  timesfm_horizon?: string; // Forecast window label (e.g. "12h")
  timesfm_conf?: number; // TimesFM model confidence (0-1)

  // Calibration fields (v3)
  calibration_bias?: number; // avg(predictedPWin) - actual_win_rate
  calibration_status?: string; // "CALIBRATED" | "OVERCONFIDENT" | "UNDERCONFIDENT"

  // Tail risk fields (v3)
  tail_risk_score?: number; // 0.0 (none) - 1.0 (critical)
  tail_risk_action?: string; // "none" | "reduce_25" | "reduce_50" | "skip"

  // Sweep fatigue fields (v3)
  sweep_skip_until?: number; // epoch ms — skip heavy sweeps until this time
}

// Noble Trader /regime/detect actual response
export interface RegimeResult {
  symbol: string;
  vol_probs: Record<string, number>;
  trend_probs: Record<string, number>;
  vol_state: string; // "low" | "med_low" | "med_high" | "high"
  trend_state: string; // "strong_bear" | "bear" | "neutral" | "bull" | "strong_bull"
  regime_label: string; // e.g. "high_vol_strong_bull"
  confidence: number; // 0.0 - 1.0
  risk_multiplier: number; // 0.2 - 1.0
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
  kelly_inputs: {
    win_rate: number;
    avg_win: number;
    avg_loss: number;
    implied_kelly: number;
  };
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
  BTC: 1, // szDecimals=5 → max 1 decimal,  5 sig figs
  ETH: 2, // szDecimals=4 → max 2 decimals, 5 sig figs
  SOL: 4, // szDecimals=2 → max 4 decimals, 5 sig figs
  // ── US stocks (penny ticks) ──
  AAPL: 2,
  AMZN: 2,
  GOOGL: 2,
  META: 2,
  MSFT: 2,
  NVDA: 2,
  TSLA: 2,
  SPY: 2,
  QQQ: 2,
  // ── Alpaca crypto spot ──
  BTCUSD: 1, // same tick as BTC perp
  ETHUSD: 2, // same tick as ETH perp
  SOLUSD: 4, // same tick as SOL perp
};

/** Set of Hyperliquid perp symbols for sig-fig rounding */
const HL_PERP_SYMBOLS = new Set(["BTC", "ETH", "SOL"]);

/** Default decimal places by asset class (fallback if symbol not in map) */
const DEFAULT_TICK_DECIMALS: Record<string, number> = {
  crypto: 6, // Hyperliquid max for low-priced perps
  stocks: 2,
  forex: 5,
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
export function roundPrice(
  price: number,
  symbol: string,
  assetClass?: string,
): number {
  if (price === 0) return 0;

  const maxDecimals =
    PRICE_TICK_DECIMALS[symbol] ?? DEFAULT_TICK_DECIMALS[assetClass ?? ""] ?? 2;

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
    "Access-Control-Allow-Headers":
      "authorization, x-client-info, apikey, content-type",
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

async function redisCommand(
  cmd: string,
  ...args: (string | number)[]
): Promise<unknown> {
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

export async function redisSetConfig(
  symbol: string,
  config: RedisConfig,
): Promise<void> {
  // Determine tick-size decimals for this symbol (for price formatting)
  const decimals =
    config.price_decimals ??
    PRICE_TICK_DECIMALS[symbol] ??
    DEFAULT_TICK_DECIMALS["crypto"] ?? // fallback
    2;

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
    last_update: Number.isNaN(config.last_update)
      ? Date.now()
      : config.last_update,
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
    entries.push([
      "sweep_timestamp",
      Number.isNaN(ts) || ts <= 0 ? Date.now() : ts,
    ]);
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
    entries.push([
      "ev_per_dollar",
      Number.isNaN(config.ev_per_dollar) ? 0 : config.ev_per_dollar,
    ]);
  }
  if (config.p_win !== undefined) {
    entries.push(["p_win", Number.isNaN(config.p_win) ? 0 : config.p_win]);
  }
  if (config.p_regime !== undefined) {
    entries.push([
      "p_regime",
      Number.isNaN(config.p_regime) ? 0 : config.p_regime,
    ]);
  }
  if (config.p_imbalance !== undefined) {
    entries.push([
      "p_imbalance",
      Number.isNaN(config.p_imbalance) ? 0 : config.p_imbalance,
    ]);
  }
  if (config.p_markov !== undefined) {
    entries.push([
      "p_markov",
      Number.isNaN(config.p_markov) ? 0 : config.p_markov,
    ]);
  }
  if (config.aggression !== undefined) {
    entries.push(["aggression", config.aggression]);
  }
  if (config.ev_scale !== undefined) {
    entries.push([
      "ev_scale",
      Number.isNaN(config.ev_scale) ? 0 : config.ev_scale,
    ]);
  }
  if (config.effective_kelly !== undefined) {
    entries.push([
      "effective_kelly",
      Number.isNaN(config.effective_kelly) ? 0 : config.effective_kelly,
    ]);
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
    entries.push([
      "shift_at",
      Number.isNaN(config.shift_at) || config.shift_at <= 0
        ? 0
        : config.shift_at,
    ]);
  }
  if (config.shifts_24h !== undefined) {
    entries.push([
      "shifts_24h",
      Number.isNaN(config.shifts_24h) ? 0 : config.shifts_24h,
    ]);
  }

  // TimesFM fields (v3)
  if (config.p_timesfm !== undefined) {
    entries.push([
      "p_timesfm",
      Number.isNaN(config.p_timesfm) ? 0 : config.p_timesfm,
    ]);
  }
  if (config.timesfm_horizon !== undefined) {
    entries.push(["timesfm_horizon", config.timesfm_horizon]);
  }
  if (config.timesfm_conf !== undefined) {
    entries.push([
      "timesfm_conf",
      Number.isNaN(config.timesfm_conf) ? 0 : config.timesfm_conf,
    ]);
  }

  // Calibration fields (v3)
  if (config.calibration_bias !== undefined) {
    entries.push([
      "calibration_bias",
      Number.isNaN(config.calibration_bias) ? 0 : config.calibration_bias,
    ]);
  }
  if (config.calibration_status !== undefined) {
    entries.push(["calibration_status", config.calibration_status]);
  }

  // Tail risk fields (v3)
  if (config.tail_risk_score !== undefined) {
    entries.push([
      "tail_risk_score",
      Number.isNaN(config.tail_risk_score) ? 0 : config.tail_risk_score,
    ]);
  }
  if (config.tail_risk_action !== undefined) {
    entries.push(["tail_risk_action", config.tail_risk_action]);
  }

  // Sweep fatigue fields (v3)
  if (config.sweep_skip_until !== undefined) {
    entries.push([
      "sweep_skip_until",
      Number.isNaN(config.sweep_skip_until) || config.sweep_skip_until <= 0
        ? 0
        : config.sweep_skip_until,
    ]);
  }

  for (const [k, v] of entries) {
    fields.push(k, String(v));
  }

  await redisPipeline([["HSET", `trading:config:${symbol}`, ...fields]]);
}

export async function redisSetHeartbeat(
  symbol: string,
  ttlSeconds: number,
): Promise<void> {
  await redisPipeline([
    [
      "SET",
      `trading:heartbeat:${symbol}`,
      String(Date.now()),
      "EX",
      String(ttlSeconds),
    ],
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
  regime_shift?: string; // "true" | "false" — persisted to Redis
  prev_regime?: string; // Regime transitioned FROM
  shift_at?: number; // Unix ms when shift detected
  shifts_24h?: number; // Count of shifts in last 24h

  // TimesFM fields (v3)
  p_timesfm?: number; // TimesFM forecast probability
  timesfm_horizon?: string; // Forecast window label
  timesfm_conf?: number; // TimesFM confidence

  // Calibration fields (v3)
  calibration_bias?: number;
  calibration_status?: string;

  // Tail risk fields (v3)
  tail_risk_score?: number;
  tail_risk_action?: string;

  // Sweep fatigue fields (v3)
  sweep_skip_until?: number;
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
    await redisPipeline([["SET", REGIME_CACHE_KEY, JSON.stringify(cache)]]);
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
    "signal",
    signal,
    "entry_price",
    entryPrice.toFixed(decimals),
    "stop_loss",
    stopLoss.toFixed(decimals),
    "take_profit",
    takeProfit.toFixed(decimals),
    "last_update",
    String(Date.now()),
    "sweep_timestamp",
    String(Date.now()), // Refresh on every light-regime cycle too
  ];

  // Add EV fields if provided
  if (evFields) {
    if (evFields.ev !== undefined) fields.push("ev", String(evFields.ev));
    if (evFields.ev_per_dollar !== undefined)
      fields.push("ev_per_dollar", String(evFields.ev_per_dollar));
    if (evFields.p_win !== undefined)
      fields.push("p_win", String(evFields.p_win));
    if (evFields.aggression !== undefined)
      fields.push("aggression", evFields.aggression);
    if (evFields.ev_scale !== undefined)
      fields.push("ev_scale", String(evFields.ev_scale));
    if (evFields.effective_kelly !== undefined)
      fields.push("effective_kelly", String(evFields.effective_kelly));
  }

  // Add TimesFM fields if provided
  if (timesfmFields) {
    if (timesfmFields.p_timesfm !== undefined)
      fields.push("p_timesfm", String(timesfmFields.p_timesfm));
    if (timesfmFields.timesfm_horizon !== undefined)
      fields.push("timesfm_horizon", timesfmFields.timesfm_horizon);
    if (timesfmFields.timesfm_conf !== undefined)
      fields.push("timesfm_conf", String(timesfmFields.timesfm_conf));
  }

  // Add calibration fields if provided
  if (calibrationFields) {
    if (calibrationFields.calibration_bias !== undefined)
      fields.push(
        "calibration_bias",
        String(calibrationFields.calibration_bias),
      );
    if (calibrationFields.calibration_status !== undefined)
      fields.push("calibration_status", calibrationFields.calibration_status);
  }

  // Add tail risk fields if provided
  if (tailRiskFields) {
    if (tailRiskFields.tail_risk_score !== undefined)
      fields.push("tail_risk_score", String(tailRiskFields.tail_risk_score));
    if (tailRiskFields.tail_risk_action !== undefined)
      fields.push("tail_risk_action", tailRiskFields.tail_risk_action);
  }

  // Add regime shift fields if provided
  if (shiftFields) {
    if (shiftFields.regime_shift !== undefined)
      fields.push("regime_shift", shiftFields.regime_shift);
    if (shiftFields.prev_regime !== undefined)
      fields.push("prev_regime", shiftFields.prev_regime);
    if (shiftFields.shift_at !== undefined)
      fields.push("shift_at", String(shiftFields.shift_at));
    if (shiftFields.shifts_24h !== undefined)
      fields.push("shifts_24h", String(shiftFields.shifts_24h));
  }

  await redisPipeline([["HSET", `trading:config:${symbol}`, ...fields]]);
}

export async function redisGetConfig(
  symbol: string,
): Promise<Record<string, string> | null> {
  const result = await redisCommand("HGETALL", `trading:config:${symbol}`);
  if (!result || !Array.isArray(result) || result.length === 0) return null;

  const obj: Record<string, string> = {};
  for (let i = 0; i < result.length; i += 2) {
    obj[result[i]] = result[i + 1];
  }
  return obj;
}

export async function redisPushError(
  symbol: string,
  fnName: string,
  errorMsg: string,
): Promise<void> {
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
    const twentyFourHoursAgo = new Date(
      Date.now() - 24 * 60 * 60 * 1000,
    ).toISOString();
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
// Calls the Lightning AI TimesFM endpoint from within the edge function.
//
// API (from openapi.json):
//   POST /forecast
//   Header: X-API-Key
//   Body:   {"horizon": int, "inputs": [[prices...], [prices...], ...]}
//   Response: {"point_forecast": [[f1,f2,...], ...], "num_series": N, ...}
//
// The /forecast endpoint accepts multiple time series in one call (batched by design).
// p_timesfm is computed from the forecast direction: if point_forecast > last price → bullish.
export async function timesfmPost(
  symbolsAndPrices: { symbol: string; prices: number[] }[],
  horizon = 12,
): Promise<Map<string, number | null>> {
  if (!TIMESFM_INFERENCE_URL || !TIMESFM_API_KEYS) {
    return new Map();
  }

  // Filter out symbols with no prices; build inputs 2D array
  const valid = symbolsAndPrices.filter((s) => s.prices.length >= 10);
  if (valid.length === 0) return new Map();

  const inputs = valid.map((s) => s.prices.slice(-500));
  const symbols = valid.map((s) => s.symbol);
  const lastPrices = valid.map((s) => s.prices[s.prices.length - 1]);

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    const res = await fetch(`${TIMESFM_INFERENCE_URL}/forecast`, {
      method: "POST",
      headers: {
        "X-API-Key": TIMESFM_API_KEYS,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ inputs, horizon }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    if (!res.ok) {
      console.warn(
        `[timesfm] HTTP ${res.status} — ${await res.text().catch(() => "")}`,
      );
      return new Map();
    }

    const data = (await res.json()) as {
      point_forecast?: number[][];
      num_series?: number;
    };

    if (!data.point_forecast || data.point_forecast.length === 0) {
      return new Map();
    }

    // Convert forecast direction to p_timesfm (0.5 = neutral, >0.5 = bullish)
    const map = new Map<string, number | null>();
    for (let i = 0; i < data.point_forecast.length && i < symbols.length; i++) {
      const forecast = data.point_forecast[i];
      const lastPrice = lastPrices[i];
      if (!forecast || forecast.length === 0 || lastPrice <= 0) {
        map.set(symbols[i], null);
        continue;
      }
      // Average of first 3 forecast steps for stability
      const avgForecast =
        forecast
          .slice(0, Math.min(3, forecast.length))
          .reduce((a, b) => a + b, 0) / Math.min(3, forecast.length);
      // Directional probability: how far in % the forecast moved, normalized to 0-1
      const pctChange = (avgForecast - lastPrice) / lastPrice;
      // Scale: 1% move = 0.50 probability, capped at [0.1, 0.9]
      const pTimesfm = Math.max(0.1, Math.min(0.9, 0.5 + pctChange * 50));
      map.set(symbols[i], Math.round(pTimesfm * 10000) / 10000);
      console.log(
        `[timesfm] ${symbols[i]}: price=${lastPrice.toFixed(2)} → forecast=${avgForecast.toFixed(2)} (p=${pTimesfm.toFixed(4)})`,
      );
    }
    return map;
  } catch (err) {
    console.warn(
      `[timesfm] Request failed: ${err instanceof Error ? err.message : String(err)}`,
    );
    return new Map();
  }
}

// ─── Calibration Bias ──────────────────────────────────────
// Queries nt_calibration_records for a symbol and returns the
// calibration bias: AVG(predicted_p_win) - actual_win_rate.
// Returns null on failure or insufficient data.
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
      return null; // Insufficient data
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
export async function supabaseRest(
  table: string,
  method: string,
  body?: unknown,
  query?: string,
): Promise<unknown> {
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
async function supabaseRestWithRetry(
  table: string,
  method: string,
  body?: unknown,
  query?: string,
  retries = 1,
): Promise<unknown> {
  try {
    return await supabaseRest(table, method, body, query);
  } catch (err) {
    if (retries <= 0) throw err;
    const msg = err instanceof Error ? err.message : String(err);
    // Only retry on transient errors (503, connection, network), not 404/400
    if (
      msg.includes("503") ||
      msg.includes("connection") ||
      msg.includes("network") ||
      msg.includes("reset")
    ) {
      console.warn(
        `[supabaseRestWithRetry] ${table} transient error, retrying: ${msg}`,
      );
      await sleep(1000);
      return supabaseRest(table, method, body, query);
    }
    throw err;
  }
}

export async function getSymbolsByAssetClass(
  assetClass: string,
): Promise<SymbolConfig[]> {
  const data = await supabaseRestWithRetry(
    "nt_symbol",
    "GET",
    undefined,
    `asset_class=eq.${assetClass}&active=eq.true&select=*`,
  );
  return data as SymbolConfig[];
}

export async function getSymbolByName(
  symbol: string,
): Promise<SymbolConfig | null> {
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

export async function logSweepResult(
  result: Record<string, unknown>,
): Promise<void> {
  await supabaseRest("nt_sweep_result", "POST", result);
}

export async function logRegimeEntry(
  entry: Record<string, unknown>,
): Promise<void> {
  await supabaseRest("nt_regime_log", "POST", entry);
}

export async function logError(
  symbol: string,
  fnName: string,
  errorMsg: string,
): Promise<void> {
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

async function nobleTraderPost(
  path: string,
  body: unknown,
  timeoutMs = 30000,
): Promise<unknown> {
  const maxRetries = 2; // 1 initial + 1 retry (down from 3)
  const baseDelayMs = 2000; // 2s base (down from 5s)
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
            `Skipping — next cron cycle will retry. Retry-After: ${retryAfter ?? "unknown"}`,
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
          `[nobleTraderPost] ${path}: attempt ${attempt + 1} failed, retrying in ${delayMs}ms — ${lastError.message}`,
        );
        await sleep(delayMs);
        continue;
      }
      break;
    } finally {
      clearTimeout(timer);
    }
  }

  throw (
    lastError ||
    new Error(`Noble Trader ${path}: all ${maxRetries} retries exhausted`)
  );
}

export async function runRenkoBacktest(
  prices: number[],
  symbol: string,
  brickSize: number,
  slBricks: number,
  tpBricks: number,
  initialCapital = 100000,
): Promise<BacktestResult> {
  const raw = (await nobleTraderPost("/renko/backtest/run", {
    prices,
    symbol,
    brick_size: brickSize,
    sl_bricks: slBricks,
    tp_bricks: tpBricks,
    initial_capital: initialCapital,
    commission_bps: 5,
    slippage_bps: 2,
  })) as Record<string, unknown>;

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
    total_pnl_bricks:
      journal.total_pnl_bricks ?? stats.state?.total_pnl_bricks ?? 0,
    profit_factor: journal.profit_factor ?? 0,
    avg_pnl_bricks: journal.avg_pnl_bricks ?? 0,
    avg_win_bricks: journal.avg_win_bricks ?? 0,
    avg_loss_bricks: journal.avg_loss_bricks ?? 0,
    kelly_inputs: journal.kelly_inputs ?? {
      win_rate: 0,
      avg_win: 0,
      avg_loss: 0,
      implied_kelly: 0,
    },
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
  const filteredTp = tpBricksRange.filter((tp) => {
    // At least one SL in range produces R:R >= minRR with this TP
    return filteredSl.some((sl) => tp / sl >= minRR);
  });

  if (filteredTp.length === 0) {
    // Fallback: use full range if filter eliminated everything
    return {
      n_combinations: 0,
      results: [],
      best: null,
      multiple_testing: { summary: { raw_significant: 0 } },
      deflated_sharpe: {
        dsr: 0.0,
        interpretation: "No combos with R:R >= " + minRR,
      },
    };
  }

  const raw = (await nobleTraderPost(
    "/renko/backtest/optimize",
    {
      prices,
      symbol,
      brick_sizes: brickSizes,
      sl_bricks_range: filteredSl,
      tp_bricks_range: filteredTp,
      min_rr: minRR, // Server-side R:R filter (new field)
      commission_bps: 5,
      slippage_bps: 2,
      spread_bps: 1,
      confidence_level: 0.95,
      multiple_testing_alpha: 0.05,
    },
    90000,
  )) as OptimizeResult; // 90s timeout — optimize is the bottleneck, runs in parallel with regime/kelly

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
  return nobleTraderPost(
    "/regime/detect",
    {
      prices,
      symbol,
      window: 200,
      kelly_fraction: 0.5,
      target_vol: 0.15,
      base_risk_limit: 0.02,
    },
    20000,
  ) as Promise<RegimeResult>; // 20s (down from 30s — parallel with optimize)
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
  return nobleTraderPost(
    "/size/kelly",
    {
      prices,
      symbol,
      kelly_fraction: 0.5,
      target_vol: 0.15,
      use_regime: true,
    },
    20000,
  ) as Promise<KellyResult>; // 20s (down from 30s — parallel with optimize)
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
  const HL_MAINNET_URL =
    Deno.env.get("HL_API_URL") || "https://api.hyperliquid.xyz";

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
    throw new Error(
      `HyperLiquid candle fetch failed for ${coin}: ${res.status} — ${text.substring(0, 200)}`,
    );
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
          console.warn(
            `[yahoo] ${res.status} for ${ticker} (${tryInterval}) with UA ${ua.substring(0, 30)}... — trying next`,
          );
          lastError = new Error(
            `Yahoo Finance ${res.status} for ${ticker} (${tryInterval}) with UA ${ua.substring(0, 30)}...`,
          );
          // Small delay before trying next UA
          await sleep(500);
          continue;
        }

        if (!res.ok) {
          throw new Error(
            `Yahoo Finance fetch failed for ${ticker} (${tryInterval}): ${res.status}`,
          );
        }

        const data = await res.json();

        // Check for Yahoo API error
        if (data?.chart?.error) {
          throw new Error(
            `Yahoo API error for ${ticker} (${tryInterval}): ${data.chart.error.description || JSON.stringify(data.chart.error)}`,
          );
        }

        const quotes = data?.chart?.result?.[0]?.indicators?.quote?.[0];
        if (!quotes?.close) {
          throw new Error(
            `No close prices returned from Yahoo for ${ticker} (${tryInterval})`,
          );
        }

        const prices = quotes.close.filter(
          (p: number | null) => p !== null && !isNaN(p as number),
        );

        if (prices.length > 0) {
          console.log(
            `[yahoo] ${ticker}: Got ${prices.length} prices (${tryInterval}, UA=${ua.substring(0, 20)}...)`,
          );
          return prices;
        }

        lastError = new Error(
          `Yahoo returned 0 valid prices for ${ticker} (${tryInterval})`,
        );
      } catch (err) {
        lastError = err instanceof Error ? err : new Error(String(err));
      }
    } // end UA loop
  } // end interval loop

  throw (
    lastError ||
    new Error(
      `Yahoo Finance fetch failed for ${ticker} after all retries (UAs + intervals)`,
    )
  );
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
  AAPL: "AAPL",
  AMZN: "AMZN",
  GOOGL: "GOOGL",
  META: "META",
  MSFT: "MSFT",
  NVDA: "NVDA",
  TSLA: "TSLA",
  SPY: "SPY",
  QQQ: "QQQ",
};

function isAlpacaCrypto(alpacaSymbol: string): boolean {
  return alpacaSymbol.includes("/"); // e.g. "BTC/USD"
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
    console.warn(
      `[alpaca] No API credentials set, falling back to Yahoo for ${symbol}`,
    );
    // Fall back to Yahoo — derive the yahoo ticker from the symbol
    const yahooTicker =
      symbol === "BTCUSD"
        ? "BTC-USD"
        : symbol === "ETHUSD"
          ? "ETH-USD"
          : symbol === "SOLUSD"
            ? "SOL-USD"
            : symbol;
    return fetchYahooCandles(
      yahooTicker,
      hours <= 1080 ? "45d" : "90d",
      interval,
    );
  }

  const headers: Record<string, string> = {
    "APCA-API-KEY-ID": apiKey,
    "APCA-API-SECRET-KEY": secretKey,
  };

  const startTime = new Date(Date.now() - hours * 3600000).toISOString();
  // Shift endTime 16 min back so Alpaca's free tier serves SIP data
  // (free tier blocks real-time SIP — data <15 min old returns 403)
  const endTime = new Date(Date.now() - 16 * 60 * 1000).toISOString();
  const timeframe =
    interval === "1h" ? "1Hour" : interval === "1d" ? "1Day" : "1Hour";

  try {
    if (isAlpacaCrypto(alpacaSymbol)) {
      // Crypto bars: /v1beta3/crypto/us/bars
      const url = `${ALPACA_DATA_URL}/v1beta3/crypto/us/bars?symbols=${encodeURIComponent(alpacaSymbol)}&timeframe=${timeframe}&start=${startTime}&end=${endTime}&limit=10000`;
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(
          `Alpaca crypto bars failed for ${alpacaSymbol}: ${res.status} — ${text.substring(0, 200)}`,
        );
      }
      const data = (await res.json()) as {
        bars?: Record<string, Array<{ c: number }>>;
      };
      const bars = data?.bars?.[alpacaSymbol];
      if (!bars || bars.length === 0) {
        throw new Error(`Alpaca returned no crypto bars for ${alpacaSymbol}`);
      }
      const prices = bars
        .map((b) => parseFloat(String(b.c)))
        .filter((p) => !isNaN(p));
      console.log(
        `[alpaca-crypto] ${alpacaSymbol}: Got ${prices.length} prices`,
      );
      return prices;
    } else {
      // Stock/ETF bars: /v2/stocks/{symbol}/bars
      const url = `${ALPACA_DATA_URL}/v2/stocks/${encodeURIComponent(alpacaSymbol)}/bars?timeframe=${timeframe}&start=${startTime}&end=${endTime}&limit=10000&adjustment=raw`;
      const res = await fetch(url, { headers });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(
          `Alpaca stock bars failed for ${alpacaSymbol}: ${res.status} — ${text.substring(0, 200)}`,
        );
      }
      const data = (await res.json()) as { bars?: Array<{ c: number }> };
      const bars = data?.bars;
      if (!bars || bars.length === 0) {
        throw new Error(`Alpaca returned no stock bars for ${alpacaSymbol}`);
      }
      const prices = bars
        .map((b) => parseFloat(String(b.c)))
        .filter((p) => !isNaN(p));
      console.log(
        `[alpaca-stock] ${alpacaSymbol}: Got ${prices.length} prices`,
      );
      return prices;
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.warn(`[alpaca] ${symbol}: ${msg} — falling back to Yahoo`);
    // Fall back to Yahoo
    const yahooTicker =
      symbol === "BTCUSD"
        ? "BTC-USD"
        : symbol === "ETHUSD"
          ? "ETH-USD"
          : symbol === "SOLUSD"
            ? "SOL-USD"
            : symbol;
    return fetchYahooCandles(
      yahooTicker,
      hours <= 1080 ? "45d" : "90d",
      interval,
    );
  }
}

// Fetch full history for sweep.
// BETA: Using 45d (1080 hourly bars) instead of 90d to reduce CPU load on
// Render's 0.5 CPU tier. The /optimize endpoint does HMM fitting + backtesting
// for each brick×SL×TP combo, which is CPU-bound. On 0.5 CPU this takes 60-90s
// for 45d; 90d would exceed Supabase's edge function timeout.
// UPGRADE PATH: When upgrading to ≥1 CPU on Render, change to 90d/2160h for
// more statistical significance.
const SWEEP_CANDLE_HOURS = 2160; // 90 days × 24h
const SWEEP_YAHOO_RANGE = "90d";

export async function fetchSweepCandles(
  symbolConfig: SymbolConfig,
): Promise<number[]> {
  if (symbolConfig.data_source === "hyperliquid") {
    return fetchHyperLiquidCandles(
      symbolConfig.hyperliquid_coin!,
      SWEEP_CANDLE_HOURS,
      "1h",
    );
  } else if (symbolConfig.data_source === "alpaca") {
    const alpacaSymbol =
      symbolConfig.alpaca_symbol ||
      ALPACA_SYMBOL_MAP[symbolConfig.symbol] ||
      symbolConfig.symbol;
    return fetchAlpacaCandles(
      symbolConfig.symbol,
      alpacaSymbol,
      SWEEP_CANDLE_HOURS,
      "1h",
    );
  } else {
    return fetchYahooCandles(
      symbolConfig.yahoo_ticker!,
      SWEEP_YAHOO_RANGE,
      "1h",
    );
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
    const alpacaSymbol =
      symbolConfig.alpaca_symbol ||
      ALPACA_SYMBOL_MAP[symbolConfig.symbol] ||
      symbolConfig.symbol;
    return fetchAlpacaCandles(
      symbolConfig.symbol,
      alpacaSymbol,
      Math.min(nBars, 500),
      "1h",
    );
  } else {
    // Try hourly first (more granular for regime detection)
    const range = nBars <= 390 ? "5d" : "90d";
    let prices = await fetchYahooCandles(
      symbolConfig.yahoo_ticker!,
      range,
      "1h",
    );

    // Noble Trader /regime/detect requires >= 81 prices.
    // Fallback to daily interval if hourly returned insufficient data
    // (common outside market hours for stocks/ETFs/commodities)
    if (prices.length < 100) {
      console.log(
        `[fetchLatestCandles] ${symbolConfig.symbol}: hourly returned ${prices.length} bars (need >=81 for regime), falling back to daily`,
      );
      prices = await fetchYahooCandles(symbolConfig.yahoo_ticker!, "1y", "1d");
    }

    // Final safety: if daily also failed, try 2y daily for more history
    if (prices.length < 100) {
      console.log(
        `[fetchLatestCandles] ${symbolConfig.symbol}: daily 1y returned ${prices.length} bars, trying 2y daily`,
      );
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
  const volPct = (atr / price) * 100; // e.g. BTC ~2.7% daily vol
  let baseBrickPct: number;
  if (volPct > 3.0) {
    baseBrickPct = 0.75; // High vol (crypto, TSLA): 0.75% of price
  } else if (volPct > 1.5) {
    baseBrickPct = 0.6; // Medium vol (most stocks): 0.60% of price
  } else if (volPct > 0.5) {
    baseBrickPct = 0.4; // Low vol (indices, commodities): 0.40% of price
  } else {
    baseBrickPct = 0.25; // Forex: 0.25% of price
  }

  const base = (price * baseBrickPct) / 100;
  // 5 brick sizes — TimesFM gates ~70% of heavy sweeps, so the CPU budget
  // is available for a wider grid (120 combos vs 72). The extra 0.75× and
  // 3.0× bricks capture fine-grain and coarse-grain Renko regimes that
  // the middle 3 bricks miss.
  const bricks = [
    Math.round(base * 0.75 * 10000) / 10000,
    Math.round(base * 1.0 * 10000) / 10000,
    Math.round(base * 1.5 * 10000) / 10000,
    Math.round(base * 2.0 * 10000) / 10000,
    Math.round(base * 3.0 * 10000) / 10000,
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
export function computeMarkovProbs(regimeLabel: string): {
  pUp: number;
  pDn: number;
} {
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
  trendProbs: Record<string, number>; // e.g. { strong_bull: 0.10, bull: 0.65, bear: 0.15, strong_bear: 0.05, neutral: 0.05 }
  // From L2 imbalance engine (optional — 0.50 default if missing)
  imbalanceSignal?: {
    signal: "long" | "short" | "flat";
    confidence: number; // 0-1
    persistence: number; // bars
  };
  // From Markov transition matrix (optional — 0.50 default if missing)
  markovTransition?: {
    current_state: "UP" | "DOWN" | "FLAT";
    p_up: number; // P(UP | current_state)
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
  p_timesfm: number; // v3: TimesFM component (0 if unused)
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
    if (bullKeys.some((k) => lower.includes(k))) pBull += val;
    else if (bearKeys.some((k) => lower.includes(k))) pBear += val;
    else if (neutralKeys.some((k) => lower.includes(k))) pNeutral += val;
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
  if (!imbalanceSignal) return 0.5; // No data → neutral

  const minPersistenceTarget = 5;

  if (imbalanceSignal.signal === direction) {
    const persistenceBonus = Math.min(
      1.0,
      imbalanceSignal.persistence / minPersistenceTarget,
    );
    return imbalanceSignal.confidence * (0.6 + 0.4 * persistenceBonus);
  } else if (imbalanceSignal.signal === "flat") {
    return 0.5;
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
  if (!markovTransition) return 0.5; // No data → neutral

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
    pWin = 0.35 * pRegime + 0.2 * pImbalance + 0.2 * pMarkov + 0.25 * pTimesfm;
  } else {
    pWin = 0.4 * pRegime + 0.3 * pImbalance + 0.3 * pMarkov;
  }

  // EV computation
  const profit = input.tpBricks * input.brickSize;
  const stake = input.slBricks * input.brickSize;
  const pLoss = 1 - pWin;
  const ev = pWin * profit - pLoss * stake;
  const evPerDollar = stake > 0 ? ev / stake : 0;
  const rr = stake > 0 ? profit / stake : 0;

  // Aggression dispatcher
  let aggression: "passive" | "mid" | "aggressive";
  if (evPerDollar > 0.4) aggression = "passive";
  else if (evPerDollar > 0.15) aggression = "mid";
  else aggression = "aggressive";

  // EV-scaled Kelly
  const evScale = evPerDollar > 0 ? Math.min(1.0, evPerDollar / 0.5) : 0;
  const baseKelly = input.baseKelly ?? 0;
  const effectiveKelly = baseKelly * evScale;

  // Min EV gate
  const MIN_EV = 0.0; // Accept any positive EV
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
    return {
      current_state: "FLAT",
      p_up: 0.33,
      p_down: 0.33,
      p_flat: 0.34,
      n_observations: 0,
    };
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
    return {
      current_state: states[0] ?? "FLAT",
      p_up: 0.33,
      p_down: 0.33,
      p_flat: 0.34,
      n_observations: states.length,
    };
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
    return {
      current_state: currentState,
      p_up: 0.33,
      p_down: 0.33,
      p_flat: 0.34,
      n_observations: states.length - 1,
    };
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
  else if (result.full_kelly_f > 0)
    f = result.full_kelly_f * 0.5; // Never use full Kelly raw
  else return 0.1; // fallback minimum
  return Math.min(f, KELLY_CAP);
}

// ─── Minimum SL Distance (per-symbol) ───────────────────────
// Prevents stop-losses so tight they get hit by normal market noise.
// Expressed as minimum % distance from entry price.
const MIN_SL_PCT: Record<string, number> = {
  // Crypto: higher noise, need wider stops
  BTC: 0.5, // ~$320 at $64k — above 1-min noise
  ETH: 0.5, // ~$8.50 at $1,700
  SOL: 0.8,
  AVAX: 0.8,
  ARB: 1.0,
  DOGE: 1.0,
  LINK: 0.8,
  XRP: 0.8,
  WIF: 1.0,
  // Stocks: moderate noise
  AAPL: 0.3,
  AMZN: 0.3,
  GOOGL: 0.3,
  META: 0.3,
  MSFT: 0.3,
  NVDA: 0.5,
  TSLA: 0.5,
  SPY: 0.3,
  QQQ: 0.3,
  // Forex: tight spreads but still need room
  EURUSD: 0.3,
  GBPUSD: 0.3,
  USDJPY: 0.3,
  AUDUSD: 0.3,
  USDCAD: 0.3,
  // Commodities
  GOLD: 0.3,
  SILVER: 0.3,
  OIL: 0.5,
  COPPER: 0.5,
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
): {
  signal: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
} {
  const lower = regime.toLowerCase();
  const isBullish = lower.includes("bull");
  const isBearish = lower.includes("bear");

  let slDistance = slBricks * brickSize;
  const tpDistance = tpBricks * brickSize;

  // ── Minimum SL distance guard ──
  // If SL is too tight (< MIN_SL_PCT of price), widen it
  const minSlPct = MIN_SL_PCT[symbol] ?? DEFAULT_MIN_SL_PCT;
  const minSlDistance = (currentPrice * minSlPct) / 100;
  if (slDistance < minSlDistance) {
    console.warn(
      `[computeTradeSignal] ${symbol}: SL too tight (${((slDistance / currentPrice) * 100).toFixed(3)}% < ${minSlPct}%), ` +
        `widening from ${slBricks} bricks ($${slDistance.toFixed(2)}) to $${minSlDistance.toFixed(2)} (${Math.ceil(minSlDistance / brickSize)} bricks)`,
    );
    slDistance = minSlDistance;
  }

  const entry = roundPrice(currentPrice, symbol, assetClass);

  if (isBullish) {
    return {
      signal: "buy",
      entry_price: entry,
      stop_loss: roundPrice(
        Math.max(entry * 0.01, entry - slDistance),
        symbol,
        assetClass,
      ), // Floor at 1% of entry, never $0
      take_profit: roundPrice(entry + tpDistance, symbol, assetClass),
    };
  } else if (isBearish) {
    return {
      signal: "sell",
      entry_price: entry,
      stop_loss: roundPrice(entry + slDistance, symbol, assetClass),
      take_profit: roundPrice(
        Math.max(entry * 0.01, entry - tpDistance),
        symbol,
        assetClass,
      ), // Floor at 1% of entry, never $0
    };
  } else {
    // Neutral / unknown / market_closed — still compute entry for reference
    return {
      signal: "neutral",
      entry_price: entry,
      stop_loss: roundPrice(
        Math.max(entry * 0.01, entry - slDistance),
        symbol,
        assetClass,
      ),
      take_profit: roundPrice(entry + tpDistance, symbol, assetClass),
    };
  }
}

// ─── NaN-Safe Parsers ────────────────────────────────────────
// "NaN" is a truthy string in JS, so parseFloat("NaN") returns NaN
// but the truthiness check doesn't catch it. These helpers prevent
// the NaN feedback loop where NaN gets read from Redis, stays NaN,
// and gets written back forever.

export function safeParseFloat(
  val: string | undefined | null,
  fallback: number,
): number {
  if (!val) return fallback;
  const n = parseFloat(val);
  return Number.isNaN(n) ? fallback : n;
}

export function safeParseInt(
  val: string | undefined | null,
  fallback: number,
): number {
  if (!val) return fallback;
  const n = parseInt(val, 10);
  return Number.isNaN(n) ? fallback : n;
}

// ─── Lead/Lag: Correlation Matrix ───────────────────────────
// Computes rolling Pearson correlation between symbols from
// recent price returns. Used to adjust TimesFM forecasts for
// correlated leaders (e.g. BTC → ETH, SPY → QQQ).
const CORRELATION_WINDOW = 20; // bars

export interface CorrelationResult {
  matrix: Record<string, Record<string, number>>; // sym → sym → r
  leaders: Record<string, string[]>; // sym → [leader, ...]
}

/**
 * Compute rolling Pearson correlation matrix from price histories.
 * Only symbols with >= CORRELATION_WINDOW bars are included.
 * Returns empty matrix on insufficient data (fail-open).
 */
export function computeCorrelationMatrix(
  pricesBySymbol: Record<string, number[]>,
): CorrelationResult {
  const matrix: Record<string, Record<string, number>> = {};
  const leaders: Record<string, string[]> = {};
  const symbols = Object.keys(pricesBySymbol).filter(
    (s) => (pricesBySymbol[s]?.length ?? 0) >= CORRELATION_WINDOW,
  );

  if (symbols.length < 2) return { matrix, leaders };

  // Compute log-returns for each symbol
  const returns: Record<string, number[]> = {};
  for (const sym of symbols) {
    const prices = pricesBySymbol[sym]!;
    const r: number[] = [];
    for (
      let i = prices.length - CORRELATION_WINDOW + 1;
      i < prices.length;
      i++
    ) {
      if (prices[i - 1] > 0) r.push(Math.log(prices[i] / prices[i - 1]));
    }
    if (r.length >= CORRELATION_WINDOW - 1) returns[sym] = r;
  }

  const validSyms = Object.keys(returns);
  if (validSyms.length < 2) return { matrix, leaders };

  // Pearson correlation: r = sum((x-mx)(y-my)) / sqrt(sum((x-mx)²) * sum((y-my)²))
  for (const a of validSyms) {
    matrix[a] = {};
    const ra = returns[a]!;
    const meanA = ra.reduce((s, v) => s + v, 0) / ra.length;
    for (const b of validSyms) {
      if (a === b) {
        matrix[a][b] = 1;
        continue;
      }
      const rb = returns[b]!;
      const meanB = rb.reduce((s, v) => s + v, 0) / rb.length;
      let cov = 0,
        varA = 0,
        varB = 0;
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

  // For each symbol, find top-2 positively correlated symbols as "leaders"
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

/**
 * Adjust a symbol's TimesFM forecast based on correlated leaders.
 *
 * adjusted = self_weight × own_p_timesfm + (1 - self_weight) × avg(leader_p_timesfm)
 *
 * If no leaders or leader forecasts, returns own_p_timesfm unchanged.
 */
export function adjustLeadLag(
  symbol: string,
  pTimesfmMap: Map<string, number | null>,
  leaders: Record<string, string[]>,
  selfWeight = 0.7,
): number | null {
  const own = pTimesfmMap.get(symbol) ?? null;
  if (own === null) return null;

  const leaderSyms = leaders[symbol] ?? [];
  if (leaderSyms.length === 0) return own;

  let sum = 0;
  let count = 0;
  for (const ls of leaderSyms) {
    const lp = pTimesfmMap.get(ls);
    if (lp !== null) {
      sum += lp;
      count++;
    }
  }

  if (count === 0) return own;
  const leaderAvg = sum / count;
  return selfWeight * own + (1 - selfWeight) * leaderAvg;
}

// ─── Fat-Tail Risk: Tail Risk Score ─────────────────────────
// Converts extreme TimesFM forecasts into a risk-reduction signal.
// High tail risk = extreme p_timesfm (<0.2 or >0.8) + high_vol regime.
export interface TailRiskResult {
  score: number; // 0.0 (none) – 1.0 (critical)
  action: string; // "none" | "reduce_50" | "skip"
  multiplier: number; // position size multiplier (1.0 = no change)
}

export function computeTailRisk(
  pTimesfm: number | null,
  regime: string,
  timesfmConf: number,
): TailRiskResult {
  const result: TailRiskResult = { score: 0, action: "none", multiplier: 1.0 };

  if (pTimesfm === null || timesfmConf < 0.5) return result;

  const isExtreme = pTimesfm < 0.2 || pTimesfm > 0.8;
  const isHighVol = regime.includes("high_vol");
  const isExtremeConfident = isExtreme && timesfmConf >= 0.7;

  if (isExtremeConfident && isHighVol) {
    // TimesFM strongly predicts a move in a high-vol regime → tail event risk
    result.score = 0.85;
    result.action = "skip";
    result.multiplier = 0.0;
  } else if (isExtremeConfident) {
    // Extreme forecast in normal vol → reduce
    result.score = 0.6;
    result.action = "reduce_50";
    result.multiplier = 0.5;
  } else if (isExtreme && isHighVol) {
    // High vol + extreme but low confidence → mild reduction
    result.score = 0.35;
    result.action = "reduce_25";
    result.multiplier = 0.75;
  }

  return result;
}

// ─── Volatility Shift Detection ────────────────────────────
// Returns true if a regime change involves a volatility transition
// (e.g., low_vol → high_vol or vice versa). Used for Shift-Aware
// Wakeups — trigger emergency heavy sweep on vol shifts.
export function isVolatilityShift(
  prevRegime: string,
  currentRegime: string,
): boolean {
  if (!prevRegime || !currentRegime) return false;
  const prevHigh = prevRegime.includes("high_vol");
  const currHigh = currentRegime.includes("high_vol");
  return prevHigh !== currHigh;
}

// ─── Utility ────────────────────────────────────────────────
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
