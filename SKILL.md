---
name: noble-trader-fastapi-backend
description: Build and maintain the fastapi sweep orchestrator.
version: 1.1.0
author: Hermes Agent | User
license: MIT
platforms: [windows, linux]
metadata:
  hermes:
    tags: [fastapi, sweep-orchestrator, signal-producer, regime-platform, timesfm, supabase]
---

# Noble Trader FastAPI Backend

**Codename: Kairos** (καιρός — the opportune moment). Sweep orchestrator and signal producer. FastAPI stack on LightningAI Studio.

## Always-On Rules

| Rule | Applies to |
|---|---|
| **Response style** | Never explain, narrate, or hedge. Return tool output directly; state results in plain language tables. Verbosity gets dismissed. |
| **Terminology** | Use only the codebase's own terms. Do not invent labels (e.g., "win-rate band" existed as `WR_LO/WR_HI`, not as a named filter). |
| **Studio restart** | MUST use `on_stop.sh` → `on_start.sh`. Do NOT use `start.sh`. |
| **Ruff compliance** | All new Python files must pass `ruff check` with zero errors before committing. |
| **Reopt validation** | Reports use `allow_best_effort=True` — show raw picks. Verification uses `allow_best_effort=False` — returns None if second half negative. Both must be distinguished. |
| **Roadmap sync** | After any measurement, update `nt_roadmap` with mechanism, status, review_at, and exact data. Idempotent scripts: create-then-patch pattern. |

## When to Use

- Editing `regime_platform/` Python modules (orchestrator, macro_filter, macro_edge, context_alignment, trend_filter)
- Adding/modifying symbols in the sweep path
- Working with TimesFM integration, HMM regime models, EV Engine v6
- Writing or reviewing Supabase migrations for the backend schema
- Auditing the orchestrator chain for gaps / unwired processes
- Validating that the geometry+tiers apply uniformly across symbols

## Studio Service Lifecycle

```bash
# Verify services are live
for p in 8001 8002 8003 8004; do
  curl -s -m 8 -o /dev/null -w "  :%s -> %{http_code}\n" http://127.0.0.1:$p/health
done

# Clean restart using the canonical scripts
python scripts/on_stop.sh   # kills everything cleanly
python scripts/on_start.sh  # starts orchestrator + services
```

## Reopt Cell Validation Pattern

```python
# The reopt reports WITH allow_best_effort=True (may show non-strict picks)
# Verification requires allow_best_effort=False (second-half must be positive)

from tier_api import select_tp_sl, walk_envelopes
from regime_platform.sweep_orchestrator.tiered_atr import live_same_bar_policy

env, _ = await _paths_for(syms, members, since, until)
# ... score cell ...
strict = select_tp_sl(
    envelopes=env, tp_grid=[tp], sl_grid=[sl],
    min_rr=..., max_expiry=..., cost_atr=...,
    min_signals=..., allow_best_effort=False  # THIS IS THE BAR
)
print("SURVIVES" if strict else "FAILS — best-effort only")
```

## Roadmap Update Pattern

After measurement, immediately record in `nt_roadmap`:

```python
# Idempotent: 201 on create, 204 on patch, always 1 row
payload = {
  "mechanism": "...",
  "status": "pending",
  "current_state": "...exact data...",
  "review_condition": "...what settles it...",
  "notes": "...exact record...",
  "worklog_ref": "worklog/... (+ scripts/...)",
  "review_at": "YYYY-MM-DD"
}
```

## Linter Compliance Checklist

Before any `git commit`:
1. `ruff check <file> --fix` → edit until clean
2. `ruff format <file>` → normalize whitespace  
3. Verify unused imports: `grep -n "import" <file> | grep -v "#"`

## References

- `references/reopt-validation.md` — detailed scoring procedure, drift gate rules, RR interpretation
- `references/studio-lifecycle.md` — restart sequences, log sanitization, supervisor patterns
- `references/geometry-conformance.md` — how select_tp_sl validates band equality, drift thresholds

## Known Pitfalls

1. **allow_best_effort mismatch** — the reopt report shows picks with `True`; `select_tp_sl` with `False` may return `None` for a cell that had positive EV only in first half. Always clarify which mode is being used.

2. **Forex far-TP / BTCUSD high RR** — these can achieve positive EV in first half but negative in second. The free-lattice may pick them (max EV), but `select_tp_sl(..., allow_best_effort=False)` would reject them. The drift gate (>15%) decides if reopt writes them.

3. **Code-server port (8000)** blocks the gateway. This is permanent, not a bug.

4. **PYTHONPATH shadowing on Windows** — `PYTHONPATH="$PWD/src"` is for local dev; Studio uses `PYTHONPATH=""`.