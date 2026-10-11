# Calibration + reachability reframe — fused-p_win decomposition + 3-outcome all-in scoring

**Date:** 2026-10-11
**Scope:** Replace the fused, resolved-only calibration with a decomposed `p_direction × p_reachability` model scored on the **all-in population (3 outcomes)**. Built as a pure module + E2E/replay; deployed as **measurement/shadow**, behavioral swap gated on validation.
**Status:** Module built, replay confirms the defect at book scale, E2E green. Shadow — the live enforcement swap is gated.
**Roadmap:** `calibration_reachability_reframe`.

---

## 1. The defect (confirmed live, not theoretical)

The platform prices every signal with ONE fused `p_win` (the regime+markov+timesfm blend) and scores it against the realized **resolved-only** win rate. Two independent failures, both now measured:

### 1a. Fusion — the bias can't be attributed

Era-clean resolved-only win rate vs the model's published prediction, all 14 symbols (`nt_calibration_records`, `entered_at ≥ 2026-10-02`):

| symbol | realized WR | published p_win | bias |
|---|---|---|---|
| AUDUSD | 5.1% | 0.678 | **+0.627** |
| USDCHF | 10.6% | 0.708 | **+0.602** |
| US500 | 5.0% | 0.650 | **+0.600** |
| GBPUSD | 7.5% | 0.661 | +0.586 |
| US30USD | 2.6% | 0.610 | +0.584 |
| XAUAUD | 9.4% | 0.646 | +0.552 |
| XAUEUR | 7.5% | 0.612 | +0.537 |
| XAUUSD | 13.8% | 0.671 | +0.533 |
| USDCAD | 11.2% | 0.569 | +0.457 |
| EURUSD | 7.4% | 0.506 | +0.432 |
| USDJPY | 7.9% | 0.472 | +0.393 |
| BTCUSD | 19.0% | 0.541 | +0.351 |
| XAGUSD | 20.1% | 0.455 | +0.254 |
| AAPL | 23.3% | 0.440 | +0.207 |

**No symbol is calibrated or underconfident.** The shrink-toward-0.5 penalty cannot fix this: moving AUDUSD 0.68→0.59 when the truth is 0.05 does nothing. The fused number hides *which* parent is broken.

### 1b. Wrong population — "calibrated but losing"

The all-in era-clean book (`nt_bracket_reachability`, 111 cells, `n≥20`):

| outcome | share |
|---|---|
| reach TP | **12.6%** |
| hit SL | **76.6%** |
| expired | **10.9%** |
| *resolved-only WR* (what old calibration scores) | *13.8%* |

Calibration **excludes expiry** ("a timeout is neither win nor loss"), so it scores a 13.8% resolved-only rate while the book that loses money is a 12.6/76.6/10.9 loss machine. A cell can read "calibrated" on its resolved subset while the all-in book bleeds. **That is the observed "large losses while a symbol claims calibrated."**

## 2. The reframe

```
p_win_fused  =  p_direction  ×  p_reachability   (currently mashed into one)
```

- **`p_direction`** — was the regime/trend/vol read honest, *given the bracket resolved at all*? = resolved TP share (`tp/(tp+sl)`).
- **`p_reachability`** — was the bracket winnable given geometry + expiry? = all-in TP share (`tp/n`).

**Verdict is scored on the all-in base rate** (`tp/n`), with expiry as a real third outcome, and `claimed_edge = published_p_win − no_skill_line` (the geometry's own coin-flip line, `SL/(TP+SL)`) so a claim is judged against what the bracket alone delivers.

## 3. Built — `regime_platform/sweep_orchestrator/calibration_reframe.py`

Pure, no I/O. `cell_calibration(tp, sl, expired, published_p_win, tp_distance, sl_distance, min_n)` → `CellCalibration`: all-in shares, resolved-only WR, the decomposition, `bias_all_in` vs `bias_resolved_only`, `no_skill_line`, `claimed_edge`, and a 3-way + THIN verdict. `reframe_reachability_row()` adjudicates a live `nt_bracket_reachability` row.

## 4. E2E + replay results

### Unit/E2E (`tests/test_calibration_reframe.py`, 10 passed, 1 live-skip)
All branches: overconfident (all-in harsher than resolved when expiry>0), underconfident, calibrated, THIN, negative claimed edge (claim below geometry line), positive claimed edge, fused-rebuild = product (behavior-preserving swap), zero-expiry degenerate case, row reconstruction preserves n.

### Live replay/backfill (111 era-clean cells, claim 0.55)

| finding | result |
|---|---|
| Verdict distribution | **109 OVERCONFIDENT, 1 CALIBRATED, 1 UNDERCONFIDENT** |
| Cells with expiry>0 where all-in bias ≥ resolved-only | **20/20 (100%)** |
| Mean bias all-in | **+0.424** |
| Mean bias resolved-only (old lens) | +0.412 |
| Mean gap (all-in harsher by) | +0.013 |
| Mean all-in book | TP 0.126 / SL 0.766 / EXP 0.109 |
| Mean resolved-only WR (old lens) | 0.138 |

**Every era-clean cell is overconfident at the published claim; the all-in lens is harsher on 100% of expiry-bearing cells.** Worst cells: several with 0% TP — 100% SL or 100% expired — still published at 0.55 (bias +0.55). These are the cells the shrink penalty "managed" while delivering nothing.

## 5. Deployed as: shadow, gated swap

Per the one-lever-at-a-time / evidence-first convention:

- **Now (measurement):** the reframe verdicts are available to stamp (`bias_all_in`, `p_direction`, `p_reachability`, `claimed_edge`) so the live-vs-all-in delta accumulates — the evidence for the swap.
- **Gated (behavioral, NOT yet live):** (a) `p_win → p_reachability` in EV/Kelly, (b) retire the shrink-toward-0.5 penalty, (c) score the EOD bias loop on the all-in population. Each is a separate flag, one cohort at a time, flipped only after the shadow data shows per-symbol `bias_all_in → 0` and expectancy improves, era-clean walk-forward. The fused-rebuild = product equivalence test guarantees the swap is math-preserving when it lands.

## 6. Files

- `regime_platform/sweep_orchestrator/calibration_reframe.py` — pure module (new).
- `tests/test_calibration_reframe.py` — 10 E2E + 1 live-replay (new).
- `worklog/20261011_CALIBRATION_REACHABILITY_REFRAME.md` — this.
- Roadmap `calibration_reachability_reframe` — build bar.

## 7. Next (gated)

1. Stamp the reframe verdict on `nt_signal_sim` (shadow) — all-in bias + decomposition per signal.
2. Pull the resolved-signal `bias_all_in` distribution → confirm it's the +0.42 the replay predicts, per symbol.
3. Flip `p_win → p_reachability` for one cohort (e.g. the 0%-TP cells first — they're provably worthless), validate bias→0 + expectancy, then widen.
4. Retire the shrink penalty once both parents are honest.
