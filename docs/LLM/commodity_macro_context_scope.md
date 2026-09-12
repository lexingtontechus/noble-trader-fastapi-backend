# Commodity Macro Context — Assessment & Scope Document

**Date:** 2026-09-11  
**Status:** Assessment only — no code changes  
**Input:** 13-commodity 12-month forecast model (commodity_12m_forecast_model_2027.md)

---

## Executive Summary

We need to bring 12 liquid commodity symbols into the existing `nt_symbol` registry and wire the 12-month forecast model's macro regime signals into the pipeline's `macro_filter.py` layer — inline within the orchestrator, in `macro_regime_mode=shadow`, on the existing sweep cadence. DAP is out (no live feed). The design combines **Option A** (extend macro_filter.py) with **Option C** (inline LLM agent in orchestrator).

---

## 1. Symbol Scope

### 1.1 What goes into nt_symbol

| # | Commodity | TradingView Symbol | Exchange | Existing in _PRICE_DECIMALS? | Notes |
|---|-----------|-------------------|----------|------------------------------|-------|
| 1 | Brent Crude | `TVC:B` | TradingView | ❌ (`OIL` exists = WTI) | Need `B` separately |
| 2 | RBOB Gasoline | `TVC:RB` | TradingView | ❌ | |
| 3 | ULSD Diesel / Heating Oil | `TVC:HO` | TradingView | ❌ | |
| 4 | Jet Fuel | `TVC:JX` or `TVC:HO` proxy | TradingView | ❌ | JX may not exist; evaluate |
| 5 | European Natural Gas (TTF) | `TVC:VGT` or ICE-specific | TradingView | ❌ | May need alternative source |
| 6 | FCPO (Palm Oil) | `TVC:FCPO` | Bursa Malaysia via TVC | ❌ | |
| 7 | CT (Cotton No.2) | `TVC:CT` | ICE via TVC | ❌ | |
| 8 | SB (Sugar No.11) | `TVC:SB` | ICE via TVC | ❌ | |
| 9 | RR (Rough Rice) | `TVC:RR` | CBOT via TVC | ❌ | |
| 10 | ZC (Corn) | `TVC:ZC` | CBOT via TVC | ❌ | |
| 11 | ZW (Wheat) | `TVC:ZW` | CBOT via TVC | ❌ | |
| 12 | Sulfur | `TVC:SI` | ICE via TVC | ❌ | Verify TVC has sulfur |
| — | **DAP** | **N/A** | **OTC/Spot** | **—** | **FLAGGED: no live feed** |

### 1.2 DAP Exclusion Rationale

DAP is OTC/spot-only with no liquid futures contract and no TradingView ticker. The forecast model has price ranges ($750–820/t Q4 2026) but no live data source can feed the `macro_filter.py` daily-bar pipeline. Options for DAP:
- **Manual injection**: weekly CSV/JSON dump of DAP prices → `nt_macro_context` table
- **Full exclusion**: treat as a research-only symbol, not in the sweep path
- **Recommendation**: exclude from sweep entirely; reference the forecast model for context only

---

## 2. Architecture: Where Macro Context Fits

### 2.1 Current Pipeline

```
nt_symbol (active symbols)
    │
    ├──► light sweep (*/15 * * * *) ──► _process_symbol()
    │       ├── HMM regime → p_regime
    │       ├── TimesFM forecast → p_timesfm
    │       ├── macro_filter (daily) → macro_label/macro_strength
    │       ├── trend_filter (daily) → trend_label/trend_strength
    │       ├── context_alignment gate (Tier 1)
    │       └── 7-gate qualification → QUALIFIED
    │
    └── heavy sweep (0 20 * * *) ──► grid search + renko optimization
```

The `macro_filter.py` runs a **daily refresh** at 06:00 UTC (independent cron, `_run_macro_regime_refresh`). It fetches 300 daily bars per symbol from TradingView, computes a 200-day SMA + EMA, and stores `macro_label`/`macro_strength` in `nt_macro_regime`. The sweep path reads this at signal time.

### 2.2 The Problem

`macro_filter.py` currently looks ONLY at each symbol's own price history. It has no concept of cross-commodity context. For example:
- `HO` (diesel) gets its own macro_label from its own 200-day SMA — but it doesn't know Brent is spiking
- `FCPO` doesn't know crude oil is surging (which affects biodiesel economics)
- `RB` doesn't know distillate cracks are at record highs

### 2.3 What We're Adding

**Inline LLM agent in the orchestrator** that transforms the 12-month forecast model into structured macro regime signals, stored per-symbol in `nt_macro_regime` (or a new table), consumed by the existing `macro_filter.py` pipeline in `shadow` mode.

