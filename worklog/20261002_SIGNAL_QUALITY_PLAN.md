# Signal Quality Plan — best of both methods, immediate fixes, end-state calibration

**Date:** 2026-10-02 · **Type:** plan worklog (supersedes the recommendation sections of
`docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` and
`docs/scopes/20261002_EDGE_CALIBRATION_BENCHMARK.md`)
**Status:** PLAN — nothing built from §4 onward. §3 items are testable now.

---

## 0. The one-paragraph version

`p_win` cannot rank (bracket AUC 0.486), is overconfident (`REL` 0.0692 vs `RES` 0.0016),
and its realised base rate is 0.1186 once you require the trade to have *survived* its
own stop (not 0.2805 as the close-walk label claims). The reachability cell carries real
cross-sectional information (~0.5486 AUC) but is only partly a property of the cell —
47% of its variance is cell×time interaction. **Sizing is the immediate, low-risk lever**:
Masaniello's `M_i`/`DD_i`/`V_i` factors are already `p_win`-free and already correct, so
they can be switched on now, while `p_win` is progressively replaced by a calibrated
reachability probability as the entry signal. Calibration ends when a walk-forward,
out-of-sample survivor probability beats the base rate (`BSS > 0`), ranks at `AUC ≥ 0.55`,
and produces positive net expectancy after costs — and when sizing no longer consumes
`p_win` at all.

---

## 1. What the two methods are (and are not)

The two analyses are **complementary layers**, not competing replacements. Neither
replaces Kelly; Masaniello is applied *on top of* the sizing stack as a strategic
calibration layer.

| | what it governs | mechanism |
|---|---|---|
| **Kelly** (`PositionSizer`) | the **base risk fraction** — how much of equity to risk given the trade's own statistics | `kelly_f` from `_mu`/`_sigma2` (direction-aware, worklog 20260904), annualised |
| **EV scale** (`ev_engine`) | the **confidence multiplier** on that fraction | `ev_scale = min(1, ev_per_dollar / 0.50)` — a function of `p_win` |
| **Masaniello** (`DynamicMasanielloSizer`) | the **strategic/batch calibration layer** — urgency, drawdown, volatility, regime quality | `f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i` |

Live chain today (`orchestrator.py:3860`):

```
effective_kelly = kelly_f × ev_scale        ← both terms; ev_scale is the p_win-dependent one
```

**The claim being corrected.** "Masaniello replaces Kelly" is wrong on two counts: it is
not a replacement (it multiplies a base risk fraction, which is what Kelly produces), and
it does not remove the `p_win` dependency, because its `Q_i` contains `prob_factor`.

**The claim that IS right.** Masaniello's `M_i`, `DD_i`, `V_i` are `p_win`-free, already
implemented, already correct, and currently **unused in the sweep path**. That is the
strategic opportunity: three of its five factors can be adopted *now* with no dependency
on the unresolved probability question.

---

## 2. What is actually broken (measured, not asserted)

| # | finding | number | source |
|---|---|---|---|
| 1 | `p_win` is directional, not a P(win) | bracket AUC **0.4869** | `calibration_decomp` |
| 2 | `p_win` is overconfident, near-zero resolution | `REL` 0.0616 vs `RES` 0.0008 | same |
| 3 | the reachability cell has signal that does not transfer | in-sample 0.7256 → out-of-fold 0.4994 | `cell_conditioning` |
| 4 | the bucket space is degenerate | **71.2%** of rows in one bucket (`4+`) | same |
| 5 | no trailing window recovers transfer | best 0.5093, refuted on replication | `reachability_decay` |
| 6 | the cell is cross-sectional but unstable | cell 43% / time 10% / **interaction 47%** | `common_factor` |
| 7 | **ghost edge — the label is optimistic** | survivable base rate **0.1186**, not 0.2805; **57.7%** of bracket "wins" never survived the stop | `calibration_decomp` §1b |
| 8 | `p_win` hard floor discards good setups | `min_prob = 0.50` | audit E7; `SizingConfig` |

**Finding 7 is the most consequential and was the last found.** The platform's outcome
label is produced by a close-walk that checks TP before SL on the same bar. Re-labelling
against intrabar MAE (migration 170) collapses the win rate by more than half. Every
calibration number the platform has ever computed is against the optimistic label.

---

## 3. Immediate fixes (testable now, no new data)

