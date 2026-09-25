# Kairos: fixed-horizon path risk + regime survival (2026-09-25)

**Branch:** `kairos`. **Migration:** `170_kairos_path_excursions.sql`.
**Why now:** the structural-audit review (2026-09-25) kept two items from its §4 and rejected the rest:
- **4.1 "ghost edge"**: kept as separate fixed-horizon MAE/MFE columns. The audit's multiplier formula was dropped, because it scores full losses as roughly zero.
- **4.4 non-parametric survival**: kept as a Kaplan-Meier curve next to the existing life-table hazard.

Both are **measurements only**. No gate, knob or TimesFM input reads them.

## 0. The target these measurements serve (user, 2026-09-25)

> System generates S1 at T0, then S2 at T0+61h. S2 needs enough context to know the regime
> transition state and age, and to be tuned to the fact that it will have a less-than-48h
> clock. Its entry, SL, TP and risk should reflect this. We can't do this yet, but TimesFM 3.0
> dynamic covariates can affect it.

What a later signal S2 needs to know, and what this build provides toward it:

| S2 needs to know | Measured by | Built here? |
|---|---|---|
| How old the regime is at S2 | `regime_age_h` (run tracker; restart-safe since 2026-09-25) | already live |
| How long the regime is likely to **remain**, given that age | KM conditional survival `S(a+Δ)/S(a)` — the "remaining regime clock" | ✅ |
| How far price typically goes **against** a signal inside that remaining window | MAE_h at h = 4/12/24/48, in ATR units | ✅ |
| Whether the edge survives a realistic stop | `m_h | stop k` = endpoint drift, stopped at −k ATR | ✅ |
| Entry, SL, TP and risk set from the above | Layer 7 knobs; geometry track | ❌ later (shadow; see §5) |
| The regime state as a forecast input | TimesFM 3.0 `forecast_with_covariates()` | ❌ later (§5) |

The shortened clock is the conditional survival read at S2's age. If runs are memoryless (P1: a
flat 0.12/h hazard), the remaining clock is the same at any age, and "S2 is late in the regime"
carries no information. If KM shows ageing (survival falling faster at later ages), S2's clock
really is shorter, and its horizon should be read from the curve, not assumed to be 48h. This
build measures which case holds before anything is tuned on it.

## 1. Path risk (fixed-horizon MAE/MFE)

- **Columns.** `nt_signal_sim.mae_{4,12,24,48}h_pct` and `mfe_…`: % of entry, ≥ 0, over bars
  1..h after the entry bar, from bar highs and lows.
  - The entry bar is excluded because its range includes prices from before the signal.
  - Bar h is the same close `drift_h` uses, so m_h ∈ [−MAE_h, +MFE_h]. This is enforced by a
    test and checked live in report §E2.
- **Not the existing `mae`/`mfe` columns.** Those stop walking at the TP/SL bracket exit, so they
  depend on the geometry (scope §9). The new columns walk every bar whatever the outcome.
- **Writer.** `kairos.path_excursions()` is a pure function, called from
  `backfill_drift_horizons` (the hourly `kairos-daily` job). The job now fetches OHLC
  (`fetch_tradingview_batch(..., ohlcv_out=)`) instead of closes only.
  - It selects rows where `drift_48h` **or** `mae_48h` is NULL.
  - It writes only NULL fields.
  - If migration 170 isn't applied (PostgREST 400), it falls back to drift-only.
- **Missing data.** A bar with no high or low sets that horizon to NULL rather than an
  understated excursion, which would read as safer than it was.
- **View.** `v_kairos_signal_episodes` gains `mae4..mae48` and `mfe4..mfe48` in ATR units,
  appended after the migration-168 columns (a test asserts the old column order is unchanged).
- **Report §E2** (weekly check, `--accel` too):
  - Per matured horizon: median MAE/MFE, P(MAE ≥ 1/2/3 ATR), mean m_h, and m_h with a 2-ATR stop
    (symbol|day cluster-bootstrap CI).
  - The **ghost share**: of episodes with m_h > 0, the fraction whose MAE reached 2 ATR first.
  - By tier at h = 24.
  - The invariant check, which is flagged if violated.
  - Coverage of `mae24` on matured episodes is a data-health row in §A (≥ 95%).
