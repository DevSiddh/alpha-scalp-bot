"""Alpha-Scalp Bot – ShadowTracker.

GP-S6: Runs "ghost trades" in parallel for every sub-strategy on each
candle signal.  Uses Beta distributions to model each strategy's win
probability, enabling Thompson Sampling in Step 7 (TournamentEngine).

Critical fixes applied:
  FIX-1: ShadowTracker div-by-zero guard
          if pnl_max == pnl_min: reward = 0.0
  FIX-3: Shadow PnL must deduct taker fees on both legs
          fee = (entry_price × size × 0.001) + (exit_price × size × 0.001)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from loguru import logger


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ShadowTrade:
    """One open ghost position."""
    ghost_id: str
    strategy_name: str
    entry_price: float
    side: str        # "BUY" | "SELL"
    size: float
    entry_time: float
    open: bool = True


@dataclass
class StrategyBeta:
    """Beta(alpha, beta_param) distribution — models win probability for one strategy.

    Prior: Beta(1, 1) = uniform over [0, 1].
    Each closed ghost trade updates the distribution via the reward signal.
    """
    alpha: float = 1.0       # successes (weighted by reward)
    beta_param: float = 1.0  # failures   (weighted by 1 - reward)
    total_trades: int = 0
    total_wins: int = 0       # trades where reward >= 0.5

    def sample(self) -> float:
        """Thompson sampling — draw one sample from Beta(alpha, beta_param)."""
        return float(np.random.beta(self.alpha, self.beta_param))

    def update(self, reward: float) -> None:
        """Update distribution with reward ∈ [0, 1].

        reward = 1.0 → clear win, reward = 0.0 → clear loss.
        Fractional rewards encode how good the win (or bad the loss) was.
        """
        reward = max(0.0, min(1.0, reward))  # clamp to valid range
        self.alpha += reward
        self.beta_param += (1.0 - reward)
        self.total_trades += 1
        if reward >= 0.5:
            self.total_wins += 1

    def mean(self) -> float:
        """Posterior mean win probability."""
        return self.alpha / (self.alpha + self.beta_param)

    def std(self) -> float:
        """Posterior standard deviation."""
        a, b = self.alpha, self.beta_param
        return float(((a * b) / ((a + b) ** 2 * (a + b + 1))) ** 0.5)


# ---------------------------------------------------------------------------
# ShadowTracker
# ---------------------------------------------------------------------------

class ShadowTracker:
    """Tracks ghost trades for multiple sub-strategies concurrently.

    Usage
    -----
    ghost_id = tracker.open_ghost("Breakout", entry_price=50000, side="BUY")
    ...
    result   = tracker.close_ghost(ghost_id, exit_price=50300)
    # result = {"ghost_id": ..., "strategy": ..., "pnl": ..., "reward": ..., "won": ...}
    samples  = tracker.thompson_sample()   # dict[strategy_name, float]
    """

    FEE_RATE: float = 0.001  # FIX-3: Binance taker fee per side (0.1%)

    def __init__(self) -> None:
        self._open: dict[str, ShadowTrade] = {}
        self._betas: dict[str, StrategyBeta] = {}
        self._pnl_history: dict[str, list[float]] = {}  # used for min-max normalisation

    # ── Ghost lifecycle ─────────────────────────────────────────────────────

    def open_ghost(
        self,
        strategy_name: str,
        entry_price: float,
        side: str,
        size: float = 1.0,
    ) -> str:
        """Open a ghost trade. Returns ghost_id."""
        ghost_id = str(uuid.uuid4())[:8]
        self._open[ghost_id] = ShadowTrade(
            ghost_id=ghost_id,
            strategy_name=strategy_name,
            entry_price=entry_price,
            side=side.upper(),
            size=size,
            entry_time=time.time(),
        )
        if strategy_name not in self._betas:
            self._betas[strategy_name] = StrategyBeta()
        if strategy_name not in self._pnl_history:
            self._pnl_history[strategy_name] = []

        logger.debug(
            "ShadowTrade opened | id={} strategy={} side={} entry={:.2f}",
            ghost_id, strategy_name, side.upper(), entry_price,
        )
        return ghost_id

    def close_ghost(self, ghost_id: str, exit_price: float) -> dict[str, Any]:
        """Close a ghost trade and update the strategy's Beta distribution.

        Returns a result dict with pnl, reward, and outcome.
        """
        trade = self._open.pop(ghost_id, None)
        if trade is None:
            logger.warning("ShadowTracker: ghost_id {} not found", ghost_id)
            return {"error": "ghost_not_found", "ghost_id": ghost_id}

        pnl = self._compute_shadow_pnl(
            trade.entry_price, exit_price, trade.side, trade.size,
        )

        # Append PnL to history before normalising (so this trade is included)
        history = self._pnl_history[trade.strategy_name]
        history.append(pnl)

        reward = self._pnl_to_reward(pnl, trade.strategy_name)
        self._betas[trade.strategy_name].update(reward)

        result = {
            "ghost_id": ghost_id,
            "strategy": trade.strategy_name,
            "side": trade.side,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "pnl": round(pnl, 6),
            "reward": round(reward, 4),
            "won": pnl > 0,
        }
        logger.debug(
            "ShadowTrade closed | id={} strategy={} pnl={:+.4f} reward={:.3f}",
            ghost_id, trade.strategy_name, pnl, reward,
        )
        return result

    # ── Private helpers ─────────────────────────────────────────────────────

    def _compute_shadow_pnl(
        self,
        entry: float,
        exit_price: float,
        side: str,
        size: float,
    ) -> float:
        """FIX-3: Include taker fees on both entry and exit legs."""
        entry_fee = entry * size * self.FEE_RATE
        exit_fee  = exit_price * size * self.FEE_RATE
        if side == "BUY":
            return (exit_price - entry) * size - entry_fee - exit_fee
        else:
            return (entry - exit_price) * size - entry_fee - exit_fee

    def _pnl_to_reward(self, pnl: float, strategy_name: str) -> float:
        """Normalise PnL to [0, 1] reward using min-max over strategy history.

        FIX-1: if pnl_max == pnl_min (all trades identical), return 0.0
               to avoid ZeroDivisionError.
        """
        history = self._pnl_history[strategy_name]
        if len(history) < 2:
            return 1.0 if pnl > 0 else 0.0

        pnl_min = min(history)
        pnl_max = max(history)

        if pnl_max == pnl_min:   # FIX-1
            return 0.0

        reward = (pnl - pnl_min) / (pnl_max - pnl_min)
        return max(0.0, min(1.0, reward))

    # ── Thompson sampling ───────────────────────────────────────────────────

    def thompson_sample(self) -> dict[str, float]:
        """Draw one sample from each strategy's Beta distribution.

        Higher sample → strategy currently "thinks" it has a better edge.
        Used by TournamentEngine (Step 7) to select the active strategy.
        """
        return {name: beta.sample() for name, beta in self._betas.items()}

    # ── Introspection ───────────────────────────────────────────────────────

    def get_beta(self, strategy_name: str) -> StrategyBeta | None:
        """Return StrategyBeta for *strategy_name*, or None if unseen."""
        return self._betas.get(strategy_name)

    def open_count(self) -> int:
        """Number of currently open ghost trades."""
        return len(self._open)

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """Summary stats for every strategy seen so far."""
        return {
            name: {
                "alpha":          round(b.alpha, 4),
                "beta":           round(b.beta_param, 4),
                "mean_win_prob":  round(b.mean(), 4),
                "std":            round(b.std(), 4),
                "total_trades":   b.total_trades,
                "total_wins":     b.total_wins,
                "pnl_observations": len(self._pnl_history.get(name, [])),
            }
            for name, b in self._betas.items()
        }
