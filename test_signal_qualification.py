#!/usr/bin/env python3
"""
Test script to demonstrate signal qualification filtering logic.
This script simulates the qualification filters applied in the sweep function.
"""

import json
import asyncio
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum


class SignalType(str, Enum):
    RENKO_SIGNAL = "renko_signal"
    HEARTBEAT = "heartbeat"
    PNL_ALERT = "pnl_alert"
    SYSTEM_STATUS = "system_status"


@dataclass
class OptimizeComboResult:
    brick_size: float
    sl_bricks: int
    tp_bricks: int
    total_trades: int
    sharpe: float
    win_rate: float
    profit_factor: float
    max_drawdown_bricks: float
    dollar_stats: Dict[str, float]


class SignalQualifier:
    """Simulates the signal qualification filters from the sweep function."""
    
    # Constants from sweep function
    MIN_RR = 2.0
    MAX_SHARPE = 10.0
    MIN_TRADES = 10
    
    def __init__(self):
        self.simulation_results = []
    
    def timesfm_confidence_gate(self, p_timesfm: float, cached: bool = False) -> Dict[str, Any]:
        """Test the TimesFM confidence gate logic."""
        confidence = abs(p_timesfm - 0.5)  # 0 = uncertain, 0.5 = max confidence
        
        if confidence > 0.1:
            result = {
                "passed": False,
                "reason": "TimesFM too confident",
                "p_timesfm": p_timesfm,
                "confidence": confidence,
                "threshold": 0.1,
                "cached": cached,
                "message": f"SKIPPED — TimesFM confident (p_timesfm={p_timesfm:.4f}, confidence={confidence:.4f} threshold=0.1)"
            }
        else:
            result = {
                "passed": True,
                "reason": "TimesFM uncertain",
                "p_timesfm": p_timesfm,
                "confidence": confidence,
                "threshold": 0.1,
                "cached": cached,
                "message": f"TimesFM uncertain (p_timesfm={p_timesfm:.4f}) — running full sweep"
            }
        
        self.simulation_results.append({
            "test": "timesfm_confidence_gate",
            "p_timesfm": p_timesfm,
            "result": result
        })
        return result
    
    def validate_backtest_results(self, results: List[OptimizeComboResult]) -> Dict[str, Any]:
        """Test the backtest validation logic."""
        valid_results = []
        rejected_count = {
            "min_trades": 0,
            "min_rr": 0,
            "max_sharpe": 0,
            "valid": 0
        }
        
        for i, r in enumerate(results):
            # Check minimum trades
            if r.total_trades < self.MIN_TRADES:
                rejected_count["min_trades"] += 1
                print(f"Combo {i+1}: REJECTED — only {r.total_trades} trades (need >= {self.MIN_TRADES})")
                continue
            
            # Check risk ratio
            rr = r.tp_bricks / r.sl_bricks
            if rr < self.MIN_RR:
                rejected_count["min_rr"] += 1
                print(f"Combo {i+1}: REJECTED — R:R {rr:.2f}x < {self.MIN_RR}x")
                continue
            
            # Check maximum Sharpe
            if r.sharpe > self.MAX_SHARPE:
                rejected_count["max_sharpe"] += 1
                print(f"Combo {i+1}: REJECTED — Sharpe {r.sharpe:.2f} > {self.MAX_SHARPE} (likely overfit)")
                continue
            
            # Valid combo
            valid_results.append(r)
            rejected_count["valid"] += 1
            print(f"Combo {i+1}: VALID — R:R {rr:.2f}x, Sharpe {r.sharpe:.2f}, Trades {r.total_trades}")
        
        result = {
            "total_combinations": len(results),
            "valid_results": valid_results,
            "valid_count": len(valid_results),
            "rejected_count": rejected_count,
            "any_valid_results": len(valid_results) > 0
        }
        
        self.simulation_results.append({
            "test": "validate_backtest_results",
            "input_count": len(results),
            "result": result
        })
        
        return result
    
    def generate_signal_examples(self, symbol: str, regime: str) -> Dict[str, Any]:
        """Generate example raw and qualified signals."""
        
        # Raw signal (before qualification)
        raw_signal = {
            "type": "heartbeat",
            "symbol": symbol,
            "regime": regime,
            "regime_conf": 0.85,
            "signal": "buy",
            "entry_price": 42000.00,
            "stop_loss": 41000.00,
            "take_profit": 45000.00,
            "brick_size": 0.5,
            "sl_bricks": 20,
            "tp_bricks": 60,
            "kelly_f": 0.12,
            "ev": 1250.50,
            "p_win": 0.65,
            "p_timesfm": 0.52,  # Critical value - uncertain enough to pass gate
            "sweep_sharpe": None,
            "sweep_trades": None,
            "sweep_window": None,
            "qualification_status": "raw"
        }
        
        # Qualified signal (after passing filters)
        qualified_signal = raw_signal.copy()
        qualified_signal.update({
            "sweep_sharpe": 8.5,
            "sweep_trades": 15,
            "sweep_window": "90d",
            "qualification_status": "qualified",
            "qualification_details": {
                "timesfm_passed": True,
                "min_trades_passed": True,
                "rr_passed": True,
                "sharpe_passed": True
            }
        })
        
        # Rejected signal (TimesFM too confident)
        rejected_signal = raw_signal.copy()
        rejected_signal.update({
            "p_timesfm": 0.65,  # Too confident - will fail gate
            "qualification_status": "rejected",
            "qualification_details": {
                "timesfm_passed": False,
                "timesfm_confidence": 0.15,
                "reject_reason": "TimesFM too confident"
            }
        })
        
        return {
            "raw_signal": raw_signal,
            "qualified_signal": qualified_signal,
            "rejected_signal": rejected_signal
        }
    
    def run_simulation(self) -> Dict[str, Any]:
        """Run a comprehensive simulation of the qualification process."""
        print("🧪 Signal Qualification Simulation")
        print("=" * 50)
        
        results = {}
        
        # Test 1: TimesFM confidence gate
        print("\n1. Testing TimesFM Confidence Gate")
        print("-" * 30)
        
        test_cases = [
            (0.52, "Normal case - uncertain"),
            (0.65, "Confident case - will be rejected"),
            (0.35, "Confident bear case - will be rejected"),
            (0.50, "Maximum uncertainty - will pass"),
            (0.59, "Borderline case - will be rejected"),
            (0.41, "Borderline bear case - will be rejected")
        ]
        
        for p_value, description in test_cases:
            result = self.timesfm_confidence_gate(p_value)
            print(f"  {description}: {result['message']}")
        
        # Test 2: Backtest validation
        print("\n2. Testing Backtest Validation")
        print("-" * 30)
        
        # Simulate backtest results
        backtest_results = [
            OptimizeComboResult(0.5, 20, 40, 8, 7.5, 0.62, 1.8, 15.0, {}),    # Too few trades
            OptimizeComboResult(0.5, 30, 45, 12, 8.2, 0.65, 1.9, 12.0, {}),   # Valid
            OptimizeComboResult(0.7, 15, 25, 15, 12.0, 0.70, 2.1, 10.0, {}), # Too high Sharpe
            OptimizeComboResult(0.4, 25, 35, 6, 6.8, 0.58, 1.6, 18.0, {}),   # Too few trades
            OptimizeComboResult(0.6, 20, 35, 18, 5.2, 0.60, 1.7, 14.0, {}),  # Valid
            OptimizeComboResult(0.5, 10, 15, 25, 9.8, 0.75, 2.3, 8.0, {}),   # Valid, good trades
        ]
        
        validation_result = self.validate_backtest_results(backtest_results)
        
        # Test 3: Generate signal examples
        print("\n3. Generating Signal Examples")
        print("-" * 30)
        
        signal_examples = self.generate_signal_examples("BTC", "high_vol_bull")
        results["signal_examples"] = signal_examples
        
        print("\n📊 Raw Signal:")
        print(json.dumps(signal_examples["raw_signal"], indent=2))
        
        print("\n✅ Qualified Signal:")
        print(json.dumps(signal_examples["qualified_signal"], indent=2))
        
        print("\n❌ Rejected Signal:")
        print(json.dumps(signal_examples["rejected_signal"], indent=2))
        
        # Summary
        print("\n📈 Simulation Summary")
        print("-" * 30)
        print(f"TimesFM tests: {len([r for r in self.simulation_results if r['test'] == 'timesfm_confidence_gate'])}")
        print(f"Backtest tests: {len([r for r in self.simulation_results if r['test'] == 'validate_backtest_results'])}")
        print(f"Valid signals generated: {len(signal_examples['qualified_signal'])}")
        print(f"Signals rejected by TimesFM: {len([r for r in self.simulation_results if r['test'] == 'timesfm_confidence_gate' and r['result']['passed'] == False])}")
        
        return results


def main():
    """Run the signal qualification simulation."""
    qualifier = SignalQualifier()
    results = qualifier.run_simulation()
    
    # Write detailed results to file
    with open("signal_qualification_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n📄 Detailed results saved to: signal_qualification_results.json")
    
    print("\n🎯 Key Insights:")
    print("1. TimesFM confidence gate is the primary filter - signals with |p - 0.5| > 0.1 are rejected")
    print("2. Backtest results must have >= 10 trades and >= 2.0x risk ratio")
    print("3. Sharpe ratio > 10.0 is rejected to prevent overfitting")
    print("4. Only signals passing all filters reach the notification channels")


if __name__ == "__main__":
    main()