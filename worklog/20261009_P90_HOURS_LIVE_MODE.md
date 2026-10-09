# Worklog: per-signal p90 settlement expiry (dynamic deadline live mode)

**Date:** 2026-10-09
**Scope:** Replace the fixed `EXPIRY_BARS = 48` settlement horizon with a per-signal expiry derived from `nt_bracket_reachability`, shadow-first.
**Status:** **Built + shipped at `shadow`/`fixed`.** Flag `settlement_expiry_mode` defaults to **`fixed`** (byte-identical to today); `shadow` is the calibration run; `live` is the flip.

---

## 1. The finding that drives the design

The fixed 48h bar is **right for wide brackets and wrong for tight ones**, asymmetrically. Measured across all `nt_bracket_reachability` buckets (n≥30):

| ATR bucket | n buckets | median p90 | p90 range | median settle | avg expired |
|---|---|---|---|---|---|
| 0.5-1 | 24 | **26.1h** | 2.3–35.3 | 7.1h | 0.0% |
| 1-2 | 52 | **23.0h** | 2.8–55.0 | 4.3h | 0.0% |
| 2-4 | 42 | **26.0h** | 2.6–55.1 | 12.1h | 0.0% |
| 4+ | 56 | **56.1h** | 29.2–78.2 | 41.3h | **54.3%** |

Against the flat 48h bar, of 180 buckets: **120 (67%) have p90 ≤ 48h** (the bar over-generous), **60 (33%) have p90 > 48h** (the bar too short). Median p90 across all buckets is **30.9h** — the fixed 48h sits well above the central tendency.

**The trap:** the naive "expiry = ceil(p90)" is wrong. A 1-2 ATR bracket settles in **4.3h median**; under a p90 rule it would get a ~23h leash — *looser*, not tighter. The market-driven expiry is **bracket-aware**:
- **tight/mid buckets** (settle fast, ~0% expire) → expiry from the **median × safety**, so a bracket that hasn't resolved in a small multiple of its typical settlement time is expired.
- **wide bucket (4+)** → expiry from **p90**, because that bucket has a real tail (54% expire, median 41h, p90 56h); cutting it at 48h mislabels in-progress resolution as expiry.

## 2. The fractional-ATR question (user-raised)

> Should ATR buckets be fractional (not rounded up) and reflect dynamic_atr?

**Checked — the current buckets aren't even being used as buckets.** The continuous `tp_distance_atr` spans a **10× range** across symbols (median 5.7 → 63.1) and the bucket table only has edges at 0.5/1/2/4, so **everything above 4.0 collapses into one "4+" bucket** (p90 29–78h within it). The coarse ladder throws away the actual signal.

**But fractional bins would be too sparse to be reliable.** Probing the stored `tp_distance_atr` at 0.25-ATR resolution: fine bins have **n=1–3 each**. You cannot compute a trustworthy p90 on 1–3 samples.

**So neither "fractional buckets" nor the current 5-bucket scheme is right.** The principled fix is **continuous, per-symbol normalization** — express `tp_distance_atr` relative to the symbol's own geometry (its `sl_distance_atr`/`tp_distance_atr` ratio, or the dynamic_atr band) and regress settlement hours on the continuous value. That is a **separate scope** (it rebuilds the reachability key) and is deliberately **not** bundled into this lever.

**This is exactly why it ships shadow-first:** shadow logs the dynamic expiry on the *existing coarse bucket* so we can observe whether the coarse key even yields a usable expiry distribution — before committing to the continuous rekey. If shadow shows the "4+" bucket produces one useless expiry for a 29–78h span, that's the empirical proof the rekey is necessary, and we'd do it with evidence.

## 3. Current state (what "live" actually means)

The `dynamic_deadline` tier today is **metadata-only**:
- `DeadlineTier.decay()` has **no caller**.
- The tier's median is **pinned at ~42–45h for every symbol and vol-tier** (BTCUSD 44.5h, XAGUSD 43.9h) — near-identical pinning is the tell: it's derived from the same reachability rows the 48h resolver already truncated, so it measures the fixed horizon, not an independent cadence.
- It only **stamps columns** on `nt_signal_sim` (`expected_resolution_hours`, `soft/hard_deadline_hours`).

**Settlement is hard-coded.** `eod_resolver.py:79` sets `EXPIRY_BARS = 48`; both resolve call sites passed it literally. **Those two sites were the entire seam** — the resolve functions already accept a per-call `expiry_bars`; nobody overrode it.

**Therefore "live mode" = passing a per-signal `expiry_bars` into those two sites instead of the constant.**

## 4. What was built

