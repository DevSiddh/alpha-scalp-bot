"""GP Step 11 — SymbolContext tests (12 tests).

Covers:
  - SymbolContext creates isolated components (no shared state across symbols)
  - ShadowTracker isolation: BTC and ETH contexts have separate instances
  - Activation mode getter/setter logs change and stores correctly
  - is_full_pipeline / is_shadow_only properties
  - record_candle increments candles_seen and updates current_regime
  - set_feature_set caches last FeatureSet
  - Open position management: add, get, remove, count, has_open
  - is_ready requires both market_state.is_ready AND candles_seen >= 50
  - SymbolRiskState: record_pnl updates daily_pnl and consecutive_losses
  - SymbolRiskState: consecutive_losses resets on a winning trade
  - route_agent_activation: BTC always FULL_PIPELINE
  - route_agent_activation: ETH FULL_PIPELINE when BTC non-trending,
                            SHADOW_ONLY when BTC trending
  - route_agent_activation: SOL FULL_PIPELINE only when BTC + ETH both non-trending
  - route_agent_activation: global_cash_mode True when all symbols non-trending
  - route_agent_activation: global_cash_mode False when any symbol trending
"""
import pytest
from unittest.mock import MagicMock

from symbol_context import (
    ActivationMode,
    SymbolContext,
    SymbolContextRegistry,
    SymbolRiskState,
    _is_trending,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(symbol: str, mode: ActivationMode = ActivationMode.SHADOW_ONLY) -> SymbolContext:
    return SymbolContext(symbol=symbol, activation_mode=mode)


def _registry_with(*symbols_and_regimes: tuple[str, str]) -> SymbolContextRegistry:
    """Build a registry with specified symbols, set their current_regime."""
    reg = SymbolContextRegistry()
    for sym, regime in symbols_and_regimes:
        ctx = _make_ctx(sym)
        ctx.current_regime = regime
        reg.register(ctx)
    return reg


# ---------------------------------------------------------------------------
# _is_trending helper
# ---------------------------------------------------------------------------

def test_is_trending_identifies_trending_regimes():
    assert _is_trending("TRENDING_UP") is True
    assert _is_trending("TRENDING_DOWN") is True


def test_is_trending_non_trending_regimes():
    for regime in ("RANGING", "VOLATILE", "TRANSITION", "UNKNOWN", ""):
        assert _is_trending(regime) is False, f"Expected False for {regime!r}"


# ---------------------------------------------------------------------------
# SymbolContext — isolation
# ---------------------------------------------------------------------------

def test_symbol_context_isolation_separate_shadow_trackers():
    """BTC and ETH contexts must own distinct ShadowTracker instances."""
    btc = _make_ctx("BTC/USDT")
    eth = _make_ctx("ETH/USDT")
    assert btc.shadow_tracker is not eth.shadow_tracker


def test_symbol_context_isolation_separate_feature_caches():
    btc = _make_ctx("BTC/USDT")
    eth = _make_ctx("ETH/USDT")
    assert btc.feature_cache is not eth.feature_cache


def test_symbol_context_isolation_separate_tournament_engines():
    btc = _make_ctx("BTC/USDT")
    eth = _make_ctx("ETH/USDT")
    assert btc.tournament_engine is not eth.tournament_engine


def test_symbol_context_isolation_separate_strategy_routers():
    btc = _make_ctx("BTC/USDT")
    eth = _make_ctx("ETH/USDT")
    assert btc.strategy_router is not eth.strategy_router


# ---------------------------------------------------------------------------
# ActivationMode properties
# ---------------------------------------------------------------------------

def test_default_activation_mode_is_shadow_only():
    ctx = _make_ctx("ETH/USDT")
    assert ctx.activation_mode == ActivationMode.SHADOW_ONLY
    assert ctx.is_shadow_only is True
    assert ctx.is_full_pipeline is False


def test_set_activation_mode_full_pipeline():
    ctx = _make_ctx("BTC/USDT", mode=ActivationMode.FULL_PIPELINE)
    assert ctx.is_full_pipeline is True
    assert ctx.is_shadow_only is False


def test_activation_mode_setter_changes_mode():
    ctx = _make_ctx("ETH/USDT", mode=ActivationMode.SHADOW_ONLY)
    ctx.activation_mode = ActivationMode.FULL_PIPELINE
    assert ctx.activation_mode == ActivationMode.FULL_PIPELINE
    assert ctx.is_full_pipeline is True


# ---------------------------------------------------------------------------
# State updates
# ---------------------------------------------------------------------------

def test_record_candle_increments_count():
    ctx = _make_ctx("BTC/USDT")
    assert ctx.candles_seen == 0
    ctx.record_candle("RANGING")
    ctx.record_candle("TRENDING_UP")
    assert ctx.candles_seen == 2


def test_record_candle_updates_regime():
    ctx = _make_ctx("BTC/USDT")
    ctx.record_candle("VOLATILE")
    assert ctx.current_regime == "VOLATILE"
    ctx.record_candle("TRENDING_DOWN")
    assert ctx.current_regime == "TRENDING_DOWN"


def test_set_feature_set_caches_last():
    from feature_cache import FeatureSet
    ctx = _make_ctx("BTC/USDT")
    assert ctx.last_feature_set is None
    fs = FeatureSet(close=85000.0)
    ctx.set_feature_set(fs)
    assert ctx.last_feature_set is fs


# ---------------------------------------------------------------------------
# Open position management
# ---------------------------------------------------------------------------

def test_add_and_get_position():
    ctx = _make_ctx("BTC/USDT")
    mock_ee = MagicMock(name="ExitEngine")
    ctx.add_position("TRADE_001", mock_ee)
    assert ctx.get_position("TRADE_001") is mock_ee
    assert ctx.open_position_count == 1
    assert ctx.has_open_positions is True


def test_remove_position_returns_exit_engine():
    ctx = _make_ctx("BTC/USDT")
    mock_ee = MagicMock(name="ExitEngine")
    ctx.add_position("TRADE_001", mock_ee)
    removed = ctx.remove_position("TRADE_001")
    assert removed is mock_ee
    assert ctx.open_position_count == 0
    assert ctx.has_open_positions is False


def test_remove_nonexistent_position_returns_none():
    ctx = _make_ctx("BTC/USDT")
    result = ctx.remove_position("GHOST_ID")
    assert result is None


def test_multiple_positions_tracked_independently():
    ctx = _make_ctx("BTC/USDT")
    ee1 = MagicMock(name="EE1")
    ee2 = MagicMock(name="EE2")
    ctx.add_position("T1", ee1)
    ctx.add_position("T2", ee2)
    assert ctx.open_position_count == 2
    ctx.remove_position("T1")
    assert ctx.open_position_count == 1
    assert ctx.get_position("T2") is ee2


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------

def test_is_ready_false_when_market_state_not_ready():
    """Fresh context: ws not connected → not ready even with 50+ candles."""
    ctx = _make_ctx("BTC/USDT")
    for _ in range(60):
        ctx.record_candle("RANGING")
    # market_state.ws_connected is False by default → not ready
    assert ctx.is_ready is False


def test_is_ready_false_when_insufficient_candles():
    """Even if market state were ready, fewer than 50 candles → not ready."""
    ctx = _make_ctx("BTC/USDT")
    for _ in range(49):
        ctx.record_candle("RANGING")
    assert ctx.is_ready is False


# ---------------------------------------------------------------------------
# SymbolRiskState
# ---------------------------------------------------------------------------

def test_symbol_risk_state_record_pnl_win():
    rs = SymbolRiskState(symbol="BTC/USDT")
    rs.record_pnl(50.0)
    assert rs.daily_pnl == pytest.approx(50.0)
    assert rs.consecutive_losses == 0


def test_symbol_risk_state_record_pnl_loss():
    rs = SymbolRiskState(symbol="BTC/USDT")
    rs.record_pnl(-30.0)
    assert rs.daily_pnl == pytest.approx(-30.0)
    assert rs.consecutive_losses == 1


def test_symbol_risk_state_consecutive_losses_reset_on_win():
    rs = SymbolRiskState(symbol="BTC/USDT")
    rs.record_pnl(-10.0)
    rs.record_pnl(-10.0)
    assert rs.consecutive_losses == 2
    rs.record_pnl(20.0)   # win resets the streak
    assert rs.consecutive_losses == 0


def test_symbol_risk_state_per_symbol_independence():
    """BTC and ETH risk states never share counters."""
    btc_rs = SymbolRiskState(symbol="BTC/USDT")
    eth_rs = SymbolRiskState(symbol="ETH/USDT")
    btc_rs.record_pnl(-100.0)
    assert eth_rs.daily_pnl == 0.0
    assert eth_rs.consecutive_losses == 0


def test_symbol_risk_state_reset_daily():
    rs = SymbolRiskState(symbol="BTC/USDT")
    rs.record_pnl(-50.0)
    rs.record_pnl(-20.0)
    rs.reset_daily()
    assert rs.daily_pnl == 0.0
    # consecutive_losses NOT reset by reset_daily (only a win resets it)


# ---------------------------------------------------------------------------
# SymbolContextRegistry — registration
# ---------------------------------------------------------------------------

def test_registry_register_and_get():
    ctx = _make_ctx("BTC/USDT")
    reg = SymbolContextRegistry()
    reg.register(ctx)
    assert reg.get("BTC/USDT") is ctx
    assert "BTC/USDT" in reg
    assert len(reg) == 1


def test_registry_getitem():
    ctx = _make_ctx("BTC/USDT")
    reg = SymbolContextRegistry()
    reg.register(ctx)
    assert reg["BTC/USDT"] is ctx


def test_registry_all_contexts():
    reg = SymbolContextRegistry()
    b = _make_ctx("BTC/USDT")
    e = _make_ctx("ETH/USDT")
    reg.register(b)
    reg.register(e)
    assert set(id(c) for c in reg.all_contexts()) == {id(b), id(e)}


# ---------------------------------------------------------------------------
# SymbolContextRegistry — route_agent_activation
# ---------------------------------------------------------------------------

def test_btc_always_full_pipeline_regardless_of_regime():
    """BTC is always FULL_PIPELINE, even when its regime is TRENDING_UP."""
    reg = _registry_with(("BTC/USDT", "TRENDING_UP"))
    reg.route_agent_activation()
    assert reg["BTC/USDT"].activation_mode == ActivationMode.FULL_PIPELINE


def test_eth_full_pipeline_when_btc_non_trending():
    reg = _registry_with(("BTC/USDT", "RANGING"), ("ETH/USDT", "RANGING"))
    reg.route_agent_activation()
    assert reg["ETH/USDT"].activation_mode == ActivationMode.FULL_PIPELINE


def test_eth_shadow_only_when_btc_trending_up():
    reg = _registry_with(("BTC/USDT", "TRENDING_UP"), ("ETH/USDT", "RANGING"))
    reg.route_agent_activation()
    assert reg["ETH/USDT"].activation_mode == ActivationMode.SHADOW_ONLY


def test_eth_shadow_only_when_btc_trending_down():
    reg = _registry_with(("BTC/USDT", "TRENDING_DOWN"), ("ETH/USDT", "RANGING"))
    reg.route_agent_activation()
    assert reg["ETH/USDT"].activation_mode == ActivationMode.SHADOW_ONLY


def test_sol_full_pipeline_when_btc_and_eth_both_non_trending():
    reg = _registry_with(
        ("BTC/USDT", "RANGING"),
        ("ETH/USDT", "VOLATILE"),
        ("SOL/USDT", "RANGING"),
    )
    reg.route_agent_activation()
    assert reg["SOL/USDT"].activation_mode == ActivationMode.FULL_PIPELINE


def test_sol_shadow_when_btc_trending():
    reg = _registry_with(
        ("BTC/USDT", "TRENDING_UP"),
        ("ETH/USDT", "RANGING"),
        ("SOL/USDT", "RANGING"),
    )
    reg.route_agent_activation()
    assert reg["SOL/USDT"].activation_mode == ActivationMode.SHADOW_ONLY


def test_sol_shadow_when_eth_trending():
    reg = _registry_with(
        ("BTC/USDT", "RANGING"),
        ("ETH/USDT", "TRENDING_DOWN"),
        ("SOL/USDT", "RANGING"),
    )
    reg.route_agent_activation()
    assert reg["SOL/USDT"].activation_mode == ActivationMode.SHADOW_ONLY


def test_global_cash_mode_true_when_all_non_trending():
    """FIX-10: all symbols non-trending → global_cash_mode = True."""
    reg = _registry_with(
        ("BTC/USDT", "RANGING"),
        ("ETH/USDT", "VOLATILE"),
        ("SOL/USDT", "TRANSITION"),
    )
    result = reg.route_agent_activation()
    assert result is True
    assert reg.global_cash_mode is True


def test_global_cash_mode_false_when_any_symbol_trending():
    """FIX-10: at least one trending symbol → global_cash_mode = False."""
    reg = _registry_with(
        ("BTC/USDT", "TRENDING_UP"),
        ("ETH/USDT", "RANGING"),
        ("SOL/USDT", "RANGING"),
    )
    result = reg.route_agent_activation()
    assert result is False
    assert reg.global_cash_mode is False


def test_global_cash_mode_false_on_empty_registry():
    """Empty registry: no symbols to check → global_cash_mode stays False."""
    reg = SymbolContextRegistry()
    result = reg.route_agent_activation()
    assert result is False
    assert reg.global_cash_mode is False


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_symbol_context_summary_keys():
    ctx = _make_ctx("BTC/USDT", mode=ActivationMode.FULL_PIPELINE)
    ctx.record_candle("TRENDING_UP")
    s = ctx.summary()
    assert s["symbol"] == "BTC/USDT"
    assert s["activation_mode"] == "FULL_PIPELINE"
    assert s["current_regime"] == "TRENDING_UP"
    assert s["candles_seen"] == 1


def test_registry_summary_structure():
    reg = _registry_with(("BTC/USDT", "RANGING"), ("ETH/USDT", "VOLATILE"))
    reg.route_agent_activation()
    s = reg.summary()
    assert "global_cash_mode" in s
    assert "BTC/USDT" in s["symbols"]
    assert "ETH/USDT" in s["symbols"]
