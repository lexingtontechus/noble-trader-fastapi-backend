# The pick store is a second geometry source — G1 at the store boundary

**Date:** 2026-10-13
**Follows:** `worklog/20261012_FEATURE_FLAGS_VERSION_RECONCILIATION.md` (G1), `20261012_GEOMETRY_G2_G3_AND_FOREX_BAND_REPLAY.md` (G2/G3)
**Found by:** `scripts/validate_geometry_flow.py` against live sweeps

## What the validation found

G1 made the *config* the single authority, G2 made the solve always return
geometry, G3 made the light path resolve instead of falling back to brick. All
three hold live — 4 consecutive sweeps at 11/11 ATR geometry, zero brick.

But the validator surfaced a hole: **`read_pick_from_store` returns whatever was
persisted, with no check that it lies inside the symbol's *current* band**, and
the light path applies it verbatim.

| symbol | its band (tp / sl) | store served | in band? |
|---|---|---|---|
| BTCUSD | `7.8–8.4` / `1.65–1.95`, clamps tp ≤ 8.8 | **`13` / `2`** | ✗ |
| US30USD | `12.5–13.1` / `2.25–2.6`, clamps sl ≤ 2.8 | **`8` / `5`** | ✗ |
| US500 | `12.9–13.6` / `7.8–8.4` | `12.9` / `7.8` | ✓ |
| AUDUSD | `4.27–4.77` / `2.63–2.87` | `4.52` / `2.87` | ✓ |
| XAUAUD | `6.32–6.82` / `4.19–4.43` | `6.32` / `4.19` | ✓ |

2 of 6 hits were out of band. The mechanism: the store is a **cache of past
solves**, keyed by symbol with a 6-hour TTL, and a symbol's band can change
(retune, reopt, the 2026-10-12 reconciliation). A pick that is fresh by *age* can
therefore still carry a bracket from a *previous* band. G1 fixed config
resolution; it did not close this second path into the applied geometry.

This is exactly the "second source of truth" shape the redesign exists to
remove — and note it is **pre-existing**, not introduced by G3: the old light
path read the same store. G3 changed only what happens on a *miss*.

## The fix

`tiered_atr._pick_in_band(pick, cfg)` — a pick is only authoritative if it is
inside the clamps **and** a member of the current grid (a pick is always a grid
cell of the band it was solved under, so both tests are exact, not heuristic).

The store-hit path now treats out-of-band as a **miss**:

```python
_stored = read_pick_from_store(symbol, max_age_hours=_PICK_TTL_S / 3600.0)
if _stored is not None and _pick_in_band(_stored, cfg):
    return _stored                      # fresh AND in band → authoritative
if _stored is not None:
    logger.warning("... store hit REJECTED — ... outside the current band; recomputing")
# falls through to the solve → recompute (G3), never apply the stale bracket
```

**Coverage review (done before implementing):** the guard needs a band for every
scope it can see. Checked all of them — all 14 registry symbols resolve their own
`by_symbol` band (none fall through to global), and every class scope
(`forex`, `commodities`, `crypto`, `equity`, `stocks`) resolves the global band.
So `get_dynamic_atr_config(asset_class, symbol)` is total: no symbol or class is
left without a band to validate against.

## The other finding: most applied geometry is the band centre

From the same window — per symbol, applied geometry by source:

| symbol | band centre (n=0) | walked (n>0) |
|---|---|---|
| AUDUSD | 1 | **6** |
| BTCUSD | 5 | 2 |
| XAUAUD | 5 | 1 |
| USDJPY, USDCHF, EURUSD, XAUUSD, XAUEUR, USDCAD, GBPUSD, XAGUSD | 7 each | **0** |
| **total** | **68** | **9** |

**8 of 11 symbols never get a walked pick** — they apply the operator's band
centre every sweep. The gate log explains it: 50 walk-forward REJECT and 29
CONSISTENCY REJECT across the window.

- *Consistency gate* (`dynamic_atr_consistency_mode: live`, tol 0.20): the walked
  TP-hit rate must agree with the platform's own measured bucket TP rate. A walk
  saying 55% against a measured 33% is rejected as contradicted.
- *Walk-forward gate*: the bracket must generalise on a held-out fold, not just
  the in-sample halves. Negative out-of-sample → rejected as overfit.

This is **not** a G1/G2/G3 defect — the gates are doing what they were built to
do, and G2's fallback keeps the flow alive (no brick, one band, always geometry).
But the practical consequence is that the "dynamic" geometry is, for most of this
book, the operator's static mid-band default rather than a fitted bracket. The
gates are reporting the same thing the no-edge analysis did: the walks do not
generalise on this evidence.

**Open design question, not a bug:** whether those two gates are calibrated
correctly for the evidence volume the book actually has (per-symbol solves want
~80 resolvable paths; the book yields 2–3). Rejecting a non-generalisable pick is
correct; rejecting so many that the default is what trades may mean the bar is
set for an evidence volume this book does not have.
