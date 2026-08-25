# 2026-08-25 — Auth simplification: JWT/Clerk/per-user → single shared API key

**Scope:** `regime_platform/auth/` (new `api_key.py`, deleted
`jwt_auth.py`/`clerk_auth.py`/`auth_config.py`, rewritten `__init__.py`),
`regime_platform/routers/auth_router.py` (deleted), 22 router files,
`renko/router.py`, `main.py`, `regime_platform/core/settings.py`,
`regime_platform/services/pnl_service.py` (verified, no changes needed),
`requirements.txt`/`pyproject.toml`/`uv.lock`, `README.md`,
`.env.example`, `render.yaml`, both `docs/openapi.yaml` copies,
`tests/test_calibration_router_unification.py`. Part 4 of
`.claude/plans/goofy-rolling-swing.md`.

**Status:** Built, tested (611 passed, 0 failed), end-to-end smoke
tested, committed (`8c363b0`), pushed to `origin/main`.

---

## Problem / design decision

"jwt and auth are no longer supported. scope for cleanup." → scoped via
a read-only Explore pass (same discipline as the Redis deprecation's
3-parallel-agent sweep) → design settled once the user confirmed: "there
are no live users, the only fallback is a render deployment... I think
maybe a generic auth for gatekeeping will suffice?"

This wasn't "delete all auth" — it was "replace the multi-tenant
JWT/Clerk/per-user-role system with one shared gatekeeping secret,"
since there was never a real multi-tenant identity behind ~116
`Depends(get_authed_user)`/`require_write`/`require_admin` call sites
across 22 router files + `main.py`. A single `API_KEY` value was chosen
over a comma-separated set — no rotation/multi-key need with one
deployment.

Four items were flagged during scoping as NOT pure gatekeeping and
needed an explicit resolution before touching:

1. **`renko/router.py`'s live-order execution** resolves per-user/org
   Alpaca credentials via `resolve_alpaca_credentials(user.sub,
   org_id=user.org_id)`. Confirmed this whole path already sits behind
   `NOBLE_TRADER_DISABLE_EXECUTION` (default `True`) — real submission
   is blocked deeper inside regardless. **User direction: deprecate,
   don't delete** — fall back to global `ALPACA_API_KEY`/
   `ALPACA_SECRET_KEY` env credentials, keep the path reachable in case
   `NOBLE_TRADER_DISABLE_EXECUTION` is ever flipped for emergency use.
2. **Data-scoping fields** (`backtest.py`'s `user_id=user.sub`,
   `operational.py`'s `triggered_by=user.sub`, `pnl.py`'s export/
   alert-threshold storage, `risk.py`'s `user_id`/`org_id`, `config.py`'s
   `changed_by=user.sub`) — resolved: replace with a fixed constant
   (`DEFAULT_ACTOR = "system"`) rather than deleting the columns/calls,
   so audit-trail shape and any downstream queries keep working.
3. **`main.py`'s `/forecast` proxy** (documented as an anti-open-proxy
   control, not identity) and **4. the FastAPI-MCP mount** — resolved:
   both move to the same `require_api_key` gate. "Generic auth for
   gatekeeping" applies directly — these were never about identity, just
   about not being open/anonymous.

## Fix

1. **New `regime_platform/auth/api_key.py`**: `require_api_key` (FastAPI
   dependency, checks `X-API-Key` header against `settings.API_KEY`,
   fails closed when unset), `ws_auth` (same check for WebSocket
   handshakes, raises `WebSocketAuthError` before `accept()`), and
   `DEFAULT_ACTOR = "system"`. Rewrote `auth/__init__.py` to export
   these three names instead of the old JWT/Clerk surface.
2. **Deleted**: `jwt_auth.py`, `clerk_auth.py`, `auth_config.py`,
   `routers/auth_router.py` (all `/auth/token`, `/auth/refresh`,
   `/auth/clerk/*` endpoints), `test_auth_modes.py`, `test_auth_simple.py`
   (both repo-root, not pytest-collected), `docs/lightningai_auth_guide.md`,
   `docs/jwt_disable_implementation.md`.
3. **Mechanical strip** across `calibration.py`, `feeds.py`, `gpu.py`,
   `multi_asset.py`, `observation.py`, `pipeline.py`, `portfolio.py`,
   `regime.py`, `simulate.py`, `sizing.py`, `strategy.py`,
   `stream_rest.py`, `tda.py`, `venue.py`, `routers/__init__.py`: swapped
   every `Depends(get_authed_user/require_write/require_admin)` for
   `Depends(require_api_key)`, dropped the `user: TokenData` parameter
   entirely. Verified via grep (`\buser\.`) that none of these 14 files
   read `user` after the dependency — confirmed clean.
