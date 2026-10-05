# Clef-Flash Repurpose — Build Worklog

**Date:** 2026-10-05
**Status:** plan — build not started. Supersedes the ad-hoc framing; incorporates
`docs/scopes/20261005_CLEF_EV_CHAIN_DECISION_LAYER.md` §2/§9.
**Excludes:** reachability (a feasibility question — answered "no", §9.3 of the
scope doc; recorded here in §9 so it is not re-opened).

---

## 1. What is being repurposed, and why the current usage is wrong

Two Clef-Flash usages exist today, and **both ask it to re-derive something the
pipeline already computed**:

| Usage | What it does | Verdict |
|---|---|---|
| `p_clef_dir` shadow stamp (shipped, `clef_flash_mode: shadow`) | Re-states the signal's own direction as a probability | Measured at chance — retired as a research line |
| `clef_flash_mode: live` cutover (`kairos_pivot`, `macro_forecast`) | Re-states a label the chat LLM already produced | Reliability swap only — not a value question |

### 1.1 The measurement that closes the re-derivation line

Episode-deduped, n=85 decided (2026-10-05, live):

| Predictor | AUC (→ `tp_hit`) |
|---|---|
| `p_clef_dir` | **0.506** (chance) |
| `p_win_dir` / `p_win_bracket` | 0.665 |
| base rate | 0.165 |

`p_clef_wait` averages **0.52** — abstains on over half of signals. Below the
≥200-row bar in `20261004_P_CLEF_SHADOW_STAMP_REQUIREMENTS.md` §6, so directional
not conclusive — but exactly where `20261004_LLM_VALUE_AUDIT…` §1 predicted a
re-derivation would land. This is the **control condition** for everything below.

### 1.2 The one thing the EV chain cannot produce

`compute_ev()` is a **fixed-weight logit pool**:

```python
P_WIN_WEIGHTS_FULL = { "p_regime": 5/14, "p_markov_hold_n": 4/14, "p_timesfm": 5/14 }
```

With equal weights, `p_regime=0.9, p_timesfm=0.2` and `p_regime=0.55,
p_timesfm=0.55` pool to the **same** `p_win`. The pool has **no read on
disagreement** — the information is discarded on the way in. That is a judgment
*about* the chain's own inputs, not a number the chain has, and it is the only
non-redundant question available.

**The test for legitimacy is not "does Clef read price data" (it must not) but
"is the answer already in the pipeline?"** For direction: yes. For coherence: no.

## 2. Live worker contract — VERIFIED 2026-10-05

Probed against the deployed Worker (`https://nobletradingapp.lexington.workers.dev`).
This is the contract the build must target, and one part of the proposed design
**does not work as written**.

| Question type | Result | Notes |
|---|---|---|
| `choice` | ✅ 200 | Returns `{choice, probabilities{...}, confidence}` |
| `noul` | ✅ 200 | Returns `{noul: 0.8284}` — a single P(true) |
| `score` | ❌ **500** | Worker threw an exception. **Do not use.** |
| multi-question (one call) | ✅ 200 | 3 questions, `choice` + `noul`, 437 input tokens |

**Consequences for the design:**

- **Q2 (geometry fit) must be re-expressed as a `choice`**, not a `score`. The
  scope doc's `score 0–3` is not available. `choice{good, marginal, poor}` works
  and is strictly better for a modifier (a probability distribution, not a point).
- The scope doc's other `score` usages (in the retired classifier path) are
  equally unavailable — that path must not be built as written.
- `confidence` is returned on `choice` answers and is a usable second signal
  (observed 0.0808 on a 4-way choice — low confidence is itself informative).

## 3. The state — what is ACTUALLY available at capture time

