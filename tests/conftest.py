# tests/conftest.py
# Shared fixtures — prevents test drift between files.

# Ensure repo root is on sys.path so bot modules are importable in CI.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
# Matches Grand Prix architecture (Steps 1-8 complete).
#
# Available fixtures:
#   make_feature_set   — FeatureSet factory (real dataclass)
#   make_alpha_votes   — AlphaVotes factory with per-signal Vote objects
#   make_shadow_tracker — real ShadowTracker seeded with ghost trades
#   mock_telegram      — TelegramAlerts mock
#   make_position      — open position dict for ExitEngine tests
#   base_config        — canonical config values (matches current .env defaults)

import pytest
from unittest.mock import AsyncMock, MagicMock

from alpha_engine import AlphaVotes, Vote
from feature_cache import FeatureSet
from shadow_tracker import ShadowTracker


# ── FeatureSet factory ───────────────────────────────────────────────────────

@pytest.fixture
def make_feature_set():
    """Factory fixture — returns a real FeatureSet with sensible defaults.

    Usage:
        fs = make_feature_set(regime="VOLATILE", atr=150.0)
    """
    def _make(
        symbol="BTC/USDT",
        close=85_000.0,
        high=85_200.0,
        low=84_800.0,
        atr=120.0,
        atr_ma50=120.0,
        regime="TRENDING_UP",
        ob_imbalance=0.70,
        trade_aggression_ratio=0.65,
        **overrides,
    ):
        defaults = dict(
            close=close,
            high=high,
            low=low,
            atr=atr,
            atr_ma50=atr_ma50,
            regime=regime,
            ob_imbalance=ob_imbalance,
            trade_aggression_ratio=trade_aggression_ratio,
        )
        defaults.update(overrides)
        return FeatureSet(**defaults)
    return _make


# ── AlphaVotes factory ───────────────────────────────────────────────────────

@pytest.fixture
def make_alpha_votes():
    """Factory fixture — returns a real AlphaVotes with per-signal Vote objects.

    Usage:
        votes = make_alpha_votes(bb_squeeze="BUY", vwap_cross="SELL")
        # Any Phase-1 signal can be overridden with "BUY", "SELL", or "HOLD"
    """
    PHASE1 = [
        "bb_squeeze", "vwap_cross", "liquidity_sweep",
        "trade_aggression", "ob_imbalance", "mtf_bias", "funding_bias",
    ]

    def _make(**signal_overrides):
        votes = AlphaVotes()
        for signal in PHASE1:
            direction = signal_overrides.get(signal, "HOLD")
            strength = 0.75 if direction != "HOLD" else 0.0
            setattr(votes, signal, Vote(direction, strength, f"fixture_{signal}"))
        return votes
    return _make


# ── ShadowTracker factory ────────────────────────────────────────────────────

@pytest.fixture
def make_shadow_tracker():
    """Factory fixture — returns a real ShadowTracker seeded with ghost trades.

    Uses varying exit prices so FIX-1 (div-by-zero guard) does not suppress
    rewards. Strategies default to the standard 5 sub-strategies.

    Usage:
        tracker = make_shadow_tracker(wins=10, losses=2)
        tracker = make_shadow_tracker(
            strategies=["Breakout"],
            wins=5,
            losses=0,
        )
    """
    DEFAULT_STRATEGIES = [
        "LiquiditySweepReversal",
        "Breakout",
        "TrendPullback",
        "VWAP_MeanReversion",
        "OrderFlowMomentum",
    ]

    def _make(strategies=None, wins=5, losses=0):
        tracker = ShadowTracker()
        names = strategies or DEFAULT_STRATEGIES
        for name in names:
            for i in range(wins):
                exit_p = 85_000.0 + (i + 1) * 500   # varying profits
                gid = tracker.open_ghost(name, entry_price=85_000.0, side="BUY", size=0.01)
                tracker.close_ghost(gid, exit_price=exit_p)
            for i in range(losses):
                exit_p = 85_000.0 - (i + 1) * 500   # varying losses
                gid = tracker.open_ghost(name, entry_price=85_000.0, side="BUY", size=0.01)
                tracker.close_ghost(gid, exit_price=exit_p)
        return tracker
    return _make