4. **`renko/router.py`** (29 endpoints, the heaviest single consumer):
   same mechanical strip via `sed`, plus real fixes the auth-scoping
   report's "pure gatekeeping" read had missed:
   - `_execute_signal`/`_close_pipeline_position` dropped their `user`
     parameter; `resolve_alpaca_credentials(user.sub, org_id=user.org_id)`
     → `resolve_alpaca_credentials(DEFAULT_ACTOR)` (the function already
     had a global-env fallback built in — this was true "deprecate, not
     delete," zero new code needed there).
   - Several `_pipeline_key(symbol, getattr(user, 'sub', None) if user
     else None)` call sites simplified to `_pipeline_key(symbol, None)`
     — single-admin mode, which the helper already supported.
   - Three `log.warning(f"... by user {user.sub}")` audit lines and one
     Discord notification payload field switched to `DEFAULT_ACTOR`.
5. **Data-scoping files** (`backtest.py`, `operational.py`, `pnl.py`,
   `risk.py`, `config.py`): bulk `sed` replaced `user.sub`→`DEFAULT_ACTOR`,
   `user.org_id`→`None`, alongside the signature strip. `config.py`
   additionally had `is_admin = user.role == "admin"` (gates whether
   sensitive config values are masked) → `is_admin = True` — the single
   shared key now IS the old admin tier, there's no lesser tier to
   distinguish. Removed the now-dead `if user.role != "admin": raise
   HTTPException(403, ...)` block on the audit-log endpoint.
6. **`stream_ws.py`**: `/ws/{symbol}` dropped the `?token=<jwt>` query
   param entirely (api-key-only now); `/sse/pnl`'s per-user Alpaca P&L
   stream subscription (`alpaca_stream_manager.subscribe(user.sub)` /
   `unsubscribe(user.sub, q)`, plus the SSE payload's `user_id` field)
   switched to `DEFAULT_ACTOR`. The other 3 endpoints got the standard
   mechanical strip.
7. **`pnl_service.py`**: verified, no changes needed — `_resolve_credentials`/
   `_alpaca_get`/`list_alert_thresholds`/etc. are already generically
   parameterized by `user_id: str`, so they transparently receive
   `DEFAULT_ACTOR` from the now-updated `pnl.py` callers.
8. **`main.py`**: `/forecast` proxy's `_user: TokenData =
   Depends(get_authed_user)` → `_: None = Depends(require_api_key)`. The
   MCP mount's conditional `if settings.MCP_AUTH_ENABLED: ... else: None`
   auth toggle removed — `AuthConfig(dependencies=[Depends(require_api_key)])`
   unconditionally now. Removed `auth_router` from the router-mount list
   and the `_mcp_excluded_ops` list's 6 now-nonexistent auth operation IDs.
9. **Settings**: removed `JWT_SECRET_KEY`, `JWT_ALGORITHM`,
   `JWT_EXPIRE_MINS`, `AUTH_USERS`(+`auth_users_dict`),
   `CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`, `CLERK_JWKS_URL`
   (+`clerk_auth_enabled`), `MCP_ISSUER`, `API_KEYS`(+`api_keys_set`),
   `AUTH_ENABLED`, `MCP_AUTH_ENABLED`. Also caught (via grep, not in the
   original scoping list) `DISCORD_WEBHOOK_DEBUG` — already fully
   orphaned since the Redis deprecation removed its only mechanism
   (`DEBUG_SIGNALS` + `signal.debug.noble_trader`). Added `API_KEY: str
   = ""`. Updated `model_post_init`'s startup warning accordingly.
10. **Dependencies**: removed `python-jose[cryptography]`,
    `passlib[bcrypt]`, `PyJWT[crypto]`, `clerk-backend-api` from
    `requirements.txt`/`pyproject.toml` (confirmed zero remaining
    imports via grep first), regenerated `uv.lock` via `uv lock`
    (dropped `bcrypt`, `clerk-backend-api`, `ecdsa`, `passlib`, `pyasn1`,
    `python-jose`, `rsa` — `PyJWT` itself stayed, confirmed still needed
    transitively by the `mcp`/`fastapi-mcp` packages).
11. **Docs**: rewrote README.md's `## Authentication` section (was a
    ~180-line JWT/Clerk mode guide with curl examples for `full`/
    `api_key_only`/`disabled` modes) down to the single-key model, plus
    its architecture-tree, dependency-table, and endpoint-table
    mentions. Rewrote `.env.example`'s entire Auth block (was
    `AUTH_MODE`/`AUTH_ENABLED`/`MCP_INTERNAL_KEY`/JWT/`API_KEYS`/
    `AUTH_USERS`/Clerk — confirmed `MCP_INTERNAL_KEY` fully orphaned too,
    its only reader was inside the deleted `jwt_auth.py`) down to one
    `API_KEY=` line. Fixed `render.yaml`'s `CLERK_SECRET_KEY`/
    `MCP_AUTH_ENABLED`/`JWT_SECRET_KEY` env-var declarations. Fixed both
    `docs/openapi.yaml` copies (confirmed byte-identical before and
    after each edit via `diff`): collapsed ~35 `- BearerAuth: []` / `-
    ApiKeyAuth: []` pairs down to a single `ApiKeyAuth` security scheme
    (and its `securitySchemes` definition), removed the dead
    `/auth/debug/clerk-flow` path entirely, rewrote the spec's top-level
    Authentication description, and bulk-replaced ~30 "**Admin-only
    endpoint** - requires the `admin` role" / "**Write access** -
    requires `admin` or `trader` role" per-operation descriptions that no
    longer describe a real role tier.

