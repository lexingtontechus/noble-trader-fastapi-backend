# HMM → first-passage + regime-transition reinforcement (build worklog)

**Date:** 2026-10-01 · **Status:** AGREED — decisions taken, Phase 1 ready to build
**Repo:** `noble-trader-fastapi-backend`
**Scope (parent):** `docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md`
**Evidence (parent):** `worklog/kairos/reports/20261001_rm1_markov_source_eval.md`
**Supersedes:** `worklog/20260825_CHANGE_B_MARKOV_SHADOW_MODE.md` (Change B staging — moot)

---

## 0. Decisions taken (2026-10-01, user)

| # | Decision | Consequence |
|---|---|---|
| **D1** | Retire the `trend_hmm_shadow` comparison | `markov_source_mode: legacy_hmm` — **shipped** (`5e195bf`) |
| **D2** | Withdraw `p_markov_hold_n` from the EV blend | `p_markov_hold_n_mode: "off"` — **shipped** (`5e195bf`) |
| **D3** | **Agree scope §2.4** — the platform already answers the Markov question exactly | `tiered_atr.walk()` becomes the replacement substrate, not the HMM |
| **D4** | **Agree option E is the real target** — first-passage replaces the HMM | A/B/C/D demoted to optional hygiene; effort goes to E |
| **D5** | **Reinforce with regime transition** — freshness × type conditions the first-passage read | E and the Kairos transition programme compose rather than compete |

D1/D2 are live. D3–D5 are the programme this worklog plans.

---

## 1. Why the HMM is being replaced (the forcing measurement)

31,328 decided `nt_signal_sim` rows carrying `legacy_markov_state`
(2026-05-31 → 2026-10-01, `outcome ∈ {tp_hit, sl_hit}`).

| model | accuracy | skill vs baseline |
|---|---:|---:|
| legacy HMM (its own directional calls) | 0.5251 | **−0.0404** |
| trend_hmm | 0.3230 | −0.2426 |
| naive "always DOWN" | **0.5655** | — |
| shrunk per-symbol base rate (k=20) | **0.5898** | — |

**The legacy HMM scores below both trivial baselines.** Mean per-symbol skill
**−0.0622** (positive on 5/14; USDCAD +0.069 best, AAPL −0.320 worst). Calls are
mis-mixed (UP 17.6% / DOWN 17.4% against a 43.5/56.5 DOWN population) and mildly
inverted (P(UP | says UP) = 0.4909 < P(UP | says DOWN) = 0.4403 is correct in
*ordering* but both sit near 0.5 with DOWN likelier in each bucket). FLAT — 65% of
all decided rows — deviates from the base rate by 1.7 points: not a signal.

**This is not a tuning problem.** A model whose directional calls are worse than
saying nothing is not one feature-scaling fix away from working. Three structural
defects compound (§2 of the scope): unscaled 9-feature fit, symmetric-in /
asymmetric-out labelling, and per-tick refit against a 1h label that flaps when
price does not move.

---

## 2. The thesis (D3 + D4)

> **The HMM is a noisy 3-state detour to a question the platform already answers
> exactly.**

`p_markov_hold_n` asks: *P(price reaches TP bricks within N bars | current state)*,
computed as `T^N[current][target]` — a matrix power over a discretised abstraction.

`tiered_atr.walk()` answers the same question **on the actual price path**:

```python
def walk(paths, tp_atr, sl_atr):
    hit_tp = paths >= tp_atr          # paths are (n,48), direction-signed, ATR-scaled
    hit_sl = paths <= -sl_atr
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    tp_first = (ti < EXPIRY_BARS) & (ti <= si)   # TP first on a same-bar tie
    ...
```

No states, no EM, no covariance assumption, no refit instability. It is already
validated in production — it is what `dynamic_atr_pick` selects grids with, and
what the monthly re-opt proposes clamps from (XAUUSD 4.94/1.00, BTCUSD 8.07/1.77).

So the replacement is not "a better HMM". It is **removing the abstraction** and
exposing the first-passage probability as a first-class EV input, computed by
machinery that is already trusted.

### 2.1 What is genuinely new (and must be measured, not assumed)

`walk()` today returns `(pnl, expired)` for **grid selection** — it is asked
"which TP/SL is best?" It is *not* asked "what is P(TP before SL) for this
signal, right now?" Those are different products:

