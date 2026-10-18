# PBM drift-layer AUC — Phase E measurement (measured or nothing)

Generated 2026-10-16 20:51 UTC · since 2026-10-14 · sigma 1 ATR/bar · T = 48 bars · bootstrap 200 resamples (seed 13)
Population: resolved `nt_signal_sim` rows.

## Promotion bar (pre-registered)

A directional candidate may enter the PBM as a drift term **μ** on the barrier model only if:

1. its **walk-forward barrier-orientation AUC > 0.5** — each symbol's era-clean decided rows split chronologically 50/50, scored on the later half only (fixed scores, nothing fit), and
2. the **bootstrap 95% CI lower bound on that walk-forward AUC > 0.5** (200 row-level resamples), and
3. its walk-forward AUC is **above the null's own walk-forward AUC** — the geometry baseline. A candidate that cannot beat the null carries no timing information beyond geometry.

The analytic null is scored on the same bar too: it encodes bracket GEOMETRY, and its row reads BASELINE (it is the reference, never a candidate). **Expected honest outcome: everything stays off** (directional blend AUC 0.499 on ~74k resolved signals; `p_clef_dir` 0.58 = chance).

## Era discipline

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z** — the same marker reachability, the bias loop and atr_reopt exclude on.
- rows dropped as pre-cutover: **0**
- era-clean rows: **1451** of 1451 fetched (0 unreadable ts skipped)

## Population

- era-clean decided (tp_hit/sl_hit): **1451** (tp 433 / sl 1018 · qualified 505 / not 946)
- expired (resolved but orientation-undefined — excluded from AUC, counted here): **0**
- vol-tier mix (decided): high_vol: 455, low_vol: 254, med_high_vol: 463, med_low_vol: 279

## Per-scorer AUC (barrier orientation)

| scorer | target | n | AUC all-era | AUC walk-fwd | WF n | 95% CI (WF) | Δ vs null (WF) | verdict |
|---|---|---:|---:|---:|---:|---|---:|---|
| **p_win_dir** (directional logit blend) | tp_hit | 1417 | 0.503 | **0.466** | 701 | [0.428, 0.512] | -0.073 | **STAY-OFF** |
| **p_clef_dir** (LLM direction-agreement stamp) | tp_hit | 1399 | 0.358 | **0.358** | 706 | [0.318, 0.393] | -0.181 | **STAY-OFF** |
| **null_p_win** (analytic null p_win (geometry only)) | tp_hit | 1451 | 0.587 | **0.539** | 729 | [0.500, 0.582] | +0.000 | **BASELINE** |
| **p_clef_wait** (LLM abstention (published skill claim)) | sl_hit | 1399 | 0.362 | **0.364** | 706 | [0.324, 0.396] | -0.175 | **STAY-OFF** |

*`null_p_win` is the geometry baseline, not a drift candidate — its AUC measures how much bracket geometry alone explains. `p_clef_wait` targets the inverted label (sl_hit): higher wait ≈ higher P(adverse) — a suppressor claim, judged against the same bar.*

## Per-symbol walk-forward AUC

