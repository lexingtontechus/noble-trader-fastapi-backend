# Hosting platform assessment — where the Kairos stack should run

**Date:** 2026-10-16
**Type:** Forward-looking assessment (no code change at HEAD).
**Trigger:** the entire LightningAI platform went down. Last sweep 2026-10-16 03:00:52 UTC,
last signal 03:07:10 — **~21 hours with zero output**, detected only because a question was
asked, not because anything alerted.
**Status:** recommendation only. Nothing migrated.

---

## 1. What the stack actually needs (measured from the repo, not assumed)

`start.sh` launches **five co-located processes on one box**:

| port | process | what it is |
|---|---|---|
| 8000 | `proxy.py` | gateway — the only public port |
| 8001 | `main.py` | HMM / Renko / auth |
| 8002 | `server.py` | TimesFM 2.5 200M forecast API |
| 8003 | `python -m regime_platform.sweep_orchestrator` | APScheduler sweep (15-min light) |
| 8004 | `llm_server.py` | local instruct LLM (Spark-X2.5-1.7B-INT8) |

**Dependency weight** (`requirements.txt` / `pyproject.toml`): `torch>=2.4`, `timesfm`,
`transformers`, `sentence-transformers`, `hmmlearn` (C extension), `scipy`/`numpy`/`pandas`,
`fastapi`/`uvicorn`, `apscheduler`, `alpaca-py`, `yfinance`.

**Derived requirements:**

| requirement | value | why |
|---|---|---|
| RAM | **8–12 GB** | torch + TimesFM 200M (~1 GB) + 1.7B INT8 LLM (~2 GB) + embeddings (~0.5 GB) + Python overhead |
| vCPU | 2–4 | CPU inference only — **no GPU needed** |
| Persistent disk | 4–6 GB | HF model cache; without it every boot re-downloads (30–60 s, 2–3 min cold) |
| Uptime model | **always-on, never idle-sleep** | APScheduler runs *in-process*; a suspended box silently stops sweeping |
| Python | 3.11 / 3.12, `pip install -r requirements.txt` | Render builds from requirements.txt, not pyproject |
| Network | outbound HTTPS (Supabase, Discord, TradingView, Alpaca, HF); inbound for the gateway | |
| Secrets | env vars | `API_KEY`, Supabase keys, Discord webhooks |
| Arch | **x86_64 preferred** | see §4 |

**No Dockerfile exists** — only `render.yaml` (which deploys just `uvicorn main:app` on a
`starter` plan, i.e. a *subset* of the stack, no TimesFM/orchestrator/LLM). Containerising is
a prerequisite for most managed hosts and good practice for a VPS.

---

## 2. Cost for the same always-on shape (~2–4 vCPU / 8 GB, CPU-only)

| platform | shape | ≈ monthly | model | notes |
|---|---|---:|---|---|
| **Hetzner Cloud** | CPX31 4 vCPU / 8 GB (x86) | **~$13–16** | VPS, hourly, capped | 20 TB traffic EU; repriced twice in 2026 (Apr 1, Jun 15) — verify at order time |
| Hetzner (dedicated) | CCX13 2 vCPU / 8 GB | ~€43 | VPS | dedicated vCPU, no noisy neighbours |
| Hetzner (ARM) | CAX21 4 vCPU / 8 GB | ~$7.49 | VPS | **ARM** — see §4 |
| **DigitalOcean** | 4 vCPU / 8 GB / 160 GB SSD | **$48** | VPS, per-second | 5 TB transfer; best docs/support of the VPS set |
| **Fly.io** | shared-cpu-2x + 6 GB extra | **~$49** | containers + volumes | $13.39 base + $6/GB extra RAM; volume $0.15/GB |
| Oracle Cloud | Ampere A1 2 OCPU / 12 GB | **$0** | always-free | **ARM**; halved from 4/24 in 2026; A1 capacity is flaky |
| Northflank | ~2 vCPU / 8 GB | ~$60 | usage-based | no plan fee; $0.01667/vCPU-h + $0.00833/GB-h; free sandbox far too small |
| **Railway** | 2 vCPU / 8 GB | **~$120** | $20/vCPU + $10/GB | per-second but always-on never stops |
| **Koyeb** | 1 vCPU / 4 GB | **~$119** | Pro $29 + compute | free tier withdrawn Feb 2026 (joining Mistral) |
| **Render** | `2c-8g` | **$135** | per-service | + workspace fee; already used for a subset |

Cross-check from an independent 2026 review: the *same* steady always-on stack is
**≈$79/month on Render vs ≈$10/month on a fixed Hetzner node**.

**The shape of the market:** for a 24/7 CPU-only Python process, managed PaaS costs
**5–10x** a plain VPS. PaaS buys you deploy ergonomics and TLS; it does not buy uptime —
Lightning was a managed platform and it ate 21 hours.

---

## 3. Recommendation

