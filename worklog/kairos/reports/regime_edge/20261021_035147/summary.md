# Edge Lab — regime values vs the edge (regime_edge v1)

Run `20261021_035147` · symbols: BTCUSD, XAUUSD, XAGUSD, XAUEUR, XAUAUD, USDJPY, EURUSD, GBPUSD, USDCAD, USDCHF, AUDUSD, AAPL, US500, US30USD · period 2023-11-21T14:30:00+00:00 → 2026-10-21T03:00:00+00:00

Every number is point-in-time (no lookahead), seeded (7), and scored out-of-sample where a model is involved. `records.csv` carries the per-slot audit trail.

## 1 · HMM (Kairos-as-is) 24h-ahead direction skill — W-X1/W-X2/W-X5

| symbol | n_decided | acc | majority | **skill** | flip-rate low-ER | flip-rate high-ER | mean conf low-ER |
|---|---|---|---|---|---|---|---|
| BTCUSD | 17245 | 0.4977 | 0.5166 | **-0.0190** | 0.225 | 0.228 | 0.673 |
| XAUUSD | 13486 | 0.4924 | 0.5416 | **-0.0492** | 0.254 | 0.232 | 0.640 |
| XAGUSD | 13449 | 0.5023 | 0.5434 | **-0.0410** | 0.274 | 0.240 | 0.632 |
| XAUEUR | 13359 | 0.4781 | 0.5469 | **-0.0688** | 0.253 | 0.234 | 0.644 |
| XAUAUD | 13360 | 0.4835 | 0.5374 | **-0.0538** | 0.244 | 0.223 | 0.640 |
| USDJPY | 16919 | 0.4948 | 0.5661 | **-0.0713** | 0.238 | 0.211 | 0.661 |
| EURUSD | 16783 | 0.5087 | 0.5149 | **-0.0062** | 0.256 | 0.228 | 0.664 |
| GBPUSD | 17009 | 0.4932 | 0.5161 | **-0.0230** | 0.268 | 0.231 | 0.641 |
| USDCAD | 17073 | 0.5100 | 0.5344 | **-0.0243** | 0.250 | 0.209 | 0.657 |
| USDCHF | 16942 | 0.4828 | 0.5244 | **-0.0416** | 0.248 | 0.206 | 0.645 |
| AUDUSD | 17041 | 0.4920 | 0.5104 | **-0.0184** | 0.245 | 0.222 | 0.654 |
| AAPL | 4850 | 0.4942 | 0.5701 | **-0.0759** | 0.283 | 0.270 | 0.674 |
| US500 | 13448 | 0.4537 | 0.5556 | **-0.1019** | 0.260 | 0.198 | 0.681 |
| US30USD | 13464 | 0.5212 | 0.5221 | **-0.0009** | 0.282 | 0.228 | 0.667 |

Negative skill = the debounced HMM label is worse than always guessing the majority class. Confidence stays above the 0.60 gate in the most choppy (bottom-quartile-ER) windows — the confidence gate is not a chop brake (W-X5).

## 2 · Edge predictability — does the regime value carry edge information?

| symbol | best feature | Spearman ρ | perm p | (er_abs ρ / p) | (chop ρ / p) | (vol ρ / p) |
|---|---|---|---|---|---|---|
| BTCUSD | er_abs | **-0.0401** | 0.2649 | -0.040/0.265 | -0.038/0.323 | -0.010/0.813 |
| XAUUSD | adx | **-0.1165** | 0.0060 | -0.042/0.334 | -0.053/0.218 | +0.009/0.865 |
| XAGUSD | chop | **+0.0584** | 0.1754 | -0.002/0.974 | +0.058/0.175 | -0.050/0.258 |
| XAUEUR | adx | **-0.0624** | 0.1314 | +0.021/0.618 | +0.031/0.466 | +0.004/0.932 |
| XAUAUD | vol | **-0.0801** | 0.0715 | +0.009/0.858 | +0.034/0.408 | -0.080/0.071 |
| USDJPY | hurst | **+0.0797** | 0.0295 | -0.003/0.947 | -0.048/0.208 | -0.017/0.670 |
| EURUSD | chop | **+0.0723** | 0.0535 | -0.072/0.065 | +0.072/0.053 | -0.027/0.508 |
| GBPUSD | er_abs | **+0.0429** | 0.2784 | +0.043/0.278 | -0.010/0.792 | +0.027/0.470 |
| USDCAD | chop | **-0.0601** | 0.1139 | +0.042/0.255 | -0.060/0.114 | +0.005/0.885 |
| USDCHF | hurst | **-0.0173** | 0.6442 | +0.001/0.984 | -0.017/0.635 | -0.007/0.845 |
| AUDUSD | vol | **+0.0413** | 0.2789 | -0.018/0.642 | +0.023/0.536 | +0.041/0.279 |
| AAPL | hurst | **-0.0515** | 0.4988 | +0.022/0.767 | -0.009/0.887 | +0.010/0.891 |
| US500 | vol | **-0.0400** | 0.3668 | +0.007/0.862 | +0.022/0.599 | -0.040/0.367 |
| US30USD | chop | **-0.0374** | 0.3743 | +0.020/0.639 | -0.037/0.374 | +0.027/0.544 |

