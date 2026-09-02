# 2026-08-31 — TimesFM 2.5 → 3.0 upgrade scope, and the "5-minute short
# horizon" impact on the EV engine (scope only, not built)

Scope per instruction: evaluate the TimesFM 2.5 → 3.0 upgrade, specifically
whether the "5-minute short horizon" changes anything for the EV engine's
macro filter / market-trend components, and whether TimesFM needs a "prior
state" lookback. Investigated via two parallel read-only agents over
`regime_platform/` + `server.py` + `orchestrator.py`, cross-checked against
[TimesFM-3's announcement](https://research.google/blog/timesfm-3-a-zero-shot-foundation-model-for-multivariate-forecasting/).
Nothing below is built. No proposal here touches `compute_ev`'s blend
formula itself, per the standing scope boundary on that function.

## Correcting the framing: there is no 5-minute *data* horizon today

Before answering the actual question, one premise needs correcting: **"5
minutes" in this codebase is the sweep's polling cadence, not a candle
timeframe anything is modeled on.** The light sweep cron runs `*/5 * * * *`
(`orchestrator.py:974`), but every input that cron tick reads is **hourly**
bars — `LIGHT_CANDLE_BARS=200`, `interval="1h"` default
(`price_fetcher.py:31-33,55-56`; call site `orchestrator.py:1730`,
`fetch_tradingview_batch(active, 200)`, only 2 positional args so interval
stays at its default). `price_fetcher.py:99`'s `interval_map` supports
`"5m"`/`"15m"` TradingView codes, but **no call site in the signal path
ever passes them** — it's unused capability for this purpose. So:

- TimesFM's own context: up to the last 500 of those hourly bars (capped
  further by `max_context=1024` server-side, `server.py:56,81`), horizon
  fixed at 1 (next *hour*, not next 5 minutes — `orchestrator.py:314-319`).
- Trend HMM (`RegimeHMM`, `regime_engine.py`): same 200-hourly-bar array,
  refit from scratch every tick, `min_bars=80` (`regime_engine.py:288-293`).
- Macro filter (`macro_filter.py`): ~300 **daily** closes (200-day SMA +
  100 buffer), refreshed once a day, independent of the 5-min cron
  entirely (`macro_filter.py:166-169`).

So "does the short (5min) horizon change the macro filter / market trend
picture" has a direct answer: **no candle-level 5-minute horizon exists to
change** — the 5-minute cadence just means the same hourly-bar EV blend
gets *re-evaluated* more often, not that it's sampled more finely. If a
genuine sub-hourly signal is wanted, that's an independent data-plumbing
change (fetch `5m`/`15m` candles somewhere in the pipeline) — orthogonal to
2.5→3.0 and not implied by it. Flagging this now so the rest of the scope
below isn't built on a false premise.

## Current TimesFM 2.5 integration, as it actually is

- **Separate HTTP inference server, not a library import in the trading
  path.** `server.py` (repo root) is the only file that `import timesfm`s
  (`server.py:38`) and loads `timesfm.TimesFM_2p5_200M_torch.from_pretrained(...)`
  (`server.py:78`). The sweep orchestrator only ever reaches it over HTTP —
  `_fetch_timesfm_cached()` (`orchestrator.py:239-467`) tries
  `TIMESFM_INFERENCE_URL`/`TIMESFM_URL` → gateway `:8000` → direct `:8002`
  fallback, `httpx.AsyncClient`, 10s timeout. `heavy_sweep.py:399-434`
  reuses the same function.
- **Genuinely univariate, one symbol per call**, despite the request
  schema (`ForecastRequest.inputs: list[list[float]]`, `server.py:203`)
  technically allowing a batch: `orchestrator.py:304-321` builds
  `{"horizon": 1, "inputs": [price_samples]}` — a length-1 outer list —
  inside a `for sym in symbols_to_process` loop with no cross-symbol
  batching anywhere.