Ordered by (risk × value). Items 3.1–3.3 need no schema change and no more history.

### 3.1 Adopt the survivable outcome label — the highest-value fix

**What:** define a win as *TP reached **and** MAE never breached the stop distance*,
computed from intrabar highs/lows, not closes.

**Why first:** every downstream number — bias, Brier, Kelly, expectancy — is currently
computed against a label that overstates wins by up to 2.4×. Fixing the label fixes the
target of every other fix.

**Blocker:** the resolver (`eod_resolver.resolve_signal_outcome`) is a **close-walk**,
while the MAE columns are intrabar. The 57.7% figure is therefore an upper bound and
cannot be adopted as-is. Two steps:

1. Make the resolver intrabar-consistent (use the same highs/lows the MAE columns use) —
   this is audit **R1**, *"close-only ≠ first-touch"*, still open.
2. Re-measure the survivor base rate on the corrected label.

**Kill criterion:** if the corrected survivor base rate is within ±0.02 of the current
bracket base rate, the ghost edge was a close-vs-intrabar artefact and this fix is
unnecessary. *Do this measurement before building anything on top of it.*

### ✅ RESOLVED — kill criterion NOT met; the label is genuinely optimistic

Both dependencies built and the measurement run.
`worklog/kairos/reports/20261002_resolver_basis_eval.md`.

- **`resolve_signal_outcome_ohlc`** — intrabar first-touch resolver (18 tests).
- **`eod_resolver_basis`** flag — `close_walk` (default, byte-identical) | `intrabar`,
  wired through both the writing resolver and the hourly dry run (38 tests).
- **The measurement** — both labels on a consistent intrabar basis, at every horizon:

| horizon | BRACKET_INTRA | SURVIVABLE | ghost gap | % of bracket | kill |
|---|---:|---:|---:|---:|---|
| 4h | 0.1681 | 0.0522 | **0.1158** | 68.9% | NOT MET |
| 24h | 0.2463 | 0.0570 | **0.1893** | 76.9% | NOT MET |
| 48h | 0.2588 | 0.0690 | **0.1897** | 73.3% | NOT MET |

**The gap is positive at every horizon, so it is not a horizon artefact.** The ghost
edge survives when both legs share the intrabar basis — the close-walk label is
genuinely optimistic, not a measurement artefact.

**Two effects were stacked in the earlier 0.2805 → 0.1186 drop, now separated:**

- the **basis change** (close-walk → intrabar) accounts for **+0.0265**
- the **survival requirement** accounts for the remaining **~0.19**

**Discrimination is unaffected by the label choice.** AUC is at or below 0.5 under every
label (close-walk 0.4861; intrabar bracket 0.4491–0.4605; survivable 0.4587–0.4880). So
the label debate is **orthogonal to the model's ranking ability** — fixing the label
does not and cannot restore discrimination. That remains a Phase 3 problem.

### 3.2 Freeze `p_win` as an entry signal; stop letting it size

**What:** remove `ev_scale` from `effective_kelly`, or replace its `p_win` input with a
constant until a calibrated probability exists.

**Why:** `ev_scale` currently multiplies position size by a quantity with **negative**
skill (AUC 0.486). Sizing on an anti-predictive number is worse than not sizing on it.

**Evidence this is material:** withdrawing the `p_markov` source raised mean `ev_scale`
0.5576 → 0.8403 and moved **23% of signals from unsized to sized** (`leverage_eval`).
The sizing lever is sensitive to a source whose skill is below the naive base rate.

**Do NOT** simply revert `p_markov_hold_n_mode` — that restores an anti-predictive blend.
The honest move is to decouple size from `p_win` while the probability question is open.

### 3.3 Raise `min_prob` off the 0.50 floor (audit E7)

**What:** `SizingConfig.min_prob = 0.50` is a hard floor that rejects any trade whose
probability is below 0.50 **regardless of reward/risk**. At RR 2:1, the break-even
probability is 0.333 — the floor discards every trade in the 0.333–0.50 band, which is
where positive-expectancy high-RR setups live.

**Why now:** it is one constant, and it is currently doing the *opposite* of its
intent — rejecting edge, not protecting against it. It also governs Masaniello's
`prob_factor`, so it must be settled before Masaniello's `Q_i` is trusted.

**Fix:** replace the floor with a **reward/risk-aware** break-even check
(`p_win > 1/(1+RR)`), which is what the floor was approximating.

