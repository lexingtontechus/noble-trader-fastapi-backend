# KAIROS — per-symbol edge-state matrix: research plan and design scope

**2026-09-23 · scope / design only.** No code, config, schema, or live behavior was changed.
Evidence base: `docs/scopes/20260923_REGIME_CELL_EDGE_PERSISTENCE.md` (v3), reproducible with
`scripts/regime_cell_edge_persistence.py`.

## Name

**Kairos** (Καιρός): in Greek, the *opportune moment*, as opposed to Chronos, sequential time. He is
shown with a single lock of hair at the front and a bald back of the head: you can seize the
moment as it arrives, but not once it has passed. That is the product thesis: edges are real but
fleeting, and the job is to know which symbol is in its moment now and how long that is likely
to last.

- **Why this name:** no collision anywhere in the workspace (checked every repo).
- **Alternatives considered:** Proteus (shape-shifting — describes the change, not the timing) and Tyche (fortune — implies luck, the opposite of the claim).
- **Naming convention:** the flag and all artifacts use the prefix `kairos_`.

## Plain-language explanation

For every symbol, the platform already knows which market regime it is in: volatility (4
levels) × trend direction. Kairos adds one question on top of that: **inside this regime, is our
signal making money for this symbol right now, and is that getting better or worse?** The
answer puts each symbol-regime into one of four boxes:

| | Getting better | Getting worse |
|---|---|---|
| **Making money (last 14 days)** | Leader | Fading |
| **Not making money** | Emerging | Dead |

The v3 research says three things about these boxes:
- **The boxes are informative.** Symbols in the "Dead" box went on to perform *best* (the edge reverts upward).
- **Two boxes are stable.** Leader and Dead tend to stay put for a day or more; the other two don't.
- **Nobody knows yet what to do in each box.** Every claim about "reversal" boxes or box-specific stop/target settings failed the stricter tests on 30 days of data.

So this scope is about getting more data under clean, pre-registered conditions, not about
changing live trading.

## Current state

| Item | Status |
|---|---|
| Research script | `scripts/regime_cell_edge_persistence.py`: episodes, persistence, quadrants, knob counterfactuals, transition null, covariance. Read-only. |
| Local history cache | `.cache/regime_cell_edge_persistence_sim.json`, 71,511 rows, 2026-08-23 → 09-22. `fetch()` now **merges** into the cache instead of overwriting. |
| Data retention | **Approved and changed 2026-09-23 (not yet deployed):** `signal_sim_retention_resolved_days: 120` in `config/feature_flags.yaml` (getter `get_signal_sim_retention_resolved_days`, floor 7). It replaces `settings.EOD_RETENTION_RESOLVED_DAYS = 30`, which has been removed. Until the deploy, resolved `nt_signal_sim` rows are still deleted after 30 days. |
| `nt_calibration_records` | 37,041 rows since 2026-08-19, but no regime, SL/TP geometry, or MFE/MAE. It can't substitute. |
| Regime labels | `regime_label_debounce_mode: live` since **2026-09-22** (3-tick debounce). Every label in the 30-day discovery set is **raw**; every label from now on is **debounced**. This is a structural break (see Design D4). |
| Quadrant labels | Not stored anywhere. The research reconstructs them after the fact. |
| Covariance | `core/correlation_regime.py` (DCC-style 4-state correlation HMM) exists, but only behind the router/Masaniello bridge. The sweep path does not use it. |

## Data-grounded findings (v3, 30 days, cluster bootstrap over symbol-days)