- **Output usage**: only `point_forecast[0][0]` is read
  (`orchestrator.py:411-419`); the server's `quantile_forecast` field
  (`server.py:223`) is fetched but **discarded by every caller today.**
- **Where it plugs into the EV pipeline** — field name `p_timesfm`,
  unchanged end to end: one of 4 logit-pooled sources in
  `EVEngine.compute_ev()` at nominal weight 0.25 (`ev_engine.py:89-94`,
  alongside `p_historical` 0.30, `p_regime` 0.25, `p_markov_hold_n` 0.20).
  Independently of the blend weight, TimesFM confidence
  (`abs(p_timesfm - 0.5)`) also drives a **Kelly-sizing multiplier**
  (1.2× / 1.0× / 0.6×, `orchestrator.py:2676-2685`) and feeds
  `EVEngine.compute_tail_risk()`'s extreme-forecast skip/reduce logic
  (`ev_engine.py:913-941`).
- **`TIMESFM_GATE_MODE`** (plain env var, default `soft` —
  `orchestrator.py:143`; note `.env.example` contradicts itself on the
  default at lines 144 vs 270, and `README.md:619` is stale — side finding,
  not fixed here): `hard` = binary veto gate; `soft` (live default) = no
  veto, just dynamically shrinks TimesFM's blend weight when confidence is
  low; `off` = full nominal weight, no discount. All three still feed the
  Kelly multiplier and tail-risk paths regardless of gate mode.

## What TimesFM 3.0 actually changes

Per the announcement: native **multivariate + covariate support**
(TimesFM 2.5 was "strictly limited to univariate"), an alternating
causal-temporal / full-variate attention architecture, **non-autoregressive
single-forward-pass decoding** (vs. iterative patch-by-patch generation),
**9-quantile probabilistic output** (10th–90th percentile), 330M params
trained on 1T+ time points. The blog does not state a changed context
length, and says nothing 5-minute/short-horizon-specific.

Mapped onto what's actually in this codebase:

1. **Multivariate/covariate support has no plumbing to attach to today.**
   Every call site sends one symbol's own series; there is no covariate
   concept anywhere in the TimesFM request path. The closest *existing*
   multi-asset component is `correlation_regime.py`'s
   `CorrelationRegimeDetector` (rolling cross-asset correlation matrix →
   4-state HMM), but it's wired only to a standalone portfolio-sizing API
   (`routers/multi_asset.py`), **not** to the live sweep or `compute_ev` —
   confirmed no reference to it in `orchestrator.py`. Adopting 3.0's
   multivariate capability for real would mean redesigning TimesFM's input
   construction (e.g. feeding the macro-trend signal or correlated gold
   pairs as covariates) — a materially bigger change than a model-version
   bump, and a separate decision from the upgrade itself.
2. **Non-autoregressive decoding is a latency win with a concrete existing
   pain point to point at**: today's call is synchronous, sequential
   per-symbol, inside a 10s-timeout HTTP round trip, on a 5-minute-cadence
   cron with a 15-minute cache TTL (`_TIMESFM_CACHE`,
   `orchestrator.py:236-268`) already in place specifically to avoid
   re-querying every tick. A faster single-forward-pass server response
   reduces per-symbol latency in that loop; it doesn't remove the need for
   the cache (cadence vs. TTL is an unrelated tradeoff) but lowers the risk
   of the loop backing up when many symbols are due at once.
3. **9-quantile output is additive, not currently consumed** — the
   response schema already has an (unused) `quantile_forecast` field
   today. If 2.5's version also returns quantiles (unconfirmed — the blog
   doesn't detail 2.5's probabilistic output), this may already be latent
   capability; 3.0 at minimum guarantees it. This is the one piece of 3.0
   most directly relevant to the existing `TIMESFM_GATE_MODE`/tail-risk
   logic: today's "confidence" is a hand-rolled proxy
   (`abs(p_timesfm - 0.5)`) computed from a single point forecast plus
   whatever the server separately reports; genuine quantile spread (e.g.
   p90 − p10) is a more direct confidence signal than that proxy, and
   would slot into the same `timesfm_confidence`/gate/Kelly-multiplier/
   tail-risk call sites without changing their shape — just their input.
   Not proposing that swap here, only noting the fit.

