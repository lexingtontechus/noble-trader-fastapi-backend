# 2026-11-18 — Security scan fixes (scan_d5e17dd9d64a3cf9d2d49860)

Deep security scan (3 independent passes + 4 validator batches + duplicate
adjudication) sealed as **partial**: 21 findings (1 critical, 4 medium, 16 low),
56/1247 files fully reviewed. This change set fixes every actionable finding.
Report: plugin-data `…/scans/scan_d5e17dd9d64a3cf9d2d49860/report.md`.

## Fixes by finding

| # | Sev | Finding | Fix | Files |
|---|---|---|---|---|
| 1 | Critical | Vulnerable dependency aggregate (19 pkgs / 143 advisories) | Raised floors + regenerated `uv.lock` to fixed versions (see below) | `pyproject.toml`, `requirements.txt`, `requirements-0xdweb.txt`, `uv.lock` |
| 4 | Medium | Live-format credentials in `.env-0xdweb.local` (untracked, unignored) | Added `.env*.local` catch-all to `.gitignore` (verified `git check-ignore` matches). **Rotation is a user/provider action — not done here.** | `.gitignore` |
| 9 | Medium | `.dockerignore` glob misses hyphenated env file → baked into image | Added `.env-*.local` + `.env*.local` catch-all | `.dockerignore` |
| 5 | Medium | `llm_server` fail-open auth when key unset | Fail-closed: requires env key OR a random dev key persisted to `.cache/local_llm_api_key` (0600, never logged); `llm_client` sends the key (env or dev file); bind defaults to `127.0.0.1` (`LOCAL_LLM_HOST`) | `llm_server.py`, `regime_platform/services/self_learning/llm_client.py` |
| 13 | Medium | Residual anon/authenticated write+TRUNCATE grants on `nt_*` | Migration 222 revokes INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on every `nt_%` table (SELECT preserved); **applied live + verified** | `supabase/migrations/222_…sql` |
| 2/3 | Low | `trust_remote_code=1` default (llm_server + in-process llm_client) | Default flipped to `0` (fail-safe, explicit opt-in); launchers export `=1` for Spark; warning logged when enabled | `llm_server.py`, `llm_client.py`, `start.sh`, `start-0xdweb.sh` |
| 6 | Low | Unauthenticated `/crt/state|claims|promotion` | Gated with `Depends(require_api_key)`; `/crt/health` stays public; router not mounted if auth import fails | `crt/router.py` |
| 7 | Low | Unauthenticated `/prices` + URL interpolation | Requires API key; ticker/range validated (`400` on injection) and URL-encoded; generic 502 detail | `regime_platform/routers/prices.py` |
| 8/21 | Low | CF worker fail-open auth + caller model in token-bearing URL | Secret REQUIRED (503 when unset, 401 on mismatch); model allowlist (400 otherwise) in both handlers + `_callAi` | `cloudflare/worker.js`, `cloudflare/wrangler.toml` |
| 10 | Low | TimesFM logs generated dev key | Dev key persisted to `.cache/timesfm_dev_key` (0600), only the path logged | `server.py`, `server-0xdweb.py` |
| 11 | Low | `DEPLOY_ENV` defaults to development; render.yaml inert CORS key | Fail-safe default `production`; render.yaml sets `DEPLOY_ENV=production` + `CORS_ALLOWED_ORIGINS` (removed inert duplicate `CORS_ORIGINS`) | `main.py`, `main-0xdweb.py`, `render.yaml`, `.env.example` |
| 12/16 | Low | Log filter on root logger (no-op for propagated records); WS key in URL | Record-factory scrubbing covers every logger/handler incl. `uvicorn.access`; added current credential shapes + `api_key` query pattern; format-then-scrub (placeholder-safe); cheap trigger pre-check for perf | `regime_platform/utils/log_filter.py`, `regime_platform/auth/api_key.py` (docstring) |
| 14 | Low | Non-constant-time API-key `==` | `secrets.compare_digest`, fail-closed when unset | `regime_platform/auth/api_key.py` |
| 17 | Low | `SECURITY DEFINER` refresh anon-executable | Migration 222 revokes EXECUTE from PUBLIC/anon/authenticated; grants postgres+service_role; **applied live + verified** | `supabase/migrations/222_…sql` |
| 18 | Low | Kill-switch gate fails open | TTL-cached (300s) last-known halt state; fail-CLOSED (`403 TRADING_HALTED_UNKNOWN`) when state unavailable and no fresh cache | `regime_platform/middleware/kill_switch_gate.py` |
| 19 | Low | mcp 1.28.1 session exhaustion | Floor `mcp>=1.30.0,<2.0.0` (lock → 1.30.0) | manifests + `uv.lock` |
| 20 | Low | Unbounded `/forecast` batch | `inputs` `max_length=256` | `server.py`, `server-0xdweb.py` |
| 15 | Low | certifi trust-store advisory | lock → certifi 2026.10.23 | `uv.lock` |

