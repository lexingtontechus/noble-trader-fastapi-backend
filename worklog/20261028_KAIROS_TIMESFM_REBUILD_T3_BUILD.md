# TimesFM rebuild T3 — the arm flip (build log)

**Date:** 2026-10-28
**Type:** Build (T3 of the rebuild plan; design = `worklog/20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md` §5 T3/§8).
**Scope:** exactly what the design's T3 is — **one global, auditable switch**:
`config/feature_flags.yaml` `claims_vol_fan_arm: "egarch" → "tsfm"`. Zero code
changes (T1 built the arm seam, T2 built the mint path; T3 is the
pre-registration the design doc §5 describes). The flip **ships dark**:
`claims_vol_fan_mode` stays `"off"`, so nothing live changes — the vol_fan
claim still does not register on any cadence until the Phase D shadow deploy.

---

## 1. What landed (1 file, 1 line + the decision record)

| File | Change |
|---|---|
| `config/feature_flags.yaml` | `claims_vol_fan_arm: "tsfm"` (from `"egarch"`). The comment block rewritten as the flip record: operator decision, shipped DARK (mode off), evidence-gate status recorded honestly (below), flipping back is one line. The vocabulary comment (`garch|gjr|egarch|tsfm`) and the tsfm arm description (T1-era text) retained. |

**Zero changes** to: `claims_vol_fan.py`, `claims_tsfm_arm.py`,
`feature_flags.py` (`_DEFAULTS["claims_vol_fan_arm"]` stays `"egarch"` — the
code fallback; the yaml is the deliberate operator surface, per the
config-centralization doctrine), the resolver, migrations, both sweep call
sites, `server.py`.

## 2. The evidence gates — status recorded honestly

The design doc §8 states the T3 flip's gates: (a) T2's artifact carries tsfm
cells, (b) the GARCH-era baseline shows on `GET /claims/leaderboard`. Status
at flip time, as recorded in the yaml comment and here:

- **(a) NOT yet satisfied.** T2's *mint path* is landed (`3d67c8d`) but the
  deployed `config/claims_vol_fan_recalib.json` carries `garch`/`gjr`/`egarch`
  cells only — the operator mint (the :8002-up CLI run) has not happened.
  Consequence, verified below: the flipped arm runs **identity calibration,
  stamped `calib_level='none'`** — honest, never an error, exactly the
  design's "missing cells ⇒ identity" clause.
- **(b) NOT yet satisfied.** The GARCH-era leaderboard baseline has not
  accumulated — the Phase D shadow deploy that starts that clock is still
  pending (the standing Phase D reminder).

The operator's explicit instruction ("build T3") is the decision; this record
keeps the gates' status visible rather than silently satisfied. Because the
flip ships dark (mode off), the pre-registration is live in the config while
zero runtime behavior changes; when the Phase D shadow deploy lands, the
first vol_fan claims register under `arm='tsfm'` (identity-calibrated until
the mint). An operator preferring the design's literal evidence ordering can
flip back with one line — the ledger stamps make either era auditable.

## 3. Verification (offline, stubbed httpx — the T1/T2 pattern; 31/31)

Throwaway suite `/home/z/verify/t3_verify.py` (fake `httpx` in `sys.modules`
before any claims import; loader patched **only** for the registration-mode
overrides — the arm value is never injected, it must flow from the real
flipped yaml; that is the entire point of the suite):

- **K (8)** — the flip surfaces: yaml carries arm `tsfm` + mode `off`;
  `feature_flags.get_claims_vol_fan_arm()` and the claims layer's own
  `claims_vol_fan_arm()` both read `tsfm` from the real yaml (mtime
  hot-reload, no restart); `_DEFAULTS` stays `egarch`; `_ARM_SOURCE` maps
  tsfm → `tsfm_sidecar` AND the GARCH family → `vol_backtest_recalib`
  (both sides of the reversible switch); real-yaml `claims_mode='shadow'`
  (the AND-gate's other leg is ON, so darkness is vol_fan_mode's alone).
- **D (2)** — darkness with the flip live in the yaml: mode off ⇒
  `register_vol_fan_claim` returns None with **zero httpx calls of any
  kind** (no health probe — the arm is never even constructed; no
  `/forecast`; no ledger POST). The flip changes nothing live.
