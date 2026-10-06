# Regime Edge — Scalable (Symbol, Signal, Regime) Qualification

**Date:** 2026-10-05
**Status:** design → build
**Supersedes:** the manual per-symbol macro disable (Stage 2)

---

## 1. The finding

AAPL's edge is **direction-specific** and **regime-specific**. The HMM regime label (e.g. `high_vol_bull`, `med_low_vol_bear`) is a stronger signal than the macro label (which is a constant for AAPL).

### AAPL BUY by regime (90d)

| Regime | n | WR | Edge |
|---|---|---|---|
| high_vol_bull | 75 | 81.3% | ✅ Trend |
| med_low_vol_bear | 61 | 88.5% | ✅ Mean reversion |
| low_vol_bull | 80 | 47.4% | ⚠️ Coin flip |
| med_low_vol_strong_bull | 244 | 19.5% | ❌ Overbought |
| low_vol_strong_bull | 138 | 11.6% | ❌ Overbought |

### AAPL SELL by regime (90d)

| Regime | n | WR | Edge |
|---|---|---|---|
| med_low_vol_bear | 61 | 88.5% | ✅ Trend |
| low_vol_bear | 165 | 30.3% | ❌ |
| high_vol_bear | 68 | 0.0% | ❌ |
| med_high_vol_strong_bear | 50 | 0.0% | ❌ |

### The pattern

The edge is **mean reversion** at the regime level:
- **BUY** works in `high_vol_bull` (trend) and `med_low_vol_bear` (buy the dip)
- **SELL** works in `med_low_vol_bear` (sell the rally)
- **Strong trends** (`strong_bull`, `strong_bear`) fail — the move is exhausted

This is not a geometry problem (the replay proved no stable TP/ATR edge). It's a **regime-conditional qualification** problem.

---

## 2. Design principles

1. **Derived, not manual.** The edge table is computed from `nt_signal_sim` outcomes, not hand-configured. It adapts as the regime distribution changes.

2. **Scalable by (symbol, signal, regime).** The edge is a 3-tuple, not a per-symbol flag. AAPL's buy edge is different from its sell edge.

3. **Fail-open.** If a (symbol, signal, regime) bucket has n < 20, the gate does not reject — it has no evidence either way.

4. **Shadow first.** The edge table is computed and stamped, but does not gate qualification until the shadow period proves it discriminates.

5. **One lever at a time.** The regime edge gate is independent of the macro filter (already disabled for AAPL) and the geometry (already proven stable).

---

## 3. Architecture

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

### New table: `nt_regime_edge`

| Column | Type | Meaning |
|---|---|---|
| `symbol` | text | Symbol |
| `signal` | text | `buy` or `sell` |
| `regime` | text | HMM regime label (e.g. `high_vol_bull`) |
| `n` | int | Episode count |
| `wins` | int | TP hits |
| `losses` | int | SL hits |
| `win_rate` | float | wins / (wins + losses) |
| `expectancy_r` | float | win_rate × avg_rr - (1 - win_rate) × 1.0 |
| `computed_at` | timestamp | Last rebuild |

**Primary key:** `(symbol, signal, regime)`

### Config

```yaml
# config/feature_flags.yaml
regime_edge_mode: shadow  # off | shadow | live
regime_edge:
  min_n: 20           # minimum episodes to gate
  min_win_rate: 0.50  # reject if WR below this
  rebuild_interval_hours: 24  # how often to rebuild the edge table
```

### Gate logic (in `_process_symbol`)

```python
# After all existing gates pass, before qualification:
if get_regime_edge_mode() == "live":
    edge = get_regime_edge(symbol, signal, regime_label)
    if edge and edge["n"] >= min_n and edge["win_rate"] < min_win_rate:
        # Reject — this (symbol, signal, regime) bucket has negative edge
        gate_outcome = "regime_edge_reject"
        suppress_reason = f"regime_edge: {symbol} {signal} {regime_label} WR={edge['win_rate']:.1%} < {min_win_rate:.1%}"
```

---

## 4. Implementation stages

| Stage | What | Gate |
|---|---|---|
| **E0** | Build `regime_edge.py` — compute edge from `nt_signal_sim` | Unit test: edge table matches manual computation |
| **E1** | Wire into orchestrator as shadow stamp (no gate) | Shadow: ≥200 stamps, edge table populates |
| **E2** | **PROMOTE** to live gate or **RETIRE** | Binary endpoint |

### Pass bar for E2

The regime edge gate must show that:
1. Rejected buckets (WR < 50%) have **worse** outcomes than the gate predicts
2. Passed buckets (WR ≥ 50%) have **better** outcomes than the gate predicts
3. The gate improves overall expectancy vs no gate

If the gate does not discriminate, it is retired.

---

## 5. Why this is the right frame

The macro filter was a **constant** for AAPL — it never changed, so it couldn't provide a signal. The regime label is a **variable** — it changes with the market, and the edge is direction-specific.

The key insight: **the HMM regime is the signal, not the noise.** The macro filter was trying to add "long-term context" to a signal that is already regime-conditional. The regime edge gate uses the regime directly, without the macro filter's horizon mismatch.

---

## 6. Risks

1. **The edge may not be stable.** AAPL's regime distribution changes over time. The edge table must be rebuilt frequently (daily or weekly).

2. **The edge may not generalize.** AAPL's edge may be specific to the current market regime. Other symbols may have different edges.

3. **The gate may be too aggressive.** If the edge table is too granular (many buckets with n < 20), the gate may reject too many signals. Mitigation: the `min_n` threshold.

4. **The gate may be too conservative.** If the edge table is too coarse (few buckets), the gate may not discriminate. Mitigation: the `min_win_rate` threshold.

5. **The edge may be a regime artifact.** The 90d window may not be representative. Mitigation: the edge table should be rebuilt on a rolling window.

---

## 7. Endpoint

**Binary:** Either the regime edge gate is live and improves overall expectancy, or it is retired and the macro filter remains disabled for AAPL.

The regime edge is the **right clock** for a 22-40h signal. The macro filter was the wrong clock. The geometry is not the problem. The regime is the signal.
