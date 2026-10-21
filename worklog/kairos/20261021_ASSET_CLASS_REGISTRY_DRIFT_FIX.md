# Asset-Class Registry Drift — Corrected (2026-10-21)

**Scope**: the `nt_symbol.asset_class` column disagrees with the engine's own class
vocabulary in two places, in opposite directions. This change corrects the drift at
the normalization layer (`tiered_atr.asset_class_key`, the single source every
class-keyed consumer already routes through), threads the symbol through the live
call sites, makes the raw-value session matcher safe for the corrected values, and
ships the registry fix as migration 217.

## 1 · The two drifts (traced live)

| Registry value | Instruments | Normalized (value-only) | Engine vocabulary expects | Live effect |
|---|---|---|---|---|
| `stocks` | US500, US30USD (+ inactive UK100, RUSS2000) | `equity` | `index` | EV cost **0.06** charged, index key says **0.05** → 0.01 ATR/trade **overcharge**; every `index` key in the config dead vocabulary |
| `commodities` | XAUUSD, XAGUSD, XAUEUR, XAUAUD | `commodity` | `metal` | `commodity` **not in** `cost_atr_by_class` (table has `metals` alias) → silent **0.05 default**, canonical is **0.06** → **undercharge**; p_win gate reads `commodity 0.30`, authored `metal 0.20` |

Evidence the repo itself intends the true classes:
- `asset_class_key`'s own value special-cases `("us30", "us500", "ndx") → index` and
  `("gold","silver","xau","xag") → metal` — intent encoded, never fired, because the
  registry delivers catch-all values and `"stock" in "stocks"` / `"commod" in
  "commodities"` win first.
- The instruments' calendars: US500 13,703 / US30USD 13,695 hourly bars over 730d
  (~23h futures/CFD session) vs AAPL 5,073 (6.5h cash session). A "stocks" class
  where one member trades 2.7× the hours of another is not a behaviorally
  meaningful class.
- The 2026-10-13 harmonization scripts (`update_nt_roadmap_mr_shadow_20261013.py`)
  already documented the mirror-image symptom: golds resolving `commodity` found no
  archetype listing it.

## 2 · The fix — four layers

1. **`asset_class_key(asset_class, symbol=None)` — symbol-aware normalization.**
   The symbol map (US500/US30USD/UK100/RUSS2000/NDX → `index`; `XAU*`/`XAG*` →
   `metal`) lives ahead of the value arms. Value-only calls keep the historical
   mapping byte-for-byte — research replay scripts stay faithful to what ran.
2. **Symbol threaded through every class-keyed live call site**: EV cost
   (orchestrator light `_ev_cost_price` + heavy_sweep via the shared helper), the
   `p_win_ok` bracket gate (light 5176 + heavy 2470), the strategy router's
   `ack`, `get_dynamic_atr_config`'s by_class fallback, and the DTO pooled-solve
   band fingerprint + peers filter.
3. **`market_hours` raw-value safety**: the Sun 22:00–Fri 22:00 UTC arm now also
   accepts `metals` (the post-migration registry value for XAU*/XAG* — same session
   `commodities` gave them; without the alias they would fall through to the
   unknown-class "always open" default → Saturday sweeps on spot metals); the US
   cash-session arm also accepts `index` (singular) so a canonical-key value
   cannot fall through either.
4. **Migration 217** (`217_asset_class_registry_drift.sql`): US500/US30USD/UK100/
   RUSS2000 `stocks → indices`; XAU*/XAG* `commodities → metals`. Idempotent
   (WHERE includes the old value). The code-side correction resolves the true
   classes even before the migration is applied, so the two layers converge.

**Found by the new tests (latent, fixed in passing)**: the value arms never
understood the PLURAL — `"indices"` contains no `"index"` substring and fell
through to `"default"`. The index arm now also matches `a.startswith("indic")`.
Without this, post-migration value-only consumers (replays) would have resolved
the corrected registry value to the unknown-class default.

## 3 · Before → after, every ACTIVE symbol (resolution table, real getters)

| symbol | registry | before | after | EV cost | p_win gate | ATR band |
|---|---|---|---|---|---|---|
| AAPL | stocks | equity | equity | 0.06→0.06 | 0.40→0.40 | by_symbol — unchanged |
| US500 | stocks | equity | **index** | **0.06→0.05** | 0.40→0.40 | by_symbol — unchanged |
| US30USD | stocks | equity | **index** | **0.06→0.05** | 0.40→0.40 | by_symbol — unchanged |
| XAUUSD | commodities | commodity | **metal** | **0.05→0.06** | **0.30→0.20** | by_symbol — unchanged |
| XAGUSD | commodities | commodity | **metal** | **0.05→0.06** | **0.30→0.20** | by_symbol — unchanged |
| XAUEUR | commodities | commodity | **metal** | **0.05→0.06** | **0.30→0.20** | by_symbol — unchanged |
| XAUAUD | commodities | commodity | **metal** | **0.05→0.06** | **0.30→0.20** | by_symbol — unchanged |
| BTCUSD | crypto | crypto | crypto | 0.10→0.10 | 0.50→0.50 | by_symbol — unchanged |
| 6 FX majors | forex | forex | forex | 0.04→0.04 | 0.30→0.30 | by_symbol.FOREX — unchanged |