| Question | Answer | Evidence |
|---|---|---|
| Does a quadrant define an edge? | **Yes for edge level, per symbol.** Dead has the best forward edge. Leader is no better than average. | Forward 7d R: Dead +0.162 [+0.056, +0.275]. Same-day Dead − all +0.076 vs shuffled null [−0.064, +0.033]. Symbol × quadrant η² 4.25% vs 1.14% chance (in-sample). |
| Probability of staying in a quadrant | **Leader and Dead are genuinely sticky.** Fading and Emerging are not distinguishable from noise. | 1-day P(stay) observed vs null: Leader 0.80 vs 0.64, Dead 0.79 vs 0.68, Fading 0.76 vs 0.70, Emerging 0.74 vs 0.74. |
| Edge "rotation" (Leader → Fading → Emerging) | **An artifact of the rolling windows.** | Observed Leader→Fading is *below* null at 1, 3 and 7 days. |
| Dead\|Leader = reversal (user hypothesis) | **Not supported.** Neither is v2's Fading/Dead-reversal mapping. | Excess reversal over the symbol's own base rate: every quadrant's CI includes 0, for price (4h, 24h, >1 ATR) and for HMM direction flip (4h, 24h). |
| A different knob per quadrant | **Not supported yet.** | Full stream: no quadrant differs from the pooled knob effect. Gate-pass: one of eight tests nominally significant (Fading tight-TP), uncorrected. |
| Tight SL | ~~Helps globally~~ **Reversed by the EURUSD 21-month path-exact test: tight SL hurts (−0.196 R), tight TP helps (+0.119 R).** Never by quadrant. | 30-day pooled +0.137 came from close-based MFE/MAE shortcuts; see the reconstruction section. |
| Regime durability over a 48h hold | **Poor.** | P(same 16-cell after 48h) 0.12; direction 0.55. |
| Correlated exposure | **14 symbols ≈ 5.3 independent price bets.** | Gold complex 0.97–0.99, US30/US500 0.88, EURUSD/USDCHF −0.82. |

**The main risk is sample size, not the idea.** The 30-day window gives a quadrant panel of only
14 days (193 symbol-day clusters). The gate-pass cohort (the product-relevant one) has 28–49
clusters per quadrant.

## Historical reconstruction test — EURUSD, 21 months (2026-09-23)

`scripts/reconstruct_signal_history.py`. It is read-only; outputs go to `.cache/recon_EURUSD_*`.

**Data.** TDVA (the same `/api/price/batch` the sweeps use) serves **1h OHLC back to
2025-01-01**. That is 10,751 bars, about 21 months, not 2 years. 4h bars go back to 2023-07 and
daily bars to 2022-11, but the light sweep runs on 1h.

**What it rebuilds.** One signal per completed 1h bar.

| Step | Source |
|---|---|
| Regime label | Production `RegimeHMM` on the trailing 200 closes |
| Direction | `_compute_trade_signal` rule |
| ATR | `compute_atr` |
| Settlement | Production `settlement_contract.settle`: 48 bars, tp_first |

Knob variants are re-settled on the actual price path. In v3 they came from MFE/MAE shortcuts.

**What it cannot rebuild.** Everything below means this is the **full-stream (ungated)
cohort**, not qualified signals.
- **Geometry:** the heavy sweep re-picks daily. The test holds geometry fixed at the current live
  EURUSD values: brick = 5.79 × ATR14, SL 3 / TP 5.
- **Gates:** Kelly, p_win, TimesFM and calibration state have no history.
- **Cadence:** the live sweep runs every 15 minutes; this runs once per hour.

**Validation against the live 30-day overlap.** Good enough to trust labels and outcomes.

| Check | Result | Chance |
|---|---|---|
| Label match, direction | 83% | 54% |
| Label match, vol tier | 69% | 28% |
| Label match, exact 16-cell | 52% | 8% |
| Settlement parity (live entry/SL/TP re-settled on TDVA closes) | **96.2%** (553/575) | — |

- Live ticks disagree *with each other* within the same hour 50% of the time, so about 83% is near the ceiling.
- Labels matched best when reconstructed on the forming bar, not the last completed bar.
- Replay pitfall: carrying the label-continuity means bar to bar across 10k bars drifts label identity to chance (5% 16-cell agreement). The reconstruction therefore cold-fits each bar.

