-- ═══════════════════════════════════════════════════════════════════════════════
-- 032: nt_symbol plan assignment — multi-plan curation columns
--
-- Background
-- ─────────
-- nt_symbol (formerly sim_symbols) holds the canonical symbol universe that the
-- sweep orchestrator (LightningAI) iterates over. With the launch of the
-- noble-trader-proxy codebase + Railway Redis, signals for each symbol must be
-- fan-out-routable to specific subscription plans:
--
--   • Signal Scout  ($79/mo)  — entry-level signal feed
--   • Precision Pro ($199/mo) — full signal feed + hermes agent integration
--
-- A symbol may belong to BOTH plans, ONE plan, or NEITHER (paused). The
-- `plan_ids UUID[]` column encodes this membership as an array so that:
--
--   1. The proxy can SELECT symbols WHERE plan_ids @> ARRAY[<plan_uuid>]::uuid[]
--      in a single round-trip.
--   2. The admin UI (nobletradingapp) can curate which plans see which symbols
--      without spinning up per-plan symbol tables.
--   3. The sweep orchestrator remains unchanged — it iterates ALL active
--      symbols; plan filtering happens at the proxy / Redis stream fan-out
--      layer, not at sweep time.
--
-- Schema changes
-- ──────────────
--   1. plan_ids      UUID[]      — array of plans.id values (NULL = unassigned)
--   2. curated_at    TIMESTAMPTZ — when plan assignment was last touched
--   3. curated_by    UUID        — public.users.id of the admin who curated
--
-- Postgres does not support FK constraints on array columns natively, so we
-- enforce referential integrity via a BEFORE INSERT/UPDATE trigger that
-- verifies every element of plan_ids exists in public.plans(id).
--
-- Backfill
-- ─────────
-- All currently-active symbols are assigned to BOTH plans (Signal Scout +
-- Precision Pro). This preserves the pre-migration behaviour (every symbol
-- flowed to every consumer) and is the safest non-breaking default. The
-- admin UI can later remove symbols from a plan if a curated universe is
-- desired for a specific tier.
--
-- The backfill uses a subquery on plans.title (NOT a hardcoded UUID) so it
-- works regardless of the gen_random_uuid() value assigned at plan-seed time.
--
-- Dependencies
-- ────────────
--   • public.plans           — created in nobletradingapp migration 0001_init.sql
--   • public.users           — created in nobletradingapp migration 0001_init.sql
--   • nt_symbol (public)     — created in backend migration 001_sim_tables.sql
--                              (renamed from sim_symbols in 028_add_tradingview_datasource)
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Add columns ───────────────────────────────────────────────────────
ALTER TABLE nt_symbol
  ADD COLUMN IF NOT EXISTS plan_ids    UUID[]        DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS curated_at  TIMESTAMPTZ   DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS curated_by  UUID          DEFAULT NULL
    REFERENCES public.users(id) ON DELETE SET NULL;

COMMENT ON COLUMN nt_symbol.plan_ids IS
  'Array of public.plans.id values that this symbol is curated for. '
  'NULL = unassigned (symbol is paused for plan delivery but still swept). '
  'Referential integrity enforced by trg_nt_symbol_plan_ids_fk trigger.';
COMMENT ON COLUMN nt_symbol.curated_at IS
  'Timestamp of the last manual change to plan_ids. NULL = never curated (backfilled).';
COMMENT ON COLUMN nt_symbol.curated_by IS
  'public.users.id of the admin who last changed plan_ids. NULL = system backfill.';

-- ─── 2. GIN index on plan_ids for array-containment queries ───────────────
-- Supports:  WHERE plan_ids @> ARRAY[<plan_uuid>]::uuid[]
CREATE INDEX IF NOT EXISTS idx_nt_symbol_plan_ids_gin
  ON nt_symbol USING GIN (plan_ids);

-- ─── 3. FK validation trigger for plan_ids[] ──────────────────────────────
-- Postgres FKs cannot target array elements, so we validate in a trigger.
-- On INSERT or UPDATE of plan_ids, verify every element exists in plans(id).
-- Raise an exception (aborting the write) if any element is orphaned.

CREATE OR REPLACE FUNCTION fn_validate_nt_symbol_plan_ids()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
  orphan UUID;
BEGIN
  IF NEW.plan_ids IS NULL THEN
    RETURN NEW;
  END IF;

  SELECT p.id INTO orphan
    FROM unnest(NEW.plan_ids) AS p(id)
    WHERE NOT EXISTS (
      SELECT 1 FROM public.plans pl WHERE pl.id = p.id
    )
    LIMIT 1;

  IF orphan IS NOT NULL THEN
    RAISE EXCEPTION
      'nt_symbol.plan_ids contains unknown plan UUID %', orphan
      USING ERRCODE = 'foreign_key_violation';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_nt_symbol_plan_ids_fk ON nt_symbol;
CREATE TRIGGER trg_nt_symbol_plan_ids_fk
  BEFORE INSERT OR UPDATE OF plan_ids ON nt_symbol
  FOR EACH ROW
  EXECUTE FUNCTION fn_validate_nt_symbol_plan_ids();

COMMENT ON FUNCTION fn_validate_nt_symbol_plan_ids() IS
  'Validates that every UUID in nt_symbol.plan_ids references an existing '
  'public.plans.id. Postgres cannot enforce FK on array elements natively.';

-- ─── 4. Backfill: assign all active symbols to BOTH plans ─────────────────
-- Uses plans.title subquery so this works regardless of the auto-generated
-- UUID assigned at seed time. Idempotent — safe to re-run.
--
-- After backfill:
--   • Every active symbol is visible to Signal Scout + Precision Pro
--   • curated_at = NULL (signals "system backfill, not admin-curated")
--   • curated_by = NULL
--
-- A subsequent admin-UI update will set curated_at + curated_by when a human
-- actually curates the list.

UPDATE nt_symbol
  SET plan_ids = ARRAY(
        SELECT id FROM public.plans
        WHERE title IN ('Signal Scout', 'Precision Pro')
          AND is_active = true
      ),
      curated_at = NULL,    -- NULL distinguishes system backfill from manual curation
      curated_by = NULL,
      updated_at = now()
WHERE active = true
  AND plan_ids IS NULL;     -- only backfill rows that haven't been curated yet

-- ─── 5. Verify ────────────────────────────────────────────────────────────
SELECT
  ns.symbol,
  ns.asset_class,
  ns.exchange,
  ns.active,
  ns.plan_ids,
  COUNT(p.id) AS matched_plans
FROM nt_symbol ns
LEFT JOIN unnest(ns.plan_ids) AS ns_pid ON TRUE
LEFT JOIN public.plans p ON p.id = ns_pid
GROUP BY ns.symbol, ns.asset_class, ns.exchange, ns.active, ns.plan_ids
ORDER BY ns.asset_class, ns.symbol;

-- Show trigger + index
SELECT
  tgname AS trigger_name,
  tgtype AS trigger_type_bits
FROM pg_trigger
WHERE tgrelid = 'nt_symbol'::regclass
  AND NOT tgisinternal;

SELECT
  indexname,
  indexdef
FROM pg_indexes
WHERE tablename = 'nt_symbol'
  AND indexname = 'idx_nt_symbol_plan_ids_gin';
