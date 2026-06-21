# Markov Entry Signal Plan — Noble Trader v7.2.0 Integration

## The Core Problem

Our backtesting and regime detection systems tell us **WHAT** happened (optimal params, Sharpe ratios, P/L, regime labels). They do not tell us **WHEN** to act in live markets. This plan bridges that gap by implementing the 4-step Markov Application Framework as a real-time entry signal pipeline on top of our existing Noble Trader + HyperLiquid infrastructure.

---

## 1. Architecture Overview

### 1.1 How the 4 Markov Steps Map to Our System

| Markov Step | Existing System Component | Gap | New Component |
|---|---|---|---|
| **Step 1: EV Calculation** | HMM regime confidence (`/regime/detect-v2` → `vol_confidence`, `trend_confidence`), L2 `ImbalanceSignal.confidence` | No unified EV computation | **EV Engine** (`src/lib/ev-engine.ts`) |
| **Step 2: Kelly Criterion Sizing** | `/size/kelly` returns `kelly_f`, `half_kelly`, `full_kelly`, `risk_multiplier` | Kelly uses static win-rate; doesn't feed from live EV | **Dynamic Kelly Bridge** (backend endpoint) |
| **Step 3: Bayesian Updating** | HMM refits every `refit_every` bars; L2 EMA smoothing in `generateImbalanceSignals()` | No Markov transition matrix; no real-time posterior updates | **Bayesian Updater** (`src/lib/bayesian-updater.ts`) + **Markov Chain** (`src/lib/markov-chain.ts`) |
| **Step 4: Aggression Scale** | No execution logic exists | Completely missing | **Aggression Dispatcher** (`src/lib/aggression-dispatcher.ts`) + **Executor** (`src/lib/executor.ts`) |

### 1.2 System Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        LIVE MARKET DATA LAYER                          │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ HyperLiquid WS   │  │ HyperLiquid REST │  │ Noble Trader        │  │
│  │ (L2 Book stream) │  │ (OHLCV candles)  │  │ (/regime/detect-v2) │  │
│  └───────┬──────────┘  └───────┬──────────┘  └──────────┬──────────┘  │
└──────────┼─────────────────────┼─────────────────────────┼─────────────┘
           │                     │                         │
┌──────────▼─────────────────────▼─────────────────────────▼─────────────┐
│                     SIGNAL GENERATION LAYER                            │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ L2 Imbalance     │  │ HMM Regime       │  │ Markov Transition   │  │
│  │ Engine           │  │ Detector         │  │ Matrix              │  │
│  │ (existing)       │  │ (existing)       │  │ (NEW)               │  │
│  └───────┬──────────┘  └───────┬──────────┘  └──────────┬──────────┘  │
└──────────┼─────────────────────┼─────────────────────────┼─────────────┘
           │                     │                         │
┌──────────▼─────────────────────▼─────────────────────────▼─────────────┐
│                       EV ENGINE (NEW)                                  │
│  P_win = f(regime_confidence, imbalance_confidence, markov_posterior) │
│  EV = (P_win × profit) - (P_loss × stake)                            │
│  Bayesian Update: P(A|B) = P(B|A) × P(A) / P(B)                     │
└───────────────────────────────┬────────────────────────────────────────┘
                                │
┌───────────────────────────────▼────────────────────────────────────────┐
│                    POSITION SIZING LAYER                               │
│  ┌──────────────────┐  ┌──────────────────────────────────────────┐   │
│  │ Kelly Criterion  │  │ Risk Gates: regime_gate, max_drawdown,   │   │
│  │ f = (p×b - q)/b │  │ max_position_pct, daily_loss_limit       │   │
│  └───────┬──────────┘  └──────────────────┬───────────────────────┘   │
└──────────┼────────────────────────────────┼───────────────────────────┘
           │                                │
┌──────────▼────────────────────────────────▼───────────────────────────┐
│                    EXECUTION LAYER (NEW)                               │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │ Aggression Dispatcher                                         │   │
│  │ EV > 0.40 → Passive Limit  (deep entry, capture max spread)   │   │
│  │ 0.15 < EV ≤ 0.40 → Mid-Point Limit (balanced fill prob)      │   │
│  │ EV ≤ 0.15 → Aggressive/Market (guaranteed fill)               │   │
│  └────────────────────────────────────────────────────────────────┘   │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │ Executor → HyperLiquid Order API (limit/market orders)        │   │
│  └────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

### 1.3 Key Files Referenced

| File | Role | Key Exports/Functions |
|---|---|---|
| `src/lib/hyperliquid-client.ts` | HL data fetching | `getOHLCV()`, `getL2Book()`, `getAllMids()`, `getPrice()` |
| `src/lib/l2-imbalance-engine.ts` | L2 signal generation | `computeImbalance()`, `generateImbalanceSignals()`, `runL2Backtest()` |
| `src/lib/noble-trader-client.ts` | Backend API client | `detectRegimeV2()`, `kellySize()`, `runRenkoBacktest()`, `getRiskDashboard()`, `simulate()` |
| `src/types/noble-trader.ts` | Backend types | `RegimeV2Request`, `SizeRequest`, `SizeResponse`, `RegimeSnapshot` |
| `src/types/hyperliquid.ts` | HL types | `HyperLiquidL2Book`, `HyperLiquidCandle`, `CandleInterval` |
| `src/types/simulation.ts` | Simulation types | `SimulationParams`, `BacktestMetrics`, `SimulationResult` |
| `src/stores/simulation-store.ts` | Zustand state | `renkoParams`, `l2Params`, `selectedSymbol` |

---

## 2. The Entry Signal Pipeline

### 2.1 Step-by-Step Flow: "Report Says Optimal Params" → "Live Entry Signal"

**Prerequisite**: A completed backtest optimization has identified optimal parameters (e.g., `brick_size=0.5`, `sl_bricks=3`, `tp_bricks=5`, `regime_gate=true`, `kelly_fraction=0.5`). These are stored in `simulation-store.ts` as `renkoParams` / `l2Params`.

```
STEP 0: PARAMETER LOAD
  ┌─────────────────────────────────────────┐
  │ Load optimal params from Zustand store  │
  │ renkoParams.brick_size = 0.5           │
  │ renkoParams.sl_bricks = 3              │
  │ renkoParams.regime_gate = true         │
  │ l2Params.depth_levels = 10             │
  │ l2Params.imbalance_threshold = 0.3     │
  └────────────────┬────────────────────────┘
                   │
STEP 1: LIVE DATA INGESTION (every tick / bar close)
  ┌────────────────▼────────────────────────┐
  │ 1a. Fetch latest N candles via          │
  │     hyperliquidClient.getOHLCV(         │
  │       symbol, interval, bars=200)       │
  │                                         │
  │ 1b. Fetch L2 book snapshot via          │
  │     hyperliquidClient.getL2Book(symbol) │
  │                                         │
  │ 1c. Fetch mid price via                 │
  │     hyperliquidClient.getPrice(symbol)  │
  └────────────────┬────────────────────────┘
                   │
STEP 2: REGIME DETECTION (on bar close)
  ┌────────────────▼────────────────────────┐
  │ nobleTraderClient.detectRegimeV2({      │
  │   prices: closePrices,                 │
  │   symbol,                              │
  │   n_states: renkoParams.n_hmm_states   │
  │ })                                     │
  │ → Returns: regime labels + confidence  │
  │   e.g. BULLISH 0.72, BEARISH 0.18,    │
  │        NEUTRAL 0.10                     │
  └────────────────┬────────────────────────┘
                   │
STEP 3: L2 IMBALANCE COMPUTATION (every tick)
  ┌────────────────▼────────────────────────┐
  │ computeImbalance(l2Snapshot, config)   │
  │ → imbalance: -1 to +1                  │
  │                                         │
  │ EMA-smooth: emaAlpha * raw + (1-alpha) │
  │ persistence tracking (min bars)         │
  │ → ImbalanceSignal {                     │
  │     signal: 'long'|'short'|'flat',     │
  │     confidence: abs(smoothed),         │
  │     persistence: N bars                │
  │   }                                     │
  └────────────────┬────────────────────────┘
                   │
STEP 4: MARKOV TRANSITION UPDATE (every bar)
  ┌────────────────▼────────────────────────┐
  │ Update Markov transition matrix with    │
  │ observed price state transition         │
  │                                         │
  │ markovChain.observe(prevState, newState)│
  │ → Updated transition probabilities      │
  │ e.g. P(UP | NEUTRAL) = 0.42           │
  └────────────────┬────────────────────────┘
                   │
STEP 5: EV CALCULATION (every tick/bar)
  ┌────────────────▼────────────────────────┐
  │ P_win = computePwin({                  │
  │   regimeConfidence,                    │  ← Step 2
  │   imbalanceConfidence,                 │  ← Step 3
  │   markovPosterior,                     │  ← Step 4
  │   direction: signal.signal             │
  │ })                                      │
  │                                         │
  │ profit = tp_bricks × brick_size        │  ← from renkoParams
  │ stake  = sl_bricks × brick_size        │  ← from renkoParams
  │                                         │
  │ EV = (P_win × profit) - (P_loss × stake)│
  └────────────────┬────────────────────────┘
                   │
STEP 6: BAYESIAN UPDATE (on new evidence)
  ┌────────────────▼────────────────────────┐
  │ If new bar closes confirming signal:    │
  │   P(A|B) = P(B|A) × P(A) / P(B)      │
  │   where:                                │
  │     A = "trade wins"                   │
  │     B = "new confirming evidence"      │
  │                                         │
  │ Posterior P_win replaces prior in EV   │
  └────────────────┬────────────────────────┘
                   │
STEP 7: POSITION SIZING (on entry decision)
  ┌────────────────▼────────────────────────┐
  │ kellySize({                             │
  │   prices,                               │
  │   symbol,                               │
  │   kelly_fraction: renkoParams.kelly_fraction, │
  │   target_vol: renkoParams.target_vol,   │
  │   use_regime: renkoParams.regime_gate   │
  │ })                                      │
  │ → position_size, position_pct,          │
  │   half_kelly, risk_multiplier           │
  │                                         │
  │ Override: position_pct × (EV / 0.5)    │
  │ (scale position by EV confidence)       │
  └────────────────┬────────────────────────┘
                   │
STEP 8: RISK GATE (before execution)
  ┌────────────────▼────────────────────────┐
  │ Check ALL risk conditions:              │
  │ ✅ EV > 0 (positive edge)              │
  │ ✅ regime_gate: regime aligns w/ signal │
  │ ✅ max_position_pct < limit             │
  │ ✅ current_drawdown < max_drawdown      │
  │ ✅ daily_loss < daily_loss_limit        │
  │ ✅ no open position (or within limits)  │
  │ If any check fails → NO ENTRY           │
  └────────────────┬────────────────────────┘
                   │
STEP 9: AGGRESSION MAPPING + EXECUTION
  ┌────────────────▼────────────────────────┐
  │ EV > 0.40 → Passive Limit Order        │
  │   limit_price = mid - (spread × 1.5)   │
  │                                          │
  │ 0.15 < EV ≤ 0.40 → Mid-Point Limit     │
  │   limit_price = mid - (spread × 0.5)   │
  │                                          │
  │ EV ≤ 0.15 → Aggressive/Market Order     │
  │   market order at current best ask/bid  │
  │                                          │
  │ Dispatch to HyperLiquid Order API       │
  └─────────────────────────────────────────┘
```

