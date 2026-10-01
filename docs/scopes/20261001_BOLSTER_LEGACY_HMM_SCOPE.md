# Bolster the legacy HMM — research scope & proposal

**Date:** 2026-10-01 · **Status:** PROPOSED (nothing built; decisions requested)
**Parent:** `worklog/kairos/reports/20261001_rm1_markov_source_eval.md` (RM-1 evaluation)
**Trigger:** RM-1 early decision — retire `trend_hmm_shadow`, drop `p_markov_hold_n` from the EV blend
**Repo:** `noble-trader-fastapi-backend` · **Model:** `regime_platform/services/hmm/model.py`

---

## 0. Why this document exists

The RM-1 evaluation retired the shadow comparison and withdrew `p_markov_hold_n`
from the blend. That closes the *question* ("legacy or trend_hmm?") but leaves the
*asset*: the legacy HMM is still fitted every tick and still writes
`nt_sweep_result.markov_p_up` / `markov_p_dn`.

This scope asks the harder question the evaluation surfaced: **is the legacy HMM
worth keeping at all, and if so what would make it informative?**

It is a proposal. No flag flips, no model edits. The only buildable item is a
diagnostic that does not exist yet (§5.1).

---

## 1. The measurement that forces the question

Population: 31,328 decided `nt_signal_sim` rows carrying a `legacy_markov_state`
(2026-05-31 → 2026-10-01, `outcome ∈ {tp_hit, sl_hit}`).

### 1.1 The naive baseline beats the model

| model | calls | accuracy | coverage | skill vs baseline |
|---|---:|---:|---:|---:|
| legacy HMM (directional calls only) | 10,971 | **0.5251** | 0.350 | **−0.0404** |
| trend_hmm | 18,475 | 0.3230 | 0.590 | −0.2426 |
| **naive "always predict DOWN"** | 31,328 | **0.5655** | 1.000 | — |
| **shrunk per-symbol base rate (k=20)** | 31,328 | **0.5898** | 1.000 | — |

**The legacy HMM's accuracy is *below* both trivial baselines.** Its 0.525 is not
weak skill — it is negative skill. Saying nothing and predicting the base rate is
**+0.065 better** than the model's own directional calls.

### 1.2 It is anti-predictive, and mis-mixed

| when legacy says | realized UP | realized DOWN | n |
|---|---:|---:|---:|
| UP | 0.4909 | 0.5091 | 5,520 |
| DOWN | 0.4403 | **0.5597** | 5,451 |

The direction *ordering* is correct (UP-calls really do have more UP outcomes than
DOWN-calls: 0.4909 vs 0.4403), but the **magnitudes are inverted relative to the
call**: when it says UP, DOWN is still the more likely outcome (0.5091 > 0.4909).
A 3-class coin flip would give 0.333; the model is at 0.49/0.51.

Call mix is near-symmetric (UP 17.6% / DOWN 17.4% / FLAT 65.0%) while the realized
population is 43.5% UP / 56.5% DOWN — so the model calls UP and DOWN equally in a
population that is 13 points more DOWN. That mix skew alone accounts for much of
the deficit.

### 1.3 FLAT carries nothing

FLAT rows (n=20,357, 65% of all decided rows): realized UP 0.4176 vs population
0.4345. A 1.7-point deviation — **FLAT is indistinguishable from the base rate.**
The model's silence is not a signal, it is 65% of the population doing nothing.

### 1.4 Per-symbol skill

