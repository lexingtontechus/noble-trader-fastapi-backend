# 20261115_KAIROS_CRT3_BUILD.md — CRT-3: the leaderboard-gated live promotion

**Scope** (pre-registered by the Task 67 assessment §6, line 92, verbatim):
"the claims leaderboard arbitrates promotion to `live` exactly as it
arbitrates the TSFM sidecar — no operator eyeballing; this closes the loop
with the lab's standing question ('is the sidecar/CRT-class edge real after
costs?')". Built on the CRT-2 claims ledger (commit 1de875f).

## 1. What landed

| Piece | Where | Notes |
|---|---|---|
| Verdict kernel | `crt/promotion.py` | Pure `promotion_verdict` + evidence aggregation through `crt.claims.aggregate_evidence` (single source) + TTL cache |
| Bars | `config/crt/crt_engine.yaml` `promotion:` block | Pre-registered: the optimizer's OWN enable thresholds on live paper claims |
| Enforcement | `crt/jobs.py` `_apply_promotion` | `live` + unmet bars → paper semantics + recorded refusal; bars met → live dispatch |
| Read surface | `crt/router.py` | `GET /crt/promotion` + `/crt/state` promotion block |
| Flag comment | `config/feature_flags.yaml` | Comment-only live-rung description; `crt_mode` still `"off"` |

## 2. The bars (pre-registered — the mint thresholds gate the promotion)

The optimizer's own enable thresholds (README pipeline: OOS n≥30 ∧ EV>0.02
∧ t≥1.0 ∧ final LCB>0), applied to LIVE paper claims instead of the research
corpus — the same numbers that minted the config gate its live promotion:

- `min_n: 30` — resolved crt_option claims
- `min_ev: 0.02` — mean R **after costs** (the labels carry the
  spread/slippage cost model — the standing question answered directly)
- `min_t: 1.0` — t = mean/(sd/√n)
- `lcb_z: 1.28` with LCB > 0 — the max-LCB walk-forward convention
- `require_decay_not_off: true` — no ENABLED option in decay OFF (engine
  health; DISABLED options excluded by construction)

Demotion on evidence decay is AUTOMATIC by construction: the verdict is
recomputed on a TTL cache (default 300s) every live tick — no operator
action, no calendar. `cache_seconds` is the only operational knob.

## 3. What "live" means under the standing contract (the honest scope)

The only live path that exists under the 5-clause contract (MT5 untouched,
no nt_signal_sim writes, no order execution, price space at boundary):
**the #crt signal stream becomes the actionable surface** — while the bars
hold, engine events carry a `[LIVE]` prefix (read at call time from
`_LIVE_DISPATCH`, so a mid-process bar flip changes the stream immediately).
Unmet bars: paper semantics continue, the refusal is recorded on the tick
stats (`live_refused` — survives even the boundary-gated idle return) AND
surfaced at `/crt/promotion` — the refusal is never silent. Eligibility
changes signal to #crt labeled **CRT-3** (`PROMOTED to live dispatch…` /
`live REFUSED — bars unmet…`, bounded: state-change transitions only). No
order execution exists or is planned under this contract; a broker path
would be CRT-4 and an explicit operator decision.

## 4. Enforcement points (all flag-gated, all fail-open)

- `crt_mode: off` → the tick returns before any fetch — unchanged (zero
  DB reads; verified F1/J1-style).
- `crt_mode: shadow` → `_apply_promotion` is a pass-through (no verdict
  fetch — shadow stays DB-light); the verdict is still surfaced on demand at
  `/crt/promotion` + `/crt/state` (TTL-cached).
- `crt_mode: live` → verdict fetch (TTL-cached) + dispatch flag + refusal
  recording + eligibility-change signal.
- Fetch failure → the last cached verdict; no cache at all → the honest
  zero-evidence verdict (n=0 → not eligible). Never raises.

## 5. Verification (`/home/z/verify/crt3_verify.py`, 42/42)

A(4) bars loading (defaults, yaml override, no-cfg, None-value fail-open);
B(3) decay_off_count (enabled+OFF only, DISABLED excluded, broken objects
tolerated); C(9) verdict kernel — each bar independently unmet (n / EV
after-costs / t / LCB / decay-OFF), all-met eligible, decay bar switchable,
zero evidence refuses every bar (4 reasons — never silent), evidence echoed;
D(6) TTL cache (hit / force / elapsed) + fail-open chain (fetch failure
keeps the cached verdict; no cache → honest zero evidence); E(9)
`_apply_promotion` — shadow pass-through (no fetch, no keys, no signal),
live refusal recorded + signaled once (first observation) then bounded,
bars met → dispatch + stats.live, evidence decay mid-flight → auto-demotion
+ refusal + signal, decay-OFF engine blocks eligibility, `[LIVE]` title
prefix on/off through `_signal_fn`; F(5) router — `/crt/promotion` shape
(crt_mode + eligible + evidence + bars + decay_off), off-mode dark note
intact, live/shadow state views carry the promotion block, never raises
(empty-evidence verdict); G(6) yaml bars pre-registered, discord events
include promotion, flag comment + crt_mode still off, FULL-TICK
integration — live tick with eligible evidence → stats.live + dispatch on
+ PROMOTED signal; evidence decayed → next tick auto-demotes (refusal
recorded); idle boundary tick keeps the refusal visible.

Regressions: **crt1_verify 122/122** (engine untouched); **crt2_verify
94/94**; claims-adjacent + router/strategy set (eod_resolver 35,
eod_resolver_basis 18, paper_book 13, monitor_paper_book 9,
settlement_contract 23, settlement_expiry 14, resolver_head_of_line 21,
variable_kelly 11, router_edge + strategy_registry 46) = **190/190**;
ruff clean; py_compile clean; yamls parse. Initial failures: 3
test-harness bugs of mine (a decay-OFF stub reused where a clean engine was
intended; E2 pointed at eligible evidence; a fake-`fetch_open_positions`
signature) — the code was right each time. One real ordering bug found and
fixed DURING the build (never reached a suite): `stats` was referenced
before creation in `crt_engine_tick`; the idle early-returns were also
repaired to carry the refusal (never-silent contract).

## 6. Operator steps

1. Migration 221 (CRT-2) applied; CRT-2 claims accumulating in shadow.
2. Flip `crt_mode` → `live` when you want the gate ARMED (hot-reload, no
   restart): paper semantics continue until the ledger says otherwise;
   `/crt/promotion` shows the bars; #crt carries `CRT-3 | PROMOTION`
   notices on each eligibility flip.
3. The lab's standing question — "is the CRT-class edge real after
   costs?" — is answered by the same rows that gate the promotion:
   `/crt/claims` (pooled EV after costs, t, LCB, win rate + Wilson).

## 7. Honest limits

- Promotion needs ≥30 resolved claims BEFORE it can pass — with the thin
  H1 windows (README: 5–20 setups/45d) the pooled bar can take weeks of
  shadow time. That is the contract working as designed, not a defect
  (evidence, not calendar).
- `decay_off_count` reads the LIVE in-memory engine states (the operating
  truth); on a cold first tick states are pre-replay (NORMAL) — the
  decay-off bar self-heals on the first bar close.
- The `[LIVE]` stream is a signal surface, not execution: any broker path
  (CRT-4) is a separate, explicit operator decision outside this contract.
- No real data has flowed through the engine yet (artifact still
  operator-side); everything above is synthetic-end-to-end.