---

## 3. EV Engine Design

### 3.1 Core Formula

```
EV = (P_win × profit) - (P_loss × stake)

Where:
  profit = tp_bricks × brick_size   (take-profit distance in price units)
  stake  = sl_bricks × brick_size   (stop-loss distance in price units)
  P_loss = 1 - P_win
```

### 3.2 Computing P_win Dynamically

P_win is NOT a static backtest win-rate. It is computed in real-time from three signal sources blended with configurable weights:

```
P_win = w_regime × P_regime + w_imbalance × P_imbalance + w_markov × P_markov

Where:
  w_regime    + w_imbalance + w_markov = 1.0
  Default:    0.40        + 0.30        + 0.30       = 1.0
```

**P_regime** — From HMM regime detection (`/regime/detect-v2`):
```typescript
// Given: regime_result = { states: ["BULLISH", "BEARISH", "NEUTRAL"], 
//                          probs: [0.72, 0.18, 0.10] }
//
// For a LONG signal:
//   P_regime = P(BULLISH) + 0.5 × P(NEUTRAL)
//            = 0.72 + 0.5 × 0.10 = 0.77
//
// For a SHORT signal:
//   P_regime = P(BEARISH) + 0.5 × P(NEUTRAL)
//            = 0.18 + 0.5 × 0.10 = 0.23

function computePRegime(
  regimeProbs: Record<string, number>,
  direction: 'long' | 'short'
): number {
  if (direction === 'long') {
    return (regimeProbs['BULLISH'] ?? 0) + 0.5 * (regimeProbs['NEUTRAL'] ?? 0);
  } else {
    return (regimeProbs['BEARISH'] ?? 0) + 0.5 * (regimeProbs['NEUTRAL'] ?? 0);
  }
}
```

**P_imbalance** — From L2 imbalance engine (`computeImbalance()` + `generateImbalanceSignals()`):
```typescript
// ImbalanceSignal.confidence = abs(smoothedImbalance) ∈ [0, 1]
// ImbalanceSignal.persistence ∈ [0, N]
// ImbalanceSignal.signal = 'long' | 'short' | 'flat'
//
// P_imbalance = f(confidence, persistence, alignment)
//
// If signal aligns with direction:
//   P_imbalance = confidence × min(1.0, persistence / min_persistence_target)
// If signal misaligns or is flat:
//   P_imbalance = 1 - confidence  (reduced probability)

function computePImbalance(
  signal: ImbalanceSignal,
  direction: 'long' | 'short',
  minPersistenceTarget: number = 5
): number {
  if (signal.signal === direction) {
    const persistenceBonus = Math.min(1.0, signal.persistence / minPersistenceTarget);
    return signal.confidence * (0.6 + 0.4 * persistenceBonus);
  } else if (signal.signal === 'flat') {
    return 0.5; // No signal - neutral
  } else {
    // Opposing signal
    return 1.0 - signal.confidence;
  }
}
```

**P_markov** — From Markov transition matrix (NEW):
```typescript
// The Markov chain tracks discrete price states:
//   UP   = price increased by > brick_size from prev bar
//   DOWN = price decreased by > brick_size from prev bar
//   FLAT = price change within brick_size
//
// Transition matrix T[i][j] = P(state_j | state_i)
// After N observations, T converges to true transition probabilities
//
// P_markov = T[currentState][targetState]
//   For LONG:  targetState = 'UP'
//   For SHORT: targetState = 'DOWN'

function computePMarkov(
  transitionMatrix: TransitionMatrix,
  currentState: PriceState,
  direction: 'long' | 'short'
): number {
  const targetState = direction === 'long' ? 'UP' : 'DOWN';
  return transitionMatrix[currentState][targetState] ?? 0.33;
}
```

### 3.3 EV Engine Implementation

File: `src/lib/ev-engine.ts`

```typescript
// ─── Types ─────────────────────────────────────────────────

export type PriceState = 'UP' | 'DOWN' | 'FLAT';

export interface EVInput {
  // From HMM regime detection
  regimeProbs: Record<string, number>;    // e.g. { BULLISH: 0.72, BEARISH: 0.18, NEUTRAL: 0.10 }
  
  // From L2 imbalance engine
  imbalanceSignal: ImbalanceSignal;
  
  // From Markov chain
  markovPosterior: number;               // P(targetState | currentState)
  currentState: PriceState;
  
  // From backtest optimization (stored params)
  direction: 'long' | 'short';
  tpBricks: number;                       // from renkoParams.tp_bricks
  slBricks: number;                       // from renkoParams.sl_bricks
  brickSize: number;                      // from renkoParams.brick_size
  
  // Blending weights (configurable)
  weights?: {
    regime: number;     // default 0.40
    imbalance: number;  // default 0.30
    markov: number;     // default 0.30
  };
}

export interface EVResult {
  ev: number;               // Expected Value
  pWin: number;             // Blended P(win)
  pRegime: number;          // Regime component
  pImbalance: number;       // Imbalance component
  pMarkov: number;          // Markov component
  profit: number;           // TP distance ($)
  stake: number;            // SL distance ($)
  aggression: AggressionLevel;
  limitOffset: number;      // Offset from mid for limit order
}

export type AggressionLevel = 'passive_limit' | 'midpoint_limit' | 'aggressive_market';

// ─── Core Computation ──────────────────────────────────────

export function computeEV(input: EVInput): EVResult {
  const w = input.weights ?? { regime: 0.40, imbalance: 0.30, markov: 0.30 };
  
  const pRegime = computePRegime(input.regimeProbs, input.direction);
  const pImbalance = computePImbalance(input.imbalanceSignal, input.direction);
  const pMarkov = input.markovPosterior;
  
  // Blended P_win
  const pWin = w.regime * pRegime + w.imbalance * pImbalance + w.markov * pMarkov;
  const pLoss = 1 - pWin;
  
  // Profit and stake in price units
  const profit = input.tpBricks * input.brickSize;
  const stake = input.slBricks * input.brickSize;
  
  // EV = (P_win × profit) - (P_loss × stake)
  const ev = (pWin * profit) - (pLoss * stake);
  
  // Map EV to aggression level
  const aggression = mapAggression(ev);
  const limitOffset = computeLimitOffset(ev, input.brickSize);
  
  return { ev, pWin, pRegime, pImbalance, pMarkov, profit, stake, aggression, limitOffset };
}

// ─── Aggression Mapping ───────────────────────────────────

function mapAggression(ev: number): AggressionLevel {
  if (ev > 0.40) return 'passive_limit';
  if (ev > 0.15) return 'midpoint_limit';
  return 'aggressive_market';
}

// Limit offset in brick_size units relative to mid-price
// Positive = worse price (deeper into book for maker rebate)
function computeLimitOffset(ev: number, brickSize: number): number {
  if (ev > 0.40) {
    // Passive: aim 1.5 bricks inside the spread
    return 1.5 * brickSize;
  } else if (ev > 0.15) {
    // Mid-point: aim 0.5 bricks inside the spread
    return 0.5 * brickSize;
  } else {
    // Aggressive: no offset, take the market
    return 0;
  }
}
```

### 3.4 EV Normalization

EV is expressed in `brick_size × brick_count` units. For cross-asset comparison, we normalize:

