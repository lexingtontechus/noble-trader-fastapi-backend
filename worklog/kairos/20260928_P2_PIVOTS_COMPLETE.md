# P2 complete — typed pivots on `nt_macro_forecast` (one LLM store)

**Date:** 2026-09-28 · **Type:** build + interpretation · **Parent:** `20260927_P0_P4_PROGRAMME.md` §4  
**Principle:** one LLM dataset. Calendar is raw vendor data; labels land on the existing `nt_macro_forecast` upsert.

---

## 0. Where things live (consolidated)

| Data | Store | LLM? | Writer |
|---|---|---|---|
| Economic calendar (TDVA `/api/calendar/economic`) | **`.cache/kairos/econ_calendar.json`** | No | `kairos.refresh_calendar_cache` (daily `kairos-daily`) |
| Symbol outlook (trend / macro / narrative) | **`nt_macro_forecast`** | Yes | `macro_forecast_agent.run_macro_forecast` |
| **Typed pivot** (`major_pivot` / `incremental` / `no_change`) | **Same `nt_macro_forecast` row** (`pivot_*` cols) | Yes | Same job after classification (migration **181**) |

No `nt_pivot_*` table. No second JSON LLM dump. Research scripts (`kairos_p2_*`) stay read-only.

```text
TDVA /api/calendar/economic  ──►  .cache/kairos/econ_calendar.json
                                         │
                                         ▼
                         macro_forecast_agent.run_macro_forecast
                         (symbol classify + pivot label via NIM/OpenRouter)
                                         │
                                         ▼
                                  nt_macro_forecast
                         (current_* + pivot_* on every row)
```

---

## 1. Build (this change)

| Piece | What |
|---|---|
| `supabase/migrations/181_nt_macro_forecast_pivots.sql` | `pivot_label`, `pivot_axis`, `pivot_intensity`, `pivot_surprise`, `pivot_as_of`, `pivot_event_title`, `pivot_summary` |
| `kairos_pivot.py` | `attach_pivot_fields`, `select_pivot_candidates`, `label_event`, `latest_pivot_for_rows` |
| `macro_forecast_agent.run_macro_forecast` | After symbol classify, stamp **one** latest pivot onto all rows (same `upsert_macro_forecast`) |
| `supabase_client.NT_MACRO_FORECAST_COLUMNS` | Allowlist includes `pivot_*` |
| Tests | `tests/test_kairos_pivot.py` + existing macro agent tests — **30 green** |

**P2.1 eval artifacts** (standalone): `scripts/kairos_p2_pivot_extract.py`, gold needs event `context` (title-only collapses to `no_change`). NIM works; OpenRouter is 402 until credits.

**P2.2 lead-lag** (standalone): `scripts/kairos_p22_leadlag.py` → `reports/20260927_p22_leadlag.md`.

**P2.3 stamps:** still **not** wired into gates. Pivot fields are shadow/read-only on `nt_macro_forecast`.

---

## 2. What “lead-lag pivot labels vs `dir_flip`” means

Two clocks:

| Clock | Object | Product meaning |
|---|---|---|
| **T0** | Macro event / labeled pivot (`major_pivot`) | When the *information* hits (calendar release / statement) |
| **T1** | HMM regime-cell **`dir_flip`** | When *price labels* flip direction (your transition object) |

**Lead-lag** = distribution of `T1 − T0` (hours). Three regimes of meaning:

| Pattern | What it implies | Use |
|---|---|---|
| **T0 ≪ T1** (text/pivot *leads* flip) | Macro is a **leading** channel — raise flip *prior* / size before HMM confirms | T-Layer prior, R-Layer feature on `τ` after pivot |
| **T0 ≈ T1** (co-move, ~0–6h) | Same news moves prices *and* the HMM — **confirmation**, not prediction | Confirm/deny `dir_flip` (union+dedup); do not pre-size |
| **T0 ≫ T1** (flip then event) | Event is *not* the driver; ignore for priors | Drop from T-Layer |

**P2.2 measurement (this book, calendar timestamps):**

| Window after T0 | lift vs baseline |
|---|---:|
| **0–6h** | **1.31×** |
| 6–24h | 0.93× |
| 24–72h | 0.76× |
| 72–168h | 0.98× |

→ **Co-move only** (1.3× in the first hours). Calendar *timestamps* do **not** lead for days.

Literature (“FOMC text leads weeks”) is about **statement language**, not the calendar row. That is why pivots now store `pivot_summary` / `pivot_event_title` on `nt_macro_forecast` — the **narrative** is the candidate leading channel.

---

## 3. Recommendations (product)

1. **Do not** raise flip priors or size off calendar T0 alone (P2.2). Treat calendar as **confirm** in `0–6h`, not forecast.
2. **Do** use `nt_macro_forecast.pivot_label == 'major_pivot'` as a **shadow T-Layer stamp** only: log `pivot_as_of` next to `dir_flip` times; score lead-lag on **narrative** (`pivot_summary`, `narrative`) once enough rows accumulate.
3. **Gate rule (when/if P2.3):** LLM pivot feature **off** under `vol_expand` / macro-shock (documented sign-flip). Keep `major_pivot → dir_flip_candidate` as research map only.
4. **One job, one table:** all future LLM macro context joins `run_macro_forecast` → `nt_macro_forecast`. Do not add `nt_pivot_labels` or JSON side-cars.
5. **P3** can ignore pivots until narrative lead-lag clears a bar; ship ranker on P0 + `timesfm_gap` + `cell_p` first.
6. **OpenRouter 402** — fund the key or stay on NVIDIA NIM (`P2_SKIP_OPENROUTER=1` default in the eval script).

---

## 4. Stage status

| Stage | Status |
|---|---|
| P2.1 extraction | Built · gold w/ `context` + calendar sample (NIM) |
| P2.2 lead-lag | **Done** — co-move 1.3× @0–6h, no multi-day lead on calendar T0 |
| P2.3 live stamp | **Gated** — wait for narrative lead-lag evidence |

**Migration 181 must be applied** on Supabase before the next `run_macro_forecast` writes `pivot_*` (columns missing → filtered out by allowlist is safe fail-open; PostgREST 400 if allowlist includes unknown cols — allowlist is updated, so apply migration first).
