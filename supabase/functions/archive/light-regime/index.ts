// ─── Light Regime Edge Function (v2.2 — Regime Cache) ──────────
// Fast HMM regime detection + Kelly update per symbol.
// Fetches latest candles → detects regime → computes Kelly
// → updates Redis config (regime fields only) + heartbeat + log
//
// v2.2 Changes (Jun 11 2026):
//   - Uses single regime_cache key (1 GET) instead of per-symbol HGETALL (N reads)
//   - Always updates price-dependent fields (entry/SL/TP) every cycle via targeted HSET
//   - Full config write + cache save only on regime shift
//   - ~90% reduction in Redis reads per cycle
//
// Trigger: pg_cron via nt_invoke_edge_function('light-regime', {asset_class})
// Or direct: POST with { symbol: "BTC" } for single-symbol update
//            POST with { symbols: ["BTC","ETH","SOL"] } for batch update
//            POST with { warmup: true } to pre-boot the worker
//
// Runs every 15 minutes (reduced from 5 min — regimes don't shift that fast).

import {
  corsHeaders,
  getSymbolsByAssetClass,
  getSymbolByName,
  fetchLatestCandles,
  detectRegime,
  computeKelly,
  redisSetConfig,
  redisGetConfig,
  redisUpdateSignal,
  redisLoadRegimeCache,
  redisSaveRegimeCache,
  redisPushError,
  logRegimeEntry,
  logError,
  isMarketOpen,
  parseRegimeLabel,
  computeMarkovProbs,
  parseKellyF,
  computeTradeSignal,
  computeEV,
  computeMarkovTransition,
  countShifts24h,
  safeParseFloat,
  safeParseInt,
  sleep,
  PRICE_TICK_DECIMALS,
  type SymbolConfig,
  type RedisConfig,
  type RegimeResult,
  type KellyResult,
  type RegimeCache,
  type RegimeCacheEntry,
  type EVResult,
  type MarkovTransitionResult,
} from "../_shared/utils.ts";