### Primary — Hetzner Cloud, x86, run the stack under Docker Compose + systemd
`CPX31` (4 vCPU / 8 GB, ~$13–16/mo) or `CCX13` if noisy-neighbour latency ever matters.

- Cheapest always-on CPU-only option by a wide margin; hourly billing, monthly cap.
- **x86_64** — zero ARM wheel risk (§4).
- Full control: five compose services on localhost, only the gateway published; HF cache on
  a named volume so restarts don't re-download weights.
- The operator already runs ssh, bash launchers and Studio secrets — the ops surface is
  familiar, not new.

### Alternative — Fly.io if managed ergonomics matter more than ~$35/mo
`shared-cpu-2x` + RAM to 8 GB (~$49/mo) + a volume for the HF cache. Managed TLS, regions,
per-second billing, `fly deploy` from a Dockerfile. Same stack, less sysadmin, 3x the price.

### Second opinion — Oracle Always Free A1 (2 OCPU / 12 GB, $0) as a **standby**, not primary
Attractive on price, but: **ARM** (build risk), A1 capacity requests routinely fail, and the
allocation was halved in 2026 with inconsistent enforcement. Useful as a *warm spare* that
can run a reduced stack (orchestrator + gateway, no local LLM) if the primary dies.

### Not recommended for this workload
Render ($135), Railway (~$120), Koyeb (~$119) — all priced for always-on PaaS and 5–10x a
VPS for the same shape. Render stays fine for the small `main:app` subset it already serves.

---

## 4. ARM caveat (rules out treating Oracle/Hetzner-CAX as drop-in)

PyTorch **does** publish aarch64 Linux wheels (and 2.11+ pulls CUDA-enabled ones), and
`numpy`/`scipy`/`pandas` are fine. The blockers are elsewhere:

- **`triton` has no aarch64 PyPI wheel for Linux** — if TimesFM's tree pulls it, ARM fails.
- **`flash-attn`** historically hardcoded `linux_x86_64` wheel URLs.
- **`hmmlearn`** is a C extension: compiles on aarch64, but needs a build toolchain in the image.

None of these are confirmed blockers for *this* stack — but each is a plausible multi-hour
rabbit hole. Treat ARM as a project, not a checkbox, and only after the x86 path is stable.

---

## 5. The actual failure was reliability, not price — fix that independently

Host choice would not have prevented this. What was missing:

1. **External liveness watchdog.** `pulse_check_r2.py` now gates on heartbeat age and exits
   **2** when the sweep/signal/PBM heartbeats are stale — but it only runs when a human runs
   it. Put it (or a plain HTTP health check on the gateway) on an **external** scheduler —
   GitHub Actions cron, cron-job.org, UptimeRobot — alerting to Discord. A 21-hour outage
   should page within 15 minutes. **This is the single highest-value fix and it is host-independent.**
2. **Reproducible deploy.** A `Dockerfile` + `docker-compose.yml` in the repo, secrets from
   env, HF cache on a volume. Without it, "move to another host" is a day of yak-shaving
   under pressure — which is exactly when you need it to be an hour.
3. **A documented failover path.** Because all durable state lives in Supabase, the app tier
   is nearly stateless — a second host can be brought up and pointed at the same DB. Decide
   now whether that's a warm standby (Oracle free) or a cold redeploy.

---

## 6. Migration order (when green-lit)

1. Write `Dockerfile` + `docker-compose.yml`; verify the five-process stack boots locally.
2. Stand up the chosen host; run the stack with `pbm_*` flags unchanged (shadow-only).
3. Confirm heartbeats fresh in `pulse_check_r2.py` (the new liveness gate is the acceptance test).
4. Add the external watchdog and prove it fires (kill the stack, watch it page).
5. Only then retire Lightning.

---

## 7. Open questions for the operator

- What does Lightning cost today? (If it is free, it is still not viable as the *sole* host
  after a 21-hour silent outage.)
- Is EU (Hetzner Nuremberg/Helsinki) acceptable latency for the TradingView/Supabase calls?
- Warm standby (extra complexity, free) or cold redeploy (simpler, ~30 min RTO)?

---

## 8. Google Cloud — options when a GPU is on the roadmap

Added 2026-10-16 (operator: "4 CPUs now, GPU in the near future"). GCP is the one provider
here with a first-class CPU→GPU upgrade path *and* managed GPU runtimes. It is also 5–10x
the Hetzner CPU price, so it is a deliberate premium, not a cost play.

### 8.1 The constraint that decides everything

**`e2` machine types do not support GPUs.** Only the **accelerator-optimized** families
(A4 / A3 / A2 / G4 / **G2**) and the **N1 general-purpose** family accept one. So "start
cheap on e2, bolt on a GPU later" is not a thing — it is a migration. Pick the family now:

