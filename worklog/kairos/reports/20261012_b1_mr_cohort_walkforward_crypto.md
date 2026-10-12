# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:42:23.214785+00:00
Symbols: ['BTCUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 728

## Per-class (pooled)

### crypto (pooled)
  false-cohort: n=  35  WR=0.086
  pass-cohort:  n= 693  WR=0.193
  overall ratio: 0.44x
  split @ 2026-10-07T17:12:47.111873+00:00
    H1 (in-sample): false n= 35 WR=0.086 | pass n=329 WR=0.119
    H2 (held-out):  false n=  0 WR=0.000 | pass n=364 WR=0.261  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Per-symbol

### BTCUSD
  false-cohort: n=  35  WR=0.086
  pass-cohort:  n= 693  WR=0.193
  overall ratio: 0.44x
  split @ 2026-10-07T17:12:47.111873+00:00
    H1 (in-sample): false n= 35 WR=0.086 | pass n=329 WR=0.119
    H2 (held-out):  false n=  0 WR=0.000 | pass n=364 WR=0.261  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Per (symbol, direction)

### BTCUSD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 487  WR=0.240
  overall ratio: 0.00x
  split @ 2026-10-07T17:12:47.111873+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=238 WR=0.160
    H2 (held-out):  false n=  0 WR=0.000 | pass n=249 WR=0.317  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### BTCUSD sell
  false-cohort: n=  35  WR=0.086
  pass-cohort:  n= 206  WR=0.083
  overall ratio: 1.04x
  split @ 2026-10-07T17:12:47.111873+00:00
    H1 (in-sample): false n= 35 WR=0.086 | pass n= 91 WR=0.011
    H2 (held-out):  false n=  0 WR=0.000 | pass n=115 WR=0.139  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
crypto (pooled)              35     0.086    0.193       0.44           0      0.00     FAIL
BTCUSD                       35     0.086    0.193       0.44           0      0.00     FAIL
