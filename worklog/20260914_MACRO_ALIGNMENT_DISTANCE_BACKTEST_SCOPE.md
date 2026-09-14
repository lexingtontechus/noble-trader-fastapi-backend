# Macro-Alignment × Bracket-Distance Backtest — Findings + Per-Symbol Treatment Research

> **Priority**: P2 · **Status**: analysis complete, findings documented, no code changed ·
> **Date**: 2026-09-14 · **Roadmap refs**: `macro_regime_mode`

---

## 1. What was tested

Following the XAGUSD finding (worklog same-day, prior turn) that
`nt_macro_edge_by_symbol`'s plain "opposed beats aligned" read hid a
sharper pattern when split by distance/ATR bucket, the same analysis
(alignment × bracket bucket, using the existing `_alignment_for`/
`_bucket_for` pure functions — no new code) was run against every other
symbol with enough aligned+opposed data: AAPL, BTCUSD, US30USD, US500,
XAUAUD.

**`nt_macro_edge_by_symbol` itself was found stale** (last computed
2026-09-02) and was rebuilt fresh against all 23,000+ resolved rows
before this analysis — several symbols (XAUUSD, USDJPY, EURUSD, AUDUSD,
etc.) had shown 0 rows in the table despite having thousands of resolved,
macro-labeled trades, simply because the diagnostic hadn't been re-run
in 12 days.

---

## 2. System-wide inversion scan (fresh `nt_macro_edge_by_symbol`)

| Symbol | Aligned n | Aligned Win% | Aligned ExpR | Opposed n | Opposed Win% | Opposed ExpR | Verdict |
|---|---|---|---|---|---|---|---|
| AAPL | 2,782 | 11.9% | −0.655 | 91 | 82.4% | +1.596 | INVERTED |
| BTCUSD | 901 | 28.2% | −0.257 | 1,253 | 51.2% | +0.371 | INVERTED |
| US30USD | 1,019 | 23.5% | −0.304 | 638 | 43.4% | +0.280 | INVERTED |
| US500 | 584 | 21.8% | −0.273 | 349 | 27.5% | −0.152 | INVERTED (both losing, opposed less bad) |
| XAGUSD | 1,143 | 30.1% | +0.010 | 1,338 | 57.0% | +0.712 | INVERTED |
| XAUAUD | 104 | 35.6% | +0.064 | 212 | 24.5% | −0.311 | CONFIRMED (as macro filter assumes) |
| AUDUSD/EURUSD/GBPUSD/USDCAD/USDCHF/USDJPY/XAUEUR/XAUUSD | 0 | — | — | 0 | — | — | **insufficient — 100% neutral macro reads** |

**5 of 6 measurable symbols invert the macro filter's core assumption.**
Only XAUAUD confirms it. 8 of 14 symbols show zero usable data — their
200-day-SMA read has never crossed the `bear_bull_threshold: 0.05` band
for their whole trading history, a separate open question not pursued
further here.

---

## 3. Does XAGUSD's tight-ATR fix generalize? No — checked, ruled out

XAGUSD showed: aligned stays flat (~22–27%) across every bucket; opposed
splits sharply — 88.5% win in tight buckets (0.5–2 ATR) vs. 7.5% win in
wide buckets (2–4+ ATR). The hypothesis tested: does the same
"opposed-only-wins-if-tight" interaction hold for the other 5 symbols?

| Symbol | Aligned tight | Aligned wide | Opposed tight | Opposed wide | Pattern vs. XAGUSD |
|---|---|---|---|---|---|
| XAGUSD | 26.8% (n=82) | 21.7% (n=1287) | **88.5%** (n=486) | 7.5% (n=626) | (reference) |
| AAPL | 0.0% (n=2) | 11.9% (n=2780) | 100% (n=1) | **82.2%** (n=90) | Opposed wins at the SAME bucket — not bucket-conditional |
| BTCUSD | 0.0% (n=21) | 28.7% (n=825) | 63.0% (n=27) | 51.2% (n=1085) | Opposed beats aligned in BOTH buckets — uniform |
| US30USD | 39.2% (n=79) | 20.4% (n=901) | 59.3% (n=113) | 41.5% (n=479) | Opposed beats aligned by ~20pp in BOTH — uniform |
| US500 | **50.6%** (n=241) | 0.0% (n=317) | 43.7% (n=103) | 20.7% (n=246) | **Opposite of XAGUSD** — aligned wins in tight |
| XAUAUD | 37.2% (n=94) | ~no data (n=1) | 30.9% (n=165) | ~no data (n=46) | Aligned > opposed in tight (consistent with confirmed status); wide has almost no data either side |

**Verdict: does NOT generalize.** XAGUSD's specific mechanism (opposed
wins only when the bracket is also tight) is real for XAGUSD, but US500
shows the *opposite* direction (aligned wins in tight, not opposed), and
AAPL/BTCUSD/US30USD show opposed winning *regardless* of bucket width —
the bucket dimension doesn't explain their inversion at all.

