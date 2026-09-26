# Directional edge, p_win ranking, and regime *transitions* (not age)

**Date:** 2026-09-26 · **Type:** research scope + build · **Parent:** `20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md`, V3 scope §15
**Evidence so far:** G2.2 NO PASS at power (Δm₂₄ −0.32); p_win overconfident every era; V2/V4 age+plane disproved; geometry (dynamic ATR) proven on expectancy
**Build:** `regime_platform/sweep_orchestrator/kairos_rank.py` · `scripts/kairos_rank_transition.py` · `tests/test_kairos_rank.py`

---

## 0. The two product questions

| # | Question | Status |
|---|---|---|
| **Q1** | Does **anything** rank signals within a cell (so `p_win` can be a score)? | Open — scorecard I thin; this scope’s first job |
| **Q2** | Can **regime transitions** locate edge and its decay (user thesis)? | Age/plane **failed**; event-time transitions **not yet tested** |

Directional edge for the product = **beating the bracket no-skill line** under the geometry the book actually uses (now dynamic ATR), not raw m₂₄ and not raw TP rate.

---

## 1. Why age / plane was the wrong object

| Failed construct | What it measured | Why it died |
|---|---|---|
| Regime **age** → edge decay | Hours since run start vs mean m₂₄ | Slope flips sign by quarter; almost no mass ≥ 24h; V2 disconfirmed |
| Regime **age** → exit hazard | Non-memoryless duration | Hazard flat 0.09–0.13/h (geometric) on 1h labels |
| Quadrant **plane** (level × momentum of rolling edge) | 4-box state | Flow = noise under circular-shift null; rotation was window mechanics |
| R-based age knobs | W/L, P/L | Geometry-dependent; withdrawn |

**What is still true from 09-23:** most symbol-level *edge-state* changes and diagonal jumps **coincide with a regime-cell change**. The transition *event* may carry information even when *duration* does not.

That is the split to keep:

- **Duration (age)** — dead on 1h as a knob driver.
- **Transition events and their aftermath** — untested in event time; this is the sound residual thesis.

---

## 2. Sound regime-transition programme (replaces age/plane)

Do **not** rebuild L4/L5. Measure transitions as **events** with a local clock and typed outcomes.

### 2.1 Event time (not age bands as a state)

For each symbol and each regime-cell change at T₀ (first bar of the new cell, debounced):

| Window | Symbol | Question |
|---|---|---|
| T₀−6h … T₀ | `pre` | Was edge already dying *before* the observed flip? (anticipation / lag) |
| T₀ … T₀+3h | `fresh` | Highest information regime identification |
| T₀+3h … T₀+12h | `warm` | Information half-life of the *new* label |
| T₀+12h … T₀+24h | `stale` | Does the label still condition edge? |
| ≥ T₀+24h | `deep` | Baseline “mid-regime” (what V2 lumped into age) |

**Stats (pre-registered):**
- Geometry-free `m_h` **and** accuracy vs no-skill (under recorded/dynamic brackets).
- Target contrast: `fresh − deep`, not a monotone age slope.
- Null: circular-shift the transition times vs the outcome series (same machinery as V1–V4).
- Cluster: symbol-day. Holm over the 4 windows × 2 metrics.

**Hypothesis T1 (information freshness):** |edge| or accuracy excess is higher in `fresh` than `deep`.  
**Hypothesis T2 (decay shape):** if T1 holds, fit a half-life on event time — *this* is “edge decay”, not age of the regime.

### 2.2 Transition type (signed, not just “a change”)

From cell C = (vol_tier, dir) → C′:

| Type | Definition | Why it may differ |
|---|---|---|
| `dir_flip` | direction changes | New trend label; follow-through vs stop-run |
| `vol_expand` | vol tier rises (low→…→high) | Breakout / risk-on shock |
| `vol_contract` | vol tier falls | Compression; fade or wait |
| `both` | dir + vol | Rare, large; study separately |

**H3:** `dir_flip` and `vol_expand` have different accuracy-excess profiles in `fresh`. Pooling all transitions (what age did) destroys this.

### 2.3 Transition surprise (rarity)

`surprise = −log P(C′ | C)` from the HMM `transmat_` (or empirical 30d transition counts). High-surprise flips should carry more information than “back to the modal neighbour”.

**H4:** accuracy excess in `fresh` rises with surprise (controlling for type).

### 2.4 Cross-sectional agreement (cluster transitions)

Gold complex / US indices / EUR bloc move together (~5.3 independent bets).

`agree` = share of cluster-mates that flipped to the same direction within ±2h.

**H5:** `fresh ∧ high-agree` beats `fresh ∧ idiosyncratic` (macro/USD-driven vs noise).

