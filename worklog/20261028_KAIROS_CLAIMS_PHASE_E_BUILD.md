# kairos-claims Phase E build — the "arm 11" leaderboard + the claims→calibration bridge

**Date:** 2026-10-28
**Type:** Build (Phase E of `20261027_KAIROS_CLAIMS_LAYER_DESIGN.md` §4 row E + §6).
**Precondition:** operator confirmed migrations 218 (Phase A) + 219 (B/C) + 220 (D) applied.
**Contract:** 5-clause consistency contract intact — this phase only READS the shadow ledger; zero geometry/EV/gate/MT5/resolver changes; every live-path addition is flag-gated with the default = byte-identical no-op.

---

## 1. What landed (6 files)

| File | Kind | Contents |
|---|---|---|
| `regime_platform/sweep_orchestrator/claims_leaderboard.py` | NEW | The Phase E core: stdlib-only aggregation (`aggregate_claim_cells` — pooled coverage + Wilson bands), the paired bracket-vs-vol_fan Diebold–Mariano test (`paired_pinball_dm`), the mtime-cached lab-arm-table loader (`load_lab_reference`), `build_leaderboard` assembly, and `fetch_symbol_claims_evidence` (the TTL-cached per-symbol evidence gate). |
| `config/claims_lab_arm_reference.json` | NEW | The committed lab arm table — the design doc §0 measured verdicts verbatim (6 symbol×horizon vol cells incl. XAUUSD's UNPROVEN downgrade, price/direction lane FAILs, the EGARCH best-in-family 0.944 UNPROVEN row, the power-control harness meta-row), with provenance + the join-semantics caveat baked into the file. |
| `regime_platform/routers/claims.py` | NEW | `GET /claims/leaderboard` (read-only, `require_api_key`, `RATE_LIMIT_DEFAULT`, 503/502/504 mapping) returning the leaderboard document; no flags — the ledger is shadow-first evidence. |
| `regime_platform/routers/__init__.py` + `main.py` | EDIT | Router registration (next to `calibration.router`; import at the Phase-10 line). |
| `regime_platform/core/feature_flags.py` + `config/feature_flags.yaml` | EDIT | The `claims_prior_mode` triple: `_DEFAULTS["claims_prior_mode"]="off"` + `get_claims_prior_mode()` + the yaml entry with flip discipline. |
| `regime_platform/sweep_orchestrator/calibration_enforcement.py` | EDIT | The bridge: `bayesian_adjusted_p_win(…, prior_mult=1.0)` multiplies the PRIOR COUNT (never the 0.5 anchor); `_rank_conditioned_shrink` passes it through (orthogonal to, and multiplicative with, the rank modulation); the `_claims_prior()` gate helper (lazy imports, fail-open); the hook in `apply_calibration_adjustment` after `resolve_bias_evidence`; an additive `"claims_prior"` stamp on all 9 post-evidence return dicts; one new log line that fires only when the mult ≠ 1.0. |
| `requirements.txt` | EDIT | `arch>=7.0.0` ACTIVATED (was a comment since Phase 0): the Phase D vol_fan arm needs it in production, and the arm-11 leaderboard is meaningless without vol_fan claims accruing. Pulls `statsmodels` transitively. |

No migration. Phase E reads `nt_signal_claims` as migrations 218+220 left it.

## 2. The leaderboard, designed honestly

**Primary statistic — the PAIRED DM, not a number-vs-number cross-comparison.** The design doc §6 asks "does the thesis layer's registered distribution beat the free TSFM vol fan on the same cells?" Inside the ledger there is a strictly better version of that question: every qualified signal may carry TWO sibling claims (the `bracket` ATR control and the `vol_fan` recalibrated GARCH arm), both scored by the ONE settlement resolver over the SAME bars. `paired_pinball_dm` pairs them per `signal_id` (d = pinball_vol_fan − pinball_bracket, pinball is a loss so d<0 favors the vol fan) and runs the lab-shaped DM test (z = mean(d)/se, two-sided NormalDist p; per-signal differences are near-iid by 15-min sweep construction, so no NW lag term at this n). Verdicts: `insufficient_pairs` / `unproven` (n<8) / `vol_fan_better` / `bracket_better` / `equivalent` / `degenerate` (zero variance).

**Cells.** `aggregate_claim_cells` groups resolved rows by (symbol, kind): pooled coverage50/80/90 (Σ cov·n/Σ n — bar-weighted, not a mean of proportions; the plain mean is also reported), the Wilson 95% band on the pooled proportion (documented caveat: within-claim bar correlation makes the true interval wider, so the band is an optimistic lower bound — verdict gates therefore ALSO require the plain n_claims floor), mean CRPS/pinball/brier_resolve, the arm/calib_level breakdown for vol_fan rows, and the lab-style verdict:

- `unproven` — n_claims < 8 (the ledger is young; say so),
- `calibrated` — pooled coverage80 in the §6 acceptance band [0.80, 0.95] AND Wilson-lo ≥ 0.55,
- `miscalibrated` — coverage80 < 0.55 or > 0.98 (actionable in either direction),
- `off_nominal` — everything between.

**Lab join = context, never a cross-horizon comparison.** Claims resolve at 48h only (the settlement contract); the lab's significant cells are 6–72h ratios. `cells_for_symbols_on_the_board` joins by SYMBOL with the caveat in both the JSON and the response notes — provenance, not a scoreboard row. The committed table carries only cells documented in the design doc (the full 15-cell vol table lives in the lab); extend cell-by-cell as the lab re-measures — additive evidence, never edited in place.

**The read is not flag-gated.** The endpoint computes nothing the ledger doesn't already hold (design doc §1: claims are evidence, not live state); writes stay gated by `claims_mode`/`claims_vol_fan_mode` as in A/D.

## 3. The `claims_prior_mode` bridge (off by default, gentle by construction)

Design doc §1: "if claim coverage proves reliable on era-clean replay, it may *inform* the shrink prior via a feature flag — opt-in, evidence-gated, same discipline as `pbm_*_mode`." Implementation, when `claims_prior_mode: live`:

1. `_claims_prior(symbol)` (calibration_enforcement) → `fetch_symbol_claims_evidence(symbol)`: resolved rows for the symbol; **vol_fan preferred** once it clears the n≥10 floor (the §6 acceptance bar is written against the recalibrated arm), the `bracket` control otherwise (the all-bracket era); TTL-cached 300s (the hook runs per signal; evidence moves at resolver cadence); ANY error ⇒ None.
2. The gate maps the cell's verdict to a multiplier on `BAYES_PRIOR_COUNT`: **calibrated ⇒ ×0.75** (proven-honest distribution ⇒ the raw p_win has earned a little more weight), **miscalibrated ⇒ ×1.5**, anything else ⇒ ×1.0. Bounds are deliberately narrow because claims measure the FAN's coverage — an adjacent, not identical, object to directional p_win. The asymmetry is intentional: the trust bonus is the dangerous direction, so it requires the Wilson floor; the penalty (more shrink = more conservative) needs only a clear point estimate.
3. The multiplier scales the prior COUNT in the ONE existing shrink funnel (`bayesian_adjusted_p_win` via all four `_rank_conditioned_shrink` call sites). It can never touch the 0.5 anchor, the observation count, or the raw p_win; `n_records=0` still returns raw p_win unshrunk (this module's documented divergence).
4. Observability: every post-evidence return dict gains `"claims_prior"` (the evidence dict, or None — additive key, all consumers are `.get`-style; the existing suite's key-wise assertions pass unchanged), and one log line fires only when the mult ≠ 1.0.

`off` (shipped) = zero I/O, zero arithmetic change: `_claims_prior` returns None before touching the ledger and `prior_mult` stays 1.0.

## 4. Verification record

- **py_compile** ×6 touched/new files; yaml.safe_load + json.load OK.
- **Ruff:** new files 0 errors (after fixing two REAL catches: a bare `sqrt` left in `wilson_interval` and a missing `datetime` import — both would have been runtime NameErrors on first leaderboard call). Edited files: 67 vs 66 at HEAD (stash round-trip) — the +1 is `F401 .claims` in `routers/__init__.py`, the same accepted pattern as the 25 pre-existing F401s in that aggregator (every entry has one; main.py imports the module explicitly). Zero new error classes anywhere else.
- **Inline suite 72/72** (throwaway, per the A/B/D no-committed-tests discipline): Wilson known-value + n=0 + clamp + tightening; all four verdict bands + the Wilson-floor edge + NaN; pooled-vs-plain coverage with unequal bar counts (exact fractions); arms/calib_level breakdowns; malformed/unknown-kind skipping; paired-DM direction (both mirror cases, z>3), degenerate (zero-variance), single-pair, small-n, no-pairs; lab reference loads (10 cells, symbol + lane-row joins); leaderboard shape; evidence gate offline with stubbed httpx (bracket era → calibrated ×0.75; vol_fan preferred at n≥12; thin → unproven ×1.0; miscalibrated ×1.5; REST failure → None; TTL cache hit; empty ledger → honest unproven); enforcement math (default identical to 1e-9; ×0.75 exact (13+3.75)/27.5; ×1.5 exact; n=0 raw; rank pass-through both paths); flag-off ⇒ zero fetch attempts; flag-live ⇒ evidence dict; `apply_calibration_adjustment` system path returns the additive stamp with p_win untouched; live + fetch failure ⇒ fail-open standard shrink.
- **Existing suites:** full `pytest tests/` (minus the 3 files that cannot even collect in this sandbox — `test_e2e_registry_wiring.py` the documented pre-existing failure, plus `test_calibration_router_unification.py` / `test_renko_backtest.py` both hitting the sandbox's missing `slowapi` optional dep): **2270 passed / 24 failed**, and a stash round-trip proves the 24-failure set is **byte-identical at HEAD** — all pre-existing environment-dependent, zero new. `test_calibration_enforcement.py` (the 1e-9 shrink assertions) passes.

## 5. Operator actions

1. **Nothing is required for Phase E itself** — no migration; `claims_prior_mode` ships "off"; the leaderboard endpoint is live as soon as this deploys.
2. `arch` is now ACTIVE in requirements.txt — the next Render deploy will install it (pulls statsmodels). This completes Phase D's deployment precondition; after deploy, flip `claims_vol_fan_mode: "shadow"` when you want the vol_fan arm registering (per the Phase D build log).
3. Optional flips, in evidence order: let the ledger accrue → inspect `GET /claims/leaderboard` → if vol_fan cells land `calibrated`, `claims_prior_mode: "live"` is then an evidence-backed, gentle (×0.75/×1.5) modulation of the existing shrink — flip on evidence, not calendar, per the yaml discipline.

## 6. Honest limitations

- **The leaderboard is empty until claims resolve.** First scored claims appear ~48h after the first post-218 qualified signal; the paired-DM table needs both siblings resolved per signal, so until `claims_vol_fan_mode` flips to shadow it has nothing to pair (the endpoint says so: `insufficient_pairs`). The bracket cells accrue now.
- **The lab join is provenance, not a comparison** — claims are 48h-only; the lab's PASS cells are 6–72h. The §6 question is answered inside the ledger by the paired DM, whose baseline is the house ATR control (not Log-HAR). "Beating the lab's BTCUSD 6h 0.727" in the strict cross-harness sense remains unanswerable until a shared-cell replay harness exists.
- **The Wilson band on pooled bars is optimistic** (within-claim correlation); the gates compensate with the n_claims floor, and the §6 acceptance bar is judged on the band + floor together.
- **`thesis`-kind claims aggregate automatically** when a registrant ever lands (the 218 CHECK allows it; nothing writes them today) — the leaderboard and the evidence gate already handle the third kind generically.
- Ruff +1 F401 in `routers/__init__.py` (pattern-consistent with that file's 25 pre-existing re-export F401s) — the alternative (aliasing one entry) would diverge from the file's established style.

## 7. Phase status after E

| Phase | Status |
|---|---|
| A (ledger + registration + resolver scoring) | LIVE (218 applied) |
| B (matview + broadcast transport) | LIVE (219 applied; `claims_publish` off) |
| C (frontend SignalContextCard) | LIVE (nobletradingapp dc29cf7) |
| D (recalibrated GARCH-family vol_fan arm) | LIVE (220 applied; `claims_vol_fan_mode` off; `arch` NOW IN requirements) |
| E (arm-11 leaderboard + claims_prior bridge) | LIVE on deploy (read-only endpoint; `claims_prior_mode` off) |

The kairos-claims layer is complete per the design doc: register (A) → transport (B) → render (C) → the recalibrated arm (D) → the measured verdict surface + the evidence-gated calibration bridge (E). What remains is what only time can provide: resolved claims.
