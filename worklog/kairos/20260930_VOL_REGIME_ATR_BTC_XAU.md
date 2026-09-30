# Vol-regime ATR — BTCUSD & XAUUSD build (not volume)

> **Date:** 2026-09-30 · **Scope:** `reports/20260930_vol_regime_atr.md` + `_diag.md`
> **Flag:** `dynamic_atr` `by_symbol.BTCUSD.vol_ratio_max` (optional band; default off)
> **Scripts:** `scripts/vol_atr_solver.py`, `scripts/vol_atr_diag.py`

---

## 1. Read this first — `vol_ratio` is NOT volume

**`vol_ratio` is a short/long realized-VA-volatility ratio**, not traded volume.

```
vol_ratio = std(Δ log close over last 24 bars) / std(Δ log close over last 168 bars)
```

Computed at signal **entry** from 1h closes (bars strictly before/at entry — no look-ahead).
It answers: *is short-term vol quiet or expanded vs the past week?*

| | |
|---|---|
| **What it is** | `rv24 / rv168` of 1h log returns |
| **What it is not** | volume, ATR level, or a trend filter |
| **Where** | `tiered_atr.compute_vol_ratio` / `vol_atr_solver.build_paths_meta` |
| **Why invented** | BTCUSD has **no** balanced pooled +EV cell; the only live-gateable cell is low `vol_ratio` |

If you see "volume filter" in notes, it means **this** volatility gate. Nothing in
the platform filters on exchange volume for geometry.

---

## 2. Why not just wait for 10/3

The 10-day production walk (and the old 0.91/0.71 XAU / 0.95/0.15 BTC picks) is
**underpowered** and averages regimes together. A 45-day free-lattice replay
(`wr∈[0.40,0.60]`, `exp≤0.20`, cost 0.1, **both halves EV>0**) finds:

| Symbol | Cell | TP | SL | RR | wr | EV | h1 / h2 | n |
|---|---|---:|---:|---:|---:|---:|---|---:|
| **XAUUSD** | pooled 45d | **4.94** | **1.00** | 4.94 | 0.405 | **+1.23** | +2.12 / +0.34 | 817 |
| **BTCUSD** | `vol_ratio` ≤ 0.71 | **8.07** | **1.77** | 4.56 | 0.413 | **+1.93** | +0.64 / +3.23 | 186 |
| BTCUSD | pooled | — | | | | | | 778 |

XAU 14d EV −0.32 was sample noise; 45d pooled is +EV **without** a gate.
BTC still has no pooled balanced cell — the gate is mandatory for the 8.07/1.77 cell.

---

## 3. Aggressive-move hypothesis (crypto)

User note: BTC historical wins came when price moved aggressively; prior TP
supported volatile trends.

**Verdict: half-true.** On `post_absmax` (path amplitude after entry):

| | Geometry | wr | EV | Notes |
|---|---|---:|---:|---|
| Aggressive paths | ~20 / 2.5 | **~0.25** | ~+2.6 (OOS holds) | far-TP **trend runner** |
| Same paths, old tight 0.95/0.15 | 0.95 / 0.15 | ~0.35 | **≈ −0.09** | tight TP **loses** in vol |
| Balanced engine | 8.07 / 1.77 on **low** `vol_ratio` | 0.41 | +1.93 | calm-vs-baseline regime |

`Spearman(vol_ratio, post_absmax) ≈ −0.57` — high entry short-term vol does **not**
predict the big follow-through. Do not gate "trend runner" on high `vol_ratio`.

**Crypto note (ETHUSD and friends):** same split may apply. Before live brackets:
1. own 45d walk (do **not** inherit BTC 8.07/1.77);
2. test pooled vs `vol_ratio` terciles;
3. decide explicitly: **balanced engine** (w 0.4–0.6) vs **trend runner** (wr≈0.25,
   far TP) as a *separate* product — never fold wr≈0.25 into the balanced bar.

---

## 4. What shipped (2026-09-30)

### Config (`config/feature_flags.yaml` → `dynamic_atr.by_symbol`)

```yaml
XAUUSD:   # 45d pooled, no gate
  tp_atr: [4.80, 4.94, 5.10]
  sl_atr: [0.95, 1.00, 1.10]
  # clamps 4.6–5.3 / 0.9–1.2
BTCUSD:   # low-vol only
  tp_atr: [7.80, 8.07, 8.40]
  sl_atr: [1.65, 1.77, 1.95]
  # clamps 7.5–8.8 / 1.55–2.1
  vol_ratio_max: 0.71    # ≈ 45d bottom tercile
```

### Code (`tiered_atr.py`)

- `compute_vol_ratio(closes, j, short=24, long=168)` — entry-time rv ratio.
- `build_paths(..., return_vol=True)` — parallel vol list.
- `dynamic_atr_pick`:
  - if `vol_ratio_min/max` set and **current** ratio outside band → return `None`
    (fail-closed: heavy brick pick kept; no live ATR override);
  - walk uses only in-band paths;
  - cache key includes the vol band (no 6h leak across gate edits).

Default: **no gate** (`vol_ratio_min/max` unset) for every other symbol.

---

## 5. Heavy sweep vs this research (they are not the same)

| | Heavy `dynamic_atr_pick` | This research |
|---|---|---|
| Window | `lookback_days` **10** | **45** days |
| Search | fixed YAML grid + clamps | free 0.25→0.05→0.01 lattice |
| Regimes | none (pooled) | `vol_ratio` / range / disp terciles |
| Stability | both halves EV>0 | both halves + wr band + **OOS 70/30** |
| Hypothesis | — | post-absmax unconstrained (diag only) |
| Shared | `walk()` first-passage, 48-bar, cost 0.1 | same |

Heavy **does** re-walk recent signals every daily sweep and can update the grid
pick — but it does **not** vol-split, free-search, or OOS-validate. Ship the
research cells as **by_symbol clamps** (narrow lattice around the 45d optima);
re-run `vol_atr_solver.py` as settles land.

---

## 6. Kill / rollback

- BTC gate off: delete `vol_ratio_max` under `BTCUSD` (falls to pooled/global —
  which has **no** balanced cell; expect heavy bricks).
- XAU back: restore `0.91/0.71` only if 45d evidence is rejected (not recommended).
- Gate code: `vol_ratio_min/max` absent = pre-change behaviour.

---

## 7. Evidence paths

- `worklog/kairos/reports/20260930_vol_regime_atr.md` — constrained solves
- `worklog/kairos/reports/20260930_vol_regime_diag.md` — unconstrained / OOS / legacy TP
- `worklog/kairos/reports/20260930_per_symbol_atr.md` — prior 10d per-symbol table
- `scripts/vol_atr_solver.py`, `scripts/vol_atr_diag.py`
