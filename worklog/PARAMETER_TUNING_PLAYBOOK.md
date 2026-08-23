# EV Engine & Calibration Parameter Tuning Playbook

**Purpose:** the process an agent (or human) MUST follow before proposing,
implementing, or promoting a change to any tunable parameter in the EV
engine / signal-qualification / calibration pipeline. This is a standing
process document, not a dated incident report — update it in place as the
parameter inventory or gates change.

**Origin:** codified from the verification steps actually followed during
the 2026-08-22 EV engine & calibration bias audit
(`20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md`) and its 4 follow-up fixes.

**Revision (2026-08-23):** the first version of this doc gated Tier B/C
changes on human sign-off. That gate is fictional for how this system
actually operates: most tunables live in Studio Secrets, there is no
standing ops review, and — per direct operator confirmation — realistic
human intervention on individual parameter adjustments is ~zero. Safety
in this doc no longer comes from "a human looks at it." It comes from
hard bounds, rate limits, and automatic rollback, enforced in code,
because those are the only gates that actually fire.

---

## 1. Why this exists

The system already tried one form of automated parameter correction —
`calibration_enforcement.py`'s live Bayesian-shrink loop — and it produced
a real incident: enforcement adjusted live values based on recent outcomes,
then the bias check measured against its own adjusted values, masking true
model drift for days (see the audit's §2 "compounding" discussion).

The lesson is NOT "add a human review step" (see Revision note above) — the
system doesn't have one in practice, and pretending otherwise just means the
real gate silently disappears. The lesson is: **any agent-driven tuning
must be bounded, rate-limited, and self-reverting, because no one is
watching it tick-to-tick.**

---

## 2. The registry: config-file-first, DB-fallback, settings.py-as-bounds

### 2.1 Why settings.py can't be the live tuning surface

`settings.py` is a pydantic `Settings()` singleton instantiated once at
process import. Its values on the live Studio come from **Studio
Secrets** — env vars edited through a human-facing UI, which only take
effect after a process restart. That's a real structural blocker for
autonomous tuning, not just an inconvenience: an agent cannot change a
Studio Secret and have it take effect without (a) a human touching the
Studio Secrets UI or an equivalent API, and (b) a restart of the live
trading process. Given zero realistic human intervention, anything gated
on that path is effectively frozen forever at its current value.

### 2.2 The pattern that already works: `config/brick_params.yaml`

`heavy_sweep.py` already tunes brick geometry autonomously, with no human
step and no restart, by writing a YAML file that `_load_brick_params()`
reads fresh on (effectively) every sweep tick. This is the model to
generalize — not settings.py.

### 2.3 Registry design

**Read precedence (per parameter):**

1. **Config file** (`config/tuning_registry.yaml`) — primary. Fast local
   read, no network round-trip, and — this is the important part —
   hot-reloadable via `lru_cache` + an explicit `reload()` call after
   every write (same shape as `brick_params.py`'s
   `_load_yaml_cached()`/`reload()`), so a value written by an autonomous
   tuner is live on the very next read, no restart.
   **Correction (2026-08-23):** an earlier draft of this doc claimed the
   file gives a git-diffable audit trail "for free." Checked against
   `config/brick_params.yaml` — false. It IS git-tracked, but
   `heavy_sweep.py`'s live rewrites on Studio never get committed back;
   the tracked copy is stale (last touched months ago by an unrelated
   commit) while the live one has moved on. `config/tuning_registry.yaml`
   is git-ignored on purpose (see `.gitignore`) — it's a fast local cache
   only. The real audit trail is #2 below.
2. **Supabase table** (`nt_tuning_params`) — fallback + durability. Used
   when the file has no entry for a parameter (cold start, or the file
   was lost in a Studio rebuild), and as the cross-instance sync target.
   Mirror the existing `nt_eod_actions` shape: append-only rows, "latest
   row per `param_name`" is the current value — this gives you the audit
   trail for free, using a pattern the codebase already has, rather than
   inventing an upsert table.
3. **`settings.py`** — factory default only, used when neither file nor
   DB has an entry. This is the ONE place a value is still baked into the
   process, and that's fine because it's the fallback, not the live path.

**Bounds — the actual safety mechanism**, in a new
`regime_platform/core/tuning_registry.py` (not on `Settings`, to avoid
doubling every field into `_MIN`/`_MAX` pairs):

```python
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "MARKOV_EMA_ALPHA": (0.05, 0.80),
    "STAGNATION_TIER1_CYCLES": (1, 6),
    "STAGNATION_TIER2_CYCLES": (2, 12),
    "KELLY_REDUCTION": (0.10, 0.75),
    # ... one entry per tunable, set once, rarely touched
}
```

Every read clamps into `PARAM_BOUNDS` regardless of what's in the file or
DB — defense in depth against a bad value getting written in the first
place. Every write is rate-limited to one bounded step per tuning cycle
(no jumping straight to an extreme) and is followed by an automatic
revert-to-`previous_value` if the tracked metric regresses over the next
N cycles — the same auto-recovery shape the stagnation breaker already
has (§`STAGNATION_RESET_CYCLES`), generalized into the registry itself
rather than reimplemented per parameter.

**Writes go to both, file first:** the tuner writes the file (immediately
live for this Studio instance), then inserts the audit row into
`nt_tuning_params` (durability + cross-instance visibility) — same
two-step shape `heavy_sweep.py` conceptually has (YAML write +
`nt_sweep_result`/`nt_eod_actions` audit rows), just generalized beyond
brick params.

**The one remaining human-facing surface — an emergency kill switch, not
a review gate:** `settings.py: TUNING_AUTONOMY_ENABLED: bool = True`,
checked before any registry write. This is deliberately NOT a per-change
approval step (that gate doesn't fire in practice) — it's a single global
switch a human can flip in Studio Secrets if they ever want to freeze all
autonomous tuning at once, mirroring existing kill switches like
`NOBLE_TRADER_DISABLE_EXECUTION`. Absence of human attention is the
default assumption everywhere else in this design.

### 2.4 Build status (updated 2026-08-23)

**Layer 0 (this section's design) is now built**, on top of
`heavy_sweep.py` + `brick_params.yaml` (Tier A, already running this
pattern before this work started):

| Piece | Status |
|---|---|
| `regime_platform/core/tuning_registry.py` (`PARAM_BOUNDS`, `clamp()`, `read_param()`/`read_param_async()`, `write_param()`, `get_history()`) | ✅ Built, tested (`tests/test_tuning_registry.py`) |
| `nt_tuning_params` Supabase table (migration 117) | ✅ Migration written — **not yet applied to live Supabase** (no DDL execution access from this session; needs the operator or `supabase db push`) |
| `config/tuning_registry.yaml` | Created lazily on first write; git-ignored |
| `settings.TUNING_AUTONOMY_ENABLED` | ✅ Built |
| `fetch_latest_tuning_param` / `fetch_tuning_param_history` / `insert_tuning_param_row` (`supabase_client.py`) | ✅ Built |

**Layer 1 (§2.5.2) is now built** — `regime_platform/sweep_orchestrator/auto_tuner.py`
(`run_auto_tuning()`), scheduled daily at 01:30 UTC on the same in-process
APScheduler instance as `eod_correction` (`orchestrator.py`'s new
`_run_auto_tuning` job, gated on `TUNING_AUTONOMY_ENABLED`). Tested
(`tests/test_auto_tuner.py`, 29 tests). Health metric: mean |raw
calibration bias| across active symbols (lower is better) — one shared
objective for all Tier D pilot parameters rather than a bespoke metric
per parameter, to keep attribution clean (only one parameter is nudged
per cycle, round-robin by recency). Each cycle: evaluate the previous
cycle's pending write (auto-revert on regression), then nudge whichever
pilot parameter has gone longest without a write, by requesting the full
bound extreme in the chosen direction — `write_param()`'s own rate
limiter clips that to one small step, so Layer 1 only decides direction,
never step size.

**IMPORTANT — Layer 1 does not affect live behaviour yet.** It writes
tuned values into the registry (exercising the real bounds/rate-limit/
rollback machinery, building a real audit trail), but no read site has
been migrated to consume them — `eod_correction.py`,
`calibration_enforcement.py`, etc. still read `settings.py`/env vars
directly for these same parameter names. Migrating those read sites is
separate, not-yet-done work; until then, Layer 1 is tuning values nobody
acts on.

**Layer 2 (§2.5.3) is still design-only** — not built.

**Bug found while building this (unrelated to Layer 0 itself, migration
116):** `nt_eod_actions.action` has a `CHECK` constraint (migrations
102/104) listing only 4 action values. Two action types added to the
Python code since then — `degeneracy_breaker` (2026-08-21) and this
session's `stagnation_kill_switch` (fix #3c, commit `3b37e89`) — were
never added to the constraint. Confirmed live via Supabase query
(2026-08-23): **`degeneracy_breaker` has 0 rows ever**, despite the code
path having run for 2+ days — every insert has been silently failing
(best-effort writes swallow the exception). `stagnation_kill_switch` would
hit the same wall. Migration 116 widens the constraint; **it also has not
been applied to live Supabase yet**, for the same DDL-access reason.

---

## 2.5 Layer 1 & Layer 2 — the tuning logic itself (design, not built)

Layer 0 only provides bounded storage. This section is the two-layer
architecture for what actually decides to change a value — matching the
"dream state" framing: a cheap, continuous, autonomous baseline, with an
agent escalation reserved for detected regime shifts, not routine ticks.

### 2.5.1 Scheduling: in-process APScheduler, not Supabase cron

The orchestrator already runs an in-process APScheduler instance
(`orchestrator.py`) scheduling the light sweep (5 min), heavy sweep
(weekly), and `eod_correction.run_eod_correction` (4h). Supabase-side
scheduling (pg_cron / Edge Functions) is a legacy, now-deprecated path
for this system — `EVEngine.compute_tail_risk()`'s docstring already
flags its only caller as "the deprecated Supabase Edge Function path."

**Both Layer 1 and Layer 2's triggers belong on the same in-process
APScheduler instance**, as new scheduled jobs alongside `eod_correction`
— not a new external cron, and not Supabase-side. Reasons: they need the
same Supabase credentials/connection handling the orchestrator process
already holds, and any value they tune needs to be picked up by that
SAME process's next tick — an external process would just be reading the
registry and hoping the live orchestrator notices, for no benefit over
being in-process. `eod_correction.py`'s CLI entrypoint
(`python -m regime_platform.sweep_orchestrator.eod_correction`) is the
model if an out-of-process/manual trigger is ever wanted in addition.

### 2.5.2 Layer 1 — autonomous continuous self-tuning

A scheduled job (own cadence, e.g. daily — slower than the 4h correction
pass, since a Tier D parameter's effect needs time to show up in realized
outcomes) that, per registry parameter:

1. Reads the tracked outcome metric over a rolling window (calibration
   bias, stagnation frequency, false-qualify rate — whatever §3's tier
   table says to watch).
2. Takes ONE bounded step (via `tuning_registry.write_param()`, which
   already enforces `PARAM_BOUNDS` + the rate limit) in whichever
   direction recently correlated with improvement.
3. Relies on `write_param()`'s built-in clamp/rate-limit, plus a rollback
   check on its next run: if the metric regressed since the last step,
   the following write moves back toward `previous_value` instead of
   continuing in the same direction.

Deliberately NOT a black-box optimizer (no RL, no Bayesian search) —
simple coordinate-ascent-with-a-governor, matching this codebase's
existing style (explicit, logged, debuggable) and honest about the
"arbitrary within bounds" reality instead of pretending a fancier
algorithm would make the adjustments less arbitrary.

**Pilot scope:** Tier D only (stagnation-breaker tunables first) — lowest
blast radius, already reversible, already has an analogous auto-recovery
shape (`STAGNATION_RESET_CYCLES`) to generalize from. Tier B/C stay out of
Layer 1 entirely; they're Layer 2's job.

#### 2.5.2.1 Read-site migration scope (E2E — Layer 1 → live behaviour)

As of the Layer 1 build (commit `392d582`), it writes tuned values but
**nothing reads them** — `eod_correction.py`, `calibration_enforcement.py`,
and `drift_monitor.py` still read `settings.py`/hardcoded module constants
directly for these same 11 parameter names. This subsection scopes the
7 read sites (3 files) that need to change for a tuned value to actually
reach live behaviour.

| Parameter(s) | File : function | Current code |
|---|---|---|
| `EOD_BIAS_ENFORCED_THRESHOLD` | `eod_correction.py` : `_classify_bias()` | Compares `bias` against hardcoded `OVERCONFIDENT_BIAS`/`UNDERCONFIDENT_BIAS` — no settings/registry lookup at all today |
| `EOD_BIAS_RAW_OVERCONFIDENT`, `EOD_BIAS_RAW_UNDERCONFIDENT` | `eod_correction.py` : `_classify_bias_raw()` | Already has a lazy `getattr(settings, "EOD_BIAS_RAW_OVERCONFIDENT", DEFAULT_RAW_OVERCONFIDENT)` indirection per side — closest thing to a ready-made hook |
| `EOD_DRIFT_THRESHOLD` | `drift_monitor.py` : `check_drift()` | `threshold = float(getattr(settings, "EOD_DRIFT_THRESHOLD", 0.15) or 0.15)` |
| `STAGNATION_EPSILON`, `_TIER1_CYCLES`, `_TIER2_CYCLES`, `_RESET_CYCLES` | `eod_correction.py` : `_settings_number()` | One shared helper already funnels all four through `getattr(settings, name, default)` — one change covers four parameters |
| `STAGNATION_KELLY_REDUCTION_TIER1` | `eod_correction.py` : Tier-1 escalation block (~L465) | `getattr(_settings, "STAGNATION_KELLY_REDUCTION_TIER1", DEFAULT_STAGNATION_KELLY_REDUCTION_TIER1)` |
| `BAYES_PRIOR_COUNT` | `eod_correction.py` **and** `calibration_enforcement.py` : `bayesian_adjusted_p_win()` (mirrored copy in each file) | `numerator = p * n + BAYES_PRIOR_PWIN * BAYES_PRIOR_COUNT` |
| `KELLY_REDUCTION` | `eod_correction.py` : `run_eod_correction()` **and** `calibration_enforcement.py` : `apply_calibration_adjustment()` (mirrored constants) | `detail["kelly_factor"] = KELLY_REDUCTION` / `... or KELLY_REDUCTION` |

`BAYES_PRIOR_PWIN` (the 0.5 shrink anchor) stays fixed — deliberately not
in `PARAM_BOUNDS`, out of scope.

**The change, mechanically:** every site swaps its `getattr(settings,
name, default)` (or hardcoded constant) for `tuning_registry.read_param(
name, default=<today's value>)` — the sync, file-only variant, since all
seven call sites are themselves synchronous and Layer 1 already writes to
the file first. No new async plumbing needed anywhere.

**Why this is lower-risk than it looks:** the registry file has at most
one entry per day (whichever parameter Layer 1's round-robin touched),
and `read_param()` falls back to `default` when there's no entry — so
wiring all seven sites is a no-op for every parameter Layer 1 hasn't
tuned yet, and a small, already-bounded/rate-limited nudge for whichever
one it has. The risk is concentrated in "did I wire the read correctly,"
not "did I change behaviour."

**Sequencing (mechanical → careful):**
1. `_classify_bias_raw` — trivial, already has the shape.
2. `_settings_number` — one change, four parameters.
3. `drift_monitor.check_drift` — trivial.
4. Tier-1 `STAGNATION_KELLY_REDUCTION_TIER1` — trivial.
5. `_classify_bias` — needs restructuring (no lookup exists today), and
   its threshold is also *displayed* by `routers/calibration.py`'s
   `threshold_enforced` field (fix #2) — that display value should read
   the same way so what's shown matches what's enforced.
6. `bayesian_adjusted_p_win` in **both** files — same registry key, so
   they become identical by construction instead of by a sync test.
7. `KELLY_REDUCTION` default in **both** files — same treatment.

**Testing:** per site, one test asserting no-registry-entry → byte-identical
to today's value (regression guard), one test asserting a seeded registry
entry → that value is actually used — mirrors the escape-hatch tests
already written for fixes #3a/#3b.

### 2.5.3 Layer 2 — agent-triggered tuning on detected regime shift

This is cheaper to build than it sounds because half of it already
exists, unwired:

- `orchestrator.py` already computes and persists `regime_shift` /
  `prev_regime` / `shift_at` on every tick (a simple label-change
  boolean).
- `EVEngine.detect_regime_shift()` already computes shift `direction`,
  `magnitude`, and `confidence` from two regime snapshots — but it is
  called **nowhere** in the live codebase (confirmed via grep,
  2026-08-23). It's a complete, unused method.

**What's missing:**
1. Call `detect_regime_shift()` with real consecutive snapshots (it needs
   `prev`/`curr` dicts with `regime_label` + `trend_probs` — both already
   available in `_process_symbol`).
2. A threshold on `magnitude`/`confidence` that, when crossed, writes an
   alert (new `nt_eod_actions`-style action, e.g. `regime_shift_alert`, or
   a small dedicated table) instead of silently discarding the result
   like today.
3. A Hermes cron job (same pattern as the existing `eod-lookback-monitor`)
   that watches for unactioned alerts and triggers an agent session — the
   same kind of live-evidence-gathering, bounded-fix, test-gated work this
   session did manually, but on a detected trigger instead of a human
   asking.

Tier B/C tuning (blend weights, gate thresholds — the parameters explicitly
excluded from Layer 1) lives here: infrequent by construction (shifts
aren't routine), which is what makes the full gate list in §4 affordable
to run every time, unlike a tick-by-tick loop.

### 2.5.4 Where MIN_RR fits, concretely

`MIN_RR` (currently `1.5` in Studio Secrets, shared by the orchestrator
gate and `heavy_sweep.py`'s grid search since the 2026-08-17 fix) is Tier
C — a risk-acceptance knob, not a performance knob. Per §3's tier table
it does NOT belong in Layer 1's continuous loop. It's exactly the kind of
value that should only move via Layer 2, triggered by live evidence of a
specific failure mode (as the 1.5 vs. 2.0 change itself was, per
`20260817_RECALIBRATION_PERF_ASSESSMENT.md`), not a routine daily nudge.

---

## 3. Parameter inventory (as of 2026-08-23)

Every tunable parameter below should eventually move to the registry in
§2 (file + DB, bounded) rather than living only in `settings.py`/Studio
Secrets. An agent proposing a change to a parameter NOT yet in this
inventory must add it here first.

### Tier A — Symbol-scoped, already on the target pattern

| Parameter | Location | Current mechanism |
|---|---|---|
| `brick_size`, `sl_bricks`, `tp_bricks` | `config/brick_params.yaml`, written by `heavy_sweep.py` | Weekly grid search + on-demand `emergency_heavy_sweep`, backtested (Sharpe/win-rate/trade-count), gated by `qualified=True/False` before writing |

The model for everything below: file-based, hot-reloaded, no human step,
no restart, and already bounded implicitly by the backtest gate.

### Tier B — Global blend/model weights (affects every symbol at once)

| Parameter | Location today | Current value |
|---|---|---|
| `P_WIN_WEIGHTS_FULL` (p_pattern/p_regime/p_markov_hold_n/p_timesfm) | `ev_engine.py` module constant | 0.30 / 0.25 / 0.20 / 0.25 |
| `_P_REGIME_CAP` | `ev_engine.py` | 0.97 |
| `TIMESFM_SIGMOID_K` | `orchestrator.py`, env `TIMESFM_SIGMOID_K` | 10.0 |
| `TIMESFM_CLAMP_MIN` / `_MAX` | `orchestrator.py`, env | 0.05 / 0.95 |
| `MARKOV_EMA_ALPHA` | `settings.py` | 0.30 |

Highest blast radius (every symbol, non-linear interactions in the logit
pool) — needs the tightest `PARAM_BOUNDS` and smallest rate-limit step
size once moved into the registry, precisely because there's no human
backstop.

### Tier C — Gate thresholds (safety margins, not performance)

| Parameter | Location today | Current value |
|---|---|---|
| `MIN_REGIME_CONF` | `orchestrator.py`, env | 0.60 |
| `MIN_KELLY` | `orchestrator.py`, env | 0.02 |
| `MIN_TIMESFM_CONFIDENCE` | `orchestrator.py`, env | 0.05 |
| `MIN_EV` | `orchestrator.py`, env | 0.0 |
| `MIN_RR` | `orchestrator.py`, env (+ separately in `heavy_sweep.py`) | 1.5 (live) / 2.0 (default) |
| `MIN_P_WIN` | `orchestrator.py`, env | 0.55 |
| `SIGNAL_COOLDOWN_MINUTES` | `orchestrator.py`, env | 60 |
| `KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU` | `settings.py` | True |

Loosening these is how false signals get let through. The `MIN_RR` split
between the orchestrator and `heavy_sweep.py` (documented in
`20260817_RECALIBRATION_PERF_ASSESSMENT.md`) is exactly the kind of drift
the inventory step in this doc is meant to catch — two supposedly-linked
values that silently diverged because nothing was tracking them as one
registry entry.

### Tier D — Calibration/enforcement mechanics

| Parameter | Location today | Current value |
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

The tier most likely to need tuning as live behavior is observed (e.g.
after the stagnation breaker runs for a few weeks) — the best first
candidate for migrating into the §2 registry, since it's already
designed to be reversible and most values here already ship with an
escape hatch (§4.6).

---

## 4. Gates — required before ANY parameter change ships

These replace "get it reviewed" with mechanisms that actually fire without
a human in the loop. Every fix in the 2026-08-22 audit followed 4.1–4.5;
4.7 replaces the old human-sign-off gate.

### 4.1 Baseline capture
Record the CURRENT live values of whatever metric the change is meant to
move (per-symbol calibration bias, win rate, Sharpe, kelly_f distribution)
**before** touching code. Use live Supabase queries (`nt_eod_actions`,
`nt_calibration_records`, `nt_sweep_result`), not assumptions.

### 4.2 Live-evidence root cause
No parameter change ships on theory alone. Confirm the mechanism with live
data — SSH into the Studio and read `orchestrator.log`, query Supabase
directly, or add a temporary diagnostic log line (as fix #3a's `efd6189`
commit did) and wait for real ticks if the mechanism is still ambiguous.
Worklogs describe a point in time, not a permanent truth — re-verify
against current live state, don't cite a past worklog as still-current
fact.

### 4.3 Isolated, pure, unit-tested implementation
Extract the tuning logic into a pure function wherever possible (e.g.
`_apply_regime_kelly_floor()`, `_stagnation_streak()`/`_stagnation_tier()`)
rather than leaving it inline in a 2000-line orchestrator method. A pure
function is unit-testable without mocking half the Supabase/Redis/HMM
stack, and its tests double as executable documentation of the intended
behavior at the boundaries.

### 4.4 Full test-suite gate
`pytest tests/ -q` must be 100% green (0 failed) before ANY commit. Every
fix in the 2026-08-22 audit was verified this way, including catching and
fixing 3 test regressions that a change to shared logic (the stagnation
breaker) silently introduced in unrelated test files.

### 4.5 Escape hatch required
Every behavioral parameter change must ship with a way to instantly revert
to the pre-change behavior WITHOUT a code change. Examples already in
place: `MARKOV_EMA_ALPHA=1.0` disables smoothing;
`KELLY_REGIME_FLOOR_REQUIRE_NONNEG_MU=False` restores the always-floor
behavior. Once §2's registry exists, the registry's own auto-rollback
(4.7) supersedes needing a bespoke escape hatch per parameter — but until
a parameter is migrated, it still needs one.

### 4.6 Deploy vs. code-complete are different states
Pushing a commit to `origin/main` is NOT deployment. State explicitly, in
both the worklog and to the human, whether a change is (a) code-complete
and tested, (b) pushed, or (c) live on Studio — and never restart the live
Studio process for a behavioral change without explicit human go-ahead
(diagnostic-only log lines are the one exception, per this session's
established practice).

### 4.7 Bounded, rate-limited, self-reverting — the actual safety net
Replaces the old "human sign-off by tier" gate. Applies per §3 tier once a
parameter is on the §2 registry:

| Tier | Bound width | Max step per cycle | Auto-rollback |
|---|---|---|---|
| A (brick params) | Implicit via backtest gate | Whatever the grid search picks | `qualified=False` already blocks a bad write |
| B (blend weights) | Narrow — small % of current value | Smallest step of any tier; these interact non-linearly | Mandatory: revert on ANY regression in calibration bias or Sharpe over the monitoring window (§4.8) |
| C (gate thresholds) | Narrow, and only loosen with extra evidence (§4.1/4.2); tightening is lower-risk than loosening | Small | Mandatory: revert if false-qualify rate or realized loss rate increases |
| D (calibration mechanics) | Wider — this tier is meant to be tuned as behavior is observed | Can be larger | Already has an analogous shape via the stagnation breaker's own `STAGNATION_RESET_CYCLES`; generalize it |

`TUNING_AUTONOMY_ENABLED=false` (§2.3) is the global escape hatch above
all four rows — the one switch a human might actually use.

### 4.8 Post-deployment monitoring window
Define, before deploying, how long to watch and what confirms
success/failure — e.g. "N correction cycles" or "N hours of ticks" — and
wire the revert condition in 4.7 to fire automatically at the end of that
window, not on someone remembering to check.

---

## 5. Required worklog structure for any parameter-tuning work

Every piece of parameter-tuning work — whether a single fix or a batch —
gets a worklog entry (`worklog/YYYYMMDD_<TOPIC>.md`) with this structure:

1. **Header** — scope, type (assessment / fix / both), date, status line
   that states plainly whether anything has been deployed yet.
2. **Plain-language summary** — what's broken/being tuned, in terms a
   non-engineer stakeholder can follow, before any code detail.
3. **Live evidence** — actual log lines, actual Supabase query results,
   actual timestamps. Not hypothetical examples.
4. **Root cause / mechanism** — the specific code path, with file:line
   references, and WHY it produces the observed behavior.
5. **What changed** — code diff summary + commit hash, referencing the
   pure function/test added (§4.3).
6. **Verification trail** — test suite result (exact pass/fail count),
   backtest/replay result if applicable, live re-verification if applicable.
7. **Status / candidate-fix table** — for multi-part work, one row per
   fix: scope, status (not started / implemented / deployed), commit,
   bounds + rate limit + rollback condition if it's a §2-registry parameter.
8. **Open questions** — anything genuinely ambiguous, stated as an
   explicit question. Do not phrase this as "awaiting sign-off" for
   routine Tier D changes — per this doc's revision, that review will not
   happen; only raise a question here if it's something that actually
   needs the operator's judgment (e.g. "should this bound be widened").

---

## 6. What an agent should NOT do

- Do not tune a Tier B/C parameter without the bounds + rate limit + auto-
  rollback in §4.7 — "arbitrary within bounds" is acceptable; "arbitrary
  and unbounded" is not.
- Do not build a live loop that adjusts a parameter from the system's own
  MOST RECENT output only, with no history/rate-limit — that is the exact
  shape of the compounding-bias incident this playbook exists to prevent.
  The stagnation breaker's cycle-count-based streak detection (looking at
  several prior cycles, not just the last one) is the right shape.
- Do not put a new tunable parameter only in `settings.py`/Studio Secrets
  if it's meant to be tuned autonomously — that path requires a human +
  restart that will not happen. Use the §2 registry instead.
- Do not restart the live Studio process to deploy a behavioral change
  without explicit human confirmation (§4.6).
- Do not promote a change without a green full test suite (§4.4).
- Do not ship a parameter change without either an escape hatch (§4.5) or
  (once migrated) the registry's own auto-rollback (§4.7).
- Do not disable or bypass `TUNING_AUTONOMY_ENABLED` in code — it is the
  one switch reserved for a human, precisely because everything else in
  this doc assumes they won't otherwise be watching.
