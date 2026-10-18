# PBM Operator Flip Roadmap + Pulse Check Schedule

**Date:** 2026-10-14
**Type:** Roadmap / followup schedule
**Base:** HEAD `93386b7` (PBM Phases A–E deployed to Studio)

---

## 0. Current State (verified at HEAD `93386b7`)

| Phase | Flag | Current State | Live Behaviour | Flip Condition |
|---|---|---|---|---|
| **A** — measurement | `pbm_mode: "shadow"` | **ACTIVE** | Stamps + posterior writes. Zero publish change. | — |
| **B** — EV input swap | `pbm_ev_mode: "off"` | INERT | EV reads `p_win_bracket` (table lookup). | Phase A ledger log-score ≥ table lookup, era-clean |
| **C** — continuous geometry | `pbm_geometry_mode: "grid"` | INERT | Grid argmax via `select_tp_sl`. | replay book EV ≥ grid; band-centre 68/77 → 0 |
| **D** — sizing | `pbm_sizing_mode: "legacy"` | INERT | Platform Kelly unchanged. | C accepted (size honest only once price is) |
| **E** — drift harness | — | run-on-demand | `scripts/measure_pbm_drift_auc.py` | promote only if WF AUC CI > 0.5 |

**Key facts:**
- B does NOT flip to "posterior" — that's Phase C. B flips `off → shadow → live`.
- D flips `legacy → conservative`, not from B.
- B and C are independent. C does NOT require B to be live first.
- The retirement of `calibration_enforcement.py` requires D accepted.

---

## 1. Flip Sequence (dependency graph)

```
A (shadow) ─────────────────────────────────────────► always on
                                                    │
B (off → shadow → live) ──┬──► D (legacy → conservative)
                          │         │
C (grid → posterior) ─────┘         │
                                    ▼
                          retire calibration_enforcement.py
```

**Order:**
1. **B shadow** — observe Phase A ledger vs table lookup (1–2 weeks of stamps)
2. **B live** — EV reads posterior triple (after log-score ≥ table, era-clean)
3. **C posterior** — continuous geometry (after replay book EV ≥ grid, band-centre → 0)
4. **D conservative** — bound-scaled Kelly (after C accepted)
5. **Retire** — delete `calibration_enforcement.py` + gate block (after D accepted)

---

## 2. Pulse Check Integration

### 2.1 What to add to `scripts/pulse_check_r2.py`

```python
# ── PBM Pulse Check ──────────────────────────────────────────────────
# Run after the existing per-table recipes.
# Verifies: (a) tables exist, (b) stamps are landing, (c) drift layer verdict.

def pbm_pulse_check(supabase_client):
    results = {}

    # Table existence
    for table in ("nt_cell_posterior", "nt_pbm_shadow"):
        try:
            resp = supabase_client.table(table).select("*").limit(1).execute()
            results[f"{table}_exists"] = True
            results[f"{table}_count"] = len(resp.data)
        except Exception as e:
            results[f"{table}_exists"] = False
            results[f"{table}_error"] = str(e)

    # Phase A stamp freshness (latest nt_pbm_shadow row)
    try:
        resp = (
            supabase_client.table("nt_pbm_shadow")
            .select("ts,symbol,signal,p_w,p_w_null,gap,ev_lb_pass")
            .order("ts", desc=True)
            .limit(5)
            .execute()
        )
        results["pbm_shadow_latest"] = resp.data
        if resp.data:
            latest_ts = resp.data[0]["ts"]
            age_hours = (datetime.now(timezone.utc) - latest_ts).total_seconds() / 3600
            results["pbm_stamp_age_h"] = round(age_hours, 1)
            results["pbm_stamps_fresh"] = age_hours < 2.0  # within 2h = healthy
    except Exception as e:
        results["pbm_shadow_latest"] = None
        results["pbm_stamps_fresh"] = False

    # Cell posterior population (how many cells have n_eff > 0)
    try:
        resp = (
            supabase_client.table("nt_cell_posterior")
            .select("symbol,side,n_eff,p_w,p_w_null,gap")
            .gt("n_eff", 0)
            .limit(1000)
            .execute()
        )
        cells = resp.data
        results["pbm_cell_count"] = len(cells)
        if cells:
            gaps = [c["gap"] for c in cells if c.get("gap") is not None]
            results["pbm_gap_mean"] = round(sum(gaps) / len(gaps), 4) if gaps else None
            results["pbm_n_cells_gt_k0"] = sum(
                1 for c in cells if c.get("n_eff", 0) >= 30.0
            )
    except Exception as e:
        results["pbm_cell_count"] = 0
        results["pbm_error"] = str(e)

    # Drift layer verdict (from latest Phase E report)
    report_path = "worklog/kairos/reports/20261014_pbm_drift_auc.md"
    if os.path.exists(report_path):
        with open(report_path) as f:
            content = f.read()
        # Extract the verdict section
        verdict_section = re.search(
            r"## Verdict.*?(?=\n## |\Z)", content, re.DOTALL
        )
        results["pbm_drift_verdict"] = (
            verdict_section.group(0)[:500] if verdict_section else "not found"
        )
    else:
        results["pbm_drift_verdict"] = "report not run yet"

    return results
```

