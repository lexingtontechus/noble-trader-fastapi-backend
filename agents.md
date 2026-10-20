# Agent Instructions — noble-trader-fastapi-backend

> **Repo:** `noble-trader-fastapi-backend` · **Role:** Sweep orchestrator / signal producer · **Host:** LightningAI Studio (`noble-trader-unified`)
>
> **Deeper docs:** the Hermes skills `noble-trader-repo-guide` (orientation, runbooks, invariants) and `noble-trader-fastapi-backend` (deep reference library) cover this repo in more detail than this file.

## Version
**v8.2.0.** Anchors that must move together on any bump: `pyproject.toml`, `main.py`, **both** `openapi.yaml` copies (`docs/openapi.yaml`, `docs/Setup/openapi.yaml`), and `tests/test_version_consistency.py` (asserts the exact version).

## Quick Start
```bash
# Dev / testing: always use this exact invocation (plain pytest can hang on heavy backtest files)
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider

# Hermes verify (Windows-safe — the start phase hangs on Windows on proc.stdout.read())
hermes verify --json --skip-start
```

## Architecture at a Glance
- **Five processes, launched by `start.sh`** (TimesFM first, then LLM, main, orchestrator, gateway):

| port | process | role |
|---|---|---|
| 8000 | `proxy.py` | gateway — the only public port |
| 8001 | `main.py` | HMM / Renko / auth |
| 8002 | `server.py` | TimesFM 2.5 forecast API |
| 8003 | `python -m regime_platform.sweep_orchestrator` | APScheduler sweep loop |
| 8004 | `llm_server.py` | local instruct LLM (optional) |

