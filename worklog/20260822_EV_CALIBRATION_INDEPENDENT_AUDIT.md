# Independent Audit — EV Engine & Calibration Bias (2026-08-22)

**Scope:** `noble-trader-fastapi-backend` — EV engine math, signal-determination gates,
calibration bias pipeline.
**Type:** Read-only audit (code review + live Supabase queries + live SSH log/data
inspection on the `noble-trader-unified` Lightning Studio). **No code changed.**
**Status:** Findings for review — awaiting instructions before any fix is built.

---

## Plain-language summary

The EV engine's math is correct, and the team's fixes from this week (regime
confidence cap, Markov absorbing-state handling, TimesFM sigmoid/clamp tuning) are
confirmed live in the code and covered by a green test suite (446 passed, 0 failed).

Four things are worth your attention, in order of how much they affect trust in the
system:

1. **AAPL and US500 are stuck in a live, ongoing overconfidence loop** — not a
   theory, confirmed right now via SSH into the Studio and direct Supabase queries.
   Root cause identified: a "Kelly floor" override force-feeds a non-zero position
   size even when the statistically measured edge is negative, and — independently —
   the Markov component of the EV blend is flapping between "almost certainly up"
   and "almost certainly down" every 5 minutes while the market itself hasn't moved.
   Both feed a real, correctly-detected, but self-perpetuating overconfidence signal.
2. **Four different, disconnected definitions of "calibrated"** exist in the
   codebase (0.05 / 0.10 / 0.15 / 0.30 bias thresholds), so a dashboard built on the
   public `/calibration` API will disagree with what the live enforcement loop is
   actually doing.
3. **A bookkeeping bug**: after calibration enforcement shrinks `p_win`, the EV
   engine's derived fields (`ev`, `ev_per_dollar`, `rr`, `aggression`, `ev_scale`)
   are never recomputed from the corrected value — they stay stale, corrupting
   telemetry and the execution-style decision.
