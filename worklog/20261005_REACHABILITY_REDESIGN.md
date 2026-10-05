# Reachability Methodology Redesign

**Date:** 2026-10-05
**Status:** design — not yet built
**Supersedes:** the fixed-48h deadline + all-or-nothing bucket filter design

---

## 1. Problems with the current design

| # | Problem | Evidence |
|---|---|---|
| 1 | **Hard 48h deadline is uniform** — XAUUSD (high vol) and USDCAD (low vol) get the same clock, but their natural resolution speeds differ by 3–5× | `nt_bracket_reachability` shows XAUUSD median 12h vs USDCAD median 36h |
| 2 | **All-or-nothing bucket filter** — `reachability_aware_pick` uses `expectancy_r > 0` as a binary threshold. A bucket at +0.01 is treated identically to +2.0 | `heavy_sweep_reachability.py:reachability_aware_pick()` |
| 3 | **Fixed ATR buckets don't adapt to regime** — A `high_vol` regime and `low_vol` regime have very different natural TP distances, but the 5 fixed buckets (`<0.5, 0.5-1, 1-2, 2-4, 4+`) are regime-blind | `bracket_reachability.py:ATR_BUCKETS` |
| 4 | **Deadline filter and ATR geometry are separate stages** — ATR-anchoring picks the bracket, then deadline-aware scoring re-scores with a fixed 48h clock. They don't share information | `heavy_sweep.py` steps 5→5a |
| 5 | **No per-symbol deadline** — The 48h clock is global. But the data shows per-symbol resolution speed varies 3–5× | `nt_bracket_reachability` percentiles |
| 6 | **No expectation surfaced to user/admin** — A qualified signal fires with no indication of when it will resolve. The user sees "qualified" but not "resolves in ~12h" or "resolves in ~3d" | `nt_sweep_result` has no resolution expectancy column |
| 7 | **Macro filter is price-only** — `macro_filter.py` reads SMA only. The LLM narrative in `nt_macro_forecast` is computed but never consumed by the filter. Disagreement between price and narrative is invisible | `macro_filter.py` vs `macro_forecast_agent.py` |
| 8 | **ATR proxy backfill is look-ahead contaminated** — Historical rows use current ATR. `calibration_bias_bucket.py` excludes these (Batch B), but `bracket_reachability.py` still includes them in hit-rate buckets | `bracket_reachability.py:_current_atr_by_symbol()` |

---

## 2. Design principles

1. **The deadline is a distribution, not a constant.** Each (symbol, vol_tier) has a resolution speed distribution. The deadline should be derived from that distribution, not imposed uniformly.

2. **ATR geometry and deadline are the same question.** "Will this bracket resolve?" and "will it resolve in time?" are one question. The ATR bucket tells you the distance; the deadline tells you the time. They should be computed together.

3. **Regime drives everything.** Vol tier, ATR, and deadline are all regime-conditional. A `high_vol` regime has wider natural brackets AND faster resolution. A `low_vol` regime has tighter brackets AND slower resolution.

4. **Clef-Flash reads text, not price.** The macro forecast is text (thesis/drivers/risks). Clef-Flash can read that text and answer coherence questions the price-based filter cannot. It must never re-derive direction from price.

5. **Surface expectations, not just signals.** A qualified signal should carry its expected resolution time and confidence. The user/admin should see "resolves in ~12h (high confidence)" not just "qualified."

---

## 3. Component redesigns

### 3.1 Dynamic Deadline Tiers (replaces hard 48h)

**Concept:** Replace the uniform 48h clock with per-(symbol, vol_tier) deadline tiers derived from the reachability table's own percentiles.

```python
# regime_platform/sweep_orchestrator/dynamic_deadline.py (NEW)

@dataclass(frozen=True)
class DeadlineTier:
    """Per-(symbol, vol_tier) deadline configuration."""
    symbol: str
    vol_tier: str
    soft_deadline_hours: float   # p75 — full Sharpe credit
    hard_deadline_hours: float   # p90 — zero credit
    median_hours: float          # p50 — expected resolution
    n: int                       # sample size
    confidence: str              # high (n>=50), medium (n>=20), low (n<20)

    def decay(self, hours: float) -> float:
        """Sharpe decay factor for a trade resolving in `hours`."""
        if hours <= self.soft_deadline_hours:
            return 1.0
        if hours >= self.hard_deadline_hours:
            return 0.0
        # Linear decay between soft and hard
        span = self.hard_deadline_hours - self.soft_deadline_hours
        return 1.0 - (hours - self.soft_deadline_hours) / span
```

