"""GP Step 8 — StrategyRouter tests (10 tests).

Covers:
  - FIX-2: burn-in blocked when only candle count is met (no regime diversity)
  - FIX-2: burn-in blocked when only regime diversity is met (not enough candles)
  - FIX-2: burn-in passes when BOTH candles and regimes are satisfied
  - Benched strategy always routes to Cash
  - Velocity collapse below -0.30 benches the strategy
  - Velocity within threshold does NOT bench
  - Correlation >= 0.85 benches the lower-ranked strategy
  - Correlation < 0.85 does NOT bench either strategy
  - promote() fails (returns False) if burn-in is incomplete
  - promote() succeeds (returns True) if burn-in is complete and strategy is benched
"""
import pytest

from shadow_tracker import ShadowTracker
from strategy_router import (
    BURN_IN_CANDLES,
    BURN_IN_MIN_REGIMES,
    CORRELATION_MIN_SAMPLES,
    VELOCITY_WINDOW,
    StrategyRouter,
    StrategyState,
    _pearson,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _router(strategies=("Breakout", "TrendPullback")) -> tuple[StrategyRouter, ShadowTracker]:
    shadow = ShadowTracker()
    router = StrategyRouter(shadow_tracker=shadow, telegram=None, strategy_names=strategies)
    return router, shadow


def _complete_burn_in(state: StrategyState) -> None:
    """Manually satisfy burn-in for a state object."""
    state.candles_seen = BURN_IN_CANDLES
    state.regimes_seen = {"RANGING", "TRENDING_UP"}


# ---------------------------------------------------------------------------
# FIX-2: Burn-in gate
# ---------------------------------------------------------------------------

def test_burnin_blocked_candles_only():
    """Burn-in must fail when candle count is met but only 1 distinct regime seen."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")

    # Satisfy candle count but only one regime
    state.candles_seen = BURN_IN_CANDLES
    state.regimes_seen = {"RANGING"}

    assert not state.burn_in_complete()
    result = router.tick(regime="RANGING", tournament_winner="Breakout")
    assert result == "Cash"


def test_burnin_blocked_regimes_only():
    """Burn-in must fail when regime diversity is met but not enough candles."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")

    # Satisfy regimes but not candles — set to -2 so tick()'s +1 still leaves it short
    state.candles_seen = BURN_IN_CANDLES - 2
    state.regimes_seen = {"RANGING", "TRENDING_UP"}

    assert not state.burn_in_complete()
    result = router.tick(regime="RANGING", tournament_winner="Breakout")
    assert result == "Cash"


def test_burnin_passes_both_conditions():
    """Burn-in must pass when BOTH candles and regimes thresholds are satisfied."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")
    _complete_burn_in(state)

    result = router.tick(regime="RANGING", tournament_winner="Breakout")
    assert result == "Breakout"


# ---------------------------------------------------------------------------
# Bench gate
# ---------------------------------------------------------------------------

def test_benched_strategy_routes_cash():
    """A benched strategy must always route to Cash regardless of burn-in."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")
    _complete_burn_in(state)

    router.bench("Breakout", reason="test")
    result = router.tick(regime="RANGING", tournament_winner="Breakout")
    assert result == "Cash"


# ---------------------------------------------------------------------------
# Velocity check
# ---------------------------------------------------------------------------

def test_velocity_collapse_benches_strategy():
    """Win-rate drop > 0.30 between snapshots must bench the strategy."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")
    _complete_burn_in(state)

    # First snapshot: high win rate (all wins)
    state.win_rate_snapshots.append(0.8)
    # Second snapshot: collapsed win rate (drop > 0.30)
    state.win_rate_snapshots.append(0.4)  # 0.4 - 0.8 = -0.40

    router._check_velocity(state)
    assert state.benched is True
    assert "velocity_collapse" in state.bench_reason


def test_velocity_within_threshold_no_bench():
    """Win-rate drop <= 0.30 must NOT bench the strategy."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")
    _complete_burn_in(state)

    # Drop of exactly 0.20 (below threshold)
    state.win_rate_snapshots.append(0.7)
    state.win_rate_snapshots.append(0.5)  # 0.5 - 0.7 = -0.20

    router._check_velocity(state)
    assert state.benched is False


# ---------------------------------------------------------------------------
# Correlation check
# ---------------------------------------------------------------------------

def test_correlation_high_benches_lower_ranked():
    """ρ >= 0.85 must bench the strategy with the lower win-probability."""
    router, shadow = _router(strategies=("Breakout", "TrendPullback"))

    # Give Breakout higher win prob
    for _ in range(10):
        gid = shadow.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=0.01)
        shadow.close_ghost(gid, exit_price=51_000.0)

    # Give TrendPullback lower win prob
    for _ in range(10):
        gid = shadow.open_ghost("TrendPullback", entry_price=50_000.0, side="BUY", size=0.01)
        shadow.close_ghost(gid, exit_price=49_000.0)

    # Build identical PnL sequences → ρ = 1.0
    pnl_seq = [100.0, -50.0, 80.0, -30.0, 90.0,
                -60.0, 110.0, -20.0, 70.0, -40.0,
                100.0, -50.0, 80.0, -30.0, 90.0,
                -60.0, 110.0, -20.0, 70.0, -40.0]
    assert len(pnl_seq) == CORRELATION_MIN_SAMPLES

    router.get_state("Breakout").recent_pnl = list(pnl_seq)
    router.get_state("TrendPullback").recent_pnl = list(pnl_seq)

    router._check_correlations()

    # TrendPullback has lower win prob → must be benched
    assert router.get_state("TrendPullback").benched is True
    assert router.get_state("Breakout").benched is False


def test_correlation_low_no_bench():
    """ρ < 0.85 must NOT bench either strategy."""
    router, shadow = _router(strategies=("Breakout", "TrendPullback"))

    # Uncorrelated PnL sequences
    pnl_a = [100.0 if i % 2 == 0 else -50.0 for i in range(CORRELATION_MIN_SAMPLES)]
    pnl_b = [-50.0 if i % 2 == 0 else 100.0 for i in range(CORRELATION_MIN_SAMPLES)]

    router.get_state("Breakout").recent_pnl = pnl_a
    router.get_state("TrendPullback").recent_pnl = pnl_b

    router._check_correlations()

    assert not router.get_state("Breakout").benched
    assert not router.get_state("TrendPullback").benched


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

def test_promote_fails_before_burn_in():
    """promote() must return False when burn-in is not complete."""
    router, _ = _router(strategies=("Breakout",))
    router.bench("Breakout", reason="test")
    # Don't complete burn-in

    result = router.promote("Breakout")
    assert result is False
    assert router.get_state("Breakout").benched is True


def test_promote_succeeds_after_burn_in():
    """promote() must return True and un-bench the strategy when burn-in is done."""
    router, _ = _router(strategies=("Breakout",))
    state = router.get_state("Breakout")
    _complete_burn_in(state)
    router.bench("Breakout", reason="test")

    result = router.promote("Breakout")
    assert result is True
    assert state.benched is False
    assert state.active is True


# ---------------------------------------------------------------------------
# Pearson helper sanity
# ---------------------------------------------------------------------------

def test_pearson_identical_sequences():
    """Identical sequences should have ρ = 1.0."""
    seq = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pearson(seq, seq) == pytest.approx(1.0)


def test_pearson_opposite_sequences():
    """Perfectly anti-correlated sequences should have ρ = -1.0."""
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert _pearson(a, b) == pytest.approx(-1.0)
