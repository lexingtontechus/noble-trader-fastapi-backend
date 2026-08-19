# HMM 5→3 State Collapse & Markov Impact on Renko Brick Pricing

## Quick Reference

**What causes the 5→3 collapse**: `STATE_MAP_THRESHOLD_STDS = 0.25` creates a very narrow mapping boundary. When all 5 HMM states cluster near zero log-returns, multiple states map to the same 3-state label (UP/DOWN/FLAT), producing degenerate rows in the aggregated 3×3 transition matrix.

**State mapping** (`model.py:194-201`):
```python
threshold = STATE_MAP_THRESHOLD_STDS * lr_std  # 0.25 * std(log_returns)
state_map[i] = "UP" if mean_lr[i] > threshold
state_map[i] = "DOWN" if mean_lr[i] < -threshold  
state_map[i] = "FLAT" otherwise
```

**Impact on Markov**: When a 5-state HMM produces 4 FLAT + 1 UP states, the 3×3 T matrix has:
- UP row: receives all transition mass (valid)
- DOWN row: all zeros → degenerate
- FLAT row: all zeros → degenerate

**Impact on Renko pricing**: **NONE**. Brick sizing (`brick_size × sl_bricks`, `brick_size × tp_bricks`) is computed independently in `_compute_trade_signal()`. The EV fixes only affect `p_win`, `ev`, and `kelly_f` — not geometric entry/SL/TP pricing.

## EV Engine Blend Formula

```
logit(P_win) = Σ w_i × logit(p_i)
```

With p_pattern=None (server-side), weights renormalize:
- w_regime = 0.25/0.70 = 0.357
- w_markov = 0.20/0.70 = 0.286  
- w_timesfm = 0.25/0.70 = 0.357

## Fix Values Applied

| Fix | File | Old | New |
|-----|------|-----|-----|
| Regime cap | ev_engine.py:410 | 1.0 (uncapped) | 0.97 |
| Markov degenerate | ev_engine.py:235 | return 0.5 | return 0.0 (absorbing) |
| Bootstrap filter | model.py:365 | `closes[closes > 0]` | `closes` |
