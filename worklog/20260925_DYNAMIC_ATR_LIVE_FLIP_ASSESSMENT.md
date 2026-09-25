# Dynamic / tiered ATR — live-flip assessment (hybrid promote first)

> **Priority:** P0 economics (geometry) · **Status:** assessment + decision (no flags flipped in this file) · **Date:** 2026-09-25  
> **Parent:** `worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md` · `worklog/20260919_HEAVY_GEOMETRY_PROMOTE_P0_P1_P2.md`  
> **Evidence:** `nt_heavy_sweep_atr_anchored_shadow` (heavies 2026-09-24/25) · light `tp_distance_atr` / `geometry_risk_hat` 7–14d · scorecard + live expectancy rollup  
> **Roadmap rows:** `atr_anchored_evidence_mode_flip`, `dynamic_atr_mode_flip`, `heavy_sweep_atr_anchored_full_flip` (this worklog)

---

## 0. Terminology (authoritative)

| Phrase | Maps to | Meaning |
|---|---|---|
| **Wide grid / fixed brick params** | `config/brick_params.yaml` (`brick_size`, `sl_bricks`, `tp_bricks`) | Heavy-sweep **price-% candidate grid** winner. **Sole writer of live SL/TP.** Produces `tp_distance_atr` ~10–50 → reachability bucket **`4+`**. This is what the live book uses today. |
| **Tight ATR** | ATR-anchored grid candidates (~0.15 / 0.3 / 0.6 × ATR brick sizes) | Land in **0.5–1 / 1–2 / 2–4**. Shadow label: *widened*. |
| **Dynamic ATR** | Tiered / risk-capped **picker** (`dynamic_atr_mode`, `risk_capped_pick`, `should_promote_atr_candidates`, cap `dynamic_atr_risk_cap=0.35`) | Chooses among candidates under a `geometry_risk_hat` cap. Not the bricks themselves — the selection rule that would write the tight ones. |

Related flags (all **shadow** as of this worklog):

| Flag | Role |
|---|---|
| `heavy_sweep_atr_anchored_bricks_mode` | Extra ATR candidates in the heavy grid |
| `atr_anchored_evidence_mode` | P0.2 hybrid promote: admit ATR candidates when evidence rule fires |
| `dynamic_atr_mode` | Risk-capped pick comparison / later live pick |

---

## 1. Clarifications (operator Q&A, 2026-09-25)

### 1.1 What is the P0.4 bar?

From `20260919_HEAVY_GEOMETRY_PROMOTE_P0_P1_P2.md`:

| ID | What it is |
|---|---|
| **P0.2** | **Evidence promote rule** (`atr_anchored_evidence_mode`) — if shadow pick **changed**, widened long-lag **better**, and Sharpe(widened) ≥ baseline → allow ATR combos into the live pick **pool** for that symbol only. |
| **P0.3** | Wire that pool into deadline-aware pick. |
| **P0.4** | **Full-mode live flip** — set `heavy_sweep_atr_anchored_bricks_mode: live` so **every** symbol’s grid is ATR-wide, not just promoted ones. |

**Go-live wording (P0.4):** *“Stage 1 table green **or** hybrid evidence promote already live with improved `tp_atr` / long-lag on promoted symbols for one cycle.”*

So “P0.4 bar not met” means: we do **not** yet have (a) a green Stage 1 report used as the flip gate, nor (b) hybrid promote **running live** with a clean heavy cycle showing promoted-symbol `tp_atr` / long-lag improved. Shadow comparisons exist; **live promoted-symbol impact does not**. That is why full flip is premature — not because ATR candidates look bad (they look good), but because the only bar that measures *live* impact has not been run.

### 1.2 Why is RR / `min_rr` not a blocker?

Operator is correct that **`min_rr` is inert on light qualification** (locked 2026-09-20): live brick pairs already sit at RR = `tp_bricks/sl_bricks` ≈ **1.67–2.0+**, so `rr >= 1.5` almost never binds; rejects are kelly / regime / p_win. We **keep** `min_rr: 1.5` as the single source — do not tighten it as a quality lever.

**Where RR still appears (and why tight ATR does not automatically break it):**

```text
RR = tp_bricks / sl_bricks          # brick COUNTS, not ATR distance
tp_distance_atr = tp_bricks * brick_size / ATR
```

ATR-anchored candidates primarily change **`brick_size`** (finer bricks / scale), while `sl_bricks` / `tp_bricks` ratios in the candidate set are still filtered by `quality_filter(..., min_rr=1.5)` in `tiered_atr.py`. So:

- If ATR candidates keep `tp_bricks/sl_bricks ≥ 1.5` (e.g. still 6/3), **`min_rr` stays inert** — no light-path change, no new quality gate.
- If a candidate used a lower count ratio (e.g. 4/3 = 1.33), it would be **dropped from the heavy pick pool** by the existing geometry pre-filter — that is grid hygiene, not a new product rule.

**Correction to the prior assessment:** tighter TP/SL *distance* does **not** by itself change RR or re-activate `min_rr` as a quality lever. The only check on flip is: ATR candidates that win the shadow still pass the **existing** heavy `min_rr` pre-filter (RR ≥ 1.5 on brick counts). No MIN_RR code change required; do not touch `min_rr` / `dynamic_min_rr_mode`.

---

