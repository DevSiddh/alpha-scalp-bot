"""GP Step 7 — TournamentEngine tests (10 tests).

Covers:
  - Thompson sampling selects the strategy with the highest sample
  - Cash mode triggers when winner's sample is below threshold
  - Cash mode triggers when no eligible strategies exist
  - Expectancy values are normalised to [0, 1]
  - HmmScheduler: initial training triggers after HMM_INITIAL_CANDLES ticks
  - HmmScheduler: no training before HMM_INITIAL_CANDLES ticks
  - HmmScheduler: Sunday retrain fires when new_candles > 500
  - HmmScheduler: no retrain on Sunday when new_candles <= 500
  - run_tournament respects the eligible filter
  - cash_rate returns the correct fraction
"""
from unittest.mock import MagicMock, patch

import pytest

from shadow_tracker import ShadowTracker
from tournament_engine import (
    CASH_STRATEGY_NAME,
    HmmScheduler,
    TournamentEngine,
    TournamentResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seeded_engine(
    strategies: list[str],
    wins: int = 5,
    losses: int = 0,
) -> TournamentEngine:
    """Build a TournamentEngine whose ShadowTracker has *wins* wins for each strategy.

    Uses varying exit prices so FIX-1 (div-by-zero guard) does not suppress
    rewards — each trade has a distinct PnL so pnl_max != pnl_min.
    """
    shadow = ShadowTracker()
    for name in strategies:
        for i in range(wins):
            # Varying profitable exits: 50500, 51000, 51500 ...
            exit_p = 50_000.0 + (i + 1) * 500
            gid = shadow.open_ghost(name, entry_price=50_000.0, side="BUY", size=0.01)
            shadow.close_ghost(gid, exit_price=exit_p)
        for i in range(losses):
            # Varying losing exits: 49500, 49000, 48500 ...
            exit_p = 50_000.0 - (i + 1) * 500
            gid = shadow.open_ghost(name, entry_price=50_000.0, side="BUY", size=0.01)
            shadow.close_ghost(gid, exit_price=exit_p)
    return TournamentEngine(shadow)


# ---------------------------------------------------------------------------
# Thompson sampling
# ---------------------------------------------------------------------------

def test_tournament_selects_highest_sample():
    """The winner must be the strategy whose Beta sample is highest.

    Varying exit prices are required so FIX-1 (div-by-zero guard when
    pnl_max == pnl_min) does not suppress rewards and flatten the Beta
    distributions.
    """
    shadow = ShadowTracker()

    # Give "Breakout" 20 wins with incrementally rising exits → reward ≈ 1.0 each
    for i in range(20):
        exit_p = 50_000.0 + (i + 1) * 500   # 50500, 51000, … 60000
        gid = shadow.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=0.01)
        shadow.close_ghost(gid, exit_price=exit_p)

    # Give "VWAP_MeanReversion" 20 losses with incrementally falling exits → reward ≈ 0.0 each
    for i in range(20):
        exit_p = 50_000.0 - (i + 1) * 500   # 49500, 49000, … 40000
        gid = shadow.open_ghost("VWAP_MeanReversion", entry_price=50_000.0, side="BUY", size=0.01)
        shadow.close_ghost(gid, exit_price=exit_p)

    engine = TournamentEngine(shadow)

    # Over 50 rounds, Breakout should win the vast majority
    results = [engine.run_tournament() for _ in range(50)]
    breakout_wins = sum(1 for r in results if r.winner == "Breakout")
    assert breakout_wins > 30, f"Expected Breakout to dominate, got {breakout_wins}/50"


def test_tournament_winner_sample_matches_result():
    """result.sample must equal samples[result.winner]."""
    engine = _seeded_engine(["Breakout", "TrendPullback"])
    result = engine.run_tournament()
    if not result.cash_mode:
        assert result.sample == result.samples[result.winner]


# ---------------------------------------------------------------------------
# Cash mode
# ---------------------------------------------------------------------------

def test_cash_mode_when_sample_below_threshold():
    """If all strategies have many losses, samples drop below threshold → Cash."""
    shadow = ShadowTracker()
    # 30 losses → Beta heavily skewed toward 0 → samples near 0
    for _ in range(30):
        gid = shadow.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=0.01)
        shadow.close_ghost(gid, exit_price=48_000.0)

    engine = TournamentEngine(shadow)
    # Over 20 rounds at least some should hit Cash
    results = [engine.run_tournament() for _ in range(20)]
    cash_count = sum(1 for r in results if r.cash_mode)
    assert cash_count > 0, "Expected at least some Cash rounds with all-loss strategies"