```
EV_normalized = EV / stake = (P_win × R - 1) / 1
where R = profit / stake = tp_bricks / sl_bricks (reward-to-risk ratio)

Example: tp_bricks=5, sl_bricks=3, P_win=0.65
  R = 5/3 = 1.667
  EV_norm = 0.65 × 1.667 - 0.35 × 1 = 1.083 - 0.35 = 0.733
```

A normalized EV > 0 means positive edge per unit of risk.

---

## 4. Bayesian Updating Layer

### 4.1 Markov Transition Matrix

We model price state as a first-order Markov chain with 3 states: UP, DOWN, FLAT.

```typescript
// File: src/lib/markov-chain.ts

export type PriceState = 'UP' | 'DOWN' | 'FLAT';
const STATES: PriceState[] = ['UP', 'DOWN', 'FLAT'];

export interface TransitionMatrix {
  UP:   Record<PriceState, number>;
  DOWN: Record<PriceState, number>;
  FLAT: Record<PriceState, number>;
}

export class MarkovChain {
  private counts: Record<PriceState, Record<PriceState, number>>;
  private matrix: TransitionMatrix | null = null;
  private prevState: PriceState | null = null;
  private totalObservations: number = 0;
  
  constructor(
    private brickSize: number,
    private priorCounts: number = 3  // Laplace smoothing (pseudo-counts)
  ) {
    // Initialize with uniform prior (Laplace smoothing)
    this.counts = {
      UP:   { UP: priorCounts, DOWN: priorCounts, FLAT: priorCounts },
      DOWN: { UP: priorCounts, DOWN: priorCounts, FLAT: priorCounts },
      FLAT: { UP: priorCounts, DOWN: priorCounts, FLAT: priorCounts },
    };
  }

  // Classify a price change into a state
  classifyState(currentPrice: number, previousPrice: number): PriceState {
    const change = currentPrice - previousPrice;
    if (change > this.brickSize) return 'UP';
    if (change < -this.brickSize) return 'DOWN';
    return 'FLAT';
  }

  // Observe a transition and update counts
  observe(prevState: PriceState, newState: PriceState): void {
    this.counts[prevState][newState]++;
    this.totalObservations++;
    this.matrix = null; // Invalidate cached matrix
  }

  // Process a new price, auto-detecting transition
  observePrice(price: number): PriceState {
    const state = this.prevState
      ? this.classifyState(price, /* need prev price */ 0)
      : 'FLAT';
    
    if (this.prevState) {
      this.observe(this.prevState, state);
    }
    this.prevState = state;
    return state;
  }

  // Compute transition probabilities with Laplace smoothing
  getMatrix(): TransitionMatrix {
    if (this.matrix) return this.matrix;
    
    const result: TransitionMatrix = { UP: {} as any, DOWN: {} as any, FLAT: {} as any };
    
    for (const from of STATES) {
      const row = this.counts[from];
      const total = row.UP + row.DOWN + row.FLAT;
      
      for (const to of STATES) {
        result[from][to] = row[to] / total;
      }
    }
    
    this.matrix = result;
    return result;
  }

  // Get transition probability: P(nextState | currentState)
  getTransitionProbability(currentState: PriceState, nextState: PriceState): number {
    return this.getMatrix()[currentState][nextState];
  }

  // Get total observations (for confidence weighting)
  getObservationCount(): number {
    return this.totalObservations;
  }

  // Warm-start the chain from historical price data
  warmup(prices: number[]): void {
    for (let i = 1; i < prices.length; i++) {
      const prev = this.classifyState(prices[i], prices[i - 1]);
      // We need to track the previous state properly
      const state = this.classifyState(prices[i], prices[i - 1]);
      if (i > 1) {
        const prevState = this.classifyState(prices[i - 1], prices[i - 2]);
        this.observe(prevState, state);
      }
    }
  }

  // Serialize for persistence
  serialize(): string {
    return JSON.stringify({
      counts: this.counts,
      brickSize: this.brickSize,
      priorCounts: this.priorCounts,
      totalObservations: this.totalObservations,
    });
  }

  // Deserialize
  static deserialize(data: string): MarkovChain {
    const parsed = JSON.parse(data);
    const chain = new MarkovChain(parsed.brickSize, parsed.priorCounts);
    chain.counts = parsed.counts;
    chain.totalObservations = parsed.totalObservations;
    return chain;
  }
}
```

### 4.2 Bayesian Posterior Update

The Bayesian updater takes the Markov transition probability as a prior and updates it with new evidence (L2 imbalance shifts, regime changes).

```typescript
// File: src/lib/bayesian-updater.ts

export interface BayesianPrior {
  pA: number;       // Prior P(A) = P(trade wins)
  pNotA: number;    // P(not A) = P(trade loses)
}

export interface BayesianEvidence {
  pBGivenA: number;    // P(B|A) = P(observed evidence | trade wins)
  pBGivenNotA: number; // P(B|¬A) = P(observed evidence | trade loses)
}

export interface BayesianPosterior {
  pAGivenB: number;    // Posterior P(A|B)
  bayesFactor: number; // pBGivenA / pBGivenNotA (evidence strength)
  prior: number;
  posterior: number;
  updateMagnitude: number; // |posterior - prior|
}

/**
 * Core Bayesian update:
 * P(A|B) = P(B|A) × P(A) / P(B)
 * where P(B) = P(B|A)×P(A) + P(B|¬A)×P(¬A)
 */
export function bayesianUpdate(prior: BayesianPrior, evidence: BayesianEvidence): BayesianPosterior {
  const pB = evidence.pBGivenA * prior.pA + evidence.pBGivenNotA * prior.pNotA;
  
  if (pB === 0) {
    return { pAGivenB: prior.pA, bayesFactor: 1, prior: prior.pA, posterior: prior.pA, updateMagnitude: 0 };
  }
  
  const pAGivenB = (evidence.pBGivenA * prior.pA) / pB;
  const bayesFactor = evidence.pBGivenA / evidence.pBGivenNotA;
  
  return {
    pAGivenB,
    bayesFactor,
    prior: prior.pA,
    posterior: pAGivenB,
    updateMagnitude: Math.abs(pAGivenB - prior.pA),
  };
}

/**
 * Construct evidence from L2 imbalance signal
 * 
 * If imbalance is strongly bullish AND direction is long:
 *   P(strong_bid_imbalance | trade_wins) = high (0.8)
 *   P(strong_bid_imbalance | trade_loses) = low (0.3)
 *
 * If imbalance is flat:
 *   P(flat_imbalance | trade_wins) = 0.5
 *   P(flat_imbalance | trade_loses) = 0.5
 *   → No update (Bayes factor = 1.0)
 */
export function imbalanceToEvidence(
  signal: ImbalanceSignal,
  direction: 'long' | 'short'
): BayesianEvidence {
  const isAligned = signal.signal === direction;
  const confidence = signal.confidence; // abs(smoothedImbalance) ∈ [0, 1]
  const persistence = signal.persistence;
  
  if (signal.signal === 'flat') {
    return { pBGivenA: 0.5, pBGivenNotA: 0.5 }; // Neutral evidence
  }
  
  if (isAligned) {
    // Aligned imbalance: evidence FOR the trade
    // Higher confidence + persistence → stronger evidence
    const alignmentStrength = confidence * Math.min(1.0, persistence / 5);
    return {
      pBGivenA:    0.5 + 0.4 * alignmentStrength,   // 0.5 → 0.9
      pBGivenNotA: 0.5 - 0.3 * alignmentStrength,    // 0.5 → 0.2
    };
  } else {
    // Opposing imbalance: evidence AGAINST the trade
    const oppositionStrength = confidence * Math.min(1.0, persistence / 5);
    return {
      pBGivenA:    0.5 - 0.3 * oppositionStrength,   // 0.5 → 0.2
      pBGivenNotA: 0.5 + 0.4 * oppositionStrength,   // 0.5 → 0.9
    };
  }
}

/**
 * Construct evidence from regime confirmation
 *
 * If regime is BULLISH and direction is long:
 *   P(bullish_regime | trade_wins) = 0.75
 *   P(bullish_regime | trade_loses) = 0.35
 */
export function regimeToEvidence(
  regimeProbs: Record<string, number>,
  direction: 'long' | 'short'
): BayesianEvidence {
  const bullishProb = regimeProbs['BULLISH'] ?? 0;
  const bearishProb = regimeProbs['BEARISH'] ?? 0;
  
  const alignmentScore = direction === 'long'
    ? bullishProb - bearishProb
    : bearishProb - bullishProb;
  
  // alignmentScore ∈ [-1, 1]
  // Positive = regime aligns with trade direction
  
  return {
    pBGivenA:    0.5 + 0.35 * alignmentScore,   // 0.15 → 0.85
    pBGivenNotA: 0.5 - 0.25 * alignmentScore,   // 0.25 → 0.75
  };
}

/**
 * Sequential Bayesian update: apply multiple evidence sources in order
 * Each update's posterior becomes the next update's prior
 */
export function sequentialBayesianUpdate(
  initialPrior: number,
  evidenceList: BayesianEvidence[]
): BayesianPosterior[] {
  const results: BayesianPosterior[] = [];
  let currentPrior = initialPrior;
  
  for (const evidence of evidenceList) {
    const result = bayesianUpdate(
      { pA: currentPrior, pNotA: 1 - currentPrior },
      evidence
    );
    results.push(result);
    currentPrior = result.posterior;
  }
  
  return results;
}
```

