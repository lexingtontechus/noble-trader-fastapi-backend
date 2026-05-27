"""
Tests for the Renko HFT module.

Run with: python -m pytest renko/tests.py -v
Or:       python renko/tests.py          (standalone)
"""

from __future__ import annotations

import time
import math
from typing import List

from renko.brick_engine import RenkoBrickEngine, Brick, BrickDirection
from renko.swing_classifier import SwingClassifier, SwingLabel, SwingType
from renko.pattern_detector import PatternDetector, PatternType, SignalDirection
from renko.signal_filter import SignalFilter
from renko.risk_manager import RiskManager, RiskParams, PositionStatus
from renko.trade_journal import TradeJournal
from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline


# ── Brick Engine Tests ─────────────────────────────────────────────


def test_brick_engine_basic():
    """Test basic brick creation with fixed brick size."""
    engine = RenkoBrickEngine(brick_size=1.0)

    # Seed with first tick
    result = engine.process_tick(100.0)
    assert result == [], "First tick should not create bricks"

    # Move up 1 brick
    result = engine.process_tick(101.0)
    assert len(result) == 1
    assert result[0].direction == BrickDirection.UP
    assert result[0].open_price == 100.0
    assert result[0].close_price == 101.0

    # Move up another brick
    result = engine.process_tick(102.0)
    assert len(result) == 1
    assert result[0].direction == BrickDirection.UP

    # Move down 1 brick (not enough for reversal)
    result = engine.process_tick(101.0)
    assert len(result) == 0, "1 brick down should not create a reversal"

    # Move down 2 bricks total (reversal!)
    result = engine.process_tick(100.0)
    assert len(result) == 1
    assert result[0].direction == BrickDirection.DOWN

    print("  [PASS] test_brick_engine_basic")


def test_brick_engine_gap():
    """Test that a large gap creates multiple bricks."""
    engine = RenkoBrickEngine(brick_size=1.0)
    engine.process_tick(100.0)

    # Gap of 5 bricks
    result = engine.process_tick(105.0)
    assert len(result) == 5, f"Expected 5 bricks, got {len(result)}"
    assert all(b.direction == BrickDirection.UP for b in result)

    print("  [PASS] test_brick_engine_gap")


def test_brick_engine_small_moves():
    """Test that small moves within brick size don't create bricks."""
    engine = RenkoBrickEngine(brick_size=1.0)
    engine.process_tick(100.0)

    # Small moves within brick size
    for price in [100.1, 100.2, 100.5, 100.9, 100.3]:
        result = engine.process_tick(price)
        assert len(result) == 0, f"Price {price} should not create a brick"

    print("  [PASS] test_brick_engine_small_moves")


def test_brick_engine_backtest():
    """Test batch brick building from prices."""
    engine = RenkoBrickEngine(brick_size=0.50)
    prices = [100.0, 100.3, 100.6, 100.9, 101.2, 101.5, 101.0, 100.5, 100.0, 99.5]
    bricks = engine.build_from_prices(prices)
    assert len(bricks) > 0, "Should create at least some bricks"
    assert engine.brick_count == len(bricks)

    print(f"  [PASS] test_brick_engine_backtest ({len(bricks)} bricks from {len(prices)} prices)")


# ── Swing Classifier Tests ─────────────────────────────────────────


def test_swing_classifier_labels():
    """Test HH/HL/LH/LL classification."""
    engine = RenkoBrickEngine(brick_size=1.0)
    classifier = SwingClassifier(swing_lookback=2)

    # Create a series of UP bricks
    engine.process_tick(100.0)
    for i in range(5):
        bricks = engine.process_tick(101.0 + i)
        for brick in bricks:
            classified = classifier.classify(brick)
            # Consecutive UP bricks should be HH
            if classified.brick.index > 0:
                assert classified.label in (SwingLabel.HH, SwingLabel.HL), \
                    f"Expected HH or HL for UP brick, got {classified.label}"

    print("  [PASS] test_swing_classifier_labels")


