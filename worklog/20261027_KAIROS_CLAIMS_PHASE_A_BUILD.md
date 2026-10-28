# Kairos claims layer — Phase A build (registration + resolver scoring)

**Date:** 2026-10-27
**Type:** Implementation (Phase A of `worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md`).
**Scope guard:** additive, shadow-first, flag-gated (`claims_mode: "shadow"`).
Zero changes to geometry, EV inputs, gates, p_win semantics, MT5 payloads, or any
calibration loop — the design doc's Phase A non-goals, enforced below by construction.

---

## 0. What landed

| Piece | File | What it does |
|---|---|---|
| Table | `supabase/migrations/218_nt_signal_claims.sql` | `nt_signal_claims` (211 house style): uuid PK, `unique (signal_id, kind)`, `(symbol, ts desc)` + partial-open indexes, RLS enabled (service-role), column comments, `NOTIFY pgrst`. **Operator must apply it manually** (SQL editor / management API) — until then every write 404s into the fail-open no-op path and nothing else changes. |
| Flag | `feature_flags.py` + `feature_flags.yaml` | `claims_mode: "shadow"` — `_DEFAULTS` entry + commented yaml block + `get_claims_mode()` getter (the `pbm_mode` registration triple). Mtime hot-reload; `off` = full no-op. |
| Registration | `sweep_orchestrator/claims.py` (new) | `build_atr_normal_fan` (price-space q05..q95, hourly h=1..48 = the settlement window bars), `fan_quantile_position` (knot interpolation → tp_q/sl_q), `build_bracket_ref`, `record_bracket_claim` (fail-open POST, `posterior.record_pbm_shadow` write pattern, module-owned `NT_SIGNAL_CLAIMS_COLUMNS`). |
| Wiring (light) | `orchestrator.py` | UUID pre-minted in `_build_signal_sim_payload(signal_id=...)`; claim dispatched fire-and-forget inside the `if qualified:` block — the SAME population the paper book opens and the broadcast publishes. |
| Wiring (heavy) | `heavy_sweep.py` | Heavy twin, one definition both paths. |
| Scoring | `sweep_orchestrator/claims_resolver.py` (new) | `ensure_claims_index` (run-scoped ledger prime, `_ensure_reach_index` precedent), pure kernels (`_pinball`, `fan_metrics` → coverage50/80/90 + pinball + CRPS=2×pinball, `brier_fields` → brier_resolve/brier_win), `score_claim_outcome`, `score_claims_for_signal` (the hook), `void_stale_claims` (bulk PATCH hygiene). |
| Resolver hooks | `eod_resolver.py` | 3 additions: index prime at the top of `resolve_pending_signals`; claims hooks in `_resolve_one` AND `_resolve_one_bars` (the PBM-hook precedent: lazy import, try/except → `logger.debug`). |
| EOD part | `orchestrator.py` `_run_eod_lookback` | Stale-claim void sweep after Part II (orphaned open claims older than 14d → `status='void'`, `outcome={result: void, reason: stale_open}`). |
| signal_id linkage | `supabase_client.py` `log_signal_sim` | `if not sim_payload.get("signal_id")` guard — honors the caller's pre-minted UUID, byte-identical for every legacy caller. |

## 1. Design decisions (and why)

