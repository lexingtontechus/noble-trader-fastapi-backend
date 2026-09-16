# Two-Week Retrospective: Signal Quality, Reachability & Scorecard Evolution

> **Scope**: Review of work from 2026-08-25 through 2026-09-15 (vol-tier
> reachability, ATR-anchored bracket widening, deadline-aware scoring,
> shadow-composition bug fix, macro-alignment analysis). **Type**:
> retrospective + forward-looking metrics review, not an
> implementation worklog. **Next checkpoint**: 2026-10-05 Phase 3b
> Stage 1 review.

---

## Framing: two audiences, two different "most important" metrics

This retrospective deliberately separates two perspectives that used to
get blended into one number:

- **System/admin perspective**: every facet matters —
  calibration, win/loss, reachability, expiry rate. Each measures a
  different failure mode and none substitutes for another. This is the
  perspective the past two weeks of engineering work was conducted from.
- **User/P&L perspective**: **reachability is what actually matters.**
  Pre-Sep-8, the system was running at a 60.6% expiry rate — the
  majority of trades never resolved to a real win or loss — and still
  produced **+$24,058** for the week. A user watching only the PnL line
  would have had no complaint. That number was propped up by a few
  large wins riding on top of a mostly-broken resolution mechanism, but
  from where a user sits, **the dollar figure was good.** This is the
  central tension this retrospective has to hold honestly: the system
  got structurally healthier and simultaneously less profitable in the
  same two weeks, and both of those things are true at once.

