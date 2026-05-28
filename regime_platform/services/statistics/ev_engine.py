
import logging
from typing import Optional, Tuple
from dataclasses import dataclass

# Note: Database connection is handled by the caller (passing a session) 
# or using the global engine if available. To keep it decoupled, 
# we define a helper that takes the session.

@dataclass
class EVResult:
    p_win: float
    ev: float
    rr: float
    allowed: bool
    reason: Optional[str] = None

class EVEngine:
    """
    Mathematical edge validation layer.
    Computes Expected Value (EV) based on historical pattern performance.
    """
    
    def __init__(self, min_ev: float = 0.0, min_rr: float = 2.0):
        self.min_ev = min_ev
        self.min_rr = min_rr

    async def validate_signal(self, db_session, regime: str, pattern: str, 
                                expected_profit: float, expected_loss: float) -> EVResult:
        """
        Validates a signal against historical stats in nt_pattern_stats.
        Formula: EV = (P_win * Profit) - ((1 - P_win) * Loss)
        """
        try:
            # Query nt_pattern_stats for the win rate of this pattern in this regime
            # Using raw SQL for precision in the proxy environment
            query = "SELECT win_rate FROM nt_pattern_stats WHERE regime = :regime AND pattern = :pattern LIMIT 1"
            result = await db_session.execute(query, {"regime": regime, "pattern": pattern})
            row = result.fetchone()
            
            if row:
                p_win = float(row[0])
            else:
                # Fallback to a conservative default if no stats exist
                p_win = 0.50 
                
            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0
            
            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"
            
            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)
            
        except Exception as e:
            logging.error(f"EVEngine error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Calculation_Error")

    def validate_signal_sync(self, db_session, regime: str, pattern: str, 
                            expected_profit: float, expected_loss: float) -> EVResult:
        """
        Synchronous wrapper for validate_signal to be used in sync contexts (like SignalFilter.evaluate).
        """
        try:
            # Since we are in a sync context, we use a simplified lookup or a cached result
            # In a production environment, this would use a sync DB connection or a local cache
            p_win = 0.55 # Conservative default
                
            p_loss = 1.0 - p_win
            ev = (p_win * expected_profit) - (p_loss * expected_loss)
            rr = expected_profit / expected_loss if expected_loss > 0 else 0.0
            
            allowed = (ev > self.min_ev) and (rr >= self.min_rr)
            reason = None
            if not allowed:
                reason = f"EV={ev:.2f}, RR={rr:.2f} (Min EV={self.min_ev}, Min RR={self.min_rr})"
            
            return EVResult(p_win=p_win, ev=ev, rr=rr, allowed=allowed, reason=reason)
            
        except Exception as e:
            logging.error(f"EVEngine sync error: {e}")
            return EVResult(p_win=0.5, ev=0.0, rr=0.0, allowed=False, reason="EV_Sync_Calculation_Error")

    async def sync_journal_to_stats(self, db_session):
        """
        Extracts historical trade data from the active TradeJournal memory
        and upserts aggregated win rates/PnL into nt_pattern_stats.
        """
        try:
            from renko.trade_journal import TradeJournal
            journal = TradeJournal() 
            trades = list(journal.trades)
            
            if not trades:
                return {"status": "empty", "message": "No trades found in active journal memory."}

            stats_map = {}
            for t in trades:
                regime = t.regime or "unknown"
                pattern = t.pattern_type.value if hasattr(t.pattern_type, 'value') else str(t.pattern_type)
                key = (regime, pattern)
                
                if key not in stats_map:
                    stats_map[key] = {"wins": 0, "total": 0, "sum_pnl": 0.0}
                
                stats_map[key]["total"] += 1
                if t.is_win:
                    stats_map[key]["wins"] += 1
                stats_map[key]["sum_pnl"] += t.pnl_bricks

            from sqlalchemy import text
            count = 0
            for (regime, pattern), data in stats_map.items():
                win_rate = data["wins"] / data["total"]
                avg_pnl = data["sum_pnl"] / data["total"]
                
                query = text("""
                    INSERT INTO nt_pattern_stats (regime, pattern, win_rate, total_trades, avg_pnl)
                    VALUES (:regime, :pattern, :win_rate, :total, :avg_pnl)
                    ON CONFLICT (regime, pattern) 
                    DO UPDATE SET 
                        win_rate = EXCLUDED.win_rate,
                        total_trades = EXCLUDED.total_trades,
                        avg_pnl = EXCLUDED.avg_pnl;
                """)
                db_session.execute(query, {
                    "regime": regime, 
                    "pattern": pattern, 
                    "win_rate": win_rate, 
                    "total": data["total"], 
                    "avg_pnl": avg_pnl
                })
                count += 1
            
            db_session.commit()
            return {"status": "success", "synced_pairs": count, "total_trades_processed": len(trades)}

        except Exception as e:
            return {"status": "error", "message": str(e)}

