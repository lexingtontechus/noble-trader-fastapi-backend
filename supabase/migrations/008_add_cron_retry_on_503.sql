-- Patch 008 (v2): Add 503 BOOT_ERROR retry logic to sim_invoke_edge_function
-- ======================================================================
-- PROBLEM: net.http_post is fire-and-forget. When Supabase returns
--          503 BOOT_ERROR (cold start timeout), the cron job has no
--          idea it failed. The symbol stays stale until the next cycle.
--          This is especially bad at off-peak hours (3-5 AM) when the
--          worker pool is completely cold.
--
-- ROOT CAUSE OF v1 FAILURE: The original 008 had two SQL syntax bugs:
--   1. "SELECT id INTO v_request_id FROM net.http_post(...)" — WRONG.
--      net.http_post() returns bigint, not a record with an id column.
--      Correct: "SELECT net.http_post(...) INTO v_request_id"
--   2. "net.http_collect_response(id, timeout_milliseconds := 1000)" — WRONG.
--      timeout_milliseconds parameter does not exist in pg_net.
--      Correct: "net.http_collect_response(id)"
--   Both errors caused CREATE OR REPLACE FUNCTION to fail, silently
--   leaving the old 005 fire-and-forget version in place.
--
-- SOLUTION (v2): Rewrite with correct pg_net 0.20 API:
--   1. Fire request via net.http_post() → get request_id (bigint)
--   2. Call net.http_collect_response(request_id) — this BLOCKS until
--      the HTTP response is available.
--      - 503 BOOT_ERROR returns in <2s → we detect and retry
--      - 200 OK returns in 30-130s → we wait for it (acceptable for
--        single-symbol cron jobs)
--   3. If 503, wait 15s for workers to free up, then retry (up to 3x)
--
-- Timing budget per symbol:
--   Normal: net.http_collect_response blocks 30-130s → returns 200 → done
--   Retry:  2s (503) + 15s (wait) + 30-130s (retry success) ≈ 47-147s
--   Max:    3 × (2s + 15s) = 51s (all 3 attempts fail with 503)
--   pg_cron has no hard timeout per job, so all paths are safe.

-- ─── Rewrite sim_invoke_edge_function with retry ──────────
CREATE OR REPLACE FUNCTION sim_invoke_edge_function(
  p_function TEXT,
  p_symbol   TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url        TEXT;
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_attempt    INTEGER := 1;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  WHILE v_attempt <= 3 LOOP
    -- Fire the request (returns bigint request_id immediately)
    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('symbol', p_symbol)
    ) INTO v_request_id;

    -- Collect the response (BLOCKS until available)
    -- 503 BOOT_ERROR returns in <2s → we catch it quickly
    -- 200 OK returns in 30-130s → we wait (fine for single-symbol jobs)
    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      -- If collect fails for any reason, assume request was sent
      v_status := NULL;
    END;

    -- Success
    IF v_status = 200 THEN
      RAISE NOTICE '[sim_invoke] %/%: 200 OK (attempt %)', p_function, p_symbol, v_attempt;
      RETURN;
    END IF;

    -- 503 BOOT_ERROR — retry after delay
    IF v_status = 503 AND v_attempt < 3 THEN
      RAISE NOTICE '[sim_invoke] %/%: 503 BOOT_ERROR (attempt %), retrying in 15s...',
        p_function, p_symbol, v_attempt;
      PERFORM pg_sleep(15);
      v_attempt := v_attempt + 1;
      CONTINUE;
    END IF;

    -- Other error or max retries exhausted
    IF v_status IS NOT NULL THEN
      RAISE NOTICE '[sim_invoke] %/%: HTTP % after % attempts, giving up',
        p_function, p_symbol, v_status, v_attempt;
    ELSE
      RAISE NOTICE '[sim_invoke] %/%: could not collect response (attempt %)',
        p_function, p_symbol, v_attempt;
    END IF;

    RETURN;
  END LOOP;
END;
$$;

-- Revoke from non-admin roles
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM authenticated;

-- ─── Also update batch invoker ─────────────────────────────
CREATE OR REPLACE FUNCTION sim_invoke_edge_function_batch(
  p_function    TEXT,
  p_asset_class TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url        TEXT;
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_attempt    INTEGER := 1;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  WHILE v_attempt <= 3 LOOP
    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('asset_class', p_asset_class)
    ) INTO v_request_id;

    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      v_status := NULL;
    END;

    IF v_status = 200 THEN
      RETURN;
    END IF;

    IF v_status = 503 AND v_attempt < 3 THEN
      RAISE NOTICE '[sim_invoke_batch] %/%: 503 (attempt %), retrying in 15s...',
        p_function, p_asset_class, v_attempt;
      PERFORM pg_sleep(15);
      v_attempt := v_attempt + 1;
      CONTINUE;
    END IF;

    RETURN;
  END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM authenticated;