- **R (13)** — the flipped registration (mode patched to shadow, arm from
  the REAL yaml): row returned; exactly ONE `/forecast` POST (512 log
  closes, horizon 48) after exactly ONE lazy health probe; exactly ONE
  ledger POST; row stamped `kind='vol_fan'`, `source='tsfm_sidecar'`,
  `arm='tsfm'`, `space='price'`, `status='open'`, `horizon_h=48`,
  deadline = ts + 48h; fan = 48 monotone rows, q50 = entry;
  `claimed_probs` carries the qualification facts + barrier null; no clamp
  stamp for the clean √h term structure; terminal q95/q05 =
  exp(2·z95·σ_raw(48)) with identity m.
- **C (5)** — calibration honesty: the real artifact carries
  garch/gjr/egarch cells and NO tsfm cells (the honest pre-mint state);
  flipped arm + that artifact ⇒ identity m=1, `calib_level='none'`; the
  GARCH-family cells in the same artifact remain readable (arm-keyed
  lookup undisturbed); **composability** — a minted tsfm cell reached
  through the flag-driven `claims_vol_fan_calib_path` ⇒ `calib_level='cell'`,
  terminal width = exp(2·z95·σ_raw·m) with m = k·exp(a)·F^(b−1). The flip
  composes with the future operator mint with zero further changes.
- **A (3)** — determinism: two registrations byte-equal (minus `created_at`);
  the fan byte-equal at 6 dp; the row's `arm` is the flag's value verbatim
  (the auditable switch).

The 2 initial failures were TEST state-isolation bugs (the default-path
`_CALIB_CACHE` is mtime-throttled for 30 s — the R-series load shadowed the
C-series path override; T2's cached-probe lesson re-learned, fixed by
resetting the module cache between series). **Zero implementation bugs.**

## 4. Regressions

Claims-adjacent set (`eod_resolver`, `settlement_contract`,
`eod_lookback_wiring`, `eod_resolver_basis`, `recalib`,
`eod_timesfm_config_migration`, `vol_backtest`): **132 passed / 11 failed**
with a stash round-trip proving the 11-failure set **byte-identical at HEAD**
(pre-existing, `arch` absent in this sandbox — same set as T1 and T2).
`heavy_sweep_shadow_mode_composition` 8/8. The yaml parses; both flag
readers verified against it.

## 5. Operator actions

- **None required for this flip itself.** Ships dark; no migration; no
  restart (mtime hot-reload).
- The standing Phase D reminder is now THE gating item: next deploy
  installs `arch` → flip `claims_vol_fan_mode: "shadow"` — the first
  vol_fan claims then register under `arm='tsfm'`, identity-calibrated.
- The operator mint (T2 build log §5): with :8002 up,
  `run_vol_backtest.py --arms garch,gjr,egarch,tsfm --horizons 6,12,24,48`
  on the 5 lab symbols → `python -m vol_backtest.recalib` → point
  `claims_vol_fan_calib_path` at the minted artifact (or copy to
  `config/claims_vol_fan_recalib.json`) — mtime hot-reload picks up the
  tsfm cells within 30 s, no restart.
- The leaderboard (`GET /claims/leaderboard`) gains the tsfm-era rows as
  they resolve; the GARCH-era baseline the design wanted first does not
  exist — recorded here and in the yaml comment, not silently assumed.

## 6. Honest limitations

- The flip precedes both design gates (artifact cells, GARCH-era baseline)
  — an operator call, recorded above. The protection is structural: mode
  off means zero live change; row stamps mean the tsfm era is auditable as
  an era; the flip back is one line.
- Until the mint, every registered tsfm fan carries `calib_level='none'`
  (identity m) — the leaderboard would be grading an uncalibrated arm.
  The honest sequencing therefore remains: mint BEFORE the Phase D shadow
  flip if the leaderboard comparison is to mean anything from the first row.
- Sandbox verification uses the fake-service harness (the house pattern);
  the determinism bar for the real service was T1's acceptance and the
  EGARCH seed=7 precedent — unchanged by a yaml line.
