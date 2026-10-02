# Signal quality — what we are trying to achieve, and the metric that measures it

**Date:** 2026-10-02 · **Status:** THESIS — §3.3 TESTED, result below
**Parents:** `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` (the E1/R1/C4 findings this answers),
`worklog/kairos/reports/20261002_calibration_decomp.md` (the measurement that forced this),
`worklog/kairos/20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md` (the binding quant cut-list)
**Supersedes:** the implicit objective in `docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md`

---

## ⚠ §3.3 TESTED — the thesis is INCOMPLETE (2026-10-02)

The falsifiable test ran. `worklog/kairos/reports/20261002_cell_conditioning.md`.

60d, 38,038 resolved light signals, 5-fold expanding walk-forward, every cell rate
fit on training rows only:

| model | AUC (out-of-fold) | AUC (in-sample ceiling) |
|---|---:|---:|
| `cell` (symbol × vol_tier × bucket) | **0.4994** | 0.7256 |
| `cell+dir` | 0.5318 | 0.7915 |
| `cell+trend` | 0.5301 | 0.8262 |
| `cell+p_win` | 0.5115 | 0.7994 |

**Three findings, all of which change the plan:**

1. **The cell does NOT transfer across time.** In-sample 0.7256 (real
   discrimination — the cell genuinely orders outcomes) → out-of-fold 0.4994
   (chance). The historical reachability rate does not predict the future
   reachability rate over a 60-day window. This is **non-stationarity**, and it is
   a far more serious problem than the category error: even a perfectly specified
   `P(win | cell)` is useless if the cell's rate moves under it.

2. **No conditioning transfers either.** Every direction term improves in-sample
   (0.79–0.83) and collapses out-of-fold (0.51–0.53), with gains *inside* the
   ±0.0566 fold spread. `cell+p_win` — the live blend — adds **+0.0121**, below
   the noise bar. Direction is not a conditioning term worth adding; it is not
   even a reliable one.

3. **The bucket space is degenerate.** **71.2% of all rows sit in the single `4+`
   bucket** (27,101 of 38,038). A key of `(symbol, vol_tier, bucket)` is therefore
   barely finer than `(symbol, vol_tier)` — the distance/ATR dimension is not doing
   the work the thesis assumed. This is audit **G2 in data**: brackets are built in
   %-price space, so almost everything lands in the widest ATR bucket.

**What survives of the thesis:** geometry *does* carry the signal in-sample, so
"the cell is the model" is not wrong — but it is not yet *usable*, because the cell
is non-stationary and its bucketing is degenerate. The correction is that the
reachability table must become **time-local** (rolling window, or an explicit decay
on the cell rate) and the geometry must be **generated in ATR space** so the buckets
have occupancy. Until both are true, `P(win | cell)` cannot be the primary model.

**What does not survive:** the claim that conditioning on direction is a live
question worth testing further. It failed the bar. The live `p_win` blend adds
+0.0121 out-of-fold — nothing.

---

## ⚠ FOLLOW-UP — recency does NOT fix it, but the rate IS non-stationary (2026-10-02)

`worklog/kairos/reports/20261002_reachability_decay.md`. The two tests disagree, and
the disagreement is the finding.

**Test 1 — does a trailing window recover transfer? NO.**

| training lookback | out-of-fold AUC | mean train n |
|---|---:|---:|
| 7d | 0.4905 | 7,945 |
| 14d | 0.5093 | 16,580 |
| 30d | 0.5003 | 26,082 |
| 60d | 0.5003 | 26,246 |
| all history | 0.5003 | 26,246 |

No window clears 0.55. **14d is nominally best at 0.5093 — but that gain is 0.009,
which is noise, and 7d is *worse* (0.4905).** The pattern is non-monotone, which is
what a noise signal looks like.

**Test 2 — is the rate unstable? YES, strongly.**

| metric | value |
|---|---:|
| mean abs drift between consecutive eras | **0.1996** |
| mean range across eras | 0.3461 |
| sign-flip share (crossing 0.5) | 24.1% |
| split-half correlation | **0.1636** |

A cell's rate moves ~0.20 between eras, and first-half rates explain only 16% of the
variance in second-half rates. The era table shows it plainly — `AAPL · low_vol · 4+`
walks `0.00 → 0.06 → 0.03 → 0.63 → 0.35 → 0.12`.

**The two together mean something neither says alone.** The rate *does* move
(non-stationarity is real), but a trailing window does not recover transfer. That
combination is consistent with a third explanation the two-way framing missed:

> **The cell rate moves because it is dominated by a shared market-wide factor, not
> because each cell has its own drifting property.**