4. **The regime-based Kelly floor** (found while investigating #1) is a deliberate
   design choice, but it means a symbol can be sized and published even when the
   raw historical edge (`mu`) is negative — worth a deliberate decision on whether
   that's the intended behavior.

---

## 1. EV Engine — correctness assessment

**Verified sound:** the 4-source logit-pooled blend (`ev_engine.py`), the T^N
Markov hold-probability approximation, and the RR/EV/Kelly-scale formulas are all
arithmetically correct and match their documentation.

**Verified live (not just "documented as fixed"):**
- `_P_REGIME_CAP = 0.97` is present in `compute_p_regime()` — prevents
  `logit(1.0)=34.5` from dominating the blend.
- Degenerate Markov rows return `0.0` (absorbing) for the current state's own row,
  not the old neutral `0.5` fallback.
- `TIMESFM_SIGMOID_K=10.0` and `TIMESFM_CLAMP=[0.05, 0.95]` are the live Studio
  values (confirmed in `orchestrator.log` at multiple recent startups).
- Full test suite: **446 passed, 23 skipped, 0 failed** (`pytest tests/`).

**Known, accepted limitation:** `p_pattern` is always `None` server-side (the
backend has no access to the agent's DuckDB pattern stats), so the "4-source"
blend is really a 3-source blend in production; the 0.30 weight is always
renormalized away. This is documented as intentional in the code — flagging only
so it's an explicit, not implicit, characteristic of the live system.

### New finding — stale derived fields after calibration enforcement

`orchestrator.py` (~line 1773–1794): calibration enforcement
(`apply_calibration_adjustment`) shrinks `p_win` and mutates it **in place** on the
`EVFullResult` object:

```python
p_win_raw = ev_result.p_win          # captured pre-enforcement (correct)
...
if calib_adj and calib_adj.get("applied"):
    ev_result.p_win = calib_adj["p_win"]      # mutated
    effective_kelly = calib_adj["kelly_f"]    # correctly overridden separately
```

But `ev_result.ev`, `.ev_per_dollar`, `.rr`, `.allowed`, `.ev_scale`, and
`.aggression` are **not recomputed** — they remain whatever they were computed as
from the pre-shrink `p_win`. Confirmed via grep that all of these stale fields are
used downstream:
- `aggression` (execution style: passive/mid/aggressive order placement) — persisted
  to the agent heartbeat payload and `nt_signal_sim` unchanged.
- `ev`, `ev_per_dollar`, `ev_scale` — persisted to Redis heartbeat, agent payload,
  `nt_signal_sim.ev` — all reflect the overconfident pre-correction value.
- `gate_results["ev_allowed"]` — evaluated from the stale (higher) EV, not the
  corrected one.

**Practical severity today:** low-to-moderate for the qualify/reject decision itself
— `rr` doesn't depend on `p_win` at all (it's `tp_bricks/sl_bricks`), `MIN_EV=0.0`
is lax enough that `ev_allowed` rarely flips on this alone, and the gates that
actually matter (`p_win_ok`, `kelly_min_ok`) do use the corrected values. But it's a
real correctness defect in the object's invariants, and it does affect the
`aggression` execution-style decision and all EV-based telemetry/reporting.

---

## 2. Calibration bias — does the process work?

**Architecture is sound and mostly working.** The two-track design — raw-bias
(`predicted_p_win_raw`, wide band 0.30/−0.20, every 4h, clears stale actions at the
cycle boundary) vs. enforced-bias (`predicted_p_win`, tight band ±0.10, drift
monitor every 5 min + nightly pass) — is a real fix for the compounding-penalty
bug diagnosed earlier this week. I verified in the current code (not just the
worklog's proposal) that:
- The unconditional `_clear_stale_eod_actions(symbol)` call now runs for **every**
  symbol at the top of each 4h raw-bias pass (`eod_correction.py` L247-248),
  fixing the "5 low-volume symbols never get cleared" bug that the
  `20260822_STALE_ACTION_CLEARING_SCOPE.md` plan proposed.
- `fetch_calibration_bias_raw` correctly separates `avg_pred` (enforced, for
  stamping) from `avg_pred_raw` (raw, for the actual bias calc) and
  `eod_correction.py` uses the raw average for the Bayesian shrink input in
  raw-bias mode — no compounding.

**Finding A — four disconnected bias-threshold definitions:**

| Consumer | Reads | Window | Threshold |
|---|---|---|---|
| `/calibration/status`, `/calibration/bias/{symbol}` (routers/calibration.py) | `predicted_p_win` | **all-time, unwindowed** | ±0.05 |
| Nightly EOD pass (`eod_correction._classify_bias`) | `predicted_p_win` (enforced) | 7-day rolling | ±0.10 |
| Drift monitor (`drift_monitor.py`) | `predicted_p_win` (enforced) | configurable (default 48h) | 0.15 |
| 4h intraday pass (`eod_correction._classify_bias_raw`) | `predicted_p_win_raw` | 7-day rolling | +0.30 / −0.20 |

The public API's own internal helper (`fetch_calibration_bias`'s returned `status`
field) even computes a *fifth*, unused classification (±0.05) that nothing reads —
`eod_correction.py` ignores it and reclassifies independently. If any dashboard is
built on `/calibration/status`, it will show a different verdict than what the live
enforcement loop is actually acting on, because it's reading an entirely different
(unwindowed, all-time) dataset with a different threshold. **There is currently no
single source of truth for "is symbol X calibrated."**

**Finding B — live, confirmed, unresolved overconfidence loop (AAPL, US500):**

Queried `nt_eod_actions` directly — AAPL (raw bias **0.4447**) and US500 (raw bias
**0.6771**) have been flagged `OVERCONFIDENT` at bit-for-bit identical bias values
across every 4h correction pass from at least 04:30 UTC through 20:31 UTC today
(6+ consecutive cycles), each time re-firing `emergency_heavy_sweep` +
`bayesian_penalty` + `kelly_reduction` without resolving.

I SSH'd into the live `noble-trader-unified` Studio (`lightning ssh configure`,
already authorized) and cross-referenced `orchestrator.log` against
`nt_sweep_result` to find the actual mechanism — this goes beyond what the prior
worklogs identified:

**Mechanism 1 — Markov T^N flapping.** For AAPL, `regime` (`low_vol_bull`),
`regime_conf` (`0.9967`), and `p_timesfm` (`0.5033`) are **frozen bit-for-bit
identical** across 50+ minutes of consecutive 5-minute ticks (22:05–22:55 UTC).
Meanwhile `markov_p_up`/`markov_p_dn` swing wildly tick-to-tick between
`{0.9286, 0}` and `{0, 0.0064}` — i.e. the discretized Markov state classification
flips between "almost certainly UP" and "almost certainly DOWN/FLAT" every 5
minutes in a market that (per the frozen regime/TimesFM inputs) isn't actually
moving. This single component swings `p_win` between **0.4739** and **0.8526** on
alternating ticks:

