# Quant Audit: Signal Generation, Qualification, and the Calibration–Reachability Gap

> **Audience:** product/engineering owners and any quant risk reviewer  
> **Perspective:** hedge-fund research / risk desk  
> **Scope:** live signal path in `noble-trader-fastapi-backend` (sweep orchestrator, EV engine, EOD resolver, reachability diagnostics, scorecard PnL)  
> **Evidence base:** source code, feature flags, brick params, migrations, and worklogs through 2026-09-15 (including the two-week signal-quality retrospective)  
> **Not a fix PR** — methodology review and technical specification of what is wrong, why it matters, and what “correct” looks like.

---

# Part I — Plain Language Summary

## The one-sentence problem

The system predicts **direction** (“price should go up”), then scores and publishes **brackets** (“if it hits this take-profit before this stop-loss within 48 hours, we win”). Those are different questions. Direction can be well-calibrated while almost every trade expires unresolved — and that is exactly what the system was doing for months.

## What the system actually does today

1. Every ~15 minutes, for each symbol, the **light sweep** loads recent 1h closes, fits an HMM regime, optionally a TimesFM forecast, builds a buy/sell signal from the regime label, and runs a chain of **qualification gates**.
2. Direction is **not** blended. It is a string match on the regime label: if the label contains “bull,” the trade is long; if “bear,” short. Everything else only *conditions* confidence on that already-chosen side.
3. Entry is simply the **last close**. Stop-loss and take-profit are fixed distances:
   - SL = entry ∓ `brick_size × sl_bricks`
   - TP = entry ± `brick_size × tp_bricks`
   - Brick sizes come from a weekly **heavy-sweep** grid search that rewrites `config/brick_params.yaml`.
4. A probability `p_win` is computed as a **logit blend of three sources** (regime posterior, Markov T^N, TimesFM). That number is treated as “probability this trade wins.”
5. Expected value is the classic binary formula:  
   `EV = p_win × TP_distance − (1 − p_win) × SL_distance`.  
   Signals only publish if EV, RR, Kelly, regime confidence, and related gates all pass.
6. Overnight, an **EOD resolver** walks post-entry **hourly closes** (no highs/lows). First close past TP → win. First close past SL → loss. Neither in 48 bars → **expired**.
7. **Calibration** compares average predicted `p_win` to realized win rate — but **only on signals that resolved to win or loss**. Expired trades are deliberately excluded from calibration.
8. **Reachability** diagnostics bucket historical brackets by `distance / ATR` and vol regime to show which geometries never get hit. Most of that machinery is still **shadow/read-only**.

## What that means for money

| Reality | What the metrics said |
|---|---|
| For weeks, **~60% of signals expired** — neither TP nor SL closed past. | Win rate looked like a normal 30% because it only measured the 40% that resolved. |
| A week could show **+$24,058 PnL** while most trades never resolved. | Users saw green; the resolution mechanism was broken underneath. |
| After deadline-aware scoring and tighter gates, expiry fell to **~21%**. | Structural health improved. Weekly PnL turned slightly negative (−$892). Calibration bias got *worse* (+0.27 → +0.34). |
| Gold pairs often had TP distance **4+ ATR** away. | Directional models could be “right” while every trade timed out. |

The honest framing already in the repo’s own retrospective is correct: **the system got healthier and less flattering at the same time.** The remaining gap is not “tune the gates harder.” It is that **`p_win` is still a directional score being sold as a bracket-win probability.**

## The five load-bearing methodology errors

### 1. Category error in `p_win` (the core gap)

`p_regime` and `p_timesfm` answer “will price rise?” The EV formula needs “will price reach +TP before −SL within 48h?”  
There is no first-passage / barrier-touch model bridging them. Markov T^N is *framed* as reachability but implemented as **terminal-state probability at step N**, not “hit anytime by N,” and N is a **global 10 bars**, not the actual TP brick distance.

**Result:** overconfident EV on wide brackets, silent under-pricing of tight ones, and a calibration loop that can never fully close because the prediction target and the outcome target are different random variables.

### 2. Survivorship in calibration

Outcomes used for bias/win-rate are **only** `tp_hit` and `sl_hit`. Expired rows never enter `nt_calibration_records`.

If a bucket has 90% expiry and 100% win on the 10% that resolved, calibration reports a perfect model. Reachability tables show the expiry, but **nothing in the bias formula reads them.**

