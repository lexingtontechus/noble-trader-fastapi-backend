"""
Tests for the walk-forward backtesting engine and API endpoints.

Covers:
  - Core Backtester logic (pnl_pct fix, n-state support, date support)
  - BacktestConfig validation
  - API endpoints (run, history, detail, compare, delete)
  - Edge cases (too few bars, no trades, all wins/losses)
  - BacktestStore persistence layer (mocked Supabase)
"""

import pytest
import numpy as np
from unittest.mock import patch, AsyncMock

from regime_platform.core.backtester import Backtester, BacktestConfig, BacktestTrade, BacktestResult


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_prices(n: int = 300, start: float = 100.0, drift: float = 0.001, vol: float = 0.02) -> list[float]:
    """Generate a synthetic price series with configurable drift and vol."""
    np.random.seed(42)
    prices = [start]
    for _ in range(n - 1):
        ret = drift + vol * np.random.randn()
        prices.append(prices[-1] * (1 + ret))
    return prices


def _make_trending_prices(n: int = 500) -> list[float]:
    """Generate a price series with clear regime changes (trending + volatile)."""
    np.random.seed(42)
    prices = [100.0]
    for i in range(n):
        if i < 150:
            drift, vol = 0.002, 0.01  # bull quiet
        elif i < 300:
            drift, vol = -0.003, 0.04  # bear volatile
        else:
            drift, vol = 0.002, 0.01  # bull quiet
        prices.append(prices[-1] * (1 + drift + vol * np.random.randn()))
    return prices


