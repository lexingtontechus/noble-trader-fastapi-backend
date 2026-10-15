# PBM drift-layer AUC — first-run assessment and the above-null harness fix

**Date:** 2026-10-15
**Type:** Measurement assessment (first run of `scripts/measure_pbm_drift_auc.py`) + one harness correctness fix + report cosmetics. No flags, no tables, no live wiring — **μ stays pinned to 0.**
**Run assessed:** `worklog/kairos/reports/20261014_pbm_drift_auc.md` — generated 2026-10-14 23:12 UTC on the operator machine; the uploaded copy is archived in-repo (the repo's `reports/` had stopped at 20261013).
**Base:** HEAD `4953e96` + the PBM working tree (Phases A–E, `pbm_mode: "shadow"`; migrations 210/211 live).
**Companions:** `20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md` §5 Phase E (the pre-registration this run executes), `20261014_TP_FENCE_COSTS_AND_ENTRY_GAP_SEARCH.md` (the fence + honest cost table the assessed brackets now run under).

---

## 0. What changed

| # | Change | Where | Live effect |
|---|---|---|---|
| 1 | `verdict_for()` now enforces the pre-registered **third clause**: a drift candidate's WF AUC must be *above the null's own WF AUC*, not merely > 0.5 with CI lower bound > 0.5 | `scripts/measure_pbm_drift_auc.py:344-374`, caller `:449` | none — offline harness |
| 2 | The null scorer's verdict renders **BASELINE** (was a skim-read-hazard **PROMOTE**; the null is the reference, never a candidate) | `verdict_for` + `render_report` | none |
| 3 | Per-symbol table footnote: "—" = AUC undefined (coverage gap or single-class WF half); rows with n WF < 30 are noise-grade | `:622-626` | none |
| 4 | Module docstring (`:9-16`) and the report's promotion-bar prose (`:536-548`) now state all three clauses | same file | none |
| 5 | First-run report archived in-repo | `worklog/kairos/reports/20261014_pbm_drift_auc.md` | none |

**No verdict changed on this run's data.** All three candidates already failed the 0.5 / CI bar, so the added clause only appends reasons; the null's PROMOTE→BASELINE is display-only. The fix binds **future** runs to the registration (§5).

---

## 1. The run in one line — and what was checked

Era-clean (marker `nt_calibration_cutover` 2026-10-02T00:00:00Z read, **0** rows dropped, 1623/1623), window ~2 days since 2026-10-13, **1623 decided (459 tp / 1164 sl), 0 expired**, seed 13, fixed scorers. **Every directional candidate STAY-OFF — the pre-registered expected outcome; the stable resting state, not a failure.** Nothing to wire, nothing to flip.

Validity checks performed on the report before trusting it (all passed):

- **Arithmetic self-consistency:** 459 + 1164 = 1623; qualified 423 + 1200 = 1623; pooled WR 459/1623 = 0.2828 exactly as the pooled line reports.
- **Walk-forward split arithmetic** re-derived for all 14 symbols (137→69, 125→63, 133→67, 41→21, …); the per-symbol WF counts **sum to exactly 815 = the null's WF n**. The 804/791 counts of the other scorers are coverage gaps (~97% coverage; 11 rows without `p_win_dir`, ~24 without LLM stamps), not split leakage.
- **Bootstrap machinery sane:** the null's CI [0.622, 0.700] matches the Hanley–McNeil analytic SE (~±0.04) at n₁≈230 / n₀≈585 — the percentile bootstrap is behaving.
- **Determinism:** fixed scorers + seed 13 → re-running on the same rows reproduces the report byte-for-byte.
- **Cadence consistency:** ~855 resolved rows/day matches the sim table's fill rate; read-only, no flags touched.

---

## 2. The four scorers

| scorer | WF AUC | 95% CI (WF) | Δ vs null | read |
|---|---:|---|---:|---|
| `p_win_dir` | 0.521 | [0.469, 0.574] | −0.145 | chance — the 74k-era 0.499 now confirmed **era-clean**; 0.521 vs 0.507 all-era is split noise, and the CI says so. Fails **only** the CI clause — the harder half of the bar doing its job. |
| `p_clef_dir` | 0.343 | [0.304, 0.394] | −0.323 | **inverted** this window (0.58 = chance on 74k). Sign instability across windows is the signature of noise; the bar is one-sided by design — not a suppressor to flip. |
| `null_p_win` | 0.666 | [0.622, 0.700] | — | the geometry baseline — §3. |
| `p_clef_wait` | 0.330 | [0.291, 0.381] | −0.336 | inverted against its published 0.61 claim. Per-symbol it tracks `p_clef_dir` within ±0.03 everywhere — **the two LLM stamps are one candidate measured twice**, not two independent chances. |

---

## 3. Finding 1 — the null's 0.666 is *between-cell* separation, not *within-cell* skill

The pooled 0.666 would read as "geometry has skill." The per-symbol table says otherwise: the null's per-symbol AUCs are mostly **below 0.5** (median ≈ 0.40 across the 12 scorable symbols; range 0.000–1.000). What actually happened:

- The null **priced the extreme cells correctly**: BTCUSD/sell (null p_win 0.146) → 0 wins in 92 rows; XAUEUR/buy (null 0.195) → 0 wins in 96 rows; mid-null FX buys delivered 78–80% (AUDUSD null 0.381 → 0.782; GBPUSD null 0.372 → 0.800). Cross-cell ordering tracked realized outcomes.
- Within a symbol the null is **side-blind** — brackets are side-symmetric. XAUAUD is the cleanest illustration: null 0.314 (sell) vs 0.316 (buy), realized **0.757 vs 0.056**. Only a drift term μ could rank side-driven outcomes — and that is exactly the layer Phase E measured at zero.

**Correct interpretation: geometry orders cells; nothing orders within cells.** That is precisely the job the PBM needs from the analytic layer (cell pricing for the no-trade/EV gate), and precisely the confirmation that the entry layer contributes no timing information — the review thesis, now measured era-clean at the current brackets. It is *not* evidence of "geometry has edge": AUC ranks outcomes *given* brackets; the EV question is unchanged, and μ=0 + honest costs still prices most cells below the bound.

---

## 4. Finding 2 — pooled selection gap −0.0154: near parity, with two caveats

Realized WR 0.2828 vs null p_win 0.2982 over n=1623 (SL rate 71.7% vs ~70.2% null-implied). Near parity — dramatically different from the review-era motivating numbers (13–15% vs ~37.5% first-touch; SL 89.5%). Two caveats before anyone quotes "entry timing is fixed":

1. **The brackets are not the bands those numbers were measured on.** Null mean 0.298 ⇒ TP ≈ 2.3× SL here, vs 1.57/1.05 ⇒ 0.40 in the review era. The thermometer moved partly because the thermometer's scale moved.
2. **One ~2-day window.** Not a re-measurement of the ~30pt entry-timing damage estimate; it is a first reading on the stabilized regime (fence live, honest costs live).

Per-cell dispersion is violent (−0.31 … +0.44) with a clear **buy/sell anti-symmetry**: XAUAUD sell +0.44 / buy −0.26; AUDUSD buy +0.40 / sell −0.24; GBPUSD buy +0.43 / sell −0.18; XAGUSD sell +0.15 / buy −0.24. That is the window's own trend projected onto each cell's brackets — what a μ=0 world looks like locally. These are **not** stable per-cell properties; do not condition on them.

`expired = 0` is a consistency check, not a mystery: at σ = 1 ATR/bar, σ√48 ≈ 6.9 ATR ≫ bracket distances, so absorption before T is near-certain. Side effect: the Dirichlet "expired full-outcome" branch saw zero mass this window — it remains correct to include, unexercised.

---

## 5. The spec-vs-code gap, fixed

**The gap.** The pre-registration (phases doc §5 Phase E; the harness's own docstring and the report's own prose: *"cannot beat the null's own walk-forward AUC … μ stays pinned to 0"*) requires candidates to beat the null. The mechanical verdict in `verdict_for()` checked only `auc_wf > 0.5` and CI lower bound > 0.5 — `delta_null` was computed and reported but **never enforced**.

**The failure mode it closed.** A future candidate at WF AUC 0.60 [0.55, 0.65] under a 0.666 null would have mechanically **PROMOTED** while sitting 6.6pp *below* the geometry baseline — violating the registration and wiring a drift term with negative incremental timing information. (This exact case is now a regression test, §6.)

**The fix.** `verdict_for(stat, null_wf)` — three clauses for a drift candidate (WF AUC > 0.5; CI lower bound > 0.5; **above the null's WF AUC**), the null exempt (it *is* the baseline) and rendered **BASELINE** in every branch. `null_wf = None` (null itself unscoreable — cannot happen with parseable geometry) leaves the third clause vacuous and is reported via the null's own no-rows verdict. The caller passes the already-computed `null_wf` (`:449`); nothing else in the pipeline moved.

**This run's verdicts replayed through the fixed bar:** `p_win_dir` STAY-OFF (CI clause only — unchanged); `p_clef_dir` and `p_clef_wait` STAY-OFF (all three clauses now listed); null BASELINE (display). No flips.

---

## 6. Verification (this fix, offline — the harness's own standard)

- `python3 -m py_compile` OK; `ruff check` clean (line-length 120).
- **9 `verdict_for` branch cases**, including the regression case above (0.60 [0.55, 0.65] vs null 0.666 → STAY-OFF "above the null"), the first-run numbers replayed (all three candidates STAY-OFF, reasons as listed in §5), null clearing/failing/unscoreable → BASELINE in every branch, and the vacuous-clause case.
- **Synthetic end-to-end** (240 prepared rows through `prepare_rows → measure → render_report`): the report renders the three-clause bar, the BASELINE label, and the new footnotes; no PROMOTE banner in a null world; the era fail-open banner still shouts when the marker is missing.
- **Determinism:** byte-identical re-render on identical inputs.
- **Boundary guard unchanged:** the no-creds `SystemExit` message in `main()` is intact.

---

## 7. Recommendations (the actionable residue)

1. **No action on μ.** Resting state; nothing to wire, nothing to flip. Re-run the harness weekly (same seed → comparable) as post-cutover data accrues; keep `--since 2026-10-13` fixed so the bracket regime stays consistent while n grows.
2. **Above-null fix — DONE** in this change set (§5); future runs are bound to the registration.
3. **Probe BTCUSD/sell (0/92) and AAPL/buy (0/20) as an entry-layer/data pathology investigation.** All-loss cells are the sharpest single data point in the run, and they are timing/cell evidence, not drift evidence.
4. **Track the LLM stamp inversion for 2+ more windows** before saying anything more about it. If it ever persists, pre-register a flipped-suppressor test separately — do not act on one window.
5. **Re-measure the entry-timing damage estimate deliberately** on the stabilized bracket regime (fence + honest costs live). Pooled near-parity here vs the review-era catastrophic numbers is the biggest open question this run raised — and it feeds directly into the entry-gap search harness (`scripts/measure_entry_timing_gap.py`) already delivered.

---

## 8. Power (what this run could and could not decide)

At WF n ≈ 800 with ~28% positives, clearing the CI bar effectively demands WF AUC ≳ 0.56. So this run could neither promote a modest real edge (0.52–0.55) nor treat that as proven absent — the deliberately-hard-bar trade-off, accepted at pre-registration. What it *did* conclusively establish this window: nothing directional is anywhere near the bar, and the LLM stamps failed even in sign.

---

## 9. Rollback / honest boundaries

- **Rollback:** revert `scripts/measure_pbm_drift_auc.py` to the delivered version (the fix is ~30 lines in one function + prose; `git diff` in the working tree). Nothing else consumed the changed signature — `verdict_for`'s only caller is `measure()` (`:449`); `scripts/measure_entry_timing_gap.py` has its own independent `verdict_for`.
- **The bootstrap is row-level** (overlapping 15-min same-symbol signals correlate rows → CIs optimistic). The bar demands the CI lower bound clear 0.5 regardless; no episode dedupe, matching the platform's own 74k-row directional measurement.
- **The null is a model** (no jumps/gaps, constant σ). The empirical posterior layer exists to absorb its systematic deviation; the intrabar resolver keeps pricing gap-through-SL risk.
- **The selection gap is in-sample by construction** — a thermometer of what the entry layer delivered vs the driftless price of the same brackets, not a forecast (§4's caveats).
- **Per-symbol extremes are artifacts until n says otherwise:** US500 1.000 (n_wf 21) and XAUEUR 0.000 (n=60) are single-class/regime artifacts; the new footnote marks the noise-grade rows, it does not censor them.

---

## 10. Files / lineage

- Harness: `scripts/measure_pbm_drift_auc.py` (delivered 2026-10-14; fixed this change set — §0/§5).
- First-run report (assessed): `worklog/kairos/reports/20261014_pbm_drift_auc.md` (operator-machine run, archived in-repo).
- Pre-registration: `worklog/20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md` §5 Phase E; proposal `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` §6 (kept with the review set).
- Context: `worklog/20261014_TP_FENCE_COSTS_AND_ENTRY_GAP_SEARCH.md` (fence §1, costs §3 — the regime this window's brackets ran under); `scripts/measure_entry_timing_gap.py` (the entry-layer twin of this gate).
