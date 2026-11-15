"""regime_edge_crt — the CRT (Candle Range Theory) v5 CONT research twin.

Offline research package for the CRT auction-reversal CONT leg, ported from
``CRT_v5_CONT_MT4.zip`` (``CRT_EV_Research_Export.mq4`` v1.10 +
``crt_ev_optimizer.py`` + ``baseline_check.py`` + ``README_v5_CONT.md``).

Zero imports from ``regime_platform``: this twin is importable standalone,
exactly like ``regime_edge`` / ``vol_backtest``. The live engine (the
renko-style ``crt/`` package + a ``CRTOrchestrator`` job family — NOT a
``regime_platform_crt`` fork) is the CRT-1 phase; see
worklog/20261115_CRT_V5_CONT_INTEGRATION_ASSESSMENT.md §4/§6 for the naming
verdict and the phased rollout.

Pipeline (README_v5_CONT.md parity):

    MT4 CRT_EV_Research_Export.mq4  ->  data/crt/raw/CRT_CONT_RESEARCH_<SYM>.csv
      -> baseline (v3 filters, no fitting, cost burden)
      -> walk-forward (max-LCB selection, decay replay, enable thresholds)
      -> config/crt_cont.json (the CRT-1 engine artifact)
      -> reports under worklog/kairos/reports/regime_edge_crt/<runid>/
      -> signals to the Discord #crt channel (fail-open)

Modules
-------
config          self-contained yaml config (config/crt/crt_lab.yaml + crt_discord.yaml)
data            compact-path CSV ingest + #meta + sha256 input manifest
decay           the CUSUM edge-decay monitor (EA rules verbatim)
optimizer       walk-forward search + decay replay + deploy mint (ported)
baseline        the no-fitting v3 baseline check (ported)
report          crt_lab.json artifact assembly + summary.md
discord_notify  the #crt webhook signal sender (fail-open, bounded, masked)
selftest        deterministic synthetic compact-path corpus (seed 7)
"""
__version__ = "1.0"
ENGINE = "regime_edge_crt v1"
SOURCE_ZIP = "CRT_v5_CONT_MT4.zip"
SEED = 7
