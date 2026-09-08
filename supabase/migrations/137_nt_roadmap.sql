-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 137: nt_roadmap
--
-- 2026-09-08: pulse-check's own SKILL.md pointed at
-- worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md as "the roadmap" for review
-- dates -- but that file is dated, static prose, written once and never
-- revisited. Nobody (human or agent) reliably re-checks N worklogs
-- against today's date every day, which is exactly how a known root
-- cause (worklog 20260825_US500_USDCAD_TIME_HORIZON_SCOPE.md) sat
-- unbuilt for two weeks, and how macro_regime_mode's 2026-09-21 review
-- clock kept running after a 4-day data freeze silently invalidated it.
--
-- This table IS the roadmap: one row per mode-gated mechanism or pending
-- action, with a real `review_at` timestamp pulse-check can query
-- mechanically (`WHERE review_at < now() AND status NOT IN ('resolved',
-- 'superseded')`) instead of a human/agent judgment call over prose.
-- Worklogs stay exactly as they are (historical narrative, append-only,
-- never revisited) -- this table is what replaces re-reading them to
-- find out what's overdue.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_roadmap (
  mechanism          TEXT PRIMARY KEY,
  current_state      TEXT NOT NULL,
  set_at             TIMESTAMPTZ,
  review_at          TIMESTAMPTZ,
  review_condition   TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'overdue', 'resolved', 'superseded', 'blocked')),
  worklog_ref        TEXT,
  notes              TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_roadmap IS
  'THE roadmap -- live, queryable, one row per mode-gated mechanism or pending action. Built 2026-09-08 (worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md) because the prior "roadmap" (a dated worklog file) was never mechanically re-checked and let review dates go stale/silently invalidated. pulse-check queries this FIRST, every run: `select * from nt_roadmap where status not in (''resolved'',''superseded'') and (review_at is null or review_at < now())`. Update in place when a mechanism''s state changes -- this is NOT append-only like worklog/*.md.';
COMMENT ON COLUMN nt_roadmap.review_at IS
  'When this should next be checked. NULL means no date-based trigger (see review_condition for what actually gates it). If a prior review_at is invalidated by an unrelated event (e.g. a data freeze resetting an accumulation clock), UPDATE this field with the corrected date and note why in `notes` -- do not leave a stale date standing.';

ALTER TABLE nt_roadmap ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_roadmap;
CREATE POLICY "Service role full access" ON nt_roadmap
  FOR ALL TO service_role USING (true) WITH CHECK (true);