| Piece | Where | Shape |
|---|---|---|
| `dynamic_expiry_hours(reach_row, ...)` | `settlement_expiry.py` (new) | pure: tight→`median×safety`, wide(`4+`)→`p90`, clamped `[floor, cap]`; `None` on no/insufficient evidence → fail-open |
| `hours_to_bars(hours, fixed_bars)` | `settlement_expiry.py` | pure: `ceil(hours)`, clamped to `[1, fixed×1.5]` (=72), `None`→fixed |
| `_fetch_reachability_index()` / `_ensure_reach_index()` / `_prime_reach_index()` | `eod_resolver.py` | one reachability fetch per resolve run (run-scoped `_REACH_INDEX`), so `_judge`/`_judge_bars` stay pure (no per-signal I/O) |
| `_reach_row_for(sig, index)` | `eod_resolver.py` | lookup by `(symbol, vol_tier, bucket)` with a vol_tier-agnostic fallback; derives the bucket from `tp_distance_atr` when the label is absent |
| `_dynamic_expiry_bars(sig)` | `eod_resolver.py` | returns `(bars, log_info)`; `fixed`→`(48, None)`, `shadow`→`(48, info)` [compute+log, settle unchanged], `live`→`(dyn, info)` |
| wiring | `eod_resolver.py` both `_judge` sites + minimum-age gate | `expiry_bars` threaded from `_dynamic_expiry_bars`; `_ensure_reach_index()` primed at the top of `resolve_pending_signals` |
| flags | `feature_flags.py` + `feature_flags.yaml` | `settlement_expiry_mode` (fixed\|shadow\|**live**→default `fixed`) + `floor_hours` 12 + `cap_hours` 72 + `tight_safety_mult` 2.0 |

**Ships at `fixed`** — zero live change today, byte-identical to the historical behaviour. Flip path: `fixed → shadow` (observe the log delta) → `live` (one config edit, no restart). The `minimum-age gate` was updated to use the chosen `_expiry_bars` so a young signal isn't force-expired at the dynamic horizon either.

## 5. Critical interaction — resolver-basis consistency

This ships **after** and **compatible with** the 2026-10-08 intrabar harmonization. `reachability.p90_hours` is computed on **stored `outcome`** (the live intrabar resolver's verdicts), so the percentile and the resolver already agree on basis. That consistency is why this lever is safe to derive from reachability rather than a re-walk — the same lesson as geometry ≠ reachability.

## 6. Risks (why it stays shadow until observed)

1. **Feedback loop.** Changing expiry changes the `expired` label → changes `pct_expired` → feeds reachability, calibration bias, and the adaptive incubation floor. A bad expiry contaminates the table used to derive it. Shadow logs the delta before any label moves.
2. **The 4+ extension is the sharper edge** — it converts some current `expired` into eventual `tp_hit`/`sl_hit`. That *raises* measured win rate over time, but only by correctly resolving signals that were being force-closed. Must not be mistaken for edge.
3. **Resolution lag** — extending wide buckets to 72h means those signals take 3 days to settle, slowing the calibration loop for 4+ geometry specifically.

## 7. Verification plan (shadow → live)

- **Shadow:** log per-signal `{symbol, bucket, n, dynamic_hours, dynamic_bars, fixed_bars}`. Confirm tight-bucket expiries predominantly reclassify `expired`→tp/sl (mislabel correction) and wide-bucket extensions predominantly convert `expired`→tp/sl, not the reverse.
- **Promotion bar:** the fraction of `expired` labels a dynamic expiry reclassifies as tp/sl should be **substantial**; the fraction that flips a would-be tp into an sl should be **small** (that would mean changing outcomes, not just timing).
- **Flip to live** only after the shadow deltas match §1.

## 8. Files changed

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/settlement_expiry.py` | **new** — pure `dynamic_expiry_hours` + `hours_to_bars` |
| `regime_platform/sweep_orchestrator/eod_resolver.py` | reachability index (run-scoped), `_reach_row_for`, `_dynamic_expiry_bars`, wiring at both `_judge` sites + min-age gate, `_ensure_reach_index()` at resolve start |
| `regime_platform/core/feature_flags.py` | `get_settlement_expiry_*` getters + defaults (`fixed`) |
| `config/feature_flags.yaml` | `settlement_expiry_mode: fixed` + floor/cap/safety |
| `tests/test_settlement_expiry.py` | **new** — pure math (tight/wide/floor/cap/fail-open) + flag defaults |

Full suite at ship: **1893 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs` — unrelated).

## 9. Next step (not built — separate scope)

The continuous / per-symbol-normalized ATR rekey (§2). Gated on the shadow run showing whether the coarse bucket is usable. That is the change that would make the expiry genuinely reflect `dynamic_atr` rather than a 5-rung global ladder.
