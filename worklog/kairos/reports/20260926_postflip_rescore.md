# Post-flip rescore (2026-09-26) — era-split scorecard + G2.2 power

Generated 2026-09-26 21:17 UTC · read-only · `kairos_quant` eras · ATR scale `sweep_cc_sma14`.
Unit: light buy/sell rows with a decided outcome (TP/SL) for scorecard; episodes for G2.2.

## 0. Population by signal era (ts)

| era | n signals | decided | qualified | suppressed | gate_fail |
|---|---:|---:|---:|---:|---:|
| pre_debounce | 71654 | 34668 | 968 | 11329 | 59357 |
| debounce_to_atr | 3693 | 1546 | 25 | 566 | 3102 |
| atr_to_resolver | 1274 | 123 | 8 | 114 | 1152 |
| post_resolver | 1129 | 20 | 2 | 259 | 868 |

**Post-resolver decided (ts or exit in era):** 22. Promotion bar for evidence pool (ΔBrier ≤ −0.05 on S2) **cannot be met on this era alone** until qualified S2 volume exists here.

## 1. Scorecard D–E by era × tier (decided only)

| era | tier | n | TP rate | no-skill | mean err | mean acc | mean claimed | mean p_win |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| pre_debounce | qualified | 438 | 0.258 | 0.332 | 0.341 | -0.074 | 0.268 | 0.599 |
| pre_debounce | suppressed | 5017 | 0.255 | 0.337 | 0.378 | -0.082 | 0.296 | 0.632 |
| pre_debounce | gate_fail | 29213 | 0.269 | 0.324 | 0.144 | -0.055 | 0.089 | 0.413 |
| pre_debounce | all | 34668 | 0.267 | 0.326 | 0.180 | -0.059 | 0.121 | 0.447 |
| debounce_to_atr | qualified | 11 | 0.273 | 0.332 | 0.320 | -0.059 | 0.261 | 0.593 |
| debounce_to_atr | suppressed | 227 | 0.066 | 0.316 | 0.594 | -0.250 | 0.344 | 0.660 |
| debounce_to_atr | gate_fail | 1308 | 0.200 | 0.339 | 0.186 | -0.139 | 0.047 | 0.387 |
| debounce_to_atr | all | 1546 | 0.181 | 0.336 | 0.247 | -0.155 | 0.092 | 0.428 |
| atr_to_resolver | qualified | 1 | 0.000 | 0.231 | 0.564 | -0.231 | 0.333 | 0.564 |
| atr_to_resolver | suppressed | 17 | 0.000 | 0.253 | 0.609 | -0.253 | 0.356 | 0.609 |
| atr_to_resolver | gate_fail | 105 | 0.219 | 0.307 | 0.192 | -0.088 | 0.104 | 0.411 |
| atr_to_resolver | all | 123 | 0.187 | 0.299 | 0.253 | -0.112 | 0.141 | 0.440 |
| post_resolver | gate_fail | 20 | 0.000 | 0.303 | 0.623 | -0.303 | 0.320 | 0.623 |
| post_resolver | all | 20 | 0.000 | 0.303 | 0.623 | -0.303 | 0.320 | 0.623 |

**Exit-era post_resolver (incl. earlier entries):** n=22 TP=0.000 err=0.584 acc=-0.301

## 2. Evidence-pool ΔBrier walk-forward (all eras + post-ATR)

| rule | S2 scored | Brier uncor | Brier cor | ΔBrier |
|---|---:|---:|---:|---:|
| all tiers, prod 3d HL, nightly | 423 | 0.315 | 0.235 | -0.080 |
| q+s, prod 3d HL, nightly | 397 | 0.311 | 0.214 | -0.097 |
| q+s, 3h HL, hourly | 401 | 0.310 | 0.274 | -0.036 |
| q+s, 3h HL, nightly | 400 | 0.309 | 0.282 | -0.028 |
| gate_fail, 3h HL, hourly | 422 | 0.316 | 0.250 | -0.066 |

Note: walk-forward uses prior **decided** same-symbol evidence only (exit_ts < S2 ts). This is a scorecard proxy, not a byte-identical `fetch_calibration_bias` replay. Post-flip S2 volume is still too small for the −0.05 promote bar.

## 3. G2.2 — do gates select direction (Δm₂₄)?

Episodes with matured m₂₄: **9407** · symbol-day clusters: **419**
q∪s n=1357 mean m₂₄ (raw)=-0.657 · demeaned=-0.274
gate_fail n=8050 mean m₂₄ (raw)=-0.272 · demeaned=0.046
**Δm₂₄ demeaned (q∪s − gate_fail) = -0.321 [-1.871, +0.821]**

### H. Power criterion (demeaned m₂₄)
- m₂₄ SD = 10.025 · 0.2·SD = 2.005
- Δm₂₄ 95% CI half-width = 1.346 → **PASS**
- At the power bar — G2.2 can be decided (go/no-go), not merely monitored.

**Decision (pre-registered):** G2.2 **NO PASS** (point estimate negative, CI spans 0): no evidence gates select direction.

### Δm₂₄ (demeaned) by signal era

| era | n eps | q∪s n | gate_fail n | Δm₂₄ | CI lo | CI hi |
|---|---:|---:|---:|---:|---:|---:|
| pre_debounce | 8292 | 1192 | 7100 | -0.411 | -2.131 | +0.918 |
| debounce_to_atr | 1076 | 163 | 913 | +0.512 | -1.878 | +3.291 |
| atr_to_resolver | 39 | 2 | 37 | -3.880 | -15.968 | +2.539 |

## 4. Scorecard A–B proxy

- `nt_calibration_records` rows: 38853 · closed: 38853 · closed since 09-26: 24
- entered→closed delay (recent 2000): median 22.0h · p90 43.0h

