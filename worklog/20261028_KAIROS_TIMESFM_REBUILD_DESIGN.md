# TimesFM rebuild — the vol-lane arm (design + cadence alignment, zero-migration slot)

**Date:** 2026-10-28
**Type:** Design (pre-implementation scope doc + measured-evidence grounding + verified cadence alignment).
**Grounding:** TSFM Probabilistic Forecast Lab verdicts (rolling-origin, DM-tested) + this repo @ `a940086` (claims Phases A–E landed; migrations 218/219/220 applied).
**Contract:** same five clauses as the claims layer — additive, shadow-first, flag-gated; **no geometry, EV, gate, or resolver changes**; one resolver, one bar window; price space at the boundary; immutable claims; MT5 untouched.

---

## 0. Why a rebuild (the measured motivation)

### 0.1 What the current TimesFM consumption actually is

Verified end-to-end at HEAD:

- `server.py` (:8002, launched first by `background.py`, 180 s ready-wait, gateway via `proxy.py`) hosts
  `google/timesfm-2.5-200m-pytorch`, `ForecastConfig(max_context=1024, max_horizon=256, normalize_inputs=True,
  use_continuous_quantile_head=True, fix_quantile_crossing=True)` — and `POST /forecast`
  (`server.py:270-293`) returns **both** `point_forecast` and the full `quantile_forecast`.
- The orchestrator consumes it as: last 500 bars, **horizon = 1**, `point_forecast[0][0]` → `rel_change`
  → z-map (`timesfm_map_mode: zmap_live`) → **`p_timesfm` = P(next bar up)**
  (`orchestrator.py:473-796`; 10 s HTTP timeout; 15-min in-memory cache `_TIMESFM_CACHE_TTL_S = 900`).
- Downstream: EV direction blend weight 5/14 raw in `P_WIN_WEIGHTS_FULL` (`ev_engine.py:109-126`) —
  effectively ~50 % of the live blend when Markov is withheld — plus a soft confidence gate.
  The **quantile output is discarded** (q10/q50/q90 kept as research stamps only, `orchestrator.py:448-452`).

So the model's one structural advantage on financial series — a coherent joint **distributional** forecast —
is thrown away, and the one number kept is aimed at the one question the lab measured as unanswerable.

### 0.2 What the lab measured (independent harness, 90 origins, DM-tested)

| Lane | Verdict | Numbers |
|---|---|---|
| Direction (P(next bar up)) | no skill | Brier 0.2686 vs coin 0.2497; live probes pinned `p_timesfm` 0.496–0.501 |
| Price fan H ≤ 72h | calibrated, unskilled | CRPS ratio ≈ 1.07 vs RW; coverage ≈ nominal |
| **Vol 6–72h** | **the one live lane** | conditioned TSFM-class beats Log-HAR significantly on 5/15 (symbol, horizon) cells: BTCUSD 6h 0.727 (p 0.0001), 24h 0.828 (p 0.0023), USDCHF 6h, USDJPY 24h/72h |
| GARCH family (now the claims vol_fan arm) | EGARCH-t best-in-family | skillRatio 0.944 vs Log-HAR (p 0.085); TSFM-class beats best GARCH arm 11/15 in the lab |

The honest caveat carried through this whole doc: the lab's PASS cells belong to the **covariate-conditioned
lab arm on log-RV targets**. The sidecar service (zero-shot, price-level inputs, no covariates) is a
*different, unproven* arm. This rebuild does not promise it wins — it gives it the one arena where the
question is answered by measurement instead of blend weights: the claims ledger, scored by the same
resolver, against the EGARCH-t default and the `atr_normal` control.

### 0.3 The decision

