# Replay — class / goal / best · exact ATR vs brick-quantised

Window 2026-09-14 → 2026-09-28 · cost 0.1 ATR · band w∈[0.40,0.60] exp≤0.20

| class | TP | SL | RR | wr | exp | EV | goal | note |
|---|---:|---:|---:|---:|---:|---:|---|---|
| commodity | 1.5 | 0.5 | 3.00 | 0.422 | 0.000 | **+0.095** | BAL | BAL+EV |
| commodity (brick) | 1.50 | 0.50 | 3.00 | 0.422 | 0.000 | +0.095 | — | quantised tp_bricks=9 |
| crypto | 1 | 0.5 | 2.00 | 0.443 | 0.000 | **-0.066** | BAL | EV&lt;0 |
| crypto (brick) | 1.00 | 0.50 | 2.00 | 0.443 | 0.000 | -0.066 | — | quantised tp_bricks=6 |
| equity | 13 | 7 | 1.86 | 0.403 | 0.194 | **+1.326** | BAL | BAL+EV |
| equity (brick) | 14.00 | 7.00 | 2.00 | 0.368 | 0.232 | +1.144 | — | quantised tp_bricks=6 |
| forex | 3 | 1 | 3.00 | 0.400 | 0.000 | **+0.246** | BAL | BAL+EV |
| forex (brick) | 3.00 | 1.00 | 3.00 | 0.400 | 0.000 | +0.246 | — | quantised tp_bricks=9 |

## Crypto / fractional ATR

Crypto best in-band is a **small TP/SL** (often 1–2 ATR). Brick snap with
sl_bricks=3 moves TP by up to **0.5 × sl_atr** (round(tp/sl*3)). On 1 ATR
TP that is a **large relative error** — live geometry ≠ validated geometry.
Exact price brackets (`entry ± tp_atr×ATR`) keep the walk’s EV.
