# Reachability Vol-Tiering: SL/TP Hit Validation by Regime Vol Tier

> **Priority**: P1 · **Status**: scoped, ready to build ·
> **Date**: 2026-09-13 · **Roadmap refs**: `heavy_sweep_reachability_mode`,
> `heavy_sweep_atr_anchored_bricks_mode` (Phase 3b)

---

## Reframe (correcting the prior turn's misdirection)

The previous pass in this session proposed a "Gate C" — a new qualification
gate that would block signal generation in low-vol regimes. **That was the
wrong mechanism.** Signal qualification (buy/sell direction, the 7-gate
chain) is not the problem here. **Reachability — whether the SL or TP
actually gets hit before the 48h expiry — is measured one dimension short.**
This document scopes the correct fix: extend the existing reachability/
calibration-bias pipeline (already built, already shadow-running, already
read-only) to bucket by volatility tier, not just distance/ATR.

**Nothing in this scope changes when a signal fires, its direction, or
whether it qualifies.** This only sharpens the accuracy of the reachability
diagnostic that Phase 3a/3b already consume.

---

## The evidence that motivated this

Migration 151's regime-tiered scorecard (`v_symbol_signal_scorecard_7d`,
grouped by `(symbol, signal, regime)`) surfaced this pattern on 2026-09-13:

| symbol | signal | regime | W | L | win% |
|---|---|---|---|---|---|
| XAUUSD | sell | med_high_vol_bear | 3 | 2 | 60% |
| XAUUSD | sell | med_low_vol_bear | 0 | 8 | **0%** |
| BTCUSD | sell | med_low_vol_strong_bear | 2 | 1 | 67% |
| BTCUSD | sell | high_vol_strong_bear | 0 | 2 | **0%** |

Same symbol, same direction, wildly different outcome — driven by which
volatility tier the HMM regime label carries. **The direction of the effect
is not universal**: metals lose in low-vol and do fine in higher-vol; BTC
shows the opposite. Any fix has to be per-symbol, empirical, and derived
from the reachability data itself — not a hardcoded "low vol is bad" rule.

## Why this is a reachability problem, not a qualification problem

`nt_bracket_reachability` / `nt_calibration_bias_by_bucket` bucket every
signal by `distance / ATR` (`bracket_reachability.py:_bucket_for`,
`ATR_BUCKETS` = `<0.5 | 0.5-1 | 1-2 | 2-4 | 4+`). **ATR itself is
regime-conditional** — the HMM's volatility dimension is fit on the same
price history ATR is computed from. A bracket that lands in "4+" during a
`low_vol` regime represents a much smaller *absolute* price distance than a
"4+" bracket during a `high_vol` regime of the same symbol — but today's
bucket key treats them identically. **The bucket key is missing the
dimension that actually explains the variance the scorecard just exposed.**

This is exactly the same root cause Phase 1 (`bracket_reachability.py`)
already fixed once — raw %-distance was meaningless across symbols, so it
became distance/ATR, per symbol. The same argument applies one level
deeper: distance/ATR is still incomplete within a symbol, because ATR's own
scale shifts across the four vol-tier regimes. **Tiering the bucket key by
vol_tier is the same fix, applied one dimension further.**

---

## What already exists (nothing new to build for the underlying mechanism)

| Component | File | Current key | Role |
|---|---|---|---|
| Reachability diagnostic | `bracket_reachability.py` | `(symbol, distance_atr_bucket)` | hit-rate/time-to-resolve per bucket |
| Calibration bias | `calibration_bias_bucket.py` | `(symbol, distance_atr_bucket)` | `expectancy_r` per bucket |
| Heavy-sweep shadow filter | `heavy_sweep_reachability.py` | looks up `bias_by_bucket[bucket]` | Phase 3a: would-pick comparison |
| ATR-anchored bricks | `heavy_sweep.py::generate_atr_anchored_brick_sizes` | uses live ATR at sweep time | Phase 3b: widens the grid |

