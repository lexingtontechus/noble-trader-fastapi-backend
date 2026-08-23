# 2026-08-23 — AAPL/US500 Exception Deep-Dive: Root Causes

**Origin:** follow-up to `20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`. That audit
found and fixed two mechanisms (Markov T^N flapping, negative-mu Kelly floor) and
confirmed both fixes are now live (Studio process restarted, verified via SSH — see
§4). Despite that, AAPL and US500 are *still* the two most severely overconfident
symbols on the live 7-day calibration board. This entry digs into why, tracing raw
price data through the full EV-engine signal-generation chain with live numbers.

**Scope:** audit/investigation only, per user request ("dig into these 2 symbols'
data ... determine the reasons for these 2 exceptions"). Nothing in this entry has
been fixed. All numbers below are live, pulled directly from Supabase and the live
Studio's `orchestrator.log` at investigation time (2026-08-23, ~04:30-05:00 UTC).

---

## Current state (live, `v_eod_calibration_bias_latest`, all symbols)

| symbol | n | avg predicted p_win | realized win rate | bias_raw | status |
|---|---|---|---|---|---|
| **US500** | 63 | 0.677 | **0.0%** | **+0.677** | OVERCONFIDENT |
| **AAPL** | 425 | 0.555 | **11.1%** | **+0.444** | OVERCONFIDENT |
| XAGUSD | 641 | 0.497 | 15.3% | +0.344 | OVERCONFIDENT |
| USDJPY | 122 | 0.188 | 0.0% | +0.188 | CALIBRATED (predicted low too) |
| BTCUSD | 563 | 0.513 | 57.0% | -0.057 | CALIBRATED |
| XAUUSD | 167 | 0.607 | 71.3% | -0.105 | CALIBRATED (underconfident) |

AAPL and US500 aren't marginal cases — US500 has resolved **zero** wins in 63
tp/sl-resolved trades against a 68% average prediction. AAPL has resolved 11%
against 55.5%. XAGUSD is a third symbol with a similarly severe problem, not asked
about here but worth a footnote for later.

---

## Signal generation map (server-side, confirmed from `ev_engine.py` + `orchestrator.py`)

```
raw price → renko bricks → [3 live sources, NOT 4]
                              ├─ p_regime   (RegimeHMM, dual 4-state Gaussian HMM, weight 0.25→0.357 renorm.)
                              ├─ p_markov_hold_n (5-state HMM → 3-state T^N, EMA-smoothed, weight 0.20→0.286)
                              └─ p_timesfm  (external forecast → sigmoid, weight 0.25→0.357)
                            → logit-pooled blend → p_win
                            → ev = p_win×profit − (1−p_win)×stake   (profit/stake from tp_bricks/sl_bricks × brick_size)
                            → ev_scale = min(1, ev_per_dollar / 0.50)
                            → effective_kelly = kelly_f(from raw historical mu/sigma, INDEPENDENT of p_win) × ev_scale
```

**`p_pattern` is dead weight in production** — the server always calls
`compute_ev(p_pattern=None, ...)` (`orchestrator.py:1827,1901`); pattern scoring only
happens client-side in the Hermes agent's own DuckDB, not in this repo. So the
"4-source blend" documented in code comments is really a 3-source blend live, with
weights renormalized to `{p_regime: 0.357, p_markov_hold_n: 0.286, p_timesfm: 0.357}`
(or `{0.556, 0.444}` if TimesFM is also down). This matters directly below: with
`p_pattern` permanently absent, `p_regime` alone carries over a third of the blend's
weight at all times — there's no dilution source to soften a bad regime call.

`kelly_f` (the base sizing fraction) comes from raw historical mu/sigma
(`position_sizer.py`-adjacent logic) and is **computed independently of `p_win`** —
they only multiply together at the very last step via `ev_scale`. A signal can
therefore show a large `effective_kelly` even at `p_win` below 50%, if the
`tp_bricks:sl_bricks` ratio is high enough to push `ev_per_dollar` past the 0.50
saturation point (worked example: AAPL's 12:3=4:1 ratio makes `ev_per_dollar>1.4`
even at `p_win=0.483`, saturating `ev_scale=1.0`) — confirmed this is exactly what's
happening on the AAPL `sweep-heavy` row with `p_win=0.483, kelly_f=0.767,
effective_kelly=0.767` pulled live today; not a floor-mechanism artifact, a
legitimate (if aggressive) high-R:R sizing outcome.

---

## Root cause 1 (primary, unaddressed): regime-HMM misclassification, not just Markov flapping

The 2026-08-22 audit's Mechanism 1 fix (EMA-smoothing) targets the **Markov T^N**
component only. It does **not** touch the separate **regime HMM**
(`regime_platform/core/regime_engine.py`'s `RegimeHMM`), which is refit from scratch
every 5-minute tick on that symbol's own recent price series and has no smoothing at
all. Confirmed live today:

```
AAPL 2026-08-23T04:30:29Z  regime=low_vol_bear  conf=0.9822  p_win=0.4833
AAPL 2026-08-23T04:35:02Z  regime=low_vol_bull  conf=0.9967  p_win=0.7432   <- full flip, 5 min later
AAPL 2026-08-23T04:40:03Z  regime=low_vol_bull  conf=0.9967  p_win=0.7913
```
A full bull↔bear flip, each side claiming >98% confidence, inside 5 minutes with no
underlying price move of that magnitude. `compute_p_regime()` caps the probability
itself at 0.97 (`ev_engine.py:427-467`, fixed 2026-08-18) so it can't *alone* drive
`p_win` to the extremes — but at 25.7-35.7% of blend weight (more once TimesFM is
down), a wrong-but-max-confidence regime call still swings `p_win` by a large,
directionally wrong margin every time it happens.

US500 shows the mirror-image pathology: **pinned** at `regime=high_vol_strong_bear,
conf=1.000` continuously for hours (every sample pulled today, 04:30-04:55Z, same
label/confidence) while `nt_signal_sim` shows it has been issuing `sell` signals the
entire time with **zero tp_hit and zero sl_hit** in the last 20 resolved trades (all
`expired` — see root cause 3). `worklog/20260818_EV_COMPONENT_BREAKDOWN.md:40` records
an earlier instance of the same symbol showing `p_win=1.000, regime=strong_bull`
directly contradicting a 5-hour realized `DOWN` move — this instability runs in
*both* directions over time for US500, not just one.

**Neither AAPL's flip-flopping nor US500's pinning was targeted by any of the
session's fixes** — those addressed Markov-T^N smoothing and the Kelly floor
specifically, not the regime HMM's own stability.

## Root cause 2 (confirmed still occurring, despite the live fix): Markov T^N flapping persists through the EMA smoother

Verified the fix is deployed and running: `orchestrator.log` shows the live process
(started 2026-08-23 03:35 UTC, PID 2090, currently running) repeatedly logging
`"Kelly was 0, regime floor WITHHELD — measured mu=... is negative"` for AAPL and
US500 every ~5 minutes through 05:00 UTC today — this is the same 2026-08-22 deploy
batch that includes the Markov EMA-smoothing fix, so both are live.

Despite that, AAPL's `markov_p_up`/`markov_p_dn` are still swinging wildly tick to
tick, live, today:
```
04:30:29Z  markov_p_up=0.5,    markov_p_dn=0.5       (uniform/degenerate row)
04:35:02Z  markov_p_up=0,      markov_p_dn=0.0018
04:40:03Z  markov_p_up=0.9286, markov_p_dn=0
04:45:02Z  markov_p_up=0,      markov_p_dn=0.0018
```
This is expected, not a broken fix: `settings.MARKOV_EMA_ALPHA=0.30` blends 30% of
each new raw value into the running smoothed estimate
(`orchestrator.py:2866-2906`). Smoothing 30% of a swing between ~0.93 and ~0.002 still
produces large tick-to-tick moves in the *smoothed* series (e.g. previous=0.65,
raw=0.02 → new = 0.3×0.02+0.7×0.65 ≈ 0.461; next raw=0.93 → new ≈ 0.602 — still a
±0.14-0.19 swing per tick). The smoother dampens the symptom; it doesn't fix the root
cause, which is the discrete UP/DOWN/FLAT state classification itself legitimately
flip-flopping at its `±0.25σ` log-return boundary (`hmm/model.py:63`) in a genuinely
calm/choppy market — exactly AAPL and US500's current regime.

## Root cause 3 (new finding, not previously flagged in any prior worklog): the EOD resolver has no minimum-age gate

`eod_resolver.resolve_pending_signals` runs **once daily**, 00:30 UTC
(`orchestrator.py`'s `_run_eod_lookback`, "Scheduled daily 00:30 UTC"). Each run
fetches **every** `nt_signal_sim` row with `outcome IS NULL` inside a lookback
window, oldest first — **with no minimum-age check** (`_fetch_pending`,
`eod_resolver.py:298-320`) — and force-resolves each one against however many
post-entry hourly closes have actually accumulated so far
(`anchor_post_entry_closes`, `eod_resolver.py:118-158`). If TP/SL hasn't been touched
in that (possibly tiny) number of bars, the signal is marked `outcome='expired'`
**permanently** — the row is then forever excluded from any future resolution
attempt (`outcome IS NULL` no longer matches it).

The intended fairness window is `EXPIRY_BARS=48` (48 hourly bars ≈ 2 days,
`eod_resolver.py:71`) — but because the batch only ever runs once a day and sweeps
up everything currently open regardless of entry time, a signal entered even a few
minutes before the run gets judged against however few bars have elapsed, not 48.
Confirmed directly in live data — actual `hold_bars` at resolution for recent
`expired` outcomes: **US500: 2, 3, 3; EURUSD: 2, 3, 3, 3; AAPL: 1, 5, 9**. None
anywhere near 48. Compounding this: **`nt_calibration_records` is only ever written
for `tp_hit`/`sl_hit` outcomes** (`eod_resolver.py:438`,
`if resolved["outcome"] in ("tp_hit", "sl_hit"): await _write_calibration_record(...)`)
— an `expired` verdict never produces a calibration record at all, win or loss.

**This is a universal mechanism, not AAPL/US500-specific** — confirmed the same
"mostly expired" pattern in BTCUSD and XAUUSD's recent qualified signals too (6 of 8
sampled `expired` for both, same 6:3 R:R as US500). It suppresses fresh calibration
evidence for every symbol equally. What makes AAPL (4:1 and 2.67:1 TP:SL ratios) and
US500 (2:1) worse-hit specifically: their TP targets sit proportionately farther from
entry than a coin-flip-normalized SL, so they need proportionately more elapsed bars
to reach TP than symbols with a tighter R:R — exactly the population most vulnerable
to being cut off before the resolver batch catches up to them.

## Root cause 4 (direct consequence of #3): the tracked bias/n for these symbols is frozen, not current

US500's most recent `nt_calibration_records` row is from **2026-08-20** — three days
stale as of this writing — despite US500 continuing to generate qualified signals
every day since (99 qualified in the last 7 days, confirmed live). Every
`eod_correction`/`calibration_enforcement` cycle since then has been re-computing and
re-"correcting" against that same frozen `n=63, bias=0.677` snapshot, which is why
the earlier background-monitor pass (04:30 UTC today) showed AAPL/US500's bias
numbers **bit-for-bit unchanged** across 6+ consecutive correction cycles spanning
18+ hours — not because correction isn't working, but because there's no new
evidence for it to work on. The tiered stagnation breaker (fix #3c) currently reports
`stagnation_tier=0` for both — it doesn't yet distinguish "still receiving fresh bad
evidence every cycle" from "stopped receiving any fresh evidence at all, frozen on
an old snapshot" — these are different failure modes and arguably warrant different
handling.

## Root cause 5 (baseline severity — rules out "it's just a stale-metric artifact")

If root causes 1-4 were the *whole* story, BTCUSD and XAUUSD should show the same
degradation — they don't (57%/71% realized win rate, well-calibrated). Both are
continuously-traded (BTCUSD 24/7, XAUUSD near-24/5). AAPL (NASDAQ single stock,
~6.5h/day) and US500 (index CFD, session-bound liquidity) are the two symbols in this
comparison set whose underlying markets have real trading-hours gaps and
open/close-adjacent volatility artifacts — exactly the conditions (calm/choppy price
action relative to the HMMs' fitting assumptions) that root causes 1-2 describe as
triggering spurious high-confidence misclassification. This is circumstantial, not
proven — no direct code-level asset-class branching was found causing this — but it
is the most consistent explanation for why these two specific symbols, out of the
full active set, are the worst-hit.

---

## What's confirmed fixed vs. not, as of this investigation

| Mechanism | Status |
|---|---|
| Recompute stale EV fields post-enforcement (fix #1) | ✅ Live |
| Unified bias-threshold API (fix #2) | ✅ Live |
| Markov T^N EMA smoothing (fix #3a) | ✅ Live, confirmed running — but insufficient alone (root cause 2) |
| Kelly floor gated on mu≥0 (fix #3b) | ✅ Live, confirmed correctly withholding for AAPL/US500 right now |
| Tiered stagnation breaker (fix #3c) | ✅ Live — but doesn't distinguish stale-frozen vs. actively-bad (root cause 4) |
| Regime-HMM instability (root cause 1) | ❌ Not addressed by any existing fix |
| EOD resolver premature-expiry (root cause 3) | ❌ Newly found here, not addressed |

## Not done in this pass

No code changes. No proposed-fix design work beyond what's implicit in the root
causes above (a resolver minimum-age gate, and separately smoothing/dampening the
regime HMM the same way Markov was smoothed, are the two most obvious candidate
directions — not scoped or estimated here).
