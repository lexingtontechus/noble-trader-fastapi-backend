# TimesFM rebuild T2 — the recalib mint (build log)

**Date:** 2026-10-28
**Type:** Build (T2 of the rebuild plan; design = `worklog/20261028_KAIROS_TIMESFM_REBUILD_DESIGN.md` §5 T2; hooks = the T1 build log §7).
**Scope:** the research harness can now run the claims layer's vol_fan arm as a catalog arm — `run_vol_backtest.py --arms garch,gjr,egarch,tsfm` — whose `rv` is **the fan's own σ_raw** (the identical-quantity invariant), so the offline recalib mint (`python -m vol_backtest.recalib`) produces cells keyed `(symbol, horizon, 'tsfm')` that the claims layer consumes unchanged. Zero migration (nothing DB-adjacent), zero claims-side changes, zero recalib changes — T2 is exactly two code surfaces: one new file + one catalog entry.

---

## 1. What landed (3 files)

| File | Change |
|---|---|
| `vol_backtest/tsfm_fan_arm.py` | NEW — `TimesFMVolFanResearchArm` (`name='tsfm'`): the `forecast()`-shaped adapter the T1 build log demanded. Wraps ONE `TimesFMVolFanArm` (lazy import inside `__init__` — vol_backtest stays importable without the regime_platform tree, and no import cycle exists: the claims arm is stdlib+httpx only, and `claims_vol_fan`'s vol_backtest import is equally lazy). Contains **no math of its own** beyond the H-bar collapse: `rv = sqrt(Σ var_path[:H])` — the fan builder's terminal `σ_raw` verbatim (clamp behavior included) — and the family interval convention `lo80/hi80 = rv ∓/± Z80·rv/√(2H)` (same as EWMA/GARCH/`timesfm25`; the recalib standardizes by whatever the arm claims, so k repairs an understated interval with the same machinery). |
| `scripts/run_vol_backtest.py` | `build_arms` catalog gains `'tsfm': TimesFMVolFanResearchArm` + the mint command in the docstring examples. Default `--arms` string UNCHANGED (opt-in only — additive). `--no-models` filter excludes 'tsfm' automatically (not in the baseline subset). |
| `vol_backtest/__init__.py` | Docstring Layout section gains the `tsfm_fan_arm.py` line. No export changes (model arms are never package-exported — the `model_arms` convention). |

**Zero changes** to: `vol_backtest/recalib.py` (verified: `fit_calibration_map` groups by `df.groupby(["symbol","horizon","arm"])` — whatever arm names appear in records, no whitelist; `to_row()` emits exactly the `horizon_bars/mz_a/mz_b/spread_k/level` fields the claims `_cell_from_row` reads), `claims_vol_fan.py`/`claims_tsfm_arm.py` (T1 untouched), the resolver, migrations, `server.py`, both sweep call sites.

## 2. The arm-key contract (the one trap that matters)

`name = "tsfm"` is load-bearing, not cosmetic. The chain: this class's `name` → records.csv `arm` column → recalib cell key `(symbol, horizon, arm)` → claims lookup `vol_fan_multiplier(cells, symbol, arm_name, h, σ_raw)` keyed off the `claims_vol_fan_arm` flag value (whitelist `garch|gjr|egarch|tsfm`). Registering under `timesfm25` — or "refactoring" the adapter onto the existing `TimesFM25HTTP` — would mint cells the claims layer can never find: identity calibration forever, silently. The executable proof is check J7 of the verification suite (below).

`timesfm25` itself stays untouched in the catalog: it is a DIFFERENT arm answering a different question (log-RETURN input, per-STEP q10/q90 spreads → per-step RV aggregate; the Brini-style research comparison). `tsfm` feeds log-PRICE levels and reads the h-step TERM STRUCTURE (the claims derivation). Both are legitimate; neither is a cleanup of the other.

## 3. The identical-quantity invariant (why this file exists)

The recalib's MZ center is fitted on `(log F, log RV)` pairs where F is the arm's `rv`. If the research arm's `rv` were even slightly differently derived (e.g. a re-read of the raw q10/q90 spread instead of the clamped telescoped path), the minted `m = k·exp(a)·F^(b−1)` would multiply a related-but-different quantity live. The adapter therefore derives `rv` FROM `variance_path` — the same array the fan builder cumsums — so:

```
harness rv(H)  ==  sqrt(Σ var_path[:H])  ==  fan σ_raw(48)   (mod the 8dp meta stamp)
```

including the telescoping clamp behavior (vol contractions floored at 1e-12 and counted; the count rides along in `ArmForecast.extra["tsfm_clamped_increments"]` — the harness ignores `extra`, so it is debug-only on this path). Verified H16/J10: `meta['sigma_h48'] == adapter rv(48)` to the meta's 8-dp stamp.

One honest nuance recorded during verification: a **flat** σ_total across horizons yields zero (not negative) telescoping increments — the positivity guard rejects the whole path (T1's documented behavior; a degenerate forecast is no forecast). The clamp path is for **decreasing** σ_total (vol contraction). Both tested.

## 4. Verification (offline, stubbed httpx — the T1 pattern; 40/40)

Throwaway suite `/home/z/verify/t2_verify.py` (fake `httpx.Client` injected into `sys.modules` before any arm constructs; deterministic quantile layouts; per-instance state isolation — the T1 lesson about the cached health probe applied):

- **G (6)** — catalog: `build_arms(['tsfm'])` → one arm, `.name == 'tsfm'` (THE KEY); 'tsfm'+'timesfm25' coexist as distinct entries; adapter is not TimesFM25HTTP; unknown arm still `SystemExit`; `--no-models` excludes 'tsfm'; `default_arms()` unchanged (additive-only, opt-in).
- **H (18)** — adapter math: `rv == σ_total(H) = C·√H` exactly for H∈{6,12,24,48} (telescoping identity, no clamps); interval == `rv ∓/± Z80·rv/√(2H)`; exactly ONE POST per forecast; POST body == last **512 log closes** + horizon; determinism (3 calls byte-equal); contraction (decreasing σ_total) → 47 clamps counted + stamped, rv ≈ σ_total(1) and rv > σ_total(48) (clamp only raises); crossing → whole-path None; zero-spread → None; malformed body → None; POST 503 → None; health 503 → None AND zero POSTs (probe short-circuit, fresh instance); 9-level == 10-level layout rv; short window → None; **H16 the invariant**: `meta['sigma_h48'] == adapter rv(48)`; fan stamps `arm='tsfm'`, `calib_level='none'`, `m_h48=1.0`; 48 monotone rows, q50 == entry.
- **I (5)** — harness: records carry `arm='tsfm'`; every record's forecast == `C·√H` (the derivation per cutoff, on each cutoff's own window); `aggregate_metrics` rows keyed (symbol, horizon, tsfm); two full runs byte-equal; service down → honest NaNs + `n_success == 0`, no exception.
- **J (11)** — the mint round trip: synthetic records (100 cutoffs × H∈{6,12,24,48}, seeded noise, MZ-generating `a=0.05, b=1.1`) → `fit_calibration_map` mints cells `(BTCUSD, H, 'tsfm')` all level `'cell'` → `write_artifacts` → claims `load_calib_cells(artifact)` finds them → **acceptance #6**: `m = k·exp(a)·F^(b−1)` reproduced from the artifact's own values to 1e-9 (far beyond 3 dp); h=18 interpolation == geometric mean of m12/m24; **J7 THE TRAP**: identical records relabeled `arm='timesfm25'` mint cells invisible to `arm='tsfm'` (`m=1.0, level='none'`); the CLI minter (`python -m vol_backtest.recalib --run-dir …`) mints offline rc=0; **J9–J11 the money shot**: the claims fan built with the minted artifact — `meta['m_h48'] == k·exp(a)·F^(b−1)` at `F = σ_raw(48)`, `calib_level='cell'`, σ_raw unchanged (m applied separately), terminal row `q95/q05 == exp(2·z95·σ_raw·m)`.

All 7 initial failures were TEST bugs (fixture/ordering/state — the flat-vs-decreasing contraction fixture, `sorted()` ordering of 'timesfm25' before 'tsfm', reused arm instances carrying cached healthy probes past a 503 mode switch, and the fan meta's 8-dp `sigma_h48` stamp vs a 1e-12 tolerance). **Zero implementation bugs found.**

Regression sweep (same set as T1): claims-adjacent suites (`eod_resolver`, `settlement_contract`, `eod_lookback_wiring`, `eod_resolver_basis`, `recalib`, `eod_timesfm_config_migration`, `vol_backtest`) — 132 passed / 11 failed, and a stash round-trip proves the 11-failure set is **byte-identical at HEAD** (pre-existing, `arch` absent in this sandbox). `heavy_sweep_shadow_mode_composition` 8/8. ruff clean on the new file; py_compile clean on all three touched files.

## 5. Operator actions (the actual mint — design §5 T2/§8)

With the repo's TimesFM server up (:8002) and the data dir populated:

```
python scripts/run_vol_backtest.py --data-dir data/vol_backtest \
    --symbols BTCUSD,XAUUSD,XAGUSD,USDJPY,USDCHF \
    --arms garch,gjr,egarch,tsfm --horizons 6,12,24,48
python -m vol_backtest.recalib --run-dir <run_dir>
# → recalib.json cells keyed (symbol, horizon, 'tsfm') — point
#   claims_vol_fan_calib_path at it (or copy to
#   config/claims_vol_fan_recalib.json): mtime hot-reload, no restart.
```

The GARCH-family cells re-mint in the same run (`--arms garch,gjr,egarch` included). The T3 flip (`claims_vol_fan_arm: "tsfm"`) remains gated on: T2's artifact carries tsfm cells AND the GARCH-era baseline shows on `GET /claims/leaderboard` — evidence, not calendar.

## 6. Honest limitations

- The adapter wraps the claims arm via a lazy cross-package import — deliberate single-source-of-truth (the derivation lives once, in `claims_tsfm_arm.py`); the cost is that `--arms tsfm` requires the repo root importable as a whole (true for every invocation of `scripts/run_vol_backtest.py`, which itself sits at the root and inserts it into `sys.path`).
- The harness's `BacktestRecord` drops `ArmForecast.extra` — the clamp count does not reach records.csv (it reaches the claims layer's `claimed_probs` via the T1 path; on the research path it is debug-only).
- The sidecar is zero-shot (NOT the covariate-conditioned lab arm) — the minted cells calibrate the zero-shot arm as it actually runs live. That is the honest object: whatever the leaderboard grades is what the fan asserts.
