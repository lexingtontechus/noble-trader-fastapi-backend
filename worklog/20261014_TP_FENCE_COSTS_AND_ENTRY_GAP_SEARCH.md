# Operator change set — TP fence, honest cost table, and the entry-gap search harness

**Date:** 2026-10-14
**Type:** Config change set + one new offline harness. Two live-behaviour changes (both one-line, both documented with revert); the harness changes nothing.
**Trigger:** operator decisions taken with PBM live in shadow — the "today, zero infra" tier of the Task-12 advisory applied as config, plus the one genuinely new design layer (entry timing) instrumented as code.
**Base:** HEAD `4953e96` + the PBM working tree (Phases A–E; `pbm_mode: "shadow"` deployed; migrations 210/211 applied — operator confirmed).
**Companions:** `20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md` (the build whose flags these changes feed), `20261014_SSE_STREAMING_VS_15MIN_SWEEP_ENTRY_TIMING.md` (entry-layer sequencing: predicate before pipe), `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` §4 (costs) / §8 (the gap KPI).

---

## 0. What changed

| # | Change | Where | Live effect |
|---|---|---|---|
| 1 | `dynamic_atr.tp_atr_min` **0.20 → 2.0** | `feature_flags.yaml` global floor | uncovered scopes only — every covered symbol already floors ≥ 4.0, so covered picks are byte-identical (§1, verified in §6) |
| 2 | `atr_reopt_urgency.auto_apply_clamps` **true → false** | `feature_flags.yaml` | the 4h reopt stops auto-rewriting `by_symbol` clamps; reopts still run + report; clamp application is operator-GO again (§2) |
| 3 | `pbm_cost_atr_by_symbol` **`{}` → 14-symbol table** | `feature_flags.yaml` | honest costs enter the PBM EV gate — mtime hot-reload, next tick, no restart (§3) |
| 4 | `scripts/measure_entry_timing_gap.py` **new** (1,087 LOC) | offline harness | none — read-only measurement; the entry-timing edge search at the Phase E bar (§4) |

Flips that did NOT happen: `pbm_ev_mode` / `pbm_geometry_mode` / `pbm_sizing_mode` untouched (`off` / `grid` / `legacy`). The replay-gated B/C/D checklist in the PBM build worklog §4 stands unchanged.

---

## 1. The TP fence — `tp_atr_min: 0.20 → 2.0`

**What the key is.** The global lower clamp on TP distance (ATR units) for the dynamic ATR picker. Precedence is `by_symbol.<SYM>` → global (`get_dynamic_atr_config`, feature_flags.py:859-913), and every symbol in the active universe is covered — majors via the `FOREX` alias (4.02), metals 4.0-6.07, BTCUSD 7.50, AAPL 6.0, indices 12.0/12.5. The floor is therefore the **backstop for scopes without a by_symbol block**: new symbols, class-level walks (symbol=None), lookup misses.

**What it closes.** The tight-corner degenerate class — the 136→1.57 jump: `max_expiry: 0.20` plus the both-halves objective squeezed the argmax below TP 2 while the raw walk itself preferred wide (code comment: TP 9-17 / SL 0.5-2). At global scope the 1.71 grid point of `tp_atr: [1.71, 2.0, ...]` is now unreachable (`select_tp_sl` skips grid points below the floor, tiered_atr.py:512; the stored-pick validator rejects below-floor picks, :799). The Phase C posterior solve uses the same floor as its search box lower bound (`_tp_lo`, tiered_atr.py:908), so the fence holds in that mode too.

**Why 2.0.** Task-12's advisory: fence `tp_atr_min ≥ 2.0` where the 1-2 bucket is measured dead. 2.0 sits below every measured-valid covered band (≥ 4.0) and above the degenerate corner (the 1.57-class picks). Covered picks are provably unchanged — the by_symbol override wins before the global is ever consulted (verified in §6).

**What it does NOT do.** The ruin identity: every bracket is exactly fair on random entries, so geometry cannot manufacture edge. The 4.5/3.0 band's null EV stays `−0.0135 − c` with or without the fence. The fence removes a measured optimizer failure mode (the argmax pricing the tight corner as if it could); it does not move the book's expectation. Nothing about SL is fenced — `sl_atr_min` stays 0.15; the failure mode was tight TP, and `min_rr` already ties SL's ceiling to TP.

