-- 2026-09-16 reachability bootstrap (worklog
-- 20260916_REACHABILITY_BOOTSTRAP_UNBLOCK.md).
--
-- Migration 153 added n_stored/n_proxy at 0. Existing bucket rows were
-- built before those counters existed, so every row showed n_stored=0
-- even when atr_source='stored'. Batch C's p_win_bracket lookup then
-- failed open forever (insufficient_stored_n). Backfill treats legacy
-- 'stored' rows as fully stored for the Beta sample size.
--
-- Safe / idempotent: only fills zeros.

update nt_bracket_reachability
set n_stored = n
where atr_source = 'stored'
  and n_stored = 0
  and n > 0;

comment on column nt_bracket_reachability.n_stored is
  'Rows bucketed using the signal''s own stored ATR. Legacy rows with '
  'atr_source=stored were backfilled from n (migration 155).';
