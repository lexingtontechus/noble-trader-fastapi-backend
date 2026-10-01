# Phase 2 result — first-passage does NOT clear the bar

**Date:** 2026-10-01 · **Status:** KILL CRITERIA MET — decision requested
**Phase:** 2 of `worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md`
**Report:** `worklog/kairos/reports/20261001_fp_eval.md` (regenerate any time)
**Build:** `regime_platform/sweep_orchestrator/fp_eval.py`, `scripts/kairos_fp_eval.py`

---

## 0. Headline

**1 of 5 bar items passes. First-passage is NOT promotable, and the D5
regime-transition overlay makes the arm *worse*, not better.**

| bar | requirement | result | detail |
|---|---|---|---|
| B1 | beat the base-rate control | **FAIL** | skill −0.0691 vs base rate 0.7041 |
| B2 | coverage ≥ 0.30 | **PASS** | 0.9968 |
| B3 | positive skill ≥ 2/3 of symbols | **FAIL** | 3/14 |
| B4 | holds in both era halves | **FAIL** | clears the bracket-blind floor in 1/2 halves |
| B5 | overlay adds skill | **FAIL** | overlay ΔD +0.0236 vs bare +0.0502 (**Δ −0.0265**) |

Population: 6,904 decided light-sweep episodes, 45d lookback, one closed
48-bar path per signal, episode-deduped (symbol × direction × hour).

---

## 1. The measurement (all eras)

| arm | calls | coverage | accuracy | base rate | **skill** | positive symbols |
|---|---:|---:|---:|---:|---:|---:|
| **first_passage** | 6,882 | 0.997 | 0.6350 | 0.7041 | **−0.0691** | 3/14 |
| **base_rate** | 6,904 | 1.000 | 0.7041 | 0.7041 | +0.0000 | 0/14 |
| incumbent (legacy HMM, withdrawn) | 2,204 | 0.319 | 0.5349 | — | — | — |

**Skill = accuracy − the symbol's own majority-direction base rate.** The
first-passage arm scores **−0.0691**: it is worse than silence, by about the
same margin the HMM was (−0.0622 at the RM-1 evaluation). The bar was to *beat*
the base rate; it does not.

Coverage passes trivially (0.997) — the arm is not silent, it is wrong.

## 2. ΔD — and the honest floor

| arm | gated n | rest n | ΔD | 95% CI (symbol-day clustered) |
|---|---:|---:|---:|---|
| first_passage | 2,763 | 4,141 | +0.050 | [+0.001, +0.099] |
| first_passage + overlay | 2,763 | 4,141 | **+0.024** | — |
| base_rate | 2,893 | 4,011 | **+0.076** | [+0.032, +0.120] |
| *trailing_base_rate* (bracket-blind floor) | 2,763 | 4,141 | +0.059 | [+0.016, +0.106] |

Three things to read here:

1. **The trivial base rate beats first-passage on ΔD** (+0.076 vs +0.050).
2. **First-passage does not clear the honest floor.** The bracket-blind trailing
   rate — which knows *nothing* about geometry — scores **+0.059**, above the
   first-passage arm's +0.050. **The bracket conditioning bought nothing.**
3. The first-passage CI [+0.001, +0.099] barely excludes zero, while the base
   rate's [+0.032, +0.120] sits comfortably above it.

## 3. The overlay (D5) is actively harmful

Bare +0.0502 → overlaid **+0.0236** (Δ **−0.0265**). The regime-transition
weighting *degrades* the arm it was meant to reinforce.

That is a meaningful result, not a null: it says the freshness/type prior —
which is genuinely supported on **regime-label** accuracy (+0.117 fresh−deep) —
does **not** transfer to a first-passage *level*. Two plausible reasons, both
testable:

- **The level already prices the transition.** First-passage conditions on the
  path distribution; if a fresh flip already moved the paths, the carry is
  double-counting.
- **The carry's damping is the wrong shape here.** Shrinking toward 0.5 is a
  *confidence* transform. Applied to a level that is already miscalibrated, it
  mostly moves rows across the P60 gate threshold without improving the ordering.

## 4. Era split (B4)

| half | first_passage ΔD | floor ΔD | clears? |
|---|---:|---:|---|
| first | −0.050 [−0.120, +0.015] | +0.083 | ✗ |
| second | +0.111 [+0.051, +0.176] | +0.065 | ✓ |

The arm is **wrong-signed in the first half**. It holds in the second half only.
B4 required both.

---

## 5. What this does and does not kill

**Killed:**

- **Option E as specified** — first-passage-as-source, conditioned on
  (symbol, direction, geometry bucket), with the D5 transition overlay.
- **The D5 overlay in its current form** — measured, and it subtracts.

