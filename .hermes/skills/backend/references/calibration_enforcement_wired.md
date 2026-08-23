# Calibration enforcement loop — WIRED (2026-08-10)

The EOD self-correction was AUDIT-ONLY: `run_eod_correction` wrote
`nt_eod_actions` recommendations (bayesian_penalty / kelly_reduction /
emergency_heavy_sweep) but NOTHING consumed them; `ev.calibration_enabled`
in runtime_config had ZERO consumers; the heavy sweep only READ bias to
skip 12h when CALIBRATED. Wired per user "build code - opt a wire the
recalibration process":

1. **NEW module**
   `regime_platform/sweep_orchestrator/calibration_enforcement.py`
   — `apply_calibration_adjustment(symbol, p_win, kelly_f)` reads the LATEST
   `nt_eod_actions` row (penalty actions only, fresh ≤8h window — EOD_ACTION_FRESH_HOURS) and returns
   `p_win' = (p_win*n + 0.5*10)/(n+10)` (Bayesian shrink toward 0.5) +
   `kelly' = kelly × 0.5`. No-op when enforcement disabled / no action /
   audit-only action / stale action.
2. **Light sweep** `orchestrator._process_symbol` §5b — applies the
   correction BEFORE the qualification gates (mutates `ev_result.p_win` +
   `effective_kelly`), stamps `calibration_enforced/status/bias` into
   `passed_gates` (→ nt_sweep_result via the result dict).
3. **Heavy sweep** `heavy_sweep._push_heavy_signal` — same adjustment + stamp.
4. **`_run_eod_lookback` Part III** — the missing consumer: actually RUNS
   `emergency_heavy_sweep` recommendations via `run_heavy_sweep_for_symbol`
   (re-optimising params on fresh data is the only true bias-recovery path).
5. **`eod_correction.py`** — `detail["n"] = n_records` now carried for the
   live shrink (previously the detail had adjusted_p_win but no n).
6. **Env toggle** `EOD_CALIBRATION_ENFORCE` (default `1`; `0` = audit-only
   again). Constants MUST mirror eod_correction (BAYES_PRIOR_COUNT=10,
   BAYES_PRIOR_PWIN=0.5, KELLY_REDUCTION=0.5) — a test asserts this.

Tests: `tests/test_calibration_enforcement.py` (12). Full backend suite
401 passed / 23 skipped (2026-08-13; was 332 before the 08-13 builds).

**CRITICAL ROOT CAUSE (2026-08-10):** `nt_calibration_records` was EMPTY
because every EOD resolver insert failed HTTP 400 / PG 23502 (NOT NULL
violation) — payload omitted `id` (TEXT PK, no DB default) + `aggression`
(NOT NULL CHECK enum). 713 failures in orchestrator.log. Consequence:
`fetch_calibration_bias` returned None (<20 records) → EOD correction wrote
0 actions → `nt_eod_actions` stayed EMPTY (looks like the loop never ran,
but it ran every 00:30 with 0 actions). The `v_eod_calibration_bias` VIEW
shows OVERCONFIDENT because it reads `nt_signal_sim` (populated), while the
correction reads `nt_calibration_records` (empty) — the two paths disagree.
FIXED in commit 30a8c78: `id=uuid4()` + `aggression` from signal (default
'mid', validated against the CHECK enum). Regression tests:
`test_eod_resolver.py::TestCalibrationRecordPayload` (3).

**DEPLOYED-CONFIRMED 2026-08-12 (verified live):** the fix only reached the
LightningAI runtime after a restart — the Aug 11 00:30 run STILL logged
181× 23502 failures, and only the Aug 12 00:30 run wrote 182 calibration
records successfully (`nt_calibration_records insert OK`). Deployed runtime
can lag git HEAD by a day+; verify from ITS orchestrator.log, not git.
After the fix, `nt_eod_actions` can STILL stay empty while healthy: each
symbol+venue needs >= 20 records before `fetch_calibration_bias` returns a
bias — counts were 18–19/symbol → 0 actions every night by design. The
threshold lives at `supabase_client.py:456` (`len(data) < 20 → return None`);
with 18–19 records per symbol, every symbol is just under the bar, so
`run_eod_correction` sees bias=None for all symbols → 0 actions. Full
verification workflow: see `eod_recalibration_verification.md`.