### 2.5 What we will *not* do

- No quadrant plane, no flow field, no per-cell fitted knobs.
- No HSMM (memoryless on 1h).
- No SL/TP in the transition *definition* (geometry is the other track).
- No re-open of monotone age-slope as a primary.

---

## 3. Getting `p_win` to rank

`p_win` is a **logit pool** of `p_historical` / `p_regime` / `p_markov_hold_n` / `p_timesfm` (EV engine v5/v6), then optional EOD correction. It is a *level* forecast of P(win the bracket). It does not rank today.

### 3.1 Target (frozen)

Primary ranking target on decided signals (TP or SL):

```text
y_acc = 1{TP first} − SL/(TP + SL)     # accuracy vs no-skill  (scorecard D/I)
y_bin = 1{TP first}                     # raw hit, reported alongside
```

Never rank on raw TP rate across symbols/geometries. Never use expired in y.

Secondary: `y_dec = 1{decided before expiry}` if we later model two-part outcomes.

### 3.2 Score inventory (what we can rank *today*)

| Feature | Source | Role |
|---|---|---|
| `p_win` | blend | Current score — expect weak IC |
| `p_win_dir` | direction-blend | Possibly better for `y_bin` |
| `p_win_bracket` | bracket-aware | Closer to settle |
| `predicted_p_win_raw` | pre-correction | Separates blend vs correction |
| `p_resolve` / `p_win_resolve` | resolve model | Timing of TP/SL vs expiry |
| `regime_conf`, `raw_p_regime` | HMM | Regime confidence |
| `efficiency_24h`, `clv_1h`, `rel_volume_1h`, `atr_ratio_run` | Kairos stamps | Path quality / activity |
| `tp_distance_atr`, `sl_distance_atr`, `geometry_risk_hat` | geometry | Path reachability |
| **recent same-dir TP rate (3h)** | derived | Prior lead (AUC ~0.62) |
| **τ event-time + transition type + surprise + agree** | §2 | Regime-transition features |

### 3.3 Ranking protocol (pre-registered)

1. **Within-cell Spearman** of each feature vs `y_acc` (cell = symbol × regime cell; drop cells with &lt; 30 decided).
2. **Pooled AUC** of feature vs `y_bin` **inside symbol** (demean / stratify) — do not use cross-symbol pooled AUC (already withdrawn as mix artefact).
3. **Incremental:** does feature beat `p_win` and a **cell mean shrink** baseline (`k=10` toward symbol mean)?
4. **Walk-forward recalibration:** isotonic or Platt fit on trailing 14d → score S2 on next day; ΔBrier vs uncorrected and vs cell-shrink.
5. **Stop:** a feature is kept for gates only if (a) within-cell IC CI excludes 0 **or** (b) walk-forward ΔBrier ≤ −0.02 vs cell-shrink, in **both** era halves (post-ATR split).

### 3.4 If nothing ranks (expected fallback)

Replace `p_win` *as a ranking score* with a **calibrated cell rate**:

```text
p̂ = shrink( empirical P(TP|decided, cell, geometry_bucket), n_cell, k )
```

Gates then select on `p̂ − no_skill` (claimed edge) and risk (kelly/ev), not on an uninformative blend. The blend can still feed EV *level* for sizing once calibrated.

### 3.5 Two-stage (only if 3.4 is not enough)

1. P(decide | features) — expiry vs touch (dynamic ATR already drives expiry to ~0; less critical).  
2. P(TP | decide, features).  

Product p_win = p_decide × p_tp … only if stage 1 has real variation under live geometry.

---

## 4. Getting gates to select

Gates currently threshold **unranked** scores (`p_win ≥ 0.55`, `regime_conf ≥ 0.60`, kelly, ev). G2.2 failed on **m₂₄**.

### 4.1 Redefine “select” for the geometry era

| Metric | Use |
|---|---|
| **Primary selection metric** | **Scorecard D**: mean accuracy vs no-skill of gated vs rest, under *live* geometry (dynamic ATR) |
| Secondary | Δm₂₄ demeaned (continuity with G2.2) |
| Kill | Any gate change justified only by raw TP rate |

### 4.2 Gate programme (after §3 produces a score)

1. **Baseline:** current 7-gate vs all episodes → ΔD (accuracy excess). If ΔD ≤ 0 at power, gates are not a quality layer — they are a *publish filter*. Accept that and use risk/cooldown for risk, not “quality”.
2. **Replace** `p_win` threshold with the new rank score’s quantile or `p̂ − no_skill ≥ ε`.
3. **Regime_conf / kelly / ev:** re-test one at a time on ΔD, Holm, post-ATR era. Drop any that does not lift D.
4. **G2.2 rerun on D** (not m₂₄) once ≥ 200 post-resolver decided qualified vs rest.

