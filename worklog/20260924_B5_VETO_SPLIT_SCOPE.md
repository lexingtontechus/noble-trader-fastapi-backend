# B5 veto split — risk-only vs bracket-only vs both (SCOPE → BUILT)

> **Date:** 2026-09-24 · **Status:** **built (shadow, no live behavior change)** — design revised by assessment §0; original scope §1–§6 kept below, superseded parts struck  
> **Parent:** `20260920_QUALIFIED_QUALITY_GATES_MACRO_HYGIENE.md` (B2/B5)  
> **Evidence:** `20260923_QUALIFIED_GEOMETRY_SHADOW_STAMP.md` · pulse 2026-09-24  
> **Roadmap:** `light_publish_geometry_gate` (overdue 2026-09-22 → review 2026-09-30) · D1/D2 blocked on this review

---

## 0. Assessment, decisions & build (2026-09-24)

Checked against code (`orchestrator.py` B block, `geometry_risk.py`,
`p_win_bracket.py`) and live `nt_signal_sim`.

### 0.1 Veracity of the scope

| Scope claim | Verdict | Evidence |
|---|---|---|
| B computes `_risk_over` / `_br_reject` separately, then collapses them | ✅ true | orchestrator B block (pre-change) |
| Total reject rate is inflated by the bracket floor | ✅ true | live `p_win_bracket` p50 0.076, max 0.310 vs floor 0.20 → ~79% bracket-reject by design |
| Three distinct causes (risk_only / bracket_only / both) | ❌ **effectively two** | 0.2 |
| "Nudge cap in shadow" | ❌ **not possible** | 0.3 |
| Needs migration 165 + a new column | ❌ unneeded (165 is already `nt_geometry_incubation`) | cause is derivable from `geometry_risk_hat` + `bracket_gate_shadow` |
| `risk_reject_rate` alone picks the action | ⚠️ incomplete | 0.4 |

### 0.2 Risk and bracket are the same signal

Both read the same `nt_bracket_reachability` row (`signal=all`):
`long_lag = pct_expired → geometry_risk_hat`, `p_resolve = 1 − pct_expired → p_win_bracket`.

Light rows, last 7d, both scored:

| n | corr(risk, bracket) | risk_only | both | bracket_only |
|---:|---:|---:|---:|---:|
| 7,152 | **−0.866** | **12 (0.2%)** | 4,769 | 1,123 |

The 13 stamped would-qualify rows: **9 both · 4 bracket_only · 0 risk_only**.
`risk_reject_rate = 9/13 = 0.692` (10 symbols, one day, 12/13 open). Not decision-grade.

### 0.3 `dynamic_atr_risk_cap` is shared with live consumers

`heavy_publish_geometry_gate: live` (`heavy_sweep.py` ~1511),
`geometry_tuning_sweep_mode: live` (orchestrator T3 block),
`eod_correction.py` ~415, `heavy_sweep.py` risk-capped picks (~885, ~1121).
Editing the flag changes live behavior. Cap candidates must be **replayed
offline** from stored `geometry_risk_hat` (`b5_cap_replay`).

### 0.4 The cap currently separates symbols, not trades

Reconstructed gate-pass cohort (regime_conf ≥ 0.60, effective_kelly ≥ 0.02,
ev > 0), first row per (symbol, side, hour), resolved, 14d, R = `pnl_bricks/sl_bricks`:

| arm (cap 0.35) | episodes | symbols | TP | SL | expired | expectancy R |
|---|---:|---:|---:|---:|---:|---:|
| kept | 135 | 6 | 7.4% | 49.6% | 43.0% | −0.346 |
| vetoed | 235 | 13 | 5.1% | 20.4% | 74.5% | +0.137 |

`b5_cap_replay`: vetoed R > kept R at every cap (0.35 / 0.45 / 0.60 / 0.80 / 1.00).
**This is symbol-confounded; it does not show the cap is backwards:**

- 8 symbols are 100% vetoed (AUDUSD, EURUSD, GBPUSD, US30USD, US500, USDCAD,
  USDCHF, USDJPY); AAPL is 100% kept. Risk is mostly a per-symbol property
  (bucket `pct_expired`), not a per-trade one.
- No symbol has n ≥ 20 on **both** arms (closest: BTCUSD 34/5, XAUAUD 52/3, XAUEUR 9/13).
- The vetoed arm's positive R is mostly mark-to-market on expiries (74.5% expired).

