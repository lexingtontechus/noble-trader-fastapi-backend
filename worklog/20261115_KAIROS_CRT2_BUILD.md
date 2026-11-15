# 20261115_KAIROS_CRT2_BUILD.md — CRT-2: claims + monitors

**Scope** (pre-registered by the Task 67 assessment §6, line 91): the CRT v5
CONT paper engine's claims + monitors layer — additive CHECK migration for
`kind IN (..., 'crt_option')` / `source IN (..., 'crt_walkforward')`;
per-option pre-registration at config deploy; CUSUM state rows + rolling-EV
table; confidence-sizing shadow column (variable_kelly precedent). The
claims layer's own honesty contract: immutable once written except
`status/outcome/resolved_at`.

## 1. What landed

| Piece | Where | Notes |
|---|---|---|
| Migration 221 | `supabase/migrations/221_crt_claims_monitor_state.sql` | Widens both `nt_signal_claims` CHECKs (116/217 drop+re-add pattern, old values retained); creates `nt_crt_monitor_state` + `nt_crt_rolling_daily` |
| Claims sibling | `crt/claims.py` | Mint at fill / resolve at close / aggregate kernels |
| Monitor persistence | `crt/state_store.py` | Pre-registration + throttled flush + daily rolling |
| Engine hooks | `crt/engine.py`, `crt/gates.py`, `crt/paper.py` | Claims opt-in via config; freeze-time monitor stamp; `signal_id` hydration |
| Job hooks | `crt/jobs.py` | Pre-registration at rebuild, throttled flush, daily snapshot, snapshot blocks |
| Read surface | `crt/router.py` | `GET /crt/claims` (pooled + per-option evidence) |
| Config | `config/crt/crt_engine.yaml` | `claims:` + `monitors:` blocks (self-contained contract holds) |
| Flag comment | `config/feature_flags.yaml` | Comment-only CRT-2 note; `crt_mode` still `"off"` |

## 2. Design decisions (the fidelity manifest)

1. **One claim per paper fill, minted at the fill moment** (C3 close), keyed
   by the paper row's own `signal_id` — `unique (signal_id, kind)` makes the
   claim the position's ledger sibling. NOT a config-deploy claim row: the
   assessment's "pre-register per-option claims at config deploy" lands as
   the `nt_crt_monitor_state` pre-registration rows (the artifact's promised
   numbers + thresholds BEFORE evidence exists) — a claim without a trade
   would have nothing to resolve against; the per-trade claims carry the
   per-option pre-registration inside `claimed_probs` (oos_ev/sd/t/n,
   decay/rolling state at freeze, thresholds live in the state rows).