### 3. Live settlement ≠ backtest selection physics

| | Heavy-sweep backtest (picks brick params) | Live EOD resolver (scores outcomes) |
|---|---|---|
| Path data | Renko bricks, gaps, slippage fields | Hourly **closes only** |
| Same-bar both hit | **SL first** (OCO default) | **TP first** (optimistic) |
| Exits | Fixed SL/TP **+ trailing + time stop** | Fixed SL/TP only |
| Deadline | Unbounded in base Sharpe (fixed only for *selection* via `deadline_aware_sharpe`) | Hard 48h |

The strategy that is *optimized* is not the strategy that is *settled*.

### 4. Close-only resolution flatters wins and misses path

- Intraday TP/SL touches that reverse by the hourly close become **expired**, not hits.
- Gaps through SL never produce worse-than-SL fills.
- Same-bar priority awards TP when both levels could apply.
- Exit “price” for TP/SL is the triggering **close**, but PnL uses **full brick counts**, not the actual fill distance.

### 5. PnL has three (sometimes four) truths

| Layer | Expired treatment | Costs | Units |
|---|---|---|---|
| Signal resolver | MTM at 48h (since 2026-09-08) | None | 1-unit price delta |
| Scorecard SQL | **Excluded** from PnL/win% | None | lot-multiplied notional |
| Paper book | r_multiple forced 0; age-expire at 7d = $0 | None | Kelly-sized notional |
| Heavy-sweep grid | Deadline-aware zeros long holds | Default 0 in selection | brick PnL Sharpe |

A user-facing “PnL” can disagree with the sim ledger, which can disagree with the paper book, which can disagree with the combo that was selected.

## What the recent work got right

Credit where due — this is not a greenfield mess:

- **Deadline-aware scoring is live** and independently validated (win% 58.5→74.5, expiry 49.3→42.0, PnL +$59.6k→+$66.6k on a 70-anchor replay).
- **Vol-tier reachability keying** `(symbol, vol_tier, distance/ATR)` is the right diagnostic grain.
- **Episode clustering** for bias (overlapping trades = one observation) is the right statistical move.
- **Shadow-composition leak** in reachability/ATR/deadline stages was found and fixed before the Oct 5 review.
- **Expired ≠ loss** for calibration is a *correct* design choice given the current outcome definition — the bug is excluding expired from *measurement*, not from the win/loss label.
- **Raw vs enforced `p_win` capture** exists so model miscalibration can be separated from enforcement compensation.

The infrastructure is honest instrumentation for a problem it does not yet solve.

## What a quant desk would require before live capital

1. **Redefine `p_win` as a deadline-conditioned bracket outcome** (or rename it `p_dir` and stop using it as P(win) in EV).
2. **OHLC / path-aware resolution** aligned with the backtest’s gap and same-bar rules.
3. **Measure expiry in the model**, not only as a post-hoc rate:  
   `P(win) = P(resolve) × P(win | resolve)`.
4. **One canonical PnL definition** across resolver, scorecard, paper book, and selection.
5. **Costs (spread/slippage/fees)** on every decision-relevant expectancy.
6. **Reliability curves / Brier decomposition** per (symbol, direction, regime, bucket) — not one system-wide mean bias.
7. **Walk-forward / multiplicity control** on the heavy-sweep grid (n_trials, embargo, DSR/PSR).
8. **Quarantine proxy-ATR and contaminated shadow windows** from any go/no-go evidence.

Until then, the stack is a research simulator with production-shaped side effects — not a fund-grade signal factory.

---

# Part II — Full Technical Specifications

## 1. System map

```
TradingView 1h candles (200 light / 2160 heavy)
        │
        ▼
┌───────────────────┐     brick_params.yaml      ┌──────────────────┐
│  Heavy sweep      │ ─────────────────────────► │  Light sweep     │
│  (daily 20:00)    │   brick_size, sl, tp       │  (every ~15 min)  │
│  Renko grid+Sharpe│                            │  HMM + TimesFM   │
└─────────┬─────────┘                            │  EV + 7–10 gates │
          │ deadline_aware_sharpe (LIVE)         └────────┬─────────┘
          │ reachability_aware_pick (SHADOW)              │
          │ ATR-anchored bricks (SHADOW)                  ▼
          │                                    Discord + nt_signal_sim
          │                                    (every non-neutral signal)
          ▼                                              │
   nt_heavy_sweep_*_shadow                               ▼
                                              EOD resolver (close-walk, 48h)
                                                        │
                    ┌───────────────────────────────────┼──────────────────┐
                    ▼                                   ▼                  ▼
         nt_calibration_records              nt_bracket_reachability   scorecard SQL
         (tp_hit/sl_hit only)                (all resolved)            (tp/sl only)
                    │                                   │
                    ▼                                   ▼
         bias / drift / enforcement          expectancy_r / pct_expired
         (SHAPES LIVE p_win + Kelly)         (DIAGNOSTIC / SHADOW FILTER)
```