## Does TimesFM need a "prior state" lookback?

**No — and adding one wouldn't be consistent with what "state" means
elsewhere in this pipeline.** Two distinct things already exist under that
name, and TimesFM resembles neither today, nor would 3.0 change that:

- **Genuine cross-tick memory** in this codebase means engineered
  smoothing/aggregation bolted onto stateless refits: per-symbol EMAs
  (`self._regime_trend_ema`, `self._markov_ema`, `REGIME_EMA_ALPHA`/
  `MARKOV_EMA_ALPHA` = 0.30, `orchestrator.py:826-844`), regime-label
  continuity means fed forward into the next HMM refit
  (`self._regime_label_means`, `orchestrator.py:833-843`,
  `regime_engine.py:814-880`), a persisted-to-disk legacy 5-state HMM
  object (`HmmModel._models`, `model.py:94-98,300-359`), and a
  DB-persisted rolling-7-day win rate feeding `p_historical`
  (`calibration_enforcement.py:132-183`). All of these carry *something*
  computed from history forward across calls.
- **TimesFM today carries none of that.** Each (non-cached) call is a pure
  function of the raw price window handed to it — no hidden state, no
  fed-forward posterior, nothing persisted between calls except the 15-min
  TTL memoization cache, which is a cost-saving measure, not model state.
- **TimesFM 3.0 doesn't change this shape.** Its architecture (alternating
  attention over patches, non-autoregressive decoding) is still a
  feed-forward model whose only "memory" is whatever's inside the context
  window passed in on a given call — same as 2.5, just attending across
  variates within that one call instead of only across time. Nothing in
  the announcement introduces a recurrent/persisted hidden state across
  calls the way the HMM/Markov layers have.
- So the honest reframe of "does it need a prior-state lookback" is: **the
  context window *is* its state, same as today** — the only lever that
  changes anything is how much history is in that window per call (today:
  ≤500 hourly bars, ~20 days), not whether TimesFM remembers past calls.
  Whether that window should be wider, or should include a finer (5m/15m)
  timeframe, is a data-plumbing decision independent of the 2.5→3.0
  version bump — see the framing correction above.

## Proposed scope, if this proceeds

**Explicitly NOT proposed**: no change to `compute_ev`'s blend formula or
weights, no change to `TIMESFM_GATE_MODE`'s hard/soft/off semantics, no
change to `macro_filter.py`/`regime_engine.py`/`correlation_regime.py`, no
new 5-minute (or other sub-hourly) candle feed. All of that is either
out of scope for this line of work or a separate decision this doc
deliberately doesn't make.

1. **Phase A (server-only, shadow-verified)**: since every caller only
   ever talks HTTP to `server.py` on a fixed request/response contract,
   the upgrade is isolated there — swap the model class/checkpoint id,
   update `ForecastConfig`, confirm the response shape
   (`point_forecast`/`quantile_forecast`) is unchanged or additive. Verify
   by running both versions side by side (e.g. a second port, or a
   version-selector flag) and comparing `point_forecast` divergence on
   live symbols for some period before switching over — same shadow
   discipline as `MARKOV_SOURCE_MODE`/`macro_regime_mode`. Following the
   existing config-centralization precedent (`macro_regime_mode`/
   `markov_source_mode`/`trend_probs_gate_mode` already live in
   `config/feature_flags.yaml`, not env vars — `TIMESFM_GATE_MODE` is the
   one holdout, still a plain env var), any new mode toggle this needs
   (e.g. `timesfm_model_version_mode: off|shadow|live`) belongs in that
   YAML, not a new env var.