**Config:**
```yaml
# config/feature_flags.yaml
dynamic_deadline_mode: shadow  # off | shadow | live
dynamic_deadline:
  # Per-symbol overrides (same pattern as dynamic_atr by_symbol)
  by_symbol:
    XAUUSD: { soft_deadline_hours: 18, hard_deadline_hours: 36 }
    XAUEUR: { soft_deadline_hours: 18, hard_deadline_hours: 36 }
    XAUAUD: { soft_deadline_hours: 18, hard_deadline_hours: 36 }
    XAGUSD: { soft_deadline_hours: 18, hard_deadline_hours: 36 }
    USDCAD: { soft_deadline_hours: 36, hard_deadline_hours: 72 }
    USDJPY: { soft_deadline_hours: 24, hard_deadline_hours: 48 }
  # Fallback: derive from reachability table percentiles
  derive_from_reachability: true
  soft_deadline_percentile: 0.75
  hard_deadline_percentile: 0.90
  min_soft_deadline_hours: 12
  max_hard_deadline_hours: 120
  min_n_for_high_confidence: 50
  min_n_for_medium_confidence: 20
```

**Derivation logic:**
```python
async def derive_deadline_tier(
    symbol: str,
    vol_tier: str,
    reachability_rows: list[dict],
) -> DeadlineTier:
    """Derive deadline tier from nt_bracket_reachability percentiles."""
    # Filter rows for this (symbol, vol_tier)
    rows = [r for r in reachability_rows 
            if r['symbol'] == symbol and r['vol_tier'] == vol_tier]
    
    if not rows:
        # Fallback to global percentiles for this vol_tier
        rows = [r for r in reachability_rows if r['vol_tier'] == vol_tier]
    
    if not rows:
        # Ultimate fallback: global defaults
        return DeadlineTier(
            symbol=symbol, vol_tier=vol_tier,
            soft_deadline_hours=24, hard_deadline_hours=48,
            median_hours=24, n=0, confidence='low',
        )
    
    # Use the row with the most episodes (most reliable)
    best = max(rows, key=lambda r: r.get('n', 0))
    n = best.get('n', 0)
    
    soft = best.get('p75_hours') or best.get('median_hours') or 24
    hard = best.get('p90_hours') or soft * 2
    median = best.get('median_hours') or soft
    
    # Clamp
    soft = max(12, min(soft, 72))
    hard = max(soft * 1.5, min(hard, 120))
    
    confidence = 'high' if n >= 50 else 'medium' if n >= 20 else 'low'
    
    return DeadlineTier(
        symbol=symbol, vol_tier=vol_tier,
        soft_deadline_hours=soft, hard_deadline_hours=hard,
        median_hours=median, n=n, confidence=confidence,
    )
```

### 3.2 Harmonized ATR + Deadline Scoring

**Concept:** Merge the ATR-anchoring and deadline-aware scoring into one stage that uses the same ATR bucket and dynamic deadline.

