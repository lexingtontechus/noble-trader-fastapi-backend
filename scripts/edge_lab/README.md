# Edge Lab scripts

Scripts for publishing and registering the offline Edge Lab proof-engine output
in the Kairos repo. These support the `regime_edge` mechanism:

- **`publish_edge_lab.py`** — pushes `edge_lab.json` to Supabase
  (`nt_edge_lab_run`, migration 0031). Called by the orchestrator after each
  daily run or manually:
  ```
  .venv/bin/python scripts/edge_lab/publish_edge_lab.py <path>/edge_lab.json
  ```
  Idempotent (run_id is the PK; re-run upserts). Uses `SUPABASE_SECRET_KEY`
  (formerly `SUPABASE_KEY`).

- **`update_nt_roadmap_edge_lab.py`** — registers the `edge_lab_run` mechanism
  in `nt_roadmap` (the daily heartbeat the orchestrator PATCHes); without this
  row the daily run is invisible in the roadmap. Idempotent (select-then
  insert/patch, never blind-POST):
  ```
  .venv/Scripts/python.exe scripts/edge_lab/update_nt_roadmap_edge_lab.py
  ```

## Exact changes in the current commit set

1. **Supabase key rotation** — every `SUPABASE_KEY` /
   `SUPABASE_SERVICE_ROLE_KEY` reference renamed to `SUPABASE_SECRET_KEY`
   across `regime_platform/`, `scripts/`, and `tests/` (the 2026-10
   service_role → secret rename; see `noble-trader-repo-guide` §5).
2. **Feature-flag migration** — `TIMESFM_DISABLE_FALLBACK`,
   `EOD_CORRECTION_INTRADAY_ENABLED`, `EOD_CORRECTION_INTERVAL_HOURS` moved
   from `settings.py`/env-vars to `config/feature_flags.yaml` getters
   (`get_timesfm_disable_fallback`, `get_eod_correction_intraday_enabled`,
   `get_eod_correction_interval_hours`).
3. **14-symbol ACTIVE expansion (commit `fd2cb64`)** — the proof engine now
   runs against all 14 active symbols (BTCUSD XAUUSD XAGUSD XAUEUR XAUAUD
   USDJPY EURUSD GBPUSD USDCAD AUDUSD AAPL US500 US30USD) instead of the
   5-symbol test subset. `export_edge_lab_data.py` builds the 9 extra CSVs;
   `run_edge_lab.py` runs the daily cadence with `--checkpoint-dir` resuming.
4. **`.env.example`** restructured with an explicit SECRETS-vs-KNOBS split
   (Studio Secrets vs `config/feature_flags.yaml`), audited 2026-10-20.
5. **Live-wiring** — `edge_lab_run_mode` (daily 18:30 UTC) feeds
   `nt_edge_lab_run`; `regime_edge_mode` (per-signal) reads it to gate —
   deliberately separate (see `regime-edge-proof-engine` skill).

## Related

- `scripts/run_edge_lab.py` (repo root) — the daily proof-engine CLI.
- `scripts/export_edge_lab_data.py` (repo root) — TDVA cache refresh for the
  14-symbol set.
- `regime_edge/` package — the audit engine implementation.
- `.hermes/skills/regime-edge-proof-engine/` — the skill documenting the
  engine end-to-end.
- `docs/scopes/20261020_REGIME_EDGE_PROOF_ENGINE_AND_LIVE_FIXES.md` — the
  original scope + the 7 live fixes.
