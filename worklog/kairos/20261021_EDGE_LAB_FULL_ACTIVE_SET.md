# Edge lab — full ACTIVE symbol set (14/14), the 5-symbol test baseline extended

> **Date:** 2026-10-21 · **Scope:** `scripts/run_edge_lab.py`, `regime_edge/barrier.py`,
> `regime_edge/report.py`, `scripts/export_edge_lab_data.py` (new), run artifacts
> `worklog/kairos/reports/regime_edge/20261021_035147/`
> **Baseline:** run `20261020_055039` (5 symbols) — the first 5 numbers are byte-identical
> (determinism witness: same seed 7, same data files, same engine).

---

## 1. What was asked and what shipped

The operator asked for two things:

1. **How does the allocation work for the nominal settlement calcs used by the scorecard?**
   — answered in §5 below (file-traced, migrations 138/148/149 + `settlement_contract.py`
   + `eod_resolver.compute_pnl`); the edge lab itself settles in R units (SL-distance
   units), deliberately NOT in the scorecard's nominal 1-unit dollars — see §6.
2. **Extend the edge lab to the full ACTIVE symbol list** (5 were used to test) —
   shipped: `DEFAULT_SYMBOLS` is now the 14-symbol ACTIVE set from the Supabase symbol
   table (2026-10-21 fetch); run `20261021_035147` executed end-to-end on all 14.

```
BTCUSD XAUUSD XAGUSD XAUEUR XAUAUD USDJPY EURUSD GBPUSD USDCAD USDCHF AUDUSD AAPL US500 US30USD
```

## 2. What changed in code (additive; the 5-symbol behavior is unchanged)

| File | Change |
|---|---|
| `regime_edge/barrier.py` | `SYMBOL_CLASS` + `COST_BY_CLASS` extended with the repo's OWN live cost table (`config/feature_flags.yaml` `ev_costs.cost_atr_by_class`): equity 0.06, index 0.05 added to forex 0.04 / metals 0.06 / crypto 0.10. `ACTIVE_SYMBOLS` exported. US500/US30USD priced as INDEX (their economic class) — the live registry carries `asset_class=stocks`, a 0.01-ATR/trade overcharge recorded in the Part-C review. |
| `scripts/run_edge_lab.py` | Default symbols 5 → 14; `--checkpoint-dir` / `--recompute`: per-symbol pickled checkpoints make the ~30-min 14-symbol run resumable (per-symbol compute is pure + seeded, so a resumed run is byte-identical); data-basis + cost-source notes added to `meta.approximation_notes`. |
| `regime_edge/report.py` | The 5-symbol run's hardcoded narrative numbers ("2 of 25 tests", "the 10 per-symbol tests", pooled p, starvation list, "worst violator", AUC ranges, take-rate) are now DATA-DRIVEN — a 14-symbol summary can no longer quote 5-symbol numbers. |
| `scripts/export_edge_lab_data.py` (new) | yfinance 1h×730d export for the 9 added symbols; XAUEUR/XAUAUD CONSTRUCTED as `GC=F / EURUSD=X` and `GC=F / AUDUSD=X` on inner-joined hourly timestamps (same metal basis as the XAUUSD proxy → isolates exactly the quote-currency difference; wick extremes carry the quote-bar-close approximation, ≤ the quote bar's own range ~0.05%). |

Data basis (git-ignored `data/regime_edge/`, same convention as the baseline): direct
tickers EURUSD=X, GBPUSD=X, USDCAD=X, AUDUSD=X, AAPL, ES=F (US500), YM=F (US30USD);
ratio-constructed XAUEUR/XAUAUD. AAPL trades ~6.5h/day → 5,073 bars over a longer
calendar window (2023-11-21 →) vs ~13.6–17.5k bars for the 24h symbols — the engine
treats each file as a plain bar sequence (repo regime-stack convention).

## 3. Run 20261021_035147 — headline deltas vs the 5-symbol baseline

The first five rows of every table reproduce the baseline exactly (BTCUSD kairos
139/+0.3734, XAUUSD 103/+0.3673, XAGUSD 0/−0.3250, USDJPY 220/+0.1313, USDCHF
0/+0.0826-corrected; barrier crosscheck Δ ≤ 0.0034). What the 9 added symbols add:

1. **EV-gate starvation is the DOMINANT live behavior, not an edge case: 8 of 14
   symbols take ZERO kairos trades over the window** (XAGUSD, XAUEUR, XAUAUD, USDCAD,
   USDCHF, AUDUSD, US500, US30USD). The live book, as gated today, can only ever
   trade BTCUSD, XAUUSD, USDJPY, EURUSD, GBPUSD, AAPL. Both index symbols starve —
   W-C1/W-G3's predicted US500 π_E 0.70 expiry-farm measured in-symbol (current band
   13.23/8.09/48: π_SL 0.241 but EV_null −0.158, and the EV gate never opens).
2. **HMM direction skill is negative on 14 of 14** (mean −0.0425; worst US500
   −0.1019 — the repo's own live US500 AUC 0.444 now has the engine-side twin;
   best EURUSD −0.0062, still not better than majority).
3. **US30USD's current band is the worst SL-farm in the fleet**: 12.80/2.41/48 →
   π_SL 0.724, EV_null **−0.462** per trade under the driftless null (RR 5.3 at the
   widest ATR distance with a tight SL). W-G4's worst-violator is now US500 (π_E
   0.70 > 0.25), with US30USD the worst EV bleed.
