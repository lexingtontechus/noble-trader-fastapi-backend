# MSE Implementation Plan (Future) — Market Sync Engine into noble-trader-fastapi-backend

Status: **PLANNED — not built.** Analysis + integration review only. No code changes.
Date: 2026-08-08
Scope: `noble-trader-fastapi-backend` (LightningAI sweep orchestrator, v7.7.0)
Sources: `BasketScalper_MSE_Operating_Guide.md` (MQL4 EA, six-theme build `BasketScalper_EA_MSE_Phase3.mq4`), `Market Sync Engine (MSE) Design.md` (backend port design, Option B), verified against backend source.

---

## 1. Verdict (TL;DR)

The MSE concept ports cleanly to the backend. The backend is **better positioned** than the design doc assumes: an EV blend engine with runtime weight overrides (`set_weights()`/`temp_weights()`), a 7-gate qualification chain, an existing rolling-correlation module (`core/correlation_regime.py`), and a plan-split broadcast layer all already exist.

The design doc contains **three material inaccuracies** about the codebase:

1. **"Leverage existing `feed_adapters.py` (MetaAPI feed adapters)"** — WRONG. `regime_platform/adapters/feed_adapters.py` has Alpaca / Binance / IB adapters only; there is **no MetaApi adapter anywhere in the backend**. MetaApi lives in the agent repo.
2. **"EV Engine v4"** — the engine is **v5** (4-source logit pool). The weight-override API claim (`set_weights()`/`temp_weights()`) is correct.
3. **M1-bar assumption unaddressed** — the EA is an M1 scalp (warm-up 100 M1 bars ≈ 100 min, CorrWindow 200 M1 bars); the backend sweep consumes **1h closes** (168 bars = 7 days). This is the biggest conceptual gap and drives Decision D1 below.

Recommended integration: **Method A (Gate 8) + Method B (5th EV source) as phases, not alternatives**, on **hourly data (Path 1)** with theme baskets rebuilt from the live symbol universe.

---

## 2. What MSE is (condensed from the Operating Guide)

| Component | MQL4 source | Port target |
|---|---|---|
| 6 theme baskets | USD / Metals / Crypto / JPY-haven / Equity / Oil (CSV symbol + weight lists) | Symbol sets from `nt_symbol` (DB-driven, plan-aware) |
| Signal per theme | currency-strength / spike counts / sync % | Return direction over rolling window |
| Evidence layer | rolling Pearson corr of each theme → chart symbol (window 200) | `numpy` rolling corr (machinery already in `correlation_regime.py`) |
| Adaptive weights | `BaseW_*` prior shrunk by `CorrShrinkLambda` (0.5) toward learned corr; `CorrNoiseFloor` (0.15); `MaxThemeWeight` (0.40); `MinWarmThemes` (2) | Same math, pure Python |
| Output | `MSS %`, `mse_direction`, `conviction`, `mss_slope`; `WARM` after 100 samples | `MarketSyncSnapshot` dataclass |
| Gate | `MSS ≥ 90` + direction match + rising 3 bars + conviction ≥ 0.20 | New Gate 8 in sweep qualification chain |
| Risk scaling | `ScaleRiskByMSS` → 0.1×–1.0× size | Multiply `effective_kelly` in paper_book sizing |

The math is small, deterministic, numpy-only. **This is a plumbing + data-fidelity risk, not a research risk.**

---

## 3. Design doc claim verification (verified against code 2026-08-08)