**What this implies for how "signal quality" should be reported going
forward**: admin-facing metrics (calibration bias, expiry rate,
reachability bucket distribution) explain *why* the system is behaving
a certain way; user-facing metrics (P&L, win rate) tell them *whether
it worked*. A scorecard that only shows one audience's view will either
alarm a user over a healthy structural fix (win rate dipped while
expiry rate collapsed) or reassure them during a genuinely fragile
period (like the pre-Sep-8 baseline's misleading +$24k week).

---

## Where we were (baseline, week of Aug 25–31 — before this project's work began)

| Metric | Value |
|---|---|
| Qualified signals | 477/week |
| Win rate (resolved) | 30.9% |
| Expiry rate | **60.6%** — the majority of trades never hit SL or TP, they timed out |
| Weekly PnL | **+$24,058** — good from a user's chair; masked the expiry problem underneath |
| Calibration bias | +0.269 (model overconfident: predicted 57.8% win, actual 30.9%) |
| Reachability/bucket awareness | None — Sharpe-only pick, no visibility into which brackets were geometrically reachable |
| Vol-tier awareness | None — one bucket size fits every regime |
| Shadow-mode correctness | Untested — the composition bug found this week had been silently possible since day one |

## Where we are now (week of Sep 8–15, post all fixes)

| Metric | Value | Direction |
|---|---|---|
| Qualified signals | 114–116/week | ↓ (fewer, tighter qualification — expected side effect of MIN_RR/gate fixes mid-project) |
| Win rate (resolved) | 28.9% | ~flat vs. baseline (30.9%) |
| Expiry rate | **21.1%** | ↓↓ from 60.6% — the single biggest structural improvement this project produced |
| Weekly PnL | **-$892** | ↓ from baseline, but baseline's positive number was propped up by the expiry-dominated regime; this is a smaller, cleaner sample |
| Calibration bias | +0.341 | **↑ worse**, not better (see honest caveat below) |
| Reachability shadow (all-time accumulated) | 287 compared, 20.6% "nothing survived," 11.5% pick would have changed | New — didn't exist as measurable evidence before |
| Vol-tier awareness | Live in production (migration 152) | New |
| Shadow-mode correctness | Verified — found and fixed 2 leak points this week (deadline-aware + reachability stages both silently promoting shadow candidates) | New — this was a genuinely invisible risk until traced |

**Honest caveat, not a spin**: calibration bias *worsened* over this
window (+0.269 → +0.341), and win rate is flat. **The expiry-rate
collapse (60.6% → 21.1%) is real and is the mechanism actually doing
the work** — signals are far more often getting a genuine resolution
(win or loss) instead of timing out unresolved. That's a necessary
precondition for everything else (calibration, reachability,
macro-alignment) to even be measurable — you can't calibrate a model
against trades that never resolved. But it's not yet translating into
a better win rate or tighter calibration on its own; those are separate
problems this project surfaced but did not yet fix (P_win
overconfidence, the macro-alignment inversion for BTCUSD/US500,
XAGUSD/USDJPY's specific 0%-win weeks). Framing this as "fixed" would
be wrong — framing it as "the measurement and selection infrastructure
is now trustworthy enough to actually fix those next" is accurate.

### What actually happened, mechanism by mechanism

| Built/Fixed | What it changed |
|---|---|
| Deadline-aware scoring (live 9/8) | Stopped crediting backtest "wins" that would never resolve within the real 48h window — root driver of the expiry-rate collapse |
| Vol-tier reachability bucketing (live 9/13) | Reachability/calibration lookups now split by `(symbol, vol_tier, bucket)` — same bucket has very different real win rates by regime; this was invisible before |
| ATR-anchored bracket widening (shadow 9/13) | Gives gold pairs (and others) a grid candidate that can actually land near price — XAUUSD/XAUAUD already show +$7–12 on 7d PnL, a first real signal this is working |
| Shadow-composition bug (found + fixed 9/14) | Reachability's own shadow data was silently contaminated for ~26h; without catching this, the Oct 5 review would have been evaluating corrupted evidence |
| Macro-alignment reliability-filtered analysis (9/14) | Corrected a false "5/6 symbols invert the macro trend" claim down to 2 genuine cases (BTCUSD, US500) once properly sample-size-controlled — prevented a wrong decision from being made off noisy data |
| ATR-dynamic/conviction thesis (9/15) | Parked, not built — correctly deferred until reachability's trustworthiness is proven at Oct 5 (see `worklog/20260915_AGGRESSION_VOLATILITY_ATR_DYNAMIC_THESIS.md`) |

## Post–Oct 5 outlook (what changes, contingent on the Stage 1 review)

Nothing here is decided yet — Oct 5 is a review, not a guaranteed flip.
Three branches:

1. **If reachability/ATR-anchoring go live**: expect the reachability
   shadow's own numbers (20.6% nothing-survived, 11.5% pick-changed) to
   become real behavior change, not just a comparison log — should show
   up as a further expiry-rate drop and improved PnL specifically for
   symbols where "nothing survived" was common (per the Sep 13 pattern:
   AAPL 32%, USDCAD 45%, XAUUSD 47%, XAUEUR 74%, XAUAUD 89%). This is
   also the trigger condition for revisiting the parked
   ATR-dynamic/conviction thesis.
2. **If review surfaces new problems** (e.g. the mixed good/bad split
   already seen for GBPUSD/US30USD/USDCHF/XAUEUR's widened buckets
   persists at higher sample size): stays shadow longer, or gets a
   symbol-specific carve-out — no existing mechanism for that yet,
   would be new scope.
3. **Calibration bias and win rate remain open regardless of Oct 5's
   outcome** — that's a separate track (EV Engine weight tuning, P_win
   overconfidence) this project didn't touch.

---

## True calibration is not one number — it's per-symbol behavioral understanding

The system currently reports calibration as a single blended bias
(`avg(predicted_p_win) − actual_win_rate`), and that number got *worse*
this fortnight even as the underlying mechanism got healthier. That's
not a contradiction to explain away — it's evidence that **a single
system-wide calibration number is the wrong level of resolution to
trust or act on.**

This fortnight's own findings make the case directly:

- **AAPL**: overconfident specifically on `buy` signals in
  `high_vol_strong_bull` (0% actual win against 66% predicted) —
  interpreted as HMM lag (the regime label reflects a move already
  exhausted by entry time), not generic overconfidence.
- **XAGUSD**: the macro-alignment finding reversed *for this symbol
  specifically* once properly sample-size-controlled (aligned trades
  actually win once thin cells are excluded) — a system-wide macro
  rule would have gotten XAGUSD backwards.
- **BTCUSD/US500**: the only two symbols where the macro-alignment
  inversion held up under scrutiny — everywhere else it was noise.
- **XAUUSD/XAUAUD vs. GBPUSD/US30USD/USDCHF/XAUEUR**: the exact same
  ATR-anchoring mechanism produces a *better* bucket for the first pair
  and a *worse* one for the second, on the same day, same code path.

**The pattern across all four findings is the same**: symbols do not
share a single volatility profile, a single macro sensitivity, or a
single geometry response — treating them as if they do is what produced
the "5/6 symbols invert" false alarm this fortnight (corrected down to
2 genuine cases once properly filtered) and is likely also why
system-wide calibration bias moved in a direction that doesn't match
the underlying mechanism's real improvement.

**What "true calibration" should mean going forward**: not a tighter
single number, but a **blended, tiered treatment per symbol** —
recognizing that (as this fortnight demonstrated repeatedly) the same
mechanism, the same bucket, the same macro read can mean something
different depending on the symbol's own volatility regime, asset
class, and historical behavior. The vol-tier reachability work
(`(symbol, vol_tier, bucket)` keying, shipped 9/13) is a first working
example of this principle in production — the next candidate for the
same treatment is calibration bias itself, computed and trusted
per-symbol rather than blended into one system-wide figure.

---

## Key user-facing metrics: signal quality → scorecard, and what's missing

**What the scorecard already surfaces well** (migration 151, per-
`(symbol, signal, regime)` + `macro_label`): win%, PnL (both raw and
lot-sized), qualified/resolved/expired counts, `avg_predicted_p_win`,
`precision_gap`.

**What should be added, based specifically on what this fortnight
learned the hard way:**

1. **Expiry rate, broken out as its own column, not folded into
   "loss."** This project's single biggest before/after number
   (60.6%→21.1%) isn't visible anywhere in the current scorecard shape
   as a first-class trend — a user watching only win%/PnL would miss
   the most important structural change entirely, and would have been
   falsely reassured by the pre-Sep-8 +$24k week.
2. **Calibration bias per symbol**
   (`avg_predicted_p_win − actual_win_rate`), not just system-wide —
   this fortnight showed the blended number can move the wrong
   direction even while individual symbols improve; per-symbol would
   catch a symbol like AAPL (consistently overconfident on `buy`
   signals) faster than a blended average, and is the concrete first
   step toward the "blended + tiered per-symbol treatment" principle
   above.
3. **Reachability bucket tag on each resolved trade**
   (`distance_atr_bucket`, `vol_tier`) — right now this requires
   cross-referencing `nt_bracket_reachability` separately; surfacing it
   directly on the scorecard would let a user see at a glance whether a
   symbol's losses cluster in the "4+" bucket (a fixable geometry
   problem) vs. spread evenly (a different, harder problem) — directly
   answering the "for a user, reachability is what matters most"
   framing above.
