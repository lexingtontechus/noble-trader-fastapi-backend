"""Kairos path risk (fixed-horizon MAE/MFE) + Kaplan-Meier regime survival
(2026-09-25, worklog/kairos/20260925_PATH_RISK_AND_REGIME_SURVIVAL.md)."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import numpy as np
import pytest

from regime_platform.sweep_orchestrator import kairos as K

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("kairos_weekly_check_ps", ROOT / "scripts" / "kairos_weekly_check.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)


def _bars(rows):
    return [{"t": i * 3600.0, "o": c, "h": h, "l": lo, "c": c} for i, (h, lo, c) in enumerate(rows)]


# entry 100; bar0 = entry bar (its range must be ignored), bars 1..4 walk
BARS = _bars([(150.0, 50.0, 100.0),      # entry bar: extreme range, excluded
              (101.0, 99.0, 100.5),
              (103.0, 98.0, 102.0),
              (102.5, 96.0, 97.0),
              (104.0, 97.5, 103.0),
              (105.0, 100.0, 104.0)])


class TestPathExcursions:
    def test_buy_window_excludes_entry_bar(self):
        out = K.path_excursions(100.0, BARS, True, horizons=(4,))
        assert out["mfe_4h_pct"] == pytest.approx(4.0)       # max high bars1..4 = 104
        assert out["mae_4h_pct"] == pytest.approx(4.0)       # min low = 96

    def test_sell_is_mirror(self):
        out = K.path_excursions(100.0, BARS, False, horizons=(4,))
        assert out["mae_4h_pct"] == pytest.approx(4.0) and out["mfe_4h_pct"] == pytest.approx(4.0)
        out = K.path_excursions(100.0, BARS[:3], False, horizons=(2,))
        assert out == {"mae_2h_pct": pytest.approx(3.0), "mfe_2h_pct": pytest.approx(2.0)}

    def test_non_negative_when_price_never_crosses(self):
        up = _bars([(100, 100, 100)] + [(100 + i + 0.5, 100 + i, 100 + i) for i in range(1, 6)])
        out = K.path_excursions(100.0, up, True, horizons=(4,))
        assert out["mae_4h_pct"] == 0.0 and out["mfe_4h_pct"] > 0

    def test_drift_inside_excursion_band(self):
        rng = np.random.default_rng(7)
        for _ in range(200):
            c = 100 + np.cumsum(rng.normal(0, 1, 50))
            rows = [(ci + abs(rng.normal(0, .5)), ci - abs(rng.normal(0, .5)), ci) for ci in c]
            bars = _bars(rows)
            entry = float(c[0])
            for buy in (True, False):
                d = K.drift_fields(entry, list(c), buy, horizons=(4, 12, 24, 48))
                p = K.path_excursions(entry, bars, buy)
                for h in (4, 12, 24, 48):
                    assert -p[f"mae_{h}h_pct"] - 1e-9 <= d[f"drift_{h}h_pct"] <= p[f"mfe_{h}h_pct"] + 1e-9

    def test_short_or_bad_input_is_none(self):
        out = K.path_excursions(100.0, BARS[:3], True)
        assert all(v is None for v in out.values())
        assert all(v is None for v in K.path_excursions(0.0, BARS, True).values())
        holed = [dict(b) for b in BARS]
        holed[2]["l"] = None
        out = K.path_excursions(100.0, holed, True, horizons=(1, 4))
        assert out["mae_1h_pct"] == pytest.approx(1.0) and out["mae_4h_pct"] is None

    def test_columns_match_migration(self):
        sql = (ROOT / "supabase/migrations/170_kairos_path_excursions.sql").read_text(encoding="utf-8")
        for c in K.PATH_COLUMNS:
            assert f"ADD COLUMN IF NOT EXISTS {c}" in sql
        for h in K.PATH_HORIZONS:
            assert f"AS mae{h}," in sql or f"AS mae{h}\n" in sql
            assert f"AS mfe{h}" in sql

    def test_view_keeps_168_columns_in_order(self):
        """CREATE OR REPLACE VIEW fails if existing column order changes."""
        def cols(path):
            body = (ROOT / path).read_text(encoding="utf-8").split("CREATE OR REPLACE VIEW v_kairos_signal_episodes")[1]
            sel = body.split("FROM ranked r")[0].split("SELECT r.symbol")[1]
            return [ln.split(" AS ")[-1].strip().rstrip(",") if " AS " in ln else ln.strip().rstrip(",").split(".")[-1]
                    for ln in ("r.symbol" + sel).splitlines() if ln.strip() and not ln.strip().startswith(("WHEN", "ELSE", "||", "CASE r.best"))]
        old = cols("supabase/migrations/168_kairos_g2_gate_outcome_and_episodes.sql")
        new = cols("supabase/migrations/170_kairos_path_excursions.sql")
        assert new[:len(old)] == old
        assert new[len(old):] == [f"{k}{h}" for k in ("mae", "mfe") for h in (4, 12, 24, 48)]


class TestKaplanMeier:
    def test_no_censoring_equals_empirical(self):
        km = W.kaplan_meier([1, 2, 2, 3, 5], [True] * 5)
        assert W.km_at(km, 0.5) == 1.0
        assert W.km_at(km, 1) == pytest.approx(0.8)
        assert W.km_at(km, 2) == pytest.approx(0.4)
        assert W.km_at(km, 4) == pytest.approx(0.2)
        assert W.km_at(km, 5) == pytest.approx(0.0)
        assert W.km_median(km) == 2

    def test_textbook_censoring(self):
        # durations 3,5+,6,8+,10 : S(3)=.8, S(6)=.8·(2/3), S(10)=0
        km = W.kaplan_meier([3, 5, 6, 8, 10], [True, False, True, False, True])
        assert W.km_at(km, 4) == pytest.approx(0.8)
        assert W.km_at(km, 7) == pytest.approx(0.8 * 2 / 3)
        assert W.km_at(km, 10) == pytest.approx(0.0)
        assert list(km.n_risk) == [5, 3, 1]

    def test_ci_brackets_estimate(self):
        rng = np.random.default_rng(1)
        t = rng.exponential(5, 300)
        e = rng.random(300) > 0.2
        km = W.kaplan_meier(t, e)
        inner = km[(km.S > 0.05) & (km.S < 0.95)]
        assert ((inner.lo <= inner.S) & (inner.S <= inner.hi)).all()
        assert (inner.hi - inner.lo).max() < 0.2

    def test_memoryless_km_matches_geometric(self):
        rng = np.random.default_rng(3)
        t = np.ceil(rng.exponential(1 / 0.12, 5000))
        e = np.ones_like(t, dtype=bool)
        km = W.kaplan_meier(t, e)
        for x in (6, 12, 24):
            assert W.km_at(km, x) == pytest.approx(W.geometric_survival(t, e, x), abs=0.05)
            # memoryless ⇒ conditional survival does not depend on age
        assert W.conditional_survival(km, 0, 12) == pytest.approx(W.conditional_survival(km, 12, 12), abs=0.05)

    def test_conditional_survival_s2_clock(self):
        km = W.kaplan_meier([10, 20, 30, 40], [True] * 4)
        # a signal at age 15: alive runs are 20,30,40 → P(alive at 35 | alive at 15) = 1/3
        assert W.conditional_survival(km, 15, 20) == pytest.approx(1 / 3)
        assert W.conditional_survival(km, 45, 5) != W.conditional_survival(km, 45, 5)   # NaN past the data

    def test_nan_and_empty_inputs(self):
        km = W.kaplan_meier([np.nan, 2, -1], [True, True, True])
        assert list(km.t) == [2.0]
        empty = W.kaplan_meier([], [])
        assert empty.empty and math.isnan(W.km_at(empty, 3)) and math.isnan(W.km_median(empty))


class TestStopTruncated:
    def test_stopped_rows_take_minus_k(self):
        m = np.array([0.8, -0.5, 1.5, np.nan])
        mae = np.array([3.5, 0.7, 2.0, 1.0])
        out = W.stop_truncated(m, mae, 2.0)
        assert out[0] == -2.0          # the audit's ghost-edge case: +0.8 at 24h after −3.5
        assert out[1] == -0.5 and out[2] == -2.0 and np.isnan(out[3])

    def test_never_above_endpoint_when_stopped(self):
        rng = np.random.default_rng(5)
        m = rng.normal(0, 2, 1000)
        mae = np.maximum(0, -m) + rng.exponential(1, 1000)
        out = W.stop_truncated(m, mae, 2.0)
        assert (out >= -2.0 - 1e-12).all()
        assert (out[mae < 2.0] == m[mae < 2.0]).all()


class TestBackfillWiring:
    def test_backfill_uses_ohlc_and_path_fallback(self):
        src = (ROOT / "regime_platform/sweep_orchestrator/kairos.py").read_text(encoding="utf-8")
        body = src.split("async def backfill_drift_horizons")[1].split("\ndef ")[0]
        assert "ohlcv_out=ohlc" in body and "path_excursions(" in body
        assert 'summary["path_cols"] = 0' in body            # pre-170 fallback
        assert "mae_48h_pct.is.null" in body

    def test_weekly_check_sections(self):
        src = (ROOT / "scripts/kairos_weekly_check.py").read_text(encoding="utf-8")
        assert "## E2. Path risk" in src and "Kaplan-Meier survival" in src
        assert "Remaining regime clock" in src
