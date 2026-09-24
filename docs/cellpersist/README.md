# docs/cellpersist — Kairos reports

Kairos v3 (geometry-free edge-state research) output. Spec:
`docs/scopes/20260924_KAIROS_V3_SCOPE.md` (§11 P1 results, §12 weekly G2 roadmap).

| Path | What |
|---|---|
| `weekly/YYYYMMDD_Wn.{md,json}` | G2 weekly check — `scripts/kairos_weekly_check.py` writes here by default |
| `p1/` | P1 discovery on history: `20260924_p1_discovery_nc{2,1}.txt` (n_confirm 2 primary / 1 sensitivity), summary json, cell×age matrices, html plane, 2026-09-23 smoke run |
| `tests/` | Full-suite pytest logs at P0 (2026-09-23) and G2 labels (2026-09-24) |
| `backfill/` | 2026-09-24 insert-loss backfill: the script, its validation, the stamp-recompute check, and the `returning` rows it wrote |

## Sweep cadence (for reading any gap)

Light sweeps run every **15 min on the hour** (:00/:15/:30/:45). One slot writes 14
rows (one per symbol). A 15-min gap between rows is the normal cadence. The weekly
script measures coverage per **15-min slot** and never from gaps between consecutive rows.

A slot is uncovered only when the orchestrator process was down. On 09-23/24 every
uncovered block matches a Studio `on-start-*.log` (the Studio restarted):
09-23 05:00–06:15 → on-start 06:02; 09-24 05:00–06:15 → 06:01;
09-24 12:30–14:15 → 14:09; the 22:15 slot on both days → 22:17. Restarts that land between
slots (e.g. 10:05, 18:13) cost nothing. The earlier blocks (09-18, 09-21, 09-22) predate
the on-start logs checked, so their cause is unverified.

Other slot gaps that are **not** failures:
- `nt_sweep_result` is missing US30USD/US500/USDCAD/USDCHF at the **17:15** slot every day.
  The daily heavy sweep (`6 17 * * *`) holds those symbols' per-symbol lock, so the light
  sweep logs `locked — skipping (concurrent sweep)`. Their 17:15 `nt_signal_sim` rows come
  from the heavy sweep, which is by design.

## 2026-09-24 insert-loss fix + backfill

**Cause.** `_insert_row` used a 5 s client timeout and never retried. Under light-sweep
write pile-ups Supabase answered with ReadTimeout, 502 or 504. On top of that,
`log_signal_sim` treated *any* failure as a schema lag and retried **without** the
Kairos/G2 columns. For `nt_signal_sim` the first request had usually landed, so the
stripped retry cost nothing. When it hadn't landed, the row was re-inserted without
its labels.

**Fix** (`regime_platform/sweep_orchestrator/supabase_client.py`):
- timeout raised to 15 s;
- connect/pool errors and HTTP 503 are retried once on every table;
- `idempotent=True` tables (`nt_signal_sim`, whose client `signal_id` is the PK) also
  retry ReadTimeout/502/504. A 409 on the retry means the row was already written;
- columns are dropped **only** on HTTP 400 `PGRST204` (unknown column, i.e. Studio
  deployed ahead of a migration);
- `log_regime_entry` also retries 502/503.

Non-idempotent tables such as `nt_eod_actions` and `nt_calibration_records` are not
retried after a timeout, because a duplicate would double-apply.

**Backfill** (`backfill/20260924_backfill.py`, applied 2026-09-24):

| Table | Rows | Source (validated on 3,670 healthy ticks, 100.0% equality per column) |
|---|---|---|
| `nt_signal_sim` | 3 labels (XAGUSD 15:02, EURUSD 18:01, BTCUSD 21:45) | log verdict line. Control: the same derivation matches **710/710** stored labels |
| `nt_sweep_result` | 6 rows (09-23 US30USD 17:01, AUDUSD 19:00, US30USD 19:01; 09-24 AUDUSD 16:00, US500 16:01, BTCUSD 18:45) | same-tick `nt_signal_sim` (decision fields) + `nt_regime_log` (markov/TA/shift) + log `p_timesfm`. `error` = backfill marker |
| `nt_regime_log` | 1 row (AUDUSD 21:45) | same-tick `nt_sweep_result` |

**Not backfilled.** Nothing is estimated; these stay NULL:
- the 5 Kairos factor stamps on the 3 rows. A TDVA re-fetch reproduces only 16/39
  control rows exactly (bar revisions and forming-bar timing), and `spread_bps` was a
  live quote snapshot;
- the backtest KPIs, `kelly_f`, `size_mult`, `prev_regime` and `sweep_duration_ms`
  on the 6 `nt_sweep_result` rows, because no exact same-tick source exists.

Both are excluded from Kairos analysis by construction: the stamp-coverage check
counts them and edge statistics drop NULL factors.

`nt_eod_actions` (09-23 18:01 ReadTimeout) needed nothing: the GBPUSD row landed despite
the client timeout.