ρ(feature at entry, bracket R) with 2000-permutation p-values. Decile edge surfaces with bootstrap 95% CIs are in `edge_lab.json` → `edge_surface`.

Read honestly: the conditional-edge signal is THIN — only 2 of 70 feature×symbol tests clear p<0.05 (~3.5 expected by chance). The measured answer to *“do regime values predict the edge?”* on this sample is: **weakly, not decisively** — still strictly better than the HMM label's negative skill, and now a number, not a guess.

## 3 · The decisive gate backtest — kairos vs corrected vs null (mean R per trade)

| symbol | kairos n / meanR | corrected n / meanR (τ*) | null n / meanR | DM kairos-vs-corr p (raw/Holm) | DM corr-vs-null p (raw/Holm) |
|---|---|---|---|---|---|
| BTCUSD | 139 / +0.3734 | 17 / +0.4635 (τ*=0.45) | 284 / +0.0101 | 0.1048 / 1.0000 | 0.8040 / 1.0000 |
| XAUUSD | 103 / +0.3673 | 16 / +0.3682 (τ*=0.45) | 177 / +0.0779 | 0.2555 / 1.0000 | 0.6107 / 1.0000 |
| XAGUSD | 0 / +0.0000 | 26 / -0.3250 (τ*=0.35) | 199 / -0.0917 | 0.1534 / 1.0000 | 0.5230 / 1.0000 |
| XAUEUR | 0 / +0.0000 | 40 / +0.3433 (τ*=0.40) | 184 / +0.1919 | 0.0744 / 1.0000 | 0.1795 / 1.0000 |
| XAUAUD | 0 / +0.0000 | 89 / +0.0163 (τ*=0.25) | 311 / +0.0025 | 0.9038 / 1.0000 | 0.9773 / 1.0000 |
| USDJPY | 220 / +0.1313 | 44 / +0.1344 (τ*=0.35) | 247 / -0.0152 | 0.2171 / 1.0000 | 0.6018 / 1.0000 |
| EURUSD | 113 / +0.3838 | 17 / -0.0824 (τ*=0.45) | 465 / +0.0405 | 0.0021 / 0.0588 | 0.4819 / 1.0000 |
| GBPUSD | 207 / +0.0594 | 18 / +0.2581 (τ*=0.45) | 270 / +0.0168 | 0.6692 / 1.0000 | 0.9959 / 1.0000 |
| USDCAD | 0 / +0.0000 | 56 / +0.2575 (τ*=0.20) | 350 / -0.0714 | 0.1466 / 1.0000 | 0.0909 / 1.0000 |
| USDCHF | 0 / +0.0000 | 18 / +0.0826 (τ*=0.45) | 396 / -0.0389 | 0.7872 / 1.0000 | 0.4775 / 1.0000 |
| AUDUSD | 0 / +0.0000 | 46 / -0.0404 (τ*=0.30) | 275 / -0.0605 | 0.8170 / 1.0000 | 0.4419 / 1.0000 |
| AAPL | 36 / +0.2506 | 3 / -0.1333 (τ*=0.40) | 130 / +0.0431 | 0.1793 / 1.0000 | 0.7015 / 1.0000 |
| US500 | 0 / +0.0000 | 39 / +0.0643 (τ*=0.45) | 219 / +0.0180 | 0.7375 / 1.0000 | 0.9366 / 1.0000 |
| US30USD | 0 / +0.0000 | 31 / -0.1159 (τ*=0.40) | 225 / -0.0087 | 0.5999 / 1.0000 | 0.9339 / 1.0000 |
| **pooled** | 818 / +0.2241 | 460 / +0.0918 | 3732 / +0.0013 | p=0.0101 (mld=-0.0171) | p=0.6184 (mld=-0.0046) |

