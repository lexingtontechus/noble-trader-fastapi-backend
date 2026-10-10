# Kairos signal-strategy structure — from monolithic engine to routed strategy archetypes

**Date:** 2026-10-10
**Type:** Strategy ideation / architecture scope (theoretical — no code change)
**Trigger:** ID45 Tier-1 context-gate inversion + era-clean reopt evidence. The gate inversion is not a bug to patch; it is the symptom of a structural flaw — **one signal engine applied uniformly across symbols that want opposite strategies.**
**Status:** Ideation for review. Bars and build sequence at the end.

---

## 1. The structural problem this solves

Kairos is a **monolithic** signal generator: one EV blend (`p_regime 0.5 / p_timesfm 0.5`), one 7-gate qualification chain, one bracket geometry — applied identically to all 14 symbols × both directions. The era-clean evidence says that uniformity is why the book is uniformly unprofitable.

**Era-clean scorecard (post-2026-10-02 cutover, `contaminated=false`, 7d rolling, closed only):**

| Day | closed | WR | avg_p_win | bias | exp_R |
|---|---|---|---|---|---|
| 10-10 | 103 | 50.5% | 0.311 | −0.19 | +0.32 |
| 10-09 | 885 | 18.9% | 0.326 | +0.14 | −0.45 |
| 10-08 | 932 | 9.6% | 0.331 | +0.24 | −0.69 |
| 10-07 | 1029 | 11.8% | 0.389 | +0.27 | −0.59 |
| 10-06 | 823 | 13.9% | 0.368 | +0.23 | −0.54 |
| 10-05 | 927 | 12.1% | 0.336 | +0.22 | −0.56 |
| 10-04 | 963 | 2.5% | 0.353 | +0.33 | −0.89 |
| 10-03 | 227 | 7.1% | 0.455 | +0.38 | −0.84 |
| 10-02 | 670 | 19.4% | 0.463 | +0.27 | −0.28 |

Two facts, both clean-era:
1. **The model is overconfident on every symbol, every day** — avg_p_win 0.31–0.46 vs realized WR 2.5–19%. The 10-10 positive day is a one-day artifact (BTCUSD-driven); the 7d mean is deeply negative expectancy. Calibration is the load-bearing fix and is *not* solved by the cutover alone.
2. **Per-symbol era-clean calibration bias** (post-cutover): XAUEUR +0.334, AUDUSD +0.311, US30USD +0.308, US500 +0.296, USDJPY +0.263, XAUUSD +0.257 … down to XAGUSD +0.095. Every symbol overprices TP. A uniform blend cannot fit 14 symbols whose realized hit rates span 3.4% (US30USD) to 23.2% (XAGUSD) — a 7× spread.

**The ID45 inversion, era-clean (it strengthens on clean data):**

| symbol·dir | n_false | WR_false | n_true | WR_true | read |
|---|---|---|---|---|---|
| **XAUUSD·buy** (macro=bear+trend=down) | 34 | **38.2%** | 170 | 6.5% | **mean-reversion edge, 5.9× the pass-cohort** |
| XAGUSD·buy | 165 | 14.5% | 110 | 46.4% | expected sign (silver trend-continues) |
| BTCUSD·sell | 35 | 8.6% | 178 | 9.0% | crypto MR not firing |

Gold buys into a macro/trend downtrend win ~6× more than gold buys that pass the gate. That is not noise to suppress — it is a **strategy the current engine has no slot for.** Meanwhile silver buys and crypto do the opposite. One engine, one rule, cannot serve both. This is the structural argument.

---

## 2. Quant mean-reversion methodology, mapped to the 4-step thesis

The user's thesis — *Identify Mean & Deviation → Confirm Regime & Sentiment → Apply Risk Management → Exit at Reversion* — is the canonical quant mean-reversion stack. Each step maps onto machinery Kairos **already has** (mostly as stamps, not as a routed decision):

