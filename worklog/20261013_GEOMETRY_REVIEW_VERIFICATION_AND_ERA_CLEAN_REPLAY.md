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
widened bands can never clear the walk gate". **Measured, false.** Full grid per
band, every in-band cell attributed to the first filter that rejects it:

| band | tp+sl | max walk expiry | min_rr | clamp | **expiry** | both-halves | pass |
|---|---|---|---|---|---|---|---|
| FOREX (pooled) | 7.6 ATR | 0.0009 | 1 | 0 | **0** | 8 | 0 |
| BTCUSD | 10.3 ATR | 0.0000 | 0 | 0 | **0** | 9 | 0 |
| US30USD | 15.7 ATR | 0.1250 | 0 | 0 | **0** | 9 | 0 |
| XAGUSD | 9.4 ATR | 0.0212 | 0 | 0 | **0** | 0 | **9** |
| **XAUAUD** | 11.2 ATR | **0.2299** | 3 | 0 | **3** | 0 | 3 |
| XAUEUR | 6.2 ATR | 0.0000 | 0 | 0 | **0** | 9 | 0 |
| XAUUSD | 6.2 ATR | 0.0000 | 0 | 0 | **0** | 9 | 0 |
| **totals** | | | 4 | 0 | **3** | 44 | 12 |

*(AAPL and US500 had n=0 envelopes in this window — market closed — so 7 of the 9
bands are measurable.)*

**Correction to this review's first pass.** It reported "expiry kills ZERO cells in
every band" — **wrong**. XAUAUD has **3** cells at expiry **0.2299 > 0.20** (all
`tp=6.82`); the scope list simply omitted XAUAUD. An independent verifier caught
it. The 3 are **doubly killed** (`e1 < 0` too), so expiry is never the unique or
load-bearing killer — re-running `select_tp_sl` with `max_expiry=0.20` vs `1.0`
returns an identical pick in every band — but the count is not zero, and the
generalisation from six bands to "every band" was not earned.