**Primary modules**

| Concern | Path |
|---|---|
| Light-sweep generation + gates | `regime_platform/sweep_orchestrator/orchestrator.py` |
| Heavy-sweep grid + pick | `regime_platform/sweep_orchestrator/heavy_sweep.py` |
| EV / p_win | `regime_platform/services/statistics/ev_engine.py` |
| Outcome resolution + sim PnL | `regime_platform/sweep_orchestrator/eod_resolver.py` |
| Calibration enforcement | `regime_platform/sweep_orchestrator/calibration_enforcement.py` |
| Bias by bucket | `regime_platform/sweep_orchestrator/calibration_bias_bucket.py` |
| Reachability table | `regime_platform/sweep_orchestrator/bracket_reachability.py` |
| Reachability-aware pick | `regime_platform/sweep_orchestrator/heavy_sweep_reachability.py` |
| Feature flags / thresholds | `config/feature_flags.yaml` |
| Brick geometry | `config/brick_params.yaml` |
| Renko backtest physics | `renko/risk_manager.py`, `renko/config.py` |

---

## 2. Signal generation specification

### 2.1 Direction

```
direction = "short" if "bear" in regime_label.lower() else "long"
signal    = "buy" | "sell" | "neutral"  # neutral if neither bull nor bear
```

- Sole switch: HMM regime label (`orchestrator.py` ~2997).
- TimesFM, Markov, macro, and trend **cannot flip direction**. They only affect `p_win` / Kelly / optional gates *after* the side is chosen.
- Regime-label debounce is **shadow** (`regime_label_debounce_mode: shadow`); live labels still flap.

**Quant note:** This is a single-feature classifier with a multi-source confidence layer, not a multi-source direction model. Independence of EV sources is illusory — all are functions of the same price series.

### 2.2 Bracket construction

```
entry      = prices[-1]                         # last 1h close
SL (long)  = entry - brick_size * sl_bricks
TP (long)  = entry + brick_size * tp_bricks
SL (short) = entry + brick_size * sl_bricks
TP (short) = entry - brick_size * tp_bricks
RR         = tp_bricks / sl_bricks
```

Defaults in YAML: `sl_bricks=3`, `tp_bricks=6` → RR = 2.0.  
`min_rr` is shared between light qualification and heavy grid filter (`feature_flags.yaml: min_rr: 1.5` via `get_min_rr()`, not env).

> **Operator status (2026-09-20, post-audit):** `min_rr` is **maintained but a non-factor** on light qualification. At 1.5 the RR clause rarely binds because live brick pairs already sit at RR≈1.67–2.0+; reject composition in production logs is kelly/regime/p_win-dominated. Do not interpret this key as a signal-quality tier; do not raise the light floor to 2.0 as a quality gate. Heavy-grid filtering still uses the same key as a geometry pre-filter. Code unchanged by this note.

**Brick size generation (heavy sweep):**

```
compute_atr = mean(|close[i] - close[i-1]|) over 14 bars   # close-only "TR"
vol_pct     = 100 * ATR / price
base_pct    = 0.75 if vol>3% else 0.60 if vol>1.5% else 0.40 if vol>0.5% else 0.25
candidates  = base * [0.75, 1.0, 1.5, 2.0, 3.0]
# optional ATR-anchored extras 0.15x/0.3x/0.6x ATR — currently SHADOW
```

**Issues:**

| ID | Issue | Severity |
|---|---|---|
| G1 | Close-only ATR understates true range → distance/ATR ratios inflated | Medium |
| G2 | Primary candidates are price-% tiers, not vol-normalized; gold family historically landed in 4+ ATR buckets | High |
| G3 | Entry assumes fill at last close; no bid/ask, latency, or partial fill in live EV | High |
| G4 | Light path uses 200-bar HMM; heavy pick uses 2160-bar backtest context | Medium |

