# Payoff geometry: per-asset-class TP/SL in ATR, chosen by replay

> **Date:** 2026-09-26 · **Branch:** `kairos` → `main`
> **Flag:** `payoff_geometry_mode` (`off | shadow | live`), shipped `shadow`; **`live` since 2026-09-26 ~04:55 UTC** (see §7)
> **Config:** `config/feature_flags.yaml` → `payoff_geometry_brick_atr`, `payoff_geometry` (per-class table)
> **Code:** `regime_platform/sweep_orchestrator/payoff_geometry.py`; wired in `orchestrator._process_symbol` after `[geo:intraday]`
> **Evidence:** `scripts/kairos_geometry_replay.py` → `reports/20260926_geometry_replay.md` §7–§8; scope §14.5
> **Tests:** `tests/test_payoff_geometry.py` (34)

## 1. Why this exists

The question was whether `dynamic_atr_mode` fixes the current bracket, where the TP is too wide. It can't
reach the geometry that pays, for three reasons:

| Constraint | Where | Effect |
|---|---|---|
| TP band 0.3–2 ATR | `tiered_atr.TIER_TP_ATR_BAND` (hardcoded) | Excludes the TP 9–24 ATR region entirely |
| Heavy grid SL 3/5/7, TP 4–12 bricks | `heavy_sweep.SWEEP_SL_RANGE/TP_RANGE` (hardcoded) | The largest TP:SL it can build is 4:1; the best shapes are 6–16:1 |
| Ranking by band, then expiry risk, then Sharpe | `risk_capped_pick` | Rewards "resolves fastest", not "pays most" |

Re-parameterising dynamic ATR would mean rewriting the band, the grid, the ranking rule and the heavy
sweep's Sharpe backtest together. The simpler lever is to set the bracket directly per asset class in ATR
units, using values selected by replay rather than by a shadow period.

## 2. Evidence (replay, not shadow)

- **Data:** 8,252 light-sweep signals, 09-01 → 09-24, one per symbol × direction × hour.
- **Walk:** 48-bar close-only (the live resolver rule). **P&L:** booked at the triggering close, so stop
  overshoot is included. **Parity:** 87.3% vs the recorded live outcome.