Rebuild TimesFM as a **vol-lane arm inside the claims machinery** (`kind='vol_fan'`,
`source='tsfm_sidecar'` — a source value 218's CHECK already whitelisted, anticipating exactly this).
The direction path (`p_timesfm`) is **not touched** by this rebuild's default state; its decommission is a
separate, measurement-gated decision (§7, T4).

---

## 1. What the rebuild is

One sentence: **the TimesFM sidecar becomes arm 11's vol_fan arm — its native quantile spread supplies the
variance path of the existing price fan, wrapped in the existing MZ+k recalibration, registered at
qualification, scored by the unchanged resolver, ranked by the unchanged leaderboard.**

What it is NOT:

- not a new fan shape — the fan stays `q_X(h) = entry · exp(z_X · σ_raw(h) · m(h))` on the fixed
  q05..q95 grid (`claims_vol_fan.py:346-441`); TimesFM contributes **only the variance path**;
- not a direction change — `p_timesfm`, the z-map, and the EV blend are untouched (T4 is a separate
  flag-flip decision with its own evidence gate);
- not a migration — 218's source CHECK already allows `'tsfm_sidecar'`; 220's `arm` column is CHECK-free;
- not a service change — `server.py`, `proxy.py`, `background.py` startup order are reused as-is.

---

## 2. The variance-path derivation (the one real math)

The GARCH arms' seam is `variance_path(prices, horizon) -> np.ndarray | None` — per-bar conditional
variance of hourly log-returns in original units, length ≥ 48, deterministic for a frozen window
(`vol_backtest/arms.py:329`). The fan then does `cum_var = cumsum(var_path[:48])`,
`σ_raw(h) = √cum_var[h-1]`.

The TimesFM derivation works backwards from the model's own term structure:

1. **Input**: last **512** hourly closes (log) of the claim window. 512 is the model's design context
   (the lab's convention; also ≈ 30 days of 24/5 FX) and fits the server's `max_context=1024` with headroom.
2. **Request**: `POST /forecast {"horizon": 48, "inputs": [...]}` → `quantile_forecast[0][h]` for
   h = 1..48 (layout: mean + q10..q90 or q10..q90 — both handled, the `TimesFM25HTTP` precedent,
   `vol_backtest/model_arms.py:80-185`).
3. **h-step total σ from the model's own spread**:
   `σ_total(h) = (q90(h) − q10(h)) / (2 · z80)`, `z80 = Φ⁻¹(0.90) = 1.2816`
   (`rv_from_quantile_spread` precedent, `vol_backtest/arms.py:421-444`). The h-step-ahead predictive
   spread of log-price *is* the cumulative return σ — the model's term structure, unmodified.
4. **Per-bar variance via telescoping**: `var_path[h-1] = σ_total(h)² − σ_total(h-1)²`, so that
   `cumsum` reproduces the model's own `σ_total(h)` exactly at every h. A vol-contraction forecast can
   make an increment negative — those are clamped to a small positive floor and the clamp is counted and
   stamped in the claim meta (honesty over silence). The existing degenerate guards stay:
   per-bar σ ≤ 0.5, terminal σ ≤ 10, scale ∈ (0, 15] (`claims_vol_fan.py:403-425`) — a blown fan is
   rejected whole, never truncated.
5. **Recalibration on top**: the existing `m(h) = k·exp(a)·F^(b-1)` multiplier from
   `claims_vol_fan_recalib.json` cells keyed `(symbol, horizon, arm)` — tsfm cells mint through the
   unchanged pipeline (§5 T2). Missing cells ⇒ identity `m=1`, `calib_level='none'`, as today.
6. **Determinism**: the server config already pins `fix_quantile_crossing=True` and
   `force_flip_invariance=True`; a PyTorch eval-mode forward is deterministic. The EGARCH `seed=7`
   precedent (`arms.py:401`) is the house bar for simulation-based determinism — the HTTP arm must meet
   the same bar: **two calls on one frozen window must produce a byte-equal fan (6 dp)**, verified in
   acceptance (§8). A model-id bump on the service changes fans by construction — it requires an artifact
   re-mint and is recorded via the run provenance, not silently.

**Rejected variant (recorded so it is not re-litigated):** feeding the model's own q05/q50/q95 directly as
the fan knots. It deviates from the entry-anchored, log-symmetric convention the bracket claim's
tp_q/sl_q knot interpolation depends on, forks the fan builder, and forfeits the MZ+k wrapper. The
resolver would still score it (it is kind-agnostic), but the machinery cost and the consistency-contract
tension buy nothing the spread→variance-path route does not already provide.

---

## 3. Cadence alignment — verified against the three cadences

This is the alignment audit requested at design time. All schedules verified at HEAD
(`SweepOrchestrator.start()`, `orchestrator.py:1586-2009`; APScheduler is the sole scheduling authority —
migration 215).

### 3.1 The three cadences as they run today

| Cadence | Schedule (UTC) | Entry | What runs |
|---|---|---|---|
| **Light sweep** | per-symbol `regime_cron`, default `*/15` | `_sweep_group` (`orchestrator.py:3739`) | 200-bar fetch → HMM → debounce → gates → publish; per-symbol lock + 5 s stagger; `misfire_grace_time=120 s`; 20-min stale-recovery backstop |
| **Heavy sweep** | per-symbol `sweep_cron`, DST-safe `6 17 * * *` / `6 18 * * *` (10:06 local, after the Studio restart) | `_heavy_sweep_group` (`heavy_sweep.py:531`) | 2160-bar window (90 d) → brick×SL×TP grid search → **geometry reoptimization written to `brick_params.yaml`** → qualification → publish; `misfire_grace_time=3600 s`; concurrency 4 |
| **Emergency reoptimization** | eod-correction-intraday at **04:30 / 08:30 / 12:30 / 16:30 / 20:30** (every 4 h, `interval_hours: 4`, `orchestrator.py:1683-1699`) | `_run_eod_correction_only` → on flagged bias, `_execute_emergency_heavy_sweeps` (`orchestrator.py:2514-2577`) | bias from raw `predicted_p_win_raw` (band +0.30 / −0.20, `eod_correction.py:63-125`) → `emergency_heavy_sweep` action → **the full heavy path** (grid + YAML rewrite + qualification + claims), 12 h per-symbol cooldown; degeneracy-breaker and geometry-tuning triggers fold into the same dispatch; drift-monitor tripwire between passes |

Two structural facts govern everything below:

- **The TimesFM service is static.** No sweep starts, restarts, refits, or re-tunes it. `background.py`
  launches it first and the orchestrator only starts after the health wait. Emergency reopt reoptimizes
  *geometry and calibration state* — never the forecaster.
- **Claims registration is fire-and-forget by construction.** Both light
  (`orchestrator.py:6811-6897`) and heavy (`heavy_sweep.py:2122-2192`) dispatch `asyncio.create_task`
  after qualification; CPU work rides `asyncio.to_thread`; the insert timeout is 5 s; the light task
  self-fetches its own 2160-bar window so the sweep never waits. The Phase D discipline.

### 3.2 Where the rebuild's TSFM work lands, per cadence

| Cadence | TSFM vol-lane work added | Critical-path cost | Failure mode |
|---|---|---|---|
| 15-min light | **Nothing on the sweep critical path.** One `/forecast` (horizon=48, 512-bar context) inside the fire-and-forget claim task, **only when a signal qualifies** (cooldown 15 min + dedup upstream bound the rate). | 0 s to the sweep; ~0.1–1 s inside the detached task (10 s HTTP timeout convention, `orchestrator.py:596`) | Service down / timeout / degenerate spread ⇒ arm unavailable ⇒ **no vol_fan claim**; the bracket control still registers — fail-open, sweep unaffected |
| Daily heavy | Same registration with the sweep's **own 2160-bar window** (last 512 bars as context — no refetch, same code path). No service restart, no refit. The recalib artifact re-mint is **offline** (CLI on run dirs, §5 T2) — the sweep only *reads* the artifact, mtime-hot-reloaded (30 s recheck). | 0 s to the sweep; one extra sub-second forecast inside the detached task, negligible against the measured heavy median (89.7 s/symbol, Oct 1–16) | Same fail-open; `calib_level='none'` if the artifact lacks tsfm cells — identity calibration, honestly stamped |
| Emergency 4 h | The emergency dispatch **reuses the heavy path verbatim**, so a reoptimized geometry that re-qualifies registers **new** claims with the then-current arm. The 12 h cooldown caps claim cadence per symbol. | Same as heavy | Same as heavy |

### 3.3 The invariants that make alignment structural, not incidental

1. **The sweep never waits.** Registration is off the critical path on both cadences (§3.1 fact 2);
   inference happens inside the detached task with bounded timeouts.
2. **Immutability makes the ledger cadence-proof.** A registered fan can never be touched by any later
   sweep, reopt, or flag flip (`unique(signal_id, kind)` + the immutable-claims clause). Emergency
   reoptimization can only change *future* claims — new geometry, new entry/TP/SL, new fan — never past
   ones. There is no "re-forecast on reopt" path to mis-align.
3. **Fail-open everywhere.** Service unreachable, quantile layout unexpected, negative-variance clamps
   exceeded, absurd spreads: the guards reject the whole fan and the vol_fan claim simply does not
   exist that tick. The bracket control and the live signal path are untouched.
4. **One resolver, one bar window.** Scoring stays in the EOD resolver against the same 48-bar settlement
   window the signal's verdict uses — the rebuild adds zero resolver code.
5. **Determinism per frozen window.** The fan registered is a pure function of (window, model, artifact
   cells) — verified byte-equal on repeat calls (§8). This is what makes clause 2 meaningful.
6. **Claims evidence reaches the emergency machinery only through the sanctioned bridge.** The Phase E
   `claims_prior_mode` (off) modulates the shrink prior count on Wilson-accepted coverage — it is the
   only path by which the ledger may influence live calibration, and it is already built. The emergency
   bias thresholds (`predicted_p_win_raw` bands) are untouched by this rebuild.

### 3.4 What deliberately does NOT happen

- No per-sweep inference is added to any cadence's critical path (the existing horizon-1 direction call
  and its 900 s cache — deliberately aligned to the 15-min cadence by migration 144 — are untouched in
  T1–T3).
- No service restarts, refits, or model swaps on any cadence.
- No automatic recalib re-mint inside any sweep — the artifact is minted offline and hot-reloaded.
- No direct coupling from the leaderboard into emergency triggers — evidence flows only through the
  flag-gated bridge.
- No per-symbol arm selectivity in v1 — `claims_vol_fan_arm` is one global choice; the flip switches the
  single vol_fan slot for all symbols, and every row stamps `arm`, so the switch is auditable. Per-symbol
  routing is a documented future extension, not a v1 promise.

---

## 4. The slot — zero migration, checklist form

Verified against the applied migrations:

- `218` L47-48: `kind in ('bracket','vol_fan','thesis')`, `source in ('tsfm_sidecar',
  'vol_backtest_recalib','manual','atr_normal')` — **`tsfm_sidecar` is already legal**.
- `220`: `arm`, `calib_level` nullable, **no CHECK** — `arm='tsfm'` needs nothing.
- `unique(signal_id, kind)` — one vol_fan slot per signal: the arm flag *selects* the occupant; it never
  adds a third sibling. The leaderboard's paired DM (bracket vs vol_fan) keeps its meaning.

To land the arm (implementation checklist, no resolver/leaderboard/migration changes):

1. `TimesFMVolFan` arm: reuse the `TimesFM25HTTP` plumbing (health probe, X-API-Key, quantile-layout
   handling, `vol_backtest/model_arms.py:80-185`) and add the `variance_path` seam per §2.
   Unavailable-service probe ⇒ `available=False` ⇒ no claim.
2. `claims_vol_fan.py`: extend the arm name map + the lazy whitelist (L113/L137); key the row's
   `source` off the arm (→ `'tsfm_sidecar'`, replacing the hardcoded `'vol_backtest_recalib'`, L569).
3. `feature_flags.py` + `config/feature_flags.yaml`: add `'tsfm'` to `get_claims_vol_fan_arm()`
   (L1399-1407) and its `_DEFAULTS`/yaml comment blocks. **Default stays `egarch`** — the flip is the
   pre-registration.
4. Call sites: **zero changes** — both light and heavy pass the same signature; arm selection is
   flag-driven inside.
5. Leaderboard/lab table: nothing required. `arm='tsfm'` appears in the arms histogram automatically;
   lab reference cells are appended additively when minted.

---

## 5. Phased delivery (flag-gated, shadow-first)

- **T1 — the arm seam.** `variance_path` via the sidecar + flags + offline round-trip verification with
  stubbed httpx (the Phase A/D pattern). `claims_vol_fan_mode` stays `off`; arm default stays `egarch`.
  No migration. Nothing live changes.
- **T2 — the recalib mint.** Offline research runs:
  `run_vol_backtest.py --arms garch,gjr,egarch,tsfm --horizons 6,12,24,48` on the 5 lab symbols →
  `python -m vol_backtest.recalib` → artifact refresh (recalib groups by whatever arm names appear in
  records — no whitelist). tsfm cells stamp `calib_level='cell'`; symbols without cells run identity.
- **T3 — the evidence phase.** Flip `claims_vol_fan_arm: "tsfm"` (one global, auditable switch) *after*
  the GARCH-era baseline has accumulated on the leaderboard — evidence, not calendar. The paired DM
  (bracket control vs vol_fan) and the pooled-coverage cells do the grading; the lab reference table gets
  tsfm cells appended as they are measured.
- **T4 — the direction decision (separate scope, NOT part of this rebuild).** Run the `p_timesfm` skill
  audit first (the `regime_edge` `acc − majority` pattern — the 20-line measurement the STRIDE review
  demanded before anyone argues blend weights again). Skill ≤ 0 ⇒ flip `timesfm_blend_mode: "shadow"`
  (the yaml knob already exists; heavy already withholds it in shadow) and cut the unmeasured ~50 %
  blend weight. Skill > 0 ⇒ it has earned it. Either way the number exists first. This is a live-path
  change and stays behind its own operator decision.

---

## 6. Non-goals (explicit refusals)

- No change to geometry, EV, gates, p_win semantics, calibration loops, MT5 payloads.
- No native-quantile fan (§2 rejected variant).
- No per-sweep horizon-1 spam, no new caches on the sweep critical path.
- No service lifecycle changes (start order, health waits, rate limits all reused).
- No per-symbol arm routing in v1.
- No direct claims→emergency coupling beyond the existing `claims_prior_mode` bridge.

---

## 7. Acceptance tests (pre-flip, offline)

1. **Determinism**: N ≥ 3 repeated `/forecast` calls on one frozen 512-bar window → byte-equal fan rows
   at 6 dp; differing ⇒ arm rejected.
2. **Variance-path math**: telescoping identity `cumsum(var_path) == σ_total²(h)` exact on synthetic
   monotone spreads; negative-increment clamp counted + stamped; guards fire on absurd spreads
   (per-bar σ > 0.5, terminal σ > 10, scale > 15 ⇒ whole-fan reject).
3. **Monotone/log-symmetric fan**: q05 < q10 < … < q95; `q_X · q_(100−X) ≈ entry²`; q50 = entry.
4. **Monte-Carlo calibration** (the Phase D bar): draws from the fan's own law → empirical coverage
   0.50/0.80/0.90 vs nominal within tolerance.
5. **Offline round-trip with stubbed httpx**: flag `off` ⇒ zero service calls; `mode=shadow` +
   `arm=tsfm` ⇒ exactly one POST with `kind='vol_fan'`, `source='tsfm_sidecar'`, `arm='tsfm'`,
   `calib_level` from the artifact, 48-row fan, deadline `+48 h`; service 503/timeout ⇒ no claim,
   bracket claim unaffected; publish path unchanged.
6. **Real-artifact cell math**: `m = k·exp(a)·F^(b-1)` reproduced to 3 dp on a minted tsfm cell.
7. **Cadence probes** (the alignment audit, executable): light-sweep timing unchanged with the claim
   task stubbed slow (the sweep never awaits it); heavy path passes its own window (no refetch);
   emergency dispatch produces new-claim registration only — a pre-registered row's fan is bit-identical
   before and after a simulated emergency heavy sweep.
8. **Resolver neutrality**: a hand-built tsfm fan row scores identically to a GARCH fan row with the
   same numbers (kind-agnostic by construction — regression-guard it anyway).

---

## 8. Operator actions

- **None required to land T1.** No migration (`tsfm_sidecar` already legal in 218; `arm` CHECK-free in
  220). Ship-dark: `claims_vol_fan_mode: off`, `claims_vol_fan_arm: egarch`.
- Pending from Phase D, unchanged: next deploy installs `arch` (requirements activated) → flip
  `claims_vol_fan_mode: "shadow"` to complete the D deployment and start the GARCH-era baseline clock.
- T3 flip (`claims_vol_fan_arm: "tsfm"`) only after T2's artifact exists **and** the leaderboard shows
  the GARCH-era baseline — evidence, not calendar.
- T4 is its own decision with its own measurement gate (§5).

---

## 9. Honest limitations

- The sidecar arm is zero-shot on price levels — **not** the lab arm that passed 5/15 cells (that one is
  covariate-conditioned on log-RV). It may lose to EGARCH-t. The point of the rebuild is that the
  leaderboard will *say so*, per symbol and horizon, with p-values — and the escalation path
  (covariate conditioning, then vol-lane fine-tuning, the one configuration with a documented case,
  Fu et al. 2024) is exactly the T3+ fork this machinery is built to gate.
- The k-transfer (RV-scale multiplier applied to a price-space spread) is the same hypothesis Phase D
  registered — the ledger measures it.
- Negative-increment clamps (§2.4) slightly under-represent forecast vol contractions; the clamp count
  is stamped so the distortion is auditable.
- The 4-hour emergency cadence can re-register claims with *new* geometry inside a 12 h cooldown window
  per symbol — the ledger absorbs this as more claims, never as rewritten ones.
