// ─── Heavy Sweep Edge Function (v4 — Parallel + Fallback) ──────
// Full parameter sweep per symbol: fetch candles → ATR-based brick sizes
// → PARALLEL /optimize + /regime/detect + /size/kelly → rank by Sharpe
// → Kelly → write Golden State to Redis + log to Supabase
//
// v4 Changes:
//   - Runs optimize + regime + Kelly IN PARALLEL (was sequential → 546 timeout)
//   - Graceful fallback: if optimize times out but regime/kelly succeed,
//     writes regime + signal with cached brick params (no more stale timestamps)
//   - BETA: 45d window (1080 bars) + 3 brick sizes to fit 0.5 CPU Render tier
//     UPGRADE PATH: When upgrading to ≥1 CPU, increase to 90d/2160h + 5 bricks
//   - Uses /renko/backtest/optimize (1 API call) instead of
//     /renko/backtest/run (60 API calls) — massive Render memory relief
//   - R:R >= 2.0 pre-filter + Sharpe > 10 overfit guard
//   - Kelly capped at 0.5 (half-Kelly max)
//
// Trigger: pg_cron via nt_invoke_edge_function('heavy-sweep', {asset_class})
// Or direct: POST with { symbol: "BTC" } for single-symbol sweep
// Or batch: POST with { symbols: ["BTC", "ETH", "SOL"] } for multi-symbol
// Or warmup: POST with { warmup: true } to pre-boot the worker

import {
  corsHeaders,
  getSymbolsByAssetClass,
  getSymbolByName,
  getAllActiveSymbols,
  supabaseRest,
  fetchSweepCandles,
  computeATR,
  generateBrickSizes,
  runRenkoOptimize,
  detectRegime,
  computeKelly,
  redisSetConfig,
  redisGetConfig,
  redisLoadRegimeCache,
  redisSaveRegimeCache,
  redisSetHeartbeat,
  redisPushError,
  logSweepResult,
  logRegimeEntry,
  logError,
  sleep,
  parseRegimeLabel,
  computeMarkovProbs,
  parseKellyF,
  computeTradeSignal,
  computeEV,
  computeMarkovTransition,
  countShifts24h,
  safeParseFloat,
  safeParseInt,
  type SymbolConfig,
  type RedisConfig,
  type RegimeCache,
  type RegimeResult,
  type KellyResult,
  type OptimizeComboResult,
  type EVResult,
  type MarkovTransitionResult,
} from "../_shared/utils.ts";

// Grid ranges — sent to /optimize in a single call
const SWEEP_SL_RANGE = [2, 3, 4, 5];      // 4 SL levels (added 5 for wider stops)
const SWEEP_TP_RANGE = [4, 5, 6, 8, 10, 12]; // 6 TP levels (added 12 for runners)
const MIN_RR = 2.0;                         // Minimum risk-reward ratio (tp_bricks / sl_bricks)
const MAX_SHARPE = 10.0;                    // Sharpe above this is flagged as overfit/suspicious
const MIN_TRADES = 5;                       // Minimum trades for statistical significance

