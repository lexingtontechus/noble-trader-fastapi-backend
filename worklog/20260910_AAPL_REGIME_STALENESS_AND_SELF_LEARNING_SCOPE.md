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

## 3. Revision (same day) — Layer 2's trigger mechanism, DB placement, and a redesigned Phase 1

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
