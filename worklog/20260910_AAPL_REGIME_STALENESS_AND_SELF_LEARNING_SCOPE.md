# AAPL Qualification Drought — Investigation, and Self-Learning Scope

> **Status: investigation complete + scope only, nothing built.** Per
> "scope in worklog, get honest review, build only on explicit
> instruction" — this document answers "issue or non-issue" for AAPL
> with real production evidence, then uses that exact finding as the
> motivating case for scoping the self-learning direction named in
> [[project_optimize_then_self_learn]]. Query `nt_roadmap` for anything
> that gets built out of this later.

> **Worklog convention (2026-09-10, revised)**: this file bundles
> several distinct tasks worked across one session. Each still carries
> its own **Priority / Task / Status / Date / Roadmap refs** blockquote
> under its `##` heading (kept for in-place context while reading a
> section), but as of this revision, **new worklogs put a summary table
> of these same fields FIRST, immediately after the title** — one row
> per section — so the whole file's state is readable at a glance
> without scrolling. The table below was added retroactively to this
> file to match. `Roadmap refs` cite `nt_roadmap` rows by their `id`
> column (added 2026-09-10) as `RM-<id>`. Status vocabulary matches
> `nt_roadmap.status` (`pending` / `blocked` / `resolved` /
> `superseded`) plus `n/a` for sections that are pure investigation with
> no tracked roadmap row.

| # | Priority | Task | Status | Date | Roadmap refs |
|---|---|---|---|---|---|
| §1 | P1 | Diagnose AAPL qualification drought | resolved | 2026-09-10 | RM-40 |
| §2 | P2 | Scope 4-phase self-learning direction | superseded | 2026-09-10 | RM-36 |
| §3 | P2 | Revise self-learning scope (Layer 2 → APScheduler, DB split, embedding redesign) | resolved | 2026-09-10 | RM-36 |
| §4 | P1 | Build Phase 1 local knowledge index | resolved | 2026-09-10 | RM-36, RM-38 |
| §5 | P1 | Reframe AAPL staleness as a coverage gap, not a point-fix | blocked → resolved by §8 | 2026-09-10 | RM-40 |
| §6 | P0 | Root-cause XAUAUD staleness; harmonize APScheduler with Studio's restart schedule | resolved | 2026-09-10 | RM-41, RM-42, RM-43 |
| §7 | P1 | Fully scope the medium-term trend-confirmation module | blocked → resolved by §8 | 2026-09-10 | RM-40 |
| §8 | P1 | Build the trend-confirmation module scoped in §7 | resolved (deploy-verification pending) | 2026-09-10 | RM-40, RM-44 |
| §9 | P1 | Run the 14-symbol pre-shadow backtest on real data | resolved — result is a caution flag, not a clean pass (52% aggregate hit rate) | 2026-09-10 | RM-40 |

## 1. AAPL — issue or non-issue: **issue**, but not the one it looks like

> **Priority**: P1 · **Task**: Diagnose AAPL qualification drought ·
> **Status**: resolved (diagnosis only; the underlying gap is tracked
> separately, see §5/§7) · **Date**: 2026-09-10 · **Roadmap refs**:
> RM-40

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

> **Priority**: P2 · **Task**: Scope 4-phase self-learning direction ·
> **Status**: superseded by §3's revision · **Date**: 2026-09-10 ·
> **Roadmap refs**: RM-36

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

## 3. Revision (same day) — Layer 2's trigger mechanism, DB placement, and a redesigned Phase 1

> **Priority**: P2 · **Task**: Revise self-learning scope (Layer 2 →
> APScheduler, DB placement split, local embedding redesign) ·
> **Status**: resolved (superseded §2, itself built in §4) ·
> **Date**: 2026-09-10 · **Roadmap refs**: RM-36

Three pieces of user feedback, incorporated directly:

### 3.1 Layer 2 item 3 replaced: APScheduler, not an external Hermes trigger

Correct call — `PARAMETER_TUNING_PLAYBOOK.md`'s own Layer 2 item 3 (a
Hermes cron job polling `nt_eod_actions` for unactioned alerts and
spinning up an agent session) has sat unbuilt since 2026-08-23 (3+
weeks) precisely because it depends on configuration **outside this
repo** — a real dead link, not a live design.

**Replacement**: this repo's orchestrator already runs an in-process
`AsyncIOScheduler` doing exactly this shape of work today — light
sweep (`*/15 * * * *`), heavy sweep (`0 20 * * *`), eod-lookback
(`32 0 * * *`), eod-correction-intraday (4h), auto-tuner (daily
01:30). The self-learning trigger belongs on the **same scheduler, in
the same process** — a new job, e.g. `_run_self_learning_triage`,
proposed at 02:00 UTC daily (after eod-lookback/auto-tuner have fresh
post-correction data, before the next trading day starts in earnest).
No new platform, no new credentials, no cross-system trigger to
configure and forget. Logic, cheap-first:
1. Run Phase 2's statistical detectors (pure Python/SQL) across active
   symbols — fast, no model inference.
2. Nothing fires → log one line, exit. This is the common case; no
   embedding or LLM cost most nights.
3. Something fires → for each anomaly, query the local knowledge index
   (§3.3) for related code/worklog context, then call a local
   reasoning LLM once with the anomaly + retrieved context, asking for
   a root-cause narrative + a proposed fix outline.
4. Write the output somewhere a human reviews on their own schedule
   (§3.2) — never auto-applied, same discipline as every other
   mechanism in this codebase.

This is a strictly smaller, more reliable design than the original —
fewer moving parts, no dependency on a system this repo doesn't
control, and it reuses infrastructure already proven to run reliably
(the same scheduler has been running daily jobs without incident all
session).

### 3.2 Database placement — separate the knowledge index, keep `nt_roadmap` where it is

Agreed that a large embeddings table doesn't belong mixed in with live
trading telemetry — but "nt_roadmap, worklogs etc." aren't all the
same kind of data, and shouldn't all move the same way:

