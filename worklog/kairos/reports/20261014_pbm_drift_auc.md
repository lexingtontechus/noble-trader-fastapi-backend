# PBM drift-layer AUC — Phase E measurement (measured or nothing)

Generated 2026-10-14 23:12 UTC · since 2026-10-13 · sigma 1 ATR/bar · T = 48 bars · bootstrap 200 resamples (seed 13)
Population: resolved `nt_signal_sim` rows.

## Promotion bar (pre-registered)

A directional candidate may enter the PBM as a drift term **μ** on the barrier model only if:

1. its **walk-forward barrier-orientation AUC > 0.5** — each symbol's era-clean decided rows split chronologically 50/50, scored on the later half only (fixed scores, nothing fit), and
2. the **bootstrap 95% CI lower bound on that walk-forward AUC > 0.5** (200 row-level resamples).

The analytic null is scored as a candidate too: it encodes bracket GEOMETRY. A drift candidate that cannot beat the null's own walk-forward AUC carries no timing information beyond geometry — μ stays pinned to 0. **Expected honest outcome: everything stays off** (directional blend AUC 0.499 on ~74k resolved signals; `p_clef_dir` 0.58 = chance).

## Era discipline

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z** — the same marker reachability, the bias loop and atr_reopt exclude on.
- rows dropped as pre-cutover: **0**
- era-clean rows: **1623** of 1623 fetched (0 unreadable ts skipped)

## Population

- era-clean decided (tp_hit/sl_hit): **1623** (tp 459 / sl 1164 · qualified 423 / not 1200)
- expired (resolved but orientation-undefined — excluded from AUC, counted here): **0**
- vol-tier mix (decided): high_vol: 496, low_vol: 329, med_high_vol: 450, med_low_vol: 348

## Per-scorer AUC (barrier orientation)

| scorer | target | n | AUC all-era | AUC walk-fwd | WF n | 95% CI (WF) | Δ vs null (WF) | verdict |
|---|---|---:|---:|---:|---:|---|---:|---|
| **p_win_dir** (directional logit blend) | tp_hit | 1579 | 0.507 | **0.521** | 804 | [0.469, 0.574] | -0.145 | **STAY-OFF** |
| **p_clef_dir** (LLM direction-agreement stamp) | tp_hit | 1563 | 0.364 | **0.343** | 791 | [0.304, 0.394] | -0.323 | **STAY-OFF** |
| **null_p_win** (analytic null p_win (geometry only)) | tp_hit | 1623 | 0.673 | **0.666** | 815 | [0.622, 0.700] | +0.000 | **PROMOTE** |
| **p_clef_wait** (LLM abstention (published skill claim)) | sl_hit | 1563 | 0.370 | **0.330** | 791 | [0.291, 0.381] | -0.336 | **STAY-OFF** |

*`null_p_win` is the geometry baseline, not a drift candidate — its AUC measures how much bracket geometry alone explains. `p_clef_wait` targets the inverted label (sl_hit): higher wait ≈ higher P(adverse) — a suppressor claim, judged against the same bar.*

## Per-symbol walk-forward AUC

| symbol | n decided | n WF | p_win_dir | p_clef_dir | null_p_win | p_clef_wait |
|---|---:|---:|---:|---:|---:|---:|
| AAPL | 37 | 19 | — | — | — | — |
| AUDUSD | 134 | 67 | 0.493 | 0.507 | 0.880 | 0.519 |
| BTCUSD | 137 | 69 | — | — | — | — |
| EURUSD | 142 | 71 | 0.374 | 0.731 | 0.516 | 0.738 |
| GBPUSD | 133 | 67 | 0.385 | 0.371 | 0.704 | 0.380 |
| US30USD | 60 | 30 | 0.278 | 0.292 | 0.395 | 0.292 |
| US500 | 41 | 21 | 0.975 | — | 1.000 | — |
| USDCAD | 135 | 68 | 0.678 | 0.277 | 0.399 | 0.280 |
| USDCHF | 141 | 71 | 0.491 | 0.227 | 0.380 | 0.216 |
| USDJPY | 148 | 74 | 0.434 | 0.597 | 0.284 | 0.591 |
| XAGUSD | 144 | 72 | 0.191 | 0.720 | 0.405 | 0.698 |
| XAUAUD | 126 | 63 | 0.753 | 0.640 | 0.144 | 0.623 |
| XAUEUR | 120 | 60 | 0.000 | — | 0.000 | — |
| XAUUSD | 125 | 63 | 0.420 | 0.382 | 0.107 | 0.348 |

