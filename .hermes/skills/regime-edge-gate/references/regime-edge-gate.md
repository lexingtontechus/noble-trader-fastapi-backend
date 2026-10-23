# Regime Edge Gate — Per-(Symbol, Signal, Regime) Qualification

## When to use

When a symbol's edge is **direction-specific** and **regime-specific** — the HMM regime label (e.g. `high_vol_bull`, `med_low_vol_bear`) is a stronger signal than any macro/indicator filter. The edge is a 3-tuple: (symbol, signal, regime).

## Architecture

```
nt_signal_sim (resolved rows)
        │
        ▼
┌─────────────────────────────────────┐
│ regime_edge.py                      │  → nt_regime_edge
│ (compute edge per (symbol, signal,   │
│  regime) from outcomes)             │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ orchestrator._process_symbol()      │
│ (gate: if edge exists and WR < 50%  │
│  → reject; if n < 20 → fail-open)   │
└─────────────────────────────────────┘
```

## Table: `nt_regime_edge`

| Column | Type | Meaning |
|---|---|---|
| `symbol` | text | Symbol |
| `signal` | text | `buy` or `sell` |
| `regime` | text | HMM regime label |
| `n` | int | Episode count |
| `wins` | int | TP hits |
| `losses` | int | SL hits |
| `win_rate` | float | wins / n |
| `expectancy_r` | float | win_rate × avg_rr - (1 - win_rate) × 1.0 |
| `computed_at` | timestamp | Last rebuild |

**Primary key:** `(symbol, signal, regime)`

## Config

```yaml
regime_edge_mode: shadow  # off | shadow | live
regime_edge:
  min_n: 20           # minimum episodes to gate
  min_win_rate: 0.50  # reject if WR below this
  rebuild_interval_hours: 24
```

## Gate logic

```python
# In _process_symbol, after all existing gates pass:
if get_regime_edge_mode() == "live":
    edge = await get_regime_edge(symbol, signal, regime_label)
    reject, reason = should_reject_by_regime_edge(
        edge, min_n=20, min_win_rate=0.50
    )
    if reject:
        gate_results["regime_edge_ok"] = False
        suppress_reason = reason
```

**Fail-open:** if edge is None or n < min_n, pass (no evidence either way).

## Pass bar for promotion

1. Rejected buckets (WR < 50%) have **worse** outcomes than predicted
2. Passed buckets (WR ≥ 50%) have **better** outcomes than predicted
3. The gate improves overall expectancy vs no gate

## AAPL case study

| Signal | Regime | n | WR | Edge |
|---|---|---|---|---|
| BUY | high_vol_bull | 75 | 81.3% | ✅ Trend |
| BUY | med_low_vol_bear | 61 | 88.5% | ✅ Mean reversion |
| BUY | med_low_vol_strong_bull | 244 | 19.5% | ❌ Overbought |
| BUY | low_vol_strong_bull | 138 | 11.6% | ❌ Overbought |
| SELL | med_low_vol_bear | 61 | 88.5% | ✅ Trend |
| SELL | low_vol_bear | 165 | 30.3% | ❌ |
| SELL | high_vol_bear | 68 | 0.0% | ❌ |

The edge is **mean reversion** at the regime level: buy the dip (bear regime), sell the rally (bull regime). Strong trends (`strong_bull`, `strong_bear`) fail — the move is exhausted.

## Diagnosis chain for "constant indicator" problems

When a filter (macro, trend, etc.) appears inverted or useless for a symbol:

1. **Check if the indicator is a constant.** If the label never changes (e.g. always "bull"), it's not a signal — it's noise. Compute the label's unique values over the signal window.
2. **Check the horizon ratio.** `indicator_timeframe / signal_resolution`. If > 50×, the indicator is likely a constant.
3. **Run a geometry replay.** Test TP/ATR × SL/ATR combos against resolved signals. If no combo has both EV_h1 > 0 AND EV_h2 > 0, the edge is regime-dependent, not geometry-dependent.
4. **Build a regime edge table.** If the edge is regime-dependent, use the HMM regime label directly as the qualification signal.
5. **Disable the constant filter per-symbol.** Add `by_symbol: {SYM: {enabled: false}}` to config.

## Risks

1. **The edge may not be stable.** The regime distribution changes over time. Rebuild frequently.
2. **The edge may not generalize.** AAPL's edge may be specific to the current market regime.
3. **The gate may be too aggressive.** If the edge table is too granular (many buckets with n < 20), the gate may reject too many signals.
4. **The edge may be a regime artifact.** The 90d window may not be representative. Use a rolling window.
