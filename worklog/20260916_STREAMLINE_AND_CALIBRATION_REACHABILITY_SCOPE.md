# Streamline signal stack + close the calibration–reachability gap

> **Priority**: P0 methodology · **Status**: Batches A–D + F shipped (v7.9.0); Batch E walk-forward optional · **Date**: 2026-09-16  
> **Supersedes for execution order**: scattered Phase 2/3 follow-ups that waited on calendar reviews  
> **Depends on**: `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md` (findings), existing deadline-aware scoring (live), vol-tier buckets (live)  
> **Does not wait for**: Oct 5 Stage 1 calendar — each phase has a **same-day** proof gate (tests + historical replay)

## Why this worklog exists

Three objectives, one program:

1. **Streamline** — one settlement contract, one PnL truth, one qualification order, less dual-path drift.
2. **Close the calibration–reachability gap** — `p_win` must estimate the same event the resolver scores.
3. **Fast, tangible proof** — keep `off | shadow | live`, but replace “wait weeks of organic shadow” with **historical replay + regression tests that fail the build if methodology regresses**.

Reference audit: `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`.

## Guiding rules (non-negotiable)

| Rule | Meaning |
|---|---|
| **Same event** | Whatever number goes into `EV = p·profit − (1−p)·stake` must be P(same outcome the resolver labels win). |
| **One settlement** | Resolver, heavy-sweep scoring, paper book, scorecard call **one** module for outcome + PnL. |
| **Shadow ≠ calendar** | A mode flips to `live` only after a **named replay script** shows improvement on aggregate metrics — not after N days of logs sitting idle. |
| **Measurement first** | Fixes that make numbers *honest* ship before fixes that make numbers *look better*. |
| **Fail closed on measurement** | If `predicted_p_win_raw` is required and missing → insert fails / path errors. No silent fallback to enforced for bias/Brier. |
| **Severity batches** | Same as AGENTS.md: critical → high → m…; each batch independently verified (full pytest + behavioral smoke). |

## Target end-state (one diagram)

```
                    ┌─────────────────────────────────────┐
                    │  SettlementContract (single module) │
                    │  resolve + pnl + costs + expiry     │
                    └──────────────┬──────────────────────┘
           ┌───────────────────────┼───────────────────────┐
           ▼                       ▼                       ▼
     EOD resolver            Heavy-sweep grid         Paper / scorecard
     (nt_signal_sim)         (selection Sharpe)       (dual-report PnL)
           │                       │
           ▼                       ▼
     Calibration store         Brick params YAML
     raw + enforced p_win      (parity with live contract)
           │
           ▼
     p_win_bracket ──► EV / gates / Kelly   (was: directional p_win)
           │
           ▼
     Reliability per (symbol, direction, vol_tier, bucket)
```

---

## Phase 0 — Baseline freeze & harness (same day)

**Goal:** Make today’s behavior measurable so every later phase has a before/after table.

| ID | Task | Acceptance (same day) |
|---|---|---|
| 0.1 | Snapshot config: `feature_flags.yaml`, gate thresholds, `brick_params.yaml`, EV weights, `EXPIRY_BARS` into `worklog/references/20260916_baseline_snapshot.md` | File exists; values match live reads |
| 0.2 | Add `scripts/replay_settlement_parity.py` — for N historical anchors (reuse 70-anchor pattern from deadline-aware work), score each path with **current** resolver rules and print outcome mix + PnL | Script runs offline against cached/synthetic fixture; prints table |
| 0.3 | Add `scripts/replay_calibration_gap.py` — from resolved `nt_signal_sim` (or fixture): report bias on resolved-only vs bias if expired counted as loss vs `p_dir`-style drift metrics; print per-symbol expiry | One command; numbers match audit’s published ranges on live pull |
| 0.4 | Pin pytest invocation + add `tests/test_settlement_contract_smoke.py` skeleton (xfail or skip until Phase 1) | Suite green; skeleton registered |

**Done when:** two scripts + baseline doc exist. No production behavior change.

---

## Phase 1 — Settlement contract (Critical) · off → live via tests

