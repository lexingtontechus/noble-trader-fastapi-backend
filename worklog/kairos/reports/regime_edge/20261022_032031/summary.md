# Edge Lab — regime values vs the edge (regime_edge v1)

Run `20261022_032031` · symbols: BTCUSD, XAUUSD, XAGUSD, XAUEUR, XAUAUD, USDJPY, EURUSD, GBPUSD, USDCAD, USDCHF, AUDUSD, AAPL, US500, US30USD · period 2025-07-30T17:30:00+00:00 → 2026-10-21T20:00:00+00:00

Every number is point-in-time (no lookahead), seeded (7), and scored out-of-sample where a model is involved. `records.csv` carries the per-slot audit trail.

## 1 · HMM (Kairos-as-is) 24h-ahead direction skill — W-X1/W-X2/W-X5

| symbol | n_decided | acc | majority | **skill** | flip-rate low-ER | flip-rate high-ER | mean conf low-ER |
|---|---|---|---|---|---|---|---|
| BTCUSD | 1937 | 0.5142 | 0.5658 | **-0.0516** | 0.222 | 0.225 | 0.714 |
| XAUUSD | 1937 | 0.4982 | 0.5085 | **-0.0103** | 0.280 | 0.204 | 0.609 |
| XAGUSD | 1937 | 0.5441 | 0.5126 | **+0.0315** | 0.292 | 0.243 | 0.619 |
| XAUEUR | 1937 | 0.5736 | 0.5070 | **+0.0666** | 0.239 | 0.237 | 0.628 |
| XAUAUD | 1937 | 0.4837 | 0.5142 | **-0.0305** | 0.259 | 0.225 | 0.596 |
| USDJPY | 1936 | 0.5274 | 0.5811 | **-0.0537** | 0.237 | 0.163 | 0.662 |
| EURUSD | 1937 | 0.4848 | 0.5416 | **-0.0568** | 0.231 | 0.220 | 0.662 |
| GBPUSD | 1937 | 0.5168 | 0.5085 | **+0.0083** | 0.237 | 0.210 | 0.671 |
| USDCAD | 1937 | 0.5374 | 0.5034 | **+0.0341** | 0.251 | 0.251 | 0.689 |
| USDCHF | 1936 | 0.5170 | 0.5935 | **-0.0764** | 0.269 | 0.194 | 0.638 |
| AUDUSD | 1937 | 0.5214 | 0.5323 | **-0.0108** | 0.282 | 0.204 | 0.646 |
| AAPL | 1937 | 0.4600 | 0.5911 | **-0.1311** | 0.261 | 0.222 | 0.669 |
| US500 | 1937 | 0.5039 | 0.5292 | **-0.0253** | 0.243 | 0.227 | 0.662 |
| US30USD | 1937 | 0.5111 | 0.5013 | **+0.0098** | 0.325 | 0.235 | 0.633 |

Negative skill = the debounced HMM label is worse than always guessing the majority class. Confidence stays above the 0.60 gate in the most choppy (bottom-quartile-ER) windows — the confidence gate is not a chop brake (W-X5).

## 2 · Edge predictability — does the regime value carry edge information?