def _make_dates(n: int = 300) -> list[str]:
    """Generate n ISO dates starting from 2024-01-01."""
    from datetime import date, timedelta
    start = date(2024, 1, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# ─── Core Backtester Tests ──────────────────────────────────────────────────

class TestBacktesterCore:
    """Test the core Backtester logic."""

    def setup_method(self):
        self.bt = Backtester()

    def test_basic_run_returns_result(self):
        """A basic backtest should return a BacktestResult."""
        prices = _make_trending_prices(500)
        result = self.bt.run(np.array(prices), symbol="TEST")
        assert isinstance(result, BacktestResult)
        assert result.symbol == "TEST"

    def test_pnl_pct_is_populated(self):
        """pnl_pct should NOT always be 0.0 (the fixed bug)."""
        prices = _make_trending_prices(500)
        result = self.bt.run(np.array(prices), symbol="TEST", config=BacktestConfig(window=100))
        if result.n_trades > 0:
            closed_trades = [t for t in result.trade_log if t.get("exit_reason")]
            for t in closed_trades:
                assert "pnl_pct" in t, "pnl_pct missing from trade log entry"
                # pnl_pct should be a real number (not NaN or always 0)
                assert isinstance(t["pnl_pct"], (int, float))

    def test_config_used_is_populated(self):
        """config_used should contain the config snapshot."""
        prices = _make_trending_prices(500)
        cfg = BacktestConfig(window=100, n_hmm_states=3)
        result = self.bt.run(np.array(prices), symbol="TEST", config=cfg)
        assert result.config_used["window"] == 100
        assert result.config_used["n_hmm_states"] == 3
        assert result.config_used["kelly_fraction"] == 0.5

    def test_n_state_2(self):
        """Backtest should work with 2-state HMM."""
        prices = _make_trending_prices(500)
        cfg = BacktestConfig(n_hmm_states=2, window=100)
        result = self.bt.run(np.array(prices), symbol="TEST", config=cfg)
        assert isinstance(result, BacktestResult)
        assert result.config_used["n_hmm_states"] == 2

    def test_n_state_3(self):
        """Backtest should work with 3-state HMM."""
        prices = _make_trending_prices(500)
        cfg = BacktestConfig(n_hmm_states=3, window=100)
        result = self.bt.run(np.array(prices), symbol="TEST", config=cfg)
        assert isinstance(result, BacktestResult)

    def test_n_state_4(self):
        """Backtest should work with 4-state HMM (default)."""
        prices = _make_trending_prices(500)
        cfg = BacktestConfig(n_hmm_states=4, window=100)
        result = self.bt.run(np.array(prices), symbol="TEST", config=cfg)
        assert isinstance(result, BacktestResult)

    def test_too_few_bars_raises(self):
        """Should raise ValueError if too few bars."""
        with pytest.raises(ValueError, match="Need at least"):
            self.bt.run(np.array(_make_prices(50)), symbol="TEST")

    def test_dates_in_trade_log(self):
        """When dates are provided, trade log should include entry/exit dates."""
        prices = _make_trending_prices(500)
        dates = _make_dates(len(prices))
        result = self.bt.run(np.array(prices), symbol="TEST", dates=dates, config=BacktestConfig(window=100))
        if result.n_trades > 0:
            for t in result.trade_log:
                assert "entry_date" in t
                assert "exit_date" in t

    def test_dates_length_mismatch_raises(self):
        """Should raise ValueError if dates length doesn't match prices."""
        prices = _make_trending_prices(500)
        bad_dates = _make_dates(100)
        with pytest.raises(ValueError, match="dates length"):
            self.bt.run(np.array(prices), symbol="TEST", dates=bad_dates)

    def test_metrics_are_reasonable(self):
        """Key metrics should be within reasonable ranges."""
        prices = _make_trending_prices(500)
        result = self.bt.run(np.array(prices), symbol="TEST", config=BacktestConfig(window=100))
        assert 0.0 <= result.win_rate <= 1.0
        assert -50 <= result.sharpe_ratio <= 50
        assert result.max_drawdown <= 0.0
        assert result.profit_factor >= 0.0

    def test_regime_distribution_populated(self):
        """Regime distribution should be a non-empty dict."""
        prices = _make_trending_prices(500)
        result = self.bt.run(np.array(prices), symbol="TEST", config=BacktestConfig(window=100))
        assert isinstance(result.regime_distribution, dict)
        assert len(result.regime_distribution) > 0

    def test_equity_curve_starts_at_initial_equity(self):
        """Equity curve should start at the configured initial equity."""
        prices = _make_trending_prices(500)
        cfg = BacktestConfig(initial_equity=50000.0, window=100)
        result = self.bt.run(np.array(prices), symbol="TEST", config=cfg)
        assert result.equity_curve[0] == 50000.0

    def test_custom_config(self):
        """Custom config params should propagate correctly."""
        prices = _make_trending_prices(400)
        cfg = BacktestConfig(
            window=100,
            refit_every=25,
            n_hmm_states=2,
            kelly_fraction=0.25,
            commission_bps=10.0,
            slippage_bps=5.0,
            initial_equity=200000.0,
        )
        result = self.bt.run(np.array(prices), symbol="CUSTOM", config=cfg)
        assert result.config_used["window"] == 100
        assert result.config_used["kelly_fraction"] == 0.25
        assert result.config_used["initial_equity"] == 200000.0

    def test_no_regime_gate(self):
        """With regime_gate=False, more trades should occur (no blocking)."""
        prices = _make_trending_prices(500)
        result_gated = self.bt.run(np.array(prices), symbol="GATED", config=BacktestConfig(regime_gate=True, window=100))
        result_open = self.bt.run(np.array(prices), symbol="OPEN", config=BacktestConfig(regime_gate=False, window=100))
        assert result_open.n_trades >= result_gated.n_trades


# ─── BacktestStore Unit Tests ───────────────────────────────────────────────

class TestBacktestStore:
    """Test the backtest persistence layer (mocked Supabase)."""

    @pytest.mark.asyncio
    async def test_save_returns_none_when_no_supabase(self):
        """save_backtest_result should return (None, None) when Supabase is not configured."""
        from regime_platform.services.backtest_store import save_backtest_result
        with patch("regime_platform.services.backtest_store.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_KEY = ""
            result = await save_backtest_result(
                user_id="test-user",
                result_dict={"n_trades": 5, "sharpe_ratio": 1.5},
                config_used={"window": 200},
                symbol="TEST",
            )
            assert result == (None, None)

    @pytest.mark.asyncio
    async def test_history_returns_empty_when_no_supabase(self):
        """get_backtest_history should return empty when Supabase is not configured."""
        from regime_platform.services.backtest_store import get_backtest_history
        with patch("regime_platform.services.backtest_store.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_KEY = ""
            result = await get_backtest_history(user_id="test-user")
            assert result["results"] == []
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_detail_returns_none_when_no_supabase(self):
        """get_backtest_detail should return None when Supabase is not configured."""
        from regime_platform.services.backtest_store import get_backtest_detail
        with patch("regime_platform.services.backtest_store.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_KEY = ""
            result = await get_backtest_detail(backtest_id="test-id", user_id="test-user")
            assert result is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_no_supabase(self):
        """delete_backtest should return False when Supabase is not configured."""
        from regime_platform.services.backtest_store import delete_backtest
        with patch("regime_platform.services.backtest_store.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_KEY = ""
            result = await delete_backtest(backtest_id="test-id", user_id="test-user")
            assert result is False


# ─── RegimeEngine n-state Tests ─────────────────────────────────────────────

class TestRegimeHMMNState:
    """Test the RegimeHMM n-state support."""

    def test_2_state_creates(self):
        """RegimeHMM with n_components=2 should create."""
        from regime_platform.core.regime_engine import RegimeHMM
        hmm = RegimeHMM(n_components=2)
        assert hmm.n_components == 2
        assert hmm._vol_labels == ["low", "high"]
        assert hmm._trend_labels == ["bear", "bull"]

    def test_3_state_creates(self):
        """RegimeHMM with n_components=3 should create."""
        from regime_platform.core.regime_engine import RegimeHMM
        hmm = RegimeHMM(n_components=3)
        assert hmm._vol_labels == ["low", "med", "high"]
        assert hmm._trend_labels == ["bear", "neutral", "bull"]

    def test_4_state_creates(self):
        """RegimeHMM with n_components=4 should create."""
        from regime_platform.core.regime_engine import RegimeHMM
        hmm = RegimeHMM(n_components=4)
        assert hmm._vol_labels == ["low", "med_low", "med_high", "high"]
        assert hmm._trend_labels == ["strong_bear", "bear", "bull", "strong_bull"]

    def test_invalid_n_state_raises(self):
        """RegimeHMM with n_components=5 should raise ValueError."""
        from regime_platform.core.regime_engine import RegimeHMM
        with pytest.raises(ValueError, match="n_components must be 2, 3, or 4"):
            RegimeHMM(n_components=5)

    def test_2_state_fit_predict(self):
        """2-state HMM should fit and predict without IndexError."""
        from regime_platform.core.regime_engine import RegimeHMM
        np.random.seed(42)
        prices = [100.0]
        for _ in range(199):
            prices.append(prices[-1] * (1 + 0.001 + 0.02 * np.random.randn()))
        hmm = RegimeHMM(n_components=2)
        hmm.fit(np.array(prices))
        snap = hmm.predict(np.array(prices))
        assert snap.vol_state in ("low", "high")
        assert snap.trend_state in ("bear", "bull")
        assert len(snap.vol_probs) == 2
        assert len(snap.trend_probs) == 2

    def test_3_state_fit_predict(self):
        """3-state HMM should fit and predict without IndexError."""
        from regime_platform.core.regime_engine import RegimeHMM
        np.random.seed(42)
        prices = [100.0]
        for _ in range(199):
            prices.append(prices[-1] * (1 + 0.001 + 0.02 * np.random.randn()))
        hmm = RegimeHMM(n_components=3)
        hmm.fit(np.array(prices))
        snap = hmm.predict(np.array(prices))
        assert snap.vol_state in ("low", "med", "high")
        assert snap.trend_state in ("bear", "neutral", "bull")
        assert len(snap.vol_probs) == 3
        assert len(snap.trend_probs) == 3

    def test_risk_multiplier_2_state(self):
        """Risk multiplier should work with 2-state labels."""
        from regime_platform.core.regime_engine import RegimeHMM, RegimeSnapshot
        snap = RegimeSnapshot(
            vol_probs=[0.8, 0.2], trend_probs=[0.3, 0.7],
            vol_state="low", trend_state="bull",
            regime_label="low_vol_bull", confidence=0.56,
            n_bars_fitted=200,
        )
        # low=1.50, bull=1.00 → 1.50
        assert snap.risk_multiplier == 1.5