**Population = the qualified book.** Claims register inside the `if qualified:`
block — the same population the paper book opens and the broadcast publishes
(pre-suppression `qualified` == `_gates_pass`, the acted-on book). Gate-fail
signals get no claims (they're the noise the gates already rejected);
cooldown/dedup-suppressed-but-gate-passed signals DO get claims, matching the
paper book exactly — and their sim rows already carry `gate_outcome`/
`suppress_reason` for later slicing. Widening to the full captured population
is a one-line change if Phase E wants the counterfactual slice.

**The fan is the house ATR normal-approximation — labeled honestly.** The
three-Explore-agent fan-source inventory found NO live multi-horizon quantile
source: TimesFM live is a horizon-1 q10/q50/q90 3-point research stamp
(never a fan, never gates); `vol_backtest/` (incl. the GJR-t/EGARCH-t arms
from a64a624) is research-only and not wired into the sweep; the HMM
simulator is an on-demand API, not a sweep-path producer. So the Phase A
bracket fan is `entry·exp(z_q·σ·√h)` with σ = ATR/entry × `pbm_sigma_atr`
(the barrier.py house convention, 1 ATR/bar log-vol), converted log→price
ONCE at registration (the design §1 units contract). Provenance
`source='atr_normal'` — one value beyond the design doc's three, added for
the same reason the doc demands provenance: claims are evidence. vol_fan
claims register only when a real fan source exists — today, never; the code
path and schema are ready for Phase D.

**The strongest possible no-second-resolver reading.** Claims are scored
INSIDE `_resolve_one`/`_resolve_one_bars` — after the signal row's terminal
PATCH, using the SAME `resolved` verdict dict the settlement contract
produced and the SAME post-entry bar window the signal was resolved on.
Not even a second bar fetch: the two ledgers cannot drift on window
anchoring, basis, or policy because they literally share one walk. This is
the PBM-hook precedent (lazy import breaking the import cycle, try/except,
both paths) applied one hook later in the same functions.

**h_ref for tp_q/sl_q.** The deadline tier's `median_hours` when stamped
(light path), else 24h (documented stamp convention, not a forecast), capped
to the 48-bar window. `tp_q`/`sl_q` are read-only translations ("TP at the
fan's q72, SL at q38" for the 3-ATR/1.5-ATR buy bracket in the sanity run) —
never inputs to level selection.

**brier_resolve scores expired as unresolved — over ALL outcome classes.**
A claim that expires is a falsified resolution claim; excluding it (the
`nt_calibration_records` convention) would flatter p_resolve. `brier_win`
scores p_win_given_resolve on tp/sl rows only. π_win/π_loss/π_expire are
carried in `claimed_probs` as evidence for the Phase E leaderboard.

**Fail-open everywhere, one retry cadence.** Flag off / table missing
(migration not yet applied) / fetch error / insert error ⇒ no-op, claim
stays open, next run re-primes — the next-cron-tick-is-the-retry convention.
A failed claim PATCH pops nothing it can't recover (the row stays open in
the DB; the index re-primes next run). The 14d void sweep bounds the pool.

## 2. Verification (all offline; no test files added, per the sandbox discipline)

* **Ruff**: new files 0 errors; edited files 56 = exactly the HEAD baseline
  (129 repo-wide unchanged in character; zero new).
* **py_compile** on all 7 touched files.
* **Existing suite**: `test_eod_resolver / test_eod_lookback_wiring /
  test_signal_sim_capture / test_settlement_contract / test_eod_resolver_basis /
  test_pbm_heavy_guard / test_barrier_null` — **129/129 pass**. Broader
  `-k "flag or wiring or eod or signal_sim or claims"` sweep: 257 pass,
  1 fail (`test_e2e_registry_wiring` calibration-router threshold — verified
  pre-existing at HEAD via stash), 2 collection errors (missing optional deps,
  also pre-existing).
* **Math sanity** (pure functions): fan — 48 rows, monotone in q, symmetric
  (q95/q05 mirror in log space), √h band scaling (w4/w1 = 2.000), house σ
  exact (q95 = 100·e^{1.6449·0.01}), degenerate inputs → None (never invents).
  Quantile positions — knot-exact (0.95 at the q95 value), mid-knot
  interpolation (0.625 at the q50/q75 midpoint), clamped extrapolation.
  Pinball kernel — d≥0 → d·q, d<0 → −d·(1−q). Coverage identities —
  realized-at-median ⇒ cov50/80/90 = 1.0; far-above ⇒ 0.0; partial window ⇒
  `n_scored_h` honest. Brier — expired=unresolved, brier_win tp/sl-only.
  `score_claim_outcome` — verdict echo, exit_ts = ts + hold_bars, JSONB-safe.
* **Monte-Carlo calibration** (the strongest check): 200 synthetic claims,
  realized drawn from the SAME log-normal the fan asserts ⇒ empirical
  coverage **cov50 = 0.501, cov80 = 0.796, cov90 = 0.900** vs nominal
  0.50/0.80/0.90 — the fan math and the scorer are calibrated end-to-end.
* **End-to-end hook round-trip** (stubbed httpx, `settings.SUPABASE_URL`
  forced): index prime → hook fires on terminal resolve → exactly ONE
  `PATCH nt_signal_claims claim_id=eq.…` with `status='resolved'` +
  full outcome JSONB → re-resolve is a no-op (score-once) → 404/table-missing
  ⇒ warning + empty index + hooks no-op (fail-open) → `claims_mode=off`
  disables priming AND scoring → registration POSTs a complete row
  (deadline=+48h, 48-row fan, h_ref=20, tp_q/sl_q, claimed_probs incl. the
  barrier null triple summing to 1).

## 3. What the operator must do

1. **Apply `supabase/migrations/218_nt_signal_claims.sql`** in Supabase (SQL
   editor or management API), like 210–217. Until then everything below is a
   silent no-op (by design).
2. Nothing else. `claims_mode` ships `"shadow"`; the sweep picks it up on the
   next tick (mtime hot-reload). First claims appear at the next qualified
   signal; first scored claims ~48h later at the next resolver run.

## 4. Honest limitations (measured, not argued)

* The ATR normal-approx fan is a **baseline**, not an edge: it asserts
  zero drift and √h scaling. Its coverage WILL degrade on persistent trends
  (the MC check only validates the math against its own assumption). That is
  the point: it is the control arm the Phase-D recalibrated vol fans and the
  Phase-E "kairos = arm 11" leaderboard must beat — the lab's measured
  finding is that honest fans + vol is where the edge lives, and BTCUSD vol
  6h CRPS-ratio 0.727 vs free fans is the bar.
* `horizon_h` is fixed 48h (the settlement window under the shipped
  `settlement_expiry_mode: fixed`). If that flag ever flips to live/dynamic,
  `deadline_ts` remains a registered-at-freeze artifact — documented, not
  silently recomputed.
* One claim per signal per kind (`unique (signal_id, kind)`); re-qualification
  creates a new signal_id and a new claim (the capture convention).
* Phase A registers NO vol_fan claims (no live fan source exists — see §1).
  The design doc's `tsfm_sidecar`/`vol_backtest_recalib` sources are Phase B/D
  wiring, behind the same flag family.

## 5. Phase B+ hooks left ready (not built)

`claims_publish` (Phase B transport: matview join + dashboard-data + optional
broadcast `claim` object via the `p_clef_wait` fail-open convention),
`SignalContextCard` (Phase C frontend, the nobletradingapp fan-chart
precedent), GJR-t/EGARCH-t live fan arms + recalib k-factors (Phase D),
claims-vs-arms leaderboard + `claims_prior_mode` (Phase E).
