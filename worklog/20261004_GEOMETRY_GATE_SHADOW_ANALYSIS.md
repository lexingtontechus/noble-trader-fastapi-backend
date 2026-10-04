# 2026-10-04 — Qualified Quality Addons D1/D2: Geometry Gate Shadow Analysis

**Roadmap ID:** 56
**Mechanism:** `qualified_quality_addons_D1_D2`
**Status:** Assessment complete — per-asset-class thresholds recommended
**Date:** 2026-10-04
**Analyst:** Hermes Agent

---

## 1. Context

The geometry gate shadow (`bracket_gate_shadow`) has been stamping since 2026-09-16 (migration 154). The roadmap condition was:

> ASSESS ONLY AFTER B shadow has volume. Trigger: (1) B live or B shadow reject-rate table exists; (2) >=50 resolved qualified lights OR >=1-2 weeks of stamps.

This worklog assesses whether the geometry gate is ready for live promotion, and if so, at what threshold.

---

## 2. Data

| Metric | Value |
|---|---|
| Total `bracket_gate_shadow` stamps | 11,710 |
| Date range | 2026-09-16 → 2026-10-04 (18 days) |
| Symbols covered | 14 (all) |
| Qualified + resolved rows | 1,655 |
| Shadow would-reject | 849 (5.1%) |
| Shadow would-accept | 806 (4.9%) |

---

## 3. Pooled Results

| Metric | Value |
|---|---|
| Overall reject rate | 5.1% (849/1,655) |
| True positive (rejected → loss/expire) | 92.1% (782/849) |
| False positive (rejected → would win) | 7.9% (67/849) |
| Precision (rejected → would loss) | 56.6% (481/849) |

**Reading:** The geometry gate is **highly precise** — 92.1% of rejected signals would have lost or expired. But the false positive rate (7.9%) means ~67 winning signals would be sacrificed.

---

## 4. Per-Symbol Results

| Symbol | Qualified | Rejected | Reject % | Would Win (FP) | Would Loss | Would Expire | FP % |
|---|---|---|---|---|---|---|---|
| USDJPY | 53 | 43 | 81.1% | 7 | 11 | 25 | 16.3% |
| XAUEUR | 199 | 154 | 77.4% | 6 | 142 | 6 | 3.9% |
| US30USD | 88 | 57 | 64.8% | 4 | 43 | 10 | 7.0% |
| EURUSD | 117 | 71 | 60.7% | 0 | 2 | 69 | **0.0%** |
| USDCAD | 149 | 75 | 50.3% | 18 | 11 | 46 | **24.0%** |
| XAUAUD | 173 | 72 | 41.6% | 0 | 69 | 3 | **0.0%** |
| XAUUSD | 183 | 71 | 38.8% | 1 | 52 | 18 | 1.4% |
| GBPUSD | 268 | 89 | 33.2% | 8 | 76 | 5 | 9.0% |
| AAPL | 127 | 37 | 29.1% | 9 | 28 | 0 | **24.3%** |
| US500 | 90 | 26 | 28.9% | 7 | 17 | 2 | **26.9%** |
| USDCHF | 71 | 16 | 22.5% | 0 | 1 | 15 | **0.0%** |
| AUDUSD | 179 | 20 | 11.2% | 4 | 0 | 16 | **20.0%** |
| XAGUSD | 95 | 9 | 9.5% | 1 | 7 | 1 | 11.1% |
| BTCUSD | 153 | 8 | 5.2% | 2 | 5 | 1 | **25.0%** |

**5 symbols have >20% false positive rate** — a blanket live flip would sacrifice good signals on USDCAD, AAPL, US500, AUDUSD, BTCUSD.

---

## 5. Per-Asset-Class Results

| Asset Class | n | Reject % | FP % | TP Loss % | TP Expire % | Verdict |
|---|---|---|---|---|---|---|
| **Metals** | 650 | 47.1% | **2.6%** | 88.3% | 9.2% | ✅ Ready for live |
| **Forex majors** | 837 | 37.5% | **11.8%** | 32.2% | 56.0% | ⚠️ Moderate — needs threshold tuning |
| **Equities** | 305 | 39.3% | **16.7%** | 73.3% | 10.0% | ❌ Too aggressive |
| **Crypto** | 153 | 5.2% | **25.0%** | 62.5% | 12.5% | ❌ Not useful |

**Key finding:** The geometry gate is **highly precise on metals** (2.6% FP) but **too aggressive on equities** (16.7% FP). A single threshold cannot serve all asset classes.

---

## 6. Comparison to Dynamic ATR

The per-asset-class pattern mirrors `dynamic_atr.py`'s per-symbol ATR approach:

| Mechanism | Granularity | Approach |
|---|---|---|
| `dynamic_atr.py` | Per-symbol ATR | Each symbol gets its own brick size based on its own ATR |
| Geometry gate (proposed) | Per-asset-class threshold | Each asset class gets its own `min_p_win_bracket` threshold |

The geometry gate should follow the same pattern: **per-asset-class thresholds**, not a single global threshold.

---

## 7. Recommendation

**Do NOT flip to live with a single global threshold.**

**Recommended path:**

1. **Metals** (XAUUSD, XAGUSD, XAUEUR, XAUAUD): Flip to live immediately — 2.6% FP is acceptable
2. **Forex majors** (EURUSD, USDCHF, GBPUSD, AUDUSD, USDJPY, USDCAD): Raise threshold to P75 or P80 (rejects fewer signals, reduces FP)
3. **Equities** (AAPL, US500, US30USD): Keep shadow — 16.7% FP is too high; needs different approach (maybe geometry-risk-only, not bracket gate)
4. **Crypto** (BTCUSD): Keep shadow — 25.0% FP means the gate is not useful for crypto

**Implementation:** Add `min_p_win_bracket_by_asset_class` config (similar to `nt_symbol_lot_multiplier`), with per-class thresholds. Default to shadow for classes not yet promoted.

---

## 8. Next Steps

1. Build per-asset-class threshold config (feature_flags.yaml + feature_flags.py)
2. Add asset-class-aware gate logic in orchestrator
3. Promote metals to live, keep others shadow
4. Re-assess after 7d of per-class live data

---

## 9. Related

- `worklog/20260913_PHASE_3B_GRID_WIDENING_SCOPE.md` — original geometry gate scope
- `worklog/20260924_B5_VETO_SPLIT_SCOPE.md` — B5 veto split (geometry-risk only)
- `supabase/migrations/154_nt_signal_sim_p_win_bracket.sql` — bracket gate shadow stamp
- `supabase/migrations/172_scorecard_p_win_dir_update.sql` — scorecard view updates