def test_swing_point_detection():
    """Test that swing points are detected at direction changes."""
    engine = RenkoBrickEngine(brick_size=1.0)
    classifier = SwingClassifier(swing_lookback=2)

    # Create UP then DOWN pattern
    engine.process_tick(100.0)
    for price in [101.0, 102.0, 103.0, 104.0]:  # UP trend
        for brick in engine.process_tick(price):
            classifier.classify(brick)

    # Reversal
    for price in [103.0, 102.0, 101.0]:  # DOWN
        for brick in engine.process_tick(price):
            classifier.classify(brick)

    # Should have at least one swing point
    assert len(classifier.swing_points) > 0, "Should detect at least one swing point"

    print(f"  [PASS] test_swing_point_detection ({len(classifier.swing_points)} swing points)")


# ── Pattern Detector Tests ─────────────────────────────────────────


def test_pattern_bull_run():
    """Test that N consecutive HH/HL bricks trigger a bull signal."""
    engine = RenkoBrickEngine(brick_size=1.0)
    classifier = SwingClassifier(swing_lookback=2)
    detector = PatternDetector(bull_trigger_n=3, bear_trigger_n=3)

    signals = []

    engine.process_tick(100.0)
    for price in [101.0, 102.0, 103.0, 104.0, 105.0]:
        for brick in engine.process_tick(price):
            classified = classifier.classify(brick)
            signal = detector.detect(classified)
            if signal:
                signals.append(signal)

    assert len(signals) > 0, "Should detect at least one bull run signal"
    assert signals[0].direction == SignalDirection.LONG
    assert signals[0].pattern_type == PatternType.BULL_RUN

    print(f"  [PASS] test_pattern_bull_run ({len(signals)} signals)")


def test_pattern_bear_run():
    """Test that N consecutive LL/LH bricks trigger a bear signal."""
    engine = RenkoBrickEngine(brick_size=1.0)
    classifier = SwingClassifier(swing_lookback=2)
    detector = PatternDetector(bull_trigger_n=3, bear_trigger_n=3)

    signals = []

    # Start with some up bricks, then reverse
    engine.process_tick(100.0)
    for price in [101.0, 102.0, 103.0]:
        for brick in engine.process_tick(price):
            classifier.classify(brick)

    # Now go down (reversal + bear run)
    for price in [102.0, 101.0, 100.0, 99.0, 98.0]:
        for brick in engine.process_tick(price):
            classified = classifier.classify(brick)
            signal = detector.detect(classified)
            if signal:
                signals.append(signal)

    bear_signals = [s for s in signals if s.direction == SignalDirection.SHORT]
    assert len(bear_signals) > 0, "Should detect at least one bear signal"

    print(f"  [PASS] test_pattern_bear_run ({len(bear_signals)} bear signals)")


# ── Signal Filter Tests ────────────────────────────────────────────


def test_signal_filter_session():
    """Test that signals outside session window are rejected."""
    filt = SignalFilter(
        session_start="09:30",
        session_end="16:00",
        skip_lunch=False,
        regime_gate=False,
    )

    from renko.pattern_detector import PatternSignal
    from renko.brick_engine import Brick

    # Create a signal at 3 AM (should be rejected)
    signal = PatternSignal(
        pattern_type=PatternType.BULL_RUN,
        direction=SignalDirection.LONG,
        strength=0.8,
        brick_index=5,
        price=100.0,
        timestamp=0,  # epoch = midnight UTC = late night ET
        label_sequence=["HH", "HH", "HH"],
    )

    result = filt.evaluate(signal, regime=None)
    assert result is None, "Signal at 3 AM should be rejected"

    print("  [PASS] test_signal_filter_session")


