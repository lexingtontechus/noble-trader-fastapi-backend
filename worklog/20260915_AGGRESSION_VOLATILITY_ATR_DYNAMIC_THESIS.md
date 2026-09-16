# Aggression/Conviction × Volatility as Joint Levers for Entry/SL/TP — ATR-Dynamic Mode Thesis

> **Priority**: reference only — parked pending Phase 3b Oct 5 results
> · **Status**: thesis, no code written · **Date**: 2026-09-15 ·
> **Depends on**: `heavy_sweep_atr_anchored_bricks_mode`,
> `heavy_sweep_reachability_mode`, `reachability_vol_tier` (all under
> review 2026-10-05)

## Why this is parked, not scoped for build

This thesis proposes a **new, later, opt-in axis** that sits on top of
Phase 3b's ATR-anchored bracket widening — it does not compete with or
replace that work, and its correctness explicitly depends on Phase
3b/reachability's live-vs-shadow decision landing first (see "Explicit
interaction check" below). Do not pick this up before 2026-10-05, and
only pick it up then if the Oct 5 review's outcome is favorable enough
to justify adding a second geometry-adjustment layer on top of the
first.

## Source material reviewed

`docs/Tiered_Trading_Targets.md` describes an idealized pipeline
(`P_win → SL/TP → EV → Entry`) that does not fully match this
codebase's current state — several of its claims are stale (3-source
P_win blend; this repo is on EV Engine v5's 4-source blend per
AGENTS.md) or describe a "Dynamic Mode" that is only partially built.
Traced against the actual code:

| Doc's claim | Reality in this repo |
|---|---|
| SL/TP set from `brick_size × sl/tp_bricks`, static per symbol | **True** — `orchestrator.py:578-589`. `brick_size` comes from the **weekly** heavy sweep (`generate_brick_sizes`/`generate_atr_anchored_brick_sizes`), frozen for 7 days between sweeps. |
| "Dynamic Mode" (`brick_size_mode`: fixed→atr/dynamic) | **Partially true, per-week not per-signal.** `heavy_sweep_atr_anchored_bricks_mode` (shadow) adds ATR-anchored candidates to the weekly grid search — changes what's available in *next week's* grid, not the live signal's bracket in real time. |
| Aggression spectrum (passive/mid/aggressive) drives **order dispatch style** | **True but narrower than implied.** `EVEngine.compute_aggression(ev_per_dollar)` (`ev_engine.py:563-571`) exists, feeds `aggression`/`ev_scale` — but only affects **position sizing** (`effective_kelly = kelly_f × ev_scale`) and a cosmetic label. **Does not touch SL/TP distance at all.** |
| P_win 3-source blend (0.40/0.30/0.30) | **Stale.** This repo is EV Engine v5: `p_pattern 0.30 / p_regime 0.25 / p_markov_hold_n 0.20 / p_timesfm 0.25` (AGENTS.md). |

**The real gap**: conviction (`aggression`/`ev_per_dollar`/`p_win`) and
volatility (ATR) are each already computed live, per-signal — but only
talk to **position size**, never to **where SL/TP actually sit**. SL/TP
geometry is fixed weekly, blind to how confident *this specific signal*
is and blind to whether volatility has moved since the last heavy
sweep.

## The thesis

Two independent axes should each get a say in bracket geometry, at
signal time, not just at weekly-sweep time:

1. **Volatility (ATR-dynamic)** — "how far away should SL/TP be, in
   absolute terms, right now?" The weekly heavy sweep already partially
   addresses this, but is up to 7 days stale by design — a signal fired
   on day 6 uses a `brick_size` calibrated to day-0 volatility.
   `position_sizer.py`'s existing `_volatility_factor(atr_baseline,
   atr_current) = clip(atr_baseline/atr_current, 0.5, 1.5)` already
   solves this exact problem for position **size**; the same ratio,
   reused to scale `brick_size` intraday, is the natural "ATR-dynamic
   mode" the doc gestures at.

2. **Conviction (aggression)** — "given the volatility-set distance,
   should this specific signal ask for more or less reward relative to
   risk?" Not a volatility question — two signals on the same symbol at
   the same moment face identical ATR but shouldn't necessarily take
   identical R:R. Right now `aggression`/`ev_per_dollar` only scale *how
   much* capital goes in, never *what the trade's structure looks
   like*.

## Concrete mechanism (grounded in existing code, additive only)

```
volatility_scale = clip(atr_current / atr_baseline, 0.5, 1.5)   # reuses position_sizer.py's ratio, inverted
conviction_scale  = f(ev_per_dollar or p_win)                     # new