The flow:
```
forecast_model_2027.md (static JSON/YAML)
    │
    ├──► [ORCHESTRATOR INLINE] _run_macro_forecast_refresh()
    │       ├── Reads forecast model (local file or JSON)
    │       ├── For each symbol: maps forecast period → macro_label/macro_strength
    │       ├── Writes to nt_macro_regime (or nt_macro_forecast)
    │       └── Runs on existing schedule (or aligned to it)
    │
    ├──► [EXISTING] _run_macro_regime_refresh() (06:00 UTC daily)
    │       ├── Fetches 300 daily bars → computes own-price macro_label
    │       └── Upserts nt_macro_regime (overwrites or merges)
    │
    └──► [SWEEP PATH] _process_symbol()
            ├── fetch_macro_regime(symbol) → reads nt_macro_regime
            ├── Uses macro_label/macro_strength in:
            │   ├── kelly_macro_multiplier()
            │   ├── context_alignment gate
            │   └── nt_signal_sim capture
            └── macro_regime_mode=shadow → no live Kelly impact
```

---

## 3. Detailed Design

### 3.1 Forecast Model → Macro Signal Mapping

The 12-month model provides quarterly ranges. We need a function that maps "current date + forecast range" to `{macro_label, macro_strength}`:

```python
def forecast_to_macro_signal(symbol: str, current_date: datetime) -> dict:
    """Maps the 12-month forecast model to macro_label/macro_strength.
    
    Logic:
    - If current date falls within the forecast's bullish window:
      macro_label = "bull", strength = position within range
    - If bearish: macro_label = "bear"
    - If neutral/transitioning: macro_label = "neutral"
    - Strength: based on where in the range we are (e.g., near top of range = strong bull)
    """
```

Example for **HO (Diesel)** on 2026-09-15:
- Q4 2026 range: $160–190/bbl (record cracks, winter demand)
- Current price ~$170/bbl
- Position: middle-to-upper part of range → **macro_label = "bull"**, macro_strength = 0.7

Example for **ZW (Wheat)** on 2026-09-15:
- Q4 2026 range: $535–545 (declining trend)
- Current price ~$540
- Position: range-bound with bearish trajectory → **macro_label = "bear"**, macro_strength = 0.4

### 3.2 Data Storage

**Option 1 — Extend nt_macro_regime**
Add columns: `forecast_source`, `forecast_strength_override`, `forecast_macro_label`, `forecast_macro_strength`. When `macro_regime_mode=shadow`, the sweep path reads both the own-price macro_label and the forecast-derived label.

**Option 2 — New nt_macro_forecast table**
Cleaner separation. Stores: `symbol`, `q4_2026_range`, `q1_2027_range`, ..., `current_macro_label`, `current_macro_strength`, `model_version`, `updated_at`. The orchestrator's inline agent writes here; `macro_filter.py` reads as a secondary signal.

**Recommendation: Option 2.** Keeps the existing `nt_macro_regime` table pure (own-price computation) and adds a new `nt_macro_forecast` table for the LLM-derived context. The `context_alignment.py` gate can read both.

### 3.3 Inline Agent in Orchestrator

New method on `SweepOrchestrator`:

```python
async def _run_macro_forecast_refresh(self) -> None:
    """Inline agent: maps the 12-month forecast model to macro regime signals.
    
    Runs on the existing macro_regime cron schedule (06:00 UTC daily).
    Reads the forecast model JSON/YAML, computes per-symbol macro_label/strength
    for the current quarter, and upserts nt_macro_forecast.
    """
```

This is NOT a separate cron job — it's a method called from `start()` alongside the existing `_run_macro_regime_refresh`. It runs at the same cadence (daily 06:00 UTC) or slightly before/after.

### 3.4 Macro Filter Extension (Option A)

Modify `macro_filter.py` to optionally read `nt_macro_forecast` for each symbol and blend or overlay the forecast-derived macro_label onto the own-price macro_label:

```python
async def refresh_macro_regime(symbols: list[str]) -> list[dict[str, Any]]:
    """Extended: fetch own-price macro regime + overlay forecast context."""
    # Existing: own-price computation
    own_price_rows = await _compute_own_price_macro(symbols)
    
    # NEW: fetch forecast-derived signals
    forecast_rows = await fetch_macro_forecast(symbols)
    
    # Merge: if forecast says "bull" and own-price says "neutral",
    # the combined signal leans bull with blended strength
    merged = _merge_macro_signals(own_price_rows, forecast_rows)
    
    await upsert_macro_regime(merged)
    return merged
```