| family | GPU support | GPU models |
|---|---|---|
| `e2` | **none** | — |
| `n1` (general-purpose) | attachable | T4, V100, P100, P4 (**no L4**) |
| `g2` | attached | **L4 24 GB** |
| `g4` | attached | RTX PRO 6000 |
| `a2` | attached | A100 40/80 GB |
| `a3` / `a4` | attached | H100 / B200 |

Other GCE GPU rules that bite:

- Changing GPU count requires **stopping the instance**; **GPUs cannot live-migrate**, so a
  host-maintenance event stops the VM (relevant after a 21-hour outage).
- For G2 / A2-Standard you switch machine type *within* the series; A3 / A2-Ultra cannot
  change machine type at all — new instance.
- **GPU quota must be requested per region** (Cloud Run auto-grants 3).
- Boot disk must be ≥ 40 GiB before a GPU change.
- GPUs receive SUDs and resource-based CUDs (CUDs require an attached reservation).
- Spot GPUs are ~40–60% cheaper but are preempted by default on maintenance, with no
  auto-restart unless the VM is in a managed instance group.

### 8.2 Compute Engine shapes (us-central1, Linux, on-demand, 730 h/mo)

| shape | vCPU / RAM | GPU | ≈ monthly | 1-yr CUD |
|---|---|---|---:|---:|
| `e2-standard-4` | 4 / 16 GB | **none possible** | $97.84 | $61.64 |
| `n1-standard-4` | 4 / 15 GB | attachable (T4/V100) | ~$137 | — |
| `n1-standard-4` + 1× T4 | 4 / 15 GB | T4 16 GB | ~$389 | — |
| **`g2-standard-4`** | **4 / 16 GB** | **1× L4 24 GB** | **$516** | **$325** |
| `g2-standard-8` | 8 / 32 GB | 1× L4 | ~$621 | — |
| `a2-highgpu-1g` | 12 / 85 GB | 1× A100 40 GB | ~$2,140 | — |

GPU add-on rates: **T4 $0.35/h**, **L4 $0.70/h**, A100 40 GB $3.67/h, A100 80 GB $5.03/h,
H100 ~$10.6–11/h, B200 up to $16.11/h. Spot: T4 ~$0.40/h, A100 ~$2.17/h, H100 ~$6.26/h.

**`g2-standard-4` is exactly the current footprint plus an L4** — 4 vCPU / 16 GB is the
shape already in use, and the GPU comes with it. That is the clean target.

### 8.3 Managed GPU runtimes

**Cloud Run (GPU)** — L4 (24 GB) and RTX PRO 6000 (96 GB). Requires **instance-based
billing** (min instances billed at full rate while idle) and L4 needs **≥ 4 CPU / 16 GiB**.
GPU is billed for the **entire instance lifecycle**, and **zonal redundancy is ON by
default** (extra $/GPU-second). Always-on ≈ **$484 L4 + $187 CPU + $83 RAM ≈ $754/mo**.
No quota request; managed drivers; scale-to-zero exists but is useless for an in-process
scheduler. Flexible CUDs do **not** apply to GPUs.

**GKE** — cluster fee **$0.10/h ≈ $73/mo** in both modes. Standard bills nodes at Compute
Engine rates; Autopilot GPU pods use node-based billing = hardware **+ Autopilot GPU
premium** (L4 $0.067/h, A100-40 $0.35/h, H100 $1.17/h). Autopilot supports L4/T4/A100 on
all versions and scales GPU nodes to zero (60–90 s to provision). Most managed GPU
experience, most complexity — overkill for five processes.

**Vertex AI** — managed training/endpoints. A different deployment model (container +
model artifacts); it does not host a 5-process orchestrator with an in-process scheduler.

### 8.4 Recommendation

1. **If the GPU will be L4-class and budget allows:** go straight to **`g2-standard-4`**
   (~$516/mo, $325 with a 1-year CUD). It is the current shape plus the GPU; no migration.
2. **If budget binds now:** start on **`n1-standard-4`** (~$137/mo — the only
   *general-purpose* GPU-capable family) and attach a **T4** later (~+$252/mo), accepting
   a T4 instead of an L4 — or plan one stop-and-swap to G2 when the L4 is wanted.
3. **Do not start on `e2`** if a GPU is genuinely planned.
4. **Cloud Run / GKE** only if the architecture later splits into an always-on CPU
   orchestrator + a bursty GPU inference service. For a single always-on box they are
   1.5x (Cloud Run) to 1.2x (GKE) the cost of the equivalent GCE VM, for orchestration you
   do not currently need.
5. New accounts get **$300 credit / 90 days**; the always-free tier (1× `e2-micro`, 5 GB
   storage) is far too small for this stack.

**Honest framing:** GCP's value here is the upgrade path, region choice, CUDs and quota
certainty — not raw price. An always-on CPU box is ~$98/mo on GCE vs ~$13–16 on Hetzner,
and an L4 is ~$516/mo on GCE vs ~$0.20–0.50/GPU-hr at specialist GPU hosts (RunPod,
Vast.ai, DataCrunch, Lambda). Choose GCP for the ecosystem, not the meter.

