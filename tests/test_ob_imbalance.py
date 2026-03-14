"""Tests for P1-9: Order Book Imbalance Signal."""

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

# We need to mock heavy dependencies before importing
import sys

# Mock modules that feature_cache imports but aren't needed for OB tests
mock_modules = {
    'pandas': MagicMock(),
    'tinybrain': MagicMock(),
    'config': MagicMock(),
    'loguru': MagicMock(),
    'strategy': MagicMock(),
    'numpy': MagicMock(),
    'httpx': MagicMock(),
}
# Set config attributes that feature_cache needs
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
    """Test that FeatureSet has the new OB fields with correct defaults."""

    def test_defaults(self):
        fs = FeatureSet()
        assert fs.ob_imbalance == 0.0
        assert fs.ob_bid_depth == 0.0
        assert fs.ob_ask_depth == 0.0


class TestOBImbalanceVote:
    """Test AlphaEngine ob_imbalance vote logic."""

    def test_bid_heavy_buy(self):
        """bid_depth >> ask_depth (imbalance > 0.3) -> BUY, strength=0.6"""
        fs = FeatureSet()
        fs.ob_imbalance = 0.8  # strongly bid-heavy

        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)

        assert votes.ob_imbalance.direction == "BUY"
        assert votes.ob_imbalance.strength == pytest.approx(0.6)
        assert "bid-heavy" in votes.ob_imbalance.reason.lower()

    def test_ask_heavy_sell(self):
        """ask_depth >> bid_depth (imbalance < -0.3) -> SELL, strength=0.6"""
        fs = FeatureSet()
        fs.ob_imbalance = -0.8  # strongly ask-heavy

        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)

        assert votes.ob_imbalance.direction == "SELL"
        assert votes.ob_imbalance.strength == pytest.approx(0.6)
        assert "ask-heavy" in votes.ob_imbalance.reason.lower()

    def test_balanced_hold(self):
        """balanced book (imbalance near 0) -> HOLD, strength=0.0"""
        fs = FeatureSet()
        fs.ob_imbalance = 0.05  # nearly balanced

        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)

        assert votes.ob_imbalance.direction == "HOLD"
        assert votes.ob_imbalance.strength == pytest.approx(0.0)

    def test_empty_book_default_zero_no_crash(self):
        """empty order book -> ob_imbalance defaults to 0.0, no crash"""
        fs = FeatureSet()
        # ob_imbalance stays at default 0.0

        ae = AlphaEngine()
        votes = ae.generate_votes(features=fs)

        assert votes.ob_imbalance.direction == "HOLD"
        assert votes.ob_imbalance.strength == 0.0

    def test_ob_imbalance_in_as_dict(self):
        """ob_imbalance appears in AlphaVotes.as_dict()"""
        ae = AlphaEngine()
        fs = FeatureSet()
        fs.ob_imbalance = 0.5
        votes = ae.generate_votes(features=fs)
        d = votes.as_dict()
        assert "ob_imbalance" in d
        assert d["ob_imbalance"] == 1  # BUY -> +1

    def test_ob_imbalance_in_get_all_votes(self):
        """ob_imbalance appears in AlphaVotes.get_all_votes()"""
        ae = AlphaEngine()
        fs = FeatureSet()
        votes = ae.generate_votes(features=fs)
        all_votes = votes.get_all_votes()
        assert "ob_imbalance" in all_votes
        assert isinstance(all_votes["ob_imbalance"], Vote)
