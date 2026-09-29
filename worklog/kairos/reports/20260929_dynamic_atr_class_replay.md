# dynamic_atr — class-level grid fit (all symbols)

Settled light 2026-09-20→27. Groups via `asset_class_key` (same as
`dynamic_atr_pick`). No per-symbol overrides.

## class `commodity` (n=2684 · XAGUSD, XAUAUD, XAUEUR, XAUUSD)

| TP band | SL band | n | exp% | wr | pnl_bricks |
|---|---|---:|---:|---:|---:|
| TP 3-8 | SL 2-4 | 175 | 0.000 | 0.406 | +0.651 |
| TP 6-12 | SL 2-4 | 240 | 0.042 | 0.061 | -2.325 |
| TP 6-12 | SL 4-8 | 462 | 0.353 | 0.171 | -0.744 |
| TP 9-17 | SL 2-4 | 227 | 0.044 | 0.014 | -2.722 |
| TP 9-17 | SL 4-8 | 726 | 0.507 | 0.112 | -0.672 |
| TP 9-17 | SL 8-40 | 89 | 0.775 | 0.3 | +0.465 |
| TP 12-24 | SL 4-8 | 522 | 0.540 | 0.046 | -0.885 |
| TP 12-24 | SL 8-40 | 505 | 0.792 | 0.086 | -0.771 |
| TP 17-30 | SL 4-8 | 222 | 0.631 | 0.0 | -0.755 |
| TP 17-30 | SL 8-40 | 475 | 0.817 | 0.034 | -0.831 |
| TP 30-80 | SL 8-40 | 95 | 1.000 | None | +0.198 |

**Class pick:** TP 3-8 / SL 2-4 → n=175 exp=0.000 wr=0.406 **pnl=+0.651**
```yaml
# feature_flags dynamic_atr.by_class.commodity
tp_atr: [3, 8]
sl_atr: [2, 4]
tp_atr_min: 3
tp_atr_max: 8
sl_atr_min: 2
sl_atr_max: 4
```

## class `crypto` (n=671 · BTCUSD)

| TP band | SL band | n | exp% | wr | pnl_bricks |
|---|---|---:|---:|---:|---:|
| TP 6-12 | SL 4-8 | 54 | 0.426 | 0.0 | -1.513 |
| TP 9-17 | SL 8-40 | 53 | 0.962 | 0.0 | +1.967 |
| TP 12-24 | SL 8-40 | 86 | 0.977 | 0.0 | +1.150 |
| TP 30-80 | SL 8-40 | 231 | 0.874 | 0.0 | +0.930 |

**Class pick:** insufficient n in every grid cell — keep global defaults.

## class `equity` (n=2005 · AAPL, US30USD, US500)

| TP band | SL band | n | exp% | wr | pnl_bricks |
|---|---|---:|---:|---:|---:|
| TP 3-8 | SL 4-8 | 88 | 0.000 | 0.114 | -3.523 |
| TP 6-12 | SL 2-4 | 173 | 0.000 | 0.329 | +0.659 |
| TP 6-12 | SL 4-8 | 401 | 0.050 | 0.234 | -1.710 |
| TP 9-17 | SL 2-4 | 164 | 0.000 | 0.335 | +0.726 |
| TP 9-17 | SL 4-8 | 591 | 0.091 | 0.471 | +0.881 |
| TP 9-17 | SL 8-40 | 94 | 0.617 | 0.056 | -0.966 |
| TP 12-24 | SL 4-8 | 480 | 0.133 | 0.464 | +0.911 |
| TP 12-24 | SL 8-40 | 259 | 0.266 | 0.047 | -4.089 |
| TP 17-30 | SL 4-8 | 109 | 0.275 | 0.013 | -2.034 |
| TP 17-30 | SL 8-40 | 252 | 0.349 | 0.091 | -3.420 |
| TP 30-80 | SL 8-40 | 457 | 0.777 | 0.137 | +0.092 |

**Class pick:** TP 12-24 / SL 4-8 → n=480 exp=0.133 wr=0.464 **pnl=+0.911**
```yaml
# feature_flags dynamic_atr.by_class.equity
tp_atr: [12, 24]
sl_atr: [4, 8]
tp_atr_min: 12
tp_atr_max: 24
sl_atr_min: 4
sl_atr_max: 8
```

## class `forex` (n=4017 · AUDUSD, EURUSD, GBPUSD, USDCAD, USDCHF, USDJPY)

| TP band | SL band | n | exp% | wr | pnl_bricks |
|---|---|---:|---:|---:|---:|
| TP 6-12 | SL 4-8 | 90 | 0.989 | 0.0 | +1.834 |
| TP 9-17 | SL 4-8 | 91 | 0.989 | 0.0 | +1.821 |
| TP 9-17 | SL 8-40 | 143 | 0.238 | 0.0 | -2.373 |
| TP 12-24 | SL 8-40 | 341 | 0.378 | 0.066 | -0.925 |
| TP 17-30 | SL 8-40 | 968 | 0.642 | 0.176 | -0.127 |
| TP 30-80 | SL 8-40 | 2351 | 0.882 | 0.112 | -0.345 |

**Class pick:** insufficient n in every grid cell — keep global defaults.