- **The knowledge index (code + worklog + roadmap-note embeddings)** —
  genuinely large (potentially tens of thousands of chunk rows once the
  full codebase + all worklogs are embedded), write-heavy only during
  re-index, and only ever read **from the Studio box itself** (that's
  where the local embedding model and the self-learning job both run —
  there's no reason for this data to leave the box at all). **Recommend:
  move this to a local store on the Studio persistent volume** — SQLite
  + the `sqlite-vec` extension, same file (`config/knowledge_index.sqlite`
  or similar) and the same persistence guarantee already proven this
  session for `signal_stream_cache.sqlite`/`tuning_registry.yaml`/
  `brick_params.yaml`, and the same precedent that justified the local
  TimesFM model cache (`HF_HOME`) in the first place. Zero new Supabase
  load from this table, ever — this alone addresses most of "the
  database is growing larger" for the piece that would actually be
  large.
- **`nt_roadmap`** — small (dozens of rows), and its entire value this
  session came from being **remotely queryable via the Supabase MCP
  tool from wherever an agent session happens to be running** — every
  "what's left on the priority list" check, every pulse check, every
  daily-checkin update this whole session was a direct
  `execute_sql` call against it. Moving it to a Studio-local file would
  mean every future session needs SSH + a script to read it instead of
  one MCP call — a real workflow regression for a table that isn't
  actually contributing to the growth problem. **Recommend: leave
  `nt_roadmap` in Supabase.** If the underlying worry is more about
  logical/visual clutter than actual size, a cheaper fix than a new
  database is a dedicated Postgres **schema** (e.g. `ops.nt_roadmap`
  instead of `public.nt_roadmap`) in the same project — same MCP
  queryability, zero new credentials, real namespace separation. Not
  proposing this now, just noting it exists as a middle option if "one
  more table in `public`" is the actual complaint rather than raw size.
- **Worklogs** — already files in the git repo, not database rows.
  Nothing to move; they just become a second **source** the knowledge
  index reads from (alongside code), living in the same local SQLite
  file as everything else in this section.
- **New self-learning trigger output** (§3.1 step 4) — small, one row
  per detected anomaly, needs the same cross-session remote
  visibility `nt_roadmap` already has (a human — or a future session —
  should be able to read yesterday's triage report without SSHing in).
  **Recommend a small new Supabase table**, e.g.
  `nt_self_learning_reports` (`id`, `detector_name`, `symbol`,
  `summary`, `proposed_fix`, `created_at`, `reviewed boolean`) — this
  is exactly `nt_roadmap`'s profile (small, operational, remotely
  read), not the knowledge index's profile (large, local-only).

Net effect: the thing that would actually make the database bigger
never lands in it in the first place, while the things that are
already small and already benefit from remote queryability stay put.

### 3.3 Phase 1, redesigned: local embedding model + local vector store

Per "embedding a small local LLM model is ok — we already do this for
TimesFM (800mb)" — this resolves the embedding-provider open question
from §2 outright: **local, not an external API**. Concrete design:

- **Embedding model**: `BAAI/bge-small-en-v1.5` (~130MB, 384-dim) as
  the default — small enough to run on CPU even while the GPU is busy
  with TimesFM, well-proven for retrieval, comfortably inside the
  800MB budget the TimesFM precedent already established.
  `nomic-embed-text-v1.5` (~550MB, 768-dim, up to 8192-token context vs.
  bge-small's 512) is the upgrade path if code chunks prove too long
  for bge-small's window in practice — still smaller than TimesFM.
  Loaded via `sentence-transformers`, on the same Lightning Studio GPU
  box already running TimesFM's inference gateway.
- **Vector store**: SQLite + `sqlite-vec` (the maintained successor to
  `sqlite-vss`) — one new local file, no new service/server, reuses
  the exact convention this codebase already has for local mutable
  state. Two tables: `knowledge_chunks` (path, source_type, chunk_text,
  content_hash, updated_at) and a `sqlite-vec` virtual table for the
  embedding vectors, joined by rowid.
- **Sources indexed**: `.py` under `regime_platform/`, `renko/`,
  `scripts/` (tests/ excluded from v1 — lower signal-to-noise for a
  root-cause-retrieval use case, revisit if needed), all of
  `worklog/*.md`, and `nt_roadmap`'s `current_state`/`notes` columns
  (pulled fresh from Supabase at index time — this is the one place the
  local index still talks to Supabase, as a read-only source, not a
  destination).
- **Indexing**: incremental, content-hash-based (skip re-embedding
  unchanged chunks), triggered by the **same new APScheduler job
  family** as §3.1 rather than a separate on-push/CI mechanism — one
  scheduling story, not two. A nightly `_run_knowledge_reindex` job
  (e.g. 01:45 UTC, before the 02:00 triage job consumes it) keeps the
  index fresh from the latest deployed commit + latest `nt_roadmap`
  state.
- **Query interface**: `semantic_search(query, k=5, source_type=None)`
  — embeds the query with the same local model, KNN search via
  `sqlite-vec`, returns chunk text + path + source. This is the only
  thing the §3.1 triage job calls into.
- **Reasoning LLM (Phase 3, separate sizing decision from the
  embedder)**: a small local instruct model (e.g. Qwen2.5-3B-Instruct
  or Phi-3.5-mini, ~2-4GB) for the actual root-cause narrative
  generation — meaningfully bigger than the embedder, so proposed to
  load **on-demand only when Phase 2 actually flags something**
  (step 2 of §3.1), not kept resident alongside TimesFM continuously —
  a GPU-memory management call worth its own review once Phase 2's
  real trigger frequency is known, not decided now.

Still nothing built. This section resolves 2 of the original 4 open
questions (embedding provider: local; DB placement: split by data
profile, not all-or-nothing) and replaces the Layer 2 trigger design
outright. Remaining open: re-index cadence is now proposed as nightly
APScheduler (folded into the "no new trigger mechanism" decision, not
a separate open question anymore), and Phase 2's v1 detector scope
(all three shapes vs. just the AAPL-motivated one) is still an open
call.

## 4. Phase 1 — built (2026-09-10, explicit approval: "approve to start building phase 1")

> **Priority**: P1 · **Task**: Build Phase 1 local knowledge index
> (codebase + worklogs + nt_roadmap) · **Status**: resolved (built,
> tested, deployed-verification tracked separately) · **Date**:
> 2026-09-10 · **Roadmap refs**: RM-36, RM-38

**Hardware facts corrected before picking a model, not assumed.** SSH'd
into the Studio box directly rather than trusting a stale "T4 w/GPU"
comment elsewhere in this codebase: no `nvidia-smi`, no
`/proc/driver/nvidia`, `torch` reports no CUDA device, and TimesFM's own
gateway `/health` endpoint reports `"device":"cpu"` — **this instance
has no GPU at all**, 4 CPU cores, 346GB free disk (more generous than
even the 200GB the user cited). This changed the model recommendation
from the earlier (GPU-assumed) draft: CPU-efficient, modest-sized models
instead of anything requiring VRAM.

**Embedding model**: `jinaai/jina-embeddings-v2-base-code` (~640MB,
768-dim, 8192-token context, code+docstring domain-tuned) — comfortably
inside the 800MB/200GB budget, CPU-only inference is fine for a nightly
background job with no latency requirement. Loaded via
`sentence-transformers`, which reuses this repo's *already-installed*
`torch`/`huggingface-hub` (both already dependencies for TimesFM) — one
new package, not a new dependency stack. Points its HuggingFace cache at
the exact same `<repo_root>/.cache/huggingface` directory `server.py`
(the TimesFM gateway) already uses, so the two processes share one
on-disk model cache.

**Storage: local, not Supabase**, per §3.2's split — `regime_platform/services/self_learning/knowledge_index.py`,
a new local SQLite file (`config/knowledge_index.sqlite`, gitignored,
same convention as `signal_stream_cache.sqlite`/`tuning_registry.yaml`).

**Simplified away from the original sqlite-vec plan**: on reflection
while building, decided the vector-extension dependency wasn't
justified at this corpus's actual scale. A plain BLOB column holding
each chunk's serialized embedding, with cosine similarity computed via
numpy at query time, has zero C-extension packaging/compatibility risk
between the Windows dev box and the Linux Studio deployment, and is not
meaningfully slower — confirmed by the real smoke test below (loading
1,864 vectors and searching is well under a second). Revisit only if
the corpus grows by orders of magnitude. This is the same "don't reach
for a dependency the actual scale doesn't justify" judgment call this
session made repeatedly elsewhere today.

**Built**:
- `_chunk_python_source` — AST-based splitting on top-level
  function/class boundaries (semantically meaningful chunks, not
  arbitrary fixed windows), with a fixed-window fallback for files that
  fail to parse or have no top-level defs.
- `_chunk_markdown` — splits worklogs on `#`/`##` headings (one finding/
  decision per chunk), same fixed-window fallback. Caught and fixed a
  real bug here during testing: the original implementation's "no
  headings found" check was always false (the trailing flush of
  accumulated lines produces one non-empty "section" even when zero
  headings existed), so the fallback path was unreachable — fixed by
  tracking whether a heading was actually seen, not whether the
  sections list ended up empty.
- `build_index` — incremental (content-hash-based skip for unchanged
  chunks), prunes rows for deleted files/removed sections/sections that
  shrank, so the index never accumulates stale content. Fetches
  `nt_roadmap`'s `current_state`/`notes` as a third source (the one
  place this module still talks to Supabase — read-only). Refactored
  during the build to run the blocking chunking/embedding/SQLite work
  via `asyncio.to_thread` — the first draft called blocking work
  directly inside an `async def`, which would have stalled the same
  event loop the live 15-minute sweep ticks run on during every nightly
  rebuild, worst-case for the entire first full-corpus build. Caught
  and fixed before this ever ran for real, not after.