DM sign convention: for `kairos-vs-corrected`, p < 0.05 with negative kairos mean-loss-diff favors kairos; for `corrected-vs-null`, p < 0.05 with negative corrected mean-loss-diff favors the corrected book. Holm family = the 28 per-symbol tests.

Read honestly: pooled kairos-vs-corrected p=0.0101 favors kairos **in this drift window** — but the kairos book's mean R rides the wide-TP/tight-SL payoff (RR 1.5-5.3) on realized drift with NEGATIVE direction skill (acc < majority on 14 of 14 symbols), i.e. the W-G2 trend-following-risk inheritance measured, not HMM direction edge; under the driftless null its geometry is EV -0.462…-0.028 per trade (§5). The corrected book takes ~12% of the no-overlap trade count (460 vs null 3732) and its corrected-vs-null DM is not significant (p=0.6184) — no decisive out-of-sample edge on this sample either way. — EV-gate starvation: XAGUSD, XAUEUR, XAUAUD, USDCAD, USDCHF, AUDUSD, US500, US30USD kairos n=0 trades over the window (W-C1)

## 4 · Calibration — flat base rate vs regime-conditional (walk-forward lockbox)

| symbol | base AUC / Brier / ECE | regime AUC / Brier / ECE | n_lockbox | repo bucket AUC |
|---|---|---|---|---|
| BTCUSD | 0.4496 / 0.1215 / 0.0548 | **0.5579** / 0.1214 / 0.0270 | 603 | 0.5904 |
| XAUUSD | 0.4336 / 0.1327 / 0.0357 | **0.5313** / 0.1331 / 0.0555 | 446 | 0.5529 |
| XAGUSD | 0.4582 / 0.1449 / 0.0412 | **0.6051** / 0.1410 / 0.0437 | 447 | 0.6387 |
| XAUEUR | 0.5195 / 0.1945 / 0.0730 | **0.5058** / 0.1953 / 0.0708 | 441 | 0.5598 |
| XAUAUD | 0.4560 / 0.1230 / 0.0335 | **0.6070** / 0.1203 / 0.0458 | 441 | 0.6543 |
| USDJPY | 0.4779 / 0.1984 / 0.0632 | **0.4435** / 0.2009 / 0.0933 | 589 | 0.5455 |
| EURUSD | 0.4830 / 0.2145 / 0.0319 | **0.5545** / 0.2115 / 0.0769 | 583 | 0.5705 |
| GBPUSD | 0.4569 / 0.2074 / 0.0893 | **0.5300** / 0.2069 / 0.0551 | 594 | 0.5521 |
| USDCAD | 0.4833 / 0.1622 / 0.0364 | **0.5315** / 0.1618 / 0.0448 | 597 | 0.5963 |
| USDCHF | 0.4867 / 0.1973 / 0.0491 | **0.5262** / 0.1968 / 0.0350 | 590 | 0.5587 |
| AUDUSD | 0.4790 / 0.1975 / 0.0556 | **0.4600** / 0.1995 / 0.0624 | 594 | 0.5254 |
| AAPL | 0.4055 / 0.1619 / 0.1392 | **0.5739** / 0.1586 / 0.0856 | 87 | 0.5840 |
| US500 | 0.2452 / 0.0287 / 0.0307 | **0.3800** / 0.0290 / 0.0342 | 446 | 0.6148 |
| US30USD | 0.4850 / 0.0263 / 0.0235 | **0.5371** / 0.0265 / 0.0227 | 446 | 0.6976 |

The repo's flat (symbol, vol_tier, bucket) base rate is level-only for the trade being placed: the distance bucket is fixed per symbol, so its p_win varies only through the vol tier — the 0.53-0.70 in-sample AUC of that column is vol-state information (market state), never bracket or direction selection (the repo's own live-measured AUC on its HMM-tier table: 0.4869). The trailing base rate (walk-forward, no cells) shows AUC 0.25-0.52 — its only variation is the slowly-refit rate. The trend-quintile × vol-tercile regime-conditional model is scored strictly out-of-sample (lockbox): mean AUC 0.525 — weak but real per-trade discrimination.

