# EV Engine & Calibration Parameter Tuning Playbook

**Purpose:** the process an agent (or human) MUST follow before proposing,
implementing, or promoting a change to any tunable parameter in the EV
engine / signal-qualification / calibration pipeline. This is a standing
process document, not a dated incident report — update it in place as the
parameter inventory or gates change.

**Origin:** codified from the verification steps actually followed during
the 2026-08-22 EV engine & calibration bias audit
(`20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`) and its 4 follow-up fixes —
every fix in that audit went through live-evidence verification, an
isolated/tested implementation, and a full-suite gate before being proposed
for deployment. This doc generalizes that into a repeatable SOP so future
parameter changes (not just that audit's) are held to the same bar.

---

## 1. Why this exists

The system already tried one form of automated parameter correction —
`calibration_enforcement.py`'s live Bayesian-shrink loop — and it produced
a real incident: enforcement adjusted live values based on recent outcomes,
then the bias check measured against its own adjusted values, masking true
model drift for days (see the audit's §2 "compounding" discussion). The
lesson: **any agent-driven tuning of this system must be evidence-gated and
replayable, never a live feedback loop tuning itself from its own recent
output.**

---

## 2. Parameter inventory (as of 2026-08-22)

Every tunable parameter below MUST live in `settings.py` (pydantic
`Settings`) or a documented module-level constant with a `# MUST mirror
settings.X` comment + sync test (the existing convention — see
`eod_correction.OVERCONFIDENT_BIAS` / `settings.EOD_BIAS_ENFORCED_THRESHOLD`
for the pattern). An agent proposing a change to a parameter NOT yet in
this inventory must add it here first.

### Tier A — Symbol-scoped, already autonomous (low blast radius)

| Parameter | Location | Current mechanism |
|---|---|---|
| `brick_size`, `sl_bricks`, `tp_bricks` | `config/brick_params.yaml`, written by `heavy_sweep.py` | Weekly grid search + on-demand `emergency_heavy_sweep`, backtested (Sharpe/win-rate/trade-count), gated by `qualified=True/False` before writing |

A change only affects the one symbol it's computed for. This tier is the
model for everything below — it is the ONLY tier currently allowed to
self-promote without a human gate, and only because it's backtested before
being written.

### Tier B — Global blend/model weights (affects every symbol at once)

| Parameter | Location | Current value |
|---|---|---|
| `P_WIN_WEIGHTS_FULL` (p_pattern/p_regime/p_markov_hold_n/p_timesfm) | `ev_engine.py` module constant | 0.30 / 0.25 / 0.20 / 0.25 |
| `_P_REGIME_CAP` | `ev_engine.py` | 0.97 |
| `TIMESFM_SIGMOID_K` | `orchestrator.py`, env `TIMESFM_SIGMOID_K` | 10.0 |
| `TIMESFM_CLAMP_MIN` / `_MAX` | `orchestrator.py`, env | 0.05 / 0.95 |
| `MARKOV_EMA_ALPHA` | `settings.py` | 0.30 |

A misweighted blend is exactly what caused the overconfidence incidents
this audit fixed (regime cap, Markov absorbing states, TimesFM sigmoid k).

### Tier C — Gate thresholds (safety margins, not performance)

| Parameter | Location | Current value |
|---|---|---|
| `MIN_REGIME_CONF` | `orchestrator.py`, env | 0.60 |
| `MIN_KELLY` | `orchestrator.py`, env | 0.02 |
| `MIN_TIMESFM_CONFIDENCE` | `orchestrator.py`, env | 0.05 |
| `MIN_EV` | `orchestrator.py`, env | 0.0 |
| `MIN_RR` | `orchestrator.py`, env (+ separately in `heavy_sweep.py`) | 1.5 (live) / 2.0 (default) |
| `MIN_P_WIN` | `orchestrator.py`, env | 0.55 |
| `SIGNAL_COOLDOWN_MINUTES` | `orchestrator.py`, env | 60 |
| `KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU` | `settings.py` | True |

Loosening these is how signal volume gets "fixed" — and how false signals
get let through. The `MIN_RR` split between the orchestrator and
`heavy_sweep.py` (documented in `20260817_RECALIBRATION_PERF_ASSESSMENT.md`)
is exactly the kind of drift this playbook's inventory step is meant to
prevent.

### Tier D — Calibration/enforcement mechanics

| Parameter | Location | Current value |
|---|---|---|
| `EOD_BIAS_WINDOW_DAYS` / `EOD_BIAS_MIN_RECORDS` | `settings.py` | 7 / 20 |
| `EOD_BIAS_ENFORCED_THRESHOLD` | `settings.py` (mirrors `eod_correction.OVERCONFIDENT_BIAS`) | 0.10 |
| `EOD_BIAS_RAW_OVERCONFIDENT` / `_UNDERCONFIDENT` | `settings.py` | 0.30 / -0.20 |
| `EOD_DRIFT_THRESHOLD` / `_WINDOW_HOURS` / `_MIN_SAMPLE` | `settings.py` | 0.15 / 48 / 20 |
| `BAYES_PRIOR_COUNT` / `_PWIN` | `eod_correction.py`, `calibration_enforcement.py` (mirrored) | 10 / 0.5 |
| `KELLY_REDUCTION` | `eod_correction.py`, `calibration_enforcement.py` (mirrored) | 0.5 |
| `EOD_ACTION_FRESH_HOURS` | `settings.py` | 8 |
| `EOD_CORRECTION_INTERVAL_HOURS` | `settings.py` | 4 |
| `STAGNATION_EPSILON` / `_TIER1_CYCLES` / `_TIER2_CYCLES` / `_RESET_CYCLES` / `_KELLY_REDUCTION_TIER1` | `settings.py` | 0.02 / 2 / 4 / 8 / 0.25 |

These govern the self-correction loop itself. They are the params most
likely to need tuning as live behavior is observed (e.g. after the stagnation
breaker runs for a few weeks), and they're the tier where an agent has the
most latitude to self-promote IF the gates in §3 are followed — because
they're inherently reversible (settings values, not weights baked into a
trained model) and most already ship with an escape hatch (see §3.7).

---

## 3. Gates — required before ANY parameter change ships

Every gate below was actually followed for each of the 4 fixes in the
2026-08-22 audit. None are optional; skipping one is how the compounding-bias
incident happened in the first place.

### 3.1 Baseline capture
Record the CURRENT live values of whatever metric the change is meant to
move (per-symbol calibration bias, win rate, Sharpe, kelly_f distribution)
**before** touching code. Use live Supabase queries (`nt_eod_actions`,
`nt_calibration_records`, `nt_sweep_result`), not assumptions.

### 3.2 Live-evidence root cause
No parameter change ships on theory alone. Confirm the mechanism with live
data — SSH into the Studio and read `orchestrator.log`, query Supabase
directly, or add a temporary diagnostic log line (as fix #3a's
`efd6189` commit did) and wait for real ticks if the mechanism is still
ambiguous. "The audit worklog says X" is not sufficient if X hasn't been
re-verified against current live state — worklogs describe a point in
time, not a permanent truth (mirrors the "before recommending from memory"
discipline this session already applies).

### 3.3 Isolated, pure, unit-tested implementation
Extract the tuning logic into a pure function wherever possible (e.g.
`_apply_regime_kelly_floor()`, `_stagnation_streak()`/`_stagnation_tier()`)
rather than leaving it inline in a 2000-line orchestrator method. A pure
function is unit-testable without mocking half the Supabase/Redis/HMM
stack, and its tests double as executable documentation of the intended
behavior at the boundaries (threshold-exactly-at, threshold-just-below,
escape-hatch-disables-it, etc.).

### 3.4 Backtest/replay validation
Before promoting a Tier B or C change, evaluate it against historical
ground truth (`nt_calibration_records`, `nt_signal_sim`) or the existing
backtest harness (`heavy_sweep.py`'s pattern), not just unit tests of the
formula in isolation. Define the objective BEFORE running the replay:
- Calibration bias magnitude (raw AND enforced) must not regress.
- Realized Sharpe / win rate must not regress beyond noise.
- No new symbol should flip into a chronic degeneracy/stagnation state.

### 3.5 Statistical significance
Don't promote on small samples. Mirror the codebase's own convention —
`EOD_BIAS_MIN_RECORDS=20` — as the floor for "this parameter change's
effect is real, not noise." For Tier B/C changes affecting all symbols,
require the validation window to cover multiple regimes if at all
possible (a change validated only during one calm week is not validated).

### 3.6 Full test-suite gate
`pytest tests/ -q` must be 100% green (0 failed) before ANY commit. Every
fix in the 2026-08-22 audit was verified this way, including catching and
fixing 3 test regressions that a change to shared logic (the stagnation
breaker) silently introduced in unrelated test files — the suite is the
tripwire for exactly that class of mistake.

### 3.7 Escape hatch required
Every behavioral parameter change must ship with a way to instantly revert
to the pre-change behavior WITHOUT a code change — a settings flag whose
default restores the old behavior. Examples already in place:
`MARKOV_EMA_ALPHA=1.0` disables smoothing; `KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU=False`
restores the always-floor behavior. An agent proposing a Tier B/C/D change
without an escape hatch has not finished the change.

### 3.8 Human sign-off requirement by tier
| Tier | Who can approve | Cadence |
|---|---|---|
| A (brick params) | Autonomous — already backtested + gated by `heavy_sweep.py` | Weekly / on-demand |
| B (blend weights) | **Human required** — systemic, affects every symbol, hardest to backtest cleanly (the blend interacts non-linearly) | Infrequent, only after live evidence of a specific failure mode (as this audit's fixes were) |
| C (gate thresholds) | **Human required** — these are risk/safety knobs, not performance knobs; loosening them is a decision about how much false-positive risk to accept | Infrequent |
| D (calibration mechanics) | Agent may self-promote IF §3.1–3.7 are all satisfied AND the change is reversible via settings (no code redeploy needed to undo) | Can be more frequent — this tier is designed to be tuned as live behavior is observed |

### 3.9 Deploy vs. code-complete are different states
Pushing a commit to `origin/main` is NOT deployment. State explicitly, in
both the worklog and to the human, whether a change is (a) code-complete
and tested, (b) pushed, or (c) live on Studio — and never restart the live
Studio process without explicit human go-ahead for anything beyond a
diagnostic-only log line. This was the explicit boundary respected for all
4 fixes in the 2026-08-22 audit: code-complete + pushed, restart withheld
pending instruction.

### 3.10 Post-deployment monitoring window
Define, before deploying, how long to watch and what confirms success —
e.g. "N correction cycles" or "N hours of ticks" — and what an automatic
revert condition looks like (e.g. "if calibration bias for any symbol gets
WORSE by more than X after the change, flag it, don't wait for the next
scheduled review").

---

## 4. Required worklog structure for any parameter-tuning work

Every piece of parameter-tuning work — whether a single fix or a batch —
gets a worklog entry (`worklog/YYYYMMDD_<TOPIC>.md`) with this structure
(the format actually used in `20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`):

1. **Header** — scope, type (assessment / fix / both), date, status line
   that states plainly whether anything has been deployed yet.
2. **Plain-language summary** — what's broken/being tuned, in terms a
   non-engineer stakeholder can follow, before any code detail.
3. **Live evidence** — actual log lines, actual Supabase query results,
   actual timestamps. Not hypothetical examples.
4. **Root cause / mechanism** — the specific code path, with file:line
   references, and WHY it produces the observed behavior.
5. **What changed** — code diff summary + commit hash, referencing the
   pure function/test added (§3.3).
6. **Verification trail** — test suite result (exact pass/fail count),
   backtest/replay result if applicable, live re-verification if applicable.
7. **Status / candidate-fix table** — for multi-part work, one row per
   fix: scope, status (not started / implemented / deployed), commit,
   recommendation or rationale.
8. **Open questions** — anything requiring human sign-off per §3.8,
   stated as an explicit question, not buried in prose.

---

## 5. What an agent should NOT do

- Do not tune a parameter based on a single symbol's recent behavior and
  generalize it to all symbols (Tier B/C) without the replay validation
  in §3.4.
- Do not build a live loop that adjusts a Tier B/C parameter from the
  system's own recent output — that is the exact shape of the incident
  this playbook exists to prevent. Tier D's stagnation breaker is the
  closest thing to an exception, and it's deliberately narrow (kelly
  reduction / kill switch, not weight/threshold changes) and self-limiting
  (auto-recovery at `STAGNATION_RESET_CYCLES`).
- Do not restart the live Studio process to deploy a behavioral change
  without explicit human confirmation (§3.9).
- Do not promote a change without a green full test suite (§3.6).
- Do not ship a parameter change without an escape hatch (§3.7).
