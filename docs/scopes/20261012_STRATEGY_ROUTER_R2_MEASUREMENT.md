# Strategy Router R2 — the measurement gate

**Date:** 2026-10-12
**Type:** Measurement scope (R2 of the R0–R6 sequence — theoretical design, no live code change)
**Status:** Draft for review. R2 is **not started**; this doc defines what it measures and the bar it must clear.
**Relevant:** `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md` (§6, R2), `config/strategy_registry.yaml` (R0), `regime_platform/sweep_orchestrator/strategy_router.py` (R1), `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE.md` (§B1, the MR cohort bar)

---

## 1. What R2 decides — and what it does not

R2 is the gate that decides **whether strategy routing helps at all**, before any archetype is trusted with a live flip (R6). It answers exactly one question:

> **Does the stamped archetype predict outcome better than the current uniform engine?**

It does **not** decide which archetype is best, whether the geometry is right, or whether to flip anything live. It produces a single verdict — *routing helps / routing is inert / routing is anti-predictive* — that either opens R3 (archetype-conditioned calibration) or sends the router back for rework.

R1 (shadow) made this measurable by stamping `strategy_archetype` on `nt_signal_sim` with zero live effect. R2 reads those stamps against realized outcomes. Nothing downstream of R2 may ship until R2 returns *helps*.

### The three sub-hypotheses

| # | Hypothesis | Null (routing inert) |
|---|---|---|
| **H1** | ROUTED signals (non-`stand_by`) outperform `stand_by` signals | routed ≈ stand_by |
| **H2** | Each archetype's WR/expectancy differs from the uniform-engine baseline | every archetype ≈ baseline |
| **H3** | `stand_by` signals are genuinely worse — the router's decline carries information | stand_by ≈ routed |

H1/H2 are the load-bearing tests. H3 is the "does declining everything beat trading everything" question, which matters because **today the router declines almost everything** (§2).

---

## 2. The sample reality (measured 2026-10-12)

R1 has been stamping since 2026-10-10. Current `nt_signal_sim` stamps:

| archetype | stamped | closed | symbols |
|---|---|---|---|
| `stand_by` | **923** | 765 | 14 |
| `momentum` | **4** | 4 | 1 |
| `mean_reversion` | **0** | 0 | — |
| **total** | **927** | 769 | 14 |

The original R1 bar — *"≥200 stamped episodes across ≥8 symbols"* — reads as **met** on raw count (927 across 14) but is **~2% met on the metric that matters**: only **4 routed episodes** exist. 99.6% of the book is `stand_by`.

**Root cause — the predicates are near-inert.** `momentum` requires `requires_aligned` and `mean_reversion` requires `requires_opposes`, both of which need BOTH `macro_label` AND `trend_label` to be *definite and in agreement*. In `strategy_router._alignment()` any neutral macro (`neutral`/None) or a `consolidating`/None trend returns `None` → no archetype matches → `stand_by`. Most signals have a neutral macro or a consolidating trend, so the router is functionally a **decline-all** engine.

**Consequence for the bar:** the bar must count **ROUTED (non-`stand_by`) episodes**, never total stamps. A `stand_by`-dominated sample measures nothing about routing — it measures the predicate strictness.

### Revised bar (proposed)

