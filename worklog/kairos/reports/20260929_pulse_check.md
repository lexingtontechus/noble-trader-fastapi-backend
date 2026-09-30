# Pulse check — 2026-09-29

**Deploy:** Studio `5f4d392` = local `main` (in sync: `fix(dynamic-atr): constrain geometry to replay band TP 9-17 / SL 0.5-2`).  
**Also on tree (recent):** `6a855e0` v8.1.0 · `5775389` TimesFM P1.3 quantile capture · `cdefbf1` dashboard cache.

---

## 0. Roadmap — OVERDUE (report first)

| mechanism | status | review_at | Check |
|---|---|---|---|
| **aapl_regime_staleness_vs_realized_price** | blocked | 2026-09-27 | `nt_trend_edge_by_symbol` has only **2 rows** — still thin. Keep blocked; bump date after more shadow days. |
| **tier1_context_alignment_gate_scope** | pending | 2026-09-27 | Shadow still correct — live not justified without `context_aligned_ok=false` volume. |
| **kairos_daily_job** | pending | 2026-09-29 | Heartbeat was healthy 09-27 (`rank_ingest 500`); Studio catch-up shows light-sweep recovery. Keep hourly pulse. |
| **publish_path_redesign** | pending | 2026-09-29 | 48h light: **gate_fail 763 · suppressed 148 · qualified 89**. Publish path live; quality fork separate. |
| **eod_resolver_scan_full** | pending | 2026-09-29 | `expired_missing_exit_price` path OK (no regression signal in 48h outcomes). |
| **dynamic_atr_mode_flip** | pending | 2026-09-29 | Pre-read was positive; still need promoted vs control after more light outcomes. |
| **calibration_evidence_pool** | pending | 2026-09-29 | Still shadow. |

**Not overdue but due soon (2026-09-30 / 10-01):** `p_win_in_ev_mode`, `light_publish_geometry_gate`, `apscheduler_studio_deployment_realignment`, `qualified_quality_addons_D1_D2` (blocked on B5).

**Action:** push `review_at` forward for the five 09-29 rows with a real next date (e.g. 2026-10-03) once checks below are read — do not leave stale dates.

---

## 1. Deployment drift

Studio HEAD **equals** local `5f4d392`. **No deploy gap.**

---

## 2. Studio logs (span from 2026-09-28 08:33 UTC)

| Metric | Value |
|---|---|
| Starting sweep orchestrator | 18 |
| ERROR | **12** |
| **Traceback** | **0** ✓ |

Error types:
- 9× `record_signal_stream_meta_shadow: failed` (matches `signal_stream_meta_model_mode: off` transition — should drop next run)
- 1× `record_heavy_sweep_atr_anchored_shadow: failed`
- 2× `eod_resolver: PATCH signal … failed`

---

## 3. Scheduler catch-up

Recent startup-catchup fired for: calibration state, macro regime, **missed light sweep 14/14 symbols** (2026-09-29 06:02). Restart gaps still real on free-plan Studio — catch-up is working.

---

## 4. EOD / pipeline

| Metric | Value |
|---|---|
| Last `resolved_at` | 2026-09-29T19:38Z (fresh) |
| 48h outcomes (n=1000 cap) | **sl_hit 551 · tp_hit 409 · expired 40 (4%)** |
| Expired missing exit_price | not flagged in current sample |

Expired share is down vs earlier ~57% quality-fork days (geometry/ATR work). Still the right quality metric (`expiry_rate_threshold_alert`).

---

## 5. Rank board / Kairos

- `kairos_rank_score` stamped on **2,799** rows since 2026-09-27 (tracker feeding).
- `nt_trend_edge_by_symbol`: **2 rows** only.

---

## 6. Calibration (v_eod_calibration_bias_latest)

| Status | Symbols |
|---|---|
| OVERCONFIDENT | AAPL, BTCUSD, EURUSD, GBPUSD, US500, USDCAD, USDJPY, XAUAUD |
| UNDERCONFIDENT | US30USD |
| CALIBRATED | AUDUSD, USDCHF, XAGUSD |

Gold/silver pairs mixed; several majors still overconfident.

---

## 7. LLM on Studio — **DOWN** (root cause)

```
HF_HUB_OFFLINE=1
HF_HOME=/teamspace/studios/this_studio/hf_cache   # has TimesFM + MiniLM only
llm_server.py forces HF_HOME=.cache/huggingface   # empty of LFM
```

`llm.log`: `OSError: couldn't connect to huggingface.co … offline mode` → **startup failed**.  
**No LFM weights anywhere in Studio cache.** `/health` = LLM_DOWN.

**Fix (pick one):**
1. Prefetch when network on:  
   `HF_HUB_OFFLINE=0 huggingface-cli download LiquidAI/LFM2.5-1.2B-Instruct --local-dir /teamspace/studios/this_studio/hf_cache/hub/...`  
   then keep offline.
2. Align `llm_server.py` `HF_HOME` with **Studio** `hf_cache` (same as TimesFM), not `.cache/huggingface`.
3. Allow outbound HF briefly at boot (remove `HF_HUB_OFFLINE=1` for first load).

---

## 8. Spark-X2.5-1.7B vs LFM (small extract model)

| | **Spark-X2.5-1.7B** (`XHToken/Spark-X2.5-1.7B`) | **LFM2.5-1.2B-Instruct** |
|---|---|---|
| License | Apache 2.0 | Liquid “other” (check) |
| Size | ~2B BF16 | ~1.2B |
| Context | 1M (hybrid SWA) | shorter |
| IFEval | **89.5** (strong structured output) | good but less public bench |
| Agent / JSON | Strong (BFCL 46.9) | Weaker on tools |
| Load | **`trust_remote_code=True`** + chat_template.jinja | Standard transformers |
| Deploy | vLLM/SGLang recommended; transformers OK | transformers / GGUF |

**Verdict:** **Yes — Spark-X2.5-1.7B is a solid alternative**, better fit if we want reliable **JSON classify** and later agentic steps. Costs more VRAM than LFM-1.2B and needs `trust_remote_code`. For pure `macro_forecast` JSON either works; Spark’s IFEval is the tie-breaker.

**Recommend:** use **`XHToken/Spark-X2.5-1.7B`** as `LOCAL_LLM_MODEL`, serve via **`llm_server.py`** with `trust_remote_code=True` (one-line change), prefetch weights into **`/teamspace/studios/this_studio/hf_cache`**. Keep NIM as fallback.

---

## Next actions

1. **Unblock LLM:** fix HF_HOME + prefetch Spark or LFM (Studio network or offline pack).  
2. **Roadmap dates:** move 2026-09-29 overdue rows to 2026-10-03 after this read-out.  
3. Watch `record_signal_stream_meta_shadow` errors drop after `signal_stream_meta_model_mode: off`.  
4. Calibration / expiry still the quality program (not new).
