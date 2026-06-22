# EV Engine + Aggression Dispatcher — Full Architecture Plan

> **⚠️ STATUS: IMPLEMENTED (v7.5.0)**
> This plan has been executed. See CHANGELOG for the v7.5.0 delta.
> Key differences from original plan:
> - Markov chain is a **sizing multiplier** not a P_win blend (was double-counting)
> - Weights are now **40% Regime + 25% External + 35% TimesFM** (log-odds pooled)
> - L2 pipeline is **live** (Alpaca + Hyperliquid) not just planned
> - `p_imbalance` renamed to `p_ext_signal` for honest naming
> - `CalibrationTracker` added with Brier scores and reliability diagrams

## The Short Answer: EV IS Built In, But It Was Crippled

You're right that EV exists in the system. The problem isn't that it's missing — it's that **what's built is a skeleton with hardcoded defaults**. Here's the reality:

| Component | Status | What It Actually Does |
|---|---|---|
| `EVEngine.validate_signal()` (async) | ⚠️ Built but unused | Queries `nt_pattern_stats` for historical win rate — but **the table is likely empty** because `sync_journal_to_stats()` was never called in production |
| `EVEngine.validate_signal_sync()` | ⚠️ Built, used in pipeline | **Hardcoded `p_win = 0.55`** — never queries DB, never checks regime, never checks L2 |
| `computeMarkovProbs()` (Edge) | ⚠️ Built but fake | Returns static values based on regime label strings (bull=0.65, bear=0.35). Not a real Markov transition matrix |
| `markov_p_up/p_dn` in Redis | ⚠️ Written but unread | Edge functions write them; backend EV engine **ignores them entirely** |
| Aggression Dispatcher | ❌ Not built | Only exists in `ENTRY_SIGNAL_PLAN.md` |
| Bayesian Updater | ❌ Not built | Only in the plan |
| Real Markov Chain | ❌ Not built | Only in the plan |

**Bottom line**: The EV "gate" in the live pipeline is just checking `rr >= 2.0` with `p_win = 0.55`. It's an R:R filter wearing an EV costume.

---

## How It SHOULD Work: The Market Pulse View

### The Core Insight

EV isn't a single number — it's a **live composite of market conviction** from three independent signal sources. Each source answers a different question about the trade:

| Signal Source | Question It Answers | Time Horizon | Weight |
|---|---|---|---|
| **HMM Regime** | "Is the MACRO regime supporting this direction?" | Hours–Days | 40% |
| **L2 Imbalance** | "Is the MICRO order flow supporting this direction RIGHT NOW?" | Seconds–Minutes | 30% |
| **Markov Transition** | "Given the current price STATE, what happens NEXT?" | Minutes–Hours | 30% |

```
P_win = 0.40 × P_regime + 0.30 × P_imbalance + 0.30 × P_markov
```

This blended P_win feeds into:
```
EV = (P_win × profit) - (P_loss × stake)
```

And EV then drives **everything downstream** — sizing, execution style, and live Bayesian updates.

---

## Walkthrough: 3 Real Market Scenarios

### Scenario A: "The No-Brainer" — Strong Bull, L2 Support, UP State

**Market conditions**: BTC just broke above 100k. HMM detects `high_vol_strong_bull`. L2 book shows heavy bid imbalance (buyers stacked). Price state is UP (last 3 bars up).

| Source | Raw Data | P Calculation | Result |
|---|---|---|---|
| HMM Regime | BULL=0.78, NEUTRAL=0.12, BEAR=0.10 | `P_regime = 0.78 + 0.5×0.12 = 0.84` | **0.84** |
| L2 Imbalance | signal=long, confidence=0.72, persistence=8 bars | `P_imbalance = 0.72 × (0.6 + 0.4×1.0) = 0.72` | **0.72** |
| Markov | currentState=UP, T[UP][UP]=0.58 | `P_markov = T[UP][UP] = 0.58` | **0.58** |

