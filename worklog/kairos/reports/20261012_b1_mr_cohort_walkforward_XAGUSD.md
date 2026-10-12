# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:42:25.924949+00:00
Symbols: ['XAGUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 565

## Per-class (pooled)

### explicit (pooled)
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 410  WR=0.239
  overall ratio: 0.65x
  split @ 2026-10-07T10:31:03.414538+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=127 WR=0.244
    H2 (held-out):  false n=  0 WR=0.000 | pass n=283 WR=0.237  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Per-symbol

### XAGUSD
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 410  WR=0.239
  overall ratio: 0.65x
  split @ 2026-10-07T10:31:03.414538+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=127 WR=0.244
    H2 (held-out):  false n=  0 WR=0.000 | pass n=283 WR=0.237  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Per (symbol, direction)

### XAGUSD buy
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 169  WR=0.325
  overall ratio: 0.48x
  split @ 2026-10-07T10:31:03.414538+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n= 20 WR=0.400
    H2 (held-out):  false n=  0 WR=0.000 | pass n=149 WR=0.315  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAGUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 241  WR=0.178
  overall ratio: 0.00x
  split @ 2026-10-07T10:31:03.414538+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=107 WR=0.215
    H2 (held-out):  false n=  0 WR=0.000 | pass n=134 WR=0.149  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
explicit (pooled)           155     0.155    0.239       0.65           0      0.00     FAIL
XAGUSD                      155     0.155    0.239       0.65           0      0.00     FAIL
