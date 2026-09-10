# 2026-08-25 — Redis deprecation Phase 3: operational services to Supabase-only

**Scope:** `regime_platform/services/operational/mode_manager.py`,
`kill_switch.py`, `reconciler.py`, new
`regime_platform/services/operational/system_config.py`,
`regime_platform/routers/operational.py`. Phase 3 of the full Redis
deprecation plan (`.claude/plans/goofy-rolling-swing.md`, Part 3) —
follows Phase 0 (orchestrator), Phase 1 (renko/), Phase 2 (main.py's
lifespan services), all already live.

**Status:** Built, tested (611 passed, 0 failed), functionally dry-run
validated (mode_manager's token flow — see below), committed (`d410f6b`),
pushed to `origin/main`. Awaiting Studio restart + live verification.

---

> **Priority**: P3 · **Task**: Redis deprecation Phase 3: migrate
> operational services (mode_manager, kill_switch, reconciler) from Redis
> to Supabase-only ·
> **Status**: resolved (confirmed "already live" by the Phase 4 worklog) ·
> **Date**: 2026-08-25

## Problem

Three operational services each independently duplicated the same
Redis GET/SET/DELETE boilerplate for durable state, on top of already
having (in `mode_manager`'s case) a partial Supabase write-through:

- **`mode_manager.py`**: mode state (paper/live/simulation) read Supabase
  first, then Redis, then inferred from Alpaca config — reasonably
  layered. But the **confirmation-token flow had no Supabase path at
  all** — `request_mode_change`/`confirm_mode_change` stored/read the
  5-minute confirmation token exclusively in Redis. With Redis down (as
  it has been this session), `request_mode_change` would silently
  produce a token that could never be confirmed (the `if client:` guard
  just skipped storage without raising), and `confirm_mode_change` hit
  `if not client: raise RuntimeError("Redis unavailable...")` immediately.
  Paper↔live mode switching was fully broken, independent of anything
  else in this migration. `get_mode_state()` also had no Supabase
  fallback (only `get_current_mode()` did) — another gap.
- **`kill_switch.py`**: already fully fail-open (`is_halted()`/
  `get_active_halt()` never raise, they just fall through to
  False/None). Background refresh has been deactivated since v7.4
  (a prior incident — Redis pub/sub caused a Render crash loop), so the
  in-memory cache was already the real enforcement mechanism for this
  single-process deployment. Redis was write-only "for future use."
- **`reconciler.py`**: Redis held `LAST_RUN_KEY` (read in `get_status()`,
  written in `reconcile_all_recent()`) alongside an existing Supabase
  write-through for the same value — Redis was actually redundant here
  already, just not removed. `RESULTS_KEY`/`RESULTS_TTL_SECONDS` were
  already fully dead code (declared, never read or written anywhere).

## Fix

1. **Added `system_config.py`** — a small shared `get_config`/`set_config`/
   `delete_config` helper against Supabase's `nt_system_config` table
   (the same durable store `mode_manager` and `reconciler` already wrote
   to independently). Fail-open by design: every method returns
   `None`/`False` on any error rather than raising, matching the
   "best-effort persistence, not source of truth" posture all three
   services already had with Redis.
2. **`mode_manager.py`**: removed `_redis`/`_get_client()` entirely.
   `get_current_mode()` now tries Supabase then Alpaca-config inference
   (Redis tier deleted). `get_mode_state()` gained the Supabase read it
   never had. **The token fix**: `request_mode_change()` now stores the
   token via `system_config.set_config()` and raises a clear
   `RuntimeError` only if that write genuinely fails (Supabase down) —
   no more silent "invisible" tokens. `confirm_mode_change()` reads the
   token via `get_config()` and checks expiry manually against the
   stored `requested_at` timestamp (`age_seconds > TOKEN_TTL`), since
   Supabase has no native per-key TTL the way Redis did. Also fixed a
   latent inconsistency while touching this code: the Supabase mode-state
   write previously used a hardcoded `"noble:trading:mode"` key while the
   Redis write used the `tenant_key()`-scoped key — now both reads/writes
   consistently use `tenant_key(MODE_BASE_KEY, org_id=org_id)` (identical
   to the old hardcoded string today since `org_id` is always `None` in
   this single-tenant deployment, but no longer silently divergent if
   that ever changes).
3. **`kill_switch.py`**: `_redis_get_record()` → `_stored_get_record()`,
   backed by `get_config()`. `activate_halt()`/`deactivate_halt()` now
   call `set_config()`/`delete_config()`. In-memory-cache-first behavior
   is completely unchanged — this only swaps what the on-demand
   fallback reads/writes.
4. **`reconciler.py`**: removed `_redis`/`_get_client()` and the
   already-dead `RESULTS_KEY`/`RESULTS_TTL_SECONDS` constants.
   `reconcile_all_recent()` keeps its existing Supabase write-through as
   the sole persistence path (dropped the redundant Redis write).
   `get_status()` now reads an in-memory `self._last_run_at` first
   (set at write time), falling back to a Supabase `get_config()` read
   for the case where this process hasn't reconciled anything yet since
   its own restart but a prior process did.
5. **`operational.py` router**: updated `GET /kill-switch/status`'s
   docstring (referenced "Redis" for where stale halt records might
   live — now says Supabase). Deleted `POST /kill-switch/clear-redis`
   entirely — an admin endpoint whose entire purpose (scan and delete
   `noble:halt:*` Redis keys) no longer has a target.

## Verification

Full suite: **611 passed, 21 skipped, 0 failed** — identical to Phase 2's
count, since none of these three services had existing test coverage to
update. `python -c "import main"` smoke-tested clean.

Given the complete absence of test coverage for `mode_manager.py` (the
file with the real behavior change — the token-expiry logic moved from
Redis-native TTL to a manual timestamp check) and its significance
(paper↔live trading-mode switching), ran a manual dry-run with
`get_config`/`set_config`/`delete_config` monkeypatched to an in-memory
fake store, exercising:
- Token issue → immediate confirm with a bad/missing Alpaca config →
  correct `RuntimeError` from the pre-switch health check (not a
  Supabase-related crash).
- Token issue → manually backdated `requested_at` beyond `TOKEN_TTL` →
  confirm correctly raises `ValueError("Invalid or expired confirmation
  token")`.
- Token issue → immediate confirm (PAPER target, no health check
  required) → mode change succeeds, `previous_mode`/`current_mode`
  correct.
- Confirm with a nonexistent token → correct `ValueError`.
- **Cross-instance persistence**: a brand-new `ModeManager()` instance
  (no in-memory cache) correctly read back the mode state written by a
  *different* instance via the fake Supabase store — this is the actual
  restart-survival property Redis used to provide, now proven to work
  through Supabase instead.

One incidental finding during the dry run: `audit_logger.log_event()`
(called internally by the mode-change audit trail, not mocked in this
dry run) made a real call against the actual configured Supabase project
and got a 404 (`Could not find the table 'public.trade_audit_log'`) —
harmless (no write occurred) and unrelated to this phase's changes, but
worth noting as a pre-existing gap: the `trade_audit_log` table
`audit_logger.py` expects doesn't currently exist in the live schema.
Out of scope for this phase; flagging for whoever next touches
`audit_logger.py`.

## Deployment status

Committed as `d410f6b`, pushed to `origin/main`. Not yet verified live —
next step is the user restarting the Studio and confirming via SSH that
`GET /operational/kill-switch/status` and a mode-change request/confirm
round-trip both work against the real Supabase project (not just the
mocked dry run).

## Not done in this pass

Phase 4 (delete `redis_pool.py`, remove remaining Redis settings, remove
the `redis` package dependency, delete `main_v3.py`/`main_v4.py` and
other orphaned dev scripts, doc cleanup) remains scoped in the plan but
not started — this was the last phase with actual Redis *consumers* to
migrate; Phase 4 is pure infra/dependency/docs cleanup once nothing
reads `redis_pool` anymore.