**`regime_label` is already captured on every `nt_signal_sim` row** (used by
migration 151's scorecard) and **already computed before the reachability
call** in `heavy_sweep.py` (`result.regime_label` set at line 806; the
reachability call is at line 936-988 — 130 lines later, same function,
already in scope). No new capture, no new network call, no new HMM
inference. This is purely a re-bucketing of data that's already flowing.

---

## Vol-tier extraction

`regime_label` is built as `f"{vol_state}_vol_{trend_state}"`
(`regime_engine.py:562`), where `vol_state ∈ {low, med_low, med_high, high}`
(`RegimeHMM.VOL_LABELS`, `regime_engine.py:232`). Vol tier is a pure string
split, no new computation:

```python
def _vol_tier(regime_label: str | None) -> str | None:
    """'med_low_vol_strong_bear' -> 'med_low_vol'; None if unparseable."""
    if not regime_label or "_vol_" not in regime_label:
        return None
    return regime_label.split("_vol_")[0] + "_vol"
```

Four possible values: `low_vol`, `med_low_vol`, `med_high_vol`, `high_vol`.
(`"unknown"` regime label, the pre-HMM-bootstrap fallback, correctly
produces `None` — same fail-open posture as a missing ATR today.)

---

## Scope of the build

### 1. Schema — add `vol_tier` to both bucket tables

**`nt_bracket_reachability`** (migration 120) and
**`nt_calibration_bias_by_bucket`** (same migration): add nullable
`vol_tier TEXT` column, widen the primary key from
`(symbol, distance_atr_bucket)` to `(symbol, vol_tier, distance_atr_bucket)`.

`vol_tier` nullable because historical `nt_signal_sim` rows don't carry
`regime` in every case (pre-migration rows, or rows where `regime_label`
was `"unknown"` at capture time) — same fail-open pattern `atr_source =
proxy_current` already establishes for missing ATR. A `NULL` vol_tier
bucket becomes a 5th "unknown regime" bucket per symbol, not an error.

**Migration 152** (next in sequence): `ALTER TABLE ... ADD COLUMN vol_tier`,
drop + recreate the composite primary key on both tables, update the CHECK
constraint set (add `'low_vol','med_low_vol','med_high_vol','high_vol'` as
allowed `vol_tier` values, nullable).

### 2. `bracket_reachability.py` — bucket by `(symbol, vol_tier, bucket)`

- `_fetch_resolved_rows()`: add `regime` to the `select` list.
- `build_reachability_table()`: bucket key becomes
  `(symbol, _vol_tier(row.get("regime")), bucket)`.
- `upsert_bracket_reachability` payload gains `vol_tier`.
- CLI report (`_print_report`) adds a `vol_tier` column.

### 3. `calibration_bias_bucket.py` — same 3-key bucketing

- `build_calibration_bias_by_bucket()`: add `regime` to select, same bucket
  key change as above.
- The `nt_bracket_reachability` join (for `pct_expired`) becomes a 3-key
  join: `(symbol, vol_tier, bucket)`.
- `upsert_calibration_bias_by_bucket` payload gains `vol_tier`.

### 4. `heavy_sweep_reachability.py` — vol-tier-aware lookup at sweep time

`reachability_aware_pick()` currently takes `bias_by_bucket:
dict[str, float | None]` keyed on bucket alone. Changes to
`dict[tuple[str, str], float | None]` keyed on `(vol_tier, bucket)`.

New parameter: `vol_tier: str | None` — the **live** regime's vol tier at
sweep time (`_vol_tier(result.regime_label)`, already computed in
`heavy_sweep.py` before this call). `_bucket_for_combo` is unchanged (still
computes the distance/ATR ratio per combo); `_is_reachable` now looks up
`bias_by_bucket.get((vol_tier, bucket))` instead of `bias_by_bucket.get(bucket)`.

`fetch_bias_by_bucket(symbol)` becomes
`fetch_bias_by_bucket(symbol) -> dict[tuple[str, str], float | None]` — same
best-effort fail-open shape, just returns a 2-key dict now.

**Call site in `heavy_sweep.py`** (around line 946-967): pass
`vol_tier=_vol_tier(result.regime_label)` into `reachability_aware_pick()`.
`result.regime_label` is already in scope at this point — zero new state
threaded through.

### 5. `nt_heavy_sweep_reachability_shadow` — record which vol tier was compared

Add `vol_tier TEXT` column (nullable) to the existing shadow table
(migration 152, same file as the schema change above) purely for
visibility — lets a human query "is XAUUSD's nothing-survived pattern
concentrated in low_vol specifically" directly from the shadow table
instead of cross-referencing `nt_signal_sim.regime` by timestamp.

### 6. Tests

- `test_bracket_reachability.py` (new — doesn't exist today, only the CLI
  report was manually verified before): `_vol_tier()` unit tests (all 4
  tiers, `None` on unparseable/missing), bucket-key now 3-tuple, backward
  compatibility (a row with no `regime` still buckets, just with
  `vol_tier=None`).
- `test_calibration_bias_bucket.py` (new, same reasoning): same coverage
  for `expectancy_r`/`bias` bucketing.
- `test_heavy_sweep_reachability.py` (extend existing 8 tests): update
  every `bias_by_bucket` fixture to the new `(vol_tier, bucket)` key shape,
  add 2 new tests — vol-tier mismatch correctly excludes a combo whose
  bucket is fine in the *wrong* tier, `None` vol_tier fails open (same
  posture as missing ATR).

---

## What this does NOT change

- **No qualification gate.** The 7-gate chain, `MIN_RR`/`MIN_KELLY`/
  `MIN_TIMESFM_CONFIDENCE`/`MIN_P_WIN`, direction logic — all untouched.
  A signal that qualifies today still qualifies, with the exact same
  entry/SL/TP it would have gotten before this change, as long as
  `heavy_sweep_reachability_mode` stays in `shadow`.
- **No live behavior change while `heavy_sweep_reachability_mode = shadow`**
  (its current state). The vol-tiered lookup only changes what the shadow
  comparison logs and persists — `pick_best_combo`'s actual return value is
  untouched in shadow mode, same as today.
- **No change to Phase 3b's brick-size generation.** ATR-anchored bricks
  already use the *live* ATR at sweep time, which is implicitly
  regime-conditional per sweep — that mechanism doesn't need to change.
  What changes is the **calibration data it gets scored against** in Stage 1
  review, which is currently vol-blind and will now be vol-aware.

---

## What this DOES change (once shadow data accumulates)

1. **Phase 3a's "nothing survived" diagnostic becomes regime-aware.** A
   bracket that's unreachable for XAUUSD in `low_vol` but reachable in
   `med_high_vol` will now be correctly distinguished, instead of both
   being folded into one misleading `(XAUUSD, "4+")` aggregate number.
2. **Phase 3b's Stage 1 review** (2026-10-05, per the existing roadmap
   entry) will be judged against vol-tiered `expectancy_r`, not a single
   blended number — sharper evidence for the same go/no-go decision already
   scheduled.