**STATUS 2026-08-13 — threshold CROSSED, loop fully live:** records 182 →
3,849 (all symbols ≥200, past the 20-bar), `nt_eod_actions` now has 27 rows
(9 OVERCONFIDENT × 3 actions, 08-13 00:45 pass), and enforcement stamps
`calibration ENFORCED (OVERCONFIDENT, bias=…)` appear live in 5-min sweeps.
**CAVEAT:** the records table shows statistically impossible outcomes
(USDCHF/USDJPY 100% win 382/382; BTCUSD/USDCAD ~95% loss) — derived biases
(+0.93 for BTCUSD/USDCAD) are suspect until the resolver artifact is
confirmed; spot-check before trusting penalties. **ROOT-CAUSED 2026-08-13:**
all 3,849 records come from the Aug 5–8 pre-fix stale-price window (sweep
`prices[-1]` = oldest of DESCENDING TDVA history; deployed runtime lagged
git) — the ENTIRE table is contaminated, the biases are garbage-in/
garbage-out, and USDCHF/USDJPY "CALIBRATED" is a false negative. Full
evidence chain, reproduction recipe, purge/guard recommendations + backlog
equilibrium: `eod_stale_price_artifact.md` (this file was previously named
calibration_stale_price_contamination.md — the actual reference file is
eod_stale_price_artifact.md). Query gotchas:
`supabase_rest_query_patterns.md`.

**DB-HEALTH BUILD (2026-08-12, changelog [9.3.0]):** the nt_signal_sim
backlog + stale-resolution problem was fixed in 4 actions:
1. **Window anchoring** — `eod_resolver.anchor_post_entry_closes()` slices
   the fetched closes to the POST-ENTRY window per signal (the fetched array
   is ascending after the TDVA order fix; `entry_idx = min(len - offset,
   len-1)`). Without it, a backlogged signal (entry days old) was scored
   against the FIRST bars of the 200-bar window, which predate the entry.
   `_fetch_pending` now bounds to `ts >= now() - LIGHT_CANDLE_BARS hours`.
2. **Throughput** — `EOD_RESOLVE_LIMIT` (default 1000, was hardcoded 200);
   `scripts/drain_signal_backlog.py` for the one-time 16k drain.
3. **Retention** — `eod_retention.run_eod_retention()` (wired into
   `_run_eod_lookback` Part IV): DELETE resolved rows older than
   `EOD_RETENTION_RESOLVED_DAYS` (30), PATCH unresolved rows older than
   `EOD_RETENTION_STALE_DAYS` (7) to `outcome='expired'` (no calibration
   record — a timeout is neither win nor loss).
4. **Capture trim (opt-in)** — `NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY`
   (default False) persists only QUALIFIED signals to nt_signal_sim.
Diagnostic: `grep "nt_calibration_records insert HTTP" orchestrator.log` →
23502 = missing NOT NULL col in the payload; `Content-Range: */0` on the
table via anon = RLS-blocked (service role reads fine), NOT necessarily
empty — check via the resolver log, not the anon probe.

**NOTE:** bias itself still only moves passively (new resolved trades via
EOD resolver + fresh params from emergency sweeps) — enforcement corrects
the SIGNAL's p_win/Kelly live so overconfidence stops inflating published
signals, which over time re-converges calibration toward 0.