**Goal:** One module owns outcome + PnL. Live backtest/selection cannot diverge again.

### 1.1 New module

`regime_platform/sweep_orchestrator/settlement_contract.py` (name flexible; keep pure):

```text
Inputs:  entry, sl, tp, direction, bars, deadline_bars, same_bar_policy, costs
Bars:    {close} or {open, high, low, close} — detect; prefer H/L when present
Output:  {outcome, exit_price, hold_bars, mfe, mae, pnl_bricks, pnl_dollars_net, flags}
```

| Rule | Spec |
|---|---|
| Deadline | `EXPIRY_BARS` shared constant (today 48) |
| Touch | If OHLC: `high≥TP` / `low≤SL`; else close-walk |
| Same-bar both | **Parameterized** `same_bar_policy: sl_first | tp_first`; **default `sl_first`** (matches backtest OCO; conservative). Keep `tp_first` only behind flag for A/B replay |
| Expired MTM | Signed brick delta at last bar |
| Costs | Half-spread bps + commission bps per symbol class (config table; default 0 until instrument params filled) |
| Gap | If OHLC and open beyond SL → fill at open (worse) |

### 1.2 Rewire callers (order matters)

| Caller | Change |
|---|---|
| `eod_resolver.resolve_signal_outcome` + `compute_pnl` | Thin wrappers over contract; keep SQL write shape |
| `heavy_sweep` deadline-aware Sharpe | Score trades with **same** contract (no trail/time-stop unless live has them — see 1.3) |
| `paper_book` | Use contract PnL + costs; stop forcing `r_mult=0` on expired when MTM exists |
| Scorecard migration (new) | Dual columns — see Phase 2 |

### 1.3 Kill selection/live path mismatch

In heavy-sweep grid RenkoConfig used for **production pick**:

- Trailing stop **off** (or live-equivalent only)
- Time stop **off** unless live uses it
- OCO priority = `same_bar_policy`
- Deadline always on (already live)

### 1.4 Proof (same day, not weeks)

| Gate | Pass criteria |
|---|---|
| Unit tests | Same-bar both-hit, gap-through SL, expiry MTM, OHLC vs close-only, cost subtraction |
| Parity test | For fixture series, resolver and `settlement_contract` produce identical outcomes |
| Replay | `replay_settlement_parity.py --policy sl_first --policy tp_first` — document win%/expiry/PnL delta; pick default from evidence, not preference |
| Regression | Full `pytest tests/ -q` green |

**Flag:** `settlement_contract_mode: off | shadow | live`  
- `shadow`: contract computes; old path still writes outcomes; log both to `nt_settlement_shadow`  
- `live`: only contract writes  
**Flip to live when:** unit+parity green and replay shows no catastrophic symbol regression (define: no symbol’s resolved win% moves &gt;15pp *and* total PnL does not drop &gt;20% without written rationale).

---

## Phase 2 — Honest measurement (Critical/High) · ship without waiting

**Goal:** Stop lying to ourselves about calibration and user PnL. No model change required.

| ID | Task | Detail | Proof |
|---|---|---|---|
| 2.1 | **Mandatory raw capture** | `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` default **true**; if column null on write → **error**, not silent skip | Test: insert with missing raw raises |
| 2.2 | **Kill measurement fallback** | `fetch_calibration_bias_raw` and Brier **never** fall back to enforced p_win; return `None` / skip record with explicit `error_source=enforced_fallback` only if forced | Unit tests; grep shows no silent fallback |
| 2.3 | **Scorecard dual-report** | New view/SQL: `pnl_closed_only`, `pnl_all_resolved` (includes expired MTM), `win_pct_closed`, `expiry_pct`, `pct_expired_bucket` | Migration + fixture query matches hand calc |
| 2.4 | **Expiry first-class** | Scorecard + admin API expose expiry rate trend; alert already exists — ensure it reads same view as scorecard | One query path |
| 2.5 | **Quarantine proxy ATR** | `atr_source=proxy_current` rows excluded from expectancy/bias inputs (or `n_proxy/n` column); never look-ahead into live gates | Tests on bucket builder |
| 2.6 | **SQL ↔ Python bias parity** | Dashboards call Python bias **or** SQL uses identical decay+episode logic | Diff test on fixture |