### 4.3 Complete Update Flow

On each new bar/tick, the Bayesian update proceeds as:

```
1. Prior = Markov transition probability
   P_prior = markovChain.getTransitionProbability(currentState, targetState)

2. Evidence 1: L2 imbalance alignment
   evidence_imbalance = imbalanceToEvidence(imbalanceSignal, direction)

3. Evidence 2: HMM regime alignment
   evidence_regime = regimeToEvidence(regimeProbs, direction)

4. Sequential Bayesian update
   posteriors = sequentialBayesianUpdate(P_prior, [evidence_imbalance, evidence_regime])

5. Final posterior = posteriors[1].posterior
   This is the P_markov input to the EV engine
```

---

## 5. Aggression Scale Implementation

### 5.1 The Aggression Spectrum (from Markov doc)

| EV Range | Strategy | Logic | Target |
|---|---|---|---|
| EV > 0.40 | Passive Limit | Deep entry | Capture max spread (maker rebate) |
| 0.15 < EV ≤ 0.40 | Mid-Point Limit | Standard entry | Balanced fill probability |
| EV ≤ 0.15 | Aggressive/Market | Immediate entry | Guaranteed fill |

### 5.2 Limit Order Offset Computation

The limit offset determines where to place a limit order relative to the mid-price. The goal: higher EV → we can afford to wait for a better price (maker rebate); lower EV → we must seize the edge before it disappears.

```typescript
// File: src/lib/aggression-dispatcher.ts

export interface AggressionConfig {
  // Per-asset-class spread multipliers
  spreadMultiplier: {
    passive: number;      // default: 1.5
    midpoint: number;     // default: 0.5
    aggressive: number;   // default: 0.0 (market)
  };
  
  // Time-in-force per level
  timeInForce: {
    passive: 'GTC' | 'IOC' | 'POST_ONLY';    // default: POST_ONLY
    midpoint: 'GTC' | 'IOC';                   // default: GTC
    aggressive: 'IOC';                          // default: IOC
  };
  
  // Max wait time for passive fills (ms)
  passiveTimeout: number;  // default: 60000 (60 seconds)
  
  // Fallback: if passive not filled within timeout, re-submit at midpoint
  fallbackOnTimeout: boolean;  // default: true
}

export interface DispatchOrder {
  symbol: string;
  direction: 'buy' | 'sell';
  orderType: 'limit' | 'market';
  limitPrice: number | null;
  size: number;
  timeInForce: string;
  aggressionLevel: AggressionLevel;
  ev: number;
  expectedFillProbability: number;
}

export function dispatchOrder(
  ev: number,
  evResult: EVResult,
  aggressionLevel: AggressionLevel,
  midPrice: number,
  spread: number,          // best ask - best bid
  positionSize: number,    // from Kelly sizing
  symbol: string,
  direction: 'long' | 'short',
  config: AggressionConfig
): DispatchOrder {
  const isBuy = direction === 'long';
  
  switch (aggressionLevel) {
    case 'passive_limit': {
      // Place limit deep in the book to capture maker rebate
      const offset = spread * config.spreadMultiplier.passive;
      const limitPrice = isBuy
        ? midPrice - offset   // Buy below mid
        : midPrice + offset;  // Sell above mid
      
      return {
        symbol,
        direction: isBuy ? 'buy' : 'sell',
        orderType: 'limit',
        limitPrice: roundToTick(limitPrice, symbol),
        size: positionSize,
        timeInForce: config.timeInForce.passive,
        aggressionLevel,
        ev,
        expectedFillProbability: 0.3,  // Lower fill prob, but better price
      };
    }
    
    case 'midpoint_limit': {
      const offset = spread * config.spreadMultiplier.midpoint;
      const limitPrice = isBuy
        ? midPrice - offset
        : midPrice + offset;
      
      return {
        symbol,
        direction: isBuy ? 'buy' : 'sell',
        orderType: 'limit',
        limitPrice: roundToTick(limitPrice, symbol),
        size: positionSize,
        timeInForce: config.timeInForce.midpoint,
        aggressionLevel,
        ev,
        expectedFillProbability: 0.6,
      };
    }
    
    case 'aggressive_market': {
      return {
        symbol,
        direction: isBuy ? 'buy' : 'sell',
        orderType: 'market',
        limitPrice: null,
        size: positionSize,
        timeInForce: config.timeInForce.aggressive,
        aggressionLevel,
        ev,
        expectedFillProbability: 0.99,
      };
    }
  }
}

function roundToTick(price: number, symbol: string): number {
  // HyperLiquid tick sizes vary by asset
  // BTC: 0.1, ETH: 0.01, most alts: 0.0001
  const tickSizes: Record<string, number> = {
    BTC: 0.1, ETH: 0.01, SOL: 0.01,
  };
  const tick = tickSizes[symbol] ?? 0.001;
  return Math.round(price / tick) * tick;
}
```

### 5.3 HyperLiquid-Specific Considerations

HyperLiquid uses **maker/taker fee model**:
- Maker: **-0.01%** (rebate)
- Taker: **+0.02%** (fee)

This makes passive limit orders doubly valuable:
1. Better entry price (offset from mid)
2. Maker rebate of 0.01% (1 basis point)

**Revised EV with maker/taker fees:**
```
For passive/midpoint limit (maker):
  EV_adjusted = EV + (position_value × 0.0001)   // +1bp maker rebate

For aggressive/market (taker):
  EV_adjusted = EV - (position_value × 0.0002)   // -2bp taker fee
```

This means the aggression threshold should lean slightly more passive on HyperLiquid because the maker rebate adds to EV.

### 5.4 POST_ONLY Flag for Passive Orders

HyperLiquid supports `POST_ONLY` orders that are rejected if they would take liquidity. This is ideal for the passive limit tier:

```typescript
// When EV > 0.40, use POST_ONLY to ensure maker execution
{
  orderType: 'limit',
  limitPrice: midPrice - offset,
  postOnly: true,           // Reject if it would cross
  timeInForce: 'Gtc',
}
```

---

## 6. Per-Asset-Class Considerations

### 6.1 Crypto (HyperLiquid Perpetuals) — Primary Focus

| Factor | Details | Implementation |
|---|---|---|
| **Market Hours** | 24/7, no breaks | Continuous monitoring; no session logic needed |
| **L2 Data** | Real-time via `getL2Book()`, WebSocket available | Direct integration with `hyperliquid-client.ts` |
| **Tick Size** | Asset-dependent (BTC: 0.1, ETH: 0.01) | `roundToTick()` in aggression dispatcher |
| **Leverage** | Up to 50x (asset-dependent) | Factor into position sizing: `notional = size × leverage` |
| **Funding Rate** | 8-hour cycles | Include in EV as carry cost: `EV -= funding_rate × position_duration` |
| **Maker/Taker** | -1bp / +2bp | Aggression scale favors passive execution |
| **Slippage** | Low for majors (BTC/ETH), higher for alts | Dynamic slippage estimate from L2 book depth |
| **Markov Brick Size** | Asset-specific ATR | Use ATR-based brick sizing: `brick_size = ATR(14) × 0.5` |

**Funding Rate Adjustment to EV:**
```typescript
// Fetch funding rate from HyperLiquid
async function getFundingRate(coin: string): Promise<number> {
  const response = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    body: JSON.stringify({ type: 'metaAndAssetCtxs' }),
  });
  const data = await response.json();
  // Parse funding rate for the coin
  return data[1]?.[coinIndex]?.funding ?? 0;
}

// Adjust EV for funding cost (for longs, positive funding = cost)
function adjustEVForFunding(ev: number, direction: 'long' | 'short', fundingRate: number, hoursToHold: number): number {
  const fundingCost = fundingRate * hoursToHold / 8;  // funding charged every 8h
  return direction === 'long' ? ev - fundingCost : ev + fundingCost;
}
```

### 6.2 Stocks (Yahoo Finance / Alpaca)

| Factor | Details | Implementation |
|---|---|---|
| **Market Hours** | 09:30-16:00 ET, Mon-Fri | Gate entry signals: only compute EV during market hours |
| **L2 Data** | Via Alpaca or broker API | Replace `hyperliquidClient.getL2Book()` with `alpacaClient.getQuotes()` |
| **Tick Size** | $0.01 (most stocks) | Fixed tick rounding |
| **Short Selling** | Hard-to-borrow, borrow fees | `is_hard_to_borrow` flag in `RenkoBacktestRequest` already exists |
| **Reg T Margin** | 2x overnight, 4x intraday | Cap position at margin limit |
| **Earnings/Events** | Gap risk | Reduce EV by earnings gap probability near dates |
| **Markov Brick Size** | ATR-based | Same ATR logic, but need overnight gap handling |

**Key adaptation:** Replace `hyperliquidClient` with `alpacaClient` or `yahooClient` for data fetching. The EV engine, Bayesian updater, and aggression dispatcher remain asset-agnostic.

```typescript
// Stock-specific data adapter interface
interface MarketDataAdapter {
  getOHLCV(symbol: string, interval: string, bars: number): Promise<OHLCVData>;
  getL2Book(symbol: string): Promise<L2Snapshot>;
  getMidPrice(symbol: string): Promise<number>;
  getFundingRate?(symbol: string): Promise<number>; // Not applicable for stocks
  isMarketOpen(): Promise<boolean>;
  getTickSize(symbol: string): number;
}
```