## Cadence: computation nightly, enforcement every 5 min (verified 2026-08-13)
When asked "is recalibration supposed to run every 5 min?" the answer splits:
- **COMPUTATION runs nightly at 00:30 UTC AND every 4h intraday** (04:30/08:30/
  12:30/16:30/20:30 UTC, `eod-correction-intraday-*` jobs, 2026-08-13 build): the
  nightly adds resolver + retention; the 4h passes are correction-only. Writers of
  `nt_eod_actions`: the correction passes AND the drift monitor (light sweep §5c).
  Detail: references/intraday_recalibration_cadence.md.
- **ENFORCEMENT runs on EVERY 5-min light sweep**: `apply_calibration_adjustment()`
  (orchestrator §5b) re-reads the LATEST `nt_eod_actions` row per symbol; if it is
  a penalty action (`bayesian_penalty`/`kelly_reduction`/`emergency_heavy_sweep`)
  and ≤8h old (`EOD_ACTION_FRESH_HOURS`; fallback `ACTION_FRESH_WINDOW_HOURS=8`), it
  applies p_win→Bayesian shrink + Kelly×0.5 and stamps
  `[sweep:orch] <sym>: calibration ENFORCED (OVERCONFIDENT, bias=...)`.
- Evidence: 1,289 ENFORCED stamps on 2026-08-13 (~143/symbol over ~17.5h ≈ every
  sweep). It does NOT recompute bias per sweep — it applies the latest
  recommendation (nightly or 4h intraday). Computation: nightly + every 4h.
  Application: every sweep.

## DEFECT — Double-encoded JSON in eod_action.detail (H2, FIXED)

**`eod_correction.py:99` (pre-fix):** wrote `detail=json.dumps(detail)` into a JSONB column
(`nt_eod_actions.detail`). The extra encoding layer meant:

- `calibration_enforcement.py:214` checked `isinstance(detail, dict)` → **False**
  (the detail was a string, not a dict)
- The enforcement fell through to `n_records=0` → skipped the Bayesian shrink
  calculation entirely
- It fell back to **raw p_win** (no correction applied), even though the
  orchestrator logged "applied"
- Kelly×0.5 reduction WAS still applied (that path doesn't depend on detail
  being a dict), so published sizing used halved kelly but UNadjusted p_win

**Symptom (live 2026-08-13):** 1,289 `calibration ENFORCED` stamps showed
overconfident symbols still publishing inflated p_win — the p_win shrink was
silently defeated while Kelly halving proceeded.

**Fix (DEPLOYED 2026-08-13, committed):** Removed the `json.dumps()` wrapper —
`eod_correction.py:103` now sends `detail` as a raw dict (httpx serialization
handles JSONB correctly). `calibration_enforcement.py:224-234` added a defensive
string-parse fallback (`json.loads()`) for any legacy double-encoded rows. The
Bayesian shrink now correctly reads `n_records` from `detail["n"]`.

**Verification:** Enforcement stamps post-fix show p_win shrink in the log lines.
See `worklog/20260813_CHANGELOG.md` for full build evidence (401 passed / 23 skipped).

## CRON WATCHDOG VERIFICATION (2026-08-15, verified live)

The `noble-calib-health` cron job (daily 02:10 UTC) was validated as a correct
watchdog for this loop. It checks three failure signatures:

1. Resolver resolving signals but `nt_calibration_records` EMPTY (insert failures)
2. Calibration rows exist but no `nt_eod_actions` in 7 days (when symbols have
   ≥20 records AND bias ≥0.05)
3. Fresh penalty present but sweep still produces UNREVISED p_win (requires
   orchestrator.log grep — best-effort, offline)

**Key design insight:** Empty `nt_eod_actions` is EXPECTED at low calibration
volume (<20 records/symbol). The watchdog correctly only alerts when BOTH
calibration rows exist AND no action was written AND a symbol crossed the
bias band. This prevents false alarms during cold starts.

**Verification method:** The watchdog queries live Supabase via REST API
(anon key from `.env`), not local logs. It uses `select=column&limit=5` with
checks recency of the latest rows. This is the correct
pattern for cron-based health checks — direct DB probing, not log parsing.

