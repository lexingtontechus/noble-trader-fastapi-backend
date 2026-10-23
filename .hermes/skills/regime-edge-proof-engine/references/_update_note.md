---
# Update note — 14-symbol ACTIVE set (commit fd2cb64)

> Source: commit message + diff of `fd2cb64` ("feat(regime-edge): full ACTIVE symbol set 14/14 + resumable checkpoints + data-driven summary").

## What changed

- Default run extended 5 -> 14 symbols per the Supabase symbol table (2026-10-21).
  `DEFAULT_SYMBOLS = list(rbar.ACTIVE_SYMBOLS)` (14 symbols). The 5-symbol run
  `20261020_055039` is the comparability baseline; `20261021_035147` has
  byte-identical first rows.
- New cache builder: `scripts/export_edge_lab_data.py` constructs the 9 added
  `data/regime_edge/{SYM}.csv` files (yfinance 1h x 730d; XAUEUR/XAUAUD built
  as GC=F/EURUSD=X and GC=F/AUDUSD=X on inner-joined timestamps).
- Resumable checkpoints: `run_edge_lab.py --checkpoint-dir <dir> [--recompute]`;
  per-symbol pickle checkpoints; pure+seeded => byte-identical resumes. The
  ~30 min 14-symbol run survives host kills.
- Data-driven report: `report.py` verdict text + summary read `len(S)`
  (W-X2 prints "n of N" not "on all 5"; Holm family = 2*n_symbols).

## 14-symbol baseline findings (20261021_035147, seeded 7)

| metric | 5-symbol | 14-symbol |
|---|---|---|
| symbols | 5 | 14 |
| starved (zero kairos trades) | XAGUSD, USDCHF | 8/14 (incl. both indices) |
| HMM direction skill | -0.0444 (5/5 neg) | -0.0425 (14/14 neg; US500 -0.1019) |
| Spearman sig / expected | 2/25 (~1.3) | 2/70 (~3.5) |
| pooled kairos-vs-corrected DM p | 0.0163 (favors kairos) | 0.0101 (drift inheritance) |
| corrected-vs-null DM p | 0.43 | 0.62 (n.s.) |
| worst SL-farm | XAUUSD pi_SL 0.83 | US30USD pi_SL 0.724, EV_null -0.462 |
| US500 pi_E under null | 0.70 | 0.70 (in-symbol) |

Verdicts (11 PROVEN-FAIL / 6 MEASURED) and the 7 live fixes are UNCHANGED —
the data-driven report just quotes the 14-symbol numbers. Honest read holds:
thin signal, negative HMM skill, wins are coherence + physics floor.
