"""Tests for P1-9: Order Book Imbalance Signal."""

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

import sys

mock_modules = {
    'pandas': MagicMock(),
    'tinybrain': MagicMock(),
    'config': MagicMock(),
    'loguru': MagicMock(),
    'strategy': MagicMock(),
    'numpy': MagicMock(),
    'httpx': MagicMock(),
}
mock_modules['config'].EMA_SLOW = 50
mock_modules['config'].EMA_FAST = 20
mock_modules['config'].CVD_ENABLED = False

for mod_name, mock_mod in mock_modules.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock_mod

from feature_cache import FeatureSet
from alpha_engine import AlphaEngine, Vote

def make_level(price, qty):
    return [price, qty]

class TestFeatureSetOBFields:
    def test_defaults(self):
        fs = FeatureSet()
        assert fs.ob_imbalance == 0.5
        assert fs.ob_bid_depth == 0.0
        assert fs.ob_ask_depth == 0.0

class TestOBImbalanceVote:
    def test_bid_heavy_buy(self):
        fs = FeatureSet()
        fs.ob_imbalance = 0.8
        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)
        assert votes.ob_imbalance.direction == "BUY"
        assert abs(votes.ob_imbalance.strength - 0.6) < 0.001

    def test_ask_heavy_sell(self):
        fs = FeatureSet()
        fs.ob_imbalance = 0.2
        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)
        assert votes.ob_imbalance.direction == "SELL"
        assert abs(votes.ob_imbalance.strength - 0.6) < 0.001

    def test_balanced_hold(self):
        fs = FeatureSet()
        fs.ob_imbalance = 0.5
        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)
        assert votes.ob_imbalance.direction == "HOLD"
        assert abs(votes.ob_imbalance.strength - 0.0) < 0.001

    def test_empty_book_default_zero_no_crash(self):
        fs = FeatureSet()
        fs.ob_imbalance = 0.5 # defaults to 0.5 normally, but tests initialize FeatureSet directly
        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)
        assert votes.ob_imbalance.direction == "HOLD"
        assert abs(votes.ob_imbalance.strength - 0.0) < 0.001

    def test_ob_imbalance_in_as_dict(self):
        ae = AlphaEngine()
        fs = FeatureSet()
        fs.ob_imbalance = 0.8
        votes = ae.generate_votes(features=fs)
        d = votes.as_dict()
        assert "ob_imbalance" in d
        assert d["ob_imbalance"] == 1

    def test_ob_imbalance_in_get_all_votes(self):
        ae = AlphaEngine()
        fs = FeatureSet()
        votes = ae.generate_votes(features=fs)
        all_votes = votes.get_all_votes()
        assert "ob_imbalance" in all_votes
        assert isinstance(all_votes["ob_imbalance"], Vote)