| symbol | n | base | coverage | accuracy | skill |
|---|---:|---:|---:|---:|---:|
| USDCAD | 1316 | 0.6444 | 0.406 | 0.7135 | **+0.0691** |
| XAUUSD | 2891 | 0.5164 | 0.300 | 0.5582 | +0.0418 |
| XAGUSD | 3264 | 0.5564 | 0.513 | 0.5764 | +0.0200 |
| AUDUSD | 892 | 0.6031 | 0.353 | 0.6190 | +0.0159 |
| XAUAUD | 3286 | 0.5627 | 0.341 | 0.5657 | +0.0030 |
| XAUEUR | 3233 | 0.5032 | 0.315 | 0.4971 | −0.0062 |
| USDCHF | 1256 | 0.5183 | 0.381 | 0.5052 | −0.0131 |
| US30USD | 2216 | 0.6444 | 0.368 | 0.5576 | −0.0868 |
| EURUSD | 854 | 0.6335 | 0.330 | 0.5426 | −0.0909 |
| US500 | 2019 | 0.6241 | 0.355 | 0.5251 | −0.0989 |
| USDJPY | 2415 | 0.5222 | 0.447 | 0.4032 | −0.1190 |
| GBPUSD | 1145 | 0.6306 | 0.332 | 0.4974 | −0.1332 |
| BTCUSD | 2797 | 0.5020 | 0.213 | 0.3490 | −0.1530 |
| AAPL | 3744 | 0.8114 | 0.292 | 0.4918 | **−0.3197** |

**Mean skill: −0.0622.** Positive on 5/14, negative on 9/14. USDCAD (+0.069) is the
only symbol with a meaningful positive read; AAPL (−0.320) is catastrophically
anti-predictive — its base rate is 0.8114 (81% of AAPL's decided outcomes are
DOWN) and the model calls at 0.4918.

---

## 2. Root cause — three defects, all structural

### 2.1 Unscaled features (known)

`FeatureBuffer` supplies 9 features (log-return + 3 microstructure + 5 TA) with
**wildly different scales**; `GaussianHMM` is fit on them raw. A single diagonal
covariance per component must cover all nine, so the fit is dominated by whichever
feature has the largest variance. The log-return component — the one that decides
UP/DOWN — is drowned.

This is the documented instability (`startprob_ must sum to 1 (got nan)`, state
collapse). `RegimeHMM` already fixed this class of bug via Change A feature
standardization; the legacy model never got the same fix.

### 2.2 Symmetric-in, asymmetric-out

The model is fit with no knowledge that the realized population is 43.5/56.5 DOWN.
Its state-mean threshold (`STATE_MAP_THRESHOLD_STDS = 0.25` × σ) is symmetric about
zero, so it produces roughly balanced UP/DOWN calls — exactly the mix skew in §1.2.
Nothing in the fit anchors it to the actual outcome distribution.

### 2.3 Refit-every-tick against a 1-hour label

`fit()` runs every light sweep (15 min) on a rolling ~300-bar window and the
state is re-derived per tick. `markov_current_state` flapped UP→FLAT between
consecutive ticks with no price move (already documented, and the reason
`MARKOV_EMA_ALPHA=0.30` smoothing exists). **A model whose state label changes when
nothing moves cannot carry directional information** — the label is fitting noise.

### 2.4 The structural question underneath all three

`p_markov_hold_n` asks: *"P(price reaches TP bricks within N bars | current state)"*,
derived as `T^N[current][target]`.

The **geometry-free version of that exact question is already answered** by
`tiered_atr.py`'s `walk()` first-passage machinery — which is what `dynamic_atr_pick`
uses to select TP/SL grids, and what the monthly ATR re-opt uses to propose new
ones. It is validated (XAUUSD 4.94/1.00, BTCUSD 8.07/1.77) and it works directly on
price paths rather than through a discretized 3-state abstraction.

So the legacy HMM is a **noisy 3-state detour to a question the platform already
answers exactly.** That is the strategic finding, and it reframes the whole
programme: the goal is not "make the HMM accurate", it is "decide whether a
discrete Markov state adds anything the first-passage walk does not".

---

## 3. What a re-entry bar looks like

`p_markov_hold_n_mode: "live"` should only be flipped if a candidate clears:

| bar | threshold | why |
|---|---|---|
| accuracy | **≥ 0.64** | shrunk base rate (0.5898) + 0.05 margin |
| coverage | **≥ 0.30** | a source that calls 10% of the time cannot move a blend |
| consistency | **≥ 2/3 of symbols positive skill** | not one symbol carrying the average |
| era split | holds in both halves | guards against a single-regime fit |
| incremental | beats `p_regime` + `p_timesfm` alone on **ΔD** (accuracy vs no-skill, gated vs rest) | the actual product question |

