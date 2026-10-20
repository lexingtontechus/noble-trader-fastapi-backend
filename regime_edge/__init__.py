"""regime_edge — point-in-time regime VALUE assignment + conditional-edge proof engine.

The user's core idea, made measurable: assign numeric VALUES to regimes across
the time series (trend / chop / volatility), then measure whether those values
PREDICT THE EDGE — walk-forward, with confidence intervals and significance
tests — instead of shadow-mode guessing.

Modules
-------
features   causal (point-in-time) regime features from OHLCV bars
values     the {trend, chop, vol} value assignment + tri-state hysteresis label
hmm_ref    faithful offline reproduction of the live HMM direction stack
brackets   bracket outcome resolution (repo settlement basis: intrabar, SL first)
barrier    first-passage physics (gambler's ruin + drift + expiry MC) + geometry
edge       conditional-edge model: edge surface, Spearman tests, calibration
books      the decisive gate backtest (kairos-as-is vs corrected vs null)
tsfm       machine-parsed reframe of the repo's own vol_backtest artifacts
report     edge_lab.json assembly + summary.md
"""
__version__ = "1.0"
ENGINE = "regime_edge v1"
SEED = 7
