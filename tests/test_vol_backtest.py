"""test_vol_backtest.py — Phase 0 vol-forecast backtest framework tests.

Only numpy/scipy/pandas/pytest are required. Model arms (TimesFM server,
Chronos-2) are exercised through their pure extraction math and layout
handling, never through live services.

Covers the corrected methodology's load-bearing properties:
  * RV / trailing-RV arithmetic (hand-computed)
  * calendar filter drops FX weekends, keeps crypto bars
  * QLIKE identity + asymmetry; directional accuracy + persistence null
  * Newey-West variance (MA(1) closed form), DM significance, Holm ordering
  * naive / EWMA / Log-HAR / blend produce finite positive forecasts
  * Log-HAR beats naive on weekly-cyclic vol (implementation sanity)
  * harness no-lookahead sentinel + record accounting
  * quantile-spread sigma recovery; TimesFM 9- and 10-level layouts
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vol_backtest.realized import (  # noqa: E402
    calendar_mask,
    forward_rv,
    log_returns,
    rv_trailing,
    synthetic_symbol,
    annualize_horizon,
)
from vol_backtest.metrics import (  # noqa: E402
    directional_accuracy,
    interval_coverage,
    persistence_direction_accuracy,
    qlike,
)
from vol_backtest.dm import _newey_west_variance, dm_test, holm_adjust  # noqa: E402
from vol_backtest.arms import (  # noqa: E402
    EWMARV,
    HAR_EWMA,
    LogHAR,
    NaiveRV,
    rv_from_quantile_spread,
)
from vol_backtest.harness import backtest_symbol, aggregate_metrics, dm_family  # noqa: E402
from vol_backtest.model_arms import TimesFM25HTTP  # noqa: E402


# ---------------------------------------------------------------------------
# Realized volatility
# ---------------------------------------------------------------------------

class TestRealized:
    def test_forward_rv_hand_computed(self):
        # r2[i] = (ln P_{i+1} / P_i)^2 — explicit prices, no cumsum confusion
        prices = np.array([100.0, 101.0, 99.0, 100.5, 100.0, 102.0])
        d = np.diff(np.log(prices))  # d[0]=ln(101/100), d[1]=ln(99/101), ...
        r2 = d ** 2
        rv = forward_rv(r2, cutoff=1, horizon=2)   # d[1], d[2]
        expected = np.sqrt(d[1] ** 2 + d[2] ** 2)
        assert rv == pytest.approx(expected, rel=1e-12)
        rv0 = forward_rv(r2, cutoff=0, horizon=3)  # d[0], d[1], d[2]
        assert rv0 == pytest.approx(np.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2), rel=1e-12)

    def test_forward_rv_out_of_bounds_is_nan(self):
        r2 = np.ones(5)
        assert np.isnan(forward_rv(r2, cutoff=3, horizon=5))

    def test_rv_trailing_matches_manual(self):
        r = np.array([0.01, 0.02, -0.01, 0.05, 0.01])
        r2 = r * r
        out = rv_trailing(r2, 3)
        # out[t] = sqrt(sum r2[t-2..t]) — NaN for t<2
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert out[2] == pytest.approx(np.sqrt(r2[0] + r2[1] + r2[2]), rel=1e-12)
        assert out[4] == pytest.approx(np.sqrt(r2[2] + r2[3] + r2[4]), rel=1e-12)

    def test_calendar_mask_fx_vs_crypto(self):
        # raw hourly grid over 21 days (includes weekends) — the synthetic
        # generator pre-filters, so calendar_mask must be tested on the grid
        n_hours = 21 * 24
        ts_all = np.datetime64("2025-10-01T00:00", "ns") + np.arange(n_hours).astype("timedelta64[h]")
        m_fx = calendar_mask(ts_all, "USDJPY")
        assert m_fx.sum() < n_hours            # weekend bars dropped
        assert (~m_fx).sum() > 0
        m_btc = calendar_mask(ts_all, "BTCUSD")
        assert m_btc.all()                     # crypto keeps all bars
        # Saturday 2025-10-04 and Sunday 2025-10-05 must be (mostly) dropped
        import pandas as pd

        idx = pd.DatetimeIndex(ts_all)
        sat = np.asarray(idx.dayofweek == 5)
        assert (~m_fx[sat]).all()              # all Saturday bars dropped

    def test_annualization_scales(self):
        # BTC (8760 bpy) vs FX (6240 bpy): same per-bar vol, BTC annualizes higher
        rv = 0.01 * np.sqrt(24)  # 1% per bar over 24 bars
        a_btc = annualize_horizon(rv, 24, "BTCUSD")
        a_fx = annualize_horizon(rv, 24, "USDJPY")
        assert a_btc > a_fx
        assert a_btc / a_fx == pytest.approx(np.sqrt(8760 / 6240), rel=1e-9)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_qlike_zero_when_perfect(self):
        rv = np.array([0.01, 0.02, 0.005, 0.03])
        assert qlike(rv, rv) == pytest.approx(0.0, abs=1e-15)

    def test_qlike_asymmetric_penalises_underforecast(self):
        rv = np.array([0.02])
        under = qlike(np.array([0.01]), rv)   # F < RV
        over = qlike(np.array([0.04]), rv)    # F > RV (same ratio distance)
        assert under > over

    def test_directional_accuracy_and_persistence_null(self):
        prev = np.array([1.0, 1.0, 1.0, 1.0])
        rv = np.array([1.2, 0.8, 1.1, 0.9])          # up, down, up, down
        prev2 = np.array([0.9, 1.2, 0.8, 1.2])       # today up, down, up, down
        f_good = np.array([1.3, 0.7, 1.2, 0.8])      # matches rv direction 4/4
        f_bad = np.array([0.7, 1.3, 0.8, 1.2])       # opposite 0/4
        f_flat = np.array([1.0, 1.0, 1.0, 1.0])      # naive: no direction, 0/4
        assert directional_accuracy(f_good, rv, prev) == pytest.approx(1.0)
        assert directional_accuracy(f_bad, rv, prev) == pytest.approx(0.0)
        assert directional_accuracy(f_flat, rv, prev) == pytest.approx(0.0)
        # persistence null: today's direction repeats tomorrow (4/4 here)
        assert persistence_direction_accuracy(rv, prev, prev2) == pytest.approx(1.0)

    def test_interval_coverage(self):
        rv = np.linspace(0.5, 1.5, 101)
        lo = np.full(101, 0.6)
        hi = np.full(101, 1.4)
        cov = interval_coverage(lo, hi, rv)
        assert cov == pytest.approx(0.80, abs=0.01)


# ---------------------------------------------------------------------------
# DM / HAC / Holm
# ---------------------------------------------------------------------------

class TestDM:
    def test_newey_west_ma1_closed_form(self):
        # Contract test: NW(lag=1) = (g0 + 2*w1*g1)/n with g_j the sample
        # autocovariances (biased, /n) and Bartlett w1 = 1 - 1/2 = 0.5.
        # Sanity bound vs the TRUE long-run variance of MA(1):
        #   gamma0 + 2*gamma1 = (1+theta^2) + 2*theta  (unit-variance shocks)
        rng = np.random.default_rng(11)
        e = rng.standard_normal(200_000)
        theta = 0.6
        d = e[1:] + theta * e[:-1]
        n = len(d)
        dbar = d.mean()
        xc = d - dbar
        g0 = float(np.dot(xc, xc)) / n
        g1 = float(np.dot(xc[1:], xc[:-1])) / n
        v = _newey_west_variance(d, lag=1)
        expected = (g0 + 2.0 * 0.5 * g1) / n
        assert v == pytest.approx(expected, rel=1e-9)
        # long-run variance sanity: NW(1) sits between gamma0 and the truth
        assert g0 / n < v < (g0 + 2.0 * g1) / n

    def test_dm_favors_lower_noise_forecaster(self):
        rng = np.random.default_rng(3)
        rv = np.abs(0.02 + 0.008 * rng.standard_normal(400)) + 0.005
        # multiplicative noise keeps forecasts strictly positive (QLIKE domain)
        f_good = rv * (1.0 + 0.05 * rng.standard_normal(400))
        f_bad = np.abs(rv * (1.0 + 0.50 * rng.standard_normal(400)))
        lg = rv / f_good
        lb = rv / f_bad
        lg = lg - np.log(lg) - 1
        lb = lb - np.log(lb) - 1
        res = dm_test(lg, lb, hac_lag=0, name_a="good", name_b="bad")
        assert res.mean_loss_diff < 0
        assert res.p_value < 0.05
        assert res.a_better

    def test_dm_identical_forecasts_not_significant(self):
        rng = np.random.default_rng(5)
        rv = np.abs(0.02 + 0.01 * rng.standard_normal(300))
        l = np.maximum(rv, 1e-12) / np.maximum(rv, 1e-12)
        l = l - np.log(l) - 1
        res = dm_test(l, l, hac_lag=0)
        assert not res.a_better
        assert res.p_value > 0.5 or np.isnan(res.p_value)

    def test_holm_ordering(self):
        # p = [.01, .04, .03, .5] -> Holm: .04, .08, .06(actually sorted: .01*.4=.04,
        # .03*3=.09? no — Holm: sorted .01,.03,.04,.5 -> .04, .09, .08(capped by
        # running max .09), .5) — check monotonicity and >= raw
        raw = np.array([0.01, 0.04, 0.03, 0.5])
        adj = holm_adjust(raw)
        assert np.all(np.isfinite(adj))
        assert adj[0] == pytest.approx(0.04)
        assert np.all(adj[1:] >= raw[1:])
        assert np.all(np.diff(adj[np.argsort(raw)]) >= -1e-12)


# ---------------------------------------------------------------------------
# Baseline arms
# ---------------------------------------------------------------------------

class TestArms:
    @pytest.fixture()
    def fx_series(self):
        return synthetic_symbol("USDJPY", n_days=200, seed=42)

    def test_baselines_finite_positive(self, fx_series):
        s = fx_series
        for arm in (NaiveRV(), EWMARV(), LogHAR(), HAR_EWMA()):
            f = arm.forecast(s.prices, s.timestamps, 24)
            assert f is not None, arm.name
            assert np.isfinite(f.rv) and f.rv > 0, arm.name

    def test_ewma_interval_brackets_forecast(self, fx_series):
        f = EWMARV().forecast(fx_series.prices, fx_series.timestamps, 24)
        assert f.lo80 is not None and f.hi80 is not None
        assert f.lo80 < f.rv < f.hi80

    def test_har_beats_naive_on_weekly_cyclic_vol(self):
        """Implementation sanity: on a weekly vol cycle, Log-HAR's m120
        component must extract information the last-24h naive cannot."""
        rng = np.random.default_rng(9)
        n = 24 * 120  # 120 days
        t = np.arange(n)
        sigma = 0.004 * (1.0 + 0.9 * np.sin(2 * np.pi * t / 168.0))  # weekly cycle
        rets = rng.normal(0.0, sigma)
        prices = 100.0 * np.exp(np.cumsum(rets))
        har, naive = LogHAR(), NaiveRV()
        H = 24
        r2 = rets ** 2
        fs_h, fs_n, acts = [], [], []
        for cut in range(24 * 40, n - H, 24):  # ~80 cutoffs
            fh = har.forecast(prices[: cut + 1], None, H)
            fn = naive.forecast(prices[: cut + 1], None, H)
            if fh is None or fn is None:
                continue
            fs_h.append(fh.rv)
            fs_n.append(fn.rv)
            acts.append(float(np.sqrt(np.sum(r2[cut:cut + H]))))
        assert len(acts) >= 60
        q_h = qlike(np.array(fs_h), np.array(acts))
        q_n = qlike(np.array(fs_n), np.array(acts))
        assert q_h < q_n, f"HAR {q_h:.5f} should beat naive {q_n:.5f}"


# ---------------------------------------------------------------------------
# Quantile-spread extraction + TimesFM layouts
# ---------------------------------------------------------------------------

class TestQuantileSpread:
    def test_sigma_recovery_from_normal_band(self):
        rng = np.random.default_rng(21)
        H = 24
        sigma_true = 0.01
        from scipy import stats as st

        z = st.norm.ppf(0.9)
        samples = rng.normal(0.0, sigma_true, size=(200, H))
        # build average empirical q10/q90 per step from the sample distribution
        q10 = np.quantile(samples, 0.10, axis=0)
        q90 = np.quantile(samples, 0.90, axis=0)
        rv = rv_from_quantile_spread(q10, q90)
        expected = sigma_true * np.sqrt(H)
        assert rv == pytest.approx(expected, rel=0.15)

    def test_timesfm_layout_10_levels(self):
        H = 6
        rng = np.random.default_rng(2)
        path = np.zeros((H, 10))
        path[:, 0] = 0.0                                    # mean
        for k, q in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]):
            path[:, k + 1] = st_norm_ppf(q) * 0.01
        body = {"quantile_forecast": [path.tolist()]}
        f = TimesFM25HTTP._extract(body, H)
        assert f is not None and np.isfinite(f.rv)
        assert f.rv == pytest.approx(0.01 * np.sqrt(H), rel=1e-6)

    def test_timesfm_layout_9_levels(self):
        H = 4
        path = np.zeros((H, 9))
        for k, q in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]):
            path[:, k] = st_norm_ppf(q) * 0.02
        body = {"quantile_forecast": [path.tolist()]}
        f = TimesFM25HTTP._extract(body, H)
        assert f is not None
        assert f.rv == pytest.approx(0.02 * np.sqrt(H), rel=1e-6)


def st_norm_ppf(q: float) -> float:
    from scipy import stats as st

    return float(st.norm.ppf(q))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class SentinelArm(NaiveRV):
    """Records exactly what it was shown — the no-lookahead auditor."""

    name = "sentinel"

    def __init__(self):
        super().__init__()
        self.seen: list[tuple[int, float]] = []

    def forecast(self, prices, timestamps, horizon):
        self.seen.append((len(prices), float(prices[-1])))
        return super().forecast(prices, timestamps, horizon)


class TestHarness:
    def test_no_lookahead_sentinel(self):
        s = synthetic_symbol("USDJPY", n_days=80, seed=3)
        prices = np.asarray(s.prices)
        sentinel = SentinelArm()
        arms = [sentinel, NaiveRV(), EWMARV(), LogHAR()]
        res = backtest_symbol(s, arms, [24], spacing=24, min_train_bars=720,
                              max_cutoffs=10)
        assert sentinel.seen, "sentinel was never called"
        for (n_seen, last_price), rec in zip(sentinel.seen, [
            r for r in res.records if r.arm == "sentinel"
        ]):
            # cutoff t -> exactly t+1 prices, last one is prices[t]
            assert n_seen == rec.cutoff + 1
            assert last_price == prices[rec.cutoff]

    def test_record_accounting(self):
        s = synthetic_symbol("BTCUSD", n_days=60, seed=4)
        arms = [NaiveRV(), LogHAR()]
        res = backtest_symbol(s, arms, [24], spacing=24, max_cutoffs=15)
        n_records = sum(1 for r in res.records if r.horizon == 24)
        n_cutoffs = len({r.cutoff for r in res.records})
        assert n_records == n_cutoffs * len(arms)
        assert res.arm_stats["naive"].n_success == n_cutoffs

    def test_aggregate_metrics_rows(self):
        s = synthetic_symbol("BTCUSD", n_days=90, seed=5)
        res = backtest_symbol(
            s, [NaiveRV(), LogHAR()], [24], spacing=24, max_cutoffs=20
        )
        rows = aggregate_metrics(res)
        by_arm = {r["arm"]: r for r in rows}
        assert set(by_arm) == {"naive", "har"}
        for r in rows:
            assert r["n"] > 0
            assert np.isfinite(r["qlike"])
            assert np.isfinite(r["dir_null_persistence"])
        # naive never has an interval; har does
        assert np.isnan(by_arm["naive"]["coverage80"])
        assert np.isfinite(by_arm["har"]["coverage80"])

    def test_dm_family_vs_har_and_naive(self):
        s = synthetic_symbol("USDJPY", n_days=150, seed=6)
        # NOTE: no max_cutoffs — subsampling would widen the effective cutoff
        # spacing and legitimately change the HAC lag (spacing enters the lag).
        res = backtest_symbol(
            s, [NaiveRV(), EWMARV(), LogHAR()], [24, 48], spacing=24
        )
        rows = dm_family(res, reference="har", second_reference="naive")
        assert rows, "no DM rows produced"
        arms_tested = {(r["arm_a"], r["arm_b"]) for r in rows}
        assert ("naive", "har") in arms_tested
        assert ("ewma", "har") in arms_tested
        # HAC lag: H=48 with spacing 24 -> overlap -> lag 1; H=24 -> lag 0
        lag_by_h = {}
        for r in rows:
            lag_by_h.setdefault(r["horizon"], set()).add(r["hac_lag"])
        assert lag_by_h.get(24) == {0}
        assert lag_by_h.get(48) == {1}
        for r in rows:
            assert np.isfinite(r["p_value"])
            assert "p_holm" in r and "significant_holm" in r


# ---------------------------------------------------------------------------
# Runner end-to-end (subprocess)
# ---------------------------------------------------------------------------

class TestRunnerSmoke:
    def test_synthetic_smoke_run(self, tmp_path):
        import subprocess

        out = tmp_path / "smoke"
        cmd = [
            sys.executable, str(ROOT / "scripts" / "run_vol_backtest.py"),
            "--synthetic", "--symbols", "USDJPY,BTCUSD",
            "--horizons", "24", "--cutoffs", "12",
            "--arms", "naive,ewma,har,har_ewma", "--no-models",
            "--days", "120", "--out", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert (out / "results.csv").exists()
        assert (out / "dm_tests.csv").exists()
        assert (out / "summary.md").exists()
        assert (out / "records.csv").exists()
        import pandas as pd

        df = pd.read_csv(out / "results.csv")
        assert set(df["arm"]) == {"naive", "ewma", "har", "har_ewma"}
        assert set(df["symbol"]) == {"USDJPY", "BTCUSD"}
        assert df["qlike"].notna().all()
        summary = (out / "summary.md").read_text()
        assert "Log-HAR" in summary
        assert "Skipped arms" not in summary  # no model arms requested
