# Macro-Alignment Backtest — Resolved Against Reachability Confound

> **Priority**: P1 · **Status**: analysis complete, corrects the 2026-09-14
> inversion finding · **Date**: 2026-09-14 (same-day follow-up) ·
> **Roadmap refs**: `macro_regime_mode`

## What this resolves

Two open points from the earlier same-day inversion backtest
(`nt_macro_edge_by_symbol` finding "5 of 6 symbols invert the macro
filter's assumption"):

1. **Is the finding actually a reachability artifact?** The earlier
   pass checked bucket alone (tight vs. wide ATR) and ruled out a shared
   mechanism, but never jointly controlled for BOTH `vol_tier` and
   `distance_atr_bucket` at once, and never checked whether the
   aggregate numbers were being driven by adequately-sampled cells or by
   a few extreme, thin-sample outliers.
2. **Is "5/6 symbols would be hurt by `macro_regime_mode=live`" still
   true once that confound is controlled for?**

## Method

For each symbol, cross-tabbed `(vol_tier, distance_atr_bucket,
alignment)` using the same existing pure functions as before
(`_vol_tier`, `_bucket_for`, `_alignment_for` — no new code). Then
**restricted the aggregate to only cells where BOTH `aligned` and
`opposed` have `n >= 20`** — the same minimum-sample-size bar this
codebase already uses elsewhere (`ev.calibration_min_records = 20`,
worklog 20260907) before trusting a calibration read. Cells below that
bar are dropped from the aggregate rather than averaged in, since a
100%-or-0%-win-rate cell on n=1–10 trades is statistical noise, not
evidence.

## Result: the "5/6 inverted" headline does not survive proper controls

| Symbol | Plain aggregate (prior finding) | **Reliability-filtered** (n≥20 both sides) | % of data dropped as unreliable | Verdict change |
|---|---|---|---|---|
| **XAGUSD** | aligned 25.7% / opposed 46.8% → INVERTED | **aligned 22.0% / opposed 11.5%** | opposed: 45.3% dropped | **REVERSES** — aligned actually wins once reliable |
| **AAPL** | aligned 11.9% / opposed 82.4% → INVERTED | aligned 12.5% / opposed 100.0% (n=35, one cell) | opposed: 62.0% dropped | Survives, but rests on ONE reliable cell at an extreme 100% win rate — needs its own scrutiny, not yet trustworthy at face value |
| **BTCUSD** | aligned 28.2% / opposed 51.2% → INVERTED | aligned 29.7% / opposed 46.2% | opposed: 10.8% dropped | Survives, direction unchanged, magnitude similar |
| **US30USD** | aligned 23.5% / opposed 43.4% → INVERTED | **aligned 29.9% / opposed 31.8%** | both ~3% dropped | **LARGELY DISAPPEARS** — nearly flat once filtered |
| **US500** | aligned 21.8% / opposed 27.5% → INVERTED | aligned 9.2% / opposed 22.3% | ~35% dropped both sides | Survives, strengthens somewhat, but ~1/3 of data on both sides was unreliable |
| **XAUAUD** | aligned 35.6% / opposed 24.5% → confirmed | aligned 42.5% / opposed 11.4% (n=40/35, one cell) | 62–88% dropped | Confirms more strongly, but on very little reliable data (most of XAUAUD's history is `neutral` macro, per earlier finding) |

**Revised count: not "5/6 inverted."** After controlling for the
reachability confound and requiring adequate sample size:
- **1 reverses entirely** (XAGUSD — aligned actually wins when measured
  on cells with real sample size; the plain "opposed wins" read was
  driven almost entirely by thin cells, worst example: XAGUSD's
  `high_vol/1-2` cell had opposed n=381 at 99% win — an extreme,
  isolated result that dominated the un-filtered aggregate).
- **1 largely disappears** (US30USD — flattens to a ~2pp difference,
  not the 20pp the plain aggregate suggested).
- **2 survive with reduced/similar magnitude** (BTCUSD, US500) —
  these are the only two where "opposed beats aligned" holds up under
  scrutiny with reasonable confidence.
- **1 is unresolved, resting on a single suspicious cell** (AAPL — its
  only reliable cell shows a 100% win rate on 35 trades, which is itself
  worth independent verification before treating as real edge).
- **1 confirms the macro filter's original assumption more strongly**
  once filtered (XAUAUD), though on thin data.

## Direct answer to point #1: does reachability explain the inversion?

**Partially, and it's the dominant single factor.** The reachability
confound (vol_tier × bucket) is not a full explanation for every
symbol's number, but it is large enough to **flip the sign entirely for
XAGUSD** and **erase most of the effect for US30USD** — 2 of the 5
originally-flagged "inverted" symbols. For BTCUSD and US500, the
inversion is more robust to this control (survives with similar
magnitude), meaning something other than reachability bucket noise is
driving those two specifically. AAPL and XAUAUD's numbers are both now
resting on too little reliable data to draw a firm conclusion either
way — the honest state is "unresolved, not enough clean signal."

**Root mechanism, stated plainly**: the plain `nt_macro_edge_by_symbol`
aggregate blends every `(vol_tier, bucket)` cell together with equal
weight regardless of sample size. A handful of cells with extreme win
rates on tiny samples (XAGUSD's opposed/high_vol/1-2 at 381 trades and
99% win is the biggest single driver in this dataset) can dominate a
symbol-level average even though they represent a narrow, specific
slice of history, not a general property of "opposed trades." This is
the exact same class of statistical trap the codebase already guards
against elsewhere (`ev.calibration_min_records`, `heavy_sweep.min_trades`)
— it had just not yet been applied to the macro-alignment read.

## Direct answer to point #2: is flipping `macro_regime_mode=live` still risky for 5/6 symbols?

**No — that specific claim was wrong; the corrected picture is narrower
but the caution is not eliminated.** With proper filtering:
- XAGUSD would no longer be harmed by a live flip — it would actually be
  helped, since aligned wins once measured on real sample size (though
  this reverses the sizing DIRECTION `kelly_macro_multiplier` needs for
  this symbol, not just whether to trust it).
- US30USD is close to indifferent — a live flip would neither
  meaningfully help nor hurt it, based on current data.
- BTCUSD and US500 remain genuine candidates for harm if flipped live —
  these two still show a real, reliably-measured inversion.
- AAPL and XAUAUD don't have enough reliable data yet to make either
  call safely — both currently rest on single cells with n in the
  30s-40s, below where this codebase treats a read as trustworthy for
  anything but a single confirming data point.

**This does NOT mean "flip macro_regime_mode to live for XAGUSD/US30USD
and leave BTCUSD/US500 off."** Per-symbol overrides for a single global
mode flag don't exist in this codebase (`macro_regime_mode` is one
value, not a per-symbol dict — same limitation already noted for
`heavy_sweep_atr_anchored_bricks_mode` in the Phase 3b scope). Building
a per-symbol override is a real, separate engineering decision, not
implied by this finding alone.

## What this changes about next steps — still nothing to build yet

- **The plain `nt_macro_edge_by_symbol` diagnostic itself is not wrong**
  as a shadow-mode observability tool — it's doing its job (showing
  which trades won/lost by alignment). What's wrong is treating its
  *unfiltered, un-joint-controlled* aggregate as decision-grade evidence.
- **Before any `macro_regime_mode` decision** (live, per-symbol carve-
  out, or otherwise), the minimum bar should be: re-run this same
  reliability-filtered cross-tab, not the plain aggregate. That's a
  read-only re-query of already-captured data — no new capture, no new
  table.
- **AAPL's single extreme cell (100% win, n=35, opposed/low_vol/4+)**
  deserves its own targeted look before being counted as evidence either
  way — is that 35-trade population genuinely different from AAPL's
  massive aligned/low_vol/4+ population (n=2642, 12.5% win), or is there
  a data/timing artifact concentrating those 35 trades in a specific
  window worth checking directly (same kind of sanity check already
  applied to XAGUSD's macro read this week).
- **Still no code changed.** This is a corrected analysis, not a build.
  `macro_regime_mode` stays `shadow`.

## nt_roadmap update

Append this correction to the existing `macro_regime_mode` row's notes
— supersedes the "5/6 symbols invert" framing from the earlier same-day
entry with the reliability-filtered numbers above. Review date
(2026-09-26) unchanged — this is additional evidence for that review,
not a new decision point.