The scope doc's state block (§3) lists `sources: {p_regime, p_timesfm,
p_markov_hold_n}`. Two corrections, verified against `_process_symbol` and the
live table:

1. **`p_markov_hold_n` is not live.** `p_markov_hold_n_mode: "off"` (2026-10-01)
   means the sweep passes no transition matrix; the engine falls back to a
   neutral 0.5. **The live blend is two sources: `p_regime` + `p_timesfm`,
   renormalised to 0.5/0.5.** Coherence over "the sources" is coherence over two.
   Asking Clef to weigh a source that is pinned at 0.5 would be asking it to read
   a constant.
2. **`p_timesfm` is not a stored column** on `nt_signal_sim`. It is available
   **in-memory** at capture (`p_timesfm` in `_process_symbol`), so the live call
   can carry it — but it **cannot be reconstructed offline** from the table.
   `raw_p_regime` / `gated_p_regime` **are** stored.

**Requirement this creates:** the offline evaluation (D1) must be runnable from
stored columns alone. Therefore **D0 stamps the state's source values alongside
the answers** (§6) — otherwise the eval cannot audit or re-run what the model saw.

## 4. The questions (revised to the verified contract)

| # | Type | Question | Returns | Role |
|---|---|---|---|---|
| Q1 | `noul` | "Do the EV sources (regime and TimesFM) agree on this setup's direction and strength?" | P(agree) | **the coherence read — primary** |
| Q2 | `choice` | "How well does this bracket geometry fit this regime?" `{good, marginal, poor}` | probabilities | geometry-fit modifier |
| Q3 | `choice` | "What is the dominant risk?" `{source_conflict, geometry_mismatch, regime_fresh, none}` | probabilities | risk tag / routing |
| Q4 | `noul` | "Would a disciplined trader act on this signal as-is, or stand aside?" | P(true) | abstain read |

All four in **one call** (verified: 3 questions = 437 input tokens). Q1 is the
primary hypothesis; Q2–Q4 are the supporting set, consumed by a rule in code.

**Input discipline (the §1 rule, enforced at the payload level):** the `state`
carries the signal's **already-computed** context only — no candle array, no renko
series. Clef is never asked to re-derive anything the deterministic engine produced.

## 5. The return, and what it is FOR

The answers do **not** replace `p_win`. They feed a **modifier**, exactly like the
existing calibration/rank machinery:

```
coherence     = answers.Q1.noul
geometry_fit  = answers.Q2.probabilities.good
stand_aside   = answers.Q4.noul