- `semantic_search(query, k, source_type)` — the only query interface;
  embeds the query with the same model, ranks by cosine similarity.
- Wired as a new APScheduler job (`_run_knowledge_reindex`, orchestrator.py,
  01:45 UTC daily) on the SAME in-process scheduler as light/heavy sweep,
  eod-lookback, and the auto-tuner — the Layer-2-replacement decision
  from §3.1, now actually in place. Gated by `knowledge_index_enabled`
  (`config/feature_flags.yaml`, default `true`) — a plain on/off switch,
  not the off/shadow/live convention used elsewhere, since there's no
  live-baseline comparison for a search index to shadow against.

**Verified two ways, not just synthetic unit tests**:
1. 21 new tests (chunking edge cases, incremental re-index behavior,
   pruning on file deletion/shrinkage, source-type filtering, the
   roadmap fetch, and the orchestrator wrapper's best-effort error
   handling) — all using an injected fake embedder, never the real
   model or network, so CI never needs to download a 640MB model.
2. **Real smoke test against the actual codebase** (fake embedder,
   avoiding the real download for a quick structural check): 211 real
   files (`regime_platform/`, `renko/`, `scripts/`, `worklog/`), 1,864
   chunks, **0 errors**, ~1 second — proves the AST/markdown chunkers
   handle every real file in this repo cleanly, not just small
   synthetic fixtures.

815 passed, 21 skipped, 0 failures (full suite, +21 from this build).
Real embedding-model download and the first real production index
build happen on Studio deployment, per this session's established
"code-complete vs. deployed are different states" gate — not forced in
this session, matching how the TimesFM checkpoint itself was first
populated by a real deployment run, not pre-staged.

**Phase 2 methodology guidance received, not yet built**: "start with
AAPL as a test case then backtest/regression test against more
symbols" — captured for when the regime-vs-price-mismatch detector
(§2, Phase 2) is actually built: validate it correctly flags the real
AAPL case first, then run it against the other 13 symbols' historical
data to check for false positives (a symbol correctly staying in one
regime for a legitimately trending week) and false negatives, before
trusting it in the nightly triage job. Not started this turn — only
Phase 1 was approved.

## 5. AAPL — a fresh view, not another point-fix (2026-09-10)

> **Priority**: P1 · **Task**: Reframe AAPL regime-staleness as a
> coverage gap (medium-term trend confirmation), not a point-fix ·
> **Status**: blocked (scope only, awaiting approval — see §7 for the
> full build scope) · **Date**: 2026-09-10 · **Roadmap refs**: RM-40

Explicit instruction received: don't patch the staleness symptom again
(§1's two candidate directions, `regime_conf` debounce, warm-start,
etc.) — instead take a fresh look at whether the platform's regime
engine actually understands **multi-day market structure** at all. The
user supplied a real external technical read on AAPL (21-day window,
late Aug–mid Sep 2026): 10-day MA crossed back above 50-day MA on
08-28 (bullish reversal), price structurally above its 20/50/200-day
MAs ($303.79 / $281.72 / $284.11), RSI ~50.2 (cooled from overbought,
consolidating not exhausted) — framed as the kind of read the
platform's "short lookback + market pulse" direction has been aiming
at.

**What "short lookback" and "market pulse" actually are in this
codebase today** (neither is a literal existing term — both are this
session's shorthand for a real direction; verified by reading the
code, not assumed):

- **Short lookback, live**: `RegimeHMM` (`regime_engine.py`) — the
  engine that actually drives Kelly sizing — fits on **hourly closes**,
  purely return-derived features (rolling vol over 5h/20h windows,
  trend = 1-bar return + cumsum over 10h/30h windows). Max lookback
  ~30 hourly bars, **~1.25 calendar days**. No price level, no moving
  average, no structural feature of any kind.
