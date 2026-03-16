"""Alpha-Scalp Bot – TournamentEngine.

GP-S7: Selects the active strategy each candle via Thompson Sampling
over Beta distributions maintained by ShadowTracker.

Cash mode triggers when no strategy samples above CASH_SAMPLE_THRESHOLD.

Normalised expectancy (E*) is computed per strategy as:
    E* = 2 × mean_win_prob − 1   (range [-1, +1], unit pnl assumed)
    then min-max scaled to [0, 1] over the current strategy pool.

FIX-5: HmmScheduler — first training uses 6-month BTC 3m history
       (~87 000 candles).  Retrains every Sunday when
       new_candles_since_last_training > 500.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from loguru import logger

if TYPE_CHECKING:
    from shadow_tracker import ShadowTracker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASH_STRATEGY_NAME: str = "Cash"
CASH_SAMPLE_THRESHOLD: float = 0.45  # winner sample < this → Cash mode


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TournamentResult:
    """Outcome of one tournament round."""
    winner: str                              # strategy name, or "Cash"
    sample: float                            # Thompson sample of the winner
    samples: dict[str, float] = field(default_factory=dict)
    expectancy: dict[str, float] = field(default_factory=dict)
    cash_mode: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# HMM Scheduler  (FIX-5)
# ---------------------------------------------------------------------------

class HmmScheduler:
    """Tracks HMM regime-model training state.

    FIX-5 rules
    -----------
    * First training: waits until at least HMM_INITIAL_CANDLES candles have
      been observed (≈ 6 months of 3-minute BTC data = 87 000 bars).
    * Subsequent retrains: every Sunday, but only when
      new_candles_since_last_train > HMM_RETRAIN_MIN_CANDLES (500).

    The actual HMM fit is delegated to an injected callable (set via
    ``set_train_fn``).  If no callable is provided the scheduler logs the
    trigger but skips execution — safe for testing.
    """

    HMM_INITIAL_CANDLES: int = 87_000   # ~6 months × 3-minute bars
    HMM_RETRAIN_MIN_CANDLES: int = 500  # minimum new candles before Sunday retrain

    def __init__(self) -> None:
        self.initial_training_done: bool = False
        self.candles_seen: int = 0
        self.candles_at_last_train: int = 0
        self._train_fn: Callable[[], None] | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    def set_train_fn(self, fn: Callable[[], None]) -> None:
        """Inject the HMM training callable."""
        self._train_fn = fn

    def tick(self) -> bool:
        """Record one new candle.  Returns True if training was triggered."""
        self.candles_seen += 1

        if not self.initial_training_done:
            if self.candles_seen >= self.HMM_INITIAL_CANDLES:
                self._trigger_train("initial")
                return True
            return False

        # After initial training: Sunday + enough new candles
        new_since = self.new_candles_since_last_train
        if self._is_sunday() and new_since > self.HMM_RETRAIN_MIN_CANDLES:
            self._trigger_train("sunday_retrain")
            return True

        return False

    @property
    def new_candles_since_last_train(self) -> int:
        return self.candles_seen - self.candles_at_last_train

    # ── Private ─────────────────────────────────────────────────────────────

    def _trigger_train(self, reason: str) -> None:
        self.candles_at_last_train = self.candles_seen
        if not self.initial_training_done:
            self.initial_training_done = True

        logger.info(
            "HmmScheduler: training triggered | reason={} total_candles={} new_since_last={}",
            reason, self.candles_seen, self.new_candles_since_last_train,
        )
        if self._train_fn is not None:
            try:
                self._train_fn()
            except Exception as exc:
                logger.error("HmmScheduler: train_fn raised {}", exc)

    @staticmethod
    def _is_sunday() -> bool:
        """Return True when UTC weekday is Sunday (weekday() == 6)."""
        return datetime.now(tz=timezone.utc).weekday() == 6


# ---------------------------------------------------------------------------
# TournamentEngine
# ---------------------------------------------------------------------------

class TournamentEngine:
    """Selects the winning sub-strategy each candle via Thompson Sampling.

    Usage
    -----
    engine = TournamentEngine(shadow_tracker)
    result = engine.run_tournament(eligible=["Breakout", "TrendPullback"])
    active_strategy = result.winner
    """

    CASH_SAMPLE_THRESHOLD: float = CASH_SAMPLE_THRESHOLD

    def __init__(self, shadow_tracker: "ShadowTracker") -> None:
        self.shadow = shadow_tracker
        self.hmm_scheduler = HmmScheduler()
        self._history: list[TournamentResult] = []

    # ── Main entry point ────────────────────────────────────────────────────

    def run_tournament(
        self,
        eligible: list[str] | None = None,
    ) -> TournamentResult:
        """Run one tournament round.

        Parameters
        ----------
        eligible:
            Strategy names to consider.  If None, all strategies known to
            ShadowTracker are candidates.  "Cash" may appear explicitly.

        Returns
        -------
        TournamentResult with the winning strategy (or Cash).
        """
        samples = self.shadow.thompson_sample()

        if eligible is not None:
            samples = {k: v for k, v in samples.items() if k in eligible}

        if not samples:
            result = TournamentResult(
                winner=CASH_STRATEGY_NAME,
                sample=0.0,
                cash_mode=True,
                reason="no_eligible_strategies",
            )
            self._history.append(result)
            return result

        expectancy = self._compute_expectancy(samples)

        winner_name = max(samples, key=lambda k: samples[k])
        winner_sample = samples[winner_name]

        if winner_sample < self.CASH_SAMPLE_THRESHOLD:
            result = TournamentResult(
                winner=CASH_STRATEGY_NAME,
                sample=winner_sample,
                samples=samples,
                expectancy=expectancy,
                cash_mode=True,
                reason=(
                    f"winner_sample_{winner_sample:.3f}"
                    f"_below_threshold_{self.CASH_SAMPLE_THRESHOLD}"
                ),
            )
        else:
            result = TournamentResult(
                winner=winner_name,
                sample=winner_sample,
                samples=samples,
                expectancy=expectancy,
                cash_mode=False,
                reason="thompson_sampling",
            )

        self._history.append(result)
        logger.debug(
            "Tournament | winner={} sample={:.3f} cash={} expectancy={}",
            result.winner, result.sample, result.cash_mode,
            {k: f"{v:.3f}" for k, v in expectancy.items()},
        )
        return result

    # ── Expectancy ───────────────────────────────────────────────────────────

    def _compute_expectancy(
        self,
        samples: dict[str, float],
    ) -> dict[str, float]:
        """Normalised expectancy for each strategy in *samples*.

        Raw:    E(s) = 2 × mean_win_prob − 1   ∈ [-1, +1]
        Scaled: min-max over the current pool → [0, 1]

        If all strategies have identical expectancy the raw values are
        returned as-is (avoids division by zero).
        """
        raw: dict[str, float] = {}
        for name in samples:
            beta = self.shadow.get_beta(name)
            p = beta.mean() if beta is not None else 0.5
            raw[name] = round(2.0 * p - 1.0, 6)

        values = list(raw.values())
        v_min, v_max = min(values), max(values)

        if v_max == v_min:
            # All strategies indistinguishable — return neutral mid-point
            return {k: 0.5 for k in raw}

        return {
            k: round((v - v_min) / (v_max - v_min), 4)
            for k, v in raw.items()
        }

    # ── Introspection ────────────────────────────────────────────────────────

    def last_result(self) -> TournamentResult | None:
        """The most recent tournament result."""
        return self._history[-1] if self._history else None

    def history(self, n: int = 20) -> list[TournamentResult]:
        """Last *n* tournament results."""
        return self._history[-n:]

    def cash_rate(self, window: int = 50) -> float:
        """Fraction of last *window* rounds that went to Cash mode."""
        recent = self._history[-window:]
        if not recent:
            return 0.0
        return sum(1 for r in recent if r.cash_mode) / len(recent)
