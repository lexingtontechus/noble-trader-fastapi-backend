# AAPL Macro Regime — Definitive Path to Resolution

**Date:** 2026-10-05
**Roadmap:** ID 3 (`macro_regime_mode`)
**Status:** reframe — the problem is not what we thought it was

---

## 1. The actual problem (reframed)

**What we thought:** The macro filter is inverted for AAPL — trading WITH the 200-day SMA trend loses money, trading AGAINST it earns money.

**What's actually happening:** The 200-day SMA is a **constant** for AAPL, not a signal. It has been "bull" for the entire trading history. The "inversion" is an artifact of comparing a constant (macro=bull) with a variable (HMM regime).

### The data

| Metric | Value |
|---|---|
| AAPL 200-day SMA score | +15.2% (bull, saturated) |
| AAPL macro label history | Always "bull" — never crosses the 5% threshold |
| AAPL median hours to resolve | 40h (aligned), 22h (opposed) |
| Horizon mismatch | 200 days = 4,800h vs 22-40h resolution = **120-220×** |

### Why the "inversion" is an artifact

When macro is always "bull":
- "Aligned" = HMM says bull (macro agrees with HMM)
- "Opposed" = HMM says bear (macro disagrees with HMM)

So the "inversion" is really: **HMM bear is a better time to buy AAPL than HMM bull**. This is mean reversion — AAPL tends to bounce after pullbacks (HMM bear), and tends to stall after rallies (HMM bull). The 200-day SMA is irrelevant to this dynamic.

### The regime distribution proves it

| Regime | Signals | Win Rate | Interpretation |
|---|---|---|---|
| high_vol_bull | 75 | 81.3% | Strong trend — works |
| med_low_vol_bear | 61 | 88.5% | Pullback — mean reversion works |
| low_vol_bull | 80 | 47.4% | Weak rally — coin flip |
| med_low_vol_strong_bull | 244 | 19.5% | Overbought — stalls |
| low_vol_strong_bull | 138 | 11.6% | Overbought — stalls |
| high_vol_bear | 68 | 0.0% | Crash — don't catch |
| med_high_vol_strong_bear | 50 | 0.0% | Crash — don't catch |

The HMM regime is the **real** signal. The macro label is noise.

---

## 2. What we tried and why it didn't work

| Approach | Why it failed |
|---|---|
| **Debounce** | The macro label is stable (always "bull"), not noisy. Debounce fixes noise, not constants. |
| **Dynamic ATR** | This is a macro/clock problem, not a geometry problem. ATR optimizes TP/SL distance, not time horizon. |
| **Reliability filter (2026-09-14)** | Showed AAPL rests on a single suspicious cell (n=35, 100% win). But the real issue is the constant macro label, not a thin cell. |
| **Per-symbol override** | Doesn't exist in the current architecture (single global flag). But even if it did, the problem is the clock, not the threshold. |

---

## 3. The definitive path

### Stage 0: Prove the horizon mismatch (CHEAPEST — kills the whole thing if wrong)

**Question:** Does a shorter macro lookback (5-day, 10-day, 20-day) produce a *variable* label for AAPL that correlates with outcomes better than the 200-day constant?

**Method:** Rebuild `nt_macro_edge_by_symbol` with SMA windows of 5/10/20/50/100/200 days. For each window, compute the aligned/opposed win rate for AAPL. If any window produces a label that (a) actually changes over time AND (b) shows aligned > opposed, the horizon mismatch is confirmed and the fix is a shorter window.

**Gate:** If NO window produces aligned > opposed for AAPL, the macro filter is fundamentally broken for this symbol → disable it per-symbol and move on.

**Cost:** One SQL query + one Python script. No code changes.

### Stage 1: Per-symbol macro lookback (if Stage 0 confirms)

**Concept:** Replace the global 200-day SMA with a per-symbol lookback matched to the signal resolution horizon.

```yaml
# config/feature_flags.yaml
macro_regime_mode: shadow
macro:
  sma_window_days: 200  # global default
  by_symbol:
    AAPL: {sma_window_days: 10}   # ~240h — closer to 40h resolution
    BTCUSD: {sma_window_days: 20}  # ~480h — closer to 35h resolution
    # ... per-symbol overrides
```

**Derivation:** The lookback should be ~5-10× the median hours-to-resolve. For AAPL (40h), a 10-day SMA = 240h = 6× resolution. For XAUUSD (4h), a 2-day SMA = 48h = 12× resolution.

**Gate:** The per-symbol lookback must produce a label that (a) changes at least once per week AND (b) shows aligned > opposed on a reliable cell (n≥20).

