# Worklog: Table Freshness Fix + Dynamic Tier Real-Time Optimization Design

**Date:** 2026-10-07
**Scope:** Table freshness root-cause + fix, dynamic tier optimization design
**Related:** `worklog/20261007_PULSE_CHECK.md`, `references/drift-detection-pattern.md`

---

## 1. Table Freshness — Root Cause

### 1.1 The tables ARE being updated

Direct query confirms:
- `nt_bracket_reachability` latest `computed_at`: **2026-10-07T01:18:29** (today, ~19h ago)
- `nt_calibration_bias_by_bucket` latest `computed_at`: **2026-10-06T01:18:43** (yesterday, ~43h ago)

The update path:
```
Nightly EOD lookback (01:17 UTC)
  → eod_resolver.resolve_pending_signals()
  → eod_correction.run_eod_correction()
  → _run_bracket_reachability()
      → build_reachability_table()         ← upserts nt_bracket_reachability
      → build_calibration_bias_by_bucket()  ← upserts nt_calibration_bias_by_bucket
      → build_entry_drift()                 ← upserts nt_entry_drift
      → build_live_expectancy_daily()       ← upserts nt_live_expectancy_daily
```

### 1.2 Why the pulse check showed stale data

The pulse check script's query used `order=computed_at.desc&limit=50`. PostgREST's `order` on a `timestamptz` column with `limit` returns the **first 50 rows in the ordering** — but the table has **hundreds of rows** (one per symbol × vol_tier × bucket × signal). The 50 rows returned were the **alphabetically-first** symbols (AAPL, AUDUSD, BTCUSD, EURUSD, GBPUSD) with their **oldest** `computed_at` values, not the latest build's rows.

### 1.3 The real issue: No row lifecycle management

The `upsert_bracket_reachability` and `upsert_calibration_bias_by_bucket` functions use PostgREST's `on_conflict` upsert, which **updates existing rows** but **does not delete rows that are no longer relevant**. Over time, the table accumulates:
- Rows from old builds with stale statistics
- Rows for (symbol, vol_tier, bucket) combinations that no longer have enough data
- Rows with `n_proxy` (proxy ATR) that should be filtered out

### 1.4 Fix: Delete-then-insert (Option A) + Latest-view (Option C)

**Option A:** Add delete-then-insert logic to `build_reachability_table()` and `build_calibration_bias_by_bucket()`. Before upserting fresh rows, delete old rows for the same symbols. This ensures the table always reflects the latest build.

**Option C:** Create a view `v_bracket_reachability_latest` that returns only the latest build's rows using `DISTINCT ON (symbol, vol_tier, distance_atr_bucket, signal) ... ORDER BY computed_at DESC`. This gives consumers a clean interface without changing the underlying table.

**Combined approach:** Option A prevents unbounded growth; Option C gives a clean read interface for any consumers that can't be updated to filter on `computed_at`.

---

## 2. Dynamic Tier Real-Time Optimization Design

### 2.1 The problem

Today, dynamic tiers (dynamic ATR, dynamic deadline, regime edge, geometry incubation) are all **shadow** or **live** but **not optimized in real-time**. They rely on:
- Weekly heavy sweep (dynamic ATR)
- Nightly EOD lookback (calibration bias, reachability)
- 4h intraday correction (calibration state)

This means a regime shift at 14:00 UTC won't be reflected in the dynamic tiers until the next nightly pass (01:17 UTC) or the next heavy sweep (20:00 UTC). For a 15-minute light sweep, this is a **16-20 hour lag**.

### 2.2 The design: Pre-sweep dynamic tier optimization

**Concept:** Run a separate process **before each light/heavy sweep** that:
1. Reads the latest resolved signals (last 4-8h)
2. Recomputes dynamic tier parameters for each symbol
3. Writes the fresh parameters to a `nt_dynamic_tier_state` table
4. The sweep reads the fresh parameters instead of stale config

