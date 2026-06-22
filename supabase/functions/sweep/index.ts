// ─── Unified Sweep Edge Function (v1 — Option F) ───────────────
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

import {
  corsHeaders,
  getSymbolsByAssetClass,
  getSymbolByName,
  getAllActiveSymbols,
  supabaseRest,
  fetchSweepCandles,
  fetchLatestCandles,
  computeATR,
  generateBrickSizes,
  runRenkoOptimize,
  detectRegime,
  computeKelly,
  redisSetConfig,
  redisGetConfig,
  redisUpdateSignal,
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
  isMarketOpen,
  safeParseFloat,
  safeParseInt,
  PRICE_TICK_DECIMALS,
  NOBLE_TRADER_URL,
  TIMESFM_INFERENCE_URL,
  type SymbolConfig,
  type RedisConfig,
  type RegimeCache,
  type RegimeCacheEntry,
  type RegimeResult,
  type KellyResult,
  type OptimizeComboResult,
  type EVResult,
  timesfmPost,
  computeCalibrationBias,
  computeCorrelationMatrix,
  adjustLeadLag,
  computeTailRisk,
  isVolatilityShift,
  type CorrelationResult,
  type TailRiskResult,
} from "../_shared/utils.ts";

// ─── Concurrency Control Constants ────────────────────────────
const GLOBAL_CAP = 4;                    // max concurrent sweep invocations
const GLOBAL_STALE_MS = 300_000;         // 5 min — evict stale slots
const GLOBAL_EXPIRE_S = 330;             // safety net EXPIRE on sorted set
const HEAVY_LOCK_TTL_S = 360;            // 6 min — heavy sweep lock
const LIGHT_LOCK_TTL_S = 90;             // 1.5 min — light regime lock

// ─── F-Lite-1: Heavy Hours (UTC) ──────────────────────────────
// Light regime is suppressed during these hours to avoid colliding
// with heavy sweep cron jobs. Enforced BOTH in the cron schedule
// AND here (defense in depth).
//   Crypto heavy: hours 6, 18 (2x/day, TimesFM-gated)
//   Stocks heavy: hour 21    (3x/week Mon/Wed/Fri)
const HEAVY_HOURS_UTC = [6, 18, 21];

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

    // 1.5. TimesFM confidence gate — skip heavy if forecast is confident
    // TimesFM p_timesfm near 0 or 1 = high confidence (stable regime)
    // TimesFM p_timesfm near 0.5 = uncertain → run full sweep
    try {
      const timesfmMap = await timesfmPost([
        { symbol: symbolConfig.symbol, prices: prices.slice(-500) },
      ]);
      const pTimesfm = timesfmMap.get(symbolConfig.symbol);
      if (pTimesfm !== null && pTimesfm !== undefined) {
        const confidence = Math.abs(pTimesfm - 0.5); // 0 = uncertain, 0.5 = max confidence
        if (confidence > 0.1) {
          console.log(`[sweep:heavy] ${symbolConfig.symbol}: SKIPPED — TimesFM confident (p_timesfm=${pTimesfm.toFixed(4)}, confidence=${confidence.toFixed(4)} threshold=0.1)`);
          return {
            best: null, regime: null, kelly: null,
            duration: 0, nCombos: 0,
            error: `TimesFM-stable (p=${pTimesfm.toFixed(4)})`,
          };
        }
        console.log(`[sweep:heavy] ${symbolConfig.symbol}: TimesFM uncertain (p_timesfm=${pTimesfm.toFixed(4)}) — running full sweep`);
      }
    } catch (e) {
      console.warn(`[sweep:heavy] ${symbolConfig.symbol}: TimesFM gate failed (${e instanceof Error ? e.message : String(e)}) — proceeding without gate`);
    }

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
        sweep_window: "90d (fallback)",
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
		        const bias = cal?.bias ?? 0;
		        const status = cal?.status ?? "INSUFFICIENT_DATA";
		        const skipUntil = status === "CALIBRATED" ? String(Date.now() + 12 * 3600 * 1000) : "0";
		        await sweepRedisPipeline([
		          ["HSET", `trading:config:${symbolConfig.symbol}`,
		            "calibration_bias", String(bias),
		            "calibration_status", status,
		            "sweep_skip_until", skipUntil,
		          ],
		        ]);
		        if (cal) {
		          console.log(`[sweep:heavy] ${symbolConfig.symbol}: calibration_bias=${cal.bias} (${cal.status})`);
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
		      const bias = cal?.bias ?? 0;
		      const status = cal?.status ?? "INSUFFICIENT_DATA";
		      const skipUntil = status === "CALIBRATED" ? String(Date.now() + 12 * 3600 * 1000) : "0";
		      await sweepRedisPipeline([
		        ["HSET", `trading:config:${symbolConfig.symbol}`,
		          "calibration_bias", String(bias),
		          "calibration_status", status,
		          "sweep_skip_until", skipUntil,
		        ],
		      ]);
		      if (cal) {
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
        } : {
          p_timesfm: 0.5,
          timesfm_horizon: "0h",
        },
        {
          tail_risk_score: tailRisk.score,
          tail_risk_action: tailRisk.action,
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
        p_timesfm: pTimesfm ?? 0.5,
        timesfm_horizon: pTimesfm !== null ? "12h" : "0h",
        tail_risk_score: tailRisk.score,
        tail_risk_action: tailRisk.action,
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
  const url = NOBLE_TRADER_URL || Deno.env.get("NOBLE_TRADER_URL") || "http://127.0.0.1:8001";
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
  "NOBLE_TRADER_URL",
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

    // ── Light mode: process in background, return 202 immediately ──
    // (preserves the light-regime pattern that avoids pg_net 5s timeout)
    const symbolsToProcessCaptured = symbolsToProcess;
    setTimeout(() => processLightBatch(symbolsToProcessCaptured, invocationToken), 0);

    return new Response(
      JSON.stringify({
        status: "accepted",
        mode: "light",
        message: `Processing ${symbolsToProcess.length} symbols in background`,
        symbols: symbolsToProcess.map(s => s.symbol),
        timestamp: Date.now(),
      }),
      { status: 202, headers: { ...corsHeaders(), "Content-Type": "application/json" } },
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
): Promise<void> {
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
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    console.error(`[sweep:light] Background processing error: ${errMsg}`);
  }
}