| # | Design doc claim | Code reality | Verdict |
|---|---|---|---|
| 1 | "Leverage existing `feed_adapters.py` (**MetaAPI** feed adapters)" | `regime_platform/adapters/feed_adapters.py` = Alpaca / Binance / IB only. **No MetaApi adapter anywhere in the backend** (grep = 0 hits). MetaApi lives in the **agent** repo. | ❌ WRONG |
| 2 | "(Redis Price Buffers)" | Exists: `services/redis_persistence.py` → `regime:prices:{symbol}` stream (maxlen 5000), in-memory buffer + batch flush — but serves the **StreamSession** streaming path (`routers/feeds.py`, `stream_ws.py`), **not** the sweep path. Sweep fetches TradingView candles per tick. | ⚠️ Half-right — buffer infra exists but not on the sweep path |
| 3 | "`ev_engine.py` **v4** … `set_weights()`, `temp_weights()`" | Engine is **v5**, 4-source logit pool: `p_pattern 0.30 / p_regime 0.25 / p_markov_hold_n 0.20 / p_timesfm 0.25`, renormalises on missing sources. `set_weights()/temp_weights()/get_weights()/restore_default_weights()` all exist and are documented as the public override API. | ⚠️ Version stale (v5 not v4); API claim **correct** |
| 4 | "Gated by **7+ Qualification Gates**" | Light sweep (`_process_symbol`) evaluates exactly 7: `signal_qualifiable, regime_conf_ok, kelly_min_ok, timesfm_available, timesfm_confidence_ok, ev_allowed, p_win_ok` (+ post-check cooldown). | ✅ Accurate — MSE = Gate 8 slots in cleanly |
| 5 | "Position Sizing (Kelly / Masaniello)" | Sweep path uses **Kelly only** (`core/position_sizer.py` `PositionSizer.size()` → `recommended_f`; paper book `min(base_notional × kelly × ev_scale, 20% cap)`). **Masaniello exists** (`services/masaniello_bridge.py`) but is a *separate portfolio-level path* (`routers/sizing.py`), NOT in the sweep pipeline. | ⚠️ Partially accurate |
| 6 | "StreamSplitter automatically splits into `signals:signal_scout` / `signals:precision_pro`" | Confirmed — `services/stream_splitter.py` does exactly this (consumer group `stream_splitter`). | ✅ Accurate |
| 7 | "`regime_platform/services/statistics/`" + "`regime_platform/core/market_sync.py`" as homes | Both directories exist and are the natural homes (`services/statistics/` already holds ev_engine, markov_chain, calibration, …). | ✅ Accurate |
| 8 | *(omission)* Rolling-correlation machinery | `core/correlation_regime.py` — rolling Pearson matrix (window 60), correlation HMM → `low/mid/high/crisis` + `corr_risk_multiplier`. Used by `masaniello_bridge`, `routers/multi_asset`, `routers/sizing`. | ⚠️ Unmentioned existing asset — reuse patterns, don't rebuild |
| 9 | *(omission)* **M1 bar assumption** | Sweep candles are **1h closes** (`interval="1h"`, 168 bars = 7 days) via TradingView batch (`price_fetcher.py`). EA guide is built on **M1**. | ❌ Not addressed — biggest gap |

---

## 4. Current architecture seams (where MSE plugs in)

```
TradingView batch (1h candles, 168 bars)          ← sweep data path (NOT MetaApi, NOT M1)
        │
        ▼
_sweep_group (per-symbol cron, lock, stagger)
        │
        ▼
_process_symbol()   ← MSE hooks here (light sweep)
  │  1. RegimeHMM fit/predict
  │  2. Kelly sizing
  │  └─► [NEW] MarketSyncEngine.compute(symbol, prices) → MarketSyncSnapshot
  │  3. EV engine compute (4-source P_win)
  │  └─► [Option B] 5th source p_mse blended via set_weights/temp_weights
  │  4. ── 7 Qualification Gates ──
  │  └─► [Option A] Gate 8: mss ≥ threshold ∧ dir match ∧ rising ∧ conviction
  │  5. qualified → Redis publish / StreamSplitter plan-split / Realtime broadcast / paper book
  ▼
heavy_sweep.py  ← SEPARATE qualification (5 checks) — MUST mirror the MSE gate there too
```

### Hard stack facts that govern the build