effective_sl_bricks = round(sl_bricks_base × volatility_scale)
effective_tp_bricks = round(tp_bricks_base × volatility_scale × conviction_scale)
```

- `volatility_scale` widens/narrows SL and TP **symmetrically** —
  protects against a bracket too tight for expanded conditions, and
  against a bracket needlessly wide when vol has contracted (the exact
  "nothing survives"/expiry failure mode Phase 3a/3b exist to fix).
- `conviction_scale` only stretches the **reward** side (TP), never SL —
  conviction bleeding into risk tolerance would be a different, more
  dangerous idea. Two candidate forms, both computable from data already
  in hand at signal time:
  - **Simple**: 3-tier step mirroring `compute_aggression`'s own
    breakpoints (`ev_per_dollar > 0.40 → tp×1.2`, `0.15–0.40 → tp×1.0`,
    `≤0.15 → tp×0.85`).
  - **Smoother**: sigmoid in `p_win` centered on `MIN_P_WIN` (0.55),
    same shape as the existing `TIMESFM_SIGMOID_K` pattern elsewhere in
    this codebase.

## Why shadow-first, and the hard dependency on Phase 3b/reachability

This touches the same `effective_kelly`/`aggression`/`ev_scale` chain
that already broke once (`worklog/20260904_SIGNAL_ENGINE_MATH_TRIAGE.md`
— Kelly direction-blind bug, `_mu` direction-blind bug) and the same
5a/5a2 pick-chain composition bug found and fixed this week
(`worklog/20260914_DEADLINE_AWARE_ATR_ANCHOR_SHADOW_LEAK.md`). Any
future build must follow the established `off | shadow | live` pattern:
compute both bracket variants, log a comparison row (same shape as
`nt_heavy_sweep_atr_anchored_shadow`), backfill-validate against
historical signals before ever going live (same precedent as the
70-anchor historical replay in
`worklog/20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md`).

**The blocking dependency**: `conviction_scale` widening TP pushes a
bracket toward the `4+` ATR bucket — exactly the bucket weeks of
reachability work have shown is often negative-expectancy. A
high-conviction signal getting a wider TP could walk straight into the
same reachability trap this project just spent a month fixing. Any
future implementation **must** run its shadow comparison through
`reachability_aware_pick()`'s bucket lookup before considering `live` —
conviction should never be allowed to override a bucket the reachability
data already knows is bad. This is why the thesis cannot proceed
sensibly until reachability itself is trustworthy and its Oct 5
disposition (shadow continues / goes live / gets revised) is known.

## What this is not

Not a proposal to touch `entry_price` (no evidence entry timing/offset
is the problem — SL/TP distance is). Not a TDA-based conviction measure
(already discussed and set aside as the wrong tool for this problem,
see chat history 2026-09-14). Not scope creep into Phase 3b itself —
Phase 3b's ATR-anchoring only affects the weekly grid search's
candidate list; this thesis is a live per-signal adjustment layered
*after* that grid search has already picked a base bracket.

## Trigger to revisit

Re-open this thesis only after the 2026-10-05 Stage 1 review resolves
`heavy_sweep_atr_anchored_bricks_mode` / `heavy_sweep_reachability_mode`
/ `reachability_vol_tier` (see `nt_roadmap`). If reachability proves
trustworthy enough to go live (or stays shadow with a clear positive
trend), this becomes a reasonable next scope item. If Oct 5 instead
surfaces new problems with the underlying bucket/expectancy data, this
thesis needs to be revised or dropped before any build — it inherits
whatever correctness gaps still exist in reachability at that point.