**Decision made from this: do NOT build a `(symbol, alignment,
distance_atr_bucket)` extension to `macro_edge.py`.** A shared
implementation would encode "tight fixes the inversion" as if it were a
general mechanism, when the data shows it's true for exactly one of six
symbols and false (or opposite) for the others. This is the over-
engineering trap avoided by checking generalization before building —
the robust finding is simply "aligned is losing money for most of this
symbol set," which is already visible in the un-split numbers without
any bucket dimension.

---

## 4. Research: why would per-symbol treatment legitimately differ?

Before assuming this is noise or a shared bug, checked what the academic/
practitioner literature says about momentum vs. mean-reversion by asset
class and horizon — because if the effect is well-documented as
asset/horizon-specific, that's a structural explanation, not a red flag.

### 4.1 The core academic finding: momentum and reversal are BOTH real, at different horizons

- **Jegadeesh & Titman (1993)**: 3–12 month momentum in individual US
  stocks — recent winners keep winning over that horizon.
- **De Bondt & Thaler (1985)**: 3–5 YEAR reversal — extreme losers over
  a multi-year horizon outperform extreme winners over the following
  multi-year horizon. Structural mirror image of short-term reversal,
  driven by fundamental overreaction correcting, not microstructure.
- **Moskowitz, Ooi & Pedersen (2012), "Time Series Momentum"**: tested
  the trend-follower's rule (12-month lookback) across 58 futures
  markets (stock indices, currencies, commodities, bonds) — positive in
  ALL 58. Persistence lasts ~1–12 months, then partially reverses.
- The unifying academic view (multiple sources, converging): **momentum
  dominates at short-to-intermediate horizons (weeks to ~12 months) in
  trending regimes; mean-reversion dominates at very short horizons
  (intraday/days, driven by microstructure/overreaction-correction) and
  very long horizons (3–5 years, driven by fundamental overreaction).**
  Conflating horizons is explicitly named as "one of the most reliable
  ways to build a strategy that works on paper and fails in practice."

**Why this matters for this platform specifically**: `nt_macro_regime`'s
200-day SMA is squarely in the "long-horizon" camp, while every
signal-generating mechanism here (Renko bricks off 1h candles, the HMM's
~8–12 day refit window, brackets with a 48h expiry) operates at a
*days* horizon. **The platform is structurally comparing a long-horizon
trend-following read against short-horizon trades — exactly the horizon
mismatch the literature warns produces spurious "inversions."** A short
-horizon trade opposing a 200-day trend is not necessarily "fighting the
trend" in the momentum sense; if it's a fast countertrend move within a
still-active longer trend, both the short-term reversal effect AND the
long-term momentum effect can be real and simultaneously true — they're
different phenomena at different clocks.

### 4.2 Per-asset-class findings, from the literature

**Silver/XAGUSD** — described repeatedly (multiple independent sources)
as more volatile than gold with sharper retracements, where
"mean-reversion and breakout strategies tend to outperform
trend-following on lower timeframes." This matches the data directly:
XAGUSD's opposed+tight cell (a short, sharp counter-move) is its best
performer by far (88.5% win), consistent with silver's documented
retracement-heavy character.

**Bitcoin/BTCUSD** — the literature is explicit and quantified on
horizon-dependence *within the same asset*: one 2026 study found mean-
reversion dominates on **hourly** data (Sharpe 1.86) while buy-and-hold/
trend-following dominates on **daily** data — "Bitcoin's behaviour is
mean-reverting at high frequency and trend-following at low frequency."
Separately, "Time-series momentum and market timing in Bitcoin" (2026)
found **slow signals (12-week baseline) outperform fast signals** —
fast/short-horizon signals in Bitcoin are prone to overreaction and noise.
This directly supports BTCUSD's inversion: the platform's short-horizon
signal is operating in the noisy, overreaction-prone regime the
literature says loses money in crypto, while the 200-day macro trend is
capturing the genuinely momentum-favorable long horizon.

**Stock indices (US500, US30USD)** — the literature flags something
specific and recent: **index-level short-term autocorrelation has been
*declining*, turning slightly negative**, attributed to retail options
flow and market-maker gamma hedging creating "a structural counter-trend
player" that resets daily. This is a mechanism-level explanation for why
a short-horizon signal on an index might systematically underperform a
long-horizon trend read right now — a real, documented, and relatively
recent (post-2020 options-volume growth) market-structure shift, not
necessarily a stable historical property of indices.

**Individual stocks (AAPL)** — the 3–12 month momentum literature
(Jegadeesh & Titman) is specifically about intermediate horizons; it says
nothing that predicts short-horizon (days) behavior against a long
(200-day) trend read. AAPL's inversion is the most extreme in the dataset
(aligned 11.9% vs. opposed 82.4%) — worth flagging that this could
reflect the same HMM-lag mechanism already suspected from the earlier
regime-vol-tier work (AAPL loses in extreme vol tiers, wins in middle
tiers) rather than a momentum/reversal story specifically. Not resolved
here — flagged as a candidate follow-up.

