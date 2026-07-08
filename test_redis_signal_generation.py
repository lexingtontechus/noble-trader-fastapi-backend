#!/usr/bin/env python3
"""
Test script to simulate the complete signal generation and Redis publishing process.
This demonstrates how signals flow from sweep through Redis to the Signal Aggregator.
"""

import json
import asyncio
import time
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict


@dataclass
class RedisConfig:
    """Represents the Redis config structure used in the sweep function."""
    regime: str
    regime_conf: float
    brick_size: float
    tp_bricks: int
    sl_bricks: int
    markov_p_up: float
    markov_p_dn: float
    kelly_f: float
    last_update: int
    signal: str
    entry_price: float
    stop_loss: float
    take_profit: float
    ev: float
    ev_per_dollar: float
    p_win: float
    p_regime: float
    p_imbalance: float
    p_markov: float
    aggression: str
    ev_scale: float
    effective_kelly: float
    markov_current_state: str
    regime_shift: str
    prev_regime: str
    shift_at: int
    shifts_24h: int
    sweep_sharpe: float
    sweep_trades: int
    sweep_window: str


class RedisSignalPublisher:
    """Simulates the Redis signal publishing process from sweep function."""
    
    def __init__(self):
        self.published_signals = []
        self.redis_pipeline_calls = []
    
    def build_redis_config(self, params: Dict[str, Any]) -> RedisConfig:
        """Simulates the buildRedisConfig function from sweep/index.ts."""
        
        # Calculate derived values
        evResult = params.get('evResult', {
            'ev': 1250.50,
            'ev_per_dollar': 1.25,
            'p_win': 0.65,
            'p_regime': 0.70,
            'p_imbalance': 0.60,
            'p_markov': 0.68,
            'aggression': 'mid',
            'ev_scale': 1.0,
            'effective_kelly': 0.12
        })
        
        return RedisConfig(
            regime=params.get('regime', 'high_vol_bull'),
            regime_conf=params.get('regimeConf', 0.85),
            brick_size=params.get('brickSize', 0.5),
            tp_bricks=params.get('tpBricks', 60),
            sl_bricks=params.get('slBricks', 20),
            markov_p_up=params.get('markovPUp', 0.65),
            markov_p_dn=params.get('markovPDn', 0.35),
            kelly_f=params.get('kellyF', 0.12),
            last_update=int(time.time() * 1000),
            signal=params.get('tradeSignal', {}).get('signal', 'buy'),
            entry_price=params.get('tradeSignal', {}).get('entry_price', 42000.0),
            stop_loss=params.get('tradeSignal', {}).get('stop_loss', 41000.0),
            take_profit=params.get('tradeSignal', {}).get('take_profit', 45000.0),
            ev=evResult['ev'],
            ev_per_dollar=evResult['ev_per_dollar'],
            p_win=evResult['p_win'],
            p_regime=evResult['p_regime'],
            p_imbalance=evResult['p_imbalance'],
            p_markov=evResult['p_markov'],
            aggression=evResult['aggression'],
            ev_scale=evResult['ev_scale'],
            effective_kelly=evResult['effective_kelly'],
            markov_current_state=params.get('evResult', {}).get('markov_current_state', 'UP'),
            regime_shift=params.get('regimeShift', 'false'),
            prev_regime=params.get('prevRegime', ''),
            shift_at=params.get('shiftAt', 0),
            shifts_24h=params.get('shifts24h', 0),
            sweep_sharpe=params.get('sweepSharpe', 8.5),
            sweep_trades=params.get('sweepTrades', 15),
            sweep_window=params.get('sweepWindow', '90d')
        )
    
    def simulate_redis_pipeline(self, commands: list) -> None:
        """Simulates the Redis pipeline calls from sweep function."""
        
        pipeline_call = {
            'timestamp': time.time(),
            'commands': []
        }
        
        for cmd in commands:
            if cmd[0] == 'HSET':
                # Extract key and fields
                key = cmd[1]
                fields = cmd[2:]
                
                # Convert to dict for easier reading
                field_dict = {}
                for i in range(0, len(fields), 2):
                    field_dict[fields[i]] = fields[i+1]
                
                pipeline_call['commands'].append({
                    'type': 'HSET',
                    'key': key,
                    'fields': field_dict
                })
            
            elif cmd[0] == 'PUBLISH':
                # Extract channel and message
                channel = cmd[1]
                message_data = cmd[2]  # Already a dict, no need to parse
                
                pipeline_call['commands'].append({
                    'type': 'PUBLISH',
                    'channel': channel,
                    'message': message_data
                })
        
        self.redis_pipeline_calls.append(pipeline_call)
        print(f"📡 Redis pipeline executed with {len(pipeline_call['commands'])} commands")
    
    def simulate_sweep_publishing(self, symbol: str, regime: str, qualified: bool = True) -> Dict[str, Any]:
        """Simulates the signal publishing process from sweep function."""
        
        # Build parameters (simulating the sweep function logic)
        if qualified:
            params = {
                'regime': 'high_vol_bull',
                'regimeConf': 0.85,
                'brickSize': 0.5,
                'slBricks': 20,
                'tpBricks': 60,
                'kellyF': 0.12,
                'markovPUp': 0.65,
                'markovPDn': 0.35,
                'tradeSignal': {
                    'signal': 'buy',
                    'entry_price': 42000.0,
                    'stop_loss': 41000.0,
                    'take_profit': 45000.0
                },
                'evResult': {
                    'ev': 1250.50,
                    'ev_per_dollar': 1.25,
                    'p_win': 0.65,
                    'p_regime': 0.70,
                    'p_imbalance': 0.60,
                    'p_markov': 0.68,
                    'aggression': 'mid',
                    'ev_scale': 1.0,
                    'effective_kelly': 0.12,
                    'markov_current_state': 'UP'
                },
                'regimeShift': False,
                'prevRegime': '',
                'shiftAt': 0,
                'shifts24h': 0,
                'sweepSharpe': 8.5,
                'sweepTrades': 15,
                'sweepWindow': '90d'
            }
        else:
            params = {
                'regime': 'unknown',
                'regimeConf': 0.0,
                'brickSize': 0.5,
                'slBricks': 20,
                'tpBricks': 60,
                'kellyF': 0.05,
                'markovPUp': 0.5,
                'markovPDn': 0.5,
                'tradeSignal': {
                    'signal': 'neutral',
                    'entry_price': 42000.0,
                    'stop_loss': 42000.0,
                    'take_profit': 42000.0
                },
                'evResult': {
                    'ev': 0.0,
                    'ev_per_dollar': 0.0,
                    'p_win': 0.5,
                    'p_regime': 0.5,
                    'p_imbalance': 0.5,
                    'p_markov': 0.5,
                    'aggression': 'passive',
                    'ev_scale': 0.0,
                    'effective_kelly': 0.0,
                    'markov_current_state': 'FLAT'
                },
                'regimeShift': False,
                'prevRegime': '',
                'shiftAt': 0,
                'shifts24h': 0,
                'sweepSharpe': 0.0,
                'sweepTrades': 0,
                'sweepWindow': 'no_signal'
            }
        
        # Build Redis config
        redis_config = self.build_redis_config(params)
        
        # Create heartbeat payload (simulating sweep function)
        heartbeat_payload = {
            "type": "heartbeat",
            "symbol": symbol,
            "ts": int(time.time() * 1000),
            "regime": redis_config.regime,
            "regime_conf": redis_config.regime_conf,
            "signal": redis_config.signal,
            "entry_price": redis_config.entry_price,
            "stop_loss": redis_config.stop_loss,
            "take_profit": redis_config.take_profit,
            "brick_size": redis_config.brick_size,
            "sl_bricks": redis_config.sl_bricks,
            "tp_bricks": redis_config.tp_bricks,
            "kelly_f": redis_config.kelly_f,
            "ev": redis_config.ev,
            "p_win": redis_config.p_win,
            "aggression": redis_config.aggression,
            "effective_kelly": redis_config.effective_kelly,
            "sweep_sharpe": redis_config.sweep_sharpe,
            "sweep_trades": redis_config.sweep_trades,
            "sweep_window": redis_config.sweep_window,
            "regime_shift": redis_config.regime_shift,
            "prev_regime": redis_config.prev_regime,
            "shift_at": redis_config.shift_at,
            "shifts_24h": redis_config.shifts_24h
        }
        
        # Simulate Redis pipeline (from sweep/index.ts lines 1118-1121)
        commands = [
            ["PUBLISH", f"agent:{symbol}", heartbeat_payload],
            ["PUBLISH", "signal.raw.noble_trader", heartbeat_payload]
        ]
        
        self.simulate_redis_pipeline(commands)
        
        # Store published signal
        published_signal = {
            'symbol': symbol,
            'regime': regime,
            'qualified': qualified,
            'timestamp': time.time(),
            'heartbeat_payload': heartbeat_payload,
            'redis_config': asdict(redis_config)
        }
        
        self.published_signals.append(published_signal)
        
        return published_signal
    
    def simulate_signal_aggregator_processing(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Simulates how the Signal Aggregator processes signals."""
        
        print(f"\n🔄 Signal Aggregator processing signal for {signal['symbol']}:")
        print(f"   Type: {signal['heartbeat_payload']['type']}")
        print(f"   Signal: {signal['heartbeat_payload']['signal']}")
        print(f"   Regime: {signal['heartbeat_payload']['regime']} (conf: {signal['heartbeat_payload']['regime_conf']})")
        
        # Simulate routing decisions
        routing_result = {
            'discord_routed': False,
            'telegram_routed': False,
            'webhook_routed': False,
            'channels': []
        }
        
        # Route to Discord if it's a qualified signal
        if signal['qualified'] and signal['heartbeat_payload']['signal'] != 'neutral':
            routing_result['discord_routed'] = True
            routing_result['channels'].append('Discord')
            print(f"   ✅ Routed to Discord")
        
        # Route to Telegram for PnL alerts and qualified signals
        if signal['heartbeat_payload']['type'] == 'pnl_alert' or (signal['qualified'] and signal['heartbeat_payload']['signal'] != 'neutral'):
            routing_result['telegram_routed'] = True
            routing_result['channels'].append('Telegram')
            print(f"   ✅ Routed to Telegram")
        
        # Route to webhooks if enabled
        if signal['qualified']:
            routing_result['webhook_routed'] = True
            routing_result['channels'].append('Webhook')
            print(f"   ✅ Routed to Webhook")
        
        return routing_result
    
    def run_simulation(self) -> Dict[str, Any]:
        """Run a comprehensive simulation of the signal generation and publishing process."""
        
        print("🔄 Complete Signal Generation Simulation")
        print("=" * 60)
        
        results = {}
        
        # Test 1: Qualified signal generation
        print("\n1. Generating Qualified Signal")
        print("-" * 35)
        
        qualified_signal = self.simulate_sweep_publishing(
            symbol="BTC",
            regime="high_vol_bull",
            qualified=True
        )
        
        print("📊 Qualified Signal Generated:")
        print(json.dumps(qualified_signal['heartbeat_payload'], indent=2))
        
        # Test 2: Rejected signal generation
        print("\n2. Generating Rejected Signal")
        print("-" * 35)
        
        rejected_signal = self.simulate_sweep_publishing(
            symbol="ETH",
            regime="unknown",
            qualified=False
        )
        
        print("❌ Rejected Signal Generated:")
        print(json.dumps(rejected_signal['heartbeat_payload'], indent=2))
        
        # Test 3: Signal aggregator processing
        print("\n3. Signal Aggregator Processing")
        print("-" * 35)
        
        qualified_routing = self.simulate_signal_aggregator_processing(qualified_signal)
        rejected_routing = self.simulate_signal_aggregator_processing(rejected_signal)
        
        # Test 4: Multiple symbols
        print("\n4. Multiple Symbol Processing")
        print("-" * 35)
        
        symbols = ["AAPL", "MSFT", "TSLA", "SPY", "QQQ"]
        for symbol in symbols:
            signal = self.simulate_sweep_publishing(
                symbol=symbol,
                regime="low_vol_bull",
                qualified=True
            )
            routing = self.simulate_signal_aggregator_processing(signal)
        
        # Summary
        print("\n📈 Simulation Summary")
        print("-" * 35)
        print(f"Total signals generated: {len(self.published_signals)}")
        print(f"Qualified signals: {len([s for s in self.published_signals if s['qualified']])}")
        print(f"Rejected signals: {len([s for s in self.published_signals if not s['qualified']])}")
        print(f"Redis pipeline calls: {len(self.redis_pipeline_calls)}")
        print(f"Total notifications routed: {sum(len(r['channels']) for r in [qualified_routing, rejected_routing])}")
        
        results = {
            'published_signals': self.published_signals,
            'redis_pipeline_calls': self.redis_pipeline_calls,
            'routing_results': {
                'qualified': qualified_routing,
                'rejected': rejected_routing
            }
        }
        
        return results


def main():
    """Run the complete signal generation simulation."""
    publisher = RedisSignalPublisher()
    results = publisher.run_simulation()
    
    # Write detailed results to file
    with open("redis_signal_generation_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n📄 Detailed results saved to: redis_signal_generation_results.json")
    
    print("\n🎯 Key Insights:")
    print("1. Signals are published to Redis channels 'agent:{symbol}' and 'signal.raw.noble_trader'")
    print("2. The Signal Aggregator subscribes to 'signal.raw.noble_trader' and routes signals")
    print("3. Only qualified signals (passing all filters) are routed to notification channels")
    print("4. Rejected signals still get published to Redis but aren't routed to notifications")
    print("5. Each signal includes comprehensive metadata for risk management and analysis")


if __name__ == "__main__":
    main()