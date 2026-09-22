# R2 Build — p_win_bracket into EV / gates / Kelly

> **Date:** 2026-09-22 · **Build Code** authorized  
> **Implements:** R2 of `worklog/20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md`  
> **Status:** Code complete + unit tests green · **Modes still `shadow`** — flip assessment below

---

## What shipped (uncommitted)

| File | Change |
|---|---|
| `config/feature_flags.yaml` | + `p_win_in_ev_mode: shadow` (off \| shadow \| live) |
| `regime_platform/core/feature_flags.py` | + `get_p_win_in_ev_mode()` |
| `regime_platform/services/statistics/ev_engine.py` | `recompute_ev_fields(..., p_expire=)` — 3-outcome EV `p_win*profit − (1−p_win−p_expire)*stake`; returns `loss_prob` |
| `regime_platform/sweep_orchestrator/p_win_bracket.py` | + `expire_prob()`, `apply_bracket_to_ev()` (mutates EVFullResult; fail-open when unscored) |
| `regime_platform/sweep_orchestrator/orchestrator.py` | **R2c/R2e wiring** — see below |
| `tests/test_p_win_bracket.py` | + `expire_prob`, `apply_bracket_to_ev` tests; fixed broken TestGate merge; + `TestGateThresholds` |
| `tests/test_ev_engine_recompute.py` | + 3-outcome EV tests (`p_expire`) |

### Orchestrator flow (after this change)

1. **Before `compute_ev`** — lookup `p_win_bracket` / `p_resolve` / `p_win_resolve` (moved from sim-capture time).
2. **After `p_win_raw` capture + calibration enforce** — if `p_win_in_ev_mode == live` and bracket scored: `apply_bracket_to_ev()` → EV/Kelly use `p_win_bracket` (expire not charged as loss). `p_win_ok` gate keeps scoring **`p_win_raw`** (directional) — different scale from `min_p_win_bracket`.
3. **Shadow** (`p_win_in_ev_mode == shadow`) — log would-be bracket EV; live EV unchanged.
4. **Sim capture** — reuses cached bracket values (no second lookup).

---

## Verification

| Check | Result |
|---|---|
| Syntax (`orchestrator.py`, `test_p_win_bracket.py`) | ok |
| `pytest tests/test_p_win_bracket.py tests/test_ev_engine_recompute.py` | **23 passed** |
| `scripts/replay_p_win_bracket_gate.py` (fixture) | see table below |

### Fixture replay (illustrative only)

| Cohort | n | win%res | exp% | expectancy_r | avg_p_dir |
|---|---:|---:|---:|---:|---:|
| baseline (p_dir only) | 80 | 70.0 | 50.0 | **+1.300** | 0.650 |
| bracket gate (p_dir + p_br) | 40 | 75.0 | **20.0** | **+1.250** | 0.600 |

Pass bar (script): expired% ↓ **and** expectancy_r ↑ **and** n not collapsed.

| Criterion | Fixture | Verdict |
|---|---|---|
| expired% ↓ | 50 → 20 | **PASS** |
| expectancy_r ↑ | +1.300 → +1.250 | **FAIL (slightly down)** |
| n not ~0 | 80 → 40 | PASS (halved) |

Script itself states: *“Fixture is illustrative — attach live table before flip.”*

---

## Flip assessment (R2c `p_win_in_ev_mode` / R2d `p_win_bracket_mode`)

### Recommendation: **do not flip either mode to `live` yet**

**Why not `p_win_bracket_mode: live` (R2d gate):**
- Fixture expectancy_r **worsened** slightly under the bracket filter. That is the wrong direction for a go-live gate.
- Live `--live` replay against `nt_signal_sim` has **not** been run (needs Studio/Supabase with migration 160 + current rows).
- 20260916 scope explicitly required a named replay table before live — fixture is not that table.

**Why not `p_win_in_ev_mode: live` (R2c EV/Kelly):**
- Same missing live evidence: EV substitution changes every published `p_win` / Kelly. A slightly worse fixture expectancy means we cannot yet claim C1/C2 improvement.
- `p_win_bracket` coverage on recent rows is still thin (bootstrap `n_stored` 09-16; many buckets `legacy_stored` / unscored → fail-open to directional EV). Live flip would be partially inert or noisy.

**What IS safe now:**
- Keep `p_win_bracket_mode: shadow` and `p_win_in_ev_mode: shadow`.
- Shadow path already logs would-reject + would-be bracket EV — collect ≥2 weeks of clean logs (R2b).
- After migration 160 is applied and Studio runs this code: run `scripts/replay_p_win_bracket_gate.py --live` and attach the table to a follow-up worklog.

### Flip criteria (when evidence exists)

Flip **`p_win_in_ev_mode: live`** first (EV/Kelly), **then** `p_win_bracket_mode: live` (gate) only if:

1. Live replay 28–70d: expired% ↓ **and** expectancy_r ↑ or flat **and** |bias_raw| ↓ on the accepted set.
2. Qualified count does not collapse (≥50% of current baseline, not ~0).
3. Unscored-bucket fail-open rate is understood (logged, not silent).
4. `p_win_ok` still gates `p_win_raw` (already coded) — do not retune `MIN_P_WIN` in the same flip.

### Rollback
Set `p_win_in_ev_mode: shadow` (or `off`) and/or `p_win_bracket_mode: shadow`. No schema change required.

---

## Not in this build

| Item | Status |
|---|---|
| Live replay table (`--live`) | Blocked on Studio + migration 160 apply |
| Mode flip | Assessment: **hold at shadow** |
| R3 cell-level enforcement | Separate |
| Heavy-sweep path `apply_bracket_to_ev` | Not wired (heavy is research; light is the product path) |

---

## Tasks

| ID | Status |
|---|---|
| T12 orchestrator wiring | **done** |
| T13 tests + replay | **done** (23 pass; fixture mixed) |
| T14 worklog + ACTIVE_MODES | **this file** |
