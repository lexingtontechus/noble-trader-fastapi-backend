# Calibration + leverage scaling — where we are, and the next step

**Date:** 2026-10-01 · **Status:** SCOPE — proposed, nothing built
**Supersedes the framing in:** `worklog/20261001_PHASE2_FIRST_PASSAGE_RESULT.md` §6
**Parents:** `docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md`,
`worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md`

---

## 0. The goal, restated

The platform's goal is **calibration + reachability**:

- **Calibration** — does the number we publish (`p_win`) match reality?
- **Reachability** — does the bracket we publish actually get reached?

The first-passage work was an attempt to serve reachability *through the EV
blend*. That framing was wrong. Reachability is already served by geometry
(`dynamic_atr_pick`, `nt_bracket_reachability`, `p_win_bracket`). What the
withdrawal of `p_markov_hold_n` actually changed is **calibration** — and, through
`ev_scale`, **leverage**.

**This change is a calibration change with a leverage-scaling consequence.** That
is the correct frame, and it is not what the Phase-2 report measured.

---

## 1. Where this leaves us

### 1.1 What is settled

| | |
|---|---|
| The legacy HMM is not informative | accuracy 0.5251 vs 0.5655 naive / 0.5898 base rate; skill −0.0404; positive on 5/14 |
| The trend_hmm shadow comparison answered negatively | legacy won 13/14; 65.9% disagreement vs 66.7% random |
| First-passage did **not** beat the base rate | skill −0.0691; 1/5 bar items |
| The D5 transition overlay subtracts | ΔD +0.024 vs bare +0.050 |
| `p_regime`, regime labelling, `trend_probs_gate_mode` have **zero** coupling to the legacy HMM | `RegimeHMM` is a separate model |

### 1.2 What is NOT settled — and is the actual risk

**The withdrawal is not leverage-neutral.** `ev_scale = min(1, ev_per_dollar / 0.5)`
and `effective_kelly = kelly_f × ev_scale`. Dropping a low `p_markov` from the
logit pool *raises* `p_win` → raises `ev_per_dollar` → raises `ev_scale` → **raises
position size**. Measured on the live weights:

| p_regime | p_markov | p_timesfm | p_win before | p_win after | **ev_scale before → after** |
|---:|---:|---:|---:|---:|---|
| 0.60 | 0.10 | 0.60 | 0.4163 | 0.6000 | **0.220 → 1.000** |
| 0.50 | 0.10 | 0.50 | 0.3480 | 0.5000 | **0.000 → 0.667** |
| 0.40 | 0.05 | 0.40 | 0.2440 | 0.4000 | **0.000 → 0.133** |

**A signal can go from zero size to full size.** This is the single most important
open item, and no measurement covers it yet.

### 1.3 What was measured wrongly

The Phase-2 evaluation scored every arm on **accuracy / ΔD** — a *discrimination*
metric. But the source being replaced fed **calibration and sizing**, not
selection. A probability source can be useless at ranking and still be the thing
that makes `p_win` honest (or dishonest).

**The correct metric is Brier score and the reliability curve of the blended
`p_win`** — and that is exactly what the platform already computes elsewhere:
`eod_resolver` writes `calibration_brier` per record; `hmm_shadow_accuracy`
Tier 2 computes Brier for `raw` vs `gated` `p_regime`. Phase 2 computed neither.
So **the question the change actually raises was never tested.**

---

## 2. The two pipelines, and what each depends on

### 2.1 Calibration loop (live)

```
sweep tick ──► p_win (blend) ──► nt_signal_sim
                                      │
                       eod_resolver ──┼──► outcome (tp_hit/sl_hit/expired)
                                      │    predicted_p_win_raw
                                      │    calibration_brier
                                      ▼
                            nt_calibration_records
                                      │
                fetch_calibration_bias / _raw  (EPISODE-grouped)
                                      ▼
                  calibration_enforcement.apply_calibration_adjustment
                  cell = (symbol, side, vol_tier, distance_atr_bucket)
                      ├─ OVERCONFIDENT → shrink p_win  + kelly × reduction
                      ├─ UNDERCONFIDENT → shrink p_win only (no Kelly cut)
                      └─ CALIBRATED → no-op
                                      ▼
                       nt_symbol_calibration_state / nt_eod_actions
```

**Depends on the HMM:** only through `p_win`'s blend membership (now removed) and
`markov_p_up`/`markov_p_dn` as diagnostic context in `degeneracy_breaker`.

### 2.2 Reachability (live, independent)

```
nt_signal_sim (distance/atr) ──► nt_bracket_reachability   (migration 120)
                                          │
                                          ▼
                       p_win_bracket = p_resolve × Beta-shrunk P(tp|resolve)
                                          │
                              bracket_gate_shadow / R2 EV+Kelly
```

**Depends on the HMM:** **nothing.** Verified — no `markov`/`hmm` reference in
`bracket_reachability.py` or `heavy_sweep_reachability.py`.

### 2.3 The shared input: TA

Both pipelines read `nt_regime_log.ta_*` and `nt_signal_sim.spread_bps`. **Both
come from `services/hmm/`**, and `ta_fetcher.py` + `trend_filter.py` import
`_compute_ta_indicators_local` / `_compute_rsi` **from `model.py`**.