# ── Telegram mock ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_telegram():
    """TelegramAlerts mock — all async send methods are no-ops."""
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=None)
    tg.send_trade_alert = AsyncMock(return_value=None)
    tg.send_close_alert = AsyncMock(return_value=None)
    tg.enabled = False   # prevents accidental real HTTP calls
    return tg


# ── Open position factory ────────────────────────────────────────────────────

@pytest.fixture
def make_position():
    """Factory for open position dicts consumed by ExitEngine.

    Defaults represent a BUY on BTC/USDT in TRENDING_UP with
    ATR-based SL (2×ATR below entry) and TP (3×ATR above entry).

    Usage:
        pos = make_position(side="SELL", entry_price=85000, regime_at_entry="RANGING")
    """
    def _make(
        position_id="TEST_POS_001",
        symbol="BTC/USDT",
        side="BUY",
        size=0.002,
        entry_price=85_000.0,
        entry_atr=120.0,
        regime_at_entry="TRENDING_UP",
        strategy="Breakout",
        candles_open=0,
    ):
        atr_sl_mult = 2.0
        atr_tp_mult = 3.0
        if side == "BUY":
            sl = entry_price - atr_sl_mult * entry_atr
            tp = entry_price + atr_tp_mult * entry_atr
        else:
            sl = entry_price + atr_sl_mult * entry_atr
            tp = entry_price - atr_tp_mult * entry_atr

        return {
            "position_id":      position_id,
            "symbol":           symbol,
            "side":             side,
            "size":             size,
            "entry_price":      entry_price,
            "entry_atr":        entry_atr,
            "regime_at_entry":  regime_at_entry,
            "strategy":         strategy,
            "candles_open":     candles_open,
            "sl_price":         sl,
            "tp_price":         tp,
            "exit_state":       0,       # State 0 = ENTRY
            "state_history":    [],
        }
    return _make


# ── Canonical config reference ───────────────────────────────────────────────

@pytest.fixture
def base_config():
    """Current canonical config values — matches CLAUDE.md + .env defaults.

    Use as a reference in tests that need to assert against config-driven
    thresholds without importing config.py directly.
    """
    return {
        # Tournament
        "THOMPSON_NONE_THRESHOLD":      0.50,   # updated from 0.45
        "BURN_IN_CANDLES":              50,
        "DISTINCT_REGIMES_REQUIRED":    2,
        "VELOCITY_CHECK_THRESHOLD":     0.30,
        "CORRELATION_BLOCK_THRESHOLD":  0.85,

        # Risk
        "RISK_PER_TRADE":               0.02,
        "DAILY_DRAWDOWN_LIMIT":         0.03,
        "EQUITY_FLOOR_PCT":             0.80,
        "THREE_STRIKE_COOLDOWN_MINUTES": 90,
        "MAX_OPEN_POSITIONS":           3,
        "MIN_SL_DISTANCE_PCT":          0.0015,

        # Leverage ceilings
        "LEVERAGE_CEILING_TRENDING":    5.0,
        "LEVERAGE_CEILING_RANGING":     3.0,
        "LEVERAGE_CEILING_VOLATILE":    2.0,
        "LEVERAGE_CEILING_TRANSITION":  2.0,

        # Thompson confidence multipliers
        "CONFIDENCE_MULT_LOW":          0.50,
        "CONFIDENCE_MULT_MID":          0.75,
        "CONFIDENCE_MULT_HIGH":         1.00,

        # Exit engine
        "ATR_SL_MULTIPLIER":            2.0,
        "ATR_TP_MULTIPLIER":            3.0,
        "SCALP_TRAIL_ACTIVATE_PCT":     0.005,
        "SCALP_TRAIL_ATR_MULT":         2.0,
        "TRAIL_TIGHTEN_AT_5PCT":        1.0,
        "TRAIL_TIGHTEN_AT_10PCT":       0.75,
        "RR_RANGING":                   1.5,
        "RR_TRENDING":                  2.0,
        "RR_VOLATILE":                  1.8,

        # Fees
        "TAKER_FEE":                    0.001,  # 0.1% per side

        # Symbols
        "SCALP_SYMBOLS":                "BTC/USDT",
        "PASSIVE_SHADOW_SYMBOLS":       "ETH/USDT,SOL/USDT",
    }
