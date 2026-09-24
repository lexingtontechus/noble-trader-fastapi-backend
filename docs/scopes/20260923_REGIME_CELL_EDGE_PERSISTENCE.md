# Regime-cell edge matrix: persistence, transitions, knobs, covariance

**2026-09-23 · theoretical / research only.** No code, config, schema, or live behavior was changed.
Reproduce with `scripts/regime_cell_edge_persistence.py` (read-only; caches to gitignored `.cache/`).
Every number below comes from one run of that script (sections `== 1` … `== 10`).

## Changelog

| Version | Change |
|---|---|
| v1 | Persistence test. Result: edge decays in about 3–4 days, and momentum ("trade the Leaders") gates lose. |
| v2 | **Correction:** `qualified=false` also marks cooldown-suppressed signals, so "1.4% qualified" understated how many signals pass the gates. **Added:** does a quadrant define an edge; regime and quadrant transition probabilities; per-quadrant knob response; a covariance-matrix framing. |
| v3 | **Stricter statistics; several v2 claims withdrawn.** CIs now use a cluster bootstrap over symbol-days, reversal is measured as excess over each symbol's own base rate, and quadrant transitions are compared with a shuffled-outcome null. See "v3 status" below. Design and next steps: `docs/scopes/20260923_KAIROS_EDGE_STATE_SCOPE.md`. |

## v3 status — what survives the stricter tests

v2 used episode-level bootstrap CIs. Episodes on the same symbol-day share one price path, so v2's intervals were about 2–3× too narrow. v3 re-tests everything with the cluster bootstrap and adds two nulls.

| v2 claim | v3 result (script section) | Status |
|---|---|---|
| Dead quadrant has the best forward edge | G8 forward 7d R +0.162, cluster CI [+0.056, +0.275] (`== 4`). Same-day Dead − all +0.076 vs shuffled null [−0.064, +0.033] (`== 9b`) | **Holds** |
| Quadrants are sticky | 1-day P(stay) exceeds the shuffled null for Leader (0.80 vs 0.64) and Dead (0.79 vs 0.68); Fading is at null (0.76 vs 0.70); Emerging shows nothing (0.74 vs 0.74) (`== 9b`) | **Holds for Leader and Dead only** |
| Edges rotate Leader → Fading → Emerging | Rolling windows alone produce this rotation. Observed Leader→Fading is *below* null at every horizon (1d 0.13 vs 0.25; 7d 0.58 vs 0.64) (`== 9b`) | **Withdrawn**: a mechanical effect of the rolling windows |
| Fading / Dead are "reversal" quadrants, Leader is "continuation" | Excess reversal over the symbol's base rate: every quadrant's CI includes 0. This holds for the price definition at 4h and 24h and for HMM direction flips at 4h and 24h (`== 8`, `== 9c`) | **Not supported.** No quadrant predicts reversal. This applies equally to the user's Dead\|Leader = reversal hypothesis and to v2's Fading/Dead mapping. |
| The best knob flips sign by quadrant | Full stream: no quadrant's knob effect differs from the pooled effect (all CIs include 0). Gate-pass: only Fading tight-TP differs (−0.222 [−0.478, −0.010]), 1 of 8 tests, no multiple-comparison correction (`== 8`, `== 8b`) | **Not supported yet** |
| Tight SL helps | 30-day MFE/MAE shortcut: +0.137 [+0.063, +0.211]. **Superseded:** EURUSD 21-month path-exact re-settlement gives −0.196 R (OHLC) / −0.157 (close); tight TP is +0.119 instead | **Reversed.** See `20260923_KAIROS_EDGE_STATE_SCOPE.md` reconstruction section |
| Symbol × quadrant explains 4× chance | In-sample η² 4.25% vs 1.14% chance (`== 10`) | **Holds in-sample.** Needs an out-of-sample check |
| Correlated clusters (gold, US indices, EUR bloc) | Price correlations of 0.8–0.99; 5.3 effective bets of 14 (`== 10`) | **Holds.** Descriptive, not a prediction |

**Net:** the matrix does identify edge *level* per symbol, and the Dead-quadrant result survives every test so far. The claims about *what to do* in each quadrant (reversal, per-quadrant knobs) are not supported by 30 days of data. These become the pre-registered hypotheses for the next phase. The §2.3 rotation narrative and the §2.4 action map below are kept for the record but are **superseded**.

