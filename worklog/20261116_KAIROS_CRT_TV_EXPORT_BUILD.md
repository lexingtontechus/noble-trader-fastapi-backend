# CRT Path B — the TDVA-basis research corpus export (2026-11-16)

Task 74 offered Path B: a TV-basis corpus generator so `config/crt_cont.json`
can be minted WITHOUT MT4, by replaying the CRT export schema on TradingView
(TDVA) candles. The operator greenlit it ("build Path B") with
`DISCORD_WEBHOOK_CRT` confirmed set and `crt_engine.yaml` explicitly left
as-is (values mirrored, file untouched). This is the build record.

## 1. What landed

```
regime_edge_crt/tv_corpus.py        NEW   pure corpus math (no network, no
                                          regime_platform imports)
scripts/run_crt_tv_export.py        NEW   CLI: TDVA fetch + cache + export
config/crt/crt_tv_export.yaml       NEW   self-contained export config
regime_edge_crt/config.py           +     TV_EXPORT_DEFAULTS + loader (additive)
regime_edge_crt/report.py           +     optional metas param -> basis block
scripts/run_crt_lab.py              +     passes metas (2 call sites, additive)
```

Zero platform-module edits. Zero migrations. Zero live surface. `crt_mode`
untouched. MT5 untouched. The 5-clause contract holds.

## 2. The design (why Path B is honest)

The corpus is produced by **the engine's own ports on the engine's own feed**:
`crt.setup.detect_setups` (detection), `crt.atr` (MT4-seeded Wilder ATR),
`crt.sessions` (ET windows), `crt.candles` (closed bars, M30 = 2x15m) — on
the same TDVA history `crt-engine-tick` trades. The evidence basis EQUALS the
trading basis; the MT4-vs-TV data-basis mismatch (the original Path A risk)
disappears by construction.

**Port discipline**: `variant_tail` is the EA `CRT_EV_Research_Export.mq4`
`VariantRow` port for CONT stop models — geometry (SL = C2 opposite extreme +
sb*ATR; entry stop = C2 sweep extreme +/- eb*ATR in the sweep direction),
fill asymmetry (buy `h3+spread >= entry` / sell `l3 <= entry`), cost encoding
(`entry_cost_r`/`exit_slip_r` in R, the optimizer applies them per barrier),
C3-bar-first SL-before-TP (same-bar tie -> SL), the held=1..max_hold walk
(R targets of the SL bar still recorded — the EA loop does; the optimizer
resolves ties SL-first), `bars_avail` right-edge truth, `c_<H>` close-R
(sell exits on ask).

**Engine-parity divergences from the MT4 export** (each is the engine's own
rule — the corpus describes what the engine trades, not what MT4 would have
scanned):

- C3 matched by EXACT open time (`c2_open + tf`); a gap skips the setup
  (counted `gap_skipped`). The MT4 export was positional;
- session frozen at C2 close from the C3 boundary time (clock arithmetic,
  `crt.sessions`);
- M30 = the engine's 2x15m aggregation (both halves required);
- `swept_pd` from the prior TRADING day's H/L on the TF's own bars (the EA
  used broker D1 bars; UTC trading-day stand-in — the column is inert for
  the optimizer);
- times written UTC (`clock=utc` in `#meta`).

**Basis stamping** (audit trail): `#meta` carries `basis=tdva_tv,feed=<TV
symbol>`; `report.build_artifact(metas=...)` (additive optional param) lifts
any `#meta` basis keys into the artifact's `meta.basis` block and the run
record's. MT4 corpora carry no `basis` key -> no block -> byte-identical
artifacts (proven: selftest artifact byte-compat vs the pre-change run).

**Fetch** (script side): one POST `/api/price/batch` per symbol with both TF
requests (`15` at `m15_range` 60000, `60` at `h1_range` 25000 — request more
than TDVA's depth, the server returns its full depth; the
`reconstruct_signal_history.py` range=20000 precedent). Platform conventions:
epoch ms heuristic, DESCENDING->ASCENDING, dedupe, malformed bars dropped,
one bounded 429 retry. Raw bars cached to `data/crt/tv/cache/<SYM>_<TF>.json`
(offline re-runs via `--offline`). Bars -> `closed_bars` -> `aggregate_m30`.
**Depth honesty**: the export REPORTS the span it got per TF and flags
`THIN<30m` (the default 24m/6m walk-forward needs ~30 months); thin spans are
visible, never padded.

## 3. Config (self-contained, `config/crt/crt_tv_export.yaml`)

Grids = EA `EntryBufGrid/SLBufGrid/RGridResearch/HoldGrid` defaults verbatim
(eb 0/0.05/0.10, sb 0/0.10/0.20, R 1.0/1.5/2.0, hold 12/24/48). Core = the
EA numbers (ATR 14, chop 6/0.70/0.75/4, vol 500, liquidity 10). Sessions =
EA defaults. Symbols/costs = BUILD-TIME COPY of `crt_engine.yaml` (2026-11-15
values; the engine yaml is the live authority, NOT read here — keep them in
step by hand). TF switches = EA ExportH1/M30/M15.

## 4. Verification (/home/z/verify/crt_tv_verify.py — 51/51)

