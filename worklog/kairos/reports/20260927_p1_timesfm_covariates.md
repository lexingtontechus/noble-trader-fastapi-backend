# P1.1 — TimesFM dynamic covariates probe

Model: `google/timesfm-2.5-200m-pytorch` · context=128 · horizon=1

## 0. Capability surface

- `TimesFM_2p5_200M_torch.forecast_with_covariates` present: **True**
- 2.5 native `forecast(horizon, inputs)` is **univariate batch** (list of series), not cross-variate attention. True multivariate is TimesFM 3.0.
- `forecast_with_covariates` on 2.5 is the **XReg residual path** (`xreg + timesfm` / `timesfm + xreg`), not variate attention.
- Requires `ForecastConfig(return_backcast=True)` and `timesfm[xreg]` (jax + sklearn).

Model load+compile: **144.4s**

## A. Univariate `forecast()`

- latency **0.395s**
- point shape (1, 97) · q shape (1, 97, 10)
- point[0] = 99.648239 · last=95.393597

## B. `forecast_with_covariates()` XReg path

- status **OK** · latency **5.463s**
- return type: <class 'tuple'> len=2
- `timesfm_outputs` shape (1, 1) finite=True first=95.393599
- `xreg_outputs` shape (1, 1, 10) finite=True first=95.393599

## C. Multi-series `timesfm + xreg`

- status **OK** · latency **0.872s** · series=2
- timesfm_out shape (2, 1) finite=True

## D. P1.1 verdict

| Question | Answer |
|---|---|
| Can 2.5 take dynamic covariates? | **Yes — via XReg `forecast_with_covariates`**, not native variate attention |
| Native multivariate (joint attention)? | **TimesFM 3.0 only** (`google/timesfm-3.0-pytorch`, non-commercial weights) |
| Server path today | `/forecast` → univariate `forecast()` only; quantiles already returned but unused by orchestrator |
| P1 next | Add `/forecast/covariates` **or** switch experiment model to 3.0; keep lean channel list (trend_z, mom, vol, cluster) |

Research-only. Do not flip EV weights or live gates from this probe.