### 6.3 Forex (OANDA / IB)

| Factor | Details | Implementation |
|---|---|---|
| **Market Hours** | 24/5 (Sun 17:00 – Fri 17:00 ET) | Session gate; reduced EV outside London/NY overlap |
| **L2 Data** | Limited (ECN depth from broker) | Use bid/ask spread as proxy for imbalance |
| **Sessions** | Asian, London, NY | Adjust P_win by session: London/NY overlap = highest volatility |
| **Leverage** | 50:1 (US), up to 500:1 (offshore) | Factor into Kelly sizing |
| **Carry** | Interest rate differential | Add to EV like crypto funding rate |
| **Markov Brick Size** | Pips-based | `brick_size = ATR(14) × 0.5` in pips |

**Session-based EV adjustment:**
```typescript
function adjustEVForSession(ev: number, hourUTC: number): number {
  // London/NY overlap (13:00-17:00 UTC) = best liquidity
  // Asian session (00:00-08:00 UTC) = lower volume
  const sessionMultiplier = 
    (hourUTC >= 13 && hourUTC < 17) ? 1.0 :   // Full EV
    (hourUTC >= 8  && hourUTC < 13) ? 0.85 :   // Moderate
    (hourUTC >= 0  && hourUTC < 8)  ? 0.7  :   // Reduced
    0.5;                                        // Off-hours
  return ev * sessionMultiplier;
}
```

### 6.4 Commodities (Futures Roll)

| Factor | Details | Implementation |
|---|---|---|
| **Contract Expiry** | Monthly/quarterly | Roll to next contract 5 days before expiry |
| **Roll Cost** | Contango/backwardation | Adjust EV: `EV -= roll_cost` if holding across roll |
| **Market Hours** | Nearly 24h with breaks | Session gates with break handling |
| **L2 Data** | CME Globex depth | Broker-specific adapter |
| **Tick Size** | Contract-specific | ES: 0.25, CL: 0.01, GC: 0.10 |
| **Margin** | CME span margin | Factor into position sizing |

### 6.5 Per-Asset-Class Aggression Config Defaults

```typescript
const ASSET_CLASS_DEFAULTS: Record<string, AggressionConfig> = {
  crypto: {
    spreadMultiplier: { passive: 1.5, midpoint: 0.5, aggressive: 0.0 },
    timeInForce: { passive: 'POST_ONLY', midpoint: 'GTC', aggressive: 'IOC' },
    passiveTimeout: 60000,
    fallbackOnTimeout: true,
  },
  stocks: {
    spreadMultiplier: { passive: 2.0, midpoint: 0.5, aggressive: 0.0 },
    timeInForce: { passive: 'GTC', midpoint: 'GTC', aggressive: 'IOC' },
    passiveTimeout: 300000,  // 5 min (slower market)
    fallbackOnTimeout: true,
  },
  forex: {
    spreadMultiplier: { passive: 1.0, midpoint: 0.3, aggressive: 0.0 },
    timeInForce: { passive: 'GTC', midpoint: 'GTC', aggressive: 'IOC' },
    passiveTimeout: 120000,
    fallbackOnTimeout: true,
  },
  commodities: {
    spreadMultiplier: { passive: 2.0, midpoint: 0.5, aggressive: 0.0 },
    timeInForce: { passive: 'GTC', midpoint: 'GTC', aggressive: 'IOC' },
    passiveTimeout: 180000,
    fallbackOnTimeout: true,
  },
};
```

---

## 7. Implementation Phases

### Phase 1: Signal Pipeline Foundation (Week 1)

**Goal**: Wire up live data → Markov chain → EV computation

| Task | Deliverable | Files |
|---|---|---|
| 1.1 | `MarkovChain` class with warmup, observe, getMatrix | `src/lib/markov-chain.ts` |
| 1.2 | `EVEngine` with computeEV, P_regime, P_imbalance, P_markov | `src/lib/ev-engine.ts` |
| 1.3 | `BayesianUpdater` with sequential updates | `src/lib/bayesian-updater.ts` |
| 1.4 | Unit tests for all three core modules | `src/lib/__tests__/markov-chain.test.ts`, `ev-engine.test.ts`, `bayesian-updater.test.ts` |
| 1.5 | Warmup script: load 500 bars from HyperLiquid → warm Markov chain | `src/scripts/warmup-markov.ts` |
| 1.6 | Zustand store for live signal state | `src/stores/signal-store.ts` |

**Validation**: Given 500 bars of BTC 1h data, Markov chain converges to stable transition matrix within ±0.05 of theoretical probabilities. EV computation produces sensible values (0.0–1.0 range for normalized EV).

### Phase 2: Live Signal Monitor (Week 2)

**Goal**: Real-time dashboard showing live signals, EV, and Bayesian posteriors

| Task | Deliverable | Files |
|---|---|---|
| 2.1 | WebSocket connection to HyperLiquid L2 book | `src/lib/hyperliquid-ws.ts` |
| 2.2 | Live signal loop: poll regime + L2 → compute EV | `src/lib/signal-loop.ts` |
| 2.3 | `SignalMonitor` dashboard component | `src/components/signal/signal-monitor.tsx` |
| 2.4 | `EVMeter` gauge component (visual EV readout) | `src/components/signal/ev-meter.tsx` |
| 2.5 | `MarkovMatrix` heatmap component | `src/components/signal/markov-matrix.tsx` |
| 2.6 | `BayesianTrace` line chart (prior→posterior over time) | `src/components/signal/bayesian-trace.tsx` |
| 2.7 | Backend endpoint: `/signal/ev` (POST) | FastAPI new endpoint |
| 2.8 | Backend endpoint: `/signal/markov` (POST) | FastAPI new endpoint |

**Validation**: Open dashboard for BTC, see live EV computation updating every bar close with regime + L2 + Markov inputs.

### Phase 3: Aggression & Execution (Week 3)

**Goal**: Map EV to order type, dispatch to HyperLiquid

| Task | Deliverable | Files |
|---|---|---|
| 3.1 | `AggressionDispatcher` with per-asset configs | `src/lib/aggression-dispatcher.ts` |
| 3.2 | `Executor` interface + HyperLiquid implementation | `src/lib/executor.ts` |
| 3.3 | HyperLiquid order placement API (private endpoints) | `src/lib/hyperliquid-order-client.ts` |
| 3.4 | `ExecutionPanel` UI component | `src/components/execution/execution-panel.tsx` |
| 3.5 | `OrderHistory` table component | `src/components/execution/order-history.tsx` |
| 3.6 | Paper trading mode (log without executing) | `src/lib/paper-executor.ts` |
| 3.7 | Backend endpoint: `/execution/dispatch` (POST) | FastAPI new endpoint |

**Validation**: Run in paper mode for 24h. Verify aggression mapping is correct (high EV → passive limit, low EV → aggressive). No real orders placed.

### Phase 4: Risk Integration & Kelly Bridge (Week 4)

**Goal**: Full risk management loop with dynamic Kelly sizing from EV

| Task | Deliverable | Files |
|---|---|---|
| 4.1 | `DynamicKellyBridge`: pass EV-adjusted P_win to `/size/kelly` | `src/lib/dynamic-kelly.ts` |
| 4.2 | `RiskGate`: pre-execution risk checks | `src/lib/risk-gate.ts` |
| 4.3 | Drawdown circuit breaker | `src/lib/risk-gate.ts` |
| 4.4 | `RiskDashboard` live component (current DD, VaR, limits) | `src/components/risk/risk-dashboard.tsx` |
| 4.5 | Backend endpoint: `/size/kelly-ev` (POST, accepts P_win from EV) | FastAPI new endpoint |
| 4.6 | Backend endpoint: `/risk/check` (POST, pre-trade risk check) | FastAPI new endpoint |
| 4.7 | Position tracking store | `src/stores/position-store.ts` |

**Validation**: Paper trade for 1 week with risk gates active. Verify that drawdown circuit breaker fires when DD exceeds threshold. Verify Kelly sizes scale with EV.

### Phase 5: Multi-Asset & Production Hardening (Week 5-6)

**Goal**: Production-ready system with multi-asset support, monitoring, and fail-safes

| Task | Deliverable | Files |
|---|---|---|
| 5.1 | `MarketDataAdapter` interface + crypto/stock/forex implementations | `src/lib/adapters/*.ts` |
| 5.2 | Multi-symbol signal loop (parallel EV for BTC + ETH + SOL) | `src/lib/signal-loop.ts` (extended) |
| 5.3 | `MultiSignalGrid` component | `src/components/signal/multi-signal-grid.tsx` |
| 5.4 | Persistence: Markov chain state to SQLite via Prisma | `src/lib/markov-persistence.ts` |
| 5.5 | Alert system: email/webhook on high-EV signals | `src/lib/alerts.ts` |
| 5.6 | Health check endpoint + uptime monitoring | FastAPI + frontend |
| 5.7 | Comprehensive logging (structured JSON) | `src/lib/logger.ts` |
| 5.8 | Rate limiting and error recovery | `src/lib/signal-loop.ts` |
| 5.9 | End-to-end integration test | `src/__tests__/integration/` |
| 5.10 | Documentation and runbook | `docs/` |

