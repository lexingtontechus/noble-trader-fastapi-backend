# Worklog: Clean Baseline Cutover — Removing Contaminated Data & Establishing New Benchmark

**Date:** 2026-10-07
**Scope:** Data contamination audit, cutover establishment, clean baseline
**Related:** `worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md`, `supabase/migrations/192_calibration_cutover.sql`

---

## 1. Problem

The calibration system has accumulated **five distinct contamination events** since 2026-08-11. Each event changed the meaning of historical data, making it incompatible with current measurements. The existing cutover mechanism (migration 192) was designed for the 2026-10-02 intrabar flip but was **never activated** — `nt_calibration_cutover` is empty, so all calibration reads still mix pre- and post-intrabar rows.

This means every calibration number computed today — bias, Brier, expectancy, enforcement actions — is a **blend of incompatible bases**. The 7-day rolling window contains:
- Pre-intrabar rows (close-walk basis): TP/SL resolved by 1h close
- Post-intrabar rows (intrabar basis): TP/SL resolved by high/low touch

These are **different events**. A `tp_hit` under close-walk is not the same as `tp_hit` under intrabar. Mixing them corrupts every downstream metric.

---

## 2. Contamination Event Audit

| # | Date | Event | Impact | Records affected | Status |
|---|---|---|---|---|---|
| 1 | 2026-08-11 | TDVA price ordering bug (`history` DESCENDING) | Stale prices fed HMM → wrong regime labels → wrong p_win | 14,248 calibration records | ✅ Purged (2026-08-13) |
| 2 | 2026-08-19 | EV engine fix (p_regime cap + Markov degenerate) | Pre-fix: p_regime=1.0 → logit(1.0)=34.5 dominated blend | All `nt_calibration_records` with `entered_at < 2026-08-19T00:26:52Z` | ✅ Purged via `scripts/purge_stale_calibration.py` |
| 3 | 2026-09-13→14 | Shadow-composition leak | Reachability/ATR/deadline shadow data self-contaminated for ~26h | Shadow tables only (not calibration records) | ✅ Fixed 2026-09-14; shadow data excluded from go/no-go |
| 4 | 2026-10-02 | `eod_resolver_basis` flipped `close_walk` → `intrabar` | **Outcome semantics changed**: `tp_hit` now means "intrabar high/low touched TP first" instead of "1h close passed TP" | All `nt_signal_sim` rows resolved before 2026-10-02 | ⚠️ **NOT excluded** — cutover mechanism exists but was never activated |
| 5 | 2026-10-03 | Weekend stale-price window | TDVA returned last Friday close unchanged → mass SL hits (artifact, not real calibration failure) | ~484 qualified rows on 2026-10-03 | ⚠️ **NOT excluded** — weekend batch is an artifact |

### Event 4 is the critical one

The 2026-10-02 intrabar flip is the **most impactful** contamination event because:
- It changes the **meaning** of `tp_hit`/`sl_hit`/`expired` for all future resolutions
- The survivable base rate moved from 0.2728 (close-walk) to 0.057–0.069 (intrabar)
- The intrabar bracket rate is 0.0265 lower
- Every calibration number computed from mixed data is **meaningless**

The cutover mechanism (migration 192) was built exactly for this, but the operator never set the timestamp. The flag `calibration_cutover_mode: on` is set, but the view returns ALL rows when `nt_calibration_cutover` is empty.

### Event 5 is a separate issue

The 2026-10-03 weekend batch is not a calibration failure — it's a **stale-price artifact**. The orchestrator swept on frozen Friday close data, signals fired, and the resolver resolved them against the same frozen prices → mass SL hits. These rows should be excluded from calibration reads (they tell us nothing about signal quality).

---

## 3. The Cutover Mechanism (Migration 192)

### What exists

```sql
-- Singleton table: one row, set once at deploy time
CREATE TABLE nt_calibration_cutover (
  id          INT PRIMARY KEY DEFAULT 1,
  cutover_at  TIMESTAMPTZ NOT NULL,
  basis       TEXT NOT NULL DEFAULT 'intrabar',
  note        TEXT,
  CONSTRAINT nt_calibration_cutover_singleton CHECK (id = 1)
);

-- View: only post-cutover records
CREATE VIEW v_calibration_records_current AS
  SELECT c.*
  FROM nt_calibration_records c
  WHERE NOT EXISTS (
    SELECT 1 FROM nt_calibration_cutover k
    WHERE COALESCE(s.resolved_at, c.entered_at) < k.cutover_at
  );
```

