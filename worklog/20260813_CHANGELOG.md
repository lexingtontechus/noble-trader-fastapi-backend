## [ASSESSMENT] - 2026-08-13 — EOD / recalibration deep-dive: nt_eod_actions, resolver artifact root cause, backlog

**Type:** Assessment + root-cause investigation. **No code changed** (recommendations
only; awaits "build code" before any implementation).

**Scope:** `nt_eod_actions`, `nt_calibration_records`, `nt_signal_sim`, EOD resolver
(`eod_resolver.py`), EOD correction (`eod_correction.py`), calibration enforcement
(`calibration_enforcement.py`), retention (`eod_retention.py`), light-sweep price
source (`orchestrator.py` / `price_fetcher.py`).

---

### 1. Status summary (live Supabase, 2026-08-13 ~18:00 UTC)

| Table | State |
|---|---|
| `nt_eod_actions` | **27 rows** — all written by the 2026-08-13 00:45 UTC correction pass (9 symbols × 3 actions). First non-empty run in the loop's history. |
| `nt_calibration_records` | **3,849 rows** (182 from 08-12 00:30 + 3,667 from 08-13 00:44). All symbol+venue pairs ≥ 20-record threshold. |
| `nt_signal_sim` | 23,191 total; **17,991 pending**; 5,200 resolved (tp/sl/expired); oldest pending 2026-08-08 04:35 UTC; capture tail current (18:11 UTC). |

**The recalibration loop is fully wired and LIVE** (verified in the deployed
orchestrator log):

```
2026-08-13 00:45:37 [eod:correction] XAGUSD: OVERCONFIDENT bias=0.1825 → emergency sweep + p_win'=0.9631 + Kelly×0.50
... (9 symbols) ...
2026-08-13 00:45:42 [eod:correction] complete: 14 symbols, 27 actions, 0 errors
2026-08-13 00:45:42 [eod:lookback] emergency heavy sweeps requested: [AUDUSD, BTCUSD, EURUSD, GBPUSD, USDCAD, XAGUSD, XAUAUD, XAUEUR, XAUUSD]
2026-08-13 00:45:48 [eod:lookback] emergency heavy sweep AUDUSD: ok          (BTCUSD, XAGUSD, XAUAUD, XAUEUR, XAUUSD: ok)
2026-08-13 00:46:04 [eod:lookback] emergency heavy sweep EURUSD: no_combo_passed_filters (also GBPUSD, USDCAD)
2026-08-13 00:46:53 [eod:lookback] retention done: deleted_resolved=0 expired_stale=0 errors=1
2026-08-13 18:10:17 [sweep:orch] BTCUSD: calibration ENFORCED (OVERCONFIDENT, bias=0.9285) — p_win→0.9641, kelly→0.020000   (live every 5-min sweep)
```

---

### 2. ROOT CAUSE (a) — the impossible win/loss rates in `nt_calibration_records`

**Symptom:** USDJPY & USDCHF resolved 100% win (382/382), BTCUSD 5% win (19/382),
USDCAD 4.5% win (17/382). Every resolved row has `hold_bars=1` (TP/SL tripped on
the FIRST post-entry bar) and exit prices cluster at fixed values (USDJPY exits at
158.472 / 157.696 / 157.73425).

**Evidence chain (all reproduced live):**

1. **`nt_signal_sim` direction mix:** USDJPY 991 sell / 9 buy; USDCHF 991 sell / 9 buy;
   USDCAD 979 buy / 21 sell; BTCUSD 555 sell / 445 buy. The extreme-win symbols are the
   mostly-SELL ones; the extreme-loss symbols are mostly-BUY (USDCAD) or mostly-SELL
   into a rising market (BTCUSD).
2. **Signal levels are internally sane** (e.g. USDJPY sell e=163.6075, s=163.9075
   above entry, tp=163.0075 below entry) — the artifact is NOT inverted TP/SL geometry.
3. **Entry prices vs candle history disagree by ~3.6–3.8% during Aug 6–8:**
   - Sweep recorded USDJPY entries 163.60–163.85 on 2026-08-06 07:50 – 08-08 04:35.
   - The live TDVA 1h candle history (fetched and verified locally, continuous hourly
     bars, no weekend gap) shows USDJPY at **157.5–157.9** at those same timestamps.
   - Today the two agree (sweep entry 159.52 at 18:11 ≈ history close 159.53).
