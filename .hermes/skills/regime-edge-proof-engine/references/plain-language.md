---
# Plain-language explanation

## The problem Kairos had

Kairos turns market data into trade signals (buy/sell with TP/SL brackets). It
uses a hidden-Markov model (HMM) to label the market regime — trend or range —
and that label **decides the trade direction**. If the regime label is wrong,
you trade the wrong way.

The proof engine's job: **assume Kairos might be wrong, and measure it honestly.**

## What `regime_edge/` actually does

It is an **offline replay auditor**, run once a day. It takes 14 ACTIVE symbols'
worth of historical 1h price data (2 years), and re-runs Kairos's logic exactly
as it is — same HMM, same geometry, same gates — to see what Kairos *would* have
done. The 5-symbol subset was the original test; the default run now covers all
14 per commit `fd2cb64` (the 5-symbol run remains the comparability baseline).

Then it asks three questions, all measured, none assumed:

1. **Do regime values predict edge?** Instead of trusting the HMM's direction
   label, it assigns *numeric* regime values — `trend` (signed 24h ER), `chop`
   (Choppiness/100), `vol` (ATR percentile) — and asks whether those numbers
   predict whether a trade wins. Answer on the 14-symbol set: mostly **no —
   thinly.** 2 of 70 feature tests are significant (vs ~3.5 expected by chance);
   `er_abs` is nowhere significant.

2. **Is the HMM direction any good?** It reproduces the live HMM's 24h-ahead
direction and scores it against simply guessing the majority direction.
   Result: **negative skill on all 14 symbols** (mean -0.0425; US500 -0.1019).
   The HMM's direction is worse than always guessing the market's more-likely
direction — worse than a coin flip with a thumb on the scale.

3. **Are the bracket bands (TP/SL) any good?** This is the barrier-physics check.
   It computes, mathematically first, the probability that a given TP/SL band
   hits the SL before the TP (gambler's-ruin problem) — and finds **every
   configured band centre is an SL-farm by physics**: under a no-drift
   assumption, hitting the stop is far more likely than hitting the take-profit,
   so expected value after costs is NEGATIVE. US30USD is now the worst offender
   (pi_SL 0.724, EV_null -0.462).

It also runs a **head-to-head backtest**: the actual Kairos rules vs a
*corrected* geometry (the physics-conform one, never an SL-farm) vs a
*driftless null* (random direction, corrected bands). This is how it proves
whether the corrections are real edge or just riding realized drift.

## What the 7 fixes are (why they shipped live)

| Fix | What it stops | Mechanism |
|---|---|---|
| W-C1 | EV gate pricing a band that is never shipped | prices the real shipped bracket |
| W-C2 | Heavy sweep failing open on unscored brackets | fail_closed + per-asset-class floors |
| W-C3 | Calibration with survivor-biased target | migration predicate, inert until marker |
| W-C4 | Cost-blind, zero-cost EV | per-class ATR cost (forex 0.04 / metals 0.06 / crypto 0.10...) charged once |
| W-X3 | Direction whipsaw + stale lock-in | 6h cross-direction cooldown + flip-rate breaker |
| W-X4 | No chop gate | `efficiency_24h < 0.30` suppresses; fail-open on cold start |
| W-G2/G4 | Bands that are SL-farms by physics | barrier_floor.py: stamp pi on every pick; pi_SL>0.62 => adjust or reject |

### The key consequence nobody expected

**XAUUSD and BTCUSD now REJECT** — their bands (4.94/1.00 and 8.07/1.77) have
pi_SL of 0.83/0.79, far above the 0.62 cap, and widening within the allowed
band cannot get under it. So those symbols publish from **brick geometry** until
re-banded. The engine gives corrected bands that clear: XAUUSD 7.00/4.50/96h
(pi_SL 0.599), BTCUSD 6.25/4.00 (pi_SL 0.611) — but re-banding is an operator
decision, not an auto-fix.

## How to read the verdict: honesty over optimism

- kairos-as-is still earns money pooled across symbols — but **only because**
it rides wide-TP/tight-SL payoffs on realized drift, *with negative direction
skill*. That is trend-following risk, not direction alpha.
- **corrected vs null is NOT significant** per-symbol (Holm p=1.0).
- **8 of 14 symbols now starve** (zero published trades in 2 years) — the EV
gate threshold, not the market, is the binding constraint (incl. both indices,
US500 pi_E 0.70 in-symbol).
- The corrected book takes only ~10% of trade slots; corrected-vs-null DM
  p=0.62 (not significant).

## Why two modes: `edge_lab_run_mode` vs `regime_edge_mode`

- `edge_lab_run_mode` (off/shadow/live, default **shadow**): controls whether
the daily AUDIT RUN executes (job `edge-lab-daily`, 18:30 UTC). When live,
it publishes `edge_lab.json` to `nt_edge_lab_run` for the Edge Lab dashboard.
- `regime_edge_mode` (off/shadow/live, default **shadow**): controls whether
the **live per-signal gate** rejects. When shadow, the gate stamps
  `regime_edge_ok: True` (fail-open default pass) — it looks up the edge but
  never rejects.

The engine run is the **evidence** that would justify flipping the live gate to
live. Making the run depend on the gate's own mode would mean the evidence goes
dark exactly when you need it.

## Where the evidence lives

- `regime_edge/` (11 modules) + `scripts/run_edge_lab.py` + `scripts/export_edge_lab_data.py` (cache builder).
- 19/19 engine tests + 62/62 fix tests; full suite zero regressions vs HEAD.
- Canonical 14-symbol run artifact: `worklog/kairos/reports/regime_edge/20261021_035147/`.
- The 17 verdicts + numbers render on the **Kairos Edge Lab** page in
  nobletradingapp (reads `edge_lab.json`).

## Bottom line

`regime_edge/` is the audit that made Kairos stop guessing. It measured every
claim with real data, shipped 7 concrete correctness fixes live (not shadow),
and in doing so **reduced** Kairos's published signals — honestly, for the first
time, with real costs and real physics priced in.
