"""GP Step 5 — SubStrategyManager tests (9 tests).

Covers:
  - Microstructure gate: passes with imbalanced order book
  - Microstructure gate: passes with aggressive takers
  - Microstructure gate: blocks when both readings are neutral
  - Swing-bias gate: passes when swing_bias agrees
  - Swing-bias gate: passes when swing_bias is HOLD
  - Swing-bias gate: blocks when swing_bias opposes action
  - select() returns Cash when microstructure gate fails
  - select() returns LiquiditySweepReversal when sweep is active
  - select() returns VWAP_MeanReversion in ranging regime with vwap vote
"""
from alpha_engine import AlphaVotes, Vote
from feature_cache import FeatureSet
from sub_strategy_manager import SubStrategyManager, CASH_STRATEGY


def _mgr() -> SubStrategyManager:
    return SubStrategyManager()


def _neutral_features(**overrides) -> FeatureSet:
    """Neutral microstructure — both gates at 0.5 by default."""
    defaults = dict(
        ob_imbalance=0.5,
        trade_aggression_ratio=0.5,
        regime="RANGING",
        close=50_000.0, high=50_200.0, low=49_800.0,
        atr=200.0, atr_ma50=200.0,
    )
    defaults.update(overrides)
    return FeatureSet(**defaults)


def _neutral_votes(**overrides) -> AlphaVotes:
    v = AlphaVotes()
    for attr, val in overrides.items():
        setattr(v, attr, val)
    return v


# ---------------------------------------------------------------------------
# Microstructure gate
# ---------------------------------------------------------------------------

def test_microstructure_gate_passes_imbalanced_book():
    """ob_imbalance = 0.80 (> 0.65) → gate passes."""
    mgr = _mgr()
    fs = _neutral_features(ob_imbalance=0.80)
    assert mgr.check_microstructure_gate(fs) is True


def test_microstructure_gate_passes_aggressive_takers():
    """trade_aggression_ratio = 0.25 (< 0.40) → gate passes."""
    mgr = _mgr()
    fs = _neutral_features(trade_aggression_ratio=0.25)
    assert mgr.check_microstructure_gate(fs) is True


def test_microstructure_gate_blocks_neutral():
    """Both ob_imbalance=0.5 and aggression=0.5 → gate blocked."""
    mgr = _mgr()
    fs = _neutral_features(ob_imbalance=0.5, trade_aggression_ratio=0.5)
    assert mgr.check_microstructure_gate(fs) is False


# ---------------------------------------------------------------------------
# Swing-bias gate
# ---------------------------------------------------------------------------

def test_swing_bias_gate_passes_aligned():
    """swing_bias=BUY, proposed=BUY → passes."""
    mgr = _mgr()
    votes = _neutral_votes(swing_bias=Vote("BUY", 0.7, "trend up"))
    assert mgr.check_swing_bias_gate(votes, "BUY") is True


def test_swing_bias_gate_passes_hold():
    """swing_bias=HOLD → passes regardless of proposed action."""
    mgr = _mgr()
    votes = _neutral_votes(swing_bias=Vote("HOLD", 0.0, "neutral"))
    assert mgr.check_swing_bias_gate(votes, "BUY") is True


def test_swing_bias_gate_blocks_opposing():
    """swing_bias=SELL, proposed=BUY → blocked."""
    mgr = _mgr()
    votes = _neutral_votes(swing_bias=Vote("SELL", 0.7, "trend down"))
    assert mgr.check_swing_bias_gate(votes, "BUY") is False


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

def test_select_returns_cash_when_microstructure_fails():
    """Neutral microstructure → select() must return Cash."""
    mgr = _mgr()
    fs = _neutral_features(ob_imbalance=0.5, trade_aggression_ratio=0.5)
    result = mgr.select(_neutral_votes(), fs)
    assert result.is_cash


def test_select_sweep_reversal_when_sweep_active():
    """liquidity_sweep=BUY + non-neutral book → LiquiditySweepReversal selected."""
    mgr = _mgr()
    fs = _neutral_features(ob_imbalance=0.80, regime="TRENDING_UP")
    votes = _neutral_votes(
        liquidity_sweep=Vote("BUY", 0.85, "sweep"),
        ob_imbalance=Vote("BUY", 0.6, "imbalance"),
    )
    result = mgr.select(votes, fs)
    assert result.name == "LiquiditySweepReversal"


def test_select_vwap_mean_reversion_in_ranging():
    """vwap_cross=BUY in RANGING + non-neutral book → VWAP_MeanReversion selected."""
    mgr = _mgr()
    fs = _neutral_features(ob_imbalance=0.72, regime="RANGING")
    votes = _neutral_votes(vwap_cross=Vote("BUY", 0.7, "above vwap"))
    result = mgr.select(votes, fs)
    assert result.name == "VWAP_MeanReversion"