### 2.3 `p_win` and EV

**Live production blend (3 effective sources):**

| Source | Nominal w | Live | Definition |
|---|---|---|---|
| `p_historical` | 0.30 | **No** (renormalized out since 2026-09-09) | — |
| `p_regime` | 0.25 | Yes | `min(0.97, P(bull|bear)+0.5·P(neutral))` |
| `p_markov_hold_n` | 0.20 | Yes | `EMA_0.3( (T^N)[state][UP|DOWN] )`, N=`MARKOV_HOLD_STEPS`=**10** |
| `p_timesfm` | 0.25 | Yes | `clamp(σ(k·Δp/p), …)`, code default k=20 |

```
p_win = inv_logit( Σ w_i · logit(p_i) )          # missing sources dropped, weights renormed
p_win'= calibration_shrink(p_win)                # post-blend enforcement
EV    = p_win'·(tp·bs) − (1−p_win')·(sl·bs)
allowed = (EV > min_ev) and (RR ≥ min_rr)   # min_rr=1.5 maintained; RR clause inert on light path (2026-09-20)
```

**EV engine issues:**

| ID | Finding | Severity |
|---|---|---|
| E1 | **Category error:** directional probs used as P(TP before SL) | **Critical** |
| E2 | Markov `(T^N)[i][j]` is **P(state at N)**, not **P(hit by N)**; docstring overclaims first-passage | **Critical** |
| E3 | N=10 decoupled from `tp_bricks` (typically 5–6) | High |
| E4 | EV ignores 48h expiry: profit is all-or-nothing full TP | High |
| E5 | Logit pool assumes independence among correlated sources | High |
| E6 | Code defaults TimesFM k=20 / clamp [0.001,0.999] vs documented prod k=10 / [0.05,0.95] | High |
| E7 | `p_win` hard floor 0.50 rejects positive-EV high-RR low-p trades | Medium |

### 2.4 Qualification gates (light sweep)

Evaluated as a set; `qualified = all(gates)` (with TimesFM availability optional in soft mode).

| # | Gate | Threshold | Notes |
|---|---|---|---|
| 1 | `signal_qualifiable` | non-neutral | direction exists |
| 2 | `regime_conf_ok` | ≥ 0.60 | HMM confidence |
| 3 | `kelly_min_ok` | ≥ 0.02 | **post-enforcement** Kelly |
| 4 | `timesfm_available` | not None | hard veto only if mode=`hard` |
| 5 | `timesfm_confidence_ok` | soft mode: always pass after weight shrink | |
| 6 | `ev_allowed` | EV>0 and RR≥1.5 | |
| 7 | `p_win_ok` | hard floor 0.50; soft band to 0.55 shrinks Kelly | |
| 8 | `context_aligned_ok` | only if live | currently **shadow** |
| + | `cooldown_ok` | 60 min; open prior position blocks | |

**Soft `p_win` band:**

```
if p_win < 0.50: reject
elif p_win < 0.55: qualify with kelly_shrink = (p_win-0.50)/0.05
else: full size
```

So a signal with `p_win=0.501` is “qualified.” The word **qualified does not mean “edge”** in the current contract.

**Heavy-sweep path is a different gate set** (no TimesFM, no cooldown, hard `p_win≥0.55`, and **enforcement applied after qualification** vs light path’s enforcement-before-gates). Two production definitions of “qualified.”

---

## 3. Outcome / W-L specification

### 3.1 Close-walk resolver

```
window = post_entry_closes[:48]                    # EXPIRY_BARS = 48 × 1h
for close in window:
    BUY:  if close >= TP → tp_hit; elif close <= SL → sl_hit   # TP first
    SELL: if close <= TP → tp_hit; elif close >= SL → sl_hit
else: expired
exit_price = triggering close, or last close if expired
```

| Property | Value |
|---|---|
| Bar type | closes only |
| Same-bar both | **TP wins** |
| Gaps | not modeled |
| Expiry | 48 wall-clock hourly bars (includes weekend for equities) |
| Min-age gate | would-be expire with &lt;48 available bars → stay pending |
| Calibration write | only `tp_hit`→win, `sl_hit`→loss |

### 3.2 PnL (`compute_pnl`)