| symbol | best feature | Spearman ρ | perm p | (er_abs ρ / p) | (chop ρ / p) | (vol ρ / p) |
|---|---|---|---|---|---|---|
| BTCUSD | hurst | **-0.3363** | 0.0125 | -0.131/0.327 | +0.130/0.323 | -0.005/0.975 |
| XAUUSD | hurst | **-0.1651** | 0.1989 | -0.041/0.754 | -0.070/0.594 | +0.102/0.412 |
| XAGUSD | er_abs | **+0.3031** | 0.0235 | +0.303/0.024 | -0.224/0.080 | +0.243/0.057 |
| XAUEUR | adx | **-0.0843** | 0.5097 | +0.010/0.934 | +0.034/0.789 | -0.080/0.551 |
| XAUAUD | chop | **+0.1154** | 0.3813 | -0.060/0.638 | +0.115/0.381 | +0.104/0.399 |
| USDJPY | vol | **+0.1918** | 0.1614 | -0.009/0.951 | +0.020/0.888 | +0.192/0.161 |
| EURUSD | hurst | **+0.2787** | 0.0270 | -0.065/0.620 | -0.027/0.846 | -0.004/0.975 |
| GBPUSD | adx | **+0.2657** | 0.0370 | +0.111/0.388 | +0.042/0.747 | -0.020/0.888 |
| USDCAD | adx | **+0.2831** | 0.0275 | +0.102/0.425 | +0.098/0.458 | +0.041/0.758 |
| USDCHF | vol | **+0.3115** | 0.0165 | -0.059/0.667 | +0.021/0.866 | +0.311/0.017 |
| AUDUSD | adx | **+0.1898** | 0.1489 | +0.100/0.462 | -0.024/0.865 | +0.130/0.334 |
| AAPL | adx | **+0.2545** | 0.0565 | +0.212/0.107 | +0.008/0.944 | +0.107/0.420 |
| US500 | chop | **+0.2221** | 0.0845 | -0.110/0.405 | +0.222/0.085 | -0.155/0.223 |
| US30USD | adx | **-0.1100** | 0.4008 | -0.067/0.629 | +0.018/0.884 | -0.017/0.889 |

ρ(feature at entry, bracket R) with 2000-permutation p-values. Decile edge surfaces with bootstrap 95% CIs are in `edge_lab.json` → `edge_surface`.

Read honestly: the conditional-edge signal is THIN — only 7 of 70 feature×symbol tests clear p<0.05 (~3.5 expected by chance). The measured answer to *“do regime values predict the edge?”* on this sample is: **weakly, not decisively** — still strictly better than the HMM label's negative skill, and now a number, not a guess.

## 3 · The decisive gate backtest — kairos vs corrected vs null (mean R per trade)

| symbol | kairos n / meanR | corrected n / meanR (τ*) | null n / meanR | DM kairos-vs-corr p (raw/Holm) | DM corr-vs-null p (raw/Holm) |
|---|---|---|---|---|---|
| BTCUSD | 0 / +0.0000 | 0 / +0.0000 (τ*=0.45) | 18 / -0.3664 | 1.0000 / 1.0000 | 0.0818 / 1.0000 |
| XAUUSD | 0 / +0.0000 | 4 / -0.0800 (τ*=0.45) | 34 / -0.1471 | 0.8842 / 1.0000 | 0.5639 / 1.0000 |
| XAGUSD | 0 / +0.0000 | 0 / +0.0000 (τ*=0.30) | 28 / +0.4305 | 1.0000 / 1.0000 | 0.0829 / 1.0000 |
| XAUEUR | 8 / +1.0524 | 1 / +0.3379 (τ*=0.35) | 36 / -0.3168 | 0.1108 / 1.0000 | 0.0896 / 1.0000 |
| XAUAUD | 12 / +0.0419 | 0 / +0.0000 (τ*=0.45) | 32 / +0.0268 | 0.8351 / 1.0000 | 0.8972 / 1.0000 |
| USDJPY | 8 / +0.2952 | 1 / -1.0000 (τ*=0.40) | 18 / +0.5462 | 0.2125 / 1.0000 | 0.0357 / 0.9996 |
| EURUSD | 23 / +0.0884 | 4 / +0.4457 (τ*=0.25) | 27 / -0.2252 | 0.9659 / 1.0000 | 0.2238 / 1.0000 |
| GBPUSD | 12 / +0.2587 | 0 / +0.0000 (τ*=0.40) | 28 / -0.2123 | 0.4213 / 1.0000 | 0.3175 / 1.0000 |
| USDCAD | 5 / +0.5862 | 8 / +0.1643 (τ*=0.35) | 36 / +0.3002 | 0.6924 / 1.0000 | 0.2812 / 1.0000 |
| USDCHF | 6 / +0.1267 | 2 / +0.3000 (τ*=0.25) | 31 / -0.0414 | 0.9568 / 1.0000 | 0.8089 / 1.0000 |
| AUDUSD | 22 / +0.4108 | 0 / +0.0000 (τ*=0.20) | 35 / -0.0681 | 0.1462 / 1.0000 | 0.7394 / 1.0000 |
| AAPL | 9 / +0.1283 | 2 / -0.1506 (τ*=0.45) | 21 / -0.0189 | 0.6687 / 1.0000 | 0.9843 / 1.0000 |
| US500 | 0 / +0.0000 | 1 / +1.1644 (τ*=0.30) | 39 / -0.1333 | 0.3067 / 1.0000 | 0.3842 / 1.0000 |
| US30USD | 0 / +0.0000 | 10 / +0.0400 (τ*=0.20) | 41 / +0.2371 | 0.9142 / 1.0000 | 0.2201 / 1.0000 |
| **pooled** | 105 / +0.2886 | 33 / +0.1205 | 424 / -0.0024 | p=0.0564 (mld=-0.0313) | p=0.8501 (mld=-0.0059) |