**Mechanics.** Read per pick; the pick cache key (`grid_fp`) includes `tp_atr_min`, so the change takes effect on the next uncached solve — no restart, no migration.

---

## 2. `auto_apply_clamps: true → false` — the fence's bodyguard

**The trap.** `apply_symbol_clamps` (atr_reopt.py:568-634) line-patches exactly the six `by_symbol.<SYM>` keys (`tp_atr`, `sl_atr`, `tp_atr_min`, `tp_atr_max`, `sl_atr_min`, `sl_atr_max`) in feature_flags.yaml, and `atr_reopt_urgency` runs it on a 4h cycle (auto_apply read at atr_reopt.py:702) plus the monthly free-lattice solve on the 1st 03:30 UTC. While the reopt's objective is the D1 argmax the review measured broken (tight-corner picks; INVALID bands by its own solve — US30USD band EV −1.592), auto-apply re-imports the failure mode this change set fences out — and would overwrite any operator fence written *into* `by_symbol` (e.g. hard-fencing the INVALID bands, §5 #3).

Two honest clarifications: (a) the *global* floor from §1 is not a by_symbol key and would not be overwritten — this flip protects the by_symbol fences and, more importantly, stops the broken objective from re-tightening live clamps; (b) the monthly reopt was originally operator-GO by contract ("Never mutates dynamic_atr.by_symbol — operator GO applies clamps", yaml comment above `atr_reopt_enabled`); it was the 4h urgency path that made application automatic. This restores the original discipline until Phase C (`pbm_geometry_mode: "posterior"`) re-points the objective the clamps serve.

**What still runs.** Monthly and 4h-urgency reopts still execute, still write their reports (`worklog/kairos/reports/`), still heartbeat `nt_roadmap`, still alert Discord. Only the yaml patch step is withheld. **Revert:** `true` (one line).

---

## 3. The honest cost table — `pbm_cost_atr_by_symbol`

**Recipe (proposal §4).** Per-symbol round-trip cost in ATR units: (spread + slippage) round-trip ÷ 1h ATR — the same ATR already stamped on `nt_signal_sim` rows, so the numbers are denominated exactly where the EV gate spends them.

```
pbm_cost_atr_by_symbol:
  EURUSD: 0.02    GBPUSD: 0.02    USDJPY: 0.02
  AUDUSD: 0.025   USDCAD: 0.025   USDCHF: 0.02
  XAUUSD: 0.015   XAGUSD: 0.04    XAUAUD: 0.03   XAUEUR: 0.03
  BTCUSD: 0.03    AAPL: 0.02      US30USD: 0.025 US500: 0.02
```

**Mechanism (verified this session).** Consumed by `posterior.pbm_cost_atr` (posterior.py:148-157): an **exact-match** dict lookup — there is no `FOREX` alias on this table (the alias lives only in `get_dynamic_atr_config`), which is why the six majors are listed individually; that subtlety was left open by the build. Unlisted symbols fall back to the band's `cost_atr` (0.1). Call sites: the light path's PBM read (orchestrator.py:4343), the heavy twin (heavy_sweep.py:1521), and the Phase C continuous solve (tiered_atr.py:915). `_flag` reads go through the mtime-checked `_load()` (feature_flags.py:433) — the edit is live on the next tick, no restart (verified in §6).

**Why it matters — the arithmetic.** Null EV for the fair-timed 4.5/3.0 band is `−0.0135 − c`. Flat `c = 0.1` forces −0.113: a 3-10× overstatement of honest majors/metals costs that makes the EV lower-bound gate structurally deaf — *nothing* can pass, so the empty book was partly a cost-table artefact. At honest `c ≈ 0.02` the band sits at ≈ −0.034: still no-trade (correct — no measured edge), but now a measured entry-timing gap of ~0.04 in EV space could flip a band positive. The gate regains sensitivity to small real edges while keeping zero tolerance for imagined ones. "Empty book vs small book" was a cost-table question; it is now answered honestly: **the book stays empty until `gap > 0` is measured — and the measurement can actually move it.**