- **Limits.**
  - Uses 1h bars: intrabar order is unknown, and gaps are ignored (a stop that gaps through k
    reads as −k, which is optimistic).
  - The window starts at the first **full** bar after entry. Adverse moves in the remainder of
    the entry bar are missed, so MAE is understated (also optimistic).
  - k ∈ {1, 2, 3} is a reporting grid in the sweep's close-to-close ATR, not a proposed SL. That
    ATR is small: the first live look had a median MAE₂₄ of about 8 of these units, so k = 2 is a
    tight stop.
  - **Read medians.** Means are dominated by a few very large moves (BTCUSD-scale). A stop caps
    exactly those, so "stopped mean > endpoint mean" is tail-capping, not edge.
  - ATR here is the sweep ATR (SMA14 of close-to-close), the same scale as m_h, so it is not
    comparable to P1's OHLC ATR.
- **Raw-table invariant.** 225 of 6,056 rows break drift_24h ∈ [−MAE, +MFE]. All are resolved
  rows from 2026-09-17/18, before the 09-22 view start, with drift written by the resolver's
  older anchoring. They are outside `v_kairos_signal_episodes`, which shows 0 violations. Not
  rewritten: the resolver's drift was left untouched on purpose.

## 2. Regime survival (Kaplan-Meier)

- `kaplan_meier()` is the product-limit estimator with a Greenwood log-log 95% CI.
  - Runs still open, or ended by a data gap, count as right-censored.
  - Runs that start after a gap are excluded (left-censored), same as the life table.
- **Report §E** now adds, under the life-table hazard:
  - KM S(x) at 2/6/12/24/48h with CI.
  - The memoryless geometric reference S(x) = e^(−λx), where λ = exits ÷ hours at risk.
  - The KM median.
  - The **remaining regime clock** table: P(alive at a+Δ | alive at a) for signal ages
    a ∈ {0, 2, 6, 12, 24} and Δ ∈ {4, 12, 24, 48}.
- **Reading it.** If KM tracks the geometric curve and the clock rows match across ages, runs are
  memoryless: age adds nothing, as in P1 V4.
  - A falling clock with age means ageing.
  - A rising clock means heavy tails: survivors persist, which is the audit's §1D worry.
- **Status.** Descriptive. The confirmatory V4 test stays as pre-registered (§12.3).
- **Why not Cox PH.** It needs a new dependency (`lifelines` is not installed), and its
  covariates are the V7 factor families, which have their own pre-registered order. It stays
  out until V7.

## 3. What this does not change

- No live behaviour: no gate, threshold, sizing, delivery or TimesFM path reads the new columns
  or tables.
- The G2 test family, nulls, Holm correction and the p < 0.001 interim rule are unchanged. §E2
  and the KM lines are descriptive, like §D/§E/§F.
- The existing `mae`/`mfe` columns and the resolver are untouched.

## 4. Verification

- `tests/test_kairos_path_survival.py` has 17 tests, covering:
  - Excursions: buy/sell mirror; entry bar excluded; non-negative; drift inside the band
    (200 random paths × 2 directions × 4 horizons); NULL on short or holed input.
  - Migration and view: migration columns match the code; the view keeps 168's column order.
  - KM: equals the empirical survival function without censoring; matches the textbook
    censored example; CI brackets the estimate; memoryless data gives KM ≈ geometric with an
    age-independent clock; the conditional-survival arithmetic for the S2 case.
  - Stop truncation, including the audit's +0.8/−3.5 case → −2.
  - Wiring of the backfill and the report.
- Live: migration applied 2026-09-25, backfill run, `--accel` look with §E2 and KM (see report
  `reports/20260925_A*.md`).

## 5. Parked: the S2 context and TimesFM covariates

Not built. Recorded so it isn't lost.

- **Order of evidence.**
  1. KM shows whether the remaining clock depends on age.
  2. §E2 shows whether path risk depends on age band and tier. Add an age-band split of MAE₂₄
     once enough uncensored episodes aged ≥ 12h exist (§H counter).
  3. Only if (1) or (2) is non-null: shadow-derive S2's effective horizon
     (h* = the Δ where the conditional survival crosses 0.5) and a shadow risk multiplier.
     These are Layer 7 knobs, shadow only.
- **TimesFM 3.0.** `forecast_with_covariates()` could take the regime age, the conditional
  survival S(a+Δ)/S(a), and the rolling path risk as **dynamic covariates**, so the price prior
  itself reflects a shortened regime clock.
  - **Blocker:** TimesFM-3 weights are non-commercial, so this is benchmark only; production stays
    on 2.5.
  - **Precondition:** the covariates must be stable. The raw quadrant Y must not be used, per the
    audit's §3 point, which is valid here.
  - **Test design when it comes:** an offline benchmark, TimesFM-3 with vs without covariates, on
    held-out signals, scored on geometry-free m_h and MAE_h. It needs its own pre-registration.
- **Trigger to revisit:** the `kairos_g2` roadmap row, after the first KM read with ≥ 200 completed
  uncensored runs, or when §H reports G2.4/G2.5 as decidable.
