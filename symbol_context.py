"""Alpha-Scalp Bot – SymbolContext.

GP-S11: Per-symbol isolated state container.

Each traded symbol (BTC/USDT, ETH/USDT, SOL/USDT) gets exactly ONE
SymbolContext.  Components that are per-symbol live here; components
that are shared are injected at the call-site.

Component isolation rules (from CLAUDE.md):
  SymbolContext     NOT shared — one instance per symbol, fully isolated.
  FeatureCache      NOT shared — one per SymbolContext.
  OrderFlowCache    NOT shared — one per SymbolContext.
  ShadowTracker     NOT shared — one per SymbolContext.
                    BTC and ETH Beta distributions never mix.
  TournamentEngine  NOT shared — one per SymbolContext (wraps its ShadowTracker).
  StrategyRouter    NOT shared — one per SymbolContext.
  ExitEngine        NOT shared — one per open position, stored inside SymbolContext.
  AlphaEngine       SHARED — stateless, injected at call-site (reads only).
  RiskEngine        SHARED — equity_floor and kill_switch are global;
                    daily_pnl and consecutive_losses live in SymbolRiskState
                    (per-symbol) inside SymbolContext.

Multi-symbol activation rules (FIX-10 from CLAUDE.md):
  BTC/USDT:  ALWAYS                              → FULL_PIPELINE
  ETH/USDT:  BTC not trending                    → FULL_PIPELINE
             BTC is trending                     → SHADOW_ONLY
  SOL/USDT:  BTC not trending AND ETH not trending → FULL_PIPELINE
             otherwise                           → SHADOW_ONLY
  ALL symbols non-trending simultaneously        → global_cash_mode = True

"Trending" = regime in {TRENDING_UP, TRENDING_DOWN}.
"Not trending" = regime in {RANGING, VOLATILE, TRANSITION} or unknown.

FULL_PIPELINE  = SignalScoring → Tournament → Router → Risk → Execution.
SHADOW_ONLY    = ShadowTracker runs every candle, no live orders placed.
global_cash_mode = additional flag; even FULL_PIPELINE symbols skip live orders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from loguru import logger

from feature_cache import FeatureCache, FeatureSet, OrderFlowCache
from market_state import MarketState
from shadow_tracker import ShadowTracker
from strategy_router import StrategyRouter
from tournament_engine import TournamentEngine


# ---------------------------------------------------------------------------
# Activation mode
# ---------------------------------------------------------------------------

class ActivationMode(str, Enum):
    """Per-symbol pipeline activation level."""
    FULL_PIPELINE = "FULL_PIPELINE"   # SignalScoring → Tournament → Risk → Execution
    SHADOW_ONLY   = "SHADOW_ONLY"     # ShadowTracker only, no live orders


# Regime sets
_TRENDING_REGIMES: frozenset[str] = frozenset({"TRENDING_UP", "TRENDING_DOWN"})

# Known symbols (activation order)
_BTC = "BTC/USDT"
_ETH = "ETH/USDT"
_SOL = "SOL/USDT"


def _is_trending(regime: str) -> bool:
    """True when the symbol's regime is directionally trending."""
    return regime in _TRENDING_REGIMES


# ---------------------------------------------------------------------------
# Per-symbol risk counters
# ---------------------------------------------------------------------------

@dataclass
class SymbolRiskState:
    """Per-symbol mutable risk counters for the shared RiskEngine.

    RiskEngine (SHARED) manages global equity_floor and kill_switch.
    These per-symbol counters (daily_pnl, consecutive_losses) live inside
    SymbolContext so they never cross between instruments.
    """
    symbol: str
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    _last_daily_reset: float = field(default_factory=time.time, repr=False)

    def reset_daily(self) -> None:
        """Reset per-symbol counters at each UTC daily boundary."""
        self.daily_pnl = 0.0
        self._last_daily_reset = time.time()

    def record_pnl(self, pnl: float) -> None:
        """Update daily_pnl and consecutive_losses after a closed trade."""
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0


# ---------------------------------------------------------------------------
# SymbolContext
# ---------------------------------------------------------------------------