- **Method:** walk-forward. The shape is picked per class on one half of the period and scored on the
  other, in both directions. The grid is TP {3…24} × SL {1, 1.5, 2, 3} ATR with SL ≥ 1 ATR (a sub-ATR
  stop sits inside one bar's noise), TP:SL ≥ 1.5 and expiry share ≤ 25%. A cost of 0.1 ATR per trade is
  charged.

| Class | Fit→test | Picked TP/SL (ATR) | Test expectancy (ATR/signal) | Recorded geometry, same test half | Test expiry share |
|---|---|---|---|---|---|
| forex | h1→h2 | 24 / 1.5 | **+0.90** | −3.18 | 18% |
| forex | h2→h1 | 24 / 1.5 | **+0.90** | +0.40 | 21% |
| equity | h1→h2 | 17 / 2 | **+2.65** | +0.36 | 30% |
| equity | h2→h1 | 12 / 2 | **+0.67** | −1.31 | 6% |
| commodity | h1→h2 | 6 / 1 | **+0.28** | −1.38 | 0% |
| commodity | h2→h1 | 6 / 1 | **+0.73** | −1.00 | 0% |
| crypto | h1→h2 | 4 / 2 | −0.41 | +1.01 | 2% |
| crypto | h2→h1 | 24 / 3 | 0.00 | +1.44 | 27% |

**Shipped table:** forex 24/1.5, equity 17/2, commodity 6/1, crypto `null`.
- Crypto keeps the heavy geometry because out of sample it lost to its recorded geometry.
- Forex and commodity picked the same shape from both halves. Equity picked 17/2 and 12/2; it ships 17/2,
  the full-period pick.

**Payoff shape:** trend-following. At 12/1.5 the TP rate is 15–19% of decided signals: small, frequent
losses and rare large wins.

**Directional p_win does rank at this geometry** (§8, 12/1.5, cost 0.1 ATR):

| p_win band | n | TP rate | Expectancy (ATR/signal) |
|---|---|---|---|
| < 0.45 | 3,963 | 0.152 | +0.21 |
| 0.45–0.55 | 2,139 | 0.172 | +0.58 |
| 0.55–0.65 | 887 | 0.170 | +0.76 |
| ≥ 0.65 | 1,263 | 0.192 | +0.77 |

The same p_win that reads as having no ranking skill at the recorded geometry separates expectancy by
about 3.5× here. The current `min_p_win: 0.55` gate therefore selects the better half, so it isn't the
blocker it looked like. It stays as is.

## 3. What the code does

**Scope: one lever, geometry only.**
- `live` replaces `brick_size / sl_bricks / tp_bricks` for listed classes before `_compute_trade_signal`.
  That changes the SL/TP prices, EV's profit and stake, RR, the payload, the dedup hash and the settlement
  contract.
- **p_win doesn't change.** The Markov T^N horizon keeps the heavy-sweep `tp_bricks` (`_markov_tp_bricks`),
  so the blended p_win, every probability gate and the calibration loop see exactly what they see today.
  A test asserts both `compute_ev` calls use `_markov_tp_bricks`.
- **Brick counts:** the bracket is expressed as counts of `payoff_geometry_brick_atr` (0.5) × ATR. Forex
  24/1.5 → 48/3 bricks. The existing brick plumbing (`pnl_bricks`, dedup, settlement) works unchanged.
- **Fallback:** a class that's absent or `null`, a missing ATR or an invalid entry keeps the heavy
  geometry. An exception logs a warning and keeps the heavy geometry.
- **`shadow`** logs one line per tick with both geometries, for example
  `[payoff:geometry] EURUSD mode=shadow would set ac=forex tp=24ATR sl=1.5ATR … | heavy tp=15.80ATR sl=7.50ATR`.
- **Relationship to `dynamic_atr_mode`:** superseded for listed classes. On the light path the payoff
  override runs after the heavy pick. Dynamic ATR stays in shadow, and the 09-29 review should be replaced
  by this lever's read.

## 4. What changes downstream in `live` (expected, not bugs)

- **Signals resolve faster.** Forex expiry share drops from 43% to about 20%, so calibration evidence
  arrives faster: scorecard B/C.
- **Calibration biases move to OVERCONFIDENT almost everywhere.** p_win is unchanged (mean ≈ 0.48) while
  the TP rate drops to about 15–20%, so the bias is roughly +0.30. The 4h passes enforce Bayesian shrink
  and a Kelly cut, so fewer signals qualify and sizes shrink.
  - This is the known limit of a directional p_win measured against a bracket event (R2
    `p_win_bracket`). It is a separate lever and is **not** changed here.
  - Read it through the no-skill line (§14.4 D), not through the bias status alone.
- **EV and RR jump.** RR goes to 16 (forex), 8.5 (equity) and 6 (commodity). `ev_allowed` passes more
  often, while `kelly_min_ok` and `p_win_ok` stay binding.
- **Losses are tighter.** A forex stop is about 1.5 ATR (≈ 1.5 hours of typical movement), so there are
  more small losses. Expected by design.
- **Era boundary:** outcomes from the flip onward are a new geometry era. Compare across the boundary only
  via the no-skill line.

## 5. Promotion (replay replaces the shadow wait)

The replay is the evidence; `shadow` is kept only to confirm the wiring on the Studio (the log lines show
the intended bricks per symbol). Flip to `live` once:
1. one light sweep shows a `[payoff:geometry]` line for all 13 non-crypto symbols, with brick counts
   matching §3, and no `failed` warnings;
2. it's not in the same read window as another lever. The resolver fix is live; the hourly resolve and
   the evidence pool stay in shadow.

**Read after the flip** (48–72h, per class): expiry share, TP rate vs no-skill, and realised expectancy
in ATR against the §2 test column.
- Rollback is `payoff_geometry_mode: off`, with no restart needed.
- Re-run `scripts/kairos_geometry_replay.py` weekly. The table is a one-month, in-sample-selected result
  (hourly signals overlap, so there are far fewer independent episodes than 8,252).

## 6. Caveats

- Entries and directions are held fixed. The geometry doesn't change which signals pass the probability
  gates (by design, since p_win is unchanged), but EV/RR can let more through `ev_allowed`.
- Close-only walk. A live venue with intrabar stops would fill tight stops earlier and more often. The
  replay's triggering-close booking is conservative on the loss size but not on the fill frequency.
- One month is one set of trends. The trend-following payoff depends on trends continuing.
- Live `compute_bricks_pnl` books nominal ±bricks. At a 1–2 ATR stop that understates losses under the
  close-only walk. Treat `pnl_bricks` as nominal and use the replay's close-based numbers for expectancy.

## 7. Activation record

**Shadow check, 04:45 UTC light sweep** (Studio `12f5dfc`, orchestrator started 04:42:52): 13 of 13
non-crypto symbols logged `[payoff:geometry] … would set`, with 0 `failed` warnings and 0 tracebacks.
BTCUSD correctly kept the heavy geometry.

| Class | Would set (bricks of 0.5 ATR) | Heavy bracket at the time (TP / SL, ATR) |
|---|---|---|
| forex ×6 | 48 / 3 (24 / 1.5 ATR) | AUDUSD 64.7 / 37.8, EURUSD 33.1 / 19.9, GBPUSD 33.9 / 20.4, USDCAD 68.1 / 40.9, USDCHF 56.5 / 32.9, USDJPY 13.6 / 8.1 |
| equity ×3 | 34 / 4 (17 / 2 ATR) | AAPL 17.2 / 10.0, US30USD 12.6 / 7.5, US500 1.2 / 0.45 |
| commodity ×4 | 12 / 2 (6 / 1 ATR) | XAGUSD 1.2 / 0.45, XAUAUD 0.75 / 0.45, XAUEUR 10.5 / 3.9, XAUUSD 1.8 / 1.05 |

**Notes from the check:**
- **Four symbols had already moved to tight brackets.** US500, XAGUSD, XAUAUD and XAUUSD were at
  0.45–1.05 ATR stops, most likely from the hybrid-promoted heavy sweep (`atr_anchored_evidence_mode`
  live). For these four the flip *widens* the stop to 1–2 ATR. The replay's "recorded" baseline was the
  wide 09-01→09-24 geometry, so compare these four against the tight-shape rows of the replay report
  instead.
- **Forex heavy brackets are fixed in price, so they swell in ATR terms when ATR is low** (Asia session).
  Payoff geometry scales with live ATR, so the forex stop is tightest in quiet hours. The replay walked
  signals from all hours, so the effect is already in its numbers. Watch London-open stop-outs anyway.

**Flip:** `payoff_geometry_mode: "live"` ~04:55 UTC. It is the only new lever in the read window. The
resolver full-scan fix is live (it's evidence plumbing, not a signal change). The hourly resolve and the
evidence pool stay in shadow; dynamic ATR stays in shadow and is superseded for the listed classes.

**Read at 48h and 72h after the flip, per class, via the §14.4 scorecard:**
- expiry share;
- TP rate vs the no-skill line;
- realised expectancy (ATR/signal, close-based);
- qualified count vs the prior 72h (the calibration shrink is expected to cut it).

Compare against the §2 test column: forex +0.90, equity +0.67…+2.65, commodity +0.28…+0.73 ATR/signal.
Rollback: `payoff_geometry_mode: off`.