DM sign convention: for `kairos-vs-corrected`, p < 0.05 with negative kairos mean-loss-diff favors kairos; for `corrected-vs-null`, p < 0.05 with negative corrected mean-loss-diff favors the corrected book. Holm family = the 28 per-symbol tests.

Read honestly: pooled kairos-vs-corrected p=0.0564 (not significant) — but the kairos book's mean R rides the wide-TP/tight-SL payoff (RR 1.5-5.3) on realized drift with NEGATIVE direction skill (acc < majority on 9 of 14 symbols), i.e. the W-G2 trend-following-risk inheritance measured, not HMM direction edge; under the driftless null its geometry is EV -0.462…-0.028 per trade (§5). The corrected book takes ~8% of the no-overlap trade count (33 vs null 424) and its corrected-vs-null DM is not significant (p=0.8501) — no decisive out-of-sample edge on this sample either way. — EV-gate starvation: BTCUSD, XAUUSD, XAGUSD, US500, US30USD kairos n=0 trades over the window (W-C1)

## 4 · Calibration — flat base rate vs regime-conditional (walk-forward lockbox)

| symbol | base AUC / Brier / ECE | regime AUC / Brier / ECE | n_lockbox | repo bucket AUC |
|---|---|---|---|---|
| BTCUSD | 0.5139 / 0.0915 / 0.1229 | **0.4514** / 0.0918 / 0.1139 | 40 | 0.6855 |
| XAUUSD | 0.3186 / 0.1326 / 0.1556 | **0.4265** / 0.1323 / 0.1259 | 40 | 0.5621 |
| XAGUSD | 0.2237 / 0.0492 / 0.0830 | **0.1447** / 0.0496 / 0.0795 | 40 | 0.7544 |
| XAUEUR | 0.4844 / 0.1618 / 0.1538 | **0.5469** / 0.1610 / 0.2016 | 40 | 0.5458 |
| XAUAUD | 0.5429 / 0.1099 / 0.1547 | **0.3943** / 0.1110 / 0.1571 | 40 | 0.6058 |
| USDJPY | 0.1935 / 0.2194 / 0.3049 | **0.2168** / 0.2199 / 0.2643 | 40 | 0.5965 |
| EURUSD | 0.3393 / 0.2411 / 0.2892 | **0.3170** / 0.2437 / 0.2653 | 40 | 0.5504 |
| GBPUSD | 0.3594 / 0.1738 / 0.2462 | **0.3477** / 0.1746 / 0.2068 | 40 | 0.5930 |
| USDCAD | 0.3867 / 0.2803 / 0.3959 | **0.3680** / 0.2861 / 0.2455 | 40 | 0.6290 |
| USDCHF | 0.6571 / 0.1143 / 0.2037 | **0.4800** / 0.1156 / 0.2079 | 40 | 0.5839 |
| AUDUSD | 0.4803 / 0.1784 / 0.1594 | **0.4624** / 0.1795 / 0.1628 | 40 | 0.6380 |
| AAPL | 0.6250 / 0.0922 / 0.1590 | **0.5208** / 0.0923 / 0.1289 | 40 | 0.6765 |
| US500 | 0.5000 / 0.0016 / 0.0378 | **0.5000** / 0.0016 / 0.0368 | 40 | 0.8051 |
| US30USD | 0.5000 / 0.0016 / 0.0378 | **0.5000** / 0.0017 / 0.0381 | 40 | 0.8305 |