4. **Reproduction:** `fetch_candles_for_symbols` + `anchor_post_entry_closes` +
   `resolve_signal_outcome` on the 08-08 04:35 USDJPY signal reproduces the DB record
   exactly: `tp_hit, exit_price=157.73425, exit_idx=0, hold_bars=1` — because the first
   post-entry bar (157.73) is far below the sell's TP (≈163.0) when the entry is the
   stale 163.85.
5. **Mechanism:** the light sweep takes `current_price = prices[-1]` (orchestrator
   price path via `fetch_tradingview_batch`). The TDVA ordering bug (history returned
   DESCENDING; `prices[-1]` was the OLDEST bar) was fixed in the repo 2026-08-11 but
   the **deployed LightningAI runtime lagged git** — signals captured Aug 6–8 were
   priced with the stale ~8–12-day-old value (~163.8 instead of ~157.7). The EOD
   resolver, running on the fixed runtime at 08-13 00:44, scored those signals against
   CORRECT fresh bars. A SELL entered 3.8% too high instantly "wins" (first bar already
   below TP); a BUY entered 3.8% too high instantly "loses" (first bar already below
   SL). → `hold_bars=1` for essentially every row, and the win/loss split is just the
   direction mix (sell-heavy pairs "won", buy-heavy or wrong-side pairs "lost").

**Consequence — the ENTIRE calibration table is contaminated.** All 3,849 records
come from signals captured Aug 5–8 (oldest-first drain), i.e. the pre-fix stale-price
window. The recorded biases for the overconfident symbols (USDCAD +0.9363, BTCUSD
+0.9285, EURUSD +0.389, …) are **garbage-in/garbage-out**, not genuine
signal-quality measurements. The correction loop acted on them exactly as designed —
that part is healthy — but its inputs are wrong.

Secondary observation: `predicted_p_win` is almost always 0.97–1.00 (many rows are
exactly 1.0), so any realized win rate below ~97% reads as a large positive bias.
This is a second, independent calibration problem (systemic overconfidence in the
probability model, or p_win inflation), worth a separate look.

---

### 3. Overconfident symbols & recalibration — status and recommendations

- 9 symbols were flagged OVERCONFIDENT (all with |bias| ≥ 0.18; USDCAD/BTCUSD at +0.93).
- USDCHF & USDJPY (100% win per the resolver) computed bias ≈ −0.02 → classed CALIBRATED
  → no actions. This is a *false negative* caused by the same artifact (they should
  also be treated as untrustworthy).
- Enforcement (Bayesian p_win shrink toward 0.5, Kelly ×0.5) is applied live in every
  5-min sweep for the 9 flagged symbols; emergency heavy sweeps re-optimized params on
  fresh data for 6 symbols (3 found no passing combo).

**Recommendations (await "build code"):**

1. **Purge or quarantine the contaminated calibration records.** All 3,849 rows are
   from the Aug 5–8 stale-price window. Options:
   - (Preferred) DELETE `nt_calibration_records` where `entered_at < 2026-08-11` (the
     pre-fix window) and let fresh post-fix signals rebuild the dataset (~285 records/
     symbol/night at 4,000 resolved/night → ≥20/symbol threshold back in ~1 night,
     meaningful n in ~1 week).
   - Or quarantine (flag column) and exclude from `fetch_calibration_bias`.
2. **Rebuild decision:** after purging, keep `EOD_CALIBRATION_ENFORCE=1` (the loop is
   correct; it just needs clean inputs). Optionally set it to 0 for 1–2 nights while
   the table repopulates to avoid acting on near-empty samples.
3. **Add a data-quality guard to `fetch_calibration_bias` / the correction:**
   reject a symbol's bias when its records show degenerate outcome mixes (e.g.
   win_rate == 0 or 1.0 with n ≥ 20, or all `hold_bars=1` patterns) — treat as
   "insufficient/untrustworthy" instead of emitting a huge bias. Cheap proxy:
   skip bias when `min(win, loss) == 0`.
4. **Clamp/floor bias magnitude** (e.g. |bias| ≤ 0.5) so a single contaminated
   window cannot produce a +0.93 penalty.
