"""
Renko HFT Trading Module for Noble Trader Agent.

Pipeline:
    Renko Brick Stream -> Swing Classifier -> Pattern Detector ->
    Signal Filter -> Order Executor -> Risk Manager

Integrates with existing HMM regime detection, Kelly sizing,
and risk analysis modules.
"""

from renko.brick_engine import RenkoBrickEngine, Brick, BrickDirection
from renko.swing_classifier import SwingClassifier, SwingLabel, SwingPoint
from renko.pattern_detector import PatternDetector, PatternSignal
from renko.signal_filter import SignalFilter, FilteredSignal
from renko.risk_manager import RiskManager, RiskParams, TradeResult
from renko.executor import AlpacaExecutor, OrderParams
from renko.trade_journal import TradeJournal, TradeRecord
from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline

__all__ = [
    "RenkoBrickEngine",
    "Brick",
    "BrickDirection",
    "SwingClassifier",
    "SwingLabel",
    "SwingPoint",
    "PatternDetector",
    "PatternSignal",
    "SignalFilter",
    "FilteredSignal",
    "RiskManager",
    "RiskParams",
    "TradeResult",
    "AlpacaExecutor",
    "OrderParams",
    "TradeJournal",
    "TradeRecord",
    "RenkoConfig",
    "RenkoPipeline",
]

__version__ = "1.0.0"
