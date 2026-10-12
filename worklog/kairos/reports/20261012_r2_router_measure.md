# R2 — strategy-router measurement
Generated: 2026-10-12T20:56:08.156506+00:00
Scope: docs/scopes/20261012_STRATEGY_ROUTER_R2_MEASUREMENT.md
Era: post 2026-10-02, contaminated=false, stamped rows only
Bar: >= 200 routed episodes; >= 40 resolved per archetype

Stamped rows: 928  |  routed (non-stand_by): 4 across 1 symbols
  momentum         total=    4  resolved=    4
  stand_by         total=  924  resolved=  765

## Uniform-engine baseline (all stamped)
  n_resolved=769 WR=0.109 expectancy=-2.0182054616384915 bias=0.0009447369441184655

## H1 — routed vs stand_by
  UNDECIDABLE — 4 routed episodes < bar 200. The router declines 99.6% of signals.

## Per-archetype verdicts

### momentum
  n_total=4 n_resolved=4 n_expired=0 expired_share=0.000
  WR=0.000 expectancy=-3.0 avg_p_win_dir=0.1688 bias=0.1688
  walk-forward split @ 2026-10-11T03:05:11.271151+00:00
    H1 n=2 exp=-3.0 | H2 n=2 exp=-3.0
  VERDICT: UNDECIDABLE

### stand_by
  n_total=924 n_resolved=765 n_expired=0 expired_share=0.000
  WR=0.110 expectancy=-2.0130718954248366 avg_p_win_dir=0.10985803814713897 bias=5.4116578511523294e-05
  walk-forward split @ 2026-10-12T12:01:13.100522+00:00
    H1 n=447 exp=-1.883668903803132 | H2 n=318 exp=-2.1949685534591197
  VERDICT: INERT