## Verification

**Full suite regression caught during this pass**: after the mechanical
strip, `tests/test_calibration_router_unification.py` failed 3 tests —
they called `calibration_router.get_calibration_bias(...)` /
`get_calibration_status(...)` directly with a `user=None` keyword arg,
which no longer exists on the rewritten signatures
(`TypeError: got an unexpected keyword argument 'user'`). Fixed by
dropping the now-nonexistent kwarg from all three test calls.

**Real bugs the "pure gatekeeping" scoping read missed, caught by a
post-edit `pyflakes` undefined-name sweep** (not by trusting the earlier
Explore-agent report at face value): `renko/router.py` had several
`user.sub`/`getattr(user, ...)` reads inside endpoint bodies that the
initial per-file grep during scoping had lumped in as "read-only, safe
to strip" — these would have been silent `NameError`s at runtime the
first time those code paths executed (pipeline-key generation, sizing/
execution-mode-change audit logs), not caught by static syntax checks
or `ast.parse()`. Running `python -m pyflakes` across every touched file
after each stripping pass caught all of them before they shipped —
worth repeating as a checklist item for any future large mechanical
dependency-signature rewrite, since `ast.parse()` alone does not detect
undefined names.

Full suite: **611 passed, 21 skipped, 0 failed** — both immediately
after the mechanical strip (with the 3 calibration-router test fixes)
and again after all docs work.

**End-to-end smoke test** (`TestClient` against the real `app`, not a
mock): set `settings.API_KEY = "test-secret-123"`, then confirmed
`GET /portfolio` with no key → `401`, wrong key → `401`, correct key →
`200`, and `GET /health` (no auth dependency) → `200` unaffected. This
is direct proof the dependency reads `settings.API_KEY` live per-request
(not a snapshotted default baked in at route-registration time).

## Deployment status

Committed as `8c363b0`, pushed to `origin/main`. Not yet verified live —
next step is the user restarting the Studio and setting `API_KEY` in the
environment, then confirming a real request with the correct
`X-API-Key` header succeeds and an unauthenticated one gets `401`.
**Important**: until `API_KEY` is set on the live deployment, every
protected endpoint fails closed exactly like the old system did with no
`JWT_SECRET_KEY`/`API_KEYS` configured — this is not a new outage, just
the same "no key configured" posture carried forward under a new name.

## Not done in this pass

The plan's docs list also named `docs/Setup/API.md`,
`ADMIN_CONFIG_API.md`, `ADMIN_CONFIG_KEY_CATALOG.md`, `CHANGELOG.md`,
`PHASE_8_OPERATIONAL_HARDENING.md`, `docs/marketing-overview.md`, and
`index.html` — not touched in this pass. These are lower-priority
reference/historical/marketing material rather than active operator
guidance (same category as this session's treatment of `docs/HMM/`,
`docs/TimeFM/`, and other planning docs during the Redis deprecation's
docs cleanup) and were deprioritized in favor of the deploy-relevant
docs (README, `.env.example`, `render.yaml`, `openapi.yaml`) given the
scope of everything else in this pass.