**Validation**: Run full paper trading for 2 weeks across 3+ symbols. No crashes, no stale data, all risk gates functioning.

---

## 8. New Backend Endpoints Needed

### 8.1 Signal Computation Endpoints

#### `POST /signal/ev`

Compute Expected Value for a potential trade.

```python
# Request
{
  "symbol": "BTC",
  "direction": "long",
  "prices": [95000, 95100, ...],          # Recent close prices
  "regime_probs": {"BULLISH": 0.72, "BEARISH": 0.18, "NEUTRAL": 0.10},
  "imbalance_confidence": 0.65,
  "imbalance_signal": "long",
  "imbalance_persistence": 5,
  "markov_p_up": 0.42,                    # P(UP | current_state) from Markov
  "markov_p_down": 0.30,
  "markov_p_flat": 0.28,
  "tp_bricks": 5,
  "sl_bricks": 3,
  "brick_size": 0.5,
  "weights": {"regime": 0.4, "imbalance": 0.3, "markov": 0.3}
}

# Response
{
  "ev": 0.73,
  "ev_normalized": 0.244,
  "p_win": 0.65,
  "p_regime": 0.77,
  "p_imbalance": 0.68,
  "p_markov": 0.42,
  "aggression": "passive_limit",
  "profit": 2.5,
  "stake": 1.5,
  "limit_offset": 0.75
}
```

#### `POST /signal/markov`

Update Markov transition matrix and get transition probabilities.

```python
# Request
{
  "symbol": "BTC",
  "brick_size": 0.5,
  "prices": [95000, 95100, ...],   # Prices to warm up / update
  "action": "warmup"               # "warmup" | "update" | "query"
}

# Response
{
  "symbol": "BTC",
  "matrix": {
    "UP":   {"UP": 0.52, "DOWN": 0.20, "FLAT": 0.28},
    "DOWN": {"UP": 0.25, "DOWN": 0.50, "FLAT": 0.25},
    "FLAT": {"UP": 0.35, "DOWN": 0.30, "FLAT": 0.35}
  },
  "current_state": "UP",
  "observations": 499,
  "transition_probs": {
    "UP":   0.52,
    "DOWN": 0.20,
    "FLAT": 0.28
  }
}
```

#### `POST /signal/bayesian-update`

Apply Bayesian update with new evidence.

```python
# Request
{
  "prior_p_win": 0.55,
  "evidence": [
    {"p_b_given_a": 0.80, "p_b_given_not_a": 0.25, "source": "imbalance"},
    {"p_b_given_a": 0.70, "p_b_given_not_a": 0.40, "source": "regime"}
  ]
}

# Response
{
  "posterior_p_win": 0.82,
  "updates": [
    {"source": "imbalance", "prior": 0.55, "posterior": 0.74, "bayes_factor": 3.2, "magnitude": 0.19},
    {"source": "regime", "prior": 0.74, "posterior": 0.82, "bayes_factor": 1.75, "magnitude": 0.08}
  ]
}
```

### 8.2 Position Sizing Endpoint

#### `POST /size/kelly-ev`

Kelly Criterion sizing that accepts a dynamically computed P_win (from EV engine) instead of using only historical win rate.

```python
# Request
{
  "symbol": "BTC",
  "p_win": 0.65,              # From EV engine (not historical)
  "reward_risk": 1.667,       # tp_bricks / sl_bricks
  "capital": 100000,
  "kelly_fraction": 0.5,      # Half-Kelly by default
  "target_vol": 0.15,
  "current_vol": 0.22,
  "regime_risk_multiplier": 0.8,
  "max_position_pct": 0.25
}

# Response
{
  "symbol": "BTC",
  "full_kelly": 0.267,       # f = (p*b - q) / b where b = reward_risk
  "half_kelly": 0.134,
  "position_size_usd": 13400,
  "position_pct": 0.134,
  "risk_multiplier": 0.8,
  "vol_adjusted_size": 9126,  # Scaled by target_vol / current_vol
  "final_size_usd": 7301,    # After all adjustments
  "final_pct": 0.073,
  "leverage_required": 1.0
}
```

### 8.3 Risk Check Endpoint

#### `POST /risk/check`

Pre-trade risk validation. Returns GO/NO-GO decision.

```python
# Request
{
  "symbol": "BTC",
  "direction": "long",
  "position_size_usd": 7301,
  "ev": 0.73,
  "current_portfolio": {
    "equity": 100000,
    "open_positions": 2,
    "current_drawdown_pct": 3.2,
    "daily_pnl": -500,
    "margin_used_pct": 15
  },
  "limits": {
    "max_drawdown_pct": 10,
    "max_daily_loss": 2000,
    "max_open_positions": 5,
    "max_margin_pct": 50,
    "min_ev": 0.0
  }
}

# Response
{
  "approved": true,
  "checks": {
    "ev_positive": {"pass": true, "value": 0.73},
    "drawdown_within_limit": {"pass": true, "value": 3.2, "limit": 10},
    "daily_loss_within_limit": {"pass": true, "value": -500, "limit": -2000},
    "positions_within_limit": {"pass": true, "value": 2, "limit": 5},
    "margin_within_limit": {"pass": true, "value": 15, "limit": 50}
  },
  "reduced_size": null,       # If any check was close to limit
  "warnings": []
}
```

### 8.4 Execution Endpoint

#### `POST /execution/dispatch`

Dispatch an order to the exchange with aggression-mapped pricing.

```python
# Request
{
  "symbol": "BTC",
  "direction": "long",
  "size_usd": 7301,
  "aggression": "passive_limit",
  "mid_price": 95000,
  "spread": 5.0,              # best_ask - best_bid
  "limit_offset": 0.75,       # From EV engine
  "time_in_force": "POST_ONLY",
  "paper_mode": true          # Set false for live
}

# Response
{
  "order_id": "paper_20250101_001",
  "status": "submitted",
  "symbol": "BTC",
  "direction": "buy",
  "order_type": "limit",
  "limit_price": 94992.5,     # mid - offset
  "size": 0.0769,             # BTC units
  "size_usd": 7301,
  "aggression": "passive_limit",
  "ev": 0.73,
  "paper_mode": true,
  "timestamp": "2025-01-01T00:00:00Z"
}
```

---

## 9. New Frontend Components

### 9.1 Signal Monitor Dashboard

New tab in the main dashboard: **"Live Signals"**

```
┌──────────────────────────────────────────────────────────────────────┐
│ [Backtest] [Live Signals] [Risk] [Execution]                        │
├──────────────────────────────────────────────────────────────────────┤
│ Symbol: [BTC ▼]  │  Status: 🟢 LIVE  │  Last Update: 12:34:56 UTC │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                    EV METER (Gauge)                             │ │
│  │         ████████████████░░░░░░░░░                               │ │
│  │         EV: 0.73  │  Aggression: PASSIVE LIMIT                 │ │
│  │         P_win: 0.65 │ Direction: LONG                          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌────────────────────┐  ┌────────────────────┐  ┌───────────────┐  │
│  │ REGIME DETECTION   │  │ L2 IMBALANCE       │  │ MARKOV CHAIN  │  │
│  │ BULLISH: 0.72      │  │ Imbalance: +0.65   │  │ UP:   0.42    │  │
│  │ BEARISH: 0.18      │  │ Signal: LONG       │  │ DOWN: 0.30    │  │
│  │ NEUTRAL: 0.10      │  │ Persistence: 5     │  │ FLAT: 0.28    │  │
│  │ P_regime: 0.77     │  │ P_imbalance: 0.68  │  │ P_markov: 0.42│  │
│  └────────────────────┘  └────────────────────┘  └───────────────┘  │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ BAYESIAN TRACE (Line Chart)                                    │ │
│  │ Prior: 0.42 → +Imbalance: 0.74 → +Regime: 0.82               │ │
│  │ ─────○────────○─────────○──────────○                           │ │
│  │ 0.0  0.42    0.55      0.74      0.82                          │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ MARKOV TRANSITION MATRIX (Heatmap)                             │ │
│  │          UP    DOWN   FLAT                                      │ │
│  │  UP   [0.52  0.20   0.28]                                      │ │
│  │  DOWN [0.25  0.50   0.25]                                      │ │
│  │  FLAT [0.35  0.30   0.35]                                      │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │ SIGNAL HISTORY (Recent)                                        │ │
│  │ Time     │ Symbol │ Dir  │ EV   │ Aggression │ Status          │ │
│  │ 12:30    │ BTC    │ LONG │ 0.73 │ PASSIVE    │ Filled @ 94992  │ │
│  │ 12:15    │ ETH    │ LONG │ 0.28 │ MIDPOINT   │ Pending         │ │
│  │ 11:45    │ SOL    │ SHORT│-0.05 │ —          │ SKIPPED (EV<0)  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### 9.2 Component File Plan

| Component | File | Description |
|---|---|---|
| `SignalMonitor` | `src/components/signal/signal-monitor.tsx` | Main live signal tab container |
| `EVMeter` | `src/components/signal/ev-meter.tsx` | Visual gauge showing EV, P_win, aggression level |
| `RegimePanel` | `src/components/signal/regime-panel.tsx` | Live regime probabilities + P_regime contribution |
| `ImbalancePanel` | `src/components/signal/imbalance-panel-live.tsx` | Live L2 imbalance + P_imbalance contribution |
| `MarkovMatrix` | `src/components/signal/markov-matrix.tsx` | 3x3 heatmap of transition probabilities |
| `BayesianTrace` | `src/components/signal/bayesian-trace.tsx` | Prior → posterior update visualization |
| `SignalHistory` | `src/components/signal/signal-history.tsx` | Table of recent signals with EV, aggression, status |
| `MultiSignalGrid` | `src/components/signal/multi-signal-grid.tsx` | Grid of EV meters for multiple symbols |
| `ExecutionPanel` | `src/components/execution/execution-panel.tsx` | Order dispatch controls + paper/live toggle |
| `OrderHistory` | `src/components/execution/order-history.tsx` | Table of executed orders with fill info |
| `RiskDashboard` | `src/components/risk/risk-dashboard-live.tsx` | Live DD, VaR, position limits, circuit breakers |
| `PositionTracker` | `src/components/risk/position-tracker.tsx` | Open positions with unrealized P/L |

### 9.3 New Zustand Stores

**Signal Store** (`src/stores/signal-store.ts`):
```typescript
interface SignalState {
  // Per-symbol signal data
  signals: Record<string, {
    ev: number;
    pWin: number;
    pRegime: number;
    pImbalance: number;
    pMarkov: number;
    aggression: AggressionLevel;
    direction: 'long' | 'short' | 'flat';
    regimeProbs: Record<string, number>;
    imbalanceSignal: ImbalanceSignal | null;
    markovState: PriceState;
    markovMatrix: TransitionMatrix | null;
    lastUpdate: number;
  }>;
  