def test_cash_mode_no_eligible_strategies():
    """Empty eligible list → Cash with reason=no_eligible_strategies."""
    engine = _seeded_engine(["Breakout"])
    result = engine.run_tournament(eligible=[])
    assert result.cash_mode is True
    assert result.winner == CASH_STRATEGY_NAME
    assert "no_eligible_strategies" in result.reason


# ---------------------------------------------------------------------------
# Expectancy
# ---------------------------------------------------------------------------

def test_expectancy_values_in_0_1():
    """All normalised expectancy values must be in [0, 1]."""
    engine = _seeded_engine(["Breakout", "TrendPullback", "VWAP_MeanReversion"])
    result = engine.run_tournament()
    for name, e in result.expectancy.items():
        assert 0.0 <= e <= 1.0, f"Expectancy out of range for {name}: {e}"


def test_expectancy_keys_match_samples():
    """Expectancy dict must have the same keys as samples dict."""
    engine = _seeded_engine(["Breakout", "TrendPullback"])
    result = engine.run_tournament()
    assert result.expectancy.keys() == result.samples.keys()


# ---------------------------------------------------------------------------
# HmmScheduler — FIX-5
# ---------------------------------------------------------------------------

def test_hmm_initial_training_triggers():
    """Training must trigger exactly at HMM_INITIAL_CANDLES ticks."""
    scheduler = HmmScheduler()
    called = []
    scheduler.set_train_fn(lambda: called.append(1))

    target = scheduler.HMM_INITIAL_CANDLES
    for _ in range(target - 1):
        triggered = scheduler.tick()
        assert not triggered

    # The final tick should trigger training
    triggered = scheduler.tick()
    assert triggered is True
    assert scheduler.initial_training_done is True
    assert len(called) == 1


def test_hmm_no_training_before_initial_candles():
    """No training before HMM_INITIAL_CANDLES ticks."""
    scheduler = HmmScheduler()
    called = []
    scheduler.set_train_fn(lambda: called.append(1))

    for _ in range(100):
        scheduler.tick()

    assert not scheduler.initial_training_done
    assert len(called) == 0


def test_hmm_sunday_retrain_fires():
    """After initial training, Sunday retrain fires when new_candles > 500."""
    scheduler = HmmScheduler()
    # Fast-forward past initial training
    scheduler.initial_training_done = True
    scheduler.candles_seen = scheduler.HMM_INITIAL_CANDLES
    scheduler.candles_at_last_train = scheduler.HMM_INITIAL_CANDLES

    called = []
    scheduler.set_train_fn(lambda: called.append(1))

    # Add 501 new candles
    for _ in range(501):
        scheduler.candles_seen += 1

    with patch.object(HmmScheduler, "_is_sunday", return_value=True):
        triggered = scheduler.tick()

    assert triggered is True
    assert len(called) == 1


def test_hmm_sunday_no_retrain_insufficient_candles():
    """Sunday retrain must NOT fire when new_candles <= 500."""
    scheduler = HmmScheduler()
    scheduler.initial_training_done = True
    scheduler.candles_seen = scheduler.HMM_INITIAL_CANDLES + 10
    scheduler.candles_at_last_train = scheduler.HMM_INITIAL_CANDLES

    called = []
    scheduler.set_train_fn(lambda: called.append(1))

    with patch.object(HmmScheduler, "_is_sunday", return_value=True):
        triggered = scheduler.tick()

    assert triggered is False
    assert len(called) == 0


# ---------------------------------------------------------------------------
# Eligible filter & cash_rate
# ---------------------------------------------------------------------------

def test_eligible_filter_restricts_candidates():
    """Only strategies in the eligible list should appear in samples."""
    engine = _seeded_engine(["Breakout", "TrendPullback", "VWAP_MeanReversion"])
    result = engine.run_tournament(eligible=["Breakout"])
    if result.samples:
        assert set(result.samples.keys()).issubset({"Breakout"})


def test_cash_rate_correct_fraction():
    """cash_rate must return the fraction of Cash rounds in the last window."""
    shadow = ShadowTracker()
    engine = TournamentEngine(shadow)

    # Force cash_mode by injecting results directly
    engine._history = [
        TournamentResult(winner="Cash", sample=0.0, cash_mode=True, reason="test")
        for _ in range(3)
    ] + [
        TournamentResult(winner="Breakout", sample=0.7, cash_mode=False, reason="test")
        for _ in range(7)
    ]

    rate = engine.cash_rate(window=10)
    assert rate == pytest.approx(0.3)
