# Signal Generation Flow: Validation + Optimization Comparison

## 1. ACTIVE SIGNAL GENERATION PATH (UPDATED)

### 1.1 Active signal generation path — CONFIRMED

The **Sweep Orchestrator** (`regime_platform/sweep_orchestrator/`) is the current signal generation path, replacing the deprecated Supabase Edge Functions (`supabase/functions/sweep/`). The orchestrator runs as a standalone Python service with APScheduler-based cron scheduling.

**Key files**:
- `regime_platform/sweep_orchestrator/orchestrator.py` - Core signal generation with qualification gates
- `regime_platform/sweep_orchestrator/__main__.py` - Entry point for orchestrator service
- `regime_platform/sweep_orchestrator/market_hours.py` - Market hours validation
- `regime_platform/sweep_orchestrator/price_fetcher.py` - Price data fetching
- `regime_platform/sweep_orchestrator/supabase_client.py` - Supabase integration (active symbols, logs)

**Architecture**:
```
APScheduler cron jobs → SweepOrchestrator → Redis publish → Signal Aggregator → Discord/Telegram/Webhooks
```

### 1.2 Redis publish targets — CONFIRMED

| Check | Result |
|---|---|
| Active sig-gen module | `regime_platform/sweep_orchestrator/orchestrator.py` |
| Deprecated module | `supabase/functions/sweep/index.ts` (Deno edge fn — do not reference) |
| Orchestrator docstring | `__init__.py:5-18` explicitly states *"Replaces Supabase Edge Functions + pg_cron"* |
| Entry point | `SweepOrchestrator.start()` → APScheduler cron jobs per symbol group |
| Per-symbol worker | `_process_symbol()` (orchestrator.py:360-581) |

### 1.2 Redis publish — CONFIRMED

`orchestrator.py:540-546` — single pipelined round-trip:

```python
pipe = self._redis.pipeline(transaction=False)
pipe.hset(config_key, mapping=config_dict)        # trading:config:{symbol}
pipe.expire(config_key, CONFIG_TTL)               # 1h TTL
pipe.set(heartbeat_key, str(now_ms), ex=ttl)      # trading:heartbeat:{symbol}
pipe.publish(agent_channel, agent_payload)        # agent:{symbol}
pipe.publish("signal.raw.noble_trader", agent_payload)  # aggregate L0 channel
await pipe.execute()
```

Both channels carry the **same** `agent_payload` (JSON heartbeat). Confirmed at:
- `orchestrator.py:480` → `agent_channel = f"agent:{symbol}"`
- `orchestrator.py:544` → `pipe.publish(agent_channel, agent_payload)`
- `orchestrator.py:545` → `pipe.publish("signal.raw.noble_trader", agent_payload)`

### 1.3 CRITICAL FINDING — orchestrator publishes without qualifying

The orchestrator's `_process_symbol()` computes EV via `self._ev_engine.compute_ev(...)`
(orchestrator.py:450-457) but **never checks `ev_result.allowed`** before publishing.
The EV engine *does* have an allow/deny gate (`ev_engine.py:365`):

```python
allowed = (ev > self.min_ev) and (rr >= self.min_rr)
```

…but the orchestrator only reads `ev_result.ev_scale` and `ev_result.aggression` for
sizing, then publishes unconditionally. **Every sweep cycle publishes a heartbeat to
`signal.raw.noble_trader` — qualified or not.**

### 1.4 There are actually TWO publishers to `signal.raw.noble_trader`

| Publisher | File:Line | Payload `type` | Qualified? |
|---|---|---|---|
| Sweep Orchestrator | `orchestrator.py:545` | `"heartbeat"` | ❌ No gate — publishes every cycle |
| Renko HFT Pipeline | `pipeline.py:260` (via `_publish_qualified_signal` @221) | `"renko_signal"` | ✅ Only after `SignalFilter.evaluate()` passes all 8 filters |

So the aggregator receives **two payload shapes** on the same channel. The "no qualified
signals" symptom is most likely because:
1. The orchestrator's heartbeats carry `signal: "neutral"` most of the time (no edge), and
2. The renko pipeline's qualified publisher only fires when a renko pattern + filters align.