### What's missing

The table is **empty**. The flag is `on`, but the view returns all rows. To activate:

```sql
INSERT INTO nt_calibration_cutover (id, cutover_at, basis, note)
VALUES (1, '2026-10-02T00:00:00Z', 'intrabar', 'eod_resolver_basis flip deployed')
ON CONFLICT (id) DO UPDATE
  SET cutover_at = EXCLUDED.cutover_at,
      basis = EXCLUDED.basis,
      note = EXCLUDED.note;
```

### Why this matters

Once set:
- `fetch_calibration_bias` / `fetch_calibration_bias_raw` read `v_calibration_records_current` instead of the base table
- All pre-intrabar rows are excluded from bias/Brier/enforcement
- The 7-day rolling window starts fresh — it takes ~7 days to fill with clean post-intrabar data
- The pre-intrabar rows are **not deleted** — they remain for historical comparison

---

## 4. Clean Baseline Strategy

### 4.1 Set the cutover timestamp

**Action:** Set `nt_calibration_cutover.cutover_at = '2026-10-02T00:00:00Z'`

This excludes all pre-intrabar rows from calibration reads. The 7-day rolling window will fill with clean data by ~2026-10-09.

### 4.2 Exclude the weekend artifact batch

**Action:** Add a second exclusion for the 2026-10-03 weekend batch.