### 4.3 What “directional edge” actually requires

In order:

1. **Geometry that can pay** — dynamic ATR (done / promoting).  
2. **A ranking score** that separates winners from losers *within* cell/geometry (§3).  
3. **Gates that pick the top of that ranking** (§4.2) → positive D.  
4. **Only then** regime-transition conditioning (§2) as an overlay: weight size/entry by `fresh`×type×surprise, **if** T1–T5 pass.

Without (2), transitions can describe the market and still not create product edge.

---

## 5. Build (this change)

| Artefact | What |
|---|---|
| `kairos_rank.py` | Pure: `transition_type`, `event_window`, `surprise_from_counts`, `rank_ic`, `auc_within`, `shrink_cell_mean`, `recent_tp_rate`, `cluster_agree` |
| `scripts/kairos_rank_transition.py` | Live read-only: event-time accuracy/m_h, type split, ranking IC table vs `y_acc`, walk-forward cell-shrink vs p_win |
| `tests/test_kairos_rank.py` | Unit tests for the pure maths |
| Report | `worklog/kairos/reports/20260926_rank_transition.md` |

Capture unchanged. No gate/flag flips in this change.

---

## 6. Stage gates (new, small)

| Gate | Pass |
|---|---|
| R1 | Event-time tool runs; T1 contrast reported with nulls on live data |
| R2 | ≥ 1 feature with within-cell IC ≠ 0 **or** ΔBrier ≤ −0.02 vs cell-shrink (both halves) |
| R3 | Gate change on **ΔD** ≥ 0 with power; else gates stay publish-only |
| R4 | If T1 passes, transition-weighted size **shadow** only (no live knob) |

---

## 8. R1 live result (2026-09-26, `reports/20260926_rank_transition.md`)

Discovery window mixed-era (mostly pre-debounce). Transitions parsed from HMM labels via `regime_cell` (10,143 cell changes). Decided n = 36,357.

### 8.1 T1 — **supported** (event-time information decay)

| window (τ since cell change) | n decided | mean y_acc | mean y_bin |
|---|---:|---:|---:|
| fresh 0–3h | 24,768 | **−0.037** | 0.293 |
| warm 3–12h | 2,807 | −0.052 | 0.273 |
| stale 12–24h | 2,397 | −0.085 | 0.233 |
| deep ≥24h | 5,188 | **−0.154** | 0.168 |

**fresh − deep = +0.117 [+0.105, +0.129]** — not a knife-edge.

This is the user’s transition thesis, measured correctly: **the regime label is more informative immediately after it flips**, and that information decays in event time. V2 failed because it fitted a *monotone age slope* on rolling R and mixed transition types; the plane failed as a state machine. Freshness works.

Note: accuracy is still ≤ 0 in every window (below no-skill). Freshness **attenuates the deficit**; it is not yet positive edge. Under dynamic ATR, re-check D on post-flip data.

### 8.2 H3 — type matters

| type | n | y_acc | y_bin |
|---|---:|---:|---:|
| **dir_flip** | 1,571 | **+0.007** | 0.339 |
| both | 829 | −0.014 | 0.316 |
| vol_contract | 1,445 | −0.047 | 0.284 |
| vol_expand | 1,473 | −0.052 | 0.278 |

**Direction flips are the only non-negative type.** Vol-only transitions are worse than average. Do not pool types.

### 8.3 H4 — surprise: no

Spearman(surprise, y_acc) = **0.004** on 5,318. Rarity of the HMM transition does not rank accuracy. Drop surprise as a gate feature; keep type + τ.

### 8.4 Ranking inventory — **p_win does not rank** (confirmed)

| feature | within-cell IC | AUC(y_bin) |
|---|---:|---:|
| `p_win` | −0.042 | 0.494 |
| `p_win_dir` | −0.087 | 0.516 |
| `p_win_bracket` | −0.071 | 0.438 |
| `predicted_p_win_raw` | −0.033 | 0.499 |
| `p_resolve` | −0.055 | 0.517 |
| `regime_conf` | +0.019 | 0.447 |

Kairos factor stamps too sparse (n=295) to read. Cell-shrink Brier ≈ p_win Brier (0.214); the Brier gap is not there — the **level** is the problem and **IC is ~0**.

### 8.5 Immediate product ranking features (next)

Priority order for a real score (worklog §3.2 / §3.4):

