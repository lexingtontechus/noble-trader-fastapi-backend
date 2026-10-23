---
# Bracket resolution — outcome, R, end bar

`brackets.py` resolves one bracket to one of three outcomes: **tp, sl, or expire**,
returning `(outcome, R, end_j)`. It is faithful to the repo's settlement basis
so the head-to-head proof measures the *same event* Kairos settles on.

## The resolve() contract

```python
resolve(entry_i, direction, tp_atr, sl_atr, expiry_bars,
        high, low, close, atr)
# returns: (outcome, R, end_j)
```

- **entry** = `close[i]` (the stale-close policy — deliberately kept for the
  head-to-head proof, verdict W-G1).
- **tp_dist** = `tp_atr * atr[i]`; **sl_dist** = `sl_atr * atr[i]` (ATR-scaled,
  so geometry is in R-multiples of risk).
- **horizon** = `min(i + expiry_bars, n-1)` — capped at the series end.

## Outcome semantics (intrabar, SL-first)

| outcome | long direction (+1) | short direction (-1) | R |
|---|---|---|---|
| `tp` | `low[j] >= tp_price`? no — check SL first: `low[j] <= sl_price`? | mirror | `+tp_atr/sl_atr` (the RR ratio) |
| `sl` | `low[j] <= sl_price` (SL first on same-bar double touch) | `high[j] >= sl_price` | `-1.0` |
| `expire` | mark-to-market at `close[last]` | `close[last]` | signed MTM in SL-distance units |

**SL-first on same-bar double touches** — the repo's own intrabar basis
(`eod_resolver.py:820-849`, `tiered_atr.py:97-98`). If a single bar touches
both barriers, SL wins. Same-sub-step double touches in MC count as neither
(repo convention, documented in `barrier.py`).

## R multiples

- TP hit: `R = +tp_atr / sl_atr` (the configured RR).
- SL hit: `R = -1.0` (one unit of risk lost).
- Expiry: `R = (close[last] - entry) / sl_dist` (long) or `(entry - close[last]) / sl_dist` (short) — signed mark-to-market in SL-distance units, can be fractional.

## The slot grid + no-overlap walk

```python
slot_grid(n, warmup=720, step=24)   # entry every 24 bars from bar 720
```

- Warmup 720 (>= max(200, 720)) — the HMM needs 200 bars to fit; 720 ensures
  feature stability (ER/choppiness/ADX warm-up + regime label settling).
- Step 24 = one day of hourly bars.

**No-overlap rule** (`walk_book`): while the previous taken trade is unresolved
(`end_j >= current slot`), the slot is skipped. A trade's horizon is `[entry, end_j]`,
so two live trades never coexist on the grid. Abstain slots (`decide_fn -> None`)
contribute no trade but also don't block.

## Why this basis matters

The corrected-vs-kairos-vs-null backtest in `books.py` runs all three books on
the SAME slot grid with the SAME `resolve()`. That is what makes the DM test a
fair fight: differences come from geometry (current band centre vs corrected
physics-constrained geometry) and from direction (debounced HMM vs
`sign(er_signed)` with abstention), not from a different settlement convention.

## Source

- `regime_edge/brackets.py` — `resolve`, `resolve_many`, `slot_grid`, `walk_book`.
- `tests/test_regime_edge.py` — `test_bracket_pure_tp`, `test_bracket_pure_sl`,
  `test_bracket_both_touch_sl_first`, `test_bracket_expiry_mtm_sign`,
  `test_bracket_short_mirror`, `test_bracket_data_end_before_expiry`.
