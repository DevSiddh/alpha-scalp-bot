"""Regression tests for ExitEngine — GP Step 9.

Mandatory tests (all must pass before Step 9 is complete):
  test_ranging_exit_hits_fixed_tp_not_trailing
  test_trending_trail_tightens_at_5pct_profit
  test_volatile_time_exit_triggers_at_candle_4
  test_breakeven_state_transitions_correctly

All tests use the make_position fixture from tests/conftest.py.
"""

import pytest
from exit_engine import ExitEngine, ExitSignal


# ---------------------------------------------------------------------------
# Mandatory regression tests
# ---------------------------------------------------------------------------

def test_ranging_exit_hits_fixed_tp_not_trailing(make_position):
    """RANGING: exit at fixed TP (entry + 1.5×ATR). No trailing state reached."""
    pos = make_position(regime_at_entry="RANGING", entry_price=85_000.0, entry_atr=120.0)
    engine = ExitEngine(pos)

    # Fixed TP must be 1.5×ATR above entry = 85180
    expected_tp = 85_000.0 + 1.5 * 120.0   # = 85_180.0
    assert abs(engine.tp_price - expected_tp) < 0.01, (
        f"RANGING TP should be {expected_tp}, got {engine.tp_price}"
    )

    # Candle 1: price below TP — hold
    sig = engine.on_candle(current_price=85_100.0, current_atr=120.0)
    assert sig.action == "HOLD"
    assert engine.state == ExitEngine.STATE_ENTRY

    # Candle 2: price hits TP → exit
    sig = engine.on_candle(current_price=85_200.0, current_atr=120.0)
    assert sig.action == "EXIT"
    assert sig.exit_reason == "tp_hit"
    assert engine.state == ExitEngine.STATE_EXIT

    # Confirm trailing state was NEVER entered
    trailing_transitions = [
        h for h in engine.state_history
        if h["to_state"] == ExitEngine.STATE_TRAILING
    ]
    assert len(trailing_transitions) == 0, (
        "RANGING exit must not involve trailing state"
    )