```
tp_hit:     pnl_bricks = +tp_bricks
sl_hit:     pnl_bricks = -sl_bricks
expired:    pnl_bricks = signed(exit - entry) / brick_size     # since 2026-09-08
pnl_dollars = pnl_bricks * brick_size                          # 1-unit notional
```

No fees, spread, slippage, financing, or lot conversion at this layer.

### 3.3 Expiry taxonomy is not one concept

| Path | exit_price | PnL | Calibration | Reachability denom |
|---|---|---|---|---|
| Resolver full 48h expire | last close | MTM | skipped | counted expired |
| Retention force-expire (stale &gt;7d) | NULL | NULL | none | counted expired |
| Paper age-expire | — | $0 | — | — |

Same label `expired`, three economic meanings.

### 3.4 Reachability definition

```
ratio  = |TP - entry| / ATR14
bucket ∈ {<0.5, 0.5-1, 1-2, 2-4, 4+}
key    = (symbol, vol_tier, bucket)
```

Stats: `pct_hit_tp`, `pct_hit_sl`, `pct_expired`, time-to-resolve percentiles.  
Population: **all resolved rows** (qualified and not).

**Expectancy by bucket:**

```
expectancy_r = win_rate * avg_rr_winners - (1 - win_rate) * 1.0
# win_rate over tp_hit/(tp_hit+sl_hit) only; losers forced -1R; expired excluded
```

Used by `reachability_aware_pick` (shadow): keep combos with unscored buckets or `expectancy_r > 0`.

| ID | Finding | Severity |
|---|---|---|
| R1 | Close-only ≠ first-touch | **Critical** |
| R2 | No gap-through SL on live path | High |
| R3 | No costs on production PnL | High |
| R4 | Proxy ATR uses **current** ATR for historical rows (look-ahead) | High |
| R5 | Retention expire pollutes reachability without MTM | High |
| R6 | `expectancy_r` survivorship-conditioned on resolution | High |
| R7 | Scorecard excludes expired MTM from user PnL | High (reporting) |
| R8 | Losers hardcoded to −1R | Medium |
| R9 | Shadow window 2026-09-13→14 contaminated unless excluded | High (decision) |

---

## 4. Calibration specification

### 4.1 Bias

```
bias = decay_weighted_avg(predicted_p_win) - decay_weighted_win_rate
# window 7d, min 20 records, min 2 independent episodes (overlapping holds merged)
# decay: weight = 0.5 ** (age_days / 3)
```

| Cadence | Source | Overconf trip | Underconf trip |
|---|---|---|---|
| Drift ~15m | enforced | bias &gt; 0.15 | — |
| 4h pass | raw | ≥ 0.30 | ≤ −0.20 |
| Nightly | enforced | &gt; 0.10 | &lt; −0.10 |

### 4.2 Enforcement

```
p_win' = (p_win * n + 0.5 * 10) / (n + 10)     # n = episode count
kelly' = kelly * 0.5                            # same cut for under AND over
# stagnation tier 2: kelly=0, p_win=0.5
```

At the common `n_episodes=2` case: prior carries ~83% of the shrink. That is a hard pull to 0.5, not a posterior update on a win-rate map.

### 4.3 Critical measurement defects

| ID | Finding | Severity |
|---|---|---|
| C1 | Raw `p_win` capture default **off**; NULL raw **falls back to enforced** for raw-bias *and* Brier | **Critical** |
| C2 | Calibration excludes expired → survivorship on reachability | **Critical** |
| C3 | Heavy path enforces **after** gates; light path **before** | **Critical** |
| C4 | Mean bias only — no reliability curve; zero bias ≠ calibrated | High |
| C5 | Same Kelly cut for underconfidence | High |
| C6 | Self-starving loop: penalty → fewer records → insufficient data → penalty off → overconfidence returns | High |
| C7 | SQL dashboards use simple AVG without decay — can disagree with Python | Medium |
| C8 | Brier stored, never consumed by any control path | Medium |

### 4.4 What “calibrated” is not

A single scalar `avg(p) − win_rate` cannot detect:

- conditional miscalibration (AAPL buy in `high_vol_strong_bull`: 66% predicted vs 0% actual),
- direction asymmetry,
- bucket/geometry effects,
- regime mix shifts.

The two-week retrospective already documents the blended number **moving opposite** structural health. That is evidence the metric is the wrong level of aggregation, not that the system “got worse” in one line.

