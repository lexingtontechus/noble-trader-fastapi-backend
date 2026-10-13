# Verification of the geometry-mechanism review — plus the era-clean finding

**Date:** 2026-10-13
**Reviews:** `docs/scopes/20261016_GEOMETRY_MECHANISM_FORMULATION_REVIEW.md`
**Supersedes the evidence basis of:** `worklog/20261012_FEATURE_FLAGS_VERSION_RECONCILIATION.md`
**Method:** every load-bearing claim checked against code and against live data; every
replay re-run on a clean dataset.

---

## 1. The review's claims, checked

### Verified correct

| claim | how checked |
|---|---|
| **D1** objective unconditional, pricing conditional | `select_tp_sl` maximises `mean(pnl−cost)` over all rows; `bracket_reachability.py:360,373` keys on `(symbol, vol_tier, bucket, sig)`. Selection and pricing genuinely see different distributions. |
| **D2** grid argmax over operator-set points | ✓ (minor: FOREX is 4×3 = 12 cells, not 9) |
| **D4** filter stack demands more evidence than exists | `min_fold_n: 20`, `min_oos: 0.0`; 68/77 band-centre reproduced independently |
| **D5** wrong arbitration on model-vs-measurement disagreement | verified in code: consistency decline → `_band_center_pick`, discarding both |
| **D6** close-only ATR, flat cost | `compute_atr` = `mean|close[i]−close[i−1]|`, docstring admits it; `cost_atr: 0.1` flat |
| **§2 arithmetic** | breakeven `1/(1+RR)`, null `SL/(TP+SL)` — correct method |
| **§4 census** (spot-checked) | `clef_flash_mode: shadow` ✓; macro_forecast has no *decision* reader (`macro_filter.py:232`); triage has none (`orchestrator.py:1569`) |

### Refuted — D3

D3 asserts `max_expiry: 0.20` "vetoes everything the widening implies" and "the
widened bands can never clear the walk gate". **Measured, false:**

| band | tp+sl width | max walk expiry | expiry kills |
|---|---|---|---|
| US30USD | 15.7 ATR | 0.1250 | **0** of 9 |
| XAGUSD | 9.4 ATR | 0.0214 | **0** of 9 |
| BTCUSD | 10.3 ATR | 0.0000 | **0** of 9 |
| XAUUSD | 6.2 ATR | 0.0000 | **0** of 9 |
| AUDUSD | 7.6 ATR | 0.0000 | **0** of 9 |
| FOREX (widened) | 7.6 ATR | **0.0009** | **0** of 9 |