**Conclusion:** no evidence for or against the cap. Do **not** tune on this
data. The natural frame is **D2 (per-symbol)**.

### 0.5 Decisions (answers to §6)

1. **B5 bar = `risk_reject_rate`** (share of would-qualify rows with
   `geometry_risk_hat > cap`, i.e. risk_only + both). **Plus** a required
   outcome condition: a cap tune needs within-symbol vetoed < kept with
   n ≥ 20 per arm. Replay caps offline; never "nudge in shadow".
2. **B drops the bracket clause now, not later.** R2's bracket block runs
   before B in the same capture block and already sets `qualified=False` when
   `p_win_bracket_mode: live`, which would make B's clause dead code. The real
   risk was the opposite flip order: B live while R2 was still in shadow would
   push the 0.20 floor live and bypass R2's sequence (2026-10-07). This now
   matches the heavy gate (risk cap + deadline Sharpe, no bracket).
3. **Sample floor = whichever is LATER: n ≥ 50 stamps AND ≥ 7 days AND ≥ 8 symbols.**
   14d is a timeout: report as underpowered, no cap change. Accrual is ~7
   qualified/day (43 over 09-18→23), so the earliest date is ~2026-09-30.

### 0.6 Revised decision table (replaces §3.4)

| risk share | within-symbol vetoed vs kept (n≥20/arm) | Action |
|---|---|---|
| any | vetoed ≥ kept, or no symbol clears n≥20/arm | **No cap change.** B stays shadow. |
| ≤ 0.30 | vetoed < kept | Cap fine; B stays shadow pending flip bar. |
| 0.30–0.70 | vetoed < kept | Pick cap from `b5_cap_replay`; change only if heavy/T3/eod consumers accept it, else split out a light-only cap. |
| > 0.70 | vetoed < kept, concentrated in a few symbols | **D2** per-symbol cap. |

**Flip bar (`light_publish_geometry_gate: live`):** decision-grade sample **and**
within-symbol vetoed < kept. Never on total reject rate.

### 0.7 Shipped (replaces §4 S1–S5)

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/geometry_risk.py` | new pure `light_geometry_would_reject(geometry_risk_hat, *, risk_cap)`; `None` fails open |
| `regime_platform/sweep_orchestrator/orchestrator.py` (B block) | uses it; bracket clause + `_min_br` removed; `false` stamped **only when risk is scored** (before: stamped even when unscored); logs show `cap=` instead of bracket |
| `supabase/migrations/166_qualified_geometry_shadow_risk_only.sql` | **COMMENT only**: documents new semantics + the pre-2026-09-24 era; no schema/data change |
| `scripts/pulse_check_r2.py` | `b5_geometry_reject_rate` → `b5_risk_share` (risk derived from `geometry_risk_hat`, so both eras read the same way; `decision_grade` flag), `b5_veto_outcomes`, `b5_cap_replay`; cap read via `get_dynamic_atr_risk_cap()` |
| `config/feature_flags.yaml` | comment on `light_publish_geometry_gate` (value unchanged: `shadow`) |
| `tests/test_qualified_geometry_shadow.py` | +4: risk-over veto + strict `>`, fail-open on `None`, signature has no bracket input, B block no longer reads `_p_win_bracket` / `get_min_p_win_bracket` |

**Not changed:** `dynamic_atr_risk_cap` 0.35, `min_p_win_bracket` 0.20, every
mode flag, the R2 bracket block, the heavy path, D1/D2.

**Stamp semantics from 2026-09-24:** `true` = risk over cap; `false` = risk
scored and within cap; `NULL` = unscored or not would-qualify. Pre-change
`true` rows include bracket-only vetoes. B5 SQL derives risk from
`geometry_risk_hat`, so the era break doesn't affect it.

### 0.8 Verification

- `tests/test_qualified_geometry_shadow.py` + `tests/test_light_gate_macro_edge.py`: **12 passed**.
- Full suite: **1145 passed, 21 skipped** (208s).
- `scripts/pulse_check_r2.py` live: `b5_risk_share` → 13 / 10 symbols / risk_over 9 /
  risk_and_bracket 9 / bracket_only 4 / 0.692 / `decision_grade=false`.
- Migration 166 applied (Management API, HTTP 201; column COMMENT verified). `nt_roadmap.light_publish_geometry_gate` updated: condition = §0.6, review_at 2026-09-30.

### 0.9 Next review

`nt_roadmap.light_publish_geometry_gate` → 2026-09-30. Run
`scripts/pulse_check_r2.py`, check `decision_grade`, then do a per-symbol
split of `b5_veto_outcomes` before applying 0.6.

---

## 1. Problem

B2 vetoes would-qualify light signals with a single boolean:

```
would_reject = (geometry_risk_hat > dynamic_atr_risk_cap=0.35)
            OR (p_win_bracket scored AND p_win_bracket < min_p_win_bracket=0.20)