2. **Phase B (only after Phase A is reviewed)**: decide whether to spend
   the (materially larger) effort wiring multivariate/covariate input —
   e.g. feeding the macro-trend signal or correlated symbols directly into
   TimesFM's context instead of/alongside the separate `macro_filter.py`/
   `correlation_regime.py` modules. Fully scoped below (§Phase B).

## Phase A build attempt (2026-08-31) — paused on a license blocker

Attempted to build Phase A per instruction (download + pin the model into
the studio's `hf_cache/hub`). Stopped before the actual download —
everything else below is verified, real-environment findings, not
build-blocked speculation.

**License**: verified directly against `google-research/timesfm`'s current
`README.md` (last touched 2026-08-28, same-day commits as the 3.0
announcement — not stale docs): *"model weights up to version 2.5 remain
Apache-2.0. However, for the time being, TimesFM 3.0 pretrained weights
are distributed under the separate `timesfm-non-commercial-license-v1.0`
license and are restricted to non-commercial, non-production use...
Commercial or production use of the default pretrained weights is not
permitted."* Cross-checked against the HF model card for
`google/timesfm-3.0-pytorch` (license tag: the non-commercial license,
vs. `apache-2.0` on `google/timesfm-2.5-200m-pytorch`) — consistent. Also
checked the two paths flagged for this review
(`timesfm3-usage/benchmarks/fev_bench`,
`timesfm3-usage/notebooks/promotion_and_synthetic_parity_demo.ipynb`) and
the repo's recent commit history for evidence the weights license has
since moved to Apache-2.0 — found none; the same 2026-08-28 commit batch
that added Apache-2.0 *code* headers to the new `timesfm3-usage` files is
the same batch that reaffirmed the weights restriction, and "for the time
being" reads as an announced future intent, not a completed change. **This
repo's live orchestrator is a production trading system** — every call
into TimesFM feeds real (paper) trading decisions on a continuous
schedule — which is exactly the use the current license prohibits. Not
downloading the weights into `hf_cache/hub` (or anywhere) until this is
resolved one way or the other; flagged to the user directly rather than
guessing past it.

**Fork status** (`lexingtontechus/noble-trader-timesfm`, the editable
install backing `server.py`'s `import timesfm`): pinned at `v2.0.1` /
commit `0d8a85c "Launch to Production"`, tracking
`upstream/master` = `google-research/timesfm`. No `TimesFM3Evaluator`/3.0
code exists in the current checkout. Upstream has since cut a `v3.0.0` tag
(23 commits past `v2.0.2`, adding — per the tag-compare summary — "full
torch implementation with forecaster, evaluator, and benchmarking suite"
plus HF Hub integration and the Apache-2.0 code headers). **The fork needs
updating regardless of the license outcome** — merge/rebase onto
`upstream v3.0.0` (or cherry-pick the relevant commits) before
`TimesFM3Evaluator`/`ModelConfig` exist anywhere the studio can import
them. This is a real, separate piece of work from the license question —
worth doing even if the license question stays "wait," since eval/
benchmarking against 3.0 in a non-production context is unaffected by
the non-commercial restriction.

**Environment, verified live on the studio (`noble-trader-unified` via
SSH)**:
- No GPU (`torch.cuda.is_available() == False`, no `nvidia-smi` binary) —
  TimesFM 2.5 runs on CPU today. Relevant to Phase A's own shadow-verify
  step (running 2.5 and 3.0 side by side): two ~200-330M-param CPU models
  loaded simultaneously is plausible on this box (387GB disk, 883MB
  currently used by the 2.5 checkpoint) but adds real per-request CPU
  latency during the comparison window — worth sizing before flipping
  shadow mode on, not assumed free.
- `HF_HOME=/teamspace/studios/this_studio/hf_cache` (matches the path the
  user named), populated hub cache confirmed at
  `.../hf_cache/hub/models--google--timesfm-2.5-200m-pytorch` (883MB).
  Network egress to `huggingface.co` confirmed working (`HTTP 200`).
- **`HF_HUB_OFFLINE=1`** is set via
  `/teamspace/studios/this_studio/.ssh/environment` — separate from
  `.bashrc` (which only sets `HF_HOME`) and not visible by just reading
  the dotfiles. This is the same "invisible platform-level override"
  failure mode as the `MARKOV_SOURCE_MODE` Studio Secret incident (see
  [[config_centralization_preference]]) — before any future download
  attempt, check whether this also applies to the actual running
  `server.py` process's environment (`/proc/<pid>/environ`, PID found live
  at time of writing: `2765`), not just interactive SSH sessions, since an
  offline-mode override would silently no-op a download that looks like
  it should work.

**Net**: Phase A is ready to execute mechanically (fork update path
identified, cache path confirmed, network path confirmed) the moment the
license question is resolved. Nothing was downloaded, pinned, or changed
on the studio or in this repo.

## Phase B — full scope (2026-08-31)

Goal per instruction: fully scope (not build) wiring TimesFM 3.0's
multivariate/covariate support into the platform, evaluating whether it
should replace or augment the two existing components that currently do
similar broader-than-one-symbol work: `macro_filter.py` (200-day SMA
regime, Kelly-multiplier only) and `correlation_regime.py` (cross-asset
correlation HMM, portfolio-sizing API only, not wired to the live sweep —
see the earlier research section above).

### Standing constraint this scope is designed around

Per [[ev_engine_no_rework]], `compute_ev`'s blend formula/weights are not
up for rework. Every option below is therefore scoped to change only
**how `p_timesfm` itself gets computed** (upstream of `compute_ev`) —
`p_timesfm` keeps entering the blend exactly as it does today: one float,
weight 0.25, same call site (`ev_engine.py:89-94`, `orchestrator.py:2252-2264`).
Nothing here proposes retiring `p_regime`/`p_markov_hold_n` as separate
blend inputs or changing how many sources `compute_ev` blends.

### What 3.0's covariate API actually offers (verified against upstream
`google-research/timesfm` README, 2026-08-28)

