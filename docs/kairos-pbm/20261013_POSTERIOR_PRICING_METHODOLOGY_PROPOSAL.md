# Posterior Pricing — a clean methodology for calibration & reachability

**Date:** 2026-10-13
**Type:** Methodology proposal (design; no code change). Third of the 2026-10-13 review set — follows `20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md` (wiring triage) and `20261013_GEOMETRY_MECHANISM_FORMULATION_REVIEW_AND_LLM_SUPPLEMENT.md` (mechanism review + LLM map). This document proposes the replacement formulation.
**Trigger:** Operator request — *"propose a new methodology for calibration and reachability. recommend a clean, clear and robust approach."*
**Status:** Proposal for review. Acceptance bars and migration phases at §11-12.
**Design contract (the whole methodology in six words):** **one probability, one price, one gate.**

---

## 0. Plain terms — what changes

Today the platform has **two probabilities** (a directional `p_win` blend and a table-lookup `p_win_bracket`), **two geometries** (a walked pick and a band-centre fallback), **two EVs** (directional and bracket, one of which can clobber the other), and **~ten gates** deciding which of each you get. The proposal replaces all of it with a single object:

> **A per-cell posterior over the three trade outcomes (win / loss / expire), anchored on a closed-form barrier model and sharpened by the platform's own realized outcomes. The posterior prices the EV; the EV selects the geometry; the EV's lower credible bound is the only trade gate.**

Three layers:

1. **Analytic null** — a driftless barrier model gives win/loss/expire probabilities for *any* geometry in closed form. No grid, no sample floor, no cold-start problem. This is the honest default: what a random walk would do to this bracket.
2. **Empirical posterior** — the platform's realized outcomes (all three categories, expired *included*, era-clean, decay-weighted) update the null per cell via a conjugate Dirichlet update with hierarchical pooling. Thin evidence → stays near the null; heavy evidence → data dominates. **No cliffs by construction.**
3. **Posterior pricing** — one three-outcome EV with per-symbol costs, used for geometry selection (continuous, in-band), qualification (lower-bound > 0), and sizing (Kelly on the lower bound). The directional model is demoted to a *measured drift candidate* that earns admission only on walk-forward skill.

The selection-gap between posterior and null becomes a live KPI: **it measures entry-timing damage per cell** — the thing actually causing the poor win rate.

---

## 1. What it replaces (failure modes of the current formulation)

| Current mechanism | Failure mode (evidence in the companion triage) | Posterior Pricing replacement |
|---|---|---|
| `p_win` logit blend (directional) | Category error: P(price rises) sold as P(TP before SL); AUC 0.499 | Demoted to drift candidate; never priced directly |
| `p_win_bracket` table lookup | Raw `pct_hit_tp` of pooled `signal=all` row; no CI; unscored cliff (`fail_closed`) vs scored-low shadow — evidence asymmetry | Posterior with credible intervals; no cliffs (§9) |
| Calibration = mean-scalar bias, resolved-only | Survivorship (expired excluded); Brier stored never consumed; enforcement shrink 83% prior at n=2; self-starving loop; heavy-path clobber | Calibration = reliability of the posterior, consumed as promotion gates; enforcement retired → conservative Kelly |
| Geometry = 3×3 grid argmax + gate stack | 68/77 shipped geometries are zero-evidence band-centre; `max_expiry` cap contradicts widened bands; consistency-gate arbitration discards both model and measurement | Continuous in-band argmax of the priced EV; gates retire because the price is the test |
| Two EVs (directional/bracket) + clobber paths | Light guard present, heavy unguarded; light/heavy gate divergence | One EV definition, both paths, structurally |
| Close-only ATR, flat `cost_atr` | Depressed denominator; cost error by ~10× across symbols | True-range ATR; per-symbol spread cost |

---

## 2. Layer 1 — the analytic null (barrier model)

**Model.** Log-price as driftless Brownian motion with per-symbol, per-bar volatility σ derived from **true-range ATR** (OHLC already fetched for the envelope walk — use it). For a candidate geometry:

```
u = log(TP / entry)   > 0      # favourable barrier (log distance)
d = log(SL / entry)   < 0      # adverse barrier
T = deadline in bars (48 standard; deadline-class keyed if dynamic deadlines live)
σ = ATR_true_range / price      # per-bar log-vol
```

