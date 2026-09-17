# Agent Instructions — noble-trader-fastapi-backend

> **Repo:** `noble-trader-fastapi-backend` · **Role:** Sweep orchestrator / signal producer · **Host:** LightningAI Studio (`noble-trader-unified`)

## Version
**v7.9.0** (as of 2026-09-16). Version anchors across `pyproject.toml`, `main.py`, and **both** `openapi.yaml` copies must be updated together on any bump, along with `tests/test_version_consistency.py` (asserts exact version).

## Quick Start
```bash
# Dev / testing: ALWAYS unset PYTHONPATH to avoid shadowing the project venv
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```

## Architecture at a Glance
- **3-layer stack** (Launched via `start.sh`): `main.py` (port 8001 — HMM/Renko/auth), `server.py` (port 8002 — TimesFM), `proxy.py` (port 8000 — gateway).
- **Sweep Orchestrator** (`regime_platform/sweep_orchestrator/`) — the live signal generator. Light sweep every **15 min** × 14 symbols (default `*/15`; was `*/5` until 2026-09-09); heavy sweep Sunday 20:00 UTC.
- **EV Engine v5** — 4-source P_win blend: `p_pattern 0.30 / p_regime 0.25 / p_markov_hold_n 0.20 / p_timesfm 0.25`. Public weight API: `set_weights()` / `temp_weights()` / `restore_default_weights()`.
- **HMM** — 4-state dual GaussianHMM (volatility × trend, `regime_engine.py`) drives `p_regime`. `p_markov_hold_n` currently comes from a separate 5-state multivariate Gaussian HMM (`regime_platform/services/hmm/model.py`, 2026-08-13, v7.7.0) — confirmed numerically unstable (9 unscaled features) and being retired via `MARKOV_SOURCE_MODE` (Part 1 Change B, 2026-08-25; see Key Conventions below).
- **7-gate qualification chain** in `_process_symbol`: signal_qualifiable, regime_conf_ok, kelly_min_ok, timesfm_available, timesfm_confidence_ok, ev_allowed, p_win_ok.

## Signal Pipeline
```
TradingView batch (1h candles, 168 bars) → Renko bricks → HMM regime → TimesFM forecast → EV Engine v5 → 7 gates → QUALIFIED
   ──► Discord (direct delivery, discord_delivery.py — Redis/proxy/agent-stream fan-out retired 2026-08-25)
```

## EOD Recalibration Loop (v7.9.0)
- **Nightly 00:30 UTC**: resolver (12,000 cap) + correction + retention.
- **4h correction passes**: 04:30/08:30/12:30/16:30/20:30 UTC — recomputes bias + `nt_eod_actions`; emergency heavy sweeps fire only for newly-flagged symbols (12h cooldown).
- **Drift monitor** on every light sweep — writes immediate `bayesian_penalty` when 48h rolling bias > 0.15 (≥20 records, no fresh action).
- **Rolling bias window** — last 7 days, min 20 records.

## Worklog / Changelog
| File | Scope |
|---|---|
| `worklog/20260811_CHANGELOG.md` | TDVA price ordering fix (`prices.reverse()`) |
| `worklog/20260812_CHANGELOG.md` | Resolver window anchoring, throughput, retention, capture trim |
|| `worklog/20260813_CHANGELOG.md` | Full EOD recalibration build (4h passes + drift monitor + purge + `predicted_p_win_raw`) |
|| `worklog/20260815_CHANGELOG.md` | M4 open-position orphan fix (close pass rewrites, retroclose, monitoring) |
|| `worklog/20260817_RECALIBRATION_PERF_ASSESSMENT.md` | Recalibration perf deep assessment + MIN_RR split fix + TimesFM sigmoid/k/clamp env vars |
|| `worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md` | Regime label stability investigation |
|| `worklog/20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md` | Scheduler misses and log sanitization |
|| `worklog/20260902_MACRO_REGIME_COMPUTED_AT_BUG.md` | Macro regime computed-at bug |
|| `worklog/20260904_KELLY_DIRECTION_AWARE_SIZING.md` | Kelly sizing direction-blind fix (direction-aware returns negation) |
|| `worklog/20260904_MIN_RR_REGRESSION.md` | MIN_RR/MAX_SHARPE/MIN_TRADES silently tightened 08-30, fixed |
|| `worklog/20260904_SIGNAL_ENGINE_MATH_TRIAGE.md` | Full financial logic triage — Masaniello exponent inverted, fractional kelly cap bug, kelly_edge unbounded, orchestrator _mu direction-blind |
|| `docs/mse-implementation-plan.md` | MSE Gate 8 integration plan (1h timeframe recommendation) |
|| `docs/SIGNAL_GEN_FLOW_COMPARISON.md` | Signal generation flow documentation |
|| `docs/marketing-overview.md` | Marketing copy reference |

