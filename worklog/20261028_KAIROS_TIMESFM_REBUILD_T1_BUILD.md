# TimesFM rebuild T1 — the arm seam (build log)

**Date:** 2026-10-28
**Type:** Build (T1 of the rebuild plan; design = `worklog/20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md`).
**Scope:** the TimesFM sidecar becomes a selectable `vol_fan` arm — `variance_path` from the model's own q10..q90 log-price spread, stamped `source='tsfm_sidecar'` (whitelisted in migration 218's CHECK from day one — **zero migration**). Ships dark: `claims_vol_fan_mode: off`, `claims_vol_fan_arm: egarch`. Nothing live changes until both flags are flipped, and each flip is an operator decision with its own evidence gate.

---

## 1. What landed (4 files)

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/claims_tsfm_arm.py` | NEW — `TimesFMVolFanArm` (`name='tsfm'`): URL env chain `TIMESFM_INFERENCE_URL → TIMESFM_URL → TIMESFM_BASE_URL → localhost:8002` (both full-URL and base-URL conventions normalized), key from `TIMESFM_API_KEYS` (comma-split first) with `TIMESFM_API_KEY` fallback, lazy `/health` probe cached per instance (5 s), one bounded retry on 429/exception (10 s timeout — the orchestrator convention), context = last **512** log closes, 9- and 10-level quantile layouts both parsed, `variance_path` = the design §2 derivation. |
| `regime_platform/sweep_orchestrator/claims_vol_fan.py` | `_ARM_NAMES` gains `tsfm`; `_ARM_SOURCE` map keys the row's `source` (tsfm → `tsfm_sidecar`, GARCH family unchanged); `_arm_instance` tsfm branch (lazy import, fail-open); `build_garch_price_fan` reads `last_negative_increments` from the arm into build meta; the registration stamps `tsfm_clamped_increments` into `claimed_probs` when non-zero. |
| `regime_platform/core/feature_flags.py` | `get_claims_vol_fan_arm()` whitelist + `_DEFAULTS` comment — `tsfm` legal, **default stays `egarch`**. |
| `config/feature_flags.yaml` | `claims_vol_fan_arm` comment block: the tsfm option + the flip discipline (only after the T2 artifact carries tsfm cells AND the GARCH-era baseline shows on the leaderboard). |

**Zero changes** to: the resolver, the leaderboard, migrations, `server.py`, the direction path (`p_timesfm`/z-map/EV blend — untouched by construction), both sweep call sites (verified: both dispatch `asyncio.create_task(_register_vol_claim(...))` with the same arm-agnostic signature — arm selection is flag-driven inside `register_vol_fan_claim`).

## 2. The variance-path derivation (as shipped)

Request: last 512 closes, **log-transformed** (levels, not diffs — the deliberate difference from the research arm `TimesFM25HTTP`, which feeds per-bar log RETURNS and reads per-step spreads). The forecast point at horizon h is then the log PRICE at T+h, so:

- `σ_total(h) = (q90(h) − q10(h)) / (2·Φ⁻¹(0.90))` — the model's own term structure, unmodified;
- `var_path[h−1] = σ_total(h)² − σ_total(h−1)²` (telescoping) — `cumsum(var_path)` reproduces `σ_total(h)²` at every h exactly (verified A1);
- vol-contraction increments (negative) clamp to `1e-12` and are **counted** → build meta → `claimed_probs["tsfm_clamped_increments"]` (honesty over silence);
- quantile CROSSING (`q90 < q10` — malformed output, not contraction) rejects the whole path;
- the existing degenerate guards (per-bar σ ≤ 0.5, terminal σ ≤ 10, scale ∈ (0, 15]) apply unchanged downstream in `build_garch_price_fan` — a blown fan is rejected whole, never truncated.

## 3. Decisions worth recording

1. **The arm lives in `claims_tsfm_arm.py`, not `vol_backtest/model_arms.py`.** The design doc said "reuse the TimesFM25HTTP plumbing" — honored at the pattern level (probe/key/layout/retry), not the import: the claims module stays free of the vol_backtest import graph, so an absent `arch`/`scipy` can never take the TSFM arm down with it. T2's harness registration will import this class explicitly under the name `tsfm` so recalib cells mint with the SAME arm key the claims layer looks up (`(symbol, horizon, "tsfm")`) — the mismatch trap (cells keyed `timesfm25` vs claims asking `tsfm`) is documented here so T2 does not fall into it.
2. **`claimed_probs` carries the clamp count.** The design doc said "stamped in the claim meta" — but build meta is not persisted as columns (only `arm` + `calib_level` are). `claimed_probs` is the row's only free JSONB; the resolver's brier fields read named keys only, so the stamp is inert to scoring. Implementation note, recorded as a deviation from the design doc's letter, not its intent.
3. **Health probe, not forecast-storm.** Availability = httpx imports; reachability = one cached 5 s `/health` probe at the first `variance_path` call — a down service costs one probe per claim build, never a retry loop (the 429/exception retry is bounded to one extra attempt).
4. **`min_bars=25` arm floor** (sanity only) — the claims machinery already enforces `VOL_FAN_MIN_BARS=720` upstream; the arm floor mirrors the research arm's sanity convention, nothing more.

## 4. Verification record

Throwaway suite `/home/z/verify/t1_verify.py` (not committed, per the A/B/D/E discipline) — **49/49**:

- **A (14)**: telescoping exact for iid √h (per-bar variance `s0²` to 1e-15); contraction clamps counted (10) and floored at `1e-12`; 10-level vs 9-level layouts byte-identical; quantile crossing / zero spread / NaN / missing body / short rows / health-503 / forecast-500 all ⇒ `None`; 429-then-200 retried exactly once (2 attempts); sub-floor window and non-positive prices ⇒ `None`; request carries exactly the last 512 log closes at horizon 48.
- **B (7)**: 48-row fan, monotone q05<…<q95, q50 = entry, log-symmetry `q_X·q_(100−X) = entry²` (1e-5); √h term structure; tsfm cell `m = k·exp(a)·F^(b−1) = 2` doubles the terminal log-width exactly (`calib_level='cell'`); absurd per-bar σ ⇒ whole-fan reject; clamp count reaches build meta; **3 repeated builds byte-equal** (code-path determinism); the egarch branch's contract intact (fail-open `None` in this arch-less sandbox).
- **C (2)**: Monte-Carlo, 600 draws × 48 h from the fan's own law → coverage **0.500/0.802/0.900** vs nominal 0.50/0.80/0.90; `claims_resolver.fan_metrics` consumes the tsfm fan (crps > 0, coverage keys present).
- **D (12)**: mode `off` ⇒ zero service calls, zero POSTs; `shadow`+`tsfm` ⇒ exactly one POST with `kind='vol_fan'`, `source='tsfm_sidecar'`, `arm='tsfm'`, 48-row fan, bracket_ref, probs, `deadline = ts + 48h` exact, `calib_level='none'` (the real artifact has no tsfm cells — honest identity); contraction stamped `tsfm_clamped_increments=10`; service down ⇒ no claim + zero POSTs; insert 500 ⇒ fail-open `None`; broadcast payload with publish off ⇒ no crash; window-fetch failure ⇒ no claim; garbage arm falls back `egarch`; both flag surfaces whitelist `tsfm`.
- **E (2)**: identical fans score identically regardless of arm stamp (kernels never see `arm`/`source`); fan row schema keys exactly `{h, q05..q95}`.
- **F (2)**: both production call sites dispatch fire-and-forget (`asyncio.create_task` — structural, zero call-site changes); full registration < 5 s in-sandbox (and it runs detached in production).

**Existing suites**: claims-adjacent set (eod_resolver, settlement_contract, eod_lookback_wiring, eod_resolver_basis, recalib, eod_timesfm_config_migration, vol_backtest) — 132 passed / 11 failed, and the stash round-trip proves the 11-failure set **byte-identical at HEAD** (pre-existing, environment-dependent in this sandbox — arch absent). `test_heavy_sweep_shadow_mode_composition` 8/8. ruff: all three touched files clean. `py_compile` clean. YAML parses.

## 5. Honest limitations

- **The service-side determinism bar is not yet measured against the real model.** The code path is deterministic (no RNG anywhere in the arm; 3 builds byte-equal), and the server config pins `fix_quantile_crossing`/eval-mode — but design §7.1's "N ≥ 3 real /forecast calls on one frozen window → byte-equal fan" can only be checked on the deployment where `server.py` actually runs. First-live checklist item.
- **The arm is zero-shot on price levels** — NOT the lab arm that passed 5/15 vol cells (covariate-conditioned, log-RV targets). It may lose to EGARCH-t. That is the point of the ledger: the leaderboard will say so, per symbol and horizon, with p-values.
- **Clamps slightly under-represent vol contractions** (each clamped increment contributes `1e-12` instead of a negative) — the distortion is bounded by 48·1e-12 in cumulative variance and auditable per row via the stamp.
- **The light path self-fetches its 2160-bar window** (unchanged Phase D machinery); the heavy path passes its own window with no timestamps, so the calendar filter never applies there — same asymmetry as the GARCH arms, kept deliberately (same code path for all arms, no special-casing).
- 5-clause contract intact: additive, shadow-first, flag-gated; no geometry/EV/gate/resolver/MT5 changes; price space at the boundary; one settlement resolver; immutable claims.

## 6. Operator actions

- **None required to deploy T1.** No migration; ships dark (`claims_vol_fan_mode: off`, `claims_vol_fan_arm: egarch`).
- Pending from Phase D, unchanged: flip `claims_vol_fan_mode → shadow` after the next deploy installs `arch` — that starts the GARCH-era baseline clock on the leaderboard.
- The T3 flip (`claims_vol_fan_arm → tsfm`) only after T2's artifact carries tsfm cells AND the GARCH-era baseline shows on `GET /claims/leaderboard` — evidence, not calendar.
- First-live check (when the sidecar runs where `server.py` lives): re-run the frozen-window byte-equality probe against the real service (design §7.1).

## 7. T2 hooks (next phase)

- Register `TimesFMVolFanArm` in `scripts/run_vol_backtest.py`'s catalog under the name **`tsfm`** (cells must mint with the claims layer's arm key — see §3.1).
- The harness needs a `forecast()`-shaped adapter: `rv = σ_total(H)` from `variance_path` (the cumulative derivation, matching the fan's `σ_raw` exactly — the MZ center then applies to the identical quantity). The class ships `variance_path` only; the adapter is a T2 file.
- Mint cells: `run_vol_backtest.py --arms garch,gjr,egarch,tsfm --horizons 6,12,24,48` → `python -m vol_backtest.recalib` → artifact refresh (the loader is mtime-hot-reloaded, no restart).