1. **Both sweep paths need the gate.** `_process_symbol` (light) and `heavy_sweep._evaluate_heavy_qualification` (heavy, 5 simpler checks) are **separate code paths** — a hook added to `_process_symbol` does NOT cover heavy signals (recurring stack lesson). Any MSE gate must be added to **both**.
2. **Symbol universe is too small for 6 themes.** Active sweep universe (config/brick_params.yaml + nt_symbol seeds): BTC/ETH/SOL, AAPL/NVDA/TSLA/SPY/QQQ, EURUSD/GBPUSD/USDJPY, GOLD/SILVER/OIL — 14 symbols. The MSE's six baskets want XPT/XPD, JPY crosses (EURJPY/GBPJPY), LTC/XRP, indices (US500/USTECH/US30), Brent/nat-gas. **"Equity" and "JPY-haven" themes barely exist in the current universe.** Either rebuild theme baskets from live coverage, or expand `nt_symbol` (product/entitlement impact — per-plan symbol lists are DB-owned).
3. **Cadence mismatch changes MSS semantics.** EA: M1 scalp — MSS is a *timing* filter. Backend: 1h bars swept every 5–15 min — MSS becomes a *directional regime* filter. Warm-up at 1h = 100 **hours** (~4 days); CorrWindow 200h ≈ 8 days. Thresholds (MSS ≥ 90!) were tuned for M1 and need re-tuning for hourly.
4. **RapidAPI 429 budget.** Adding per-symbol basket fetches per sweep tick would hammer the TradingView batch endpoint (the HMM fetcher already 429s at 11 symbols; `hmm_batch_consolidation` exists for exactly this). **Critical constraint: MSE must NOT add per-tick fetches.** Theme correlations need either (a) one additional batched fetch of the theme universe on a slow cadence (hourly/daily, cached), or (b) reuse of `nt_signal_sim` / `nt_renko_bricks` as the price-history source. This is the real cost driver.
5. **Warm-up state must survive restarts.** The EA persists state to a .bin. Backend equivalent: persist theme correlation state to Redis/Supabase, or accept a 4-day re-warm on every studio restart (deploys restart the studio — frequent).

---

## 5. Key decisions (USER)

### D1 — Timeframe: M1 vs 1h (RECOMMEND 1h)

| | Path 1: MSE on 1h (recommended, cheap) | Path 2: MSE on M1 (design-doc faithful, expensive) |
|---|---|---|
| Data | Already have it — 168 bars × every sweep tick | Needs a **new persistent streaming feed** (MetaApi streaming not in backend; TradingView WS not wired; proxy `/quotes` is a snapshot cache) |
| Warm-up | ~4 days to WARM | ~100 min to WARM |
| Meaning | Directional-regime filter (fits backend's 1h engine) | Scalp-timing filter (fits EA, but backend doesn't scalp) |
| Infra change | One module + gate + tests | StreamSession/Redis-buffer pipeline + feed wiring + new symbol coverage |
| Risk | Low | High — and the backend **never executes** (sweep-only P5 contract), so M1 timing adds no execution value |

Rationale for 1h: the backend is a signal producer on hourly candles; M1 fidelity has no downstream consumer that can use it (the agent executes on its own MT4/5 bridge with its own timing). An M1 pipeline serving a timing gate nobody consumes is over-engineering.

### D2 — Theme coverage strategy (RECOMMEND: baskets from live universe, expand later)

- Option A: Rebuild the 6 baskets from the 14-symbol live universe (JPY-haven → USDJPY only; Equity → SPY/QQQ/AAPL/NVDA/TSLA; no XPT/XPD). Zero product impact; MSE starts with partial themes.
- Option B: Expand `nt_symbol` with the full MSE universe (XPT/XPD, JPY crosses, LTC/XRP, US500/USTECH/US30, XTI/XBR/XNG). Full fidelity; touches per-plan symbol entitlement and TradingView symbol mappings — a product decision.

---

## 6. Phased implementation plan