The last row is the one that matters. A source can be individually accurate and
still add nothing to a blend that already contains a correlated regime read.

---

## 4. Candidate approaches

Ranked by (expected value ÷ build cost). Each is independent; they can be run in
any order.

### 4.1 A — Standardize the features (fix the known bug) · *low cost, low risk*

Fit `GaussianHMM` on z-scored features (`(x − μ)/σ` per feature, computed on the
fit window), keeping the existing 5-state → 3-label aggregation.

- **Why:** directly addresses §2.1; mirrors the Change A fix that already worked
  for `RegimeHMM`.
- **Expected:** removes the NaN/collapse class entirely; may lift accuracy toward
  the base rate.
- **Honest expectation:** probably **does not** clear §3 on its own — §2.2 and §2.3
  are untouched. This is the necessary hygiene step, not the fix.
- **Cost:** ~1 day + tests. Highest confidence, lowest ceiling.

### 4.2 B — Re-label to the outcome the platform actually trades · *low cost*

Stop mapping states by `mean log_return`. Label each state by its **realized
first-passage outcome** using the `walk()` machinery: for the bars assigned to each
component, what fraction reached TP before SL under the live geometry?

- **Why:** aligns the label with the objective. The current label answers "did price
  drift up?" — the trade asks "did price reach TP before SL?" These differ, and
  §1.2 shows the mismatch.
- **Expected:** the strongest single change available; converts a proxy label into
  the target label.
- **Cost:** ~2-3 days. Requires joining component assignments to resolved outcomes.

### 4.3 C — Asymmetric fit / prior correction · *low cost*

Seed the initial state distribution and the label threshold from the realized
population (43.5/56.5) rather than a symmetric zero threshold.

- **Why:** §2.2. Cheap, and directly attacks the mix skew.
- **Risk:** risks overfitting to a regime; the 56.5% DOWN share is itself
  era-dependent (the 2026 window was broadly USD-up). Must be walk-forward.
- **Cost:** ~1 day. Treat as a companion to A/B, not standalone.

### 4.4 D — Hysteresis / debounce the state label · *low cost*

Apply the same debounce discipline the platform already uses for `regime_label`
(`regime_label_debounce_mode` / `regime_label_debounce_ticks`) to
`markov_current_state`, instead of EMA-smoothing the *output* probability.

- **Why:** §2.3. Smoothing the output hides flapping; it does not stop the model
  from re-deriving a different state each tick. Debouncing the state is the root
  fix, and the pattern already exists in-repo.
- **Cost:** ~1 day. Low risk, and independently useful even if the source stays off.

### 4.5 E — Replace the state with the first-passage probability directly · *medium cost, strategic*

Stop deriving `p_markov_hold_n` from a transition matrix. Compute the hold
probability directly from `walk()` on the live geometry — the exact question, no
3-state abstraction, no HMM.

- **Why:** §2.4. This is arguably what the source *should have been*. It reuses
  validated machinery and removes an entire unstable component.
- **Risk:** it is no longer "the Markov source" — it duplicates what
  `dynamic_atr_pick` already computes. The real question becomes whether a
  *first-passage probability* (as distinct from the ATR grid *selection*) earns
  blend weight. That is a legitimate and testable question.
- **Cost:** ~3-4 days. Needs its own ΔD evaluation before any live flip.

### 4.6 F — Retire it permanently · *zero cost*

If A–E all fail to clear §3, delete the source and the model
(`services/hmm/model.py`, `feature_buffer.py`, the `hmm_quote_fetcher` call, the
`MARKOV_SOURCE_MODE`/`p_markov_hold_n_mode` flags). Keep `hmm_ta_fetcher` /
`latest_ta()` — separate purpose, feeds `nt_regime_log.ta_*`.

- **Why:** a permanently-off source is dead weight and per-tick compute.
- **Note:** this is exactly the "B5" deletion the original Change B plan staged,
  now reachable directly because the blend no longer depends on the model.

---

## 5. Proposed sequence

