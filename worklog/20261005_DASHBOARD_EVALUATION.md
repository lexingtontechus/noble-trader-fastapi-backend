# Dashboard Evaluation — Renko Chart & User Caution

**Date:** 2026-10-05
**Status:** evaluation + prescription
**Context:** regime_edge table built (218 buckets), shadow mode active

---

## 1. Edge table build — confirmed

`nt_regime_edge` populated: **218 buckets** from 46,034 resolved rows.

### AAPL edge (15 buckets)

| Signal | Regime | n | WR | Edge |
|---|---|---|---|---|
| buy | high_vol_bull | 93 | 65.6% | ✅ +0.78R |
| buy | high_vol_strong_bull | 254 | 18.5% | ❌ -0.45R |
| buy | low_vol_bull | 1,060 | 35.8% | ❌ -0.00R |
| buy | low_vol_strong_bull | 3,251 | 8.8% | ❌ -0.74R |
| buy | med_high_vol_bull | 38 | 21.1% | ❌ -0.23R |
| buy | med_high_vol_strong_bull | 32 | 9.4% | ❌ -0.72R |
| buy | med_low_vol_bull | 37 | 18.9% | ❌ -0.44R |
| buy | med_low_vol_strong_bull | 335 | 16.1% | ❌ -0.45R |
| sell | high_vol_bear | 71 | 4.2% | ❌ -0.87R |
| sell | low_vol_bear | 437 | 50.8% | ✅ +0.69R |
| sell | low_vol_strong_bear | 258 | 10.8% | ❌ -0.68R |
| sell | med_high_vol_bear | 82 | 80.5% | ✅ +1.12R |
| sell | med_high_vol_strong_bear | 100 | 32.0% | ❌ -0.05R |
| sell | med_low_vol_bear | 77 | 79.2% | ✅ +1.38R |
| sell | med_low_vol_strong_bear | 30 | 16.7% | ❌ -0.34R |

**AAPL: 4 buckets with edge, 11 without.** The edge is direction-specific and regime-specific.

### All symbols — edge is rare

| Symbol | Buckets | WR≥50% | WR<50% |
|---|---|---|---|
| AAPL | 15 | 4 | 11 |
| AUDUSD | 14 | 0 | 14 |
| BTCUSD | 16 | 2 | 14 |
| EURUSD | 15 | 1 | 14 |
| GBPUSD | 15 | 1 | 14 |
| US30USD | 16 | 3 | 13 |
| US500 | 16 | 1 | 15 |
| USDCAD | 15 | 2 | 13 |
| USDCHF | 16 | 0 | 16 |
| USDJPY | 16 | 2 | 14 |
| XAGUSD | 16 | 2 | 14 |
| XAUAUD | 16 | 1 | 15 |
| XAUEUR | 16 | 0 | 16 |
| XAUUSD | 16 | 0 | 16 |

**Only 17 of 218 buckets (7.8%) have WR ≥ 50%.** The edge is rare and specific.

---

## 2. Dashboard renko chart evaluation

### What the dashboard shows

The `RenkoSection` component (`app/dashboard/page.jsx:562-810`) renders:
- **10 most recent renko bricks** from `v_talaria_renko` (live grid)
- **Entry/SL/TP levels** from `v_talaria_latest_signals`
- **Direction badge** (LONG·BUY / SHORT·SELL)
- **Market proxy** (last brick close)
- **Brick size** and grid info

### What the dashboard does NOT show

| Missing | Impact |
|---|---|
| **Regime edge** | User doesn't know if this (symbol, signal, regime) bucket has historical edge |
| **Regime label** | User doesn't know which HMM regime the signal fired in |
| **Edge confidence** | User doesn't know how many episodes support the edge |
| **Geometry fit** | User doesn't know if the TP/SL bracket is appropriate for the regime |
| **Caution indicator** | No warning when the signal is in a negative-edge bucket |

### The usability problem

The renko chart shows **price action** (bricks) and **trade levels** (entry/SL/TP), but it does not show **signal quality**. A user looking at the chart cannot distinguish:
- A signal in `high_vol_bull` (65.6% WR, +0.78R edge) from...
- A signal in `low_vol_strong_bull` (8.8% WR, -0.74R edge)