```python
# regime_platform/sweep_orchestrator/heavy_sweep.py (MODIFY)

def select_best_combo_harmonized(
    combos: list[ComboResult],
    atr: float,
    deadline_tier: DeadlineTier,
    vol_tier: str,
    min_rr: float,
    max_sharpe: float,
    min_trades: int,
) -> dict:
    """Unified ATR + deadline scoring.
    
    Replaces the current 3-stage pipeline:
      1. select_best_combo_with_atr_anchoring()
      2. select_best_combo_deadline_aware()
      3. reachability_aware_pick()
    
    With a single pass that:
      - Computes ATR bucket for each combo
      - Applies dynamic deadline decay to each combo's Sharpe
      - Filters by bucket expectancy (graduated, not binary)
      - Returns the best combo with full provenance
    """
    # Step 1: Quality filter (same as before)
    quality = [c for c in combos 
               if c.tp_bricks / c.sl_bricks >= min_rr
               and c.sharpe <= max_sharpe
               and c.total_trades >= min_trades]
    
    if not quality:
        return {"nothing_survived": True, "reason": "quality_filter"}
    
    # Step 2: Score each combo with dynamic deadline decay
    scored = []
    for combo in quality:
        # ATR bucket
        distance = combo.tp_bricks * combo.brick_size
        ratio = distance / atr if atr > 0 else None
        bucket = _bucket_for(ratio) if ratio else None
        
        # Deadline decay: use the combo's own median_hours_to_resolve
        # (computed during grid search from the backtest trades)
        median_hours = combo.median_hours_to_resolve or deadline_tier.median_hours
        decay = deadline_tier.decay(median_hours)
        decayed_sharpe = combo.sharpe * decay
        
        # Bucket expectancy (graduated — not binary)
        bucket_exp = _get_bucket_expectancy(vol_tier, bucket)
        # bucket_exp is None (no data), 0.0 (negative), or positive
        # A negative expectancy reduces the score; None is neutral
        
        scored.append({
            "combo": combo,
            "bucket": bucket,
            "median_hours": median_hours,
            "decay": decay,
            "decayed_sharpe": decayed_sharpe,
            "bucket_expectancy": bucket_exp,
            "final_score": decayed_sharpe * (1.0 + _expectancy_factor(bucket_exp)),
        })
    
    # Step 3: Pick best by final_score
    best = max(scored, key=lambda s: s["final_score"])
    
    return {
        "best": best,
        "all_scored": scored,
        "nothing_survived": False,
        "deadline_tier": deadline_tier,
    }

def _expectancy_factor(expectancy_r: float | None) -> float:
    """Convert bucket expectancy to a score modifier.
    
    None (no data) → 0.0 (neutral)
    Positive → up to +0.2 boost
    Negative → down to -0.5 penalty
    """
    if expectancy_r is None:
        return 0.0
    if expectancy_r > 0:
        return min(0.2, expectancy_r * 0.1)
    return max(-0.5, expectancy_r * 0.25)
```

### 3.3 Regime-Conditional ATR Buckets

**Concept:** The 5 fixed ATR buckets are replaced with regime-conditional buckets that adapt to the symbol's current vol tier.

```python
# regime_platform/sweep_orchestrator/bracket_reachability.py (MODIFY)

# OLD: Fixed buckets
ATR_BUCKETS = (
    (0.0, 0.5, "<0.5"),
    (0.5, 1.0, "0.5-1"),
    (1.0, 2.0, "1-2"),
    (2.0, 4.0, "2-4"),
    (4.0, float("inf"), "4+"),
)

# NEW: Regime-conditional buckets
# The bucket boundaries scale with the vol tier's natural ATR
VOL_TIER_BUCKET_SCALES = {
    "low_vol": 0.5,      # Buckets are tighter
    "med_low_vol": 0.75,
    "med_high_vol": 1.0,  # Baseline
    "high_vol": 1.5,      # Buckets are wider
    "unknown": 1.0,
}

def _bucket_for_regime(ratio: float, vol_tier: str) -> str:
    """Regime-conditional ATR bucket.
    
    The ratio is scaled by the vol tier's natural ATR before bucketing.
    This means a 2.0 ATR TP in a high_vol regime lands in a different
    bucket than a 2.0 ATR TP in a low_vol regime.
    """
    scale = VOL_TIER_BUCKET_SCALES.get(vol_tier, 1.0)
    scaled_ratio = ratio / scale
    
    if scaled_ratio < 0.5:
        return "<0.5"
    elif scaled_ratio < 1.0:
        return "0.5-1"
    elif scaled_ratio < 2.0:
        return "1-2"
    elif scaled_ratio < 4.0:
        return "2-4"
    else:
        return "4+"
```

### 3.4 Clef-Flash Macro Coherence (wired into macro_filter.py)