---

## 4. The plan — adopting the best of both methods

Four phases. Each has a bar, and each is independently abandonable.

### Phase 1 — Label integrity *(gates everything)*

| step | detail |
|---|---|
| 1.1 | Intrabar-consistent resolver (audit R1) |
| 1.2 | Re-measure survivor base rate; run the §3.1 kill criterion |
| 1.3 | Re-run all four instruments against the corrected label |
| **bar** | corrected survivor base rate is stable across eras (not just a level shift) |

**Rationale:** every other phase is measured against this label. Building on the
optimistic one would calibrate the model to an outcome that does not occur.

### Phase 2 — Sizing decoupled from `p_win` *(immediate, low-risk)*

Adopt Masaniello's `p_win`-free factors as the strategic calibration layer, keeping Kelly
as the base fraction.

```
effective_kelly  = kelly_f × V_i × DD_i × M_i        ← the p_win-free backbone
                   (Kelly)   (vol) (drawdown) (batch urgency)

prob_factor      = f( calibrated reachability P(survive) )   ← Phase 3 fills this in
                   not raw p_win
```

| factor | source | status |
|---|---|---|
| `V_i = clip(ATR_base/ATR_cur, 0.5, 1.5)` | inverse-vol | already implemented, correct |
| `DD_i = clip(1 − dd/max_dd, 0.25, 1.0)` | drawdown | already implemented, correct |
| `M_i = (W − w)/(N − i + 1)` | batch urgency | already implemented, correct |

**Note on `M_i`:** batch urgency is only meaningful inside a **defined batch**. The sweep
emits signals continuously with no batch boundary, so `M_i` needs an explicit batch
definition (e.g. per-symbol rolling window of N signals) before it means anything. This
is real design work, not a flag flip — flagged as such.

| | |
|---|---|
| **bar** | sizing no longer consumes `p_win`; realised risk-per-trade is bounded by `DD_i` in a drawdown replay |
| **kill** | if `V_i`/`DD_i` show no effect on realised drawdown or PnL dispersion in replay, they are decoration — drop them |

### Phase 3 — Calibrate the probability *(the core work)*

Replace the `p_win` blend with a calibrated survivor probability.

**Model:** the reachability cell, `P(survive | symbol, vol_tier, distance/ATR)`, with
the interaction problem addressed rather than ignored.

| step | detail |
|---|---|
| 3.1 | **Fix the bucket degeneracy** (finding 4): generate geometry in **ATR space**, not %-price space (audit G2). A bucket holding 71% of rows cannot discriminate. |
| 3.2 | **Address the interaction** (finding 6): 47% of cell-rate variance is cell×time. Time explains only 10%, so the driver is cell-specific and time-varying — test local vol state, session, microstructure as additional keys. |
| 3.3 | **Recalibrate**: isotonic or Platt on **rolling out-of-fold folds**, frozen before each evaluation slice. Never fit and evaluate on the same data. |
| 3.4 | **Feed it to `prob_factor`** in Masaniello's `Q_i` (Phase 2's placeholder). |

| | |
|---|---|
| **bar** | walk-forward, out-of-sample: `AUC ≥ 0.55`, `BSS > 0`, net `expectancy_r > 0` with CI excluding 0, gain survives **per-symbol** |
| **kill** | if no bucketing scheme clears the bar, the cell is not the model and the platform should move to the continuous-forecast family (§5) |

**Honest constraint:** 40 days of history (2026-08-23 → 2026-10-02, verified). Phase 3's
bars cannot be trusted until the history is longer. This is the binding limit and it is
external — only time fixes it.

### Phase 4 — Optional: continuous forecast *(only if Phase 3 fails)*

The attachment's hedge-fund model — `r ~ N(μ_t, σ_t²)`, IR = μ/σ, mean-variance
optimisation. **Not recommended yet.** It requires a return-distribution model this
platform does not have, and it is a much larger build than finishing Phase 3. Recorded
as the fallback, not the plan.

---

## 5. What the end calibration looks like

The target state, stated so it can be checked:

**Outcome**
- A win is a **survivable** win: TP reached without breaching the stop, resolved on
  intrabar data consistent with the MAE columns.
- Expired trades carry mark-to-market, not exclusion (audit C2/R6).

**Probability**
- One published number per signal: `P(survive | cell, conditioning)`, **calibrated**.
- Monotone reliability curve: gaps `< 0.10` across the traded range, measured
  out-of-sample, re-measured on a rolling window.
