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

**8 of 11 symbols never get a walked pick** — they trade the operator's band
centre every sweep. Log: 50 walk-forward declines, 29 consistency declines.

### Terminology: these are candidate filters, not gates

The design already says it (`docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md`
§3): *"Candidate quality filters (`min_rr`, expiry, both-halves, consistency,
walk-forward) run **inside** the solve as **candidate filters**: a rejected
candidate is simply not chosen; the solve moves to the next candidate. **They
never null the result.**"* §2's table calls them "gates" only to describe the OLD
behaviour (`on failure → brick`) that the redesign removed. **"Gate" belongs to
signal qualification** — the single gate. The band does not gate the signal; it
bounds the *edge space*, and the filters inside select which bracket in that space
defines the edge that sets TP/SL.

### The structural finding this exposes

Only three of the five candidate filters actually run **inside** the solve:

| candidate filter | where it runs | on failure |
|---|---|---|
| `min_rr` | inside `select_tp_sl` | candidate skipped, solve moves on ✓ |
| expiry (`max_expiry`) | inside `select_tp_sl` | candidate skipped ✓ |
| both-halves (`e1`/`e2`) | inside `select_tp_sl` | candidate skipped ✓ |
| consistency | **after** — on the single returned pick | `_band_center_pick` |
| walk-forward | **after** — on the single returned pick | `_band_center_pick` |

`check_pick_reachability_consistency` (`tiered_atr.py:1024`) and
`walk_forward_validate` (`:1064`) are called in `dynamic_atr_pick` *after*
`select_tp_sl` has already chosen a winner, and they act on that one pick. So when
the winner fails there is no "next candidate" to move to — the solve substitutes
the band centre, which carries **n=0 and exp=0: no evidence at all**.

That is why 68 of 77 applied geometries are the default. Not because the filters
are too strict, and not because the walks lack an edge — but because **two of the
five filters sit on the wrong side of the selection.** Per the design they belong
*inside* the candidate loop, so the solve returns the best bracket that survives
them and a "next candidate" exists to move to.

The old register also survives in the naming: `tiered_atr.py:614` is headed
"Stage 5: pick ↔ reachability consistency gate", and the log emits
`CONSISTENCY REJECT` / `walk-forward REJECT`.

### Consequence, stated plainly

Not a G1/G2/G3 defect: no brick, one band, always geometry, and G2's fallback
keeps the flow alive. But the "dynamic" geometry is, for most of this book, the
operator's static mid-band default **carrying no measured evidence**, while the
design's own contract promises a bracket that passed the filters. Closing that gap
is a code change — move the two checks inside the candidate loop — not a policy
change, and it is the next lever.
