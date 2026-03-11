"""Alpha-Scalp Bot -- Main Entry Point.

Orchestrates the full scalping loop in TWO modes:

A) **WebSocket mode** (cfg.USE_WEBSOCKET = True, default)
   MarketState <- BinanceWSManager (kline + depth + trade streams)
   StateChangeDispatcher reads change-flags and fires callbacks:
     candle_complete  -> full alpha pipeline (features -> votes -> score -> trade)
     book_update      -> refresh spread/imbalance for spread guard
     price_jump       -> urgent re-score of last signal
     book_invalidated -> pause trading until book rebuilds

B) **Polling mode** (cfg.USE_WEBSOCKET = False, fallback)
   Original REST polling loop (fetch_ohlcv every N seconds).

Both modes share:
  - RiskEngine, OrderExecutor, TelegramAlerts, TradeTrackerV2
  - Alpha Engine pipeline (FeatureCache -> AlphaEngine -> SignalScoring)
  - WeightOptimizer (weekly, triggers at 30 trades)
  - Position monitor (SL/TP detection)
  - Swing trading
  - Kill switch + daily reset
  - Graceful shutdown on SIGINT / SIGTERM

Usage:
    python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime, timezone

import ccxt
import pandas as pd
from loguru import logger

import config as cfg
from order_executor import FatalExchangeError, OrderExecutor
from risk_engine import RiskEngine
from strategy import ScalpStrategy, Signal
from swing_strategy import SwingStrategy, SwingSignal
from telegram_alerts import TelegramAlerts
from trade_tracker_v2 import TradeTrackerV2
from feature_cache import FeatureCache
from alpha_engine import AlphaEngine
from signal_scoring import SignalScoring
from weight_optimizer import WeightOptimizer

# WebSocket imports (only used when cfg.USE_WEBSOCKET is True)
try:
    from market_state import MarketState
    from ws_manager import BinanceWSManager
    from state_dispatcher import StateChangeDispatcher, PipelineCallbacks
    _WS_AVAILABLE = True
except ImportError as _ws_err:
    _WS_AVAILABLE = False
    logger.warning("WebSocket modules not available: {} - falling back to polling", _ws_err)

# ---------------------------------------------------------------------------
# Intercept stdlib logging so third-party libs (ccxt, websockets, asyncio)
# don't crash the process with '%'-style format mismatches.
# ---------------------------------------------------------------------------
import logging as _stdlib_logging

class _SafeInterceptHandler(_stdlib_logging.Handler):
    """Route stdlib logging -> loguru, swallowing format errors."""
    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        try:
            msg = record.getMessage()
        except Exception:
            # Format string mismatch — render what we can
            msg = str(record.msg)
        logger.opt(depth=6, exception=record.exc_info).log(level, "{}", msg)

_stdlib_logging.basicConfig(handlers=[_SafeInterceptHandler()], level=0, force=True)

# Timeframe-aware loop intervals (seconds) -- used in polling mode
_TF_INTERVALS = {"1m": 5, "3m": 15, "5m": 25}


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------
def _create_exchange() -> ccxt.Exchange:
    """Instantiate and configure the Binance Futures CCXT client."""
    # ccxt >= 4.5.6 required for Binance Demo Trading support
    _ccxt_ver = tuple(int(x) for x in ccxt.__version__.split(".")[:3])
    logger.info("ccxt version: {} (parsed: {})", ccxt.__version__, _ccxt_ver)

    common_cfg = {
        "apiKey": cfg.BINANCE_API_KEY,
        "secret": cfg.BINANCE_SECRET,
        "enableRateLimit": True,
        "timeout": 30000,  # 30s – demo exchangeInfo is large
        "options": {
            "adjustForTimeDifference": True,
        },
    }

    if cfg.BINANCE_DEMO_TRADING:
        # Binance deprecated the Futures sandbox/testnet environment.
        # CCXT v4.5.6+ supports the new unified "Demo Trading" mode via
        # exchange.enable_demo_trading(True), which correctly routes to
        # demo-fapi.binance.com for futures (and demo spot if needed).
        # API keys must be generated from Binance's Demo Trading page:
        # https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
        if _ccxt_ver < (4, 5, 6):
            raise RuntimeError(
                f"ccxt {ccxt.__version__} is too old for Demo Trading. "
                f"Run: pip install -U ccxt   (need >= 4.5.6)"
            )
        exchange = ccxt.binance({
            **common_cfg,
            "options": {**common_cfg["options"], "defaultType": "future"},
        })
        exchange.enable_demo_trading(True)
        logger.info("Exchange: Binance Futures DEMO (demo-fapi.binance.com)")
    else:
        exchange = ccxt.binance({**common_cfg, "options": {**common_cfg["options"], "defaultType": "future"}})
        logger.warning("Exchange: Binance Futures LIVE – real funds at risk")

    # Verify connectivity (retry up to 3× on timeout — demo endpoint can be slow)
    import time as _time
    for _attempt in range(1, 4):
        try:
            exchange.load_markets()
            break
        except (ccxt.RequestTimeout, ccxt.NetworkError) as exc:
            logger.warning("load_markets() attempt {}/3 timed out: {}", _attempt, exc)
            if _attempt < 3:
                _time.sleep(3)
                continue
            # Final attempt failed — surface hint and re-raise
            if cfg.BINANCE_DEMO_TRADING:
                logger.error(
                    "HINT: Demo Trading requires API keys generated from "
                    "Binance Demo Trading page (NOT old testnet keys). "
                    "See: https://www.binance.com/en/support/faq/detail/"
                    "9be58f73e5e14338809e3b705b9687dd\n"
                    "Also ensure ccxt is up-to-date: pip install -U ccxt"
                )
            raise
        except Exception as exc:
            logger.error("load_markets() failed: {}: {}", type(exc).__name__, exc)
            if cfg.BINANCE_DEMO_TRADING:
                logger.error(
                    "HINT: Demo Trading requires API keys generated from "
                    "Binance Demo Trading page (NOT old testnet keys). "
                    "See: https://www.binance.com/en/support/faq/detail/"
                    "9be58f73e5e14338809e3b705b9687dd\n"
                    "Also ensure ccxt is up-to-date: pip install -U ccxt"
                )
            raise
    logger.info(
        "Markets loaded | {} pairs available", len(exchange.markets)
    )
    return exchange


# ---------------------------------------------------------------------------
# OHLCV fetcher (with error classification – Fix 3)
# ---------------------------------------------------------------------------
def fetch_ohlcv(exchange: ccxt.Exchange) -> pd.DataFrame | None:
    """Fetch recent OHLCV candles and return a DataFrame.

    Raises FatalExchangeError on auth/permission failures so the main
    loop can distinguish retryable errors from shutdown-worthy ones.
    """
    try:
        raw = exchange.fetch_ohlcv(
            cfg.SYMBOL, cfg.TIMEFRAME, limit=cfg.LOOKBACK_CANDLES
        )
        if not raw:
            logger.warning("Empty OHLCV response")
            return None

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        logger.debug(
            "Fetched {} candles | latest close={:.2f}",
            len(df),
            df["close"].iloc[-1],
        )
        return df

    except (ccxt.AuthenticationError, ccxt.AccountNotEnabled, ccxt.PermissionDenied) as exc:
        logger.critical("FATAL: OHLCV fetch hit auth error – {}", exc)
        raise FatalExchangeError(f"Auth error in fetch_ohlcv: {exc}") from exc
    except ccxt.ExchangeNotAvailable as exc:
        logger.critical("FATAL: Exchange unavailable (maintenance/IP ban) – {}", exc)
        raise FatalExchangeError(f"Exchange unavailable: {exc}") from exc
    except Exception as exc:
        logger.error("OHLCV fetch failed (transient): {}", exc)
        return None


# ---------------------------------------------------------------------------
# Swing OHLCV fetcher
# ---------------------------------------------------------------------------
def fetch_swing_ohlcv(exchange: ccxt.Exchange, symbol: str) -> pd.DataFrame | None:
    """Fetch 4h OHLCV candles for swing trading."""
    try:
        raw = exchange.fetch_ohlcv(
            symbol, cfg.SWING_TIMEFRAME, limit=cfg.SWING_LOOKBACK_CANDLES
        )
        if not raw:
            logger.warning("[SWING] Empty OHLCV for {}", symbol)
            return None

        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        logger.debug(
            "[SWING] {} candles for {} | latest close={:.2f}",
            len(df), symbol, df["close"].iloc[-1],
        )
        return df
    except (ccxt.AuthenticationError, ccxt.AccountNotEnabled, ccxt.PermissionDenied) as exc:
        raise FatalExchangeError(f"Auth error in swing fetch: {exc}") from exc
    except ccxt.ExchangeNotAvailable as exc:
        raise FatalExchangeError(f"Exchange unavailable: {exc}") from exc
    except Exception as exc:
        logger.error("[SWING] OHLCV fetch failed for {}: {}", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Midnight detection
# ---------------------------------------------------------------------------
def _is_new_utc_day(last_date: str) -> tuple[bool, str]:
    """Check whether we've crossed into a new UTC day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return today != last_date, today