- **Short lookback, newer**: `meta_regime_model.py` /
  `signal_stream_cache.py` (built this session) — a 2-state HMM over
  the platform's *own signal stream* (confidence deviation, label
  persistence, regime_conf volatility), ~12.5h minimum window growing
  with the cache's age. This answers "is my own process coherent," not
  "what is the market doing" — it has no price input at all.
- **Long lookback, built but never promoted**: `macro_filter.py` — a
  genuine multi-month feature that already exists: last daily close vs.
  its **200-day SMA**, EMA-smoothed, labeled bull/bear/neutral. Gated
  `macro_regime_mode` (`off`/`shadow`/`live`), **currently `shadow`** —
  explicitly forbidden from touching Kelly sizing by its own module
  docstring. Checked its live output for AAPL right now: `macro_label:
  bull, macro_score_smoothed: 0.132, macro_strength: 0.878, computed
  2026-09-10 06:00 UTC` — **this already agrees with the user's own
  external read** ("structurally above the 200-day MA"). The platform
  has this piece of the picture correct; it just isn't wired to
  anything.

**The actual gap, stated precisely**: there is a full order of
magnitude missing between "hours" (RegimeHMM's live trend feature,
~1.25 days) and "200 days" (macro_filter, shadow-only, single
timeframe, static level-check only) — nothing at the **10-to-50-day
scale**, and nothing anywhere in the codebase does **crossover-based
reversal detection** (a moving average crossing another, which is
exactly what caught AAPL's 08-28 reversal in the user's external
read and is exactly the kind of signal a saturated-confidence HMM
structurally cannot produce on its own). RSI/MACD/ADX are separately
computed (`services/hmm/model.py::_compute_ta_indicators_local`) and
do feed the legacy 3-state `trend_hmm` (currently the live
20%-weighted `p_markov_hold_n` component of the EV blend per
`markov_source_mode: trend_hmm_shadow`) — but buried as one of 9 HMM
emission inputs, never surfaced as a standalone momentum read the way
the user's "RSI near 50.2 = consolidating" framing uses it.

**Proposed direction (scope only, nothing built, awaiting approval)**:
a new medium-term trend-confirmation module — a sibling to
`macro_filter.py`, same shadow-first discipline (`off`/`shadow`/`live`,
default `off` or `shadow`) — computing 10/20/50-day SMAs (or EMAs) on
daily closes plus a crossover-based reversal flag (short MA re-crossing
above/below a medium MA), captured to its own `nt_*` shadow table the
same way `macro_filter` captures to `nt_macro_regime`. Framed as a
genuine coverage gap fix (the platform gains a timeframe it currently
lacks entirely), not a targeted AAPL patch — validated against AAPL
first only because that is where a precise, dated ground-truth reversal
(08-28) and a precisely diagnosed failure (stuck regime through
09-01→09-10) both already exist, then backtested against the other 13
symbols before any live wiring — the same AAPL-first-then-13-more
methodology already captured in `self_learning_phase_2_detectors`
above. Not started this turn — this section is scope only, per
"scope in worklog, get honest review, build only on explicit
instruction."

## 6. XAUAUD deep-dive → Studio restart schedule harmonization (2026-09-10)

> **Priority**: P0 · **Task**: Root-cause XAUAUD's 4+ day heavy-sweep
> staleness; harmonize APScheduler cron times with Studio's free-plan
> restart schedule · **Status**: resolved (fix applied and deployed —
> `nt_symbol.sweep_cron` is DB-driven and took effect live; the
> `eod-lookback` code change is pushed) · **Date**: 2026-09-10 ·
> **Roadmap refs**: RM-41, RM-42, RM-43

**Root cause found**: reconstructed the full 09-09 20:00 UTC heavy-sweep
batch from the log. Only ~10 of 14 symbols ever completed it (5 by
20:08, another 5 in a suspicious tight cluster at 22:22-22:25 after a
2h14m dead silence); 4 (US30USD, USDJPY, XAGUSD, XAUAUD) never
completed that day. Per-symbol duration was wildly inconsistent (24s
to 471s for the *same* 72-combo grid search) — consistent with
`HEAVY_SWEEP_MAX_CONCURRENCY=4` running CPU-bound numpy/hmmlearn work
via `asyncio.to_thread` on this box's 4 real cores (no GPU, confirmed
earlier) — GIL contention, not the stale docstring's "T4 GPU: 14
symbols <30s" assumption.

The actual trigger: the user shared Studio's real free-plan restart
schedule (confirmed via a screenshot of 6 active scheduled restarts,
PDT) — Studio sleeps after ~4h and is kept alive by a fixed restart
cycle: **06:00, 10:04, 14:08, 18:12, 22:16, 02:24 PDT** (=
**13:00, 17:04, 21:08, 01:12, 05:16, 09:24 UTC**, with the last window
shortened to realign back to 13:00). A restart kills any in-flight
`asyncio` work with zero resume. Heavy sweep was firing `0 20 * * *`
(20:00 UTC) — inside the 17:04-21:08 UTC window, but only **68 minutes**
before that window's restart. `eod-lookback` (`32 0 * * *`, 00:32 UTC)
was inside the 21:08-01:12 UTC window with only **40 minutes** before
its restart — and had 2 confirmed `PATCH signal ... failed` errors
logged 2026-09-09 22:09, consistent with this.

XAUAUD (position 12 of 14 in `nt_symbol`'s list order, alongside
XAUEUR/XAUUSD/XAGUSD at the tail) is structurally the most exposed to
losing its turn in the 4-way-concurrent queue every time a restart
lands mid-batch, while early-list symbols (AAPL, AUDUSD, BTCUSD,
EURUSD, GBPUSD) reliably finish first and every day. Other tail
symbols missed a day too but recovered on a later attempt; XAUAUD's
4-day streak looks like bad luck compounding across multiple restarts,
not a symbol-specific defect.

**Fix applied 2026-09-10** — align both jobs to fire just *after* a
restart instead of near the *end* of a window, so each gets close to
the full ~4h runway instead of a random tail-end scrap:
- `nt_symbol.sweep_cron`: `0 20 * * *` → **`6 17 * * *`** (17:06 UTC,
  2 min after the 17:04 UTC restart) for all 14 active symbols. This is
  a **DB-only change** — `heavy_sweep.py`'s job-refresh logic derives
  the APScheduler job ID from the cron string itself and both adds the
  new job and removes the stale one on the next 5-minute symbol-registry
  refresh, already confirmed by reading that code path. Took effect on
  the currently-running Studio process immediately, no deploy needed.