## Hypothesis (user, v2)

The matrix is for spotting **shifting edges per symbol**, not for setting bracket or tier levers. It is our version of a Magic Quadrant, and it has three parts:

1. **A quadrant defines an edge.** Which quadrant is each symbol in now?
2. **Movement matters.** The probability of staying in a regime or moving out changes risk and p_win.
3. **Each quadrant needs its own knobs, including leverage.** One fixed set of 1–2 global knobs is not enough.

A hedge-fund correlation/covariance view is the suggested frame.

**Verdict (v2 — see v3 status above, which supersedes this):**
- **The hypothesis holds up.** Quadrant state carries real information: the best knob for each quadrant differs in sign, and quadrants persist long enough to act on.
- **v1 rejected one application, not the matrix.** Using the matrix as a momentum filter failed.
- **The user's proposed quadrant→action mapping is half right.** See §2.4.

## Dimensions

| Axis | Source | States |
|---|---|---|
| Volatility | `RegimeHMM.VOL_LABELS` (`regime_engine.py:232`) | low, med_low, med_high, high |
| Trend | `RegimeHMM.TREND_LABELS` (`regime_engine.py:233`) | strong_bear, bear, bull, strong_bull |
| Edge level | 14-day shrunk mean R of the cell | high (> 0) / low (≤ 0) |
| Edge momentum | 7-day minus 14-day shrunk mean R | rising / falling |

**The regime grid.** It is vol × trend: 16 cells per symbol.
- **Trend already contains direction and strength.** The four trend states are {strong, weak} × {bull, bear}, so no separate direction × strength axis is needed.
- **Signal side is not an extra axis.** It equals HMM direction in all 10,789 episodes.
- **The 3×3 in the codebase is a different object.** It is the UP/DOWN/FLAT Markov *transition* matrix used by `p_markov_hold_n`, not a regime grid.
- **G8 is the working grain.** G8 (vol4 × direction2) has more samples per cell and outperforms G16 on every test (§1).

**The quadrant.** It is edge level × edge momentum, computed per symbol × cell. This is the 2×2 Magic Quadrant:

| | Momentum rising | Momentum falling |
|---|---|---|
| **Level high** | Leader | Fading |
| **Level low** | Emerging | Dead |

## Data and method

- **Source:** `nt_signal_sim`, resolved light-sweep rows. 71,511 rows, 14 symbols, 2026-08-23 → 2026-09-22.
- **Unit = episode.** Keep the first row per (symbol, side, hour), giving 10,789 episodes. The light sweep re-emits a setup about 6.6× per hour, so raw rows would overstate n.
- **Qualified vs gate-pass (v2 correction).** `qualified=false` covers both gate failures and **cooldown suppression**. The cooldown (`orchestrator.py:3803-3874`) is a 60-minute same-direction window, plus a hard block while the previous qualified signal is still open.

  | | Rows | Episodes |
  |---|---|---|
  | Delivered (`qualified=true`) | 966 (1.36%) | 8.6% |
  | Pass the value gates | 12,216 (17.2%) | 22.5% |
  | Gate-pass but not delivered (mostly cooldown) | 11,250 | — |

  Gate-pass is reconstructed from stamped values: regime_conf ≥ 0.60, effective_kelly ≥ 0.02, EV > 0, p_win ≥ 0.50 hard floor. The TimesFM-confidence and context gates are not reconstructed, so gate-pass is an upper bound. No delivered row fails the reconstruction.
- **Two cohorts.** **Full stream**: every episode, a research sample of whether edge exists. **Gate-pass**: episodes whose first row passes the value gates, which is close to what the product would send without the cooldown.
- **R per episode.** tp = +tp/sl, sl = −1, expired = 0. A sensitivity run marks expired trades to market.
- **No lookahead.** Lookback windows only use episodes with `resolved_at` before the decision time. Quadrants are recomputed daily.
- **Shrinkage.** Each cell mean is pulled toward its symbol mean with k = 10.

## Part 1: persistence (v1, unchanged)

Overall mean R per episode was −0.051 (expired = 0R).