- Native multi-channel/multivariate targets (jointly forecast several
  series in one call) plus `past_only_covariates` and
  `past_future_covariates` parameters — the old per-series `frequency`
  indicator is removed.
- Context length up to 16k tokens (vs. 2048 previously) — today's caller
  sends ≤500 bars, so this is unused headroom either way, not a forcing
  function to change anything.

### Candidate covariate designs (three, not mutually exclusive)

1. **Macro score as a past-only covariate.** Feed `macro_score`/
   `macro_score_smoothed` (`macro_filter.py:61-111`) alongside the
   symbol's own price series into the same TimesFM call, instead of (or
   alongside) its current life as an independent Kelly-multiplier read at
   `orchestrator.py:2433-2473`. **Tradeoff**: the macro filter refreshes
   once a **day**; TimesFM is called on the 5-min sweep cadence. A
   covariate needs a defined value on every call, so this requires either
   forward-filling yesterday's macro score across all intraday calls
   (cheap, but means the "covariate" is stale for up to 24h by
   construction) or refactoring the macro refresh cadence to match — the
   latter is a bigger, separate change this scope does not recommend.
   Forward-fill is the only option that doesn't touch `macro_filter.py`'s
   own cadence.
2. **Correlated symbols as joint multivariate targets.** Batch related
   symbols (e.g. the three gold pairs XAUUSD/XAUEUR/XAUAUD, which the
   Phase-1/2 calibration-reframe worklog already found share a
   reachability/expectancy fingerprint) into one multi-channel TimesFM
   call instead of the current per-symbol loop
   (`orchestrator.py:1799-1813`, one series per call today). This is the
   most direct analog to `correlation_regime.py`'s own "stack returns from
   N assets" shape, but genuinely new plumbing, not a config change:
   requires (a) grouping logic — which symbols batch together, and
   whether that grouping is static (a hardcoded gold-pair group) or
   dynamic (reuse `correlation_regime.py`'s own correlation-regime output
   to decide, which would be the first time that module feeds the live
   sweep at all); (b) timestamp alignment across symbols before batching,
   since `fetch_tradingview_batch` calls are currently independent
   per-symbol with no guaranteed identical bar timestamps; (c) a
   degraded-batch path for when one symbol in a group has a fetch failure
   or short history — falling back to single-symbol univariate for that
   tick, or dropping the whole batch, are both live options with
   different failure blast radii; (d) the per-symbol 15-minute TTL cache
   (`_TIMESFM_CACHE`, `orchestrator.py:236-268`) is keyed per-symbol
   today — a batched call changes the natural cache key to
   per-symbol-group, meaning one symbol's cache-refresh timing now drags
   its whole group's refresh cadence with it (a cache-invalidation
   coupling that doesn't exist today).