3. **The regime-tiered scorecard (migration 151) and this reachability
   fix converge on the same evidence.** The scorecard is the visual/
   diagnostic layer (human-readable, per-symbol/direction/regime); this
   fix is the validation layer underneath it (per-symbol/vol-tier/bucket,
   feeding the shadow comparison and eventually the live bracket pick).
   Together: **the scorecard shows WHERE win rate diverges by regime; the
   reachability re-bucketing explains WHY (bracket geometry vs. ATR scale)
   and gives the pipeline a mechanism to act on it** — via the existing
   shadow → live ladder, not a new gate.

---

## Rollout — same shadow-first discipline as everything else this session

1. **Build now** (this session, per explicit instruction): migration 152,
   3 module changes, `heavy_sweep.py` call-site update, new/extended tests.
   `heavy_sweep_reachability_mode` stays at `shadow` — zero live-behavior
   change from this build alone.
2. **Verify live**: after the next heavy sweep (or emergency sweep),
   confirm `nt_bracket_reachability`/`nt_calibration_bias_by_bucket` rows
   are populating with real `vol_tier` values (not all `NULL`) and
   `nt_heavy_sweep_reachability_shadow.vol_tier` is set on new comparisons.
3. **Re-run Stage 1 review at the existing 2026-10-05 date** (no new
   review date needed — this is the same Phase 3b review, now with sharper
   inputs). If the vol-tiered `expectancy_r` shows a consistent split (e.g.
   metals: low_vol negative, higher-vol positive), that becomes concrete
   evidence for whether `heavy_sweep_reachability_mode`'s live flip should
   be **vol-tier-conditional** in a future pass (a live per-tier gate would
   be a much smaller, better-justified version of the "Gate C" idea from
   the prior turn — but that's a decision for 2026-10-05, not this build).