**Results (10,504 episodes, 506-day quadrant panel).** Day-cluster bootstrap; 40-perm shuffled-R
null; Holm across the pre-registered family.

| Hypothesis | 21-month EURUSD result | vs 30-day, 14-symbol result | Verdict |
|---|---|---|---|
| H1 Dead has above-average edge | Same-day +0.016 [−0.022, +0.055]; 7d-forward +0.027 [−0.009, +0.064]; OHLC +0.009. Sign flips across the four ~5-month blocks (+0.05, +0.05, −0.05, +0.01). Leader is nominally best (+0.059, p = 0.08). | Did not replicate (was +0.16 R) | **Fail** |
| H2 Leader & Dead stickier than rolling windows produce | 1-day P(stay): Leader 0.72 vs null 0.51; Fading 0.73 vs 0.61; Dead 0.74 vs 0.69; Emerging 0.75 vs 0.73. Raw p 0.024 / 0.024 / 0.049 / 0.146. | Same pattern, and Fading now sticky too | **Direction holds. Not significant after Holm**: 0.024 is the floor for 40 permutations, so this needs ≥ 1,000 permutations to settle. |
| H3 Dead\|Leader predict reversal (user) | Price 24h against signal: Leader 0.47 vs base 0.51 (leans *continuation*); Dead 0.50. HMM flip +24h: Leader +0.046 (p = 0.10); Dead −0.021. | Same: no reversal | **Fail, now on two independent datasets** |
| H4 best knob depends on quadrant | No quadrant differs from the pooled effect, under either touch rule (all p ≥ 0.19) | Same | **Fail** |
| Pooled knobs (not a hypothesis) | Tight SL (×0.5) **−0.196 R** (OHLC) / −0.157 (close); tight TP (×0.5) **+0.119 R** / +0.097 | v3's "tight SL helps +0.137" came from close-based MFE/MAE shortcuts | **v3 tight-SL result reversed.** Path-exact re-settlement says tight stops hurt. Tight TP helps, consistent with 77% of brackets expiring. |
| Exploratory (not pre-registered) | Fading: price reverses more (0.58 vs 0.51, +0.076, p = 0.008), but the HMM flips *less* (−0.052, p = 0.044) | — | Contradictory definitions. **Record as a hypothesis for the confirmation set, not a finding.** |

**What this means for Kairos (EURUSD alone — superseded by the 5-symbol run below).**
- ~~**Quadrant *state* is real.**~~ Withdrawn: the "persistence" was mostly the iid null being too weak. See the 5-symbol section.
- **Quadrant *level* is not a tradeable edge** for EURUSD. It does not tell you which way price goes next, and it does not tell you which SL/TP knob to use.
- H1 is demoted from "survives every test" to "30-day artifact until it replicates in the forward confirmation data".

## Five-symbol reconstruction — EURUSD, XAUUSD, US500, BTCUSD, USDJPY (2026-09-23)

`scripts/reconstruct_signal_history.py --symbols EURUSD,XAUUSD,US500,BTCUSD,USDJPY --n-perm 1000 --geom-perm 300`
Output: `.cache/recon_<SYM>_summary.json`, `recon_POOLED_summary.json`.

**Symbol choice.** One per independent cluster, from 21-month daily-return correlation.
- **EURUSD:** EUR bloc (GBPUSD 0.82, USDCHF −0.84).
- **XAUUSD:** gold (XAUEUR 0.96, XAUAUD 0.93, XAGUSD 0.80).
- **US500:** US indices (US30 0.92). Picked over US30 because it has less overlap with the others; |corr| to EURUSD/XAUUSD is 0.02/0.18.
- **BTCUSD:** crypto. Its largest correlation is US500 at 0.56; to EURUSD 0.27, XAUUSD 0.39.
- **USDJPY (my pick):** yen, rates-driven, and the least correlated with the other four (max 0.64, to EURUSD, sign-inverted). AUDUSD is equally low but is a risk-on proxy that overlaps US500 (0.54).