# ---------------------------------------------------------------------------
# /stats command polling task
# ---------------------------------------------------------------------------
async def _stats_poller(
    alerts: TelegramAlerts,
    tracker: TradeTrackerV2,
    shutdown_event: asyncio.Event,
) -> None:
    """Poll Telegram for /stats commands and reply with bot statistics."""
    if not cfg.STATS_COMMAND_ENABLED or not alerts.enabled:
        logger.info("/stats command polling disabled")
        return

    logger.info("/stats command polling started")
    offset = 0
    # Skip past any old messages on startup
    _, offset = await alerts.get_updates(offset=0, timeout=0)

    while not shutdown_event.is_set():
        try:
            updates, offset = await alerts.get_updates(offset=offset, timeout=5)
            for update in updates:
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Only respond to /stats from the configured chat
                if text.lower() in ("/stats", "/stats@" + cfg.TELEGRAM_BOT_TOKEN.split(":")[0]):
                    if chat_id == alerts.chat_id or not alerts.chat_id:
                        logger.info("/stats command received from chat {}", chat_id)
                        session_stats = tracker.get_session_stats()
                        cumulative_stats = tracker.get_cumulative_stats()
                        await alerts.send_stats(session_stats, cumulative_stats)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("/stats poller error: {}", exc)
            await asyncio.sleep(5)

    logger.info("/stats command polling stopped")


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------
async def run_bot_polling() -> None:  # noqa: C901
    """Original REST polling loop (fallback when WS is disabled)."""    

    # --- Initialisation ---------------------------------------------------
    logger.info("Initialising Alpha-Scalp Bot (Binance Futures)...")

    try:
        exchange = _create_exchange()
    except Exception as exc:
        logger.critical("Exchange init failed: {}", exc)
        sys.exit(1)

    risk = RiskEngine(exchange)
    strategy = ScalpStrategy()
    executor = OrderExecutor(exchange, risk)
    alerts = TelegramAlerts()
    tracker = TradeTrackerV2()
    risk.set_trade_tracker(tracker)

    # ── Phase 1: Alpha Engine pipeline ──
    feature_cache = FeatureCache()
    alpha_engine = AlphaEngine()
    signal_scoring = SignalScoring()

    # ── Phase 2: Weight Optimizer ──
    optimizer = WeightOptimizer(tracker)
    last_optimize_time = 0.0

    # Store last scoring result per symbol for position monitor
    last_scoring: dict = {}

    # Set margin type and leverage once at startup
    executor.set_margin_type(cfg.SYMBOL)
    executor.set_leverage(cfg.SYMBOL, cfg.LEVERAGE)

    # --- Swing trading init ---
    swing_strategy = SwingStrategy() if cfg.SWING_ENABLED else None
    last_swing_check = 0.0  # epoch timestamp of last swing check

    # Set margin type and leverage for swing symbols
    if cfg.SWING_ENABLED:
        for sym in cfg.SWING_SYMBOLS:
            executor.set_margin_type(sym)
            executor.set_leverage(sym, cfg.SWING_LEVERAGE)
        logger.info("Swing trading ENABLED for {} symbols", len(cfg.SWING_SYMBOLS))

    # Send startup alert
    await alerts.send_startup_message()
    logger.info("Bot started – entering main loop")

    # Track current UTC date for midnight reset
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ----- Position monitor state -----
    # Dict of symbol -> {side, entry_price, contracts, entry_time}
    known_positions: dict[str, dict] = {}
    last_position_check: float = 0.0

    # Build initial known positions snapshot
    all_monitored_symbols = [cfg.SYMBOL]
    if cfg.SWING_ENABLED:
        all_monitored_symbols.extend(s for s in cfg.SWING_SYMBOLS if s != cfg.SYMBOL)
    for sym in all_monitored_symbols:
        pos_info = executor.get_position_info(sym)
        if pos_info:
            known_positions[sym] = {
                "side": pos_info["side"],
                "entry_price": pos_info["entry_price"],
                "contracts": pos_info["contracts"],
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
    logger.info("Position monitor: {} known positions at startup", len(known_positions))

    # --- Shutdown flag (set by signal handlers) ---------------------------
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: int, _frame) -> None:
        sig_name = signal.Signals(sig).name
        logger.warning("Received {} – initiating graceful shutdown", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # --- Start /stats command polling task --------------------------------
    stats_task = asyncio.create_task(
        _stats_poller(alerts, tracker, shutdown_event)
    )

    # --- Main loop --------------------------------------------------------
    while not shutdown_event.is_set():
        try:
            # 1. Midnight reset
            is_new_day, today = _is_new_utc_day(current_date)
            if is_new_day:
                logger.info("UTC midnight crossed – resetting daily stats")
                cumulative = tracker.get_cumulative_stats()
                summary = risk.reset_daily()
                await alerts.send_daily_summary(
                    pnl=summary["pnl"],
                    trades=summary["trades"],
                    win_rate=summary["win_rate"],
                    start_balance=summary["start_balance"],
                    end_balance=summary["end_balance"],
                    cumulative_stats=cumulative,
                )
                current_date = today

            # 2. Kill switch (also refreshes balance cache for this loop)
            if risk.check_kill_switch():
                if not getattr(run_bot_polling, "_ks_alerted", False):
                    await alerts.send_kill_switch_alert()
                    run_bot_polling._ks_alerted = True  # type: ignore[attr-defined]
                logger.warning(
                    "Kill switch active – sleeping {} s", cfg.LOOP_INTERVAL * 12
                )
                await asyncio.sleep(cfg.LOOP_INTERVAL * 12)  # back off
                continue
            else:
                run_bot_polling._ks_alerted = False  # type: ignore[attr-defined]

            # 3. Fetch candles
            df = fetch_ohlcv(exchange)
            if df is None or df.empty:
                logger.warning("No candle data – retrying in {} s", cfg.LOOP_INTERVAL)
                await asyncio.sleep(cfg.LOOP_INTERVAL)
                continue

            # ── Phase 1: Alpha Engine Signal Pipeline ──
            sym = cfg.SYMBOL
            features = feature_cache.compute(df)
            votes = alpha_engine.generate_votes(features)
            result = signal_scoring.score(votes, features)

            logger.info(
                "Alpha signal | {} | score={:+.2f} confidence={:.0f}% action={} regime={}",
                sym, result.score, result.confidence * 100,
                result.action, result.regime,
            )

            if result.action in ("BUY", "SELL"):
                side = "long" if result.action == "BUY" else "short"
                entry = features.close
                atr_val = features.atr

                # Store scoring for position monitor to use on close
                last_scoring[sym] = result.as_dict()

                # Risk-managed SL/TP using ATR + regime
                sl = risk.get_stop_loss(entry, side, atr=atr_val, regime=result.regime)
                tp = risk.get_take_profit(entry, side, atr=atr_val, regime=result.regime)

                # Confidence-scaled position size
                base_size = risk.calculate_position_size(entry, sl)
                confidence_scale = 0.5 + (result.confidence * 0.5)  # 50%-100% of base
                size = round(base_size * confidence_scale, 6)

                can_trade, gate_reason = risk.can_open_trade()
                if size > 0 and can_trade:
                    logger.info(
                        "Opening {} {} | entry={:.2f} sl={:.2f} tp={:.2f} | "
                        "size={:.6f} (conf_scale={:.0f}%) | score={:+.2f}",
                        side.upper(), sym, entry, sl, tp,
                        size, confidence_scale * 100, result.score,
                    )
                    order = executor.open_position(
                        symbol=sym, side=side, amount=size,
                        stop_loss=sl, take_profit=tp,
                        expected_entry=entry,
                    )
                    if order:
                        risk.invalidate_balance_cache()
                        await alerts.send_trade_alert(
                            side="BUY" if side == "long" else "SELL",
                            symbol=sym,
                            entry_price=entry,
                            stop_loss=sl,
                            take_profit=tp,
                            size=size,
                            leverage=cfg.LEVERAGE,
                            strategy="scalp",
                            regime=result.regime,
                            confidence=result.confidence,
                            atr_value=atr_val,
                        )
                elif size > 0:
                    logger.warning("Trade blocked by risk gate: {}", gate_reason)

            # ========== SWING TRADING CHECK ==========
            _time = time
            if cfg.SWING_ENABLED and swing_strategy is not None:
                now_ts = _time.time()
                if now_ts - last_swing_check >= cfg.SWING_CHECK_INTERVAL:
                    last_swing_check = now_ts
                    logger.info("[SWING] Running swing check across {} symbols...", len(cfg.SWING_SYMBOLS))

                    if not risk.check_swing_max_positions(cfg.SWING_SYMBOLS):
                        logger.info("[SWING] Max swing positions or exposure cap reached – skipping")
                    elif not risk.check_swing_total_exposure():
                        logger.info("[SWING] Total exposure cap exceeded – skipping")
                    else:
                        for swing_sym in cfg.SWING_SYMBOLS:
                            try:
                                # Skip if already have position for this symbol
                                if risk.check_swing_symbol_position(swing_sym):
                                    logger.debug("[SWING] Already have position in {} – skip", swing_sym)
                                    continue

                                swing_df = fetch_swing_ohlcv(exchange, swing_sym)
                                if swing_df is None or swing_df.empty:
                                    continue

                                swing_signal = swing_strategy.calculate_signals(swing_df, swing_sym)

                                if swing_signal.signal in (SwingSignal.BUY, SwingSignal.SELL):
                                    side = swing_signal.signal.value.lower()
                                    entry = swing_signal.entry_price

                                    sl = risk.get_swing_stop_loss(entry, side, swing_sym, atr=swing_signal.atr)
                                    tp = risk.get_swing_take_profit(entry, side, swing_sym)
                                    size = risk.calculate_swing_position_size(entry, sl)

                                    if size > 0:
                                        logger.info(
                                            "[SWING] Executing {} on {} | entry={:.2f} | SL={:.2f} | TP={:.2f} | size={:.6f}",
                                            side.upper(), swing_sym, entry, sl, tp, size,
                                        )

                                        order = executor.open_position(
                                            symbol=swing_sym,
                                            side=side,
                                            amount=size,
                                            stop_loss=sl,
                                            take_profit=tp,
                                            expected_entry=entry,
                                        )

                                        risk.invalidate_balance_cache()

                                        if order:
                                            fill_price = float(order.get("_fill_price", entry))
                                            actual_sl = float(order.get("_actual_sl", sl))
                                            actual_tp = float(order.get("_actual_tp", tp))
                                            await alerts.send_swing_trade_alert(
                                                side=side,
                                                symbol=swing_sym,
                                                entry=fill_price,
                                                sl=actual_sl,
                                                tp=actual_tp,
                                                size=size,
                                                confidence=swing_signal.confidence,
                                                reason=swing_signal.reason,
                                            )
                                        else:
                                            logger.warning("[SWING] Order returned None for {}", swing_sym)
                                    else:
                                        logger.warning("[SWING] Position size is 0 for {} – skipping", swing_sym)

                            except FatalExchangeError:
                                raise
                            except Exception as swing_exc:
                                logger.error("[SWING] Error processing {}: {}", swing_sym, swing_exc)
                                continue

            # ========== POSITION MONITOR (SL/TP detection) ==========
            _time2 = time
            now_pm = _time2.time()
            if now_pm - last_position_check >= cfg.POSITION_MONITOR_INTERVAL:
                last_position_check = now_pm
                for sym in all_monitored_symbols:
                    try:
                        current_pos = executor.get_position_info(sym)
                        was_known = sym in known_positions

                        if was_known and current_pos is None:
                            # Position disappeared -> SL/TP was hit by exchange
                            old = known_positions.pop(sym)
                            logger.info(
                                "[MONITOR] Position closed by exchange: {} {} {}",
                                old["side"].upper(), sym, old["contracts"],
                            )
                            # We don't know the exact exit price from the exchange fill,
                            # but we can estimate from last known price
                            try:
                                ticker = exchange.fetch_ticker(sym)
                                exit_price = float(ticker.get("last", old["entry_price"]))
                            except Exception:
                                exit_price = old["entry_price"]  # fallback

                            # Determine trade type based on symbol
                            trade_type = "swing" if sym in cfg.SWING_SYMBOLS and sym != cfg.SYMBOL else "scalp"
                            # Determine reason - if position vanished, likely SL or TP
                            entry_p = old["entry_price"]
                            side = old["side"]
                            if side == "long":
                                reason = "tp" if exit_price > entry_p else "sl"
                            else:
                                reason = "tp" if exit_price < entry_p else "sl"

                            record = await tracker.record_trade(
                                symbol=sym,
                                side=side,
                                trade_type=trade_type,
                                entry_price=entry_p,
                                exit_price=exit_price,
                                size=old["contracts"],
                                reason=reason,
                                entry_time=old.get("entry_time"),
                                scoring=last_scoring.get(sym),
                            )
                            risk.record_trade_pnl(record["pnl_usdt"], record["is_win"])
                            risk.invalidate_balance_cache()

                            if record:
                                await alerts.send_close_alert(
                                    side=side,
                                    symbol=sym,
                                    entry_price=entry_p,
                                    exit_price=exit_price,
                                    pnl=record["pnl_usdt"],
                                    pnl_pct=record["pnl_pct"],
                                    strategy=trade_type,
                                    exit_reason=reason.upper(),
                                )

                        elif not was_known and current_pos is not None:
                            # New position appeared (opened by us or externally)
                            known_positions[sym] = {
                                "side": current_pos["side"],
                                "entry_price": current_pos["entry_price"],
                                "contracts": current_pos["contracts"],
                                "entry_time": datetime.now(timezone.utc).isoformat(),
                            }
                            logger.info(
                                "[MONITOR] New position tracked: {} {} {} @ {:.2f}",
                                current_pos["side"].upper(), sym,
                                current_pos["contracts"], current_pos["entry_price"],
                            )

                        elif was_known and current_pos is not None:
                            # Update known position info (contracts/entry might change)
                            known_positions[sym]["contracts"] = current_pos["contracts"]
                            known_positions[sym]["entry_price"] = current_pos["entry_price"]

                    except Exception as mon_exc:
                        logger.debug("[MONITOR] Error checking {}: {}", sym, mon_exc)

            # ── Phase 2: Periodic weight optimization (weekly) ──
            _time_opt = time
            now_opt = _time_opt.time()
            if now_opt - last_optimize_time >= 86400 * 7:  # 7 days
                if len(tracker._trades) >= 30:
                    try:
                        logger.info("Starting weight optimization cycle...")
                        success = await optimizer.run_optimization_cycle()
                        if success:
                            signal_scoring._load_weights()  # reload updated weights
                            logger.info("Weight optimization completed successfully")
                        last_optimize_time = now_opt
                    except Exception as opt_exc:
                        logger.error("Weight optimization failed: {}", opt_exc)
                        last_optimize_time = now_opt  # don't retry immediately

        # --- Fix 3: Fatal error = shutdown, transient = retry ---
        except FatalExchangeError as exc:
            logger.critical("FATAL ERROR – shutting down: {}", exc)
            await alerts.send_error_alert(f"FATAL: {exc}")
            shutdown_event.set()
            break

        except Exception as exc:
            logger.exception("Unhandled error in main loop: {}", exc)
            await alerts.send_error_alert(exc)

        # 6. Sleep (timeframe-aware interval)
        _loop_sleep = _TF_INTERVALS.get(cfg.TIMEFRAME, cfg.LOOP_INTERVAL)
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=_loop_sleep
            )
        except asyncio.TimeoutError:
            pass  # normal – just loop again

    # --- Graceful shutdown ------------------------------------------------
    # Cancel the /stats poller
    stats_task.cancel()
    try:
        await stats_task
    except asyncio.CancelledError:
        pass

    logger.info("Shutting down – closing open positions...")
    try:
        executor.close_position(cfg.SYMBOL)
        executor.cancel_all_orders(cfg.SYMBOL)
        # Close swing positions too
        if cfg.SWING_ENABLED:
            for sym in cfg.SWING_SYMBOLS:
                try:
                    executor.close_position(sym)
                    executor.cancel_all_orders(sym)
                except Exception as swing_exc:
                    logger.error("Swing cleanup error for {}: {}", sym, swing_exc)
    except Exception as exc:
        logger.error("Cleanup error: {}", exc)

    await alerts.send_shutdown_message(reason="Graceful shutdown (signal)")
    logger.info("Alpha-Scalp Bot stopped.")