  // Bayesian trace
  bayesianHistory: Record<string, BayesianPosterior[]>;
  
  // Signal loop status
  isRunning: boolean;
  intervalMs: number;
  
  // Actions
  updateSignal: (symbol: string, data: Partial<SignalData>) => void;
  startLoop: () => void;
  stopLoop: () => void;
}
```

**Position Store** (`src/stores/position-store.ts`):
```typescript
interface PositionState {
  positions: Record<string, {
    symbol: string;
    direction: 'long' | 'short';
    entryPrice: number;
    size: number;
    sizeUsd: number;
    unrealizedPnl: number;
    ev: number;
    aggression: AggressionLevel;
    openedAt: number;
  }>;
  
  orderHistory: OrderRecord[];
  
  // Risk state
  currentDrawdown: number;
  dailyPnl: number;
  maxDrawdown: number;
  
  // Paper/Live mode
  mode: 'paper' | 'live';
  
  // Actions
  openPosition: (position: Position) => void;
  closePosition: (symbol: string, exitPrice: number) => void;
  setMode: (mode: 'paper' | 'live') => void;
}
```

---

## 10. Risk Management Integration

### 10.1 How Kelly, Regime Gates, and Drawdown Limits Work Together

The risk management system operates as a **layered defense**:

```
Layer 1: EV FILTER
  ┌─────────────────────────────────────────┐
  │ IF EV <= 0: NO TRADE                    │
  │ No edge = no entry, regardless of other │
  │ factors.                                │
  └────────────────┬────────────────────────┘
                   │
Layer 2: REGIME GATE
  ┌────────────────▼────────────────────────┐
  │ IF regime_gate = true:                  │
  │   LONG requires P(BULLISH) > threshold  │
  │   SHORT requires P(BEARISH) > threshold │
  │ Default threshold: 0.40                 │
  │ Already implemented in renkoParams.      │
  └────────────────┬────────────────────────┘
                   │
Layer 3: KELLY SIZING (EV-adjusted)
  ┌────────────────▼────────────────────────┐
  │ f = (p × b - q) / b                    │
  │ where:                                  │
  │   p = EV_posterior (from Bayesian)      │
  │   b = reward_risk = tp_bricks/sl_bricks │
  │   q = 1 - p                            │
  │                                         │
  │ position_size = equity × f × kelly_frac │
  │ kelly_frac = 0.5 (half-Kelly default)  │
  │                                         │
  │ Additional scaling:                     │
  │   × (target_vol / current_vol)  [vol]   │
  │   × regime_risk_multiplier      [regime]│
  │   × (1 - current_dd/max_dd)     [DD]   │
  └────────────────┬────────────────────────┘
                   │
Layer 4: POSITION LIMITS
  ┌────────────────▼────────────────────────┐
  │ max_position_pct:   ≤ 25% of equity    │
  │ max_open_positions: ≤ 5                 │
  │ max_margin_pct:     ≤ 50%              │
  │ min_ev:             > 0 (hard floor)    │
  └────────────────┬────────────────────────┘
                   │
Layer 5: DRAWDOWN CIRCUIT BREAKER
  ┌────────────────▼────────────────────────┐
  │ IF current_drawdown > 5%:               │
  │   Reduce all sizes by 50%               │
  │ IF current_drawdown > 8%:               │
  │   Only take EV > 0.40 (passive only)   │
  │ IF current_drawdown > 10%:              │
  │   HALT ALL TRADING                      │
  │   Require manual reset                  │
  └────────────────┬────────────────────────┘
                   │
Layer 6: DAILY LOSS LIMIT
  ┌────────────────▼────────────────────────┐
  │ IF daily_pnl < -2% of equity:           │
  │   Reduce position sizes to 25%          │
  │ IF daily_pnl < -3% of equity:           │
  │   HALT TRADING until next day           │
  └─────────────────────────────────────────┘
```

### 10.2 Risk Gate Implementation

```typescript
// File: src/lib/risk-gate.ts

export interface RiskLimits {
  maxDrawdownPct: number;         // default: 10
  maxDailyLossPct: number;        // default: 3
  maxOpenPositions: number;       // default: 5
  maxPositionPct: number;         // default: 25
  maxMarginPct: number;           // default: 50
  minEv: number;                  // default: 0
  drawdownReducePct: number;      // default: 5 (start reducing)
  drawdownHaltPct: number;        // default: 10 (halt all)
  dailyLossReducePct: number;     // default: 2 (start reducing)
  dailyLossHaltPct: number;       // default: 3 (halt all)
}

export interface PortfolioState {
  equity: number;
  openPositions: number;
  currentDrawdownPct: number;
  dailyPnlPct: number;
  marginUsedPct: number;
}

export type RiskDecision = 'go' | 'reduce' | 'halt';

export interface RiskCheckResult {
  decision: RiskDecision;
  sizeMultiplier: number;         // 1.0 = full size, 0.5 = half, 0 = no trade
  evFloorOverride: number | null; // If set, only trade if EV > this
  checks: Record<string, { pass: boolean; value: number; limit: number }>;
  warnings: string[];
}

export function checkRiskGate(
  ev: number,
  direction: 'long' | 'short',
  proposedSizeUsd: number,
  portfolio: PortfolioState,
  limits: RiskLimits
): RiskCheckResult {
  const checks: Record<string, { pass: boolean; value: number; limit: number }> = {};
  const warnings: string[] = [];
  let sizeMultiplier = 1.0;
  let evFloorOverride: number | null = null;

  // Check 1: EV positive
  checks['ev_positive'] = { pass: ev > limits.minEv, value: ev, limit: limits.minEv };

  // Check 2: Drawdown
  if (portfolio.currentDrawdownPct > limits.drawdownHaltPct) {
    checks['drawdown'] = { pass: false, value: portfolio.currentDrawdownPct, limit: limits.drawdownHaltPct };
    return { decision: 'halt', sizeMultiplier: 0, evFloorOverride: null, checks, warnings: ['HALTED: Drawdown exceeds limit'] };
  } else if (portfolio.currentDrawdownPct > limits.drawdownReducePct) {
    const ddRatio = (portfolio.currentDrawdownPct - limits.drawdownReducePct) / 
                    (limits.drawdownHaltPct - limits.drawdownReducePct);
    sizeMultiplier *= (1 - 0.75 * ddRatio);  // Reduce 0-75% as DD approaches halt
    evFloorOverride = 0.15 + 0.25 * ddRatio;  // Require higher EV as DD grows
    warnings.push(`Drawdown reducing size to ${(sizeMultiplier * 100).toFixed(0)}%`);
  }
  checks['drawdown'] = { pass: portfolio.currentDrawdownPct <= limits.drawdownHaltPct, value: portfolio.currentDrawdownPct, limit: limits.drawdownHaltPct };

  // Check 3: Daily loss
  if (portfolio.dailyPnlPct < -limits.dailyLossHaltPct) {
    checks['daily_loss'] = { pass: false, value: portfolio.dailyPnlPct, limit: -limits.dailyLossHaltPct };
    return { decision: 'halt', sizeMultiplier: 0, evFloorOverride: null, checks, warnings: ['HALTED: Daily loss limit reached'] };
  } else if (portfolio.dailyPnlPct < -limits.dailyLossReducePct) {
    sizeMultiplier *= 0.25;  // Reduce to 25%
    warnings.push('Daily loss reducing size to 25%');
  }
  checks['daily_loss'] = { pass: portfolio.dailyPnlPct >= -limits.dailyLossHaltPct, value: portfolio.dailyPnlPct, limit: -limits.dailyLossHaltPct };

  // Check 4: Open positions
  checks['positions'] = { pass: portfolio.openPositions < limits.maxOpenPositions, value: portfolio.openPositions, limit: limits.maxOpenPositions };

  // Check 5: Position size
  const proposedPct = (proposedSizeUsd / portfolio.equity) * 100;
  checks['position_size'] = { pass: proposedPct <= limits.maxPositionPct, value: proposedPct, limit: limits.maxPositionPct };

  // Check 6: Margin
  checks['margin'] = { pass: portfolio.marginUsedPct < limits.maxMarginPct, value: portfolio.marginUsedPct, limit: limits.maxMarginPct };

  // Check 7: EV floor (if overridden by drawdown)
  if (evFloorOverride !== null && ev < evFloorOverride) {
    warnings.push(`EV ${ev.toFixed(2)} below floor ${evFloorOverride.toFixed(2)} due to drawdown`);
    return { decision: 'halt', sizeMultiplier: 0, evFloorOverride, checks, warnings };
  }

  // Final decision
  const allPass = Object.values(checks).every(c => c.pass);
  const decision: RiskDecision = allPass ? (sizeMultiplier < 1 ? 'reduce' : 'go') : 'halt';

  return { decision, sizeMultiplier, evFloorOverride, checks, warnings };
}
```

### 10.3 Dynamic Kelly Sizing Flow

The current `/size/kelly` endpoint uses historical win-rate. We modify the flow:

```
CURRENT FLOW (static Kelly):
  backtest → win_rate = 0.58 → kelly_size(position)