| Test | Result |
|---|---|
| Symbol edge, 7d → next 7d | ρ = −0.045. The best symbol last week says nothing about next week. |
| Cell ordering within a symbol, 7d → 7d | G8 ρ = +0.102 (80% of anchors > 0, median p ≈ 0.20). G16 is +0.020. |
| Decay (G8, 3.5-day forward window, pushed out by a gap) | +0.192 at 0 days → +0.073 at 3.5 days → −0.005 at 7 days, a half-life of about 3–4 days |
| Quadrant → next-7-day R (G8) | Dead +0.162 [+0.116, +0.209]; Emerging −0.067 [−0.119, −0.012]; Leader +0.037 (n.s.). Holds leave-one-symbol-out. |
| Walk-forward "trade cells with positive edge" | Worse than no gate at every lookback from 3 to 14 days (G8 at 14 days: −0.038 vs +0.022) |

**Reading:** edges do decay fast. A momentum reading of the quadrant (trade the Leaders) loses. Part 2 asks what the quadrant *is* useful for.

## Part 2: applying the matrix

Section 2.1 covers the full 30 days. Sections 2.3 and 2.4 use the daily point-in-time quadrant panel: G8, 2026-09-06 → 09-19, 4,737 episodes, of which 667 are gate-pass.

### 2.1 How long does the regime itself last?

Share of symbol-hours whose HMM label is unchanged after h hours:

| h | 16-cell | Vol | Trend | Direction |
|---|---|---|---|---|
| 1h | 0.50 | 0.66 | 0.67 | 0.79 |
| 4h | 0.39 | 0.56 | 0.55 | 0.72 |
| 24h | 0.24 | 0.42 | 0.41 | 0.62 |
| 48h | 0.12 | 0.33 | 0.31 | 0.55 |

- **The hold period is 48h, and the regime rarely survives it.** A trade entered in a given regime cell is in the same cell at 48h only 12% of the time. Direction survives only 55% of the time, close to a coin flip.
- **So the user's point is correct:** a p_win scored on the entry regime alone is scoring a state the trade mostly will not be in.
- **Regime at exit is not a usable test.** Comparing outcomes by the regime at exit is confounded: price movement itself relabels the HMM, and stop hits exit early. The usable quantity is the *ex-ante* transition probability.

### 2.2 Does a quadrant define an edge?

Share of episode-R variance explained (in-sample η²; "chance" ≈ groups / n):

| Label | η² | Chance |
|---|---|---|
| Symbol | 0.53% | 0.30% |
| Quadrant | 0.28% | 0.08% |
| **Symbol × quadrant** | **4.25%** | 1.14% |

- **The quadrant alone is weak; symbol × quadrant is about 4× chance.** A quadrant only defines an edge *per symbol*, as the user said: "which one for which symbol."
- **Small absolute η² is normal.** Single-trade outcomes are mostly noise. What matters is that the label is well above chance and that the actions differ by label (§2.4).

Snapshot of scored cells on 2026-09-19:

| Symbol | Quadrants |
|---|---|
| BTCUSD | 2 Leader, 4 Fading, 1 Emerging |
| USDCHF, USDJPY, XAGUSD, XAUAUD | Leader |
| EURUSD, GBPUSD, AUDUSD, XAUUSD | Fading |
| US30USD, US500, XAUEUR | Dead |
| USDCAD | Emerging |

### 2.3 Probability of staying or moving between quadrants

P(quadrant at t+h | quadrant at t), per symbol × cell:

| From \ to (1 day) | Leader | Fading | Emerging | Dead |
|---|---|---|---|---|
| Leader | **0.80** | 0.13 | 0.05 | 0.02 |
| Fading | 0.07 | **0.76** | 0.06 | 0.11 |
| Emerging | 0.08 | 0.02 | **0.74** | 0.16 |
| Dead | 0.03 | 0.01 | 0.17 | **0.79** |

| From \ to (7 days) | Leader | Fading | Emerging | Dead |
|---|---|---|---|---|
| Leader | 0.36 | **0.58** | 0.00 | 0.06 |
| Fading | 0.21 | 0.36 | **0.36** | 0.06 |
| Emerging | 0.34 | 0.15 | 0.39 | 0.12 |
| Dead | 0.37 | 0.03 | **0.47** | 0.13 |

