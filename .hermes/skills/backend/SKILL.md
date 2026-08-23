---
name: backend
description: >
  Signal production repo — sweep qualification, price precision, EV engine,
  calibration, paper portfolio. Signal PRODUCER; never executes.
category: trading
version: "2026.08.23"
triggers:
  - "sweep qualification"
  - "price precision"
  - "EV engine"
  - "calibration bias"
  - "paper portfolio"
  - "backend signal production"
related_skills:
  - noble-trader-stack
  - noble-trader-operating-workflow
---

# Backend (noble-trader-fastapi-backend)

Signal PRODUCER. LightningAI/Render sweep orchestrator. Qualifies signals
→ XADD to `signal.raw.noble_trader`. Sweep-only (P5 contract) — NEVER
executes. Version 7.7.0.

## Repo identity

- LightningAI/Render sweep orchestrator
- Version 7.7.0 (uniform: `pyproject.toml`, `main.py`, both `openapi.yaml` copies)
- Python + FastAPI + APScheduler + DuckDB + Supabase
- Tests: 315 pass / 23 skip / 0 fail (skips are intentional deprecation markers)

## Operating context

- **Agent does NOT execute.** Backend only qualifies + produces signals. Execution
  is the agent's domain (MT4/MT5 bridge).
- **Sweep path**: TradingView RapidAPI batch (`/api/price/batch`, 10 syms/chunk,
  `SWEEP_CANDLE_HOURS=168`). NOT the MetaApi feed path — `feed_adapters.py` is
  streaming-only, not sweep.
- **7 qualification gates** in `_process_symbol`, all evaluated: `signal_qualifiable`,
  `regime_conf_ok` (MIN_REGIME_CONF=0.60), `kelly_min_ok` (MIN_KELLY=0.02),
  `timesfm_available`, `timesfm_confidence_ok`, `ev_allowed` (MIN_EV=0.0 /
  MIN_RR=2.0), `p_win_ok` (hard floor 0.50, soft band [0.50, MIN_P_WIN)). Post-check:
  `cooldown_ok` (SIGNAL_COOLDOWN_MINUTES=60).
- **EV Engine v5**: 4-source logit-pooled P_win. Weights:
  `p_pattern:0.30, p_regime:0.25, p_markov_hold_n:0.20, p_timesfm:0.25`.
  Missing sources renormalize. Runtime override: `set_weights()` /
  `temp_weights()` / `get_weights()` / `restore_default_weights()`.
- **Price precision — full-value rule**: sweep prices MUST survive the pipeline at
  full precision. 6dp round → `rstrip` → keep ≥2dp for display. Two truncation
  classes were fixed: per-symbol decimals maps lacked XAUUSD/XAUEUR/XAUAUD/XAGUSD
  → truncated gold; payload/save rounding used `decimals` instead of full precision.
  Correct maps: XAUUSD=3, XAUEUR=4, XAUAUD=4, XAGUSD=4. Regression:
  `tests/test_price_full_value.py` (11 pass).
- **Calibration view** `v_eod_calibration_bias` reads POST-enforcement `p_win` —
  bias converges toward 0 partly because enforcement is shrinking p_win, masking
  whether the raw model is still miscalibrated. To audit the model itself: query
  `nt_calibration_records.predicted_p_win_raw` (pre-shrink) vs `predicted_p_win`
  (post-shrink). Bias range thresholds: 0.30 / -0.20. Enforcement shrink formula:
  `p_win' = (p_win*n + 0.5*10)/(n+10)` (Bayesian toward 0.5) + `kelly' = kelly × 0.5`.
- **Paper portfolio — TWO sources, do NOT conflate:**
  - `nt_signal_sim` (`v_talaria_signal_health`) — HYPOTHETICAL signal PnL at unit size
  - `nt_paper_positions` + `v_paper_equity` — ACTUAL realized PnL (written only on close)
  - `$0 equity while open` ≠ losing strategy = nothing closed yet. `v_paper_equity`
    sums realized ONLY (`status IN ('closed','expired')`).
- **Version bump**: update BOTH `openapi.yaml` copies (`docs/openapi.yaml` +
  `docs/Setup/openapi.yaml`) + `tests/test_version_consistency.py` (3 tests assert
  exact version). `patch` tool refuses both openapi.yaml files (pre-existing tab
  char ~line 2994) — use `sed` for those two.

## References

### Per-repo (this skill)

| Topic | Reference |
|---|---|
| Price precision full-value fix | `references/sweep_price_full_value_fix.md` |
| Calibration enforcement wiring | `references/calibration_enforcement_wired.md` |
| Paper book close-pass bug | `references/paper_book_close_pass_bug.md` |
| Signal sim capture hook | `references/nt_signal_sim_capture.md` |
| EOD signal lookback (built) | `references/eod_signal_lookback_implemented.md` |
| Test suite drift + deprecations | `references/backend-test-drift-and-deprecations.md` |
| Version bump (both openapi.yaml copies) | `references/backend_version_bump_consistency.md` |
| Raw vs enforced p_win calibration audit | `references/predicted_p_win_raw_calibration_audit.md` |

### Cross-repo (→ noble-trader-stack)

| Topic | Reference |
|---|---|
| Signal topology (backend rows) | `→ noble-trader-stack:references/topology_and_naming.md` |
| Three Redis URLs | `→ noble-trader-stack:references/sse_and_redis_url_clarification.md` |

## Repo-specific gotchas

- **`search_files` fails on this repo's root** (OneDrive reparse quirk). Use
  `terminal` `grep -rln` from repo root instead.
- **PYTHONPATH inherited from shell** → broken global `hermes-agent/venv` (missing
  `pydantic_core`). Always: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe`.
- **`hermes verify` spawns cmd.exe on Windows** — bash syntax in `.hermes/environment.json`
  fails. Use `set` syntax. Full recipe: `→ noble-trader-workspace-maintenance:references/hermes_verify_agent_start_null.md`.
- **`uv sync` strips the dev extra** — run `uv sync --extra dev` to restore pytest.
- **`@dataclass(slots=True)` + `importlib`**: register in `sys.modules` before
  `exec_module` or use the project's real `pytest`.
- **SQLite `UPDATE ... RETURNING` not supported** (older SQLite). Paper book uses
  separate `SELECT` after `UPDATE` + explicit retry loops for `SELECT ... FOR UPDATE`
  (SERIALIZABLE).