**And the reliability point still stands:** a single GCE VM is a single point of failure,
and GPU instances cannot live-migrate. Whatever the host, the external watchdog (§5.1) is
the fix that would actually have caught the 21-hour outage.

---

## 9. GPU-on-demand platforms — Lightning's toggle and its comparables

Added 2026-10-16 (operator: *"lightning is a good option at present as it has the gpu
ondemand switch capability"*). The capability is real and it is the right **shape** for
Kairos' bursty Stage-3 need (see `20261016_SCALE_ARCHITECTURE_AND_GPU_DECISION.md` §4):
pay for a GPU only when it is attached, keep the filesystem when it is not.

### 9.1 Two different things both called "toggle"

| flavour | what persists | built for |
|---|---|---|
| **dev studio** — attach/detach a GPU on a live workspace | the whole dev environment (repo, env, data) | exploration, training runs |
| **prod compute** — stop/start an instance or scale to zero, with a persistent volume | a data volume, not a live process | serving, always-on services |

Lightning is the first. Kairos runs **production** on it — which is the root of the
friction described in §9.3.

### 9.2 Comparables

| platform | toggle mechanism | persistent storage | GPU rates (on-demand) | model |
|---|---|---|---|---|
| **Lightning AI** | attach/detach GPU on a live Studio | bundled; 10 GB free then $0.10/GB/mo | T4 ~$0.29–0.41, A10G ~$0.50, H100 ~$2.99 | dev studio |
| **RunPod** | stop pod (GPU billing stops); network volume moves between pods; savings plans follow you across GPU types | network volume **$0.07/GB/mo**, pod-independent | **L4 $0.39**, A5000 $0.27, A40 $0.44, H100 PCIe $2.89 | prod pods + serverless |
| **Modal** | serverless scale-to-zero, per-second | volumes | **L4 $0.80**, T4 $0.59, A100-80 $2.50, H100 $3.95 | serverless functions |
| **Paperspace (DigitalOcean)** | machine type CPU ↔ GPU | yes | — | dev/prod machines |
| **Colab Enterprise / Workbench** | runtime shape CPU ↔ GPU | limited | — | notebook, session-capped |
| **Hugging Face Spaces** | per-Space hardware selector | yes | — | per-Space |
| **GCE** | stop instance, change machine type (GPU cannot live-migrate) | persistent disk | L4 $0.70, T4 $0.35 | IaaS |
| **Spheron / Vast / Lambda / Hyperstack / Massed** | hourly rental, no toggle | varies | H100 ~$2.64, A100-80 $1.43 | GPU rental |
| ~~**Fly.io**~~ | **removed — GPUs deprecated, unavailable after 2026-08-01** | — | — | — |

**Correction to §3:** Fly.io no longer has a GPU product at all. It was deprecated in
February 2026 and switched off 1 August 2026 (A10, L40S, A100 — gone). It remains a fine
CPU container host but is off the table for the GPU path.

### 9.3 The catch with Lightning specifically

Lightning's own pricing page: *"Free Studios run 24/7 but require restart every 4 hours.
No restrictions in Pro or higher."* Plus auto-sleep after ~10 minutes idle.

Two consequences for this repo:

1. **The codebase already pays for it.** `orchestrator.py:1484-1494` hardcodes Lightning's
   restart schedule — *"Lightning AI free-plan Studio sleeps after ~4h and is restarted on
   a fixed schedule (PDT): 06:00, 10:04, 14:08, 18:12, 22:16, 02:24"* — and was root-caused
   as the real reason XAUAUD's heavy sweep went 4+ days stale (heavy-sweep firing 20:00 UTC,
   only 68 minutes before the 21:08 restart). That is **host-specific technical debt**.
2. **The 4-hour restart is a single point of failure.** A free Studio depends on an external
   restart cycle to stay alive; if that cycle does not fire, the Studio simply stays asleep.
   Observed 2026-10-16: activity ran from the 01:12 UTC restart to **03:07 UTC** and then
   stopped entirely, with the 05:16 / 09:24 / 13:00 restarts never restoring service. The
   operator reports a platform-wide outage, so this is Lightning's failure, not the app's —
   but it illustrates that a restart-dependent host has no self-recovery path.

Pro and above remove the restart restriction. The open question is the price and the
reliability posture, given that the same platform just went dark for 21 hours.

### 9.4 Recommendation

1. **Split the architecture rather than toggling one box.** An always-on **CPU** host for
   the gateway + orchestrator + scheduler (which must never sleep — APScheduler is
   in-process), and **GPU on demand** for TimesFM / LLM inference. This matches the measured
   duty cycle: ~10% wall-clock utilisation, bursty by construction.
