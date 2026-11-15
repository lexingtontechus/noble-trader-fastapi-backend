"""crt/ — the CRT v5 CONT live engine (CRT-1, 2026-11-15).

Renko-style top-level engine package (the Task 67 assessment §4 verdict:
deliberately NOT a ``regime_platform_crt`` fork — repo convention for
independent engines is a short top-level package). Ports the MQL4 EA
``CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4`` (v5.00, 3,217 LOC) engine half —
setup detection, option gates, CUSUM decay, rolling-EV gate, frozen-at-C2-close
decisions, paper fills — onto Kairos candle/scheduler/paper-book seams. The
research half already landed as CRT-0 (``regime_edge_crt/``, commit b49e2bc).

Layout (assessment §4, with decay.py living once in the research twin):

    sessions.py        ET session windows (NY 09:30-12:00 / Asia 20:00-22:00)
    candles.py         closed-bar extraction + M30 = 2x15m aggregation (TDVA)
    atr.py             Wilder ATR, MT4 iATR seeding (SMA seed)
    config_loader.py   config/crt_cont.json artifact -> OptionCfg rows
    setup.py           EvaluateArmedAt port: C1/C2/C3 detection + features
    labeler.py         LabelCustomR port: triple-barrier R labeling
    gates.py           SelectTargetOpt port: enabled->filters->decay->rolling
    monitors.py        ComputeMonitors/ComputeRolling ports (decay replay)
    paper.py           nt_paper_positions writer/resolver (source='crt_cont')
    engine.py          SymbolTFEngine: per-closed-bar loop (one MT4 chart)
    jobs.py            CRTOrchestrator job family (scheduler registration)
    router.py          APIRouter(prefix="/crt") — renko-seam mount in main.py

Contracts (the 5-clause discipline, CRT-1 scope):

- SHIPS DARK: ``crt_mode: off`` (config/feature_flags.yaml) = zero work, zero
  fetches, zero writes — the platform is byte-identical without the flag.
- PAPER-ONLY: fills land on ``nt_paper_positions(source='crt_cont')`` via this
  package's own thin writer; no ``nt_signal_sim`` writes, no resolver edits,
  no claims writes (CRT-2), MT5 untouched.
- The decay rules import from ``regime_edge_crt.decay`` — one source of truth
  for the CUSUM state machine, never a second port.
- Every number preserved from the EA (fidelity checklist Task 67 §7): gates,
  sizing ladder, caps, thresholds verbatim; the fidelity manifest lives in
  worklog/20261115_KAIROS_CRT1_BUILD.md §2.
"""

ENGINE = "crt v1 (CRT-1)"
SOURCE_EA = "CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4 (v5.00, CRT_v5_CONT_MT4.zip)"
PAPER_SOURCE = "crt_cont"
