"""Alpha-Scalp Bot – Market State Module (Async Upgrade).

Maintains a live, in-memory representation of market data fed by WebSocket streams.
Designed to decouple the transport layer (ws_manager) from the signal pipeline.

Components:
1. CandleBuilder  – assembles real-time kline WS events into OHLCV candles
2. OrderBook      – local order book with Binance depth sequence validation
3. MarketState    – unified state container with change-flag dispatch
"""

from __future__ import annotations

import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger("market_state")

# ---------------------------------------------------------------------------
# 1. CandleBuilder – assembles kline WS events into candles
# ---------------------------------------------------------------------------

@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: int        # open time ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = False
    trades: int = 0


class CandleBuilder:
    """Builds candles from Binance kline WebSocket events.

    - Tracks the *current* (partial) candle being formed
    - On candle close, pushes to history deque
    - Only converts deque → DataFrame on explicit request (lazy cast)
    """

    def __init__(self, max_history: int = 500):
        self.max_history = max_history
        self._history: deque[Candle] = deque(maxlen=max_history)
        self._current: Optional[Candle] = None
        self._df_cache: Optional[pd.DataFrame] = None
        self._df_dirty: bool = True  # invalidated when new candle completes

    @property
    def current(self) -> Optional[Candle]:
        return self._current

    @property
    def history_len(self) -> int:
        return len(self._history)

    def update(self, kline_event: dict) -> bool:
        """Process a Binance kline WS event.

        Args:
            kline_event: The 'k' field from the kline WS message.
                Expected keys: t, o, h, l, c, v, x, n

        Returns:
            True if a candle just *completed* (closed), False otherwise.
        """
        k = kline_event
        candle = Candle(
            timestamp=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            is_closed=bool(k["x"]),
            trades=int(k.get("n", 0)),
        )

        completed = False

        if candle.is_closed:
            # Push completed candle to history
            self._history.append(candle)
            self._current = None
            self._df_dirty = True
            completed = True
            logger.debug(
                "Candle closed: ts=%d close=%.2f vol=%.4f trades=%d",
                candle.timestamp, candle.close, candle.volume, candle.trades,
            )
        else:
            # Update partial candle
            self._current = candle

        return completed

    def to_dataframe(self) -> pd.DataFrame:
        """Convert candle history to DataFrame (lazy-cached).

        Only rebuilds when a new candle has completed since last call.
        Returns columns: timestamp, open, high, low, close, volume, trades
        """
        if self._df_dirty or self._df_cache is None:
            if not self._history:
                self._df_cache = pd.DataFrame(
                    columns=["timestamp", "open", "high", "low", "close", "volume", "trades"]
                )
            else:
                rows = [
                    {
                        "timestamp": c.timestamp,
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                        "trades": c.trades,
                    }
                    for c in self._history
                ]
                self._df_cache = pd.DataFrame(rows)
            self._df_dirty = False
            logger.debug("DataFrame rebuilt: %d candles", len(self._df_cache))
        return self._df_cache

    def seed_from_rest(self, ohlcv_list: list[list]) -> None:
        """Seed history from REST API response (ccxt format).

        Args:
            ohlcv_list: List of [timestamp, open, high, low, close, volume]
        """
        self._history.clear()
        for row in ohlcv_list:
            self._history.append(Candle(
                timestamp=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                is_closed=True,
            ))
        self._df_dirty = True
        logger.info("Seeded %d candles from REST", len(self._history))


# ---------------------------------------------------------------------------
# 2. OrderBook – local book with sequence validation
# ---------------------------------------------------------------------------