async function updateRegimeForSymbol(
  symbolConfig: SymbolConfig,
  cacheEntry: RegimeCacheEntry | undefined,
): Promise<{ regime: string; regimeShift: boolean; entry: RegimeCacheEntry | null; error?: string }> {
  try {
    const marketOpen = isMarketOpen(symbolConfig.asset_class);

    // 1. Fetch latest candles
    let prices: number[] = [];
    try {
      prices = await fetchLatestCandles(symbolConfig, 200);
    } catch (err) {
      console.warn(`[light-regime] ${symbolConfig.symbol} candle fetch failed: ${err}`);
    }

    if (prices.length >= 81) {
      // 2. Detect regime
      console.log(`[light-regime] ${symbolConfig.symbol}: Detecting regime (${prices.length} bars)...`);
      const regimeResult: RegimeResult = await detectRegime(prices, symbolConfig.symbol);
      const combinedRegime = parseRegimeLabel(regimeResult);
      const regimeConf = regimeResult.confidence ?? 0;

      // 3. Compute Kelly
      let kellyF = cacheEntry?.kelly_f ?? 0.1;
      try {
        const kellyResult: KellyResult = await computeKelly(prices, symbolConfig.symbol);
        kellyF = parseKellyF(kellyResult);
      } catch (err) {
        console.warn(`[light-regime] ${symbolConfig.symbol} Kelly failed: ${err}`);
      }

      // 4. Detect regime shift from cache entry (no HGETALL needed)
      //    GUARD: If detectRegime returned "unknown" (API timeout / incomplete data),
      //    do NOT overwrite a valid previous regime. Instead, fall back to the
      //    previous regime for all downstream computations (EV, signal, Markov).
      //    We still update price-dependent fields so they don't go stale.
      const prevRegime = cacheEntry?.regime ?? null;
      let effectiveRegime = combinedRegime;
      let effectiveRegimeConf = regimeConf;
      let regimeDegraded = false;
      if (combinedRegime === "unknown" && prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed") {
        console.warn(`[light-regime] ${symbolConfig.symbol}: Regime detection returned "unknown" — falling back to previous regime "${prevRegime}" for downstream computations`);
        effectiveRegime = prevRegime;
        effectiveRegimeConf = cacheEntry?.regime_conf ?? 0;
        regimeDegraded = true;
      }
      const regimeShift = !regimeDegraded && prevRegime !== null && prevRegime !== "unknown" && prevRegime !== "market_closed" && prevRegime !== combinedRegime;

      if (regimeShift) {
        console.log(`[light-regime] ${symbolConfig.symbol}: REGIME SHIFT ${prevRegime} → ${combinedRegime}`);
      }

      // 5. Compute Markov probabilities
      const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(effectiveRegime);
      const riskMultiplier = regimeResult.risk_multiplier ?? 1.0;

      // 6. Load brick params — prefer cache, but fall back to Redis config
      //    on cold start. This preserves the heavy-sweep's optimized values
      //    which are written to trading:config:{symbol} but NOT to regime cache.
      let brickSize = cacheEntry?.brick_size ?? NaN;
      let slBricks  = (cacheEntry?.sl_bricks as number) ?? NaN;
      let tpBricks  = (cacheEntry?.tp_bricks as number) ?? NaN;
      // If cache is cold (NaN), read from the full Redis config (set by heavy-sweep)
      if (isNaN(brickSize) || isNaN(slBricks) || isNaN(tpBricks)) {
        try {
          const fullCfg = await redisGetConfig(symbolConfig.symbol);
          if (fullCfg) {
            brickSize = safeParseFloat(String(fullCfg.brick_size ?? ""), 0.5);
            slBricks  = safeParseInt(String(fullCfg.sl_bricks ?? ""), 3);
            tpBricks  = safeParseInt(String(fullCfg.tp_bricks ?? ""), 5);
            console.log(`[light-regime] ${symbolConfig.symbol}: cold cache — loaded brick params from Redis config (brick=${brickSize}, sl=${slBricks}, tp=${tpBricks})`);
          } else {
            brickSize = 0.5; slBricks = 3; tpBricks = 5;
          }
        } catch {
          brickSize = 0.5; slBricks = 3; tpBricks = 5;
        }
      }

      // 6b. Compute real EV with blended P_win
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
      });

      const effectiveKelly = evResult.effective_kelly > 0 ? Math.min(evResult.effective_kelly, 0.5) : kellyF;

      console.log(
        `[light-regime] ${symbolConfig.symbol}: EV=${evResult.ev.toFixed(2)} ($${evResult.ev_per_dollar.toFixed(3)}/$risk), ` +
        `P_win=${evResult.p_win.toFixed(3)}, aggression=${evResult.aggression}, effective_kelly=${effectiveKelly.toFixed(3)}`
      );

      // 6c. Compute shifts_24h (volatility meta-signal)
      //   - On shift: fresh count from nt_regime_log (expensive PostgREST query)
      //   - No shift: carry forward from cache (set by heavy-sweep or previous shift)
      //   - This ensures shifts_24h is ALWAYS written to Redis, even when no shift
      let shifts24h = cacheEntry?.shifts_24h ?? 0;
      if (regimeShift) {
        shifts24h = await countShifts24h(symbolConfig.symbol);
        console.log(`[light-regime] ${symbolConfig.symbol}: shifts_24h=${shifts24h} (fresh count)`);
      }

      // 7. Compute trade signal from current price + brick params
      const currentPrice = prices[prices.length - 1];
      const tradeSignal = computeTradeSignal(effectiveRegime, currentPrice, brickSize, slBricks, tpBricks, symbolConfig.symbol, symbolConfig.asset_class);

      // 7. Always update price-dependent signal fields (even without regime shift)
      // Use price_decimals from cache (set by heavy-sweep) or from PRICE_TICK_DECIMALS map
      const decimals = cacheEntry?.price_decimals
        ?? PRICE_TICK_DECIMALS[symbolConfig.symbol]
        ?? (symbolConfig.symbol === "BTC" ? 1 : 2); // tick precision
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
          // Always write ALL 4 shift fields so the bot never sees stale/missing data.
          // On shift: fresh prev_regime + new timestamp + fresh shifts_24h.
          // No shift: carry forward from cache (or sensible defaults for cold start).
          regime_shift: regimeShift ? "true" : "false",
          prev_regime: prevRegime ?? "",
          shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
          shifts_24h: shifts24h,
        },
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
        // EV Engine fields
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
        // Regime Shift fields (always populated)
        regime_shift: regimeShift ? "true" : "false",
        prev_regime: prevRegime ?? "",
        shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
        shifts_24h: shifts24h,
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
          sweep_timestamp: Date.now(),  // Light-regime also refreshes sweep_timestamp
          signal: tradeSignal.signal,
          entry_price: tradeSignal.entry_price,
          stop_loss: tradeSignal.stop_loss,
          take_profit: tradeSignal.take_profit,
          // EV Engine fields
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
          // Regime Shift fields (always populated)
          regime_shift: regimeShift ? "true" : "false",
          prev_regime: prevRegime ?? "",
          shift_at: regimeShift ? Date.now() : (cacheEntry?.shift_at ?? 0),
          shifts_24h: shifts24h,
        };
        await redisSetConfig(symbolConfig.symbol, redisConfig);

        await logRegimeEntry({
          symbol: symbolConfig.symbol,
          source: regimeDegraded ? "light-regime-degraded" : "light-regime",
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
        `[light-regime] ${symbolConfig.symbol}: regime=${effectiveRegime}${regimeDegraded ? " (degraded)" : ""}, conf=${effectiveRegimeConf.toFixed(2)}, ` +
        `kelly=${kellyF.toFixed(3)}, risk_mult=${riskMultiplier}, shift=${regimeShift}`,
      );

      return { regime: effectiveRegime, regimeShift, entry: newEntry };

    } else {
      // No data available
      if (!cacheEntry) {
        console.log(`[light-regime] ${symbolConfig.symbol}: Initial market_closed write`);
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
        console.log(`[light-regime] ${symbolConfig.symbol}: market_closed — FILTERED (already exists)`);
      }
      return { regime: "market_closed", regimeShift: false, entry: null };
    }

  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[light-regime] ${symbolConfig.symbol} FAILED: ${errMsg}`);

    // Minimal error config write
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
    } catch {
      // Best effort
    }

    await redisPushError(symbolConfig.symbol, "light-regime", errMsg);
    await logError(symbolConfig.symbol, "light-regime", errMsg);
    return { regime: "error", regimeShift: false, entry: null, error: errMsg };
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
    `[light-regime] FATAL: Missing required env vars: ${MISSING_ENV_VARS.join(", ")}. ` +
    `Set them in Supabase Dashboard → Edge Functions → Secrets.`,
  );
}

// ─── Background Processor ────────────────────────────────────
// Processes symbols in the background after returning 202 to caller.
// This is critical because pg_net has a 5-second HTTP timeout, but
// processing 3 symbols takes 30-120s. If we don't return fast,
// pg_net kills the connection and Supabase aborts the edge function.
async function processSymbolsBackground(symbolsToProcess: SymbolConfig[]) {
  try {
    // ── Load regime cache (1 GET instead of N HGETALL) ──
    const regimeCache = await redisLoadRegimeCache();

    // ── Global deadline ──
    // Supabase kills us at ~150s. Leave 40s buffer for Redis writes.
    const DEADLINE_MS = 110_000;
    const PER_SYMBOL_DEADLINE_MS = 40_000;
    const startTime = Date.now();
    const RATE_LIMIT_DELAY_MS = 2000;
    let cacheChanged = false;

    for (let i = 0; i < symbolsToProcess.length; i++) {
      const elapsed = Date.now() - startTime;
      if (elapsed > DEADLINE_MS) {
        console.warn(`[light-regime] Deadline exceeded (${elapsed}ms). Skipping remaining ${symbolsToProcess.length - i} symbols.`);
        break;
      }

      const sym = symbolsToProcess[i];
      const cacheEntry = regimeCache[sym.symbol];
      console.log(`[light-regime] Processing ${sym.symbol} (${i + 1}/${symbolsToProcess.length}, cache=${cacheEntry ? "hit" : "miss"}, ${elapsed}ms elapsed)...`);

      // Race: process symbol vs per-symbol deadline
      const symbolResult = await Promise.race([
        updateRegimeForSymbol(sym, cacheEntry),
        sleep(PER_SYMBOL_DEADLINE_MS).then(() => ({
          regime: "skipped_timeout" as const,
          regimeShift: false,
          entry: null as RegimeCacheEntry | null,
          error: `Per-symbol deadline exceeded (${PER_SYMBOL_DEADLINE_MS}ms)`,
        })),
      ]);

      // Update cache if we got a new entry back
      if (symbolResult.entry) {
        regimeCache[sym.symbol] = symbolResult.entry;
        if (symbolResult.regimeShift) cacheChanged = true;
      }

      if (i < symbolsToProcess.length - 1) {
        await sleep(RATE_LIMIT_DELAY_MS);
      }
    }

    // ── Save regime cache only if anything changed ──
    if (cacheChanged) {
      await redisSaveRegimeCache(regimeCache);
      console.log(`[light-regime] Regime cache saved (1 SET)`);
    } else {
      console.log(`[light-regime] Regime cache unchanged — no write needed`);
    }
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[light-regime] Background processing error: ${errMsg}`);
  }
}

