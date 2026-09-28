# P0 Scope — Event-time edge half-life estimator (T2)

**Date:** 2026-09-27 · **Type:** research scope + read-only tool · **Parent:** `20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md` §2, R1 result §8  
**Depends on:** `kairos_rank.py` (event_window, transition_type), `scripts/kairos_rank_transition.py` pipeline  
**Non-goal:** any live gate, size knob, LLM, or schema change

---

## 0. Decision this scope answers

| # | Question | Status |
|---|---|---|
| **T2** | Does edge decay in **event time** after a transition, with a stable half-life `h` by type? | **Done 2026-09-27** — **supported for `dir_flip` (h≈6h) and pooled (h≈12h)**; vol-only types not supported. See `reports/20260927_halflife_t2.md` |

If T2 fails (no stable `h`, or slope sign-unstable like V2 age), kill half-life as a product knob and keep only the coarse `fresh`/`deep` contrast already in `rank_score`.  
If T2 passes, `h` becomes the **E-Layer** clock for freshness weighting and the R-Layer τ feature — still shadow until R3.

---

## 1. What exists (do not rebuild)

| Piece | Where | Role in P0 |
|---|---|---|
| Cell transitions + type | `kairos_rank.transition_type`, migration 167 stamps | Stratify |
| Event windows τ | `kairos_rank.event_window` (pre/fresh/warm/stale/deep) | Coarse grid (already reported) |
| Ranking target | `y_acc = 1{TP} − SL/(TP+SL)` | Primary |
| Event-time tool | `scripts/kairos_rank_transition.py` | **Extend**, don’t fork |
| Circular-shift null | same script / V1–V4 machinery | Keep |
| Unit | episode (symbol × direction × hour) | Keep — raw-row CIs lie |

**Already known (R1, 2026-09-26):** fresh − deep y_acc = **+0.117**; `dir_flip` only non-negative type; surprise IC ≈ 0 (drop as gate feature).

---

## 2. Estimator (pre-registered)

For each transition event at T₀ and each decided episode with event-time τ ≥ 0 (τ = decision/entry age since T₀, hours):

```text
y_acc  = 1{TP first} − SL/(TP+SL)          # decided only; never expired in y
x      = τ                                   # continuous, not just window bins

Per (type ∈ {dir_flip, both, vol_expand, vol_contract}):
  1. Binned means: τ ∈ [0,1) [1,3) [3,6) [6,12) [12,24) [24,48) [48,∞)
  2. Exponential fit (only if ≥ 3 bins with n ≥ 30):
       y_acc(τ) ≈ a + b · 2^(−τ/h)
     equivalently fit log-linear on demeaned series:
       IC(τ) = IC0 · 2^(−τ/h)   →   h = ln(2) / |slope|
  3. Null: circular-shift T₀ times vs outcomes (1000 draws) → null h / null ΔIC
  4. Cluster CI: symbol-day bootstrap on the fresh−deep (or slope) contrast
```

**Primary contrast (keep R1):** `fresh − deep` on y_acc.  
**T2 pass bar (new):** exponential fit in at least **one** type with

- slope < 0 (decay), Holm-corrected p < 0.05 against circular-shift null,
- `h` finite and **sign-stable across 2 chronological halves** (not the V2 failure mode),
- symbol-day cluster CI on the decay contrast excluding 0.

**Secondary (report only):** same fit on `y_bin`, on post-ATR subset, and on episode-deduped vs raw (raw is diagnostic only).

---

## 3. Stratification (frozen)

| Key | Levels | Why |
|---|---|---|
| `type` | dir_flip, both, vol_expand, vol_contract | H3: do not pool |
| `era` | pre-debounce / post-debounce / post-ATR | R1 mixed-era warning |
| Geometry | recorded/dynamic bracket D | Economics lives under ATR |

Do **not** re-open age-of-regime, plane, surprise-as-gate, or HSMM in P0.

---

## 4. Build (this change only)

| Artefact | What |
|---|---|
| `kairos_rank.py` | Add pure helpers: `event_time_halflife(tau, y)`, `exp_decay_fit`, `halflife_null` (circular shift). No I/O. |
| `scripts/kairos_halflife.py` | Read-only report (same style as `kairos_rank_transition.py`): tables + T2 verdict |
| `tests/test_kairos_halflife.py` | Unit: synthetic exp decay recovers h; flat series → no T2; type split |
| Report | `worklog/kairos/reports/20260927_halflife_t2.md` |

Capture unchanged. No migration. No gates. No flags. No LLM. No TimesFM change.

---

## 5. Stage gates

| Gate | Pass |
|---|---|
| **P0.1** | Tool runs on live decided+transition join; binned table by type published |
| **P0.2** | **T2 verdict** (supported / reversed / no-stable-h / underpowered) with nulls + half-split |
| **P0.3** | If T2 supported: document `h` per type in Kairos README; still **research-only** |
| **P0.4** | Explicit kill note if T2 fails — freshness stays coarse (`fresh`/`deep`) in `rank_score` |

Promotion of `h` into `rank_score` weights or Kelly mult is **out of P0** (that is R4 / later, needs post-ATR power).

---

## 6. Data & power

- Join: decided episodes ⋈ nearest prior cell change (existing event-time logic in `kairos_rank_transition.py`).
- R1 mass: fresh n≈24.8k, deep n≈5.2k decided; **post-ATR is thin (n=43 rank-score episodes)** — T2 may be underpowered on the live geometry era; report that as `underpowered`, not as a negative, and prefer all-eras + post-debounce for the discovery fit.
- Min cells for a type-level `h`: 3 τ-bins × n≥30 decided episodes.

---

## 7. Out of scope (explicit)

- Text / LLM / macro pivot channel (that is P2).
- TimesFM quantiles (P1).
- R-Layer model (P3).
- Changing `rank_score` weights, gates, Kelly, or EV blend.
- McLean–Pontiff-style publication clocks (different origin).

---

## 8. Local LLM note (binding for P0)

**P0 needs zero LLMs.** Half-life is pure survival/decay statistics on prices + labels you already store.  
If/when P2 adds a text channel, prefer a **small extract model** (LFM / Qwen / Llama 7–14B class or OpenRouter/NVIDIA route already used for `nt_macro_forecast`) — not BloombergGPT-scale, not a local TSFM.

---

## 9. P0 result (2026-09-27)

| Stratum | n | b (fresh lift) | h | p_b | half-split | Verdict |
|---|---:|---:|---:|---:|---|---|
| **dir_flip** | 12,871 | +0.205 | **6h** | 0.00 | b>0 both halves (h 12h/3h) | **supported** |
| vol_expand | 10,446 | +0.102 | 12h | 0.17 | b sign flips | not_supported |
| vol_contract | 8,828 | −0.063 | 6h | 0.78 | b sign flips | not_supported |
| both | 4,388 | −0.132 | 3h | 0.95 | unstable | not_supported |
| pooled | 36,533 | +0.124 | **12h** | 0.00 | stable | supported |

T1 refresh: fresh − deep = **+0.121**. Build: `kairos_rank.exp_decay_fit` / `halflife_null`, `scripts/kairos_halflife.py`, `tests/test_kairos_halflife.py`.  
**Research-only (P0.3).** Do not wire `h` into `rank_score` until post-ATR mass.