# ===================================================================== #
#                   MODE A: WEBSOCKET EVENT-DRIVEN                       #
# ===================================================================== #

async def run_bot_ws() -> None:
    """WebSocket-driven event loop.

    Architecture:
        BinanceWSManager -> MarketState -> StateChangeDispatcher -> Callbacks
                                                                      |
                                                            Alpha Pipeline -> Trade
    """
    if not _WS_AVAILABLE:
        logger.error("WebSocket modules not importable - cannot start WS mode")
        sys.exit(1)

    logger.info("Initialising Alpha-Scalp Bot (WebSocket mode)...")

    # --- Core components (same as polling) --------------------------------
    try:
        exchange = _create_exchange()
    except Exception as exc:
        import traceback as _tb
        logger.critical("Exchange init failed: {}: {}", type(exc).__name__, exc)
        logger.critical("Full traceback:\n{}", _tb.format_exc())
        sys.exit(1)

    risk = RiskEngine(exchange)
    executor = OrderExecutor(exchange, risk)
    alerts = TelegramAlerts()
    tracker = TradeTrackerV2()
    risk.set_trade_tracker(tracker)

    feature_cache = FeatureCache()
    alpha_engine = AlphaEngine()
    signal_scoring = SignalScoring()
    optimizer = WeightOptimizer(tracker)
    last_optimize_time = 0.0
    last_scoring: dict = {}

    executor.set_margin_type(cfg.SYMBOL)
    executor.set_leverage(cfg.SYMBOL, cfg.LEVERAGE)

    # Swing init
    swing_strategy = SwingStrategy() if cfg.SWING_ENABLED else None
    last_swing_check = 0.0
    if cfg.SWING_ENABLED:
        for sym in cfg.SWING_SYMBOLS:
            executor.set_margin_type(sym)
            executor.set_leverage(sym, cfg.SWING_LEVERAGE)
        logger.info("Swing trading ENABLED for {} symbols", len(cfg.SWING_SYMBOLS))

    # Position monitor state
    known_positions: dict[str, dict] = {}
    last_position_check: float = 0.0
    all_monitored_symbols = [cfg.SYMBOL]
    if cfg.SWING_ENABLED:
        all_monitored_symbols.extend(s for s in cfg.SWING_SYMBOLS if s != cfg.SYMBOL)
    for sym in all_monitored_symbols:
        pos_info = executor.get_position_info(sym)
        if pos_info:
            known_positions[sym] = {
                "side": pos_info["side"],
                "entry_price": pos_info["entry_price"],
                "contracts": pos_info["contracts"],
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }
    logger.info("Position monitor: {} known positions at startup", len(known_positions))

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Shutdown ---------------------------------------------------------
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: int, _frame) -> None:
        sig_name = signal.Signals(sig).name
        logger.warning("Received {} - initiating graceful shutdown", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # --- MarketState + WSManager ------------------------------------------
    market_state = MarketState(
        symbol=cfg.SYMBOL,
        candle_history=cfg.WS_CANDLE_HISTORY,
        book_depth=cfg.WS_BOOK_DEPTH,
        price_jump_threshold_bps=cfg.WS_PRICE_JUMP_BPS,
    )

    ws_manager = BinanceWSManager(
        state=market_state,
        interval=cfg.TIMEFRAME,
        book_depth_limit=cfg.WS_BOOK_DEPTH * 50,
        on_connected=lambda: logger.info("WS connected for {}", cfg.SYMBOL),
        on_disconnected=lambda: logger.warning("WS disconnected for {}", cfg.SYMBOL),
    )

    # --- Pipeline callbacks -----------------------------------------------

    async def on_candle_complete(state: MarketState, meta: dict) -> None:
        """Full alpha pipeline on each completed candle."""
        nonlocal last_optimize_time, current_date, last_swing_check, last_position_check

        # Midnight reset
        is_new_day, today = _is_new_utc_day(current_date)
        if is_new_day:
            logger.info("UTC midnight crossed - resetting daily stats")
            cumulative = tracker.get_cumulative_stats()
            summary = risk.reset_daily()
            await alerts.send_daily_summary(
                pnl=summary["pnl"], trades=summary["trades"],
                win_rate=summary["win_rate"],
                start_balance=summary["start_balance"],
                end_balance=summary["end_balance"],
                cumulative_stats=cumulative,
            )
            current_date = today

        # Kill switch
        if risk.check_kill_switch():
            if not getattr(run_bot_ws, "_ks_alerted", False):
                await alerts.send_kill_switch_alert()
                run_bot_ws._ks_alerted = True
            logger.warning("Kill switch active - skipping candle")
            return
        else:
            run_bot_ws._ks_alerted = False

        # Get candle DataFrame from MarketState
        df = state.get_candle_df()
        if df is None or len(df) < 50:
            logger.warning("WS: Not enough candles yet ({}/50)", len(df) if df is not None else 0)
            return

        # Alpha pipeline
        sym = cfg.SYMBOL
        features = feature_cache.compute(df)
        votes = alpha_engine.generate_votes(features)
        result = signal_scoring.score(votes, features)

        logger.info(
            "[WS] Alpha signal | {} | score={:+.2f} confidence={:.0f}% action={} regime={}",
            sym, result.score, result.confidence * 100, result.action, result.regime,
        )

        # Execute trade (same logic as polling mode)
        if result.action in ("BUY", "SELL"):
            side = "long" if result.action == "BUY" else "short"
            entry = features.close
            atr_val = features.atr
            last_scoring[sym] = result.as_dict()

            sl = risk.get_stop_loss(entry, side, atr=atr_val, regime=result.regime)
            tp = risk.get_take_profit(entry, side, atr=atr_val, regime=result.regime)
            base_size = risk.calculate_position_size(entry, sl)
            confidence_scale = 0.5 + (result.confidence * 0.5)
            size = round(base_size * confidence_scale, 6)

            can_trade, gate_reason = risk.can_open_trade()
            if size > 0 and can_trade:
                logger.info(
                    "Opening {} {} | entry={:.2f} sl={:.2f} tp={:.2f} | "
                    "size={:.6f} (conf_scale={:.0f}%) | score={:+.2f}",
                    side.upper(), sym, entry, sl, tp,
                    size, confidence_scale * 100, result.score,
                )
                order = executor.open_position(
                    symbol=sym, side=side, amount=size,
                    stop_loss=sl, take_profit=tp, expected_entry=entry,
                )
                if order:
                    risk.invalidate_balance_cache()
                    await alerts.send_trade_alert(
                        side="BUY" if side == "long" else "SELL",
                        symbol=sym, entry_price=entry,
                        stop_loss=sl, take_profit=tp, size=size,
                        leverage=cfg.LEVERAGE, strategy="scalp",
                        regime=result.regime, confidence=result.confidence,
                        atr_value=atr_val,
                    )
            elif size > 0:
                logger.warning("Trade blocked by risk gate: {}", gate_reason)

        # Swing check
        if cfg.SWING_ENABLED and swing_strategy is not None:
            now_ts = time.time()
            if now_ts - last_swing_check >= cfg.SWING_CHECK_INTERVAL:
                last_swing_check = now_ts
                try:
                    logger.info("[SWING] Running swing check across {} symbols...", len(cfg.SWING_SYMBOLS))
                    if risk.check_swing_max_positions(cfg.SWING_SYMBOLS) and risk.check_swing_total_exposure():
                        for swing_sym in cfg.SWING_SYMBOLS:
                            try:
                                if risk.check_swing_symbol_position(swing_sym):
                                    continue
                                swing_df = fetch_swing_ohlcv(exchange, swing_sym)
                                if swing_df is None or swing_df.empty:
                                    continue
                                swing_signal = swing_strategy.calculate_signals(swing_df, swing_sym)
                                if swing_signal.signal in (SwingSignal.BUY, SwingSignal.SELL):
                                    s_side = swing_signal.signal.value.lower()
                                    s_entry = swing_signal.entry_price
                                    s_sl = risk.get_swing_stop_loss(s_entry, s_side, swing_sym, atr=swing_signal.atr)
                                    s_tp = risk.get_swing_take_profit(s_entry, s_side, swing_sym)
                                    s_size = risk.calculate_swing_position_size(s_entry, s_sl)
                                    if s_size > 0:
                                        order = executor.open_position(
                                            symbol=swing_sym, side=s_side, amount=s_size,
                                            stop_loss=s_sl, take_profit=s_tp, expected_entry=s_entry,
                                        )
                                        risk.invalidate_balance_cache()
                                        if order:
                                            fill_price = float(order.get("_fill_price", s_entry))
                                            actual_sl = float(order.get("_actual_sl", s_sl))
                                            actual_tp = float(order.get("_actual_tp", s_tp))
                                            await alerts.send_swing_trade_alert(
                                                side=s_side, symbol=swing_sym,
                                                entry=fill_price, sl=actual_sl, tp=actual_tp,
                                                size=s_size, confidence=swing_signal.confidence,
                                                reason=swing_signal.reason,
                                            )
                            except FatalExchangeError:
                                raise
                            except Exception as swing_exc:
                                logger.error("[SWING] Error processing {}: {}", swing_sym, swing_exc)
                except FatalExchangeError:
                    raise
                except Exception as exc:
                    logger.error("[SWING] Error in swing check: {}", exc)

        # Position monitor
        now_pm = time.time()
        if now_pm - last_position_check >= cfg.POSITION_MONITOR_INTERVAL:
            last_position_check = now_pm
            for sym in all_monitored_symbols:
                try:
                    current_pos = executor.get_position_info(sym)
                    was_known = sym in known_positions
                    if was_known and current_pos is None:
                        old = known_positions.pop(sym)
                        try:
                            ticker = exchange.fetch_ticker(sym)
                            exit_price = float(ticker.get("last", old["entry_price"]))
                        except Exception:
                            exit_price = old["entry_price"]
                        trade_type = "swing" if sym in cfg.SWING_SYMBOLS and sym != cfg.SYMBOL else "scalp"
                        entry_p = old["entry_price"]
                        p_side = old["side"]
                        reason = ("tp" if exit_price > entry_p else "sl") if p_side == "long" else ("tp" if exit_price < entry_p else "sl")
                        record = await tracker.record_trade(
                            symbol=sym, side=p_side, trade_type=trade_type,
                            entry_price=entry_p, exit_price=exit_price,
                            size=old["contracts"], reason=reason,
                            entry_time=old.get("entry_time"), scoring=last_scoring.get(sym),
                        )
                        risk.record_trade_pnl(record["pnl_usdt"], record["is_win"])
                        risk.invalidate_balance_cache()
                        if record:
                            await alerts.send_close_alert(
                                side=p_side, symbol=sym, entry_price=entry_p,
                                exit_price=exit_price, pnl=record["pnl_usdt"],
                                pnl_pct=record["pnl_pct"], strategy=trade_type,
                                exit_reason=reason.upper(),
                            )
                    elif not was_known and current_pos is not None:
                        known_positions[sym] = {
                            "side": current_pos["side"],
                            "entry_price": current_pos["entry_price"],
                            "contracts": current_pos["contracts"],
                            "entry_time": datetime.now(timezone.utc).isoformat(),
                        }
                    elif was_known and current_pos is not None:
                        known_positions[sym]["contracts"] = current_pos["contracts"]
                        known_positions[sym]["entry_price"] = current_pos["entry_price"]
                except Exception as mon_exc:
                    logger.debug("[MONITOR] Error checking {}: {}", sym, mon_exc)

        # Weight optimizer (weekly, 30 trades threshold)
        now_opt = time.time()
        if now_opt - last_optimize_time >= 86400 * 7:
            if len(tracker._trades) >= 30:
                try:
                    logger.info("Starting weight optimization cycle...")
                    success = await optimizer.run_optimization_cycle()
                    if success:
                        signal_scoring._load_weights()
                        logger.info("Weight optimization completed successfully")
                    last_optimize_time = now_opt
                except Exception as opt_exc:
                    logger.error("Weight optimization failed: {}", opt_exc)
                    last_optimize_time = now_opt

    async def on_book_update(state: MarketState, meta: dict) -> None:
        """Lightweight spread/imbalance refresh on book updates."""
        book = state.get_book_snapshot()
        spread_bps = book.get("spread_bps", float("inf"))
        imbalance = book.get("imbalance", 0.5)
        if spread_bps > 50:
            logger.warning("[WS] Wide spread: {:.1f} bps | imbalance={:.2f}", spread_bps, imbalance)

    async def on_price_jump(state: MarketState, meta: dict) -> None:
        """Urgent re-score on large price moves."""
        direction = meta.get("direction", "unknown")
        move_bps = meta.get("move_bps", 0)
        logger.info("[WS] Price jump {} {:.1f} bps - re-scoring", direction, move_bps)

        df = state.get_candle_df()
        if df is None or len(df) < 50:
            return

        features = feature_cache.compute(df)
        votes = alpha_engine.generate_votes(features)
        result = signal_scoring.score(votes, features)

        # Only act if confidence is very high on a price jump
        if result.confidence >= 0.8 and result.action in ("BUY", "SELL"):
            logger.info(
                "[WS] Price jump re-score: {} confidence={:.0f}% - executing",
                result.action, result.confidence * 100,
            )
            side = "long" if result.action == "BUY" else "short"
            entry = features.close
            atr_val = features.atr
            last_scoring[cfg.SYMBOL] = result.as_dict()
            sl = risk.get_stop_loss(entry, side, atr=atr_val, regime=result.regime)
            tp = risk.get_take_profit(entry, side, atr=atr_val, regime=result.regime)
            base_size = risk.calculate_position_size(entry, sl)
            confidence_scale = 0.5 + (result.confidence * 0.5)
            size = round(base_size * confidence_scale, 6)
            can_trade, gate_reason = risk.can_open_trade()
            if size > 0 and can_trade:
                order = executor.open_position(
                    symbol=cfg.SYMBOL, side=side, amount=size,
                    stop_loss=sl, take_profit=tp, expected_entry=entry,
                )
                if order:
                    risk.invalidate_balance_cache()
                    await alerts.send_trade_alert(
                        side="BUY" if side == "long" else "SELL",
                        symbol=cfg.SYMBOL, entry_price=entry,
                        stop_loss=sl, take_profit=tp, size=size,
                        leverage=cfg.LEVERAGE, strategy="scalp",
                        regime=result.regime, confidence=result.confidence,
                        atr_value=atr_val,
                    )

    async def on_book_invalidated(state: MarketState, meta: dict) -> None:
        """Pause trading when order book has a sequence gap."""
        logger.warning(
            "[WS] Order book INVALIDATED for {} - trading paused until rebuild",
            cfg.SYMBOL,
        )
        await alerts.send_error_alert(
            f"Order book invalidated for {cfg.SYMBOL} - trading paused, awaiting snapshot rebuild"
        )

    # --- Assemble dispatcher ----------------------------------------------
    callbacks = PipelineCallbacks(
        on_candle_complete=on_candle_complete,
        on_book_update=on_book_update,
        on_price_jump=on_price_jump,
        on_book_invalidated=on_book_invalidated,
    )

    dispatcher = StateChangeDispatcher(
        state=market_state,
        callbacks=callbacks,
        queue_maxsize=1000,
        poll_interval_ms=50.0,
        coalesce_window_ms=100.0,
        max_processing_time_s=5.0,
    )

    # --- Start everything -------------------------------------------------
    await alerts.send_startup_message()

    stats_task = asyncio.create_task(
        _stats_poller(alerts, tracker, shutdown_event)
    )

    await ws_manager.start()
    logger.info("WebSocket manager started - waiting for market data...")

    # Wait for MarketState to be ready (enough candles seeded)
    for _ in range(60):
        if market_state.is_ready:
            break
        await asyncio.sleep(1)

    if not market_state.is_ready:
        logger.warning(
            "MarketState not fully ready after 60s (candles={}, book={}) - starting dispatcher anyway",
            market_state.candles.history_len,
            market_state.book.initialized,
        )
    else:
        logger.info(
            "MarketState ready: {} candles, book {}, last price={:.2f}",
            market_state.candles.history_len,
            "OK" if market_state.book.initialized else "pending",
            market_state.last_trade_price,
        )

    await dispatcher.start()
    logger.info("Dispatcher started - bot is LIVE in WebSocket mode")

    # --- Keep alive until shutdown ----------------------------------------
    bot_start_time = time.time()
    last_heartbeat = bot_start_time
    HEARTBEAT_INTERVAL = 1800  # 30 minutes

    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            # Health check
            if not ws_manager.is_healthy:
                logger.warning("[WS] Health check FAILED: {}", ws_manager.metrics())

            d_metrics = dispatcher.metrics()
            if d_metrics.get("pipeline_runs", 0) > 0:
                logger.info(
                    "[WS] Dispatcher: runs={} errors={} dropped={} avg={:.1f}ms",
                    d_metrics["pipeline_runs"], d_metrics["pipeline_errors"],
                    d_metrics["events_dropped"], d_metrics["avg_pipeline_ms"],
                )

            # --- Periodic heartbeat alert (every 30 min) ------------------
            now_hb = time.time()
            if now_hb - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now_hb
                uptime_secs = int(now_hb - bot_start_time)
                hours, remainder = divmod(uptime_secs, 3600)
                mins, _ = divmod(remainder, 60)
                uptime_str = f"{hours}h {mins}m"

                # Gather latest state for heartbeat
                last_result = last_scoring.get(cfg.SYMBOL, {})
                hb_signal = last_result.get("action", "HOLD")
                hb_score = last_result.get("score", 0.0)
                hb_regime = last_result.get("regime", "UNKNOWN")
                hb_book_ok = market_state.book.initialized if hasattr(market_state, "book") else True
                hb_spread = 0.0
                try:
                    book_snap = market_state.get_book_snapshot()
                    hb_spread = book_snap.get("spread_bps", 0.0)
                except Exception:
                    pass
                session_stats = tracker.get_session_stats()
                hb_trades = session_stats.get("total_trades", 0)
                hb_pnl = session_stats.get("total_pnl", 0.0)

                await alerts.send_heartbeat(
                    uptime_str=uptime_str,
                    last_signal=hb_signal,
                    last_score=hb_score,
                    regime=hb_regime,
                    book_ok=hb_book_ok,
                    total_trades=hb_trades,
                    session_pnl=hb_pnl,
                    spread_bps=hb_spread if hb_spread != float("inf") else 9999.9,
                )

            # Midnight reset (in case no candle fires near midnight)
            is_new_day, today = _is_new_utc_day(current_date)
            if is_new_day:
                cumulative = tracker.get_cumulative_stats()
                summary = risk.reset_daily()
                await alerts.send_daily_summary(
                    pnl=summary["pnl"], trades=summary["trades"],
                    win_rate=summary["win_rate"],
                    start_balance=summary["start_balance"],
                    end_balance=summary["end_balance"],
                    cumulative_stats=cumulative,
                )
                current_date = today

    # --- Graceful shutdown ------------------------------------------------
    logger.info("Shutting down WebSocket mode...")
    await dispatcher.stop()
    await ws_manager.stop()

    stats_task.cancel()
    try:
        await stats_task
    except asyncio.CancelledError:
        pass

    logger.info("Closing open positions...")
    try:
        executor.close_position(cfg.SYMBOL)
        executor.cancel_all_orders(cfg.SYMBOL)
        if cfg.SWING_ENABLED:
            for sym in cfg.SWING_SYMBOLS:
                try:
                    executor.close_position(sym)
                    executor.cancel_all_orders(sym)
                except Exception as swing_exc:
                    logger.error("Swing cleanup error for {}: {}", sym, swing_exc)
    except Exception as exc:
        logger.error("Cleanup error: {}", exc)

    await alerts.send_shutdown_message(reason="Graceful shutdown (signal)")
    logger.info("Alpha-Scalp Bot stopped (WebSocket mode).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Synchronous entry point - picks WS or polling mode."""
    use_ws = cfg.USE_WEBSOCKET and _WS_AVAILABLE

    if cfg.USE_WEBSOCKET and not _WS_AVAILABLE:
        logger.warning(
            "USE_WEBSOCKET=true but WS modules not importable - falling back to polling"
        )

    mode = "WebSocket" if use_ws else "Polling"
    logger.info("Starting Alpha-Scalp Bot in {} mode", mode)

    try:
        if use_ws:
            asyncio.run(run_bot_ws())
        else:
            asyncio.run(run_bot_polling())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt - exiting")
    except Exception as exc:
        logger.critical("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
