"""Alpha-Scalp Bot – State Change Dispatcher (Async Upgrade).

Bridges MarketState change flags to the alpha pipeline.

Design principles:
- asyncio.Queue absorbs WS burst traffic (50+ depth updates/sec)
- Coalesces rapid-fire updates — only processes latest state
- Different event types trigger different pipeline depths:
  * candle_complete → full alpha pipeline (features → votes → score → trade)
  * book_update     → lightweight spread/imbalance refresh
  * price_jump      → re-score existing signals with urgency
  * book_invalidated → pause trading until book rebuilds
- Backpressure: if alpha engine is slow, stale ticks are dropped

Depends on: market_state.MarketState, market_state.ChangeFlags
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, Any

from market_state import MarketState, ChangeFlags

logger = logging.getLogger("state_dispatcher")


# ---------------------------------------------------------------------------
# Event Types & Priorities
# ---------------------------------------------------------------------------

@dataclass(order=True)
class DispatchEvent:
    """Prioritized event for the dispatch queue.

    Lower priority number = higher priority (processed first).
    """
    priority: int
    event_type: str = field(compare=False)
    timestamp: float = field(compare=False, default_factory=time.time)
    metadata: dict = field(compare=False, default_factory=dict)


class EventPriority:
    """Priority levels for dispatch events."""
    CRITICAL = 0    # book_invalidated, price_jump
    HIGH = 10       # candle_complete
    NORMAL = 20     # book_update (coalesced)
    LOW = 30        # candle_update (partial, informational)


# ---------------------------------------------------------------------------
# Pipeline Callbacks
# ---------------------------------------------------------------------------

@dataclass
class PipelineCallbacks:
    """Registered callbacks for different event types.

    Each callback receives (MarketState, metadata_dict) and returns None.
    The dispatcher handles exceptions internally.
    """
    on_candle_complete: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None
    on_book_update: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None
    on_price_jump: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None
    on_book_invalidated: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None
    on_candle_update: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None
    on_trade: Optional[Callable[[MarketState, dict], Awaitable[None]]] = None


# ---------------------------------------------------------------------------
# State Change Dispatcher
# ---------------------------------------------------------------------------

class StateChangeDispatcher:
    """Event-driven dispatcher that bridges MarketState → Alpha Pipeline.

    Architecture:
    ┌──────────┐     ┌──────────────┐     ┌───────────────┐
    │ WS Mgr   │────>│ MarketState  │────>│  Dispatcher   │
    │ (writes) │     │ (flags)      │     │  (reads+acts) │
    └──────────┘     └──────────────┘     └───────┬───────┘
                                                   │
                              ┌─────────────┬──────┴──────┬────────────┐
                              ▼             ▼             ▼            ▼
                        full_pipeline  book_refresh  re_score    pause_trading
                        (candle_done)  (depth_upd)  (price_jmp) (book_invalid)

    Usage:
        state = MarketState("BTCUSDT")
        callbacks = PipelineCallbacks(
            on_candle_complete=run_full_alpha_pipeline,
            on_book_update=refresh_spread_data,
            on_price_jump=rescore_signals,
            on_book_invalidated=pause_trading,
        )
        dispatcher = StateChangeDispatcher(state, callbacks)
        await dispatcher.start()
    """

    def __init__(
        self,
        state: MarketState,
        callbacks: PipelineCallbacks,
        queue_maxsize: int = 1000,
        poll_interval_ms: float = 50.0,
        coalesce_window_ms: float = 100.0,
        max_processing_time_s: float = 5.0,
    ):
        self.state = state
        self.callbacks = callbacks

        # Queue configuration
        self._queue: asyncio.PriorityQueue[DispatchEvent] = asyncio.PriorityQueue(
            maxsize=queue_maxsize
        )
        self._poll_interval = poll_interval_ms / 1000.0
        self._coalesce_window = coalesce_window_ms / 1000.0
        self._max_processing_time = max_processing_time_s

        # State
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._paused = False  # True when book is invalidated

        # Metrics
        self._events_dispatched: int = 0
        self._events_dropped: int = 0
        self._events_coalesced: int = 0
        self._pipeline_runs: int = 0
        self._pipeline_errors: int = 0
        self._last_pipeline_duration: float = 0.0
        self._total_pipeline_time: float = 0.0

        # Coalescing state
        self._last_book_event_ts: float = 0.0
        self._pending_book_coalesce: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatcher polling and processing loops."""
        if self._running:
            return

        self._running = True

        # Poller: reads MarketState flags → enqueues events
        self._tasks.append(
            asyncio.create_task(self._flag_poller(), name="dispatcher-poller")
        )
        # Consumer: dequeues events → calls callbacks
        self._tasks.append(
            asyncio.create_task(self._event_consumer(), name="dispatcher-consumer")
        )

        logger.info("StateChangeDispatcher started for %s", self.state.symbol)

    async def stop(self) -> None:
        """Stop dispatcher gracefully."""
        self._running = False

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        logger.info(
            "StateChangeDispatcher stopped for %s "
            "(dispatched=%d, dropped=%d, coalesced=%d, pipeline_runs=%d)",
            self.state.symbol,
            self._events_dispatched,
            self._events_dropped,
            self._events_coalesced,
            self._pipeline_runs,
        )

    @property
    def is_paused(self) -> bool:
        """True if trading is paused (e.g., book invalidated)."""
        return self._paused

    def metrics(self) -> dict:
        """Dispatcher performance metrics."""
        avg_pipeline = (
            self._total_pipeline_time / self._pipeline_runs
            if self._pipeline_runs > 0 else 0.0
        )
        return {
            "symbol": self.state.symbol,
            "running": self._running,
            "paused": self._paused,
            "queue_size": self._queue.qsize(),
            "events_dispatched": self._events_dispatched,
            "events_dropped": self._events_dropped,
            "events_coalesced": self._events_coalesced,
            "pipeline_runs": self._pipeline_runs,
            "pipeline_errors": self._pipeline_errors,
            "last_pipeline_ms": round(self._last_pipeline_duration * 1000, 1),
            "avg_pipeline_ms": round(avg_pipeline * 1000, 1),
        }

    # ------------------------------------------------------------------
    # Flag Poller – reads MarketState, enqueues events
    # ------------------------------------------------------------------

    async def _flag_poller(self) -> None:
        """Poll MarketState change flags and convert to dispatch events.

        Runs at ~20Hz (50ms interval). Consumes flags atomically to prevent
        double-processing.
        """
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)

                flags = self.state.consume_flags()
                if not flags:
                    continue

                now = time.time()

                # Priority order matters — critical events first

                # CRITICAL: Book invalidated → pause immediately
                if flags.is_set(ChangeFlags.BOOK_INVALIDATED):
                    await self._enqueue(DispatchEvent(
                        priority=EventPriority.CRITICAL,
                        event_type="book_invalidated",
                        metadata=flags.get_meta(ChangeFlags.BOOK_INVALIDATED),
                    ))

                # CRITICAL: Price jump → re-score urgently
                if flags.is_set(ChangeFlags.PRICE_JUMP):
                    await self._enqueue(DispatchEvent(
                        priority=EventPriority.CRITICAL,
                        event_type="price_jump",
                        metadata=flags.get_meta(ChangeFlags.PRICE_JUMP),
                    ))

                # HIGH: Candle complete → full pipeline
                if flags.is_set(ChangeFlags.CANDLE_COMPLETE):
                    await self._enqueue(DispatchEvent(
                        priority=EventPriority.HIGH,
                        event_type="candle_complete",
                    ))

                # NORMAL: Book update → coalesce rapid-fire updates
                if flags.is_set(ChangeFlags.BOOK_UPDATE):
                    if now - self._last_book_event_ts > self._coalesce_window:
                        await self._enqueue(DispatchEvent(
                            priority=EventPriority.NORMAL,
                            event_type="book_update",
                        ))
                        self._last_book_event_ts = now
                        self._pending_book_coalesce = False
                    else:
                        self._events_coalesced += 1
                        self._pending_book_coalesce = True

                # LOW: Partial candle update
                if flags.is_set(ChangeFlags.CANDLE_UPDATE):
                    # Only enqueue if queue isn't backed up
                    if self._queue.qsize() < self._queue.maxsize * 0.5:
                        await self._enqueue(DispatchEvent(
                            priority=EventPriority.LOW,
                            event_type="candle_update",
                        ))
                    else:
                        self._events_dropped += 1

                # Flush pending coalesced book update if window expired
                if (
                    self._pending_book_coalesce
                    and now - self._last_book_event_ts > self._coalesce_window
                ):
                    await self._enqueue(DispatchEvent(
                        priority=EventPriority.NORMAL,
                        event_type="book_update",
                    ))
                    self._last_book_event_ts = now
                    self._pending_book_coalesce = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Flag poller error: %s", e, exc_info=True)

    async def _enqueue(self, event: DispatchEvent) -> None:
        """Enqueue event with backpressure handling.

        If queue is full, drop lowest-priority events to make room.
        """
        if self._queue.full():
            # Queue full — drop this event if it's low priority
            if event.priority >= EventPriority.NORMAL:
                self._events_dropped += 1
                logger.debug(
                    "Dropped %s event (queue full, size=%d)",
                    event.event_type, self._queue.qsize(),
                )
                return

            # High-priority event but queue full — drain one low-priority item
            try:
                self._queue.get_nowait()
                self._events_dropped += 1
            except asyncio.QueueEmpty:
                pass

        try:
            self._queue.put_nowait(event)
            self._events_dispatched += 1
        except asyncio.QueueFull:
            self._events_dropped += 1

    # ------------------------------------------------------------------
    # Event Consumer – dequeues and dispatches to callbacks
    # ------------------------------------------------------------------

    async def _event_consumer(self) -> None:
        """Consume events from the priority queue and dispatch to callbacks."""
        while self._running:
            try:
                # Block until event available (with timeout for shutdown check)
                try:
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Skip stale events (older than 2x coalesce window)
                age = time.time() - event.timestamp
                if age > self._coalesce_window * 10 and event.priority > EventPriority.CRITICAL:
                    self._events_dropped += 1
                    logger.debug(
                        "Dropped stale %s event (age=%.1fms)",
                        event.event_type, age * 1000,
                    )
                    continue

                await self._dispatch(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Event consumer error: %s", e, exc_info=True)
                self._pipeline_errors += 1

    async def _dispatch(self, event: DispatchEvent) -> None:
        """Dispatch a single event to the appropriate callback."""
        callback = self._get_callback(event.event_type)
        if callback is None:
            return

        # Handle pause state
        if self._paused and event.event_type not in ("book_invalidated", "book_update"):
            # When paused, only process book-related events
            logger.debug(
                "Skipping %s event — trading paused (book invalidated)",
                event.event_type,
            )
            return

        start = time.time()
        try:
            await asyncio.wait_for(
                callback(self.state, event.metadata),
                timeout=self._max_processing_time,
            )
            self._pipeline_runs += 1

            duration = time.time() - start
            self._last_pipeline_duration = duration
            self._total_pipeline_time += duration

            if duration > 1.0:
                logger.warning(
                    "Slow pipeline: %s took %.1fms",
                    event.event_type, duration * 1000,
                )

        except asyncio.TimeoutError:
            duration = time.time() - start
            self._pipeline_errors += 1
            logger.error(
                "Pipeline TIMEOUT: %s exceeded %.1fs limit (took %.1fs)",
                event.event_type, self._max_processing_time, duration,
            )

        except Exception as e:
            self._pipeline_errors += 1
            logger.error(
                "Pipeline error on %s: %s", event.event_type, e, exc_info=True,
            )

        # Special handling for book_invalidated → toggle pause
        if event.event_type == "book_invalidated":
            self._paused = True
            logger.warning("Trading PAUSED — order book invalidated for %s", self.state.symbol)
        elif event.event_type == "book_update" and self._paused:
            if self.state.book.initialized:
                self._paused = False
                logger.info("Trading RESUMED — order book restored for %s", self.state.symbol)

    def _get_callback(self, event_type: str) -> Optional[Callable]:
        """Map event type to registered callback."""
        mapping = {
            "candle_complete": self.callbacks.on_candle_complete,
            "book_update": self.callbacks.on_book_update,
            "price_jump": self.callbacks.on_price_jump,
            "book_invalidated": self.callbacks.on_book_invalidated,
            "candle_update": self.callbacks.on_candle_update,
            "trade": self.callbacks.on_trade,
        }
        return mapping.get(event_type)


# ---------------------------------------------------------------------------
# Multi-Symbol Dispatcher
# ---------------------------------------------------------------------------

class MultiSymbolDispatcher:
    """Manages dispatchers for multiple symbols.

    Usage:
        from ws_manager import MultiSymbolWSManager

        ws = MultiSymbolWSManager(["BTCUSDT", "ETHUSDT"])
        states = await ws.start()

        dispatchers = MultiSymbolDispatcher(states, callbacks_factory)
        await dispatchers.start()
    """

    def __init__(
        self,
        states: dict[str, MarketState],
        callbacks_factory: Callable[[str], PipelineCallbacks],
        **dispatcher_kwargs,
    ):
        """
        Args:
            states: symbol → MarketState mapping (from MultiSymbolWSManager)
            callbacks_factory: function(symbol) → PipelineCallbacks
            **dispatcher_kwargs: passed to each StateChangeDispatcher
        """
        self._dispatchers: dict[str, StateChangeDispatcher] = {}

        for symbol, state in states.items():
            callbacks = callbacks_factory(symbol)
            self._dispatchers[symbol] = StateChangeDispatcher(
                state=state,
                callbacks=callbacks,
                **dispatcher_kwargs,
            )

    async def start(self) -> None:
        await asyncio.gather(
            *[d.start() for d in self._dispatchers.values()]
        )
        logger.info("MultiSymbolDispatcher started: %d symbols", len(self._dispatchers))

    async def stop(self) -> None:
        await asyncio.gather(
            *[d.stop() for d in self._dispatchers.values()]
        )
        logger.info("MultiSymbolDispatcher stopped")

    def metrics(self) -> dict:
        return {
            sym: d.metrics()
            for sym, d in self._dispatchers.items()
        }

    @property
    def any_paused(self) -> bool:
        return any(d.is_paused for d in self._dispatchers.values())