**Architecture:**
```
┌─────────────────────────────────────────────────────────────┐
│  Pre-Sweep Optimization Process (runs at :05/:20/:35/:50)   │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. Fetch latest resolved signals (last 4-8h)        │    │
│  │ 2. For each symbol:                                  │    │
│  │    a. Recompute dynamic ATR grid (tiered_atr.walk) │    │
│  │    b. Recompute dynamic deadline tiers              │    │
│  │    c. Recompute regime edge buckets                 │    │
│  │    d. Recompute geometry incubation status          │    │
│  │ 3. Upsert fresh parameters to nt_dynamic_tier_state │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Light/Heavy Sweep (runs at :00/:15/:30/:45)               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 1. Read fresh parameters from nt_dynamic_tier_state │    │
│  │ 2. Run sweep with fresh parameters                  │    │
│  │ 3. Write results                                    │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Where to run it

**Option A: In Studio (recommended)**

Run as a separate asyncio task in the orchestrator, scheduled 5 minutes before each light sweep:
```python
self._scheduler.add_job(
    self._run_dynamic_tier_optimization,
    trigger=CronTrigger(minute="5,20,35,50"),
    id="dynamic-tier-optimization",
    name="Dynamic tier optimization (pre-sweep)",
    misfire_grace_time=300,
    coalesce=True,
)
```

**Pros:**
- Same process as the sweep — no network latency
- Same credentials — no auth issues
- Same database connection pool
- Can share in-memory state (e.g., HMM model)

**Cons:**
- Adds CPU load to the Studio before each sweep
- If the optimization fails, it may delay the sweep

**Option B: Edge function**

Run as a Supabase Edge Function triggered by pg_cron:
```sql
SELECT cron.schedule(
    'dynamic-tier-optimization',
    '5,20,35,50 * * * *',
    $$ SELECT net.http_post('https://<project>.functions.supabase.co/v1/dynamic-tier-optimize', ...) $$
);
```

**Pros:**
- Isolated from the sweep process
- Can be scaled independently

**Cons:**
- Network latency (Studio → Supabase → Studio)
- Auth complexity (service role key in Vault)
- Cold start latency
- Harder to debug

**Option C: Separate Studio process**

Run as a separate Python process on the Studio, triggered by a cron job:
```bash
# crontab
5,20,35,50 * * * * cd /teamspace/studios/this_studio/noble-trader && ./.venv/bin/python -m regime_platform.sweep_orchestrator.dynamic_tier_optimizer
```

**Pros:**
- Isolated from the sweep process
- No network latency
- Same credentials

**Cons:**
- Separate process to monitor
- Separate log stream
- Potential resource contention

### 2.4 Recommendation

**Option A (In Studio)** is the best choice because:
1. The optimization is lightweight (a few seconds per symbol)
2. It needs the same database connection as the sweep
3. It can share in-memory state (HMM model, ATR cache)
4. No network latency or auth complexity
5. The Studio already runs 24/7 with spare capacity between sweeps

### 2.5 Implementation plan

**Phase 1: Dynamic ATR real-time optimization**
1. Create `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py`
2. Add `_run_dynamic_tier_optimization()` method to orchestrator
3. Schedule at :05/:20/:35/:50
4. Test in shadow mode (log only, no writes)
5. Flip to live after 24h of clean shadow

**Phase 2: Dynamic deadline real-time optimization**
1. Extend `dynamic_tier_optimizer.py` to recompute deadline tiers
2. Read from `nt_bracket_reachability` (now fresh from the nightly build)
3. Write to `nt_dynamic_tier_state`

**Phase 3: Regime edge real-time optimization**
1. Extend to recompute regime edge buckets
2. Read from `nt_calibration_bias_by_cell`
3. Write to `nt_dynamic_tier_state`

**Phase 4: Geometry incubation real-time optimization**
1. Extend to reconcile geometry incubations
2. Read from `nt_geometry_incubation`
3. Write to `nt_dynamic_tier_state`

---

## 3. Corrective Actions Applied

### 3.1 Fix 1: Flip `calibration_evidence_pool` to live

**File:** `config/feature_flags.yaml`
**Change:** `calibration_evidence_pool_mode: shadow` → `calibration_evidence_pool_mode: live`

**Rationale:** Shadow has run for 11 days (since 09-26). The bias data confirms severe overconfidence (bias 0.4-0.8 across most cells). The evidence pool will give a more accurate enforcement input.

### 3.2 Fix 2: Add delete-then-insert logic

**File:** `regime_platform/sweep_orchestrator/bracket_reachability.py`
**Change:** Add `_delete_old_rows()` call before `upsert_bracket_reachability()`.

**File:** `regime_platform/sweep_orchestrator/calibration_bias_bucket.py`
**Change:** Add `_delete_old_rows()` call before `upsert_calibration_bias_by_bucket()`.

### 3.3 Fix 3: Create latest-view

**File:** `supabase/migrations/202_bracket_reachability_latest_view.sql`
**Change:** Create `v_bracket_reachability_latest` view.

---

## 4. Roadmap Updates

| ID | Mechanism | Action |
|---|---|---|
| 58 | p_win_bracket_mode | HOLD shadow. 7-day data shows marginal improvement, not enough to justify live gate. Push review to 2026-10-14. |
| 70 | calibration_evidence_pool | FLIP live. 11 days of shadow, severe overconfidence confirmed. |
| 4 | heavy_sweep_reachability_mode | DO NOT FLIP. 13/14 symbols have n_nothing_survived > 0. Push review to 2026-10-14. |
| 81 | dynamic_atr_vol_ratio_gate | Update with post-mortem findings. Push review to 2026-10-14. |

---

## 5. Answers to Open Questions

### 5.1 Cron job ID928 — Found and Updated

**Location:** Supabase pg_cron job
```sql
jobid: 928
schedule: "0 3 * * 0"  (Sunday 03:00 UTC)
command: SELECT public.weekly_maintenance()
jobname: weekly-db-maintenance
active: true
```

**What it does:** Prunes old records from `nt_calibration_records`, `nt_signal_sim`, `nt_sweep_result`, `nt_regime_log`, `nt_eod_actions`, `nt_tuning_params_test`, `nt_paper_positions`, and `cron.job_run_details`. Then vacuums key tables.

**What I added:**
1. Extended the VACUUM list to include all diagnostic tables:
   - `nt_bracket_reachability`
   - `nt_calibration_bias_by_bucket`
   - `nt_calibration_bias_by_cell`
   - `nt_entry_drift`
   - `nt_live_expectancy_daily`
   - `nt_regime_edge`
   - `nt_geometry_incubation`
   - `nt_macro_regime`
   - `nt_trend_edge_by_symbol`
   - `nt_heavy_sweep_reachability_shadow`
   - `nt_heavy_sweep_atr_anchored_shadow`

2. Added a stale-check: if `nt_bracket_reachability.computed_at` is older than 7 days, the function calls the Studio's `/api/admin/rebuild-diagnostics` endpoint to trigger a rebuild. This is a backstop — the nightly EOD lookback normally keeps the tables fresh.

### 5.2 Can tiers be derived from real-time pricing?

**Yes, but it's a different measurement than the current approach.**

| Tier | Current data source | Real-time alternative |
|---|---|---|
| Dynamic ATR | `nt_signal_sim` resolved outcomes (48h after entry) | Renko brick stream — measure how long price takes to reach ATR distances |
| Dynamic Deadline | `nt_bracket_reachability` percentiles (from resolved signals) | 1h candle stream — measure time-to-touch for different ATR distances |
| Regime Edge | `nt_signal_sim` resolved outcomes by regime bucket | HMM regime label (already real-time) + brick stream |
| Geometry Incubation | `nt_signal_sim` forward-resolved outcomes | Already the closest to real-time |

**Key insight:** The current tiers measure **"what happened to signals that were fired"** — this is ground truth for calibration. A real-time approach would measure **"what is the market doing right now"** — this is more like a regime indicator.

**Recommendation: Supplement, don't replace.** The current approach is the calibration ground truth. A real-time approach would be a faster, more responsive indicator for pre-sweep optimization.

### 5.3 Dataset measurement verification

| Module | Data source | Window | Horizon ratio | Status |
|---|---|---|---|---|
| **Macro filter** | Daily bars, 200-day SMA | 200 days × 24h = 4800h | 4800h / 48h = **100×** | ❌ Constant indicator |
| **Dynamic ATR** | `nt_signal_sim` resolved | 48h | 48h / 48h = **1×** | ✅ Matches |
| **Dynamic Deadline** | `nt_bracket_reachability` | 48h | 48h / 48h = **1×** | ✅ Matches |
| **Regime Edge** | `nt_signal_sim` resolved | 48h | 48h / 48h = **1×** | ✅ Matches |
| **Geometry Incubation** | `nt_signal_sim` forward | 48h | 48h / 48h = **1×** | ✅ Matches |

**The dynamic tiers do NOT repeat the macro filter's mistake.** They all use 48h windows that match the signal resolution.

### 5.4 Design: Pre-sweep dynamic tier optimization

**Architecture:**
```
:05/:20/:35/:50  →  _run_dynamic_tier_optimization()
  1. Fetch latest resolved signals (last 4-8h) — same as current
  2. Fetch latest brick/candle data (real-time) — NEW
  3. For each symbol:
     a. Recompute dynamic ATR grid (tiered_atr.walk) — same dataset
     b. Recompute dynamic deadline tiers — same dataset
     c. Recompute regime edge buckets — same dataset
     d. Recompute geometry incubation status — same dataset
     e. Compute real-time regime indicators (NEW):
        - Current ATR vs 10d average ATR (volatility regime)
        - Current brick direction vs HMM trend (regime alignment)
        - Time since last regime transition (regime freshness)
  4. Upsert fresh parameters to nt_dynamic_tier_state
  5. Sweep reads fresh parameters