```
Phase 0 (now)   p_markov_hold_n_mode: off, markov_source_mode: legacy_hmm
                legacy HMM keeps fitting (capture continues)
      │
Phase 1         §5.1 measurement harness  ← the ONLY build in this proposal
      │         A (standardize) + D (debounce)   [low cost, both hygiene]
      │
Phase 2         re-measure against §3's bar
      │         ├─ clears  -> Phase 3
      │         └─ fails   -> B (outcome label) or E (first-passage)
      │
Phase 3         ΔD incremental test vs p_regime + p_timesfm alone
      │         ├─ adds  -> p_markov_hold_n_mode: "live"
      │         └─ flat  -> F (retire permanently)
```

### 5.1 Phase 1 — the one buildable item

A diagnostic that does not exist today: **per-symbol HMM skill vs the naive
baseline**, reusing the existing `hmm_shadow_accuracy.py` shape.

| | |
|---|---|
| **Reads** | `nt_signal_sim` (`legacy_markov_state`, `signal`, `outcome`, `symbol`, `ts`) |
| **Computes** | per symbol: base rate, coverage, accuracy, **skill = accuracy − base rate**, era halves |
| **Writes** | `nt_hmm_skill` (or extend `nt_hmm_shadow_accuracy` with a `skill` metric — smaller change) |
| **Cadence** | chained onto the existing weekly job, alongside `build_hmm_shadow_accuracy` |
| **Changes** | nothing live; read-only |

**Why it is the right first build:** the RM-1 evaluation could only report
`accuracy` because that is all `nt_hmm_shadow_accuracy` stores. It took a bespoke
query against `nt_signal_sim` to discover the model is *below baseline* — a fact
that should have been on the review table all along. Without `skill` stored, every
future re-evaluation repeats that work and can again mistake "0.527" for
"marginally useful".

This mirrors the platform's own precedent: `kairos_rank.py` exists because
`p_win`'s IC was not being measured, and the discovery that it did not rank changed
the programme. The same instrument is missing here.

---

## 6. Open questions for the user

| # | Question | Recommendation |
|---|---|---|
| Q1 | Build §5.1 (the skill diagnostic) now? | **Yes** — smallest possible step, and it is the instrument the RM-1 evaluation had to hand-roll |
| Q2 | Which Phase-1 candidate — A, D, or both? | **Both** — each ~1 day, independent, and both are hygiene rather than speculation |
| Q3 | Is E (first-passage replacing the HMM) the real target? | **Worth a decision now** — if yes, A/B/C/D are steps toward a component we intend to replace, and effort should go to E instead |
| Q4 | Set a retirement deadline? | Suggest: if Phase 2 does not clear §3 by **2026-11-01**, take F (retire permanently) rather than carrying it indefinitely |

**Q3 is the pivotal one.** §2.4 says the platform already answers the Markov
question exactly, via validated machinery. If that is accepted, the honest
programme is E-then-F, and A–D are only worth doing if the discrete-state
representation is independently valuable (e.g. as a cheap, low-latency regime read
for something other than the EV blend).

---

## 7. Evidence & reproduce

All numbers in §1 come from `nt_signal_sim`, read-only:

```bash
# population base rate + per-symbol skill
curl "$SUPABASE_URL/rest/v1/nt_signal_sim?select=symbol,signal,outcome,legacy_markov_state\
&outcome=in.(tp_hit,sl_hit)&legacy_markov_state=not.is.null&limit=1000&offset=0" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY"
# (paginate with offset until an empty batch; 31,328 rows as of 2026-10-01)
```

| artefact | what |
|---|---|
| `worklog/kairos/reports/20261001_rm1_markov_source_eval.md` | RM-1 evaluation — the shadow decision |
| `regime_platform/services/hmm/model.py` | the legacy HMM (`fit`, `emit_markov`) |
| `regime_platform/services/hmm/feature_buffer.py` | the 9 unscaled features (§2.1) |
| `regime_platform/sweep_orchestrator/tiered_atr.py` | `walk()` first-passage (§2.4, §4.5) |
| `regime_platform/sweep_orchestrator/hmm_shadow_accuracy.py` | the diagnostic §5.1 extends |
| `worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md` | Change A/B, Tier 1/2 history |
