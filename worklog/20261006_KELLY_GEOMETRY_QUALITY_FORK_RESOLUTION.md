# ID 78 — Kelly/Geometry Quality Fork: Decision & Build Log

**Date:** 2026-10-06  
**Roadmap ID:** 78 (`kelly_geometry_quality_fork`)  
**Status:** resolved — binary Kelly gate rejected; geometry band urgency drift loop recommendation  
**Supersedes:** `worklog/20260927_PULSE_ROADMAP_DECISIONS_AND_QUALITY_FORK.md` §1, `worklog/20260927_CHANGELOG_EV_ENGINE_KELLY_GATE.md`

---

## 1. Original problem (2026-09-27)

Two quality problems, neither fixed by cooldown:

| Problem | Evidence | Root cause |
|---|---|---|
| **6a. Kelly = 0 blocks publishes** | 61% of directional scans had `kelly_f = 0` (24h pulse) | Historical-mu Kelly (recent price mean/variance) disagrees with p_win/EV (model blend). A gold pair prints `p_win 0.75`, `EV +3.5`, then `kelly = 0` → `kelly_min_ok` fails. |
| **6b. Expiry rate too high** | 56.8% of settled trades expired (7d) | Bracket geometry (TP/SL distance) wrong for how price actually moves. Signals fade instead of resolving. |

The user's framing: *"quantity cut, quality not up."* The Kelly gate was asking a price-history sizer for permission after the model already liked the trade.

---

## 2. What was built (2026-09-27)

### 2.1 Historical Kelly gate removed

`kelly_min_ok` removed from `gate_results` in `orchestrator.py`. The gate was:

```python
# BEFORE (removed)
"kelly_min_ok": effective_kelly >= MIN_KELLY,  # MIN_KELLY = 0.02
```

Historical Kelly (`PositionSizer` mu/sigma²) is still computed and stored (`kelly_f`, `effective_kelly`) for **research only**. It no longer vetoes publication.

### 2.2 Variable binary Kelly (shadow)

`variable_kelly.py` built. Shadow-only sizing research:

```
f*  = (p_win·b − (1−p_win)) / b     b = TP/SL reward-risk
m   = context(event_window, transition_type, vol_tier)   # never age
size_shadow = f* × 0.5 × m
```

Context multipliers (frozen from Kairos R1 event-time contrasts):

| Context | Values |
|---|---|
| **Freshness** | fresh=1.12, warm=1.00, pre=0.95, stale=0.88, deep=0.85 |
| **Transition type** | dir_flip=1.12, both=1.06, none=1.00, vol_expand=1.02, vol_contract=0.98 |
| **Vol tier** | low_vol=1.05, med_low=1.00, med_high=0.98, high_vol=0.92 |

Clipped to [0.70, 1.35]. Columns on `nt_signal_sim` (migration 174): `kelly_binary_f`, `kelly_context_mult`, `kelly_effective_shadow`. **Never read back into qualification or live size.**

### 2.3 Geometry band urgency (live)

`geometry_band_urgency.py` built and wired into `_sweep_group`. Re-computes per-cell reachability stats when calibration bias drifts. See `worklog/20261005_GEOMETRY_BAND_URGENCY_DEFECTS_AND_BUILD.md` for the full defect record (6 defects, 4 introduced in-build).

---

## 3. 2026-10-06 pulse check — binary Kelly gate decision

### 3.1 The question

Should we switch the Kelly gate from "historical Kelly" (removed 2026-09-27) to "binary Kelly" (p_win × RR)?

### 3.2 What binary Kelly would do as a gate

```
f* = (p_win × RR − (1−p_win)) / RR
gate: f* >= MIN_KELLY_BINARY
```

This uses the signal's own p_win and the bracket's RR to compute position size, then gates on that size. The idea: if the model says p_win=0.60 and the bracket is RR=2:0, Kelly says bet X% — if X% is too small, don't publish.

### 3.3 Why it was rejected

1. **It zeroed 61% of directional scans in testing.** The current gate set (p_win/EV/regime/TimesFM) already filters signals well. Adding binary Kelly as a gate would reject most signals that pass the current gates — too aggressive.

2. **Kelly is sizing, not gating.** The current design separates concerns:
   - **Gates** decide IF a signal fires (p_win, EV, regime confidence, TimesFM)
   - **Sizing** decides HOW BIG (Kelly formula, calibration adjustment, rank score)
   - Binary Kelly mixes these — it uses a sizing formula as a quality gate

3. **The variable binary Kelly stamp** (`kelly_binary_f`) remains shadow-only for research. It's stamped on `nt_signal_sim` but never read back into a decision.

### 3.4 Recommendation

**Keep the current gate set. Binary Kelly is sizing research, not a gate.** No code change needed.

The current 7-gate chain is:

