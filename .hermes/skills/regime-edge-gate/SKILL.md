---
name: regime-edge-gate
version: 0.1.0
description: Per-signal per-regime qualification gate for Kairos.
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [kairos, regime-edge, signal-gate, qualification, supabase]
---

# regime-edge-gate

The **live per-signal qualification gate** that rejects a Kairos signal when the
edge for `(symbol, signal, regime)` is measured below a win-rate threshold. This
is the gate half of the regime_edge mechanism; the **audit half** (the offline
proof engine that justifies flipping the gate to `live`) lives in the
`regime-edge-proof-engine` skill. It is shadow-only by default (`shadow` stamps
but never rejects).

## When to Use

- You are investigating why a signal was (or was not) suppressed — this gate is
  the `regime_edge_ok` entry in `gate_results`.
- You are promoting `regime_edge_mode` from `shadow` → `live` (requires the
  3-bucket pass-bar, see Verification).
- You are diagnosing a "constant indicator" that looks inverted or useless for a
  symbol — the diagnosis chain below applies.
- You are reading or rebuilding `nt_regime_edge`.

## Prerequisites

- Kairos running on the Studio (orchestrator port 8003); the gate lives in
  `_process_symbol` (`orchestrator.py`).
- Supabase access with a `SUPABASE_SECRET_KEY` (PostgREST) to read/write
  `nt_regime_edge` (see `noble-trader-fastapi-backend` skill §4 for the
  two-token rule).
- HMM regime labels must already exist in `nt_sweep_result` (upstream of the
  gate).

## How to Run

The gate is **read-only at sweep time** — it consumes `nt_regime_edge`. To
recompute the table that feeds it, invoke through the `terminal` tool:

```bash
# The daily offline rebuild that feeds the gate (see regime-edge-proof-engine
# for the daily cadence / edge_lab_run_mode wiring)
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.rebuild_regime_edge
# or, for a single symbol's row:
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -c "
from regime_platform.services.regime_edge_cache import refresh_symbol; import asyncio; asyncio.run(refresh_symbol('AAPL'))"
```

## Quick Reference

| item | value |
|---|---|
| Mode flag | `regime_edge_mode` (`shadow` / `live` / `off`) — in `config/feature_flags.yaml` |
| Table | `nt_regime_edge` (PK: `symbol, signal, regime`) |
| Min episodes | `min_n: 20` (`regime_edge.min_n`) |
| Min win rate | `min_win_rate: 0.50` (`regime_edge.min_win_rate`) |
| Rebuild cadence | 24 h (`regime_edge.rebuild_interval_hours`) |
| Fail-open threshold | `n < min_n` → pass (no evidence) |

Config block (`config/feature_flags.yaml`):

```yaml
regime_edge_mode: "shadow"   # off | shadow | live
regime_edge:
  min_n: 20
  min_win_rate: 0.50
  rebuild_interval_hours: 24
```

## Procedure

**Gate evaluation** (`orchestrator.py` `_process_symbol`, after the existing
gate chain passes):

```python
if get_regime_edge_mode() == "live":
    edge = await get_regime_edge(symbol, signal, regime_label)
    reject, reason = should_reject_by_regime_edge(
        edge, min_n=20, min_win_rate=0.50
    )
    if reject:
        gate_results["regime_edge_ok"] = False
        suppress_reason = reason
```

**Fail-open rule:** if `edge is None` or `n < min_n`, the gate passes — no
evidence either way. This is deliberate: a gate that blocks signals it cannot
score is a defect (see Pitfalls).

**Architecture:**

```
nt_signal_sim (resolved rows)
        │
        ▼
┌─────────────────────────────────────┐
│ regime_edge.py                      │  → nt_regime_edge
│ (compute edge per (symbol, signal,  │
│  regime) from outcomes)             │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│ orchestrator._process_symbol()      │
│ (gate: if edge exists and WR < 50%  │
│  → reject; if n < 20 → fail-open)   │
└─────────────────────────────────────┘
```

**Diagnosis chain for "constant indicator" problems** (when a filter looks
inverted or useless for a symbol):

1. **Check if the indicator is a constant.** If the label never changes (e.g.
   always "bull"), it's not a signal — it's noise. Compute the label's unique
   values over the signal window.
2. **Check the horizon ratio.** `indicator_timeframe / signal_resolution`. If
   > 50×, the indicator is likely a constant.
3. **Run a geometry replay.** Test TP/ATR × SL/ATR combos against resolved
   signals. If no combo has both `EV_h1 > 0` AND `EV_h2 > 0`, the edge is
   regime-dependent, not geometry-dependent.
4. **Build a regime edge table.** If the edge is regime-dependent, use the HMM
   regime label directly as the qualification signal.
5. **Disable the constant filter per-symbol.** Add `by_symbol: {SYM:
   {enabled: false}}` to the filter config.

## Pitfalls

- **`regime_edge` ≠ `edge_lab`.** The gate (this skill) reads `nt_regime_edge`
  at sweep time; the offline proof engine (`regime-edge-proof-engine`) writes
  `nt_edge_lab_run`. They are deliberately separate so gate evidence is
  observable when the gate flips.
- **A fail-open fallback that blocks = the defect.** If `n < min_n` rejects
  signals, the `min_n` is too high or the edge table is too sparse — lower
  `min_n` or widen the regime buckets, never flip fail-open to fail-closed.
- **Granularity trap.** If buckets are finer than the symbol's episode count,
  most buckets have `n < 20` and the gate passes everything by default — it
  becomes a no-op. Coarse buckets (fewer regimes) are more gate-effective.
- **AAPL edge is mean-reversion, not trend.** Strong-trend regimes
  (`strong_bull`, `strong_bear`) have near-zero win rate — the move is
  exhausted. Buying the dip in bear regimes and selling the rally in bull
  regimes works; chasing strong trends fails.

## Verification

**Pass bar for promotion** (`shadow` → `live`):

1. Rejected buckets (WR < 50%) have **worse** outcomes than predicted.
2. Passed buckets (WR ≥ 50%) have **better** outcomes than predicted.
3. The gate improves overall expectancy vs no gate.

```bash
# spot-check one symbol's edge table
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -c "
from regime_platform.sweep_orchestrator.regime_edge import get_regime_edge; import asyncio; print(asyncio.run(get_regime_edge('AAPL','buy','med_low_vol_bear')))"
```

The AAPL case study (live reference: `references/regime-edge-gate.md`) shows
BUY in `med_low_vol_bear` at 88.5% WR (mean-reversion edge) vs BUY in
`med_low_vol_strong_bull` at 19.5% WR (exhausted trend). A gate that promotes
only when the pass-bar holds across all symbols, not just AAPL, is ready for
`shadow` → `live`.

## References

- `references/regime-edge-gate.md` — deep reference (architecture, full table
  schema, case study, full diagnosis chain)
- `regime-edge-proof-engine` skill — the offline audit that justifies a
  `shadow` → `live` promotion
