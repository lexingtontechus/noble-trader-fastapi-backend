# B1 — context-alignment false-cohort walk-forward
Generated: 2026-10-12T20:43:04.113790+00:00
Symbols: ['USDJPY', 'EURUSD', 'GBPUSD', 'USDCAD', 'USDCHF', 'AUDUSD']
Direction: all | Era: post 2026-10-02, contaminated=false, tp_hit/sl_hit only
Bar: WR(false) >= 1.5x WR(pass) on held-out half, n >= 40

Resolved era-clean rows: 3321

## Per-class (pooled)

### forex (pooled)
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n=3321  WR=0.101
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=1660 WR=0.113
    H2 (held-out):  false n=  0 WR=0.000 | pass n=1661 WR=0.090  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Per-symbol

### USDJPY
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 468  WR=0.090
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=239 WR=0.054
    H2 (held-out):  false n=  0 WR=0.000 | pass n=229 WR=0.127  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### EURUSD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 610  WR=0.097
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=325 WR=0.095
    H2 (held-out):  false n=  0 WR=0.000 | pass n=285 WR=0.098  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### GBPUSD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 582  WR=0.103
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=287 WR=0.084
    H2 (held-out):  false n=  0 WR=0.000 | pass n=295 WR=0.122  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCAD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 444  WR=0.135
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=168 WR=0.220
    H2 (held-out):  false n=  0 WR=0.000 | pass n=276 WR=0.083  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCHF
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 599  WR=0.127
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=321 WR=0.178
    H2 (held-out):  false n=  0 WR=0.000 | pass n=278 WR=0.068  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### AUDUSD
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 618  WR=0.065
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=320 WR=0.081
    H2 (held-out):  false n=  0 WR=0.000 | pass n=298 WR=0.047  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Per (symbol, direction)

### USDJPY buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 286  WR=0.059
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=188 WR=0.032
    H2 (held-out):  false n=  0 WR=0.000 | pass n= 98 WR=0.112  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDJPY sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 182  WR=0.137
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 51 WR=0.137
    H2 (held-out):  false n=  0 WR=0.000 | pass n=131 WR=0.137  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### EURUSD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 304  WR=0.112
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=137 WR=0.124
    H2 (held-out):  false n=  0 WR=0.000 | pass n=167 WR=0.102  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### EURUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 306  WR=0.082
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=188 WR=0.074
    H2 (held-out):  false n=  0 WR=0.000 | pass n=118 WR=0.093  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### GBPUSD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 230  WR=0.130
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=106 WR=0.132
    H2 (held-out):  false n=  0 WR=0.000 | pass n=124 WR=0.129  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### GBPUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 352  WR=0.085
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=181 WR=0.055
    H2 (held-out):  false n=  0 WR=0.000 | pass n=171 WR=0.117  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCAD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 197  WR=0.142
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 79 WR=0.228
    H2 (held-out):  false n=  0 WR=0.000 | pass n=118 WR=0.085  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCAD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 247  WR=0.130
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 89 WR=0.213
    H2 (held-out):  false n=  0 WR=0.000 | pass n=158 WR=0.082  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCHF buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 176  WR=0.159
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n= 40 WR=0.575
    H2 (held-out):  false n=  0 WR=0.000 | pass n=136 WR=0.037  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### USDCHF sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 423  WR=0.113
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=281 WR=0.121
    H2 (held-out):  false n=  0 WR=0.000 | pass n=142 WR=0.099  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### AUDUSD buy
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 335  WR=0.072
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=119 WR=0.109
    H2 (held-out):  false n=  0 WR=0.000 | pass n=216 WR=0.051  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

### AUDUSD sell
  false-cohort: n=   0  WR=0.000
  pass-cohort:  n= 283  WR=0.057
  overall ratio: 0.00x
  split @ 2026-10-07T06:30:27.623123+00:00
    H1 (in-sample): false n=  0 WR=0.000 | pass n=201 WR=0.065
    H2 (held-out):  false n=  0 WR=0.000 | pass n= 82 WR=0.037  ratio=0.00x
    VERDICT: NO_DATA — context_alignment gate never opposes this cohort (0 false-cohort rows). No MR inversion exists to validate.

## Summary
cell                    n_false  WR_false  WR_pass  ratio_all  n_false_h2  ratio_h2  verdict
forex (pooled)                0     0.000    0.101       0.00           0      0.00  NO_DATA
USDJPY                        0     0.000    0.090       0.00           0      0.00  NO_DATA
EURUSD                        0     0.000    0.097       0.00           0      0.00  NO_DATA
GBPUSD                        0     0.000    0.103       0.00           0      0.00  NO_DATA
USDCAD                        0     0.000    0.135       0.00           0      0.00  NO_DATA
USDCHF                        0     0.000    0.127       0.00           0      0.00  NO_DATA
AUDUSD                        0     0.000    0.065       0.00           0      0.00  NO_DATA
