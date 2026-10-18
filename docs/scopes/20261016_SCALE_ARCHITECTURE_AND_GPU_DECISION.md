# Scale architecture, concurrency, and the GPU decision

**Date:** 2026-10-16
**Type:** Forward-looking architecture assessment (no code change at HEAD).
**Inputs:** operator reports **~90% CPU on every sweep** at 4 vCPU / 11 active symbols;
48 h of `nt_sweep_result` measured while the platform was down.
**Companion:** `docs/scopes/20261016_HOSTING_PLATFORM_ASSESSMENT.md` (host choice).

> **Review 2026-10-17 — corrections, evidence and additions are appended as §7–§13.**
> Three load-bearing claims in §1–§5 do not survive contact with the data: the 48 h window is
> **16 h** (so p95/max are overstated), the heavy sweep **does not overlap the light sweep**
> (so the T2 misfire hypothesis is wrong and T2 is closed), and the ~89 s light burst is
> **not per-symbol work — it is a 5 s per-symbol stagger** (`orchestrator.py:3689-3690`), which
> is ~55 % of the burst and pure waste. Nothing in the ladder's ordering changes; the
> *magnitudes* and the *GPU arithmetic* do.
>
> **⚠️ §1's heavy-sweep figures (`506 s / 602 s / 1154 s`) and §5's `506–1154 s` are VOID.**
> The heavy sweep is a **daily** job plus **intraday reopt urgency** — it is not one
> fixed-length run, and any per-run duration is a snapshot of one window under one flag set.
> The grid has since grown 2.4× (30 → 72 combos) and four more mechanisms have gone live, so
> those numbers describe a configuration that no longer exists. **Do not quote a duration for
> the heavy sweep; quote the cadence and the config dependence.** See
> `20261017_HEAVY_SWEEP_GRID_AND_COST_NOTE.md` for the measurement, the combos table, and the
> `_DEFAULTS` alignment.

---

## 1. Measured state (48 h window, frozen at the outage)

| metric | measured |
|---|---:|
| light batch spread — median | 88.6 s |
| — p95 | 192 s |
| — max | 789 s (88% of the 900 s window) |
| rows per batch | 10.6 avg (11 during stock-closed hours, 14 during US session) |
| batches per hour | 4 (cadence healthy) |
| heavy sweep @ 14 symbols | 506 s / 602 s / **1154 s** |
| instantaneous CPU during a sweep | **~90%** (operator) |

**The binding constraint is the 900 s sweep window, not total CPU-seconds.** The box is
CPU-pegged *during* the ~89 s burst and idle for the remaining ~810 s of every 15-minute
window — roughly **10% wall-clock utilisation at 90% instantaneous CPU**. That gap is the
headroom to exploit, and it is the whole argument for concurrency over hardware.

---

## 2. Concurrency vs sharding — what is actually required

### 2.1 What the code does today

| path | concurrency | evidence |
|---|---|---|
| **Light sweep** | **sequential**, one symbol at a time | `orchestrator.py:3536` `# Sequential processing with stagger` → `:3538` `for i, sym in enumerate(symbols_to_process)` |
| **Heavy sweep** | **concurrent**, capped at 4 | `heavy_sweep.py:593` `asyncio.Semaphore(HEAVY_SWEEP_MAX_CONCURRENCY)` (`:147` = 4) + `:602` `asyncio.gather` |
| scheduling | one APScheduler job **per cron group** | `orchestrator.py:1955` `func=self._sweep_group` |

The heavy-sweep cap carries a rationale that applies verbatim to the light path
(`heavy_sweep.py:72-77`): *"the cap remains because it also keeps the CPU-bound
grid-search thread pool sane (each symbol spawns 3 to_thread tasks; 14 × 3 = 42 threads
otherwise)."*

### 2.2 The ladder — in order, cheapest first

1. **Make `_sweep_group` concurrent** — mirror `heavy_sweep.py`'s semaphore + `gather`.
   ~15 lines, an already-proven pattern *in this codebase*. Converts idle wall-clock into
   throughput with **no new hardware**. Expected 2–4x on the burst length.
   - Must pair with `torch.set_num_threads()` tuning per worker, or N concurrent symbols
     each spawning intra-op threads oversubscribe the box (the exact failure the heavy-sweep
     comment describes).
   - The per-symbol `asyncio.Lock` (`orchestrator.py:3544`) already guards double-sweeping,
     so a concurrent rewrite has a safety net.