5. **Investigate `predicted_p_win` inflation** (rows pinned at 1.0) separately —
   if the model emits ~1.0 confidence broadly, bias will be systematically positive
   even on clean data.
6. Keep the emergency heavy-sweep path (fresh-data re-optimization is the only true
   bias-recovery mechanism) — but note 3/9 symbols found no passing combo; consider
   relaxing MIN_RR/MIN_TRADES for the emergency path.

---

### 4. Backlog — why `nt_signal_sim` "never catches up" (confirmed steady-state)

**Mechanics (verified in code + live run):**
- **Capture:** every non-neutral signal from the 5-min light sweep × 14 symbols is
  persisted to `nt_signal_sim` (`NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY=False`
  default) ≈ **3,200–4,032 rows/day**.
- **Resolve:** the nightly 00:30 run calls
  `resolve_pending_signals(limit=EOD_RESOLVE_LIMIT=1000, max_total=EOD_RESOLVE_MAX_TOTAL=4000)`.
  The loop batch-fetches 1000 at a time but **stops at max_total=4000** — the
  08-13 run resolved exactly 4,000 (`batches: 4`), i.e. it hit the cap, not the drain.
- 4,000/night ≈ 4,000/day capture → **equilibrium, not catch-up**: pending has been
  ~17–18k for days (17,992 on 08-12 → 17,991 now). The oldest pending advances ~1
  day/night and will reach the 7-day retention stale boundary
  (`EOD_RETENTION_STALE_DAYS=7`) in ~1.4 days — after which retention starts marking
  the oldest pending rows `expired` (never resolved → **no calibration record**), so
  a growing fraction of captured signals silently loses its outcome data.

**Proposed solutions (await "build code"):**

1. **Raise `EOD_RESOLVE_MAX_TOTAL`** (e.g. 8,000–12,000) so nightly resolve > daily
   capture and the backlog actually shrinks (~2–4k/night net drain → clears the 18k in
   ~5–9 nights). Cost: longer nightly runtime + ~2–3× TDVA/PostgREST calls (bounded).
2. **Run the resolver more than once a day** (e.g. 00:30 + 12:30 UTC) — same effect,
   spreads API load.
