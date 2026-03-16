"""Alpha-Scalp Bot – StrategyRouter.

GP-S8: Manages strategy lifecycle (active → benched → promoted).

Burn-in gate (FIX-2)
---------------------
A strategy cannot be routed to until BOTH conditions are true:
  1. candles_seen >= BURN_IN_CANDLES (50)
  2. distinct_regimes_seen >= BURN_IN_MIN_REGIMES (2)
Candle count alone is NOT sufficient.

Velocity check
--------------
Win-rate is snapshotted every VELOCITY_WINDOW (10) trades.
If the change between the last two snapshots is below
−VELOCITY_CHECK_THRESHOLD (−0.30), the strategy is benched
immediately (rapid win-rate collapse).

Correlation check
-----------------
If two active strategies' recent PnL sequences have Pearson ρ ≥
CORRELATION_BENCH_THRESHOLD (0.85), the one with the lower
mean win-probability (from ShadowTracker Beta) is benched.
Requires at least CORRELATION_MIN_SAMPLES (20) observations each.

Telegram alerts
---------------
Bench and promote events fire a Telegram notification via
TelegramAlerts (fire-and-forget via asyncio).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from shadow_tracker import ShadowTracker
    from telegram_alerts import TelegramAlerts


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BURN_IN_CANDLES: int = 50
BURN_IN_MIN_REGIMES: int = 2
VELOCITY_CHECK_THRESHOLD: float = 0.30   # win-rate drop > this → bench
VELOCITY_WINDOW: int = 10                # trades per velocity snapshot
CORRELATION_BENCH_THRESHOLD: float = 0.85
CORRELATION_MIN_SAMPLES: int = 20


# ---------------------------------------------------------------------------
# Per-strategy state
# ---------------------------------------------------------------------------

@dataclass
class StrategyState:
    """Live tracking state for one sub-strategy."""
    name: str
    active: bool = True
    benched: bool = False
    bench_reason: str = ""

    # Burn-in tracking
    candles_seen: int = 0
    regimes_seen: set[str] = field(default_factory=set)

    # Velocity tracking
    win_rate_snapshots: list[float] = field(default_factory=list)
    trades_since_snapshot: int = 0

    # Recent PnL for correlation check
    recent_pnl: list[float] = field(default_factory=list)

    # ── Burn-in ─────────────────────────────────────────────────────────────

    def burn_in_complete(self) -> bool:
        """FIX-2: both candle count AND regime diversity required."""
        return (
            self.candles_seen >= BURN_IN_CANDLES
            and len(self.regimes_seen) >= BURN_IN_MIN_REGIMES
        )

    @property
    def distinct_regimes(self) -> int:
        return len(self.regimes_seen)

    # ── Record events ────────────────────────────────────────────────────────

    def record_candle(self, regime: str) -> None:
        self.candles_seen += 1
        self.regimes_seen.add(regime)

    def record_trade(self, pnl: float) -> None:
        self.recent_pnl.append(pnl)
        self.trades_since_snapshot += 1
        if self.trades_since_snapshot >= VELOCITY_WINDOW:
            window = self.recent_pnl[-VELOCITY_WINDOW:]
            win_rate = sum(1 for p in window if p > 0) / len(window)
            self.win_rate_snapshots.append(win_rate)
            self.trades_since_snapshot = 0

    # ── Velocity ─────────────────────────────────────────────────────────────

    def velocity(self) -> float | None:
        """Win-rate change between last two snapshots.
        Returns None if fewer than 2 snapshots available.
        """
        if len(self.win_rate_snapshots) < 2:
            return None
        return self.win_rate_snapshots[-1] - self.win_rate_snapshots[-2]


# ---------------------------------------------------------------------------
# StrategyRouter
# ---------------------------------------------------------------------------

class StrategyRouter:
    """Routes candles to active strategies; promotes/benches based on evidence.

    Parameters
    ----------
    shadow_tracker:
        ShadowTracker instance for win-probability lookups.
    telegram:
        Optional TelegramAlerts instance.  Pass None to disable alerts.
    strategy_names:
        Strategies to manage.  Defaults to the standard 5 sub-strategies.
    """

    BURN_IN_CANDLES: int = BURN_IN_CANDLES
    BURN_IN_MIN_REGIMES: int = BURN_IN_MIN_REGIMES
    VELOCITY_CHECK_THRESHOLD: float = VELOCITY_CHECK_THRESHOLD
    CORRELATION_BENCH_THRESHOLD: float = CORRELATION_BENCH_THRESHOLD

    DEFAULT_STRATEGIES: tuple[str, ...] = (
        "LiquiditySweepReversal",
        "Breakout",
        "TrendPullback",
        "VWAP_MeanReversion",
        "OrderFlowMomentum",
    )

    def __init__(
        self,
        shadow_tracker: "ShadowTracker",
        telegram: "TelegramAlerts | None" = None,
        strategy_names: tuple[str, ...] | None = None,
    ) -> None:
        self.shadow = shadow_tracker
        self.telegram = telegram
        names = strategy_names or self.DEFAULT_STRATEGIES
        self._states: dict[str, StrategyState] = {
            n: StrategyState(name=n) for n in names
        }

    # ── Main tick ────────────────────────────────────────────────────────────

    def tick(self, regime: str, tournament_winner: str) -> str:
        """Advance all state counters; apply routing and guard logic.

        Parameters
        ----------
        regime:
            Current market regime (e.g. "TRENDING_UP", "RANGING").
        tournament_winner:
            Strategy name chosen by TournamentEngine this candle.

        Returns
        -------
        Routed strategy name, or "Cash" if the winner is blocked.
        """
        # Advance candle counters for all un-benched strategies
        for state in self._states.values():
            if not state.benched:
                state.record_candle(regime)

        # Unknown strategies (e.g. "Cash") pass through unchanged
        if tournament_winner not in self._states:
            return tournament_winner

        state = self._states[tournament_winner]

        # FIX-2: burn-in gate
        if not state.burn_in_complete():
            logger.debug(
                "StrategyRouter: burn-in incomplete for {} "
                "| candles={}/{} regimes={}/{}",
                tournament_winner,
                state.candles_seen, BURN_IN_CANDLES,
                state.distinct_regimes, BURN_IN_MIN_REGIMES,
            )
            return "Cash"

        # Benched gate
        if state.benched:
            logger.debug(
                "StrategyRouter: {} is benched ({}), routing Cash",
                tournament_winner, state.bench_reason,
            )
            return "Cash"

        # Velocity check
        self._check_velocity(state)
        if state.benched:
            return "Cash"

        # Correlation check across all active pairs
        self._check_correlations()
        if state.benched:
            return "Cash"

        return tournament_winner

    # ── Trade feedback ───────────────────────────────────────────────────────

    def record_trade_result(self, strategy_name: str, pnl: float) -> None:
        """Feed a completed trade back into the router for velocity tracking."""
        state = self._states.get(strategy_name)
        if state:
            state.record_trade(pnl)

    # ── Bench / promote ──────────────────────────────────────────────────────

    def bench(self, strategy_name: str, reason: str) -> None:
        """Bench a strategy; it will not be routed to until promoted."""
        state = self._states.get(strategy_name)
        if state and not state.benched:
            state.benched = True
            state.active = False
            state.bench_reason = reason
            logger.warning(
                "StrategyRouter: BENCHED {} | reason={}", strategy_name, reason,
            )
            self._send_alert(
                f"🚫 Strategy BENCHED: <b>{strategy_name}</b>\nReason: {reason}"
            )

    def promote(self, strategy_name: str) -> bool:
        """Attempt to un-bench a strategy.

        Returns True if the strategy is now active, False if burn-in is
        still incomplete.
        """
        state = self._states.get(strategy_name)
        if state is None:
            logger.warning("StrategyRouter.promote: unknown strategy {}", strategy_name)
            return False

        if not state.burn_in_complete():
            logger.debug(
                "StrategyRouter.promote: {} not ready (candles={} regimes={})",
                strategy_name, state.candles_seen, state.distinct_regimes,
            )
            return False

        state.benched = False
        state.active = True
        state.bench_reason = ""
        logger.info("StrategyRouter: PROMOTED {}", strategy_name)
        self._send_alert(f"✅ Strategy PROMOTED: <b>{strategy_name}</b>")
        return True

    # ── Velocity ─────────────────────────────────────────────────────────────

    def _check_velocity(self, state: StrategyState) -> None:
        """Bench strategy if win-rate collapsed by > VELOCITY_CHECK_THRESHOLD."""
        v = state.velocity()
        if v is None:
            return
        if v < -self.VELOCITY_CHECK_THRESHOLD:
            self.bench(
                state.name,
                f"velocity_collapse_{v:.3f}_threshold_{self.VELOCITY_CHECK_THRESHOLD}",
            )

    # ── Correlation ──────────────────────────────────────────────────────────

    def _check_correlations(self) -> None:
        """Bench the lower-ranked strategy from any highly-correlated pair."""
        active = [s for s in self._states.values() if not s.benched]
        for i, a in enumerate(active):
            for b in active[i + 1:]:
                if (
                    len(a.recent_pnl) < CORRELATION_MIN_SAMPLES
                    or len(b.recent_pnl) < CORRELATION_MIN_SAMPLES
                ):
                    continue

                rho = _pearson(
                    a.recent_pnl[-CORRELATION_MIN_SAMPLES:],
                    b.recent_pnl[-CORRELATION_MIN_SAMPLES:],
                )
                if rho >= self.CORRELATION_BENCH_THRESHOLD:
                    beta_a = self.shadow.get_beta(a.name)
                    beta_b = self.shadow.get_beta(b.name)
                    p_a = beta_a.mean() if beta_a else 0.5
                    p_b = beta_b.mean() if beta_b else 0.5
                    loser = a if p_a <= p_b else b
                    other = b if loser is a else a
                    self.bench(
                        loser.name,
                        f"correlation_with_{other.name}_rho={rho:.3f}",
                    )

    # ── Telegram ─────────────────────────────────────────────────────────────

    def _send_alert(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.telegram.send_message(text))
            else:
                loop.run_until_complete(self.telegram.send_message(text))
        except Exception as exc:
            logger.debug("StrategyRouter: Telegram alert failed: {}", exc)

    # ── Introspection ────────────────────────────────────────────────────────

    def get_state(self, strategy_name: str) -> StrategyState | None:
        return self._states.get(strategy_name)

    def active_strategies(self) -> list[str]:
        return [n for n, s in self._states.items() if not s.benched]

    def benched_strategies(self) -> list[str]:
        return [n for n, s in self._states.items() if s.benched]

    def summary(self) -> dict[str, dict]:
        return {
            name: {
                "active": not state.benched,
                "burn_in_complete": state.burn_in_complete(),
                "candles_seen": state.candles_seen,
                "distinct_regimes": state.distinct_regimes,
                "bench_reason": state.bench_reason,
                "velocity": state.velocity(),
            }
            for name, state in self._states.items()
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation coefficient for two equal-length sequences."""
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom_sq = (
        sum((xi - mx) ** 2 for xi in x)
        * sum((yi - my) ** 2 for yi in y)
    )
    if denom_sq == 0.0:
        return 0.0
    return num / denom_sq ** 0.5
