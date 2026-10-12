# The real defect — calibration enforcement clobbers the reachability EV

**Date:** 2026-10-12
**Scope:** Trace the full orchestrator chain to the single root cause of "-EV signals publishing as +EV," and fix it. Pure math, one change. No new shadow modes.
**Status:** Root-caused to one line; fix built, E2E + replay, deployed.
**Roadmap:** `calibration_reachability_reframe`.

---

## 1. Symptom (live Asia/Australia sweep, 2026-10-12 00:46 UTC)

XAUAUD sell, one tick, two contradictory numbers:

| stage | p_win | ev | kelly |
|---|---|---|---|
| Reachability path (log `R2 EV/Kelly`) | 0.0556 | **−1.5033** | 0.000000 |
| What `nt_signal_sim` stored + published | **0.5** | **+0.9022** | 1.0 |

A 5.6%-reachability signal shipped as **+EV, kelly=1, qualified, PUBLISHED.** 24h ground truth: 156 resolved, **7W/149L = 4.5% WR, −$1,057 PnL.** The measurement fixes (all_in, EV floor) are real but **inert at the publish decision** — the honest reachability EV is computed, then destroyed one step later.

## 2. Root cause — one line, pure math

The chain in `_process_symbol` (orchestrator.py), in order:

1. **compute_ev()** → directional `ev_result` (overconfident blend, p_win≈0.5–0.8).
2. **`apply_bracket_to_ev()`** (L4467, `p_win_in_ev_mode=live`) → mutates `ev_result` **in place**: `p_win = p_win_bracket` (0.0556), `ev = −1.50`. ✅ Honest.
3. **`apply_calibration_adjustment()`** (L4516) → returns a shrunk **directional** p_win (0.5), `applied=True` (the symbol has an active overconfidence penalty).
4. **L4528: `ev_result.p_win = calib_adj["p_win"]`** → **overwrites the reachability probability (0.0556) with the directional 0.5.**
5. **L4539: `ev_result.ev = recompute_ev_fields(ev_result.p_win, profit, stake)`** → recomputes EV from the **directional** p_win=0.5, **no `p_expire`, no bracket probability** → `ev = +0.90`.
6. Payload (L5725-5726) stores `p_win=0.5, ev=+0.90`; gate sees positive EV → **PUBLISHED.**

**The calibration-enforcement pass runs AFTER the bracket-EV substitution and reprices from the directional blend, clobbering the reachability result.** −1.50 → +0.90 in two lines. That is the entire circular failure: every measurement-layer fix (all_in, EV floor, reachability stamp) is computed correctly and then overwritten by a downstream pass that re-derives EV from the overconfident directional `p_win`.

## 3. Why the enforcement pass is wrong here (the math)

Calibration enforcement exists to shrink an **overconfident directional** p_win toward 0.5. But once `apply_bracket_to_ev` has substituted the **reachability** probability, that probability is *already* the honest era-clean base rate — it is **not** the overconfident directional number the penalty was designed to correct. Shrinking it toward 0.5 is exactly backwards:

- Reachability 0.0556 (5.6% — the truth) shrunk toward 0.5 → 0.5 → repriced to +EV. **The penalty re-inflates the honest low probability back toward the overconfident regime it came from.**
- For the wide +EV pocket (BTCUSD 2-4, reachability 0.42), shrinking toward 0.5 would *lower* a legitimately high probability.

The two corrections operate on **different quantities** and must not stack. When the bracket EV is applied, the reachability probability supersedes the directional one, and enforcement has nothing to shrink.

## 4. The fix

When `_bracket_applied_to_ev` is True, **calibration enforcement must not reprice from the directional p_win.** Two sub-cases:

- If the reachability probability is BELOW the directional (the normal overpriced case), enforcement would inflate it → **skip enforcement's EV repricing; keep the reachability EV.** (The reachability probability is already the corrected number.)
- Enforcement's *diagnostic* stamps (bias/status) can still record, but the EV/p_win used for qualification and publish stays the reachability values.

Concretely: guard the L4528/L4539 mutation so it only runs when the bracket EV was **not** applied. When it was applied, `ev_result` keeps its reachability p_win/ev, and the EV floor + bracket gate judge that.

## 5. What this changes

- XAUAUD-type signals (reachability 0.056, ev −1.50) → **no longer repriced to +0.90** → EV floor blocks them → **not published.**
- The +EV pocket (BTCUSD 2-4, metals 4+ where reachability genuinely clears the floor) → **published**, EV intact.
- The book finally reflects the reachability math all the way to the publish decision. Signal count drops to the honest +EV set.

## 6. E2E + replay

`tests/test_ev_not_clobbered_by_enforcement.py`: pure-function replay of the two orderings — with the fix, a reachability p_win of 0.056 stays 0.056 and ev stays negative through the enforcement stage; without it, enforcement inflates to 0.5/+EV. Pins the contract: **bracket-applied EV is terminal; enforcement must not overwrite it.**

## 7. Files

- `regime_platform/sweep_orchestrator/orchestrator.py` — guard the enforcement mutation on `not _bracket_applied_to_ev`.
- `tests/test_ev_not_clobbered_by_enforcement.py` — E2E + replay.
- `worklog/20261012_ENFORCEMENT_CLOBBERS_REACHABILITY_EV.md` — this.