```
P_win = 0.40 × 0.84 + 0.30 × 0.72 + 0.30 × 0.58 = 0.336 + 0.216 + 0.174 = 0.726

With brick=750, tp=5, sl=2:
  profit = 5 × 750 = $3,750
  stake  = 2 × 750 = $1,500

EV = (0.726 × 3750) - (0.274 × 1500) = 2722 - 411 = $2,311

EV per dollar risked = 2311 / 1500 = 1.54
```

**Dispatcher Action**: `EV = 1.54 >> 0.40` → **PASSIVE LIMIT** (deep entry, capture the spread)
- Place limit 1.5× spread below mid — the edge is so fat we can afford to wait for a better fill
- **Kelly sizing**: Full recommended_f (regime is strongly supportive)
- **Market pulse**: 🟢 Full conviction — macro, micro, and transitional all agree

---

### Scenario B: "The Marginal Call" — Bull Regime, Flat L2, FLAT State

**Market conditions**: ETH is in a bull regime but consolidating. No clear order flow direction. Price is range-bound.

| Source | Raw Data | P Calculation | Result |
|---|---|---|---|
| HMM Regime | BULL=0.55, NEUTRAL=0.30, BEAR=0.15 | `P_regime = 0.55 + 0.5×0.30 = 0.70` | **0.70** |
| L2 Imbalance | signal=flat, confidence=0.15 | `P_imbalance = 0.50` (neutral) | **0.50** |
| Markov | currentState=FLAT, T[FLAT][UP]=0.38 | `P_markov = T[FLAT][UP] = 0.38` | **0.38** |

```
P_win = 0.40 × 0.70 + 0.30 × 0.50 + 0.30 × 0.38 = 0.280 + 0.150 + 0.114 = 0.544

With brick=40, tp=5, sl=2:
  profit = 5 × 40 = $200
  stake  = 2 × 40 = $80

EV = (0.544 × 200) - (0.456 × 80) = 108.8 - 36.5 = $72.30

EV per dollar risked = 72.3 / 80 = 0.90
```

**Dispatcher Action**: `0.15 < EV = 0.90 < 0.40... wait, 0.90 > 0.40` — actually this is still PASSIVE territory.

Let me recalculate with a more marginal setup (tighter R:R):

With brick=40, tp=3, sl=2:
  profit = 3 × 40 = $120
  stake  = 2 × 40 = $80

EV = (0.544 × 120) - (0.456 × 80) = 65.3 - 36.5 = $28.80
EV per dollar risked = 28.8 / 80 = 0.36

**Dispatcher Action**: `0.15 < EV = 0.36 ≤ 0.40` → **MID-POINT LIMIT**
- Place limit at mid-price — balanced between fill probability and entry quality
- **Kelly sizing**: Full recommended_f but EV scaling reduces it: `position_pct × (EV / 0.5) = position_pct × 0.72`
- **Market pulse**: 🟡 Mixed signals — macro supports it but micro and transitional are neutral/weak

---

### Scenario C: "The Trap" — Bear Regime, Bid Imbalance (Fakeout), UP→DOWN Transition

**Market conditions**: SOL pops on news, but HMM says bear regime. L2 shows bid imbalance but it's a liquidity trap (sellers hidden above). Markov shows UP→DOWN transition is likely.

| Source | Raw Data | P Calculation | Result |
|---|---|---|---|
| HMM Regime | BEAR=0.65, NEUTRAL=0.20, BULL=0.15 | `P_regime = 0.15 + 0.5×0.20 = 0.25` | **0.25** |
| L2 Imbalance | signal=long, confidence=0.60, persistence=2 | `P_imbalance = 0.60 × (0.6 + 0.4×0.4) = 0.456` | **0.456** |
| Markov | currentState=UP, T[UP][DOWN]=0.42 | `P_markov = T[UP][DOWN] = 0.42` | **0.42** |