1. **τ event window** (`fresh`/`warm` vs `deep`) — proven contrast  
2. **transition type** (`dir_flip` best)  
3. **recent same-direction TP rate (3h)** — prior AUC lead  
4. **cell-shrink calibrated rate** as the *level* (replace raw p_win)  
5. Rebuild `p_win` only from components that show IC after (1)–(4)

Gates: start measuring selection as **ΔD (accuracy vs no-skill)**, and treat `fresh ∧ dir_flip` as a shadow size/entry overlay (R4), not a live gate yet.

---

## 9. Composite rank score — walk-forward (2026-09-26)

Build: `rank_score()` in `kairos_rank.py` (weights frozen before scoring):
`0.35·freshness + 0.25·type + 0.25·recent_tp_rate(3h) + 0.15·cell_p` (shrunk cell hit-rate).
Unit: **episode** (symbol × direction × hour), not raw light rows. Eval: `scripts/kairos_rank_score_eval.py` → `reports/20260926_rank_score_eval.md`.

### 9.1 Results (episode-deduped, walk-forward, no lookahead)

| block | n | rank IC(y_acc) | AUC(y_bin) | ΔD @P60 gate | ΔD CI |
|---|---:|---:|---:|---:|---|
| All eras | 6,300 | **0.126** | 0.67 | **+0.233** | [+0.212, +0.256] |
| Post-debounce | 518 | **0.199** | 0.785 | **+0.295** | [+0.234, +0.359] |
| **Post-ATR** | **43** | 0.183 | 0.671 | +0.113 | **[−0.096, +0.356]** |
| Qualified only | 89 | −0.049 | 0.557 | +0.028 | [−0.102, +0.190] |

Contrast: `p_win` IC ≤ 0 everywhere; top-vs-bot tercile of p_win is flat or inverted.

### 9.2 Reading (quant-honest)

1. **R2 discovery: PASS** on broader samples — composite **ranks** `y_acc` / `y_bin` where `p_win` does not. Gated arms are **above no-skill** in all-eras (y_acc +0.10) and post-debounce (+0.04).
2. **R3 post-ATR: NOT YET** — n=43 episodes, ΔD CI spans 0. Do **not** flip a live gate on this.
3. **Qualified-only does not separate** (ΔD +0.03). The current 7-gate pass set is not aligned with this score; consistent with G2.2.
4. Raw-row (no dedupe) AUC ~0.86 was **overlapping-hour inflation**. Episode unit is the only one we report.
5. Much of the lift is **`cell_p` persistence** (trailing hit-rate) + freshness. That is real but path-overlapping — keep symbol-day cluster CIs in any promote memo.

### 9.3 Shadow gate (built, not live)

- `shadow_entry_gate(score, thr)` + `delta_d` / `delta_d_bootstrap` in `kairos_rank.py`.
- Rule shape: publish / size-up when `rank_score` ≥ trailing P60 (threshold recomputed on the trailing score history).
- **Status:** shadow only (R4). Next bar: ≥200 post-ATR episodes with ΔD CI > 0 on the same rule, then a `kairos_rank_score` stamp on `nt_signal_sim` (additive column) and one lever at a time vs the 7 gates.

---

## 10. Shadow stamp build (2026-09-26)

| Piece | What |
|---|---|
| Migration **172** | `nt_signal_sim.kairos_rank_score` (double), `kairos_rank_gate_shadow` (boolean) — additive, nullable |
| `kairos_rank.RankScoreTracker` | Trailing decided outcomes + cell hit-rates + event-time (τ, type). `score()` uses prior state only; `to_dict`/`from_dict` restart-safe |
| `kairos_rank.ingest_decided_rows` | Feeds TP/SL rows (kairos-daily) |
| Orchestrator | `note_regime` on every light tick; `_kairos_rank_stamp` fail-open at capture; never read by gates |
| `supabase_client` | Allowlist + `fetch_recent_decided_signals` for daily ingest |
| `regime_state_store` | Persists `_kairos_rank` with debouncers/run tracker |

Stamp is written only when `kairos_mode != off` and the tracker has trailing evidence (no invented priors). Schema-lag retries drop these columns like other Kairos stamps. **Still shadow:** `kairos_rank_gate_shadow` is a would-publish bit, not a gate.

---

## 7. Decision summary

1. **Keep the transition thesis**, drop *duration* as the state variable. Study **events**: fresh vs deep, typed flips, surprise, cluster agreement.  
2. **Fix ranking first** (`y_acc` vs no-skill, within-cell IC). That is how `p_win` becomes a score.  
3. **Gates select on D (vs no-skill under live geometry)**, not on m₂₄ or raw TP.  
4. **Dynamic ATR** stays the economics engine; this programme feeds the feedback loop around it.  
5. If ranking fails, ship calibrated cell rates and stop blending four weak probabilities.