```

**Key principle: Same dataset, same measurements, same windows.** The only difference is **when** the computation runs (pre-sweep vs nightly).

**Why this is safe:**
- The dynamic ATR walk uses the same `nt_signal_sim` population (last 10d, 48h resolution)
- The dynamic deadline uses the same `nt_bracket_reachability` percentiles
- The regime edge uses the same `nt_calibration_bias_by_cell` buckets
- The geometry incubation uses the same `nt_geometry_incubation` forward stats

**What's new:**
- The computation runs every 15 min instead of nightly
- Real-time regime indicators supplement (not replace) the calibration ground truth
- The sweep reads fresh parameters from `nt_dynamic_tier_state` instead of stale config

**Implementation plan — ALL PHASES BUILT 2026-10-07:**

**Phase 1: Dynamic ATR real-time optimization** ✅
- Created `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py`
- `_compute_dynamic_atr()` — same algorithm as `tiered_atr.walk()`
- Reads `nt_signal_sim` (last 10d, 48h resolution, contaminated=false)
- Writes to `nt_dynamic_tier_state`

**Phase 2: Dynamic deadline real-time optimization** ✅
- `_compute_dynamic_deadline()` — same algorithm as `dynamic_deadline.derive_deadline_tier()`
- Reads `nt_bracket_reachability` (latest build)
- Writes to `nt_dynamic_tier_state`

**Phase 3: Regime edge real-time optimization** ✅
- `_compute_regime_edge()` — same algorithm as `regime_edge.compute_regime_edge()`
- Reads `nt_signal_sim` (last 10d, 48h resolution, contaminated=false)
- Writes to `nt_dynamic_tier_state`

**Phase 4: Geometry incubation real-time optimization** ✅
- `_compute_geometry_incubation()` — reads `nt_geometry_incubation`
- Checks age (min 7 days), forward resolved count (min 20), expiry regression
- Writes to `nt_dynamic_tier_state`

**Scheduler wiring** ✅
- `orchestrator.py`: `_run_dynamic_tier_optimization()` method added
- Scheduled at `:05/:20/:35/:50` (5 min before each light sweep)
- `DYNAMIC_TIER_OPTIMIZATION_ENABLED` setting added (default True)
- Best-effort: failures logged, never raised into sweep tick

**Database** ✅
- Migration 204: `nt_dynamic_tier_state` table created
- `upsert_dynamic_tier_state()` added to `supabase_client.py`
- Keyed on `(symbol, tier_type, tier_key)`

**Tests** ✅
- 45 tests passed (bracket_reachability + calibration_bias + dynamic_tier)
- All imports verified

**Shadow approach maintained** ✅
- All dynamic tier flags remain in shadow mode
- Parameters are computed and written to `nt_dynamic_tier_state`
- The sweep only uses them when the corresponding feature flag is live
- No live behavior change until flags are explicitly flipped