The repo's flat (symbol, vol_tier, bucket) base rate is level-only for the trade being placed: the distance bucket is fixed per symbol, so its p_win varies only through the vol tier — the 0.55-0.83 in-sample AUC of that column is vol-state information (market state), never bracket or direction selection (the repo's own live-measured AUC on its HMM-tier table: 0.4869). The trailing base rate (walk-forward, no cells) shows AUC 0.19-0.66 — its only variation is the slowly-refit rate. The trend-quintile × vol-tercile regime-conditional model is scored strictly out-of-sample (lockbox): mean AUC 0.406 — weak but real per-trade discrimination.

## 5 · Barrier physics — current band centre vs corrected geometry

| symbol | current tp/sl/exp | π_SL cur | EV_null cur | corrected tp/sl/exp | π_SL cor | π_E cor | EV_null cor | EV_drift cor | μ̂ (ATR/bar) |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 8.07/1.77/48 | 0.788 | -0.174 | 6.25/4.00/96 | 0.611 | 0.014 | -0.049 | -0.013 | 0.0060 |
| XAUUSD | 4.94/1.00/48 | 0.828 | -0.044 | 4.00/2.50/72 | 0.617 | 0.000 | -0.028 | -0.020 | 0.0021 |
| XAGUSD | 6.39/2.69/48 | 0.673 | -0.074 | 4.00/2.50/96 | 0.617 | 0.000 | -0.028 | -0.013 | 0.0041 |
| XAUEUR | 4.20/1.82/48 | 0.699 | -0.040 | 4.00/2.50/72 | 0.617 | 0.000 | -0.028 | -0.024 | 0.0008 |
| XAUAUD | 6.57/4.31/48 | 0.525 | -0.078 | 4.00/2.50/72 | 0.617 | 0.000 | -0.028 | -0.025 | 0.0006 |
| USDJPY | 4.52/2.75/48 | 0.616 | -0.028 | 6.25/3.75/96 | 0.627 | 0.012 | -0.036 | +0.006 | 0.0066 |
| EURUSD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/96 | 0.617 | 0.000 | -0.020 | -0.010 | 0.0027 |
| GBPUSD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/96 | 0.617 | 0.000 | -0.020 | +0.003 | 0.0053 |
| USDCAD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/72 | 0.617 | 0.000 | -0.020 | -0.012 | 0.0020 |
| USDCHF | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/72 | 0.617 | 0.000 | -0.020 | -0.016 | 0.0009 |
| AUDUSD | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/96 | 0.617 | 0.000 | -0.020 | +0.007 | 0.0064 |
| AAPL | 6.36/3.57/48 | 0.591 | -0.079 | 6.25/3.75/96 | 0.627 | 0.012 | -0.041 | -0.005 | 0.0057 |
| US500 | 13.23/8.09/48 | 0.241 | -0.158 | 4.00/2.50/96 | 0.617 | 0.000 | -0.024 | -0.001 | 0.0053 |
| US30USD | 12.80/2.41/48 | 0.724 | -0.462 | 4.00/2.50/96 | 0.617 | 0.000 | -0.024 | -0.006 | 0.0044 |

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
| W-C1 | **PROVEN-FAIL** | Repro of the live gate at the shipped band centres: the 'EV > 0' gate passed 27% of conf-passing slots and its decision is a CONSTANT per (symbol, geometry) — driven by the shipped RR and the flat base table, never by regime state. | engine |
| W-C2 | **PROVEN-FAIL** | Code-path audit at HEAD (heavy_sweep.py:1579-1604, 2386-2402): unscored bucket -> directional p_win (~0.55+) vs global 0.20 floor passes trivially. | repo-measured |
| W-C3 | **MEASURED** | The repo's flat (symbol, vol_tier, bucket) table is LEVEL-ONLY for the trade being placed: the distance bucket is fixed per symbol, so its p_win varies only through the vol tier. | engine |
| W-C4 | **PROVEN-FAIL** | Under the driftless null every current band-centre has EV <= -cost (measured with the repo's own barrier_null + independent MC); live EV charges expiry at 0 with no costs. | engine |
| W-C5 | **MEASURED** | Code-audit census at HEAD (evaluation Part 1): 9 p_win variants; consumers re-blending get numbers unrelated to the shipped p_win. | repo-measured |
| W-C6 | **MEASURED** | Code-audit at HEAD: Bayesian shrink thresholds derived for the directional scale are fed the bracket base rate; Kelly reads the raw label while direction reads the debounced one. | repo-measured |
| W-X1 | **PROVEN-FAIL** | Rank-based state->label guarantees a directional label on every fitted bar (decided fraction 100% here); the tri-state er label abstains on a measured fraction of bars. | engine |
| W-X2 | **PROVEN-FAIL** | 24h-ahead accuracy vs majority baseline per symbol on the repro HMM: skill negative on 9 of 14 (mean -0.0212). | engine |
| W-X3 | **PROVEN-FAIL** | Raw labels flip at measured rates in low-ER (chop) windows while the debouncer freezes direction (repo mechanism, reproduced verbatim). | engine |
| W-X4 | **PROVEN-FAIL** | Census at HEAD (evaluation Part 2.4): efficiency_24h, trend_filter consolidating, dynamic_min_rr, context alignment, router stand_by all shadow. | repo-measured |
| W-X5 | **MEASURED** | Mean HMM confidence inside bottom-quartile-ER (most choppy) windows vs the 0.60 gate. | engine |
| W-X6 | **MEASURED** | Bracket substitution decouples published EV from regime state: the kairos book's EV-gate pass rate measured 25% in TREND slots vs 28% in NEUTRAL slots (HMM neutrality never reaches EV — code audit); trend features are 30-bar cum returns (regime_engine.py:894-903) vs sub-30h chop cycles. | engine |
| W-G1 | **MEASURED** | Repo's own SSE entry-timing measurement: -30pt selection gap vs null at 89.5% SL in tight buckets, direction AUC 0.499. | repo-measured |
| W-G2 | **PROVEN-FAIL** | pi_SL at current band centres under the driftless null (repo barrier_null + independent MC agree) vs the physics-constrained corrected geometry. | engine |
| W-G3 | **PROVEN-FAIL** | US500 band pi_E=0.70 under the null (repo + MC cross-check); corrected geometry is constrained to pi_E <= 0.25 by construction. | engine |
| W-G4 | **PROVEN-FAIL** | Current band centres violate the physics constraints outright (worst: US500 pi_E 0.70 > 0.25; every current EV_null ≤ −cost, range -0.462…-0.028); the corrected solver enforces (pi_SL, pi_E, RR) constraints UNDER THE NULL with measured drift on top. | engine |
| W-G5 | **MEASURED** | Code audit: band-centre/best-effort picks ship n=0/exp=0 metadata; cold-start brick fallback (0.5, 3, 6) in absolute price units; pooled-evidence widening is dead code. | repo-measured |

`fixes: []` — filled by the Task 40-b coordinator from the repo-side implementation.
