# TSFM vs Reasoning LLM — the Rethink, and the Strategy Router as a Measured Next-Candle Edge Predictor (R2)

> **Priority**: operator ask, implemented · **Status**: R2 BUILT (shadow), tests 38/38,
> full suite at clean-HEAD baseline · **Date**: 2026-10-23 · **Repo**: HEAD `700f794` + this
> commit · **Roadmap refs**: `20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` §4 (R1/R2),
> `kairos-review-worklogs/20261023_STRIDE_TSFM_VALIDATOR_VS_LLM_REASONING_REVIEW.md`
> (the evidence review this decision rests on), vol-backtest Phase 0/0.5
> (`d205d42`/`4fe5203`/`24a52c8`), `20261019_VOL_CALIBRATION_AND_LLM_SERVER_REPURPOSE.md`

---

## 0. The operator's questions

1. **Rethink the use of TSFM — is it required at all — vs replacing it with a
   reasoning LLM, as an agentic workflow vs zero-shot?**
2. **Fully implement the market strategy router in the orchestrator as an
   edge predictor of the next candle.**

Both answered with one architecture: the router state becomes the
conditioning for a **measured** next-candle edge estimate (R2), TSFM's role
shrinks to a **measured competitor** on the same outcome stream with an exit
ladder, and the reasoning LLM gets the **categorical prior slot** (agentic
structured-choice), never the number.

---

## 1. The decision matrix (question 1, answered from evidence)

What each candidate can and cannot do for **P(next candle direction)** — the
quantity p_timesfm currently claims:

| Candidate | Mechanism | Evidence | Verdict |
|---|---|---|---|
| **TSFM zero-shot** (status quo: TimesFM-2.5 point forecast → z-map → p_timesfm) | Univariate black-box extrapolation; no covariates, no events, no cross-symbol; network dependency; 15-min cache | Its 50% weight in the live directional blend has **never been skill-measured**. The z-map exists only because the raw mapping saturated at p≈0.5 (yaml:61-65). TFRBench: TSFMs beat LLMs at numbers (Chronos-2 0.778 vs Gemini-3.1-Pro 1.408 MASE) **but** on the only two financial price series TimesFM-2.5 beat both Chronos-2 and STRIDE; Brini VOLARE: zero-shot TSFMs do not clear Log-HAR on RV; Rahimikia: poor on financial returns zero-shot AND fine-tuned | **Keep provisionally, measure honestly, prepare the exit.** Not "required" — replaceable by a measured conditional the repo already owns (§2) |
| **Empirical conditional from router state** (R2, this build) | P(up \| symbol, archetype, direction) from stamped outcome frequencies — decay-weighted Beta, base-rate anchor (PBM pattern), zero network, deterministic, self-calibrating | The router's predicates (regime vol-tier, macro/trend alignment, asset class, direction) are exactly the categorical conditioning STRIDE says pays; the repo's own regime_edge harness proved the measurement discipline works (HMM skill negative 14/14 — the same instrument now pointed at the router) | **THE replacement candidate.** Ships today as shadow; promotes on measured skill |
| **Reasoning LLM zero-shot numbers** | Ask the LLM for P(up) directly | The mirage, twice over: frontier LLMs ~2× worse MASE than a 120M TSFM (which itself is not the bar on financial series); token-fragmentation of numeric series; hallucinated causal links | **Ruled out.** Standing doctrine (never LLM probability into p_win/EV) now externally re-confirmed by STRIDE/TFRBench |
| **Reasoning LLM agentic (structured choice)** | Fixed-vocabulary categorical over numeric state (Clef-Flash pattern): {stance, vol_expansion}, event digests, provider disagreement | STRIDE's own ablation: **shuffled** reasoning is *worse than none* — mis-paired reasoning is negative value, so the categorical must be MEASURED before trusted; structured-choice is where small models hold up (vocabulary, not numbers) | **The future rung** — the categorical conditioning slot (§5), not a number source |