**Concept:** Supplement the price-based SMA macro with the LLM narrative from `nt_macro_forecast`. Clef-Flash reads the narrative text and answers a coherence question — never re-derives direction from price.

```python
# regime_platform/sweep_orchestrator/macro_filter.py (MODIFY)

async def compute_macro_regime_enhanced(
    symbol: str,
    daily_closes: list[float],
    prev_score_smoothed: float,
    macro_forecast_row: dict | None,  # from nt_macro_forecast
) -> tuple[str, float, dict]:
    """Enhanced macro regime with Clef-Flash coherence.
    
    Returns (label, strength, provenance_dict).
    The provenance dict carries the coherence stamp for shadow evaluation.
    """
    # Price-based macro (existing logic, unchanged)
    price_label, price_strength = compute_macro_regime(
        daily_closes, prev_score_smoothed
    )
    
    if macro_forecast_row is None:
        return price_label, price_strength, {
            "source": "price_only",
            "coherence": None,
        }
    
    # LLM narrative macro
    narrative_label = macro_forecast_row.get("direction", "neutral")
    narrative_strength = macro_forecast_row.get("strength", 0.0)
    thesis = macro_forecast_row.get("thesis", "")
    drivers = macro_forecast_row.get("drivers", [])
    risks = macro_forecast_row.get("risks", [])
    
    # Clef-Flash coherence: do price and narrative agree?
    coherence = None
    if get_clef_flash_mode() == "shadow" and thesis:
        coherence = await _clef_macro_coherence(
            symbol=symbol,
            price_label=price_label,
            price_strength=price_strength,
            narrative_label=narrative_label,
            narrative_strength=narrative_strength,
            thesis=thesis,
            drivers=drivers,
            risks=risks,
        )
    
    # Combine: agreement boosts, disagreement shrinks
    if price_label == narrative_label:
        combined_strength = min(1.0, max(price_strength, narrative_strength) * 1.1)
        agreement = "agree"
    else:
        # Disagreement → shrink strength (don't zero it — both may be partially right)
        combined_strength = min(price_strength, narrative_strength) * 0.8
        agreement = "disagree"
    
    # If coherence is available, use it to modulate
    if coherence is not None:
        # High coherence → trust the combined read more
        # Low coherence → shrink further
        combined_strength *= (0.5 + 0.5 * coherence)
    
    return price_label, combined_strength, {
        "source": "price_narrative_coherence",
        "price_label": price_label,
        "narrative_label": narrative_label,
        "agreement": agreement,
        "coherence": coherence,
    }

async def _clef_macro_coherence(
    symbol: str,
    price_label: str,
    price_strength: float,
    narrative_label: str,
    narrative_strength: float,
    thesis: str,
    drivers: list[dict],
    risks: list[dict],
) -> float | None:
    """Ask Clef-Flash: do the price trend and narrative forecast agree?
    
    This is a COHERENCE read, not a re-derivation. The model reads the
    narrative text (thesis/drivers/risks) and the price-based label,
    and answers whether they agree. It never sees price data.
    """
    state = {
        "symbol": symbol,
        "price_macro_label": price_label,
        "price_macro_strength": price_strength,
        "narrative_thesis": thesis,
        "narrative_direction": narrative_label,
        "narrative_strength": narrative_strength,
        "drivers": [d.get("name", "") for d in drivers[:4]],
        "risks": [r.get("name", "") for r in risks[:3]],
    }
    
    questions = {
        "coherence": {
            "type": "noul",
            "instructions": (
                "Does the price-based macro trend agree with the narrative "
                "forecast direction? Answer true if they point the same way, "
                "false if they conflict."
            ),
        },
        "confidence": {
            "type": "choice",
            "instructions": (
                "How confident are you in this coherence assessment?"
            ),
            "options": {
                "high": "Strong agreement or clear disagreement",
                "medium": "Some ambiguity but leaning one way",
                "low": "Cannot determine from the given context",
            },
        },
    }
    
    answers = await decide(state, questions)
    if answers is None:
        return None
    
    coherence_answer = answers.get("coherence", {})
    p_agree = coherence_answer.get("noul")
    if p_agree is None:
        return None
    
    return float(p_agree)
```