- **H1 / H2 are undecidable until** `≥ 200 ROUTED episodes across ≥ 8 symbols` exist, **with ≥ 40 in the smallest archetype being evaluated** (mirrors B1's `n ≥ 40` per-cell convention).
- **H3 (stand_by as suppress) may be assessed descriptively now**, but only ever as a secondary read: with `stand_by` at 99.6% of the book, "stand_by underperforms routed" is nearly the statement "the average signal underperforms the 4 routed ones" — it carries almost no routing information and must never be the basis for a live flip.

---

## 3. Test design

### 3.1 Cohort and era

- **Source:** `nt_signal_sim` where `strategy_archetype IS NOT NULL`.
- **Era:** post-cutover (`2026-10-02`, `eod_resolver_basis: intrabar`), `contaminated = false`.
- **Resolved only:** `outcome IN (tp_hit, sl_hit)` — `expired` is excluded from both numerator and denominator (a timeout is neither a win nor a loss; the platform's own convention).
- **Group key:** `strategy_archetype` (the R1 stamp).

### 3.2 Metrics per archetype

1. **WR** = `tp_hit / (tp_hit + sl_hit)`.
2. **Expectancy** (R, net of the platform's cost) — the primary metric; WR alone can be gamed by bracket geometry.
3. **Expired share** as its own trend, never folded into "loss" — a structural geometry fix shows up here before WR moves.
4. **Calibration bias** = `avg(p_win_dir) − WR`, per archetype (this is R3's input, computed here to size the R3 prize).
5. **Baseline** = the same metrics over ALL stamped rows (the uniform engine, since in shadow the engine trades everything).

### 3.3 Walk-forward

Chronological split at the **median resolved timestamp** of the routed cohort. Report **both halves** — a pick that only earns on the half it was observed on is overfit (same discipline as `tiered_atr.walk_forward_validate`). A degenerate split (one archetype entirely in one half — exactly what B1 hit on XAUUSD) is reported explicitly and **fails on sample, not on effect**.

### 3.4 Decision rules

| Condition (era-clean, walk-forward, bar met) | Verdict | Next |
|---|---|---|
| routed expectancy > stand_by in **both** halves | **ROUTING HELPS** | open R3 |
| routed ≈ stand_by (difference inside the bootstrap CI) | **ROUTING INERT** | do not open R3; revisit predicates/registry |
| routed < stand_by | **ANTI-PREDICTIVE** | retire or rework the router |
| bar not met (too few routed episodes) | **UNDECIDABLE** | accumulate, or re-scope the predicates |

**A null result is a finding.** If the predicates are so strict that R1 routes 0.4% of signals, the honest output is *UNDECIDABLE — the router is a decline-all*, not a manufactured pass. The B1 precedent applies directly: report `NO_DATA`/`UNDECIDABLE` distinctly from `FAIL`.

### 3.5 Statistical honesty

- Per-archetype and per-`(symbol, direction)` breakdowns are mandatory — the pooled number is a Simpson's-paradox trap (measured on the B1 cohort: commodities pooled 1.52× = XAUUSD 2.92× averaged against XAGUSD 0.65×).
- `nt_signal_sim` rows are not independent samples — dedupe to episodes before any edge statistic.
- Report n per cell; a thin cell fails open to "undecidable", never to a pass.

---

## 4. What R2 explicitly does NOT do

- **No live flip.** R2 is a read. R6 flips one archetype at a time, each behind its own flag, only after R2 returns *helps*.
- **No predicate loosening to manufacture a sample.** Widening the predicates to fit the current (consolidating) regime is the dynamic-ATR in-sample-overfit trap; a looser predicate is a *separate hypothesis with its own walk-forward*, not a fix to make R2 measurable.
- **No Clef-Flash.** R4 (Role A archetype classifier) is measured against R2's deterministic router — it cannot be evaluated until R2 exists.
- **No R3 calibration work.** R3 is gated on an R2 *helps* verdict.

---

## 5. Build

### 5.1 Measurement script

`scripts/kairos_r2_router_measure.py` — reads the stamps, splits by archetype, computes §3.2 metrics + walk-forward, emits the §3.4 verdict per archetype and pooled. Writes `worklog/kairos/reports/YYYYMMDD_r2_router_measure.md`. Fail-open on a thin cohort → `UNDECIDABLE`.

### 5.2 Sequence

- **R2a — Stand up the measurement script** (read-only, runnable now). Confirms the current `UNDECIDABLE` state and the stand_by dominance.
- **R2b — Accumulate routed episodes.** Either wait for organic accumulation (slow — the predicates fire ~0.4% of the time) or run a *separate, pre-registered* predicate-review hypothesis (which archetypes should fire, and why) with its own walk-forward. **Not a prerequisite to writing R2a.**
- **R2c — Verdict.** Once `≥200 routed across ≥8 symbols` (and `≥40` per evaluated archetype), run R2a and record the verdict on `nt_roadmap.strategy_router_r2_measurement`.

### 5.3 Roadmap linkage

This doc is the `worklog_ref` for `nt_roadmap.strategy_router_r2_measurement`. The R2 verdict updates that row in place (status → `resolved` with the verdict, or `blocked` while undecidable).

---

## 6. Files / lineage

- R0 catalog: `config/strategy_registry.yaml`; loader in `regime_platform/core/feature_flags.py`.
- R1 router: `regime_platform/sweep_orchestrator/strategy_router.py`; tests `tests/test_strategy_router.py`, `tests/test_strategy_registry.py`.
- Wider lens (R0–R6): `docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md`.
- B1 MR cohort bar (the `n ≥ 40`, `NO_DATA` vs `FAIL` precedent): `scripts/kairos_mr_cohort_walkforward.py`, `references/mr-cohort-walkforward.md`.
- Era-clean evidence: `nt_signal_sim` post `2026-10-02`, `contaminated=false`; `v_calibration_records_current`.