4. **Pooled DM kairos-vs-corrected p=0.0101 (mld −0.0171) still favors kairos in
   this drift window** — unchanged interpretation: trend-following-risk inheritance
   (RR 1.5–5.3 payoff on realized drift with 14/14 negative direction skill), not
   HMM direction edge. Corrected-vs-null remains not significant (p=0.6184).
   EURUSD is the one new near-significant per-symbol cell (kc p_raw 0.0021 → Holm
   0.0588 across the 28-test family): kairos 113 trades +0.3838 vs corrected
   −0.0824 — same drift-window story on the most-traded FX pair.
5. **Edge predictability gets THINNER at 14 symbols: 2 of 70 feature×symbol tests
   at p<0.05 (~3.5 expected by chance)** — XAUUSD-ADX (ρ −0.1165, p 0.006) remains
   the only strong cell; the honest verdict "weakly, not decisively" is unchanged
   and now better powered.
6. Calibration: regime-conditional lockbox mean AUC 0.525 (base 0.46–0.48 band);
   US500's base AUC collapses to 0.2452 (its HMM-direction trade population is
   nearly constant-outcome — a starvation artifact worth remembering when reading
   the repo's live US500 calibration rows).

## 4. Honest boundaries of this run

- Proxy data (yfinance 1h×730d, ES=F/YM=F futures for the index CFDs, GC=F÷FX
  constructed ratios for XAUEUR/XAUAUD) — NOT broker-quality exports; the operator's
  TDVA CSVs remain the upgrade path (same `data/regime_edge/` schema).
- The AAPL window is longer (2023-11-21 →) because yfinance serves more hourly
  history for equities; period_start in `meta` reflects it. Engine treats each
  symbol as its own bar sequence (repo convention), so no cross-symbol alignment
  is assumed.
- 19/19 tests pass at HEAD of this change; no engine numerics were touched —
  only symbol tables, defaults, notes, and narrative text (data-driven now).
- The 5-symbol baseline artifacts (20261020_053412 determinism witness,
  20261020_055039) are retained untouched.

## 5. The scorecard's nominal-settlement allocation (file-traced answer)

Chain: `eod_resolver._resolve_one` → `compute_pnl` → `settlement_contract.compute_bricks_pnl`
→ `nt_signal_sim.pnl_bricks/pnl_dollars` → views `v_symbol_signal_scorecard{,_7d,_monthly}`
(migrations 138/146/148/149).

- **Per-trade settlement (the "nominal calc")**: `tp_hit → pnl_bricks = +|tp_bricks|`;
  `sl_hit → pnl_bricks = −|sl_bricks|`; `expired → signed MTM (exit − entry)/brick_size`
  (direction-signed). `pnl_dollars = pnl_bricks × brick_size`, 6dp, **no lot/contract/
  position-size term anywhere** — it is the P/L of holding a notional **1 unit of the
  underlying** (1 share / 1 unit of quote currency / 1 index point). Same-bar TP+SL →
  `settlement_same_bar_policy` (legacy live `tp_first`, backtest OCO `sl_first`);
  costs exist only in the contract's separate `pnl_dollars_net` (bps of entry) and are
  NOT charged in the column the scorecard reads.
- **Allocation into the scorecard**: population = `qualified` signals; win/loss/PnL
  allocated ONLY over `tp_hit + sl_hit` (expired deliberately excluded — a timeout is
  neither win nor loss, permanent until the expiry-rate alert); windows = all-time /
  `ts ≥ now()−7d` / `date_trunc('month', ts)`.
  `pnl_notional_1unit = Σ pnl_dollars` (within-symbol comparable ONLY);
  `pnl_001_lot = Σ (pnl_dollars × multiplier)` **per row before aggregation** (avoids
  compounding the rounded aggregate) with multipliers from `nt_symbol_lot_multiplier`
  (migration 148): FX 1,000 (0.01 lot = 1,000 base units), gold 1 (0.01 lot = 1 oz),
  silver 50, indices 0.01 ($0.01/point), AAPL 1, BTC 0.01 (0.01 BTC) — a real table,
  `LEFT JOIN` so an unknown symbol yields NULL rather than a silent fallback.
  `precision_gap = avg(p_win) − realized win rate` over the same tp/sl population.
- The edge lab's books settle in **R units** (SL-distance multiples) — a THIRD
  convention, chosen because it is scale-free across symbols and matches the
  barrier physics EV algebra; the live scorecard's 1-unit dollars and the repo's
  other two dollar conventions (renko risk_manager equity-Kelly, paper_book
  $1000-base Kelly) are deliberately not mixed into it.

## 6. Operator quickstart (unchanged shape)

```
python3 scripts/export_edge_lab_data.py          # refresh data/regime_edge/*.csv
python3 scripts/run_edge_lab.py                  # 14 symbols, ~30 min
python3 scripts/run_edge_lab.py --checkpoint-dir /tmp/ckpt   # resumable
```

Decision gates this run feeds: the S1–S6 sequencing in
`20261021_REOPT_SETTLEMENT_DEADLINE_COHERENCE_AND_SPECIALIST_BOOKS.md` Part B —
US30USD (EV_null −0.462) and US500 (π_E 0.70 + starvation) now carry engine-side
numbers for the deadline-coherence argument; the 8-symbol starvation list is the
W-C1 evidence for the p_win/EV gate rework already scoped.