### 3.5 Expectation Surfacing

**Concept:** Add resolution expectancy to the signal payload so the user/admin sees "resolves in ~N days" for every qualified signal.

```python
# regime_platform/sweep_orchestrator/orchestrator.py (MODIFY)

def _build_signal_sim_payload(...) -> dict:
    """Add resolution expectancy to the signal payload."""
    payload = {
        # ... existing fields ...
    }
    
    # Resolution expectancy
    deadline_tier = await derive_deadline_tier(
        symbol=symbol,
        vol_tier=_vol_tier(regime_label),
        reachability_rows=reachability_rows,
    )
    
    if deadline_tier:
        payload["expected_resolution_hours"] = deadline_tier.median_hours
        payload["expected_resolution_days"] = round(
            deadline_tier.median_hours / 24.0, 1
        )
        payload["resolution_confidence"] = deadline_tier.confidence
        payload["soft_deadline_hours"] = deadline_tier.soft_deadline_hours
        payload["hard_deadline_hours"] = deadline_tier.hard_deadline_hours
    
    return payload
```

**New columns on `nt_signal_sim`:**
```sql
-- Migration NNN
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS expected_resolution_hours DOUBLE PRECISION;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS expected_resolution_days DOUBLE PRECISION;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS resolution_confidence TEXT;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS soft_deadline_hours DOUBLE PRECISION;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS hard_deadline_hours DOUBLE PRECISION;
```

**Dashboard view:**
```sql
CREATE OR REPLACE VIEW v_signal_resolution_expectancy AS
SELECT 
    s.symbol,
    s.signal,
    s.ts,
    s.qualified,
    s.expected_resolution_hours,
    s.expected_resolution_days,
    s.resolution_confidence,
    s.soft_deadline_hours,
    s.hard_deadline_hours,
    r.median_hours_to_resolve as actual_median_hours,
    r.n as reachability_sample_size,
    CASE 
        WHEN s.outcome IS NOT NULL THEN 'resolved'
        WHEN s.expected_resolution_hours IS NOT NULL 
             AND extract(epoch from (now() - s.ts)) / 3600 > s.hard_deadline_hours
        THEN 'overdue'
        ELSE 'pending'
    END as resolution_status
FROM nt_signal_sim s
LEFT JOIN nt_bracket_reachability r 
    ON r.symbol = s.symbol 
    AND r.vol_tier = _vol_tier(s.regime)
    AND r.distance_atr_bucket = _bucket_for(s.tp_distance_atr)
WHERE s.qualified = true
  AND s.ts > now() - interval '7 days';
```

---

## 4. Data flow (redesigned)

```
nt_signal_sim (resolved rows)
        │
        ▼
┌─────────────────────────────────────┐
│ Phase 1: bracket_reachability       │  → nt_bracket_reachability
│ (REGIME-CONDITIONAL ATR buckets)    │     (bucket key now includes
│                                      │      vol_tier-scaled ratio)
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ Phase 2: calibration_bias_bucket    │  → nt_calibration_bias_by_bucket
│ (same, but with regime-conditional   │
│  buckets + confidence intervals)     │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ Phase 3: dynamic_deadline           │  → DeadlineTier per (symbol, vol_tier)
│ (derive soft/hard deadline from     │
│  reachability percentiles)           │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ Phase 4: harmonized scoring         │  → best combo with full provenance
│ (ATR bucket + dynamic deadline      │
│  decay + graduated expectancy)       │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ Phase 5: expectation surfacing      │  → nt_signal_sim columns
│ (expected_resolution_days,          │  → v_signal_resolution_expectancy
│  resolution_confidence)             │
└─────────────────────────────────────┘
```

---

## 5. Clef-Flash macro integration — the disagreement question

The user's specific question: **"forecast vs price macro are in disagreement."**

This is exactly the coherence question Clef-Flash should answer. The design:

| Layer | Source | What it reads | What it produces |
|---|---|---|---|
| Price macro | `macro_filter.py` | 200-day SMA | `price_label` (bull/bear/neutral), `price_strength` |
| Narrative macro | `macro_forecast_agent.py` | LLM thesis/drivers/risks | `narrative_label`, `narrative_strength` |
| Coherence | Clef-Flash (new) | Both labels + thesis text | `coherence` (0–1), `agreement` (agree/disagree) |

