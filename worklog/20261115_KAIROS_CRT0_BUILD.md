# 2026-11-15 — CRT-0 Build: the research twin lands (regime_edge_crt)

## Context

Operator instruction (after Task 67's assessment, commit 405cd8c): **adopt the
assessment's recommendations** and add two things — **self-contained config
files** and **signal delivery to the Discord `#crt` channel** — then **build
CRT-0**.

Adopted recommendations (Task 67 §4/§6/§9, verbatim decisions):

1. **`regime_edge_crt`** — the research twin, `regime_edge`-style, built NOW.
2. **`crt/`** renko-style engine package + a `CRTOrchestrator` job family —
   **NOT** a top-level `regime_platform_crt` (it would read as a platform
   fork). That is CRT-1; CRT-0 stays research-only.
3. CRT-0 scope (Task 67 §6): ingest the MT4 research CSVs, run the baseline
   check + walk-forward with the toolkit's thresholds verbatim, mint
   `config/crt_cont.json`, report under `worklog/kairos/reports/regime_edge_crt/<runid>/`.
4. Ships dark: zero live surface, zero claims writes, zero migrations, MT5
   untouched, `regime_platform` never imported by the twin.

Additions from the operator (this build):

5. **Self-contained config** — every CRT knob lives in `config/crt/*.yaml`;
   the ONLY platform-file edit is the additive `crt_mode` ladder flag
   (`config/feature_flags.yaml` + `_DEFAULTS`), which CRT-0 does not even read
   at runtime (it documents the CRT-1 ladder and keeps flag drift clean).
6. **Discord `#crt` signals** — `regime_edge_crt/discord_notify.py`, a
   fail-open webhook sender reading `config/crt/crt_discord.yaml`; the lab
   emits run/baseline/walk-forward/mint/error events to `#crt`.

## 1. What was built

```
regime_edge_crt/                 # research twin (importable WITHOUT regime_platform)
  __init__.py                    # ENGINE, SEED=7, SOURCE_ZIP provenance
  config.py                      # self-contained yaml loader (defaults = toolkit verbatim)
  data.py                        # compact-path CSV ingest + #meta + sha256 manifest
  decay.py                       # DecayMonitor CUSUM (EA rules verbatim; CRT-1 imports this)
  optimizer.py                   # crt_ev_optimizer.py port: outcomes/folds/LCB/decay replay/HTML
  baseline.py                    # baseline_check.py port: v3 filters, no fitting, cost burden
  report.py                      # crt_lab.json artifact assembly + summary.md
  discord_notify.py              # #crt webhook sender (fail-open, 5/2s window, masked URL)
  selftest.py                    # deterministic synthetic corpus (seed 7) — smoke without MT4
scripts/run_crt_lab.py           # offline CLI (run_edge_lab.py style, UTC runid dirs)
config/crt/crt_lab.yaml          # SELF-CONTAINED lab knobs (thresholds verbatim)
config/crt/crt_discord.yaml      # SELF-CONTAINED #crt webhook + events + rate limits
config/crt_cont.json             # MINTED artifact (generated; not committed until real data)
config/feature_flags.yaml        # + additive crt_mode: "off" ladder block
regime_platform/core/feature_flags.py  # + "crt_mode" default + get_crt_mode() getter
```

## 2. Port fidelity manifest

Kept VERBATIM from the toolkit (fidelity checklist §7 of the assessment):

- `outcomes()`: triple-barrier rebuild — `sl_r = -1 - entry_cost_r - exit_slip_r`,
  `tp_r = R - entry_cost_r`, SL-wins-ties (`bsl <= br`), time-exit at hold
  (`closeR - entry_cost_r`), not-filled → NaN, `-1` bars → `inf`.
- `vol_bit()` 0.33/0.67 banding, unknown→mid(2), bits 1/2/4.
- `DecayMonitor`: `k = max(kappa_min, kappa_frac·max(mu0,0)/sd0)`,
  `S = max(0, S − z − k)`, OFF at `S ≥ h_off` OR rolling mean ≤ `roll_off_ev`
  (n ≥ roll_n), CAUTION at `S ≥ h_caution`, OFF-reactivation needs
  `react_n` trades with mean > 0 → CAUTION else stay OFF.
- `make_folds()`: month-normalized rolling 24m/6m, stub test window < 28 days
  skipped.
- `filter_space()`: stage 1 = `[(0.0, 0, 7, 0)]`; stage 2 grid verbatim.
- `best_config()`: LCB = EV − z·SE with `n ≥ min_train_n` else −inf; argmax.
- Enable gate: OOS n ≥ 30 ∧ EV > 0.02 ∧ t ≥ 1.0 ∧ final LCB > 0; the better
  model per (symbol, tf, dir, session) picked by (enabled, OOS-LCB score).
- Baseline v3: sweep ≤ 0.75, chop on, sessions {NY, ASIA}, CONT 0/0/1.5R/24.
- Per-symbol EA CSVs keep CRLF line endings (MQL4 `FILE_ANSI` parity).

Sanctioned changes (Task 67 §2: "IO-path/config-artifact changes, provenance
stamps"):

- argparse → self-contained yaml + CLI overrides; `sys.exit` → `CRTLabError`
  (CLI maps to exit 2 with the same message text).
- The CSV is loaded ONCE and shared by baseline + optimizer (IO dedupe).
- `DecayMonitor` moved to `decay.py` (CRT-1's engine imports it — one source
  of truth; the optimizer's rules are untouched).
- Provenance stamps: sha256 input manifest, thresholds echo, runid, engine,
  SOURCE_ZIP in every artifact; `generated`/`train_end` columns preserved as
  the deliberate provenance stamps (the claims `created_at` discipline).
- Report outputs moved under `worklog/kairos/reports/regime_edge_crt/<runid>/`.
- NEW (operator additions): discord_notify + selftest + config/crt yamls.

## 3. Self-contained config contract

`config/crt/crt_lab.yaml` + `config/crt/crt_discord.yaml` hold ALL CRT knobs.
The platform's config files receive exactly one additive key (`crt_mode`,
default `off`) — the CRT-1 ladder, registered now so the flag surface exists
before any engine ships. `regime_edge_crt` never imports `regime_platform`:
the twin runs on a bare pandas/numpy/httpx environment (verified importable
before any platform dependency).

The Discord webhook URL lives in `config/crt/crt_discord.yaml` per the
operator's explicit instruction (self-contained signal config). It is a
secret committed to the repo: if the repo ever widens, rotate the webhook
(Discord: channel → Integrations → Webhooks) — one yaml line, zero code.

## 4. Discord `#crt` signal design

- Events: `handshake` (run start: runid, symbols, span), `baseline` (does CONT
  have an edge after costs at v3 settings? per symbol×TF n/EV/t), `walkforward`
  (deployed/enabled options, OOS EV/t totals, gated ΣR, OFF%), `mint`
  (artifact path, rows, enabled, train_end), `error` (pipeline failure).
- Fail-open: any notifier failure (timeout, 4xx/5xx, 429 exhausted, bad
  config) is logged to stderr and swallowed — the lab never fails because of
  Discord. `--no-discord` disables per run; `enabled: false` disables globally.
- Bounded: 10 s timeout, ≤ 2 attempts, 429 `Retry-After` honored up to 3 s,
  sliding-window 5 msgs / 2 s (Discord's documented per-webhook budget).
- Chunked at ≤ 1900 chars (Discord caps content at 2000).
- Masked: the webhook URL is never logged (last 6 chars only).

## 5. Verification record

- **Deep suite** (throwaway `/home/z/verify/crt0_verify.py`, T-series pattern):
  **42/42 passed** —
  - A(16) port math: outcomes() triple-barrier hand-cases (TP-before-SL, SL-wins-ties,
    time-exit, not-filled NaN, unresolved-at-H NaN), vol_bit bands + unknown→mid,
    make_folds (month-normalized rolling, <28d stub skip), DecayMonitor all five
    transition paths (NORMAL→CAUTION at h_caution, CAUTION→OFF at h_off, rolling-mean
    OFF, OFF-reactivation OFF→CAUTION→NORMAL, sticky-OFF), best_config min_train_n
    gate, series_stats + baseline stats() hand-computed.
  - B(7) config: toolkit defaults verbatim (z 1.28 / n 30 / EV 0.02 / t 1.0 / LCB /
    folds / bands / decay / v3 baseline), fail-open loaders with status stamps.
  - C(8) Discord, fake httpx: success, 429→Retry-After→success, 429-exhausted
    fail-open with URL masking (full URL never in stderr), 5xx retry, transport
    exception never raises, chunking ≤1900 on newlines, event gating, disabled no-op.
  - D(3) CLI: no-data error path rc=2 with the MT4 operator instructions in the
    message, --symbols filter, --baseline-only skips the walk-forward.
  - E(4) flags: yaml `crt_mode: off`, `_DEFAULTS` off, `get_crt_mode()` off,
    garbage value → off fallback.
  - F(1) import hygiene: fresh-process import of regime_edge_crt never loads
    regime_platform.
  - **G(3) PORT PARITY — the strongest evidence**: the ORIGINAL toolkit
    `crt_ev_optimizer.py` and the port run on the SAME corpus with the SAME params:
    `CRT_EVOPT_folds.csv` **byte-equal**, per-symbol deploy CSVs **equal minus the
    `generated` provenance stamp**, and the HTML report **byte-equal**.
- **End-to-end selftest** (`scripts/run_crt_lab.py --selftest`, seed 7): 157,536-row
  synthetic corpus → A0 baseline (buckets/year/cost-burden tables) → stage-1
  walk-forward (3 folds, 36 options deployed, decay replay) → minted
  `crt_cont_SELFTEST.json` (36 rows, thresholds echo, sha256 input manifest,
  honesty notes) + `crt_lab.json` run record + summary.md + EA-parity
  CRLF deploy CSVs. RC=0.
- **Determinism**: two back-to-back runs → baseline CSVs, folds.csv, HTML report,
  deploy CSVs all BYTE-EQUAL; `crt_lab.json` differs by exactly the `runid`
  provenance field; the corpus regenerates md5-stable.
- **Discord #crt, LIVE**: the real webhook delivered (1) a labeled handshake and
  (2) the full 4-event selftest run (handshake, baseline, walkforward, mint) —
  the sender stayed within the 5-msg/2s window.
- **Regressions**: adjacent suites `tests/test_router_edge.py` +
  `tests/test_strategy_registry.py` 46/46 (the yaml append + flag registration
  broke nothing); ruff clean on every new file — `optimizer.py` carries a
  DOCUMENTED file-level E501 exemption (the HTML template is the toolkit's,
  kept byte-verbatim; verification group G3 depends on it); py_compile clean;
  all three yamls parse.
- **Bugs found and fixed during verification**: 1 real robustness bug — the
  notifier's `_post` fail-open try wrapped only the POST call, not the response
  handling (a malformed response could raise through `send()`; now the whole
  attempt body is guarded — the never-raise contract holds). The other 3 initial
  failures were TEST bugs (my fold-count/CAUTION-step arithmetic and the
  reactivation expectation — the port's `[OFF, CAUTION, NORMAL]` sequence matches
  the original state machine exactly, re-derived by hand).

## 6. Operator steps (real data — the MT4 side stays the source)

1. MT4: run `CRT_EV_Research_Export.mq4` per symbol (README §1; structural
   settings from the .set file, SpreadPoints 10 XAUUSD / 2 majors / half
   spread BTC, ExportCONT=true).
2. Copy `CRT_CONT_RESEARCH_<SYMBOL>.csv` from `MQL4\Files` → `data/crt/raw/`.
3. `python scripts/run_crt_lab.py` — baseline + walk-forward + mint + #crt
   signals, reports under `worklog/kairos/reports/regime_edge_crt/<runid>/`.
   `--selftest` smokes the whole pipeline on a deterministic synthetic corpus
   (seed 7) with zero MT4 data.
4. Smoke without data: `python scripts/run_crt_lab.py --selftest`.

## 7. Honest limits (carried from Task 67 §8)

- No real MT4 CSVs exist in this sandbox yet — CRT-0's verification corpus is
  the deterministic synthetic selftest (valid compact-path schema, injected
  small edge); the REAL edge question is answered only by the operator's MT4
  export. Nothing here claims live evidence.
- M30 derivation, TDVA refresh paging, claims/CHECK migration, paper book,
  engine — all CRT-1+ scope, untouched today.
- The rolling-window-overlaps-training honesty note (README §Known limits)
  travels in the artifact meta so CRT-1 cannot forget it.