3. **Trend HMM output as a past-future dynamic covariate.** Feed
   `RegimeHMM`'s trend posterior/label into TimesFM's context directly.
   Explicitly the riskiest of the three under the no-rework constraint:
   `p_regime` (derived from the same `RegimeHMM`) is already a separate
   0.25-weight blend input, so this would make TimesFM's forecast
   partially a function of the same signal `compute_ev` blends in
   independently — not a formula change, but a correlation between two
   nominally-independent blend inputs that the logit-pooling math
   (`ev_engine.py:655-664`) assumes are at least partially independent.
   Recommend not pursuing this one without a dedicated review of what
   correlated-input logit-pooling actually does to the blended
   probability — flagged here as a considered-and-deprioritized option,
   not a recommendation.

### Interpretability regression — the real cost, independent of effort

Every design above trades a currently-interpretable, independently-legible
signal (today: `macro_label`/`macro_strength` readable directly off
`nt_signal_sim`; `RegimeHMM`'s trend label readable directly) for a
single opaque `p_timesfm` float that has silently absorbed that
information inside the model's forward pass. This project's own calibration-
reframe work (`worklog/20260825_CALIBRATION_REFRAME_SCOPE.md`) spent three
phases specifically **decomposing** a single opaque "OVERCONFIDENT" bias
number into reachability/directional/magnitude components because a
blended number without component visibility repeatedly hid which of
several distinct failure modes was actually present. Folding macro/trend
signals into TimesFM's input runs the opposite direction — it would make
a future "why did `p_timesfm` do that" investigation strictly harder than
it is today, where `macro_label`, `trend_probs`, and `p_timesfm` are each
independently inspectable. This is a real cost to weigh against whatever
accuracy gain the covariate approach might produce, not just an
engineering-effort tradeoff.

### Explicitly NOT in Phase B

No change to `compute_ev`'s blend weights/formula (standing boundary — see
above). No change to `TIMESFM_GATE_MODE`'s hard/soft/off semantics or the
Kelly-sizing/tail-risk call sites that already consume `p_timesfm` — they
keep consuming whatever `p_timesfm` becomes, unchanged. No retiring of
`macro_filter.py`, `correlation_regime.py`, or `RegimeHMM` regardless of
what a covariate experiment shows — those stay live in parallel; Phase B,
if pursued, is additive experimentation on `p_timesfm`'s own input
construction, not a replacement for the existing components until/unless a
separate, later decision says otherwise.

### Recommended sequencing

Do not start Phase B until Phase A (a clean, license-resolved model swap
plus shadow verification) has shipped and been reviewed on its own — Phase
B's multivariate batching is a materially larger, higher-risk change
(new plumbing, new failure modes, an interpretability cost) that
shouldn't be bundled into or block the comparatively mechanical version
upgrade.

Both phases remain scope-only for anything not already covered by the
explicit Phase A build instruction above — awaiting further instruction
before any Phase B code is written, same discipline as the rest of this
worklog directory.