4. **Macro-alignment tag** (`aligned`/`opposed`/`neutral`) per trade,
   now that the reliability-filtered analysis showed this only matters
   for 2 symbols — a scorecard column would let anyone confirm that
   stays true going forward instead of re-running a manual backtest
   each time.
5. **A rolling "trustworthiness" flag** for any shadow-mode metric
   currently feeding a decision — this fortnight's leak (reachability
   shadow data silently contaminated for ~26h) is exactly the kind of
   thing a scorecard should flag automatically (e.g. a
   `data_window_flagged` marker) rather than requiring someone to trace
   the call chain by hand to discover it.

---

## Summary

| Facet | Admin importance | User importance | Two-week trend |
|---|---|---|---|
| Expiry rate | High — root-cause diagnostic | **Highest** — directly determines whether P&L reflects real signal quality or an accounting artifact | 60.6% → 21.1% ✅ major improvement |
| Reachability (bucket-aware pick) | High — the mechanism behind the expiry fix | **High** — this is what "reachability" means to a user: did the bracket ever have a real chance | Shadow evidence now exists (287 compared) and is now provably trustworthy after the leak fix; not yet live |
| Win/loss rate | High | High, but secondary to expiry rate — a flat win rate on a healthier resolution mechanism is a different (better) situation than the same win rate under high expiry | Flat (30.9% → 28.9%) |
| Calibration bias | Highest for admin — the thing every other gate depends on | Low direct visibility to a user, but drives everything they see | Worsened blended (+0.269 → +0.341); root cause is likely that a single system-wide number can't represent symbol-specific behavior (AAPL/XAGUSD/BTCUSD/US500 all behave differently) |
| P&L | Low admin priority (can mask problems, as the baseline week shows) | **Highest** — the number a user actually reads | -$892 this week, down from a misleading +$24k baseline |

Both perspectives are correct simultaneously: the system is
structurally healthier and the week's dollar figure is worse. Neither
fact cancels the other out, and reporting only one of them would be
misleading in different directions depending on the audience.