async function runSweepForSymbol(
  symbolConfig: SymbolConfig,
): Promise<{ best: OptimizeComboResult | null; regime: unknown; kelly: unknown; duration: number; nCombos: number; error?: string }> {
  const startTime = Date.now();

  try {
    // 1. Fetch candles
    console.log(`[heavy-sweep] Fetching candles for ${symbolConfig.symbol}...`);
    const prices = await fetchSweepCandles(symbolConfig);

    if (prices.length < 100) {
      throw new Error(`Insufficient candle data: ${prices.length} bars (need >= 100)`);
    }

    console.log(`[heavy-sweep] ${symbolConfig.symbol}: ${prices.length} candles loaded`);

    // 2. Compute ATR and generate brick sizes (now using professional 3×/2.5× standards)
    const atr = computeATR(prices, 14);
    const currentPrice = prices[prices.length - 1];
    const brickSizes = generateBrickSizes(atr, currentPrice);

    console.log(`[heavy-sweep] ${symbolConfig.symbol}: ATR=${atr.toFixed(4)}, price=${currentPrice.toFixed(2)}, bricks=[${brickSizes.map(b => b.toFixed(4)).join(",")}]`);

    // 3. Run optimize + regime + Kelly IN PARALLEL to fit within Supabase's 150s limit.
    //    Previous sequential approach: optimize(120s) + regime(30s) + kelly(30s) = 180s → 546 timeout.
    //    Parallel approach: max(optimize, regime, kelly) ≈ 60-80s → well within budget.
    console.log(`[heavy-sweep] ${symbolConfig.symbol}: Running optimize + regime + Kelly in parallel...`);
    const [optResultRaw, regimeResultRaw, kellyResultRaw] = await Promise.allSettled([
      runRenkoOptimize(prices, symbolConfig.symbol, brickSizes, SWEEP_SL_RANGE, SWEEP_TP_RANGE, MIN_RR),
      prices.length >= 81
        ? detectRegime(prices, symbolConfig.symbol)
        : Promise.reject(new Error(`Only ${prices.length} bars (need >=81)`)),
      computeKelly(prices, symbolConfig.symbol),
    ]);

    // ── Graceful fallback: if optimize timed out but regime/kelly succeeded,
    //    write regime + signal with CACHED brick params instead of failing entirely.
    //    This prevents sweep_timestamp from going stale on 0.5 CPU Render tier. ──
    const optimizeFailed = optResultRaw.status === "rejected";
    const regimeOk = regimeResultRaw.status === "fulfilled";
    const kellyOk = kellyResultRaw.status === "fulfilled";

    if (optimizeFailed) {
      const reason = optResultRaw.reason instanceof Error ? optResultRaw.reason.message : String(optResultRaw.reason);
      console.warn(`[heavy-sweep] ${symbolConfig.symbol}: Optimization failed: ${reason}`);

      // Load cached brick params from Redis config (set by last successful sweep)
      let cachedBrick = brickSizes[0]; // fallback to ATR-based default
      let cachedSl = 3;
      let cachedTp = 5;
      try {
        const cachedCfg = await redisGetConfig(symbolConfig.symbol);
        if (cachedCfg) {
          cachedBrick = safeParseFloat(cachedCfg.brick_size ?? "", brickSizes[0]);
          cachedSl = safeParseInt(cachedCfg.sl_bricks ?? "", 3);
          cachedTp = safeParseInt(cachedCfg.tp_bricks ?? "", 5);
          console.log(`[heavy-sweep] ${symbolConfig.symbol}: Using cached brick params (brick=${cachedBrick}, sl=${cachedSl}, tp=${cachedTp})`);
        }
      } catch { /* use defaults */ }

      // If regime also failed, we can't do much — throw
      if (!regimeOk) {
        throw new Error(`Optimize + regime both failed. Optimize: ${reason}, Regime: ${regimeResultRaw.status === "rejected" ? regimeResultRaw.reason : "n/a"}`);
      }

      // ── Fallback path: write regime + signal with cached brick params ──
      const regimeResult = regimeResultRaw.value;
      const combinedRegime = parseRegimeLabel(regimeResult);
      const regimeConf = regimeResult.confidence ?? 0;
      const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(combinedRegime);
      const kellyF = kellyOk ? parseKellyF(kellyResultRaw.value) : 0.1;
      const currentPrice = prices[prices.length - 1];
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

      console.log(`[heavy-sweep] ${symbolConfig.symbol}: FALLBACK — regime=${combinedRegime}, cached brick=${cachedBrick}, signal=${tradeSignal.signal}`);

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
        // Regime Shift fields (fallback path — carry forward from previous config)
        regime_shift: "false",
        prev_regime: "",
        shift_at: 0,
        shifts_24h: 0,
      };
      await redisSetConfig(symbolConfig.symbol, redisConfig);
      await redisSetHeartbeat(symbolConfig.symbol, symbolConfig.heartbeat_ttl_seconds);

      // Update regime cache
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
          regime_shift: "false",
          prev_regime: "",
          shift_at: 0,
          shifts_24h: 0,
        };
        await redisSaveRegimeCache(cache);
      } catch { /* best effort */ }

      await logRegimeEntry({
        symbol: symbolConfig.symbol, source: "heavy-sweep-fallback",
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

    const optResult = optResultRaw.value;

    const nCombos = optResult.n_combinations;
    console.log(`[heavy-sweep] ${symbolConfig.symbol}: Optimization complete — ${nCombos} combinations tested`);

    // 4. Rank results — apply R:R and Sharpe guards client-side
    const validResults = (optResult.results || []).filter((r: OptimizeComboResult) => {
      if (r.total_trades < MIN_TRADES) return false;
      // R:R guard
      const rr = r.tp_bricks / r.sl_bricks;
      if (rr < MIN_RR) return false;
      // Sharpe guard: reject suspiciously high Sharpe (overfit/bug)
      if (r.sharpe > MAX_SHARPE) {
        console.warn(`[heavy-sweep] ${symbolConfig.symbol}: Rejecting combo ${r.brick_size}/${r.sl_bricks}/${r.tp_bricks} — Sharpe ${r.sharpe.toFixed(2)} > ${MAX_SHARPE} (likely overfit)`);
        return false;
      }
      return true;
    });

    // Fallback: if strict filter eliminated everything, relax to any with trades
    let rankedResults = validResults;
    if (rankedResults.length === 0) {
      const anyTrades = (optResult.results || []).filter((r: OptimizeComboResult) =>
        r.total_trades > 0 && r.sharpe <= MAX_SHARPE
      );
      if (anyTrades.length === 0) {
        throw new Error(`No backtest combinations produced valid trades (R:R >= ${MIN_RR}, Sharpe <= ${MAX_SHARPE})`);
      }
      // Sort by Sharpe descending as fallback
      anyTrades.sort((a, b) => b.sharpe - a.sharpe);
      rankedResults = anyTrades;
      console.warn(`[heavy-sweep] ${symbolConfig.symbol}: Strict filter eliminated all results, using best available (R:R=${(rankedResults[0].tp_bricks / rankedResults[0].sl_bricks).toFixed(2)}x)`);
    }

    // Results are already sorted by Sharpe from /optimize, but re-sort after our filters
    rankedResults.sort((a, b) => b.sharpe - a.sharpe);
    const best = rankedResults[0];
    const bestRR = best.tp_bricks / best.sl_bricks;

    console.log(
      `[heavy-sweep] ${symbolConfig.symbol} best: brick=${best.brick_size}, sl=${best.sl_bricks}, ` +
      `tp=${best.tp_bricks}, R:R=${bestRR.toFixed(2)}x, sharpe=${best.sharpe.toFixed(2)}, trades=${best.total_trades}`
    );

    // Log DSR if available
    const dsr = optResult.deflated_sharpe as Record<string, unknown> | undefined;
    if (dsr && typeof dsr.dsr === "number") {
      console.log(`[heavy-sweep] ${symbolConfig.symbol}: DSR=${dsr.dsr.toFixed(4)} (${dsr.interpretation || ""})`);
    }

    // 5. Unpack regime result (ran in parallel with optimize)
    let combinedRegime = "unknown";
    let regimeConf = 0;
    let riskMultiplier = 1.0;
    let regimeResult: RegimeResult | null = null;
    if (regimeResultRaw.status === "fulfilled") {
      regimeResult = regimeResultRaw.value;
      combinedRegime = parseRegimeLabel(regimeResult);
      regimeConf = regimeResult.confidence ?? 0;
      riskMultiplier = regimeResult.risk_multiplier ?? 1.0;
    } else {
      console.warn(`[heavy-sweep] ${symbolConfig.symbol} regime detection failed: ${regimeResultRaw.reason}`);
    }

    // GUARD: If regime detection returned "unknown" (API timeout / incomplete data),
    // preserve the previous valid regime from Redis instead of overwriting with "unknown".
    if (combinedRegime === "unknown") {
      try {
        const prevCfg = await redisGetConfig(symbolConfig.symbol);
        const prevRegime = prevCfg?.regime;
        if (prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed") {
          console.warn(`[heavy-sweep] ${symbolConfig.symbol}: Regime detection returned "unknown" — preserving previous regime "${prevRegime}"`);
          combinedRegime = prevRegime;
          regimeConf = safeParseFloat(String(prevCfg?.regime_conf ?? ""), 0);
        }
      } catch { /* use "unknown" if Redis read fails */ }
    }

    // 6. Unpack Kelly result (ran in parallel with optimize)
    let kellyF = 0.1;
    if (kellyResultRaw.status === "fulfilled") {
      kellyF = parseKellyF(kellyResultRaw.value);
    } else {
      console.warn(`[heavy-sweep] ${symbolConfig.symbol} Kelly computation failed, using default: ${kellyResultRaw.reason}`);
    }

    // Compute Markov probabilities from regime label
    const { pUp: markovPUp, pDn: markovPDn } = computeMarkovProbs(combinedRegime);

    // 6b. Compute real EV with blended P_win
    // Determine direction from regime
    const direction: "long" | "short" = combinedRegime.toLowerCase().includes("bear") ? "short" : "long";

    // Build Markov transition matrix from prices
    const markovTransition = computeMarkovTransition(prices, best.brick_size);

    // Compute full EV
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
      // L2 imbalance not available in heavy-sweep yet — defaults to 0.50
    });

    // Override kelly_f with EV-scaled version
    const effectiveKelly = evResult.effective_kelly > 0 ? Math.min(evResult.effective_kelly, 0.5) : kellyF;

    console.log(
      `[heavy-sweep] ${symbolConfig.symbol}: EV=${evResult.ev.toFixed(2)} ($${evResult.ev_per_dollar.toFixed(3)}/$risk), ` +
      `P_win=${evResult.p_win.toFixed(3)} (regime=${evResult.p_regime.toFixed(3)}, markov=${evResult.p_markov.toFixed(3)}), ` +
      `aggression=${evResult.aggression}, effective_kelly=${effectiveKelly.toFixed(3)}`
    );

    const duration = Date.now() - startTime;

    // 7a. Detect regime shift by comparing new regime with previous Redis config
    let prevRegime: string | null = null;
    let prevShiftAt = 0;
    let regimeShift = false;
    try {
      const prevCfg = await redisGetConfig(symbolConfig.symbol);
      if (prevCfg) {
        prevRegime = prevCfg.regime ?? null;
        prevShiftAt = safeParseFloat(prevCfg.shift_at ?? "0", 0);
        // Detect shift: new regime differs from previous valid regime
        if (
          combinedRegime !== "unknown" && combinedRegime !== "market_closed" &&
          prevRegime && prevRegime !== "unknown" && prevRegime !== "market_closed" &&
          prevRegime !== combinedRegime
        ) {
          regimeShift = true;
          console.log(`[heavy-sweep] ${symbolConfig.symbol}: REGIME SHIFT ${prevRegime} → ${combinedRegime}`);
        }
      }
    } catch { /* best effort — treat as no shift */ }

    // 7b. Compute shifts_24h for volatility meta-signal
    const shifts24h = await countShifts24h(symbolConfig.symbol);
    console.log(`[heavy-sweep] ${symbolConfig.symbol}: shifts_24h=${shifts24h}, shift=${regimeShift}`);

    // 8. Compute trade signal (entry / SL / TP) — uses professional brick sizes
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
      `[heavy-sweep] ${symbolConfig.symbol}: Signal=${tradeSignal.signal}, Entry=${tradeSignal.entry_price.toFixed(2)}, ` +
      `SL=${tradeSignal.stop_loss.toFixed(2)}, TP=${tradeSignal.take_profit.toFixed(2)}`
    );

    // 8. Write to Redis
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
      sweep_window: "45d",  // BETA: 45d to fit 0.5 CPU Render tier. Upgrade to 90d at ≥1 CPU
      sweep_sharpe: best.sharpe,
      sweep_trades: best.total_trades,
      signal: tradeSignal.signal,
      entry_price: tradeSignal.entry_price,
      stop_loss: tradeSignal.stop_loss,
      take_profit: tradeSignal.take_profit,
      // NEW: EV Engine fields
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
      shift_at: regimeShift ? Date.now() : prevShiftAt,
      shifts_24h: shifts24h,
    };

    await redisSetConfig(symbolConfig.symbol, redisConfig);

    // Save to regime cache so light-regime picks up optimized brick params
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
        // NEW: EV Engine fields
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
        shift_at: regimeShift ? Date.now() : prevShiftAt,
        shifts_24h: shifts24h,
      };
      await redisSaveRegimeCache(cache);
      console.log(`[heavy-sweep] ${symbolConfig.symbol}: regime cache updated with optimized brick params`);
    } catch (err) {
      console.warn(`[heavy-sweep] ${symbolConfig.symbol}: regime cache save failed: ${err}`);
    }

    await redisSetHeartbeat(symbolConfig.symbol, symbolConfig.heartbeat_ttl_seconds);

    console.log(`[heavy-sweep] ${symbolConfig.symbol}: Redis config updated (regime=${combinedRegime}, kelly=${kellyF.toFixed(3)})`);

    // 9. Log to Supabase
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
      sweep_window: "45d",  // BETA: 45d to fit 0.5 CPU Render tier. Upgrade to 90d at ≥1 CPU
      sweep_duration_ms: duration,
      n_combos_tested: nCombos,
    });

    await logRegimeEntry({
      symbol: symbolConfig.symbol,
      source: "heavy-sweep",
      regime: combinedRegime,
      regime_conf: regimeConf,
      markov_p_up: markovPUp,
      markov_p_dn: markovPDn,
      kelly_f: kellyF,
      prev_regime: prevRegime,
      regime_shift: regimeShift,
    });

    console.log(`[heavy-sweep] ${symbolConfig.symbol}: Complete in ${duration}ms (${nCombos} combos, 1 API call)`);

    return { best, regime: { regime_label: combinedRegime, confidence: regimeConf, risk_multiplier: riskMultiplier }, kelly: { kelly_f: kellyF }, duration, nCombos };

  } catch (err) {
    const duration = Date.now() - startTime;
    const errMsg = err instanceof Error ? err.message : String(err);

    console.error(`[heavy-sweep] ${symbolConfig.symbol} FAILED: ${errMsg}`);

    await redisPushError(symbolConfig.symbol, "heavy-sweep", errMsg);
    await logError(symbolConfig.symbol, "heavy-sweep", errMsg);

    return { best: null, regime: null, kelly: null, duration, nCombos: 0, error: errMsg };
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
    `[heavy-sweep] FATAL: Missing required env vars: ${MISSING_ENV_VARS.join(", ")}. ` +
    `Set them in Supabase Dashboard → Edge Functions → Secrets.`
  );
}