**This is the dependency the first assessment missed.** `model.py` is not "the
HMM" — roughly half of it is pure TA math that the calibration and reachability
pipelines stand on. See the corrected map in `docs/Setup/CHANGELOG.md`.

---

## 3. Proposed next step

Three tracks. Track A is a measurement and should go first — it is cheap and it
decides whether Tracks B/C are urgent.

### Track A — measure the leverage + calibration effect of the withdrawal (DO FIRST)

**Question:** did removing `p_markov_hold_n` make `p_win` better calibrated, worse,
or just bigger?

| | |
|---|---|
| **Metric 1** | **Brier of the blended `p_win`** against realised outcome, before vs after the withdrawal, same symbol-day clusters |
| **Metric 2** | **Reliability curve** — bucket `p_win`, plot realised hit rate; a source that only shifted the level shows as a slope change, not a curve change |
| **Metric 3** | **`ev_scale` distribution shift** — how many signals moved from 0 → >0 and from <1 → 1 |
| **Metric 4** | **Sizing counterfactual** — replay the window with the source on vs off, report the Kelly delta per symbol |
| **Data** | `nt_signal_sim` (`p_win`, `predicted_p_win_raw`, `outcome`, `ev`, `kelly_*`, `atr`, `tp/sl_distance_atr`) |
| **Build** | extend `fp_eval.py` with a Brier/reliability block, or a small new `scripts/calibration_leverage_eval.py` |

**Why first:** if the withdrawal *improved* Brier, Track B becomes housekeeping. If
it *degraded* Brier while *raising* size, that is an active risk and Track B is
urgent. Either way Track A is a day's work and it is the missing measurement.

### Track B — retire the HMM correctly (the split, not the delete)

Only after Track A. Sequence:

1. Extract `_compute_rsi` / `_compute_macd` / `_compute_ao` / `_compute_bb_power` /
   `_compute_adx_close_only` / `_compute_ta_indicators_local` / `_compute_log_returns`
   into `regime_platform/services/indicators.py` (public names, no leading `_`).
2. Repoint `ta_fetcher.py` and `trend_filter.py`.
3. **Verify `nt_regime_log.ta_*` and `nt_signal_sim.spread_bps` still write** —
   this is the acceptance test, not the unit tests.
4. Delete `HmmModel`, `_FittedModel`, `_aggregate_transition`, HMM constants.
5. Stop the per-tick `fit()` in `orchestrator.py` / `heavy_sweep.py`; keep the
   fetchers and `latest_ta()`.
6. Retire `markov_source_mode` / `p_markov_hold_n_mode`.

**Do not** delete `feature_buffer.py` (it defines the TA layout `ta_extract` reads)
or `quote_fetcher.py` (it feeds both the buffer and the Kairos `spread_bps` stamp).

### Track C — the replacement level, framed as calibration

The candidate is the **shrunk per-symbol base rate** (0.5898 accuracy, 100%
coverage, zero fitting) — but it should be evaluated as a **calibrated probability
source**, not as a ranker:

- Fit walk-forward, score on **Brier + reliability**, not accuracy.
- The `(symbol, side, vol_tier, distance_atr_bucket)` cell is the **calibration
  key** — it is what makes the rate legible and drift-detectable, and it is the
  same key `calibration_enforcement` and `_bucket_for` already use.
- Only if it beats the current blend on Brier does it become a blend input.
  Otherwise it belongs in `calibration_enforcement` as a **correction target**,
  not in the EV blend at all — which may be the cleaner home for it.

---

## 4. What NOT to do

- **Do not re-open the first-passage candidate.** It was tested against the bar and
  lost on every axis that matters. Its value was the *diagnosis* (the HMM is below
  baseline), not the *cure*.
- **Do not retire `model.py` as a file.** It is load-bearing for TA.
- **Do not treat the withdrawal as settled.** It changed sizing; that needs the
  Track-A measurement before it is called done.
- **Do not re-run accuracy-based evaluations on a calibration question.** That is
  the error that produced the wrong Phase-2 framing.

---

## 5. Open items for the user

| # | question | default |
|---|---|---|
| Q1 | Track A first? | **yes** — it is the missing measurement and it gates B/C |
| Q2 | If Track A shows the withdrawal raised size without improving Brier, revert `p_markov_hold_n_mode` to `"live"` pending a proper replacement? | lean yes — a sizing increase with no calibration gain is not a win |
| Q3 | Should the base-rate candidate live in the EV blend, or as a `calibration_enforcement` correction target? | **correction target** — it is a calibration object, and the blend is a discrimination object |
| Q4 | Is `p_win_bracket` (reachability-conditioned, already built, shadow) the better home for the level than the base rate? | worth testing in Track C — it is the one candidate that is *already* calibration-shaped |

**Q4 is the one I would look at hardest.** `p_win_bracket = p_resolve ×
Beta-shrunk P(tp|resolve)` is already built, already conditioned on the
reachability key, and already shadow-logged. If the goal is calibration +
reachability, that is the component shaped like the goal — and it was never in
the comparison.