**Data depth.** 1h back to 2025-01-01 for all but **BTCUSD: 2026-01-01 only**. That is 8.7 months. Every BTC venue on TDVA has the same limit (Coinbase, Bitstamp, Binance, Kraken, Bitfinex).

**Validation per symbol (30-day live overlap)**

| Symbol | Geometry used (live, last 7 d) | Direction match | Settlement parity |
|---|---|---|---|
| EURUSD | 5.79×ATR, 3/5 | 83% | 96.2% |
| XAUUSD | 2.28×ATR, 3/6 | 79% | **78.0%** |
| US500 | 2.28×ATR, 3/6 | 84% | 85.4% |
| BTCUSD | 1.25×ATR, 3/8 | — | 99.2% |
| USDJPY | 2.48×ATR, 3/5 | 78% | 87.8% |

- **Low parity on XAUUSD/US500/USDJPY is a price-path mismatch, not an outcome-rule bug.** The live entry sits a median 0.08 SL-distance from the TDVA close for XAUUSD (EURUSD 0.02), with p90 1.35. Most mismatches are sl↔tp/expired swaps.
- Label, R and quadrant are all built on TDVA prices consistently, so internal comparisons are valid. Absolute R levels for those three are only approximate.
- The identical XAUUSD and US500 geometry is real: both have the live median brick/ATR ≈ 2.28 and a modal 3/6.

**Null upgrade (changes earlier conclusions).**
- Hourly signals share most of their 48-bar path, so R comes in clumps.
- The within-symbol **iid** shuffle used in v3 and the EURUSD run destroys that clumping, and rolling-window quadrants then look too sticky and too predictive.
- The primary null is now a **circular shift**: each symbol's R series is rotated by at least 14 days against its labels. This keeps the autocorrelation and breaks only the alignment.
- The panel is rebuilt exactly for every draw (`FastPanel`: two prefix sums per window). It matched `quadrant_panel()` on 8,040 of 8,041 EURUSD stamps, and 1,000 draws take 15 s.
- The iid results are still printed for reference.

**Results (1,000 shift-null draws per symbol; Holm across the 16-test family within each run)**

| Test | EURUSD | XAUUSD | US500 | BTCUSD | USDJPY | Pooled (5) |
|---|---|---|---|---|---|---|
| H1 Dead − all, R (within symbol) | +0.016 | −0.050 | −0.030 | +0.058 | **+0.171** | +0.027 |
| H1 Holm (same-day / shift null) | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 / 1.0 | **0.008 / 0.015 PASS** | 1.0 / 0.70 |
| Leader − all | +0.059 | +0.010 | +0.018 | +0.064 | **−0.140** (p 0.001) | −0.010 |
| H2 P(stay) Leader, obs/shift null | 0.72/0.65 | 0.67/0.68 | 0.67/0.65 | 0.67/0.73 | 0.68/0.66 | 0.68/0.68 |
| H2 P(stay) Dead, obs/shift null | 0.74/0.71 | 0.70/0.69 | 0.66/0.66 | 0.67/0.65 | 0.65/0.67 | 0.69/0.68 |
| H2 Holm (Leader / Dead) | 0.61 / 0.72 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 | 1 / 1 |
| H3 reversal (any quadrant, either definition) | none | none | none | none | none | none |
| H4 quadrant-specific knob | none | none | none | none | none | none |
| Pooled tight SL ×0.5 (OHLC) | −0.196 | +0.033 | +0.006 | +0.006 | −0.043 | −0.043 |
| Pooled tight TP ×0.5 (OHLC) | +0.119 | +0.071 | +0.097 | +0.025 | +0.079 | **+0.082** |