The merge logic respects `macro_regime_mode=shadow` — no live Kelly impact, just capture.

### 3.5 Context Alignment Gate Enhancement

`context_alignment.py` currently reads `macro_label` and `trend_label`. With the forecast overlay, it should also read `nt_macro_forecast.current_macro_label` as a third input. The gate logic would be:

```python
# Current: macro_opposes = (macro_label not in implied)
# Extended: macro_opposes = (own_macro_label not in implied) AND 
#                      (forecast_macro_label not in implied)
#
# Or: weighted — if forecast strongly says "bull" but own-price is neutral,
#     the gate is more tolerant of a "buy" signal
```

---

## 4. Shadow Mode Flow for New Symbols

### 4.1 Registration

Each new commodity symbol added to `nt_symbol` with:
- `symbol`: e.g., `HO`, `RB`, `CT`
- `exchange`: `TVC` (TradingView)
- `tradingview_symbol`: `TVC:HO` etc.
- `regime_cron`: `*/15 * * * *` (same as existing)
- `sweep_cron`: `0 20 * * *` (same as existing)
- `macro_regime_mode`: `shadow` (per config/feature_flags.yaml)
- `trend_regime_mode`: `shadow` (recommended)
- `brick_params`: same defaults, or futures-specific (e.g., 1-min or 5-min bars instead of 1h)

### 4.2 Shadow Mode Behavior

In `macro_regime_mode=shadow`:
1. `macro_filter.py` computes `macro_label`/`macro_strength` on `nt_signal_sim` rows
2. Does NOT multiply `effective_kelly`
3. Does NOT gate qualification
4. Accumulates evidence for `macro_edge.py` to analyze later

For new commodity symbols specifically:
- The sweep paths will process them but `macro_label`/`macro_strength` come from both the own-price computation AND the forecast overlay
- `context_alignment` gate reads both signals
- Zero impact on live Kelly sizing
- Data accumulates in `nt_signal_sim` with `macro_label`/`macro_strength` captured

### 4.3 What Gets Captured on nt_signal_sim

The existing `_build_signal_sim_payload` already has columns for:
- `macro_label`, `macro_strength` (from macro_filter)
- `trend_label`, `trend_strength` (from trend_filter)
- `context_aligned_ok` (from context_alignment gate)

New columns needed (in `nt_macro_forecast`, not signal_sim):
- `forecast_macro_label`, `forecast_macro_strength` — from the inline LLM agent

### 4.4 The nt_symbol Additions

```sql
-- Example: adding HO to nt_symbol
INSERT INTO nt_symbol (symbol, exchange, tradingview_symbol, regime_cron, sweep_cron, 
    macro_regime_mode, trend_regime_mode, active, ...)
VALUES ('HO', 'TVC', 'TVC:HO', '*/15 * * * *', '0 20 * * *', 
    'shadow', 'shadow', true, ...);
```

Note: `macro_regime_mode` and `trend_regime_mode` need to be columns on `nt_symbol` (or fetched from `feature_flags.yaml` as defaults). The `feature_flags.yaml` config already defines these modes; they may need per-symbol override capability.

---

## 5. Scope: What Gets Built

### 5.1 Supabase Schema Changes

**New table: `nt_macro_forecast`**
```sql
CREATE TABLE nt_macro_forecast (
    symbol TEXT NOT NULL,
    forecast_version TEXT NOT NULL DEFAULT 'v1',
    q4_2026_low NUMERIC,
    q4_2026_high NUMERIC,
    q1_2027_low NUMERIC,
    q1_2027_high NUMERIC,
    q2_2027_low NUMERIC,
    q2_2027_high NUMERIC,
    q3_2027_low NUMERIC,
    q3_2027_high NUMERIC,
    current_macro_label TEXT CHECK (current_macro_label IN ('bull','bear','neutral')),
    current_macro_strength NUMERIC CHECK (current_macro_strength BETWEEN 0 AND 1),
    source TEXT NOT NULL DEFAULT 'forecast_model',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol)
);
```

**nt_symbol column additions** (if not already present):
- `macro_regime_mode TEXT DEFAULT 'shadow'`
- `trend_regime_mode TEXT DEFAULT 'shadow'`
- These may already exist via migration 031 or feature_flags

### 5.2 Code Changes