## 2. TimesFM: Certainty vs. Uncertainty (you are right)

### 2.1 The deprecated logic was inverted

`supabase/functions/sweep/index.ts:417` (DEPRECATED) had:

```typescript
if (cachedP !== null && Math.abs(cachedP - 0.5) > 0.1) {
    // SKIPPED — TimesFM confident
    return { best: null, regime: null, kelly: null };
}
```

**Semantics**: skip the sweep when `|p − 0.5| > 0.1` → only run when TimesFM is
**uncertain** (0.4 < p < 0.6). This was an *optimization-cadence* heuristic ("don't
re-optimize brick params when the forecast is decisive"), but it was being read as a
signal-qualification rule and produced the opposite of what a trader wants:

| TimesFM forecast | Confidence \|p−0.5\| | Deprecated behavior | What you'd want |
|---|---|---|---|
| p = 0.50 | 0.00 (uncertain) | ✅ Run sweep, emit signal | ❌ No edge — don't trade |
| p = 0.55 | 0.05 (uncertain) | ✅ Run sweep, emit signal | ⚠️ Marginal — size small |
| p = 0.65 | 0.15 (confident) | ❌ SKIPPED — no signal | ✅ Strong edge — trade |
| p = 0.80 | 0.30 (very confident) | ❌ SKIPPED — no signal | ✅ Strongest edge — trade larger |

### 2.2 The active orchestrator doesn't use TimesFM at all

`orchestrator.py:364` — `_process_symbol(..., p_timesfm: float | None = None)`.
The light-sweep call site (`_sweep_group` → `_process_symbol`) never passes a value,
so `p_timesfm` is `None` in the EV engine, which falls back to the 2-source blend
(`ev_engine.py:344-350`). **TimesFM is wired into the EV math but never fed data.**

### 2.3 Recommended semantics — require certainty

A trade signal should require the model to be *confident* about direction. Three tiers:

```
confidence = |p_timesfm − 0.5|

  confidence < 0.05  → SKIP    (no edge, model is guessing)
  0.05 ≤ confidence < 0.15 → QUALIFY w/ reduced size (markov_multiplier × 0.6)
  confidence ≥ 0.15  → QUALIFY (full size)
  confidence ≥ 0.30  → QUALIFY + size boost (×1.2, capped)
```

This inverts the deprecated gate while keeping TimesFM as a *requirement* rather than
a *blocker*. The EV engine already has `compute_tail_risk()` (`ev_engine.py:548-587`)
which uses `is_extreme = p < 0.2 or p > 0.8` — that's the right region to *boost*,
not skip.

## 3. Current Flow vs. Optimized Flow — Side-by-Side

### 3.1 Current flow (orchestrator heartbeat path)

```
┌──────────────────────────────────────────────────────────────────┐
│  APScheduler cron (per symbol group)                             │
│        │                                                         │
│        ▼                                                         │
│  _sweep_group()                                                  │
│   ├─ fetch_active_symbols() ──► nt_symbol (Supabase)             │
│   ├─ is_market_open() filter                                     │
│   ├─ fetch_latest_candles(200 bars)                              │
│   ├─ per-symbol Redis lock (lock:sweep:{symbol}, 90s TTL)        │
│   └─ _process_symbol(sym, prices, p_timesfm=None)  ◀── NO TIMESFM│
│        ├─ RegimeHMM.fit/predict → regime_label, regime_conf      │
│        ├─ PositionSizer.size → kelly_f                           │
│        │    └─ if kelly_f==0 and regime≠neutral: floor=0.04·conf │
│        ├─ _markov_probs(regime_label) → p_up, p_dn (lookup table)│
│        ├─ _compute_trade_signal() → signal: buy/sell/neutral     │
│        ├─ EVEngine.compute_ev(p_timesfm=None) → ev, ev_scale     │
│        │    └─ allowed flag computed but NEVER CHECKED           │
│        ├─ fetch_calibration_bias()                               │
│        ├─ regime shift detection (prev vs current label)         │
│        └─ Redis pipeline (1 round-trip):                         │
│             HSET trading:config:{symbol}  (~28 fields)           │
│             EXPIRE config (1h)                                   │
│             SET trading:heartbeat:{symbol}                       │
│             PUBLISH agent:{symbol}           ◀── agent channel   │
│             PUBLISH signal.raw.noble_trader  ◀── L0 aggregate    │
│                                                                  │
│  GATES PRESENT:    market_hours, per-symbol lock, min 81 bars   │
│  GATES MISSING:    EV allowed check, RR check, TimesFM certainty │
│                     check, min regime_conf, backtest validation  │
└──────────────────────────────────────────────────────────────────┘
```

**Why no *qualified* signals reach Discord/Telegram**: most heartbeats carry
`signal: "neutral"` (regime not clearly bull/bear) OR carry a buy/sell but with
`ev_scale ≈ 0` / `aggression: "aggressive"` (worst tier). The aggregator's
`_normalize_signal()` then drops them as non-actionable.

### 3.2 Optimized flow (proposed)

```
┌──────────────────────────────────────────────────────────────────┐
│  APScheduler cron (per symbol group)  [unchanged]                │
│        │                                                         │
│        ▼                                                         │
│  _sweep_group()  [unchanged scheduling]                          │
│   └─ _process_symbol(sym, prices)  ◀── p_timesfm injected       │
│        ├─ RegimeHMM.fit/predict → regime_label, regime_conf      │
│        ├─ PositionSizer.size → kelly_f                           │
│        ├─ _markov_probs()  [unchanged]                           │
│        ├─ _compute_trade_signal() → raw_signal                   │
│        ├─ ⭐ TimesFM fetch (cached 15min) → p_timesfm            │
│        │    confidence = |p_timesfm − 0.5|                       │
│        ├─ EVEngine.compute_ev(p_timesfm, ...) → ev, ev_scale,    │
│        │    allowed, markov_multiplier                            │
│        │                                                          │
│        ├─ ⭐ QUALIFICATION GATE (new)                            │
│        │    IF signal == "neutral"           → SKIP publish      │
│        │    IF regime_conf < 0.60            → SKIP publish      │
│        │    IF confidence < 0.05             → SKIP publish      │
│        │    IF not ev_result.allowed         → SKIP publish      │
│        │    IF kelly_f < 0.02                → SKIP publish      │
│        │                                                          │
│        ├─ ⭐ SIZE ENHANCEMENT (new)                              │
│        │    size_mult = 1.0                                       │
│        │    if confidence ≥ 0.15: size_mult *= 1.0  (full)       │
│        │    if confidence ≥ 0.30: size_mult *= 1.2  (boost,cap)  │
│        │    else:                 size_mult *= 0.6  (reduced)    │
│        │    effective_kelly = kelly_f * ev_scale * size_mult     │
│        │                                                          │
│        ├─ calibration bias  [unchanged]                          │
│        ├─ regime shift detection  [unchanged]                    │
│        └─ Redis pipeline:                                        │
│             HSET trading:config:{symbol}                         │
│             SET trading:heartbeat:{symbol}                       │
│             PUBLISH agent:{symbol}  (always — heartbeat)         │
│             ⭐ PUBLISH signal.raw.noble_trader (QUALIFIED ONLY)  │
│             ⭐ PUBLISH signal.qualified.noble_trader (new tier)  │
│                                                                  │
│  GATES PRESENT:    market_hours, lock, min bars, +               │
│                    EV allowed, RR, TimesFM certainty,           │
│                    min regime_conf, min kelly                    │
└──────────────────────────────────────────────────────────────────┘
```

### 3.3 Gate-by-gate comparison table

| Gate / Filter | Current (orchestrator) | Deprecated (supabase sweep) | Optimized (proposed) | Effect on signal count |
|---|---|---|---|---|
| Market hours | ✅ `is_market_open()` | ✅ | ✅ | unchanged |
| Per-symbol lock | ✅ `lock:sweep:{symbol}` 90s | ✅ | ✅ | unchanged |
| Min bars (81) | ✅ orchestrator.py:287 | ✅ | ✅ | unchanged |
| TimesFM confidence | ❌ not called | ✅ but **inverted** (skip if confident) | ✅ **correct** (require ≥0.05) | ↑ more qualified |
| EV `allowed` check | ❌ computed, ignored | ❌ | ✅ enforce `ev > min_ev` | ↓ fewer but real |
| Risk ratio ≥ 2.0 | ❌ | ✅ backtest filter | ✅ enforce `rr ≥ min_rr` | ↓ fewer but real |
| Min regime_conf | ❌ | ❌ | ✅ ≥ 0.60 | ↓ fewer but real |
| Min kelly_f | ❌ (floor 0.04) | ❌ | ✅ ≥ 0.02 | ↓ fewer but real |
| Backtest validation | ❌ | ✅ (MIN_TRADES=10, MAX_SHARPE=8) | optional tier | ↓ fewer but real |
| Markov multiplier | ✅ sizing only | ✅ sizing | ✅ sizing + tier boost | ↑ better sizing |
| Tail risk | ❌ | ❌ | ✅ `compute_tail_risk()` | ↓ skip crashes |
| Publish to `signal.raw.noble_trader` | ✅ **always** | ✅ only if qualified | ✅ only if qualified | ↓ raw, ↑ qualified |
| Separate qualified channel | ❌ | ❌ | ✅ `signal.qualified.noble_trader` | ↑ clean stream |

### 3.4 Net effect on signal generation

- **Current**: publishes a heartbeat every sweep cycle (~every 15 min per symbol). Most
  carry `signal: "neutral"` or low-EV buy/sell → aggregator treats as noise → "no
  qualified signals" symptom.
- **Optimized**: publishes a heartbeat to `agent:{symbol}` every cycle (for DOOM agent
  liveness), but only publishes to `signal.raw.noble_trader` when the signal clears
  all gates. Adds a second `signal.qualified.noble_trader` tier for high-confidence
  signals. **Fewer raw publishes, more actionable signals, cleaner notification stream.**

## 4. Concrete patch locations

| Change | File:Line | What to add |
|---|---|---|
| Inject TimesFM | `orchestrator.py:364` signature + call site in `_sweep_group` | `p_timesfm = await _fetch_timesfm(symbol, prices)` (cached 15min) |
| Enforce EV gate | `orchestrator.py:459` (after `compute_ev`) | `if not ev_result.allowed: return {..., "qualified": False}` |
| Require certainty | new block after EV | `if abs(p_timesfm - 0.5) < 0.05: skip` |
| Min regime_conf | `orchestrator.py:378` | `if regime_conf < 0.60: skip` |
| Min kelly | `orchestrator.py:417` | raise floor + add `if kelly_f < 0.02: skip` |
| Conditional publish | `orchestrator.py:545` | wrap in `if qualified: pipe.publish(...)` |
| Qualified tier channel | `orchestrator.py:545` | add `pipe.publish("signal.qualified.noble_trader", ...)` for high-conf |
| Size boost | `orchestrator.py:459` | `size_mult = 1.2 if confidence >= 0.30 else (1.0 if >=0.15 else 0.6)` |

## 5. Recommendation

1. **Acknowledge the inverted legacy gate** — the deprecated supabase TimesFM check is
   not a bug to copy; it's a warning. The orchestrator should *require* certainty.
2. **Wire TimesFM into the active path** — the EV engine is built for 3-source blending
   but the orchestrator passes `None`. Add a cached TimesFM fetcher (15-min TTL to
   amortize the ~15s HTTP cost noted in the old edge fn).
3. **Enforce `ev_result.allowed`** before publishing to the aggregate channel — the gate
   already exists in the EV engine; the orchestrator just ignores it.
4. **Split channels**: keep `agent:{symbol}` for liveness heartbeats, gate
   `signal.raw.noble_trader` to qualified-only, add `signal.qualified.noble_trader`
   for the high-confidence tier.
5. **Tune thresholds conservatively first** (regime_conf ≥ 0.60, confidence ≥ 0.05,
   kelly ≥ 0.02) then relax if volume is too low — easier to loosen than to recover
   from a bad-fill storm.