**The disagreement is information, not noise.** When price says "bull" and narrative says "bear":
- The old system: uses price only, ignores narrative
- The new system: shrinks strength (both may be partially right), stamps coherence for evaluation

**Pass bar:** The coherence stamp must show that disagreement correlates with worse outcomes. If `coherence < 0.5` rows have a lower win rate than `coherence > 0.5` rows, the coherence read is valuable. If not, it's retired.

---

## 6. Implementation stages

| Stage | What | Gate |
|---|---|---|
| **R0** | Write `dynamic_deadline.py` + config + derivation from reachability table | Unit test: derivation produces sensible tiers for all 14 symbols |
| **R1** | Harmonized scoring in `heavy_sweep.py` — replace 3-stage pipeline with single pass | Replay: Sharpe parity or improvement vs current 3-stage |
| **R2** | Regime-conditional ATR buckets in `bracket_reachability.py` | Replay: bucket distribution is more uniform (fewer empty cells) |
| **R3** | Clef-Flash macro coherence in `macro_filter.py` | Shadow: ≥200 coherence stamps, disagreement rate measured |
| **R4** | Expectation surfacing — new columns + dashboard view | Live: qualified signals show expected_resolution_days |
| **R5** | **PROMOTE** (dynamic deadline live) or **RETIRE** | Binary endpoint |

---

## 7. Design decisions (user Q&A)

### 7.1 Discord + Realtime: add resolution deadline info

**What changes:**
- `realtime_publisher.py::build_signal_payload()` — add `expected_resolution_hours`, `expected_resolution_days`, `resolution_confidence`, `soft_deadline_hours`, `hard_deadline_hours` to the broadcast payload
- `discord_delivery.py::maybe_send_discord_signal()` — pass deadline fields to the Discord embed
- `orchestrator.py::_build_signal_sim_payload()` — populate the deadline fields from the DeadlineTier