If cell rates are all loading on one common driver, then (i) they move together, so
cross-sectional correlation is high — but (ii) the *ranking* between cells barely
changes, so a per-cell rate carries little ordering information, and (iii) re-fitting
on a recent window just re-estimates the same shared factor noisily. That fits every
observation: strong drift, high sign-flip, low split-half corr (because the shared
factor moves the *level*, not the ordering), and no window helping.

**Critical constraint on this test:** the available resolved history is only **36
days** (2026-08-26 → 2026-10-02) — `nt_signal_sim` does not reach back further for
resolved light rows. So 30d/60d/"all" are the *same fit*, and each era is only ~6
days. **A longer history is required before the window sweep can be trusted**; the
36-day bound is the first thing to fix.

**Consequence for the thesis:** the §3.3 correction stands (time-local + ATR-space
geometry are both still required), but the mechanism is now better described as a
**common-factor problem**. The testable next question is whether the cell rates'
residuals *after* removing a market-wide factor carry any ordering — which is the
same question as "is there a cross-sectional edge, or only a time-series one".

---

## 0. The one-paragraph version

We are not trying to predict price direction. We are trying to **select brackets whose
expected value after costs is positive, and size them.** "Signal quality" is therefore
three different questions that the platform has been collapsing into one word —
*can the score rank?* (discrimination), *are its numbers true?* (calibration), *does
acting on it make money?* (edge). Accuracy/hit-rate is the wrong headline metric for all
three, because it ignores payoff asymmetry and is base-rate dependent. The primary metric
should be **AUC against the first-touch bracket outcome, measured walk-forward**, supported
by **BSS/reliability** (for sizing) and **net expectancy** (the economic bottom line). And
the model that produces the probability should be the **reachability cell**, not a blend of
direction signals — because the reachability cell already out-ranks the entire EV blend on
the event that pays (`cell_p` AUC 0.657 vs `p_win` 0.491).

---

## 1. What we are trying to achieve

The platform emits a **tradable object**: `(symbol, direction, geometry, size)`. The
downstream consumer is a bracket order with a hard deadline. Its outcome is one of three
things, and only one of them is what the system is paid for:

| outcome | PnL | frequency (light, 45d) |
|---|---|---|
| TP touched first | `+tp_bricks × brick_size` | 11,358 (13.6%) |
| SL touched first | `−sl_bricks × brick_size` | 30,327 (36.3%) |
| expired at deadline | mark-to-market | 41,316 (49.4%) |

**The objective is realized expectancy on that object, after costs.** Everything else —
`p_win`, regime labels, TimesFM, the HMM — is machinery that either helps or doesn't.

Two consequences that the current design violates:

1. **"Direction" is not the objective.** Being right about direction while the bracket
   times out pays nothing — the audit's own example: *"gold pairs often had TP distance
   4+ ATR away; directional models could be 'right' while every trade timed out."*
2. **Half the population expires.** Any metric computed only on resolved rows describes a
   selected sub-population. The audit calls this out as C2/R6 (survivorship).

---

## 2. What "signal quality + accuracy" means

### 2.1 The three axes (and why one word cannot hold them)

| axis | question it answers | metric | what it is FOR |
|---|---|---|---|
| **Discrimination** | can the score *rank* opportunities? | AUC (binary), Spearman IC (continuous) | the **selection gate** — "take this, skip that" |
| **Calibration** | are the stated probabilities *true*? | reliability curve, REL, Brier, BSS | the **sizing layer** — Kelly consumes the level |
| **Edge** | does acting on it make money? | net `expectancy_r`, ΔD with CI | the **bottom line** — go/no-go |

These are independent. A constant is perfectly calibrated and has zero discrimination and
zero edge. A sharp-but-overconfident score has discrimination, no calibration, and *possibly*
edge if you recalibrate. **You need discrimination to have edge at all; you need calibration
to size correctly.** A gate can only exploit discrimination; Kelly can only exploit calibration.

### 2.2 Why "accuracy" / hit-rate is rejected as the headline metric

| problem | why it breaks here |
|---|---|
| **Ignores payoff asymmetry** | RR ≈ 2:1 means a 34% win rate is profitable. Hit-rate would call that "bad" and the platform's own history shows exactly that trap: *"win rate looked like a normal 30% because it only measured the 40% that resolved."* |
| **Base-rate dependent** | The bracket base rate is 27.3% on light signals and varies by symbol (US500 28%, EURUSD 56% directional). 60% accuracy on one symbol is worse than 45% on another. Not comparable. |
| **Blind to ranking** | Accuracy at a fixed threshold says nothing about whether reordering the population would help. The platform's decision is *"which of ~1,400 signals do I take?"* — a ranking problem. |
| **Collapses calibration into it** | The audit's C4: *"Mean bias only — no reliability curve; zero bias ≠ calibrated."* Accuracy cannot see an overconfident S-curve that happens to cross the diagonal at the mean. |