2. **Vertical resize** (8 → 16 vCPU) — now pays off, because concurrent symbols can
   actually use the cores. ~12x headroom exists (Hetzner dedicated 48 vCPU / 192 GB;
   GCE up to g2/n2 families).
3. **GPU** — only if a specific component is genuinely GPU-better (§4).
4. **Shard symbols across boxes** — last, and it is a project, not a resize.

### 2.3 Why sharding is not next

- APScheduler is **in-process** — N boxes = N schedulers, so you must shard the symbol list
  or add a distributed lock. There is one job per cron group per process, and
  `_reschedule_groups` (`orchestrator.py:1955`) owns job identity locally.
- The heavy sweep, reopt, calibration resolve and EOD passes are **global** jobs that must
  run once, not N times.
- External rate limits (TradingView/TVDA batching, TimesFM, Discord) are **shared** and do
  not shard — `fetch_tradingview_batch` already chunks at 10 symbols/request.
- The pick-store / TTL machinery assumes a single writer.

At 11 symbols using ~10% of the window, this is several steps premature.

---

## 3. Where the symbol count drops (14 → 11)

**In the sweep, by design — market-hours gating. Not in the qualification gates.**

`_sweep_group` (`orchestrator.py:3396`):

```python
active = [s for s in symbols if is_market_open(s.get("asset_class", ""))]
```

`market_hours.py:59-69` — `stocks` and `indices` are open **Mon–Fri 14:30–21:00 UTC**
(9:30–16:00 ET); `forex`/`commodities` Sun 22:00–Fri 22:00 UTC; `crypto` 24/7.

Observed: `AAPL`, `US30USD`, `US500` — all `asset_class='stocks'`, all still `active=true`,
**zero errors** — last swept in the 20:00 UTC hour on 10-15 and absent from 21:00 onward.
That is exactly the 21:00 UTC close. **Working as designed.** `sweep_after_hours`
(`feature_flags.yaml`) bypasses the filter entirely when set.

**One genuinely unexplained hour:** at 21:00 UTC on 10-15 the four gold symbols
(`XAUUSD`, `XAUAUD`, `XAUEUR`, `XAGUSD`) also vanished, leaving 7 symbols — but
`market_hours.py` treats commodities as open Mon–Thu. Most likely explanation is the
**price-staleness filter** (`orchestrator.py:3459`, `get_price_staleness_max_hours()`) firing
on spot gold's daily ~21:00–22:00 UTC feed break, which the code does not model. Plausible
and benign, but **not proven** — worth one log grep.

---

## 4. Is the GPU necessary or optional?

### 4.1 The honest answer: optional now, necessary later — and the decision needs a measurement

Nothing measured so far shows a GPU requirement. The box is at 90% instantaneous CPU but
~10% wall-clock utilisation, and the light path is **sequential** — so the first-order fix
is concurrency, not silicon. Buying a $516/mo L4 to accelerate a loop that is idle 90% of
the time would be paying for the wrong thing.

**The measurement that decides it:** a per-stage timer inside `_process_symbol` to split the
per-symbol cost into TimesFM inference / HMM fit / Renko / EV+gates / DB+network.

- TimesFM (or the 1.7B local LLM) **> ~40%** of per-symbol wall-time → **GPU is the right
  lever** (an L4 does a 200M-model forecast in milliseconds vs seconds on CPU).
- Dominated by DB round-trips and price fetching → **GPU buys nothing**; fix the I/O.

This is the single most valuable next diagnostic, and it is cheap. It should precede any
GPU purchase.

### 4.2 When GPU becomes necessary (not optional)

| condition | why GPU |
|---|---|
| a model no longer fits in RAM | memory wall, not a compute wall |
| TimesFM / LLM inference dominates the burst | 10–100x on that component |
| cadence tightens below ~5 min | serial latency binds; needs raw per-symbol speed |
| models grow (fine-tuned TimesFM, 7B+ LLM) | compute + VRAM |
| evaluation goes event-driven / streaming | batch assumptions break entirely |