class OrderBook:
    """Maintains a local order book from Binance depth stream.

    Implements the Binance recommended approach:
    1. Open <symbol>@depth stream
    2. Buffer events
    3. Get REST snapshot
    4. Apply buffered + new events with sequence validation
    5. On sequence gap → drop book, re-snapshot

    Tracks:
    - Top N bid/ask levels
    - Spread, mid-price, book imbalance
    """

    def __init__(self, depth: int = 20):
        self.depth = depth
        self.bids: dict[float, float] = {}   # price → qty
        self.asks: dict[float, float] = {}   # price → qty
        self._last_update_id: int = 0
        self._snapshot_id: int = 0
        self._initialized: bool = False
        self._buffer: list[dict] = []        # buffered before snapshot
        self._seq_errors: int = 0

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def spread(self) -> float:
        """Best ask - best bid. Returns inf if book empty."""
        best_bid = self.best_bid
        best_ask = self.best_ask
        if best_bid is None or best_ask is None:
            return float("inf")
        return best_ask - best_bid

    @property
    def spread_bps(self) -> float:
        """Spread in basis points relative to mid price."""
        mid = self.mid_price
        if mid is None or mid == 0:
            return float("inf")
        return (self.spread / mid) * 10_000

    @property
    def mid_price(self) -> Optional[float]:
        best_bid = self.best_bid
        best_ask = self.best_ask
        if best_bid is None or best_ask is None:
            return None
        return (best_bid + best_ask) / 2

    @property
    def best_bid(self) -> Optional[float]:
        return max(self.bids.keys()) if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return min(self.asks.keys()) if self.asks else None

    @property
    def book_imbalance(self) -> float:
        """Bid volume / (bid volume + ask volume) for top levels.

        Returns 0.5 (neutral) if book is empty.
        Range: 0.0 (all asks) to 1.0 (all bids).
        """
        top_bids = sorted(self.bids.items(), key=lambda x: -x[0])[:self.depth]
        top_asks = sorted(self.asks.items(), key=lambda x: x[0])[:self.depth]

        bid_vol = sum(qty for _, qty in top_bids)
        ask_vol = sum(qty for _, qty in top_asks)
        total = bid_vol + ask_vol

        if total == 0:
            return 0.5
        return bid_vol / total

    def apply_snapshot(self, snapshot: dict) -> None:
        """Apply REST depth snapshot.

        Args:
            snapshot: Binance depth snapshot with 'lastUpdateId', 'bids', 'asks'
        """
        self._snapshot_id = int(snapshot["lastUpdateId"])
        self.bids.clear()
        self.asks.clear()

        for price, qty in snapshot["bids"]:
            p, q = float(price), float(qty)
            if q > 0:
                self.bids[p] = q

        for price, qty in snapshot["asks"]:
            p, q = float(price), float(qty)
            if q > 0:
                self.asks[p] = q

        # Apply buffered events that are newer than snapshot
        applied = 0
        for event in self._buffer:
            u_first = event["U"]   # first update ID in event
            u_final = event["u"]   # final update ID in event

            # Drop events older than snapshot
            if u_final <= self._snapshot_id:
                continue

            # First valid event must have U <= lastUpdateId+1 <= u
            if not self._initialized:
                if u_first <= self._snapshot_id + 1 <= u_final:
                    self._apply_deltas(event)
                    self._last_update_id = u_final
                    self._initialized = True
                    applied += 1
                    continue
                else:
                    continue

            self._apply_deltas(event)
            self._last_update_id = u_final
            applied += 1

        if not self._initialized:
            # Edge case: no buffered event bridges the snapshot
            # Mark as initialized anyway, next live event will validate
            self._initialized = True

        self._buffer.clear()
        logger.info(
            "Order book snapshot applied: id=%d, bids=%d, asks=%d, buffered_applied=%d",
            self._snapshot_id, len(self.bids), len(self.asks), applied,
        )

    def update(self, depth_event: dict) -> bool:
        """Process a depth stream event.

        Args:
            depth_event: Binance depth update with U, u, b, a fields.

        Returns:
            True if update applied successfully, False if sequence gap detected.
        """
        if not self._initialized:
            # Buffer until snapshot arrives
            self._buffer.append(depth_event)
            return True

        u_first = depth_event["U"]
        u_final = depth_event["u"]

        # Sequence validation: U should be <= last_update_id + 1
        # and u should be >= last_update_id + 1
        expected_next = self._last_update_id + 1

        if u_first > expected_next:
            # GAP DETECTED — missed events
            self._seq_errors += 1
            logger.warning(
                "Order book sequence gap! expected_next=%d, got U=%d (gap=%d, total_errors=%d)",
                expected_next, u_first, u_first - expected_next, self._seq_errors,
            )
            self.invalidate()
            return False

        if u_final < expected_next:
            # Stale event, skip
            return True

        self._apply_deltas(depth_event)
        self._last_update_id = u_final
        return True

    def _apply_deltas(self, event: dict) -> None:
        """Apply bid/ask deltas from a depth event."""
        for price, qty in event.get("b", []):
            p, q = float(price), float(qty)
            if q == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = q

        for price, qty in event.get("a", []):
            p, q = float(price), float(qty)
            if q == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = q

        # Trim to top N levels to bound memory
        if len(self.bids) > self.depth * 2:
            sorted_bids = sorted(self.bids.keys(), reverse=True)
            for p in sorted_bids[self.depth * 2:]:
                del self.bids[p]

        if len(self.asks) > self.depth * 2:
            sorted_asks = sorted(self.asks.keys())
            for p in sorted_asks[self.depth * 2:]:
                del self.asks[p]

    def invalidate(self) -> None:
        """Mark book as invalid — needs fresh REST snapshot."""
        self._initialized = False
        self.bids.clear()
        self.asks.clear()
        self._last_update_id = 0
        self._buffer.clear()
        logger.warning("Order book invalidated — awaiting fresh snapshot")

    def top_levels(self, n: int = 5) -> dict:
        """Get top N bid/ask levels for logging/display."""
        top_bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        top_asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return {
            "bids": [(p, q) for p, q in top_bids],
            "asks": [(p, q) for p, q in top_asks],
            "spread": self.spread,
            "spread_bps": self.spread_bps,
            "mid_price": self.mid_price,
            "imbalance": self.book_imbalance,
        }