**Boundaries.** (a) The legacy grid walk does not consume the table — only the PBM paths do; the cost table and the Phase C flip belong together, which is why the flag shipped in the PBM block. (b) The values are retail-spread estimates: XAGUSD and BTCUSD honest costs could plausibly run 2-3× the listed numbers at some venues, and majors vary by broker — **replace with measured round-trip ÷ 1h ATR before trusting any flip decision**. (c) `cost_atr: 0.1` (band default) is deliberately untouched — it is the legacy walk's input and the fallback; changing it would alter legacy behaviour this change set does not own.

---

## 4. The entry-gap search harness — `scripts/measure_entry_timing_gap.py`

The one genuinely new design layer. Everything else in the PBM program re-prices; this searches for the *conditioning under which entries are positively timed*.

**The object measured.** Per-signal realized selection gap:

```
g_i = 1[outcome == 'tp_hit'] − p_w_null
```

— realized first-touch win minus the analytic null's first-touch TP probability *for the same bracket*. Negative gap = adverse entry timing (the platform's measured −30pt at tight buckets; 89.5% SL vs 59.9% null). Note the discipline: the ledger's own `gap` column (nt_pbm_shadow, migration 211:30) is posterior−null and is **never** used by the search — that is the model's opinion of itself; `g_i` is reality's opinion of the entry. Section 1 of the report tabulates the unconditional per-(symbol, side) thermometer first — the −30pt measurement's permanent home.

**The search.** Row-native covariates only (nothing invented): symbol, side, UTC 4h hour bucket, vol tier (ledger's, else the sim row's), regime class (bull/bear/neutral parsed from the regime label, mirroring the live direction rule at orchestrator.py:4073), side-vs-regime alignment (with-trend / counter-trend / neutral-regime), qualified flag. Predicate family — all single-dimension conditions plus four pre-registered 2-way conjunctions (side×regime class, side×hour bucket, side×alignment, symbol×side). **No 3-way in production** (synthetic mode extends the family with symbol×side×hour so the injected truth cell is enumerable — stated in the report). The family is small and fixed before looking; the multiple-testing posture is honest because the bar is held-out confirmation, not discovery-half significance.

