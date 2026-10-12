# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:42:20.737474+00:00
Symbols: ['XAUEUR', 'XAUAUD', 'XAGUSD', 'XAUUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 2455

## Per-class (pooled)

### commodities (pooled)
  false-cohort: n= 189  WR=0.196
  pass-cohort:  n=2266  WR=0.128
  overall ratio: 1.52x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=1072 WR=0.124
    H2 (held-out):  false n= 34 WR=0.382 | pass n=1194 WR=0.132  ratio=2.89x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (2.89x)  => FAIL

## Per-symbol

### XAUEUR
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 626  WR=0.085
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=319 WR=0.078
    H2 (held-out):  false n=  0 WR=0.000 | pass n=307 WR=0.091  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 634  WR=0.098
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=319 WR=0.094
    H2 (held-out):  false n=  0 WR=0.000 | pass n=315 WR=0.102  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAGUSD
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 410  WR=0.239
  overall ratio: 0.65x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n=114 WR=0.246
    H2 (held-out):  false n=  0 WR=0.000 | pass n=296 WR=0.236  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAUUSD
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 596  WR=0.131
  overall ratio: 2.92x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=320 WR=0.156
    H2 (held-out):  false n= 34 WR=0.382 | pass n=276 WR=0.101  ratio=3.77x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (3.77x)  => FAIL

## Per (symbol, direction)

### XAUEUR buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 350  WR=0.097
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=139 WR=0.094
    H2 (held-out):  false n=  0 WR=0.000 | pass n=211 WR=0.100  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUEUR sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 276  WR=0.069
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=180 WR=0.067
    H2 (held-out):  false n=  0 WR=0.000 | pass n= 96 WR=0.073  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 260  WR=0.142
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=129 WR=0.078
    H2 (held-out):  false n=  0 WR=0.000 | pass n=131 WR=0.206  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUAUD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 374  WR=0.067
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=190 WR=0.105
    H2 (held-out):  false n=  0 WR=0.000 | pass n=184 WR=0.027  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAGUSD buy
  false-cohort: n= 155  WR=0.155
  pass-cohort:  n= 169  WR=0.325
  overall ratio: 0.48x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=155 WR=0.155 | pass n= 17 WR=0.412
    H2 (held-out):  false n=  0 WR=0.000 | pass n=152 WR=0.316  ratio=0.00x
    BAR: n>=40 FAIL (0)  ratio>=1.5 FAIL (0.00x)  => FAIL

### XAGUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 241  WR=0.178
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 97 WR=0.216
    H2 (held-out):  false n=  0 WR=0.000 | pass n=144 WR=0.153  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### XAUUSD buy
  false-cohort: n=  34  WR=0.382
  pass-cohort:  n= 183  WR=0.077
  overall ratio: 5.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 83 WR=0.084
    H2 (held-out):  false n= 34 WR=0.382 | pass n=100 WR=0.070  ratio=5.46x
    BAR: n>=40 FAIL (34)  ratio>=1.5 PASS (5.46x)  => FAIL

### XAUUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 413  WR=0.155
  overall ratio: 0.00x
  split @ 2026-10-07T04:16:17.81519+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=237 WR=0.181
    H2 (held-out):  false n=  0 WR=0.000 | pass n=176 WR=0.119  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
commodities (pooled)        189     0.196    0.128       1.52          34      2.89     FAIL
XAUEUR                        0     0.000    0.085       0.00           0      0.00  NO_DATA
XAUAUD                        0     0.000    0.098       0.00           0      0.00  NO_DATA
XAGUSD                      155     0.155    0.239       0.65           0      0.00     FAIL
XAUUSD                       34     0.382    0.131       2.92          34      3.77     FAIL