| Gate | Threshold | Purpose |
|---|---|---|
| `signal_qualifiable` | buy/sell | Direction exists |
| `regime_conf_ok` | >= 0.60 | HMM confident |
| `timesfm_available` | not None | Forecast exists |
| `timesfm_confidence_ok` | >= 0.05 | Forecast confident |
| `ev_allowed` | ev > 0 AND rr >= 1.5 | Positive expectancy |
| `p_win_ok` | >= 0.55 (or >= 0.20 when `p_win_in_ev_mode=live`) | Model confident |
| `regime_edge_ok` | WR >= 0.50 (shadow) | Regime has edge |

Binary Kelly would add a 8th gate that duplicates the p_win/EV check with a different formula. It would reject signals that already passed the model's own confidence assessment.

---

## 4. Geometry band urgency — drift loop recommendation

### 4.1 The problem

The urgency job reads `nt_calibration_bias_by_cell` to detect drift, recomputes `nt_bracket_reachability`, but nothing recomputes the bias table from the refreshed reachability. The trigger never clears — the same cell re-fires every 4h cooldown (up to 6×/day/cell) for the rest of the day.

Verified live: `nt_calibration_bias_by_cell.computed_at = 2026-10-05T01:18:58Z`, `nt_bracket_reachability.computed_at = 2026-10-05T01:18:44Z` — both from the 01:17 pass. A drift detected at 02:00 has its reachability refreshed, but the bias reading that triggered it stays stale until 01:17 the next day.

### 4.2 Why options 1+2 are off the table

- **Option 1** (recompute bias per-cell): The bias builder rebuilds all 231 rows for 14 symbols — it's not designed for per-cell recomputation. "Reachability is an issue."
- **Option 2** (widen cooldown to 24h): Too long. The whole point of the urgency process is to refresh calibration inputs quickly.

### 4.3 Recommendation: Option 3 — drop the bias table as the trigger

Detect drift directly from the reachability row's own history. The `nt_bracket_reachability` table already has `pct_hit_tp`, `pct_hit_sl`, `pct_expired`, `median_hours_to_resolve`, `p75_hours`, `p90_hours`. When the urgency job recomputes a cell, it can compare the new stats against the old stats in the same row. If the change is significant (e.g., `|Δpct_hit_tp| > 0.10` or `|Δmedian_hours| > 6h`), the drift is real and the refresh was worthwhile. If not, the cell was already stable.

This approach:
- **Removes the dependency** on `nt_calibration_bias_by_cell`'s refresh cadence
- **Is self-contained** — the reachability table is both the trigger and the target
- **Converges** — once the reachability stats stabilize, the trigger stops firing
- **Is cheap** — no additional Supabase reads or writes

### 4.4 Implementation sketch

Add a `previous_stats` JSONB column to `nt_bracket_reachability` (or use a separate `nt_urgency_state` table). On each re-computation:

```python
# Pseudocode
old_stats = row.get("previous_stats") or {}
new_stats = _recompute_cell(rows, side, vol_tier, bucket)

if old_stats:
    delta_tp = abs(new_stats["pct_hit_tp"] - old_stats.get("pct_hit_tp", 0))
    delta_median = abs(new_stats["median_hours_to_resolve"] - old_stats.get("median_hours_to_resolve", 0))
    if delta_tp < 0.10 and delta_median < 6.0:
        # Cell is stable — skip
        continue

# Drift is real — upsert and log
await upsert_bracket_reachability([{...}])
await _log_eod_action(symbol, {...}, bias=new_stats["bias_shrunk"])
```

### 4.5 Migration

```sql
-- Add previous_stats JSONB column to nt_bracket_reachability
ALTER TABLE nt_bracket_reachability ADD COLUMN IF NOT EXISTS previous_stats JSONB;

-- Backfill: copy current stats into previous_stats for all rows
UPDATE nt_bracket_reachability
SET previous_stats = jsonb_build_object(
    'pct_hit_tp', pct_hit_tp,
    'pct_hit_sl', pct_hit_sl,
    'pct_expired', pct_expired,
    'median_hours_to_resolve', median_hours_to_resolve,
    'p75_hours', p75_hours,
    'p90_hours', p90_hours
)
WHERE previous_stats IS NULL;
```

---

## 5. Summary

| Item | Decision | Status |
|---|---|---|
| Historical Kelly gate | Removed (2026-09-27) | Live |
| Variable binary Kelly | Shadow-only stamp | Live (research) |
| Binary Kelly as gate | **Rejected** — too aggressive, duplicates p_win/EV | Resolved |
| Geometry band urgency drift loop | **Option 3** — detect drift from reachability row's own history | Recommendation |

---

## 6. Related

- `worklog/20260927_PULSE_ROADMAP_DECISIONS_AND_QUALITY_FORK.md` — original problem statement
- `worklog/20260927_CHANGELOG_EV_ENGINE_KELLY_GATE.md` — Kelly gate removal changelog
- `worklog/20261005_GEOMETRY_BAND_URGENCY_DEFECTS_AND_BUILD.md` — urgency defect record
- `regime_platform/sweep_orchestrator/variable_kelly.py` — variable binary Kelly implementation
- `regime_platform/sweep_orchestrator/geometry_band_urgency.py` — urgency job implementation
- `regime_platform/sweep_orchestrator/geometry_risk.py` — geometry risk metrics