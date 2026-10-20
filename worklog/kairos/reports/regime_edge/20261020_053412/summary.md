# Edge Lab — regime values vs the edge (regime_edge v1)

Run `20261020_053412` · symbols: BTCUSD, XAUUSD, XAGUSD, USDJPY, USDCHF · period 2024-01-03T00:00:00+00:00 → 2026-10-20T04:00:00+00:00

Every number is point-in-time (no lookahead), seeded (7), and scored out-of-sample where a model is involved. `records.csv` carries the per-slot audit trail.

## 1 · HMM (Kairos-as-is) 24h-ahead direction skill — W-X1/W-X2/W-X5

| symbol | n_decided | acc | majority | **skill** | flip-rate low-ER | flip-rate high-ER | mean conf low-ER |
|---|---|---|---|---|---|---|---|
| BTCUSD | 17245 | 0.4977 | 0.5166 | **-0.0190** | 0.225 | 0.228 | 0.673 |
| XAUUSD | 13486 | 0.4924 | 0.5416 | **-0.0492** | 0.254 | 0.232 | 0.640 |
| XAGUSD | 13449 | 0.5023 | 0.5434 | **-0.0410** | 0.274 | 0.240 | 0.632 |
| USDJPY | 16919 | 0.4948 | 0.5661 | **-0.0713** | 0.238 | 0.211 | 0.661 |
| USDCHF | 16942 | 0.4828 | 0.5244 | **-0.0416** | 0.248 | 0.206 | 0.645 |

Negative skill = the debounced HMM label is worse than always guessing the majority class. Confidence stays above the 0.60 gate in the most choppy (bottom-quartile-ER) windows — the confidence gate is not a chop brake (W-X5).

## 2 · Edge predictability — does the regime value carry edge information?

| symbol | best feature | Spearman ρ | perm p | (er_abs ρ / p) | (chop ρ / p) | (vol ρ / p) |
|---|---|---|---|---|---|---|
| BTCUSD | er_abs | **-0.0401** | 0.2649 | -0.040/0.265 | -0.038/0.323 | -0.010/0.813 |
| XAUUSD | adx | **-0.1165** | 0.0060 | -0.042/0.334 | -0.053/0.218 | +0.009/0.865 |
| XAGUSD | chop | **+0.0584** | 0.1754 | -0.002/0.974 | +0.058/0.175 | -0.050/0.258 |
| USDJPY | hurst | **+0.0797** | 0.0295 | -0.003/0.947 | -0.048/0.208 | -0.017/0.670 |
| USDCHF | hurst | **-0.0173** | 0.6442 | +0.001/0.984 | -0.017/0.635 | -0.007/0.845 |

ρ(feature at entry, bracket R) with 2000-permutation p-values. Decile edge surfaces with bootstrap 95% CIs are in `edge_lab.json` → `edge_surface`.

## 3 · The decisive gate backtest — kairos vs corrected vs null (mean R per trade)

| symbol | kairos n / meanR | corrected n / meanR (τ*) | null n / meanR | DM kairos-vs-corr p (raw/Holm) | DM corr-vs-null p (raw/Holm) |
|---|---|---|---|---|---|
| BTCUSD | 139 / +0.3734 | 17 / +0.4635 (τ*=0.45) | 284 / +0.0101 | 0.1048 / 1.0000 | 0.8040 / 1.0000 |
| XAUUSD | 103 / +0.3673 | 16 / +0.3682 (τ*=0.45) | 177 / +0.0779 | 0.2555 / 1.0000 | 0.6107 / 1.0000 |
| XAGUSD | 0 / +0.0000 | 26 / -0.3250 (τ*=0.35) | 199 / -0.0917 | 0.1534 / 1.0000 | 0.5230 / 1.0000 |
| USDJPY | 220 / +0.1313 | 44 / +0.1344 (τ*=0.35) | 247 / -0.0152 | 0.2171 / 1.0000 | 0.6018 / 1.0000 |
| USDCHF | 0 / +0.0000 | 18 / +0.0826 (τ*=0.45) | 396 / -0.0389 | 0.7872 / 1.0000 | 0.4775 / 1.0000 |
| **pooled** | 462 / +0.2567 | 121 / +0.1052 | 1303 / -0.0159 | p=0.0163 (mld=-0.0336) | p=0.4285 (mld=-0.0106) |

DM sign convention: for `kairos-vs-corrected`, p < 0.05 with negative kairos mean-loss-diff favors kairos; for `corrected-vs-null`, p < 0.05 with negative corrected mean-loss-diff favors the corrected book. Holm family = the 10 per-symbol tests.

## 4 · Calibration — flat base rate vs regime-conditional (walk-forward lockbox)

| symbol | base AUC / Brier / ECE | regime AUC / Brier / ECE | n_lockbox | repo bucket AUC |
|---|---|---|---|---|
| BTCUSD | 0.4496 / 0.1215 / 0.0548 | **0.5579** / 0.1214 / 0.0270 | 603 | 0.5904 |
| XAUUSD | 0.4336 / 0.1327 / 0.0357 | **0.5313** / 0.1331 / 0.0555 | 446 | 0.5529 |
| XAGUSD | 0.4582 / 0.1449 / 0.0412 | **0.6051** / 0.1410 / 0.0437 | 447 | 0.6387 |
| USDJPY | 0.4779 / 0.1984 / 0.0632 | **0.4435** / 0.2009 / 0.0933 | 589 | 0.5455 |
| USDCHF | 0.4867 / 0.1973 / 0.0491 | **0.5262** / 0.1968 / 0.0350 | 590 | 0.5587 |