- `BSS > 0` and `AUC ≥ 0.55` walk-forward, per symbol.
- **No** hard `p_win` floor; the gate is reward/risk-aware.

**Sizing** — three layers, each with one job, and `p_win` consumed by none:

```
size = kelly_f            ← base risk fraction (trade statistics)
     × V_i × DD_i × M_i   ← strategic calibration (vol, drawdown, batch urgency)
     × Q_i(calibrated P)  ← model quality, on the CALIBRATED probability
```

**Decision**
- Selection is on **net expectancy after costs**, not Sharpe on gross PnL.
- `reachability_aware_pick` promoted from shadow once its expectancy input is corrected.

**Measurement**
- AUC (can it rank?) + reliability/BSS (are the numbers true?) + net expectancy (does it
  pay?) — reported per symbol, walk-forward.
- **Accuracy is not a headline metric** (payoff asymmetry, base-rate dependence).

**The single sentence:** *the platform publishes a calibrated, survivable, per-cell
probability of winning; gates and sizes on it; and no part of sizing consumes a
directional confidence score.*

---

## 6. Sequencing and dependencies

```
Phase 1 (label)  ──gates──▶  Phase 3 (probability)  ──feeds──▶  Masaniello Q_i
     │                              │
     └──▶ Phase 2 (sizing backbone, can run in parallel — needs no new label)
                                        │
                                        └──▶ Phase 4 (only if 3 fails)
```

- **Phase 2 is independent of Phase 1** and can start immediately — its three factors do
  not read the outcome label.
- **Phase 3 is gated by Phase 1**: calibrating to the optimistic label would produce a
  model that predicts a win rate the platform does not achieve.
- **Phase 4 is contingent**, not scheduled.

---

## 7. What would change this plan

| observation | consequence |
|---|---|
| ~~corrected survivor base rate ≈ bracket base rate (±0.02)~~ | **MEASURED — NOT the case.** The gap is 0.116–0.190 across 4h/24h/48h, so Phase 1 stands and the intrabar resolver should be adopted |
| a bucketing scheme clears AUC 0.55 | Phase 3 succeeds; Phase 4 not needed |
| no scheme clears it after bucket fix + interaction work | move to Phase 4 (continuous forecast) |
| `V_i`/`DD_i` show no replay effect | drop them from Phase 2; the backbone is thinner than assumed |
| history grows past ~90 days | re-run every Phase 3 bar; the 40-day bound is the current limit |

---

## 8. Status of the immediate fixes

| item | state |
|---|---|
| §3.1 survivable label | **answer measured** — label IS optimistic; resolver built and wired behind `eod_resolver_basis`, **not flipped** (a supported decision now) |
| §3.2 `ev_scale_sizing_mode` | built, default `"on"` (inert) |
| §3.3 break-even floor | built, inert at the live RR band |
| Phase 2 `V_i`/`DD_i` | extracted standalone; **not yet wired into the sweep path** |

**Regression discipline:** every change ships with the default reproducing prior
behaviour exactly — `ev_scale_sizing_mode: "on"`, `eod_resolver_basis: "close_walk"`,
break-even floor inert at RR 1.67–2.0. A test asserts each default; a second asserts a
typo fails closed. **1715 tests pass.**

**The analysis loop is hourly, not 40-day.** The `_h*` / `mae_*` / `mfe_*` columns
backfill hourly (`kairos.backfill_drift_horizons`), so a new signal is measurable within
hours. The 40-day figure bounds *statistical power* (how much history exists), not
iteration speed.

---

## 9. Related

- `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` — objective, metric, thesis
- `docs/scopes/20261002_EDGE_CALIBRATION_BENCHMARK.md` — the attachment benchmarked claim-by-claim
- `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` — R1 (close-only), E7 (floor), C2/R6 (survivorship), G2 (%-space geometry)
- `worklog/kairos/reports/20261002_calibration_decomp.md` — §1b ghost-edge measurement
- `worklog/kairos/reports/20261002_common_factor.md` — the 43/10/47 split
- `regime_platform/core/position_sizer.py` — Kelly + DynamicMasanielloSizer
- `regime_platform/services/statistics/ev_engine.py` — `ev_scale`
- `regime_platform/sweep_orchestrator/eod_resolver.py` — the close-walk resolver (Phase 1)
