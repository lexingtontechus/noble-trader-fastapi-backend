# Kairos signal-strategy structure — wider lens: an alpha library, not a mean-reversion patch

**Date:** 2026-10-10 (rev)
**Type:** Strategy ideation / architecture scope (theoretical — no code change)
**Reframes:** `20261010_KAIROS_STRATEGY_STRUCTURE.md` anchored too narrowly on mean-reversion as *the* strategy. This widens the frame and uses the attached *151 Trading Strategies* paper (Kakushadze & Serur, SSRN 3247865) to shortlist archetypes that fit Kairos's asset classes.
**Status:** Ideation for review. Bars and build sequence at the end.

---

## 1. The wider framing — Kairos is not missing "a strategy," it is missing a strategy *layer*

The prior draft asked "which strategy should Kairos use?" and answered "mean-reversion (for gold buys)." Too narrow. The correct question is **structural**: Kairos runs exactly **one** signal logic for **all** assets and **both** directions, and the era-clean evidence says that uniformity is the defect.

Era-clean (post-2026-10-02, `contaminated=false`): every symbol overprices TP (avg_p_win 0.31–0.46 vs realized WR 2.5–23%); realized hit rate spans **3.4% (US30USD) to 23.2% (XAGUSD)** — a 7× spread. No single logic fits a book that wide. The ID45 inversion (gold-buy counter-trend 38.2% vs pass 6.5%; silver-buy the opposite) is one *instance* of the structural problem, not the whole of it.

So the goal is not "add mean-reversion." It is to **promote strategy from a hardcoded constant to a configurable, per-signal-selected layer** — an **alpha library** of archetypes, each with its own entry/exit/risk/asset-fit, chosen by a router. Mean-reversion is one archetype in that library, not the headline.

---

## 2. Where strategy is defined today — and it is neither configurable nor a router

**Answer, precisely (orchestrator.py:683 `_compute_trade_signal`):**

```python
def _compute_trade_signal(regime_label, current_price, brick_size, sl_bricks, tp_bricks):
    lower = regime_label.lower()
    if "bull" in lower:   signal="buy";   TP=+brick*tp_bricks; SL=-brick*sl_bricks
    elif "bear" in lower: signal="sell";  TP=-brick*tp_bricks; SL=+brick*sl_bricks
    else:                 signal="neutral"
```

- **Direction** comes from the HMM regime label alone (`bull→buy, bear→sell`). No indicator, no deviation, no archetype.
- **Geometry** comes from `brick_params.yaml` (`brick_size, sl_bricks, tp_bricks`), overridden on the heavy path by the dynamic-ATR pick — but the *shape* (symmetric brick bracket, momentum-style far TP) is identical for every symbol and direction.
- **Configurable?** Only in that brick geometry is tunable per symbol. The *strategy itself* — "follow the regime trend with a symmetric bracket" — is a hardcoded constant. There is no archetype field, no strategy registry, no per-signal strategy selection.
- **Directional router?** No. Direction is a regime lookup, not a decision over (asset, direction, regime, deviation, macro, trend). Clef-Flash *classifies* the already-formed signal but never feeds back (research-only stamp).

**Consequence:** the router layer does not exist yet. Everything below proposes creating it.

---

## 3. Shortlist from the paper — archetypes that fit Kairos's asset classes

Kairos trades FX majors, gold/silver (metals), BTC (crypto), AAPL/US30/US500 (equities/index). Filtering the paper's 150+ catalog to **directional, single-asset, indicator-computable** archetypes matching those classes (excluding options/fixed-income/structured — Kairos has no options chain, no rates, no tranches):