### Stage 2: Per-symbol macro enable/disable (if Stage 0 fails)

**Concept:** If the macro filter is fundamentally broken for a symbol (no lookback produces aligned > opposed), disable it for that symbol.

```yaml
macro:
  by_symbol:
    AAPL: {enabled: false}  # macro filter disabled — HMM regime is the signal
```

**Gate:** The disabled symbol's win rate must not degrade (it shouldn't — the macro multiplier is a no-op when the label is constant).

### Stage 3: **PROMOTE** or **RETIRE**

**Binary endpoint:**
- If Stage 1 produces a per-symbol lookback that works → promote `macro_regime_mode: live` with per-symbol config
- If Stage 0 shows no lookback works → retire the macro filter for AAPL (and any other constant-label symbols) and promote for the rest

---

## 4. Why this is the right frame

The 200-day SMA was chosen as a default because it's a common "long-term trend" indicator. But the platform's signals resolve in 22-40 hours. A 200-day clock is 120-220× slower than the signal horizon. This is like using the annual rainfall to decide whether to carry an umbrella.

The HMM regime (4-state, hourly) is the **right clock** for a 22-40 hour signal. The macro filter was designed to be a "long-term context" layer, but for symbols where the long-term trend is a constant, it adds noise, not context.

The fix is not to tune the threshold or debounce the label — it's to **match the clock to the signal**.

---

## 5. Stage 0 results (2026-10-05)

**Script:** `scripts/stage0_aapl_macro.py` — fetches 300 daily bars, computes rolling SMAs, tests 6 windows against 1,000 AAPL signals.

| Window | Aligned n | Aligned WR | Opposed n | Opposed WR | Gap | Verdict |
|---|---|---|---|---|---|---|
| 5 | 0 | — | 0 | — | — | ❌ All neutral |
| 10 | 0 | — | 0 | — | — | ❌ All neutral |
| 20 | 0 | — | 0 | — | — | ❌ All neutral |
| 50 | 385 | 10.4% | 83 | 74.7% | -64.3% | ❌ Inverted |
| 100 | 591 | 29.6% | 383 | 28.7% | +0.9% | ⚠️ Constant |
| 200 | 591 | 29.6% | 383 | 28.7% | +0.9% | ⚠️ Constant |

### Analysis

**The SMA-100/200 "win" is an artifact.** Both windows produce identical aligned/opposed counts (591/383) and identical win rates. This means both SMAs produce the same constant label ("bull") for the entire 90-day signal window. The +0.9% gap is noise on a constant — not a signal.

**Short windows (5/10/20) are always neutral.** Too noisy — oscillate around SMA, never cross the 5% threshold. No signal.

**Medium window (50) is inverted.** Opposed wins 74.7% vs aligned 10.4%. Mean reversion dominates — AAPL bounces after pullbacks, stalls after rallies.

**Long windows (100/200) are constant.** AAPL has been in a steady uptrend — both SMAs below price for the entire window. No variation.

### Definitive conclusion

**The macro filter is not fixable for AAPL by changing the lookback.** No SMA window produces a reliable aligned > opposed signal. The 200-day SMA is a constant for AAPL, not a signal. The HMM regime is the correct clock for a 22-40 hour signal.

**Path forward:** Stage 2 — disable macro filter for AAPL. The macro multiplier becomes a no-op, and the HMM regime drives sizing as it should.

---

## 6. Risks

1. **Shorter lookback may be noisier.** A 5-day SMA crosses the threshold more often, which could reintroduce the noise problem. Mitigation: the reliability filter (already built) can be applied to the shorter window.

2. **Per-symbol config adds complexity.** Each symbol needs its own lookback. Mitigation: derive it automatically from the median hours-to-resolve (Stage 1 derivation rule).

3. **The macro filter may be useless for ALL symbols.** If the 200-day SMA is a constant for most symbols (not just AAPL), the entire macro filter is noise. Mitigation: Stage 0 should test all 14 symbols, not just AAPL.

4. **Disabling macro for some symbols may be wrong.** The macro filter may work for some symbols (XAGUSD confirmed the assumption in the 2026-09-14 analysis). Mitigation: per-symbol enable/disable, not global.

---

## 7. Endpoint

**Binary:** Either the macro filter is promoted with per-symbol horizon-matched lookbacks, or it is retired for symbols where it's a constant. No limbo, no "keep trying different thresholds."

The 200-day SMA is the wrong clock for a 22-40 hour signal. Fix the clock or kill the filter.