```
P_win = 0.40 × 0.25 + 0.30 × 0.456 + 0.30 × 0.42 = 0.100 + 0.137 + 0.126 = 0.363

With brick=1.50, tp=5, sl=2:
  profit = 5 × 1.50 = $7.50
  stake  = 2 × 1.50 = $3.00

EV = (0.363 × 7.50) - (0.637 × 3.00) = 2.72 - 1.91 = $0.81

EV per dollar risked = 0.81 / 3.00 = 0.27
```

**Dispatcher Action**: `0.15 < EV = 0.27 ≤ 0.40` → **MID-POINT LIMIT**

But wait — this is a LONG signal in a BEAR regime. The EV is barely positive. The current system with `p_win=0.55` would compute:

```
EV_old = (0.55 × 7.50) - (0.45 × 3.00) = 4.125 - 1.35 = $2.775
EV per dollar risked = 2.775 / 3.00 = 0.925  ← looks great!
```

**The current system thinks this trade has EV=0.925 per dollar. The real EV is 0.27.** That's a 3.4× overestimate. This is the exact scenario that produces whipsaw losses — the system enters what looks like a good trade but the macro regime is against it.

With the real EV Engine, this trade would either:
- Get a **much smaller position** (Kelly scaled by EV/0.5 = 0.27/0.5 = 0.54×)
- Or get **rejected entirely** if we set `min_ev = 0.30`

**Market pulse**: 🔴 Danger — regime opposes direction, L2 is a potential fakeout, Markov says reversal likely

---