Options:
- **Option A:** Add a `contaminated` boolean column to `nt_signal_sim` and mark the weekend rows
- **Option B:** Add a second cutover-like table for artifact windows
- **Option C:** Delete the weekend rows (they're artifacts, not real signals)

**Recommendation: Option A** — add a `contaminated` column. This is the most flexible:
- The weekend rows are marked `contaminated = true`
- All calibration reads add `WHERE contaminated = false`
- Future contamination events can be marked the same way
- The rows are preserved for debugging

### 4.3 Establish a new benchmark date

**Benchmark date: 2026-10-09** (7 days after the cutover)

On 2026-10-09:
- The 7-day rolling window is 100% post-intrabar, post-weekend-artifact
- Calibration bias/Brier are computed from clean data only
- The first clean EOD correction pass runs
- The first clean enforcement actions fire

**Before 2026-10-09:**
- Calibration numbers are **provisional** — the window is still filling
- EOD correction actions are **advisory** — not enough clean data to enforce
- The system is in a **warm-up period**

### 4.4 Data to exclude from calibration reads going forward

| Exclusion | Reason | Mechanism |
|---|---|---|
| Pre-2026-10-02 (intrabar flip) | Different outcome semantics | `nt_calibration_cutover` |
| 2026-10-03 weekend batch | Stale-price artifact | `contaminated` column on `nt_signal_sim` |
| Pre-2026-08-19 (EV fix) | Broken logit pooling | Already purged |
| Pre-2026-08-11 (TDVA ordering) | Stale prices | Already purged |
| Shadow-contaminated 2026-09-13→14 | Self-contaminated shadow | Already excluded from go/no-go |

---

## 5. Implementation Plan

### Step 1: Set the cutover timestamp (immediate)

```sql
INSERT INTO nt_calibration_cutover (id, cutover_at, basis, note)
VALUES (1, '2026-10-02T00:00:00Z', 'intrabar', 'eod_resolver_basis flip deployed 2026-10-02; excludes all pre-intrabar calibration records')
ON CONFLICT (id) DO UPDATE
  SET cutover_at = EXCLUDED.cutover_at,
      basis = EXCLUDED.basis,
      note = EXCLUDED.note;
```

### Step 2: Add `contaminated` column to `nt_signal_sim` (migration 203)

```sql
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS contaminated BOOLEAN NOT NULL DEFAULT false;

-- Mark the 2026-10-03 weekend batch
UPDATE nt_signal_sim
SET contaminated = true
WHERE ts >= '2026-10-03T00:00:00Z'
  AND ts < '2026-10-04T00:00:00Z'
  AND source = 'sweep-light';

COMMENT ON COLUMN nt_signal_sim.contaminated IS
  'True for rows that are artifacts (stale prices, infrastructure outages, etc.) and should be excluded from calibration reads. Set by the operator when a contamination event is identified.';
```

### Step 3: Update calibration reads to exclude contaminated rows

All calibration reads add `WHERE contaminated = false`:
- `fetch_calibration_bias` / `fetch_calibration_bias_raw`
- `build_calibration_bias_by_bucket`
- `build_reachability_table`
- `build_entry_drift`
- `build_live_expectancy_daily`

### Step 4: Verify the cutover is active

```sql
-- Should return 1 row with cutover_at = 2026-10-02
SELECT * FROM nt_calibration_cutover;

-- Should return only post-intrabar records
SELECT count(*) FROM v_calibration_records_current;

-- Should return 0 (all pre-intrabar records excluded)
SELECT count(*) FROM v_calibration_records_current
WHERE entered_at < '2026-10-02T00:00:00Z';
```

### Step 5: Monitor the warm-up period (2026-10-02 → 2026-10-09)

- Daily: check `nt_calibration_cutover` is set
- Daily: check `v_calibration_records_current` row count is growing
- Daily: check `nt_signal_sim.contaminated` count is stable (no new contamination)
- 2026-10-09: first clean calibration read — bias/Brier are meaningful

---

## 6. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Cutover timestamp is wrong (too early/late) | The timestamp is a single tunable — can be updated with one `UPDATE` statement |
| Weekend artifact rows are not all captured | The `contaminated` column can be set for any future event — not just 2026-10-03 |
| 7-day warm-up period has too few records | The system already handles this — `min_n=20` for bias reads, fail-open when insufficient |
| Pre-intrabar rows are needed for comparison | They are **not deleted** — only excluded from calibration reads. Historical comparison is still possible. |
| The cutover breaks existing dashboards | The view `v_calibration_records_current` is a drop-in replacement — no schema change |

---

## 7. Decision — APPLIED 2026-10-07

### 7.1 Database changes (confirmed)

| Change | Status | Evidence |
|---|---|---|
| Set `nt_calibration_cutover.cutover_at = '2026-10-02T00:00:00Z'` | ✅ Applied | `SELECT * FROM nt_calibration_cutover` returns 1 row |
| Add `contaminated` column to `nt_signal_sim` | ✅ Applied | Migration 203 |
| Mark 2026-10-03 weekend batch as contaminated | ✅ Applied | 1,047 rows marked |
| Update `v_calibration_records_current` to exclude contaminated | ✅ Applied | View updated |
| Update `v_calibration_records_tiered` to exclude contaminated | ✅ Applied | View updated |
| Update `bracket_reachability.py` to filter contaminated | ✅ Applied | `contaminated=eq.false` added |
| Update `calibration_bias_bucket.py` to filter contaminated | ✅ Applied | `contaminated=eq.false` added |

### 7.2 Confirmed data state

```
nt_calibration_records:  50,048 total
  ├── post-cutover (clean):   5,355  (10.7%)
  └── pre-cutover (excluded): 44,693  (89.3%)

nt_signal_sim contaminated:  1,047 rows
  └── reason: "weekend stale-price artifact: TDVA returned last Friday close unchanged"
```

### 7.3 Consequences

- Calibration numbers from 2026-10-02 to 2026-10-09 are **provisional** (warm-up)
- EOD correction actions during warm-up are **advisory**, not enforced
- After 2026-10-09, the system is **fully clean** and all enforcement is active
- The pre-intrabar history is **preserved** but excluded from calibration reads
- The 1,047 contaminated rows are **preserved** but excluded from calibration reads

### 7.4 Clean benchmark date: 2026-10-09

On 2026-10-09:
- The 7-day rolling window is 100% post-intrabar, post-weekend-artifact
- Calibration bias/Brier are computed from clean data only
- The first clean EOD correction pass runs
- The first clean enforcement actions fire

---

## 8. Open Questions

1. **Should the 2026-10-03 weekend batch be deleted or just marked?**
   - Marking is safer (preserves debugging evidence)
   - Deleting is cleaner (no risk of accidentally including them)
   - Recommendation: mark, don't delete

2. **Should there be a `contaminated_reason` column?**
   - Would help debugging (why was this row marked?)
   - Recommendation: yes, add `contaminated_reason TEXT`

3. **What about the `nt_calibration_records` table?**
   - Should it also have a `contaminated` column?
   - Or is the `nt_signal_sim.contaminated` flag sufficient (joined on `signal_id`)?
   - Recommendation: `nt_signal_sim.contaminated` is sufficient — the calibration record inherits the flag via the join

4. **Should the cutover be per-symbol or global?**
   - Global is simpler (one timestamp for all symbols)
   - Per-symbol would allow a symbol that was unaffected by the intrabar flip to keep its history
   - Recommendation: global — the intrabar flip affects all symbols uniformly