## Selection gap — entry-timing thermometer

- **POOLED gap: -0.0154** — realized win rate 0.2828 vs null p_win 0.2982 over n=1623 decided rows. Negative = entries resolve worse than the driftless price of the same brackets (adverse timing).
- cells require n ≥ 20 decided rows to be listed.

### Best 10 cells by gap (closest to / above the null)

| cell | n | realized WR | null p_win | gap |
|---|---:|---:|---:|---:|
| XAUAUD/sell | 37 | 0.757 | 0.314 | +0.4433 |
| GBPUSD/buy | 25 | 0.800 | 0.372 | +0.4282 |
| AUDUSD/buy | 78 | 0.782 | 0.381 | +0.4014 |
| XAUUSD/sell | 45 | 0.422 | 0.127 | +0.2954 |
| USDCHF/sell | 60 | 0.633 | 0.371 | +0.2620 |
| EURUSD/buy | 71 | 0.592 | 0.372 | +0.2197 |
| USDCAD/sell | 35 | 0.543 | 0.371 | +0.1715 |
| XAUEUR/sell | 24 | 0.458 | 0.293 | +0.1649 |
| XAGUSD/sell | 82 | 0.415 | 0.264 | +0.1502 |
| US30USD/buy | 31 | 0.484 | 0.370 | +0.1140 |

### Worst 10 cells by gap (most adverse entry timing)

| cell | n | realized WR | null p_win | gap |
|---|---:|---:|---:|---:|
| AAPL/buy | 20 | 0.000 | 0.312 | -0.3120 |
| XAUAUD/buy | 89 | 0.056 | 0.316 | -0.2603 |
| AUDUSD/sell | 56 | 0.143 | 0.380 | -0.2373 |
| XAGUSD/buy | 62 | 0.032 | 0.269 | -0.2372 |
| EURUSD/sell | 71 | 0.169 | 0.371 | -0.2022 |
| XAUEUR/buy | 96 | 0.000 | 0.195 | -0.1953 |
| GBPUSD/sell | 108 | 0.194 | 0.371 | -0.1769 |
| BTCUSD/sell | 92 | 0.000 | 0.146 | -0.1459 |
| US30USD/sell | 29 | 0.172 | 0.306 | -0.1331 |
| USDCAD/buy | 100 | 0.250 | 0.372 | -0.1219 |

## Verdict (mechanically applied bar — no discretion)

- **p_win_dir**: **STAY-OFF** — CI lower bound 0.469 <= 0.5.
- **p_clef_dir**: **STAY-OFF** — WF AUC 0.343 <= 0.5; CI lower bound 0.304 <= 0.5.
- **null_p_win**: **PROMOTE** — WF AUC > 0.5 with CI lower bound > 0.5.
- **p_clef_wait**: **STAY-OFF** — WF AUC 0.330 <= 0.5; CI lower bound 0.291 <= 0.5.

> **STAY-OFF across the board. μ stays pinned to 0 — the stable resting state, by design, not a failure.** No directional candidate is admitted to the PBM as drift on this evidence.

## Notes / honesty

- Scorers are FIXED (nothing is fit), so walk-forward = evaluation on each symbol's later half; there is no train/test leakage to guard.
- AUC is the hand-rolled Mann-Whitney rank statistic (mid-ranks for ties); no sklearn dependency.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI lower bound clear 0.5 regardless — and no episode dedupe is applied here, matching the platform's own 74k-row directional measurement.
- sigma = 1 ATR/bar (default from `posterior.pbm_sigma_atr()`; 1.0 = one ATR per bar of log standard deviation). The null is a model (no jumps/gaps, constant σ) — the empirical posterior layer exists to absorb its systematic deviation.
- The selection gap is in-sample by construction: a thermometer of what the entry layer delivered vs the driftless price of the same brackets, not a forecast.