- `orchestrator.py` `eod-lookback`: `32 0 * * *` → **`17 1 * * *`**
  (01:17 UTC, 5 min after the 01:12 UTC restart), preserving the
  original ":XX offset from the light-sweep's :00/:15/:30/:45 grid"
  collision-avoidance trick from the original 2026-08-24 fix. This is a
  **code change**, needs a Studio deploy to take effect (not yet
  deployed as of this writing).

**Deliberately not touched this pass**: `eod-correction-intraday`
(currently 04:30/08:30/12:30/16:30/20:30 UTC) also lands with a tight
30-50 minute runway before its window's restart in all 5 slots — but
there's no direct failure evidence for it this session (unlike
heavy-sweep's concrete 4-day XAUAUD staleness and eod-lookback's 2
confirmed PATCH failures), and a clean fix needs a fully custom
5-times-a-day offset table (the restart cadence isn't a uniform 4h —
minute offsets drift +4,+4,+4,+4,+8) rather than a simple formula
tweak. Flagged in `nt_roadmap` for a future pass, not built now.

## 7. Macro filter refinement — full build scope (2026-09-10)

> **Priority**: P1 · **Task**: Fully scope a medium-term (10-50 day)
> trend-confirmation module, sibling to `macro_filter.py`, per §5's
> proposal · **Status**: blocked (scope only, nothing built, awaiting
> approval) · **Date**: 2026-09-10 · **Roadmap refs**: RM-40

Fleshing out §5's proposal into a concrete, buildable spec. Nothing in
this section has been implemented.

### 7.1 Problem restated

The platform has live-short (`RegimeHMM`, ~1.25 days, pure return/vol,
no price level) and shadow-long (`macro_filter.py`, 200-day SMA, a
single static level check) trend features. Nothing covers the
10-to-50-day scale, and nothing does **crossover-based reversal
detection** — exactly the mechanism the user's external AAPL read used
(10-day MA re-crossing above the 50-day MA on 08-28) and exactly what a
confidence-saturated hourly HMM cannot produce on its own.

### 7.2 Design — new sibling module, not a `macro_filter.py` rewrite

New file `regime_platform/sweep_orchestrator/trend_filter.py`, same
shape as `macro_filter.py` (pure-calc function + async refresh + a
`get_*` live-read wrapper), **not** a change to `macro_filter.py`
itself — the two modules answer genuinely different questions (200-day
structural position vs. 10-50-day momentum/reversal) and
`macro_filter.py`'s own docstring already establishes the
"deliberately separate, not blended into one metric" convention this
should follow.

**Inputs** (daily closes, reused — see 7.5 for the fetch-sharing plan):
- `SMA_10`, `SMA_20`, `SMA_50` — three windows, not just the one pair
  the external read used, so the crossover-pair choice (7.7) can be
  validated empirically instead of guessed.
- `RSI_14` — reuse `services/hmm/model.py::_compute_rsi` (Wilder's
  smoothing, already implemented and tested there) rather than writing
  a second implementation. That function currently only takes a plain
  `np.ndarray` and returns an array with no DB/async coupling, so it
  can be imported directly with no refactor.
- Crossover state: on each refresh, compare today's `SMA_10 - SMA_50`
  sign against yesterday's (stored, same `prev_*` continuity pattern
  `compute_macro_regime` already uses for its EMA) to detect a sign
  flip = a crossover event *today*. Track `days_since_crossover` and
  `last_crossover_direction` (bullish/bearish) so "how fresh is this
  signal" is answerable, not just "which side are we on right now."

**Output fields** (mirrors `compute_macro_regime`'s return shape):
```
trend_label: "reversal_up" | "reversal_down" | "trending_up" |
             "trending_down" | "consolidating"
trend_strength: float 0-1 (normalized |SMA_10 - SMA_50| / SMA_50,
                same saturation-clamp pattern as macro_strength)
rsi_14: float
rsi_state: "overbought" | "oversold" | "neutral" (classic 70/30
           thresholds as the starting default, tunable — see 7.7)
sma_10, sma_20, sma_50: float (raw, for debugging/backtest reuse)
days_since_crossover: int
last_crossover_direction: "bullish" | "bearish" | null
```
`trend_label` derivation: `reversal_up`/`reversal_down` when
`days_since_crossover` is small (e.g. ≤3, tunable) and the direction
matches; otherwise `trending_up`/`trending_down` from the crossover
side alone; `consolidating` when RSI is neutral AND no recent
crossover — this is the AAPL 09-10 case today (RSI ~50, past the 08-28
crossover) and should read `consolidating`, not `trending_up`, matching
the external read's own framing ("cooled off... cleanly consolidating")
rather than just re-deriving the same "still bull" read macro_filter
already gives.

### 7.3 Storage — new table `nt_trend_regime`, not new columns on `nt_macro_regime`

Same reasoning as 7.2: separate concerns, separate table. Shape mirrors
`nt_macro_regime` exactly (`symbol` PK, one row per symbol reflecting
the latest computed state, no history retained in this table — history
lives in the raw daily-bar source, re-derivable on demand for
backtesting, same as `nt_macro_regime` today).

### 7.4 Feature flag — `trend_regime_mode`, same 3-stage discipline

`off` (build-time default) → `shadow` (compute + capture to
`nt_trend_regime`, no Kelly impact) → `live` (a new
`kelly_trend_multiplier()`, same `[0.25, 1.5]` hard safety band as
every other multiplier in this codebase — `kelly_macro_multiplier`,
`stagnation_kill_switch`). Config-file-first via `tuning_registry`'s
existing YAML pattern, per [[config_centralization_preference]] — no
new `os.getenv()` flag.

### 7.5 Refresh cadence and fetch reuse

Daily, piggybacked onto `macro_filter.refresh_macro_regime`'s existing
daily-bar batch fetch (`fetch_tradingview_batch(active, n_bars, "1D")`)
rather than a second independent TradingView batch call for the same
symbols on the same cadence — `n_bars` just needs to cover the largest
of the two modules' windows (`max(200, 50) + buffer`, so 200-day SMA
still gets its full window). Whether this becomes a literal shared
call or two modules each reading from one shared fetch result is an
implementation detail to settle at build time, not scope now — the
important constraint is: don't fetch the same daily bars twice.

### 7.6 Test plan

- Unit tests for `SMA`/RSI/crossover-detection correctness against
  small deterministic fixture price series with a known, hand-computed
  crossover point (same style as `test_knowledge_index.py`'s injected
  fixtures — no real network/model dependency).