---

## 5. The formal calibration–reachability gap

### 5.1 Random variables in play

| Symbol | Meaning in code |
|---|---|
| \(D\) | Direction correct (price moves favorably in some horizon) |
| \(\tau_{TP}, \tau_{SL}\) | First times price *would* touch TP / SL under a path model |
| \(R = 1[\tau_{TP} < \tau_{SL} \wedge \tau_{TP} \le 48h]\) | Realized bracket win (what the user cares about) |
| \(\hat p\) | `p_win` from EV blend — **an estimator of something like \(P(D)\), not \(P(R)\)** |

### 5.2 Implemented pipeline

```
features → directional sources → logit pool → p_win (= p̂)
p̂ used as:  EV = p̂·TP − (1−p̂)·SL
outcome:    close-walk first passage with TP-priority, 48h
calibration: E[p̂ − 1{win}] on {tp_hit, sl_hit} only
```

### 5.3 Required pipeline (desk standard)

```
features → p_dir = P(D | X)
geometry → d_tp/ATR, d_sl/ATR, horizon, vol_tier
survival → P(resolve by 48h | geometry, regime)
joint    → p_bracket = P(R) = P(τ_tp < τ_sl ∧ τ ≤ T)
EV       = p_bracket · net_profit − (1−p_bracket) · net_loss − costs
         + E[MTM | expired] · P(expired)     # if expired MTM is real PnL
calibration target = p_bracket (or dual-target p_dir AND p_resolve)
```

Even a crude, auditable approximation is better than the category error:

```
p_touch_tp ≈ P(max favorable move ≥ d_tp in 48h | ATR, vol_tier)
p_touch_sl ≈ P(max adverse move ≥ d_sl in 48h | ATR, vol_tier)
p_bracket  ≈ p_dir_scaled · p_touch_tp / (p_touch_tp + p_touch_sl)
# or empirical: lookup expectancy/win from (symbol, vol_tier, bucket) with Bayesian shrink
```

The repo already computes the **ex-post** pieces (`pct_expired`, `expectancy_r` per bucket). They are not wired **ex-ante** into `p_win` or EV.

### 5.4 Why well-calibrated direction still loses money here

Example (illustrative, matches gold-family diagnostics):

| Setup | Value |
|---|---|
| Predicted directional accuracy | 60% (fairly calibrated on “up vs down over 2 days”) |
| TP distance | 4× ATR |
| SL distance | 1.2× ATR |
| P(TP reached in 48h) | ~10% |
| P(SL reached first \| either) | high when it moves, but most paths never reach TP |
| Realized bracket win | ~5–15% |
| Modeled EV using p_dir=0.6 and RR=3.3 | strongly positive |
| Actual EV | ~0 or negative once expiry and path are honest |

That is not a tuning failure. It is a **target-variable mismatch**.

---

## 6. Heavy-sweep selection specification

### 6.1 Grid

- ~8 brick sizes × SL range × TP range (~144 combos) on 90d×1h.
- Filters: `RR ≥ MIN_RR`, `total_trades ≥ min_trades` (5), `sharpe ≤ max_sharpe` (5.0 overfit clamp).
- Objective: max Sharpe of brick PnL (not returns, not capital-normalized, no risk-free).
- `n_trials=1` → deflated Sharpe is a no-op.

### 6.2 Deadline-aware scoring (LIVE since 2026-09-08)

```
pnl_i' = 0 if hold > 48h else pnl_bricks_i
sharpe = mean(pnl') / std(pnl')
```

Validated: 70 anchors, win% 58.5→74.5, expired% 49.3→42.0, total PnL $59,658→$66,632.

### 6.3 Still open on selection

| ID | Issue | Severity |
|---|---|---|
| S1 | Grid Renko config ≠ live trade definition (trail/time-stop, OCO SL-first) | **Critical** |
| S2 | In-sample full-window argmax; weekly re-run multiplies selection bias | High |
| S3 | Reachability filter shadow only; ATR-anchored candidates shadow only | High |
| S4 | Contaminated shadow window must be excluded from Oct 5 evidence | High |
| S5 | Cost fields exist in trade journal but are not in selection objective | High |

---

## 7. Scorecard and user-facing metrics

From migration 149 / retrospective:

