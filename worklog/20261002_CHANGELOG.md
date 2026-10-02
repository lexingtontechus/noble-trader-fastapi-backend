# 2026-10-02 — Signal quality: measurement instruments + immediate sizing fixes

**Scope:** `worklog/20261002_SIGNAL_QUALITY_PLAN.md` §3.
**Type:** changelog for a session that built four measurement instruments, benchmarked
an external analysis, and shipped three code fixes.

---

## Summary

Four measurements established that the platform's published win probability cannot
rank opportunities, and that the outcome label it is measured against overstates wins
by up to 2.4×. Three fixes shipped; all are **backwards-compatible by default** (the
shipped config reproduces pre-fix behaviour exactly), so each is reversible by a single
flag or constant.

**Test suite:** 1683 passed, 21 skipped, 0 failed.

---

## 1. Code changes

### 1.1 `ev_scale_sizing_mode` — decouple size from `p_win` *(new flag, default `"on"`)*

| file | change |
|---|---|
| `regime_platform/core/feature_flags.py` | `_DEFAULTS["ev_scale_sizing_mode"] = "on"`; `get_ev_scale_sizing_mode()` |
| `config/feature_flags.yaml` | documented, quoted (`"on"`) |
| `regime_platform/sweep_orchestrator/orchestrator.py` | both `effective_kelly` sites honour the flag |

`effective_kelly = kelly_f × ev_scale` becomes `kelly_f × 1.0` when set to `"off"`.

**Why:** `ev_scale = min(1, ev_per_dollar / 0.50)` is a function of `p_win`, which
measures at **bracket AUC 0.4869** with **REL 0.0616 vs RES 0.0008** — below-chance
discrimination, overconfident. Multiplying position size by a quantity with negative
skill is worse than not using it.

**Not sizing-neutral.** Removing the `p_markov` source alone moved mean `ev_scale`
**0.5576 → 0.8403** and took **23.05%** of signals from unsized (`ev_scale = 0`) to
sized. This flag removes that lever in the other direction.

**`ev_scale` is still computed and stamped** either way — it remains a published
diagnostic. Only its multiplication into size is gated.

**Fail-safe:** anything that is not an explicit `"off"` resolves to `"on"`, so a typo
cannot change sizing. YAML booleans are mapped explicitly: an unquoted `off` parses as
boolean `False` whose `str()` is `"false"` — which would otherwise resolve to `"on"`,
the opposite of intent.

### 1.2 Break-even-aware probability floor *(audit E7)*

| file | change |
|---|---|
| `regime_platform/core/position_sizer.py` | `SizingConfig.breakeven_prob_floor` (default `True`), `breakeven_margin = 0.02`; `DynamicMasanielloSizer.effective_prob_floor()`; `_check_gates` uses it |

The flat `min_prob = 0.50` floor is payoff-blind. At RR 2:1 the break-even probability
is `1/(1+2) = 0.333`, so the floor rejects the entire 0.333–0.50 band — exactly where
positive-expectancy high-RR setups live (audit **E7**). The floor is now

```
max(min_prob, 1/(1+RR) + margin)
```

so a high-RR trade may pass at a lower probability (correct economics) while the flat
floor remains a floor *under* it. The `max` guarantees it is **never more permissive**
than the payoff-blind rule.

**Backwards-compatible at the traded geometry.** At the platform's live RR band
(1.67–2.0) the payoff-aware floor sits *under* 0.50, so the flat floor still binds and
decisions are unchanged. The fix only opens trades where RR is high enough to justify
them. Set `breakeven_prob_floor=False` to restore the legacy flat floor exactly.

Gate messages now name the binding floor (`[flat min_prob]` vs
`[break-even floor (RR 8.00)]`) so a rejection is not misattributed.

### 1.3 `vol_factor` / `drawdown_factor` — the `p_win`-free risk backbone *(Phase 2)*

| file | change |
|---|---|
| `regime_platform/core/position_sizer.py` | `vol_factor()`, `drawdown_factor()` extracted as standalone functions |

Masaniello's `V_i = clip(ATR_base/ATR_cur, 0.5, 1.5)` and
`DD_i = clip(1 − dd/max_dd, 0.25, 1.0)` are **`p_win`-free, already correct, and
currently unused in the sweep path**. Extracted so Phase 2 can adopt them while the
probability question is open. Both return neutral (`1.0`) on degenerate input rather
than silently resizing. A test asserts neither signature ever gains a probability
parameter.