| Thesis step | Quant canonical form | Kairos asset that already computes it | Gap |
|---|---|---|---|
| **1. Mean & Deviation** | `Z = (P − μ) / σ`; enter at \|Z\|≥2; Bollinger; distance-from-mean | `atr_ratio_run`, `tp_distance_atr`, `vol_ratio` (std24/std168), bracket geometry, `efficiency_24h` | Deviation is measured but not thresholded into an entry rule; it feeds geometry, not a MR trigger |
| **2. Regime & Sentiment** | ADX<20 (range) vs >25 (trend); Hurst H<0.5 (MR) vs >0.5 (trend); RSI; 200-SMA trend filter | 4-state HMM regime (vol×trend), `macro_label`, `trend_label`, `regime_edge` | Regime is *detected* and even *gated* on, but not used to *select a strategy* — the engine never asks "should this signal be MR or momentum?" |
| **3. Risk Management** | stop at \|Z\|>3.5 (regime-shift break), time-stop at 2× half-life, 1–2% sizing, vol-targeting, drawdown halt | EV Engine / Kelly, settlement deadline (`EXPIRY_BARS`=48), `dynamic_deadline` (p90-derived), `settlement_expiry_floor/cap` | Time-stop and vol-target exist as deadline geometry; the "regime-shift break" stop (Z overshoot = thesis dead) is **not** modeled |
| **4. Exit at Reversion** | exit at Z=0 (return to mean), partial at Z=−0.5, time-based fallback | TP/SL bracket, `dynamic_deadline` (median×mult for tight, p90 for wide) | Exit *is* the bracket; but a MR trade's "target" is the mean, which is a *different target* than a momentum trade's far-TP. One TP geometry serves neither well |

The core realization: **Kairos already computes every ingredient of a mean-reversion strategy. What it lacks is the routing decision** — the step that looks at (asset, direction, regime, deviation) and decides *which strategy archetype applies*, then configures entry/exit/risk accordingly. Today it runs one archetype (momentum-ish: far TP, trend-following gates) for everything.

---

## 3. Asset-selection thesis — validated on era-clean data

The user's thesis: *mean reversion works best on liquid, range-bound assets (EUR/GBP-type pairs) and is less effective on high-growth stocks or crypto during bull cycles.* The era-clean ID45 data supports the *direction* of this, with an important refinement:

| Asset class | Era-clean MR evidence | Fit |
|---|---|---|
| **Gold (XAUUSD/XAUAUD)** | buy-into-downtrend false-cohort 38.2% vs 6.5% pass | **MR works** — commodity, liquid, mean-reverts off downtrend exhaustion |
| **Silver (XAGUSD)** | buy false 14.5% vs true 46.4% (expected sign) | **MR does NOT work on buys** — silver trend-continues; the gate's original logic is right here |
| **Crypto (BTCUSD)** | sell false 8.6% vs true 9.0% | **MR not firing** — consistent with thesis (bull-cycle, momentum-dominant) |
| **FX majors** | uniform +0.25 bias, WR 6–9% | **Untested for MR specifically** — the EUR/GBP claim is the canonical textbook case but we have no MR-tagged FX cohort yet |

**Refinement to the thesis:** it is not simply "FX = MR, crypto = momentum." It is **asset × direction × regime.** Gold *buys* mean-revert; gold *sells* never fire the gate (always aligned, because gold trended down). Silver *buys* trend-continue. So the routing key is finer than asset class alone — it is the interaction of (asset microstructure, trade direction vs the macro/trend read, current vol regime). This is exactly what a strategy-registry encodes and a monolithic gate cannot.

**Why this is a real edge and not curve-fit:** the gold-buy MR cohort is a *pre-registered* structural claim (counter-trend buy when both slow filters oppose), it survives the era cutover (strengthens: 44–50% on 14d → 38% on clean 7d with the pass-cohort collapsing to 6.5%), and it has a plausible mechanism (downtrend exhaustion + fast-blend flip = capitulation reversal). That is the honesty bar. It is *not* yet walk-forward validated — see bars below.

---

## 4. Ideation — the Kairos strategy structure

**Proposal: evolve from one engine to a strategy registry that routes each signal to an archetype.**

```
                    ┌─────────────────────────────────────────┐
   signal candidate │  ROUTER: (asset, direction, regime,      │
   ────────────────►│   deviation-Z, macro, trend)             │
                    └──────────────┬──────────────────────────┘
                                   │ selects archetype + params
        ┌──────────────┬───────────┼───────────┬──────────────┐
        ▼              ▼           ▼           ▼              ▼
  [MEAN-REVERSION] [MOMENTUM] [BREAKOUT]  [RANGE-FADE]   [STAND-BY]
   gold buy into    trend-      vol-expansion chop-boundary  regime unclear
   downtrend        continue    w/ confirm    scalp          → no trade
   Z<-2 + ADX<20    ADX>25      H>0.5        H<0.5, low-ADX
   exit @ mean      trailing    exit @ ext   exit @ mid
   stop @ Z<-3.5    stop @ ATR  stop @ inval time-stop
```

