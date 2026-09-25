# Kairos: restart-safe regime state + accelerated G2 (2026-09-25)

Branch: `kairos`. Scope parent: `worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md` §12.
Roadmap rows: `nt_roadmap` → `kairos_g2`, `kairos_daily_job` (heartbeat), `regime_state_persist`.

## 1. Why

- **Faster schedule.** The user asked to stop waiting weeks: "We have enough signals generated to track across 48hrs."
  - §12.2's gates were calendar-based (W1–W6, decided on 2026-11-05). The data was not the constraint.
  - Two mechanical limits were holding it back:
    - Drift was filled once a night, and only on **resolved** rows aged 49–199h. So no episode counted as matured until 84h.
    - About 60% of episodes had tier `unknown`, because they were captured before migration 168.
- **Restarts must not disturb sweeps.** User: "studio restarts are either code deployment updates or scheduled restarts, none should mess with sweeps."
  - **Measured over 2026-09-22..25:** 22 restarts, comparing consecutive light-sweep ticks per symbol.

    | Pair type | n | Regime cell changed |
    |---|---|---|
    | Across a restart | 308 | **58.4%** |
    | Normal 15-min tick | 2,912 | 9.0% |
    | Other gaps (weekend) | 32 | 6.2% |

  - The restarts were flipping the regime labels; the market was not.
  - **Causes.** All of this state was held in memory and lost on restart:
    - `_regime_label_means`: without it, the first fit after a restart re-sorts the HMM components blind, so label identity flips.
    - Every `RegimeLabelDebouncer` cold-started and accepted the first raw label.
    - `KairosRunTracker`: every run restarted at age 0 and was left-censored.
    - The Markov and trend-posterior EMAs restarted from the raw value.
  - These labels feed direction, `regime_conf` and the regime age. So every ~4h restart was injecting fake regime changes into the live signal stream, not just into Kairos statistics.

## 2. Changes

### A. Restart-safe state (`regime_state_persist_mode: live`)

| Piece | Where |
|---|---|
| Snapshot/restore (atomic JSON; NaN-safe; version-checked) | `regime_platform/sweep_orchestrator/regime_state_store.py` |
| `to_dict` / `from_dict` | `RegimeLabelDebouncer` (`core/regime_engine.py`), `KairosRunTracker` (`kairos.py`) |
| Save after **every symbol** in a light-sweep group, and on `stop()` | `orchestrator.py` `_persist_regime_state` |
| Restore first thing in `start()` | `orchestrator.py` `_restore_regime_state` |
| Flags | `regime_state_persist_mode` (off \| live) and `regime_state_persist_max_age_hours` (3.0), in `config/feature_flags.yaml` and `_DEFAULTS` |

- **File location.** `.cache/kairos/regime_state.json` sits on the Studio's persistent disk and is gitignored, so a deploy or pull never touches it.
- **Freshness rule.** A snapshot older than 3h is ignored. That is the same >3h rule `v_kairos_regime_runs` uses for a data gap, so a real outage or a weekend still cold-starts.
- **Why it ships live rather than shadow.** It restores the state the process had before the restart. It changes no gate, threshold, sizing or delivery path; the next tick behaves as if nothing had stopped. `off` is the kill switch.
- **Why the save happens per symbol.** Restarts land mid-group (on-stop at xx:09–xx:13). Saving after each symbol means a kill loses at most one tick for one symbol.
- **Not persisted, and why that's safe:**
  - `_regime_warm_start_params`: the mode is off.
  - The cooldown: it already survives restarts through `_probe_cooldown_restart`, which reads Supabase.
  - The HMM feature buffer: it re-bootstraps from 200 bars.

### B. Item 4: the `kairos-daily` must-pass is now checkable

- **Hourly job.** `kairos-daily` now runs hourly at `:20`, instead of daily at 03:15.
  - `:20` is clear of the 15-min light-sweep grid and of the :17 EOD resolve.
  - The calendar refreshes inside the job only when the cache is more than 20h old.
