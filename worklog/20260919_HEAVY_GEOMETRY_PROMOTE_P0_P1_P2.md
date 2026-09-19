# Heavy geometry promote + measurement close — P0/P1/P2

> **Priority**: P0 · **Status**: P0+P1 built (evidence promote shadow; publish gate live; raw fix) · **Date**: 2026-09-19  
> **Trigger**: Heavy-sweep pulse — ATR-anchored shadow beats baseline in Sharpe; live pick still wide; 27× `predicted_p_win_raw` missing; Stage 1 SQL column mismatch.  
> **Parent**: `20260918_TIERED_ATR_GEOMETRY_SCOPE.md` · **Roadmap**: `nt_roadmap` ATR/reachability `review_at=2026-10-05`

---

## Decisions

### 1) Wait 1 more heavy sweep before flip?

| Option | When it is right |
|--------|------------------|
| **Wait one cycle** | Only to **complete Stage 1 properly** (fix shadow SQL column, n≥10/symbol, post-leak clean window) — **not** for “more hope” |
| **Do not wait for data** | US30/US500/USDCHF already show ATR-anchored **dominance** in logs (e.g. US30 brick 12 vs 259, Sharpe 1.75 vs 0.37) |

**Recommendation:**

1. **Implement P0/P1 now** (no wait) — evidence promote **rule**, heavy publish gates, measurement fixes.  
2. **Use one more heavy sweep as the verification cycle** for a **symbol-level ATR live promote**, not a blank wait.  
3. **Global full flip** of `heavy_sweep_atr_anchored_bricks_mode=live` can wait for clean Stage 1 SQL **or** run after this next cycle if every active symbol passes the rule.

**Bottom line:** Waiting **purely** for another sweep is **not** prudent — you already have decisive shadow evidence and are still publishing **wide** geometry (US30USD qualified at **deadline Sharpe −0.51**). Waiting **one sweep while P0/P1 ship** *is* prudent.

---

### 2) Do the 27 `predicted_p_win_raw` errors need digging?

**Yes — root-cause, not alarm.**

| Finding | Detail |
|---------|--------|
| Count | **27** in current log window |
| Pattern | Error from `regime.sweep.orch` **immediately adjacent** to `[sweep:heavy] … DONE` |
| Heavy grep of `p_win_raw` in `heavy_sweep.py` | **No matches** — heavy sim path likely never sets `predicted_p_win_raw` |
| Light path | Batch B expects `p_win_raw = ev_result.p_win` before enforce; heavy push/qualify path may skip that stash |

**Impact:** Heavy-qualified rows land in `nt_signal_sim` **without raw** → Brier/raw-bias skip or `raw_missing`; measurement honesty degrades on **exactly the signals you care about** (heavy publishes).

**Required dig (P1, not open-ended research):**

1. Trace `_build_signal_sim_payload` call sites in `heavy_sweep.py` — confirm `predicted_p_win_raw=` omitted.  
2. Capture pre-enforcement `p_win` before heavy enforce (parity with light).  
3. Add regression: heavy payload always carries raw when flag True.  
4. After fix: ERROR count → 0 on next heavy cycle.

No need for a multi-day investigation — it is a **missing argument / missing stash** on the heavy capture path.

---

### 3) Stage 1 SQL note

Roadmap condition references `nt_heavy_sweep_atr_anchored_shadow.computed_at` — live table uses **`updated_at`** (migration 135). Stage 1 query must use `updated_at` or Stage 1 **cannot run as written**.

---

## P0 — Unblock geometry (evidence-based, not calendar-only)

| ID | Work | Mode |
|----|------|------|
| P0.1 | **Stage 1 query kit** — `scripts/stage1_atr_anchored_review.py` (or SQL file): per symbol `n_compared`, `n_changed`, baseline vs widened bucket, `expectancy_r`, long-lag %; exclude contaminated shadow window if flagged; **column `updated_at`** | offline / report |
| P0.2 | **Evidence promote rule** — if `atr_anchored_mode` is shadow **and** latest heavy for symbol has `changed=True` **and** widened bucket long-lag **&lt;** baseline **and** deadline Sharpe(widened) **≥** baseline → allow **ATR candidate into live pick pool** for that symbol | `atr_anchored_evidence_mode: shadow\|live` (new flag) |
| P0.3 | Wire: when evidence rule fires, `deadline_candidates` includes ATR-anchored combos for that symbol **before** deadline-aware pick (fixes “better geometry never selected”) | heavy_sweep |
| P0.4 | **Full-mode live flip** — after P0.1 report + ≥1 clean heavy cycle: set `heavy_sweep_atr_anchored_bricks_mode: live` (roadmap-aligned) **or** keep hybrid if some symbols fail | config |
| P0.5 | Tests: evidence rule true/false; deadline pool composition; no promote when `none_survived` |

**Go-live (P0.4):** Stage 1 table green **or** hybrid evidence promote already live with improved `tp_atr` / long-lag on promoted symbols for one cycle.

**Non-goals P0:** conviction-widening TP; `p_win_bracket` live gate; `trend_hmm_live`.

---

## P1 — Heavy qualification + measurement

