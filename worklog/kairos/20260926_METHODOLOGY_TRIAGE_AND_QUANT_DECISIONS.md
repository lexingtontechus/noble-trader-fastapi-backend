# Kairos methodology triage and quant decisions (2026-09-26)

**Type:** decision record + cut-list. Supersedes nothing in the V3 data; it **narrows** the open research surface and fixes the worktree/main integration.
**Parent scope:** `worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md`
**Evidence:** P1–G1 (§11–13), calibration steps 1–3 (§14.1–14.3), geometry replay (§14.5), path-risk/KM (`20260925_PATH_RISK_AND_REGIME_SURVIVAL.md`).

---

## 0. Integration decision: main, not a long-lived worktree

| | |
|---|---|
| **State at decision** | `kairos` = `origin/kairos` = `origin/main` = `5b5c797` (one commit past local `main` `570b85b`: `20260926_payoff_revert_backfill.py`). Worktree clean. |
| **Decision** | **Fully integrate into `main` and retire `.worktrees/kairos`.** |
| **Why** | The research program is now a **measurement + ops-lever layer** inside the live backend (stamps, views, flags, jobs). A parallel worktree duplicates the whole `worklog/kairos` tree, drifts from Studio deploys, and adds no isolation benefit once G1 has fired and the live levers are flag-gated on `main`. |
| **Sanitize** | Merge `kairos` → `main` (fast-forward). Drop the worktree (it holds `__pycache__` under `worklog/kairos/backfill/`). Keep a single source of truth under `noble-trader-fastapi-backend/worklog/kairos/`. The untracked `scripts/update_nt_roadmap_kairos_0926.py` is kept and committed (roadmap upsert helper). |
| **Going forward** | Feature branches for code that must not land mid-week (resolver/correction). Research reports and scope edits land on `main` directly. No long-lived `kairos` worktree. |

---

## 1. Methodology score (what we keep vs cut)

| Dimension | Grade | Keep |
|---|---|---|
| Statistical hygiene | A− | Circular-shift nulls, symbol-day cluster bootstrap, pre-registration, Holm, no-skill line, geometry-free m_h |
| Self-correction | A | Withdrawals (R-based age/knobs, tight-SL), G1 stop rule, forking-path flags |
| Live-safety | A− | `kairos.py` read-only for trading; levers shadow-first |
| Research→product fit | C+ | Strong on calibration ops; weak on `p_win` ranking |
| Efficiency | C | Thesis thrash, inverted lever order, dual ATR scale |
| Complexity budget | D+ | L0–L8 + 10-factor catalogue + G2.1–G2.6 ahead of evidence |

**Do not “simplify away” the nulls, cluster bootstrap, no-skill line, or geometry-free separation.** Those are why the corpus is trustworthy.

---

## 2. Product bottleneck (frozen)

> **The live question is not “does some regime have edge.” It is: do the 7 gates select direction, and does `p_win` rank outcomes within a cell?**

| Fact | Implication |
|---|---|
| Qualified p_win ≈ 0.60, realised TP rate ≈ 0.26 | Level is overconfident |
| Realised TP rate flat across p_win bins (0.24–0.30) | **p_win does not rank** |
| Pooled AUC 0.48; constant no-skill beats p_win on Brier | Corrections mostly shrink toward base rate |
| Calibration-error carryover half-life **~3h**, gone by 24h | Fast evidence only |
| q+s evidence pool Δ Brier **−0.118** vs all-tier **−0.043** | **Who counts** beats **when it arrives** |
| Geometry: recorded brackets expire 43% and lose 0.87 ATR/signal | Geometry is a separate, already-mapped problem |
| 14 symbols ≈ **5.3** independent bets | Cluster risk is real and unused |

Everything below is subordinate to that bottleneck.

---

## 3. Quant decisions (binding for Kairos work)

### D1 — One primary research question
**G2.2** (do gates select geometry-free direction, `Δm₂₄` qualified∪suppressed vs gate_fail) is the **primary** confirmatory test.
**G2.3** (cooldown cost) is **secondary** and informational for the cooldown owner.
**G2.4 / G2.5** (edge decay with age; non-memoryless hazard) are **demoted to optional**. P1 already disproved both on 1h debounced labels (hazard 0.09–0.13/h flat). Re-open only with a new pre-registration and a slower regime clock (4h).
**G2.1** is a data-health / stream-edge monitor, not a product gate.
**G2.6 factors** run only after G2.2 passes.

### D2 — `p_win` must gain discrimination or be replaced
Preferred order:
1. **Within-cell discrimination (scorecard I):** does `(p_win − no_skill)` rank `(1{TP} − no_skill)` inside symbol×regime×vol? Requires ≥ 30 decided per cell — currently thin; accumulate, do not invent more features yet.
2. If I stays flat: **stop treating p_win as a ranking score.** Emit a **calibrated cell rate** (shrunk TP-rate or no-skill+measured excess) and use gates that do rank (kelly/ev/regime_conf) for selection.
3. Only then consider new ranking features (recent same-direction TP rate already showed AUC 0.62 — the one positive lead).

Do **not** invest in transition/plane models to “fix calibration” of a non-discriminating score.

### D3 — Promote operational levers in this order (post-flip re-check)
| Order | Lever | Mode target | Bar |
|---|---|---|---|
| 1 | `calibration_evidence_pool_mode` → q+s | **live** | Re-run step 2/tier split on **post** 2026-09-25 ATR-flip and post 09-26 resolver-fix data; keep if ΔBrier still ≤ −0.05 on S2 |
| 2 | `calibration_resolve_hourly_mode` | **live** | Harmless if pool is live; coverage of non-INSUFFICIENT bias is the metric (F) |
| 3 | `eod_resolver_scan_mode: full` | already live | Keep; monitor write completeness (scorecard A) |
| 4 | Half-life 3h bias | shadow only | Do not promote until D2 ranking is measured |