NEW FLOW (dynamic Kelly from EV):
  live_signals → P_win = 0.65 (from EV engine)
                      → kelly_size(position, p_win_override=0.65)
```

**Bridge implementation:**
```typescript
// File: src/lib/dynamic-kelly.ts

export interface DynamicKellyInput {
  pWin: number;               // From EV engine (Bayesian posterior)
  rewardRisk: number;         // tp_bricks / sl_bricks
  equity: number;
  kellyFraction: number;      // 0.5 for half-Kelly
  targetVol: number;
  currentVol: number;
  regimeRiskMultiplier: number;
  currentDrawdownPct: number;
  maxDrawdownPct: number;
  maxPositionPct: number;
}

export interface DynamicKellyResult {
  fullKelly: number;
  halfKelly: number;
  positionSizeUsd: number;
  positionPct: number;
  volAdjustment: number;
  regimeAdjustment: number;
  ddAdjustment: number;
  finalSizeUsd: number;
  finalPct: number;
}

export function computeDynamicKelly(input: DynamicKellyInput): DynamicKellyResult {
  const p = input.pWin;
  const q = 1 - p;
  const b = input.rewardRisk;
  
  // Kelly formula: f = (p × b - q) / b
  const fullKelly = Math.max(0, (p * b - q) / b);
  const halfKelly = fullKelly * input.kellyFraction;
  
  // Base position
  let positionPct = halfKelly;
  
  // Vol adjustment: scale by target/current vol
  const volAdjustment = input.currentVol > 0 
    ? Math.min(1.0, input.targetVol / input.currentVol) 
    : 1.0;
  positionPct *= volAdjustment;
  
  // Regime adjustment
  positionPct *= input.regimeRiskMultiplier;
  
  // Drawdown adjustment: reduce as DD grows
  const ddAdjustment = input.maxDrawdownPct > 0
    ? Math.max(0.1, 1 - (input.currentDrawdownPct / input.maxDrawdownPct) * 0.75)
    : 1.0;
  positionPct *= ddAdjustment;
  
  // Cap at max position
  positionPct = Math.min(positionPct, input.maxPositionPct / 100);
  
  const positionSizeUsd = input.equity * positionPct;
  
  return {
    fullKelly,
    halfKelly,
    positionSizeUsd,
    positionPct,
    volAdjustment,
    regimeAdjustment: input.regimeRiskMultiplier,
    ddAdjustment,
    finalSizeUsd: positionSizeUsd,
    finalPct: positionPct,
  };
}
```

### 10.4 Exit Management

Exits are triggered by:

1. **Stop-Loss Hit**: Price breaches `entry_price - (sl_bricks × brick_size)` for longs. Immediate market exit.

2. **Take-Profit Hit**: Price reaches `entry_price + (tp_bricks × brick_size)` for longs. Passive limit exit to capture maker rebate.

3. **Regime Shift**: HMM regime flips against position direction (e.g., BULLISH → BEARISH while long). Immediate exit.

4. **Signal Reversal**: L2 imbalance signal flips direction while in position. Partial exit (50%) or full exit based on confidence.

5. **EV Decay**: Recomputed EV drops below 0. Partial exit proportional to EV decay.

6. **Time Stop**: Position held longer than `avg_hold_bars × 2` from backtest. Exit to free capital.

```typescript
// Exit priority (checked in this order every tick):
// 1. SL hit → immediate market exit
// 2. Regime flip → immediate market exit
// 3. TP hit → passive limit exit (maker rebate)
// 4. EV decay below threshold → partial exit
// 5. Signal reversal → partial/full exit
// 6. Time stop → market exit
```

---

## Appendix A: Formula Reference

| Formula | Expression | Source |
|---|---|---|
| Expected Value | `EV = (P_win × profit) - (P_loss × stake)` | Markov Step 1 |
| Kelly Criterion | `f = (p × b - q) / b` where `b = reward_risk` | Markov Step 2 |
| Bayesian Update | `P(A\|B) = P(B\|A) × P(A) / P(B)` | Markov Step 3 |
| L2 Imbalance | `I = (V_bid - V_ask) / (V_bid + V_ask)` | `l2-imbalance-engine.ts:84` |
| EMA Smoothing | `S_t = α × X_t + (1-α) × S_{t-1}` where `α = 2/(N+1)` | `l2-imbalance-engine.ts:193` |
| Markov Transition | `T[i][j] = count(i→j) / count(i→*)` with Laplace smoothing | `markov-chain.ts` |
| Composite P_win | `P_win = w_r × P_regime + w_i × P_imbalance + w_m × P_markov` | `ev-engine.ts` |
| Vol-Scaled Size | `size × min(1, target_vol / current_vol)` | `dynamic-kelly.ts` |
| DD-Scaled Size | `size × max(0.1, 1 - (current_dd/max_dd) × 0.75)` | `risk-gate.ts` |
| Aggression Mapping | EV > 0.40 → passive, 0.15 < EV ≤ 0.40 → midpoint, EV ≤ 0.15 → aggressive | Markov Step 4 |
| Normalized EV | `EV_norm = P_win × R - (1-P_win)` where `R = profit/stake` | Section 3.4 |

## Appendix B: Existing Code Integration Points

| New Component | Integrates With | Method |
|---|---|---|
| `ev-engine.ts` | `l2-imbalance-engine.ts` | Consumes `ImbalanceSignal` from `generateImbalanceSignals()` |
| `ev-engine.ts` | `noble-trader-client.ts` | Consumes `detectRegimeV2()` response |
| `markov-chain.ts` | `hyperliquid-client.ts` | Consumes `getOHLCV()` for warmup |
| `bayesian-updater.ts` | `ev-engine.ts` | Provides posterior P_win to EV computation |
| `aggression-dispatcher.ts` | `ev-engine.ts` | Consumes `EVResult.aggression` and `limitOffset` |
| `dynamic-kelly.ts` | `noble-trader-client.ts` | Replaces call to `/size/kelly` with EV-adjusted sizing |
| `risk-gate.ts` | `noble-trader-client.ts` | Uses `/risk/dashboard` for portfolio state |
| `signal-loop.ts` | All above | Orchestrates the full pipeline on each tick |
| `signal-store.ts` | `simulation-store.ts` | Reads `renkoParams` and `l2Params` for configuration |
| Signal UI components | Existing `page.tsx` | Add "Live Signals" tab to existing tab layout |

## Appendix C: HyperLiquid WebSocket Integration

The current system uses REST polling (`getL2Book()`, `getOHLCV()`). For live signal monitoring, we need WebSocket subscriptions for real-time L2 book updates:

```typescript
// File: src/lib/hyperliquid-ws.ts

const HL_WS_URL = 'wss://api.hyperliquid.xyz/ws';

export class HyperLiquidWebSocket {
  private ws: WebSocket | null = null;
  private subscriptions: Map<string, (data: any) => void> = new Map();

  connect(): void {
    this.ws = new WebSocket(HL_WS_URL);
    
    this.ws.onopen = () => {
      // Subscribe to L2 book for each tracked symbol
      for (const [symbol, callback] of this.subscriptions) {
        this.ws!.send(JSON.stringify({
          method: 'subscribe',
          subscription: { type: 'l2Book', coin: symbol },
        }));
      }
    };

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const channel = data.channel;
      
      if (channel === 'l2Book') {
        const callback = this.subscriptions.get(data.data.coin);
        if (callback) callback(data.data);
      }
    };

    this.ws.onclose = () => {
      // Reconnect after 5 seconds
      setTimeout(() => this.connect(), 5000);
    };
  }

  subscribeL2(coin: string, callback: (book: HyperLiquidL2Book) => void): void {
    this.subscriptions.set(coin, callback);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        method: 'subscribe',
        subscription: { type: 'l2Book', coin },
      }));
    }
  }

  disconnect(): void {
    this.ws?.close();
  }
}
```

This replaces the polling-based `getL2Book()` for live signal computation while keeping the REST API for backtesting and warmup.
