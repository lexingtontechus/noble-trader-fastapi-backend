# AAPL Qualification Drought — Investigation, and Self-Learning Scope

> **Status: investigation complete + scope only, nothing built.** Per
> "scope in worklog, get honest review, build only on explicit
> instruction" — this document answers "issue or non-issue" for AAPL
> with real production evidence, then uses that exact finding as the
> motivating case for scoping the self-learning direction named in
> [[project_optimize_then_self_learn]]. Query `nt_roadmap` for anything
> that gets built out of this later.

## 1. AAPL — issue or non-issue: **issue**, but not the one it looks like

**The symptom**: AAPL has not qualified a single signal since
2026-09-02 (8 days). `v_symbol_signal_scorecard_7d` shows 0 qualified,
0 resolved for the trailing week.

**First-pass hypothesis, checked and ruled out**: "it's just a stock,
markets are closed most of the day." Real data confirms AAPL's price
genuinely only moves during ~13:00-19:00 UTC (NYSE hours) and is
flat outside it — but that's expected/correct behavior for an equity
feed, not a bug, and historically AAPL still qualified plenty (71
times all-time, spread across all hours of day in the historical
distribution). So "closed market = no signals" doesn't explain an
8-day drought by itself.

**Second-pass hypothesis, confirmed**: checked AAPL's Kelly-sizing
inputs specifically **during real trading hours** (13:00-19:00 UTC,
prices genuinely moving) for the last 7 trading days:

| Day | n_ticks (trading hrs) | avg p_win | kelly_pos (of n) | qualified |
|---|---|---|---|---|
| 09-09 | 23 | 0.61 | 0 | 0 |
| 09-08 | 39 | 0.19 | 0 | 0 |
| 09-07 | 83 | 0.21 | 0 | 0 |
| 09-06 | 83 | 0.22 | 0 | 0 |
| 09-05 | 83 | 0.20 | 0 | 0 |
| 09-04 | 82 | 0.20 | 0 | 0 |
| 09-03 | 84 | 0.50 | 1 | 0 |

Kelly is essentially always exactly 0 — not "small," zero — even
during hours with real price movement, every single trading day for a
week straight.

**Root cause, traced through real log lines and real price data**:
AAPL's actual close price **rose** 09-01→09-03 (\$316.66 → \$328.07),
then **fell** every single day 09-04→09-10 (\$328.07 → \$315.45) — a
clean, sustained multi-day trend reversal. But the regime classifier's
label for AAPL has been `low_vol_strong_bull` / `low_vol_bull`
**continuously since 09-01**, at confidence 0.90-1.00 (literally
`1.0000` — the numerical maximum — on 09-05, 09-06, and 09-07),
**never once correcting to a bear/down label through the entire
reversal**. Confirmed via direct log lines during real trading hours
(09-09, 14:00-19:00 UTC): `direction=long` (dictated by the stuck bull
label) while the independently-computed local mean return `mu` stays
**negative** the entire session (-0.00026 to -0.00045) — because price
was, in reality, still falling. `mu/sigma2` (the Kelly edge estimate)
comes out strongly negative every tick as a direct, correct
consequence, so `vol_scaled=0.0, regime_gated=0.0` — Kelly is
deliberately, correctly gated to zero.

**So: is this an issue?** Two separate findings, and they point in
different directions:
1. **The qualification gate is working exactly as designed** — it is
   correctly refusing to publish a confidently-stated "buy" that the
   real, independently-computed price statistics say is currently a
   losing bet. Nothing is broken about the Kelly math or the gate
   itself. This half is a **non-issue**.
2. **The regime classifier has a real, un-detected defect for this
   symbol** — it has been stuck at nearly saturated bull confidence for
   9+ days through a clean trend reversal, and nothing in the platform
   noticed. This half is a genuine **issue**.

**Why existing mechanisms didn't catch it — the actual gap, precisely
identified**: `orchestrator._write_regime_shift_alert` **did** fire 13
times for AAPL in this window (confirmed: `nt_eod_actions`, action=
`regime_shift_alert`, last at 2026-09-09 14:45 UTC) — but every one of
those was a shift **within** the bull family (`low_vol_bull` ↔
`low_vol_strong_bull` ↔ `med_low_vol_strong_bull`), never a crossing
into a bear label. `EVEngine.detect_regime_shift()` (Layer 2, built
2026-08-23 per `worklog/PARAMETER_TUNING_PLAYBOOK.md` §2.5.3) can only
detect a shift **when the regime engine itself produces one** — it
compares consecutive labels/posteriors. It has no way to notice "the
regime engine should have produced a shift and didn't," because from
its point of view nothing paradoxical happened — it only ever sees the
engine's own (in this case, stuck) output. This is a structurally
different failure mode from what every regime-related mechanism built
this session (debounce, warm-start, meta-regime coherence) targets:
those all defend against a **noisy, over-eager** classifier flipping
labels too often. AAPL is the opposite failure — a **confidently
under-eager** classifier that should have flipped and didn't. Nothing
in the platform currently checks "does the regime's implied direction
still agree with the sign of realized price movement over a real
window" — that comparison doesn't exist anywhere today.