## Testing & Verification
```bash
# Full suite (always use this exact invocation — plain pytest can hang on heavy backtest files)
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider

# Hermes verify (Windows-safe — start phase hangs on Windows due to proc.stdout.read() blocking)
hermes verify --json --skip-start   # test phase OK: true; runtime boot proven via curl
```

## Key Conventions
|- **PYTHONPATH**: Must be set to `$PWD/src` for local runs; `PYTHONPATH=""` for deployed runtime.
|- **Render.com**: Builds from `requirements.txt` (NOT `pyproject.toml`). The 2026-08-06 crash (`fastapi-mcp 0.4.0` + unpinned `mcp 2.0.0`) is fixed — `requirements.txt` now pins `mcp>=1.12.0,<2.0.0`.
|- **Migration numbering**: `supabase/migrations/` uses `NNN_snake_name.sql` convention (001–036, 100–114).
|- **Supabase functions deprecated**: Debug the active `regime_platform` Python engine; do NOT modify `supabase/functions/` (TypeScript edge functions — "leave as is").
|- **MIN_RR is shared**: `MIN_RR` (env, default 2.0) controls BOTH the orchestrator's Gate 6b qualification threshold AND the heavy sweep grid-search pre-filter in `heavy_sweep.py:737` + `pick_best_combo()`. Set once, applies everywhere.
|- **TimesFM probability tuning**: `TIMESFM_SIGMOID_K` (default 20.0), `TIMESFM_CLAMP_MIN/MAX` (default 0.001/0.999) control how aggressively the TimesFM price forecast maps to p_timesfm. Recommended production: k=10.0, clamp=[0.05, 0.95].
|- **`API_KEY`** (2026-08-25): single shared-secret gatekeeping for every protected endpoint (`regime_platform/auth/api_key.py`'s `require_api_key`) — JWT/Clerk/per-user roles retired, no live users. Empty means every request fails closed with 401. Set as a Studio Secret, never committed.
|- **`MARKOV_SOURCE_MODE`** (default `legacy_hmm`, Part 1 Change B, 2026-08-25): which source feeds `p_markov_hold_n`'s 3x3 transition matrix — `legacy_hmm` (unchanged, the numerically-unstable 5-state `HmmModel`), `trend_hmm_shadow` (compute both, log a comparison, legacy still feeds the blend — zero behavior change), `trend_hmm_live` (the new `RegimeHMM.trend_transition_matrix_3state()` derivation feeds the blend). See `worklog/20260825_CHANGE_B_MARKOV_SHADOW_MODE.md`.

## Known Pitfalls
1. **TDVA price ordering** (FIXED 2026-08-11): `/api/price/batch` returns `history` DESCENDING (newest first). `prices.reverse()` in `price_fetcher.py` fixes this. The stale-price window (Aug 6–11T14:00Z) contaminated calibration records — purged (14,248 deleted, 2,953 clean retained).
2. **MIN_RR split** (FIXED 2026-08-17): `MIN_RR` was hardcoded at `2.0` in `heavy_sweep.py:72` while the orchestrator read `MIN_RR` from env (user sets 1.5). The heavy sweep grid-search pre-filter (`heavy_sweep.py:737`) and `pick_best_combo()` (`heavy_sweep.py:199`) used 2.0, causing emergency sweeps to fail with `no_combo_passed_filters` for 3/9 overconfident symbols. Fixed: both modules now read the same `MIN_RR` env var.
3. **PostgREST URL limits**: Filter values with `+` must use `Z` not `+00:00`; timestamp microseconds (`.`) conflict with PostgREST's `.` delimiter — use `.replace(microsecond=0)`.
4. **PostgREST server-side caps**: `limit=10000` is silently capped at 1000 — use offset-based pagination (`limit=1000&offset=N` loop).
5. **Non-superuser cron**: `cron.schedule()` works (42501 on `cron.job` updates); `ALTER TYPE ADD VALUE` works; `app.functions_url` unset causes `net.http_post` 404s — recreate cron with hardcoded function URL.
6. **pg_cron trap**: When `app.functions_url` setting is unset → `net.http_post` returns 404s. Verified fix: recreate cron with hardcoded function URL via `cron.schedule`.

## Severity-Batch Auth
Backend fixes are authorized in severity batches: `critical` → `high` → `m1,m4,…` (by id) → `lows`. Each batch must be independently verified (full pytest suite green + behavioral smoke) before reporting. The user then green-lights the next tier.

## Related
- **Talaria** → `noble-trader-talaria` (client plugin, tools, cron notifiers)
- **Agent** → `noble-trader-hermes-agent-profile` (profile distribution; `NOBLE_STACK_ROOT` points here)
- **Proxy** → `noble-trader-proxy` (retired 2026-08-25 alongside Redis — no replacement needed)
- **Portal** → `nobletradingapp` (Clerk + Helio checkout, claim tokens, Edge Functions)