## 5 · Barrier physics — current band centre vs corrected geometry

| symbol | current tp/sl/exp | π_SL cur | EV_null cur | corrected tp/sl/exp | π_SL cor | π_E cor | EV_null cor | EV_drift cor | μ̂ (ATR/bar) |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 8.07/1.77/48 | 0.788 | -0.174 | 6.25/4.00/96 | 0.611 | 0.014 | -0.049 | -0.016 | 0.0056 |
| XAUUSD | 4.94/1.00/48 | 0.828 | -0.044 | 7.00/4.50/96 | 0.599 | 0.035 | -0.042 | +0.030 | 0.0108 |
| XAGUSD | 6.39/2.69/48 | 0.673 | -0.074 | 6.25/4.00/96 | 0.611 | 0.014 | -0.039 | +0.007 | 0.0076 |
| XAUEUR | 4.20/1.82/48 | 0.699 | -0.040 | 6.25/4.00/96 | 0.611 | 0.014 | -0.039 | +0.003 | 0.0069 |
| XAUAUD | 6.57/4.31/48 | 0.525 | -0.078 | 4.00/2.50/72 | 0.617 | 0.000 | -0.028 | -0.020 | 0.0021 |
| USDJPY | 4.52/2.75/48 | 0.616 | -0.028 | 6.50/4.00/96 | 0.618 | 0.018 | -0.036 | +0.024 | 0.0093 |
| EURUSD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/96 | 0.617 | 0.000 | -0.020 | -0.009 | 0.0029 |
| GBPUSD | 4.52/2.75/48 | 0.616 | -0.028 | 6.50/4.00/96 | 0.618 | 0.018 | -0.036 | +0.021 | 0.0089 |
| USDCAD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/72 | 0.617 | 0.000 | -0.020 | -0.019 | 0.0001 |
| USDCHF | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/72 | 0.617 | 0.000 | -0.020 | -0.018 | 0.0005 |
| AUDUSD | 4.52/2.75/48 | 0.616 | -0.028 | 6.25/4.00/96 | 0.611 | 0.014 | -0.034 | +0.012 | 0.0076 |
| AAPL | 6.36/3.57/48 | 0.591 | -0.079 | 4.00/2.50/96 | 0.617 | 0.000 | -0.028 | -0.009 | 0.0047 |
| US500 | 13.23/8.09/48 | 0.241 | -0.158 | 6.25/4.00/96 | 0.611 | 0.014 | -0.037 | +0.003 | 0.0064 |
| US30USD | 12.80/2.41/48 | 0.724 | -0.462 | 6.50/4.00/96 | 0.618 | 0.018 | -0.038 | +0.024 | 0.0097 |

Cross-check vs the repo's own `barrier_null` (driftless, T=48): XAUUSD pi_SL repo=0.831 ours=0.828 Δ=0.003; BTCUSD pi_SL repo=0.791 ours=0.788 Δ=0.002; US500 pi_E repo=0.701 ours=0.704 Δ=0.003

Under the driftless null EVERY current band centre is EV ≤ −cost (the SL-farm proof, W-G2/W-G4); the corrected geometry is constrained (π_SL ≤ 0.62, π_E ≤ 0.25, RR ∈ [1.5, 4]) and prices the measured TREND-regime drift μ̂ (train-split only, shrunk n/(n+100)).

## 6 · TSFM reframe (machine-parsed from the repo's own artifacts)

- HAR beats naive (Holm): **16/20** cells; HAR-EWMA beats naive: **18/20**.
- Chronos zero-shot beats HAR (Holm): **0** cells (expect 0 — measured 0).
- QLIKE best arm by cell: {'har_ewma': 12, 'har': 6, 'chronos2_cov': 2}.
- Phase 0.5 recalibration: accepted **46/100** cells, mean b=0.7166, mean k=2.2506.
- Zero-shot TSFMs carry no directional edge: no chronos arm significantly beats HAR on QLIKE in any of the 20 cells (0 Holm-significant wins), while HAR/HAR-EWMA beat naive in 16/20 and 18/20 cells. TSFM's role is calibrated vol spreads for sizing/geometry (Phase 0.5 recalibration accepted 46/100 cells at mean b=0.7166, k=2.2506); DIRECTION/EDGE comes from regime values (this engine).