```
pnl_notional = Σ pnl_dollars  FILTER (qualified ∧ outcome ∈ {tp_hit, sl_hit})
win_pct      = tp_hit / (tp_hit + sl_hit)
expired_pct  = expired / (tp_hit + sl_hit + expired)
```

**Missing first-class columns the retrospective already recommends:**

1. Expiry rate trend as its own KPI (the 60%→21% story is invisible if you only watch win%/PnL).
2. Per-symbol calibration bias (not only blended).
3. Reachability bucket + vol_tier on each resolved trade.
4. Macro-alignment tag per trade.
5. Shadow-window trustworthiness flag (`data_window_flagged`).

---

## 8. Severity rollup (quant reject list)

### Critical — do not size real capital on this

1. Directional `p_win` used as bracket-win probability (E1/E2).
2. Close-only, TP-priority settlement ≠ backtest physics (R1, S1).
3. Calibration conditioned on resolution-only outcomes (C2).
4. Raw/enforced measurement fails soft into enforced (C1).
5. Two qualification paths with different gate order (C3).

### High — material bias in reported edge

6. EV ignores expiry; modeled EV overstates tradeable edge (E4).
7. No costs on production PnL (R3).
8. Proxy-ATR look-ahead in historical reachability (R4).
9. Scorecard drops expired MTM (R7).
10. In-sample grid selection without multiplicity control (S2).
11. Correlated logit sources without shrink (E5).
12. Enforcement prior-dominated at n≈2; underconfidence punished (C5, H2).
13. Self-starving calibration evidence loop (C6).
14. TimesFM code defaults vs production intent (E6).

### Medium

15. Close-only ATR (G1).
16. N for T^N ≠ TP bricks (E3).
17. SQL vs Python bias divergence (C7).
18. Brier unused; no reliability diagrams (C8).
19. Soft band makes “qualified” include coin flips (H4).
20. Multiple expired PnL conventions (R5, paper book).
21. Kelly stacked as continuous hourly f* × binary ev_scale (M1).

---

# Part III — Target Specification (what “fixed” means)

## 3.1 Target variables

| Name | Definition | Use |
|---|---|---|
| `p_dir` | P(favorable direction over horizon H) | research, optional blend input |
| `p_resolve` | P(TP or SL touched under settlement rules within T) | expiry risk |
| `p_win_bracket` | P(TP before SL ∧ within T) | **EV, Kelly, gates, calibration** |
| `p_expire` | 1 − p_resolve | MTM branch of EV |

**Invariant:** the number placed in `EV = p·profit − (1−p)·stake` must be an estimate of the **same event** the resolver labels as win.

## 3.2 Settlement contract (single source of truth)

Proposed canonical rules (must be shared by resolver, backtest, paper book, scorecard):

1. Prefer OHLC: `high ≥ TP` / `low ≤ SL` on the bar; if only closes, document optimism and dual-report.
2. Same-bar both: **parameterize** priority; default should match broker OCO or use **SL-first** (conservative); re-calibrate after any flip.
3. Deadline: 48 **trading** bars for the symbol’s session calendar (not raw wall-clock across weekends for equities).
4. Exit price: level fill at TP/SL (± half-spread for TP), gap-through SL uses open (worse).
5. Costs: half-spread + fixed bps slippage + commission per instrument class.
6. Expired: keep `outcome=expired`; write MTM; **dual-report** closed-only and all-resolved PnL; never force one number to serve both audiences silently.

## 3.3 `p_win_bracket` minimum viable implementation

Phase A (weeks, not quarters — uses data already collected):

```
lookup: nt_bracket_reachability / nt_calibration_bias_by_bucket
key:    (symbol, vol_tier, bucket_of_d_tp_atr)
inputs: pct_hit_tp, pct_hit_sl, pct_expired, n, atr_source=stored only

shrink: Beta-Binomial posterior on tp_hit vs sl_hit among resolved
adjust: multiply by stored p_resolve estimate (or 1 - pct_expired) for EV
gate:   if posterior mean(p_win_bracket) * p_resolve < threshold → no trade
```

Phase B (model):

- Survival / first-passage with two barriers under local vol (even GBM with ATR-scaled σ is a large upgrade).
- Replace Markov terminal-state hack with true hit-by-N or drop the source.
- Correlation-aware pool or simple stacking trained on held-out windows.

## 3.4 Calibration control loop