**Not fixed here — this section is diagnosis, not a build.** Two
credible, separately-scopeable fixes exist (not chosen or built now):
(a) a direct statistical check — compare the regime's implied
direction against the realized N-bar cumulative return sign, alert if
they've disagreed for M consecutive ticks despite sustained high
confidence; (b) investigate why the regime-HMM's fit for AAPL
specifically produced a saturated, unmoving posterior through a real
reversal — a fitting/warm-start defect worth its own root-cause pass,
similar in shape to (but distinct from) the warm-start/debounce issues
already found and shadow-tested for other symbols this session.

## 2. Self-learning — scope, informed directly by the AAPL case above

**Grounding first: this isn't a green field.** Before proposing
anything new, here's what the codebase already has, so nothing below
duplicates or contradicts it:

- **Layer 0** (`tuning_registry`) — a config-file-first, DB-fallback
  registry for tunable parameters, already the read path for every
  `_settings_number()` call across `eod_correction.py`/
  `calibration_enforcement.py`/`drift_monitor.py`. Built.
- **Layer 1** (`auto_tuner.py`) — a daily, bounded, self-reverting
  coordinate-ascent tuner, deliberately narrow (Tier D
  calibration/enforcement constants only), evaluated against one
  system-wide health metric (mean |raw bias|). Built, explicitly "not
  a black-box optimizer... honest about the 'arbitrary within bounds'
  reality" (its own docstring). This is real, working, narrow
  self-tuning — a foundation, not a gap.
- **Layer 2** (`worklog/PARAMETER_TUNING_PLAYBOOK.md` §2.5.3) —
  agent-triggered tuning on detected regime shift. Items 1-2
  (`detect_regime_shift()` wired live, `regime_shift_alert` written to
  `nt_eod_actions`) are **built**. **Item 3 — "a Hermes cron job that
  watches `nt_eod_actions` for unactioned `regime_shift_alert` rows and
  triggers an agent session — the same kind of live-evidence-gathering,
  bounded-fix, test-gated work this session did manually, but on a
  detected trigger instead of a human asking" — was scoped 2026-08-23
  and explicitly marked "not built — outside this repo's scope"**
  (Hermes-platform configuration, not backend code). This is, almost
  verbatim, what "self-learning" means in this conversation. The
  scoping work below is a direct continuation of that item, not a
  competing idea.
- Calibration bias enforcement (bayesian penalty / kelly reduction /
  emergency heavy sweep / stagnation breaker, this week's `nt_eod_actions`
  → `nt_symbol_calibration_state` split) is itself a real, live,
  narrow self-correction loop: detect miscalibration → apply a
  generic penalty → auto-recover on genuine improvement, all without
  a human in the loop for the routine case.

**What's actually missing, made concrete by the AAPL case**: every one
of the mechanisms above corrects a *known, anticipated* failure shape
(a threshold drifting, a bias emerging, a detected regime shift). None
of them can notice a *new* failure shape nobody anticipated — like "a
classifier is confidently and persistently wrong in a way that never
technically triggers its own change-detection." Finding that required
a human (me, this conversation) manually pulling gate logs, cross-referencing
day-open/day-close prices, and reading raw Kelly-math log lines by
hand. That manual pattern — described in Layer 2 item 3 as "the same
kind of live-evidence-gathering, bounded-fix, test-gated work this
session did manually" — is exactly what's not yet automated.

### Proposed staged plan

Same shadow-first, human-gated-promotion discipline as every other
mechanism this session (heavy sweep modes, regime debounce, meta-regime
model) — nothing here proposes skipping that.