**Language rule (risk #5):** The deadline info is **descriptive, not predictive**. The payload carries `resolution_confidence` (high/medium/low) alongside the numeric estimate. The Discord embed shows "Est. resolution: ~12h (medium confidence)" — never "resolves in 12h". This is a statistical estimate from historical data, not a prediction or investment advice.

### 7.2 Dynamic deadline tiers — reopt_urgency similar to dynamic_atr

**Yes.** The `atr_reopt.py` module already has a 4h urgency cycle with durable state in `nt_roadmap`. The dynamic deadline system should follow the same pattern:

```python
# regime_platform/sweep_orchestrator/dynamic_deadline_reopt.py (NEW)

_URGENCY_STATE_MECH = "dynamic_deadline_reopt_urgency"

async def check_deadline_reopt_urgency() -> dict[str, Any]:
    """4h cycle: re-derive deadline tiers from fresh reachability data.
    
    Same pattern as atr_reopt.py:
    1. Every 4h: rebuild nt_bracket_reachability (or read fresh data)
    2. Re-derive DeadlineTier per (symbol, vol_tier)
    3. If tier drifted > 15% from current, flag for review
    4. State lives in nt_roadmap(dynamic_deadline_reopt_urgency)
    """
```

**Cadence:** 4h (same as ATR reopt). The deadline tiers are derived from the reachability table, which is rebuilt by the EOD pass. The 4h reopt checks if the reachability data has changed enough to warrant new tiers.

**Config:**
```yaml
dynamic_deadline_reopt:
  enabled: true
  cycle_hours: 4
  drift_threshold: 0.15  # 15% change in soft/hard deadline
  min_n_for_update: 20    # don't update tiers with < 20 episodes
```

### 7.3 Clef-Flash macro coherence — cadence and regime data

**Cadence:** The macro coherence check runs **when the macro forecast is refreshed** (daily) OR **when the regime shifts** (event-driven). It does NOT run on a fixed 1h/4h timer. The rationale:
- The macro forecast (`nt_macro_forecast`) is computed daily by `run_macro_forecast()`
- The price-based macro (`nt_macro_regime`) is also computed daily
- The coherence question is only interesting when one of them has changed
- Running it on a fixed timer would re-ask the same question when nothing has changed

**Regime data:** Yes, the coherence check needs regime data. The state block includes:
- `regime_label` (e.g. `med_high_vol_bear`) — the current HMM regime
- `regime_conf` — the regime confidence
- `vol_tier` — extracted from regime label

This is **context**, not a re-derivation. The model reads the regime label to understand the market context, but it is never asked to re-derive the regime from price data.

**Wiring:**
```python
# In macro_filter.py::refresh_macro_regime() (daily)
# After computing price_label and fetching macro_forecast_row:
if get_clef_flash_mode() == "shadow":
    coherence = await _clef_macro_coherence(
        symbol=symbol,
        price_label=price_label,
        price_strength=price_strength,
        narrative_label=narrative_label,
        narrative_strength=narrative_strength,
        thesis=thesis,
        drivers=drivers,
        risks=risks,
        regime_label=regime_label,      # NEW: regime context
        regime_conf=regime_conf,         # NEW: regime confidence
        vol_tier=vol_tier,               # NEW: vol tier
    )
```

### 7.4 Harmonized scoring does NOT override ATR-anchoring

**Clarification:** The harmonized scorer does NOT replace ATR-anchoring. It **absorbs** it. The current pipeline is:

```
grid_results → ATR-anchor (widen pool) → deadline-score (re-score) → reachability-filter
```

The harmonized pipeline is:

```
grid_results → ATR-anchor (widen pool) → harmonized-score (single pass)
```

The ATR-anchoring stage still runs first and widens the candidate pool. The harmonized scorer then scores all candidates (including ATR-anchored ones) with the dynamic deadline + graduated expectancy. The key difference: the deadline is now dynamic (per-symbol, per-regime) instead of fixed 48h, and the expectancy filter is graduated instead of binary.

### 7.5 Language rule — no definitive predictions

**Agreed.** The deadline info is **descriptive, not predictive**. Rules:
- Never say "resolves in N days" — say "Est. resolution: ~N days (confidence: high/medium/low)"
- The `resolution_confidence` field is **always present** alongside the numeric estimate
- The Discord embed and dashboard view both show the confidence level
- The language is "historical median resolution time" not "expected resolution time"
- This is a statistical estimate from historical data, not a prediction or investment advice

---

## 8. Open risks (updated)

1. **Low-vol symbols may need >12h deadline.** The `min_soft_deadline_hours: 12` floor may be too tight for USDCAD (median ~36h). **Action:** Run a replay/backtest on low-vol symbols (USDCAD, USDCHF, AUDUSD) to measure the actual resolution distribution and set a data-driven floor. The replay should use the same `build_paths()` + `walk()` machinery as `tiered_atr.py`.

2. **Regime-conditional buckets orphan existing data.** Changing the bucket key means old `nt_bracket_reachability` rows are under the old key. **Action:** Run a backfill migration that recomputes all historical rows with the new regime-conditional bucket key. The backfill should use the same `_dedupe_episodes()` + `_current_atr_by_symbol()` machinery as the original build.

3. **Clef-Flash macro coherence may not discriminate.** The HMM and first-passage both measured below base rate. A model reading a state that contains those sub-base-rate sources may inherit the ceiling. **Action:** R3 shadow accumulation (≥200 stamps) will settle this. The pass bar: `coherence < 0.5` rows must show lower win rate than `coherence > 0.5` rows.

4. **Harmonized scoring absorbs ATR-anchoring, not replaces it.** The ATR-anchoring stage still runs first. The harmonized scorer scores all candidates (including ATR-anchored ones) with the dynamic deadline. **Action:** Replay must verify that the harmonized scorer picks the same or better combo than the current 3-stage pipeline.

5. **Language rule — no definitive predictions.** The deadline info is descriptive, not predictive. **Action:** All user-facing surfaces (Discord, dashboard) must show `resolution_confidence` alongside the numeric estimate. The language is "historical median" not "expected".