**Not killed:**

- **The §2.4 thesis.** `tiered_atr.walk()` still answers the Markov question
  exactly, and the legacy HMM still scores below the naive baseline. The
  *diagnosis* stands; the *replacement* does not work as built.
- **The base rate.** At +0.076 ΔD with 100% coverage and zero fitting, the
  shrunk per-symbol base rate is now the **best-scoring candidate on the table**.
  It is not an exciting model, but it is the honest leader.
- **The HMM's retirement.** D1/D2 remain correct — `p_markov_hold_n_mode: "off"`
  and `markov_source_mode: legacy_hmm` are unchanged by this result. Nothing
  here argues for turning the HMM back on.

---

## 6. Decision requested (kill criteria, scope §5)

The scope's kill criteria say: *take option F if first-passage fails to beat
the base-rate control by 2026-11-01.* It has already failed, on the first
proper measurement, three weeks early. Three honest options:

### Option 1 — Take F now (recommended if effort matters)

Ship the **calibrated base rate** as the replacement level and retire the
legacy HMM. Concretely: delete `services/hmm/model.py`, `feature_buffer.py`,
the `hmm_quote_fetcher` call, and the `MARKOV_SOURCE_MODE` /
`p_markov_hold_n_mode` flags. Keep `hmm_ta_fetcher` / `latest_ta()` (separate
purpose, feeds `nt_regime_log.ta_*`).

- **For:** the measurement is unambiguous; the HMM is below baseline; the base
  rate is the best candidate; carrying a permanently-off source plus its
  dormant shadow paths is dead weight and per-tick compute.
- **Against:** forecloses a re-run if a materially different first-passage
  formulation is proposed.

### Option 2 — One more formulation, time-boxed

The most defensible single retry, given §3: drop the overlay, and make the
**level** the live conditioning (per-symbol, per-direction, per-regime *cell*)
rather than a geometry bucket — i.e. the `cell_p` shape that already scores
+0.145 IC in the ranking programme. If that also fails B1, take Option 1.

- **For:** cheap (the machinery exists), and it tests the one hypothesis §3
  raises (the level may already price the transition).
- **Against:** risks another cycle on a question that may simply be answered.

### Option 3 — Stop and bank the finding

Leave `p_markov_hold_n_mode: "off"`, keep the legacy HMM fitting as a
capture-only source, and take no further action. Revisit if a genuinely new
formulation appears.

- **For:** zero cost, no risk.
- **Against:** leaves dead code and a dormant shadow path in the tree.

**My recommendation: Option 1**, with Option 2 as the fallback if you want one
more swing. The bar was pre-registered precisely so that a result like this
would not be argued away — and the base rate beating every constructed
candidate is itself the finding.

---

## 7. Method notes (what made this measurement trustworthy)

Three bugs were caught during the build, all of which would have produced a
confident wrong answer:

1. **Look-ahead.** The first cut scored each signal on its own path. With one
   path per signal that is a binary outcome, not a probability — it would have
   scored ~1.0 by construction. Rebuilt as a **walk-forward trailing rate**
   (score first, append the label after), the same no-lookahead rule
   `rank_score` uses.
2. **A silent parser failure.** `split_cell` was hand-rolled against a `vol|dir`
   format the raw regime label never uses (`med_high_vol_strong_bull`), so every
   `ewin` was `None` and the overlay was a **no-op that still reported as
   measured** (Δ exactly +0.0000). Now delegates to `kairos.regime_cell`, the
   frozen shared definition, with a regression test.
3. **A metric blind to the change.** B5 was first scored on sign-accuracy —
   but shrinking a probability toward 0.5 *preserves its sign*, so accuracy
   could never detect the overlay. Moved to ΔD, which sees ranking. That is
   what turned a fake +0.0000 into the real −0.0265.

Plus the honest floor: gating on a **bracket-blind** trailing base rate
(`trailing_base_rate`) so a geometry-aware arm cannot claim credit for what is
just momentum.

Tests: 48 in `tests/test_fp_eval.py`; full suite **1491 passed, 0 failed**.

---

## 8. References

| artefact | what |
|---|---|
| `worklog/kairos/reports/20261001_fp_eval.md` | the generated report (verdict + all blocks) |
| `regime_platform/sweep_orchestrator/fp_eval.py` | arms, overlay, clustered ΔD, mechanical verdict |
| `scripts/kairos_fp_eval.py` | CLI: fetch → paths → evaluate → report |
| `worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md` | the plan + pre-registered bar B1–B7 |
| `docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md` | options A–F, kill criteria |
| `worklog/kairos/reports/20261001_rm1_markov_source_eval.md` | the RM-1 evaluation (the HMM's −0.0622) |