**Phase 1 — pgvector codebase + worklog index (low risk, pure
retrieval, read-only, no trading-logic changes).**
Supabase supports `pgvector` natively — a new table (e.g.
`nt_knowledge_embeddings`: `id`, `source_type` [`code`|`worklog`|
`roadmap_note`], `path`, `chunk_text`, `embedding vector(N)`,
`updated_at`) populated by chunking `.py` files, `worklog/*.md`, and
`nt_roadmap.notes`, embedding each chunk, and storing the vector. A
`semantic_search(query, k)` function (cosine-distance ORDER BY) is the
only new capability exposed. This directly answers the kind of question
this session kept re-deriving by hand via grep/Explore-agent passes —
"have we seen a stuck-classifier bug shaped like this before," "where
else does this exact redundant-computation pattern show up," "what did
we already decide about X" — faster and more completely than grep,
without touching anything that generates a live signal. Re-index
trigger: on push (cheapest), or a nightly job. This is the most
obviously buildable-now piece — it's infrastructure, not a decision
that touches risk.

**Phase 2 — statistical anomaly detectors for failure shapes already
seen this session, as plain functions (not LLM calls).** Deliberately
NOT AI for this layer — these are precise numerical checks, and coding
them directly is more reliable than delegating them to a model:
- Regime-direction-vs-realized-price-sign mismatch (the AAPL gap,
  §1 above) — the concrete first candidate.
- "Dead mechanism" detection: a shadow-mode table nobody has written to
  in N days despite the shadow flag still being on, or a config value
  computed and logged but never consumed by anything (the exact shape
  of the `sweep_skip_until` bug found and fixed in §13 of the
  2026-09-09 worklog).
- "Redundant computation" detection: two functions independently
  computing a classification from the same inputs with different
  thresholds and disagreeing on the boundary (the exact shape of the
  0.05-vs-0.10 calibration-status bug found and fixed in §16 item #2).
Each of these is a **known, already-lived pattern** in this specific
codebase's history — encoding the pattern directly is honest and cheap;
reaching for an LLM to "notice" something a five-line SQL/Python check
already catches would be over-engineering in the same spirit this
session has been correcting all day.

**Phase 3 — LLM-assisted triage, grounded in Phase 1's index, reading
Phase 2's detector output (medium risk, output is a written
report/proposal only, nothing auto-applied).** This is where an LLM
function genuinely earns its place: given a detector firing (e.g. "AAPL
regime/price mismatch, 9 days, confidence saturated"), retrieve related
code + worklog context via Phase 1's semantic search, and generate a
written root-cause narrative + a *proposed*, scoped fix description —
structurally the same output this worklog section itself is, just
produced automatically on a trigger instead of by a human asking. This
is the direct realization of Layer 2 item 3's "the same kind of
live-evidence-gathering, bounded-fix, test-gated work this session did
manually, but on a detected trigger." Output lands as a new row in a
table (e.g. `nt_self_learning_reports`) or a scoped worklog draft — a
human still reads it and decides whether to approve a build, exactly
like every mechanism this session went through. No write access to any
live parameter at this phase.

**Phase 4 — explicitly NOT recommended soon.** Any LLM or auto-tuner
component with *write* access to a live trading/sizing parameter.
Should only be considered, if ever, after Phases 1-3 have run long
enough on real data to build trust in the detection quality — and even
then it should enter through the exact same shadow-mode → real-data
comparison → explicit human promotion pipeline every other mechanism
in this codebase has gone through, no exception for "the AI suggested
it."

### Open questions genuinely needing your decision before any of Phase 1 is built

1. **Embedding provider** — an external API call (OpenAI/Voyage-style)
   costs money per re-index and sends code out of the Supabase/Lightning
   boundary; a local model (`sentence-transformers` running on the
   Lightning GPU box already used for TimesFM) costs compute/build time
   but keeps everything in-house. No default assumed — this is a real
   tradeoff, not a technical detail.
2. **What's actually indexed** — code + worklogs is the obvious core;
   should `nt_roadmap.notes` (a lot of the actual incident history lives
   there, not in `worklog/*.md`) be included too? Leaning yes, given how
   much of this session's own institutional memory lives in that table,
   but flagging it since it's DB content, not files.
3. **Re-index trigger** — on every push (simplest, matches this
   session's git-push-then-you-deploy rhythm) vs. a nightly job
   (matches the existing daily-cadence pattern everywhere else in this
   codebase). Leaning toward on-push for freshness, but it's your call.
4. **Phase 2 scope for v1** — build all three detector shapes listed, or
   start with just the AAPL-shaped one (regime-vs-price mismatch) since
   it's the one with a real, current, concrete case to validate against?

Nothing above is built. Waiting on direction on which phase(s) to
scope further or start building, and answers to the open questions
before Phase 1 specifically.
