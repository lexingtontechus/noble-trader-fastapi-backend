# Reopt Validation: Cell Survival Criteria

## The Two Modes

| mode | where used | result |
|---|---|---|
| `allow_best_effort=True` | Report generation (reopt.py line `select_tp_sl(..., True)`) | Returns the max-EV cell even if second half is negative |
| `allow_best_effort=False` | Strict selection (picker, replay verification) | Returns `None` if `e2 < 0` (second half expected value not positive) |

## Scoring Procedure

```python
# 1. Build envelopes for the symbol set
env, _ = await _paths_for(syms, members, since, until)

# 2. Score the candidate cell
pnl, exp = walk_envelopes(env, tp, sl, live_same_bar_policy())
n = len(env)
mid = n // 2
e1 = pnl[:mid].mean()
e2 = pnl[mid:].mean()

# 3. Strict bar check
if e2 > 0:
    # Cell SURVIVES the bar
else:
    # Cell FAILS — would be None from select_tp_sl(False)
```

## Drift Gate

The reopt only writes clamps when the pick differs **> 15%** from the shipped band. This is implemented in `attempts_to_apply()`:

```python
if abs(pick_rr - current_rr) > 0.15 * current_rr:
    # fire the write
else:
    # skip — within drift tolerance
```

## Interpretation of Results

- **Positive total EV (EV1+E2)** with **negative E2** → first-half edge only, does NOT qualify
- **Positive E2** → both halves positive, SURVIVES the strict bar
- **Expired cells** (`exp.mean()`) > 5% → watch for pathologic expiry

## Example: BTCUSD 15.19/2.11

```
n=236  tp_hits=41  sl_hits=181  expired=14  wr=0.185  expiry=5.9%
EV=+0.921  e1=+1.180  e2=+0.661  -> BOTH HALVES POSITIVE: SURVIVES
```

## Example: forex 9.75/1.39

```
n=1041  tp_hits=162  sl_hits=873  expired=6  wr=0.157  expiry=0.6%
EV=+0.252  e1=+0.850  e2=-0.346  -> E2 NEGATIVE: FAILS strict bar
```

The report shows this because reports use `allow_best_effort=True`. `select_tp_sl(..., False)` returns `None`.