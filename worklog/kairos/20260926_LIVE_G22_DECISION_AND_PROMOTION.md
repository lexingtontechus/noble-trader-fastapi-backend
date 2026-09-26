# Live data read (2026-09-26) — G2.2 decision + promotion call

**Artefact:** `worklog/kairos/reports/20260926_postflip_rescore.md` (script `scripts/kairos_postflip_rescore.py`)
**Decisions parent:** `20260926_METHODOLOGY_TRIAGE_AND_QUANT_DECISIONS.md` D3–D5
**Data:** live `nt_signal_sim` light buy/sell (77,750 rows) + `nt_calibration_records` (38,853) · eras via `kairos_quant` · m₂₄ in sweep-ATR units, within-symbol demeaned

---

## 1. Population (why promotion is blocked)

| era (signal ts) | n | decided | qualified | suppressed | gate_fail |
|---|---:|---:|---:|---:|---:|
| pre_debounce | 71,654 | 34,668 | 968 | 11,329 | 59,357 |
| debounce_to_atr | 3,693 | 1,546 | 25 | 566 | 3,102 |
| atr_to_resolver | 1,274 | 123 | 8 | 114 | 1,152 |
| **post_resolver** | **1,129** | **20** | **2** | 259 | 868 |

Post-resolver decided outcomes: **22** (0 qualified S2). The D3 promote bar (ΔBrier ≤ −0.05 **on post-flip S2**) is not measurable yet.

---

## 2. G2.2 — do gates select direction? **DECIDED: NO PASS**

Pre-registration: Δm₂₄ = mean demeaned m₂₄(qualified ∪ suppressed) − mean(gate_fail); symbol-day cluster bootstrap; decide at §H power (CI half-width ≤ 0.2·SD).

| | |
|---|---|
| Episodes (matured m₂₄) | 9,407 · 419 symbol-day clusters |
| q∪s / gate_fail | 1,357 / 8,050 |
| **Δm₂₄ demeaned** | **−0.321 [−1.871, +0.821]** |
| Power | SD 10.03 · 0.2·SD 2.01 · CI half-width **1.35 → PASS** |
| Verdict | **NO PASS** — point estimate **negative**, CI spans 0 |

**Era split (demeaned Δm₂₄):**

| era | Δ | CI |
|---|---:|---|
| pre_debounce | −0.411 | [−2.13, +0.92] |
| debounce_to_atr | **+0.512** | [−1.88, +3.29] |
| atr_to_resolver | −3.880 | [−15.97, +2.54] (n=39) |

Quant reading:
- At the **pre-registered power bar**, we can decide G2.2. We **cannot** claim the gates select geometry-free 24h direction.
- The point estimate is the **wrong sign** for “gates help” (q∪s is worse than gate_fail on demeaned m₂₄).
- The only positive era (debounce_to_atr) has a huge CI; do not re-open on one era (D5).
- Both arms have **negative raw m₂₄** (q∪s −0.66, gate_fail −0.27): on this ATR scale the stream is not printing +24h drift. Consistent with V1 disproof (label direction ≠ edge).

**Pre-registered stop-rule consequence (scope §12.3):**
- G2.2 has **failed**. If G2.1 (all-signal edge vs circular null) also fails, close the time-knob track (job C) and report.
- Immediate product implication: **do not tune gates on the assumption they currently select direction**. Gate work should either change the *inputs* (discrimination) or the *objective* (calibration / risk), not the thresholds alone.

---

## 3. Scorecard D–E (level) — overconfident in **every** era

| era | tier | n | TP rate | no-skill | mean err | mean acc | mean claimed |
|---|---|---:|---:|---:|---:|---:|---:|
| pre_debounce | qualified | 438 | 0.258 | 0.332 | **+0.341** | −0.074 | +0.268 |
| debounce_to_atr | qualified | 11 | 0.273 | 0.332 | +0.320 | −0.059 | +0.261 |
| post_resolver | (decided) | 20 | 0.000 | 0.303 | **+0.623** | −0.303 | +0.320 |

- **D (edge vs no-skill): negative in every era.** Realised TP rate sits below the bracket coin-flip line.
- **E (calibration level): p_win overconfident everywhere** (claimed edge ~+0.27–0.32 vs realised acc ≤ 0).
- Suppressed in debounce_to_atr is extreme (TP 0.07 vs no-skill 0.32, err +0.59).
- Confirms triage D2: **level is broken and ranking is still unproven** (scorecard I needs ≥30 decided/cell — still thin).

---

## 4. Evidence-pool ΔBrier (proxy walk-forward, mixed eras)

| rule | S2 n | ΔBrier |
|---|---:|---:|
| all tiers, prod 3d HL, nightly | 423 | −0.080 |
| **q+s, prod 3d HL, nightly** | **397** | **−0.097** |
| q+s, 3h HL, hourly | 401 | −0.036 |
| gate_fail, 3h HL, hourly | 422 | −0.066 |

- Directionally **q+s still beats all-tier** (−0.097 vs −0.080), matching §14.3, but the gap is smaller than the earlier 446-target study (−0.118 vs −0.043).
- **3h half-life underperforms 3d** in this proxy — opposite of the earlier persistence study. Do **not** promote the half-life lever on this; require a byte-identical `fetch_calibration_bias` replay before any half-life change.
- This proxy is **not** a full production-rule replay (no episode merge of overlapping `[entered_at, closed_at]`, no ≥28 raw floor semantics identical to live).

---

## 5. Promotion call (binding)

| Lever | Call | Why |
|---|---|---|
| `calibration_evidence_pool_mode` | **HOLD in shadow** | D3 bar is **post-flip** S2; n=0 qualified decided post-resolver. Directional support exists but D4 forbids promoting on mixed-era Brier. |
| `calibration_resolve_hourly_mode` | **HOLD in shadow** | Timing lever is secondary (§14.3); promote only after pool is decided. Keep for coverage (F). |
| `eod_resolver_scan_mode: full` | **KEEP live** | Completeness fix; scorecard A. |
| 3h half-life bias | **DO NOT promote** | Proxy contradicts earlier half-life ranking. |
| G2.2 time-knobs / age knobs | **CLOSE / park** | Pre-registered NO PASS at power. |
| `p_win` as a ranking score | **FAILING** | D2 path: wait for scorecard I, else switch to calibrated cell rate. |

**Next measurement that unblocks promotion:** accumulate **≥30 decided qualified signals in `post_resolver`** (or 14 days of post-resolver exits, whichever first), then re-run `kairos_postflip_rescore.py` + a true `fetch_calibration_bias` tiered replay. Target n for the pool bar: ≥50 S2 targets in-era.

---

## 6. What this changes in the product narrative

1. The **7 gates do not currently demonstrate selection** on geometry-free 24h moves (G2.2). Discord “QUALIFIED” is not evidence of directional skill on this metric.
2. The live edge that *is* measurable is **calibration ops** (who counts as evidence, how complete the resolver is) — not regime-age knobs.
3. Geometry (tiered ATR / wide-TP tight-SL) remains the largest *structural* lever and is **out of Kairos** (replay-first geometry track).
4. Keep capture; stop expanding the plane/age theory (scope §15 already archived it).

---

## 7. Reproduce

```bash
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/kairos_postflip_rescore.py
# → worklog/kairos/reports/20260926_postflip_rescore.md
```