# candidate modifier (shadow first):
p_win_adjusted = p_win * f(coherence, geometry_fit)
```

**Hypothesis:** low-coherence rows underperform their `p_win` — i.e. coherence
discriminates outcomes `p_win_dir` fails to. If true, it is a calibration input
the fixed-weight pool structurally cannot compute. If false, the thread retires.

## 6. Stamp contract (D0)

New nullable columns on `nt_signal_sim` (research-only, same convention as
`p_clef_dir` / `raw_p_regime` / `timesfm_q_*`):

| Column | Type | Meaning |
|---|---|---|
| `p_clef_coherence` | `double precision` | Q1 `noul` — the primary eval column |
| `p_clef_geometry_fit` | `double precision` | Q2 P(`good`) |
| `p_clef_stand_aside` | `double precision` | Q4 `noul` |
| `p_clef_risk` | `text` | Q3 arg-max label |
| `clef_state_p_regime` | `double precision` | **the state's own source values** — required so D1 is auditable/re-runnable without `p_timesfm` being stored |
| `clef_state_p_timesfm` | `double precision` | idem |

**Never read back into any decision.** Not in `P_WIN_WEIGHTS_FULL`, never passed
to `compute_ev()`, never touches `qualified` / `published` / `gate_outcome` /
Kelly / SL / TP. Nullable, no default; NULL when `off`, non-light-sweep, or on any
failure.

**Flag:** reuse `clef_flash_mode` (`off | shadow | live`). The new stamp is
shadow-only in all three values — `live` remains reserved for the classifier
cutover, which is a **separate** decision and is not this build.

## 7. Stages and gates

| Stage | What | Gate to proceed |
|---|---|---|
| **D0** | New caller method `decide_state(state, questions)` + the 6-column stamp, wired at the existing `_clef_flash_stamp` call site (light sweep, qualifiable) | — |
| **D1** | ≥200 decided, episode-deduped rows. Split by **coherence tertile**; compare realized TP rate | does low-coherence underperform? |
| **D2** | If yes → shadow modifier `p_win_adjusted`; measure Brier / ΔD vs raw `p_win` | does it improve calibration? |
| **D3** | **PROMOTE** (modifier live) or **RETIRE** (`clef_flash_mode: off`) | — |

**Endpoint is binary.** Either coherence is a live calibration modifier with
measured Brier improvement, or the Clef thread is retired and the Worker is kept
for embeddings only. No limbo, no stage that spawns another stage.

## 8. Test-first plan (before any implementation)

Adopted after the `geometry_band_urgency` defect cycle — six defects, four
introduced in-build, **all passing a green suite**
(`worklog/20261005_GEOMETRY_BAND_URGENCY_DEFECTS_AND_BUILD.md`).

1. **Drive the real entry point** (`_clef_flash_stamp` → `decide_state`) with the
   fake at the **transport boundary** (`httpx.AsyncClient`). Never patch the
   module's own helpers — patching the loader is exactly how the cooldown defect
   hid from its own unit test.
2. **Assert the observable contract**: the request body the fake captures (the
   exact `state` + `questions` sent) and the values that would land on
   `nt_signal_sim`. Not internal call counts.
3. **Cover the verified contract explicitly**: `noul` and `choice` parse;
   a `score`-type answer is rejected/handled; a 500 returns all-None (fail-open);
   multi-question answers are keyed correctly.
4. **Verify each test fails on pre-fix code** (revert the line, watch it fail,
   restore).
5. **Fixture-integrity guard** so a test cannot pass on a degenerate fixture.

## 9. Excluded — reachability (recorded so it is not re-opened)

The question was: can Clef decision-making help **reachability**?

**No.** The reachability table's structural gap is the "remaining regime clock" —
a bucket's `pct_expired` is geometry-only and ignores how much regime life is left
(`worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md` §E). That covariate is
**numeric and already computable**: Kaplan-Meier survival of the current regime is
deterministic arithmetic over the stored series. Asking a model to judge it is the
rejected pattern one level down — a model performing an in-process numpy
calculation on data we already hold.

**Action instead:** compute the remaining-regime clock deterministically and join
it into reachability. This was a feasibility question, now answered; it is not
part of this build.

## 10. Open risks (stated, not hidden)

1. **Coherence may not discriminate.** The HMM and first-passage both measured
   below the base rate; a model reading a state that *contains* those
   sub-base-rate sources may inherit the ceiling. **This is the primary risk, and
   D1 is what settles it.**
2. **Conflict rate is UNMEASURED.** Q1 has nothing to read if `p_regime` and
   `p_timesfm` rarely disagree. This could not be measured from stored data
   (`p_timesfm` is not a column — §3), which is precisely why D0 stamps the
   state's source values. **Measure the conflict rate in D0 before committing to
   D1's sample size.**
3. **Two sources, not three.** With `p_markov_hold_n` off, Q1 reads a 2-source
   disagreement. Fewer sources ⇒ fewer conflict opportunities ⇒ risk 2 is worse.
4. **Cost.** One call per qualifiable signal (~24/day at light-sweep cadence,
   437 tokens for 3 questions) — negligible.
5. **`p_timesfm` in the whitelist but not the table.** `NT_SIGNAL_SIM_COLUMNS`
   contains `p_timesfm`; the live table does not. Latent inconsistency, unrelated
   to this build but worth a look before relying on that column name.

## 11. Artefacts (planned)

| Artefact | Path |
|---|---|
| Caller method | `regime_platform/services/decision/clef_flash.py::decide_state` |
| Question builders | same module (`build_coherence_state`, `build_coherence_questions`) |
| Stamp | `orchestrator.py::_clef_flash_stamp` (extend) + `_build_signal_sim_payload` params |
| Migration | `supabase/migrations/NNN_nt_signal_sim_p_clef_coherence.sql` (6 columns) |
| Schema whitelist | `supabase_client.py::NT_SIGNAL_SIM_COLUMNS` (+6) |
| Tests | `tests/test_clef_coherence.py` (written **first**) |