2. **Resolution through CRT's OWN close pass** — the single-resolver
   contract extended, not bent: the labeler's `walk_barriers` produced the
   position's verdict (the same walk the paper PATCH carries); the claim
   echoes it, never recomputes it. The platform EOD machinery never touches
   CRT claims (its hooks pop `nt_signal_sim`-keyed claims; CRT signal_ids
   are the paper rows' self-minted UUIDs). Brier fields reuse
   `claims_resolver.brier_fields` — the one scoring-kernel source.
3. **`claimed_probs` = the frozen C2-close decision inputs**: implied
   p_win = (OOS EV + 1)/(R + 1) (fixed-R inversion, clamped), oos_ev/sd/t/n,
   decay state + CUSUM S + live/rolling stats AS CONSUMED AT FREEZE (stamped
   on `GateDecision.monitor` by the engine — no lookahead, no hindsight), the
   confidence-sizing `risk_pct`, and the **variable-Kelly SHADOW**
   (`f_binary`/`f_half` recorded, never a sizing input — migration-174's
   discipline; the engine stays EA-verbatim confidence sizing).
4. **Monitor persistence, one writer per table** (the 150 precedent):
   `nt_crt_monitor_state` = ONE row per (symbol, tf, dir, session) — the
   deploy pre-registration + throttled live flush (signature-change OR
   `flush_seconds` TTL; bounded writes, never per-tick chatter).
   `nt_crt_rolling_daily` = ONE row per (UTC day, option) — the 163
   daily-upsert discipline; fires on day change AND once per process start
   (idempotent merge). The TF pool columns are the weighted mean of the
   enabled options' (rl_n, rl_ev) — algebraically identical to
   `ComputeRolling`'s pool (same window filter); documented on the table.
5. **Fail-open everywhere**: mint/resolve/flush failures log and return
   False/0 — a lost stamp is a lost stamp (the platform claims contract);
   the paper book stays the durable record. `claims.enabled` absent → hooks
   OFF (CRT-1 byte-identical when the config block is missing — verified).
6. **Mint awaits BEFORE the ambiguous-C3 close** in the same tick, so
   mint/resolve can never race on one signal_id; the resolve PATCH is
   guarded by `status=eq.open` (a claim voided by the EOD 14d safety net is
   never re-touched).

## 3. Sanctioned translations

- CRT outcome codes → settlement vocabulary: TP→`tp_hit`, SL→`sl_hit`,
  TIME→`expired` (the names `brier_fields` reads; TIME carries r — the
  expired-is-data discipline).
- No `p_resolve` claimed (CONT can time-exit; OOS stats carry no
  resolve-rate) → no `brier_resolve` on CRT outcomes.
- `deadline_ts` = the option's own barrier horizon (ts + hold×tf_seconds);
  `horizon_h` the same in hours — CRT's hold replaces the platform's
  48-bar window (documented divergence, per-option by design).

## 4. Seams touched (all additive)

- `crt/gates.py`: `GateDecision.monitor: dict | None = None` (provenance
  only — no gate rule reads it).
- `crt/paper.py`: `SELECT_OPEN` += `signal_id` (claim sibling key).
- `crt/engine.py`: claims import + cfg read; freeze-time monitor stamp;
  mint at emit; resolve at close; `PositionState.signal_id`; decay
  transition "monitor" signal (bounded by transition rarity).
- `crt/jobs.py`: pre-registration/flush/daily hooks + `_MON_STATUS` +
  snapshot blocks + `claims` key in the engine_cfg filter + artifact_runid
  injection.
- `crt/router.py`: `/crt/claims` (read-only, never raises).
- Platform modules: ZERO edits (claims.py / claims_resolver.py /
  claims_leaderboard.py / paper_book.py / eod_resolver.py untouched).

## 5. Verification (`/home/z/verify/crt2_verify.py`, 94/94)

A(20) claim kernels: implied p_win math/clamps/degenerates; kelly shadow
(negative-f honesty); claim row (kind/source/space, deadline = hold×tf
math, horizon_h, OOS provenance, freeze-time monitor stamp, risk ladder
0.3125 at t=1.5 on the 0.25→0.50% rungs, column whitelist); outcome
mappings TP/SL/TIME + realized_pnl + zero-cash honesty.
B(10) aggregation: pooled n/tp/sl/expired, EV/sd/t/LCB hand-math (32 rows
by hand), win-rate closed-only + Wilson bounds, per-option cells,
empty-pool zeros, Wilson formula vs published value (0.236635/0.763365 at
p=.5 n=10).
C(11) state_store: monitor row (keys, pre-registration verbatim,
thresholds/rolling_gate jsonb, live fields, column whitelist), tf_pool
enabled-weighted identity, rolling row, signature change detection,
utc_day.
D(8) upsert I/O: on_conflict params (both tables), Prefer
merge-duplicates header, URL, column filtering, empty rows no-op,
unconfigured fail-open 0, mint posts to nt_signal_claims with the
whitelist, signal_id required.
E(12) ENGINE integration with fakes: claims-block-absent compat (fill,
zero mints — the CRT-1 contract); golden path freeze→stamp→fill→mint
(claim keyed to the paper row's signal_id, ts = open_ts, deadline math,
p=0.42/risk 0.3125/runid stamp) → TP resolve → claim resolved with the
SAME verdict (r/exit/pnl echo); ambiguous-C3 mint-then-resolve once each;
mint failure fail-open (fill proceeds); resolve failure fail-open (paper
patch intact); hydration carries signal_id.
F(11) jobs: dark mode → zero fetches AND zero persistence writes;
pre-registration rows = option count with base_ev/artifact_runid; daily
rolling on first tick; `_MON_STATUS` surfaced; snapshot gains claims +
monitors blocks; idle tick writes nothing; throttle (no change + TTL unmet
→ no write; signature change → flush; TTL elapsed → flush); day rollover;
monitors.persist off → nothing.
G(6) router: /crt/health green; /crt/claims math + limit clamp; /crt/claims
never raises (fail-open view); /crt/state carries claims + monitors in
shadow; dark note intact when off.
H(10) migration 221: kind/source CHECK widenings with old values
retained + drop-if-exists; both tables' PKs; CHECKs; RLS/policies; notify
pgrst; yaml claims/monitors blocks ship on; discord events include
monitor; crt_mode still "off"; flag comment documents CRT-2.
I(6) resolve chain: PATCH guards (signal_id + kind + status=eq.open), brier
from the claim's OWN frozen probs ((0.42−1)² = 0.3364 on tp_hit), patch
body exactly {status, outcome, resolved_at}, voided claim never
re-touched, missing claim honest False, empty signal_id no-raise.

Regressions: **crt1_verify 122/122** (the engine changes are
byte-compatible with the claims block absent); CRT-0 selftest RC=0
end-to-end (157k rows, 3 folds, 36 options minted); claims-adjacent set
test_eod_resolver(35) + test_eod_resolver_basis(18) + test_paper_book(13)
+ test_monitor_paper_book(9) + test_settlement_contract(23) +
test_settlement_expiry(14) + test_resolver_head_of_line(21) +
test_variable_kelly(11) = **144/144**; test_router_edge +
test_strategy_registry **46/46**; ruff clean; py_compile clean; yamls
parse. Initial failures: 2 test-expectation bugs of mine (312.5×1.5
arithmetic; gate-on cfg used for a golden-path fill) — the code was right
both times.

## 6. Operator steps

1. Apply migration 221 manually (Supabase SQL editor / management API),
   like 210–220: `supabase/migrations/221_crt_claims_monitor_state.sql`.
2. Flip `crt_mode` → `shadow` (hot-reload, no restart). With
   `config/crt/crt_engine.yaml` as shipped, claims mint at every paper
   fill and monitor state persists immediately.
3. Watch `GET /crt/claims` (pooled + per-option evidence) and
   `nt_crt_monitor_state` / `nt_crt_rolling_daily`.

## 7. Honest limits

- The leaderboard's `/claims/leaderboard` will pick up `crt_option` cells
  automatically once rows resolve (`aggregate_claim_cells` groups by
  (symbol, kind)) — CRT-2 does NOT touch `claims_leaderboard.py`; CRT's own
  `/crt/claims` view mirrors the math instead. The two views coexist.
- No real data has flowed through the engine yet (the artifact is still
  unminted by the operator); everything above is synthetic-end-to-end.
- CRT claims are scored by CRT's close pass, not the platform EOD job —
  the single-resolver contract holds per-family, and the boundary is
  documented in both modules.
- The 14-day `void_stale_claims` safety net applies to CRT claims too (a
  claim stuck open >14d — position lost between patches — voids, never
  resolves): intended, honest.
