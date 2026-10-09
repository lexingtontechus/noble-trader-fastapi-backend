# p_win bracket coherence — reachability era-filter (the real bracket-aware p_win fix)

**Date:** 2026-10-09
**Scope:** Close the coherence gap where bracket-aware `p_win` reads an era-contaminated source. The bracket-aware computation already existed (`p_win_bracket.py`); it was reading `nt_bracket_reachability`, which — unlike the bias table — had **no cutover filter**.
**Status:** Built + deployed. Reachability recompute now era-clean; `p_win_bracket` therefore reads clean data. Ships behind the existing `p_win_bracket_mode` (shadow) — no live gate change.

---

## 1. The finding: bracket-aware p_win already existed, but read dirty data

The goal is orchestrator coherence, and the honest first step was checking what already exists rather than rebuilding. It turns out **bracket-aware p_win is already built** — `regime_platform/sweep_orchestrator/p_win_bracket.py`:

```
p_resolve     = P(resolved | symbol, vol_tier, distance/ATR bucket)
p_win_resolve = Beta(α,β) shrunk P(tp_hit | resolved)     # α=β=2
p_win_bracket = p_resolve * p_win_resolve
```

with `apply_bracket_to_ev()` to substitute it into the EV engine, gated by `p_win_bracket_mode`. So "bring forward bracket-aware p_win" is **not** a build — it's a coherence fix. The computation was reading `nt_bracket_reachability`, and **that table had the exact contamination the bias table had before this session's fix**: `_fetch_resolved_rows` filtered `outcome NOT NULL AND contaminated=false` with **no `resolved_at >= cutover` bound**, so it spanned the pre-2026-10-02 close-walk era (84k of 92k rows).

### The magnitude (measured live, 2026-10-09)

`nt_bracket_reachability` as `p_win_bracket` reads it (pooled, signal=all):

| bucket | total_n | pct_hit_tp | pct_hit_sl | → p_win_resolve ≈ tp/(tp+sl) |
|---|---|---|---|---|
| 0.5-1 | 2192 | 0.364 | 0.636 | **0.364** |
| 1-2 | 5644 | 0.271 | 0.729 | **0.271** |
| 2-4 | 3660 | 0.311 | 0.689 | 0.311 |
| 4+ | 48826 | 0.112 | 0.329 | 0.254 (56% expire) |

But the **clean post-cutover WR** (from `nt_signal_sim`, `resolved_at >= 2026-10-02`):

| bucket | n | WR |
|---|---|---|
| 1-2 | 410 | **0.056** |
| 2-4 | 308 | 0.136 |
| 4+ | 278 | **0.050** |

**The tight buckets read p_win_resolve ≈ 0.27 when the true intrabar-era win rate is 0.056 — a ~5× overstatement feeding the EV engine.** That is the coherence defect: the bias table was era-filtered (`b5a6cdd`), but reachability — and therefore the bracket probability the EV engine can consume — was not. The two halves of the calibration story disagreed by 5×.

---

## 2. The fix (same pattern, now applied to reachability)

`bracket_reachability._fetch_resolved_rows` now reads the **same** `nt_calibration_cutover` marker the bias recompute and `v_calibration_records_current` use, and adds `resolved_at >= cutover_at` to the fetch. Missing marker → fail-open (no filter, prior behaviour).

```python
cutover_at = await _fetch_cutover_at()
if cutover_at:
    params["resolved_at"] = f"gte.{cutover_at}"
```

`_fetch_cutover_at()` is kept **local** to `bracket_reachability.py` (not imported from `calibration_bias_bucket`) to avoid a circular import — both read the same table, and *that* is the coherence point: one marker, one era definition, three consumers (bias table, reachability, live gate) that now agree by construction.

**No delete, no archive.** Same reasoning as the bias fix: the 84k pre-cutover rows stay for the heavy-sweep 90-day backtest window; only the *calibration/reachability recompute* is era-bounded.

---

## 3. What this does and does NOT change

- **Does:** reachability's `pct_hit_tp/sl/expired` (and thus `p_win_resolve`, `p_win_bracket`, and `apply_bracket_to_ev`) now reflect the intrabar era. Next weekly rebuild (Sun) recomputes clean; until then the live table is still the last rebuild's (01:18 UTC, pre-fix) values — the *code* is clean, the *cached table* refreshes on schedule.
- **Does not flip any live gate.** `p_win_bracket_mode` stays `shadow`. The coherence fix makes the *shadow* computation honest; promoting it to `live` remains a separate, later decision (it will suppress tight brackets, reducing signal volume — the intended, correct outcome, but a deliberate step).
- **Does not** touch the p90-expiry lever (parked, `20261009_P90_HOURS_LIVE_MODE.md` §10) or settlement basis (already unified).

---

## 4. Verification

- `tests/test_bracket_reachability.py` (+2): `_fetch_resolved_rows` forwards the cutover marker as `gte.<Z>`; missing marker fails open.
- `tests/test_calibration_bias_bucket.py` (7): unchanged, still green — same pattern, parallel test.
- `tests/test_p_win_bracket.py`: green (pure math, unaffected by the I/O filter).
- Full suite: **1898 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs` — unrelated).

---

## 5. Files changed

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/bracket_reachability.py` | `_fetch_cutover_at()` + era filter in `_fetch_resolved_rows` |
| `tests/test_bracket_reachability.py` | era-filter forwarding + fail-open tests |
| `worklog/20261009_P_WIN_BRACKET_COHERENCE.md` | this |

---

## 6. Honest coherence status after this

| Component | Era-clean? |
|---|---|
| Live calibration gate (`fetch_calibration_bias` → `v_calibration_records_current`) | ✅ was already |
| Bias-by-bucket table (`b5a6cdd`) | ✅ |
| **Reachability → `p_win_bracket`** (this) | ✅ now |
| Resolver / settlement basis | ✅ (unified, prior) |
| Geometry chooser (dynamic ATR) | ❌ still returns `None` for all symbols (separate issue) |
| p90 expiry lever | parked (inert, null result) |

The calibration/reachability/p_win triangle now agrees on one era and one basis. Remaining incoherence is in the **geometry chooser** (picks nothing) — a different problem, not a measurement problem.
