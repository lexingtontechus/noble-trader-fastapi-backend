---
# Regimes, features, tri-state hysteresis

## The regime value model (`values.py`)

Three causal, point-in-time numeric values per bar:

| value | source | range | definition |
|---|---|---|---|
| `trend` | `er_signed` | [-1, +1] | signed 24h Kaufman efficiency ratio; magnitude = trendiness, sign = direction |
| `chop` | `chop` | [0, 1] | Choppiness index / 100 (1 = pure chop) |
| `vol` | `vol_pct` | [0, 1] | rolling percentile rank of ATR% within its trailing 720-bar window |

Plus a derived **tri-state** label built from `er_signed` with hysteresis:

```python
ENTER = 0.35   # enter a trend state
EXIT  = 0.25   # drop back to NEUTRAL
state = "neutral"
if v >=  ENTER:
    state = "trend_up"
elif v <= -ENTER:  # full opposite reading flips directly
    state = "trend_down"
elif abs(v) < EXIT:
    state = "neutral"       # dead-band reset, else keep current
```

- Start `neutral`. Enter TREND_UP when `er_signed >= +0.35`, TREND_DOWN when <= -0.35.
- Exit to NEUTRAL when `|er_signed| < 0.25`.
- Dead band [0.25, 0.35): keep current state (debounce-like).
- Sub-0.25 resets to NEUTRAL. Full opposite reading flips directly.

**Why the tri-state matters (W-X1):** the live HMM's 4-state label scheme is
rank-based (vol label + trend label) and always produces a directional label
on a fitted bar — it has no NEUTRAL. That forces a direction on every tick,
including chop. The tri-state `er` label lets the engine abstain and is what
makes the chop gate (W-X4) possible.

## Feature set (`features.py`, all causal)

| feature | source | notes |
|---|---|---|
| `atr` | Wilder smoothing of true range, n=14 | sigma = 1 ATR/bar in barrier math |
| `atr_pct` | `atr / close` | input to `vol_pct` |
| `er` / `er_signed` | Kaufman efficiency ratio, 24h window | abs / signed; trend value = er_signed |
| `chop` | Choppiness index (14) / 100 | regime value = chop |
| `adx` | Wilder ADX (14) | one of the 5 edge-surface test features |
| `hurst` | rolling Hurst exponent (window 96) | mean-reverting vs trending tendency |
| `vr_z` | Lo-MacKinlay variance-ratio z-stat | iid null over window 240 |
| `vol_pct` | rolling percentile rank of atr_pct (trailing 720) | regime value = vol |
| `run_len` | signed consecutive same-direction run | directional persistence nudge |

**No-lookahead guarantee (pinned by test):** every rolling window is trailing,
Wilder recursions run forward, warm-up regions carry NaN (never backfilled —
backfilling IS lookahead). `test_no_lookahead_features_values` proves that
truncating the series never changes earlier outputs (full-series identity for
t < cutoff).

## Edge-surface feature family

Significance tests run over exactly these 5 features
(`edge.py FEATURE_KEYS = ("er_abs", "chop", "vol", "hurst", "adx")`),
each bucketed into deciles with bootstrap 95% CIs and Spearman rho + 2000-
permutation p-values vs the bracket R:

- `er_abs` — nowhere significant
- `chop`, `vol`, `hurst`, `adx` — 2/25 cells significant by chance
  (XAUUSD-ADX rho=-0.1165 p=0.006; USDJPY-Hurst rho=+0.0797 p=0.030;
  ~1.3 expected by chance).

## Key numbers

- `er` / `er_signed`: 24-bar lookback.
- `chop` / `atr` / `adx`: 14-bar Wilder window.
- `vol_pct`: trailing 720, min_periods 240.
- `hurst`: window 96, ks=(8,16,32,64).
- `vr_z`: window 240, q=48.
- Slot grid: every 24 bars from warmup 720.
- `mu_hat`: shrunk mean of direction-aligned 24h ATR-normalised moves over
  TREND-labeled TRAIN slots, shrinkage n/(n+100).

## Source files

- `regime_edge/__init__.py` — module map + ENGINE/SEED.
- `regime_edge/features.py` — `compute_all()` causal feature bundle.
- `regime_edge/values.py` — `regime_values()`, `tri_state_hysteresis()`.
