	
import logging
import math
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

# Note: Database connection is handled by the caller (passing a session) 
# or using the global engine if available. To keep it decoupled, 
# we define a helper that takes the session.

# ── Log-odds helpers (v4) ─────────────────────────────────────────
# Log-odds pooling respects probability bounds and prevents
# overconfidence when independent sources agree.

_LOGIT_CLAMP = 1e-15  # Avoid log(0) and division-by-zero


def _logit(p: float) -> float:
    """Logit function: ln(p / (1-p)). Clamped for numerical stability."""
    p = max(_LOGIT_CLAMP, min(1.0 - _LOGIT_CLAMP, p))
    return math.log(p / (1.0 - p))


def _inv_logit(x: float) -> float:
    """Inverse logit (logistic sigmoid): 1 / (1 + exp(-x))."""
    if x > 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ── EV Engine v4 defaults ────────────────────────────────────────────
# 3-source blend when TimesFM is available
# Markov chain is no longer blended into P_win — it is applied as a
# position-sizing multiplier after blending (see compute_markov_multiplier).
P_WIN_WEIGHTS_TIMESFM = {
    "p_regime": 0.40,
    "p_ext_signal": 0.25,
    "p_timesfm": 0.35,
}

# 2-source fallback when TimesFM is unavailable
P_WIN_WEIGHTS_FALLBACK = {
    "p_regime": 0.55,
    "p_ext_signal": 0.45,
}

# Regime shift meta-signal thresholds
TIMESFM_CONFIDENCE_MIN = 0.50  # Min TimesFM confidence to override HMM
CALIBRATION_OVERCONFIDENT_THRESHOLD = 0.05


@dataclass
class EVResult:
    p_win: float
    ev: float
    rr: float
    allowed: bool
    reason: Optional[str] = None


@dataclass
class EVFullResult:
    """Full EV computation result with blended P_win breakdown and aggression dispatch.

    v4: Markov chain moved from P_win blend to position-sizing multiplier.
        ``p_markov`` is returned for reporting/calibration but no longer
        blended into ``p_win``. ``markov_multiplier`` scales ``ev_scale``.
    """
    p_win: float
    p_regime: float
    p_ext_signal: float
    p_markov: float               # Reported for calibration; NOT blended into p_win
    p_timesfm: float = 0.0
    ev: float = 0.0
    ev_per_dollar: float = 0.0
    rr: float = 0.0
    profit: float = 0.0
    stake: float = 0.0
    allowed: bool = False
    aggression: str = "aggressive"
    ev_scale: float = 0.0
    markov_multiplier: float = 1.0  # v4: Markov persistence → sizing multiplier
    reason: Optional[str] = None