## 7 · Verdicts (W-C1..W-G5)

| id | verdict | one-line evidence | source |
|---|---|---|---|
| W-C1 | **PROVEN-FAIL** | Repro of the live gate at the shipped band centres: the 'EV > 0' gate passed 17% of conf-passing slots and its decision is a CONSTANT per (symbol, geometry) — driven by the shipped RR and the flat base table, never by regime state. | engine |
| W-C2 | **PROVEN-FAIL** | Code-path audit at HEAD (heavy_sweep.py:1579-1604, 2386-2402): unscored bucket -> directional p_win (~0.55+) vs global 0.20 floor passes trivially. | repo-measured |
| W-C3 | **MEASURED** | The repo's flat (symbol, vol_tier, bucket) table is LEVEL-ONLY for the trade being placed: the distance bucket is fixed per symbol, so its p_win varies only through the vol tier. | engine |
| W-C4 | **PROVEN-FAIL** | Under the driftless null every current band-centre has EV <= -cost (measured with the repo's own barrier_null + independent MC); live EV charges expiry at 0 with no costs. | engine |
| W-C5 | **MEASURED** | Code-audit census at HEAD (evaluation Part 1): 9 p_win variants; consumers re-blending get numbers unrelated to the shipped p_win. | repo-measured |
| W-C6 | **MEASURED** | Code-audit at HEAD: Bayesian shrink thresholds derived for the directional scale are fed the bracket base rate; Kelly reads the raw label while direction reads the debounced one. | repo-measured |
| W-X1 | **PROVEN-FAIL** | Rank-based state->label guarantees a directional label on every fitted bar (decided fraction 100% here); the tri-state er label abstains on a measured fraction of bars. | engine |
| W-X2 | **PROVEN-FAIL** | 24h-ahead accuracy vs majority baseline per symbol on the repro HMM: skill negative on 14 of 14 (mean -0.0425). | engine |
| W-X3 | **PROVEN-FAIL** | Raw labels flip at measured rates in low-ER (chop) windows while the debouncer freezes direction (repo mechanism, reproduced verbatim). | engine |
| W-X4 | **PROVEN-FAIL** | Census at HEAD (evaluation Part 2.4): efficiency_24h, trend_filter consolidating, dynamic_min_rr, context alignment, router stand_by all shadow. | repo-measured |
| W-X5 | **PROVEN-FAIL** | Mean HMM confidence inside bottom-quartile-ER (most choppy) windows vs the 0.60 gate. | engine |
| W-X6 | **MEASURED** | Bracket substitution decouples published EV from regime state: the kairos book's EV-gate pass rate measured 16% in TREND slots vs 17% in NEUTRAL slots (HMM neutrality never reaches EV — code audit); trend features are 30-bar cum returns (regime_engine.py:894-903) vs sub-30h chop cycles. | engine |
| W-G1 | **MEASURED** | Repo's own SSE entry-timing measurement: -30pt selection gap vs null at 89.5% SL in tight buckets, direction AUC 0.499. | repo-measured |
| W-G2 | **PROVEN-FAIL** | pi_SL at current band centres under the driftless null (repo barrier_null + independent MC agree) vs the physics-constrained corrected geometry. | engine |
| W-G3 | **PROVEN-FAIL** | US500 band pi_E=0.70 under the null (repo + MC cross-check); corrected geometry is constrained to pi_E <= 0.25 by construction. | engine |
| W-G4 | **PROVEN-FAIL** | Current band centres violate the physics constraints outright (worst: US500 pi_E 0.70 > 0.25; every current EV_null ≤ −cost, range -0.462…-0.028); the corrected solver enforces (pi_SL, pi_E, RR) constraints UNDER THE NULL with measured drift on top. | engine |
| W-G5 | **MEASURED** | Code audit: band-centre/best-effort picks ship n=0/exp=0 metadata; cold-start brick fallback (0.5, 3, 6) in absolute price units; pooled-evidence widening is dead code. | repo-measured |

`fixes: []` — filled by the Task 40-b coordinator from the repo-side implementation.