# ---------------------------------------------------------------------------
# 3. MarketState – unified state container
# ---------------------------------------------------------------------------

class ChangeFlags:
    """Tracks what changed since last engine run."""

    CANDLE_COMPLETE = "candle_complete"
    CANDLE_UPDATE = "candle_update"
    BOOK_UPDATE = "book_update"
    BOOK_INVALIDATED = "book_invalidated"
    TRADE = "trade"
    PRICE_JUMP = "price_jump"

    def __init__(self):
        self._flags: set[str] = set()
        self._metadata: dict[str, any] = {}

    def set(self, flag: str, **meta) -> None:
        self._flags.add(flag)
        if meta:
            self._metadata[flag] = meta

    def is_set(self, flag: str) -> bool:
        return flag in self._flags

    def any_set(self, *flags: str) -> bool:
        return bool(self._flags & set(flags))

    def get_meta(self, flag: str) -> dict:
        return self._metadata.get(flag, {})

    def clear(self) -> None:
        self._flags.clear()
        self._metadata.clear()

    def __repr__(self) -> str:
        return f"ChangeFlags({self._flags})"

    def __bool__(self) -> bool:
        return bool(self._flags)


@dataclass
class TradeUpdate:
    """Real-time trade event."""
    price: float
    quantity: float
    timestamp: int          # ms
    is_buyer_maker: bool    # True = sell aggressor


