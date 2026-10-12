# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:42:24.312079+00:00
Symbols: ['XAUUSD', 'XAUAUD', 'XAUEUR']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 1890

## Per-class (pooled)

### explicit (pooled)
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n=1856  WR=0.104
  overall ratio: 3.68x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=945 WR=0.111
    H2 (held-out):  false n= 34 WR=0.382 | pass n=911 WR=0.097  ratio=3.96x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.96x)  => FAIL

## Per-symbol

### XAUUSD
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 596  WR=0.131
  overall ratio: 2.92x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=315 WR=0.159
    H2 (held-out):  false n= 34 WR=0.382 | pass n=281 WR=0.100  ratio=3.84x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.84x)  => FAIL

### XAUAUD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 634  WR=0.098
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=315 WR=0.095
    H2 (held-out):  false n=  0 WR=0.000 | pass n=319 WR=0.100  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 626  WR=0.085
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=315 WR=0.079
    H2 (held-out):  false n=  0 WR=0.000 | pass n=311 WR=0.090  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

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

### XAUAUD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 260  WR=0.142
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=129 WR=0.078
    H2 (held-out):  false n=  0 WR=0.000 | pass n=131 WR=0.206  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 374  WR=0.067
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=186 WR=0.108
    H2 (held-out):  false n=  0 WR=0.000 | pass n=188 WR=0.027  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 350  WR=0.097
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=135 WR=0.096
    H2 (held-out):  false n=  0 WR=0.000 | pass n=215 WR=0.098  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 276  WR=0.069
  overall ratio: 0.00x
  split @ 2026-10-07T03:00:36.574851+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=180 WR=0.067
    H2 (held-out):  false n=  0 WR=0.000 | pass n= 96 WR=0.073  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
explicit (pooled)            34     0.382    0.104       3.68          34      3.96     FAIL
XAUUSD                       34     0.382    0.131       2.92          34      3.84     FAIL
XAUAUD                        0     0.000    0.098       0.00           0      0.00  NO_DATA
XAUEUR                        0     0.000    0.085       0.00           0      0.00  NO_DATA