- **Quadrants are sticky day to day.** The diagonal is 0.74–0.80 at 1 day and 0.49–0.58 at 3 days, so a quadrant is stable over one 48h hold.
- **Edges follow a cycle.** Over a week they rotate **Leader → Fading → Emerging**, and **Dead → Emerging / Leader**. This is the "edge decays quickly" lifecycle, and it is Markov-shaped, so it can be modelled.
- **What it gives risk and p_win:**
  - **Expected edge over the hold.** Weight it by the quadrant path, not just today's quadrant: `p_win_trade ≈ Σ_q' P_h(q → q') · p_win(symbol, cell, q')`, with h = the expected hold.
  - **Risk from uncertainty about the next quadrant.** It is a mixture: `Var = Σ P(q')·σ²_q' + Σ P(q')·(μ_q' − μ̄)²`. The second term is extra risk that exists only because the quadrant may change. A low P(stay) should therefore shrink Kelly even when the point p_win is unchanged.
  - **This is a generalisation of `p_markov_hold_n`.** That applies the same idea to three direction states. The quadrant chain applies it to *edge state*.

### 2.4 Per-quadrant knobs

These are counterfactuals from the resolver's close-based MFE/MAE:
- **Tight SL ×0.5** is scored per unit of risk taken.
- **Tight TP ×0.5** banks at half the TP distance if MFE reached it.
- **Reversed@24h** is the share of episodes whose 24h drift ran against the signal.

**Gate-pass cohort** (closest to the product), knob deltas in R per unit risk, 95% bootstrap CI:

| Quadrant | n | R/ep | tp / sl / exp | Tight SL ×0.5 | Tight TP ×0.5 | Reversed@24h | Kelly |
|---|---|---|---|---|---|---|---|
| Leader | 267 | −0.009 | .12 / .25 / **.64** | **−0.112** [−0.22, −0.01] | **+0.125** [+0.05, +0.20] | 0.39 [0.33, 0.45] | 0 |
| Fading | 89 | +0.143 | .33 / .58 / .09 | **+0.394** [+0.11, +0.68] | **−0.255** [−0.41, −0.10] | **0.64** [0.53, 0.76] | 0.063 |
| Emerging | 195 | **−0.308** | .16 / **.64** / .19 | **+0.178** [+0.05, +0.31] | −0.090 [−0.17, −0.01] | 0.37 [0.28, 0.46] | 0 |
| Dead | 116 | **+0.257** | .30 / .44 / .26 | +0.259 [−0.01, +0.52] | −0.131 [−0.31, +0.05] | 0.57 [0.45, 0.70] | **0.121** |

**Full stream** (larger n, same signs for SL/TP except Leader SL): tight SL helps Emerging (+0.159) and Dead (+0.196), both CIs clear of zero. Tight TP hurts Fading and Emerging. Only Fading reverses more than half the time (0.55 [0.50, 0.59]).

**What this shows:**
- **The best knob flips sign across quadrants.** Tight SL hurts Leader but helps Fading. Tight TP helps Leader but hurts Fading. No single global SL/TP setting is right everywhere, which supports "each quadrant needs a different knob."
- **The user's mapping, checked:**
  - **"Fading + Emerging → go hard on SL/TP": half right.** Hard on **SL**, yes. Not TP: tightening TP loses in both. Emerging stays negative even with a tight SL (−0.308 + 0.178), so its real knob is **size down or skip**.
  - **"Dead | Leader → look for reversals": half right.** Dead leans toward reversal (0.57, CI crosses 0.5). **Leader is the continuation quadrant** (0.39, CI below 0.5). **Fading is the reversal quadrant** (0.64). The pairing runs along the diagonal of the table, not across its rows.

**Resulting action map (theoretical):**

| Quadrant | Behaviour | Primary knob | Leverage |
|---|---|---|---|
| Leader | Trend continues, but 64% of brackets expire | **Bank earlier** (tighter TP / time exit); keep SL wide | Base size |
| Fading | Trend exhausting; reversal 64% | **Tight SL**; keep TP; reversal-watch flag | Moderate |
| Emerging | Worst R; 64% stopped out | **Size down / skip**; tight SL if taken | Minimal |
| Dead | Best R; mean-reversion payoff | **Tight SL**, full TP | **Highest** (Kelly 0.12) |

### 2.5 Covariance-matrix frame

A quant fund separates four jobs:

| Hedge-fund layer | Platform equivalent |
|---|---|
| **Alpha model**: expected return μ per sleeve | Quadrant × cell × symbol (§2.2, §2.4) |
| **Regime switching**: how μ changes | Quadrant transition matrix P (§2.3) and HMM dwell (§2.1) |
| **Risk model**: covariance Σ, regime-conditional | Measured below |
| **Portfolio construction**: size ∝ Σ⁻¹μ, capped (multi-asset Kelly) | Per-quadrant knobs + leverage, limited by cluster exposure |

What Σ shows today (Ledoit-Wolf shrinkage):

- **Price returns (4h): 14 symbols are about 5.3 independent bets** (first principal component 38%). The gold complex is one bet: XAUUSD / XAUEUR / XAUAUD correlate 0.97–0.99, and XAGUSD 0.82–0.88 with them. US30 / US500 correlate 0.88. EURUSD / USDCHF −0.82, EURUSD / GBPUSD +0.81. Today, Kelly is computed per signal in isolation, so three same-direction gold signals are sized as three bets when they are really one bet at 3× size.
- **Strategy returns (daily mean R per symbol): mean pairwise ρ +0.14, about 11.3 effective bets.** The signal streams are far less correlated than the prices, because they fire at different times and in different directions.
- **Quadrant sleeves (daily R, 14 days):** Leader–Fading 0.72, Fading–Dead 0.12, Fading–Emerging 0.17. Fading and Dead diversify each other, but 14 days is noisy.
- **No evidence yet that correlation rises in high vol** (strategy ρ 0.11 low-vol vs 0.10 high-vol). The sample is too short to trust that.

**Implications:**
- **First use: cap exposure per correlated cluster.** It needs Σ only, not an edge estimate, so it is the cheapest and least overfit.
- **Do not use full Σ⁻¹μ yet.** 31 days of daily R is too short, and inverting a noisy matrix amplifies estimation error. Clustered risk budgets (HRP-style) are the robust alternative.
- **Structural gap:** the HMM is fit **per symbol**. Fund-style regime models are **joint**: one regime drives the cross-asset μ and Σ. That is a longer-term direction, not a near-term build.

## Caveats

- **Short window.** 30 days overall; the quadrant panel covers 14 days. The gate-pass cells are small (n = 89 to 267), and the knob CIs are not adjusted for multiple comparisons (2 knobs × 4 quadrants × 2 cohorts).
- **The tight-SL benefit is overstated for live trading.** MAE and settlement use hourly closes. Intrabar wicks would stop out more tight stops than this counts. The result is consistent with the settlement convention, not with execution.
- **Knob counterfactuals are in-sample.** Every result is conditioned on quadrant labels computed point-in-time, but the knob menu was chosen after seeing this data.
- **Gate-pass is reconstructed** (it omits the TimesFM-confidence and context gates).
- **Confounds.** Geometry changed during the window, and 30% of episode-hours have more than one regime label.

## Recommendations

1. **Adopt the four-layer model** (alpha state / transitions / risk / action) as the working theory. Drop "trade the Leaders."
2. **Capture labels point-in-time before building any gate:** stamp `edge_quadrant` and `p_stay_1d` on `nt_signal_sim` in shadow. The 60-day re-run can then use labels as they were at signal time instead of reconstructing them.
3. **Test the transition-weighted p_win offline:** is `Σ P_h(q→q')·p_win(q')` better calibrated than point p_win on the same episodes?
4. **Covariance first:** a cluster exposure cap (gold complex, US indices, EUR bloc) as a shadow comparison against today's independent per-signal Kelly.
5. **Re-run at about 60 days (around 2026-10-22).** Pre-registered pass criteria on the *new* weeks only (out of sample):
   - Tight-SL delta > 0 in Fading and Emerging, and ≤ 0 in Leader.
   - Tight-TP delta > 0 in Leader.
   - Fading reversal rate CI above 0.5.
   - 1-day quadrant diagonal ≥ 0.7.
   - Each result holds leave-one-symbol-out.

   Per-quadrant knobs move to a shadow build only if the signs replicate.