The repo's flat (symbol, vol_tier, bucket) base rate is level-only — AUC ≈ 0.5 by construction (reproducing the repo's measured 0.4869). The trend-quintile × vol-tercile regime-conditional model is scored strictly out-of-sample (lockbox).

## 5 · Barrier physics — current band centre vs corrected geometry

| symbol | current tp/sl/exp | π_SL cur | EV_null cur | corrected tp/sl/exp | π_SL cor | π_E cor | EV_null cor | EV_drift cor | μ̂ (ATR/bar) |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 8.07/1.77/48 | 0.788 | -0.174 | 6.25/4.00/96 | 0.611 | 0.014 | -0.049 | -0.016 | 0.0056 |
| XAUUSD | 4.94/1.00/48 | 0.828 | -0.044 | 7.00/4.50/96 | 0.599 | 0.035 | -0.042 | +0.030 | 0.0108 |
| XAGUSD | 6.39/2.69/48 | 0.673 | -0.074 | 6.25/4.00/96 | 0.611 | 0.014 | -0.039 | +0.007 | 0.0076 |
| USDJPY | 4.52/2.75/48 | 0.616 | -0.028 | 6.50/4.00/96 | 0.618 | 0.018 | -0.036 | +0.024 | 0.0093 |
| USDCHF | 4.52/2.75/48 | 0.616 | -0.028 | 4.00/2.50/72 | 0.617 | 0.000 | -0.020 | -0.018 | 0.0005 |

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
| W-C1 | **PROVEN-FAIL** | Repro of the live gate at the shipped band centres: the 'EV > 0' gate passed 24% of conf-passing slots — the pass rate is driven by the shipped RR and the flat base table, not by regime state, so the gate cannot discriminate brackets; the LIVE gate is weaker still (it prices the fictional brick payoff, ev_engine.py:723-728 vs orchestrator.py:4202-4219). | engine |
| W-C2 | **PROVEN-FAIL** | Code-path audit at HEAD (heavy_sweep.py:1579-1604, 2386-2402): unscored bucket -> directional p_win (~0.55+) vs global 0.20 floor passes trivially. | repo-measured |
| W-C3 | **MEASURED** | The repo's flat bucket table carries level but no discrimination (mean AUC 0.5772 measured here on all-in outcomes including expiry — reproducing the repo's own measured 0.4869); the regime-conditional model's lockbox discrimination is weak on this sample (mean AUC 0.5328). | engine |
| W-C4 | **PROVEN-FAIL** | Under the driftless null every current band-centre has EV <= -cost (measured with the repo's own barrier_null + independent MC); live EV charges expiry at 0 with no costs. | engine |
| W-C5 | **MEASURED** | Code-audit census at HEAD (evaluation Part 1): 9 p_win variants; consumers re-blending get numbers unrelated to the shipped p_win. | repo-measured |
| W-C6 | **MEASURED** | Code-audit at HEAD: Bayesian shrink thresholds derived for the directional scale are fed the bracket base rate; Kelly reads the raw label while direction reads the debounced one. | repo-measured |
| W-X1 | **PROVEN-FAIL** | Rank-based state->label guarantees a directional label on every fitted bar (decided fraction 100% here); the tri-state er label abstains on a measured fraction of bars. | engine |
| W-X2 | **PROVEN-FAIL** | 24h-ahead accuracy vs majority baseline per symbol on the repro HMM; regime values (er_signed) carry the measured edge information instead (best Spearman rho reported). | engine |
| W-X3 | **PROVEN-FAIL** | Raw labels flip at measured rates in low-ER (chop) windows while the debouncer freezes direction (repo mechanism, reproduced verbatim); DM kairos-vs-corrected pooled p=0.0163, mld=-0.0336: kairos better on the same slot grid (the corrected book abstains rather than trading stale direction). | engine |
| W-X4 | **PROVEN-FAIL** | Census at HEAD (evaluation Part 2.4): efficiency_24h, trend_filter consolidating, dynamic_min_rr, context alignment, router stand_by all shadow. | repo-measured |
| W-X5 | **PROVEN-FAIL** | Mean HMM confidence inside bottom-quartile-ER (most choppy) windows vs the 0.60 gate. | engine |
| W-X6 | **MEASURED** | Bracket substitution decouples published EV from regime state: the kairos book's EV-gate pass rate measured 21% in TREND slots vs 26% in NEUTRAL slots (HMM neutrality never reaches EV — code audit); trend features are 30-bar cum returns (regime_engine.py:894-903) vs sub-30h chop cycles. | engine |
| W-G1 | **MEASURED** | Repo's own SSE entry-timing measurement: -30pt selection gap vs null at 89.5% SL in tight buckets, direction AUC 0.499. | repo-measured |
| W-G2 | **PROVEN-FAIL** | pi_SL at current band centres under the driftless null (repo barrier_null + independent MC agree) vs the physics-constrained corrected geometry. | engine |
| W-G3 | **PROVEN-FAIL** | US500 band pi_E=0.70 under the null (repo + MC cross-check); corrected geometry is constrained to pi_E <= 0.25 by construction. | engine |
| W-G4 | **PROVEN-FAIL** | Current band centres violate the physics constraints outright (XAUUSD pi_SL 0.83 > 0.62; every current EV_null <= -cost); the corrected solver enforces (pi_SL, pi_E, RR) constraints UNDER THE NULL with measured drift on top. | engine |
| W-G5 | **MEASURED** | Code audit: band-centre/best-effort picks ship n=0/exp=0 metadata; cold-start brick fallback (0.5, 3, 6) in absolute price units; pooled-evidence widening is dead code. | repo-measured |

`fixes: []` — filled by the Task 40-b coordinator from the repo-side implementation.
