"""GP Step 2 — Risk Engine extension tests (12 tests).

Covers:
  - Three-Strike cooldown (3 losses → 90min pause, win resets counter)
  - Equity Floor shutdown (balance ≤ 80% of start)
  - Active Cash Mode position-size halving (80-90% equity)
  - Minimum SL floor (0.15% of entry)
  - ATR validation (zero / sub-threshold rejection)
  - Regime-aware minimum R:R (RANGING=1.5, TRENDING=2.0, VOLATILE=1.8)
  - Kelly min-trades gate (unchanged at 300)
  - can_open_trade integration gate
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from risk_engine import RiskEngine, RiskDecision
import config as cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(start_balance: float = 10_000.0) -> RiskEngine:
    """Return a RiskEngine wired to a mock exchange, bypass balance fetch."""
    exchange = MagicMock()
    exchange.price_to_precision.side_effect = lambda sym, price: price

    with patch.object(RiskEngine, "_fetch_usdt_balance", return_value=start_balance):
        engine = RiskEngine(exchange)

    engine.daily_start_balance = start_balance
    engine._cached_balance = start_balance
    engine._cache_timestamp = time.monotonic()
    return engine


# ---------------------------------------------------------------------------
# Three-Strike Cooldown
# ---------------------------------------------------------------------------

def test_three_strike_activates_after_3_losses():
    """Three consecutive losses must trigger cooldown."""
    engine = _make_engine()
    assert not engine.check_three_strike_cooldown()
    engine.record_trade_pnl(-100, won=False)
    engine.record_trade_pnl(-100, won=False)
    assert not engine.check_three_strike_cooldown()  # only 2 losses
    engine.record_trade_pnl(-100, won=False)          # 3rd loss → trigger
    assert engine.check_three_strike_cooldown()


def test_three_strike_win_resets_counter():
    """A win in the middle must reset the consecutive-loss counter."""
    engine = _make_engine()
    engine.record_trade_pnl(-100, won=False)
    engine.record_trade_pnl(-100, won=False)
    engine.record_trade_pnl(+200, won=True)   # resets to 0
    engine.record_trade_pnl(-100, won=False)  # back to 1 — no cooldown
    assert not engine.check_three_strike_cooldown()


def test_three_strike_cooldown_expires():
    """After the cooldown window passes, trading must be re-allowed."""
    engine = _make_engine()
    # Set cooldown to expire in the past
    engine._three_strike_cooldown_until = time.time() - 1
    assert not engine.check_three_strike_cooldown()


def test_can_open_trade_blocked_during_three_strike(monkeypatch):
    """can_open_trade must return False while three-strike cooldown is active."""
    engine = _make_engine()
    # Disable other guards so only three-strike fires
    monkeypatch.setattr(engine, "check_kill_switch", lambda: False)
    monkeypatch.setattr(engine, "check_equity_floor", lambda: False)
    monkeypatch.setattr(engine, "check_circuit_breaker", lambda: False)
    monkeypatch.setattr(engine, "check_max_positions", lambda: True)
    monkeypatch.setattr(engine, "check_total_concurrent_trades", lambda: True)

    engine._three_strike_cooldown_until = time.time() + 5400
    allowed, reason = engine.can_open_trade()
    assert not allowed
    assert "three-strike" in reason.lower()


# ---------------------------------------------------------------------------
# Equity Floor
# ---------------------------------------------------------------------------

def test_equity_floor_triggers_at_80_pct():
    """Balance at exactly 80% of start must activate the equity floor."""
    engine = _make_engine(start_balance=10_000.0)
    engine._cached_balance = 8_000.0  # exactly 80%
    assert engine.check_equity_floor()
    assert engine.equity_floor_active


def test_equity_floor_does_not_trigger_above_80():
    """Balance at 85% of start must NOT activate the equity floor."""
    engine = _make_engine(start_balance=10_000.0)
    engine._cached_balance = 8_500.0  # 85% — above floor
    assert not engine.check_equity_floor()


# ---------------------------------------------------------------------------
# Active Cash Mode
# ---------------------------------------------------------------------------

def test_active_cash_mode_halves_size():
    """When equity is between 80-90% of start, multiplier must be 0.5."""
    engine = _make_engine(start_balance=10_000.0)
    engine._cached_balance = 8_700.0  # 87% — in active cash zone
    assert engine.get_active_cash_multiplier() == 0.5


def test_active_cash_mode_full_size_above_90():
    """When equity is ≥ 90% of start, multiplier must be 1.0."""
    engine = _make_engine(start_balance=10_000.0)
    engine._cached_balance = 9_500.0  # 95% — normal
    assert engine.get_active_cash_multiplier() == 1.0


# ---------------------------------------------------------------------------
# Minimum SL Floor
# ---------------------------------------------------------------------------

def test_sl_floor_applied_when_atr_too_tight():
    """If computed SL is < 0.15% from entry, it must be widened to the floor."""
    engine = _make_engine()
    # ATR so tiny the SL would be only ~0.05% away
    entry = 50_000.0
    # Force fallback path: set SCALP_SL_USE_ATR off so pct-based SL fires
    engine.stop_loss_pct = 0.0005  # 0.05% — below the 0.15% floor
    sl = engine.get_stop_loss(entry, "BUY", atr=0.0, regime="RANGING")
    min_distance = entry * engine._min_sl_floor_pct  # 0.15%
    assert abs(entry - sl) >= min_distance - 0.01  # allow tiny float rounding


# ---------------------------------------------------------------------------
# ATR Validation
# ---------------------------------------------------------------------------

def test_atr_validation_rejects_zero():
    engine = _make_engine()
    assert not engine.validate_atr(0.0, 50_000.0)


def test_atr_validation_rejects_sub_threshold():
    """ATR below 0.05% of entry must be rejected."""
    engine = _make_engine()
    entry = 50_000.0
    # 0.03% of entry = 15.0 — below the 0.05% threshold (25.0)
    assert not engine.validate_atr(15.0, entry)


def test_atr_validation_passes_normal():
    engine = _make_engine()
    assert engine.validate_atr(200.0, 50_000.0)  # 0.4% of entry — fine


# ---------------------------------------------------------------------------
# Regime-aware R:R
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("regime,sl_mult,tp_mult,expected_allowed", [
    # RANGING needs 1.5 — ratio 2.0/1.5 = 1.33 → BLOCKED
    ("RANGING",  1.5, 2.0, False),
    # RANGING needs 1.5 — ratio 3.0/1.5 = 2.0 → OK
    ("RANGING",  1.5, 3.0, True),
    # TRENDING needs 2.0 — ratio 2.5/1.5 = 1.67 → BLOCKED
    ("TRENDING", 1.5, 2.5, False),
    # TRENDING needs 2.0 — ratio 3.0/1.5 = 2.0 → OK (edge)
    ("TRENDING", 1.5, 3.0, True),
    # VOLATILE needs 1.8 — ratio 2.5/1.5 = 1.67 → BLOCKED
    ("VOLATILE", 1.5, 2.5, False),
    # VOLATILE needs 1.8 — ratio 2.8/1.5 = 1.87 → OK
    ("VOLATILE", 1.5, 2.8, True),
])
def test_regime_rr(regime, sl_mult, tp_mult, expected_allowed, monkeypatch):
    """check_reward_risk_ratio must use regime-specific minimum R:R."""
    engine = _make_engine()
    monkeypatch.setattr(cfg, "ATR_SL_MULTIPLIER", sl_mult)
    monkeypatch.setattr(cfg, "ATR_TP_MULTIPLIER", tp_mult)
    result = engine.check_reward_risk_ratio(50_000.0, atr=100.0, regime=regime)
    assert result.allowed == expected_allowed


# ---------------------------------------------------------------------------
# Kelly min-trades gate (300 trades required)
# ---------------------------------------------------------------------------

def test_kelly_skipped_below_300_trades():
    """Kelly sizing must be ignored when trade count < 300."""
    engine = _make_engine(start_balance=10_000.0)
    tracker = MagicMock()
    tracker._trades = list(range(50))  # only 50 trades
    engine.set_trade_tracker(tracker)

    # With a generous kelly_fraction, position should still use fixed risk
    size_kelly = engine.calculate_position_size(
        entry_price=50_000.0, stop_price=49_500.0, kelly_fraction=0.10
    )
    size_fixed = engine.calculate_position_size(
        entry_price=50_000.0, stop_price=49_500.0, kelly_fraction=0.0
    )
    assert size_kelly == size_fixed, (
        f"Kelly should be ignored below {cfg.KELLY_MIN_TRADES} trades, "
        f"but sizes differ: kelly={size_kelly}, fixed={size_fixed}"
    )