class SymbolContext:
    """Per-symbol isolated state container.

    One instance per symbol — NEVER reused or shared across symbols.
    BTC's SymbolContext never touches ETH's SymbolContext.

    Parameters
    ----------
    symbol:
        Normalised symbol string, e.g. "BTC/USDT".
    activation_mode:
        Initial activation level.  BTC defaults to FULL_PIPELINE;
        ETH/SOL passive-shadow symbols default to SHADOW_ONLY on startup.
    """

    def __init__(
        self,
        symbol: str,
        activation_mode: ActivationMode = ActivationMode.SHADOW_ONLY,
    ) -> None:
        self.symbol = symbol

        # ── Layer 1: Data (one instance per symbol) ──────────────────────
        self.market_state: MarketState = MarketState(symbol)
        self.feature_cache: FeatureCache = FeatureCache()
        self.order_flow_cache: OrderFlowCache = OrderFlowCache()

        # ── Layer 3: Tournament (isolated Beta distributions per symbol) ──
        self.shadow_tracker: ShadowTracker = ShadowTracker()
        self.tournament_engine: TournamentEngine = TournamentEngine(self.shadow_tracker)
        self.strategy_router: StrategyRouter = StrategyRouter(
            shadow_tracker=self.shadow_tracker,
            telegram=None,
        )

        # ── Layer 4: Open positions ──────────────────────────────────────
        # key = trade_id (str), value = ExitEngine instance
        # ExitEngine is created on trade entry and destroyed on exit.
        self.open_positions: dict[str, Any] = {}

        # ── Activation ───────────────────────────────────────────────────
        self._activation_mode: ActivationMode = activation_mode

        # ── Current candle state ─────────────────────────────────────────
        self.current_regime: str = "RANGING"
        self.candles_seen: int = 0
        self.last_feature_set: Optional[FeatureSet] = None

        # ── Per-symbol risk counters ──────────────────────────────────────
        self.risk_state: SymbolRiskState = SymbolRiskState(symbol=symbol)

        logger.info(
            "SymbolContext created | symbol={} mode={}",
            symbol, activation_mode.value,
        )

    # ── Activation mode ──────────────────────────────────────────────────────

    @property
    def activation_mode(self) -> ActivationMode:
        return self._activation_mode

    @activation_mode.setter
    def activation_mode(self, mode: ActivationMode) -> None:
        if mode != self._activation_mode:
            logger.info(
                "SymbolContext {}: activation {} → {}",
                self.symbol,
                self._activation_mode.value,
                mode.value,
            )
        self._activation_mode = mode

    @property
    def is_full_pipeline(self) -> bool:
        """True when this symbol runs the full live-trading pipeline."""
        return self._activation_mode == ActivationMode.FULL_PIPELINE

    @property
    def is_shadow_only(self) -> bool:
        """True when this symbol runs shadow simulation only (no live orders)."""
        return self._activation_mode == ActivationMode.SHADOW_ONLY

    # ── State updates ────────────────────────────────────────────────────────

    def record_candle(self, regime: str) -> None:
        """Advance candle counter and update current regime.

        Called once per closed candle in the main loop.
        """
        self.candles_seen += 1
        self.current_regime = regime

    def set_feature_set(self, fs: FeatureSet) -> None:
        """Cache the most recently computed FeatureSet for this symbol."""
        self.last_feature_set = fs

    # ── Open position management ─────────────────────────────────────────────

    def add_position(self, trade_id: str, exit_engine: Any) -> None:
        """Register an ExitEngine instance for a newly opened position."""
        self.open_positions[trade_id] = exit_engine
        logger.debug(
            "SymbolContext {}: position added trade_id={} total_open={}",
            self.symbol, trade_id, len(self.open_positions),
        )

    def remove_position(self, trade_id: str) -> Any | None:
        """Remove and return the ExitEngine when a position closes.

        Returns None if trade_id was not found (safe no-op).
        """
        ee = self.open_positions.pop(trade_id, None)
        if ee is not None:
            logger.debug(
                "SymbolContext {}: position removed trade_id={} remaining={}",
                self.symbol, trade_id, len(self.open_positions),
            )
        return ee

    def get_position(self, trade_id: str) -> Any | None:
        """Return the ExitEngine for trade_id, or None."""
        return self.open_positions.get(trade_id)

    @property
    def open_position_count(self) -> int:
        return len(self.open_positions)

    @property
    def has_open_positions(self) -> bool:
        return bool(self.open_positions)

    # ── Ready check ──────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        """True when the symbol has enough data to run the alpha pipeline.

        Requires:
          - WebSocket connected and book initialised (MarketState.is_ready)
          - At least 50 closed candles seen (BURN_IN_CANDLES)
        """
        return (
            self.market_state.is_ready
            and self.candles_seen >= 50
        )

    # ── Introspection ────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Quick state snapshot for logging and health monitoring."""
        return {
            "symbol":               self.symbol,
            "activation_mode":      self._activation_mode.value,
            "is_ready":             self.is_ready,
            "current_regime":       self.current_regime,
            "candles_seen":         self.candles_seen,
            "open_positions":       self.open_position_count,
            "daily_pnl":            round(self.risk_state.daily_pnl, 4),
            "consecutive_losses":   self.risk_state.consecutive_losses,
        }