Rejected by validation (no change): renko auth-import fallback (unreachable —
top-level import fails closed first); migration-005 "service_role JWT"
(two-segment placeholder, no signature). Retained inconclusive: newer-table
RLS (migration 134 default-privilege revoke contests it; needs live policy
state) — left as a separate candidate, not expanded into this patch.

## Dependency lock results (uv lock)

certifi 2026.10.23 · pyjwt 2.15.1 · cryptography 50.0.2 · filelock 4.2.12 ·
mcp 1.30.0 · msgpack 1.2.4 · soupsieve 2.10/4.1.2 · setuptools 84.2.1 ·
python-multipart 0.0.34 · torch 2.15.0 · transformers 5.23.0 ·
httpx 0.28.1 (capped `<2.0`; httpx2 line resolved 2.16.2, fixed).

**Residual:** `urllib3` stays `1.26.20` (`<2.0`) — alpaca-py requires the 1.x
API (`urllib3_compat.py`). Its decompression advisories are bounded to the
trusted Alpaca data feed; revisit when alpaca-py supports urllib3 2.x.

## Migration 222 — applied and live-verified

Applied to project `pcvscowltlrxzgxjurcr` via the Supabase MCP connector.
Read-back: `nt_*` grants for anon/authenticated are now **SELECT only**
(36/33 tables); `refresh_talaria_dashboard_cache()` ACL = postgres+service_role
only. Commerce tables (users/subscriptions/plans) untouched (out of `nt_%`
scope). Idempotent; backend writes use the service key (unaffected).

## Verification

- New regression suite `tests/test_security_fixes_20261118.py`: **18/18 pass**
  (attack trigger + alternate malicious representation + legitimate behavior
  for each boundary).
- Worker contract smoke `node cloudflare/test_worker.mjs`: **13/13 pass**
  (fail-closed 503, wrong-secret 401, allowlist 400, authed routing intact).
- `git check-ignore .env-0xdweb.local` → matched by `.gitignore:91`.
- ruff: no new errors introduced (new test file clean; changed files at or
  below their pre-existing error counts).
- Full pytest suite: **2385 passed, 22 skipped, 1 failed** (544s). The single
  failure (`test_kairos_pivot.py::TestSelectCandidates::test_policy_ranks_high`)
  is PRE-EXISTING — identical in the baseline run before any edits (baseline:
  2367 passed, 1 failed). The +18 passed are the new regression tests. Zero new
  failures introduced.

## Remaining actions (NOT done by this patch — user/deploy side)

1. **Rotate every un-annotated credential in `.env-0xdweb.local`** (Supabase
   secret+PAT, 8 Discord webhooks, Telegram bot, RapidAPI/NVIDIA/Lightning/TSFM
   keys) and move the file out of the OneDrive-synced tree. Validity was never
   tested; treat as compromised. The inert `JWT_SECRET_KEY`/`AUTH_MODE` have no
   consumer — drop rather than rotate.
2. **Install upgraded deps**: `uv sync` locally / redeploy Studio + Render so
   the fixed lock versions actually run (this patch updated manifests + lock;
   the live venv still has the old versions until synced).
3. **Provision deployment secrets**: `CF_WORKER_SECRET` via `wrangler secret
   put` (worker now fails closed without it); `LOCAL_LLM_API_KEY` + confirm
   Studio 8004 is not publicly mapped (server now loopback-default); set
   `DEPLOY_ENV`/`CORS_ALLOWED_ORIGINS` on the Render dashboard if it overrides
   render.yaml.
4. Pre-existing failure `tests/test_kairos_pivot.py::TestSelectCandidates::
   test_policy_ranks_high` predates this work (unrelated to security).
