---
# What it is — in one sentence

`regime_edge/` (engine v1, seed 7) is an **offline proof engine** that assigns
numeric VALUES to regimes across a price time series — trend, chop, volatility
— and MEASURES whether those values predict trading edge (R per slot), with
walk-forward lockboxes, bootstrap CIs and significance tests. It is NOT the
daily edge computation that feeds the live gate; it is the audit that produced
17 weakness verdicts (11 PROVEN-FAIL, 6 MEASURED) on Kairos-as-is and justified
the 7 corrective fixes that shipped LIVE on 2026-10-20.

## Inputs (point-in-time, no lookahead)

- TDVA OHLCV 1h bars, cached as `data/regime_edge/{SYM}.csv` (14 ACTIVE symbols;
git-ignored; provenance in `data/regime_edge/SOURCE.json`, built by
`scripts/export_edge_lab_data.py`). Default run was extended 5->14 symbols in
commit `fd2cb64`; the 5-symbol run `20261020_055039` is the comparability
baseline (byte-identical first rows to the 14-symbol `20261021_035147`).
- Current per-symbol geometry parsed from `config/feature_flags.yaml` ->
  `dynamic_atr.by_symbol` (band centre = middle grid value; `EXPIRY_BARS=48`).
- The repo's own `ev_costs.cost_atr_by_class` table for per-trade cost,
  mapped per `barrier.SYMBOL_CLASS`.

## One run produces

`worklog/kairos/reports/regime_edge/<run_id>/` containing:

1. `edge_lab.json` — THE artifact (schema-asserted finite, <= 3 MB): meta,
   verdicts, hmm, regime_values, edge_surface, gate_backtest, calibration,
   barrier, tsfm_reframe, fixes.
2. `summary.md` — human-readable headline verdicts + numbers.
3. `records.csv` — per-slot audit trail (one row per book x slot).

## What it does, concretely

1. **Values** (`values.py`): trend = signed 24h efficiency ratio [-1,+1];
   chop = Choppiness/100 [0,1]; vol = rolling ATR-percentile [0,1]. A
tri-state hysteresis label (UP / DOWN / NEUTRAL) over er_signed
   (+/-0.10 deadband) supplies the NEUTRAL state the live 4-state HMM lacks
   (W-X1).
2. **HMM repro** (`hmm_ref.py`): faithful offline repro of the live dual-
   GaussianHMM stack (vol x trend, rank-label, 3-tick debouncer) — the
   head-to-head target for verdicts W-X2/W-X3/W-X5.
3. **Edge surface** (`edge.py`): decile buckets of 5 features (er_abs,
   chop, vol, hurst, adx) -> outcome rates + mean R, with bootstrap 95%
   CIs and Spearman+permutation tests. At the 14-symbol run: 2/70 significant
   (~3.5 expected by chance).
4. **Books** (`books.py`): the decisive backtest — kairos-as-is vs
   corrected geometry vs driftless null on the SAME slot grid (every 24
   bars from warmup 720), scored by DM/HAC/Holm.
5. **Barrier physics** (`barrier.py`): first-passage gambler's ruin
   (driftless + drift closed form) + sub-stepped MC, cross-checked vs
   the repo's own `barrier_null` within ~0.02. Solves a corrected geometry
   constrained under the driftless null (pi_SL<=0.62, pi_E<=0.25, RR in
   [1.5,4]) that is never an SL-farm by physics.

## What it does NOT do

- It is never imported by the live sweep. `edge_lab_run_mode` (daily job,
  18:30 UTC) runs it as a SEPARATE subprocess; its output feeds the
  Edge Lab dashboard and the 7 live fixes, never the per-signal qualification.
- It does not write `nt_regime_edge` and does not gate signals. The live
  gate (`sweep_orchestrator/regime_edge.py`) is a different mechanism reading
  a different table; see the sibling `regime-edge-gate` reference.
- It does not trade, forecast prices, or host a model server.

## Source anchors

- `regime_edge/__init__.py` — the module map + ENGINE/SEED constants.
- `docs/scopes/20261020_REGIME_EDGE_PROOF_ENGINE_AND_LIVE_FIXES.md` — the
  verdicts + the 7 fixes (the primary source of truth for this skill).
- `scripts/run_edge_lab.py` — the single CLI entry point.
