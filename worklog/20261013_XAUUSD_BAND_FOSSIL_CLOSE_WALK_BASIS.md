# XAUUSD's band is a fossil of the close-walk basis — and nothing can retire it

**Date:** 2026-10-13 · **Scope:** `config/feature_flags.yaml` `dynamic_atr.by_symbol.XAUUSD`
**Evidence scripts:** `scripts/replay_xauusd_basis.py`, `scripts/replay_xauusd_band.py`
**Trigger:** XAUUSD was the one deployed symbol whose stop (1.0 ATR) sits in the worst
bucket of the platform's own distance→outcome curve (66% of its historical stop-outs
land within one bar), while every reconciled symbol sits at ~30%.

---

## 1. Why the band is 1.0 ATR

`config/feature_flags.yaml` records it as the **2026-09-30 vol-regime build**
(`worklog/kairos/20260930_VOL_REGIME_ATR_BTC_XAU.md`):

```
| XAUUSD | pooled 45d | 4.94 | 1.00 | RR 4.94 | wr 0.405 | EV +1.23 | +2.12 / +0.34 | 817 |
```

That worklog also shows the concurrent short reading was **negative and dismissed**:

> `XAU 14d EV −0.32 was sample noise; 45d pooled is +EV without a gate.`

The shipped band (`tp [4.80, 4.94, 5.10]`, `sl [0.95, 1.00, 1.10]`) and its clamps
(`tp 4.6–5.3`, `sl 0.9–1.2`) are a ±0.1 ATR window pinned around that single point
estimate. **The clamp is the search space, so the live walk cannot explore any wider
stop** — `2.0 ATR` is outside `sl_atr_max = 1.2`.

## 2. Why that figure does not survive

`scripts/vol_atr_solver.py` imports the platform's own walk —
`from ...tiered_atr import EXPIRY_BARS, walk` — so the *arithmetic* is identical. What
differs is the path it feeds it:

```python
# vol_atr_solver.py::build_paths_meta
path = (c[j:j + EXPIRY_BARS] - r["entry_price"]) * d / r["atr"]     # CLOSES ONLY
```

versus the live basis, which passes the intrabar high/low envelope
(`build_envelopes(..., basis=live_basis())`). **A close-only path cannot see wicks.**

Replaying the 09-30 window (2026-08-16..09-30, n=867) with both, same cell:

| basis | wr | e | e1 | e2 |
|---|---:|---:|---:|---:|
| CLOSE (the solver's method) | 0.391 | **+1.124** | +2.350 | −0.100 |
| INTRABAR (the live platform) | 0.201 | +0.092 | +0.615 | −0.429 |
| *recorded 2026-09-30* | *0.405* | *+1.23* | *+2.12* | *+0.34* |

CLOSE reproduces the recorded numbers; INTRABAR gives **half the win rate**. The gap
is fully accounted for: **175 of 867 signals (20.2%) are wick-only stop-outs** — the
intrabar basis stops them out, the close path never crosses the stop. That is the
entire `0.391 → 0.201` difference. The recorded `h2 +0.34` also flips negative on
every basis; the band never satisfied `both halves EV>0` outside the solver's own
close path.

So the tight stop was not chosen badly on its evidence — **it was chosen on a basis
that structurally under-counts stop-outs, and that basis no longer exists in the
platform.** The cutover to intrabar/`sl_first` landed 2026-10-02, two days *after*
the band was set. The band was never re-derived.

## 3. Why reopt did not catch it

**Reopt did fire** — `00:00:19` and `04:00:33–04:01:31` on 2026-10-13 (and the urgency
job runs every 4h; `applied=[]` on both, correct for proposal-only). Both runs were
**era-clamped** (`lookback → 2026-10-02 (post-cutover only)`) and both use the correct
basis (`atr_reopt.py:357` → `build_envelopes(signals, bars, basis=live_basis())`).

It did not miss XAUUSD. The `20261013_atr_reopt.md` report says so explicitly:

```
| XAUUSD | — | drift=False | no +EV cell in free lattice |
```

with the acceptance line `wr∈[0.40,0.60] · exp≤0.20 · cost 0.1 · both halves EV>0`.

So the band is **structurally unreachable** from both directions:

| path | why it can't fix the band |
|---|---|
| live walk (`tiered_atr`) | clamps are ±0.1 ATR around the stale estimate — a wider stop is outside the search space |
| reopt (`atr_reopt`) | `both halves EV>0` rejects every cell in the free lattice (independently confirmed: 0 of 72 cells in `replay_xauusd_band.py`), and by design it never proposes clamp changes (`Heavy sweep keeps local clamps`) |

## 4. What this actually is

Not a XAUUSD defect and not a case for hand-widening a clamp. It is an **era
incoherence**: a band derived on the retired close-walk basis persists, the live walk
cannot reach past its clamps, and the reopt's gate makes it a no-op for any symbol
whose data does not clear both halves. The reopt reports `drift=False` and stays
silent, so a stale band is indistinguishable from a healthy one.

**Generalisable check:** every `by_symbol` band set before the 2026-10-02 cutover is
suspect on exactly this mechanism, and tight stops are the most exposed — close-walk
bias grows as the stop narrows. `XAUUSD` is the one that shows up in the deployed
numbers because its clamp is the tightest (±0.1 ATR).

**Fix direction (not applied — operator-owned):** band derivation must run on the
basis that trades it. Either the reopt proposes bands on the live basis with a
clamp-change path, or pre-cutover bands are re-derived once on intrabar and the
provenance is recorded per band so a fossil is distinguishable from a live pick.