**Flag:** measurement changes can go **live immediately** (they change reporting/trust, not signals) except 2.2’s enforcement-input path — keep enforcement on enforced-bias as today; only *measurement* paths hard-fail.

**Proof same day:** replay_calibration_gap prints before/after; pytest green; no wait for new live trades.

---

## Phase 3 — `p_win_bracket` (the actual gap) · shadow then live

**Goal:** Replace “directional confidence as P(win)” with a bracket-aware probability, even if v1 is empirical.

### 3.1 Definition

```text
p_dir        = current logit blend (keep name; stop calling it P(win))
p_resolve    = P(TP or SL resolved under settlement | symbol, vol_tier, bucket)  # from reachability
p_win|resolve= Beta-Binomial shrink of tp_hit/(tp_hit+sl_hit) in bucket
p_win_bracket= p_resolve * p_win|resolve          # EV’s p
p_expire     = 1 - p_resolve
```

EV (v1, honest):

```text
ev = p_win_bracket * net_profit - (1 - p_win_bracket - p_expire) * net_loss
     + p_expire * expected_mtm_term   # optional v1.1; v1 may treat expired as 0 cost after MTM dual-report
```

Minimum bar for v1: **gate on `p_win_bracket`**, even if EV keeps using it as binary:

```text
if p_win_bracket < MIN_P_WIN_BRACKET: fail gate
```

### 3.2 Implementation slices

| Slice | Work | Mode |
|---|---|---|
| 3a | Lookup service: `(symbol, vol_tier, bucket) → {p_resolve, p_win_resolve, n, atr_source}` with Beta prior (e.g. α=β=2) | pure + unit tests |
| 3b | Wire into orchestrator **alongside** current p_win; write both to `nt_signal_sim` (`p_win_dir`, `p_win_bracket`) | always capture; gates still use old |
| 3c | Shadow gate: log `would_reject_bracket` when old qualifies but bracket doesn’t | `p_win_bracket_gate_mode: off\|shadow\|live` |
| 3d | Replay: historical signals — compare qualification set, expiry, expectancy under old vs bracket gate | **go/no-go same day** |
| 3e | Live gate + EV uses `p_win_bracket` | after 3d pass |

### 3.3 Replay acceptance (3d)

Script: `scripts/replay_p_win_bracket_gate.py`

Must report for last ~90d (or fixture):

| Metric | Required direction |
|---|---|
| Expired % of would-be qualified | ↓ |
| Expectancy_r of accepted set | ↑ or flat with higher n |
| Blended calibration bias on accepted | \|bias\| ↓ |
| Qualified count | not collapse to ~0 (floor: ≥20% of old count OR documented) |
| Worst symbol PnL | no new catastrophic (&lt;−50% vs old) without carve-out note |

If replay fails → stay shadow, diagnose bucket sparsity / vol_tier `unknown` share; do **not** flip live.

### 3.4 Immediate reliability (not weeks)

Every light-sweep tick (or every resolved signal), upsert:

`nt_calibration_cells(symbol, direction, vol_tier, bucket, n, brier, bias, win_rate)`

Admin panel reads this table — no “wait for bias to stabilize.” Thin cells show `n` and gray out.

---

## Phase 4 — Qualification & EV cleanup (High)

**Goal:** One qualification story; EV sources that mean what they say.

| ID | Task | Spec |
|---|---|---|
| 4.1 | **Unify light/heavy gate order** | Enforce calibration **before** gates on **both** paths |
| 4.2 | **Align heavy gate set** | Same TimesFM mode + cooldown + p_win semantics as light (or document heavy as “research only” and stop writing heavy signals to same sim tables without a path tag) |
| 4.3 | **Rename in telemetry** | `p_win` → `p_win_dir` everywhere in new writes; UI copy: “direction confidence” until bracket gate is live |
| 4.4 | **Markov N** | `n_steps = tp_bricks` (or estimated bars-to-TP); remove global 10 unless replay proves better |
| 4.5 | **Soft band honesty** | Either raise hard floor to `MIN_P_WIN` **or** stop labeling soft-band signals `qualified=true` in scorecard (`qualified_full` vs `qualified_soft`) |
| 4.6 | **Underconfidence** | Do not apply same Kelly cut as overconfidence — branch on status |
| 4.7 | **TimesFM defaults** | Code defaults = production intent (k, clamp); CI assert vs feature_flags/studio |

