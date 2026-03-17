"""GP Step 4 — Signal Registry tests (8 tests).

Covers:
  - All 17 signals present in the registry
  - Default weights match legacy signal_scoring.DEFAULT_WEIGHTS
  - Exactly 7 phase-1 signals (per CLAUDE.md spec)
  - Phase-2 gate: phase-2 signal disabled when live_trade_count < 200
  - Phase-2 gate: phase-2 signal enabled at live_trade_count >= 200
  - Built-in regime disable (bb_bounce in TRENDING_DOWN)
  - External DISABLED_SIGNALS_BY_REGIME respected
  - SignalScoring.DEFAULT_WEIGHTS now sourced from registry
"""
import pytest

from signal_registry import SignalRegistry, PHASE1_SIGNALS, LIVE_TRADE_PHASE2_THRESHOLD
from signal_scoring import DEFAULT_WEIGHTS


EXPECTED_PHASE1 = {"bb_squeeze", "vwap_cross", "liquidity_sweep",
                   "trade_aggression", "ob_imbalance", "mtf_bias", "funding_bias"}


def test_registry_has_all_17_signals():
    assert len(SignalRegistry.all_names()) == 17


def test_default_weights_match_signal_scoring():
    """Registry defaults must be identical to signal_scoring.DEFAULT_WEIGHTS."""
    registry_weights = SignalRegistry.default_weights()
    assert registry_weights == DEFAULT_WEIGHTS


def test_phase1_signal_set_matches_spec():
    """Exactly 7 phase-1 signals, matching CLAUDE.md."""
    phase1 = set(SignalRegistry.phase1_names())
    assert phase1 == EXPECTED_PHASE1
    assert len(phase1) == 7


def test_phase2_signal_blocked_below_threshold():
    """ema_cross (phase2) must be disabled when live_trade_count < 200."""
    assert not SignalRegistry.is_enabled("ema_cross", "RANGING", 0)
    assert not SignalRegistry.is_enabled("ema_cross", "RANGING", 199)


def test_phase2_signal_enabled_at_threshold():
    """ema_cross must be enabled at exactly 200 live trades."""
    assert SignalRegistry.is_enabled("ema_cross", "RANGING", 200)
    assert SignalRegistry.is_enabled("ema_cross", "RANGING", 500)


def test_phase1_signal_always_enabled():
    """Phase-1 signals are never blocked by the trade-count gate."""
    for name in EXPECTED_PHASE1:
        assert SignalRegistry.is_enabled(name, "RANGING", 0), \
            f"{name} should be enabled in phase 1"


def test_builtin_regime_disable_bb_bounce():
    """bb_bounce has disabled_regimes=('TRENDING_DOWN',) built into its meta."""
    assert not SignalRegistry.is_enabled("bb_bounce", "TRENDING_DOWN", 200)
    assert SignalRegistry.is_enabled("bb_bounce", "RANGING", 200)


def test_external_disabled_by_regime():
    """External DISABLED_SIGNALS_BY_REGIME config is respected."""
    external = {"VOLATILE": ["ema_cross"]}
    # ema_cross normally enabled at 200 trades in RANGING...
    assert SignalRegistry.is_enabled("ema_cross", "RANGING", 200, external)
    # ...but disabled in VOLATILE via external config
    assert not SignalRegistry.is_enabled("ema_cross", "VOLATILE", 200, external)
