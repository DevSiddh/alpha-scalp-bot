import pytest
import time
from unittest.mock import MagicMock

from alpha_engine import AlphaEngine, AlphaVotes, Vote
from feature_cache import FeatureSet
from signal_scoring import SignalScoring, ScoringResult
from risk_engine import RiskEngine
import config as cfg

def test_ob_imbalance_buy():
    engine = AlphaEngine()
    features = FeatureSet(ob_imbalance=0.66)
    votes = engine.generate_votes(features)
    assert votes.ob_imbalance.direction == "BUY"
    assert votes.ob_imbalance.strength == 0.6

def test_ob_imbalance_sell():
    engine = AlphaEngine()
    features = FeatureSet(ob_imbalance=0.34)
    votes = engine.generate_votes(features)
    assert votes.ob_imbalance.direction == "SELL"
    assert votes.ob_imbalance.strength == 0.6

def test_ob_imbalance_hold():
    engine = AlphaEngine()
    features = FeatureSet(ob_imbalance=0.50)
    votes = engine.generate_votes(features)
    assert votes.ob_imbalance.direction == "HOLD"

def test_trade_aggression_buy():
    engine = AlphaEngine()
    features = FeatureSet(trade_aggression_ratio=0.61)
    votes = engine.generate_votes(features)
    assert votes.trade_aggression.direction == "BUY"
    assert votes.trade_aggression.strength == 0.7

def test_trade_aggression_sell():
    engine = AlphaEngine()
    features = FeatureSet(trade_aggression_ratio=0.39)
    votes = engine.generate_votes(features)
    assert votes.trade_aggression.direction == "SELL"
    assert votes.trade_aggression.strength == 0.7

def test_liquidity_sweep_bull():
    engine = AlphaEngine()
    features = FeatureSet(liquidity_sweep_bull=True)
    votes = engine.generate_votes(features)
    assert votes.liquidity_sweep.direction == "BUY"
    assert votes.liquidity_sweep.strength == 0.85

def test_liquidity_sweep_bear():
    engine = AlphaEngine()
    features = FeatureSet(liquidity_sweep_bear=True)
    votes = engine.generate_votes(features)
    assert votes.liquidity_sweep.direction == "SELL"
    assert votes.liquidity_sweep.strength == 0.85

def test_sweep_cooldown():
    engine = AlphaEngine()
    features = FeatureSet(liquidity_sweep_bull=True)
    
    # First fire
    v1 = engine.generate_votes(features)
    assert v1.liquidity_sweep.direction == "BUY"
    
    # Second fire within cooldown should be HOLD
    v2 = engine.generate_votes(features)
    assert v2.liquidity_sweep.direction == "HOLD"

def test_sweep_fake_wick():
    # If wick is less than atr * 0.1, the feature cache does not trigger the sweep
    # Mocking FeatureSet to reflect this computed state
    engine = AlphaEngine()
    # Mocking condition where wick < atr*0.1 resulted in liquidity_sweep_bull=False
    features = FeatureSet(liquidity_sweep_bull=False, atr=2.0)
    
    votes = engine.generate_votes(features)
    assert votes.liquidity_sweep.direction == "HOLD"

def test_consensus_filter():
    scoring = SignalScoring()
    # Mock weights and votes
    scoring._weights = {
        "ema_cross": 1.0,
        "rsi_zone": 1.0
    }
    votes = AlphaVotes()
    votes.ema_cross = Vote("BUY", 1.0, "reason")
    votes.rsi_zone = Vote("SELL", 0.6, "reason")
    
    features = FeatureSet()
    
    # For total_score > threshold we need positive score = 1.0 - 0.6 = 0.4
    # Wait, if total_score < 3.0 it's HOLD anyway.
    # We should fake total_score calculation using weights
    scoring._weights["ema_cross"] = 6.0
    scoring._weights["rsi_zone"] = 2.0
    # Bull = 6.0, Bear = 2.0. Total = 4.0. Meets SCORE_THRESHOLD
    # Consensus = 6 / 8 = 0.75
    # Let's drop consensus < 0.65
    scoring._weights["ema_cross"] = 4.0
    scoring._weights["rsi_zone"] = 3.0
    # Bull = 4.0. Bear = 3.0. Total = 1.0
    # With Score Threshold = 3.0, it will HOLD blindly. Let's patch threshold.
    cfg.SCORE_THRESHOLD = 0.5
    cfg.CONSENSUS_THRESHOLD = 0.65
    
    # total_score = 1.0 > 0.5 (action BUY)
    # bull = 4, bear = 3 -> total_abs = 7
    # consensus = 4 / 7 = 0.57 < 0.65 -> action HOLD
    
    result = scoring.score(votes, features)
    assert result.action == "HOLD"

def test_sanity_guard_spread():
    risk = RiskEngine(MagicMock())
    cfg.MAX_SPREAD_BPS = 20
    # spread = 0.25 / 100.125 = 2.49e-3 = 24.9 bps > 20 bps
    status, reason, size = risk.sanity_guard(100.1, 100.1, 100.25, 100.0, time.time(), 10.0)
    assert status == False
    assert reason == "spread_too_wide"

def test_sanity_guard_stale():
    risk = RiskEngine(MagicMock())
    cfg.DATA_FRESHNESS_SECONDS = 5
    # time.time() - 10 means the data is 10 seconds old (> 5s)
    status, reason, size = risk.sanity_guard(100.1, 100.1, 100.1, 100.1, time.time() - 10, 10.0)
    assert status == False
    assert reason == "stale_data"