| symbol | n decided | n WF | p_win_dir | p_clef_dir | null_p_win | p_clef_wait |
|---|---:|---:|---:|---:|---:|---:|
| AAPL | 53 | 27 | — | 0.080 | 0.692 | 0.120 |
| AUDUSD | 124 | 62 | 0.146 | 0.110 | 0.283 | 0.102 |
| BTCUSD | 142 | 71 | 0.803 | 0.712 | 0.109 | 0.712 |
| EURUSD | 106 | 53 | 0.415 | 0.360 | 0.628 | 0.318 |
| GBPUSD | 107 | 54 | 0.365 | 0.106 | 0.959 | 0.273 |
| US30USD | 60 | 30 | 0.532 | 0.619 | 0.212 | 0.619 |
| US500 | 43 | 22 | — | — | — | — |
| USDCAD | 129 | 65 | 0.571 | 0.428 | 0.082 | 0.467 |
| USDCHF | 124 | 62 | 0.696 | 0.300 | 0.114 | 0.309 |
| USDJPY | 125 | 63 | 0.437 | 0.402 | 0.412 | 0.406 |
| XAGUSD | 145 | 73 | 0.384 | 0.066 | 0.965 | 0.093 |
| XAUAUD | 124 | 62 | 0.181 | 0.492 | 0.872 | 0.514 |
| XAUEUR | 88 | 44 | 0.459 | 0.465 | 1.000 | 0.482 |
| XAUUSD | 81 | 41 | — | — | — | — |
*— = AUC undefined for that scorer (coverage gap, or the symbol's walk-forward half is single-class). Rows with n WF < 30 are noise-grade: read extremes there as artifacts, not evidence.*

## Selection gap — entry-timing thermometer

- **POOLED gap: +0.0032** — realized win rate 0.2984 vs null p_win 0.2953 over n=1451 decided rows. Negative = entries resolve worse than the driftless price of the same brackets (adverse timing).
- cells require n ≥ 20 decided rows to be listed.

### Best 10 cells by gap (closest to / above the null)

| cell | n | realized WR | null p_win | gap |
|---|---:|---:|---:|---:|
| GBPUSD/buy | 32 | 0.812 | 0.372 | +0.4403 |
| XAUEUR/buy | 78 | 0.513 | 0.152 | +0.3606 |
| USDCAD/sell | 36 | 0.722 | 0.371 | +0.3511 |
| USDCHF/sell | 43 | 0.651 | 0.371 | +0.2803 |
| AUDUSD/sell | 78 | 0.538 | 0.380 | +0.1585 |
| USDJPY/sell | 76 | 0.513 | 0.371 | +0.1419 |
| BTCUSD/sell | 96 | 0.240 | 0.149 | +0.0910 |
| XAUAUD/buy | 61 | 0.393 | 0.317 | +0.0769 |
| XAGUSD/buy | 64 | 0.328 | 0.270 | +0.0577 |
| USDJPY/buy | 49 | 0.429 | 0.372 | +0.0565 |

### Worst 10 cells by gap (most adverse entry timing)

| cell | n | realized WR | null p_win | gap |
|---|---:|---:|---:|---:|
| AAPL/buy | 49 | 0.020 | 0.346 | -0.3253 |
| US30USD/buy | 30 | 0.067 | 0.377 | -0.3106 |
| EURUSD/buy | 39 | 0.128 | 0.372 | -0.2439 |
| XAGUSD/sell | 81 | 0.062 | 0.264 | -0.2019 |
| US30USD/sell | 30 | 0.200 | 0.367 | -0.1666 |
| EURUSD/sell | 67 | 0.209 | 0.371 | -0.1621 |
| XAUAUD/sell | 63 | 0.159 | 0.313 | -0.1541 |
| BTCUSD/buy | 46 | 0.000 | 0.153 | -0.1532 |
| XAUUSD/buy | 23 | 0.000 | 0.108 | -0.1083 |
| GBPUSD/sell | 75 | 0.267 | 0.371 | -0.1045 |

## Verdict (mechanically applied bar — no discretion)

- **p_win_dir**: **STAY-OFF** — WF AUC 0.466 <= 0.5; CI lower bound 0.428 <= 0.5; WF AUC 0.466 <= null WF AUC 0.539 (pre-registered: above the null).
- **p_clef_dir**: **STAY-OFF** — WF AUC 0.358 <= 0.5; CI lower bound 0.318 <= 0.5; WF AUC 0.358 <= null WF AUC 0.539 (pre-registered: above the null).
- **null_p_win**: **BASELINE** — cleared the 0.5 bar (geometry baseline, not a candidate).
- **p_clef_wait**: **STAY-OFF** — WF AUC 0.364 <= 0.5; CI lower bound 0.324 <= 0.5; WF AUC 0.364 <= null WF AUC 0.539 (pre-registered: above the null).

> **STAY-OFF across the board. μ stays pinned to 0 — the stable resting state, by design, not a failure.** No directional candidate is admitted to the PBM as drift on this evidence.

## Notes / honesty

- Scorers are FIXED (nothing is fit), so walk-forward = evaluation on each symbol's later half; there is no train/test leakage to guard.
- AUC is the hand-rolled Mann-Whitney rank statistic (mid-ranks for ties); no sklearn dependency.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI lower bound clear 0.5 regardless — and no episode dedupe is applied here, matching the platform's own 74k-row directional measurement.
- sigma = 1 ATR/bar (default from `posterior.pbm_sigma_atr()`; 1.0 = one ATR per bar of log standard deviation). The null is a model (no jumps/gaps, constant σ) — the empirical posterior layer exists to absorb its systematic deviation.
- The selection gap is in-sample by construction: a thermometer of what the entry layer delivered vs the driftless price of the same brackets, not a forecast.