| ID | Work |
|----|------|
| P1.1 | **Heavy publish gate** — do not `QUALIFIED` / push if `deadline_aware_sharpe < 0` **or** `geometry_risk_hat > dynamic_atr_risk_cap` (US30USD −0.51 class) |
| P1.2 | **Heavy sim geometry** — pass `tp_distance_atr`, `sl_distance_atr`, `long_lag_hat`, `geometry_risk_hat`, `long_lag_risk` on heavy `_build_signal_sim_payload` |
| P1.3 | **Fix 27 raw errors** — stash `p_win_raw` before heavy calibration enforce; always pass `predicted_p_win_raw` into sim payload |
| P1.4 | **P1.3 regression test** — heavy payload raw present; ERROR path gone |
| P1.5 | Tag sim rows `source=sweep-heavy` already exists — scorecard filter heavy vs light when reviewing |

**Modes:** gates can ship **live** (negative-EV publish is not a research question). Geometry/raw capture: live write (measurement).

---

## P2 — Ops / data quality / adjacent

| ID | Work |
|----|------|
| P2.1 | Stage 1 docs + scripts use **`updated_at`** (not `computed_at`) |
| P2.2 | Optional: add `computed_at` alias column if you want roadmap SQL literal — **or** update `nt_roadmap` review_condition |
| P2.3 | Shadow insert failures (markov/trend/debounce) — batch/retry or drop volume; do not block geometry |
| P2.4 | **2026-09-21** `markov_source_mode` review — do **not** flip `trend_hmm_live` on current accuracy (legacy still better on most symbols) |
| P2.5 | Keep `zmap_live`; monitor distribution only |
| P2.6 | `nt_roadmap` note: record evidence-promote hybrid + date of Stage 1 run |

---

## Execution order

```text
Day 0 (now):     Build P0.1–P0.3 + P1.1–P1.4 (tests)
Deploy:          Studio restart — heavy publish gates live; ATR evidence shadow
Next heavy sweep: Observe [tiered:atr] / ATR promote / tp_atr / long_lag / qualified
If Stage 1 or evidence green: P0.4 flip ATR-anchored live (or hybrid permanent)
P2:              Follow-ups; Sep 21 Markov review
```

## Success metrics

| KPI | Direction |
|-----|-----------|
| Avg `tp_distance_atr` on new signals | **↓** toward ≤2 |
| `long_lag_risk` share | **↓** |
| `p_win_bracket` on qualified | **↑** |
| Heavy QUALIFIED with neg deadline Sharpe | **0** |
| `predicted_p_win_raw missing` errors | **0** |
| 7d dual-report PnL | **↑** |

## Rollback

- `atr_anchored_evidence_mode: off` → previous pool  
- `heavy_sweep_atr_anchored_bricks_mode: shadow`  
- P1.1 gate: feature flag if needed (`heavy_publish_geometry_gate`)

---

## Immediate build (on approval)

1. Stage 1 review script + `updated_at`  
2. Evidence promote into deadline pool + flag  
3. Heavy no-publish if deadline Sharpe &lt; 0 or risk &gt; cap  
4. Heavy sim geometry + `predicted_p_win_raw` fix + tests  

**Awaiting:** instruction to **build P0+P1** (and optional **wait one heavy sweep** then flip P0.4).

---

## Build log — P0 + P1 + raw errors (2026-09-19)

| Piece | Detail |
|-------|--------|
| **P0.1** | `scripts/stage1_atr_anchored_review.py` — uses **`updated_at`** |
| **P0.2** | `should_promote_atr_candidates()` — long-lag improvement rule |
| **P0.3** | `compose_deadline_candidate_pool()` — promote only when `atr_anchored_evidence_mode=live` |
| **Flags** | `atr_anchored_evidence_mode: shadow`, `heavy_publish_geometry_gate: live` |
| **P1.1** | Heavy QUALIFIED blocked if `deadline_aware_sharpe < 0` or geometry risk &gt; cap |
| **P1.2** | Heavy sim captures `tp_distance_atr`, `geometry_risk_hat`, `long_lag_*`, `bracket_*` |
| **P1.3** | `p_win_raw_heavy` stashed **before** calibration enforce → `predicted_p_win_raw` + `p_win_dir` on heavy sim rows (**fixes 27 ERROR logs**) |
| **Tests** | `tests/test_p0_p1_geometry_promote.py` |

### Verification

```
pytest tests/ → 1087 passed, 21 skipped
```

### Flag defaults after build

| Flag | Default | Effect |
|------|---------|--------|
| `atr_anchored_evidence_mode` | **shadow** | Promote evaluated + logged; pool stays original until flipped |
| `heavy_publish_geometry_gate` | **live** | No QUALIFIED on neg deadline Sharpe / high geometry risk |
| `heavy_sweep_atr_anchored_bricks_mode` | shadow | Unchanged (roadmap) |

### Next deploy check

1. Restart Studio  
2. Next heavy sweep: `[atr:evidence]`, `[geometry:risk]`, no `predicted_p_win_raw missing`  
3. `scripts/stage1_atr_anchored_review.py`  
4. Commit + push when green; flip `atr_anchored_evidence_mode: live` after promote table looks right  

P2 (Markov Sep 21, roadmap SQL docs) not built.