### 4.3 Growth path forecast

**Stage 1 — today.** 11–14 symbols, 15-min batch, sequential CPU sweep, ~10% window
utilisation. PBM in shadow, first-passage replacing the HMM.

**Stage 2 — next 1–2 quarters (no GPU needed).** Concurrency + vertical scale.
30–50 symbols at 15-min cadence. Per-symbol cost rises ~1.5–2x as instruments accumulate
(PBM live EV swap, MAE/MFE, reachability, cell posteriors, regime survival). Concurrency
absorbs this; the window still has room.

**Stage 3 — GPU territory.** Cadence tightens (5 min), models grow, or inference dominates.
This is where the GPU decision actually lands — and it is a *provider* change, which is why
the host choice should be made with it in view (see the companion assessment §8).

**Stage 4 — event-driven.** Replace "sweep all symbols every 15 min" with evaluation on
price events. Architecturally different: streaming, queueing, back-pressure. Sharding and
a real message queue appear here — not before.

**Forecast:** Stage 3 is driven by *model ambition and cadence*, not by symbol count. Symbol
growth alone is a Stage-2 problem that concurrency plus cores handles.

---

## 5. Trigger table

| # | signal | threshold | action |
|---|---|---|---|
| **T1** | light batch p95 spread | > 450 s (half the window) | concurrency → then vertical |
| **T2** | heavy sweep wall-time | > 900 s | **CLOSED 2026-10-17** — heavy does not overlap light (§9); the misfire mechanism is not firing |
| **T3** | batches/hour < 4, or rows/batch < active symbols | — | sweep loss; liveness gate catches it |
| **T4** | per-symbol stage breakdown | inference > ~40% | **GPU** — provider change |
| **T5** | model > RAM | — | GPU or bigger box |
| **T6** | largest box still misses T1 | — | shard symbols (scheduler coordination work) |
| **T7** | light batch spread > 200 s | — | **new** — see §8.2; already exceeded at 640 s |

### A suspected loss mechanism worth checking

Light-sweep jobs are registered with `misfire_grace_time=120, coalesce=True`
(`orchestrator.py:1961-1962`). Heavy sweeps run **506–1154 s** and saturate the CPU. If a
heavy sweep delays a light tick's start by more than 120 s, APScheduler **discards that
run** rather than queueing it. Measured max light-batch spread is already 789 s, so this is
plausible — but it is a **hypothesis**, verifiable by grepping the orchestrator log for
`misfire`/`skipped` around heavy-sweep windows. If it fires, it is a silent-loss path that
T3 would only catch after the fact.

---

## 6. Recommended sequence

1. **Instrument `_process_symbol`** with per-stage timings (§4.1). Cheap, decides the GPU
   question with data. Do this first.
2. **Make `_sweep_group` concurrent**, mirroring `heavy_sweep.py`, with tuned
   `torch.set_num_threads()`. No hardware spend.