- **AAPL real-data validation** (the methodology already captured in
  `self_learning_phase_2_detectors`, applied here first): fetch AAPL's
  real daily closes for the relevant window, confirm
  `last_crossover_direction` flips to `bullish` on 2026-08-28 and
  `trend_label` reads `consolidating` by 2026-09-10 with `rsi_state:
  neutral` — a direct regression test against the exact external read
  that motivated this section, not a synthetic fixture.
- **13-symbol backtest** after AAPL passes: run the same computation
  against the other 13 symbols' historical daily bars, specifically
  checking for false positives (a symbol correctly trending for weeks
  gets mislabeled `reversal_*` on ordinary noise) and false negatives
  (a real multi-week reversal never flagged) — before this touches
  `shadow` mode in production, let alone `live`.

### 7.7 Open parameters — decide during/after the 13-symbol backtest, not now

- Crossover pair: default to 10/50 (matches the external read
  verbatim); 20/50 is the fallback if 10/50 proves too noisy across the
  13-symbol set.
- `days_since_crossover` freshness threshold for `reversal_*` vs
  `trending_*` (proposed default: 3 days).
- RSI overbought/oversold thresholds (proposed default: classic 70/30).

### 7.8 Non-goals

Not a replacement for `RegimeHMM`. Not a general TA-indicator library.
Does not touch win/loss/PnL (same separation `macro_filter.py` already
draws from `calibration_bias_bucket.py`/`macro_edge.py`). Not building
any of this now — scope only, per "scope in worklog, get honest
review, build only on explicit instruction."

## 8. Trend filter — built (2026-09-10, explicit approval: "build macro filter refinement")

> **Priority**: P1 · **Task**: Build the medium-term trend-confirmation
> module scoped in §7 · **Status**: resolved (built, tested, real-data
> validated locally; deploy verification tracked separately) ·
> **Date**: 2026-09-10 · **Roadmap refs**: RM-40, RM-44

Built exactly to §7's spec, no scope drift:

- **`regime_platform/sweep_orchestrator/trend_filter.py`** (new) —
  `compute_trend_regime()` (pure, SMA-10/20/50 + RSI-14 via
  `services/hmm/model.py::_compute_rsi`, crossover-recency derived
  directly from the closes array — no `prev_*` continuity argument
  needed, unlike `compute_macro_regime`'s EMA), `kelly_trend_multiplier()`
  (same `[0.25, 1.5]` clamp band as `kelly_macro_multiplier`),
  `refresh_trend_regime()` / `get_trend_regime()` (own daily TradingView
  batch fetch — fetch-sharing with `macro_filter` deliberately deferred,
  per §7.5).
- **`nt_trend_regime`** (new table, migration `add_nt_trend_regime`) —
  one row per symbol, mirrors `nt_macro_regime`'s shape.
  `nt_signal_sim.trend_label`/`trend_strength` (new columns, same
  migration) — captured regardless of mode, same nullable-when-off
  pattern as `macro_label`/`macro_strength`.
- **`trend_regime_mode`** (`config/feature_flags.yaml` +
  `feature_flags.py`) — `off` (shipped default) / `shadow` / `live`,
  plus a `trend` sub-config (window days, RSI thresholds, freshness
  days, boost/penalty scale) — all in the committed YAML, no new
  `os.getenv()` flag, per [[config_centralization_preference]].
- **Wiring** — mirrors `macro_filter`'s exact integration shape in both
  sweep paths: `orchestrator.py::_process_symbol` (light sweep) and
  `heavy_sweep.py::run_heavy_sweep_for_symbol` (heavy sweep) each read
  `nt_trend_regime`, capture `trend_label`/`trend_strength` to
  `nt_signal_sim` whenever the signal is qualifiable, and apply
  `kelly_trend_multiplier()` only when `trend_regime_mode=live`.
  `_build_signal_sim_payload` (shared by both paths) takes the two new
  fields. A new daily job (`trend-regime-daily`, `10 6 * * *` — right
  after `macro-regime-daily`, inside the same Studio-restart-safe
  05:16-09:20 UTC window as §6's harmonization work) is registered
  **unconditionally**, same as `macro-regime-daily` — deliberately NOT
  gated on `trend_regime_mode` at registration time, since
  `feature_flags.yaml` is meant to hot-reload with no restart required;
  gating registration on the mode would silently need a restart to pick
  up an `off`→`shadow` flip, which would have broken that guarantee.
  (Caught and fixed during this build, not after — the first draft did
  gate registration on the mode.)

**Verified two ways**:
1. 16 new unit tests (`tests/test_trend_filter.py`) — pure-function
   coverage of `compute_trend_regime`/`kelly_trend_multiplier` against
   hand-constructed deterministic price series (flat, established
   uptrend/downtrend, a fresh crossover in either direction 1-2 days
   post-flip, strength saturation, the clamp band) — same
   no-mocked-Supabase precedent as `test_macro_filter.py` (its own
   refresh/get functions are diagnostics, not unit-tested either).
2. **Real AAPL data** (local `.env.local` TradingView credentials,
   120 real daily closes fetched live): `compute_trend_regime` returned
   `trend_label: consolidating`, `rsi_state: neutral` (`rsi_14: 56.05`),
   `last_crossover_direction: bullish`, `days_since_crossover: 4`
   (past the 3-day freshness window — correctly NOT still
   `reversal_up`) — this is the *exact* target behavior §7.2 specified
   for the 2026-09-10 read, independently matching the user's own
   external technical analysis (10dma/50dma bullish cross 08-28,
   RSI ~50.2 "cooled off... cleanly consolidating") against real,
   independently-fetched data, not a synthetic fixture built to pass.

831 passed, 21 skipped, 0 failures (full suite, +16 from this build).
Shipped `off` by default — no live behavior change until `shadow` (then
`live`) is explicitly turned on in `config/feature_flags.yaml`, same
rollout discipline as `macro_regime_mode`. The 13-symbol backtest (§7.6)
has not been run — that was scoped as a pre-`shadow` gate, not a
pre-merge one, and doing it properly means pulling real daily history
for all 14 symbols and reviewing false positive/negative rates by hand,
which is its own piece of work, not a rubber stamp before flipping the
flag. Not deployed to Studio yet (code-complete vs. deployed gate, same
as every other build this session) — tracked as RM-44.

## 9. 14-symbol backtest — real result, NOT a clean pass (2026-09-10)

> **Priority**: P1 · **Task**: Run §7.6's pre-shadow backtest against
> all 14 symbols' real historical data · **Status**: resolved
> (backtest run, result is a genuine caution flag — do not read as
> "validated, safe to flip to shadow") · **Date**: 2026-09-10 ·
> **Roadmap refs**: RM-40

Studio deployed (`RM-44`'s subject) and the user asked for the
13-symbol backtest to be run. Fetched real daily closes for all 14
active symbols (`.env.local` TradingView creds) and ran
`compute_trend_regime` in a rolling walk-forward over the trailing 90
days per symbol (not just today's single read), counting label
frequency and checking a 5-trading-day forward-return directional
hit-rate for every `reversal_up`/`reversal_down` event.

**Frequency — sane, not miscalibrated.** No symbol fires `reversal_*`
more than 6-9 times in 90 days; `consolidating` dominates (50-76/90
days for most symbols). This rules out the "hair-trigger, flags a
reversal every few days" failure mode.

**Directional edge at 5 days — weak, and highly inconsistent per
symbol.** Aggregate hit rate across all 75 reversal events, all 14
symbols: **39/75 = 52%** — barely above a coin flip. Per-symbol rates
range from **0%** (USDCHF, 0/6 — every flagged reversal for this symbol
went the wrong way) to **100%** (EURUSD 6/6, USDCAD 6/6 — but on small
samples, 6 events each, not strong evidence either). This is real data,
not a synthetic construction, and it does not support treating this
module's `reversal_*` label as a standalone predictive edge at a 5-day
horizon.

**Read this correctly — it is not necessarily a bug.** A 10/50-day SMA
crossover is a classic *lagging* confirmation signal by construction —
by the time it fires, price has typically already moved partway through
the reversal, so testing "does price keep moving the same direction for
5 MORE days after confirmation" is a genuinely hard bar, not obviously
the right validation horizon. This result doesn't prove the feature is
useless; it means the naive "is this a standalone directional edge"
framing this backtest checked is NOT supported by real data at 5 days,
and needs one of: a longer forward horizon, a different crossover pair
(10/20 vs 10/50 vs 20/50 — flagged as an open parameter in §7.7 for
exactly this reason), or accepting the label as a coarse *contextual*
input (paired with RSI state, or blended with macro_filter's own read)
rather than a lone signal.

**What this backtest did NOT check** (a real gap, not glossed over):
false negatives — a genuine multi-week reversal that never got flagged
— require actual chart-level review per symbol, not an automated
count; not done here. §7.6's full validation is therefore still
partial, not complete.

**Recommendation, not a decision made unilaterally**: do NOT flip
`trend_regime_mode` to `shadow` on the strength of this result alone.
Worth deciding explicitly: (a) try a different crossover pair/horizon
and re-run this exact backtest before reconsidering, (b) proceed to
`shadow` anyway treating `trend_label` as a passive, non-authoritative
context field (no different in kind from capturing `macro_label` today
— shadow mode itself makes no Kelly claim), or (c) hold this module at
`off` pending further tuning. Not decided here — this section reports
the real result and the tradeoff, not a chosen path.

## 10. Phase 2, detector 1 — regime-vs-price mismatch, built and validated (2026-09-12, explicit approval: "can we start to build phase 2")

> **Priority**: P1 · **Task**: Build detector 1 of §2's three candidate
> shapes (regime-implied-direction vs realized-price-sign mismatch) ·
> **Status**: resolved (built, tested, validated against real AAPL
> history, backtested against all 14 symbols per the approved
> methodology) · **Date**: 2026-09-12 · **Roadmap refs**: RM-37

**A real design bug caught during validation, not before shipping.**
`regime_platform/services/self_learning/regime_price_mismatch.py`'s
first draft compared a candidate run's price peak only to its FINAL
day. That version correctly flagged a hand-picked 9-day AAPL fixture
(09-01..09-09) — but when run against AAPL's REAL, full daily history
(pulled fresh from `nt_sweep_result`), it found nothing. Diagnosis: the
regime's actual bull-family run is 14 days long (08-28..09-10), not 9
— it doesn't break until 09-11. By its true last day (09-10), price had
already partially recovered to $325.95, only -0.65% off the $328.07
peak — hiding the real -3.85% mismatch that existed one day earlier
(09-09, $315.45, the exact day this session originally diagnosed by
hand). **A peak-vs-final-day design structurally cannot catch a
transient mismatch that recovers before the regime label itself
changes — exactly the failure mode this detector exists for.**
Corrected to track the worst point reached at ANY day within the
qualifying run, not just its last one.

**Verified against the corrected version**:
- Real AAPL history (21 days, `nt_sweep_result`, pulled fresh): flags
  the mismatch at exactly `08-28..09-10 (worst=09-09) move=-3.85%` —
  the precise day and magnitude this session's original hand
  investigation found (§1).
- 14 new unit tests, including the full real AAPL history as a
  regression fixture (not the misleadingly-easy 9-day one) and a
  synthetic test specifically for "a transient mismatch that recovers
  before the run ends must still be caught."
- **14-symbol backtest** (direct Supabase REST pulls, ~60k raw rows,
  aggregated to daily — not committed, matches the trend-filter
  backtest's own real-data-not-a-script-in-the-repo precedent): **5/14
  symbols flagged** in the last 21 days — AAPL plus the entire gold/
  silver family (XAGUSD, XAUAUD, XAUEUR, XAUUSD), all in a tighter
  window (3-6 day runs, 2.5-4.2% moves). Forex pairs and BTCUSD/
  US30USD/US500 showed none. Worth noting, not over-claiming: the
  gold-family cluster landing in the SAME calendar window (08-27..
  08-30) suggests a possible shared cause (a real, brief volatility
  event affecting the whole complex) rather than 4 independently
  broken classifiers — plausible, not confirmed. This also lines up
  with this session's separate, already-known finding that gold/silver
  pairs read calibration-OVERCONFIDENT (§6 of the pulse-check skill) —
  a related but distinct symptom, not verified as the same root cause
  here.

877 tests passing (863 baseline + 14 new), 0 regressions.

**Deliberately not built yet**: detectors 2 (dead-mechanism detection)
and 3 (redundant-computation detection) from §2's original three-shape
scope. Not wired into any scheduled job or live path — Phase 2 stays
diagnostic-only per the 4-phase design; there is no Phase 3 (LLM-
assisted triage) built yet to consume this detector's output, so for
now it's a tested, validated function a human (or a future session)
can run on demand, not an automated nightly check.

## 11. Phase 2 complete — detectors 2+3 built, all three wired into a nightly job, a real bug caught on first run (2026-09-12, explicit approval: "finish off detectors 2+3 then wire into a scheduled job")

> **Priority**: P0 · **Task**: Build detectors 2+3, wire all three
> Phase 2 detectors into a nightly scheduled job · **Status**: resolved
> (built, tested, deployed; caught and fixed a real, live production
> bug on the very first real run) · **Date**: 2026-09-12 · **Roadmap
> refs**: RM-37

### 11.1 Detector 2 — dead-mechanism detection

`dead_mechanism_detector.py`: `detect_dead_mechanism(mechanism_name,
mode, last_write_at, now, max_staleness)` — a mode-gated mechanism
whose table has never been written, or gone stale beyond an expected
cadence, gets flagged; `off` mechanisms are never flagged (nothing is
expected to be writing). 7 new tests.

### 11.2 Detector 3 — redundant-computation detection

`redundant_computation_detector.py`: `detect_redundant_computation`
reuses this codebase's OWN existing shadow-compare tables
(`nt_markov_source_shadow`, `nt_regime_debounce_shadow`,
`nt_heavy_sweep_atr_anchored_shadow`,
`nt_heavy_sweep_deadline_aware_shadow`,
`nt_heavy_sweep_reachability_shadow` — all already built to answer
"do these two mechanisms differ" for a shadow→live decision) and asks
the opposite question: if two computations agree almost every time on
real data, one may be redundant. This is the exact shape of the
2026-09-09 `p_historical`/`apply_calibration_adjustment` consolidation
that was done by hand — this detector would have flagged that pair
automatically had it existed then. 6 new tests.

### 11.3 Orchestration + a new table

`run_detectors.py`'s `run_all_detectors()` runs all three against real
Supabase data (detector 1 aggregates `nt_sweep_result` to daily rows
per active symbol; detector 2 walks a registry of 7 mode-gated
mechanisms with a clean table+timestamp shape — `context_alignment_
gate_mode`/`dynamic_min_rr_mode`/`timesfm_k_adaptive_mode` are
excluded, log-only with no dedicated table, a documented limitation
not an oversight; detector 3 walks the 5 existing shadow-compare
tables) and writes findings to a new `nt_self_learning_findings` table
(migration, diagnostic-only). Added a generic, paginated
`fetch_all_rows()`/`insert_self_learning_finding()` to
`supabase_client.py` rather than a near-duplicate one-off fetch per
table — deliberately reusing one function across a diverse set of
tables, the same principle detector 3 itself checks for.

Wired as `self-learning-detectors-nightly` (02:00 UTC, right after
knowledge-index-nightly, same restart-safe Studio window established
in §6), gated by `self_learning_detectors_enabled` (default on, same
simple on/off convention as `knowledge_index_enabled` — no
live-baseline concept for a diagnostic sweep).

### 11.4 A real bug, found on the very first live run

The real smoke test (before wiring the scheduled job) immediately
surfaced a genuine, currently-live production bug: detector 2 flagged
`heavy_sweep_atr_anchored_bricks_mode: shadow/live but its shadow table
has zero rows`. Investigated rather than assumed a false positive —
root cause: **YAML 1.1 parses the unquoted bareword `off` as the
Python boolean `False`, not the string `"off"`.**
`config/feature_flags.yaml` had `heavy_sweep_atr_anchored_bricks_mode:
off` (unquoted). `get_heavy_sweep_atr_anchored_bricks_mode()`'s
`str(value).lower()` then returned `"false"`, not `"off"`.

`heavy_sweep.py`'s own usage has two DIFFERENT check styles for the
same variable:
- Candidate generation (line 716): `if atr_anchored_mode != "off":` —
  **permissive** — `"false" != "off"` is `True`, so this branch ran.
  ATR-anchored brick-size candidates were being silently added to
  `all_brick_sizes` on EVERY heavy sweep, despite the user having
  configured this off, and were eligible for `pick_best_combo()` to
  select as the live geometry.
- Shadow/live application (lines 818/833): `atr_anchored_mode in
  ("shadow", "live")` / `== "live"` — **strict** — both correctly
  `False` for `"false"`, so the shadow table was never written and no
  live multiplier was ever applied.

Net effect: the *generation* half of this feature was silently active
in production while the *recording/application* half correctly stayed
inert — exactly why the table looked dead (matching detector 2's
finding) while the actual behavior was NOT fully off (a second,
independent problem the dead-mechanism framing alone wouldn't have
revealed without reading the actual code, which was done before
concluding anything).

**Fixed**: quoted the value —
`heavy_sweep_atr_anchored_bricks_mode: "off"`. Checked every other
mode flag in the file exhaustively (not just this one) via
`yaml.safe_load` + a type check: `regime_warm_start_mode` had the
identical unquoted-bareword issue, but confirmed ZERO live effect —
every one of its usage sites checks `== "live"` specifically, which is
correctly `False` for `"false"` too. Quoted anyway for correctness.
`sweep_after_hours`/`knowledge_index_enabled` are genuine booleans
(correct as-is, not a bug). No other flags affected.

### 11.5 Verification

Real smoke test against live Supabase data (before wiring the job):
`{'regime_price_mismatch': 5, 'dead_mechanism': 1, 'redundant_computation': 0, 'errors': 0}`
— the 5 regime-price-mismatch findings exactly match §10's earlier
14-symbol backtest (AAPL + the gold/silver family), confirming
consistency between the standalone validation and the orchestrated
run. The 1 dead-mechanism finding was the real bug above, now fixed.
15 new tests (detector 2, detector 3, orchestration wiring), 894 total
passing, 0 regressions.

### 11.6 Also resolved this session: the deadline-aware-scoring "was this validated" question

Asked directly whether Part 5's citation of `scripts/
sim_deadline_aware_scoring.py` (worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md line
326-329, "10 expired -> 0 expired, one variable changed") was ever
validated, and whether the fix retroactively corrected historical
records. Checked with fresh real data rather than trusting the
citation: **yes, validated, and more strongly confirmed than the
original multi-window backfill predicted.** Split correctly by signal
ENTRY time (not resolution time — resolution can lag entry by up to
48h, which would misattribute pre-fix trades to the "after" cohort):
9 of 14 symbols show **0.0% expired** for signals created since the
2026-09-08 18:14 UTC deploy (down from before-rates of 1.5%-100%); the
remaining 5 improved substantially but not to zero. **No, historical
records are not and should not be retroactively rewritten** — a
signal's outcome reflects whichever brick geometry was actually live
when it was created; overwriting that would corrupt the audit trail.
The improvement applies only to new signals going forward, which the
data confirms cleanly. `nt_roadmap`
(`heavy_sweep_deadline_aware_scoring_mode`) updated with this real
reconfirmation.
