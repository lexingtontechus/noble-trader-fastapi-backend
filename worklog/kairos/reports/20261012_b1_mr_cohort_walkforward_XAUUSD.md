# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:42:27.234908+00:00
Symbols: ['XAUUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 630

## Per-class (pooled)

### explicit (pooled)
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 596  WR=0.131
  overall ratio: 2.92x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=315 WR=0.159
    H2 (held-out):  false n= 34 WR=0.382 | pass n=281 WR=0.100  ratio=3.84x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.84x)  => FAIL

## Per-symbol

### XAUUSD
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 596  WR=0.131
  overall ratio: 2.92x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=315 WR=0.159
    H2 (held-out):  false n= 34 WR=0.382 | pass n=281 WR=0.100  ratio=3.84x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.84x)  => FAIL

## Per (symbol, direction)

### XAUUSD buy
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 183  WR=0.077
  overall ratio: 5.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 80 WR=0.087
    H2 (held-out):  false n= 34 WR=0.382 | pass n=103 WR=0.068  ratio=5.63x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (5.63x)  => FAIL

### XAUUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 413  WR=0.155
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=235 WR=0.183
    H2 (held-out):  false n=  0 WR=0.000 | pass n=178 WR=0.118  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
explicit (pooled)            34     0.382    0.131       2.92          34      3.84     FAIL
XAUUSD                       34     0.382    0.131       2.92          34      3.84     FAIL