---

## 2. Measurement instruments (no live behaviour)

| module | question |
|---|---|
| `calibration_decomp.py` | Murphy decomposition + BSS + tie-corrected AUC + reliability |
| `cell_conditioning.py` | does conditioning the reachability cell beat the cell alone? (walk-forward, no leakage) |
| `reachability_decay.py` | is the cell's failure non-stationarity, and does recency fix it? |
| `common_factor.py` | is the cell rate cross-sectional or market-wide? (variance decomposition) |

All pure (no I/O, no clock), all unit-tested, driven by `scripts/kairos_*.py` CLIs.

---

## 3. Findings

| # | finding | number |
|---|---|---|
| 1 | `p_win` is a directional score, not a P(win) | bracket AUC **0.4869** |
| 2 | `p_win` is overconfident with near-zero resolution | `REL` 0.0616 vs `RES` 0.0008 |
| 3 | the reachability cell has signal that does not transfer | in-sample 0.7256 → out-of-fold 0.4994 |
| 4 | the bucket space is degenerate | **71.2%** of rows in one bucket (`4+`) |
| 5 | no trailing window recovers transfer (replicated) | best 0.5093; 14d flipped best→worst on re-run |
| 6 | the cell is cross-sectional but unstable | cell 43% / time 10% / **interaction 47%** |
| 7 | **the outcome label is optimistic** | survivable base rate **0.1186** vs 0.2805; **57.7%** of "wins" never survived the stop |
| 8 | the `p_win` floor discards good setups | `min_prob = 0.50` (E7) |

**Finding 7 is the most consequential and was the last found.** The settlement label
comes from a close-walk; re-labelling against intrabar MAE collapses the win rate by
more than half. *Caveat:* settlement is close-based and the MAE column is intrabar, so
57.7% is an **upper bound** — pinning it needs an intrabar-consistent resolver (audit
R1). The direction is not in doubt.

**Finding 2 + 7 interact:** the ghost edge is a **level** distortion, not a ranking one.
AUC moves 0.4705 → 0.4746 while `REL` more than doubles. Fixing the label restores no
ranking power and makes overconfidence worse.

---

## 4. Corrections to prior claims

- **`p_win_dir` is `p_win_raw`.** `_p_win_dir = p_win_raw` (`orchestrator.py:4816`) —
  identical on 18,064/18,064 rows. The capture stamps two column names for one
  quantity; `p_win_dir` should be dropped or redefined.
- **An earlier "anti-predictive blend" verdict was a reference-line artefact.** A
  constant forecast has `REL = 0` and `RES = 0`, so its Brier *is* the uncertainty term.
  Losing to it does not distinguish "no resolution for this event" from "calibrated for
  a different event". The Murphy decomposition separates them.
- **An earlier "common-factor" explanation was wrong.** Time explains only 10.3% of
  cell-rate variance, not a majority. Recorded and rejected.

---

## 5. What is NOT done

- **Phase 1 (label integrity)** — requires an intrabar-consistent resolver (audit R1).
  Gated: everything else is measured against this label.
- **Phase 3 (calibrate the probability)** — bounded by **40 days of history**
  (2026-08-23 → 2026-10-02, verified; the system did not exist earlier, so backfill
  cannot extend it).
- **Phase 4 (continuous forecast)** — recorded as fallback only.
- **`M_i` batch urgency** — needs an explicit batch definition; the sweep emits
  continuously with no batch boundary. Real design work, not a flag flip.

---

## 6. Verification

```
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
# 1683 passed, 21 skipped, 0 failed
```

New tests: `tests/test_signal_quality_fixes.py` (24), `tests/test_calibration_decomp.py`
(31), `tests/test_cell_conditioning.py` (38), `tests/test_reachability_decay.py` (24),
`tests/test_common_factor.py` (25).

**Three bugs were found in the analysis code by its own tests** and fixed:
variance shares summing to 1.198 on an unbalanced matrix; `residual_scores` collapsing
to the raw cell mean (making the comparison vacuous); and the in-sample ceiling computed
over a different population than the pooled out-of-fold number.

---

## 7. Related

- `worklog/20261002_SIGNAL_QUALITY_PLAN.md` — the plan (phases, bars, end state)
- `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` — objective + metric
- `docs/scopes/20261002_EDGE_CALIBRATION_BENCHMARK.md` — the external analysis, benchmarked
- `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` — E7, R1, C2/R6, G2