```

B2 stamps only `qualified_geometry_shadow` (true/false). B5 then scores
**reject_rate = would_reject / would_qualify**.

**Live sample (pulse 2026-09-24):** `13 / 13 = 1.000` — bar is `>~0.70` →
“tune cap, do not flip live.” But that recommendation is **blind**:

| Driver | Why the single flag cannot say |
|---|---|
| **risk-only** | `geometry_risk_hat > 0.35` — tune `dynamic_atr_risk_cap` (B5’s actual lever) |
| **bracket-only** | `p_win_bracket < 0.20` — **expected** on this scale (live max ~0.31, p50 ~0.08). This is the R2 product gate, not a bad cap |
| **both** | tighten neither in isolation; geometry and bracket disagree with the live book |

Mixing R2’s bracket floor into a geometry **cap** scorecard conflates two
different levers. B5’s “tune cap” only applies to the **risk** share.

---

## 2. Goal

Make B5 actionable: report reject share by **cause**, then act on the right lever.

| Cause class | Definition (at evaluation time) | Action if dominant |
|---|---|---|
| `risk_only` | risk_over **and not** br_reject | Tune `dynamic_atr_risk_cap` (and/or D2 per-symbol) |
| `bracket_only` | br_reject **and not** risk_over | **Do not** tune risk cap. Keep 0.20 as R2 floor; consider **excluding** this class from the B5 “geometry strangle” bar |
| `both` | risk_over **and** br_reject | Fix geometry first (risk), re-score bracket |
| `pass` | neither (today’s `qualified_geometry_shadow=false`) | — |

---

## 3. Proposed design (original scope — superseded by §0)

> ~~3.1–3.3 veto-reason column~~: not built. Cause is derivable in SQL (§0.1, §0.7).
> ~~3.4 decision table~~: replaced by §0.6 (the "nudge cap in shadow" row is impossible, §0.3).

### 3.1 Persist cause, not just a bool

**Option A (preferred):** one text/enum column + keep the bool.

```sql
-- migration 165 (sketch)
ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS qualified_geometry_veto_reason TEXT;
  -- 'risk_only' | 'bracket_only' | 'both' | NULL (no veto / gate not evaluated)

-- keep qualified_geometry_shadow as the boolean roll-up
-- true  ⇔ reason IS NOT NULL
-- false ⇔ gate evaluated and passed
-- NULL  ⇔ fail-open / not evaluated
```

**Option B:** two booleans (`geometry_risk_over`, `p_win_bracket_under`).  
Slightly cheaper at write time; harder to index “the veto class” in SQL. Prefer A.

### 3.2 Capture at the B gate (orchestrator ~4330)

Already computes `_risk_over` and `_br_reject` separately — only the stamp
collapses them. Change is local:

```text
reason =
  risk_over and br_reject → 'both'
  risk_over               → 'risk_only'
  br_reject               → 'bracket_only'
  else                    → NULL  (and qualified_geometry_shadow=false when shadow+pass)
```

Fail-open unchanged (B3): unscored risk **and** unscored bracket → no reason, no bool stamp.

**Out of scope:** changing either threshold, flipping the gate live, heavy path, D1/D2.

### 3.3 B5 scorecard (replace the single rate)

```sql
SELECT
  qualified_geometry_veto_reason,
  count(*) AS n,
  round(count(*)::numeric / nullif(sum(count(*)) OVER (), 0), 3) AS share
FROM nt_signal_sim
WHERE source = 'sweep-light'
  AND qualified_geometry_shadow IS NOT NULL
  AND ts >= now() - interval '14 days'
