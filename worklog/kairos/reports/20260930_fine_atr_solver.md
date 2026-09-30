# Fine continuous ATR solver (0.05 lattice)

EV = wr·TP − (1−wr)·SL − cost · cost=0.1 ATR
Band: wr ∈ [0.40, 0.60] · exp ≤ 0.20 · maximize EV

| class | TP | SL | RR | wr | exp | EV | n |
|---|---:|---:|---:|---:|---:|---:|---:|
| commodity | **1.86** | **0.53** | 3.509 | 0.406 | 0.000 | **+0.1293** | 1457 |
| crypto | **1.04** | **0.22** | 4.727 | 0.402 | 0.000 | **+0.0034** | 388 |
| equity | **13.00** | **7.00** | 1.857 | 0.403 | 0.194 | **+1.3262** | 925 |
| forex | **1.71** | **0.35** | 4.886 | 0.401 | 0.000 | **+0.2543** | 2221 |

No brick integers. No 0.5 steps. True fractional ATR on price levels.