---

## RAW-BIAS CYCLE RESET (2026-08-22)

### Motivation
The nightly 00:30 EOD pass and 5-min light sweeps both read `predicted_p_win`
(the **enforced** value — already Bayesian-shrunk). Repeated shrinking
compounds: each pass shrinks the already-shrunk p_win, pushing borderline
symbols further overconfident and creating false-positive overconfidence
flags. US500 bias stayed at 0.7565 for 6+ hours straight (Aug 21 20:30 →
Aug 22 03:30) despite daily passes running — the compounded shrink masked
the true model drift.

### The fix: raw-bias intraday passes
A new code path (`run_eod_correction(raw_bias_mode=True)`, triggered by
`_run_eod_correction_only` in `orchestrator.py`, scheduled at
**04:30/08:30/12:30/16:30/20:30 UTC**) reads `predicted_p_win_raw`
(the **pre-shrink** model output) instead. The classification thresholds
are wider to accommodate raw values:

| Mode | Source column | Overconfident threshold | Underconfident threshold | Calibrated band |
|------|--------------|------------------------|--------------------------|-----------------|
| Standard (`raw_bias_mode=False`) | `predicted_p_win` | > 0.10 | < -0.10 | -0.10 ≤ bias ≤ 0.10 |
| Raw (`raw_bias_mode=True`) | `predicted_p_win_raw` | ≥ 0.30 | ≤ -0.20 | -0.20 < bias < 0.30 |

**Important**: There is NO 0.7 threshold anywhere in the code. The
"calibrated band" for raw mode is **-0.20 < bias < 0.30**, not 0.7. Values
above 0.30 are OVERCONFIDENT.

### Three-tier architecture
1. **4h raw-bias pass** (`raw_bias_mode=True`): reads true model output,
   classifies, writes `nt_eod_actions` rows, **clears stale actions** for
   CALIBRATED symbols. Thresholds: 0.30 / -0.20.
2. **Drift monitor** (every 5-min light sweep): catches intraday drift
   BETWEEN 4h passes using `fetch_calibration_bias` (enforced path) with
   threshold **0.15**. Writes immediate `bayesian_penalty` when no fresh
   penalty exists.
3. **Light sweep enforcement**: reads the latest `nt_eod_actions.status`
   (pre-classified by whichever pass wrote it) and applies Bayesian shrink
   + Kelly×0.5. Does NOT recompute bias.

### Consequences
- **No compounding**: raw-bias pass shrinks from true model output, not from
  already-shrunk values.
- **Stale action clearing**: symbols CALIBRATED in raw mode (bias < 0.30)
  have their old penalty actions deleted, preventing the nightly pass from
  re-flagging them based on compounded enforced values.
- **Drift monitor re-flags**: after `clear_stale_eod_actions` deletes the old
  penalty, the drift monitor on the next light sweep re-checks using the
  **enforced** path. If enforced bias > 0.15, it writes a new penalty. This
  is the expected catch-up behavior between 4h passes.

### Verification (live, 2026-08-22 04:30 UTC pass)
- 14 symbols checked, **6 actions, 0 errors**
- Only 2 emergency heavy sweeps: AAPL (raw bias=0.4447), US500 (raw bias=0.6771)
- 6 symbols had stale actions cleared: XAGUSD, XAUEUR, XAUUSD, BTCUSD, USDCHF, USDJPY
- `predicted_p_win_raw` API calls confirmed: all fetches selected both
  `predicted_p_win` AND `predicted_p_win_raw` columns
- Post-restart light sweeps read the raw-bias-written actions correctly

### Config
```bash
# Raw-bias thresholds (4h intraday correction)
EOD_BIAS_RAW_OVERCONFIDENT=0.30
EOD_BIAS_RAW_UNDERCONFIDENT=-0.20

# Drift monitor threshold (5-min light sweeps)
EOD_DRIFT_THRESHOLD=0.15
```