**The bar (Phase E's, applied at the entry layer).** Chronological 50/50 split by ts. Discovery half (earlier) only *nominates*: mean gap > 0 AND n ≥ 20. Confirmation half (later) is the only thing that can *promote*: mean gap > 0 AND bootstrap 95% CI lower bound > 0 (200 resamples, seed 13, per-predicate re-seeded `default_rng`) AND n ≥ 20 → **PROMOTE**, else **STAY-OFF**. Data: `nt_pbm_shadow` × `nt_signal_sim` joined offline on (symbol, ts, signal) — the migration's documented join; `p_w_null` preferred from the ledger, recomputed via `barrier_null_from_prices` when missing; era-clean via the same cutover marker the drift harness uses, fail-open that says so.

**Verification (all rerun by the coordinator, §6).** `py_compile` OK; ruff clean; `--synthetic` PASS — the injected +0.18 truth predicate (XAUUSD sell 08-11h) is discovered and promoted with confirmation CI [+0.107, +0.401], 2 false promotes ≤ 3 (both are the truth cell's diluted parents — honest statistics at ~2.5% per candidate across 133); the confirmation half visibly kills discovery-half noise (four nominating predicates with +0.02..+0.16 discovery gaps all STAY-OFF on confirmation). No-creds run exits gracefully with the boundary message, no traceback.

**What a PROMOTE means.** The business case for the entry-layer wiring — shadow qualifier stamp first, live qualifier (skip entries when the predicate is absent) second, streamed trigger only for symbols with a measured gap AND a decay signature (SSE worklog §7: predicate before pipe). The harness itself flips nothing, writes nothing, gates nothing. **v2 covariates** deliberately deferred: deviation-Z and regime age need bar joins beyond row-native fields.

---

## 5. What else is in the change set (the "did I miss anything" audit)

1. **`auto_apply_clamps: false`** — the required companion to any fence (§2). Without it the next 4h reopt cycle re-imports the tight corner and overwrites by_symbol fences. Applied.
2. **The majors must be listed individually in the cost table** — `pbm_cost_atr` has no FOREX alias (§3). Handled; an alias-blind table would have silently left all majors at the 0.1 fallback.
3. **No-trade posture on the three INVALID bands (XAUEUR / XAUUSD / US30USD** per the reopt's own EV solve**)** — now the *default* posture rather than a separate switch: with the fence, honest costs, and the EV lower-bound gate, those bands price themselves out. A harder fence (raise the by_symbol `tp_atr_min` above the band top) is an operator decision and is now *durable* because auto-apply is off.
4. **Sizing at maintenance floor** — Masaniello monitor unchanged: it is orthogonal to geometry and the right posture is sized-to-survive until a `gap > 0` cell is measured, not sized to harvest an edge that is not there.
5. **Migrations 210/211 applied; PBM shadow live** (operator confirmed) — the ledger is accumulating Phase B acceptance data now; nothing further to do on this item.
6. **B/C/D flips remain replay-gated** — the checklist in `20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md` §4 is unchanged and remains the only path by which those flags move.
7. **`gap` is a measured column, not a switch** — `nt_cell_posterior.gap` (210:46), `nt_pbm_shadow.gap` (211:30). No flag exists or is needed; the search is SQL/script conditioning of that column.
8. **The picker era filter — known, deliberately NOT applied.** `_load_class_signals` (tiered_atr.py:714-755) pools historical picker evidence with no era/cutover filter (the reopt filters era; the picker does not). It is the remaining wiring fix from the Task-11 diagnosis, and it is deliberately out of this set: it would change live picks for covered symbols — a behaviour change beyond a backstop — and the Phase C flip re-points the objective that evidence feeds, making the pooled-argmax question moot. Decide it at Phase C, not in a change set whose contract is "no covered pick changes".

---

## 6. Verification (rerun after every edit above)

Config resolution (`PYTHONPATH=$PWD python3`, real yaml, mtime path):

```
XAUUSD tp_atr_min = 4.6        (by_symbol wins — covered pick unchanged)
EURUSD tp_atr_min = 4.02       (FOREX alias — covered pick unchanged)
UNCOVERED tp_atr_min = 2.0     (the fence, global scope)
class-walk tp_atr_min = 2.0    (the fence, class scope)
EURUSD cost = 0.02             (table hit)
XAUUSD cost = 0.015            (table hit)
UNKNOWN cost = 0.1             (fallback intact)
auto_apply_clamps = False
pbm flags intact: shadow off grid legacy
```

- yaml parses clean; 14 cost entries; `dynamic_atr.by_symbol` blocks untouched.
- PBM test suite post-edit: **39/39 passed** (14.11s) — `test_barrier_null.py`, `test_cell_posterior.py`, `test_pbm_heavy_guard.py`.
- Harness: `py_compile` OK; `ruff check` clean; `--synthetic` → `SYNTHETIC MODE: PASS` (details §4); no-creds run → graceful boundary message, no traceback.
- `git status` relative to the PBM tree: `config/feature_flags.yaml` (already-modified file, three new blocks), `scripts/measure_entry_timing_gap.py` (new), this worklog (new). No other file touched.

## 7. Rollback

| Change | Revert | Takes effect |
|---|---|---|
| TP fence | `tp_atr_min: 0.20` | next uncached pick (cache key includes the floor) |
| auto-apply | `auto_apply_clamps: true` | next 4h urgency cycle |
| cost table | `pbm_cost_atr_by_symbol: {}` | next tick (mtime hot-reload) |
| harness | delete the file | never had an effect |

## 8. Honest boundaries

- The cost values are estimates. Replace with your broker's measured round-trip ÷ 1h ATR before trusting any flip decision — at honest costs more bands die, fewer pass, and that ordering is only as good as the measurement.
- The fence is a backstop against a measured optimizer failure mode. It is not an edge source (the ruin identity) and does not change any covered symbol's pick.
- The harness has not yet run against real data (no Supabase creds in this sandbox). First credentialed run: `PYTHONPATH="$PWD" python3 scripts/measure_entry_timing_gap.py` — read section 1 (the thermometer) before the search table.
- The expected outcome of the search is **mostly STAY-OFF — and that is the system working**. A PROMOTE is the rare event that earns the qualifier wiring; until then the honest book is the empty book.
