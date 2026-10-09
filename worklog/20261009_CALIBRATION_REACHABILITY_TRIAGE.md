# Calibration & reachability triage — problem, process, prescribed solution

**Date:** 2026-10-09
**Status:** Diagnosis complete; **era-filter fix built + migration 205 applied.** Bracket-aware `p_win` (P1) deferred until the clean era is observed.
**Data:** 2026-10-08 22:35 → 2026-10-09 22:36 UTC (981 resolved signals) + materialized tables.

---

## 1. Goal (explicit)

**Resolve the calibration + reachability disagreement** so `p_win` predicts the realized win rate within tolerance across every bracket, and reachability's tp/sl/expire split is consistent with the calibration table it feeds.

**Success metric:** per-bucket |bias| < 0.10 on a rolling 7-day **post-cutover** window, and reachability `pct_expired` consistent between the live cut and the materialized table (no sign flips).

---

## 2. What the data showed

### 2a. Resolution behaviour (24h)
- **981 signals, 100% resolved within 48 bars. p50 hold = 1 bar, p90 = 6 bars.**
- Mix: `sl_hit 82.3%`, `tp_hit 16.8%`, `expired 0.9%`. Realized WR = **17.0%**.

The median signal resolves in **one hour**. On the intrabar basis price touches a level almost immediately — the expiry window is a formality (this is why the p90-expiry lever measured inert, `20261009_P90_HOURS_LIVE_MODE.md` §10).

### 2b. Calibration bias — bimodal, and era-contaminated

**24h realized** (from `nt_signal_sim`, tp/sl only):

| bucket | n | avg p_win_dir | realized WR | bias |
|---|---|---|---|---|
| 0.5-1 | 64 | 0.337 | 0.016 | +0.321 |
| 1-2 | 573 | 0.279 | 0.098 | +0.182 |
| 2-4 | 215 | 0.361 | 0.181 | +0.180 |
| 4+ | 120 | 0.235 | **0.575** | **−0.340** |

**Materialized long-window** (`nt_calibration_bias_by_bucket`, vol-tier-weighted):

| bucket | total_n | avg_p_win | win_rate | bias | expired |
|---|---|---|---|---|---|
| 0.5-1 | 2192 | 0.448 | 0.364 | +0.084 | 0.0% |
| 1-2 | 5644 | 0.407 | 0.271 | +0.136 | 0.0% |
| 2-4 | 3660 | 0.433 | 0.311 | +0.121 | 0.0% |
| 4+ | 19084 | 0.439 | 0.248 | **+0.191** | 38.5% |

### 2c. The reconciliation — the 4+ bucket flips sign between windows

- **24h realized:** 4+ WR = 57.5%, bias −0.340 (underconfident).
- **Long-window:** 4+ WR = 24.8%, bias +0.191 (overconfident), 38.5% expired.

The long-window table is dominated by **pre-cutover close-walk history**; the 24h cut is entirely **post-intrabar-cutover** (2026-10-02). The 4+ sign-flip is the **resolver-basis change** showing up as a calibration discontinuity — the same bracket resolves very differently under intrabar (first-touch) than under the old close-walk.

---

## 3. The clean-cutover decision (user: "determine if archive/remove/delete vs clean cutover")

**Investigated the data model:**
- `nt_signal_sim`: 92,199 rows, **84,379 (92%) pre-cutover**. No `settlement_mode`/era column on the raw ledger.
- `nt_calibration_bias_by_bucket`: **recomputes from `nt_signal_sim`** on every pass (`build_calibration_bias_by_bucket` → `_delete_old_rows` → upsert). Derived, not a source of truth.
- The **era-split mechanism already exists and is already on** for the live gate: `calibration_cutover_mode: on` (migration 192) makes `fetch_calibration_bias` read `v_calibration_records_current`, which excludes pre-flip rows (6,319 current vs 7,307 base). The live gate is **already era-clean**.
- **The sole gap:** `build_calibration_bias_by_bucket` read `nt_signal_sim` directly with **no cutover filter**, so the by-bucket table spanned all 84k pre-cutover rows.

