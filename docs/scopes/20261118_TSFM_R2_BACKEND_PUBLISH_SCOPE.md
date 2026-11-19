# Scope: TSFM lab price refresh — backend-owned TDVA→R2 publish (retires the tsfm-refresh cron Worker)

> **Date:** 2026-11-18
> **Ask:** the `tsfm-refresh` cron Worker in `nobletradingapp/docs/scope-tsfm-lab-cloudflare.md`
> duplicates the backend's price-fetching workload. Implement the fetch directly in
> `noble-trader-fastapi-backend` and update R2 from there.
> **Decision:** retire the cron Worker (never built). One new backend module reuses
> `fetch_tradingview_batch` — the sweep's own TDVA client — and publishes the lab's
> 5 CSVs + manifest to R2 through a secret-gated PUT on the existing `nt-tsfm-data`
> bridge Worker. One fetch implementation, one credential store, 1 batch HTTP call
> per cadence.
> **Amends:** `nobletradingapp/docs/scope-tsfm-lab-cloudflare.md` §4–§6 (that doc is
> revised in lockstep). Roadmap: `nt_roadmap` id 130 `tsfm_lab_hosting`.

---

## 0. Plain language

The lab needs five hourly price CSVs in R2, kept current. The previous scope gave
that job to a new Cloudflare cron Worker that would call TDVA itself — a second
price-fetching implementation next to the one the sweep orchestrator already runs
every 15 minutes. That duplicates code, credentials, and API quota, and the two
fetchers can drift (ordering, staleness, error handling). Instead, the backend
that already owns TDVA fetching publishes the lab's files as a small hourly side
job. The lab Worker on Cloudflare keeps reading R2 exactly as designed; only the
writer changes.

The honest trade: lab data freshness now depends on the Studio being up. The user
chose deduplication over independence; the lab degrades gracefully (it serves the
last published vintage with a visible age stamp, never errors), and a local script
running the SAME module bridges outages manually.

## 1. What already exists (nothing new to invent)

| Piece | Fact (verified in code 2026-11-18) |
|---|---|
| `price_fetcher.fetch_tradingview_batch` | POST `/api/price/batch`, `BATCH_CHUNK_SIZE=10` → the 5 lab symbols fit **one HTTP call**; creds via `settings.TRADINGVIEW_API_KEY/HOST/BASE_URL` (Studio Secrets; also in local `.env.local`) |
| `ohlcv_out` out-param | fills `{symbol: [{"t","o","h","l","c","v"}, ...]}` **ascending**, index-aligned (added 2026-09-24 for brick capture) — exactly the lab CSV's fields; `t` = normalized **epoch seconds**, `None` when the raw bar had no parseable time |
| `include_timestamps=True` | real per-bar timestamps (the 2026-09-09 anti-fabrication convention) |
| `range_bars = min(hours, 5000)` | the TDVA batch cap is 5000 — the lab's ~5000-bar window (208 days) is **exactly one request's worth** |
| Descending-order fix | already inside the fetcher (the 2026-08-11 TDVA pitfall) — a second client would have had to re-learn it |
| `supabase_client.fetch_active_symbols()` | `nt_symbol` active rows incl. `tradingview_symbol` — the mapping single source (verified: COINBASE:BTCUSD, OANDA:USDJPY/USDCHF/XAUUSD/XAGUSD, all active) |
| APScheduler job pattern | `geometry-incubation-reconcile` (orchestrator.py ~1860): `CronTrigger.from_crontab("48 * * * *")`, `misfire_grace_time=1800`, `coalesce=True`, **mode flag read per run** (flip without restart) — the template |
| `nt-tsfm-data` bridge Worker | deployed, holds the `BUCKET` binding, public GET + `/health`, traversal guard; ~25 lines from a secret-gated PUT |

**Why not piggyback the light sweep's own fetch:** the sweep pulls
`LIGHT_CANDLE_BARS=200` (heavy: 2160, daily) — an order of magnitude short of the
lab's 5000-bar window. Widening every 15-min sweep to 5000 bars × 14 symbols
bloats the hot path for a research side-product. A dedicated hourly 5-symbol
batch is the right size: **24 HTTP calls/day** vs the cron Worker's 120, against
the sweep's own ~96×⌈14/10⌉ calls.

## 2. Design

### 2.1 The module — `regime_platform/sweep_orchestrator/tsfm_r2_publish.py`

`async def publish_tsfm_prices(mode: str) -> dict` (returns a run summary; never
raises into the scheduler):

1. **Roster:** `fetch_active_symbols()` filtered to the lab set from
   `config/feature_flags.yaml` (`tsfm_r2_publish.symbols`, default the 5). The
   TV mapping comes from `nt_symbol` — never hardcoded twice.
