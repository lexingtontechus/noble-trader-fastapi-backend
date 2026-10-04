# 2026-10-04 — Geometry Band Calibration Urgency: Scope & Design

**Roadmap ID:** 56 (companion to `qualified_quality_addons_D1_D2`)
**Mechanism:** `geometry_band_calibration_urgency`
**Status:** Scoped — not built
**Date:** 2026-10-04
**Analyst:** Hermes Agent

---

## 1. Problem

Geometry bands (`nt_bracket_reachability`) serve **two distinct purposes**:

| Purpose | Refresh cadence | Current process |
|---|---|---|
| **Weekly TP/SL re-optimization** | Weekly | `bracket_reachability.py` (full rebuild) |
| **Per-signal calibration input** | **Every signal** | `p_win_dir = p_resolve × p_win\|resolve` per bucket |

For purpose #2, the reachability data is a **live calibration input** — every new signal's `p_win_dir` is computed from the current bucket statistics. If the market regime shifts and the reachability data is stale, **every new signal's calibration is wrong** until the weekly refresh.

### The gap

- `calibration_bias_bucket.py` runs every 4h and recomputes bias per bucket
- But it uses the **existing** reachability table — if the table is stale, the bias computation is stale
- There is no process to detect "this bucket's reachability has drifted" and trigger a **targeted re-computation**

### Why ATR has urgency but geometry bands don't

| Feature | ATR | Geometry bands |
|---|---|---|
| Input | Recent 24h price | Hundreds of historical signals |
| Change speed | Hours | Weeks (for TP/SL) |
| **Calibration role** | TP/SL geometry | **p_win_dir per signal** |
| **Urgency need** | Yes (fast variable) | **Yes (stale calibration input)** |

The key insight: **geometry bands are slow for TP/SL optimization but fast for calibration drift**. A regime shift can make the current reachability statistics stale within hours, not weeks.

---

## 2. Design

### 2.1 Trigger: per-bucket calibration bias

The urgency process monitors `nt_calibration_bias_by_bucket` (already computed by `calibration_bias_bucket.py` every 4h):

```
IF |bias_shrunk| > urgency_threshold (default 0.15)
AND bucket has ≥ min_n (default 20) resolved signals
AND last_recomputed_at > min_age (default 4h)
THEN trigger targeted re-computation for that (symbol, vol_tier, bucket)
```

### 2.2 Action: targeted re-computation

Unlike the weekly full rebuild, the urgency process only recomputes **affected buckets**:

1. Fetch resolved `nt_signal_sim` rows for the specific `(symbol, vol_tier, bucket)`
2. Recompute `p_resolve` and `p_win|resolve` for that bucket
3. Upsert into `nt_bracket_reachability` (same schema as weekly rebuild)
4. Log the action to `nt_eod_actions` (`geometry_band_urgency` action type)

### 2.3 Scope: not a full rebuild

| Process | Scope | Frequency | Cost |
|---|---|---|---|
| Weekly rebuild | All symbols, all buckets | Weekly | High (full table) |
| **Urgency re-computation** | **Affected buckets only** | **4h check** | **Low (targeted)** |

The urgency process is a **fast path** between scheduled weekly rebuilds — it only fires when a bucket's calibration has drifted beyond the threshold.

---

## 3. Implementation Sketch

### 3.1 New module: `geometry_band_urgency.py`

```python
async def check_geometry_band_urgency() -> dict[str, Any]:
    """4h urgency check for geometry band calibration drift.
    
    1. Read nt_calibration_bias_by_bucket (already computed by 4h EOD pass)
    2. Filter buckets where |bias_shrunk| > threshold and n >= min_n
    3. For each affected bucket, recompute reachability from resolved signals
    4. Upsert into nt_bracket_reachability
    5. Log to nt_eod_actions (geometry_band_urgency)
    
    Returns: {enabled, checked, triggered, recomputed, errors}
    """
```

### 3.2 Config: `feature_flags.yaml`

```yaml
geometry_band_urgency:
  enabled: true
  urgency_threshold: 0.15      # |bias_shrunk| threshold
  min_n: 20                     # minimum resolved signals per bucket
  min_age_hours: 4              # minimum age before re-computation
  cooldown_hours: 4             # per-bucket cooldown
```

