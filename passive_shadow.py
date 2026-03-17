"""Alpha-Scalp Bot — PassiveShadowManager (GP Step 13).

Manages ETH/USDT and SOL/USDT in shadow-only mode to warm-start their
Beta distributions before these symbols go live.

Per-candle pipeline for each passive symbol (NO live orders EVER):
  1. FeatureCache.compute(df)              → FeatureSet
  2. AlphaEngine.generate_votes_with_funding() → AlphaVotes
  3. SubStrategyManager.select()          → SubStrategy (or Cash)
  4. _try_close_ghosts()                  → close prior ghosts vs candle high/low
  5. _open_ghost()                        → open new ghost if not Cash
  6. TournamentEngine.run_tournament()    → warm Thompson distributions
  7. StrategyRouter.tick()                → warm bench/promote logic
  8. SymbolContext.record_candle(regime)  → advance candle counter
  9. SymbolContextRegistry.route_agent_activation() → update activation modes

Ghost exit logic (ATR-based, per candle):
  BUY  ghost: SL = entry - ATR * SL_MULT,  TP = entry + ATR * TP_MULT
  SELL ghost: SL = entry + ATR * SL_MULT,  TP = entry - ATR * TP_MULT
  On next candle: check if candle low hit SL (loss) or high hit TP (win).
  Timeout after GHOST_MAX_CANDLES candles — close at current close price.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

import config as cfg
from alpha_engine import AlphaEngine, AlphaVotes
from feature_cache import FeatureCache, FeatureSet
from market_state import MarketState
from sub_strategy_manager import SubStrategyManager, CASH_STRATEGY
from symbol_context import ActivationMode, SymbolContext, SymbolContextRegistry


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GHOST_SL_MULT: float = 1.5   # ATR multiples for simulated stop-loss
GHOST_TP_MULT: float = 2.0   # ATR multiples for simulated take-profit
GHOST_MAX_CANDLES: int = 5   # force-close ghost after this many candles


# ---------------------------------------------------------------------------
# Pending ghost (one per open simulated trade)
# ---------------------------------------------------------------------------

@dataclass
class _PendingGhost:
    ghost_id:     str
    strategy:     str
    entry_price:  float
    side:         str         # "BUY" or "SELL"
    sl_price:     float
    tp_price:     float
    candles_alive: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dominant_side(votes: AlphaVotes) -> str:
    """Return BUY/SELL/HOLD based on plurality of non-HOLD votes."""
    buys = sells = 0
    for vote in votes.get_all_votes().values():
        if vote.direction == "BUY":
            buys += 1
        elif vote.direction == "SELL":
            sells += 1
    if buys > sells:
        return "BUY"
    if sells > buys:
        return "SELL"
    return "HOLD"


def _try_close_ghosts(
    pending: list[_PendingGhost],
    shadow_tracker: Any,
    candle_high: float,
    candle_low: float,
    candle_close: float,
) -> list[_PendingGhost]:
    """Check each pending ghost against the current candle and close if SL/TP hit.

    Returns the list of ghosts that are still open after this candle.
    """
    still_open: list[_PendingGhost] = []
    for g in pending:
        g.candles_alive += 1

        if g.side == "BUY":
            sl_hit = candle_low <= g.sl_price
            tp_hit = candle_high >= g.tp_price
        else:
            sl_hit = candle_high >= g.sl_price
            tp_hit = candle_low <= g.tp_price

        if tp_hit and not sl_hit:
            exit_price = g.tp_price
        elif sl_hit:
            exit_price = g.sl_price
        elif g.candles_alive >= GHOST_MAX_CANDLES:
            exit_price = candle_close   # timeout
        else:
            still_open.append(g)
            continue

        shadow_tracker.close_ghost(g.ghost_id, exit_price)
        logger.debug(
            "PassiveShadow ghost closed | strategy={} side={} entry={:.2f} exit={:.2f}",
            g.strategy, g.side, g.entry_price, exit_price,
        )

    return still_open


# ---------------------------------------------------------------------------
# PassiveShadowManager
# ---------------------------------------------------------------------------

class PassiveShadowManager:
    """Manages passive shadow tracking for ETH/USDT and SOL/USDT.

    One BinanceWSManager is created per passive symbol. The main BTC loop
    runs independently; this manager runs concurrently via asyncio.

    Parameters
    ----------
    registry : SymbolContextRegistry
        Shared registry. Passive symbol contexts are registered here.
    alpha_engine : AlphaEngine
        Shared stateless instance (safe to call concurrently for different symbols).
    sub_strategy_manager : SubStrategyManager
        Shared stateless instance.
    symbols : list[str]
        Passive symbols, e.g. ["ETH/USDT", "SOL/USDT"].
    timeframe : str
        Candle interval, should match main symbol (default "3m").
    """

    def __init__(
        self,
        registry: SymbolContextRegistry,
        alpha_engine: AlphaEngine,
        sub_strategy_manager: SubStrategyManager,
        symbols: list[str] | None = None,
        timeframe: str = "3m",
    ) -> None:
        self._registry = registry
        self._alpha = alpha_engine
        self._ssm = sub_strategy_manager
        self._symbols = symbols if symbols is not None else cfg.PASSIVE_SHADOW_SYMBOLS
        self._timeframe = timeframe
        self._tasks: list[asyncio.Task] = []
        # Per-symbol pending ghosts: symbol → list of _PendingGhost
        self._pending: dict[str, list[_PendingGhost]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create SymbolContext + BinanceWSManager for each passive symbol."""
        from ws_manager import BinanceWSManager   # lazy import to avoid circular deps

        for sym in self._symbols:
            if sym in self._registry:
                logger.info("PassiveShadow: {} already in registry — skipping", sym)
                continue

            ctx = SymbolContext(symbol=sym, activation_mode=ActivationMode.SHADOW_ONLY)
            self._registry.register(ctx)
            self._pending[sym] = []

            market_state = MarketState(
                symbol=sym,
                candle_history=cfg.WS_CANDLE_HISTORY,
                book_depth=cfg.WS_BOOK_DEPTH,
                price_jump_threshold_bps=cfg.WS_PRICE_JUMP_BPS,
            )
            ctx.market_state = market_state

            ws = BinanceWSManager(
                state=market_state,
                interval=self._timeframe,
                book_depth_limit=cfg.WS_BOOK_DEPTH * 50,
                on_connected=lambda s=sym: logger.info("PassiveShadow WS connected: {}", s),
                on_disconnected=lambda s=sym: logger.warning("PassiveShadow WS disconnected: {}", s),
            )

            handler = self._make_candle_handler(ctx)
            market_state.dispatcher.on_candle_complete = handler

            task = asyncio.create_task(ws.start(), name=f"passive-shadow-{sym}")
            self._tasks.append(task)
            logger.info("PassiveShadow started for {} ({})", sym, self._timeframe)

    async def stop(self) -> None:
        """Cancel all passive shadow WS tasks."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("PassiveShadow stopped ({} symbols)", len(self._symbols))

    # ------------------------------------------------------------------
    # Per-candle logic
    # ------------------------------------------------------------------

    def _make_candle_handler(self, ctx: SymbolContext):
        """Return an async callback for on_candle_complete for *ctx*."""

        async def _on_candle(state: MarketState, meta: dict) -> None:
            try:
                await self._process_candle(ctx, state)
            except Exception as exc:
                logger.error(
                    "PassiveShadow {}: candle error — {}",
                    ctx.symbol, exc,
                )

        return _on_candle

    async def _process_candle(self, ctx: SymbolContext, state: MarketState) -> None:
        """Full shadow pipeline for one passive symbol candle."""
        df = state.get_candle_df()
        if df is None or len(df) < 50:
            return

        # 1. Features
        features: FeatureSet = ctx.feature_cache.compute(df)
        if features is None or not getattr(features, "close", 0):
            return

        candle_high  = float(df["high"].iloc[-1])
        candle_low   = float(df["low"].iloc[-1])
        candle_close = features.close
        atr          = getattr(features, "atr", candle_close * 0.005)
        if not atr or atr <= 0:
            atr = candle_close * 0.005
        regime = getattr(features, "regime", "RANGING")

        # 2. Alpha votes (async — fetches funding rate)
        votes = await self._alpha.generate_votes_with_funding(features, ctx.symbol)

        # 3. Strategy selection
        strategy = self._ssm.select(votes, features)

        # 4. Close pending ghosts vs current candle
        self._pending[ctx.symbol] = _try_close_ghosts(
            self._pending[ctx.symbol],
            ctx.shadow_tracker,
            candle_high,
            candle_low,
            candle_close,
        )

        # 5. Open new ghost if strategy has a direction
        if strategy.name != CASH_STRATEGY.name:
            side = _dominant_side(votes)
            if side != "HOLD":
                if side == "BUY":
                    sl = candle_close - atr * GHOST_SL_MULT
                    tp = candle_close + atr * GHOST_TP_MULT
                else:
                    sl = candle_close + atr * GHOST_SL_MULT
                    tp = candle_close - atr * GHOST_TP_MULT

                ghost_id = ctx.shadow_tracker.open_ghost(
                    strategy_name=strategy.name,
                    entry_price=candle_close,
                    side=side,
                )
                self._pending[ctx.symbol].append(_PendingGhost(
                    ghost_id=ghost_id,
                    strategy=strategy.name,
                    entry_price=candle_close,
                    side=side,
                    sl_price=sl,
                    tp_price=tp,
                ))
                logger.debug(
                    "PassiveShadow {}: ghost opened | strategy={} side={} entry={:.2f}",
                    ctx.symbol, strategy.name, side, candle_close,
                )

        # 6. Tournament (warm-start Thompson distributions)
        tournament_result = ctx.tournament_engine.run_tournament()

        # 7. Router tick (warm-start bench/promote)
        ctx.strategy_router.tick(regime, tournament_result.winner)

        # 8. Record candle
        ctx.record_candle(regime)

        # 9. Update activation routing for all symbols
        self._registry.route_agent_activation()

        logger.debug(
            "PassiveShadow {}: candle processed | regime={} strategy={} candles={}",
            ctx.symbol, regime, strategy.name, ctx.candles_seen,
        )