- **A (6)** `variant_tail` hand-cases: fill/nofill/risk<=0 inert tails,
  C3-bar SL=0, TP-in-C3, ambiguous-C3 tie (bars_sl=0 AND b_R=0 — the EA loop
  records R after SL at held=0; optimizer resolves SL-first), ask-side BUY
  fill, right-edge empty `c_` grid.
- **B (5)** `optimizer.outcomes()` round-trip on generated rows: SL
  `-1-ecost-slip`, TP `R-ecost`, TIME `c_H-ecost`, unfilled NaN, tie -> SL.
- **C (2) ENGINE PARITY (headline)**: seeded 6,000-bar M15 stream -> corpus
  rows -> for EVERY (eb, sb, R, hold) of 60 setups (4,860 comparisons):
  `crt.labeler.label_setup` (the engine's own labeler) agrees with the
  corpus reconstruction on outcome class 100% and on R within the compact-path
  format's own 4-decimal cost quantization (measured max deviation 9.95e-5 R;
  identities vs the CSV cost fields hold at 1e-5). The corpus describes
  exactly what the engine trades.
- **D (10)** schema/IO: CRLF, ASCII, `#meta` basis/feed/point/spread stamps,
  `data.load` + `validate_schema`, header order == EA schema byte-for-byte,
  loader time format, CONT-only, sessions {0,1,2}, M30 rows, determinism
  (byte-identical re-export).
- **E (4)** sessions (NY setups land 14:30-17:00 UTC in Jan; ASIA
  01:00-03:00 UTC), gap-skipped counted under the exact-time C3 rule,
  right-edge `bars_avail >= 0`.
- **F (3)** M30 aggregation parity (:00/:30 only, tf=M30 rows, half-bar gap
  -> M30 bar dropped).
- **H (9)** CLI end-to-end OFFLINE: pre-seeded cache -> `run_crt_tv_export.py
  --offline --no-discord` rc=0 -> two CSVs + report; `run_crt_lab.py
  --data-dir <tmp> --artifact <tmp>` rc=0 on the TV corpus (baseline +
  walk-forward + mint), artifact `meta.basis = {XAUUSD: tdva_tv, EURUSD:
  tdva_tv}`, run record folds >= 1; **the repo `config/crt_cont.json` is
  never touched by verification** (tmp artifact path only, asserted).
- **I (7)** config defaults (EA grids, 5 symbols, cost mirror, sessions),
  `_basis_block` additive semantics (None/no-key -> no block).
- 2 real bugs found and fixed by the suite: the basis block double-wrap in
  `report.py` (meta.basis nested twice); everything else was test arithmetic
  (wrong numpy axes, wrong column index, tolerance below the format's own
  4-decimal quantization).

Regressions: CRT-0 selftest end-to-end RC=0 with the artifact byte-identical
to the pre-change run (modulo runid/generated stamps); router_edge +
strategy_registry 46/46; claims-adjacent 9 suites 155/155 (eod_resolver,
eod_resolver_basis, paper_book, monitor_paper_book, settlement_contract,
settlement_expiry, resolver_head_of_line, variable_kelly, drift_monitor);
ruff clean; py_compile clean; yamls parse.

## 5. Operator steps (the Path B runbook)

On the deployment box (where `TRADINGVIEW_API_KEY` lives):

```
python scripts/run_crt_tv_export.py            # fetch + export + #crt signal
python scripts/run_crt_lab.py --data-dir data/crt/tv   # A0 + WF + MINT
```

The second command mints `config/crt_cont.json` (the repo default path) with
the `meta.basis` block stamped `tdva_tv`. Then the CRT-1 hot-reload picks it
up on artifact mtime (no restart), and the ladder proceeds: `crt_mode ->
shadow` (claims + monitors accumulate; migration 221 already applied) ->
`live` when the CRT-3 bars pass (`/crt/promotion`).

- `--offline` re-runs the export from `data/crt/tv/cache/` without TDVA.
- `--symbols XAUUSD,EURUSD` trims; `timeframes:` in the yaml disables TFs.
- Read `tv_export_report.json`: if a TF is flagged `THIN<30m`, its walk-forward
  folds will be thin/absent at the default 24m/6m — disable that TF or lower
  `train_months` in `crt_lab.yaml` (an operator decision, documented).
- Review the cost values in `crt_tv_export.yaml` if the operator edits
  `crt_engine.yaml` costs later (build-time copy, keep in step by hand).

## 6. Honest limits

- **Basis, not benchmark**: a TDVA-basis corpus removes the MT4-vs-TV
  mismatch but is NOT the toolkit-validated MT4 evidence; the sha256 input
  manifest + `meta.basis` make the basis auditable, and CRT-3's live-paper
  claims remain the deciding evidence either way.
- **TDVA depth is whatever the API returns**: M15 depth may be shorter than
  the 24-month train window (flagged `THIN`, never padded). The 60k/25k
  range requests follow the reconstruct-script precedent; actual depth is
  operator-observable on the first run.
- **Cost model**: the yaml's spread/slip stand-ins (mirroring
  `crt_engine.yaml`), not a broker's #meta line. Same basis the live engine
  will use — consistent, but a modeling choice.
- **Synthetic-data discipline unchanged**: `--selftest` still never mints
  the repo artifact; verification mints only to tmp paths; the first REAL
  TV corpus is the operator's fetch.
- **The export writes no claims, no DB, no live surface** — it is CRT-0
  research tooling, flag-free by design.