| Module | Change | Type |
|--------|--------|------|
| `orchestrator.py` | New method `_run_macro_forecast_refresh()` | Add |
| `orchestrator.py` | Call from `start()` | Modify |
| `macro_filter.py` | Read `nt_macro_forecast`, merge with own-price macro | Extend |
| `context_alignment.py` | Accept forecast_macro_label as third input | Extend |
| `supabase_client.py` | New methods: `fetch_macro_forecast()`, `upsert_macro_forecast()` | Add |
| `settings.py` | New env var for forecast model path/URL | Add |
| `feature_flags.yaml` | Add `forecast_macro_mode` | Add |
| SQL migration | Create `nt_macro_forecast` table | Add |
| `_PRICE_DECIMALS` dict | Add 12 new symbols | Extend |

### 5.3 Non-Code Assets

| Asset | Description |
|-------|-------------|
| `forecast_model_2027.md` | Already created — the 12-month model |
| `forecast_model_2027.json` | Machine-readable version for the inline agent |
| SQL migration | `supabase/migrations/XXXX_add_nt_macro_forecast.sql` |
| Test additions | `tests/` for forecast→macro signal mapping |

---

## 6. Risk & Open Questions

### 6.1 TradingView Data Availability

**Critical assumption**: TradingView RapidAPI provides daily bars for all 12 commodity symbols. This needs verification:
- FCPO (Bursa Malaysia) — likely available as `TVC:FCPO`
- VGT (European TTF gas) — may not exist on TradingView
- JX (Jet fuel) — may not exist as a standalone ticker
- SI (Sulfur) — verify availability

**If TradingView doesn't have a symbol**: That symbol can't run the light/heavy sweep pipeline. Options:
- Use `fetch_tradingview_batch` with a proxy symbol
- Exclude from the sweep path entirely
- Add a separate data source

### 6.2 Futures-Specific Considerations

- **Continuous contracts**: Commodity futures roll. TradingView symbols may be continuous or front-month. Need to verify which the TVC symbol maps to.
- **Price precision**: Futures have different tick sizes than stocks. `_PRICE_DECIMALS` needs accurate values per symbol.
- **Trading hours**: CME/ICE futures have specific sessions. `sweep_after_hours` config may need per-symbol adjustment.

### 6.3 Forecast Model Format

The inline agent needs the forecast model in **machine-readable format** (JSON/YAML), not just markdown. The `commodity_12m_forecast_model_2027.md` needs a JSON equivalent that the agent can parse.

### 6.4 Macro Regime Mode Column

The `nt_symbol` table may not have a `macro_regime_mode` column per-symbol. The existing design reads from `feature_flags.yaml` globally. Adding per-symbol override capability requires either:
- A new column on `nt_symbol`
- A lookup table (e.g., `nt_symbol_mode` or similar)
- Hardcoded in the Python config

### 6.5 Shadow Mode Data Volume

Adding 12 new symbols × 15-min intervals × 2 paths = ~24 new signals/hour per symbol = ~576 signals/hour across all new symbols. Over a week, that's ~96,768 `nt_signal_sim` rows. The existing retention logic (`EOD_RETENTION_STALE_DAYS = 7`) handles this, but the EOD resolver cap (`EOD_RESOLVE_LIMIT = 1000`) may need adjustment if these symbols resolve.

---

## 7. Implementation Sequence (Recommended)

1. **Phase 1 — Schema**: Create `nt_macro_forecast` table + `nt_symbol` column additions
2. **Phase 2 — JSON model**: Convert `commodity_12m_forecast_model_2027.md` to `forecast_model_2027.json`
3. **Phase 3 — Inline agent**: Add `_run_macro_forecast_refresh()` to orchestrator
4. **Phase 4 — Extend macro_filter**: Read + merge forecast context
5. **Phase 5 — Extend context_alignment**: Accept forecast input
6. **Phase 6 — Register symbols**: Add 12 symbols to `nt_symbol` in shadow mode
7. **Phase 7 — Price decimals**: Add 12 entries to `_PRICE_DECIMALS`
8. **Phase 8 — Supabase client**: Add fetch/upsert for `nt_macro_forecast`
9. **Phase 9 — Test**: Verify shadow mode data flow end-to-end
10. **Phase 10 — Go live**: Flip `macro_regime_mode` to `live` after evidence accumulation

---

## 8. What's NOT in Scope

- **DAP**: No live feed, excluded from the pipeline
- **LLM agent as external service**: User chose inline in orchestrator
- **Continuous 24/7 sweep**: Same cadence as existing symbols
- **Manual CSV injection**: Inline agent is preferred (user chose A+C)
- **Changes to TimesFM**: Not modifying TimesFM model or inference path
- **Changes to EV engine v5 blend weights**: Not modifying `P_WIN_WEIGHTS_FULL`
- **New features beyond macro context**: No new gates, no new calibration paths

---

*Document prepared for scope review. No code written pending approval.*
