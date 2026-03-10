"""Alpha-Scalp Bot – Main Entry Point.

Orchestrates the full scalping loop:
1. Initialise exchange, strategy, risk engine, order executor, alerts
2. Continuously fetch candles, generate signals, execute trades
3. Enforce kill-switch and daily reset at UTC midnight
4. Graceful shutdown on SIGINT / SIGTERM
5. Fatal error classification – auto-shutdown on auth/permission errors

Usage:
    python main.py
"""

from __future__ import annotations

import asyncio
import signal
import sys
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
from trade_tracker import TradeTracker

# Timeframe-aware loop intervals (seconds)
_TF_INTERVALS = {"1m": 5, "3m": 15, "5m": 25}


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------
def _create_exchange() -> ccxt.Exchange:
    """Instantiate and configure the Binance Futures CCXT client."""
    common_cfg = {
        "apiKey": cfg.BINANCE_API_KEY,
        "secret": cfg.BINANCE_SECRET,
        "enableRateLimit": True,
        "options": {
            "adjustForTimeDifference": True,
        },
    }

    if cfg.BINANCE_DEMO_TRADING:
        # Binance Demo Trading (replaced old testnet/sandbox)
        # Requires CCXT >= 4.5.6 and demo-specific API keys from:
        # https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
        exchange = ccxt.binance({**common_cfg, "options": {**common_cfg["options"], "defaultType": "future"}})
        exchange.enable_demo_trading(True)
        logger.info("Exchange: Binance Futures DEMO TRADING (paper)")
    else:
        exchange = ccxt.binance({**common_cfg, "options": {**common_cfg["options"], "defaultType": "future"}})
        logger.warning("Exchange: Binance Futures LIVE – real funds at risk")

    # Verify connectivity
    exchange.load_markets()
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
    tracker: TradeTracker,
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
                        await alerts.send_stats_message(session_stats, cumulative_stats)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("/stats poller error: {}", exc)
            await asyncio.sleep(5)

    logger.info("/stats command polling stopped")


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------
async def run_bot() -> None:  # noqa: C901 – intentionally cohesive
    """Core trading loop."""

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
    tracker = TradeTracker()
    risk.set_trade_tracker(tracker)

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
                if not getattr(run_bot, "_ks_alerted", False):
                    await alerts.send_kill_switch_alert()
                    run_bot._ks_alerted = True  # type: ignore[attr-defined]
                logger.warning(
                    "Kill switch active – sleeping {} s", cfg.LOOP_INTERVAL * 12
                )
                await asyncio.sleep(cfg.LOOP_INTERVAL * 12)  # back off
                continue
            else:
                run_bot._ks_alerted = False  # type: ignore[attr-defined]

            # 3. Fetch candles
            df = fetch_ohlcv(exchange)
            if df is None or df.empty:
                logger.warning("No candle data – retrying in {} s", cfg.LOOP_INTERVAL)
                await asyncio.sleep(cfg.LOOP_INTERVAL)
                continue

            # 4. Generate signal
            trade_signal = strategy.calculate_signals(df)

            # 5. Act on signal
            if trade_signal.signal in (Signal.BUY, Signal.SELL):
                # Check if we can open a new position
                can_trade = risk.check_max_positions()
                if not can_trade:
                    logger.info("Max positions reached – skipping signal")
                else:
                    side = trade_signal.signal.value.lower()  # "buy" / "sell"
                    entry = trade_signal.entry_price

                    atr_val = getattr(trade_signal, 'atr', 0.0)
                    sl = risk.get_stop_loss(entry, side, atr=atr_val)
                    tp = risk.get_take_profit(entry, side, atr=atr_val)
                    size = risk.calculate_position_size(entry, sl)

                    if size > 0:
                        logger.info(
                            "Executing {} | entry={:.2f} | SL={:.2f} | TP={:.2f} | size={:.6f}",
                            side.upper(),
                            entry,
                            sl,
                            tp,
                            size,
                        )

                        order = executor.open_position(
                            symbol=cfg.SYMBOL,
                            side=side,
                            amount=size,
                            stop_loss=sl,
                            take_profit=tp,
                            expected_entry=entry,  # Fix 1: slippage check
                        )

                        # Invalidate balance cache after trade execution
                        risk.invalidate_balance_cache()

                        if order:
                            fill_price = float(order.get("_fill_price", entry))
                            actual_sl = float(order.get("_actual_sl", sl))
                            actual_tp = float(order.get("_actual_tp", tp))
                            await alerts.send_trade_alert(
                                side=side,
                                symbol=cfg.SYMBOL,
                                entry=fill_price,
                                sl=actual_sl,
                                tp=actual_tp,
                                size=size,
                            )
                        else:
                            logger.warning(
                                "Order returned None – slippage reject or bracket failure"
                            )
                    else:
                        logger.warning("Position size is 0 – skipping")

            # ========== SWING TRADING CHECK ==========
            import time as _time
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
            import time as _time2
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

                            record = await risk.record_trade_full(
                                symbol=sym,
                                side=side,
                                trade_type=trade_type,
                                entry_price=entry_p,
                                exit_price=exit_price,
                                size=old["contracts"],
                                reason=reason,
                                entry_time=old.get("entry_time"),
                            )
                            risk.invalidate_balance_cache()

                            if record:
                                await alerts.send_trade_close_alert(
                                    symbol=sym,
                                    side=side,
                                    trade_type=trade_type,
                                    entry_price=entry_p,
                                    exit_price=exit_price,
                                    pnl_usdt=record["pnl_usdt"],
                                    pnl_pct=record["pnl_pct"],
                                    reason=reason,
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Synchronous entry point."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt – exiting")
    except Exception as exc:
        logger.critical("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
