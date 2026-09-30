# Monthly ATR re-optimum (rediscovery clock)

> **Date:** 2026-09-30 · **Flag:** `atr_reopt_enabled` (on/off)
> **Job:** APScheduler `atr-reopt-monthly` · **1st of month 03:30 UTC**
> **Code:** `regime_platform/sweep_orchestrator/atr_reopt.py`
> **CLI:** `scripts/monthly_atr_reopt.py` · **Related:** `20260930_VOL_REGIME_ATR_BTC_XAU.md`

---

## 1. Why not the heavy sweep

Heavy `dynamic_atr_pick` is the **wrong clock for rediscovery**:

| | Heavy (daily) | Monthly re-opt (this) |
|---|---|---|
| Window | 10d | **45d** |
| Search | fixed YAML grid + clamps | free 0.25→0.05→0.01 lattice |
| Regimes | pooled | optional vol_ratio terciles |
| Checks | both-halves EV>0 | wr band + both halves + drift vs live clamps |
| Writes | bricks / local pick | **report + roadmap only** |

Heavy **does** re-walk and can refresh the pick **inside** current clamps —
that stays. What it cannot do is notice the optimum has left the clamp box,
or that a 10d sample was underpowered (XAU −EV → +EV at 45d).

## 2. Why APScheduler, not Studio cron

Studio cron lives **outside the codebase** — a second source of truth that
goes stale on every migrate/restart and cannot be reviewed in the PR that
changes the job. The platform already schedules via APScheduler in
`orchestrator.py` (macro forecast 02:45, reachability Sun 07:00, …).
`atr-reopt-monthly` joins that list.

**Operate from one place:**

```bash
# manual / Studio debug
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/monthly_atr_reopt.py
```

Studio cron is only a last-resort run of that same CLI — never a second
schedule definition.

## 3. What the job does

1. Load 45d `nt_signal_sim` + 1h bars per symbol (forex majors pooled).
2. Free-lattice solve: wr∈[0.40,0.60], exp≤0.20, cost 0.1 ATR, **both
   halves EV>0**, then 0.05/0.01 refine.
3. Compare to `dynamic_atr.by_symbol` — flag **drift** when new TP/SL is
   &gt;15% from the published mid-grid **or** falls outside current clamps.
4. Write `worklog/kairos/reports/YYYYMMDD_atr_reopt.md`.
5. Heartbeat `nt_roadmap(atr_monthly_reopt)` (summary JSON in `current_state`).

**Never** writes `feature_flags.yaml`. Operator GO applies new clamps
(same discipline as the vol_ratio build).

## 4. Proposal → live (unchanged process)

```
monthly re-opt report
  → review (pulse-check / human)
  → edit by_symbol clamps (+ vol_ratio band if needed)
  → git commit + Studio pull
  → heavy keeps local daily pick inside the new box
```

New symbols: shadow → own 45d walk → `by_symbol` → live (see VOL_REGIME worklog).

## 5. Kill / rollback

- `atr_reopt_enabled: false` in `config/feature_flags.yaml` (hot-reload).
- Or leave it on: it is read-only against live geometry.

## 6. Root `test*.py` triage (same change)

July-2026 standalone smokes at repo root (hardcoded `/home/claude` paths,
not collected by `pytest tests/`). **Moved to `scripts/legacy_tests/`** so
they stay available as historical smoke runners without polluting the root
or the real suite. Official tests remain `tests/test_*.py`.

---

## Evidence

- Research basis: `reports/20260930_vol_regime_atr.md`, `_diag.md`, `20260930_per_symbol_atr.md`
- vol_ratio definition: `tiered_atr.compute_vol_ratio` — **volatility, not volume**