class MarketState:
    """Unified market state container for a single symbol.

    Aggregates:
    - CandleBuilder (kline stream)
    - OrderBook (depth stream)
    - Last trade price & volume
    - Change flags for event dispatch

    The alpha engine reads from this; WS manager writes to this.
    """

    def __init__(
        self,
        symbol: str,
        candle_history: int = 500,
        book_depth: int = 20,
        price_jump_threshold_bps: float = 15.0,
    ):
        self.symbol = symbol
        self.candles = CandleBuilder(max_history=candle_history)
        self.book = OrderBook(depth=book_depth)
        self.flags = ChangeFlags()

        # Trade tracking
        self.last_trade: Optional[TradeUpdate] = None
        self.last_trade_price: float = 0.0
        self._prev_trade_price: float = 0.0
        self._price_jump_threshold_bps = price_jump_threshold_bps

        # Timing
        self.last_update_ts: float = 0.0
        self.ws_connected: bool = False
        self._created_at: float = time.time()

    def on_kline(self, kline_event: dict) -> None:
        """Process kline WS event. Sets appropriate change flags."""
        completed = self.candles.update(kline_event)
        self.last_update_ts = time.time()

        if completed:
            self.flags.set(ChangeFlags.CANDLE_COMPLETE)
        else:
            self.flags.set(ChangeFlags.CANDLE_UPDATE)

    def on_depth(self, depth_event: dict) -> None:
        """Process depth WS event. Sets book_update or book_invalidated."""
        success = self.book.update(depth_event)
        self.last_update_ts = time.time()

        if success:
            self.flags.set(ChangeFlags.BOOK_UPDATE)
        else:
            self.flags.set(ChangeFlags.BOOK_INVALIDATED)

    def on_trade(self, trade_event: dict) -> None:
        """Process trade WS event. Detects price jumps."""
        trade = TradeUpdate(
            price=float(trade_event["p"]),
            quantity=float(trade_event["q"]),
            timestamp=int(trade_event["T"]),
            is_buyer_maker=bool(trade_event["m"]),
        )

        self._prev_trade_price = self.last_trade_price
        self.last_trade = trade
        self.last_trade_price = trade.price
        self.last_update_ts = time.time()

        self.flags.set(ChangeFlags.TRADE)

        # Detect price jumps
        if self._prev_trade_price > 0:
            move_bps = abs(trade.price - self._prev_trade_price) / self._prev_trade_price * 10_000
            if move_bps >= self._price_jump_threshold_bps:
                self.flags.set(
                    ChangeFlags.PRICE_JUMP,
                    move_bps=move_bps,
                    direction="up" if trade.price > self._prev_trade_price else "down",
                )
                logger.info(
                    "Price jump detected: %.1f bps %s (%.2f → %.2f)",
                    move_bps,
                    "up" if trade.price > self._prev_trade_price else "down",
                    self._prev_trade_price,
                    trade.price,
                )

    def consume_flags(self) -> ChangeFlags:
        """Return current flags and reset. Used by dispatcher."""
        current = self.flags
        self.flags = ChangeFlags()
        return current

    def get_candle_df(self) -> pd.DataFrame:
        """Get candle history as DataFrame (lazy-cached)."""
        return self.candles.to_dataframe()

    def get_book_snapshot(self) -> dict:
        """Get current order book state summary."""
        return self.book.top_levels()

    @property
    def is_ready(self) -> bool:
        """True when we have enough data for the alpha engine."""
        return (
            self.ws_connected
            and self.candles.history_len >= 50
            and self.book.initialized
            and self.last_trade_price > 0
        )

    @property
    def staleness_seconds(self) -> float:
        """Seconds since last WS update."""
        if self.last_update_ts == 0:
            return float("inf")
        return time.time() - self.last_update_ts

    def summary(self) -> dict:
        """Quick state summary for logging/stats."""
        return {
            "symbol": self.symbol,
            "ready": self.is_ready,
            "ws_connected": self.ws_connected,
            "candle_count": self.candles.history_len,
            "current_candle": self.candles.current is not None,
            "book_initialized": self.book.initialized,
            "book_spread_bps": self.book.spread_bps,
            "book_imbalance": self.book.book_imbalance,
            "last_price": self.last_trade_price,
            "staleness_s": round(self.staleness_seconds, 1),
        }