Two compounding errors remain in D3 itself: (a) "the 4+ bucket runs 26% expired"
is the *reachability bucket's realised* share, not the walk's expiry share —
different quantities treated as one; (b) the stated conclusion ("the widening
reaches the book only via `_band_center_pick`") is contradicted — **4 of 7
measurable bands return a STRICT walked pick** that clears expiry *and* both
halves (XAGUSD 6.39/2.69, XAUAUD 6.57/4.31, BTCUSD, AUDUSD 4.52/2.87), and the
verifier found those also pass the live walk-forward gate. The three that do fall
through (XAUUSD, US30USD, FOREX) do so on **negative fold expectancy**, not on
expiry.

What survives in D3 is the design point underneath: expiry is already priced in
the 3-outcome EV via `p_expire`, so a walk-side hard reject double-counts it.

**Second correction (to §1's confirmed list).** D1 was recorded as "`e1>0 ∧ e2>0`
is the only robustness bar". Imprecise: the *same* guard
(`tiered_atr.py:535`) is `if exp_m > max_expiry or e1 <= 0 or e2 <= 0` — expiry is
a co-condition in the same expression. Both-halves is the only *fold-consistency*
bar, not the only condition.

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

### R1 moved to the roadmap

The review's **R1 (fix entry timing — the WR-lever candidate)** is now
`nt_roadmap.entry_timing_conditioning`, status `pending`, `review_at` 2026-11-10
(`scripts/update_nt_roadmap_r1_entry_timing.py`; idempotent — verified create then
patch, one row).

Recorded as **pending a clean measurement, not pending a build**: R1's premise
("entry timing sits below the random-walk null") rests on the reachability table
that §1 above shows the review's own D6 distrusts, and on clean rows the current
process's walk sits *above* the null in the first half of the window. Its
`review_condition` states the measurement that would settle it. Related rows
already on the roadmap: `strategy_router_r1`, `strategy_router_r2_measurement`,
`strategy_live_route_r6`, `mean_reversion_gold_quant`,
`tier1_context_alignment_gate_scope`, `regime_edge_gate`.

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

**Subagent verification — two independent verifiers, both completed.**

*Verifier 1 (era-clean claims) — all three VERIFIED.*
- `_load_class_signals` (`tiered_atr.py:714-756`) has no `contaminated` filter and no
  cutover fetch; `dynamic_atr_pick` sets `since = until − lookback_days` unclamped.
- Window `2026-10-01T02:22Z … 2026-10-11T02:22Z`, cutover `2026-10-02T00:00Z`: **4,064
  raw rows → 450 contaminated (11.1%)**; after the picker's hour-dedupe **1,255
  signals → 120 contaminated (9.6%)**. Uniform across symbols.
- **All 450 contaminated rows are POST-cutover** — confirmed independently.
- Replay: unfiltered **+0.0232 / e1 +0.2670 / e2 −0.2202**; era-clean **−0.0845 /
  e1 +0.0405 / e2 −0.2093** — matching the figures above to the precision given.
- **Decisive isolation:** cutover-clamp only, contaminated kept → **+0.0247** (barely
  moves); removing the 120 contaminated rows flips the sign. The positive expectancy
  is specifically an artifact of the contaminated rows.
- **New finding from the verifier:** `atr_reopt` clamps to the cutover but does **not**
  filter `contaminated=false` either — and since *every* contaminated row is
  post-cutover, **the reopt's Fix B does not actually clean it.** Both the picker and
  the reopt need the `contaminated=eq.false` filter; the cutover clamp alone is
  insufficient. That sharpens §2's defect statement.
- Two judgment calls noted: shipped `min_rr` is **1.5** (`feature_flags.yaml:759`), not
  the 2.0 code default — at 2.0 *all nine* FOREX cells fail the pre-filter (max tp/sl
  = 1.81); and filtering before vs after the dedupe gives identical results.

*Verifier 2 (review claims) — D3 REFUTED, and two of this review's own claims corrected.*
- **D3 refuted** ✓ (independently), and **more strongly than this review put it**:
  `max_expiry` is *provably inert* — re-running `select_tp_sl` with `max_expiry=0.20`
  vs `1.0` returns an identical pick in all 7 measurable bands. The blocker for
  XAUUSD/US30USD/FOREX is negative fold expectancy, not expiry.
- **This review's "expiry kills ZERO cells in every band" → REFUTED.** XAUAUD has 3
  cells at expiry 0.2299 > 0.20 (all `tp=6.82`). Corrected above; the scope list
  omitted XAUAUD. `scripts/audit_band_filters.py` now covers all 9 bands and reproduces
  it exactly.
- **"the widening reaches the book only via `_band_center_pick`" → REFUTED.** 4 of 7
  bands return a strict walked pick that also passes the live walk-forward gate.
- **"`e1>0 ∧ e2>0` is the only robustness bar" → imprecise.** The same guard is
  `if exp_m > max_expiry or e1 <= 0 or e2 <= 0`. Corrected above.
- Claims B (router: shadow, per-archetype, gate verbatim "≥ 200 ROUTED episodes across
  ≥ 8 symbols … ≥ 40 in the smallest archetype") and C(ii)/(iii) **VERIFIED**, including
  the caveat that band-centre substitution only fires when the mode is `live` (both are
  `live`), and that `cost_atr` has no per-symbol override anywhere.

*One unresolved discrepancy, flagged rather than smoothed over:* for **BTCUSD** the
verifier measured 260 envelopes → 6 cells passing / 3 both-killed, while
`scripts/audit_band_filters.py` measures 209 → 9 both-killed. The other six scopes
match exactly (XAGUSD 189, XAUAUD 187, XAUUSD 190, US30USD 104, FOREX 1167). The
difference is in the fetched signal set, not the method. **Not load-bearing:** BTCUSD's
band was not changed by the reconciliation. Worth a re-check before any BTCUSD band
decision.

**The defect this leaves open (not fixed here)**

`_load_class_signals` should apply the platform's own era-clean standard:

```python
"contaminated": "eq.false",          # + optionally ts >= nt_calibration_cutover
```

**Two consumers need it, not one** (corrected by the verifier): `_load_class_signals`
applies neither filter, and `atr_reopt`'s Fix B clamps to the cutover but **does not
filter `contaminated=false`** — which is insufficient, because **every** contaminated
row is post-cutover. So the reopt's "era-clamp" does not actually clean its window.

The consumers that *are* clean: `bracket_reachability` (`:209`, `:223-225`) and
`dynamic_tier_optimizer._fetch_resolved_rows` (`:89`) both filter `contaminated=false`.
The geometry walk and the reopt are the two that do not, so a replay, a pick, or a
reopt proposal taken through them can look decisive on artifact rows. One filter per
consumer; not applied here because the verifiers were reading those files.