| | grid selection (today) | first-passage source (E) |
|---|---|---|
| unit | a whole asset class / symbol over 10–45d | one signal, at entry |
| output | the best `(tp, sl)` pair | a probability for the *live* bracket |
| conditioning | pooled | per-signal features |
| consumer | geometry | EV blend |

E reuses `walk()`'s arithmetic but changes its question and its unit of
observation. That distinction is the whole reason E needs its own ΔD evaluation
and cannot inherit the geometry work's credibility.

### 2.2 The naive baseline is the bar — and it is a real model

The base-rate result is not just a kill shot for the HMM; it is a **candidate**.
A shrunk per-symbol base rate scores 0.5898 with 100% coverage and no fitting.
Any replacement must beat it, and §5 makes it the explicit control arm. If
first-passage cannot beat "predict the symbol's own base rate", the honest answer
is to ship the base rate (option F + a calibrated rate), not to keep building.

---

## 3. The reinforcement (D5): first-passage × regime transition

D3/D4 alone would give a **static** probability: at entry, given the bracket and
the path distribution, P(TP before SL). That ignores the one thing the platform
has already proven *does* carry information about timing.

### 3.1 What is proven (Kairos R1/R2)

| finding | measurement | status |
|---|---|---|
| **Freshness** — a regime label is most informative immediately after it flips | `fresh − deep` y_acc = **+0.117** [+0.105, +0.129], n=24,768 / 5,188 | **supported** |
| **Type** — direction flips are the only non-negative transition type | `dir_flip` y_acc **+0.007**; `both` −0.014; `vol_contract` −0.047; `vol_expand` −0.052 | supported |
| **Surprise** — rarity of the HMM transition does not rank accuracy | Spearman = **0.004** | **rejected** — dropped |
| Composite `rank_score` (0.35 freshness + 0.25 type + 0.25 recent_tp + 0.15 cell_p) | IC 0.126 all-eras / 0.199 post-debounce; ΔD **+0.233** / **+0.295** | discovery (R2); post-ATR n=43, CI spans 0 |

Both live primitives already exist and are pure:

- `kairos_rank.event_window(τ)` → `pre | fresh | warm | stale | deep`
- `kairos_rank.transition_type(prev_vol, prev_dir, new_vol, new_dir)` → `dir_flip | vol_expand | vol_contract | both | none`
- `kairos_rank.freshness_score(window)` → 0–1 (`FRESHNESS_W`)
- `kairos_rank.type_score(ttype)` → 0–1 (`TYPE_W`, `dir_flip` highest)
- `kairos_rank.RankScoreTracker` — trailing, restart-safe, no lookahead

### 3.2 The composition

```
                    ┌─────────────────────────────────────────┐
   price path  ───► │  walk() first-passage                   │
   + live bracket   │  P(TP before SL | bracket, path dist)   │ ──► p_first_passage
                    └─────────────────────────────────────────┘            │
                                                                           │
                    ┌─────────────────────────────────────────┐            ├──► EV blend
   regime cell ───► │  event_window τ  ×  transition_type     │ ──► w_fresh │
   change at T0     │  (fresh beats deep by +0.117)           │            │
                    └─────────────────────────────────────────┘            │
                                                                           │
                    ┌─────────────────────────────────────────┐            │
   trailing outcomes│  cell_p / recent_tp (RankScoreTracker)  │ ──► level  ┘
                    └─────────────────────────────────────────┘
```

**The mechanism (hypothesis H-E1):** first-passage gives the *level*
(P(TP before SL) for this bracket); freshness × type gives the *timing weight*
(how much to trust that level right now). A signal entering `fresh ∧ dir_flip`
should carry more of its first-passage read into the blend than one entering
`deep ∧ vol_contract`.

This is exactly what §4.3 of the ranking scope called for — *"Only then
regime-transition conditioning as an overlay: weight size/entry by
fresh × type × surprise, if T1–T5 pass."* T1 and H3 passed; H4 (surprise) was
rejected and is dropped. E supplies the level the overlay was always missing.

### 3.3 What this is NOT

- **Not** re-opening the HMM under a new name. No states, no transition matrix,
  no EM. `MARKOV_SOURCE_MODE` and the legacy model stay as D1/D2 left them.
- **Not** re-opening age/duration. V2/V4 disproved monotone age slopes; the
  transition programme is event-time, and this worklog inherits that constraint.
- **Not** a gate. Phase 1 is measurement only — same discipline as every other
  lever on this platform.

---