**The synthesis:** conditioning is where priors pay (STRIDE's P(Y|X,R));
the router state is Kairos's own R, already computed every tick; the honest
conditional frequency of next-candle direction given that state needs no
model call at all. TSFM keeps its seat only while its measured skill earns
it; the LLM enters as the categorical layer on the same state.

---

## 2. What was BUILT (question 2): the Router Edge Model

`regime_platform/sweep_orchestrator/router_edge.py` — R2 of the
router+registry split. R1 (2026-10-10) stamps the archetype; R2 turns the
verdict into a measured edge:

```
p_router = P(next candle up | symbol, archetype, direction)
         = (k0 · p_base + s_up) / (k0 + s_up + s_dn)
```

- **Cells**: `(symbol, archetype, direction)` — decay-weighted Beta counts
  (2^(−age/τ), PBM convention; τ default 90d), raw n kept for diagnostics,
  ties skipped (a flat candle carries no information).
- **Anchor**: the symbol's own pooled next-candle base rate (the PBM
  anchor-on-null shape; the empirical base IS the honest null here). k0
  default 20 pseudo-counts.
- **The tick loop** (every light sweep, every symbol — *not* only qualified
  signals: conditioning on the qualified sub-population would select on the
  very gates we measure past):
  1. **Resolve** the previous stamp: first bar newer than the stamped bar →
     close-vs-close outcome → update cell + base.
  2. **Score BOTH predictors on the same outcome**: p_router's and
     **p_timesfm's** directional calls feed separate decay-weighted skill
     accumulators — the TSFM-vs-router competition becomes a number
     (`router_skill` vs `timesfm_skill`), not an argument.
  3. **Route the current tick** (route() with the same labels R1 sees —
     macro/trend hoisted above compute_ev, reused by 5b1/5b2 via sentinels,
     zero extra round-trips) → read the cell → stamp point-in-time.
- **Storage**: param_store tiers (`router_edge` / `<archetype>:<direction>`,
  `_base`, `_router`, `_timesfm`, `_last`) — local-SQLite authority, Supabase
  mirror via the existing sync, restart-safe, **zero schema change** (the
  JSON-artifact-first shape the vol-recalib assessment §1.7.2 prescribed).