2. **Fetch:** `fetch_tradingview_batch(symbols, hours=5000, interval="1h",
   include_timestamps=True, ohlcv_out=ohlcv)` — one batch call.
3. **Validate per symbol (fail-loud, skip the put, keep the previous CSV):**
   - rows ≥ 1000 (a truncated response is not a lab window);
   - every `t` non-None and strictly ascending;
   - last bar age ≤ 2 h at publish time (the sweep's staleness convention,
     relaxed one notch for the hourly cadence);
   - `c` finite and > 0; `o/h/l/v` finite (None → reject, never invent).
4. **Serialize:** exact existing schema `timestamp,open,high,low,close,volume`;
   `t` epoch seconds → `datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")`;
   full float precision (byte-comparable with the current files' convention).
5. **Upload:** `PUT {bridge}/tsfm/{S}.csv` with `X-Write-Secret` (httpx, already a
   dependency). Then `PUT tsfm/manifest.json`:
   `{updated_at, publisher:"backend", source:"tdva", symbols:{S:{rows,first,last}}, errors:{S:reason}}`.
   Per-symbol errors never block the healthy symbols' puts; the manifest always
   reflects the run.
6. **Shadow mode:** steps 1–4 + log the would-be writes (sizes, last bars,
   validation verdicts), zero uploads — the first-run proof on the live feed.

### 2.2 Upload path — secret-gated PUT on the existing bridge (decision)

The backend has **no R2 client today** (`pulse_check_r2.py` is Supabase-based;
"kairos_r2" is Router-R2, not Cloudflare) and no CF/R2 creds in its env. Options:

| Path | Verdict |
|---|---|
| **PUT route on `nt-tsfm-data`** (holds the binding) | **CHOSEN** — zero new Python deps, no S3 keys, reuses the deployed surface; the bridge stays free-tier-compatible (binding puts are I/O, not CPU). One new shared secret. |
| boto3/botocore SigV4 → `<acct>.r2.cloudflarestorage.com` | Direct, but adds a dependency + dashboard-created R2 S3 keys (no API to create them) + a second credential class to rotate. Not worth it for 6 objects/hour. |
| CF API token | Cannot authenticate S3 requests (the r2-datastore skill's token-kind pitfall); wrangler-subprocess from Python is a node dependency on Studio. Rejected. |

Bridge amendment (`mini-services/tsfm-r2-worker/src/index.js`, ~25 lines):
- `PUT` (and only PUT) on `tsfm/*.csv` + `tsfm/manifest.json` — prefix allowlist,
  existing `..` traversal guard, body cap 1 MB.
- `X-Write-Secret` header must equal `env.WRITE_SECRET` (wrangler secret);
  mismatch → 401, no binding touch. GET path unchanged (public reads).
- Secret provisioning: `npx wrangler secret put WRITE_SECRET` (from
  `mini-services/tsfm-r2-worker/`) + the same value as a Studio Secret
  (`TSFM_R2_WRITE_SECRET`) + local `.env.local` for the script runner.
  `settings.py` gains `TSFM_R2_WRITE_SECRET` and `TSFM_R2_BRIDGE_URL`
  (default `https://nt-tsfm-data.lexington.workers.dev`).

### 2.3 Scheduling + flag (the house triple)

- `config/feature_flags.yaml` new block `tsfm_r2_publish:` with
  `mode: "off"` (off|shadow|live) + `symbols: [BTCUSD, USDJPY, USDCHF, XAUUSD, XAGUSD]`;
  same keys in `_DEFAULTS`; `get_tsfm_r2_publish_mode()` / `..._symbols()` getters
  (the `pbm_mode` registration triple — a YAML key missing from `_DEFAULTS` is
  silently swallowed).
- APScheduler job in orchestrator.py, the geometry-incubation template:
  `CronTrigger.from_crontab("52 * * * *")` (clear of :38 calibration-resolve,
  :48 incubation, :05/:20/:35/:50 dynamic-tier), `misfire_grace_time=1800`,
  `coalesce=True`, id `tsfm-r2-publish`, mode read per run. Registered
  unconditionally; `off` = no-op (the flag flip needs no restart).
- Best-effort: failures logged, never raised into the sweep tick (the
  dynamic-tier job's convention).

### 2.4 Outage bridge / bootstrap — one implementation, two runners

`scripts/publish_tsfm_r2.py` — thin wrapper (the `.env.local`-loading script
convention) calling the SAME `publish_tsfm_prices()`, `--mode shadow|live`.
Runs from the laptop with local TDVA creds + the write secret:
- **Bootstrap:** one `--mode live` run brings R2 to the current vintage before
  Studio returns (11-23) — the lab's Phase-2 freshness gate can pass without Studio.
- **Outage bridge:** manual refresh whenever Studio is down; not a second
  implementation, no drift surface.

## 3. The trade-off, stated honestly

- **Freshness couples to Studio uptime.** The 33-day outage (2026-10-16 →
  2026-11-18) is the precedent: while Studio is down, R2 ages. The lab never
  breaks — it serves the last vintage, and the `dataVintage.age_hours` stamp +
  >48 h UI badge (cloudflare scope §4.3, unchanged) make the age visible. The
  badge doubles as the outage signal.
- **What dedup buys:** one TDVA client (ordering/staleness/error conventions
  already proven), creds stay in Studio Secrets (no TDVA secret copy on
  Cloudflare), 24 batch calls/day instead of 120, and no second fetch
  implementation to drift — the MIN_RR "silent second source" lesson applied
  to data fetching.
- **What stays independent:** the lab COMPUTE (Cloudflare Worker) and READS
  (R2 binding) never touch Studio. Only the writer moved.

## 4. Build inventory

| Repo | File | Change |
|---|---|---|
| backend | `regime_platform/sweep_orchestrator/tsfm_r2_publish.py` | NEW — fetch→validate→serialize→PUT→manifest (~180 lines) |
| backend | `regime_platform/sweep_orchestrator/orchestrator.py` | +1 job registration (~15 lines, the template) |
| backend | `regime_platform/core/feature_flags.py` + `config/feature_flags.yaml` | the `tsfm_r2_publish` triple |
| backend | `regime_platform/core/settings.py` | +`TSFM_R2_WRITE_SECRET`, +`TSFM_R2_BRIDGE_URL` |
| backend | `scripts/publish_tsfm_r2.py` | NEW — local runner/bootstrap wrapper |
| backend | `tests/test_tsfm_r2_publish.py` | NEW — offline: stubbed fetch + stubbed PUT (validation rejects stale/short/None-t/non-finite; CSV schema byte-match vs a `data/tsfm` sample; manifest shape; shadow does zero uploads; secret header sent) |
| nobletradingapp | `mini-services/tsfm-r2-worker/src/index.js` | +PUT route (secret, allowlist, cap) |
| nobletradingapp | `docs/scope-tsfm-lab-cloudflare.md` | §4–§6 amended (lockstep) |

No migrations. No Supabase writes (`nt_symbol` read-only). No sweep hot-path
changes. R2 writes only, through the one bridge.

## 5. Verification gates

| Gate | Check |
|---|---|
| G1 offline | the unit suite above green; full pytest no new failures |
| G2 shadow (local or Studio) | `--mode shadow`: 5×~5000 bars fetched, validation verdicts logged, zero uploads — proves the live TDVA window + ohlcv shape |
| G3 bootstrap live (local) | `--mode live` PUTs current CSVs; **verify by round-trip through the serving path** (bridge GET: last bar ≈ now, md5 vs source) — never trust the put's success line (the wrangler `--local` lesson generalizes) |
| G4 Studio live | flag `live`; job fires :52; manifest `updated_at` advances hourly; R2 last-bar age < 2 h; lab `dataVintage.age_hours < 2`; an eval recomputes after a refresh (vintage-keyed cache) |
| G5 outage behavior | Studio/job down → lab serves last vintage with the age badge, no 500s; manual script run restores freshness |

**Build status 2026-11-18:** module + flag triple + settings fields + orchestrator
job (`52 * * * *`) + local runner + tests BUILT; **G1 DONE — 16/16 offline tests
pass** (incl. the roster-fallback pair); bridge PUT route DEPLOYED (version
3a7c3b38) and auth-verified live (PUT without secret → 401; GET/health unchanged).
Fail-soft proven against two REAL failure modes during the first shadow attempt:
nt_symbol 401 (stale local Supabase key) → loud bootstrap-roster fallback;
TDVA HTTP 403 (lapsed subscription) → per-symbol errors, zero partial writes.

**G2 + G3 DONE 2026-11-18 (operator updated the TDVA key, sha-prefix
9a332c32e678):** shadow 5/5 × 5000 rows, zero errors, last bar current; live
bootstrap published **5/5 CSVs + manifest, zero errors, exit 0**; serving-path
round-trip verified programmatically — md5 + byte-count + last-bar match on all
5 (BTCUSD/USDJPY/USDCHF last 2026-11-18T22:00Z, XAUUSD/XAGUSD 21:00Z), manifest
`updated_at 2026-11-18T22:27:37Z`. **R2 is now the current vintage** (was
2026-10-26). Two provisioning notes: (a) the worker's `WRITE_SECRET` was absent
(the wrangler step of the secret provisioning had not landed — secrets list was
empty); synced worker ← local `.env.local` value via the CF API
(`PUT /workers/scripts/nt-tsfm-data/secrets`, 201, confirmed in the list) —
Studio Secret + local values unchanged. (b) workers.dev returned **error 1010**
(bot-signature ban) to python-urllib verification probes after the request
burst; production paths are unaffected (the httpx publisher reached the worker
throughout, and the lab reads via R2 binding, never workers.dev). Use
curl/httpx for manual verification, not urllib. G4 waits for the Studio deploy
(11-23); until then the local runner is the refresher (re-run anytime).

## 6. Open items

1. ~~**Secret provisioning (user):** generate `TSFM_R2_WRITE_SECRET`...~~
   **DONE 2026-11-18 (user):** secret set on the Worker + Studio + local
   `.env.local` (present, verified). Bridge PUT without secret → 401 confirmed;
   the worker-side secret value is proven by the first successful live PUT.
2. **Cadence:** hourly default (`52 * * * *`); 4 h is a one-line crontab change
   if TDVA quota is tight — quota math: +24 calls/day, marginal.
3. **Studio return (11-23):** until then G3's local bootstrap keeps R2 current;
   G4 waits for the deploy. The deploy itself follows the studio-deploy runbook
   (code files individually; `config/*` on Studio is live state — the new YAML
   block is merged, never overwritten wholesale).
4. **Bridge PUT deploy:** `wrangler deploy` from the worker dir (CF_API_TOKEN +
   CF_ACCOUNTID, both in nobletradingapp `.env.local`); verify GET unchanged +
   PUT 401 without secret before the first live run.

## 7. Not in scope

- No second TDVA client anywhere (the cron Worker is retired, not built).
- No change to the sweep's own fetch window, cadence, or hot path.
- No lab math/protocol changes; the lab Worker's read side + vintage design stand.
- No Supabase writes, no migrations, no Studio-independence guarantee for
  freshness (explicitly traded for dedup — §3).
- No conflation with the claims-layer `tsfm` vol-fan arm (TimesFM 2.5, different
  thread) or with Router-R2 (`kairos_r2_*`, strategy router — unrelated to R2
  storage despite the name).

## 8. Extension scope — all `nt_symbol` active symbols (scoped 2026-11-18, not built)

**Ask:** after the 5-symbol initial run, extend the publish to every active
symbol in `nt_symbol` (14 today). The module already filters the roster by the
flag list, so the extension is one knob + one validation refinement:

1. **The knob:** `tsfm_r2_publish_symbols: "active"` (string sentinel) instead of
   a list. `get_tsfm_r2_publish_symbols()` returns `"active"`; the module treats
   a non-list value as "every active roster row with a `tradingview_symbol`".
   `nt_symbol` becomes the dynamic authority — a new symbol is published the
   next hourly run; a deactivation drops it. Rollback = the fixed-5 list, one
   YAML line, no restart (mode/list read per run).
2. **Chunking + quota:** 14 symbols → 2 batch HTTP calls per run
   (`BATCH_CHUNK_SIZE=10`, already internal to `fetch_tradingview_batch`).
   48 calls/day ≈ 1,440/month = **1.4% of the Ultra plan's 100k/month**.
   Payload ~5.6 MB/run; R2 free tier (10 GB, 1M Class A ops/month) untouched by
   15 puts/hour. The bridge allowlist regex (`^tsfm/[A-Za-z0-9]+\.csv$`) already
   covers any roster symbol; each object ~450 KB < the 1 MB cap. **Zero bridge
   changes.**
3. **The one real design change — market-closed staleness.** The last-bar ≤ 2 h
   gate would wrongly skip stocks every night and forex/metals every weekend
   (Fri 22:00 → Sun 22:00 UTC closed). Reuse `market_hours.py` — the sweep's own
   asset-class calendar, never a second calendar: market CLOSED → skip the age
   gate, keep the previous CSV, stamp the manifest entry `status: "market_closed"`
   (a retention, NOT an error); market OPEN → age ≤ 2 h as today. Crypto (24/7)
   is always gated. The ascending-timestamp and finiteness gates are unchanged
   (session gaps are legal — strictly ascending, not contiguous).
4. **Lab impact: none.** The lab reads its fixed 5 (`design.ts SERIES_IDS`); the
   extra 9 CSVs are a price lake for future consumers (lab expansion, research).
   The manifest + `dataVintage` design covers all published symbols unchanged.

**Gates:** E1 shadow with `"active"` — 14/14 fetch OK, per-asset-class validation
verdicts logged. E2 live — 14 published, manifest 14 entries, bridge GET
round-trip (md5 + last bar) on one stock AND one forex. E3 closed-market behavior
— a stock overnight and forex on a weekend are retained as `market_closed`, not
errored; crypto still gated. E4 lab regression — its 5 series byte-parity vs the
5-symbol era (same fetch path, same serializer).

**Precondition:** the TDVA subscription blocker (§5 status) cleared first — the
extension multiplies a dead feed by 14 otherwise. **Not in the extension:** no
lab `SERIES_IDS` change, no new consumers wired, no CRT coupling (the CRT corpus
export fetches TDVA directly on its own schedule).
