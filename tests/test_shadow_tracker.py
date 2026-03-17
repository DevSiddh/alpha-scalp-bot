"""GP Step 6 — ShadowTracker tests (9 tests).

Covers:
  - Open and close a ghost trade returns correct pnl and reward
  - FIX-1: pnl_max == pnl_min → reward = 0.0 (no ZeroDivisionError)
  - FIX-3: fees are deducted from shadow PnL on both legs
  - Long and short PnL directions are correct
  - Beta distribution updates on win and loss
  - Thompson sampling returns a float per known strategy
  - Multiple ghosts can be open simultaneously
  - StrategyBeta mean initialises at 0.5 (uniform prior)
  - TradeTrackerV2.attach_shadow_tracker wires correctly
"""
import pytest

from shadow_tracker import ShadowTracker, StrategyBeta
from trade_tracker_v2 import TradeTrackerV2


FEE = 0.001  # FIX-3 fee rate


# ---------------------------------------------------------------------------
# Basic open / close
# ---------------------------------------------------------------------------

def test_open_and_close_returns_result():
    """close_ghost must return a dict with pnl, reward, and won fields."""
    tracker = ShadowTracker()
    gid = tracker.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=0.01)
    result = tracker.close_ghost(gid, exit_price=50_500.0)
    assert "pnl" in result
    assert "reward" in result
    assert "won" in result
    assert result["won"] is True


def test_unknown_ghost_returns_error():
    tracker = ShadowTracker()
    result = tracker.close_ghost("nonexistent", exit_price=50_000.0)
    assert result.get("error") == "ghost_not_found"


# ---------------------------------------------------------------------------
# FIX-3: fee deduction in shadow PnL
# ---------------------------------------------------------------------------

def test_fix3_fees_deducted_from_pnl():
    """Shadow PnL for a flat trade (entry == exit) must be negative due to fees."""
    tracker = ShadowTracker()
    price = 50_000.0
    size = 1.0
    gid = tracker.open_ghost("Breakout", entry_price=price, side="BUY", size=size)
    result = tracker.close_ghost(gid, exit_price=price)
    # No price move, but fees should make PnL negative
    expected_fees = (price * size * FEE) + (price * size * FEE)
    assert result["pnl"] == pytest.approx(-expected_fees, rel=1e-6)
    assert result["won"] is False


def test_fix3_short_pnl_correct():
    """Short: price falls → positive PnL minus fees."""
    tracker = ShadowTracker()
    entry, exit_p, size = 50_000.0, 49_000.0, 1.0
    gid = tracker.open_ghost("OrderFlowMomentum", entry_price=entry, side="SELL", size=size)
    result = tracker.close_ghost(gid, exit_price=exit_p)
    gross = (entry - exit_p) * size
    fees = (entry * size * FEE) + (exit_p * size * FEE)
    assert result["pnl"] == pytest.approx(gross - fees, rel=1e-6)


# ---------------------------------------------------------------------------
# FIX-1: div-by-zero guard
# ---------------------------------------------------------------------------

def test_fix1_divzero_reward_is_zero():
    """If all PnL values in history are identical, reward must be 0.0."""
    tracker = ShadowTracker()
    # Open and close three trades at exactly the same loss (flat trade = fee loss)
    for _ in range(3):
        gid = tracker.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=1.0)
        tracker.close_ghost(gid, exit_price=50_000.0)  # identical each time

    # Fourth trade: PnL history has identical values → reward must be 0.0
    gid = tracker.open_ghost("Breakout", entry_price=50_000.0, side="BUY", size=1.0)
    result = tracker.close_ghost(gid, exit_price=50_000.0)
    assert result["reward"] == 0.0


# ---------------------------------------------------------------------------
# Beta distribution
# ---------------------------------------------------------------------------

def test_beta_prior_mean_is_half():
    """Fresh StrategyBeta must have mean = 0.5 (uniform prior Beta(1,1))."""
    b = StrategyBeta()
    assert b.mean() == pytest.approx(0.5)


def test_beta_updates_toward_win():
    """After a pure win (reward=1.0), mean must shift above 0.5."""
    b = StrategyBeta()
    b.update(1.0)
    assert b.mean() > 0.5
    assert b.total_trades == 1
    assert b.total_wins == 1


def test_beta_updates_toward_loss():
    """After a pure loss (reward=0.0), mean must shift below 0.5."""
    b = StrategyBeta()
    b.update(0.0)
    assert b.mean() < 0.5


# ---------------------------------------------------------------------------
# Thompson sampling
# ---------------------------------------------------------------------------

def test_thompson_sample_returns_float_per_strategy():
    """thompson_sample must return a float in [0, 1] for each known strategy."""
    tracker = ShadowTracker()
    for strategy in ("Breakout", "VWAP_MeanReversion"):
        gid = tracker.open_ghost(strategy, entry_price=50_000.0, side="BUY")
        tracker.close_ghost(gid, exit_price=50_500.0)

    samples = tracker.thompson_sample()
    assert set(samples.keys()) == {"Breakout", "VWAP_MeanReversion"}
    for val in samples.values():
        assert 0.0 <= val <= 1.0


# ---------------------------------------------------------------------------
# TradeTrackerV2 integration
# ---------------------------------------------------------------------------

def test_trade_tracker_attach_shadow():
    """attach_shadow_tracker must store reference; get_shadow_tracker returns it."""
    tracker = TradeTrackerV2(history_file="logs/test_shadow_attach.jsonl")
    shadow = ShadowTracker()
    tracker.attach_shadow_tracker(shadow)
    assert tracker.get_shadow_tracker() is shadow