### Phase 1 — Core engine + Gate 8 (Method A)
- New `regime_platform/services/statistics/market_sync.py`: `MarketSyncEngine` (6 themes → rolling corr → adaptive weights → `MarketSyncSnapshot{mss, direction, conviction, slope, warm}`) + unit tests against a simulated 200-bar OHLCV frame (the design doc's proposed next step is correct).
- Theme definitions **config/DB-driven** (`nt_symbol`-sourced or `config/` YAML), shrunk to live universe per D2.
- Wire as **Gate 8** in `_process_symbol` gate_results + mirror in `heavy_sweep._evaluate_heavy_qualification`. Default **off** (`MSE_ENABLED=0`) — stack pattern is a toggle (cf. `NOBLE_TRADER_REDIS_PUBLISH_ENABLED`, `TIMESFM_GATE_MODE`) so the gate ships inert and is A/B'd.
- Persist snapshot onto the `nt_signal_sim` capture (migration columns) so the EOD lookback can score "did the MSS gate improve outcomes?" — the Operating Guide's own validation method (§7).
- Risk scaler (0.1×–1.0× by MSS) multiplies `effective_kelly` in `paper_book` sizing — paper book is the proving ground; no live-sizing change.

### Phase 2 — 5th source in the EV blend (Method B)
- Add `p_mse` to `P_WIN_WEIGHTS_FULL` (e.g. 0.15, renormalised — engine already renormalises missing sources, so no other code changes).
- "Runtime override via `set_weights()`/`temp_weights()` to shift emphasis during highly correlated regimes" is directly supported (v5 `temp_weights()` context manager).
- A/B per Operating Guide: gate removes losers (Phase 1), blend shifts conviction (Phase 2). Run both, keep whichever earns its place.

### Phase 3 — Product-facing (only if the product wants it)
- The Realtime broadcast plan already defers a 3rd `sweep` channel "until the Basket engine exists." MSE **is** the basket engine — it would let Talaria surface per-theme correlation/weights (EA dashboard rows: `USD sig 0.40 corr 0.71 wt 0.34`) as a new view for Precision Pro / the future Basket plan (`basket_scalper` UUID 479635b8-8d1f-40b2-9692-fd0118f72e7a, engine TBD).

---

## 7. Risks & gotchas (ranked)

1. **Timeframe semantics** (highest): M1-tuned thresholds (MSS≥90, RisingBars=3) on hourly data will behave differently; plan a re-tuning pass, keep thresholds env-driven.
2. **Data coverage for the 6 themes**: current universe can't populate JPY-haven (1 pair) or Equity (no indices) meaningfully → D2.
3. **Heavy-sweep bypass**: any gate added only to `_process_symbol` silently misses heavy signals → mirror in `heavy_sweep.py`.
4. **Feed-adapter myth**: the design doc's MetaApi ingestion path doesn't exist; do not chase it. Sweep data path is TradingView batch, full stop.
5. **RapidAPI 429 pressure**: MSE must reuse existing candles or add one slow-cadence batched fetch — never per-tick per-theme fetches.
6. **Warm-up state**: must persist across studio restarts (Redis/Supabase), else 4-day re-warm per deploy.
7. **Kelly double-counting**: the p_win soft-gate already shrinks kelly; stacking an MSS risk-scaler needs care to avoid triple-penalising (pattern → EV → MSE).

---

## 8. Acceptance / verification criteria

- Unit tests: `market_sync.py` rolling-correlation/weighting math on a simulated 200-bar frame; warm-up gating (WARM vs warming); noise-floor/max-weight/shrinkage invariants.
- Gate tests: `MSE_ENABLED=0` → identical behaviour to today (regression suite stays green — baseline 315 passed / 23 skipped / 0 failed as of 2026-08-08); `MSE_ENABLED=1` → gate_results gains `mse_*` fields, qualification log shows Gate 8.
- Heavy-sweep mirror test: heavy qualification honours the MSE gate.
- Live A/B: MSE off vs on on the paper book, compare profit factor + max drawdown (not just net profit) — the Operating Guide §7 method.
- Suite command: `PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider` (run with `-p no:cacheprovider`; plain run can hang on heavy backtest files).

---

## 9. Open questions for the user

1. **D1**: 1h (recommended) or M1?
2. **D2**: shrink baskets to live universe (recommended) or expand `nt_symbol` (product impact)?
3. Is the Basket plan (`basket_scalper`) intended to be driven by this MSE in the near term, or is this purely a sweep-quality gate for now?
4. Where should warm-up state persist: Redis (`regime:prices:*` family) or Supabase?

---

## 10. References

- `BasketScalper_MSE_Operating_Guide.md` — MQL4 EA operating/tuning guide (trigger, MSE gate, warm-up, validation method).
- `Market Sync Engine (MSE) Design.md` — backend port design (Option B), claims verified against source in §3.
- Backend source seams (2026-08-08): `sweep_orchestrator/orchestrator.py` (`_process_symbol` §1263, gate_results §1541), `sweep_orchestrator/heavy_sweep.py` (`_evaluate_heavy_qualification` §797), `services/statistics/ev_engine.py` (v5, P_WIN_WEIGHTS_FULL), `core/correlation_regime.py`, `services/redis_persistence.py`, `services/stream_splitter.py`, `sweep_orchestrator/price_fetcher.py`, `sweep_orchestrator/realtime_publisher.py`, `sweep_orchestrator/paper_book.py`.
- Skill: `noble-trader-stack` + `noble-trader-workspace-maintenance` (topology, gates, deploy patterns, migration pitfalls).