### 3.3 Cron: 4h urgency check

Register alongside the existing 4h EOD correction passes:

```python
# In orchestrator.py _schedule_jobs:
self._scheduler.add_job(
    self._run_geometry_band_urgency,
    trigger=CronTrigger(hour="*/4", minute="35"),  # 4h at :35
    name="Geometry band calibration urgency (4h)",
)
```

### 3.4 State: `nt_roadmap`

Track the urgency process in `nt_roadmap`:

```sql
INSERT INTO nt_roadmap (mechanism, current_state, review_at, ...)
VALUES (
  'geometry_band_calibration_urgency',
  'shadow: monitoring per-bucket bias, targeted re-computation on drift',
  '2026-10-11 00:00:00+00',
  ...
);
```

---

## 4. Comparison to ATR Urgency

| Aspect | ATR Urgency | Geometry Band Urgency |
|---|---|---|
| **Trigger** | `vol_ratio > elevated_ratio` | `\|bias_shrunk\| > threshold` |
| **Scope** | Full free lattice re-optimization | Targeted bucket re-computation |
| **Action** | Re-optimize TP/SL for symbol | Recompute `p_resolve` / `p_win\|resolve` for bucket |
| **Output** | New TP/SL clamps | Updated `nt_bracket_reachability` row |
| **Frequency** | 4h check, 4h cooldown | 4h check, 4h cooldown |
| **State** | `nt_roadmap(atr_reopt_urgency)` | `nt_roadmap(geometry_band_calibration_urgency)` |

The geometry band urgency is **simpler** than ATR urgency:
- No free lattice optimization (just recompute statistics)
- No Discord alert (silent re-computation, logged to `nt_eod_actions`)
- No clamps to apply (just update the reachability table)

---

## 5. Why This Is Safe

1. **Read-only trigger** — the urgency process only reads `nt_calibration_bias_by_bucket` (already computed by the 4h EOD pass)
2. **Targeted action** — only recomputes affected buckets, not the full table
3. **Same schema** — uses the same `upsert_bracket_reachability` function as the weekly rebuild
4. **Fail-open** — any error is logged and never raised into the sweep tick
5. **Shadow mode** — ships in shadow mode, zero live behavior change until promoted

---

## 6. Open Questions

1. **Threshold calibration** — is 0.15 the right `|bias_shrunk|` threshold? Need to observe the distribution of bucket biases over 7 days before tuning.
2. **Min n** — is 20 the right minimum? Too low → noisy re-computation; too high → misses small buckets.
3. **Cooldown** — is 4h the right cooldown? Too short → thrashing; too long → stale data persists.
4. **Action type** — should this write to `nt_eod_actions` or a separate audit table? The existing `nt_eod_actions` CHECK constraint may need extension.

---

## 7. Next Steps

1. **Observe** — run `calibration_bias_bucket.py` for 7 days, collect per-bucket bias distribution
2. **Tune** — set `urgency_threshold`, `min_n`, and `cooldown_hours` based on observed data
3. **Build** — implement `geometry_band_urgency.py` (targeted re-computation)
4. **Wire** — register 4h cron job in orchestrator
5. **Shadow** — run in shadow mode for 7 days, verify triggered re-computations are correct
6. **Promote** — flip to live after shadow validation

---

## 8. Related

- `worklog/20261004_GEOMETRY_GATE_SHADOW_ANALYSIS.md` — geometry gate shadow analysis (ID 56)
- `worklog/20261004_RANK_SCORE_ANALYSIS_AND_CALIBRATION_SCOPE.md` — rank score calibration feedback (ID 76)
- `regime_platform/sweep_orchestrator/calibration_bias_bucket.py` — 4h bias computation
- `regime_platform/sweep_orchestrator/bracket_reachability.py` — weekly reachability rebuild
- `regime_platform/sweep_orchestrator/atr_reopt.py` — ATR urgency (reference implementation)
- `supabase/migrations/152_bracket_reachability_vol_tier.sql` — reachability schema
- `supabase/migrations/162_calibration_bias_by_cell.sql` — bias by cell schema