**Proof:** unit tests + one replay comparing qualified sets before/after 4.1–4.5; no calendar wait.

---

## Phase 5 — Heavy-sweep selection hygiene (High)

| ID | Task | Proof |
|---|---|---|
| 5.1 | Grid uses Phase 1 contract only | Parity test: grid trade outcomes == contract outcomes on same bars |
| 5.2 | Walk-forward harness | `scripts/walkforward_brick_params.py` — train 60d / test 30d roll; record n_trials; Deflated Sharpe or PSR |
| 5.3 | Contaminated window filter | Any shadow table rows in 2026-09-13→14 marked `contaminated=true`; excluded from go/no-go |
| 5.4 | Flip reachability/ATR-anchored only via replay gates | Same pattern as 3d |

---

## Phase 6 — Streamline / delete (Medium, after 1–5 land)

**Goal:** Cohesive workflow, less surface area.

| ID | Action |
|---|---|
| 6.1 | Delete or archive stale docs that contradict live EV (old 0.40/0.30/0.30 blends in marketing/README) — replace with one pipeline doc generated from this worklog |
| 6.2 | Collapse duplicate feature flags that only exist as historical incident notes into a short `ACTIVE_MODES.md` (single table of off/shadow/live) |
| 6.3 | Single `docs/SIGNAL_PIPELINE.md`: generate → qualify → settle → calibrate → select; link modules |
| 6.4 | Remove dead `p_historical` weight row or wire it for real — no ghost 0.30 |
| 6.5 | One env/config source for gate thresholds (already mostly feature_flags.yaml) |

**Not in scope (explicit non-goals):**

- Live order dispatch / MetaAPI / broker integration changes  
- New ML models (TimesFM3, conviction×ATR geometry thesis) — **blocked on Phase 3 trust**, same as parked thesis  
- Marketing copy beyond removing false formulas  
- Waiting on Oct 5 calendar as a process gate (evidence gates replace it)

---

## Execution order & batches

```text
Batch A (Critical, ~1–2 days):     Phase 0 + Phase 1
Batch B (Critical/High, ~1 day):   Phase 2  (can parallel A after 0.2 exists)
Batch C (Critical, ~2–3 days):     Phase 3  (needs A+B data quality)
Batch D (High, ~1–2 days):         Phase 4
Batch E (High, ~1–2 days):         Phase 5
Batch F (Medium, ~0.5 day):        Phase 6
```

Each batch ends with:

1. `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider`  
2. Named replay script stdout attached to worklog section  
3. Mode table updated in `ACTIVE_MODES.md`  
4. Explicit **flip / stay shadow / revert** decision with numbers

---

## Mode flags (keep the pattern)

| Flag | Phases | Flip rule |
|---|---|---|
| `settlement_contract_mode` | 1 | Parity tests + replay; default target **live** after Batch A |
| `scorecard_dual_report` | 2 | **live** with migration (reporting only) |
| `calibration_raw_required` | 2 | **live** immediately |
| `p_win_bracket_gate_mode` | 3 | shadow → live only if 3d replay table passes |
| `p_win_in_ev_mode` | 3 | shadow → live with 3e |
| `heavy_gate_parity_mode` | 4 | live after unit+replay |
| existing reachability / ATR / deadline | 5 | deadline already live; others via 5.4 |

---

## Definition of done (program)

The program is done when **all** are true:

1. One settlement module is the only outcome/PnL implementation; parity tests guard it.  
2. Scorecard shows dual PnL + expiry; calibration measurement cannot silently use enforced p_win.  
3. Live EV/gates use `p_win_bracket` (or a written, evidence-backed exception).  
4. Light and heavy qualification share one order and one settlement.  
5. Heavy-sweep selection is walk-forward tested with multiplicity recorded.  
6. `ACTIVE_MODES.md` matches production; no dual docs claiming dead formulas.  
7. Full pytest green; replay scripts reproducible offline.