**Geometry robustness.** Same labels and brick, re-settled at SL/TP 2/4, 3/9 and 5/5, each on its own R and its own `resolved_at`.
- **USDJPY Dead:** +0.134 to +0.171 at 3/5, 2/4 and 5/5 (all p = 0.003); +0.038 at 3/9. Leader is −0.11 to −0.19 at 3/5, 3/9 and 5/5. **Robust to geometry.**
- **The other four:** no Dead/Leader effect is stable in sign across geometries. The "significant" ones come and go with the geometry, for example US500 Leader −0.057 at 5/5 only, and BTC Leader +0.14 at 2/4 only.

**Time stability for USDJPY Dead** (four ~5-month blocks): +0.06, +0.20, +0.30, +0.14. All are positive; one CI excludes 0 and one is borderline.

**Verdict.**

| Hypothesis | Status after five symbols (21 months; BTC 8.7) |
|---|---|
| H1 Dead has edge | **Symbol-specific.** Strong and geometry-robust in USDJPY only. It fails in the other four and in the pooled test (Holm 0.70), so it is not a universal quadrant property. |
| H2 Leader/Dead persist | **Fail.** Under the shift null, no symbol passes Holm. The v3/EURUSD "persistence" was the iid null's artifact. Quadrants stay ~70% day to day, but rolling windows over autocorrelated R do that on their own. Only Fading is weakly stickier (pooled 0.74 vs 0.71, raw p 0.006; not pre-registered). |
| H3 Dead\|Leader → reversal (user) | **Fail in all 5 symbols and pooled**, by price and by regime flip. |
| H4 quadrant-specific knob | **Fail in all 5 and pooled.** |
| Tight TP ×0.5 (global, not a hypothesis) | **+0.08 R pooled, positive in all 5 under OHLC.** This is the only consistent effect. It is geometry-level: brackets mostly expire (37% pooled), so a closer TP converts expiries into wins. **This is a heavy-sweep geometry question, not a Kairos one.** |
| Tight SL ×0.5 | **Mixed by symbol** (EURUSD −0.20, others ≈0/+). Not a global lever. |

**Implications for the design.**
1. **Kairos as a global quadrant-action layer is not supported.** No pre-registered quadrant effect holds across symbols. The K2 stay-probability idea loses its basis too, because stays are no stickier than mechanics.
2. **The user's thesis ("a quadrant defines an edge") holds for one symbol out of five.** A per-symbol edge exists (USDJPY Dead), but it cannot be predicted from the quadrant definition alone. That makes it a *per-symbol, per-cell* finding to confirm forward, not a rule.
3. The honest next step is a **forward confirmation of exactly one pre-registered claim**: "USDJPY Dead cells out-earn USDJPY's average", as shadow stamping on live signals. Fix the threshold now: Dead − all > 0 with the symbol-day CI excluding 0 after 60 days.
4. Put the tight-TP result into the heavy-sweep geometry work (reachability/expectancy), where it belongs.

**Caveats that remain.**
- Full-stream only: the gates (Kelly, p_win, TimesFM) cannot be rebuilt, so the qualified cohort may differ.
- Geometry is fixed per symbol at today's live value.
- Cadence is 1h rather than 15 min.
- BTC has 8.7 months of data.
- Parity is 78–88% on three symbols.

## Design

Kairos has five layers. Each can be added and removed independently, and each goes
shadow → evidence → live on its own.

```
K0 data  ──►  K1 edge state  ──►  K2 transitions  ──►  K3 action (gated on evidence)
   │                                                        ▲
   └──────────────►  K4 covariance / cluster exposure  ─────┘
```

### K0 — Data foundation (prerequisite)

1. **Keep history beyond 30 days.** Retention goes from 30 to 120 days through `config/feature_flags.yaml` `signal_sim_retention_resolved_days`. Studio injects only Secrets as env; every other knob lives in `config/*`. No schema change, no new table.
   - About 2,400 resolved rows/day → roughly 290k rows at steady state.
   - The existing `resolved_at` / `ts` indexes cover the access paths.
   - Without this, every research run is capped at 30 days, and the local cache on one machine is the only long-term record.