- **Sweep Orchestrator** (`regime_platform/sweep_orchestrator/`) — the live signal generator. Light sweep every **15 min** × **14 active symbols**; heavy sweep **daily**. **Read the real cadence from `nt_symbol.sweep_cron`** — 2026-10-16 all 14 rows said `'6 17 * * *'` (17:06 UTC); migration `215_dst_safe_heavy_sweep_cron.sql` (2026-10-19) moves them to `'6 18 * * *'` (18:06 UTC) so the sweep keeps ≥3 h of runway before the next Lightning Studio restart on both sides of the US DST transition (PST restarts land 18:04 UTC — the old 17:06 slot would have had 58 min). The `'0 20 * * *'` default older docs quote is stale.
- **EV Engine v6** (`ev_engine.py`) — logit-pooled P_win, `P_win = inv_logit(Σ wᵢ · logit(Pᵢ))`. `P_WIN_WEIGHTS_FULL` = `p_regime 5/14`, `p_markov_hold_n 4/14`, `p_timesfm 5/14`. Missing sources are dropped and the remaining weights renormalised (never substituted with 0.5). `p_historical` is an allowed key with no production caller. Public weight API: `set_weights()` / `temp_weights()` / `restore_default_weights()`. **Read the module's own dict before quoting weights.**
- **HMM** — 4-state dual GaussianHMM (volatility × trend, `regime_engine.py`) drives `p_regime` via `trend_probs`, gated by the Tier 1/2 magnitude classifier (`trend_probs_gate_mode: live`). **`p_markov_hold_n` is withdrawn from the blend** (`p_markov_hold_n_mode: "off"`, 2026-10-01) — the separate 5-state HMM (`regime_platform/services/hmm/model.py`) measured **below** the naive base rate (0.525 vs 0.5655) and is being replaced by first-passage (`tiered_atr.walk()`), reinforced by regime-transition freshness × type. It still fits each tick for `nt_sweep_result`'s legacy columns.
- **Qualification gate chain** in `_process_symbol` — **7 live gates**: `signal_qualifiable`, `regime_conf_ok`, `timesfm_available`, `timesfm_confidence_ok`, `ev_allowed`, `p_win_ok`, `content_dedup`; plus `context_aligned_ok` (g9, **shadow only** — never touches `p_win`/`ev_result`). **`kelly_min_ok` was REMOVED 2026-09-27** (changelog `20260927_CHANGELOG_EV_ENGINE_KELLY_GATE.md`): historical-mu Kelly is sizing research, never a gate — it zeroed 61% of directional scans the model liked. Do not add Kelly back as a gate.
- **Live knobs worth knowing** (all in `config/feature_flags.yaml`): `eod_resolver_basis: "intrabar"` (a basis change makes p_win immediately stale — recalibrate, don't just cut over); `price_staleness_max_hours: 1.0` (drops symbols on a frozen feed — this, not market hours, is the correct gate for staleness); `sweep_after_hours: False` (market-hours filter is ON: stocks/indices Mon–Fri 9:30–16:00 America/New_York, DST-aware since 2026-10-19; forex/commodities Sun 22:00–Fri 22:00 UTC, crypto 24/7 — a falling active-symbol count during US-closed hours is expected).
- **2026-10-20 corrective gates, all LIVE (no shadow) — see `docs/scopes/20261020_REGIME_EDGE_PROOF_ENGINE_AND_LIVE_FIXES.md` + `docs/ACTIVE_MODES.md`**: `chop_gate` (`efficiency_24h < 0.30` ⇒ suppress `chop_gate_low_er` — the consolidation gate the HMM's no-neutral vocabulary never had); `direction_stability` (6h cross-direction cooldown + `shifts_24h ≥ 6` flip-rate breaker); `barrier_floor` (first-passage `pi_sl`/`pi_tp`/`pi_e` stamped on every pick; **π_SL > 0.62 ⇒ in-band adjust or reject** — XAUUSD/BTCUSD ATR band centres reject outright ⇒ brick fallback until re-banded); `ev_costs` (per-class round-trip cost × ATR charged once in EV). Heavy sweep qualification is now **fail-closed** on unscored `p_win_bracket` (W-C2) and EV prices the SHIPPED bracket distances (W-C1, `orchestrator._shipped_bracket_distances`). Consequence to watch: XAUUSD/BTCUSD publish from brick geometry (not 4.94/1.00 or 8.07/1.77) until bands are re-banded.

## Signal Pipeline
```
TradingView batch (1h candles) → Renko bricks → HMM regime → TimesFM forecast → EV Engine v6 → gate chain → QUALIFIED
   ──► Discord (direct delivery, discord_delivery.py — Redis/proxy/agent-stream fan-out retired 2026-08-25)
```

## EOD Recalibration Loop
- **Nightly 01:17 UTC**: resolver (12,000 cap) + correction + retention.
- **4h correction passes**: 04:30/08:30/12:30/16:30/20:30 UTC — recomputes bias + `nt_eod_actions`; emergency heavy sweeps fire only for newly-flagged symbols (12h cooldown).
- **Drift monitor** on every light sweep — writes an immediate `bayesian_penalty` when 48h rolling bias > 0.15 (≥20 records, no fresh action).
- **Rolling bias window** — last 7 days, min 20 records.
- **Hourly calibration resolve** at `:38` (`calibration_resolve_hourly_mode`) — TP/SL reach `nt_calibration_records` ~1h after exit instead of waiting for the nightly.

## Liveness
`scripts/pulse_check_r2.py` runs a **liveness gate first** and exits **2** when a fast heartbeat (sweep / signal_sim / pbm_stamp) is stale. This exists because on 2026-10-16 the host died at 03:07 UTC and the sweep sat silent ~21h while every report looked healthy — nothing compared a heartbeat to `now()`. **A row count is not liveness.** Off-box equivalent: `scripts/watchdog_liveness.py` (stdlib only, one-shot, cron-driven, Discord alerting, deduped).

## Key Conventions
- **PYTHONPATH**: `$PWD/src` for local runs (pytest rootdir resolution only — there is no top-level `src/`); `PYTHONPATH=""` for the deployed runtime.
- **Render.com**: builds from `requirements.txt` (NOT `pyproject.toml`). `mcp` is pinned `>=1.12.0,<2.0.0` after the 2026-08-06 `fastapi-mcp 0.4.0` + unpinned `mcp 2.0.0` crash.
- **Migration numbering**: `supabase/migrations/` uses `NNN_snake_name.sql`.
- **Supabase functions deprecated**: debug the active `regime_platform` Python engine; do NOT modify `supabase/functions/` (TypeScript edge functions — "leave as is"). One exception: `supabase/functions/cloudflare-ai-proxy/` houses a **Cloudflare Worker** deployed via `npx wrangler deploy`.
- **Supabase credentials — two tokens, two purposes.** `SUPABASE_KEY`/`SUPABASE_SERVICE_ROLE` (219-char JWT, `eyJhbG…`) is for PostgREST `/rest/v1/`. `SUPABASE_ACCESS_TOKEN` (44-char PAT, `sbp_fc…`) is for the management API `/v1/projects/<ref>/database/query`. Using the PAT on REST returns 401. Derive the project ref from `SUPABASE_URL` — do not add a separate ref variable.
- **Read feature flags from `config/feature_flags.yaml` directly**, not via `getattr` on the module — many keys exist only in the YAML or `_DEFAULTS`, and `getattr` silently returns `NOT_FOUND` for a flag that is live. A knob added to YAML but not to `_DEFAULTS` is silently swallowed.
- **MIN_RR is shared but inert on light qualification.** Source of truth is `config/feature_flags.yaml` → `min_rr: 1.5` via `get_min_rr()` — **not** env (`MIN_RR` in Studio/`.env.local` is stale and ignored). It still flows into EVEngine Step 6 and the heavy-sweep grid pre-filter. On the **light path it is a maintained non-factor**: typical brick geometries already sit at RR≈1.67–2.0+, so `rr >= 1.5` almost never binds. Do **not** raise light `min_rr` to 2.0 expecting tighter quality — that is a geometry choke (default 5/3 bricks = RR 1.67 → fail), not a quality filter. Binding light gates are `regime_conf_ok` and `p_win_ok`.
- **TimesFM probability tuning**: `TIMESFM_SIGMOID_K` (default 20.0), `TIMESFM_CLAMP_MIN/MAX` (default 0.001/0.999). Recommended production: k=10.0, clamp=[0.05, 0.95].
- **`API_KEY`**: single shared-secret gating every protected endpoint (`regime_platform/auth/api_key.py`'s `require_api_key`) — JWT/Clerk/per-user roles retired, no live users. Empty fails closed with 401 on every request. Studio Secret, never committed.
- **`MARKOV_SOURCE_MODE`** (default `legacy_hmm`): which source feeds `p_markov_hold_n`'s 3x3 transition matrix — `legacy_hmm` (the numerically-unstable 5-state `HmmModel`), `trend_hmm_shadow` (compute both, log a comparison, legacy still feeds), `trend_hmm_live` (the `RegimeHMM.trend_transition_matrix_3state()` derivation feeds). See `worklog/20260825_CHANGE_B_MARKOV_SHADOW_MODE.md`.

## Known Pitfalls
1. **TDVA price ordering** (FIXED 2026-08-11): `/api/price/batch` returns `history` DESCENDING (newest first). `prices.reverse()` in `price_fetcher.py` fixes it. The stale-price window (Aug 6–11T14:00Z) contaminated calibration records — purged (14,248 deleted, 2,953 clean retained).
2. **MIN_RR split** (FIXED 2026-08-17): light EV allow and the heavy grid pre-filter must read the **same** value. Today that is `config/feature_flags.yaml` `min_rr: 1.5` via `get_min_rr()` (env `MIN_RR` does nothing). The 2026-08-17 split (hardcoded heavy 2.0 vs env 1.5) and the 2026-09-04 regression (yaml briefly shipped at 2.0) both show why a silent second source is dangerous.
3. **PostgREST URL limits**: filter values with `+` must use `Z`, not `+00:00`; timestamp microseconds (`.`) conflict with PostgREST's `.` delimiter — use `.replace(microsecond=0)`.
4. **PostgREST server-side caps**: `limit=10000` is silently capped at 1000 — use offset-based pagination (`limit=1000&offset=N`).
5. **pg_cron trap**: when the `app.functions_url` setting is unset, `net.http_post` returns 404s. Verified fix: recreate the cron with a hardcoded function URL via `cron.schedule`. `cron.schedule()` works as a non-superuser (42501 on `cron.job` updates).
6. **`nt_signal_sim` rows are not independent samples** — the light sweep re-captures a row every 15 min while a signal stays valid, so one economic signal becomes N identical rows (a weekend window inflated one day 10.5×). Dedupe to episodes on `(symbol, signal, regime, entry_price, take_profit, stop_loss)` keeping the earliest, and exclude `contaminated = true`, before computing ANY statistic.
7. **Diagnostic tables accumulate stale rows across builds.** `nt_bracket_reachability` and `nt_calibration_bias_by_bucket` are rebuilt nightly but old rows are never deleted — a bare `limit=50` returns an arbitrary subset. Always read freshness with `order=computed_at.desc&limit=1`.

## Testing & Verification
```bash
# Full suite — always this exact invocation
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```
- **Code on disk is not code running.** After a deploy, compare the process start time to the changed file's mtime: `ps -eo pid,lstart,cmd | grep sweep_orchestrator` vs `ls -la <file>`. A process started before the files changed is still running the old code.
- **Check the running process's env, not `.env.local`** — Studio Secrets are injected into the process and need not appear in the file: `tr '\0' '\n' < /proc/<pid>/environ | grep -E 'CF_|SUPABASE|API_KEY'`.

## Research Tooling — vol_backtest (Phase 0, OFFLINE)
- **`vol_backtest/` + `scripts/run_vol_backtest.py` + `tests/test_vol_backtest.py`** — the RV-forecast benchmark that gates the TimesFM/Chronos-2 volatility program (`.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md`). Bar = **Log-HAR**; loss = QLIKE; DM with HAC + Holm; per-symbol, never pooled. Build record: `worklog/20261018_VOL_BACKTEST_PHASE0_BUILD.md`.
- **It is NOT wired to the live path.** Nothing in `regime_platform/` imports it; do not let it grow imports back into the orchestrator. Model arms (`timesfm25`, `chronos2`, `chronos2_cov`) are optional and every skip must stay reported-never-silent.
- Runs standalone on numpy/pandas/scipy (tests need no models): `python scripts/run_vol_backtest.py --synthetic --no-models --horizons 24 --cutoffs 40`. Real data: `--data-dir data/vol_backtest` with one `<SYMBOL>.csv` (timestamp,close) per symbol.
- Optional deps (commented in `requirements.txt`): `chronos-forecasting>=2.0` (Chronos-2 arms — Apache-2.0, CPU-fine at 120M), `arch` (GARCH arm).
- **Do not** extract vol from a point-forecast std (measures mean-path slope, not dispersion) — quantile spread only. **Do not** pool raw-scale MAE across symbols (BTC dominates). Annualize per-asset-class, display only.

## Where the worklogs live
- `worklog/kairos/README.md` — **the Kairos index** (scopes/, reports/, p1/, backfill/, assets/, 15-min slot cadence rules).
- `worklog/` — dated changelogs and scope docs, `YYYYMMDD_TOPIC.md`. Browse the directory rather than relying on a curated list here.
- `docs/scopes/` — forward-looking assessments and design theses.
- **Most recent, still-active threads:** first-passage replacing the HMM (`worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md`, result in `worklog/20261001_PHASE2_FIRST_PASSAGE_RESULT.md` — 1/5 bar items passed, kill criteria met); signal-quality plan (`worklog/20261002_SIGNAL_QUALITY_PLAN.md`); legacy-HMM skill scope (`docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md`).

## Severity-Batch Auth
Backend fixes are authorized in severity batches: `critical` → `high` → `m1,m4,…` (by id) → `lows`. Each batch must be independently verified (full pytest suite green + behavioral smoke) before reporting. The user then green-lights the next tier.

## Related
- **Talaria** → `noble-trader-talaria` (client plugin, tools, cron notifiers)
- **Agent** → `noble-trader-hermes-agent-profile` (profile distribution; `NOBLE_STACK_ROOT` points here)
- **Proxy** → `noble-trader-proxy` (retired 2026-08-25 alongside Redis — no replacement needed)
- **Portal** → `nobletradingapp` (Clerk + Helio checkout, claim tokens, Edge Functions)