```
22:45  markov_p_up=0     markov_p_dn=0.0064  → p_win=0.4739
22:50  markov_p_up=0.9286 markov_p_dn=0       → p_win=0.8526
22:55  markov_p_up=0.9286 markov_p_dn=0       → p_win=0.8526
```

This is a residual instability in the 5→3 state HMM collapse
(`STATE_MAP_THRESHOLD_STDS=0.25` in `model.py`) even after the Aug 18 absorbing-state
fix — in genuinely calm/low-volatility markets, small noise in log-returns crosses
the narrow state-mapping boundary constantly, so the *non-degenerate* Markov
probability itself oscillates between near-1 and near-0 rather than settling.

**Mechanism 2 — regime-based Kelly floor overrides a negative raw edge.** Live log
line for AAPL:
```
AAPL: kelly math — mu=-0.00007129, sigma2=0.00006900, mu/sigma2=-1.0331,
      full=0.0, frac=0.0, vol_scaled=0.0, regime_gated=0.0, recommended=0.0
AAPL: Kelly was 0 — applied regime floor 0.0397 (conf=0.997, regime=low_vol_bull)
```
`orchestrator.py` ~L1569-1580: when raw Kelly comes back `0.0` (which it does here
because the measured mean hourly return `mu` is *negative*), a "regime floor"
forces a non-zero Kelly fraction anyway, scaled only by regime confidence — not by
whether the edge is actually positive:
```python
if kelly_f == 0.0 and regime_label not in ("unknown", "market_closed", "neutral"):
    _floor = 0.04 * max(0.0, min(1.0, (regime_conf - 0.5) * 2.0))
    kelly_f = round(min(_floor, 0.06), 6)
```
This is a deliberate, documented design choice ("hourly returns are noisy; mean can
be ≤0 even in a strong bull regime, driving raw Kelly to 0"), not a bug — but for
AAPL specifically, it means a symbol with a **measured negative historical edge**
still gets force-sized and force-published (subject to the other gates), which is
one of the two inputs feeding the persistent overconfidence signal that the
calibration loop then correctly — but powerlessly — keeps flagging.

**Why the correction loop can't fix it:** `emergency_heavy_sweep` re-optimizes
brick/SL/TP parameters, but neither mechanism above is a parameter-tuning problem —
one is state-classification noise in the HMM, the other is a sizing-floor override
decoupled from measured edge. Re-running the sweep grid search cannot change either,
which is exactly why AAPL/US500's raw bias has not moved in at least 18 hours across
6 correction cycles.

---

## 3. Verification trail (for reproducibility)

- `pytest tests/ -q` → 446 passed, 23 skipped, 0 failed.
- Live Supabase (`nt_eod_actions`, `nt_calibration_records`, `nt_signal_sim`,
  `nt_sweep_result`) queried directly via REST, 2026-08-22 ~22:45–22:56 UTC.
- Live SSH session to `noble-trader-unified` Studio (`ssh.lightning.ai`, key
  regenerated via `lightning ssh configure --overwrite`), read-only `grep`/`tail`
  against `/teamspace/studios/this_studio/noble-trader/logs/orchestrator.log`.
- No writes were made to Supabase or the Studio; no code was changed.

---

## 4. Candidate fixes (not yet built — awaiting your instructions)

| # | Fix | Scope | Notes |
|---|---|---|---|
| 1 | Recompute `ev`/`ev_per_dollar`/`rr`/`allowed`/`ev_scale`/`aggression` after calibration enforcement mutates `p_win` | `orchestrator.py`, ~10 lines | Low risk — makes the object internally consistent; may change `aggression` for symbols under active enforcement |
| 2 | Unify the 4 bias-threshold definitions (or explicitly document why they differ and point `/calibration/*` at the same windowed data the enforcement loop uses) | `routers/calibration.py`, possibly `supabase_client.py` | Medium — decide whether the API should mirror enforcement or serve a genuinely different (all-time) question |
| 3a | Dampen Markov state-flapping (e.g. smooth/hysteresis on the 5→3 state assignment, or widen `STATE_MAP_THRESHOLD_STDS`) | `model.py` `_aggregate_transition` | Needs care — this is the same mechanism the Aug 18 fixes touched; changing the threshold affects all symbols, not just AAPL/US500 |
| 3b | Decide whether the regime-Kelly-floor should require `mu >= 0` (or some minimum), rather than firing whenever raw Kelly is exactly 0 | `orchestrator.py` ~L1569 | Behavioral decision, not just a bug fix — changes how often "no historical edge" symbols still get sized |

No action taken on any of these — flagging for your review per your instruction.