2. **Episode view (VIEW, not table).** `v_kairos_episodes`: the first light-sweep row per (symbol, signal, hour), using `DISTINCT ON`. It carries the columns the script uses (`regime`, geometry, `outcome`, `pnl_bricks`, `mfe`, `mae`, `drift_*`, gate stamps, `qualified`) plus a derived `r`.
   - This moves the 6.6-rows-per-setup dedup rule out of Python and into one shared definition.

### K1 — Edge-state estimator (shadow stamp)

3. **Current-state view.** `v_kairos_state_current` is a VIEW over `v_kairos_episodes`. Per symbol × vol(4) × direction(2) (the G8 grain), it computes:
   - `n_14d` and `level` (14-day mean R, shrunk toward the symbol mean with k = 10);
   - `n_7d` and `momentum` (7-day minus 14-day shrunk R);
   - `quadrant`, set to `NULL` when `n_14d < 10` or `n_7d < 5`.

   It only counts episodes with `resolved_at < now()`, so there is no lookahead by construction.
4. **Stamp at signal time.** Add nullable columns to `nt_signal_sim` using `ALTER TABLE … ADD COLUMN`, the same pattern as migrations 154 and 164: `kairos_quadrant`, `kairos_level`, `kairos_momentum`, `kairos_n`.
   - The light sweep fetches `v_kairos_state_current` **once per sweep** for all symbols and passes the matching row into `_build_signal_sim_payload` (`orchestrator.py:857`).
   - This gives every future signal the label it had *at the time*, which is the confirmation dataset.
   - It has zero effect on qualification or sizing.
5. **Flag.** `kairos_mode: off | shadow | live`, added to `_DEFAULTS` and `feature_flags.yaml`. `shadow` stamps only. `live` has no meaning until K3 exists.

### K2 — Transition model (offline first)