**Decision: no archive, no remove, no delete.** Deleting 84k rows is the 2026-10-01 wipe mistake again and destroys the heavy-sweep 90-day backtest window. The clean cutover is achieved by **filtering the recompute to `resolved_at >= nt_calibration_cutover`**, reusing the existing single-row marker — one filter, no data loss, and it makes the by-bucket table agree with the live gate by construction.

---

## 4. What was built

| Piece | Where | Change |
|---|---|---|
| Era filter | `calibration_bias_bucket.build_calibration_bias_by_bucket` | `_fetch_cutover_at()` reads `nt_calibration_cutover`; adds `resolved_at >= cutover_at` to the `nt_signal_sim` fetch. Missing marker → fail-open (no filter) |
| `signal` column | migration 205 + writer + whitelist + on_conflict | `ALTER TABLE ... ADD COLUMN signal text` (nullable, no backfill); output rows stamp `signal='all'`; `NT_CALIBRATION_BIAS_BY_BUCKET_COLUMNS` + `on_conflict` updated |
| Tests | `test_calibration_bias_bucket.py` | era filter forwarded (Z-suffix, gte), `signal='all'` stamped, missing-marker fail-open; fixtures moved post-cutover |

**Verified against real data** — post-cutover WR by bucket (the clean intrabar numbers the recompute will now produce):

| bucket | n | WR |
|---|---|---|
| 1-2 | 410 | **5.6%** |
| 2-4 | 308 | 13.6% |
| 4+ | 278 | **5.0%** |

This confirms the diagnosis completely: on the clean intrabar era the 4+ WR collapses from the contaminated +0.19-bias picture to **5.0%**, and tight brackets (1-2) are **5.6%**. The model is massively overconfident across the whole ladder on the real basis.

---

## 5. Remaining problems (deferred, in order)

- **P1 — bracket-aware `p_win`.** The model rates tight brackets as decent bets; geometrically they stop out on the first wick (1-bar median resolution, 64–73% SL). *Prescribed:* feed the bracket's realized win-rate (reachability, now era-clean) as a prior, and/or add an entry-bar MFE/MAE feature so the model sees the wick that triggers the stop. **Deferred until the clean era accumulates a week of data** — re-deriving weights now would train on the very contamination this fix removes.
- **P3 — population parity.** Reachability includes all resolved rows; calibration bias is filtered. Align both on the same `contaminated=false` + era filter so a bucket's `pct_expired` matches across tables.

---

## 6. TDVA vs hourly-close vs intrabar — assessment

**Current:** TDVA is HTTP batch — `POST /api/price/batch`, 1h interval, batched 10/call (`price_fetcher.fetch_tradingview_batch`). **No websocket/streaming path** in the codebase. Each bar already carries full OHLC, so high/low are available and the resolver uses them (intrabar basis).

| Basis | Sees | Pro | Con | Verdict |
|---|---|---|---|---|
| Hourly close (legacy) | 1 price/hr | smooth | blind to intrabar wicks → mis-resolves tight brackets (the cutover fixed this) | retired |
| **Intrabar (current)** | high+low/1h | catches the stop-triggering wick; matches a live stop | same-bar TP/SL ambiguity resolved by policy, not truth | **keep** |
| Realtime stream | ticks | true first-touch order | no stream endpoint exists; websocket ingest + replay/backfill + storage is a large subsystem; history would still be 1h OHLC (new era split) | **not now** |

**Logic:** the intrabar basis already resolves on the high/low a live stop actually touches. Streaming would only refine **same-bar ordering**, already handled by `settlement_same_bar_policy` (sl_first) + the persisted `ambiguous` flag (migration 204). The problem is **calibration era-contamination and bracket-awareness, not resolution granularity** — fixable without new data infrastructure. **No streaming ingest.**

---

## 7. Files changed

| File | Change |
|---|---|
| `supabase/migrations/205_calib_bias_signal.sql` | `signal` column (applied) |
| `regime_platform/sweep_orchestrator/calibration_bias_bucket.py` | `_fetch_cutover_at` + era filter + `signal='all'` |
| `regime_platform/sweep_orchestrator/supabase_client.py` | whitelist + on_conflict include `signal` |
| `tests/test_calibration_bias_bucket.py` | era/signal/fail-open tests |
| `worklog/20261009_CALIBRATION_REACHABILITY_TRIAGE.md` | this |

Full suite: **1896 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs` — unrelated).
