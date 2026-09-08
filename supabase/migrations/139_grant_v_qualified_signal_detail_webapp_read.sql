-- 2026-09-08: v_qualified_signal_detail was created with only postgres/
-- service_role grants (confirmed via information_schema.role_table_grants)
-- -- the webapp (anon/authenticated) could never actually read it, despite
-- being built specifically to answer a webapp-facing reporting question.
-- Same read-only grant pattern as v_symbol_signal_scorecard (migration 138).

grant select on v_qualified_signal_detail to anon, authenticated;