---

## Batch A completion log (2026-09-16)

### Shipped

| Artifact | Path |
|---|---|
| Settlement contract | `regime_platform/sweep_orchestrator/settlement_contract.py` |
| Feature flags | `settlement_contract_mode` (live), `settlement_same_bar_policy` (tp_first) |
| Resolver wiring | `eod_resolver.resolve_signal_outcome` / `compute_pnl` delegate when mode≠off; legacy kept for `off` |
| Unit tests | `tests/test_settlement_contract.py` |
| Baseline freeze | `worklog/references/20260916_baseline_snapshot.md` |
| Replay harnesses | `scripts/replay_settlement_parity.py`, `scripts/replay_calibration_gap.py` |

### Same-day proof

```
pytest tests/test_settlement_contract.py tests/test_eod_resolver.py → 58 passed
pytest tests/ (full) → 1019 passed, 21 skipped
```

Fixture replay highlights (not production metrics):

- OHLC catches intraday TP that close-only marks expired (`buy_intraday_only`).
- Same-bar both-touch flips TP→SL under `sl_first` (OHLC only; close-only buy cannot hit both ordered levels).
- Calibration gap fixture: XAU-like 66.7% expiry → resolved-only bias +0.20 vs expired-as-loss bias +0.53 (survivorship visible).

### Intentionally not flipped yet

- `settlement_same_bar_policy` stays **`tp_first`** (historical parity). Flip to `sl_first` only after live candle replay table is attached here.
- Heavy-sweep RenkoConfig parity / paper_book rewrite: Batch A scope was contract + resolver; those callers are Batch D/E follow-ons.
- Costs default 0 (instrument tables empty).

### Next

Batch B (Phase 2 measurement): mandatory raw capture, kill enforced fallback for bias/Brier, scorecard dual-report.

---

## Batch B completion log (2026-09-16)

### Shipped

| ID | Change | Where |
|---|---|---|
| 2.1 | `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` default **True**; raw always written when present | `settings.py`, `orchestrator._build_signal_sim_payload` |
| 2.1b | `NOBLE_TRADER_CALIBRATION_RAW_REQUIRED` default True | `settings.py` |
| 2.2a | Brier: raw only; missing raw → `brier=None`, `error_source=raw_missing` | `eod_resolver._write_calibration_record` |
| 2.2b | Episode summaries no longer fall back raw→enforced | `supabase_client._episode_summaries` |
| 2.2c | `fetch_calibration_bias_raw`: excludes raw-absent episodes; returns None if &lt; min_episodes | `supabase_client` |
| 2.3/2.4 | Dual-report scorecard: `pnl_closed_only`, `pnl_all_resolved`, `win_pct_closed`, `expiry_pct` | migration `153_scorecard_dual_report_and_proxy_atr.sql` |
| 2.5a | Reachability buckets track `n_stored`/`n_proxy` | `bracket_reachability.py` + migration 153 |
| 2.5b | Bias-by-bucket **skips** proxy-ATR rows | `calibration_bias_bucket.py` |
| 2.5c | Reachability pick fetch filters `atr_source=stored` | `heavy_sweep_reachability.fetch_bias_by_bucket` |
| Tests | `tests/test_batch_b_measurement.py` + updated raw-bias / eod / lookback tests | |

### Same-day proof

```
pytest tests/ (full) → 1028 passed, 21 skipped
```

### Deploy note

Apply migration `153_scorecard_dual_report_and_proxy_atr.sql` to Supabase **before** relying on new scorecard columns / `n_stored`/`n_proxy` upserts.

**Re-run after 42P16 fix:** original file used `CREATE OR REPLACE VIEW` and inserted columns mid-list (`win_pct_closed` before `expired_pct`), which Postgres rejects. File now **DROP + CREATE** all three scorecard views and **appends** dual-report columns at the end of each select list. Re-apply the same file.

### Next