def test_trending_trail_tightens_at_5pct_profit(make_position):
    """TRENDING: trail multiplier tightens from 2×ATR to 1×ATR at +5% profit."""
    pos = make_position(
        regime_at_entry="TRENDING_UP",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # Step 1: trigger breakeven (0.5% above entry = 85_425)
    sig = engine.on_candle(current_price=85_500.0, current_atr=120.0)
    assert engine.state == ExitEngine.STATE_TRAILING, (
        "Should reach TRAILING after breakeven trigger"
    )
    # SL must have moved to at least entry (breakeven) level
    assert engine.sl_price >= 85_000.0

    # Step 2: price at exactly +5% profit (85_000 × 1.05 = 89_250)
    price_5pct = 85_000.0 * 1.05   # = 89_250.0
    sig = engine.on_candle(current_price=price_5pct, current_atr=120.0)

    assert sig.action == "UPDATE_SL", (
        "Trail tighten at +5% should emit UPDATE_SL"
    )
    # Expected SL: price − 1×ATR  (tightened from 2×ATR)
    expected_sl = price_5pct - 1.0 * 120.0   # = 89_130.0
    assert abs(sig.new_sl - expected_sl) < 0.01, (
        f"Trail at +5% should be 1×ATR below price. expected={expected_sl}, got={sig.new_sl}"
    )
    assert engine.sl_price == sig.new_sl


def test_volatile_time_exit_triggers_at_candle_4(make_position):
    """VOLATILE: force-exit when no profit after 4 candles."""
    pos = make_position(
        regime_at_entry="VOLATILE",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # VOLATILE must skip breakeven and go straight to trailing logic
    # Price below entry (no profit) — hold for first 3 candles
    for candle_n in range(1, 4):
        sig = engine.on_candle(current_price=84_800.0, current_atr=120.0)
        assert sig.action == "HOLD", (
            f"Candle {candle_n}: should HOLD before candle 4"
        )
        assert engine.candles_open == candle_n

    # Candle 4: time exit triggers
    sig = engine.on_candle(current_price=84_800.0, current_atr=120.0)
    assert sig.action == "EXIT"
    assert sig.exit_reason == "volatile_time_exit"
    assert engine.state == ExitEngine.STATE_EXIT
    assert engine.hold_duration == 4


def test_breakeven_state_transitions_correctly(make_position):
    """State machine: ENTRY → BREAKEVEN → TRAILING with correct SL movement."""
    pos = make_position(
        regime_at_entry="TRENDING_UP",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)
    initial_sl = engine.sl_price   # entry - 2×ATR = 84_760

    # Candle 1: price below breakeven trigger (85_000 × 1.005 = 85_425) — hold
    sig = engine.on_candle(current_price=85_200.0, current_atr=120.0)
    assert sig.action == "HOLD"
    assert engine.state == ExitEngine.STATE_ENTRY
    assert engine.sl_price == initial_sl   # SL unchanged

    # Candle 2: price above breakeven trigger → transitions through BREAKEVEN → TRAILING
    sig = engine.on_candle(current_price=85_500.0, current_atr=120.0)

    # Must reach TRAILING
    assert engine.state == ExitEngine.STATE_TRAILING

    # SL must have moved to at least entry (breakeven rule)
    assert engine.sl_price >= 85_000.0, (
        f"SL should be >= entry after breakeven. got {engine.sl_price}"
    )

    # State history must record the BREAKEVEN transition
    be_transitions = [
        h for h in engine.state_history
        if h["to_state"] == ExitEngine.STATE_BREAKEVEN
    ]
    assert len(be_transitions) == 1, "State history must record BREAKEVEN transition"
    assert be_transitions[0]["from_state"] == ExitEngine.STATE_ENTRY

    # State history must record the TRAILING transition
    trail_transitions = [
        h for h in engine.state_history
        if h["to_state"] == ExitEngine.STATE_TRAILING
    ]
    assert len(trail_transitions) == 1, "State history must record TRAILING transition"


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

def test_sl_hit_exits_from_entry_state(make_position):
    """SL hit in State 0 exits immediately regardless of regime."""
    pos = make_position(
        regime_at_entry="TRENDING_UP",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)
    sl = engine.sl_price   # 84_760

    sig = engine.on_candle(current_price=sl - 10.0, current_atr=120.0)
    assert sig.action == "EXIT"
    assert sig.exit_reason == "sl_hit"
    assert engine.state == ExitEngine.STATE_EXIT


def test_volatile_exits_with_tight_trail_when_in_profit(make_position):
    """VOLATILE: emits UPDATE_SL with 1×ATR trail when position is in profit."""
    pos = make_position(
        regime_at_entry="VOLATILE",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # Price above entry — in profit
    sig = engine.on_candle(current_price=85_300.0, current_atr=120.0)
    assert sig.action == "UPDATE_SL"
    # Tight 1×ATR trail
    expected_sl = 85_300.0 - 1.0 * 120.0   # = 85_180.0
    assert abs(sig.new_sl - expected_sl) < 0.01


def test_trending_trail_does_not_loosen_sl(make_position):
    """Trail ratchet: SL never moves against the position."""
    pos = make_position(
        regime_at_entry="TRENDING_UP",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # Get to trailing state at a high price
    engine.on_candle(current_price=87_000.0, current_atr=120.0)
    sl_after_high = engine.sl_price

    # Price dips (but above SL) — SL must NOT decrease
    sig = engine.on_candle(current_price=86_000.0, current_atr=120.0)
    assert engine.sl_price >= sl_after_high, (
        "SL ratchet violated — SL moved against BUY position"
    )


def test_trending_trail_tightens_at_10pct_profit(make_position):
    """TRENDING: trail tightens to 0.75×ATR at +10% profit."""
    pos = make_position(
        regime_at_entry="TRENDING_UP",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # Trigger breakeven first
    engine.on_candle(current_price=85_500.0, current_atr=120.0)

    # Price at +10%
    price_10pct = 85_000.0 * 1.10   # = 93_500.0
    sig = engine.on_candle(current_price=price_10pct, current_atr=120.0)

    assert sig.action == "UPDATE_SL"
    expected_sl = price_10pct - 0.75 * 120.0   # = 93_410.0
    assert abs(sig.new_sl - expected_sl) < 0.01, (
        f"Trail at +10% should be 0.75×ATR below price. expected={expected_sl}, got={sig.new_sl}"
    )


def test_to_dict_round_trips_state(make_position):
    """to_dict() serialises state correctly for persistence."""
    pos = make_position(regime_at_entry="TRENDING_UP", entry_price=85_000.0)
    engine = ExitEngine(pos)
    engine.on_candle(current_price=85_500.0, current_atr=120.0)

    d = engine.to_dict()
    assert d["exit_state"] == ExitEngine.STATE_TRAILING
    assert d["sl_price"] >= 85_000.0
    assert len(d["state_history"]) >= 1
    assert d["candles_open"] == 1


def test_exit_engine_idempotent_after_exit(make_position):
    """Calling on_candle after EXIT returns cached exit signal."""
    pos = make_position(regime_at_entry="RANGING", entry_price=85_000.0, entry_atr=120.0)
    engine = ExitEngine(pos)

    # Force SL hit
    engine.on_candle(current_price=84_500.0, current_atr=120.0)
    assert engine.state == ExitEngine.STATE_EXIT

    # Call again — must not raise or change state
    sig = engine.on_candle(current_price=84_000.0, current_atr=120.0)
    assert sig.action == "EXIT"
    assert sig.exit_reason == "sl_hit"
    assert engine.state == ExitEngine.STATE_EXIT


def test_sell_side_sl_and_tp(make_position):
    """SELL position: SL above entry, TP below entry."""
    pos = make_position(
        side="SELL",
        regime_at_entry="TRENDING_DOWN",
        entry_price=85_000.0,
        entry_atr=120.0,
    )
    engine = ExitEngine(pos)

    # SL is above entry for SELL
    assert engine.sl_price > engine.entry_price

    # Price rises above SL → SL hit
    sig = engine.on_candle(current_price=engine.sl_price + 10.0, current_atr=120.0)
    assert sig.action == "EXIT"
    assert sig.exit_reason == "sl_hit"
