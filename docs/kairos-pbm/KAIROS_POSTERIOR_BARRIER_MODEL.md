# Kairos Posterior Barrier Model

## **The recommendation, in six words: one probability, one price, one gate.**

### The methodology — three layers

**Layer 1 — Analytic null (barrier model).** Log-price as driftless BM with true-range vol. The double-barrier first-passage problem gives `(π_W, π_L, π_E)` — win/loss/expire probabilities — for **any** geometry in closed form (method-of-images series, µs cost). Sanity anchors: gambler's ruin `b/(a+b)` in the no-deadline limit. This kills the grid, the 60-path floor, and the band-center starvation *by construction*: cold symbols get priced by physics, not operator priors.

**Layer 2 — Empirical posterior (calibrated reachability).** One cell key `(symbol, side, vol_tier, bucket, deadline_class, era)`; outcomes are the full three-category population with **expired included** (survivorship closed). Conjugate update with the analytic null as the Dirichlet base measure + exponential decay + hierarchical pooling of the residual multiplier. **No cliffs anywhere**: thin evidence → null; heavy evidence → data. The unscored/scored asymmetry, `min_n` floors, and fail-open/fail-closed branches all dissolve into shrinkage.

**Layer 3 — Posterior pricing.** One 3-outcome EV with per-symbol spread costs, identical on light and heavy paths (the clobber class becomes structurally impossible). Geometry = continuous in-band argmax of the priced EV — **the price is the test**, so the ten-gate sprawl retires. Qualification = `EV_lower_bound > 0`, the single gate. Sizing = Kelly on the lower bound — enforcement retires, and the self-starving loop becomes arithmetically impossible.

### Why it's the right answer for *your* specific failures

- **The selection-gap KPI**: `gap_c = p_W,posterior − π_W,null` per cell. Currently deeply negative (= entries adverse-timed below the random walk). This finally *measures* the entry-timing damage as a live dashboard number — the acceptance metric for the entry fix, which no amount of p_win shrinking could ever see.
- **Calibration becomes verifiable, not correctable**: reliability curves + Brier by cell-depth, actually *consumed* as promotion gates (your stored-but-never-read `calibration_brier` gets a reader).
- **The directional model is demoted honestly**: `p_dir` can only enter as a drift term on the barrier model, gated on measured barrier-orientation skill. With AUC 0.499 it stays at μ=0 — a stable, honest resting state, not a failure.
- **It's also the diet**: ~1,700 LOC out (grid, band-center, gates, p_win_bracket lookup, enforcement, bias buckets), ~350 in (one barrier module, one posterior service, one table).

 


| Component                                             | Provenance                                                                                                                                                                                                                                         |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Double-barrier first-passage, method-of-images series | Classical stochastic processes (reflection principle → image solutions); the same math used to price double-barrier options since the 1990s. The gambler's-ruin limit `π_W → d/(u+d)` is centuries old and serves as a sanity anchor in the tests. |
| Dirichlet-multinomial conjugacy with decay weights    | Standard Bayesian categorical model; exponential decay handles non-stationarity (regime drift).                                                                                                                                                    |
| Hierarchical shrinkage / partial pooling              | Empirical Bayes, James-Stein lineage — thin cells borrow strength from symbol → class → the null.                                                                                                                                                  |
| Kelly criterion (1956) on a lower bound               | Standard, made conservative by feeding it the interval's 5% quantile.                                                                                                                                                                              |


The one genuinely tailored move — and I'd claim it as the design's only "new" element — is using the **closed-form barrier null as the Dirichlet prior's base measure**. Instead of a flat or hand-tuned prior, the prior *encodes the physics of the specific bracket*: `α = k0·π(geometry) + Σ evidence`. That's what makes cold cells price at honest physics rather than at a cliff, and it's what makes the whole thing need no tuning switches.

 

## Review Worklogs

The triage, the geometry mechanism review, the PBM proposal, the integration map, the complete build log, and the delivery manifest.


|                              |                                                                 |
| --------------------------------- | -------------------------------------------------------------------- |
| **The triage**                    | 20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md          |
| **The geometry mechanism review** | 20261013_GEOMETRY_MECHANISM_FORMULATION_REVIEW_AND_LLM_SUPPLEMENT.md |
| **The PBM proposal**              | 20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md                   |
| **The integration map**           | 20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md                 |
| **The complete build log**        | 20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md                            |
| **The delivery manifest**         | 20261014_PBM_DELIVERY_MANIFEST.md                                    |


## **What shipped, by phase**

All five phases landed — measurement stamps in shadow, B/C/D ship flag-gated at no-op defaults, E is the harness that decides.


|                                |                                                                                                                                                                                                                                                                                                                       |                                                           |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| **Measurement****Phase A**         | `barrier.py` (closed-form double-barrier first-passage null, μ=0, σ=ATR) + `posterior.py` (decay-weighted Dirichlet anchored on the null) + migrations 210/211 + resolver hook for **all outcomes incl. expired** (survivorship closed) + per-signal shadow stamps + **P0 heavy-clobber guard** + **P0b p_expire parity**. | Live - shadowf**lag** pbm_mode: "shadow"                      |
| **EV input swap**Phase B           | Posterior triple through the one `apply_bracket_to_ev` call site, both paths.                                                                                                                                                                                                                                              | OFF**flag** pbm_ev_mode: "off"                                |
| **Continuous geometry****Phase C** | Posterior-EV argmax over the band domain + evidence-triggered reopt.                                                                                                                                                                                                                                                       | OFF**flag** pbm_geometry_mode: "grid"                         |
| **Sizing****Phase D**              | Conservative Kelly on the EV lower bound.                                                                                                                                                                                                                                                                                  | OFF**flag** pbm_sizing_mode: "legacy"                         |
| **Drift harness****Phase E**       | Walk-forward barrier-orientation AUC with mechanical PROMOTE / STAY-OFF verdicts.                                                                                                                                                                                                                                          | **RUN-ON-DEMAND****harness** scripts/measure_pbm_drift_auc.py |


## **Flip checklist & rollback**

Operator bars for B → C → D, the drift gate, and rollback.


| **Flip**     | **Edit**                          | **Bar to clear first**                                |
| ------------ | --------------------------------- | ----------------------------------------------------- |
| **B**        | `pbm_ev_mode: "live"`             | Phase A ledger log-score ≥ table lookup, era-clean.   |
| **C**        | `pbm_geometry_mode: "posterior"`  | Replay book EV ≥ grid and band-centre rate 68/77 → 0. |
| **D**        | `pbm_sizing_mode: "conservative"` | Only after C is accepted.                             |
| **E**        | **Promote drift**                 | Only if walk-forward AUC bootstrap CI > 0.5.          |
| **Rollback** | `pbm_mode: "off"`                 | Stamps stop — HEAD behaviour restored exactly.        |


### Apply Supabase migrations `210`/`211` before `pbm_mode: shadow` can write — until the tables exist, the stamps simply don't land.

 