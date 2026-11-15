# 2026-11-15 — CRT-1 Build: the engine lands (crt/, dark + paper)

## Context

Operator instruction: "proceed to build CRT-1" (after CRT-0, commit b49e2bc,
and the Task 67 assessment's phased rollout §6). CRT-1's scope (assessment
§6, verbatim): **the engine ships dark — paper**: `crt/` engine +
`crt_mode: shadow` semantics; per-closed-bar loop on TDVA candles; gate
chain with the decision frozen at C2 close; fills →
`nt_paper_positions(source='crt_cont')`; resolution via the CRT resolver
sibling (same sl_first intrabar basis). No `nt_signal_sim` writes, no
resolver changes, MT5 untouched. The shipped default stays **off** —
flipping to shadow is the operator's one-line action once real data exists.

Naming (assessment §4 verdict): `crt/` — a renko-style top-level engine
package, **NOT** a `regime_platform_crt` fork; the orchestrator identity is
the `crt-engine-tick` APScheduler job family inside `SweepOrchestrator.start()`
(the cheapest of the two mounts the assessment weighed).

## 1. What was built

```
crt/                        # the engine (renko-style top-level package)
  __init__.py               # ENGINE/SOURCE_EA provenance + contract doc
  sessions.py               # ET session windows (ZoneInfo DST, inclusive ends)
  candles.py                # closed-bar extraction + M30 = 2x15m aggregation
  atr.py                    # Wilder ATR, MT4 iATR SMA-seed; ATR percentile
  config_loader.py          # CRT-0 artifact (config/crt_cont.json) -> OptionCfg
  setup.py                  # EvaluateArmedAt port: C1/C2/C3 detection + features
  labeler.py                # LabelCustomR port: triple-barrier R labeling
  gates.py                  # SelectTargetOpt port + sizing ladder + risk caps
  monitors.py               # ComputeMonitors/ComputeRolling ports (decay replay)
  paper.py                  # nt_paper_positions writer/resolver (source='crt_cont')
  engine.py                 # SymbolTFEngine: per-closed-bar loop (one MT4 chart)
  jobs.py                   # crt-engine-tick job family + engine registry
  router.py                 # APIRouter(prefix="/crt") — GET /crt/health, /crt/state
config/crt/crt_engine.yaml  # SELF-CONTAINED engine knobs (EA defaults verbatim)
main.py                     # +6 lines: guarded renko-seam mount of crt_router
regime_platform/sweep_orchestrator/orchestrator.py
                            # +13 lines: guarded register_crt_jobs(self) in start()
config/feature_flags.yaml   # comment-only update of the crt_mode block (still off)
```

The decay state machine imports `regime_edge_crt.decay.DecayMonitor` — ONE
source of truth for the CUSUM rules (CRT-0 landed it; the optimizer's replay
and the live engine share it; there is no second port).

## 2. Port fidelity manifest (EA verbatim — the determinism bar)

From `CRT_Auction_Reversal_AIO_EA_v5_CONT.mq4` (v5.00, reviewed line-by-line
in this session):

- **Detection** (`EvaluateArmedAt`, raw/config mode): C1=i−1, C2=i positional;
  `RequireC2CloseInsideFullC1` strict inside, same condition both sides;
  `RejectDoubleSweep`; bull wins ties; ATR(14) at C2 (MT4 SMA-seed Wilder);
  `sweep_atr = depth/ATR`; chop metrics over the 6 bars ending at C1 (wick
  ratio, overlap with the older bar, direction changes) computed but
  unfiltered (raw mode — the option's filters apply in the gate); session
  frozen at C2 close from the C3 boundary time (`c2_open + tf` — the EA's
  then-forming bar); C1-swing recorded (10-bar extreme), never filtered
  (LIQ_C1_ONLY default); vol percentile at C2 over the prior 500 ATRs
  (unknown → mid bit).
- **Labeling** (`LabelCustomR`): trade direction — CONT inverts the setup,
  REV/C3C keep it; SL = C2-opposite-extreme ± sb·ATR; entry ref = C2 sweep
  extreme ± eb·ATR (stop models), C3 close + confirmation (C3C, incl.
  c3_dir); costs: buy fill + slip (+ spread for C3C), SL exit worsened by
  slip, sell exits on ask (bid + spread); **the C3 bar itself is checked
  first for stop models, SL before TP** (the ambiguous-C3 conservative
  tie); then held=1..hold, SL before TP each bar; time exit at the hold
  bar's close; the C3 bar is matched by EXACT open time (a gapped boundary
  stays PENDING forever — the EA's `iBarShift(..., true)` semantics).
- **Gate chain** (`SelectTargetOpt`, frozen once at C2 close): disabled →
  filters (sweep cap → chop → vol mask) → decay OFF → rolling EV; reason
  strings preserved. Sizing ladder (`OptRiskPercent`): 0.25%→0.50% on OOS
  t∈[1,3], CAUTION ×0.5, frozen WITH the decision. Caps
  (`ExecutionAllowed`'s OptMode branches): daily loss stop 2%, open risk
  1% (global across CRT positions), one position per symbol.
- **Monitors** (`ComputeMonitors`/`ComputeRolling`): CUSUM replay over the
  option's resolved labels in c2 order, **after train_end only** (the
  README honesty note); rolling window = 45d wall clock, **no train_end
  filter** (the README's overlap honesty note — a live health check, not a
  validation), counts UNFILTERED labels; the TF pool aggregates ENABLED
  options' labels (the OPTION_THEN_TF fallback basis).
- **Rolling gate** (`RollingPass`, OPTION_THEN_TF): option needs n≥15 AND
  EV>0; a thin option falls back to the pooled TF value; still thin →
  blocked with the sample-size reason.
- The EA's per-chart decomposition maps to one `SymbolTFEngine` per
  (symbol, TF).

Sanctioned translations (documented in each module):

- MQL4 shift arithmetic → ascending closed-bar indices (`shift s` ↔ index
  `len−s`); `iATR` → the SMA-seed Wilder recursion in `crt/atr.py` (the
  `regime_edge` EMA-seed variant differs only in a transient dead by the
  time any setup is evaluated).
- The MQL4 broker-clock/NthSunday DST code is NOT ported — `ZoneInfo
  ("America/New_York")` (assessment §12). Session windows inclusive both
  ends, NY evaluated first.
- The EA's live pending-stop order becomes bar-data fill knowledge at C3
  close: `open_ts` = the C3 close time; unfilled stops "cancel" (no
  emission). A fill window the engine never observed (down through C3) is
  skipped and counted once — the EA would still hold its pending stop, but
  on bar data the knowledge moment passed; honest miss, no backfill.
- Break-even/trailing (`UseBreakEven`/`UseRTrailing`) NOT ported: neither
  the research labels nor the EA's own paper monitors include them (they
  are execution overlays).
- `LIQ_PRIOR_DAY` liquidity mode NOT ported (default is C1_ONLY; the
  artifact's filter space has no liquidity dimension; no D1 fetch).
- Sizing on the stop-entry REFERENCE level (EA `CalcLots(g.stopEntry, g_SL)`)
  — the row's `entry_price` remains the honest slipped fill; size_units =
  cash_risk / |entry_ref − sl|; `realized_pnl = r × cash_risk`.

## 3. The integration seams used (Task 67 §3, as mapped)

- Candles: `fetch_candles_for_symbols` (TDVA, `ohlcv_out` rows) — the ONLY
  data seam; M30 derived from 2×15m (the interval map has no 30m key); bar
  `t` read as the bar's OPEN time (TradingView history convention — the
  repo's own price_fetcher documents that no downstream consumer has ever
  verified this against a live response; the operator's M30-vs-MT4 overlap
  check doubles as that verification gate).
- Paper book: `nt_paper_positions` via `supabase_client._insert_row` +
  paper_book's PATCH pattern — but CRT owns its rows end to end
  (`crt/paper.py`): self-minted `signal_id` (no `nt_signal_sim` sibling by
  design — the shared book's open/close passes join on that table and are
  NOT bent), `source='crt_cont'`, `venue='crt:{tf}:{SESSION}'` as the
  zero-migration hydration key (direction + symbol recover the option;
  ambiguity resolves toward CONT, TP distance cross-checks the option's R).
- Flags: `crt_mode` (off|shadow|live) — registered since CRT-0, read per
  fire; `off` = the tick returns before any fetch or DB touch (byte-dark).
  `live` runs paper semantics with a mode note until CRT-3 (no live
  execution path exists).
- Scheduler: one interval job `crt-engine-tick` (60s, `misfire_grace_time`
  30, coalesce, max_instances 1), boundary-gated inside — the effective
  fetch cadence is the TF grid (a fetch happens only when the wall-clock
  expected-newest-closed-bar advanced per symbol×TF; TDVA lag self-heals on
  the next minute's retry).
- Discord #crt: the CRT-0 notifier reused via `DISCORD_WEBHOOK_CRT` (env
  var, this session's companion change); engine events (fill/resolve/error)
  fire-and-forget off the event loop (`asyncio.to_thread`), fail-open.
- Router: `GET /crt/health` + `GET /crt/state` (read-only, renko `/health`
  precedent — the assessment: "read surface becomes REST + the claims
  leaderboard"). main.py mounts it renko-seam style, guarded so a CRT
  import error can never break the platform (a deliberate divergence from
  renko's bare import — recorded in the mount comment).

Platform-file edits, complete list: main.py +6 lines (guarded import +
guarded include), orchestrator.py +13 lines (guarded registration, next to
the other add_job blocks), feature_flags.yaml comment-only. Zero changes to
any existing function; zero migrations; zero claims writes; MT5 untouched.

## 4. Engine semantics (the parts the EA does implicitly)

- **Idempotent ticks**: every tick recomputes setups/labels/monitors from
  the fetched window + the frozen artifact; only four pieces of state
  survive across ticks (frozen decisions, emitted fills, missed windows,
  open positions) — all bounded (FROZEN_MAX 256).
- **Freeze timing** (no lookahead): at the arm tick (C2 == newest closed
  bar) the monitor replay already includes labels resolved with bars up to
  and including the C2 bar — the same ordering the EA's `OnNewTFBar`
  produces (labels update before `SelectTargetOpt` runs). Late arms
  (engine started mid-C3) are honestly skipped for STOP/CONT (the EA's
  `AllowLateMarketEntry=false` blocks them anyway); the C3C model gets the
  EA's one-bar `justClosed` entry window.
- **Cascade structure**: the C3 that fills a setup sweeps the C2 extreme —
  it is itself a next C2 candidate. The engine detects and trades cascades
  like any other setup (real CRT structure; the verification suite builds
  its expectations cascade-aware).
- **Hydration**: on cold start, open CRT rows are re-loaded from the paper
  table and their resolution continues via the shared barrier walk; the
  entry-bar check re-runs (idempotent) so a crash between emission and C3
  close still catches the ambiguous-C3 SL. A hydrated engine never
  re-emits a fill for a setup whose window passed.
- **Monitors feed from the UNGATED label stream** (every raw setup of a
  configured option, "monitored but never traded" — the EA's `bOut`
  semantics); gated trades are the paper rows. The rolling gate therefore
  reads unfiltered paper EV, exactly like `ComputeRolling`.

## 5. Verification record

**Deep suite** (throwaway `/home/z/verify/crt1_verify.py`, the T-series
convention): **122/122 passed** —

- A(7) sessions: NY window EST/EDT (ZoneInfo DST), inclusive 12:00 end,
  Asia window, 12:01→OUT, config parse.
- B(7) candles: closed-bar extraction (forming dropped by wall-clock truth;
  last kept when past), missing-OHLC drop, dedupe, M30 pairing + gap skip,
  expected-boundary math.
- C(6) ATR: MT4 SMA-seed parity (NaN before seed, seed value, Wilder
  recursion), percentile strict-lower counting, thin→unknown, invalid→−1.
- D(10) detection: the planted BUY setup at arm; sweep math; positional
  C1/C2; the C3 boundary; the post-C3 cascade (2 setups, real structure);
  close-outside reject; double-sweep reject; bearish setup; session from
  the c3 boundary (NY); vol_pct unknown when thin.
- E(14) labeler: CONT trade inversion; fill geometry (sell); filled-pending
  (C3 newest, geometry carried); TP after 1 bar with exact R; exit price =
  TP; same-bar tie → SL; ambiguous C3 → SL held=0 with
  R=−(1+2·slip/risk); NOFILL; time exit at hold=3 (sell ask = close +
  spread); C3C fill with confirmation (+ spread on entry) and pending
  walk; entry_bar_check SL/None.
- F(8) config loader: row rules (unknown tf / r≤0 / hold<1 skipped, dup
  first-wins, string session, vol_mask≤0→7, model map, train_end parse,
  missing artifact fail-open).
- G(24) gates: vol_bit bands; the confidence ladder t=0/1/2/3 →
  0.25/0.25/0.375/0.50% and CAUTION ×0.5; filter order (sweep→chop→vol);
  rolling pass/option-fail/TF-fallback/too-small; the full chain
  (NORMAL/CAUTION pass, decay-OFF block, disabled block); caps (daily
  loss / open risk / one-position / ready).
- H(7) monitors: replay state + live EV on a benign stream; the train_end
  filter; disabled→OFF; rolling horizon; pool only-enabled.
- I(30) **engine end-to-end**: the golden path (arm→freeze→fill at C3
  close→TP resolve→dedup on re-feed) with row-shape, frozen levels,
  open_ts=C3-close, sizing = equity×frozen-risk% on the entry-ref risk
  distance; monitor-stream counting; the rolling "too small" block on a
  cold stream; one-position cap; daily-loss stop; disabled option
  monitored-never-traded; ambiguous C3 open+close same tick with the exact
  R; missed fill window counted once; cold-start hydration (position in
  memory, resolves via the walk, no re-emission); determinism (two fresh
  engines → identical rows/patches modulo UUIDs); **the seeded stream**
  (16+ resolved labels → the rolling gate PASSES the golden freeze → fill
  → TP resolution).
- J(7) jobs/router: `crt_mode: off` → zero fetch calls; shadow builds
  engines from the artifact and ticks them; boundary-gated re-fetch
  (second tick within the same M15 bar fetches nothing);
  `register_crt_jobs` registers `crt-engine-tick` (60s) on a fake
  scheduler; `/crt/health` and `/crt/state` shapes (dark note in off, full
  option snapshots in shadow).
- K(3) the DISCORD_WEBHOOK_CRT chain (unset→disabled, env precedence,
  secret absent from the yaml).

**Bugs found and fixed during verification** (implementation, by the suite):
1. `_emit_position` built the PositionState but never registered it in
   `self.positions` — open positions were written to the paper table but
   never resolved (I3/I8 caught it via a missing patch); fixed with the
   registration line.
2. Mid-walk PENDING labels carried `filled=False` — a filled-but-unresolved
   trade would have skipped paper emission; fixed by carrying the geometry
   on PENDING.
3. `by_option` keyed OptionCfg objects (mutable dataclass → unhashable);
   rekeyed by `option.key`.

Test-expectation bugs found along the way (mine, not the port's): epoch
arithmetic, the M30 alignment fixture, the cascade setups' legitimacy, the
C3C bar's open==close, stale label handles, unstripped UUIDs in the
determinism comparison, and a fake fetcher that returned no bars (so the
boundary gate never advanced). Each was re-derived by hand before changing
the expectation.

**Regressions**: `tests/test_router_edge.py` +
`tests/test_strategy_registry.py` 46/46; the claims-adjacent set (8 suites)
140 passed / 11 failed with a stash round-trip proving the 11-failure set
byte-identical at HEAD (pre-existing; arch/hmmlearn absent in this sandbox —
the same set recorded in every prior task). ruff clean on every new/touched
file; py_compile clean; all yamls parse. `main.py` import fails on
`hmmlearn` — proven pre-existing by the same stash round-trip (gpu_engine
line 90, long before the crt import at 137); the guarded import means CRT
can never change that outcome. The CRT-0 selftest still runs end-to-end
(RC 0, mint, reports; signals skipped — webhook unset).

## 6. Operator steps

1. CRT-0's real-data pipeline is unchanged and still the prerequisite:
   MT4 `CRT_EV_Research_Export.mq4` → `data/crt/raw/` →
   `python scripts/run_crt_lab.py` → mints `config/crt_cont.json`.
2. Set the webhook: `export DISCORD_WEBHOOK_CRT=<url>` (the prior URL is
   dead — 404; rotate if not already done).
3. Adjust `config/crt/crt_engine.yaml` if needed: the symbol list (TV feeds
   + `spread_price`/`slip_price` per symbol — the README_v5_CONT §1 points;
   BTC's "half the spread" needs the operator's feed value), bar depths,
   paper equity.
4. Arm: flip `crt_mode: "off"` → `"shadow"` in `config/feature_flags.yaml`
   (hot-reloaded per fire — no restart needed). Watch `GET /crt/state` and
   the `#crt` channel. Paper fills land on
   `nt_paper_positions(source='crt_cont')`.
5. Before trusting M30: the assessment §8 parity check — overlap a derived
   M30 window against MT4 M30 (this also verifies the open-time reading of
   TDVA's bar `t`).

## 7. Honest limits

- **No real data has flowed through the engine** — the verification corpus
  is deterministic synthetic candles with planted setups (the same class
  of evidence as CRT-0's selftest: mechanics, never edge). The first
  real-data evidence arrives when the operator mints from MT4 CSVs and
  flips to shadow.
- **The TDVA bar-`t` open-time convention is unverified against a live
  response** (no TDVA key in this sandbox; the repo itself documents the
  field as never-consumed-before). M30 derivation and session boundaries
  depend on it; the §6.5 overlap check is the gate.
- **No TDVA fetch was exercised live** — the fetcher is faked in the suite
  (the platform's own light-sweep tests do the same); the job's error
  paths (fetch failure → log + #crt error + next-minute retry) are
  code-reviewed but not live-proven.
- Rolling-window overlaps training data until the corpus ages past
  train_end (the README's own note, carried into the artifact's honesty
  notes and this port's rolling computation verbatim).
- Thin-option rolling windows on H1 (5–20 setups/45d) — the pooled fallback
  is a floor, not proof; counts are visible in `/crt/state` (rl_n per
  option) so thinness is observable, not hidden.
- `live` mode currently runs paper semantics (no execution path until
  CRT-3); `/crt/state` says so.
- CRT-2 (claims + additive CHECK migration + CUSUM state rows + rolling-EV
  table + confidence sizing as a shadow column) and CRT-3
  (leaderboard-gated promotion) remain future scope, exactly as the
  assessment phased them.
