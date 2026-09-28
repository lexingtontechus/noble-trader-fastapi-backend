"""
timesfm_p1_covariates_probe.py — P1.1 experiment: dynamic covariates on TimesFM.

Scope: worklog/kairos/20260927_P0_P4_PROGRAMME.md §3 ·
refs worklog/20260831_TIMESFM_3_UPGRADE_SCOPE.md + 20260915_TIMESFM3_IMPLEMENTATION_SCOPE.md

Proves (or documents blockers for):
  1) 2.5 `forecast()` univariate baseline
  2) 2.5 `forecast_with_covariates()` XReg path with lean dynamic covariates
  3) Multi-series batch through the same call (1 symbol + cluster mates)

Not wired to orchestrator / EV. Research-only.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/timesfm_p1_covariates_probe.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "worklog" / "kairos" / "reports" / "20260927_p1_timesfm_covariates.md"

MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-2.5-200m-pytorch")
HORIZON = 1
CTX = 128  # lean context for the probe (not production 500)


def synth_series(n: int, seed: int, drift: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, 1.0, size=n).cumsum()
    return (100.0 + steps).astype(np.float64)


def main() -> None:
    import timesfm

    lines = [
        "# P1.1 — TimesFM dynamic covariates probe",
        "",
        f"Model: `{MODEL_ID}` · context={CTX} · horizon={HORIZON}",
        "",
        "## 0. Capability surface",
        "",
    ]

    T = timesfm.TimesFM_2p5_200M_torch
    has_fw = hasattr(T, "forecast_with_covariates")
    lines.append(f"- `TimesFM_2p5_200M_torch.forecast_with_covariates` present: **{has_fw}**")
    lines.append(
        "- 2.5 native `forecast(horizon, inputs)` is **univariate batch** (list of series), "
        "not cross-variate attention. True multivariate is TimesFM 3.0."
    )
    lines.append(
        "- `forecast_with_covariates` on 2.5 is the **XReg residual path** "
        "(`xreg + timesfm` / `timesfm + xreg`), not variate attention."
    )
    lines.append(
        "- Requires `ForecastConfig(return_backcast=True)` and `timesfm[xreg]` (jax + sklearn)."
    )
    lines.append("")

    print("loading model…", flush=True)
    t0 = time.time()
    model = T.from_pretrained(MODEL_ID)
    model.compile(
        timesfm.ForecastConfig(
            max_context=CTX,
            max_horizon=HORIZON,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
            return_backcast=True,
        )
    )
    load_s = time.time() - t0
    print(f"loaded in {load_s:.1f}s", flush=True)
    lines.append(f"Model load+compile: **{load_s:.1f}s**")
    lines.append("")

    # ── A. Univariate baseline ──────────────────────────────────────────
    px = synth_series(CTX, seed=1, drift=0.02)
    print("A univariate forecast…", flush=True)
    t0 = time.time()
    pt, q = model.forecast(horizon=HORIZON, inputs=[px])
    t_a = time.time() - t0
    lines.append("## A. Univariate `forecast()`")
    lines.append("")
    lines.append(f"- latency **{t_a:.3f}s**")
    lines.append(f"- point shape {np.array(pt).shape} · q shape {np.array(q).shape}")
    lines.append(f"- point[0] = {float(np.array(pt).ravel()[0]):.6f} · last={px[-1]:.6f}")
    lines.append("")

    # ── B. XReg covariates (lean dynamic set) ───────────────────────────
    # Align with P1 keep-list stand-ins: trend proxy, momentum, vol, cluster return
    tau = np.arange(CTX, dtype=np.float64)
    trend = (px - px.mean()) / (px.std() + 1e-8)  # z-scored price distance
    mom = np.gradient(px)
    vol = np.abs(np.diff(px, prepend=px[0]))
    cluster = synth_series(CTX, seed=2, drift=0.01)

    # dynamic covariates must cover input + horizon (past+future slice)
    # docstring: test_lens = len(dynamic[...][i]) - input_len
    dyn_n = CTX + HORIZON
    def extend(x: np.ndarray, fill: float | None = None) -> np.ndarray:
        pad = np.full(dyn_n - len(x), x[-1] if fill is None else fill, dtype=np.float64)
        return np.concatenate([x, pad])

    dynamic_numerical_covariates = {
        "trend_z": [extend(trend)],
        "momentum": [extend(mom)],
        "vol_abs": [extend(vol)],
        "cluster_ret": [extend(cluster)],
    }

    print("B forecast_with_covariates…", flush=True)
    t0 = time.time()
    try:
        out = model.forecast_with_covariates(
            inputs=[px],
            dynamic_numerical_covariates=dynamic_numerical_covariates,
            xreg_mode="xreg + timesfm",
            normalize_xreg_target_per_input=True,
            ridge=1e-3,
            force_on_cpu=True,
        )
        t_b = time.time() - t0
        # returns (timesfm_outputs, xreg_outputs) — shapes vary; be defensive
        tm_out, xreg_out = out[0], out[1] if len(out) > 1 else None
        lines.append("## B. `forecast_with_covariates()` XReg path")
        lines.append("")
        lines.append(f"- status **OK** · latency **{t_b:.3f}s**")
        lines.append(f"- return type: {type(out)} len={len(out) if hasattr(out, '__len__') else 'n/a'}")

        def brief(obj, name: str) -> None:
            try:
                a = np.array(obj, dtype=object)
                # try numeric
                try:
                    an = np.array(obj, dtype=np.float64)
                    lines.append(
                        f"- `{name}` shape {an.shape} finite={bool(np.isfinite(an).all())} "
                        f"first={float(an.ravel()[0]):.6f}"
                    )
                except Exception:
                    lines.append(f"- `{name}` non-numeric container type={type(obj)} preview={str(obj)[:120]}")
            except Exception as e:
                lines.append(f"- `{name}` unreadable: {e}")

        brief(tm_out, "timesfm_outputs")
        if xreg_out is not None:
            brief(xreg_out, "xreg_outputs")
    except Exception as e:
        t_b = time.time() - t0
        lines.append("## B. `forecast_with_covariates()` XReg path")
        lines.append("")
        lines.append(f"- status **FAIL** · latency **{t_b:.3f}s**")
        lines.append(f"- error: `{type(e).__name__}: {e}`")
    lines.append("")

    # ── C. Multi-series batch + shared covariates ───────────────────────
    px2 = synth_series(CTX, seed=3, drift=-0.01)
    print("C multi-series covariates…", flush=True)
    t0 = time.time()
    try:
        out2 = model.forecast_with_covariates(
            inputs=[px, px2],
            dynamic_numerical_covariates={
                "trend_z": [extend(trend), extend((px2 - px2.mean()) / (px2.std() + 1e-8))],
                "cluster_ret": [extend(cluster), extend(cluster)],
            },
            xreg_mode="timesfm + xreg",
            ridge=1e-3,
            force_on_cpu=True,
        )
        t_c = time.time() - t0
        lines.append("## C. Multi-series `timesfm + xreg`")
        lines.append("")
        lines.append(f"- status **OK** · latency **{t_c:.3f}s** · series=2")
        try:
            arr = np.array(out2[0], dtype=np.float64)
            lines.append(f"- timesfm_out shape {arr.shape} finite={bool(np.isfinite(arr).all())}")
        except Exception:
            lines.append(f"- timesfm_out type={type(out2[0])}")
    except Exception as e:
        t_c = time.time() - t0
        lines.append("## C. Multi-series `timesfm + xreg`")
        lines.append("")
        lines.append(f"- status **FAIL** · {type(e).__name__}: {e}")
    lines.append("")

    # ── D. Verdict for P1.1 ─────────────────────────────────────────────
    lines.append("## D. P1.1 verdict")
    lines.append("")
    lines.append(
        "| Question | Answer |"
    )
    lines.append("|---|---|")
    lines.append(
        "| Can 2.5 take dynamic covariates? | "
        + ("**Yes — via XReg `forecast_with_covariates`**, not native variate attention |"
           if has_fw else "No |")
    )
    lines.append(
        "| Native multivariate (joint attention)? | **TimesFM 3.0 only** (`google/timesfm-3.0-pytorch`, non-commercial weights) |"
    )
    lines.append(
        "| Server path today | `/forecast` → univariate `forecast()` only; quantiles already returned but unused by orchestrator |"
    )
    lines.append(
        "| P1 next | Add `/forecast/covariates` **or** switch experiment model to 3.0; keep lean channel list (trend_z, mom, vol, cluster) |"
    )
    lines.append("")
    lines.append(
        "Research-only. Do not flip EV weights or live gates from this probe."
    )
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    # machine-readable sidecar for later diffs
    meta = {
        "model": MODEL_ID,
        "load_s": load_s,
        "has_forecast_with_covariates": has_fw,
        "ctx": CTX,
        "horizon": HORIZON,
    }
    (OUT.parent / "20260927_p1_timesfm_covariates.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