- **Report**: `router_edge_report()` / `scripts/measure_router_edge.py` —
  per-cell n_eff / p_up / acc / skill-vs-majority (the W-X2 convention that
  proved HMM's −14/14), plus both aggregate skills and the decision verdict.

**Blend wiring (the flag ladder):**

| Flag | Values | Default | Effect |
|---|---|---|---|
| `router_edge_mode` | off / shadow / **live** | **shadow** | shadow = stamp+measure only, EV byte-identical. live = p_router enters compute_ev as a 4th source (per-cell gate: n_eff ≥ `router_edge_min_n_eff` 30) |
| `timesfm_blend_mode` | off / shadow / **live** | **live** | live = today byte-identical. shadow = fetch+score but withheld from the blend (the A/B). **off = not fetched at all** — zero network cost; renormalization absorbs it |
| `router_edge_weight` | float | 5.0 | p_timesfm's peer (same /14 denominator — only ratios reach the renormalizer) |
| `router_edge_k0` / `_tau_days` / `_min_n_eff` | float | 20 / 90 / 30 | shrinkage / half-life / live-blend gate |

**ev_engine**: `p_router` is now a first-class source —
`P_WIN_WEIGHTS_FULL` gains `p_router: 5/14` (peer of p_timesfm; table ratio
5:4:5:5), available-source renormalization handles presence/absence exactly
like p_markov's, short-inversion matches p_timesfm's convention,
`EVFullResult.p_router` stamped. **No existing caller passes p_router →
every historical blend is byte-identical** (regression-tested against a
manual 2-source pool to the ulp).

**Heavy sweep**: `timesfm_blend_mode` applied (off → no fetch; shadow →
withheld from its compute_ev) so heavy/light price identically under the TSFM
ladder. The router-edge source is deliberately **light-only** (the heavy path
is the geometry grid, not a next-candle instrument) — documented divergence
in p_win_raw when router_edge goes live.

**leverage_eval**: `markov_weight()` made flag-aware (denominator = live
source subset) so the markov-withdrawal counterfactual matches whatever
blend produced the rows being measured.

---

## 3. Why this shape is the honest TSFM answer

1. **It converts the debate into a measurement.** Same stamps, same
   outcomes, two skill accumulators. After ~a day of light sweeps the
   `measure_router_edge.py` verdict line exists; after a week it has n_eff
   in the hundreds per symbol.
2. **The exit ladder needs no big-bang.** `timesfm_blend_mode: live → shadow`
   is the A/B (blend changes, fetch continues, skill keeps scoring);
   `→ off` kills the network dependency entirely. The renormalization
   machinery the engine already had makes each step one YAML line.
3. **The replacement is already conditioned on what the router knows.**
   If `momentum:buy` beats `stand_by:none` next-candle accuracy, the router's
   verdict carries information — the exact question R1's stamp was built to
   answer, now answered per-cell instead of per-archetype-promise.
4. **The LLM slot is preserved, not pre-empted.** p_router is a *numeric*
   source measured like any other; the reasoning LLM's future seat is the
   *categorical* prior over the same state (§5) — a different input class,
   composable with this one, judged by the same shadow-first discipline.

---

## 4. Operator decision gates (what to flip, when)

| Gate | Evidence | Action |
|---|---|---|
| Promote router edge | `router_skill > timesfm_skill` (mean, meaningful n) AND target cells n_eff ≥ 30 | `router_edge_mode: live` |
| A/B TimesFM | either skill clearly ahead | `timesfm_blend_mode: shadow` for a full refit window, compare p_win_raw/EV deltas |
| Exit TimesFM | `timesfm_skill < 0` at n_eff ≥ 100-ish (negative-skill source holding 50% of the directional blend) | `timesfm_blend_mode: off` — saves the :8002 dependency too |
| Keep both | parity | leave live/live; the cells keep accumulating either way |

Run `python -m scripts.measure_router_edge.py` (or `--symbol X` / `--json`)
on the box after a few days of sweeps. The report's verdict banner states
which gate the numbers support.

---

## 5. The reasoning-LLM rung (designed here, NOT built — needs its own evidence)

The slots from the STRIDE review, now sharpened by R2's existence:

1. **Event-window categorical vol prior** — `{stance, vol_expansion}` from
   odds+calendar+surprise (macro assessment Shape 2; event-odds migrations
   already landed in `24a52c8`). Consumption: `chronos2_cov` categorical
   covariate + HAR dummy + post-evidence `kelly_event_multiplier`. This is
   the literal P(Y|X,R) insertion and the TSFM vol program's one honest
   path back.
2. **Router-state categorical prior** — the natural extension of R2: the
   LLM answers a fixed-vocabulary question about the CURRENT router state
   (trend/macro/archetype alignment) as a second opinion on the cell;
   disagreement with the cell's own posterior is a measurable ambiguity
   stamp. Same fixed question, two providers (Clef-Flash + local tier-2) =
   free A/B (`model_provider` columns exist).
3. **Claims issuer** (`nt_macro_claims`) and **stamp-grounded rationale** —
   per the vol-cal doc §2.3C/D; unchanged designs.

**Non-slots (re-confirmed):** p_win/EV probability, vol magnitude, Kelly
multipliers, direction, entry/TP/SL. The reasoning LLM gets categories,
claims, and narratives — never the number.

---

## 6. Honest boundaries

- **Shadow = zero live effect.** The default flags change no blend, no
  gate, no published value. The EV call is byte-identical unless flipped
  (regression-tested).
- **The next-candle convention** is close-vs-close on the first fetched bar
  newer than the stamped bar, ties skipped. Fixed and self-consistent —
  skill-vs-majority is meaningful under any fixed convention, but it is NOT
  the brick-settlement event (that remains p_win_bracket's job).
- **Symbols without fetch timestamps** are skipped (no honest bar identity;
  `valid_timestamps` empty). Documented in the module.
- **Heavy/light p_win_raw divergence** when router_edge goes live (heavy
  has no p_router source) — acceptable for an audit column, revisit if
  router_edge promotes.
- **The cells start empty.** p_router emits the 0.5 prior until evidence
  arrives — no cold-start overconfidence, by construction (k0 shrinkage).
- **This does not touch the vol program.** Phase 0.5 recalib
  (`24a52c8`) is the vol thread; R2 is the direction thread. They meet only
  at the future categorical-prior slot (§5.1).
- **Agentic ≠ auto-promote.** Nothing here flips a live flag on model
  output; the operator flips flags on report evidence. Same doctrine as
  every ladder in this repo.