**The three-way split.** For two-sided BM the joint law of (which barrier first, by when) is the classical double-barrier first-passage problem — closed-form via the method of images (an exponentially-fast converging series), or a 1-D numerical evaluation. Either costs microseconds. It yields exactly the three numbers the platform needs:

```
π_W(a, b, T) = P(τ_TP < T  ∧  τ_TP < τ_SL)     # win before deadline
π_L(a, b, T) = P(τ_SL < T  ∧  τ_SL < τ_TP)     # loss before deadline
π_E(a, b, T) = 1 − π_W − π_L                    # expire
```

Sanity anchors (must hold in tests):
- No deadline: `π_W → |d|/(u+|d|)` — the gambler's-ruin null (random-entry breakeven).
- Symmetric barriers, long deadline: `π_W → 0.5`.
- Normalization: distances scale as `a/(σ√T)`; a 4.5-ATR TP against a 48-bar deadline is `4.5/√48 ≈ 0.65` horizon-normalized — reachable, priced honestly against `π_E`.

**Properties this buys:**
- **Any geometry, instantly.** The 9-point grid becomes a continuous 2-D search over `(tp_atr, sl_atr)` within the symbol band. No `min_signals`, no sample floor, no band-centre starvation. Cold symbols are priced by physics, not by operator prior.
- **The honest default is the random walk.** With no measured drift, the null IS the correct price of a bracket on a randomly-timed entry. Everything the platform has measured says this is close to the truth (§8: the measured rates currently sit *below* the null).
- **Expiry is native.** `π_E` is a first-class output, not a post-hoc bucket statistic — the `max_expiry`-cap-vs-widened-bands contradiction dissolves because expiry is *priced* (§6), never hard-rejected.

**Same-bar convention.** In continuous time ties have probability zero; the discrete resolver's `sl_first` policy (intrabar basis, already live) remains the settlement truth. The analytic layer conditions on strict ordering — consistent by construction.

---

## 3. Layer 2 — the empirical posterior (calibrated reachability)

**Cell key (one conditioning key, everywhere):**

```
cell = (symbol, side, vol_tier, bucket, deadline_class, era)
```