// ─── Noble Trader Backend Warmup ────────────────────────────────
// Cold-start on Render free tier can take 30-60s. Pre-warm to avoid
// eating into the sweep time budget.
async function warmupNobleTrader(): Promise<void> {
  const url = Deno.env.get("NOBLE_TRADER_URL") ?? "https://noble-trader-fastapi-backend.onrender.com";
  try {
    console.log("[heavy-sweep] Warming up Noble Trader backend...");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    const res = await fetch(`${url}/health`, { signal: controller.signal });
    clearTimeout(timer);
    if (res.ok) {
      console.log("[heavy-sweep] Noble Trader backend is warm");
    } else {
      console.warn(`[heavy-sweep] Noble Trader warmup returned ${res.status}`);
    }
  } catch (err) {
    console.warn(`[heavy-sweep] Noble Trader warmup failed (cold start likely): ${err instanceof Error ? err.message : String(err)}`);
  }
}

// ─── Main Handler ───────────────────────────────────────────
Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders() });
  }

  const body = await req.json().catch(() => ({}));
  const { asset_class, symbol, symbols, warmup, max_symbols, action } = body;

  // ── Warmup ping: boot the worker, return immediately ──
  if (warmup === true) {
    // Also warm up Noble Trader backend while we're at it
    await warmupNobleTrader();
    return new Response(
      JSON.stringify({ status: "warmup", message: "Worker booted + Noble Trader warmed", timestamp: Date.now() }),
      { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  // ── Sync allowlist: reads nt_symbol → writes Redis allowlist + venue_map ──
  if (action === "sync-allowlist") {
    try {
      const activeSymbols = await getAllActiveSymbols();
      const hlSymbols: string[] = [];
      const alpacaSymbols: string[] = [];
      const venueMapCommands: unknown[][] = [];

      // Delete old venue_map keys for deactivated symbols
      const allSymbols = await supabaseRest("nt_symbol", "GET", undefined, "select=symbol,active");
      const deactivated = (allSymbols as { symbol: string; active: boolean }[]).filter(s => !s.active);
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

      // Build allowlist pipeline: DEL + SADD for each venue
      const pipeline: unknown[][] = [
        ["DEL", "trading:allowlist:hyperliquid"],
        ["SADD", "trading:allowlist:hyperliquid", ...hlSymbols],
        ["DEL", "trading:allowlist:alpaca"],
        ["SADD", "trading:allowlist:alpaca", ...alpacaSymbols],
        ...venueMapCommands,
      ];

      const UPSTASH_URL = Deno.env.get("UPSTASH_REDIS_REST_URL")!;
      const UPSTASH_TOKEN = Deno.env.get("UPSTASH_REDIS_REST_TOKEN")!;

      // Execute in chunks of 10 (Upstash pipeline limit)
      let totalCommands = 0;
      for (let i = 0; i < pipeline.length; i += 10) {
        const chunk = pipeline.slice(i, i + 10);
        const res = await fetch(`${UPSTASH_URL}/pipeline`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${UPSTASH_TOKEN}`,
            "Content-Type": "application/json",
          },
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

  // ── Env var check (after warmup — warmup doesn't need env vars) ──
  if (MISSING_ENV_VARS.length > 0) {
    return new Response(
      JSON.stringify({
        error: `Missing required env vars: ${MISSING_ENV_VARS.join(", ")}`,
        hint: "Set them in Supabase Dashboard → Edge Functions → Secrets",
      }),
      { status: 503, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }

  try {
    // ── Warm up Noble Trader backend first (avoid cold start during sweep) ──
    await warmupNobleTrader();

    // ── Resolve symbols from symbol, symbols array, or asset_class ──
    let symbolsToProcess: SymbolConfig[];
    const isBatchSymbols = Array.isArray(symbols) && symbols.length > 0;

    if (symbol) {
      const found = await getSymbolByName(symbol);
      if (!found) {
        return new Response(
          JSON.stringify({ error: `Symbol ${symbol} not found in registry` }),
          { status: 404, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
        );
      }
      symbolsToProcess = [found];
    } else if (isBatchSymbols) {
      const found: SymbolConfig[] = [];
      for (const sym of symbols) {
        const s = await getSymbolByName(sym);
        if (s) found.push(s);
        else console.warn(`[heavy-sweep] Symbol ${sym} not found, skipping`);
      }
      if (found.length === 0) {
        return new Response(
          JSON.stringify({ error: "None of the provided symbols were found in registry" }),
          { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
        );
      }
      symbolsToProcess = found;
    } else if (asset_class) {
      symbolsToProcess = await getSymbolsByAssetClass(asset_class);
    } else {
      return new Response(
        JSON.stringify({ error: "Provide 'asset_class', 'symbol', or 'symbols' in request body" }),
        { status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
      );
    }

    // ── Cap symbols per invocation to prevent 546/504 timeouts ──
    //    Each symbol takes ~35-50s. Supabase kills us at ~150s (free) or ~400s (paid).
    //    Default: 2 symbols max. Override with max_symbols in request body.
    const MAX_SYMBOLS_PER_INVOCATION = max_symbols ?? 2;
    let skippedSymbols: string[] = [];
    if (symbolsToProcess.length > MAX_SYMBOLS_PER_INVOCATION) {
      console.warn(
        `[heavy-sweep] Capping from ${symbolsToProcess.length} to ${MAX_SYMBOLS_PER_INVOCATION} symbols ` +
        `(prevent 546/504 timeout). Invoke again for remaining symbols.`
      );
      skippedSymbols = symbolsToProcess.slice(MAX_SYMBOLS_PER_INVOCATION).map(s => s.symbol);
      symbolsToProcess = symbolsToProcess.slice(0, MAX_SYMBOLS_PER_INVOCATION);
    }

    console.log(`[heavy-sweep] Processing ${symbolsToProcess.length} symbols for ${asset_class || (isBatchSymbols ? symbols.join(",") : symbol)}`);

    // ── Global deadline: Supabase kills us at ~150s for free / ~400s for paid ──
    //    Each symbol takes ~35-50s. With max 2 symbols + warmup, that's ~115s.
    //    Leave 30s buffer for Redis writes + response.
    const DEADLINE_MS = 120_000;
    const startTime = Date.now();

    const results: Record<string, unknown> = {};

    // Process symbols sequentially to avoid overloading Noble Trader
    for (let i = 0; i < symbolsToProcess.length; i++) {
      // ── Check deadline before each symbol ──
      const elapsed = Date.now() - startTime;
      if (elapsed > DEADLINE_MS) {
        console.warn(`[heavy-sweep] Deadline exceeded (${elapsed}ms > ${DEADLINE_MS}ms). Skipping remaining ${symbolsToProcess.length - i} symbols.`);
        for (let j = i; j < symbolsToProcess.length; j++) {
          results[symbolsToProcess[j].symbol] = { best: null, regime: null, kelly: null, duration: 0, nCombos: 0, error: "Deadline exceeded — skipped" };
        }
        break;
      }

      const sym = symbolsToProcess[i];
      console.log(`[heavy-sweep] Starting sweep for ${sym.symbol} (${i + 1}/${symbolsToProcess.length}, ${elapsed}ms elapsed)...`);
      const result = await runSweepForSymbol(sym);
      results[sym.symbol] = result;

      // Reduced inter-symbol delay (was 2s, now 1s — Noble Trader handles its own rate limiting)
      if (i < symbolsToProcess.length - 1) {
        await sleep(1000);
      }
    }

    const successCount = Object.values(results).filter((r: any) => !r.error).length;
    const failCount = Object.values(results).filter((r: any) => r.error).length;

    const response: Record<string, unknown> = {
      success: true,
      asset_class: asset_class || "batch",
      symbols_processed: symbolsToProcess.length,
      successes: successCount,
      failures: failCount,
      results,
    };

    // Include skipped symbols info if we capped the batch
    if (skippedSymbols.length > 0) {
      response.skipped_symbols = skippedSymbols;
      response.hint = `Invoke again with same parameters to process remaining ${skippedSymbols.length} symbols`;
    }

    return new Response(
      JSON.stringify(response),
      { headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );

  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[heavy-sweep] Fatal error: ${errMsg}`);

    return new Response(
      JSON.stringify({ error: errMsg }),
      { status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
    );
  }
});