6. **Estimate from stamped history, not reconstruction.** Build P(quadrant at t+h | quadrant at t) for h ∈ {1, 2} days (the 48h hold), always reported against the shuffled-outcome null (`== 9b` in the script).
7. **Transition-weighted edge (offline test).**
   - Formula: `edge_hold = Σ P_h(q→q') · edge(symbol, cell, q')`.
   - Score it against point `p_win_dir` on the same resolved episodes, using Brier score and calibration slope.
   - Only if it beats point p_win out of sample does it become a candidate input to EV or Kelly (a separate scope, and it would go through the EV engine's shadow modes).
8. **Stamp `kairos_p_stay`** once K2 passes, as a reason to *shrink* Kelly when the state is unstable. It is not an edge source.

### K3 — Action layer (blocked until hypotheses pass)

9. **Shadow first.** Per-quadrant shadow multipliers (size) and shadow SL/TP variants are stamped as would-values, the same way `qualified_geometry_shadow` works. Nothing changes live until the gate-pass cohort confirms the sign.
10. **Hypothesis-driven only.** Only actions whose hypothesis passes (see Research plan) get a shadow variant. At current evidence, the only candidate is a **size tilt toward Dead** (H1). Per-quadrant SL/TP and reversal logic have no candidates.

### K4 — Covariance / cluster exposure (independent track)

11. **Shadow cluster cap.** Group symbols by rolling price correlation (Ledoit-Wolf shrinkage, 4h returns, 60-day window, clusters where |ρ| ≥ 0.7). Stamp `kairos_cluster` and `kairos_cluster_open_risk` (Kelly already committed in that cluster) on each signal.
    - A shadow cap such as "cluster risk ≤ 1.5× single-symbol Kelly" logs would-reject.
    - This needs no edge estimate, so it is the least overfit piece. It can go live on its own evidence (frequency of same-cluster stacking) without waiting for K1–K3.
12. **Reuse existing code where it fits.** `core/correlation_regime.py` already computes rolling correlation and a correlation-regime HMM. K4 reuses its rolling-correlation step, not the HMM (it has no shadow history and adds another label that could flap).

### D — Design constraints

- **D1. No new tables.** Views plus nullable shadow columns on `nt_signal_sim`, consistent with the view-first preference. Retention is config.
- **D2. Point-in-time or nothing.** No decision may use a quadrant computed with data resolved after the signal.
- **D3. One grain.** G8 (vol4 × direction2) is fixed for confirmation. G16 lost on every discovery test, and re-choosing the grain after seeing the confirmation data is a forking path.
- **D4. Structural break on 2026-09-22.** Labels change from raw to debounced. Discovery data (≤ 09-22, raw) generated the hypotheses; confirmation data (≥ 09-23, debounced) tests them. They are never pooled for a confirmatory test.
- **D5. Concurrent changes are covariates, not noise.** Changes that alter who qualifies and what R looks like:
  - `p_win_in_ev_mode` (evaluated after 24h),
  - geometry incubation shadow,
  - `dynamic_atr_mode` flip (planned 2026-10-05).

  Each confirmatory result is reported both overall and split before/after each change.

## Research plan — pre-registered hypotheses

- **Discovery data:** 2026-08-23 → 09-22.
- **Confirmation data:** 2026-09-23 onward, point-in-time `kairos_*` stamps once K1 ships (reconstructed labels before that).
- **Primary cohort:** full stream, for existence. **Product cohort:** gate-pass, where each result must at least have the same sign.
- **Statistics:** cluster bootstrap over symbol-days; excess over each symbol's base rate where relevant; **Holm correction across H1–H6**.

| ID | Hypothesis | Exact test | Pass |
|---|---|---|---|
| H1 | Dead has above-average forward edge | Same-day and 7-day-forward R/ep, Dead − all | CI > 0 (Holm-adjusted), full stream; same sign in gate-pass |
| H2 | Leader and Dead are stickier than the rolling windows alone produce | 1-day P(stay) − shuffled null (≥ 100 perms) | Both > 0; permutation p < 0.05 |
| H3 | **Dead\|Leader predict reversal** (user) | Excess over symbol base of (a) signed 24h drift < 0 and (b) HMM direction flip by +24h, per quadrant | Either definition CI > 0 for the named quadrant. The definitions are fixed now; no new definitions after seeing the data. |
| H4 | The best SL/TP knob depends on quadrant | (quadrant knob effect − pooled knob effect) for tight SL ×0.5 and tight TP ×0.5 | At least one quadrant CI excludes 0 after Holm, with the same sign in gate-pass |
| H5 | Transition-weighted edge is better calibrated than point p_win | Brier and calibration slope on resolved episodes | Brier improvement CI > 0 |
| H6 | Same-cluster stacking is frequent enough to matter | Share of qualified signals opened while ≥ 1 same-direction signal in the same cluster is open | > 10% of qualified signals |

**Sample-size requirements, scaled from the current CI widths:**

| Test | Current CI half-width | Effect to detect | Panel days needed |
|---|---|---|---|
| H1 same-day | ±0.12 (116 clusters) | 0.076 R | ≈ 35 |
| H3 per quadrant | ±0.11 (73 clusters) | 0.06 excess | ≈ 50 |
| H4 interaction | ±0.13 (116 clusters) | 0.06 R | ≈ 65 |

A 14-day warm-up comes before the panel. Confirmation data starting 2026-09-23 therefore gives about **57 panel days by 2026-11-21**:
- enough for H1–H3;
- borderline for H4;
- gate-pass-only conclusions will need about 6 months.

## Stage gates

| Gate | Date | What happens | Criterion |
|---|---|---|---|
| G0 | on approval | K0: retention → 120 days. Create the `v_kairos_episodes` view. | View row count ≈ distinct (symbol, signal, hour) in `nt_signal_sim` (±1%). Script run on the view reproduces v3 `== 1`–`== 4`. |
| G1 | +1 week | K1 shadow: create the `v_kairos_state_current` view, add the `kairos_*` columns, `kairos_mode: shadow`, stamp in the light sweep. K4 shadow stamps. | ≥ 95% of light-sweep rows have a non-null `kairos_quadrant` when `n` qualifies. Full pytest green. No change in qualified count vs the prior week beyond normal variance. |
| G2 | 2026-10-22 (interim) | Descriptive report on the confirmation data: quadrant shares, stamp coverage, H2 and H6. **No H1/H3/H4 decisions.** | Report only. H6 can promote K4 to a live-candidate scope. |
| G3 | 2026-11-21 (confirmation) | Full H1–H6 run with Holm correction. | A hypothesis passes only by its row in the table above. |
| G4 | after G3 | K3 shadow variants, only for passed hypotheses. K2 input scope, only if H5 passes. | Each shadow variant gets its own 4-week shadow window and gate-pass sign check before any live flip. |

Mechanical check for G1 coverage:

```sql
SELECT date_trunc('day', ts) AS d,
       count(*) FILTER (WHERE kairos_quadrant IS NOT NULL)::float / count(*) AS coverage
FROM nt_signal_sim
WHERE source = 'sweep-light' AND ts >= now() - interval '7 days'
GROUP BY 1 ORDER BY 1;
```

## Build inventory (G0–G1 only)

| Artifact | Type | Notes |
|---|---|---|
| `signal_sim_retention_resolved_days: 120` | `config/feature_flags.yaml` + getter | **Done 2026-09-23**: `eod_retention.py` reads the getter; the Settings field was removed; tests in `test_eod_retention.py`. |
| `supabase/migrations/NNN_kairos_views_and_stamps.sql` | Migration | `v_kairos_episodes`, `v_kairos_state_current`, `ALTER TABLE nt_signal_sim ADD COLUMN kairos_quadrant text, kairos_level real, kairos_momentum real, kairos_n int, kairos_cluster text, kairos_cluster_open_risk real`. The next free number at build time (165 is the latest). |
| `regime_platform/sweep_orchestrator/kairos.py` | New module | Fetches `v_kairos_state_current` once per sweep. Pure `lookup(symbol, vol, direction)`. Cluster map from rolling correlation. |
| `orchestrator.py` | Edit | Pass `kairos_*` into `_build_signal_sim_payload` when `kairos_mode != off`. |
| `supabase_client.py` | Edit | Allow-list the new columns for insert. |
| `core/feature_flags.py`, `config/feature_flags.yaml` | Edit | `kairos_mode` (default `shadow`), `kairos_shrink_k: 10`, `kairos_min_n_14d: 10`, `kairos_min_n_7d: 5`. |
| `scripts/regime_cell_edge_persistence.py` | Edit | Read `v_kairos_episodes` and the stamped `kairos_quadrant` when present; `--since 2026-09-23` confirmation mode. |
| `tests/test_kairos.py` | New | Quadrant assignment, shrinkage, n-floors, no-lookahead on `resolved_at`, payload stamping off/shadow. |

## Open decisions

1. ~~**Retention to 120 days.**~~ Approved 2026-09-23 and implemented in config (see K0).
2. **Pre-registration freeze.** Approve H1–H6 and the fixed G8 grain as written. Changing them after G2 invalidates the confirmation.
3. **Frontend exposure.** Recommended: no user-facing Kairos board before G3. An internal-only view is fine after G1.
4. **The name.** Kairos, or an alternative.

## In scope / out of scope

**In scope:** K0 retention and views; K1 shadow stamps; K4 shadow cluster stamps; the research script's confirmation mode; the G2/G3 reports.

**Out of scope until G3/G4:** any change to qualification, Kelly, SL/TP, leverage or delivery; a `kairos_mode: live` behaviour; K2 as an EV input; nobletradingapp UI; Masaniello batch (on hold); `dynamic_atr_mode` and `p_win_in_ev_mode` decisions (tracked in their own scopes).