GROUP BY 1
ORDER BY n DESC;
```

Plus a one-row summary:

```sql
SELECT
  count(*) AS would_qualify,
  count(*) FILTER (WHERE qualified_geometry_shadow) AS would_reject,
  count(*) FILTER (WHERE qualified_geometry_veto_reason = 'risk_only') AS risk_only,
  count(*) FILTER (WHERE qualified_geometry_veto_reason = 'bracket_only') AS bracket_only,
  count(*) FILTER (WHERE qualified_geometry_veto_reason = 'both') AS both,
  -- geometry-strangle bar EXCLUDES bracket-only (R2 floor is intentional)
  round(
    count(*) FILTER (WHERE qualified_geometry_veto_reason IN ('risk_only', 'both'))::numeric
    / nullif(count(*), 0), 3
  ) AS risk_reject_rate
FROM nt_signal_sim
WHERE source = 'sweep-light'
  AND qualified_geometry_shadow IS NOT NULL
  AND ts >= now() - interval '14 days';
```

### 3.4 Decision table (after ≥50 would-qualify stamps, or 14d — whichever first)

| `risk_reject_rate` | Dominant reason | Decision |
|---:|---|---|
| ≤ 0.30 | mostly `bracket_only` | Geometry is fine. Keep 0.20. **Do not** tune cap. Optionally re-baseline B5 bar to `risk_reject_rate` only; leave gate in shadow until R2 flip evidence |
| 0.30–0.70 | mixed | Tune `dynamic_atr_risk_cap` in small steps (e.g. 0.35 → 0.40–0.45) **in shadow**; re-score 7d |
| > 0.70 | mostly `risk_*` | **Tune cap** (B5 as written). Consider D2 (per-symbol) if reject is concentrated (e.g. XAU* vs FX) |
| any | `both` large | Fix risk first; bracket is then a second filter on a smaller set |

**Flip bar (unchanged):** `light_publish_geometry_gate: live` only when
`risk_reject_rate` is in the tuned band **and** shadow outcomes look better
than would-qualify (expectancy / expire). Not on raw `would_reject` including
bracket-only.

---

## 4. Work breakdown (when approved to build)

| ID | Work | Size |
|---|---|---|
| ~~S1~~ | ~~Migration 165: `qualified_geometry_veto_reason`~~ → **166 COMMENT-only** | S |
| ~~S2~~ | ~~`NT_SIGNAL_SIM_COLUMNS` + payload kwarg~~ → not needed | — |
| S3 | ~~derive reason~~ → **B gate drops bracket clause** (`light_geometry_would_reject`) ✅ | S |
| S4 | Tests: risk-over / strict `>` / fail-open / no bracket input ✅ | S |
| S5 | `pulse_check_r2.py`: `b5_risk_share` + `b5_veto_outcomes` + `b5_cap_replay` ✅ | S |
| S6 | Worklog + `nt_roadmap.light_publish_geometry_gate` condition text ✅ | S |

**Not in this pass:** threshold changes, live flip, heavy gate parity extras, D1/D2.

---

## 5. Acceptance

1. New stamps land with mutually exclusive reasons; `qualified_geometry_shadow` stays consistent (`true` ⇔ reason not null).
2. Pulse shows **risk vs bracket share** on ≥7d of light would-qualify rows.
3. Decision table applied once; `nt_roadmap` note records which lever (or “no cap change”).
4. Full pytest green; no behavior change to `qualified` while gate is `shadow`.

---

## 6. Open questions (for you)

1. **B5 bar definition:** use `risk_reject_rate` (exclude `bracket_only`) as the 0.70 strangle test — **recommended** — or keep total reject rate?
2. **Bracket-only in the gate live path:** if R2 later owns `min_p_win_bracket`, should B2 drop the bracket clause and leave geometry-only? (Avoids double-gating when `p_win_bracket_mode: live`.)
3. **Sample floor:** re-score at n≥50 would-qualify or 14d?

---

## 7. Related

- `worklog/20260920_QUALIFIED_QUALITY_GATES_MACRO_HYGIENE.md` — B1–B5, D1/D2  
- `worklog/20260923_QUALIFIED_GEOMETRY_SHADOW_STAMP.md` — B2 stamp + B5 SQL  
- `worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md` — `min_p_win_bracket: 0.20` scale  
- `nt_roadmap`: `light_publish_geometry_gate` (overdue) · `p_win_bracket_mode` (2026-10-07)