2. **RunPod is the closest production analogue to Lightning's toggle** — L4 at **$0.39/hr**
   (cheaper than GCE $0.70 and Modal $0.80), GPU billing stops when the pod stops, and a
   **network volume** persists independently of the pod and can be reattached to a different
   GPU type. Savings plans follow you to the next deployment of the same GPU.
3. **Modal if you want zero ops** — per-second, scale-to-zero, no instance to size. Costs
   ~2x RunPod's L4 and is a functions-not-a-box model, so the orchestrator would stay on CPU
   elsewhere.
4. **Lightning Pro** keeps the toggle without the 4-hour restart, if the reliability question
   is answered. Note the 10-minute auto-sleep is still a hazard for an always-on scheduler.
5. **Do not build the GPU path on Fly.io** — no GPU product remains.

---

## 10. Chinese platforms (Alibaba, Tencent, AutoDL, …)

Added 2026-10-16. **Lead finding: a mainland-China region cannot host this stack's
delivery path.**

### 10.1 The decisive constraint — Discord is blocked in mainland China

Kairos delivers qualified signals via `discord_delivery.py`. Discord has been blocked by
the Great Firewall since 2018; GreatFire's tests **rechecked 2026-10-04** still report
`discord.com` blocked through DNS poisoning, outright connection blocking and dropped
packets. **Hong Kong is not affected.**

Consequences:

- A **mainland region** host cannot deliver signals. Non-starter for production unless the
  delivery channel itself changes (a product decision, not a hosting one).
- A **Chinese provider's international regions** (Alibaba Virginia/Singapore/Tokyo/HK;
  Tencent's international site) are unaffected and work normally.
- Outbound to US-hosted Supabase, TradingView and Alpaca from a mainland region also adds
  150–250 ms RTT per round-trip, which this sweep makes many of.

### 10.2 What is actually on offer — three flavours of "toggle"

| flavour | platform | mechanism |
|---|---|---|
| **managed dev studio** | **Alibaba PAI-DSW** | persistent GPU-backed notebook; stopping releases compute and pauses compute charges |
| | **Tencent HAI** | *"按需启停，关机不计费"* — start/stop on demand, no charge when shut down; explicit `STOPPED_NO_CHARGE` state; minute-level startup |
| **GPU rental w/ stop** | **AutoDL** | per-second, no charge when stopped, data survives 15 days of shutdown |
| **IaaS + economical mode** | **Alibaba ECS** | pay-as-you-go GPU instances have **economical mode** — stop being charged for GPU resources when the instance is stopped |

Caveats worth knowing before committing:

- **PAI-DSW's system disk is temporary.** Public resource group: data is cleared after the
  instance has been stopped 15+ days. Dedicated resource group: cleared on stop or delete.
  Billing starts the moment it enters `Running`, whether or not you are using it.
- **Tencent HAI is China-region** (`cloud.tencent.com.cn`), so §10.1 applies.
- **AutoDL is mainland-only**, requires Chinese real-name verification and Chinese payment
  (Alipay/WeChat), and is a *GPU rental*, not an always-on host. Shutdown preserves data for
  15 days free, then auto-releases; data is destroyed on **release** (not on shutdown).

### 10.3 Pricing — Chinese on-demand GPU is not uniformly cheaper

| GPU | AutoDL (CN) | Alibaba CN | Alibaba Virginia | Tencent | RunPod | Modal | GCE |
|---|---:|---:|---:|---:|---:|---:|---:|
| T4 16 GB | — | — | — | **$0.204** | — | $0.59 | $0.35 |
| A10 24 GB | — | $1.41 (spot $0.44) | $2.34 (spot $0.70) | — | — | $1.10 | — |
| **L4 24 GB** | — | — | — | — | **$0.39** | $0.80 | $0.70 |
| RTX 3090 24 GB | **~$0.17** | — | — | — | $0.46 | — | — |
| RTX 4090 24 GB | **~$0.37** | — | — | — | — | — | — |
| A100 80 GB | **~$0.83** | — | — | — | — | $2.50 | $5.03 |
| H100 80 GB | ~$1.80 | — | — | — | $2.89 | $3.95 | ~$10.6 |

The picture is mixed, not "China is cheap":

- **AutoDL is genuinely the cheapest** — A100 80 GB at ~$0.83/hr against Modal's $2.50 and
  GCE's $5.03. Off-peak spot runs 30–50% lower still (midnight–06:00 Beijing).
- **Alibaba's branded GPU instances are expensive** — an A10 at $1.41/hr in CN and
  **$2.34/hr in Virginia**, i.e. ~6x RunPod's L4 for a comparable inference card.
- **Tencent's T4 at $0.204/hr is the standout** among hyperscaler-branded rates.

### 10.4 Practical barriers

Real-name verification (实名认证) for mainland accounts · Alipay/WeChat Pay for the cheap
tiers · Chinese-language docs and support on mainland products · mainland data-residency
rules if any user data ever flows through · the firewall cutting *outbound* calls, not just
inbound.