// ─── Main Handler ───────────────────────────────────────────
Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders() });
  }

  let body: Record<string, unknown>;
  try { body = await req.json(); } catch { body = {}; }

  const { asset_class, symbol, symbols, warmup } = body as {
    asset_class?: string;
    symbol?: string;
    symbols?: string[];
    warmup?: boolean;
  };

  if (warmup === true) {
    console.log("[light-regime] Warmup ping received — worker booted successfully");
    return new Response(
      JSON.stringify({ status: "warmup", message: "Worker booted successfully", timestamp: Date.now() }),
      { status: 200, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  if (MISSING_ENV_VARS.length > 0) {
    return new Response(
      JSON.stringify({ error: `Missing required env vars: ${MISSING_ENV_VARS.join(", ")}` }),
      { status: 503, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  try {
    // ── Resolve symbols ──
    let symbolsToProcess: SymbolConfig[];
    const isBatchSymbols = Array.isArray(symbols) && symbols.length > 0;

    if (symbol) {
      const found = await getSymbolByName(symbol);
      if (!found) return new Response(JSON.stringify({ error: `Symbol ${symbol} not found` }), { status: 404, headers: { ...corsHeaders(), "Content-Type": "application/json" } });
      symbolsToProcess = [found];
    } else if (isBatchSymbols) {
      console.log(`[light-regime] Batch symbols request: [${symbols!.join(", ")}]`);
      const found: SymbolConfig[] = [];
      for (const sym of symbols!) {
        const s = await getSymbolByName(sym);
        if (s) found.push(s);
        else console.warn(`[light-regime] Symbol ${sym} not found, skipping`);
      }
      if (found.length === 0) return new Response(JSON.stringify({ error: "No valid symbols found" }), { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } });
      symbolsToProcess = found;
    } else if (asset_class) {
      symbolsToProcess = await getSymbolsByAssetClass(asset_class);
    } else {
      return new Response(JSON.stringify({ error: "Provide 'symbol', 'symbols', or 'asset_class'" }), { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } });
    }

    console.log(`[light-regime] Queued ${symbolsToProcess.length} symbols for ${asset_class || symbol || symbols!.join(",")}`);

    // ── Return 202 Accepted immediately, process in background ──
    // pg_net has a 5-second HTTP timeout. If we process inline and
    // take 30-120s, pg_net kills the connection and Supabase aborts
    // the function. By returning 202 immediately, pg_net is happy
    // and the Deno worker stays alive to finish processing.
    const symbolNames = symbolsToProcess.map(s => s.symbol);
    // Schedule background work via setTimeout(0) — this puts it on the
    // event loop AFTER the response is sent, but keeps the isolate alive
    // because Deno.serve() runs the event loop continuously.
    setTimeout(() => processSymbolsBackground(symbolsToProcess), 0);

    return new Response(
      JSON.stringify({
        status: "accepted",
        message: `Processing ${symbolsToProcess.length} symbols in background`,
        symbols: symbolNames,
        timestamp: Date.now(),
      }),
      { status: 202, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );

  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[light-regime] Fatal: ${errMsg}`);
    return new Response(JSON.stringify({ error: errMsg }), { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } });
  }
});
