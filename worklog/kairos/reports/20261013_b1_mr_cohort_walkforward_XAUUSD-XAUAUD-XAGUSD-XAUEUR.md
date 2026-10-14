# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-13T20:08:02.262998+00:00
Symbols: ['XAUUSD', 'XAUAUD', 'XAGUSD', 'XAUEUR']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 2704

## Per-class (pooled)

### explicit (pooled)
  false-cohort: n= 189  WR=0.196
  pass-cohort:  n=2515  WR=0.132
  overall ratio: 1.49x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=1197 WR=0.120
    H2 (held-out):  false n= 34 WR=0.382 | pass n=1318 WR=0.142  ratio=2.69x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (2.69x)  => FAIL

## Per-symbol

### XAUUSD
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 657  WR=0.123
  overall ratio: 3.10x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=352 WR=0.151
    H2 (held-out):  false n= 34 WR=0.382 | pass n=305 WR=0.092  ratio=4.16x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (4.16x)  => FAIL

### XAUAUD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 689  WR=0.099
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=351 WR=0.097
    H2 (held-out):  false n=  0 WR=0.000 | pass n=338 WR=0.101  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAGUSD
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 482  WR=0.247
  overall ratio: 0.63x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=143 WR=0.217
    H2 (held-out):  false n=  0 WR=0.000 | pass n=339 WR=0.260  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAUEUR
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 687  WR=0.092
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=351 WR=0.074
    H2 (held-out):  false n=  0 WR=0.000 | pass n=336 WR=0.110  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Per (symbol, direction)

### XAUUSD buy
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 230  WR=0.065
  overall ratio: 5.86x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 96 WR=0.083
    H2 (held-out):  false n= 34 WR=0.382 | pass n=134 WR=0.052  ratio=7.32x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (7.32x)  => FAIL

### XAUUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 427  WR=0.155
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=256 WR=0.176
    H2 (held-out):  false n=  0 WR=0.000 | pass n=171 WR=0.123  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 312  WR=0.135
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=134 WR=0.082
    H2 (held-out):  false n=  0 WR=0.000 | pass n=178 WR=0.174  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 377  WR=0.069
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=217 WR=0.106
    H2 (held-out):  false n=  0 WR=0.000 | pass n=160 WR=0.019  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAGUSD buy
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 206  WR=0.291
  overall ratio: 0.53x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n= 24 WR=0.333
    H2 (held-out):  false n=  0 WR=0.000 | pass n=182 WR=0.286  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAGUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 276  WR=0.214
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=119 WR=0.193
    H2 (held-out):  false n=  0 WR=0.000 | pass n=157 WR=0.229  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 388  WR=0.088
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=171 WR=0.082
    H2 (held-out):  false n=  0 WR=0.000 | pass n=217 WR=0.092  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 299  WR=0.097
  overall ratio: 0.00x
  split @ 2026-10-07T14:46:32.536109+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=180 WR=0.067
    H2 (held-out):  false n=  0 WR=0.000 | pass n=119 WR=0.143  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
explicit (pooled)           189     0.196    0.132       1.49          34      2.69     FAIL
XAUUSD                       34     0.382    0.123       3.10          34      4.16     FAIL
XAUAUD                        0     0.000    0.099       0.00           0      0.00  NO_DATA
XAGUSD                      155     0.155    0.247       0.63           0      0.00     FAIL
XAUEUR                        0     0.000    0.092       0.00           0      0.00  NO_DATA