### 10.5 Recommendation

1. **Mainland regions: no** — Discord is blocked, and the delivery path is the product.
2. **Chinese providers' international regions: technically fine, commercially pointless.**
   Alibaba Virginia's A10 at $2.34/hr is 6x RunPod's L4, so there is no cost case, only added
   complexity.
3. **AutoDL is the one genuinely compelling option** — but only if the GPU half is split off
   and hosted in a mainland region, and only with Chinese payment/ID. See below.
4. **The interesting architecture: split across the firewall.** The always-on CPU
   orchestrator + Discord delivery stays in a US/EU region (Discord works); the **GPU
   inference half** runs on AutoDL in a mainland region at ~$0.83/hr for an A100 80 GB. The
   orchestrator calls it over HTTPS — the payload is 168 bars, so cross-border latency is
   irrelevant. Only *delivery* needs Discord, not inference. This is the cheapest GPU path
   identified anywhere in this assessment, at the cost of a second provider and a
   cross-border hop.
5. **Also worth a look:** ModelScope (Alibaba's model hub) offers free GPU notebooks — useful
   for evaluation, not for production.

---

## 11. Hostinger KVM 4 — spec looks better, but a specific flaw disqualifies it

Added 2026-10-16 (operator proposed Hostinger KVM 4 against Hetzner, citing Hostinger's own
comparison page). **That page is vendor marketing, not a benchmark** — it is published by one
of the two vendors and lists shared-hosting features (AI assistant, malware scanner, 1000+
templates, cPanel) that are irrelevant to a headless Python backend. Two independent facts
below matter more than anything on it.

### 11.1 Price reality — promo vs renewal

| | KVM 4 | Hetzner CPX31 | Hetzner CCX23 |
|---|---|---|---|
| vCPU / RAM | 4 / 16 GB | 4 / 8 GB | 4 dedicated / 16 GB |
| Disk | 200 GB NVMe | 160 GB | 160 GB |
| **Promo** | **$12.99/mo** (24-mo prepay) | — | — |
| **Renewal** | **$28.99–42.99/mo** | ~$73.49/mo (see §11.2) | ~$101.49/mo |
| Billing | upfront, long term | hourly, no commitment | hourly, no commitment |

Hostinger's headline rate requires a **24-month upfront prepay** and renews at 2.2–3.3x. The
effective 4-year average is ~$21–28/mo, not $12.99.

### 11.2 Correction — my earlier Hetzner figures were stale

Hetzner raised prices **twice in 2026**: ~30–37% on 1 April, then on **15 June** a further
**+144–175% on the shared-vCPU CPX family** and **+113–173% on the dedicated-vCPU CCX
family**. Hetzner's own price-adjustment doc lists CPX31 at €20.99 → **€62.49**
($24.99 → **$73.49**) and CCX23 at €31.49 → **€85.99** ($36.99 → **$101.49**). The CAX (ARM)
family rose only ~31% (CAX31 8 vCPU / 16 GB at €20.99 / $24.99).

The "~$13–16/mo Hetzner" figure used in §3 of this assessment predates those hikes and
**should not be relied on.** Sources conflict on the current number (some aggregators still
list CPX31 at ~$13, and the June table may be US-region specific), so **verify against the
live Hetzner console for the target region** before quoting it.

Net effect: Hostinger KVM 4 is genuinely cheaper than Hetzner's current 16 GB tier on
headline price. That part of the operator's read is correct.

### 11.3 The disqualifier — sustained-load CPU throttling

Reported behaviour: **sustained load past ~180 minutes triggers a 25%/hour CPU reduction**,
and reviewers report **up to 90% performance degradation on KVM 4 / KVM 8** while throttled —
a ceiling the advertised vCPU count does not reflect.

That is precisely the wrong failure mode for Kairos:

- the workload is **sustained 24/7** by construction (a sweep every 15 minutes, forever);
- the operator reports the box already sits at **~90% CPU on every sweep**;
- so the host would be throttling the exact workload the platform is built on.

**Independent corroboration** — VPSBenchmarks' measured grades for KVM 4 (trial 2026-08-26):

| Web Perf | Raw CPU | Perf Stability | Disk IO | Network | Consistency |
|---|---|---|---|---|---|
| D | C | **D** | D | **E** | 66 |

A **D on performance stability** is the same signal from a different direction. Together these
are enough to treat throttling as real until disproved.

**Cheap falsification test:** run a sustained CPU load on a trial KVM 4 for 4–6 hours and log
throughput per 15-minute bucket. If throughput decays after ~3 hours, the platform is
disqualified for this workload — no interpretation required.

### 11.4 The genuinely interesting find — Hostinger has a GPU line

Not on the KVM plans (those list no GPU), but as a **separate product**:

| GPU | VRAM | Hostinger | RunPod | Modal | GCE |
|---|---:|---:|---:|---:|---:|
| RTX 4090 | 24 GB | **$0.38–0.45** | — | — | — |
| RTX PRO 6000 | 96 GB | **$0.60–0.70** | — | — | — |
| L40S | 48 GB | **$0.92–1.09** | — | $1.95 | — |
| A100 80 GB | 80 GB | **$1.43–1.67** | — | $2.50 | $5.03 |
| B200 | 192 GB | $4.50 | — | $6.25 | — |

GPU instances ship with **8 vCPU / 16 GB RAM / 200 GB** — more than KVM 4. Billing is
per-minute on an hourly credit model with no commitment, and **availability varies with demand
and region**.

**The catch:** *"billing continues for as long as the instance exists, including while the
workload is idle, and destroying the instance is currently the only way to stop compute
charges."* There is **no stop/pause**. That makes it poor for bursty on/off use — worse than
RunPod (stop halts GPU billing, volume persists) and worse than Lightning (stop pauses
billing, filesystem persists).

### 11.5 Verdict

| question | answer |
|---|---|
| Better *shape* than Hetzner CPX31? | **Yes** — 16 GB RAM and 200 GB disk at a lower headline price |
| Better *price* than Hetzner? | **Yes on promo**, and Hetzner's 2026 hikes make it clearly cheaper than CCX23 |
| Suitable for an always-on 90%-CPU sweep? | **No, not without falsifying §11.3** |
| Useful for the GPU half? | **Possibly** — competitive rates, but no stop/pause and variable availability |

**Recommendation:** do not move the orchestrator to Hostinger KVM 4 without running the §11.3
sustained-load test first. If it throttles, the platform's core workload is the thing being
throttled. Separately, **Hostinger's GPU line is worth pricing** against RunPod/Modal — the
L40S and A100 rates are competitive — but only if a workflow can tolerate destroy-to-stop.

---

## 12. instabox.sh — CPU is fine, the maturity is not

Added 2026-10-16 (operator asked for an evaluation of instabox for the orchestrator's CPU
workload; LLM deferred). instabox is YC-backed "cloud computers for AI agents", announced
**2026-10-14** — **two days old** at time of writing, invite-only beta.

### 12.1 Measured machine (VPSBenchmarks YABS, run 2026-10-14)

| | |
|---|---|
| Underlying infra | **Amazon AWS**, Boardman, Oregon |
| OS / kernel | Debian 13 (trixie), `6.18.54-instabox` (custom) |
| CPU | **AMD EPYC, 4 cores @ 2600 MHz** |
| RAM | **7.8 GiB** |
| Swap | **0.0 B** |
| Disk | 36.8 GB |
| **Geekbench 6** | **2389 single / 7778 multi** |
| Fio disk IO (64k) | 3146 MiB/s read · 3163 MiB/s write |
| Iperf3 | 9441 Mbps down · 7188 Mbps up |

**The CPU verdict is positive.** GB6 2389 single-core is a modern, well-above-budget core
(typical budget-VPS single-core sits ~1200–1800), and 7778 multi on 4 cores implies ~0.81
scaling efficiency — real cores, not heavily oversubscribed. Disk and network are excellent.
**There is no evidence of the sustained-load throttling that disqualifies Hostinger (§11.3)**,
and it is AWS-backed, which argues against it.

So on the axis the operator asked about — **sustained CPU for the orchestrator — instabox
looks at least as good as anything else evaluated here.**

### 12.2 What actually blocks it

| # | issue | detail |
|---|---|---|
| 1 | **Auto-suspend at 30 min idle** | A new machine pauses once idle for 30 minutes. "Idle" is defined by *interaction* (commands, files, SSH, a visit to a public port URL) — **not by CPU activity** — so a background scheduler ticking every 15 min can still be classified idle. Disable with `instabox auto-suspend <machine> 0`. |
| 2 | **RAM below spec, no swap** | 7.8 GiB against the 8–12 GB requirement, and **0 B swap** — an OOM kills the process outright rather than degrading. Fine for the orchestrator *alone* (TimesFM is a separate service on :8002), tight once anything else co-locates. |
| 3 | **Two days old, invite-only** | No SLA, no track record, no incident history. After a 21-hour outage, this is the same risk class, magnified. |
| 4 | **Pricing undefined** | *"Free until we figure it out. Later: the most competitive pricing."* There is no cost model to plan against, and it runs on AWS underneath — eventual pricing will carry AWS plus a margin. |
| 5 | **No GPU anywhere** | Not a Stage-3 path. |

Pause semantics are worth noting: **paused keeps memory *and* processes**, so a pause is a
freeze, not a kill — but no scheduler ticks fire while frozen, so a 30-minute idle pause
silently drops two sweeps.

### 12.3 Where it is genuinely useful

- **A free shadow/dev instance.** 5 machines free, each 4 vCPU / 8 GB. Good for the dev loop,
  a shadow run, or CI.
- **The external watchdog (§5.1).** It costs nothing, boots in <1 s, has persistent files and
  a Vault for secrets. An external poll of the gateway's public port every 5–10 minutes would
  *both* serve as the missing watchdog *and* defeat the 30-minute auto-suspend — one
  mechanism, two problems solved.
- Not a production orchestrator host, and not a GPU path.

### 12.4 Verdict

| question | answer |
|---|---|
| CPU adequate for the orchestrator? | **Yes** — modern EPYC cores, no throttling evidence, excellent IO/network |
| Always-on capable? | **Only with auto-suspend disabled or defeated by a heartbeat** |
| Production-grade today? | **No** — 2-day-old beta, no SLA, undefined pricing |
| Worth using at all? | **Yes** — as a free shadow instance or the external watchdog |

**Recommendation:** do not make instabox the production orchestrator host. Do consider its
free tier for the **watchdog** — the one piece of infrastructure this assessment has said is
missing and host-independent — where a young platform's immaturity is tolerable and the price
is zero.

### 12.5 Runbook — deploying the watchdog on instabox

The artifact is `scripts/watchdog_liveness.py` (stdlib-only, no `pip install`, one-shot and
cron-driven). It reuses the exact heartbeat set and SQL from `pulse_check_r2.py`'s liveness
gate so there is one source of truth for "is the engine producing".

**Why data-based and not an HTTP ping:** an HTTP health check on the gateway would have
returned 200 for as long as the process happened to be alive on 2026-10-16. It cannot see a
*silent* stall, which is precisely what happened. The script queries `max(timestamp)` per
writer and compares it to `now()`.

**Account limits that shape the design** (`instabox.sh/docs/limits`): 5 machines *including
paused and stopped*, machine size **fixed at 4 vCPU / 8 GiB**, `exec` capped at **10 minutes
and non-streaming**, timeout 1 min–24 h *used once*, 200 GB storage, 16 public ports.

```bash
# 1 — install the CLI and sign in
curl -fsSL https://api.instabox.sh/install.sh | sh
instabox login                       # --device on a machine without a browser

# 2 — create the machine
instabox create --name kairos-watchdog --template universal

# 3 — CRITICAL: turn off the 30-minute idle pause.
#     "idle" is defined by interaction, not CPU activity, so a machine that only
#     runs a background timer can still be classified idle.
instabox auto-suspend kairos-watchdog 0

# 4 — secrets, write-only, injected at boot/start/resume
instabox vault set-file /data/home/watchdog.env --from ./watchdog.env
instabox vault show kairos-watchdog   # confirm what it will receive

# 5 — copy the script in
instabox exec kairos-watchdog --stdin -- \
  bash -c 'cat > /data/watchdog_liveness.py && chmod +x /data/watchdog_liveness.py' \
  < scripts/watchdog_liveness.py

# 6 — prove it works before scheduling it
instabox exec kairos-watchdog -- /usr/bin/python3 /data/watchdog_liveness.py \
  --env-file /data/home/watchdog.env --state /data/watchdog_state.json
#    exit 0 = healthy · 2 = engine down · 3 = check failed · 4 = config missing
```

Schedule it with a systemd timer (the VM has its own kernel and root):

```ini
# /etc/systemd/system/kairos-watchdog.service
[Unit]
Description=Kairos liveness watchdog
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /data/watchdog_liveness.py \
  --env-file /data/home/watchdog.env --state /data/watchdog_state.json

# /etc/systemd/system/kairos-watchdog.timer
[Unit]
Description=Run the Kairos liveness watchdog every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
```

`--env-file` is used rather than systemd's `EnvironmentFile` because the Vault exports env
items only in *login* shells; a `file` item is written under home and is readable by a
service.

**Alert discipline (already implemented and verified):** state lives in
`--state /data/watchdog_state.json`, which survives both a suspend and a fresh start because
`/data` persists. Alerts fire on the down transition, repeat at most every `--realert-min`
(default 30), and a recovery message is posted on the way back up — so a 21-hour outage pages
once and then stops, instead of 250 times.

**Alternative if you don't want a permanently-running machine:** leave auto-suspend ON and
drive the script from outside with `instabox exec kairos-watchdog -- /data/watchdog_liveness.py`
on an external cron. `exec` counts as interaction, so it keeps the machine awake, and a fresh
start after a failed resume is harmless because `/data` persists and the script is stateless
between runs.

**Watchdog-of-the-watchdog:** the machine is itself a single point of failure. Publish a port
(`instabox port publish kairos-watchdog 8080`) and point a free external monitor
(UptimeRobot, cron-job.org) at it. Per the docs, a visit also wakes a machine that paused
itself for idleness, so this doubles as the anti-suspend mechanism. Note the published URL
**is not a secret** — anyone can reach the whole service until unpublished, so put a token in
the path or a header.