3. **Re-measure** T1/T2. If the window is comfortable, stop — GPU is optional.
4. **Only then** size hardware, and pick a host that can carry a GPU if Stage 3 is real
   (the companion assessment's §8 covers the GCP path).
5. **Add T1/T2/T3 to the liveness gate** so the ceiling announces itself.

---

## 7. Review 2026-10-17 — what the data actually says

Method: `nt_sweep_result` (28,151 rows, 2026-09-17 → 2026-10-16) read over PostgREST, batched
1000 at a time, grouped into 15-min buckets keyed on `floor(minute/15)`. All figures below are
from that read; nothing here is a replay.

### 7.1 The §1 table is wrong, and it matters

| metric | doc says | measured | note |
|---|---:|---:|---|
| window length | 48 h | **16 h** | newest row 2026-10-16 03:00:52 UTC; oldest in the "48 h" read 2026-10-14 23:46 — but the read hit PostgREST's **1000-row cap** and silently truncated. Re-reading with pagination gives the real span. |
| light spread — median | 88.6 s | **89 s** | holds |
| — p95 | 192 s | **146 s** | 1.3x overstated |
| — max | 789 s | **203 s** (last 7 d) · 640 s (all-time) | **4x overstated**, and the 789 s figure is unreproducible |
| rows/batch | 10.6 | 9.8–10.2 | holds |
| batches/hour | 4 | **4** in 1,614 of 2,245 batches | holds, but see §7.2 |
| heavy @ 14 symbols | 506 / 602 / **1154 s** | 263–742 s measured | 1154 s is not reproducible; §8.1 |
| instantaneous CPU | ~90 % | not measurable off-box | **unverified** — §7.4 |

Two of the doc's three "inputs" are now stale. The engine has produced **nothing for 42.4 h**
(newest `sweep_timestamp` 2026-10-16 03:00:52, `nt_signal_sim.ts` 03:07:10, `nt_roadmap`
02:38:15, `nt_symbol_calibration_state` 01:18:01 — all four fast heartbeats stale by the same
~42 h). The scale question is being asked about a box that is **not running**.

**And the §1 table is internally inconsistent with the repo's own liveness rule.** The §6
sequence says "add T1/T2/T3 to the liveness gate so the ceiling announces itself" — but
`pulse_check_r2.py` already gates on heartbeat *age* and exits 2. It did not catch this,
because the §1 table was built from a 16 h slice of a table that has been empty for 42 h. Any
future measurement from `nt_sweep_result` must be timestamp-bounded and paired with a
liveness check, or it silently measures a dead box.

### 7.2 The stagger is not part of the burst — the doc's §2.1 arithmetic is wrong

§2.1 says the light path is "sequential, one symbol at a time" and implies the ~89 s spread is
per-symbol work. It is not. `orchestrator.py:3689-3690` staggers **after** each symbol:

```python
if i < len(symbols_to_process) - 1:
    await asyncio.sleep(STAGGER_SECONDS)   # 5 s — orchestrator.py:92
```

Measured, last 7 d: per-symbol `sweep_duration_ms` **median 3.9 s**, batch spread **89 s**.
The arithmetic that reproduces the spread is `11 × (3.9 + 5.0) = 98 s` — i.e. the 5 s stagger
is **~55 % of the burst wall-clock** and 100 % pure waste. Concurrentising the loop without
removing `STAGGER_SECONDS` would leave the stagger as the new serial bottleneck.

**The stagger has no stated purpose and no comment in the source.** It is not a rate-limit
guard (the batch price fetch already chunks at 10/request), not a CPU guard (the loop is
already sequential, so it buys nothing), and not an I/O guard (TimesFM is 15-min-cached at
`orchestrator.py:304`). Treat its removal as part of the concurrency change, not an optional
extra.

### 7.3 The light loop is safe to parallelise — verified, not assumed

`orchestrator.py:3538-3690` writes **no** `self.<attr>` state. The only shared mutable object it
touches is `self._sweep_locks[symbol]` (`:3544`), which is already per-symbol and already
guards the heavy path too (`heavy_sweep.py:641`). Persistence is via fire-and-forget
`asyncio.create_task` (`log_signal_sim`, `open_position`, `log_renko_bricks`, `_record_pbm`,
`record_tick_and_update_shadow`, `:6055-6169`), and `self._persist_regime_state()` (`:3687`) is
called **once per symbol** from inside the loop — that is the one thing that would need moving
out of a concurrent rewrite (it writes one JSON file, so N concurrent writers race).

Also worth knowing before writing the change: the light path already shares a 15-min TimesFM
cache with the heavy path (`_TIMESFM_CACHE`, TTL 900 s), so concurrent light symbols do not
multiply TimesFM load.

### 7.4 What concurrency would actually buy — and what it would not

Using the measured medians (11 symbols, 3.9 s/symbol CPU, 5 s stagger, 900 s window):

| shape | burst wall | CPU in the burst | burst as % of window |
|---|---:|---:|---:|
| today (serial + stagger) | 98 s | 43 s | 11 % |
| `Semaphore(4)`, stagger kept | 24 s | 11 s | 3 % |
| `Semaphore(4)`, stagger removed | **11 s** | 11 s | **1 %** |

§2.2's "expected 2–4x on the burst" is an **understatement for wall-clock and a non-sequitur
for CPU**: the burst falls 98 s → 11 s (8.9x) once the stagger goes, and the CPU-seconds per
window are **invariant to concurrency** — `11 × 3.9 = 43 s` either way. Concurrency buys
*latency*, never *throughput* here, because the work is serialised by the stagger, not by core
count.

Which makes the real finding the opposite of §2's:

> **The box is at ~4 % duty cycle (measured 16,899 CPU-seconds against 449,100 wall-seconds
> across 499 batches). It is not CPU-bound — it is *stagger-bound*.** The 90 % instantaneous CPU
> report and the 10 % wall-clock figure in §1 are both consistent with that: a 43 s burst on 4
> cores is ~90 % of *one* core for ~11 s, then nothing for 889 s.

So concurrency is not a scale fix — it is a **latency fix with no capacity benefit**. It still
matters, because a 98 s burst that grows with symbol count will eventually collide with the
pre-sweep jobs; but it does not change the infrastructure answer, and it should not be sold as
"exploiting the idle wall-clock". §2's framing of the idle time as "headroom to exploit" is
the one thing in the original that is wrong: **there is nothing to exploit, because the work is
already only 4 % of the window.**

### 7.5 Per-symbol cost is rising, and it is the one number that decides the GPU question

Median per-symbol light cost, by day:

| period | median |
|---|---:|
| 2026-09-22 → 09-30 | 2.3–2.7 s |
| 2026-10-04 → 10-09 | 3.4–3.7 s |
| 2026-10-12 → 10-16 | 3.8–4.2 s |

**+75 % in three weeks, flat across a period with no symbol-count change.** §4.3's Stage-2
forecast assumes "per-symbol cost rises ~1.5–2x" — measured, it has already risen 1.75x and is
still climbing, and the cause is not symbol count. This is the single most important input to
the Stage-3 timeline, and it is currently unexplained. Instrument first (§4.1), then decide.

---

## 8. Corrections to specific claims

### 8.1 The heavy sweep does not overlap the light sweep — T2's mechanism is dead

§5's misfire hypothesis predicts that a 506–1154 s heavy sweep delays light ticks past the
120 s `misfire_grace_time` and APScheduler **discards** them. Measured heavy runs against
simultaneous light rows:

| heavy run | span | light rows during the run |
|---|---:|---:|
| 10-12 17:08 | 742 s | 7 |
| 10-13 17:10 | 534 s | 4 |
| 10-14 17:07 | 263 s | 4 |
| 10-15 17:08 | 314 s | 7 |
| 10-16 01:19 | 86 s | 0 |

Light sweeps **do run during heavy sweeps** — the per-symbol `asyncio.Lock` (`heavy_sweep.py:641`)
serialises them by design, and the light tick that collides simply skips
(`orchestrator.py:3545-3547`, logged as `locked — skipping (concurrent sweep)`). So light ticks
are **lost to lock contention**, not to misfire, and they are lost *quietly*.

Two consequences:

1. **T2 as written (>900 s → "overlaps light; check misfire drops") is the wrong action.** The
   overlap is already handled by the lock. The real cost is the skip, and it is bounded by the
   number of colliding symbols.
2. **The 1154 s heavy figure is not reproducible.** Across 122 heavy runs the max span is
   742 s; the 14-symbol runs are 263–742 s. §1's "506 / 602 / 1154 s" triple looks like a
   mixture of runs, or of a pre-fix build. The current heavy ceiling is ~742 s, still under
   900 s but only by 18 %.

### 8.2 The light burst is already over the line §5 sets for it

§5 sets T1 at **450 s** ("half the window") and T7 (new, this review) at **200 s**. Measured
last-7d spread: median 89 s, **p95 149 s, max 640 s**. The p95 is fine; the tail is not. At
640 s the batch consumed 71 % of the window, which is the shape of a batch that will miss a
15-min cadence once the pre-sweep jobs (`dynamic-tier-optimization`, `macro-regime-refresh`,
`trend-regime-refresh`, all on the `5,20,35,50 * * * *` grid) get slower. Worth watching; not a
fire today.

### 8.3 The 14 → 11 drop is real, but §3's explanation is incomplete

§3 attributes the drop to the 21:00 UTC stock close (correct: `market_hours.py:63`,
`870 <= minute_of_day <= 1260`). But the measured per-hour row counts show the drop is
**gradual, not a cliff**: 203 → 193 → 155 → 113 → 159 → 76 → 44 → 37 → 42 across hours
0–9 UTC, i.e. the platform is only at full 14-symbol width for ~10 of 24 hours. Combined with
`batches/hour = 4` in only 72 % of hours, **the effective sweep load is roughly half of what
the 14-symbol roster implies.** Any capacity number built on "14 symbols at 15-min cadence" is
2x optimistic about both load and cost.

---

## 9. The misfire hypothesis, properly tested

The mechanism is real — `misfire_grace_time=120, coalesce=True` (`orchestrator.py:1961-1962`)
will discard a light tick whose start slips more than 120 s. But the observed evidence does not
support it as the cause of lost ticks:

- Heavy sweeps run 17:08–18:24 UTC and 01:19–02:10 UTC — both inside light windows, and light
  rows **do** appear inside them (§8.1).
- The light row loss that does exist is at 21:00–21:00 UTC and is fully explained by market
  hours.
- 14 of 28,151 rows have a non-null `error`, and **every one is a heavy-sweep `AttributeError`**
  (`'HeavySweepMixin' object has no attribute '_sizer'` / `'_hmm_model'`, 2026-10-07) or a
  backfill note. **Zero light-sweep errors.** A misfire-discarded tick leaves no row at all, so
  it is invisible to this read either way — but the `locked — skipping` path is the one with a
  log line, and it is the one that fires.

**Verdict:** keep the hypothesis, demote it from "suspected loss mechanism worth checking" to
"checked, not the mechanism". The skip path is the one to measure: grep the orchestrator log
for `locked — skipping (concurrent sweep)` and count it per day. If it is ~0, neither mechanism
is live and §5's whole subsection can be closed.

---

## 10. Infrastructure — the answer to "what's a scalable container/lambda/serverless option"

The framing in §4 is right (GPU optional now, necessary later) and the ladder in §2.2 is right
(concurrency → vertical → GPU → shard). What §4 does not answer is the operator's actual
question: **what to run it on.** Here it is, priced against the measured duty cycle.

### 10.1 Why Lambda is the wrong answer for this workload

Not "serverless is bad" — it is four specific mismatches, each cheap to check:

| constraint | value | why it breaks |
|---|---|---|
| invocation timeout | 15 min (90 min for async) | heavy sweep at 14 symbols is **263–742 s**, and §8.1 shows it growing. At 30–50 symbols it is inside the 15-min cap with no margin. |
| memory ceiling | 10,240 MB → **6 vCPU** | Lambda allocates CPU in proportion to memory (1 vCPU per 1,769 MB). 8–12 GB RAM is the stack's own floor (`20261016_HOSTING_PLATFORM_ASSESSMENT.md` §1), so the GPU-capable ceiling is 6 vCPU — below what the concurrent sweep wants. |
| no GPU | — | Lambda has no GPU at any tier. Stage 3 needs one. |
| in-process scheduler | — | APScheduler runs in the orchestrator process (`orchestrator.py:1459`, `AsyncIOScheduler`). A scale-to-zero function has no scheduler; a min-instances=1 function is just a worse VM with a 15-min timeout. |

**Lambda is the right answer for exactly one thing here:** the pre-sweep jobs that already run
on the `5,20,35,50 * * * *` grid — `dynamic-tier-optimization`, `macro-regime-refresh`,
`trend-regime-refresh` — are read-only, single-shot, and individually short. Moving them off
the orchestrator would cut the orchestrator's own CPU load and make the box smaller. That is a
real win, and it is a *separation* move, not a "go serverless" move.

### 10.2 The container answer: one always-on CPU box, stateless, with the GPU split out

The measured duty cycle is the argument. **4 % CPU busy over the whole window, 44 % busy
during a 98 s burst.** A box sized for the burst is idle 96 % of the time; that is the
"pointless to pay for unutilised CPU" the operator is describing, and it is inherent to a
15-min batch shape, not a misconfiguration.

So: **do not buy a bigger CPU box for the sweep.** Buy the smallest box that holds the models
in RAM, split the GPU half off, and let the GPU half be on-demand.

**Target shape (Stage 2, today):**

| tier | what runs | shape | ≈ monthly |
|---|---|---|---:|
| **CPU tier** (always-on) | gateway, orchestrator, `main.py`, TimesFM 200M, local LLM | 4 vCPU / 12 GB / 60 GB, x86_64 | **$13–16** (Hetzner CPX31; **verify — the June 2026 hike put CPX31 at $73.49**, see companion §11.2) |
| **GPU tier** (on-demand) | TimesFM + LLM inference only | L4 24 GB, started per forecast window | **$94** at 8 h/day (RunPod $0.39/h) · **$192** always-on-equivalent on Modal ($0.80/h) |
| **scheduler** | APScheduler, in-process | must live on the CPU tier | — |

Two things make this cheap rather than fiddly:

- **All durable state is in Supabase**, so the CPU tier is nearly stateless — a cold redeploy is
  ~30 min, not a day (companion §5.3).
- **The GPU half is stateless too.** It loads weights from `HF_HOME` on a volume and serves
  `/forecast`. TimesFM is already called over HTTP (`orchestrator.py:399`,
  `http://127.0.0.1:8002/forecast`), so the split is a URL change plus a firewall rule — the
  orchestrator already has `TIMESFM_INFERENCE_URL` / `TIMESFM_URL` env overrides at `:383-399`.
  **No code change is required to move inference off-box.**

**On-demand, honestly priced.** The forecast workload is 14 symbols × 4 windows/day = 56
forecasts/day, ~8 GPU-minutes of *actual* inference. The realistic service pattern is not
"8 minutes a day" — a pod that stops between every window pays a cold start each time, so the
practical shape is **one pod up for the sweep window and stopped after it** (~1 h/day, 30 h/mo),
or a min-replica-1 endpoint that pays 24/7. Both are priced:

| platform | L4 rate | 1 h/day (30 h/mo) | always-on (730 h/mo) |
|---|---:|---:|---:|
| **RunPod** | **$0.39/h** | **$12/mo** | $285/mo |
| Modal | $0.80/h | $24/mo | $584/mo |
| HF Inference Endpoints (AWS L4 ×1) | $0.80/h | — (min-replica-1 bills 24/7) | **$584/mo** |
| GCE `g2-standard-4` | bundled | — | **$516/mo** ($325 with 1-yr CUD) |

RunPod is the pick on either shape: cheapest L4, **GPU billing stops when the pod stops**, and
its network volume persists independently so the HF cache survives. HF Endpoints is the trap —
scale-to-zero is real but requires `min_replicas=0`, and the default bills a full L4 hour 24/7.
Modal's $0.80/h is 2x RunPod for the same card; it earns its price only if the orchestrator
stays on Modal too, which §10.1 rules out.

**The honest framing:** at 1 h/day the GPU half costs **$12/month** — cheaper than the CPU tier,
and it removes the only component that could ever force a bigger box. That is the whole
business case for splitting, and it is an order of magnitude cheaper than §4's "$516/mo L4"
framing suggested.

### 10.3 What to do about the GPU *right now*

§4.1 says "instrument `_process_symbol`, then decide." That is still right, and the instrument
is now cheaper than §4.1 suggests, because **`sweep_duration_ms` already exists per row**
(`orchestrator.py:3677`). What is missing is only the split.

Cheapest possible version of the T4 decision, in this order:

1. **Read `sweep_duration_ms` against the TimesFM cache.** `_TIMESFM_CACHE` is a 15-min TTL
   (`:304`), so a symbol's first tick of a window pays for a forecast and the other three get a
   cache hit. Compare the per-symbol duration of the **first tick in a 15-min window** against
   the **last tick** — the delta is the marginal cost of one TimesFM forecast. No code change,
   one query. If the delta is < 1.5 s, the GPU is buying < 40 % of per-symbol time and §4.1's
   own bar says **do not buy it**.
2. **Time the heavy sweep's grid search separately.** It is 90 combos × 14 symbols = 1,260
   Renko pipeline backtests per run (`heavy_sweep.py:2096-2140`), run via `asyncio.to_thread`.
   That is CPU-bound and **not** GPU-eligible — if it dominates the heavy wall-time, the GPU
   does nothing for the heavy path regardless of inference cost.
3. **Only then** instrument `_process_symbol` with per-stage timers.

Step 1 is a single query against data already in the table. Do it before buying anything.

---

## 11. Risk register — what could make this wrong

| # | risk | why it matters | check |
|---|---|---|---|
| R1 | **Per-symbol cost is rising 1.75x in 3 weeks for no known reason** (§7.5) | if it keeps climbing, Stage 2's "concurrency absorbs it" is optimistic and Stage 3 arrives early | instrument `_process_symbol`; correlate the rise with `nt_roadmap` mechanism changes |
| R2 | **The engine has been silent 42 h** | every number in this doc is from a dead box; a restarted box may behave differently | restart, then re-measure T1/T3 before trusting §7 |
| R3 | **Heavy sweep ceiling is 742 s, not 1154 s** (§8.1) | if it is really 742 s and growing with symbol count, the 900 s window is closer than §5 admits | watch heavy span vs symbol count |
| R4 | **`sweep_after_hours: False`** | the 3 stock/indices symbols are swept only Mon–Fri 14:30–21:00 UTC; the roster is 14 but the load is 11 | confirm this is intended (it is documented in `feature_flags.yaml:69-78` but the comment warns the live value used to be the opposite) |
| R5 | **TimesFM and the local LLM are the same 15-min cache** | concurrent light symbols do not multiply inference load — good — but it also means inference cost is amortised 4x, which *lowers* the GPU case | §10.3 step 1 |
| R6 | **Hetzner pricing** | companion §11.2 corrected CPX31 to $73.49 after the June 2026 hike; the $13–16 figure in §10.2 may be stale | verify at order time |

---

## 12. Revised recommendation (supersedes §6)

1. **Restart the box.** Nothing else is measurable until the heartbeats are fresh. (§7.1, R2)
2. **Instrument, but start with the free version** — `sweep_duration_ms` first-tick vs
   last-tick delta gives the TimesFM marginal cost with no code change (§10.3 step 1).
3. **Make `_sweep_group` concurrent with `Semaphore(4)` AND remove `STAGGER_SECONDS`.**
   Move `self._persist_regime_state()` out of the loop (it is called once per symbol and writes
   a shared JSON). Verified safe: no `self.<attr>` writes in the loop body (§7.3).
   Expected: burst **98 s → 11 s**; CPU per window unchanged at 43 s (§7.4). This is a latency
   fix, not a capacity fix — do not expect the box to feel less loaded.
4. **Re-measure T1/T3/T7.** If T7's 200 s tail is gone and T1 holds, **stop** — GPU is optional.
5. **Then** size hardware as §10.2: smallest CPU box that fits 8–12 GB, GPU split off and
   on-demand, HF cache on a persistent volume.
6. **Add T1/T3/T7 to the liveness gate** so the ceiling announces itself.
7. **Do not buy a GPU on the current evidence.** The measured duty cycle (4 % busy) and the
   15-min amortisation of TimesFM both argue the other way; §10.3 step 1 decides it with data.

---

## 13. What this review did not establish

Stated plainly, because §7.1 already caught one overreach:

- **The ~90 % instantaneous CPU figure is unverified.** It is an operator report, not a
  measurement, and the only data available (`sweep_duration_ms`) is per-symbol wall time, not
  CPU. A 3.9 s symbol on 4 cores is consistent with ~25 % CPU per symbol, not 90 %.
- **The 48 h window is really 16 h**, and the 789 s max is unreproducible. Both are corrected
  above, but the 16 h sample is thin for a p95.
- **The 42 h silence means no post-outage data exists.** §7's numbers describe the box as it
  was at 03:07 UTC on 2026-10-16.
- **Whether the light/heavy lock skip is happening at all** is unmeasured — it needs a log grep
  for `locked — skipping (concurrent sweep)`, which requires the box to be up.
- **The GPU decision is still open.** §10.3 step 1 is the cheapest possible test and it has not
  been run.