Batch C — `p_win_bracket` shadow capture + replay gate.

---

## Batch C completion log (2026-09-16) — shadow only

### SQL 153 verification (live DB)

```
v_symbol_signal_scorecard / _7d / _monthly: HTTP 200
dual-report cols: expiry_pct, pnl_all_resolved, pnl_closed_only, win_pct_closed
nt_bracket_reachability.n_stored / n_proxy: present
```

### Shipped (shadow default)

| Piece | Path |
|---|---|
| Probability module | `p_win_bracket.py` — Beta(2,2) shrink, stored-ATR-only buckets, fail-open |
| Migration | `154_nt_signal_sim_p_win_bracket.sql` |
| Feature flags | `p_win_bracket_mode: shadow`, `min_p_win_bracket: 0.45` |
| Orchestrator | Lookup on non-neutral capture; persists `p_win_dir`/`p_win_bracket`/…; logs shadow would-reject |
| Replay | `scripts/replay_p_win_bracket_gate.py` |
| Tests | `tests/test_p_win_bracket.py` |

### Same-day proof

```
pytest tests/ → 1039 passed, 21 skipped
replay fixture: exp% 50→20, n 80→40, win%res 70→75 (illustrative)
```

### Deploy

Apply `154_nt_signal_sim_p_win_bracket.sql` (ALTER TABLE additive). Until applied, new payload fields are filtered out by `NT_SIGNAL_SIM_COLUMNS` whitelist (harmless).

### Not flipped

- `p_win_bracket_mode` stays **shadow** — live gate only after a live candle/outcome replay table is attached (Phase 3.4).
- EV still uses directional `p_win` (Phase 3e).

### Next

Batch D — qualification/EV cleanup (unify light/heavy gate order).

---

## Batch D completion log (2026-09-16)

### SQL 154 verification

`nt_signal_sim.p_win_dir/p_win_bracket/...` queryable (HTTP 200).

### Shipped

| ID | Change |
|---|---|
| 4.1 | Heavy sweep **enforces calibration before** `_evaluate_heavy_qualification` (was qualify-raw then enforce at push) |
| 4.2 | Heavy push no longer double-enforces; uses post-enforcement Kelly |
| 4.3 | Soft-band stamp `p_win_soft_band` on light gate payload |
| 4.4 | `resolve_markov_n_steps(tp_bricks)` — N matches TP distance; global 10 is fallback only |
| 4.5 | Soft-band honesty: publish still allowed with shrink; stamped for scorecard |
| 4.6 | **UNDERCONFIDENT no longer cuts Kelly** (p_win Bayesian shrink retained) |
| 4.7 | TimesFM code defaults: k=10, clamp [0.05, 0.95] |

### Proof

```
pytest tests/ → 1045 passed, 21 skipped
```

### Next

Batch E — heavy-sweep selection hygiene (walk-forward). Batch F — docs streamline.

---

## Batch F + version bump completion (2026-09-16) — **v7.9.0**

### Shipped

| Item | Path |
|---|---|
| Operator modes table | `docs/ACTIVE_MODES.md` |
| Single pipeline reference | `docs/SIGNAL_PIPELINE.md` |
| README EV formula corrected | live logit pool, no `P_imbalance` |
| Ghost `p_historical` weight removed | `ev_engine.P_WIN_WEIGHTS_FULL` (5:4:5 renormalized) |
| TimesFM mapping → yaml only | `feature_flags.yaml` `timesfm:` block |
| Soft TimesFM gate reads weight table | no hardcoded 0.25 |
| Version bump | `pyproject.toml`, `main.py`, both `openapi.yaml`, version tests, README, AGENTS.md, marketing-overview |

### Version anchors (all **7.9.0**)

`pyproject.toml` · `main.py` (2) · `docs/openapi.yaml` · `docs/Setup/openapi.yaml` · `tests/test_version_consistency.py` · README · AGENTS.md · marketing-overview.md

### Proof

```
pytest tests/test_version_consistency.py → passed
```

### Next

Batch E (optional): `scripts/walkforward_brick_params.py` before flipping reachability/ATR-anchored to live.