class EVEngine:
    """
    Mathematical edge validation layer.
    Computes Expected Value (EV) based on historical pattern performance
    and blended P_win from regime posteriors, external signals,
    **and TimesFM foundation model forecasts (v4).**
    
    v4 Changes:
      - Markov chain moved from P_win blend → sizing multiplier
      - ``compute_ev()`` accepts ``external_signal`` instead of ``imbalance_signal``
      - ``compute_markov_multiplier()`` returns position-sizing factor
      - Weights configurable via ``set_weights()`` at runtime
    """
    
    def __init__(self, min_ev: float = 0.0, min_rr: float = 2.0):
        self.min_ev = min_ev
        self.min_rr = min_rr
        # Mutable weights — can be overridden at runtime
        self._timesfm_weights: Dict[str, float] = dict(P_WIN_WEIGHTS_TIMESFM)
        self._fallback_weights: Dict[str, float] = dict(P_WIN_WEIGHTS_FALLBACK)

    # ── Runtime weight override ─────────────────────────────────────

    def set_weights(self, weights: Dict[str, float]) -> None:
	        """
	        Override the blended P_win weights at runtime.

	        Accepts a dict with any subset of keys:
	        ``p_regime``, ``p_ext_signal``, ``p_timesfm``.
	        Missing keys keep their current values.
	        """
	        for key in ("p_regime", "p_ext_signal", "p_timesfm"):
	            if key in weights:
	                self._timesfm_weights[key] = float(weights[key])
	        # Also update fallback weights for consistency
	        if "p_regime" in self._timesfm_weights:
	            self._fallback_weights["p_regime"] = self._timesfm_weights["p_regime"]
	        if "p_ext_signal" in self._timesfm_weights:
	            self._fallback_weights["p_ext_signal"] = self._timesfm_weights["p_ext_signal"]

    def get_weights(self) -> Dict[str, float]:
        """Return the current active P_win weights."""
        return dict(self._timesfm_weights)

    # ── P_regime from HMM posteriors ────────────────────────────────

    @staticmethod
    def compute_p_regime(trend_probs: Dict[str, float], direction: str) -> float:
        """
        Compute P_regime from HMM trend posteriors.
        
        P_regime(long)  = P(bullish states) + 0.5 * P(neutral)
        P_regime(short) = P(bearish states) + 0.5 * P(neutral)
        
        Handles 2-state, 3-state, and 4-state HMM label variants:
          2: bear, bull
          3: bear, neutral, bull
          4: strong_bear, bear, bull, strong_bull
        """
        if not trend_probs:
            return 0.50
        
        bullish_keys = {"bull", "strong_bull"}
        bearish_keys = {"bear", "strong_bear"}
        neutral_keys = {"neutral"}
        
        p_bullish = sum(v for k, v in trend_probs.items() if k in bullish_keys)
        p_bearish = sum(v for k, v in trend_probs.items() if k in bearish_keys)
        p_neutral = sum(v for k, v in trend_probs.items() if k in neutral_keys)
        
        if direction == "long":
            return p_bullish + 0.5 * p_neutral
        elif direction == "short":
            return p_bearish + 0.5 * p_neutral
        else:
            return 0.50

    # ── P_ext_signal from external directional vote ────────────────

    @staticmethod
    def compute_p_external(external_signal: Optional[Dict], direction: str) -> float:
        """
        Compute P_ext_signal from external directional vote.
        
        Accepts a dict produced by any upstream signal source
        (L2 order book imbalance, sentiment, alternative data, etc.)
        with keys: signal, confidence, persistence.
        
        If aligned with direction: confidence * (0.6 + 0.4 * persistence_bonus)
        If flat: 0.50 (neutral)
        If opposed: 1.0 - confidence
        Default when no external signal: 0.50
        """
        if not external_signal:
            return 0.50
        
        try:
            signal = external_signal.get("signal", "flat")
            confidence = float(external_signal.get("confidence", 0.5))
            persistence = float(external_signal.get("persistence", 0.0))
            
            # Normalize confidence to [0, 1]
            confidence = max(0.0, min(1.0, confidence))
            persistence = max(0.0, min(1.0, persistence))
            
            # Determine alignment
            signal_lower = str(signal).lower()
            
            # Check if signal aligns with direction
            is_bullish_signal = signal_lower in ("buy", "long", "bullish", "bid_heavy", "bid")
            is_bearish_signal = signal_lower in ("sell", "short", "bearish", "ask_heavy", "ask")
            
            if signal_lower == "flat" or (not is_bullish_signal and not is_bearish_signal):
                return 0.50
            
            # Check alignment
            aligned = (
                (direction == "long" and is_bullish_signal) or
                (direction == "short" and is_bearish_signal)
            )
            opposed = (
                (direction == "long" and is_bearish_signal) or
                (direction == "short" and is_bullish_signal)
            )
            
            if aligned:
                persistence_bonus = min(1.0, persistence)
                return confidence * (0.6 + 0.4 * persistence_bonus)
            elif opposed:
                return 1.0 - confidence
            else:
                return 0.50
        except Exception:
            return 0.50

    # ── Markov multiplier for position sizing (v4) ────────────────
    # The Markov chain measures state persistence — how likely the
    # current direction continues. This feeds into position sizing,
    # NOT into P_win (which would double-count directionality with HMM).

    @staticmethod
    def compute_markov_multiplier(
        transition_matrix: Optional[Dict[str, Dict[str, float]]],
        current_state: str,
        direction: str,
    ) -> float:
        """
        Compute a position-sizing multiplier from Markov state persistence.

        Reads T[current_state][target_state] — e.g. T["UP"]["UP"] for long —
        and maps persistence [0.5, 1.0] → sizing multiplier [0.6, 1.0].

        When persistence is weak (< 0.5) the multiplier drops below 0.6,
        reducing position size in low-conviction environments.

        Returns 1.0 (no adjustment) when no Markov data is available.
        """
        if not transition_matrix or not current_state:
            return 1.0

        try:
            target_state = "UP" if direction == "long" else "DOWN"
            state_probs = transition_matrix.get(current_state, {})
            if not state_probs:
                return 1.0

            persistence = float(state_probs.get(target_state, 0.5))
            # Clamp to [0.0, 1.0]
            persistence = max(0.0, min(1.0, persistence))

            # Map persistence:
            #   0.0 → 0.40 (strong reversal signal, reduce by 60%)
            #   0.5 → 0.60 (random walk, slight reduction)
            #   0.75 → 0.80 (typical trending)
            #   1.0 → 1.00 (near-certain persistence, no reduction)
            multiplier = 0.40 + 0.60 * persistence
            return round(multiplier, 4)
        except Exception:
            return 1.0

    # ── Aggression Dispatcher ──────────────────────────────────────

    @staticmethod
    def compute_aggression(ev_per_dollar: float) -> str:
        """Map ev_per_dollar to aggression level."""
        if ev_per_dollar > 0.40:
            return "passive"
        elif ev_per_dollar > 0.15:
            return "mid"
        else:
            return "aggressive"

    @staticmethod
    def compute_ev_scale(ev_per_dollar: float) -> float:
        """Compute EV-scaled Kelly factor."""
        if ev_per_dollar > 0:
            return min(1.0, ev_per_dollar / 0.50)
        return 0.0

    # ── Core: compute_ev (full blended P_win, v4) ─────────────────
    # v4 changes:
    #   - Markov chain moved from P_win blend → sizing multiplier
    #   - p_markov still computed for calibration reporting

    def compute_ev(
        self,
        trend_probs: Optional[Dict[str, float]] = None,
        direction: str = "long",
        external_signal: Optional[Dict] = None,
        transition_matrix: Optional[Dict[str, Dict[str, float]]] = None,
        current_state: str = "FLAT",
        tp_bricks: int = 5,
        sl_bricks: int = 3,
        brick_size: float = 0.50,
        p_timesfm: Optional[float] = None,
    ) -> EVFullResult:
        """
        Full EV computation with blended P_win (v4).

        The Markov chain is no longer blended into P_win — it is applied
	        as a position-sizing multiplier after blending::

	                  _                                 _
	                 |  Σ w_i * logit(p_i)  if TimesFM   |
	            P_win = |_ Σ w_i * logit(p_i)  otherwise _|

	            ev_scale = kelly(P_win) * markov_multiplier

	        Default weights (with TimesFM): ``(0.40, 0.25, 0.35)``
	        Fallback (no TimesFM):          ``(0.55, 0.45)``

	        Weights can be overridden at runtime via ``set_weights()``.
	        Calibration is tracked via ``CalibrationTracker`` (see ``calibration.py``).
        """
        # Step 1: Compute individual P_win sources
        p_regime = self.compute_p_regime(trend_probs or {}, direction)
        p_ext_signal = self.compute_p_external(external_signal, direction)
        p_timesfm_actual = p_timesfm if p_timesfm is not None else 0.0

        # Step 2: Markov multiplier (v4 — not blended into P_win)
        p_markov = 0.5  # neutral default for reporting
        markov_multiplier = self.compute_markov_multiplier(
            transition_matrix, current_state, direction
        )

        # Step 3: Blended P_win — log-odds pooling (v4)
        # Log-odds pooling: logit(P) = Σ w_i * logit(p_i)
        # This respects probability bounds better than linear pooling
        # and prevents overconfidence when sources agree.
        if p_timesfm is not None:
            w = self._timesfm_weights
            logit_sum = (
                w["p_regime"] * _logit(p_regime)
                + w["p_ext_signal"] * _logit(p_ext_signal)
                + w["p_timesfm"] * _logit(p_timesfm)
            )
            p_win = _inv_logit(logit_sum)
        else:
            w = self._fallback_weights
            logit_sum = (
                w["p_regime"] * _logit(p_regime)
                + w["p_ext_signal"] * _logit(p_ext_signal)
            )
            p_win = _inv_logit(logit_sum)

        # Step 4: EV computation
        profit = tp_bricks * brick_size
        stake = sl_bricks * brick_size
        ev = (p_win * profit) - ((1.0 - p_win) * stake)
        ev_per_dollar = ev / stake if stake > 0 else 0.0
        rr = profit / stake if stake > 0 else 0.0

        # Step 5: Aggression dispatch with Markov multiplier
        aggression = self.compute_aggression(ev_per_dollar)
        ev_scale = self.compute_ev_scale(ev_per_dollar)
        ev_scale = round(ev_scale * markov_multiplier, 4)

        # Step 6: Allow/deny gate
        allowed = (ev > self.min_ev) and (rr >= self.min_rr)
        reason = None
        if not allowed:
            reason = (
                f"EV=${ev:.2f}, RR={rr:.2f}, ev/$={ev_per_dollar:.3f} "
                f"(Min EV={self.min_ev}, Min RR={self.min_rr})"
            )

        return EVFullResult(
            p_win=round(p_win, 4),
            p_regime=round(p_regime, 4),
            p_ext_signal=round(p_ext_signal, 4),
            p_markov=round(p_markov, 4),
            p_timesfm=round(p_timesfm_actual, 4),
            ev=round(ev, 4),
            ev_per_dollar=round(ev_per_dollar, 4),
            rr=round(rr, 4),
            profit=round(profit, 4),
            stake=round(stake, 4),
            allowed=allowed,
            aggression=aggression,
            ev_scale=round(ev_scale, 4),
            markov_multiplier=round(markov_multiplier, 4),
            reason=reason,
        )

    # ── Existing: validate_signal (async, DB lookup) ──────────────

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

    # ── Existing: validate_signal_sync (sync, with optional P_regime) ──

    def validate_signal_sync(self, db_session, regime: str, pattern: str, 
                            expected_profit: float, expected_loss: float,
                            trend_probs: Optional[Dict[str, float]] = None,
                            direction: Optional[str] = None) -> EVResult:
        """
        Synchronous wrapper for validate_signal to be used in sync contexts (like SignalFilter.evaluate).
        
        If trend_probs and direction are provided, uses real P_regime from HMM posteriors.
        If not, falls back to p_win=0.55 (backward compatible).
        """
        try:
            if trend_probs is not None and direction is not None:
                # Use real P_regime from HMM posteriors
                p_regime = self.compute_p_regime(trend_probs, direction)
                # Blend with default neutral sources (no imbalance/Markov in sync path)
                p_win = p_regime
            else:
                # Backward compatible: conservative default
                p_win = 0.55
                
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

    # ── v3: Regime Shift Meta-Signal ──────────────────────────────

    @staticmethod
    def _shift_direction(prev_regime: str, current_regime: str) -> str:
        """
        Determine the direction of a regime shift.

        Returns ``"bullish"``, ``"bearish"``, or ``"neutral"`` based on
        the presence of bull/bear labels in the regime strings.
        """
        bull_markers = {"bull", "strong_bull"}
        bear_markers = {"bear", "strong_bear"}

        prev_bull = any(m in prev_regime for m in bull_markers)
        prev_bear = any(m in prev_regime for m in bear_markers)
        curr_bull = any(m in current_regime for m in bull_markers)
        curr_bear = any(m in current_regime for m in bear_markers)

        if not prev_bull and curr_bull:
            return "bullish"
        if not prev_bear and curr_bear:
            return "bearish"
        if prev_bull and not curr_bull:
            return "bearish"
        if prev_bear and not curr_bear:
            return "bullish"
        return "neutral"

    def validate_regime_shift(
        self,
        hmm_regime: str,
        prev_regime: str,
        p_timesfm: Optional[float] = None,
        timesfm_confidence: float = 0.0,
    ) -> tuple:
        """
        Validate an HMM regime shift against the TimesFM forecast.

        When TimesFM disagrees with the HMM's detected shift direction,
        the shift is marked as *unconfirmed* — the caller should
        preserve the previous regime rather than switching.

        Parameters
        ----------
        hmm_regime : str
            Regime label detected by HMM (e.g. ``"high_vol_bear"``).
        prev_regime : str
            The regime before this detection cycle.
        p_timesfm : float or None
            TimesFM forecast probability (0–1). ``None`` = no signal.
        timesfm_confidence : float
            TimesFM model's own confidence in its forecast (0–1).

        Returns
        -------
        (confirmed: bool, reason: str)
            ``(True, "confirmed_...")`` or ``(False, "unconfirmed_...")``
        """
        # No shift detected — always confirmed (nothing to validate)
        if hmm_regime == prev_regime:
            return (True, "no_shift")

        # Without TimesFM signal, trust HMM
        if p_timesfm is None or timesfm_confidence < TIMESFM_CONFIDENCE_MIN:
            return (True, f"trust_hmm_shift_to_{hmm_regime}")

        shift_dir = self._shift_direction(prev_regime, hmm_regime)

        if shift_dir == "bullish" and p_timesfm >= 0.5:
            return (True, f"confirmed_bullish_shift_to_{hmm_regime}")
        elif shift_dir == "bearish" and p_timesfm <= 0.5:
            return (True, f"confirmed_bearish_shift_to_{hmm_regime}")
        elif shift_dir == "neutral":
            # Volatility-only shift (e.g. low_vol → high_vol) — trust HMM
            return (True, f"confirmed_vol_shift_to_{hmm_regime}")
        else:
            # HMM says shift, TimesFM disagrees
            return (
                False,
                f"unconfirmed_shift_hmm={hmm_regime}_timesfm={p_timesfm:.2f}",
            )

    # ── v3: Tail Risk Assessment ──────────────────────────────

    @staticmethod
    def compute_tail_risk(
        p_timesfm: Optional[float],
        regime: str,
        timesfm_confidence: float = 0.0,
    ) -> dict:
        """
        Assess tail risk from extreme TimesFM forecasts.

        When TimesFM is extremely confident in one direction during a
        volatile regime, it may be detecting a tail event (flash crash,
        regime break). Returns a risk score and position multiplier.

        Returns
        -------
        dict with keys: score (0-1), action (str), multiplier (float)
        """
        result = {"score": 0.0, "action": "none", "multiplier": 1.0}

        if p_timesfm is None or timesfm_confidence < 0.5:
            return result

        is_extreme = p_timesfm < 0.2 or p_timesfm > 0.8
        is_high_vol = "high_vol" in regime
        is_extreme_confident = is_extreme and timesfm_confidence >= 0.7

        if is_extreme_confident and is_high_vol:
            result["score"] = 0.85
            result["action"] = "skip"
            result["multiplier"] = 0.0
        elif is_extreme_confident:
            result["score"] = 0.60
            result["action"] = "reduce_50"
            result["multiplier"] = 0.5
        elif is_extreme and is_high_vol:
            result["score"] = 0.35
            result["action"] = "reduce_25"
            result["multiplier"] = 0.75

        return result

    # ── Existing: sync_journal_to_stats ───────────────────────────

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