def test_signal_filter_regime_gate():
    """Test regime gate alignment."""
    from datetime import datetime, timezone

    # Use a Monday 10:00 AM ET timestamp to ensure session window passes
    # Monday May 12, 2025 10:00 AM ET = 14:00 UTC
    test_ts = datetime(2025, 5, 12, 14, 0, 0, tzinfo=timezone.utc).timestamp()

    filt = SignalFilter(
        session_start="00:00",
        session_end="23:59",
        skip_lunch=False,
        regime_gate=True,
        max_trades_per_session=100,
        max_daily_loss_bricks=100,
    )

    from renko.pattern_detector import PatternSignal

    # Long signal in bullish regime = aligned
    signal = PatternSignal(
        pattern_type=PatternType.BULL_RUN,
        direction=SignalDirection.LONG,
        strength=0.8,
        brick_index=5,
        price=100.0,
        timestamp=test_ts,
        label_sequence=["HH", "HH", "HH"],
    )

    result = filt.evaluate(signal, regime="Bullish Trend")
    assert result is not None, "Long in bullish regime should pass"
    assert result.regime_aligned is True

    # Long signal in bearish regime = misaligned
    filt2 = SignalFilter(
        session_start="00:00",
        session_end="23:59",
        skip_lunch=False,
        regime_gate=True,
        max_trades_per_session=100,
        max_daily_loss_bricks=100,
    )

    result = filt2.evaluate(signal, regime="Bearish Trend")
    assert result is None, "Long in bearish regime should be rejected"

    print("  [PASS] test_signal_filter_regime_gate")


# ── Risk Manager Tests ─────────────────────────────────────────────


def test_risk_manager_sl_tp():
    """Test stop-loss and take-profit in brick units."""
    manager = RiskManager(params=RiskParams(sl_bricks=3, tp_bricks=5))

    from renko.brick_engine import Brick

    # Open long position at 100.0 with brick_size=1.0
    manager.open_position(
        entry_price=100.0,
        direction=SignalDirection.LONG,
        brick_size=1.0,
        entry_ts=0.0,
        entry_brick_idx=0,
    )

    assert manager.has_position
    assert manager.active_position.sl_price == 97.0  # 100 - 3
    assert manager.active_position.tp_price == 105.0  # 100 + 5

    # Create bricks moving toward TP
    for i in range(1, 6):
        brick = Brick(
            index=i, direction=BrickDirection.UP,
            open_price=100.0 + i - 1, close_price=100.0 + i,
            high=100.0 + i, low=100.0 + i - 1,
            timestamp=float(i), brick_size=1.0,
        )
        result = manager.evaluate(brick)

    # Should have hit TP at brick 5 (price 105)
    assert not manager.has_position, "Position should be closed after TP"
    assert len(manager.closed_trades) == 1
    assert manager.closed_trades[0].status == PositionStatus.CLOSED_TP
    assert manager.closed_trades[0].pnl_bricks == 5.0

    print("  [PASS] test_risk_manager_sl_tp")


def test_risk_manager_stop_loss():
    """Test that stop-loss triggers correctly."""
    manager = RiskManager(params=RiskParams(sl_bricks=3, tp_bricks=5))

    from renko.brick_engine import Brick

    # Open long position
    manager.open_position(
        entry_price=100.0,
        direction=SignalDirection.LONG,
        brick_size=1.0,
        entry_ts=0.0,
        entry_brick_idx=0,
    )

    # Move price down 3 bricks
    for i in range(1, 4):
        brick = Brick(
            index=i, direction=BrickDirection.DOWN,
            open_price=100.0 - i + 1, close_price=100.0 - i,
            high=100.0 - i + 1, low=100.0 - i,
            timestamp=float(i), brick_size=1.0,
        )
        result = manager.evaluate(brick)

    assert not manager.has_position, "Position should be closed after SL"
    assert manager.closed_trades[0].status == PositionStatus.CLOSED_SL
    assert manager.closed_trades[0].pnl_bricks == -3.0

    print("  [PASS] test_risk_manager_stop_loss")


# ── Trade Journal Tests ────────────────────────────────────────────