Both look identical on the chart — same bricks, same entry/SL/TP levels. But one has a real edge and the other is a coin flip at best.

---

## 3. Prescription — user caution layers

### Layer 1: Regime edge badge (on the renko chart)

Add a **regime edge badge** to the `RenkoSection` component, next to the direction badge:

```
[LONG · BUY] [REGIME: high_vol_bull] [EDGE: ✅ +0.78R (n=93)]
```

or

```
[SHORT · SELL] [REGIME: low_vol_strong_bear] [EDGE: ❌ -0.68R (n=258)]
```

**Data source:** `nt_regime_edge` table, joined on `(symbol, signal, regime)`.

**Implementation:**
- Add `regime_edge` to the `dashboard-data` API response
- Add a `RegimeEdgeBadge` component to the dashboard
- Color: green (WR ≥ 50%), red (WR < 50%), gray (no data)

### Layer 2: Caution note (below the chart)

Add a **caution note** when the signal is in a negative-edge bucket:

```
⚠️ This signal is in a regime with negative historical edge
   (low_vol_strong_bear: 8.8% WR, -0.74R over 3,251 episodes).
   Consider reducing size or skipping.
```

**Implementation:**
- Add a `CautionNote` component below the renko chart
- Only shows when `regime_edge.win_rate < 0.50` and `n >= 20`

### Layer 3: Regime edge summary (on the signal card)

Add a **regime edge summary** to the signal card, showing the edge for the current (symbol, signal, regime) bucket:

```
Regime Edge: +0.78R (65.6% WR, n=93)
```

or

```
Regime Edge: No data (n < 20)
```

**Implementation:**
- Add `regime_edge` to the signal card component
- Show expectancy_r, win_rate, and n

### Layer 4: Dashboard-level edge summary

Add a **dashboard-level edge summary** showing the overall edge across all symbols:

```
Regime Edge Summary:
- 17 of 218 buckets have WR ≥ 50%
- AAPL: 4/15 buckets positive
- BTCUSD: 2/16 buckets positive
- ...
```

**Implementation:**
- Add a `RegimeEdgeSummary` component to the dashboard
- Aggregate from `nt_regime_edge`

---

## 4. Alternative method — regime-conditional signal filtering

Instead of (or in addition to) caution badges, the dashboard could **filter signals by regime edge**:

- **Default view:** Show all signals (current behavior)
- **Edge-filtered view:** Show only signals in positive-edge buckets (WR ≥ 50%, n ≥ 20)
- **Toggle:** User can switch between views

**Implementation:**
- Add a `showOnlyPositiveEdge` toggle to the dashboard
- Filter `sweeps` by `regime_edge.win_rate >= 0.50` when enabled
- Default: off (show all signals)

---

## 5. Recommendation

**Immediate (this build):**
1. Add `regime_edge` to the `dashboard-data` API response
2. Add a `RegimeEdgeBadge` component to the renko chart
3. Add a `CautionNote` component below the chart

**Next build:**
4. Add a regime edge summary to the signal card
5. Add a dashboard-level edge summary
6. Add an edge-filtered view toggle

**The key insight:** The renko chart shows **what** the signal is (bricks, entry/SL/TP), but not **how good** the signal is (regime edge). The user needs both to make an informed decision.

---

## 6. Risks

1. **The edge may not be stable.** The 90d window may not be representative. Mitigation: rebuild the edge table daily.

2. **The edge may not generalize.** AAPL's edge may be specific to the current market regime. Mitigation: the edge table is rebuilt from live data.

3. **The caution may be too aggressive.** If the edge table is too granular, the caution may fire too often. Mitigation: the `min_n` threshold (20 episodes).

4. **The caution may be too conservative.** If the edge table is too coarse, the caution may not fire when it should. Mitigation: the `min_win_rate` threshold (50%).

5. **The user may ignore the caution.** The caution is advisory, not a gate. Mitigation: the caution is prominent (badge + note), not hidden.

---

## 7. Endpoint

**Binary:** Either the regime edge is surfaced on the dashboard (badge + note), or it is not. The edge table is built and the gate is shadow — the dashboard surfacing is the user-facing half.

The renko chart shows **price action**. The regime edge shows **signal quality**. The user needs both.