**Archetypes (initial set):**

1. **Mean-Reversion (MR)** — *the ID45 gold-buy pattern, generalized.* Entry: deviation beyond threshold (Z<−2 equivalent) **AND** regime range-bound/low-ADX **AND** direction opposes a spent macro/trend read. Exit: return to mean (tighter TP than momentum). Stop: deviation *overshoot* (Z breaches −3.5 = thesis dead = regime shift, not a dip). Time-stop: 2× half-life. **Asset gate:** liquid, range-prone (gold; candidate FX majors). **Explicitly excludes** high-growth/crypto-bull (per thesis).

2. **Momentum / Trend-Continue** — the current default, kept for the symbols it fits (silver buys, crypto). Entry: direction *aligned* with macro/trend, ADX high. Exit: far TP / trailing. This is where the existing EV blend already works best.

3. **Range-Fade** — boundary scalp in confirmed chop (H<0.5, low ADX, defined range). Lower conviction, smaller size.

4. **Breakout** — vol-expansion with confirmation (H>0.5 transition). Reserved; needs its own evidence.

5. **Stand-by** — regime/sentiment unclear → **no trade.** The most important archetype: today the engine *always* produces a directional signal; a router that can say "this is neither MR nor momentum, don't trade" is itself an edge on a book whose base rate is negative.

**How it routes (the decision the monolithic engine lacks):**

```
if asset in MR_WHITELIST and direction opposes macro AND opposes trend
   and vol_regime is low/ranging and deviation > entry_threshold:
       → MEAN-REVERSION archetype (tight TP, overshoot stop, short deadline)
elif direction aligned with macro/trend and trend_regime strong:
       → MOMENTUM archetype (current geometry)
elif range confirmed and boundary touch:
       → RANGE-FADE
else:
       → STAND-BY (suppress)
```

**What changes vs today:**
- The 7-gate chain stays, but **geometry + deadline become archetype-parameterized** (MR gets a tight TP + short deadline + overshoot stop; momentum keeps far TP). One TP size no longer serves both.
- The EV blend / `p_win` becomes **archetype-conditioned** — a calibrated P(win) *per archetype*, not one blend for all. This is also the fix for the uniform overconfidence: MR trades and momentum trades have different base rates and should be priced differently.
- `context_aligned_ok` flips from a *reject-gate* to a *router input* — the very thing it inverted on becomes the MR trigger.

**Sequencing / bars (one lever at a time, era-clean only):**
- **B1 — Validate the gold-buy MR cohort walk-forward.** Era-split the existing XAUUSD/XAUAUD false-cohort; confirm WR holds out-of-sample on post-cutover data. Bar: WR ≥ 1.5× pass-cohort on the held-out half, n ≥ 40. *No build before this.*
- **B2 — Stationarity test.** Hurst H < 0.5 on the gold-buy deviation series (confirms MR is the right model, not momentum-in-disguise).
- **B3 — Shadow the router.** Stamp the archetype each signal *would* route to; log would-MR vs would-momentum outcomes. Zero live change. Bar: ≥200 shadow episodes.
- **B4 — Archetype-conditioned calibration.** Per-archetype P(win) replaces the single blend for the MR cohort. Promote only if MR-cohort bias improves toward 0.
- **B5 — Live MR route for gold buys** behind its own flag, standalone from the main chain.

**Deliberately not yet:** breakout archetype (no evidence), FX-major MR whitelist (untested — B1 extension), any change to the momentum path for silver/crypto (it works; leave it).

---

## 5. Files / lineage

- Era-clean data: `nt_signal_sim` post `2026-10-02`, `contaminated=false`, `v_calibration_records_current`.
- ID45 detail: `worklog/20261010_TIER1_CONTEXT_GATE_INVERSION.md`.
- Reopt era-clean: `worklog/20261010_REOPT_CLAMP_HARMONIZATION.md` (Fix B).
- Quant methodology: z-score/ADX/Hurst mean-reversion stack (external, canonical — see research).