### 2.2 Pulse check output format

```
── PBM Pulse Check ──────────────────────────────────────────
  nt_cell_posterior: EXISTS (47 cells, 12 with n_eff≥k0)
  nt_pbm_shadow:     EXISTS (latest 0.3h ago — FRESH)
  gap mean:          +0.042 (entry timing damage)
  drift verdict:     STAY-OFF (WF AUC 0.4987, CI [0.48, 0.52])
────────────────────────────────────────────────────────────
```

### 2.3 When to run

| Check | Frequency | What it catches |
|---|---|---|
| PBM stamp freshness | Every pulse (1h) | Phase A not writing |
| Cell posterior population | Every pulse | Cells not accumulating |
| Gap mean | Every pulse | Entry-timing damage growing |
| Drift AUC (Phase E) | Weekly | Layer not beating null |
| Full pulse check | After each flip decision | Regression check |

---

## 3. Flip Schedule (calendar)

| When | Action | Owner | Bar |
|---|---|---|---|
| **2026-10-14** | A active (shadow) | done | — |
| **2026-10-21** | Run Phase E harness | operator | `python scripts/measure_pbm_drift_auc.py --since 7d` |
| **2026-10-28** | Review Phase E report + stamp volume | operator | Drift verdict + ≥50 stamps |
| **2026-11-04** | B → shadow (if log-score ≥ table) | operator | Phase A ledger ≥ table lookup |
| **2026-11-11** | B → live (if shadow period clean) | operator | 1 week of shadow stamps, no regressions |
| **2026-11-18** | Run replay: book EV grid vs posterior | operator | `scripts/replay_bands_era_clean.py --mode compare` |
| **2026-11-25** | C → posterior (if replay passes) | operator | Book EV ≥ grid; band-centre rate → 0 |
| **2026-12-02** | D → conservative (if C accepted) | operator | 1 week of posterior geometry, no regressions |
| **2026-12-09** | Retire calibration_enforcement.py | operator | D accepted + 1 week clean |

**Notes:**
- These are earliest dates. Flip only when the bar is met, not on the calendar.
- If Phase E drift verdict is STAY-OFF (expected), skip B/C/D and keep A (shadow) as measurement-only.
- The retirement of `calibration_enforcement.py` is the end state — it removes the shrink/replace machinery that PBM replaces.

---

## 4. Rollback

| If… | Then… |
|---|---|
| B live causes regression | `pbm_ev_mode: "off"` |
| C posterior causes regression | `pbm_geometry_mode: "grid"` |
| D conservative causes regression | `pbm_sizing_mode: "legacy"` |
| PBM stamps stop landing | Check `nt_cell_posterior`/`nt_pbm_shadow` exist (migration 210/211) |
| Everything goes wrong | `pbm_mode: "off"` — Phase A stops, everything else stays at defaults |

---

## 5. Key File References

| File | Purpose |
|---|---|
| `config/feature_flags.yaml:941-950` | All 10 pbm_* flags + defaults |
| `regime_platform/sweep_orchestrator/barrier.py` | Analytic null (Layer 1) |
| `regime_platform/sweep_orchestrator/posterior.py` | Cell posterior + EV + Kelly (Layers 2-3) |
| `supabase/migrations/210_nt_cell_posterior.sql` | nt_cell_posterior table |
| `supabase/migrations/211_nt_pbm_shadow.sql` | nt_pbm_shadow ledger |
| `scripts/measure_pbm_drift_auc.py` | Phase E drift AUC harness |
| `scripts/pulse_check_r2.py` | Existing pulse check (add PBM section) |
