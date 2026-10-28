# Kairos claims layer — Phase B build (transport: matview + broadcast, flag-gated)

**Date:** 2026-10-28
**Type:** Implementation (Phase B of `worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md` §4;
the operator applied migration 218 the same day, activating Phase A's shadow ledger).
**Scope guard:** additive, transport-only, `claims_publish: "off"` by default.
Zero changes to geometry, EV, gates, p_win semantics, MT5 payload contracts,
or any calibration loop. The ledger's write path (Phase A) is untouched.

---

## 0. What landed

| Piece | File | What it does |
|---|---|---|
| Matview extension | `supabase/migrations/219_extend_latest_signals_claims.sql` | `v_talaria_latest_signals` gains `to_jsonb(cl) AS claim` — the LATEST claim per symbol (any status) via a 4th `LEFT JOIN LATERAL` on `nt_signal_claims` (the 183 pattern). **Operator must apply manually** (like 218); requires 218. |
| Flag | `feature_flags.py` + `feature_flags.yaml` | `claims_publish: "off"` — the `claims_mode` registration triple, replicated (`_DEFAULTS` + yaml block + `get_claims_publish()`; plus the in-module `claims_publish()` lazy reader). |
| Broadcast transport | `claims.py::register_and_publish_bracket_claim` | One fire-and-forget task = Phase A registration + Phase B publish: after the claim PERSISTS and only when `claims_publish = "live"`, re-publishes the signal broadcast with the claim row attached (`p_clef_wait` optional-field convention). |
| Payload authority | `realtime_publisher.py::build_signal_payload` | New `claim: dict | None` kwarg — omitted when None; passed through verbatim (already column-whitelisted, price-space at the boundary). |
| Wiring (light) | `orchestrator.py` | Broadcast payload built into `_signal_broadcast` (one authority), primary broadcast unchanged in timing/shape; the claims task now takes `broadcast_payload` + `plan_ids`. |
| Wiring (heavy) | `heavy_sweep.py` | Heavy twin, same structure. |
| Bundle | — (zero code) | The dashboard bundle's `sweeps` key is `to_jsonb(s)` over the matview (migration 193's `refresh_talaria_dashboard_cache`), so the claim column rides `/api/dashboard-data` + `talaria_dashboard_cache` **with zero function changes**; pg_cron job 924 refreshes the matview unchanged. The frontend route's `select("*")` picks the column up for free. |

## 1. Design decisions (and why)

**Two transports, one claim shape.** Transport #1 (ungated, DB-side): the matview
`claim` column — latest claim per symbol, LATERAL keyed on `symbol` (NOT
`signal_id`; there is no matview↔sim-row id join, exactly like the 183 rank
stamp), any status, `outcome` included so resolved claims carry their score to
the dashboard. May lag the sweep row when the latest sweep is neutral — the
claim's own `ts`/`signal_id` are exposed for honest staleness display, and the
migration comment says so. Transport #2 (flag-gated, wire): the broadcast
`claim` object. Both emit the same row shape (the Phase A `nt_signal_claims`
row), so the frontend sees ONE claim contract from both paths.

**Fan shown ⟺ ledger holds it.** The rebroadcast fires only AFTER
`record_bracket_claim` returns a persisted row. A failed stamp (insert error,
table missing, flag off) never surfaces as a fan on the dashboard — the honest
ordering. The cost: when `claims_publish = "live"`, one qualified signal emits
TWO broadcast events (primary without claim, immediately; then the same
payload + claim ~100–500 ms later). Frontend rows replace per-symbol; the
EA/bridge dedupes on `symbol|sweep_timestamp`; talaria-copy never reads the
broadcast column-wise (`SIGNAL_COLUMNS` allow-list) — over-delivery is benign,
per the `_plan_signal_topics` doctrine. The primary broadcast's latency and
shape are unchanged in ALL modes.

