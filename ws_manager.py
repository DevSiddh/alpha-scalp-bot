"""Alpha-Scalp Bot – Binance WebSocket Manager (Async Upgrade).

Manages persistent WebSocket connections to Binance streams:
- <symbol>@kline_<interval>   → candle updates
- <symbol>@depth@100ms        → order book diffs (100ms update speed)
- <symbol>@trade              → real-time trades

Features:
- Automatic reconnection with exponential backoff
- REST snapshot fallback for order book initialization + gap recovery
- 24-hour keepalive (Binance disconnects after 24h)
- Graceful shutdown with proper resource cleanup
- Health monitoring with staleness detection

Depends on: market_state.MarketState
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Callable, Awaitable

import aiohttp
import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
)

from market_state import MarketState

logger = logging.getLogger("ws_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
BINANCE_WS_COMBINED = "wss://stream.binance.com:9443/stream"
BINANCE_REST_BASE = "https://api.binance.com"
DEPTH_SNAPSHOT_URL = BINANCE_REST_BASE + "/api/v3/depth"

# Reconnection
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
BACKOFF_MULTIPLIER = 2.0

# Binance disconnects WS after 24 hours
WS_MAX_LIFETIME_S = 23 * 3600  # reconnect proactively at 23h

# Health
STALENESS_WARN_S = 10.0
STALENESS_CRITICAL_S = 30.0


class BinanceWSManager:
    """Manages WebSocket streams for a single symbol.

    Usage:
        state = MarketState("BTCUSDT")
        ws = BinanceWSManager(state, interval="1m")
        await ws.start()
        ...
        await ws.stop()

    The manager writes directly to MarketState via on_kline/on_depth/on_trade.
    The StateChangeDispatcher reads from MarketState.flags.
    """

    def __init__(
        self,
        state: MarketState,
        interval: str = "1m",
        book_depth_limit: int = 1000,
        on_connected: Optional[Callable[[], Awaitable[None]]] = None,
        on_disconnected: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.state = state
        self.symbol = state.symbol.lower()  # Binance WS uses lowercase
        self.interval = interval
        self.book_depth_limit = book_depth_limit

        # Callbacks
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        # Internal state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._connect_time: float = 0.0
        self._reconnect_count: int = 0
        self._backoff: float = INITIAL_BACKOFF_S
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Metrics
        self._messages_received: int = 0
        self._last_message_ts: float = 0.0
        self._errors: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the WebSocket connection and message processing."""
        if self._running:
            logger.warning("WSManager already running for %s", self.symbol)
            return

        self._running = True
        self._http_session = aiohttp.ClientSession()

        # Seed candle history from REST before WS starts
        await self._seed_candles()

        # Start main connection loop
        self._tasks.append(
            asyncio.create_task(self._connection_loop(), name=f"ws-{self.symbol}")
        )
        # Start health monitor
        self._tasks.append(
            asyncio.create_task(self._health_monitor(), name=f"health-{self.symbol}")
        )

        logger.info("WSManager started for %s (interval=%s)", self.symbol, self.interval)

    async def stop(self) -> None:
        """Gracefully stop all connections and tasks."""
        self._running = False

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks to finish
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Close HTTP session
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

        self.state.ws_connected = False
        logger.info("WSManager stopped for %s", self.symbol)

    @property
    def is_healthy(self) -> bool:
        """True if connected and receiving messages."""
        return (
            self._running
            and self.state.ws_connected
            and self.state.staleness_seconds < STALENESS_CRITICAL_S
        )

    def metrics(self) -> dict:
        """Connection metrics for monitoring."""
        return {
            "symbol": self.symbol,
            "running": self._running,
            "connected": self.state.ws_connected,
            "messages_received": self._messages_received,
            "reconnect_count": self._reconnect_count,
            "errors": self._errors,
            "staleness_s": round(self.state.staleness_seconds, 1),
            "uptime_s": round(time.time() - self._connect_time, 1) if self._connect_time else 0,
        }

    # ------------------------------------------------------------------
    # Connection Loop
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        """Main loop: connect, process messages, reconnect on failure."""
        while self._running:
            try:
                await self._connect_and_process()
            except asyncio.CancelledError:
                logger.info("Connection loop cancelled for %s", self.symbol)
                break
            except Exception as e:
                self._errors += 1
                logger.error(
                    "WSManager error for %s: %s (reconnect #%d, backoff %.1fs)",
                    self.symbol, e, self._reconnect_count, self._backoff,
                )
                self.state.ws_connected = False
                if self._on_disconnected:
                    try:
                        await self._on_disconnected()
                    except Exception:
                        pass

                if self._running:
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_S)
                    self._reconnect_count += 1

    async def _connect_and_process(self) -> None:
        """Single connection lifecycle: connect → subscribe → process → close."""
        streams = [
            f"{self.symbol}@kline_{self.interval}",
            f"{self.symbol}@depth@100ms",
            f"{self.symbol}@trade",
        ]
        url = f"{BINANCE_WS_COMBINED}?streams={'/'.join(streams)}"

        logger.info("Connecting to Binance WS: %s", url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=10 * 1024 * 1024,  # 10MB max message
        ) as ws:
            self._ws = ws
            self._connect_time = time.time()
            self._backoff = INITIAL_BACKOFF_S  # Reset backoff on success
            self.state.ws_connected = True

            logger.info(
                "Connected to Binance WS for %s (streams: %s)",
                self.symbol, ", ".join(streams),
            )

            if self._on_connected:
                _res = self._on_connected()
                if _res is not None and asyncio.iscoroutine(_res):
                    await _res

            # Fetch order book snapshot after connection
            await self._fetch_book_snapshot()

            # Process messages until disconnect or 23h lifetime
            await self._message_loop(ws)

    async def _message_loop(
        self, ws: websockets.WebSocketClientProtocol
    ) -> None:
        """Process messages from the combined stream."""
        while self._running:
            # Proactive reconnect before Binance's 24h cutoff
            if time.time() - self._connect_time > WS_MAX_LIFETIME_S:
                logger.info(
                    "Proactive reconnect for %s (lifetime %.1fh)",
                    self.symbol,
                    (time.time() - self._connect_time) / 3600,
                )
                return  # Exit cleanly → connection_loop reconnects

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                # No message in 30s — likely stale, let health monitor handle
                logger.warning("No WS message for 30s on %s", self.symbol)
                continue
            except ConnectionClosed:
                logger.warning("WS connection closed for %s", self.symbol)
                return

            self._messages_received += 1
            self._last_message_ts = time.time()

            try:
                msg = json.loads(raw)
                await self._dispatch_message(msg)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from WS: %s...", raw[:100])
            except Exception as e:
                logger.error("Error processing WS message: %s", e, exc_info=True)
                self._errors += 1

    async def _dispatch_message(self, msg: dict) -> None:
        """Route combined stream message to the correct handler.

        Combined stream format: {"stream": "btcusdt@kline_1m", "data": {...}}
        """
        stream = msg.get("stream", "")
        data = msg.get("data", msg)  # fallback for single-stream format

        if "@kline_" in stream:
            self.state.on_kline(data["k"])

        elif "@depth" in stream:
            self.state.on_depth(data)
            # If book was invalidated by sequence gap, trigger snapshot rebuild
            if self.state.flags.is_set("book_invalidated"):
                logger.warning("Book sequence gap on %s — fetching new snapshot", self.symbol)
                asyncio.create_task(self._fetch_book_snapshot())

        elif "@trade" in stream:
            self.state.on_trade(data)

    # ------------------------------------------------------------------
    # REST Fallbacks
    # ------------------------------------------------------------------

    async def _seed_candles(self) -> None:
        """Seed candle history from REST API before WS starts."""
        try:
            url = f"{BINANCE_REST_BASE}/api/v3/klines"
            params = {
                "symbol": self.symbol.upper(),
                "interval": self.interval,
                "limit": 500,
            }

            async with self._http_session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error("Failed to seed candles: HTTP %d", resp.status)
                    return

                data = await resp.json()
                # Binance klines format: [open_time, open, high, low, close, volume, ...]
                ohlcv = [
                    [int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
                    for k in data
                ]
                self.state.candles.seed_from_rest(ohlcv)
                logger.info("Seeded %d candles for %s from REST", len(ohlcv), self.symbol)

        except Exception as e:
            logger.error("Error seeding candles for %s: %s", self.symbol, e)

    async def _fetch_book_snapshot(self, _retry: int = 0) -> None:
        """Fetch depth snapshot from REST to initialize/rebuild order book.

        Retries up to 5 times with exponential backoff on failure.
        """
        MAX_RETRIES = 5
        try:
            params = {
                "symbol": self.symbol.upper(),
                "limit": self.book_depth_limit,
            }

            async with self._http_session.get(DEPTH_SNAPSHOT_URL, params=params) as resp:
                if resp.status != 200:
                    logger.error("Failed to fetch book snapshot: HTTP %d", resp.status)
                    if _retry < MAX_RETRIES and self._running:
                        delay = min(2.0 * (2 ** _retry), 30.0)
                        await asyncio.sleep(delay)
                        asyncio.create_task(self._fetch_book_snapshot(_retry=_retry + 1))
                    else:
                        logger.error("Book snapshot failed after %d retries — giving up", _retry)
                    return

                snapshot = await resp.json()
                self.state.book.apply_snapshot(snapshot)
                logger.info(
                    "Book snapshot applied for %s: lastUpdateId=%s",
                    self.symbol, snapshot.get("lastUpdateId"),
                )

        except Exception as e:
            logger.error("Error fetching book snapshot for %s: %s", self.symbol, e)
            if _retry < MAX_RETRIES and self._running:
                delay = min(2.0 * (2 ** _retry), 30.0)
                await asyncio.sleep(delay)
                asyncio.create_task(self._fetch_book_snapshot(_retry=_retry + 1))
            else:
                logger.error(
                    "Book snapshot failed after %d retries for %s — giving up",
                    _retry, self.symbol,
                )

    # ------------------------------------------------------------------
    # Health Monitoring
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Periodic health check — detects stale connections."""
        while self._running:
            try:
                await asyncio.sleep(5.0)

                staleness = self.state.staleness_seconds

                if staleness > STALENESS_CRITICAL_S and self.state.ws_connected:
                    logger.error(
                        "CRITICAL: %s stale for %.1fs — forcing reconnect",
                        self.symbol, staleness,
                    )
                    # Force close the WS to trigger reconnect
                    if self._ws:
                        await self._ws.close()

                elif staleness > STALENESS_WARN_S and self.state.ws_connected:
                    logger.warning(
                        "WARN: %s stale for %.1fs", self.symbol, staleness,
                    )

                # Check book health
                if self.state.ws_connected and not self.state.book.initialized:
                    logger.warning(
                        "Book not initialized for %s — fetching snapshot",
                        self.symbol,
                    )
                    await self._fetch_book_snapshot()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health monitor error: %s", e)


# ---------------------------------------------------------------------------
# Multi-Symbol Manager
# ---------------------------------------------------------------------------

class MultiSymbolWSManager:
    """Manages WebSocket connections for multiple symbols.

    Usage:
        manager = MultiSymbolWSManager(["BTCUSDT", "ETHUSDT"], interval="1m")
        states = await manager.start()
        # states = {"BTCUSDT": MarketState, "ETHUSDT": MarketState}
        ...
        await manager.stop()
    """

    def __init__(
        self,
        symbols: list[str],
        interval: str = "1m",
        book_depth: int = 20,
        candle_history: int = 500,
    ):
        self.symbols = symbols
        self.interval = interval
        self.states: dict[str, MarketState] = {}
        self._managers: dict[str, BinanceWSManager] = {}

        for sym in symbols:
            state = MarketState(
                symbol=sym,
                candle_history=candle_history,
                book_depth=book_depth,
            )
            self.states[sym] = state
            self._managers[sym] = BinanceWSManager(
                state=state,
                interval=interval,
            )

    async def start(self) -> dict[str, MarketState]:
        """Start all symbol connections in parallel."""
        await asyncio.gather(
            *[mgr.start() for mgr in self._managers.values()]
        )
        logger.info(
            "MultiSymbolWSManager started: %d symbols", len(self.symbols)
        )
        return self.states

    async def stop(self) -> None:
        """Stop all connections."""
        await asyncio.gather(
            *[mgr.stop() for mgr in self._managers.values()]
        )
        logger.info("MultiSymbolWSManager stopped")

    def health_report(self) -> dict:
        """Health status for all symbols."""
        return {
            sym: mgr.metrics()
            for sym, mgr in self._managers.items()
        }

    @property
    def all_healthy(self) -> bool:
        return all(mgr.is_healthy for mgr in self._managers.values())

    @property
    def all_ready(self) -> bool:
        return all(state.is_ready for state in self.states.values())