### D4 — Era splits are mandatory
Structural breaks: **2026-09-22 debounce**, **2026-09-25 hybrid/dynamic ATR**, **2026-09-26 resolver full-scan + head-of-line backlog catch-up**.
Every scorecard read (A–I) reports **pre vs post** separately and compares only via no-skill (D) and levels (E). No pooled Brier across eras.

### D5 — Effective sample size (ESS) is a reported column
- Unit = episode (symbol × direction × hour), already in `v_kairos_signal_episodes`.
- Additionally report **symbol-day clusters** and, for any “n signals” claim on overlapping hourly rows, a note that ESS ≪ n.
- Geometry grids: never quote 8,252 as n; quote clusters / independent episodes.
- Power gate (existing §H): CI half-width of primary Δ ≤ 0.2·SD before any confirmatory claim. **Do not run new Holm families on n < 100 targets.**

### D6 — ATR scale
Kairos live metrics use **sweep ATR** (close-to-close SMA14). P1 OHLC ATR is historical only.
**Rule:** never mix magnitudes across scales in one table; if both appear, label the column `atr_scale`.

### D7 — Geometry is replay-first (already §14.5; enforce)
Bracket questions are answered by **offline walk** on stored entry/direction/ATR. A live shadow is only for gate-pass-rate changes. Current geometry hypothesis (handoff to geometry track, not Kairos):
- Fail region: wide TP + wide SL (current recorded) — expiry-heavy, negative expectancy.
- Candidate region: **TP 9–17 ATR, SL 0.5–2 ATR** (in-sample, both halves positive) — trend-following payoff shape.
- Dynamic/tiered ATR feasible set fixes **expiry**, not profitability (best ≈ +0.08 ATR/signal).
Do not re-mine the grid in Kairos; `tiered_atr.py` / geometry track owns it.

### D8 — Cluster exposure is a product track, not a Kairos hypothesis
Ship a **shadow cluster cap** (gold complex, US indices, EUR bloc; cap ~1.5× single-symbol Kelly per cluster). Needs Σ only — least overfit unused finding. Owner: risk/size track. Kairos supplies the correlation labels if asked.

### D9 — Archive (do not rebuild without a new scope)
| Item | Status |
|---|---|
| L4 quadrant plane, L5 flow/exit compass, L7 time knobs | **Parked** after P1 (flow = noise; V2 age knobs disproved) |
| HSMM / destination hazard models | **Do not build** (V4 memoryless on 1h) |
| §3.8 factor families 6–10 (liquidity, cross-asset, multi-TF, calendar, extra flow) | **Frozen**; only the 5 live stamps stay |
| V2 “edge decays with regime age” as a knob driver | **Disproved** on this clock |
| `/backtest/run` extension | **Out of scope** forever for this program |
| Order-book / L2 | **Parked** until an OHLCV flow proxy shows incremental value (it has not) |

### D10 — Script surface
Keep: `kairos_weekly_check.py` (G2 looks), `kairos_calibration_*.py` (scorecard G/H), `kairos_reachability.py`, `kairos_repeatability.py`.
Merge later: calibration trio → one CLI. Trim `kairos_discovery.py` plane/flow code paths if they are not run by G2.
Reports: one scorecard template (A–I + ESS + era); drop bespoke MD layouts.

---

## 4. Action checklist (build order)

| # | Action | Owner track | Done when |
|---|---|---|---|
| 1 | Integrate `kairos` → `main`, remove worktree, single worklog tree | platform | **Done 2026-09-26** (`b5a63b0`) |
| 2 | This decision record + scope §15 archive | research | **Done 2026-09-26** |
| 3 | Pure scorecard maths in `regime_platform/sweep_orchestrator/kairos_quant.py` (eras, no-skill, calibration error, ESS clusters, cluster risk) + `tests/test_kairos_quant.py` | research | **Done 2026-09-26** |
| 4 | Post-flip re-run: tier split + step 2 + scorecard A–I on post-09-26 data | research | Report with era split |
| 5 | Promote `calibration_evidence_pool_mode` if bar met | calibration | Flag `live` + monitoring |
| 6 | Promote `calibration_resolve_hourly_mode` | calibration | Flag `live` |
| 7 | G2.2 primary look at power bar; write go/no-go | research | One page decision |
| 8 | Scorecard I (within-cell discrimination) once cells ≥ 30 decided | research | I metric + call on D2 |
| 9 | Shadow cluster-cap stamps | risk | `kairos_cluster_open_risk` logged |
| 10 | Geometry track: replay-confirm wide-TP/tight-SL vs tiered ATR | geometry | Own scope, not Kairos |

---

## 5. Recommendation summary (quant view)

1. **Stop expanding the regime/edge-plane theory.** The 1h label clock is memoryless and the plane was noise. The alpha question is **gate selection + score ranking**.
2. **Treat calibration as a scoring problem, not a regime-transition problem.** Short carryover is real but secondary to “p_win doesn’t rank.”
3. **Ship the evidence-pool lever.** It is the largest measured, deployable S2 gain and is a filter, not a new model.
4. **Keep the research machinery that prevents false positives** (nulls, cluster bootstrap, pre-reg, ESS/power) and **delete the machinery that describes disproved layers**.
5. **One tree (`main`), shadow-first levers, replay for geometry.** That matches how the value is actually created in `noble-trader-fastapi-backend`.