def test_trade_journal_stats():
    """Test journal statistics computation."""
    journal = TradeJournal()

    from renko.trade_journal import TradeRecord
    from renko.pattern_detector import PatternType, SignalDirection
    from renko.risk_manager import PositionStatus

    # Add some trades
    for i in range(10):
        record = TradeRecord(
            trade_id=f"R{i:06d}",
            symbol="SPY",
            direction=SignalDirection.LONG,
            pattern_type=PatternType.BULL_RUN,
            entry_price=100.0,
            exit_price=105.0 if i % 3 != 0 else 97.0,
            entry_ts=float(i * 100),
            exit_ts=float(i * 100 + 50),
            pnl_bricks=5.0 if i % 3 != 0 else -3.0,
            pnl_dollars=5.0 * 0.50 if i % 3 != 0 else -3.0 * 0.50,
            status=PositionStatus.CLOSED_TP if i % 3 != 0 else PositionStatus.CLOSED_SL,
            bricks_held=5 if i % 3 != 0 else 3,
            signal_strength=0.7,
            regime="Bullish Trend",
        )
        journal.record(record)

    stats = journal.get_stats()
    assert stats["total_trades"] == 10
    assert 0.0 < stats["win_rate"] < 1.0
    assert stats["by_pattern"]["bull_run"]["count"] == 10
    assert "kelly_inputs" in stats

    print(f"  [PASS] test_trade_journal_stats (WR={stats['win_rate']:.2f})")


# ── Pipeline Integration Test ──────────────────────────────────────


def test_pipeline_basic():
    """Test the full pipeline with a simple price series."""
    config = RenkoConfig(
        brick_size=1.0,
        bull_trigger_n=3,
        bear_trigger_n=3,
        sl_bricks=3,
        tp_bricks=5,
        session_start="00:00",
        session_end="23:59",
        skip_lunch=False,
        regime_gate=False,
        max_trades_per_session=100,
    )
    pipeline = RenkoPipeline(config)

    # Simulate an uptrend
    prices = [100.0]
    for i in range(20):
        prices.append(100.0 + i * 0.5)  # Slow uptrend

    for i, price in enumerate(prices):
        result = pipeline.process_tick(price, ts=float(i))

    state = pipeline.get_state()
    assert state.brick_count > 0, "Should have created some bricks"

    print(f"  [PASS] test_pipeline_basic (bricks={state.brick_count})")


def test_pipeline_backtest():
    """Test pipeline backtest mode."""
    config = RenkoConfig(
        brick_size=0.50,
        bull_trigger_n=2,
        bear_trigger_n=2,
        sl_bricks=3,
        tp_bricks=5,
        session_start="00:00",
        session_end="23:59",
        skip_lunch=False,
        regime_gate=False,
    )
    pipeline = RenkoPipeline(config)

    # Create a price series with trend + reversal
    prices = []
    # Uptrend
    for i in range(50):
        prices.append(100.0 + i * 0.3)
    # Downtrend
    for i in range(50):
        prices.append(115.0 - i * 0.3)

    result = pipeline.backtest(prices)

    assert result["total_bricks"] > 0
    assert result["total_ticks"] == 100
    assert "stats" in result

    print(f"  [PASS] test_pipeline_backtest (bricks={result['total_bricks']}, trades={result['stats']['journal'].get('total_trades', 0)})")


# ── Config Tests ───────────────────────────────────────────────────


def test_config_defaults():
    """Test that config defaults are sensible."""
    config = RenkoConfig()

    assert config.brick_size == 0.50
    assert config.sl_bricks == 3
    assert config.tp_bricks == 5
    assert config.bull_trigger_n == 3

    # Check risk params R:R ratio
    params = RiskParams(sl_bricks=config.sl_bricks, tp_bricks=config.tp_bricks)
    assert params.risk_reward_ratio == 5/3

    print("  [PASS] test_config_defaults")


# ── Run all tests ──────────────────────────────────────────────────


def run_all_tests():
    """Run all test functions."""
    print("=" * 60)
    print("Renko HFT Module - Test Suite")
    print("=" * 60)

    tests = [
        test_brick_engine_basic,
        test_brick_engine_gap,
        test_brick_engine_small_moves,
        test_brick_engine_backtest,
        test_swing_classifier_labels,
        test_swing_point_detection,
        test_pattern_bull_run,
        test_pattern_bear_run,
        test_signal_filter_session,
        test_signal_filter_regime_gate,
        test_risk_manager_sl_tp,
        test_risk_manager_stop_loss,
        test_trade_journal_stats,
        test_pipeline_basic,
        test_pipeline_backtest,
        test_config_defaults,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    run_all_tests()