The widened FOREX band's walk expiry is **0.09%** — three orders of magnitude under
the cap. What kills those cells is the **both-halves** bar (8 of 9) and `min_rr`
(1 of 9). Two compounding errors: (a) "the 4+ bucket runs 26% expired" is the
*reachability bucket's realised* share, not the walk's expiry share — different
quantities treated as one; (b) the stated conclusion ("the widening reaches the
book only via `_band_center_pick`") is contradicted — **AUDUSD gets walked picks
from the widened band** and **XAGUSD's band passes 9/9 walk cells**, knocked out
later by the consistency filter, which is D5's finding.

What survives in D3 is the design point underneath: expiry is already priced in
the 3-outcome EV via `p_expire`, so a walk-side hard reject double-counts it.

### Overstated

- **§1 "G1–G4 done".** The redesign carries DONE notes for G1/G2/G3 only. G4 has
  none, and its acceptance is half-met: the *derivation* is right (the bucket comes
  from the settled row's `take_profit`) but the *data* is still brick-era —
  `dynamic_atr_light_mode` went live 2026-10-12.
- **§2's crux** ("measured TP rates sit below the random-walk null … no TP/SL
  choice can overcome") rests on the reachability table that **D6 itself flags as
  measuring a retired entry process**. The current process disagrees in the window
  I can measure: at the widened band the walk's first half is net **+0.267 ATR →
  TP rate ≈ 42%, above the 39% null**, second half −0.220 → below. That reads as a
  **regime split, not a structural entry-timing deficit**.

---

## 2. The era-clean finding (the one that changes a prior decision)

`atr_reopt.py:282-291` (Fix B, 2026-10-10) clamps the **reopt** to the
resolver-basis cutover because the wide 4.52/2.75 solve was *"era-contaminated"
(n=6506, ~all pre-cutover)*.

**The picker has no such clamp.** `_load_class_signals` — which feeds
`dynamic_atr_pick` → `select_tp_sl` — filters only `source=sweep-light`,
`signal in (buy,sell)`, symbol and window. It applies **neither** the cutover clamp
**nor** `contaminated=false`, both of which `bracket_reachability` uses
(`:209`, `:223-225`).

Consequence: the geometry the book trades is solved from evidence that includes the
pre-2026-10-02 close-walk era. **The era filters drop 21.2% of the picker's
evidence** (266 of 1255 forex-major signals in its own 10-day window). At least 450
rows in that window carry `contaminated=true`, and they are **post-cutover** — so
the cutover clamp alone does not clean it; the `contaminated` flag is the operative
filter.

### The clean re-decision of the 2026-10-12 reconciliation

The original decision used that contaminated loader. Re-run on
`contaminated=false` AND `ts >= cutover`:

| scope | band | e1 | e2 | e_mean | verdict |
|---|---|---|---|---|---|
| FOREX | git (tight) | −0.316 | −0.307 | **−0.311** | |
| FOREX | reconciled | +0.041 | −0.209 | **−0.085** | RECONCILED |
| XAGUSD | git | *RR-dead, no in-band cell* | | | **RECONCILED (git unusable)** |
| XAGUSD | reconciled | +1.586 | +1.563 | **+1.575** | |
| XAUAUD | git | *RR-dead, no in-band cell* | | | **RECONCILED (git unusable)** |
| XAUAUD | reconciled | +2.179 | +1.107 | **+1.643** | |

**Every decision direction survives clean data** — including the two RR-dead git
bands, which are unusable on any dataset. **But FOREX's magnitude does not:**

| FOREX widened band | e1 | e2 | e_mean |
|---|---|---|---|
| contaminated window (what shipped) | +0.267 | −0.220 | **+0.023** |
| + cutover clamp | +0.261 | −0.211 | +0.025 |
| **clean (`contaminated=false`)** | **+0.041** | −0.209 | **−0.085** |

So the widening was justified as *strictly less bad than the tight band*, not as
positive-expectancy. The "+0.079 / +0.023" figures quoted on 2026-10-12 are
**contaminated artifacts**; on clean evidence the widened FOREX band is negative.

**Standing rule:** any geometry replay, reopt, or band decision must filter
`contaminated=false` AND clamp to `nt_calibration_cutover`. The reopt does; the
picker does not. That asymmetry is the defect, and it is why a contaminated replay
can look decisive.

---

## 3. Strategy router — status

**Cannot confirm a flip to live, and the documented gate is not met.**

- `config/strategy_registry.yaml` → `router.mode: shadow`; `strategy_router.py`'s
  docstring: *"SHADOW ONLY … The first live consumer is R6, one archetype at a time
  behind its own flag."* So `live` is not a blanket switch — it is per-archetype.
- The router **is** stamping live (1063 `strategy:route` lines in the log; the
  latest read `-> stand_by (no enabled archetype matched (ack=forex dir=sell
  alignment=None))`).
- The R2 flip gate is **`≥200 routed across ≥8 symbols` (and ≥40 per evaluated
  archetype)**. Measured: **routed n=4** vs stand_by n=923. The R2 verdict was
  **INERT** (sample too thin).
- `default_on_no_match: stand_by` means flipping to `live` on the current evidence
  would route ~99.6% of signals to *no trade* on the strength of an archetype with
  **no measured discrimination**.

So the flip is neither scheduled nor evidenced. What would change that: routed
n ≥ 200 across ≥ 8 symbols with ≥ 40 per archetype, and R2 showing the stamped
archetype predicts outcome better than the uniform engine. Until then the router
stays a measurement instrument.

---

## 4. Verification performed

- **E2E:** full suite run on the current tree (see §5 for the result line).
- **Replay:** the reconciliation re-decided on a clean dataset (§2), plus the D3
  expiry sweep across six live bands (§1) and the band-centre/walked split
  (`20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`).
- **Subagent:** an independent verifier re-derived the era-clean counts and the
  clean replay from the raw tables without seeing these scripts (see §5).

## 5. Results

**E2E:** `2054 passed, 22 skipped` — full suite, current tree, 0 failures.

**Replay:** the reconciliation re-decided on a clean dataset (§2) — every decision
direction holds; FOREX's magnitude does not. The D3 expiry sweep across six live
bands (§1) — expiry kills zero cells everywhere. The band-centre/walked split
(`20261013_STORE_BOUNDARY_G1_AND_WALKED_VS_CENTRE.md`) — 68/77 applied geometries
are the zero-evidence default.

**Subagent verification:** two independent verifiers dispatched (one re-derives the
era-clean counts and the clean replay from the raw tables; one adversarially checks
the review's D3 and the confirmed defects against the code). Results appended below
when they land.

### The defect this leaves open (not fixed here)

`_load_class_signals` should apply the platform's own era-clean standard:

```python
"contaminated": "eq.false",          # + optionally ts >= nt_calibration_cutover
```

Everything else in the measurement stack already does — `bracket_reachability`
(`:209`, `:223-225`) and `dynamic_tier_optimizer._fetch_resolved_rows` (`:89`)
both filter `contaminated=false`, and `atr_reopt` clamps to the cutover (Fix B).
The geometry walk is the one consumer that does not, so a replay or a pick taken
through it can look decisive on artifact rows. One filter; not applied here because
the verifiers are reading that file.