## 2. Evidence snapshot (as of 2026-09-25)

### 2.1 ATR-anchored shadow (strong)

| Heavies 09-24 / 09-25 | |
|---|---:|
| Symbols | 14 |
| Comparisons | 208–230 |
| Pick changed | ~86–89% |
| Widened Sharpe **>** baseline | **13 / 14** |

Typical: baseline bucket `4+`, widened `0.5-1` / `1-2` / `2-4`, Sharpe lifts (AAPL, US500, USDCHF, XAUEUR, …). XAUUSD can stay `4+` (tie) — not every symbol needs the same band.

### 2.2 Live path (still wide)

- Light 7d: avg `tp_distance_atr` **~29**, avg `geometry_risk_hat` **~0.60**, **~5.5k / 8.5k** over cap 0.35.
- 14d bucket `4+`: ~8.2k rows, **~54% expired**, `p_win_bracket` ~0.10.
- Reachability `4+` FX cells often **~97–99% expired**.

### 2.3 Scorecard / calibration (unchanged by geometry shadow)

- 7d qualified: n=49, win_closed ~21%, exp ~50%, closed PnL small, `p_dir` 0.61 vs `p_win_bracket` 0.11.
- Raw bias still heavily overconfident on gold/crypto/FX; `bracket_accept` still 0% win / −1R.

**Geometry is the clearest pending win. It does not fix the calibration event error (E1).**

---

## 3. Decision

| Flip | Call | When |
|---|---|---|
| **`atr_anchored_evidence_mode: live`** (hybrid promote only) | **GO** | Next Studio config deploy |
| `dynamic_atr_mode: live` (risk-capped pick) | Conditional | After **1 clean heavy cycle + ~7d** on promoted symbols |
| `heavy_sweep_atr_anchored_bricks_mode: live` (full grid) | Conditional | **P0.4** — Stage 1 green **or** hybrid shows promoted `tp_atr`↓ and long-lag↓ for one cycle |

### Hybrid promote (what GO means)

`should_promote_atr_candidates()` already implements P0.2 fail-closed:

1. Shadow pick **changed** vs baseline  
2. Widened geometry risk / long-lag **≤** baseline (and ≤ cap when available)  
3. Deadline Sharpe(widened) **≥** baseline  
4. Missing metrics → **no promote**

Then `compose_deadline_candidate_pool` admits ATR combos for that symbol only. Live pick still risk-capped under `dynamic_atr_risk_cap` when `dynamic_atr_mode=live` later.

### Kill bar (promote → dynamic live)

Do **not** proceed if, on promoted symbols vs unpromoted controls, for one heavy cycle + 7d light:

- `pct_expired` flat or **up**, or  
- closed expectancy / `pnl_dollars` (TP/SL) **worse** at similar n, or  
- `tp_distance_atr` does **not** move toward preferred bands (0.5–2).

Rollback: `atr_anchored_evidence_mode: shadow` (and keep `dynamic_atr_mode` shadow).

### Explicit non-goals this flip

- No `min_rr` / `dynamic_min_rr` change  
- No `p_win_bracket_mode` / `p_win_in_ev_mode` flip (E1 stays 09-30)  
- No settlement / OHLC change  
- No B5 live gate flip on this evidence alone  

---

## 4. Expected impact (hybrid → dynamic → full)

| Surface | Today | Hybrid promote | Dynamic + full ATR live |
|---|---|---|---|
| `brick_params.yaml` | Wide %-grid | Only promoted symbols may select tighter combos | Systematically 0.5–2 ATR TP |
| Expiry | 50–80% on wide FX | Down on promoted symbols | Should fall platform-wide |
| `p_win_bracket` | ~0.10 | Up where geometry tightens | Up (p_resolve ↑) |
| Volume / qualified | Thin | Slightly up where bracket scores improve | Up if fail-closed passes scored cells |
| MIN_RR / light gates | Inert RR | Unchanged | Unchanged if RR counts ≥ 1.5 |
| Calibration honesty | Still overconfident | **Unchanged** | **Unchanged** until E1 |

---

## 5. Follow-ups (nt_roadmap)

| mechanism | review | condition |
|---|---|---|
| `atr_anchored_evidence_mode_flip` | +3d | Flip hybrid live; confirm promote log / pool admission on ≥1 symbol |
| `dynamic_atr_mode_flip` | +7d after hybrid | Promoted vs control: exp%↓, tp_atr↓, closed PnL↑ |
| `heavy_sweep_atr_anchored_full_flip` | +14d | **P0.4** Stage 1 green or hybrid one-cycle proof |
| `min_rr_geometry_guard` | +14d | Spot-check winning ATR combos still RR ≥ 1.5 on brick counts (no code change unless fail) |

---

## 6. Operator checklist (hybrid go)

1. Set `atr_anchored_evidence_mode: "live"` in `config/feature_flags.yaml` only.  
2. Deploy Studio; confirm heavy log shows promote / pool admission (not silent).  
3. After next heavy: `SELECT symbol, n_compared, n_changed, last_baseline_bucket, last_widened_bucket, last_widened_sharpe FROM nt_heavy_sweep_atr_anchored_shadow`.  
4. After +7d light on promoted symbols: `tp_distance_atr`, `pct_expired`, closed `pnl_dollars` vs control symbols.  
5. Only then discuss `dynamic_atr_mode: live`.