# ---------------------------------------------------------------------------
# SymbolContextRegistry
# ---------------------------------------------------------------------------

class SymbolContextRegistry:
    """Registry of all per-symbol SymbolContext instances.

    Implements the multi-symbol activation rules (FIX-10):

      BTC/USDT: ALWAYS                               → FULL_PIPELINE
      ETH/USDT: BTC not trending                     → FULL_PIPELINE
                BTC is trending                      → SHADOW_ONLY
      SOL/USDT: BTC not trending AND ETH not trending → FULL_PIPELINE
                otherwise                            → SHADOW_ONLY
      ALL non-trending simultaneously                → global_cash_mode = True

    "Trending" = regime in {TRENDING_UP, TRENDING_DOWN}.

    The global_cash_mode flag is set separately from per-symbol activation
    modes.  It prevents live orders even when a symbol is FULL_PIPELINE.
    This flag must be checked in the main loop before calling OrderExecutor.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, SymbolContext] = {}
        self.global_cash_mode: bool = False

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, ctx: SymbolContext) -> None:
        """Register a SymbolContext.  Called once per symbol at startup."""
        self._contexts[ctx.symbol] = ctx
        logger.info(
            "SymbolContextRegistry: registered {} (mode={})",
            ctx.symbol, ctx.activation_mode.value,
        )

    def get(self, symbol: str) -> SymbolContext | None:
        """Return the context for *symbol*, or None if not registered."""
        return self._contexts.get(symbol)

    def __getitem__(self, symbol: str) -> SymbolContext:
        return self._contexts[symbol]

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._contexts

    def __len__(self) -> int:
        return len(self._contexts)

    def all_contexts(self) -> list[SymbolContext]:
        """All registered SymbolContext instances."""
        return list(self._contexts.values())

    # ── Activation routing ───────────────────────────────────────────────────

    def route_agent_activation(self) -> bool:
        """Update activation modes for all registered symbols.

        Must be called AFTER each symbol's current_regime has been updated
        for the current candle (i.e. after record_candle() runs).

        Returns
        -------
        True if global cash mode is now active (all symbols non-trending).
        """
        btc = self._contexts.get(_BTC)
        eth = self._contexts.get(_ETH)
        sol = self._contexts.get(_SOL)

        # ── BTC: always full pipeline ────────────────────────────────────
        if btc is not None:
            btc.activation_mode = ActivationMode.FULL_PIPELINE

        btc_trending = _is_trending(btc.current_regime) if btc is not None else False

        # ── ETH: full pipeline only when BTC is not trending ────────────
        if eth is not None:
            eth.activation_mode = (
                ActivationMode.FULL_PIPELINE
                if not btc_trending
                else ActivationMode.SHADOW_ONLY
            )

        eth_trending = _is_trending(eth.current_regime) if eth is not None else False

        # ── SOL: full pipeline only when both BTC and ETH are not trending
        if sol is not None:
            sol.activation_mode = (
                ActivationMode.FULL_PIPELINE
                if (not btc_trending and not eth_trending)
                else ActivationMode.SHADOW_ONLY
            )

        # ── FIX-10: global cash mode when ALL symbols are non-trending ───
        all_non_trending = all(
            not _is_trending(c.current_regime)
            for c in self._contexts.values()
        )
        self.global_cash_mode = bool(self._contexts) and all_non_trending

        if self.global_cash_mode:
            logger.info(
                "SymbolContextRegistry: GLOBAL_CASH_MODE — "
                "all {} symbols non-trending",
                len(self._contexts),
            )

        return self.global_cash_mode

    # ── Introspection ────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """Full registry state snapshot."""
        return {
            "global_cash_mode": self.global_cash_mode,
            "symbol_count":     len(self._contexts),
            "symbols": {
                sym: ctx.summary()
                for sym, ctx in self._contexts.items()
            },
        }