3. **Enable `NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY=True`** — persists only QUALIFIED
   signals (~93% fewer rows per the settings comment). This is the highest-leverage
   change: capture becomes ~10% of current, the backlog drains in days, and calibration
   measures exactly the signals that are actually published/traded. Trade-off: the
   ledger no longer measures non-qualified signal bias (acceptable — they aren't acted on).
4. **One-time drain** via `scripts/drain_signal_backlog.py` to clear the 18k immediately
   (then rely on option 1/3 to prevent re-accumulation).
5. **Reconcile retention vs resolve window:** `min_ts` bound is 200h (8.3d) but
   retention expires at 7d — the binding constraint is retention. If options 1/3 don't
   land, raise `EOD_RETENTION_STALE_DAYS` (e.g. 10–14) so rows aren't expired before
   the resolver reaches them.

---

### 5. Minor findings

- **Retention resolved-delete pass fails nightly (HTTP 400):**
  `[eod:retention] resolved-delete pass failed: count HEAD HTTP 400` — the protect-list
  query (`signal_id=not.in.(1000 uuids)`) exceeds the Supabase gateway URL limit.
  `deleted_resolved=0, errors=1` every run. No practical impact yet (nothing is 30 days
  old) but resolved-row cleanup will never run until the protect-list is chunked.
- `nt_sweep_result` insert failed once (2026-08-12 22:30, blank error body) — one-off.
- `v_eod_calibration_bias` (reads `nt_signal_sim`) and the correction
  (reads `nt_calibration_records`) still disagree on aggregation — both are populated
  now, so the mismatch is methodology, not thin data. The correction path is authoritative.

---

### 6. Verification evidence (how the above was confirmed)

- Supabase read-only queries via the backend's own settings module
  (`regime_platform.core.settings` → `.env.local`), httpx GET `/rest/v1/…` with
  `apikey`/`Bearer` headers; exact counts via `Prefer: count=exact` + `content-range`.
- Live deployed orchestrator log pulled from LightningAI
  (`lightning studio cp lit://…/logs/orchestrator.log`, 32 MB) and grepped for the
  08-13 00:30–00:47 run, enforcement stamps (18:10–18:11), retention error, 23502 history.
- Local reproduction of the resolver artifact using live TDVA candle data
  (`fetch_candles_for_symbols`, `anchor_post_entry_closes`, `resolve_signal_outcome`)
  → reproduced `tp_hit / exit 157.73425 / hold_bars=1` for the 08-08 04:35 USDJPY signal.

---

## [BUILD] - 2026-08-13 — Backlog fixes (authorized "build code - backlog fixes")

### Action 1 — Drain the ENTIRE backlog NOW (process running)

`scripts/drain_signal_backlog.py --limit 3000 --max-passes 30` launched 2026-08-13
~13:56 local (~17:56 UTC) from the repo venv (local code = current git HEAD with all
fixes). The script calls `resolve_pending_signals(limit=3000)` with NO `max_total`
(= unlimited), so one pass drains every in-window pending row (all 17,640+ pending
fall inside the 200h candle window — oldest is 08-08 04:35 UTC).

- Pending went 17,991 → 17,640 in the first ~4 min; steady ~0.3s/signal serial HTTP
  (PATCH 204 + calibration POST 201 per row) → ETA ~1.5–2h, done well before the
  00:30 UTC nightly run.
- No errors observed so far; TDVA batch 200s, PostgREST 204/201s.
- **Side effect (documented, not fixed):** ~60% of the drained rows are from the
  pre-fix stale-price window (08-08 04:35 → 08-11 ~14:00 UTC) → their new
  `nt_calibration_records` rows are contaminated like the original 3,849. The
  recalibration purge recommendation (from the assessment above) is updated to:
  **DELETE `nt_calibration_records` WHERE `entered_at < '2026-08-11T14:00:00Z'`**
  (measured fix boundary: sweep entries 08-11 have mixed deviation, 08-12+ ≈ 0.00%).

### Action 2 — Raise the nightly resolve cap so the backlog cannot re-accumulate

`regime_platform/core/settings.py` — `EOD_RESOLVE_MAX_TOTAL` default **4000 → 12000**
(+ comment). Capture ≈ 3,200–4,000/day vs 4,000/night was a treadmill (pending stuck
~18k for days); 12,000 ≈ 3 days of capture per nightly run → drains ~8k/night and the
ledger stays current without manual intervention.

### Action 3 — Fix the retention resolved-delete HTTP 400 (protect-list URL too long)

`regime_platform/sweep_orchestrator/eod_retention.py` — the resolved-delete pass built
a single `not.in.(1000 UUIDs)` protect-list URL (~38KB) that exceeded the Supabase
gateway limit → `[eod:retention] resolved-delete pass failed: count HEAD HTTP 400`
every night since 08-13 00:46. Rewrote `_delete_resolved_older_than`:
- New `_fetch_protected_signal_ids(client)` helper (extracted the open-position query).
- **No open positions** → unchanged single HEAD-count + DELETE path (small URL).
- **Open positions** → keyset pagination over resolved rows (`signal_id` asc, `gt`
  cursor — stable under concurrent deletes), protected ids filtered client-side,
  deletes issued in `in.(≤100)` chunks.

`tests/test_eod_retention.py` — FakeClient gained a `get` (stateful responses); new
test `test_protected_delete_uses_chunked_pagination` asserts protected ids are never
in the DELETE filter; existing tests updated for the now-recorded pos-protect GET.
Retention + wiring tests: **20 passed**.

### Verification (pending at write time)

- Full backend pytest suite running in background (baseline 332 passed / 23 skipped).
- Drain completion + final counters (pending ≈ 0, calibration records growth,
  per-day outcome mix) to be appended below.

### Drain restart (2026-08-13 ~22:18 UTC) — first drain died at 11,364 pending

The first drain process (PID 10552) stopped at pending 11,364 (6,627 resolved) —
the Hermes process handle was lost and the child died with it (counters static
over 90s; TDVA API verified healthy). Restarted with two hardening changes:

1. **`scripts/drain_signal_backlog.py` resilience fix** — the old guard aborted the
   whole drain on a SINGLE zero-progress pass (`resolved==0 and errors>0`). Now it
   retries after a 10s sleep and only stops after **3 consecutive** zero-progress
   passes. A transient TDVA/PostgREST blip can no longer kill a multi-hour drain.
2. **File logging** — restarted with stdout+stderr redirected to
   `%LOCALAPPDATA%/Temp/drain2.log` so progress is inspectable even if the Hermes
   session handle is lost again.

Restart state: pending 11,364 → 11,178 within 2 min (~90/min). ETA ~00:20 UTC —
a tight ~10-min margin ahead of the 00:30 UTC nightly (LightningAI, deployed code
with the OLD 4000 cap); the drain will be paused before 00:30 if it cannot finish,
letting the nightly take the remainder. Final counters to be appended on completion.

---

## [PLAN + BUILD] — 2026-08-13 — Intraday recalibration cadence (user-approved plan)

**Problem:** bias is computed nightly (00:30 UTC) and enforcement applies the same
action for up to 26h. A mid-day regime shift leaves un-flagged symbols trading on
stale/inflated p_win for up to 24h — "waiting 24h for a recalibration could be
fatal for trading" (user, 2026-08-13).

**Approved plan (user: "B - every 4hrs over 6hrs correction pass"):**

1. **A. Nightly 00:30 UTC** — unchanged backbone: resolver (12k cap) + correction + retention.
2. **B. Correction-only passes every 4h** — 04:30 / 08:30 / 12:30 / 16:30 / 20:30 UTC:
   bias recompute → nt_eod_actions; emergency heavy sweep ONLY for newly-flagged
   symbols, cooldown ≤1 per symbol per 12h (bounds backtest cost). No resolver/retention.
3. **C. Rolling bias window** — bias computed over the last ~7 days of records
   (min 20 records) instead of all-time, so a regime shift moves bias in hours.
4. **D. Drift monitor on the 5-min light sweep** — per-symbol fast check: rolling
   48h realized win rate vs predicted p_win; if deviation > threshold with min
   sample and no fresh action exists, write an immediate penalty action (bypasses
   the 4h cadence). Whipsaw-guarded (min-n + fresh-action skip).
5. **E. ACTION_FRESH_WINDOW_HOURS 26 → 8** — actions can't overstay across the 4h cadence.

**Sequencing:** purge contaminated records (awaiting user approval post-drain) →
then the cadence build below. The code is built in stages, each verified by unit
tests + the full suite before proceeding.

### Stage status (appended per stage)

**STAGE 1 — settings (done, verified):** `EOD_CORRECTION_INTRADAY_ENABLED=True`,
`EOD_CORRECTION_INTERVAL_HOURS=4`, `EOD_ACTION_FRESH_HOURS=8`,
`EOD_BIAS_WINDOW_DAYS=7`, `EOD_BIAS_MIN_RECORDS=20`, `EOD_DRIFT_MONITOR_ENABLED=True`,
`EOD_DRIFT_WINDOW_HOURS=48`, `EOD_DRIFT_MIN_SAMPLE=20`, `EOD_DRIFT_THRESHOLD=0.15`,
`EOD_HEAVY_SWEEP_COOLDOWN_HOURS=12`. `calibration_enforcement` freshness now reads
`settings.EOD_ACTION_FRESH_HOURS` (fallback constant 8; type-safe against mocks).

**STAGE 2 — rolling bias window (done, verified):** `fetch_calibration_bias` now
defaults to `entered_at >= now - EOD_BIAS_WINDOW_DAYS` with `EOD_BIAS_MIN_RECORDS`
min-n; `window_days=0` restores all-time. Tests: `tests/test_calibration_bias_window.py` (8).

**STAGE 3 — 4h correction-only passes (done, verified):** orchestrator registers
`eod-correction-intraday-*` jobs at 04:30/08:30/12:30/16:30/20:30 UTC (computed from
interval=4) when `EOD_CORRECTION_INTRADAY_ENABLED`. New `_run_eod_correction_only()`
runs `run_eod_correction()` + emergency heavy sweeps for newly-flagged symbols with a
12h cooldown (`_heavy_sweep_last`); nightly lookback refactored to share the extracted
`_execute_emergency_heavy_sweeps()` helper (no cooldown). Tests: 4 added to
`tests/test_eod_lookback_wiring.py`.

**STAGE 4 — drift monitor (done, verified):** new
`regime_platform/sweep_orchestrator/drift_monitor.py::check_drift(symbol)` — on each
light sweep (orchestrator §5c) checks the rolling 48h bias; if > 0.15 with ≥ 20
records and no fresh penalty action exists, writes an immediate `bayesian_penalty`
to `nt_eod_actions`. Overconfidence-only by design; best-effort (never raises).
Tests: `tests/test_drift_monitor.py` (7).

**FULL SUITE + drain completion counters: appended after verification.**

**FULL SUITE: 394 passed / 23 skipped (0 failures)** — baseline 332 → +62 tests
(retention chunking, bias window, intraday correction, drift monitor).
Drain (restart 2) live at 22:39 UTC: pending 9,348, rate ~51/min → ETA ~01:40 UTC,
crossing the 00:30 UTC nightly. Overlap risk assessed as SELF-HEALING: the rows the
drain is resolving are all `entered_at < 08-11T14:00Z` (purge-bound contaminated
rows); any duplicate calibration inserts from a brief nightly overlap share the same
outcome (win rate unaffected) and are removed by the pending purge. Final drain
counters to be appended on completion.

### PLANNED (post-drain, user-approved to schedule) — predicted_p_win_raw capture

`nt_calibration_records.predicted_p_win_raw` is a **calibration** field (NOT a
live-trade field — the backend is not a trade executor, and for any live path the
used probability is the correct audit input). Purpose: enforcement (since 2026-08-13)
shrinks `p_win` toward 0.5 BEFORE it is stored, so `predicted_p_win` now records the
ENFORCED probability. The bias math (`avg_pred − win_rate`) then converges toward 0
partly because the enforcement is correcting — masking the model's true
miscalibration. `predicted_p_win_raw` (pre-shrink value) separates "is the model
still miscalibrated?" from "is enforcement compensating?".

Implementation sketch (deferred until after drain completion is confirmed):
1. In orchestrator §5b, stash the pre-enforcement `ev_result.p_win` before
   `apply_calibration_adjustment` mutates it.
2. Persist it into `nt_signal_sim` (new column or reuse) + include in the resolver's
   `nt_calibration_records` payload (`CALIBRATION_RECORD_COLUMNS` + migration).
3. Optional: `calibration_brier` compute (predicted-vs-outcome MSE) for a proper
   calibration-quality metric once raw values exist.

Status: SCHEDULED — build after drain completion confirmed + purge executed.

### ✅ DRAIN COMPLETE (2026-08-13 ~16:59 local / 22:59 UTC) — verified live

`[drain] backlog cleared — 11644 resolved over 1 pass(es)` (final restart pass;
total across both runs ≈ 18.3k). by_outcome final pass: tp_hit 3,995 / sl_hit 2,842
/ expired 4,807 (41% expired — most drained rows were Aug 8–11 signals whose 48-bar
expiry window long passed; correct behaviour). errors=9 (0.08% — transient blips,
all rows still resolved).

Live state after completion:
- `nt_signal_sim` total 23,471 — **pending 0** (was 17,991 at start of day).
- `nt_calibration_records` 17,201 (was 3,849 → +13,352 written today).
- Next nightly 00:30 UTC will find nothing pending; ledger now stays current via
  the 12,000/night resolve cap (repo-side; deploy pending).

**Purge readiness:** final contaminated/clean split computed below (assessment
table). Awaiting user approval to execute:
`DELETE nt_calibration_records WHERE entered_at < '2026-08-11T14:00:00Z'`
(+ the 27 contaminated `nt_eod_actions` rows from 00:45).

### Final purge assessment (post-drain, verified)

- **CONTAMINATED (< 08-11T14:00Z): 14,248 records** (win 8,517 / loss 5,731) —
  stale-entry artifact patterns: USDJPY/USDCHF 100% win, USDCAD 1.4%, XAUAUD 89%.
  **PURGE TARGET.**
- **CLEAN (>= 08-11T14:00Z): 2,953 records** (win 1,457 / loss 1,496) — entries
  verified correct (entry-vs-history deviation ≈ 0.00–0.04% for sampled symbols).
  The extreme outcomes here are NOT an artifact: direction×outcome shows a coherent
  Aug 11–13 macro move (USD weakening + risk-on) — engine emitted SELL EURUSD/GBPUSD/
  AUDUSD (all SL'd as those pairs rose), BUY USDCAD (SL'd as CAD strengthened),
  SELL USDJPY (TP'd as USDJPY fell), BUY US500/US30/XAGUSD (TP'd). Genuine (bad)
  directional performance over 2 days — exactly what calibration should flag.
  **KEEP.**
- After purge: ~2,953 clean records remain, every symbol ≥ 20 (threshold met) →
  biases computable immediately; EURUSD/GBPUSD/USDCAD/AUDUSD will legitimately flag
  OVERCONFIDENT (pred ≈ 0.97 vs ~0–8% realized).
- Recommended purge statement (awaiting user approval):
  `DELETE FROM nt_calibration_records WHERE entered_at < '2026-08-11T14:00:00Z';`
  plus DELETE the 27 contaminated `nt_eod_actions` rows (ts 2026-08-13T00:45, all
  based on contaminated bias) so enforcement stops applying them immediately.

---

## ✅ EXECUTED (user: "run purge and predicted_p_win_raw fixes")

### 1. PURGE — executed + verified live (2026-08-13 ~23:10 UTC)
- `DELETE nt_calibration_records WHERE entered_at < '2026-08-11T14:00:00Z'` → **14,248 rows deleted** (204).
- `DELETE nt_eod_actions WHERE ts <= 2026-08-13T01:00:00` → **27 contaminated actions deleted**.
- Verified: calibration total **2,953** (all clean ≥ boundary), contaminated remaining **0**, eod_actions **0**.
- Enforcement now no-ops (no actions) until the next correction run computes bias from the clean set.

### 2. predicted_p_win_raw build — done (verification: 401 passed / 23 skipped, +7 tests)
- `settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` (default **False** — deploy-safe: the nt_signal_sim
  column must exist before enabling, else PGRST204 kills capture).
- Orchestrator §5b stashes `p_win_raw = ev_result.p_win` BEFORE enforcement shrinks it;
  `_build_signal_sim_payload` includes `predicted_p_win_raw` in nt_signal_sim only when the
  flag is on (3 gating tests).
- `supabase_client.NT_SIGNAL_SIM_COLUMNS` + `eod_resolver.CALIBRATION_RECORD_COLUMNS` extended
  with `predicted_p_win_raw` + `calibration_brier`.
- Resolver `_write_calibration_record` now writes `predicted_p_win_raw` (pass-through) and
  computes `calibration_brier` = (1−p)² on win / p² on loss, using raw when present else the
  stored (enforced) p_win (4 tests).
- **Migration (apply in Supabase SQL editor before enabling the flag):**
  `supabase/migrations/114_add_predicted_p_win_raw.sql`
  → `ALTER TABLE public.nt_signal_sim ADD COLUMN IF NOT EXISTS predicted_p_win_raw double precision;`
- nt_calibration_records.predicted_p_win_raw / calibration_brier columns already exist — no DDL there.

---

## POST-PUSH (2026-08-13/14) — migration rename + LightningAI deploy verification

### Migration file renamed to match sequence
`supabase/migrations/20260813_add_predicted_p_win_raw.sql` →
`supabase/migrations/114_add_predicted_p_win_raw.sql` (last existing = 113;
convention is 3-digit `NNN_snake_name.sql`). References updated in README,
settings.py, supabase_client.py, orchestrator.py, worklog. **Not yet pushed**
(awaiting push approval).

### LightningAI studio restarted + verified (v7.8.0 LIVE)
- `lightning studio stop/start --name noble-trader-unified` (2026-08-13 ~22:40 UTC).
- New boot **2026-08-14 00:39:45 UTC** — verified in orchestrator.log:
  - `Intraday EOD correction scheduled every 4h at hours: [4, 8, 12, 16, 20]` ✓
  - 5 new jobs registered: `EOD intraday correction (04:30/08:30/12:30/16:30/20:30 UTC)` ✓
  - Light sweep 14 symbols (incl. AAPL/US30USD/US500), heavy sweep Sun 20:00 ✓
  - **0 ERROR/Traceback lines after boot**; sweep completions normal ✓
- Note: the 00:30 UTC nightly on 08-14 fired before this boot (00:39) and was
  skipped; first intraday correction pass will be 04:30 UTC. Deployed runtime
  now has the 12,000 resolve cap, 4h passes, drift monitor, retention fix, and
  raw-p_win capture code (capture flag still OFF until the 114 migration is
  applied in the Supabase SQL editor).
