---
# Gate backtest — kairos vs corrected vs null

`books.py` runs the decisive head-to-head on the SAME slot grid (every 24 bars
from warmup 720), same `resolve()`, same geometry basis. Three books:

## The three books

### kairos (faithful live repro)

- direction = debounced HMM label (`hmm_ref.py`); gates: HMM conf >= 0.60 AND
  EV > 0, where p_win = the full-sample repo-style bucket base rate measured
  on this repro's own HMM-direction trades (the measured-table approximation).
- Geometry = current band centre, expiry 48.
- This is kairos *as-is*, re-run exactly — the target of every PROVEN-FAIL verdict.

### corrected (the fix)

- direction = `sign(er_signed)` ONLY when `|er_signed| >= tau*`, else ABSTAIN.
- `tau*` chosen on the first 70% of slots by max mean-R (train split).
- Geometry = the physics-constrained corrected geometry (from `barrier.py`,
  fit on train slots only).
- Publish gate: `p_tp*(tp/sl) - p_sl - cost/sl > 0` from the walk-forward
  regime-conditional cells (no lookahead). Abstain otherwise.

### null

- direction = random +-1 (seed 7), corrected geometry, every slot.
- The driftless baseline: what a coin-flip earns under the corrected bands.

## The no-overlap rule

Each book walks the slot grid with `walk_book` — a slot is skipped while the
previous trade is unresolved (`end_j >= current slot`). This is what makes the
per-slot R-series comparable: the same slot is evaluated for all three books,
with 0 where a book abstains/skips.

## Decision metrics (`_book_metrics`)

| field | meaning |
|---|---|---|
| n | trades taken |
| tp / sl / exp | outcome shares |
| mean_r | mean R per trade |
| sharpe_r | R mean / R std |
| maxdd_r | max drawdown on cumulative-R equity |
| equity | downsampled (<=600 pts, 3dp) cumulative R curve |

## DM test (the verdict on who's better)

Source: the repo's own `vol_backtest/dm.py` — Newey-West HAC + Holm.

- `dm_test(-rk, -rc)` for kairos-vs-corrected; `dm_test(-rc, -rn)` for
  corrected-vs-null.
- Loss = -R; an abstaining book contributes R=0 at that slot.
- Both books evaluated on the SAME slot grid.
- **Sign convention: negative `mean_loss_diff` favors the first-named book.**
- `hac_lag = max(0, max_exp // 24 - 1)` (max expiry 96 / 24 - 1 = 3).

## Pooled (across symbols)

`pooled_books()` concatenates trades (symbol-blocked order) + DM on the
concatenated per-slot series. Holm family = `2 * n_symbols` per-symbol tests.

## The finding

| book | trades | R/trade (pooled) |
|---|---|---|
| kairos-as-is | 462 | **+0.2567** |
| corrected | 121 | +0.1052 |
| driftless null | 1303 | -0.0159 |

- Pooled DM kairos-vs-corrected: **p=0.0101** — but only in this drift-heavy
  2y window, and NOT per-symbol (Holm p=1.0). It is wide-TP/tight-SL payoff x
  realised drift with NEGATIVE direction skill (W-X2), not direction alpha
  — the W-G2 risk inheritance, measured.
- Corrected-vs-null: **p=0.62 (not significant)**.
- **8 of 14 symbols starve** (zero kairos trades over 2y) — EV-gate
  starvation, the gate threshold is the binding constraint, not the market.
  Both indices starve (US500 pi_E=0.70 in-symbol).

## Source

- `regime_edge/books.py` — `run_books`, `pooled_books`, `run_holm`.
- `vol_backtest/dm.py` — `dm_test`, `holm_adjust`.
- `regime_edge/edge.py` — `_cells` (the 15 trend-quintile x vol-tercile cells
  with shrinkage N0=30 that feed the walk-forward regime-conditional model).