**The matview extension over a sibling view.** The design doc offered both.
The matview route (a) makes the claim ride the pg_cron-refreshed bundle with
zero function changes, (b) follows the exact 183 DROP-CASCADE + recreate +
unique-index + kelly-view-rebuild + grants precedent (179/183/184/193 all did
this in production), and (c) fails closed if 218 is missing (CREATE fails in
transaction, matview untouched). `v_talaria_kelly_by_symbol` is rebuilt
byte-identically WITHOUT the claim column (sizing surface ≠ claims surface).
Grants match 183 (`anon, service_role`) — claims content is the same
market-data class the matview already exposes; RLS on `nt_signal_claims`
itself is unchanged.

**`claims_publish` gates only the wire.** The matview/bundle is ungated
DB-side state (harmless, service-role-read in practice); the flag exists for
the one live-behavior change (the second broadcast event). `claims_mode`
"live" still behaves as "shadow" — the flags stay orthogonal, and the flip
discipline is the pbm_* one: evidence, not calendar.

## 2. Verification (all offline; no test files added, per the sandbox discipline)

* **Ruff**: new/edited files — `claims.py`, `realtime_publisher.py`,
  `feature_flags.py` clean; `orchestrator.py` + `heavy_sweep.py` = 50
  findings = **exactly the HEAD baseline** (verified via `git stash`
  round-trip; zero new). `py_compile` on all 5 touched modules.
* **Existing suites**: `test_realtime_publisher / test_eod_resolver /
  test_signal_sim_capture / test_settlement_contract / test_pbm_heavy_guard /
  test_barrier_null` → **119/119 pass**. Broader `-k "flag or wiring or eod
  or signal_sim or claims or realtime or publisher"` sweep → **279 pass**,
  1 fail (`test_e2e_registry_wiring` — pre-existing at HEAD, documented in
  the Phase A build log), 2 collection errors (missing optional deps,
  pre-existing, excluded).
* **Offline round-trip** (stubbed httpx + captured broadcasts, the Phase A
  convention): (1) `build_signal_payload(claim=…)` attaches/omits per the
  p_clef_wait convention, other fields byte-identical; (3) `claims_publish()`
  default `off`; (2a) flag off → 1 POST, **0** rebroadcasts; (2b) flag live +
  insert 201 → exactly 1 rebroadcast whose `claim` IS the persisted row
  (48-row fan, q05<q50<q95, plan_ids forwarded, base payload otherwise
  identical); (2c) flag live + insert 404 → no row, **no** rebroadcast;
  (2d) flag live + no `broadcast_payload` → pure Phase A registration.
* **Migration 219 syntax**: parsed with sqlglot (postgres dialect); the only
  constructs it cannot parse (`COMMENT ON … IS`, `notify pgrst`) fail
  identically on migration **218** — which the operator applied successfully
  the same day — so they are parser gaps, not SQL errors. The matview body
  (incl. `to_jsonb(cl)` + the new LATERAL) parses cleanly; column names in
  the claims LATERAL were cross-checked against 218's DDL.

## 3. What the operator must do

1. **Apply `supabase/migrations/219_extend_latest_signals_claims.sql`** in
   Supabase (SQL editor / management API), after 218 (already applied). Until
   then the bundle path simply has no `claim` column — nothing else changes.
2. Nothing else. `claims_publish` ships `"off"`: the broadcast wire carries no
   claim object. Flip to `"live"` (mtime hot-reload, next qualified signal)
   only after migration 219 is applied AND the Phase C `SignalContextCard`
   has been verified in the dashboard (it shipped the same day in
   nobletradingapp — see that repo's `docs/worklog-kairos-claims-phase-c.md`).

## 4. Honest limitations

* The bundle's claim is **latest-per-symbol**, not
latest-per-signal: when the latest sweep is neutral (no new claim), the
dashboard keeps showing the symbol's previous claim — correct (the claim is
frozen evidence, not live state), and the card displays the claim's own ts so
staleness is visible.
* `claims_publish = "live"` doubles the broadcast event count per qualified
signal (one extra websocket publish per topic) — bounded by qualification
frequency, noted for the ops budget.
* `outcome` on the wire (transport #2) is always absent at registration time
by construction — outcomes reach the dashboard through the bundle path
(5-min pg_cron cadence) after the resolver scores the claim. No resolver-side
broadcast was added (deliberate: the EOD resolver stays write-only).
* The migration hard-drops and recreates the matview (183 precedent): a
brief non-concurrent REFRESH at apply time, inside the transaction.