- `side`-conditioned (buys and sells are different populations — migration 159 already measured this; the live lookup's `signal=eq.all` pooling retires).
- `bucket` from true-range-ATR ratio of the *shipped* bracket (coarse geometry class; the analytic layer carries the fine structure within the bucket).
- `era` hard-filtered (contaminated windows excluded — closes the `v_calibration_records_published` era leak by construction).

**Outcome (one outcome definition):** `y ∈ {W, L, E}` with **expired included** and marked-to-market at expiry (the resolver already does this). Three economic meanings of "expired" collapse into one.

**Conjugate update with the analytic base measure:**

```
α_c = k0 · π(ḡ_c)  +  Σ_i  w_i · onehot(y_i)
w_i = 2^(−age_i / τ)          # exponential decay, τ ≈ 14d (config)
k0 = 30                        # prior pseudo-counts (config)
posterior_c = Dirichlet(α_c)   →  (p_W, p_L, p_E) with credible intervals
```

`π(ḡ_c)` is the analytic null evaluated at the cell's geometry centroid; `k0` sets how much evidence it takes to move the cell off physics.

**Hierarchical pooling of the residual.** Define the cell's **selection multiplier** `f_c = p_W / π_W` (posterior odds vs the null). Pool it up the hierarchy with shrinkage:

```
f̂_c  = (n_eff,c · f_raw,c  +  K · f̄_symbol) / (n_eff,c + K)
f̄_symbol pools to f̄_class pools to 1 (the null)
```

A thin cell inherits its symbol/class behaviour; a heavy cell speaks for itself; a cold book prices at the null. **No floors, no fail-open/fail-closed branches — the shrinkage IS the missing-evidence policy.**

**Storage.** One table replaces the lookup-plus-bias-plus-calibration-records sprawl:

```
nt_cell_posterior(
  symbol, side, vol_tier, bucket, deadline_class, era,
  n_w, n_l, n_e, n_eff,
  p_w, p_l, p_e,            -- posterior means
  p_w_null, gap,            -- analytic null + selection gap (§8)
  m_mtm_expire,             -- empirical E[MTM | expire]
  ci_lo_p_w, ci_hi_p_w,     -- credible interval
  updated_at
)
```

**Cadence — event-driven, not calendar:** increment on every resolver verdict (the EOD resolver write path gains a posterior-update step); full decay rebuild daily. Reopt of geometry follows the same trigger — re-solve when ≥k new resolved outcomes accumulate, which replaces calendar reopt (churn without information at current volume).

---

## 4. Layer 3 — posterior pricing (one price, one gate)

**The EV (three outcomes, per-symbol costs, both sweep paths — identical definition):**

```
c       = spread_price(symbol) / ATR_true_range        # per-symbol cost, replaces flat 0.1
EV(g)   = p_W·(a − c) − p_L·(b + c) + p_E·(m_mtm_expire − c)
          with (p_W, p_L, p_E) = posterior at cell(g)
```

**Geometry selection — continuous, priced, gateless:**

```
g* = argmax_{(a,b) ∈ symbol_band}  EV(a, b)
```

- The operator bands survive as the **search domain** (risk appetite), not as a grid of testable points.
- A "rejected candidate" is simply one with lower priced EV — **the price is the test**, so the ten-gate sprawl (min_rr, expiry cap, both-halves, consistency, walk-forward, store-write clamp, …) retires. The consistency check survives as a *diagnostic* (walk-vs-posterior agreement monitoring), not a gate.
- `min_rr` survives as a shape constraint on the search domain if the operator wants it.

**Qualification — the one gate:**

```
qualified ⟺ EV_lower_bound(g*) > 0        # e.g. 25th percentile of the posterior EV
           ∧ regime non-neutral           # operational
           ∧ cooldown / dedup              # operational
```

- The `p_win` floors retire — they are subsumed: a low-p_W bracket with high RR *can* be +EV, and the EV bound is the correct single test (with costs, a `p_W=0.056` bracket fails it — the XAUAUD class is blocked by arithmetic, not by a bolt-on gate).
- The unscored/scored asymmetry disappears: a cold cell prices at the analytic null (usually −EV after costs → honest no-trade), not at a cliff.
- Light and heavy paths share one definition — the heavy-path clobber class of bug becomes structurally impossible (there is no second EV to clobber with).

**Sizing — conservative Kelly:**

```
kelly* = Kelly(EV_lower_bound, RR(g*))
```

Kelly on the *lower bound* of the EV: thin evidence → wide interval → small size → tends to zero as evidence → zero. This replaces calibration enforcement entirely. The shrink-toward-0.5 machinery, its 83%-prior n=2 behaviour, its self-starving loop, and its clobber-prone re-price step all retire. Sizing honesty becomes a property of the interval, not a penalty layered after the fact.

---

## 5. Calibration, redefined

Calibration stops being a *correction pass* and becomes a *property to verify*:

1. **The posterior is the calibrated probability.** It is updated on the full outcome population (expired included), era-clean, decay-weighted. There is no separate bias number to enforce.
2. **Calibration QA is consumed, not stored:** reliability curves (posterior mean vs realized frequency, stratified by cell depth `n_eff`), Brier decomposition (reliability/resolution/uncertainty per depth stratum), and the selection-gap trend (§8) become standing dashboards with alert thresholds — the existing `calibration_brier` column finally gets a reader.
3. **Promotion gate for any live change** (drift layer, new conditioning dimension, LLM suppressor): measured on era-clean walk-forward replay against the null posterior — the same bar the repo already applies, now applied to *this* methodology's own extensions.
4. **The directional model is demoted and gated.** `p_dir` (the current blend) may enter only as a **drift term** μ on the barrier model, and only if it demonstrates **barrier-orientation skill**: AUC of `p_dir` against which-barrier-hit-first, walk-forward, above the null — measured exactly as the repo measures everything else. With today's evidence (AUC 0.499), the drift layer stays at μ=0: the null is the honest price. The category error is closed because direction never again masquerades as P(win); it can only tilt a physics-based price, and only measurably.

---

## 6. Reachability, redefined

"Reachability" stops being a table read and becomes **the empirical layer of the posterior** (§3):

- The measurement grain is preserved — `(symbol, vol_tier, bucket)` was the right key — and improved (side, deadline class, era, decay).
- The reachability table's population-drift caveat (history dominated by brick-timed entries) is handled honestly: decay + era keying mean old-regime evidence fades; the analytic null is regime-agnostic physics, not a stale average.
- The `p_expire` used in pricing is the posterior's `p_E` — one number, one source, consistent with the geometry selection that chose the bracket.
- Reopt is evidence-triggered (≥k new resolved outcomes), not calendar — the tier store, its 6-hour TTL read, its stale-fallback failure modes, and its band-fingerprint cache all simplify away.

---

## 7. What stays, what retires

**Stays (the good bones):**
- `nt_signal_sim` (measurement ledger), resolver (intrabar, `sl_first`, MTM-at-expiry — already the right settlement basis), the envelope walk (becomes the **acceptance-test simulator** for the analytic layer — replay agreement between `walk_envelopes` and the barrier model is a standing test, not a selection path), symbol bands (search domain), era/contamination discipline, the shadow-first promotion culture.

**Retires (net code out, not in):**

| Retiring | LOC | Replaced by |
|---|---|---|
| `select_tp_sl` grid + `_band_center_pick` + consistency/walk-forward *gates* | ~400 of `tiered_atr.py` | analytic module (~150) + continuous argmax |
| `p_win_bracket` lookup + fail-closed/unscored branches | ~323 | posterior service (~200) |
| `calibration_enforcement.py` | 656 | conservative Kelly (in sizing) |
| `calibration_bias_bucket.py` resolved-only bias | 460 | posterior update + reliability QA |
| p_win floors, soft bands, `min_p_win_bracket` gates | scattered | the EV lower bound |
| Store-read/TTL/stale-fallback machinery | ~200 | event-driven posterior table |

Net: **~1,700 LOC out, ~350 in** — the methodology is also the "Claude diet." One new module (`barrier.py`), one service (`posterior.py`), one table, one gate.

---

## 8. The selection-gap KPI — why this methodology finds the real problem

```
gap_c = p_W,c − π_W,c        # posterior minus analytic null, per cell
```

- `gap ≈ 0`: entries are fairly timed — outcomes match what a random walk would do to the bracket.
- `gap < 0`: entries are **adverse-timed** — the bracket loses *more* than physics predicts. This is precisely the platform's current state (measured TP rates sit below the random-walk null at the shipped bands: ~13-15% vs ~37.5% first-touch).
- `gap > 0`: entries carry genuine timing skill (the thing the strategy layer is supposed to manufacture).

The gap is the **KPI for the entry-timing fix** (companion review R1): as entry conditioning improves (regime age, deviation-Z, archetype routing), the gap closes cell by cell, and book EV rises *with the same geometry*. Today's system cannot see this because it averages the effect into a scalar bias and then shinks `p_win` toward 0.5 — punishing the probability instead of diagnosing the entry. The posterior prices each cell; the gap names the disease.

**Prediction semantics (the "next signal" question, answered honestly):** the posterior *is* the forecast for the next signal's outcome distribution in its cell — a real predictive distribution with a credible interval, evaluable by log-loss/Brier on the next resolved outcome (proper scoring, walk-forward). That is what "predicts the next signal" can truthfully mean; predicting signal *occurrence* is a separate problem nobody needs solved for EV.

---

## 9. The robustness contract

Seven properties, each of which the current design violates and this one holds by construction:

1. **One probability** — no directional/bracket hybrid, no clobber ordering to get wrong.
2. **One price** — 3-outcome EV with costs, identical on light and heavy paths.
3. **One gate** — EV lower bound > 0; everything else is operational or domain constraint.
4. **No cliffs** — no `min_n` floor, no unscored/scored asymmetry, no band-centre cliff; evidence-proportional behaviour everywhere (shrinkage, not switches).
5. **One basis** — true-range ATR denominator, intrabar `sl_first` settlement, MTM-at-expiry, one resolver (already done at HEAD; the close-only ATR is the remaining fix).
6. **Expired is data, not noise** — the third category enters calibration, pricing, and PnL identically.
7. **Every live change earns its place** — drift, conditioning dimensions, LLM suppressors (S1-S3 of the companion LLM map): measured walk-forward against the null posterior before touching the price. The `p_clef_dir`-was-chance rule generalized.

---

## 10. What this does NOT fix (stated plainly)

- **It does not manufacture edge.** With μ=0 and positive costs, most cells price at or below the EV bound → the honest book is small or empty. That is the correct output for the current entry layer (§8). The win-rate lever remains entry timing (R1) and the strategy layer.
- **It does not fix the wiring P0s by itself.** The heavy-path clobber, the era-filter leak, and the EV-floor parity gap must be fixed *first* or during Phase A — a better methodology priced through a corrupted chain is still corrupted.
- **The analytic null is a model.** Barrier-series assumptions (no jumps, no overnight gaps, constant σ within the deadline) are wrong in the tails. Mitigation: the empirical layer absorbs systematic deviations (that is its job); gaps-through-SL risk stays priced by the resolver's intrabar basis and the cost term; vol-of-vol can enter later as a σ range priced at the conservative end.
- **The drift layer will likely stay null.** The platform has measured every drift candidate at or below chance. The methodology is built so that this is a stable, honest resting state — not a failure mode.

---

## 11. Migration — five phases, each with an acceptance bar

| Phase | Change | Acceptance bar (era-clean replay) |
|---|---|---|
| **A** | Build `barrier.py` + posterior service; **shadow-stamp** `(p_W, p_L, p_E)` next to `p_win_bracket` on every signal; fix the P0 wiring defects | Null sanity tests pass (gambler's-ruin, symmetry, normalization); posterior-vs-table agreement on scored cells; reliability of posterior ≥ table lookup |
| **B** | **Swap the EV input** to the posterior (one flag; old lookup kept as bake-period fallback) | Replay EV monotonicity (no sign flips on the resolved book); publish volume change explained; heavy/light EV definitions identical in code |
| **C** | **Continuous geometry** on the analytic layer; retire grid + band-centre + the gate stack (consistency/walk-forward become diagnostics) | Replay book EV ≥ grid-book EV; every symbol ships an in-band solved geometry (band-centre rate → 0); walk-vs-posterior agreement monitored |
| **D** | **Retire enforcement** → conservative Kelly on the EV lower bound | Sizing sanity on replay (thin cells size small, zero-evidence cells size ~0); no publish-volume explosion |
| **E** | **Drift-layer measurement** (`p_dir` → barrier-orientation AUC, walk-forward); LLM suppressor S1 measured against the same bar | Promote only above null; expected honest outcome: stays off |

Each phase is one lever, replay-verified, reversible — the repo's own G1-G4 discipline applied to this redesign.

---

## 12. Build order relative to the existing triage

1. **P0 wiring fixes** (companion triage §5) — prerequisites, ~10-line diffs.
2. **Phase A** of this methodology (shadow-stamp + true-range ATR + per-symbol costs) — measurement quality first.
3. **Phases B-C** — the pricing and geometry swap (this is also P1-4/P1-5 of the triage, done structurally instead of patchwise).
4. **R1 / strategy layer** (entry timing + router) in parallel — the gap KPI from §8 is its acceptance metric.
5. **Phases D-E** last — sizing and drift, only after the price they size is honest.

---

## 13. Files / lineage

- Grounding evidence: this proposal rests on the verified findings in `20261013_KAIROS_TRIAGE_CALIBRATION_GEOMETRY_REACHABILITY.md` (wiring, gate census, clobber, survivorship, era leak) and `20261013_GEOMETRY_MECHANISM_FORMULATION_REVIEW_AND_LLM_SUPPLEMENT.md` (six formulation defects D1-D6, breakeven arithmetic, random-null analysis, R1-R6, LLM map S1-S5).
- Code anchors at HEAD `aa9fff1` (pre-reset verification): `tiered_atr.py:464-558` (grid objective), `:614-660` (consistency gate), `p_win_bracket.py:146-206` (all-row lookup), `calibration_bias_bucket.py:138` (resolved-only), `eod_resolver.py:717-727` (expired exclusion), `heavy_sweep.py:1506-1536` (heavy clobber), `feature_flags.yaml:525-559` (bands, `max_expiry: 0.20`, `cost_atr: 0.1`, `min_signals: 60`).
- Measured evidence: bucket table (`20261012_BUCKET_INVERSION_HARMONIZATION.md`), era-clean WR span (`20261011_CALIBRATION_REACHABILITY_REFRAME.md`), AUC evidence (`20261004_masaniello_brier_decomp.md`, `20261002_SIGNAL_QUALITY_PLAN.md`), ID45 inversion (`20261010_TIER1_CONTEXT_GATE_INVERSION.md`), band-centre rate (`20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`).
- Mathematical basis: two-sided first-passage for Brownian motion (method-of-images series; gambler's-ruin limit); Dirichlet-multinomial conjugacy with exponential-decay weights; partial pooling (James-Stein-style shrinkage toward the analytic anchor).