**Bands are untouched**: `dynamic_atr` is `by_symbol`-only (`by_class` removed
2026-10-12, absent from the YAML); every ACTIVE symbol — including US500/US30USD/
AAPL — resolves its own `by_symbol` entry. Deadline tiers, the barrier floor, and
the reopt lattice are symbol-keyed. No band, clamp, or grid moves.

**The one band-SIDE change (deliberate, evidence-only)**: the pooled dynamic-ATR
solve groups by class. The former `stocks` pool splits — US500/US30USD now pool
together as `index`, and AAPL pools as `equity` alone. AAPL was among the 13/14
symbols whose per-symbol solve fails (`< min_signals 80`, 2026-10-12), so its
pooled evidence window shrinks from the mixed stocks pool to equity-only; if that
pool also falls short, AAPL falls down the fail-open chain to brick-fallback
geometry. Mixing equity and index paths in one evidence pool was contamination —
this is the honest direction — but watch AAPL's stored pick source
(`pooled:equity` vs brick) after deploy.

## 4 · Behavior deltas (all toward the config's authored intent)

- **Live EV round-trip cost**: indices 0.06 → 0.05 ATR/trade (were overcharged);
  metals 0.05-silent-default → 0.06 (were undercharged). Net book-level effect
  ≈ zero; per-class correctness is the point. Indices' EV rises slightly (helps
  US500's π_E 0.70 starvation); metals' EV falls slightly (the already-starving
  XAGUSD/XAUEUR/XAUAUD starve marginally harder — honest).
- **`p_win_ok` gate for metals**: 0.30 (commodity) → 0.20 (metal) — a LOWER
  qualification bar for the flagship metals. The `metal: 0.20` key was authored
  2026-10-04 but never fired (dead config, like the index cost key). If 0.30 is
  the calibrated intent, pin `min_p_win_bracket_by_asset_class.metal: 0.30` in
  the YAML rather than reverting the registry.
- **Sessions**: unchanged (`indices` hits the same US-session arm `stocks` did;
  `metals` hits the same forex-like arm `commodities` did).
- **Routing**: unchanged for every archetype (every allowlist listing `equity`
  also lists `index`; every one listing `metal` also reachable for the golds) —
  now the `index` vocabulary is actually reachable.
- **Kelly annualisation, heavy-sweep cadence, expiry**: class-independent
  (`== "crypto"` checks only), unchanged.

## 5 · Left as-is (documented for the next audit)

- **SI** — COMEX silver *future*, kept `commodities` (it duplicates XAGUSD per
  the specialist-books review; inactive).
- **VGT** — an ETF sitting in `commodities` in the registry; inactive bench.
- **JX** — semantics unverified; inactive.
- Research/replay scripts (`fine_atr_solver.py`, `dynamic_atr_class_replay.py`,
  the 2026-10-13 roadmap updaters) keep value-only normalization on purpose —
  they replay history and must resolve what actually ran.

## 6 · Verification

- New `tests/test_asset_class_registry_drift.py`: **58/58** — symbol-aware
  normalization (all 14 ACTIVE + bench), value-only byte-identical back-compat,
  the shared `_ev_cost_price` helper routing, cost/p_win getters, router
  invariant (every equity archetype also allows index), market-hours arms
  (indices/index share the US session; metals/commodities/forex share the
  forex-like session; unknown still fails open), band-resolution symbol-keying.
- Full suite: **2,201 passed, 11 skipped** + 19/19 regime_edge. 25 failures in
  8 files are **identical on clean HEAD** (DB/network-dependent: calibration
  bias window, raw bias cycle, data quality…) — environment gaps, not this
  change. `slowapi` import errors in 2 files are also pre-existing (module not
  installed in this sandbox).

## 7 · Operator moves

1. Apply migration 217 (idempotent; verify with the SELECT in the file).
2. If the metals' old 0.30 p_win bar was the calibrated intent, pin
   `min_p_win_bracket_by_asset_class.metal: 0.30` in `config/feature_flags.yaml`.
3. Watch AAPL's stored pick source after the next pooled solve cycle.