**Replacement:** report accuracy nowhere as a primary number. Use AUC for ranking, BSS for
truth, expectancy for money.

### 2.3 The metric set

**Primary — discrimination, because it is the platform's actual decision:**

```
AUC(score, Y_bracket)          Y_bracket = 1 if TP touched before SL within deadline
```
- Base-rate free (0.5 = no information, invariant to the event's rarity)
- Answers the selection question directly
- Measured **walk-forward** and **per symbol** (the population is not homogeneous — 8 of 14
  symbols currently have directional AUC > 0.5, and US500 is inverted at 0.428)

**Supporting — calibration, because Kelly consumes the level:**

```
REL, RES, UNC, Brier = REL − RES + UNC        (Murphy)
BSS = 1 − Brier/UNC                            (> 0 = beats the base rate)
reliability curve per (symbol, bucket)         (the shape, not just the mean)
```

**Supporting — edge, the bottom line:**

```
expectancy_r = p_win·(tp/sl) − (1 − p_win)·1        # in R units, net of costs
```
computed on **all** resolved paths including expired-as-MTM (fixes R6 survivorship), and
reported as **ΔD with a confidence interval** (the platform's existing shadow-gate idiom).

---

## 3. The thesis

> **The probability that pays is `P(reach TP before SL)`, and its dominant driver is the
> geometry cell — not the direction blend. The reachability table is therefore the model,
> and the direction signals are at most conditioning terms on it.**

### 3.1 The evidence, from measurements already in the repo

| score | AUC vs bracket/TP event | source |
|---|---:|---|
| `cell_p` (regime-cell base rate) | **0.657** (all eras) · 0.741 (post-debounce) | `kairos/reports/20260926_rank_score_eval.md` |
| `rank_score` (kairos composite) | 0.670 | same |
| `p_win_bracket` (`p_resolve × p_win_resolve`) | 0.566 | `20261002_calibration_decomp.md` |
| **`p_win` (the live EV blend)** | **0.486** | same |
| `p_win` (kairos, independent measurement) | 0.491 | `20260926_rank_score_eval.md` |

**The cell base rate out-ranks the entire four-source EV blend by ~0.17 AUC, on the event
that pays.** Two independent measurements agree the blend is at chance. That is the thesis
in one line: the machinery is pointed at the wrong variable.

### 3.2 Why geometry dominates

Reachability is `distance / ATR14`, bucketed `{<0.5, 0.5-1, 1-2, 2-4, 4+}`, keyed
`(symbol, vol_tier, bucket)`. A bracket 0.3 ATR away is reached almost always; 4+ ATR
almost never — and this holds **regardless of direction**. Direction is a second-order
modifier on top of a first-order geometric fact.

The audit's G2 confirms the platform has been building geometry in the *wrong space*:
*"primary candidates are price-% tiers, not vol-normalized; gold family historically landed
in 4+ ATR buckets."* The grid searches %-price; reachability lives in ATR space.

### 3.3 Where direction signals belong

Not deleted — **demoted to conditioning**. The testable question:

> Does conditioning the reachability cell on regime / TimesFM improve AUC over the cell
> alone?

If yes, they enter as a multiplicative adjustment to the cell base rate. If no, they are
noise and should be removed from the P(win) role entirely (they may still be useful
elsewhere). This is a *measurable* question with a clear bar, unlike the current blend
whose contribution is asserted by weights rather than demonstrated.

---

## 4. Methodology

### 4.1 Fix the target first (before any model)

| defect | current | required |
|---|---|---|
| R1 | close-walk, not first-touch | first-touch (OHLC high/low), so TP-vs-SL ordering is honest |
| C2/R6 | expired excluded from calibration | expired → MTM, included in every population |
| C1 | raw `p_win` capture default **off**; NULL falls back to enforced | capture raw on; never silently substitute enforced for raw |
| R3 | no costs on production PnL | costs (spread + slippage) on every decision-relevant expectancy |
| R4 | proxy ATR uses *current* ATR on historical rows (look-ahead) | point-in-time ATR, or exclude proxy rows from go/no-go evidence |

**No model work is meaningful until these are fixed** — they change what the labels *are*.

### 4.2 Establish the ceiling

Before building anything, measure the best *available* predictor so we know what "good"
looks like: the reachability cell base rate, per symbol, walk-forward. That number is the
bar every model must beat.

### 4.3 The model, in order of decreasing confidence

1. **Cell base rate** — `P(win | symbol, vol_tier, bucket)`, Beta-shrunk toward the global
   rate by sample size (already in `p_win_bracket`; `α=β=2` prior). This is the baseline.
2. **Conditioning** — test regime / TimesFM / direction as modifiers on (1). Keep only what
   improves walk-forward AUC.
3. **Recalibration** — monotone map (isotonic, or Platt if the distortion is smooth) fitted
   on rolling out-of-fold windows, frozen before the next evaluation slice. *Never* fitted
   and evaluated on the same data — the platform's existing calibration loop is already
   suspect here (the audit's C1/C6).

### 4.4 Measure

Walk-forward only (time-ordered folds, embargoed — the platform's own kairos discipline).
Random splits leak. Report per fold, per symbol:

```
AUC        discrimination          bar: > 0.55
BSS        calibration             bar: > 0
reliability curve                  bar: monotone, gaps < 0.10 in the traded range
net expectancy_r with CI           bar: > 0 after costs
```

### 4.5 Promotion bar (explicit, so "better" is falsifiable)

A candidate replaces the current `p_win` in the P(win) role only if, walk-forward and
out-of-sample:

- **AUC ≥ 0.55** on the first-touch bracket event (currently 0.486), **and**
- **BSS > 0** (currently −0.113 directional / −0.310 bracket), **and**
- **net `expectancy_r` > 0 with a CI excluding 0**, **and**
- the gain survives **per-symbol** (not carried by one symbol)

---

## 5. How this fits with reachability

**Reachability is not a side-diagnostic. It is the model.**

```
P(win) = P(reach +TP before −SL within deadline | symbol, vol_tier, distance/ATR, regime)
         └─────────────── already measured in nt_bracket_reachability ───────────────┘
```

| component | exists? | role under this thesis |
|---|---|---|
| `nt_bracket_reachability` (per symbol × vol_tier × bucket: `pct_hit_tp`, `pct_hit_sl`, `pct_expired`) | **yes** | the **prior** — the baseline probability |
| `p_win_bracket = p_resolve × p_win_resolve` | **yes** | the **current implementation** of the thesis, partial |
| `reachability_aware_pick` (filter on `expectancy_r > 0`) | **yes, shadow** | the **selection** path — already economic, promote it |
| `deadline_aware_sharpe` (heavy-sweep grid pick) | **yes, live** | selection on Sharpe, *not* on calibrated reachability — this is the gap |
| direction blend (`p_win`) | **yes, live** | demoted to a **conditioning term** pending the AUC test |

**The selection gap:** heavy sweep picks geometry by `deadline_aware_sharpe`; the
reachability-aware expectancy filter is shadow. Under this thesis, selection should be on
**net expectancy of the calibrated bracket probability**, and the geometry grid should be
searched in **distance/ATR space** (where reachability is defined) rather than %-price space
(fixes G2). `reachability_aware_pick` is already the right idea — it needs promotion and a
corrected expectancy (costs + expired).

---

## 6. What this changes

| current | proposed |
|---|---|
| `p_win` (direction blend) used as P(win) in EV | `p_win_bracket` (reachability) as P(win); direction demoted to conditioning |
| objective implicit | objective stated: net expectancy on the bracket, after costs |
| accuracy / mean bias as health metric | AUC (discrimination) + BSS/reliability (calibration) + expectancy (edge) |
| heavy pick on Sharpe; expectancy shadow | selection on net expectancy; geometry searched in ATR space |
| expired excluded from measurement | expired as MTM, included everywhere |
| close-walk resolution | first-touch resolution |

## 7. Open questions this thesis does not settle

1. **Does conditioning improve on the cell alone?** (the AUC test — §3.3)
2. **Is the expired population predictable, or is it noise?** If the 49% expiry rate is
   driven by geometry, it is already in the cell; if it is regime-driven, that is a new feature.
3. **What are the real costs?** Spread is symbol- and session-dependent; until it is on the
   expectancy path, every "positive expectancy" number is gross.
4. **Does the current calibration loop help or hurt?** The audit's C6 describes a
   self-starving loop (penalty → fewer records → insufficient data → penalty off →
   overconfidence returns). The enforcement may be actively destroying the signal it measures.

---

## 8. Related

- `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` — the E1/R1/C4/R6/G2 findings
- `worklog/kairos/reports/20261002_calibration_decomp.md` — the measurement (Murphy + AUC, both labels)
- `worklog/kairos/reports/20260926_rank_score_eval.md` — cell_p / rank_score vs p_win
- `worklog/20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md` — binding quant cut-list
- `regime_platform/sweep_orchestrator/bracket_reachability.py` — the table
- `regime_platform/sweep_orchestrator/p_win_bracket.py` — the partial implementation
- `regime_platform/sweep_orchestrator/heavy_sweep_reachability.py` — the shadow selection path
- `regime_platform/sweep_orchestrator/calibration_decomp.py` — the instrument (Murphy + AUC)