**Commodity futures (broader context, not directly this platform's
metals)** — one comprehensive study (29 commodity futures, 1979–2017)
found **momentum profitable, reversal NOT significantly profitable**
across the full commodity futures universe. XAUAUD (the one symbol
confirming the macro filter) fits this — gold/AUD may behave more like
"commodity trend-following works" than the metals with an inversion
(XAGUSD, and by extension possibly XAUUSD/XAUEUR though they had
insufficient data to test).

**Gold cross-pairs (XAUAUD specifically)** — researched why gold priced
in AUD might behave differently from gold priced in USD. XAU/AUD has a
genuinely dual-driver structure: it moves with (a) the USD gold price
and (b) the AUD/USD exchange rate — a weakening AUD *inflates* the AUD
gold price even with no USD gold move. AUD is itself a commodity currency
correlated with risk sentiment and Australian export demand. This
compound structure could plausibly *dampen* short-horizon noise relative
to a single-driver pair (the AUD leg and gold leg don't always move
together, effectively smoothing the combined series) — which would make
its long-horizon trend read comparatively more reliable relative to
short-horizon signals than a pure single-driver instrument. Directionally
consistent with XAUAUD being the one symbol where trend-following
(aligned) wins, though this is an inference from the pair's known
structure, not directly tested here.

---

## 5. Synthesis: how would the system know to invert vs. align — what indicators exist

**The honest, research-grounded answer: there is no single indicator on
this platform today that reliably predicts which regime a given symbol
is in.** The literature's own answer is that momentum/reversal dominance
is a function of **horizon** and **market microstructure**, both of
which vary by asset class and change over time (the equity
autocorrelation-decline finding is explicitly described as a recent
shift, not a timeless constant). This is a genuinely hard problem in the
literature, not a solved one this platform is missing a known trick for.

What the data + research together DO support, as candidate directions —
**none built, all requiring further validation before any code change**:

1. **Per-symbol asset-class prior, not a universal rule.** The research
   supports treating metals (mean-reversion-prone, thin single-driver
   pairs especially), crypto (short-horizon noisy, long-horizon
   trending — needs horizon-matching, not alignment/opposition), stock
   indices (recently more counter-trend at short horizons than
   historically), and compound/cross pairs (XAUAUD-style, potentially
   more trend-following-reliable) as structurally different regimes,
   consistent with `macro_regime_mode` already being `off`/`shadow`-first
   per symbol rather than a single global setting — the platform's own
   existing shadow-first, per-mechanism discipline already matches what
   the research says is necessary (don't apply one rule everywhere).

2. **Horizon-matching as the more fundamental fix than alignment/
   opposition.** The recurring theme across sources: comparing a 200-day
   read against day-scale trades is comparing two different clocks. A
   read that's closer to the platform's own actual holding period
   (median hours-to-resolve, already computed in
   `nt_macro_edge_by_symbol` — e.g. XAGUSD's ~45h) might correlate better
   than the 200-day SMA does. This is a testable hypothesis (does a
   shorter macro lookback window predict better for the inverted
   symbols?) — not attempted here, flagged as the more promising next
   research question over further bucket-splitting.

3. **BTCUSD specifically has a literature-quantified answer already**:
   slow/long signals outperform fast/short signals in Bitcoin. This is
   the most directly actionable of the five — if BTCUSD's signal
   generation could be evaluated against a longer-horizon variant, the
   research predicts improvement, independent of the macro filter
   question entirely.

4. **US500/US30USD's inversion may be partly a recent, structural,
   market-wide shift** (declining index autocorrelation from options/
   gamma flow) rather than a persistent property — worth revisiting this
   finding periodically rather than treating it as a fixed characteristic
   to permanently code around.

---

## 6. Explicitly not done / not decided

- No code changed. No migration. `macro_regime_mode` stays `shadow`.
- No `macro_edge.py` bucket-key extension (ruled out in §3 — would not
  generalize correctly).
- No per-symbol override mechanism built — the research suggests the
  DIRECTION of the needed fix, not a specific implementation. Building a
  per-symbol config knob before knowing what it should contain for each
  symbol would be premature.
- AAPL's inversion is flagged as possibly a different root cause (HMM
  lag, not momentum/reversal) — not resolved, needs its own look before
  folding into a "per-asset-class macro treatment" framework.
- The horizon-matching hypothesis (§5.2) is the most promising concrete
  next research question but is NOT tested in this pass — would need a
  variant `nt_macro_regime` computation at a shorter SMA window compared
  against the existing 200-day read, which is new code and out of scope
  for a same-day analysis-only pass.

## 7. Recommendation for `nt_roadmap`

Update the existing `macro_regime_mode` row's notes with this finding
(5/6 symbols invert; XAUAUD confirms; 8/14 have no data at the current
threshold) — no new row, no status change, mode stays `shadow`. The
existing 2026-09-26 review date for `macro_regime_mode` is unaffected;
this finding is additional evidence for that review, not a new clock.