1. Always store `predicted_p_win_raw` (hard-fail insert if missing once column exists).
2. Bias **never** falls back to enforced for measurement paths.
3. Report reliability diagrams + Brier decomposition per (symbol, direction, vol_tier).
4. Underconfidence: do **not** cut Kelly the same way as overconfidence.
5. Episode minimum raised with data; hierarchical pooling across symbols when thin.
6. Enforcement operates on a **calibration map** (isotonic / beta-binomial on bins), not a single scalar shrink of the live prediction.

## 3.5 Selection hygiene

1. Force grid RenkoConfig to live contract: no trailing/time-stop unless live has them; OCO priority = settlement priority; deadline always on.
2. Walk-forward: train window / embargo / test window; record true `n_trials`; use Deflated Sharpe or PSR.
3. Prefer `daily_expectancy_r` or capital-normalized returns over raw brick Sharpe — but only after settlement parity.
4. Flip `heavy_sweep_reachability_mode` and ATR-anchored bricks only after excluding contaminated windows and requiring minimum n per bucket.

## 3.6 Metric contract for two audiences

| Audience | Primary KPIs | Must not mix with |
|---|---|---|
| User / marketing | Closed-trade PnL (costs), win% on resolved, expiry rate as risk stat | “Qualified count” alone |
| Research / risk | p_dir reliability, p_resolve by bucket, expectancy_r, bias by cell, shadow trust flags | Blended system bias as sole health score |

The pre-fix +$24k week with 60% expiry is the canonical example of why this split is mandatory.

---

# Part IV — Evidence anchors (file:line index)

| Claim | Anchor |
|---|---|
| Entry/SL/TP formulas | `orchestrator.py:563–596` |
| Direction from regime label | `orchestrator.py:2997` |
| Gate set + soft p_win | `orchestrator.py:3465–3548` |
| EV formula + allow gate | `ev_engine.py:694–715` |
| Markov T^N terminal-state | `ev_engine.py:278–294` |
| Close-walk + TP first | `eod_resolver.py:175–231` |
| PnL brick model + expired MTM | `eod_resolver.py:276–326` |
| Calibration skip expired | `eod_resolver.py:41–44, 538–539` |
| Reachability buckets | `bracket_reachability.py:71–77, 241–248` |
| expectancy_r | `calibration_bias_bucket.py:197` |
| reachability_aware_pick | `heavy_sweep_reachability.py:73–153` |
| Deadline-aware live | `feature_flags.yaml:415–459` |
| Gate thresholds | `feature_flags.yaml:39–44` |
| MIN_RR / grid filters | `feature_flags.yaml:347–364` |
| Brick defaults RR=2 | `brick_params.yaml:19–22` |
| Two-week metrics | `docs/20260915_TWO_WEEK_RETROSPECTIVE_SIGNAL_QUALITY_REVIEW.md` |
| Shadow leak fix | `worklog/20260914_DEADLINE_AWARE_ATR_ANCHOR_SHADOW_LEAK.md` |

---

# Part V — Closing verdict

The codebase already **names** the right problems and has shipped real structural wins (deadline-aware scoring, vol-tier buckets, episode bias, shadow-leak fixes, expired MTM). The gap that remains is not missing dashboards — it is a **target-variable and settlement-contract failure**:

- **Prediction target** ≈ direction  
- **Settlement target** ≈ first-passage bracket within 48h  
- **Calibration target** ≈ conditional on resolution  
- **Selection target** ≈ a different exit process than live  

Until those four are the same event (or explicitly factored), gate tuning and bias penalties will keep fighting a category error. A hedge-fund quant would treat the current stack as a **research instrument with production side effects**, not as a capital-ready signal factory — and would require the Part III contract before allocating.

**Highest-leverage sequence:**

1. Settlement parity (OHLC + same-bar rule + costs) between grid and live.  
2. Introduce `p_win_bracket` (even empirical bucket posterior) into EV/gates.  
3. Unify light/heavy enforcement order and make raw capture mandatory.  
4. Dual-report closed vs all-resolved PnL; make expiry a first-class KPI.  
5. Reliability diagnostics per cell; stop acting on one blended bias.  
6. Walk-forward the heavy sweep with true multiplicity accounting.

---

*Audit synthesized from live source, config, migrations, and worklogs in `noble-trader-workspace` (primary: `noble-trader-fastapi-backend`). Findings are methodological; no live parameters were changed.*