- **Heartbeat.** Each run PATCHes `nt_roadmap(kairos_daily_job).current_state` to `last_run <ts> · {summary}` (`kairos.write_job_heartbeat`). No new table was added.
- **Weekly check.** Section A fails if the heartbeat is more than 2h old.
- **Drift backfill scope widened.**
  - It now covers **all** light-sweep buy/sell rows aged 2–199h, pending or resolved (it was resolved-only, 49–199h).
  - It still writes NULL fields only and never overwrites.
  - `calibration_bias_bucket` (the only other drift reader) filters `outcome IS NOT NULL`, so it is unaffected.

### C. Item 5: week label and the accelerated cadence (`scripts/kairos_weekly_check.py`)

- **Week label.** `schedule_week()` snaps to the pre-registered Thu 08:00 boundaries, with W0 = 2026-09-24. The 09-24 23:22 run had been labelled W1; it was actually W0. That report is renamed `reports/20260924_W0_partial.md`.
- **Maturity.** Maturity is now per horizon: mₕ is read once bar h has closed, plus a 2h backfill lag. This replaces the blanket 84h rule, so m₄ matures in 7h and m₂₄ in 27h.
- **Tier reconstruction.** `unknown` tiers (rows before migration 168) are reconstructed from stored columns: `regime_conf ≥ 0.60`, `effective_kelly ≥ 0.02`, `ev > 0 ∧ rr ≥ 1.5`, `p_win_dir ≥ 0.50`.
  - **Validation (2026-09-25):** checked against the 910 rows that carry a real `gate_outcome` stamp.

    | Gate | Agreement |
    |---|---|
    | regime_conf_ok | 1.000 |
    | kelly_min_ok | 1.000 |
    | ev_allowed | 0.999 |
    | p_win_ok | 0.978 |
    | **Tier** | **1.000** |

  - No threshold changed in git between 09-22 and 09-24.
  - Reports count reconstructed episodes separately (`tier_src`).
- **`--accel` mode.** It runs on a rolling `--window-h 48` window ending at the current hour, labelled `A<mmddHH>`.
- **§H decision readiness.** This replaces the W5 calendar power check with the same criterion, evaluated at every look:
  - G2.1–G2.3 become decidable once the 95% CI half-width of the gates-vs-rest Δm₂₄ is ≤ 0.2 × pooled SD.
  - G2.4/G2.5 need ≥ 200 uncensored episodes aged ≥ 12h.
  - The criterion is decided on CI width only, never on the size or sign of the effect.
- **Reports location.** Reports now go to `worklog/kairos/reports/`.

### D. Item 6: roadmap

Three `nt_roadmap` rows were upserted: `kairos_g2`, `kairos_daily_job` and `regime_state_persist`. Each has `review_at` 2026-09-26 08:00 UTC and points to this file.

### E. Hygiene

- **Kairos docs moved** under `worklog/kairos/` using `git mv`, so history is kept:
  - `docs/cellpersist/*` → `worklog/kairos/{README.md, backfill, p1, tests, reports}`
  - `docs/scopes/2026092[34]_{KAIROS_*, REGIME_CELL_*}` → `worklog/kairos/scopes/`
  - `docs/scopes/assets/20260924_{edge_plane*, kairos_*}` → `worklog/kairos/assets/`
  - `worklog/20260924_EDGE_STATE_MATRIX_REGIME_AGE.md` → `worklog/kairos/`
- **References repointed** in code docstrings, migrations 167/168 (comments only), scripts, tests and `AGENTS.md`.
- **Branch rule:** Kairos commits land on `kairos` only.
  - `main` is currently at the same commit (0f6afe7): the earlier Kairos commits were fast-forwarded into it.
  - From here on, `main` receives Kairos work only by an explicit merge the user asks for.
  - Studio runs `master`, so deploying Section A needs that merge.

## 3. What changes statistically (pre-registration note)