| Archetype (paper §) | Core logic | Best-fit Kairos assets | Evidence in our data |
|---|---|---|---|
| **Price-momentum** (3.1) | buy recent winners, ride the trend | silver, BTC, equities | XAGUSD buy aligned 46% WR; BTC momentum-dominant — the current default works *here* |
| **Mean-reversion / Contrarian** (3.9, 10.3) | fade extremes to mean; enter \|Z\|≥2, exit at mean, stop at Z≥3.5 (regime break) | **gold** (buy-into-downtrend), candidate FX majors | XAUUSD buy counter-trend 38.2% vs pass 6.5% — the ID45 cohort |
| **Single/Dual/Triple moving average** (3.11–3.13) | MA-cross trend-follow | FX majors (classic), metals | untested as a distinct archetype; overlaps momentum |
| **Carry trade / momentum&carry combo** (8.2, 8.4) | FX: hold high-yield vs low-yield, or momentum+carry | FX majors | not currently modeled — a whole FX edge Kairos ignores |
| **Multi-asset trend following** (4.6) | trend across a basket, risk-scaled | cross-asset allocation layer | a portfolio overlay, not a per-signal router |
| **ANN / sentiment classifier** (18.2, 18.3) | ML direction classifier | crypto (where it's proven) | exactly where Clef-Flash fits (§5) |
| **Fundamental macro momentum** (19.2) | macro-driven directional | all (macro_label exists) | we have `macro_label`/`trend_label` but use them only as Kelly multipliers, not strategy selectors |

**Deliberately excluded (no data / no instrument):** options spreads (2.x), fixed-income/carry/curve (5.x), volatility-as-asset (7.x — no VIX/variance feed), pairs/stat-arb (3.8 — single-asset engine, no cross-sectional book), real estate/infrastructure/tax. In the paper but requiring instruments Kairos doesn't touch.

**Honest phase-1 shortlist:** **momentum, mean-reversion, moving-average trend, FX carry** as the initial library; the rest are phase-2+. Mean-reversion is *one entry*, chosen because we have era-clean evidence for it (gold buys) — not because it's the answer.

---

## 4. The router — configurable vs directional (the key design decision)

The question — *"is it configurable or directional based on router (asset/direction/regime)"* — the answer is **both, and they must be separated:**

1. **A declarative config layer** (`config/strategy_registry.yaml`) — the *catalog*. Each archetype is a config block: entry rule, exit rule, risk rule, and an **asset-fit predicate** (which symbols/directions/regimes it's allowed on). Operator-editable, versioned, reviewed — same discipline as `feature_flags.yaml`. This is the "configurable" half. Not a per-tick code branch; data the router reads.

2. **A router** (`strategy_router.py`, new) — the *decision*. At signal time it evaluates the candidate against the registry's asset-fit predicates + (asset, direction, regime, deviation, macro, trend) and **selects which archetype applies**, returning that archetype's entry/exit/risk params. This is the "directional" half. Deterministic and unit-tested; can return **stand-by (no trade)** — which today's engine never does.

```
signal candidate ──► router reads strategy_registry.yaml
                     evaluates (asset, direction, regime, deviation-Z, macro, trend)
                     ──► selects ONE archetype  OR  stand-by
                          └─► applies that archetype's entry/exit/risk to geometry + deadline
```

**Why both:** pure-config can't make the cross-signal decision (which archetype *this* signal is); pure-code hardcodes the catalog and can't be reviewed/tuned without a deploy. The registry holds *what the strategies are*; the router holds *how to choose*. Mirrors the working `feature_flags.yaml` (config) + `feature_flags.py` (resolution) split.

**Interaction with existing gates:** the 7-gate chain stays. The router sits *upstream* of geometry/deadline/calibration — it decides the *shape* of the trade before the gates price it. `context_aligned_ok` flips from a reject-gate to a router *input* (the ID45 signal becomes the MR trigger). Calibration (`p_win`) becomes **archetype-conditioned** — MR and momentum trades have different base rates and must be priced separately (also the fix for the uniform overconfidence).

---

## 5. Where Clef-Flash adds value — multi-asset buy/sell/hold classifier

**Current state (clef_flash.py):** research-only classifier. Sends the already-formed signal's context (symbol, direction, regime, bracket, macro, trend) to the Worker, gets `p_clef_dir` / `p_clef_wait`, stamped on `nt_signal_sim`. **Never feeds back.** Era-clean eval: `p_clef_dir` AUC 0.58 (chance — retire as a direction gate); `p_clef_wait` AUC 0.61 for sl_hit (a usable *suppress* signal).

**The repurpose:** Clef-Flash is already a **structured multi-label classifier** (`classify_choice` returns a proper probability distribution over a fixed label set — no free-text parsing). That is exactly the primitive for a **multi-asset buy/sell/hold decision layer** that *computes the routing signal the deterministic router lacks.* Two distinct, high-value roles:

**Role A — archetype/routing classifier (the router's judgment input).**
Feed Clef-Flash the *indicators* — momentum, volatility, trend, and **interaction indicators** (regime×deviation, macro×trend agreement, vol×bracket) — and ask it to classify the *setup type*, not the direction:
```
choice over {mean_reversion, momentum, range_fade, breakout, stand_by}
state = {symbol, asset_class, momentum_24h, vol_ratio, trend_label,
        macro_label, hurst_proxy, deviation_z, regime, regime_conf, ...}
```
This is the "when to apply which strategy" decision, made by a model that weighs interactions a hand-coded predicate misses. The paper's crypto ANN/sentiment classifiers (18.2/18.3) are the precedent: ML direction/regime classification is proven in crypto; this extends it to *strategy selection* across assets.

**Role B — multi-asset buy/sell/hold overlay (portfolio-level).**
Clef-Flash already returns a three-way `{buy, sell, wait}` — that *is* buy/sell/hold. Run it across the 14-symbol basket with momentum/vol/trend/interaction features to produce a **cross-asset directional stance** (which symbols buy, which hold, which sell *now*), stamped for shadow. This is the paper's multi-asset trend-following / macro-momentum (4.6, 19.2) as a learned classifier rather than a fixed rule.

**Honesty constraint (from the existing scope docs):** Clef-Flash stays a **classifier, not an EV source and not a deterministic override.** Outputs are (a) shadow-stamped first, (b) measured for discrimination (AUC vs realized outcome, per archetype) before any live role, (c) fail-open (NULL stamp on any failure). The `p_clef_dir` result — AUC 0.58, chance — is the standing warning: a plausible-looking classifier can carry no signal, so **every Clef-Flash role earns promotion only on measured discrimination, era-clean, walk-forward.** Role A starts as a shadow archetype-stamp measured against the deterministic router's own choice, not as the router itself.

**Indicator set to feed it (mapped to what Kairos already computes):**

| Indicator family | Kairos source | Feeds |
|---|---|---|
| Momentum | `efficiency_24h`, `atr_ratio_run`, return slopes | momentum archetype, buy/sell/hold |
| Volatility | `vol_ratio` (std24/std168), regime vol tier | regime filter, risk sizing |
| Trend | `trend_label`, `macro_label`, HMM trend | trend/momentum vs MR selection |
| Deviation | distance-from-mean proxy (`tp_distance_atr`, price vs rolling mean) | MR entry threshold (Z) |
| **Interaction** | regime×deviation, macro×trend agreement, vol×bracket, trend×direction-alignment | the routing decision — the part a single-indicator gate can't see |

---

## 6. Revised build sequence (one lever, era-clean, shadow-first)

Supersedes the B1–B5 in the prior draft. Mean-reversion is no longer the headline; the **router + registry** is.

- **R0 — Strategy registry skeleton (config only).** `config/strategy_registry.yaml` with momentum + mean-reversion + moving-average + FX-carry archetypes, each an asset-fit predicate + entry/exit/risk block. No code consumes it yet. (Reviewable, reversible.)
- **R1 — Router v0 (deterministic, shadow).** `strategy_router.py` reads the registry, selects an archetype per signal, **stamps `strategy_archetype` on `nt_signal_sim`** — zero live change. Bar: ≥200 shadow episodes across ≥8 symbols.
- **R2 — Measure the router.** Does the stamped archetype predict outcome better than the current uniform engine? Era-clean, per-archetype WR/expectancy, walk-forward. The gate that decides whether routing helps *at all* before any archetype is trusted.
- **R3 — Archetype-conditioned calibration.** Per-archetype `p_win` replaces the single blend. Promote only if per-archetype bias improves toward 0 (fixes the uniform overconfidence).
- **R4 — Clef-Flash Role A shadow.** Archetype-classifier stamp, measured against R2's deterministic router. Promote only on measured discrimination (AUC, era-clean) — the `p_clef_dir`-was-chance lesson.
- **R5 — Clef-Flash Role B shadow.** Multi-asset buy/sell/hold overlay stamp.
- **R6 — Live route, one archetype at a time.** Mean-reversion for gold buys is the first candidate *only because it has era-clean evidence*; the momentum path for silver/BTC is left untouched (it works). Each live flip behind its own flag.

**Deliberately not in scope:** any live flip before R2 shows the router helps; Clef-Flash as anything but a shadow-stamped, discrimination-gated classifier; options/FI/vol archetypes (no instruments).

---

## 7. Files / lineage

- ID45 inversion (the instance that triggered this): `worklog/20261010_TIER1_CONTEXT_GATE_INVERSION.md`.
- Era-clean evidence: `nt_signal_sim` post `2026-10-02`, `contaminated=false`; `v_calibration_records_current`.
- Reopt era-clean: `worklog/20261010_REOPT_CLAMP_HARMONIZATION.md` (Fix B).
- Clef-Flash current role: `regime_platform/services/decision/clef_flash.py`; eval `worklog/20261010_CLEF_WAIT_CHANNEL_EVAL.md`; scope `docs/scopes/20261004_CLEF_FLASH_CLASSIFIER_ROLE_SCOPE.md`.
- Strategy catalog: Kakushadze & Serur, *151 Trading Strategies* (SSRN 3247865, attached).
