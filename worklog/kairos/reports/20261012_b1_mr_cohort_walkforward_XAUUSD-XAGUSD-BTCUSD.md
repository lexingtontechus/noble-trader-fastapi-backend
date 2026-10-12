# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:43:07.634037+00:00
Symbols: ['XAUUSD', 'XAGUSD', 'BTCUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 1923

## Per-class (pooled)

### explicit (pooled)
  false-cohort: n= 224  WR=0.179
  pass-cohort:  n=1699  WR=0.182
  overall ratio: 0.98x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=190 WR=0.142 | pass n=771 WR=0.154
    H2 (held-out):  false n= 34 WR=0.382 | pass n=928 WR=0.206  ratio=1.86x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (1.86x)  => FAIL

## Per-symbol

### XAUUSD
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 596  WR=0.131
  overall ratio: 2.92x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=337 WR=0.151
    H2 (held-out):  false n= 34 WR=0.382 | pass n=259 WR=0.104  ratio=3.67x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.67x)  => FAIL

### XAGUSD
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 410  WR=0.239
  overall ratio: 0.65x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=130 WR=0.238
    H2 (held-out):  false n=  0 WR=0.000 | pass n=280 WR=0.239  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### BTCUSD
  false-cohort: n=  35  WR=0.086
  pass-cohort:  n= 693  WR=0.193
  overall ratio: 0.44x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n= 35 WR=0.086 | pass n=304 WR=0.122
    H2 (held-out):  false n=  0 WR=0.000 | pass n=389 WR=0.249  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Per (symbol, direction)

### XAUUSD buy
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 183  WR=0.077
  overall ratio: 5.00x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 96 WR=0.083
    H2 (held-out):  false n= 34 WR=0.382 | pass n= 87 WR=0.069  ratio=5.54x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (5.54x)  => FAIL

### XAUUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 413  WR=0.155
  overall ratio: 0.00x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=241 WR=0.178
    H2 (held-out):  false n=  0 WR=0.000 | pass n=172 WR=0.122  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAGUSD buy
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 169  WR=0.325
  overall ratio: 0.48x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n= 20 WR=0.400
    H2 (held-out):  false n=  0 WR=0.000 | pass n=149 WR=0.315  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAGUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 241  WR=0.178
  overall ratio: 0.00x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=110 WR=0.209
    H2 (held-out):  false n=  0 WR=0.000 | pass n=131 WR=0.153  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### BTCUSD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 487  WR=0.240
  overall ratio: 0.00x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=221 WR=0.167
    H2 (held-out):  false n=  0 WR=0.000 | pass n=266 WR=0.301  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### BTCUSD sell
  false-cohort: n=  35  WR=0.086
  pass-cohort:  n= 206  WR=0.083
  overall ratio: 1.04x
  split @ 2026-10-07T11:01:07.04841+00:00
    H1 (in-sample): false n= 35 WR=0.086 | pass n= 83 WR=0.000
    H2 (held-out):  false n=  0 WR=0.000 | pass n=123 WR=0.138  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
explicit (pooled)           224     0.179    0.182       0.98          34      1.86     FAIL
XAUUSD                       34     0.382    0.131       2.92          34      3.67     FAIL
XAGUSD                      155     0.155    0.239       0.65           0      0.00     FAIL
BTCUSD                       35     0.086    0.193       0.44           0      0.00     FAIL