## 4. Build plan

Four phases. Each ends in a measurable artefact; no phase assumes the next.

```
Phase 1  skill diagnostic (§5.1 of the scope)          ← BUILD NOW
         + p_first_passage primitive (offline, no live wiring)
              │
Phase 2  offline evaluation: first-passage vs base-rate control vs old HMM
         + freshness/type overlay conditioned on top
              │
Phase 3  shadow stamp on nt_signal_sim (additive column), ΔD on live episodes
              │
Phase 4  one lever at a time vs the current blend, on scorecard D
              │
         ├─ clears bar -> p_markov_hold_n_mode: "live" (first-passage source)
         └─ fails      -> ship the calibrated base rate; retire the model (F)
```

### Phase 1 — what gets built (this change)

**1a. Skill diagnostic** — the instrument the RM-1 review lacked.

| | |
|---|---|
| **Module** | `regime_platform/sweep_orchestrator/hmm_shadow_accuracy.py` (extend) |
| **New metric** | `skill` — accuracy − per-symbol base rate, per `(symbol, source)` |
| **Also** | `base_rate`, `coverage` (already stored), era halves |
| **Table** | `nt_hmm_shadow_accuracy` — add `skill`, `base_rate` columns (migration 191) |
| **Cadence** | chained onto the existing weekly job (no new scheduler entry) |
| **Changes live?** | no — read-only |

*Why:* RM-1 could only report `accuracy` because that is all the table stores. It
took a bespoke `nt_signal_sim` query to discover the model is *below baseline*. A
reviewer reading `0.527` would reasonably conclude "marginally useful". The
number that matters is `−0.0404`, and it should be on the table. Precedent:
`kairos_rank.py` exists because `p_win`'s IC was not being measured, and finding
that it did not rank changed the programme.

**1b. `p_first_passage` primitive** — pure function, no live wiring.

| | |
|---|---|
| **Module** | `regime_platform/sweep_orchestrator/first_passage.py` (new) |
| **Reuses** | `tiered_atr.walk()` arithmetic; `build_paths()` shape |
| **Inputs** | direction-signed ATR-scaled paths for the signal's own bracket |
| **Output** | `P(TP before SL)`, `expiry_share`, `n_paths` |
| **Live wiring** | **none** — Phase 1 is offline only |

**1c. Overlay** — `first_passage_score(p_fp, window, ttype)` combining the level
with the freshness/type weight. Pure; frozen weights declared before scoring
(same discipline as `rank_score`).

### Phase 2 — offline evaluation

- Population: decided `nt_signal_sim` rows with a completed 48-bar window.
- Arms: **first-passage** / **shrunk base rate (control)** / **legacy HMM (incumbent)**.
- Metrics: accuracy, skill vs base rate, coverage, **ΔD** (accuracy vs no-skill,
  gated vs rest), era halves, symbol-day clustered CIs.
- The overlay is scored as an increment on top of each arm, never alone.

### Phase 3 — shadow stamp

Additive nullable column on `nt_signal_sim` (e.g. `p_first_passage_shadow`,
`fp_window`, `fp_type`), written fail-open at capture. Read by nothing. Same
pattern as `kairos_rank_score` (migration 172) and the TimesFM quantile capture
(migration 186).

### Phase 4 — one lever

Only after Phase 3 accumulates the bar below. Then a single change to the blend,
measured on scorecard D, with the other 7 gates untouched.

---

## 5. Measurement protocol (pre-registered)

**Primary metric — scorecard D**, consistent with the methodology triage:
`ΔD = mean(y_acc | gated) − mean(y_acc | rest)` where `y_acc = 1{TP first} − SL/(TP+SL)`.
Not raw TP rate, not `m₂₄`.

**Bar for promotion to live:**

| # | requirement | threshold |
|---|---|---|
| B1 | accuracy vs the shrunk base-rate control | **must beat it** (control = 0.5898 today) |
| B2 | coverage | ≥ 0.30 |
| B3 | consistency | positive skill on ≥ 2/3 of symbols |
| B4 | era split | holds in both halves |
| B5 | **incremental** | beats `p_regime` + `p_timesfm` alone on ΔD, CI > 0 |
| B6 | episode unit | symbol × direction × hour, deduped (raw-row AUC is inflated) |
| B7 | cluster CIs | symbol-day clustered, not naive |

**B5 is the real gate.** A source can be individually accurate and still add
nothing to a blend that already contains a correlated regime read — that is
precisely what happened with `p_markov_hold_n`.

