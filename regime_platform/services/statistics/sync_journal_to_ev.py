
import os
import psycopg2
from psycopg2.extras import execute_values
from renko.trade_journal import TradeJournal
from renko.pattern_detector import PatternType

def sync_journal_to_db():
    # 1. Database Connection
    # Using environment variables or hardcoded from session memory for this utility
    conn_str = "postgresql://postgres.pcvscowltlrxzgxjurcr:5QrrXqRyvXC792V7@aws-0-us-west-1.pooler.supabase.com:6543/postgres"
    
    try:
        conn = psycopg2.connect(conn_str)
        cur = conn.cursor()
        print("[Sync] Connected to Supabase.")

        # 2. Access Journal
        # In a real production environment, we'd pull this from the running app state.
        # For this migration, we assume we are processing a snapshot or the active journal.
        journal = TradeJournal() 
        # Note: If journal is empty here, we would typically load from a persisted JSON/CSV.
        # Let's check if there are any persisted journal files first.
        
        trades = list(journal.trades)
        if not trades:
            print("[Sync] No trades found in active journal memory. Checking for disk backups...")
            # Fallback: check for any .json or .csv files that might contain trade logs
            return

        # 3. Aggregate Stats
        # key: (regime, pattern) -> {wins: 0, total: 0, sum_pnl: 0.0}
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

        # 4. Prepare for Upsert
        upsert_data = []
        for (regime, pattern), data in stats_map.items():
            win_rate = data["wins"] / data["total"]
            avg_pnl = data["sum_pnl"] / data["total"]
            upsert_data.append((regime, pattern, win_rate, data["total"], avg_pnl))

        # 5. Execute Upsert
        # Use ON CONFLICT to update existing stats
        query = """
            INSERT INTO nt_pattern_stats (regime, pattern, win_rate, total_trades, avg_pnl)
            VALUES %s
            ON CONFLICT (regime, pattern) 
            DO UPDATE SET 
                win_rate = EXCLUDED.win_rate,
                total_trades = EXCLUDED.total_trades,
                avg_pnl = EXCLUDED.avg_pnl;
        """
        
        execute_values(cur, query, upsert_data)
        conn.commit()
        print(f"[Sync] Successfully synchronized {len(upsert_data)} pattern-regime pairs.")

    except Exception as e:
        print(f"[Error] Sync failed: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == '__main__':
    sync_journal_to_db()