- **Unchanged:** the test family, statistics, nulls, Holm correction and pass rules (§12.3).
- **Changed:**
  - When a look can be decided: on power, not on the calendar.
  - Maturity is now per horizon.
  - Pre-168 tiers are reconstructed.
- **Interim rule:** it still flags only at p < 0.001, with no knob change before the decision.
- **The restart-state fix is a regime break for age statistics.** Run ages from before the deploy are left-censored at each restart.
  - G2.4/G2.5 use only runs that **start** after the deploy.
  - G2.1–G2.3 are unaffected, because edge does not depend on age.

## 4. Verify after deploy

1. Studio log shows `[regime-state] restore {'restored': True, ...}` after the next restart.
2. `.cache/kairos/regime_state.json` updates every 15 min.
3. `nt_roadmap.kairos_daily_job.current_state` begins `last_run` and is ≤ 1h old.
4. Re-run the restart-impact measurement (§1). Cross-restart cell change should fall toward the 9% normal-tick rate.
5. Run `PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_weekly_check.py --accel` daily after 08:00 UTC.

## 5. Tests

`tests/test_regime_state_persistence.py` has 15 tests, covering:

- debouncer and tracker round-trips;
- stale, missing, corrupt and wrong-version snapshots;
- the atomic write;
- the flags;
- the orchestrator wiring;
- the week snap;
- per-horizon maturity;
- tier reconstruction;
- the heartbeat format;
- the hourly cron.

Full suite 2026-09-25: 1189 passed, 21 skipped, 2 failed. The 2 failures are in `tests/test_hmm_batch_consolidation.py` (quote-fetcher push count 0) and fail the same way with this change stashed, so they were already there. First live look: `reports/20260925_A092503.md`.

## 6. Post-deploy findings (2026-09-25 04:18/04:23 UTC restarts) + light-sweep catch-up

**Observed after the first deploy:**

- Both starts logged `[regime-state] restore {'restored': False, 'reason': 'no_snapshot'}`. That is
  expected: the previous code never wrote a snapshot, and the 04:18 instance ran no sweep before
  the 04:23 restart. The first snapshot was written at 04:31:44 (14 symbols, every section). The
  first restore is therefore the *next* restart.
- The cold start is visible. 12 of 14 cells changed between the 04:00 and 04:30 ticks, against
  5 of 14 between 03:30 and 04:00.
- **`stop()` never runs on the Studio.** The log has 0 "scheduler stopped" lines: `on_stop.sh`
  kills the process before the asyncio shutdown path runs. The per-symbol save is the part that
  matters, and loses at most the symbol in flight.
- **The 04:15 slot was dropped for all 14 symbols.** The process was down 04:01:51 → 04:18:01,
  and a fresh APScheduler only computes fires forward from "now". Every other slot in the 3h
  window had 14/14 with no errors.

**Fix: light-sweep startup catch-up** (`light_sweep_catchup_mode: live`)

- `_catch_up_stale_jobs()` now ends with `_catch_up_light_sweep_slot()`. For each `sweep-*` job
  it finds the most recent scheduled fire within 60 min (`_missed_light_slot`). It then reads
  which symbols have a `sweep-light` `nt_sweep_result` row since that slot
  (`fetch_light_swept_symbols_since`), and runs `_sweep_group` for the rest.
- Guards:
  1. Only the latest slot is caught up. A catch-up runs on current bars, so it stands in for one
     tick and can't rebuild older ones.
  2. It is skipped if the next tick is less than 5 min away.
  3. It is skipped if the read fails (a double tick is worse than a missed one).
  4. It uses the same pipeline and gates as a normal tick, so a caught-up signal is an ordinary
     `sweep-light` row.
- It also covers a slot the old process died part-way through, because only the unswept
  symbols are re-run.
- Tests: `tests/test_light_sweep_catchup.py` has 17 tests. They cover the live 04:18 → 04:15
  case, the lead-time boundary, most-recent-only, lookback, a partial slot, and the wiring (runs,
  no-op, fail-safe, off flag, non-light jobs ignored).
