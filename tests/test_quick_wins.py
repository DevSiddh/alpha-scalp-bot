"""GP Step 3 — Quick Wins tests (10 tests).

Covers:
  - Candle spike filter (range > N×ATR → HOLD / filter_reason=candle_spike)
  - ATR-zero guard (atr=0 → HOLD / filter_reason=atr_zero)
  - Spread guard standalone (check_spread_guard pass / fail / invalid)
  - Max signal weight validation (clamp high / clamp zero / pass valid)
  - Spike filter graceful skip when high/low not populated
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from signal_scoring import SignalScoring, ScoringResult, DEFAULT_WEIGHTS
from risk_engine import RiskEngine
from feature_cache import FeatureSet
from alpha_engine import AlphaEngine, AlphaVotes
import config as cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(start_balance: float = 10_000.0) -> RiskEngine:
    exchange = MagicMock()
    exchange.price_to_precision.side_effect = lambda sym, price: price
    with patch.object(RiskEngine, "_fetch_usdt_balance", return_value=start_balance):
        engine = RiskEngine(exchange)
    engine.daily_start_balance = start_balance
    engine._cached_balance = start_balance
    engine._cache_timestamp = time.monotonic()
    return engine


def _scoring() -> SignalScoring:
    return SignalScoring.__new__(SignalScoring)


def _flat_votes() -> AlphaVotes:
    """All-HOLD votes — score stays at 0."""
    return AlphaVotes()


def _base_features(**kwargs) -> FeatureSet:
    """Valid base candle: atr_ma50 set so volatility filter won't trip."""
    defaults = dict(
        close=50_000.0, high=50_200.0, low=49_800.0,
        atr=200.0, atr_ma50=200.0, regime="RANGING",
    )
    defaults.update(kwargs)
    return FeatureSet(**defaults)


# ---------------------------------------------------------------------------
# Spike Filter
# ---------------------------------------------------------------------------

def test_spike_filter_blocks_candle_spike():
    """Candle range = 4×ATR must trigger spike filter → HOLD, filter_reason=candle_spike."""
    sc = SignalScoring()
    # range = 400, ATR = 100, mult = 3.0 → 400 > 300 → spike
    fs = _base_features(high=50_200.0, low=49_800.0, atr=100.0, atr_ma50=100.0)
    assert fs.high - fs.low == 400.0
    result = sc.score(_flat_votes(), fs)
    assert result.action == "HOLD"
    assert result.filter_reason == "candle_spike"


def test_spike_filter_passes_normal_candle():
    """Candle range = 1.5×ATR must NOT trigger spike filter."""
    sc = SignalScoring()
    # range = 300, ATR = 200, mult = 3.0 → 300 < 600 → OK
    fs = _base_features(high=50_150.0, low=49_850.0, atr=200.0, atr_ma50=200.0)
    result = sc.score(_flat_votes(), fs)
    assert result.filter_reason != "candle_spike"


def test_spike_filter_skips_when_high_low_zero():
    """If high=0 and low=0, spike check must be skipped gracefully (no false block)."""
    sc = SignalScoring()
    fs = _base_features(high=0.0, low=0.0, atr=200.0, atr_ma50=200.0)
    result = sc.score(_flat_votes(), fs)
    assert result.filter_reason != "candle_spike"


# ---------------------------------------------------------------------------
# ATR-zero Guard
# ---------------------------------------------------------------------------

def test_atr_zero_returns_hold():
    """ATR=0 must return HOLD with filter_reason=atr_zero before any scoring."""
    sc = SignalScoring()
    fs = _base_features(atr=0.0)
    result = sc.score(_flat_votes(), fs)
    assert result.action == "HOLD"
    assert result.filter_reason == "atr_zero"


# ---------------------------------------------------------------------------
# Spread Guard (standalone)
# ---------------------------------------------------------------------------

def test_spread_guard_blocks_wide_spread():
    """Spread > MAX_SPREAD_BPS must be blocked."""
    engine = _make_engine()
    # MAX_SPREAD_BPS default = 20. Create a 30bps spread.
    mid = 50_000.0
    ask = mid * (1 + 0.0015)   # +15bps
    bid = mid * (1 - 0.0015)   # -15bps  → total 30bps
    allowed, reason = engine.check_spread_guard(ask, bid)
    assert not allowed
    assert "spread_too_wide" in reason


def test_spread_guard_passes_tight_spread():
    """Spread < MAX_SPREAD_BPS must pass."""
    engine = _make_engine()
    mid = 50_000.0
    ask = mid * (1 + 0.0005)   # +5bps
    bid = mid * (1 - 0.0005)   # -5bps  → total 10bps
    allowed, reason = engine.check_spread_guard(ask, bid)
    assert allowed
    assert reason == "ok"


def test_spread_guard_invalid_prices():
    """ask=0 must return False with reason=invalid_prices."""
    engine = _make_engine()
    allowed, reason = engine.check_spread_guard(0.0, 49_990.0)
    assert not allowed
    assert reason == "invalid_prices"


# ---------------------------------------------------------------------------
# Max Signal Weight Validation
# ---------------------------------------------------------------------------

def test_max_weight_validation_clamps_excessive():
    """Weight > MAX_WEIGHT must be clamped down to MAX_WEIGHT."""
    sc = SignalScoring()
    weights_in = {"ema_cross": 5.0, "rsi_zone": 1.0}  # 5.0 > MAX_WEIGHT=3.0
    result = sc._validate_weights(weights_in)
    assert result["ema_cross"] == cfg.MAX_WEIGHT
    assert result["rsi_zone"] == 1.0


def test_max_weight_validation_clamps_zero_or_negative():
    """Weight <= 0 must be clamped up to MIN_WEIGHT."""
    sc = SignalScoring()
    weights_in = {"rsi_zone": 0.0, "macd_cross": -1.0}
    result = sc._validate_weights(weights_in)
    assert result["rsi_zone"] == cfg.MIN_WEIGHT
    assert result["macd_cross"] == cfg.MIN_WEIGHT


def test_max_weight_validation_passes_valid():
    """Weights inside [MIN_WEIGHT, MAX_WEIGHT] must pass through unchanged."""
    sc = SignalScoring()
    weights_in = {"ema_cross": 1.4, "swing_bias": 1.6}
    result = sc._validate_weights(weights_in)
    assert result["ema_cross"] == 1.4
    assert result["swing_bias"] == 1.6