**Kill criteria — take option F if:**

- first-passage fails to beat the base-rate control by 2026-11-01, **or**
- the overlay adds nothing beyond first-passage alone (freshness/type already
  priced by `p_regime`), **or**
- B5 fails at power.

On kill: ship the calibrated base rate as the replacement level, delete the
legacy HMM (`services/hmm/model.py`, `feature_buffer.py`, the `hmm_quote_fetcher`
call, `MARKOV_SOURCE_MODE`/`p_markov_hold_n_mode`). Keep `hmm_ta_fetcher` /
`latest_ta()` — separate purpose, feeds `nt_regime_log.ta_*`.

---

## 6. Risks

| # | risk | mitigation |
|---|---|---|
| R1 | **Look-ahead.** `walk()` needs the full 48-bar window; a live signal does not have it. | Phase 1 is offline on *resolved* rows only. Any live stamp must use trailing paths only — the same no-lookahead rule `build_paths()` already enforces (`j + EXPIRY_BARS > len(t)` → skip). |
| R2 | **First-passage duplicates `dynamic_atr_pick`.** | Different unit (§2.1) — but this is the honest objection and B5 is what settles it. If the increment is zero, E has no product. |
| R3 | **Base rate is era-dependent.** The 56.5% DOWN share reflects a broadly USD-up 2026 window. | Walk-forward; era halves (B4); shrink toward population (k=20) already applied. |
| R4 | **`cell_p` persistence inflates ΔD.** Already flagged in the ranking scope §9.2 — much of the lift is trailing hit-rate + freshness, which is path-overlapping. | Symbol-day clustered CIs (B7); episode unit (B6). |
| R5 | **Overlay overfits to the 09-26 discovery window** (mixed-era, mostly pre-debounce). | Phase 2 splits by era; promote only on post-ATR confirmation. |
| R6 | **Post-ATR sample is thin** (n=43 episodes at last read). | Phase 3 accumulates before Phase 4; no promotion on n=43. |

---

## 7. What is NOT being built

- No new HMM, no HMM variant, no re-run of the legacy-vs-trend_hmm comparison.
- No option B (re-label states by realized outcome) — B was the strongest
  *HMM* fix, and D4 removes the HMM from the target. Revisit only if E fails and
  a discrete state is independently wanted.
- No option C (asymmetric prior) — same reason; also the era-overfit risk.
- No gate change. Phase 4 is the first point a live lever could move.
- No deletion of the legacy model yet — it stays fitting as the incumbent arm.

Options A (standardize features) and D (debounce state) remain **optional
hygiene**: each ~1 day, independent, and worth doing only if a discrete-state
regime read is wanted for something other than the EV blend (e.g. a cheap
low-latency regime signal elsewhere).

---

## 8. Open items for the user

| # | item | default if no answer |
|---|---|---|
| O1 | Build Phase 1 (skill diagnostic + `p_first_passage` primitive) now? | **yes** — the plan assumes it |
| O2 | Confirm the 2026-11-01 kill deadline | stands as written |
| O3 | Where should the overlay weight live — `config/feature_flags.yaml` (hot-reload) or frozen in code like `rank_score`? | frozen in code for Phase 2; YAML only if promoted |
| O4 | Should the base-rate control ship as a live source if E fails? | deferred to the kill decision |

---

## 9. References

| artefact | what |
|---|---|
| `docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md` | options A–F, re-entry bar (§3), the §2.4 thesis |
| `worklog/kairos/reports/20261001_rm1_markov_source_eval.md` | RM-1 evaluation — the shadow decision |
| `worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` | T1/H3 supported, H4 rejected; §4.3 overlay; `rank_score` |
| `worklog/kairos/reports/20260926_rank_score_eval.md` | ΔD by era; post-ATR n=43 |
| `worklog/kairos/reports/20260926_rank_transition.md` | event-time + type tables |
| `regime_platform/sweep_orchestrator/tiered_atr.py` | `walk()`, `build_paths()`, `DynamicAtrPick` |
| `regime_platform/sweep_orchestrator/kairos_rank.py` | `event_window`, `transition_type`, `freshness_score`, `type_score`, `RankScoreTracker`, `delta_d` |
| `regime_platform/services/hmm/model.py` | the legacy HMM being replaced |
| `worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md` | Change A/B, Tier 1/2 history |
