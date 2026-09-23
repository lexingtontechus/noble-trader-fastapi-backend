# L1 status + nt_roadmap update — 2026-09-23

> **Continues:** `20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md`  
> **Scope:** Close the “still not built” list; refresh `nt_roadmap` flip rows.

---

## 1. Built since 2026-09-22 addendum

| Item | Commit / migration | Objective | Expected result |
|---|---|---|---|
| `min_p_win_bracket: 0.20` | `d479170` | Fix wrong-scale 0.45 gate | ~21% pass on scored book |
| R3 cell-level enforcement | `d479170` / m162 | Bias per (symbol, side, vol_tier, bucket); cell→symbol→none | No cross-symbol crush; underconfident keeps Kelly |
| nobletradingapp L1 UI | portal `6913791` | `thin_evidence` + unit-aligned baseline | Honest L1 board + R5 delta |
| P2 live expectancy | `a7505f3` / m163 | Flip-evidence cohorts | 7d/28d: expired%↓ + expectancy_r↑/flat |
| Heavy-sweep R2 | `8d4bd99` | Parity with light | `apply_bracket_to_ev` / shadow + sim stamp |
| Supabase 15s+retry + typed logs | `f2f4265` | Kill empty `ReadTimeout('')` noise | Markov miss = WARNING |

Migrations **160–163** applied. Modes remain **shadow** (`p_win_bracket_mode`, `p_win_in_ev_mode`).

---

## 2. Flip bar (do not skip)

1. ≥7d of `nt_live_expectancy_daily` at `min_p_win_bracket: 0.20`.
2. `bracket_accept` vs `all_qualified`: **expired% ↓ AND expectancy_r ↑/flat**.
3. `p_win_in_ev_mode: live` first → +7d → `p_win_bracket_mode: live` if |bias_raw| improves.
4. Rollback: flags back to `shadow`/`off` (no schema change).

---

## 3. nt_roadmap update

Upsert (or paste into Supabase SQL editor) — script:
`scripts/update_nt_roadmap_r2.py`. SQL equivalent:

```sql
INSERT INTO nt_roadmap (
  mechanism, current_state, set_at, review_at, review_condition,
  status, worklog_ref, notes, updated_at
) VALUES
(
  'p_win_in_ev_mode',
  'shadow',
  now(),
  now() + interval '7 days',
  'After ≥7d of nt_live_expectancy_daily at min_p_win_bracket=0.20: bracket_accept vs all_qualified shows expired% DOWN and expectancy_r UP or flat. Then flip p_win_in_ev_mode to live first.',
  'pending',
  'worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md',
  '2026-09-23: R2 wired light+heavy (apply_bracket_to_ev/shadow). min_p_win_bracket=0.20 live in feature_flags.yaml. P2 nt_live_expectancy_daily (m163) is the flip-evidence table. HOLD shadow until the rollup bar is met. Rollback = flag to shadow/off.',
  now()
),
(
  'p_win_bracket_mode',
  'shadow',
  now(),
  now() + interval '14 days',
  'Only after p_win_in_ev_mode is live and |bias_raw| improves on the accepted set for +7d. Then flip p_win_bracket_mode to live (bracket gate participates in qualification).',
  'pending',
  'worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md',
  '2026-09-23: min_p_win_bracket=0.20 (was 0.45 — wrong scale, max p_win_bracket~0.31). Shadow gate log active. Flip SECOND, after EV mode. Rollback = flag to shadow/off.',
  now()
)
ON CONFLICT (mechanism) DO UPDATE SET
  current_state = EXCLUDED.current_state,
  set_at = EXCLUDED.set_at,
  review_at = EXCLUDED.review_at,
  review_condition = EXCLUDED.review_condition,
  status = EXCLUDED.status,
  worklog_ref = EXCLUDED.worklog_ref,
  notes = EXCLUDED.notes,
  updated_at = EXCLUDED.updated_at;
```

---

## 4. Next priority

1. Studio pull/restart for `f2f4265` (timeout/retry + R2 hardening).
2. Accumulate ≥7d `nt_live_expectancy_daily`.
3. Flip `p_win_in_ev_mode` only when the bar is met; then `p_win_bracket_mode`.