## The Complete Pipeline: Signal → EV → Size → Execute

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. DATA INGESTION (every bar close / tick)                         │
│                                                                     │
│   OHLCV candles ──→ HMM Regime Detector ──→ regimeProbs            │
│   L2 Book snap  ──→ Imbalance Engine   ──→ ImbalanceSignal         │
│   Price state   ──→ Markov Chain        ──→ transitionMatrix       │
│                                                                     │
│   All three run in parallel, independent data sources               │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ 2. EV COMPUTATION (the "Market Pulse" calculator)                   │
│                                                                     │
│   P_win = 0.40 × P_regime + 0.30 × P_imbalance + 0.30 × P_markov │
│   EV    = (P_win × profit) - (P_loss × stake)                      │
│   EV/RR = EV / stake  (normalized edge per dollar risked)          │
│                                                                     │
│   🟢 EV/RR > 0.40  → "High Conviction"                            │
│   🟡 0.15–0.40     → "Marginal Edge"                               │
│   🔴 < 0.15        → "Thin Edge / Reject"                          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ 3. RISK GATES (existing + EV-augmented)                             │
│                                                                     │
│   ✅ EV > min_ev (default 0.15)          ← NEW: real EV gate      │
│   ✅ R:R >= min_rr (2.0)                 ← EXISTS (in /optimize)   │
│   ✅ Sharpe <= MAX_SHARPE (10.0)         ← EXISTS (in heavy-sweep) │
│   ✅ Trades >= MIN_TRADES (5)            ← EXISTS (in heavy-sweep) │
│   ✅ Regime alignment (HMM gate)         ← EXISTS (signal_filter)  │
│   ✅ Session limits (max trades/day)      ← EXISTS (signal_filter)  │
│   ✅ Drawdown check                       ← EXISTS (position_sizer) │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ 4. POSITION SIZING (EV-scaled Kelly)                                │
│                                                                     │
│   base_kelly = /size/kelly → recommended_f (already regime-gated)  │
│   ev_scale   = min(1.0, EV_per_dollar / 0.50)                     │
│   final_f    = base_kelly × ev_scale                                │
│                                                                     │
│   Example:                                                          │
│     base_kelly = 0.35 (from /size/kelly)                           │
│     EV/dollar  = 0.72 (Scenario A — high conviction)               │
│     ev_scale   = min(1.0, 0.72/0.50) = 1.0  ← full size          │
│     final_f    = 0.35 × 1.0 = 0.35                                 │
│                                                                     │
│     base_kelly = 0.35                                              │
│     EV/dollar  = 0.27 (Scenario C — the trap)                      │
│     ev_scale   = min(1.0, 0.27/0.50) = 0.54  ← cut in half       │
│     final_f    = 0.35 × 0.54 = 0.189                               │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ 5. AGGRESSION DISPATCHER (order type + fill strategy)               │
│                                                                     │
│   ┌──────────────────────────────────────────────────────────────┐ │
│   │ EV > 0.40 → PASSIVE LIMIT                                   │ │
│   │   limit_price = mid - (spread × 1.5)                        │ │
│   │   patience = HIGH (wait for fill up to N bricks)            │ │
│   │   rationale: "Edge is fat, capture the spread"              │ │
│   ├──────────────────────────────────────────────────────────────┤ │
│   │ 0.15 < EV ≤ 0.40 → MID-POINT LIMIT                         │ │
│   │   limit_price = mid - (spread × 0.5)                        │ │
│   │   patience = MEDIUM                                         │ │
│   │   rationale: "Edge is OK, don't be greedy on fill"          │ │
│   ├──────────────────────────────────────────────────────────────┤ │
│   │ EV ≤ 0.15 → AGGRESSIVE / MARKET                             │ │
│   │   market order or limit at best bid/ask                      │ │
│   │   patience = NONE (fill immediately)                        │ │
│   │   rationale: "Edge is thin, can't risk missing it"          │ │
│   └──────────────────────────────────────────────────────────────┘ │
│                                                                     │
│   ⏱️ Time-to-Market (TTM) Decay:                                    │
│   If passive/mid limit not filled within N bricks:                  │
│     1. Tighten limit by 1 tick                                      │
│     2. After M more bricks, convert to market                       │
│     N and M are scaled by HMM regime:                               │
│       high_vol → shorter patience (price moves fast)                │
│       low_vol  → longer patience (price moves slow)                 │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│ 6. BAYESIAN LIVE UPDATE (the "Self-Correcting" layer)               │
│                                                                     │
│   After each bar close, update P_win posterior:                     │
│                                                                     │
│   P(win | new_evidence) = P(evidence | win) × P(win) / P(evidence) │
│                                                                     │
│   If the trade is already open and price moves against us:          │
│     - P(evidence | win) drops → posterior P_win drops              │
│     - If EV drops below min_ev → tighten stop or close early       │
│                                                                     │
│   If price moves with us:                                           │
│     - P(evidence | win) rises → posterior P_win rises              │
│     - Could trail stop more aggressively                            │
│                                                                     │
│   Session decay: if 3+ consecutive losses in same session,          │
│     penalize P_win by 0.10 for remainder of session                │
│     (prevents over-trading a pattern that's currently failing)      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## What's Built vs. What Needs Building

### ✅ Already Built (Can Use Immediately)

| Component | Location | What It Does |
|---|---|---|
| HMM Regime Detection | `/regime/detect-v2` | Returns `vol_probs`, `trend_probs`, `regime_label`, `confidence`, `risk_multiplier` |
| L2 Imbalance Engine | `l2-imbalance-engine.ts` | Returns `ImbalanceSignal` with confidence, persistence, direction |
| Kelly Sizing | `/size/kelly` | Returns `recommended_f` (regime-gated, vol-scaled) |
| Masaniello Sizing | `position_sizer.py` | Dynamic sizing with drawdown/vol/regime factors |
| R:R Filter | `/optimize` + heavy-sweep | `min_rr=2.0` server-side + client-side |
| Sharpe Guard | heavy-sweep | `MAX_SHARPE=10.0` overfit ceiling |
| EV Engine (skeleton) | `ev_engine.py` | Dataclass + async/sync validate — but sync uses hardcoded `p_win=0.55` |
| Redis Config Write | heavy-sweep + light-regime | Writes `trading:config:{symbol}` with all params |

### ❌ Needs Building (The 5 Gaps)

#### Gap 1: Real `P_regime` Computation
**Current**: Edge `computeMarkovProbs()` returns static bull=0.65/bear=0.35
**Needed**: Extract actual HMM posterior probabilities and compute:
```python
P_regime(long)  = P(BULLISH) + 0.5 × P(NEUTRAL)
P_regime(short) = P(BEARISH) + 0.5 × P(NEUTRAL)
```
**Effort**: LOW — the data already comes from `/regime/detect-v2`, just needs to be consumed correctly

#### Gap 2: L2 Imbalance → P_imbalance Bridge
**Current**: L2 imbalance engine exists in the frontend dashboard but is NOT wired into the Edge Function pipeline
**Needed**: Wire L2 book fetch + `computeImbalance()` into heavy-sweep / light-regime, compute:
```python
P_imbalance = confidence × (0.6 + 0.4 × persistence_bonus)  # if aligned
P_imbalance = 0.50  # if flat
P_imbalance = 1.0 - confidence  # if opposed
```
**Effort**: MEDIUM — L2 engine exists, needs API endpoint + Edge Function integration

#### Gap 3: Real Markov Transition Matrix
**Current**: `computeMarkovProbs()` is a static lookup table
**Needed**: `MarkovChain` class that tracks actual UP/DOWN/FLAT state transitions:
```python
class MarkovChain:
    T: dict[str, dict[str, float]]  # Transition matrix
    counts: dict[str, dict[str, int]]  # Observation counts

    def observe(self, prev_state, new_state):
        counts[prev][new] += 1
        self._recompute_T()

    def get_transition_prob(self, from_state, to_state):
        return T[from_state][to_state]
```
**Effort**: MEDIUM — clean implementation, needs state detection + rolling window

#### Gap 4: Blended EV Computation
**Current**: `EVEngine.validate_signal_sync()` uses hardcoded `p_win=0.55`
**Needed**: Replace with blended computation:
```python
P_win = 0.40 × P_regime + 0.30 × P_imbalance + 0.30 × P_markov
EV = (P_win × profit) - ((1-P_win) × stake)
```
**Effort**: LOW once Gap 1-3 are filled — just the weighted average + existing formula

#### Gap 5: Aggression Dispatcher
**Current**: All trades use the same execution style (no dispatcher)
**Needed**: EV → order type mapping + TTM decay logic
**Effort**: MEDIUM-HIGH — needs HyperLiquid order API integration, limit order management, fill tracking

---

## Implementation Priority & Dependencies

```
Phase 1: "Fix the EV Skeleton" (1-2 days)
├── Gap 1: Real P_regime from HMM posteriors        [LOW effort]
├── Gap 4: Replace hardcoded p_win in ev_engine.py  [LOW effort]
└── Wire EV result into Redis config as `ev` field   [LOW effort]

Phase 2: "Add Market Pulse Signals" (2-3 days)
├── Gap 2: L2 Imbalance → P_imbalance bridge         [MEDIUM effort]
├── Gap 3: Real Markov Transition Matrix              [MEDIUM effort]
└── Backfill nt_pattern_stats from trade journal      [MEDIUM effort]

Phase 3: "Aggression Dispatcher" (3-5 days)
├── Gap 5: EV → order type mapping                    [MEDIUM-HIGH effort]
├── TTM (Time-to-Market) decay logic                  [MEDIUM effort]
├── Fill tracking + limit order management            [HIGH effort]
└── Bayesian live update (session decay)              [MEDIUM effort]
```

### Phase 1 is the 80/20

Just fixing the EV Engine to use real `P_regime` instead of `p_win=0.55` would immediately:
- Stop entering trades against the regime (Scenario C — the trap)
- Correctly size positions based on actual conviction
- Give you a meaningful `ev` number in Redis for the executor to consume

The blended P_win (with L2 + Markov) is a refinement, but P_regime alone would fix the biggest gap.

---

## EV vs. What We Already Have: Complement, Not Replace

| Existing Layer | What It Catches | What It Misses |
|---|---|---|
| `/optimize` R:R filter | Bad R:R combos | Good R:R but wrong regime |
| `/optimize` DSR + Bonferroni | Overfit backtests | Real-time regime shifts AFTER backtest |
| Signal filter regime gate | Misaligned signals | Partial misalignment (55% bull, not strong enough) |
| Kelly regime-gating | Wrong-direction sizing | Sizing is binary (in/out) not graduated |
| **EV Engine** | **All of the above + graduation** | **Nothing — it's the unifying layer** |

The EV Engine doesn't replace any existing layer — it **graduates** them. Instead of "is this trade allowed Y/N?", it answers "how much conviction does this trade have, 0-100%?" That graduation drives:
- **Sizing**: `final_f = base_kelly × ev_scale` (proportional, not binary)
- **Execution**: passive/mid/aggressive (order type adapts to conviction)
- **Live management**: Bayesian updates (if conviction drops mid-trade, tighten stops)

---

## The Killer Feature: EV as "Market Pulse" Dashboard

Once the EV Engine is real, you get a single number that tells you everything:

```
┌──────────────────────────────────────────────────┐
│ BTC-PERP  │  EV: $2,311 (1.54/$)  │  🟢 HIGH  │
│           │  P_regime:   0.84      │            │
│           │  P_imbalance: 0.72     │            │
│           │  P_markov:   0.58      │            │
│           │  Blended:    0.726     │            │
│           │  Execution:  PASSIVE   │            │
├──────────────────────────────────────────────────┤
│ ETH-PERP  │  EV: $72 (0.36/$)     │  🟡 MID    │
│           │  P_regime:   0.70      │            │
│           │  P_imbalance: 0.50     │            │
│           │  P_markov:   0.38      │            │
│           │  Blended:    0.544     │            │
│           │  Execution:  MID-POINT │            │
├──────────────────────────────────────────────────┤
│ SOL-PERP  │  EV: $0.81 (0.27/$)   │  🔴 LOW    │
│           │  P_regime:   0.25      │            │
│           │  P_imbalance: 0.456    │            │
│           │  P_markov:   0.42      │            │
│           │  Blended:    0.363     │            │
│           │  Execution:  REJECT*   │            │
│           │  *min_ev=0.30 filter   │            │
└──────────────────────────────────────────────────┘
```

That's the "market pulse" — one glance tells you which assets have conviction, which are marginal, and which are traps.

---

## Redis Config Changes (New Fields)

```typescript
export interface RedisConfig {
  // ... existing fields ...

  // NEW: EV Engine outputs
  ev?: number;              // Raw EV in price units
  ev_per_dollar?: number;   // Normalized EV per $1 risked
  p_win?: number;           // Blended P_win (0-1)
  p_regime?: number;        // Regime component
  p_imbalance?: number;     // L2 imbalance component
  p_markov?: number;        // Markov transition component
  aggression?: 'passive' | 'mid' | 'aggressive';  // Execution style
}
```

These are **output fields** — the DOOM executor reads them to decide how to place orders. No more `governor_aggression` or `governor_regime` needed — EV IS the aggression.

---

## Summary: Your EV IS Built, But It's Driving With Its Eyes Closed

| Question | Answer |
|---|---|
| Is EV built in? | Yes — but the live path uses `p_win=0.55` hardcoded, so it's effectively just an R:R filter |
| Is the governor overlay needed? | No — EV replaces it. `governor_aggression` → `ev_per_dollar` (continuous, not binary) |
| Is the Aggression Dispatcher needed? | Yes — but it's Phase 3. Fix EV first (Phase 1), add signals (Phase 2), then dispatch (Phase 3) |
| What's the 80/20? | Replace `p_win=0.55` with real `P_regime` from HMM. One line change, immediate improvement |
| What does EV give us that we don't have? | Graduated conviction (0-100%) instead of binary gates (Y/N). Drives sizing, execution, and live management |
