# 2026-08-24 — Live calibration enforcement silently defeated by non-penalty "latest" rows

**Scope:** `regime_platform/sweep_orchestrator/calibration_enforcement.py`,
`regime_platform/sweep_orchestrator/drift_monitor.py`. Second of three findings from
digging into "Finding 2" of the 2026-08-24 HMM-convergence investigation
(Finding 3 done separately, Finding 1 next).

**Status:** Fixed, tested, full suite green. Not yet deployed.

---

## Problem — confirmed live, not theoretical

`fetch_latest_eod_action(symbol)` fetched the single most recent `nt_eod_actions`
row **of any action type**. `apply_calibration_adjustment` (the actual live
enforcement function — shrinks p_win, cuts Kelly sizing) treated any non-penalty
"latest" row as "nothing to enforce" (`reason: audit_only:<action>`).
`drift_monitor.check_drift`'s own "don't churn every 5 minutes" freshness gate
used the same fetch and the same logic, with the same blind spot.

`regime_shift_alert` (Layer 2, built 2026-08-23) writes to the same table and is
exactly such a non-penalty action. Queried live production data at the time of
investigation:
- **US500**: absolute latest row was a `regime_shift_alert` — enforcement
  disabled for a symbol flagged OVERCONFIDENT (bias=0.7565) that same morning.
- **USDCAD**: **9 consecutive `regime_shift_alert` rows, zero penalty actions
  interspersed, across a 2-hour span** — any active penalty was invisible to
  both call sites that whole time.

This is independent of the same-day regime-HMM standardization fix (Change A):
Change A reduces how often spurious shifts fire, but genuine shifts will always
happen sometimes, and each one — however rare — reopens this gap. The bug is a
missing filter, not a frequency problem.

## Fix

`fetch_latest_eod_action(symbol, actions=None)` — new optional `actions` param
adds `action=in.(...)` server-side to the query. `actions=None` preserves the
original "latest row of any type" behavior (no known caller needs it, kept for
safety). Both real callers now pass `actions=PENALTY_ACTIONS`:

- `apply_calibration_adjustment`: switched to the filtered fetch, which made
  the `if action_name not in PENALTY_ACTIONS: return audit_only` branch
  unreachable — **removed**, not left dormant (the fetch itself now guarantees
  it can't happen).
- `drift_monitor.check_drift`: same fetch, same filter — its freshness gate now
  correctly recognizes a fresh penalty even when a newer audit-only row exists.

**Side benefit:** this also protects `stagnation_kill_switch`'s own reliability
— its design explicitly depends on being "the latest row" to take effect
(per its own code comment in `eod_correction.py`), which means it was equally
vulnerable to being buried by a `regime_shift_alert`. Fixed for free by the same
change.

## Verification

Two failure modes were caught and fixed during testing, worth recording:

1. Manually confirmed both call-site changes are load-bearing by reverting each
   and re-running the regression tests — `test_apply_calls_fetch_with_penalty_actions_filter`
   and `test_calls_fetch_with_penalty_actions_filter` both correctly fail
   without the fix.
2. The first version of the real-interaction integration test
   (`test_real_fetch_recognizes_fresh_penalty_buried_under_regime_shift_alert`)
   **passed even against the fully-reverted buggy code** — investigated why
   rather than accept a green result at face value. Two independent test bugs,
   not fix bugs: (a) both fake rows used separate `datetime.now()` calls that
   tied to the same microsecond, so Python's stable sort preserved list order
   by coincidence regardless of the fix; (b) `_insert_row` wasn't mocked, so
   the buggy code path's attempted duplicate-penalty write silently failed on
   a real network call and got swallowed by `check_drift`'s own best-effort
   try/except — producing `result is None` for the wrong reason. Fixed both
   (explicit distinct timestamps; mocked `_insert_row` and asserted on
   `insert_mock.assert_not_awaited()` as the real signal) and reconfirmed the
   test now genuinely fails without the fix before restoring it.
3. Fixed fallout in `tests/test_stagnation_breaker.py`: two tests defined a
   local `fake_fetch(symbol)` stand-in with a signature that didn't accept the
   new `actions` kwarg — updated to `fake_fetch(symbol, actions=None)`.

Full suite: **632 passed, 23 skipped, 0 failed** (up from 621 pre-change — 11
new tests: 4 in `test_calibration_enforcement.py`, 3 in `test_drift_monitor.py`,
plus the corrections above).

## Not done in this pass

- Finding 1 (drift-monitor's differently-windowed `bayesian_penalty` writes
  diluting the stagnation streak's own history) is scoped separately, next.
- `AUDIT_ONLY_ACTIONS = ("parameter_decay_check",)` in `calibration_enforcement.py`
  is not updated to include `regime_shift_alert` — it wasn't actually used in a
  membership check anywhere (the real logic was the removed `not in
  PENALTY_ACTIONS` branch), so left alone rather than expanding an already-dead
  list; flagging only in case a future reader wonders why it wasn't touched.
