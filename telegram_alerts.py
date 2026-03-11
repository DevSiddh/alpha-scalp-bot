"""Alpha-Scalp Bot – Premium Telegram Alert Module.

All alerts sent via Telegram Bot API with HTML formatting.
Premium features:
- Trade entry alerts show: volume multiplier, regime, BB squeeze, Kelly fraction
- ATR trailing stop activation alerts
- Bollinger squeeze breakout alerts
- Daily P&L circuit breaker alerts
- Concurrent trade limit warnings
- Regime change notifications
- Enhanced trade close alerts (trailing stop exits)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

import config as cfg

_TG_API = "https://api.telegram.org"


class TelegramAlerts:
    """Async Telegram alert sender with rate-limit guard."""

    def __init__(self) -> None:
        self.bot_token: str = cfg.TELEGRAM_BOT_TOKEN
        self.chat_id: str = cfg.TELEGRAM_CHAT_ID
        self.enabled: bool = bool(self.bot_token and self.chat_id)
        self._rate_lock = asyncio.Lock()
        self._last_send: float = 0.0
        self._MIN_INTERVAL: float = 1.0  # seconds between messages

        if not self.enabled:
            logger.warning("Telegram alerts disabled – missing BOT_TOKEN or CHAT_ID")

    # =================================================================
    # Core send with rate-limit
    # =================================================================
    async def send_message(self, text: str) -> bool:
        """Send an HTML-formatted message with rate limiting."""
        if not self.enabled:
            return False

        async with self._rate_lock:
            now = asyncio.get_event_loop().time()
            wait = self._MIN_INTERVAL - (now - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)

            url = f"{_TG_API}/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                self._last_send = asyncio.get_event_loop().time()
                return True
            except Exception as exc:
                logger.error("Telegram send failed: {}", exc)
                return False

    # =================================================================
    # PREMIUM: Trade Entry Alert (enhanced)
    # =================================================================
    async def send_trade_alert(
        self,
        side: str,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        size: float,
        leverage: int,
        strategy: str = "scalp",
        *,
        volume_mult: float | None = None,
        regime: str | None = None,
        bb_squeeze: bool = False,
        kelly_fraction: float | None = None,
        atr_value: float | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Send enriched trade entry alert with premium signal metadata."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        icon = "LONG" if side.upper() == "BUY" else "SHORT"
        tag = strategy.upper()

        # Premium signal details
        premium_lines = []
        if volume_mult is not None:
            vol_icon = "HIGH" if volume_mult >= 2.0 else "OK"
            premium_lines.append(f"Volume   : <code>{volume_mult:.1f}x avg</code> [{vol_icon}]")
        if regime:
            regime_display = regime.upper()
            premium_lines.append(f"Regime   : <code>{regime_display}</code>")
        if bb_squeeze:
            premium_lines.append(f"BB Squeeze: <code>BREAKOUT DETECTED</code>")
        if kelly_fraction is not None:
            premium_lines.append(f"Kelly Size: <code>{kelly_fraction:.1%} of bankroll</code>")
        if atr_value is not None:
            premium_lines.append(f"ATR      : <code>{atr_value:.4f}</code>")
        if confidence is not None:
            conf_label = "STRONG" if confidence >= 0.75 else "MODERATE" if confidence >= 0.5 else "WEAK"
            premium_lines.append(f"Signal   : <code>{confidence:.0%} confidence</code> [{conf_label}]")

        premium_block = ""
        if premium_lines:
            premium_block = (
                f"\n"
                f"<b>-- Signal Intel --</b>\n"
                + "\n".join(premium_lines)
                + "\n"
            )

        # Risk/Reward ratio
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr = reward / risk if risk > 0 else 0

        text = (
            f"<b>[{icon}] {tag} Entry | {symbol}</b>\n"
            f"\n"
            f"Entry  : <code>{entry_price:,.4f}</code>\n"
            f"SL     : <code>{stop_loss:,.4f}</code>\n"
            f"TP     : <code>{take_profit:,.4f}</code>\n"
            f"Size   : <code>{size:.4f}</code> @ <code>{leverage}x</code>\n"
            f"R:R    : <code>1:{rr:.1f}</code>\n"
            f"{premium_block}"
            f"\n"
            f"<i>{now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: Trade Close Alert (enhanced with trailing stop info)
    # =================================================================
    async def send_close_alert(
        self,
        side: str,
        symbol: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        strategy: str = "scalp",
        *,
        exit_reason: str = "TP/SL",
        trailing_stop_used: bool = False,
        peak_price: float | None = None,
        hold_duration: str | None = None,
    ) -> bool:
        """Send enriched trade close alert."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        result = "WIN" if pnl >= 0 else "LOSS"
        tag = strategy.upper()

        # Exit reason details
        exit_lines = []
        exit_lines.append(f"Exit Via : <code>{exit_reason}</code>")
        if trailing_stop_used:
            exit_lines.append(f"Trail SL : <code>YES (ATR-based)</code>")
            if peak_price is not None:
                exit_lines.append(f"Peak     : <code>{peak_price:,.4f}</code>")
        if hold_duration:
            exit_lines.append(f"Duration : <code>{hold_duration}</code>")

        exit_block = "\n".join(exit_lines) + "\n" if exit_lines else ""

        text = (
            f"<b>[{result}] {tag} Close | {symbol}</b>\n"
            f"\n"
            f"Entry  : <code>{entry_price:,.4f}</code>\n"
            f"Exit   : <code>{exit_price:,.4f}</code>\n"
            f"P&L    : <code>{pnl:+,.2f} USDT ({pnl_pct:+.2f}%)</code>\n"
            f"{exit_block}"
            f"\n"
            f"<i>{now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: ATR Trailing Stop Activation
    # =================================================================
    async def send_trailing_stop_alert(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        trailing_stop_price: float,
        unrealized_pnl_pct: float,
        atr_value: float,
    ) -> bool:
        """Alert when ATR trailing stop activates after min-profit threshold."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        direction = "LONG" if side.upper() == "BUY" else "SHORT"

        text = (
            f"<b>[TRAIL] Trailing Stop Active | {symbol}</b>\n"
            f"\n"
            f"Side     : <code>{direction}</code>\n"
            f"Entry    : <code>{entry_price:,.4f}</code>\n"
            f"Current  : <code>{current_price:,.4f}</code>\n"
            f"Trail SL : <code>{trailing_stop_price:,.4f}</code>\n"
            f"P&L      : <code>{unrealized_pnl_pct:+.2f}%</code>\n"
            f"ATR      : <code>{atr_value:.4f}</code>\n"
            f"\n"
            f"<i>Stop ratchets only in your favor | {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: Bollinger Squeeze Breakout
    # =================================================================
    async def send_squeeze_alert(
        self,
        symbol: str,
        direction: str,
        bb_width: float,
        squeeze_threshold: float,
        current_price: float,
    ) -> bool:
        """Alert when Bollinger Band squeeze breakout is detected."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        dir_label = direction.upper()  # "BULLISH" or "BEARISH"

        text = (
            f"<b>[SQUEEZE] BB Breakout | {symbol}</b>\n"
            f"\n"
            f"Direction : <code>{dir_label}</code>\n"
            f"Price     : <code>{current_price:,.4f}</code>\n"
            f"BB Width  : <code>{bb_width:.4f}</code>\n"
            f"Threshold : <code>{squeeze_threshold:.4f}</code>\n"
            f"\n"
            f"<i>Compression released — expect volatility | {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: Regime Change Notification
    # =================================================================
    async def send_regime_change_alert(
        self,
        symbol: str,
        old_regime: str,
        new_regime: str,
        adx_value: float,
    ) -> bool:
        """Alert when market regime changes (trending <-> ranging)."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        text = (
            f"<b>[REGIME] Market Shift | {symbol}</b>\n"
            f"\n"
            f"From : <code>{old_regime.upper()}</code>\n"
            f"To   : <code>{new_regime.upper()}</code>\n"
            f"ADX  : <code>{adx_value:.1f}</code>\n"
            f"\n"
            f"<i>Strategy parameters auto-adjusted | {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: Concurrent Trade Limit Warning
    # =================================================================
    async def send_max_trades_alert(
        self,
        current_open: int,
        max_allowed: int,
        rejected_symbol: str | None = None,
    ) -> bool:
        """Alert when a new trade is blocked by concurrent trade limiter."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        rejected_line = ""
        if rejected_symbol:
            rejected_line = f"Rejected : <code>{rejected_symbol}</code>\n"

        text = (
            f"<b>[LIMIT] Max Trades Reached</b>\n"
            f"\n"
            f"Open     : <code>{current_open}/{max_allowed}</code>\n"
            f"{rejected_line}"
            f"\n"
            f"<i>New entries blocked until a position closes | {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # PREMIUM: Daily P&L Circuit Breaker
    # =================================================================
    async def send_circuit_breaker_alert(
        self,
        daily_pnl: float,
        daily_pnl_pct: float,
        threshold_pct: float,
        trades_today: int,
    ) -> bool:
        """Alert when daily P&L circuit breaker trips."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        text = (
            f"<b>[CIRCUIT BREAKER] Trading Paused</b>\n"
            f"\n"
            f"Daily P&L : <code>{daily_pnl:+,.2f} USDT ({daily_pnl_pct:+.2f}%)</code>\n"
            f"Threshold : <code>{threshold_pct:.1%} daily loss</code>\n"
            f"Trades    : <code>{trades_today} today</code>\n"
            f"\n"
            f"<i>Bot paused until next UTC midnight | {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # Stats / Performance (enhanced)
    # =================================================================
    async def send_stats(
        self,
        session_stats: dict[str, Any],
        cumulative_stats: dict[str, Any] | None = None,
        *,
        active_trades: int = 0,
        max_trades: int | None = None,
        daily_pnl: float | None = None,
        circuit_breaker_active: bool = False,
        current_regime: str | None = None,
    ) -> bool:
        """Send performance stats with premium risk status."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        s = session_stats
        c = cumulative_stats or {}

        pf = c.get("profit_factor", 0)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "INF"
        streak = c.get("current_streak", "0")

        # Premium risk status block
        risk_lines = []
        if max_trades is not None:
            risk_lines.append(f"Positions: <code>{active_trades}/{max_trades}</code>")
        if daily_pnl is not None:
            risk_lines.append(f"Day P&L  : <code>{daily_pnl:+,.2f} USDT</code>")
        if circuit_breaker_active:
            risk_lines.append(f"Circuit  : <code>TRIPPED (paused)</code>")
        if current_regime:
            risk_lines.append(f"Regime   : <code>{current_regime.upper()}</code>")

        risk_block = ""
        if risk_lines:
            risk_block = (
                f"\n"
                f"<b>-- Risk Status --</b>\n"
                + "\n".join(risk_lines)
                + "\n"
            )

        text = (
            f"<b>[STATS] Alpha-Scalp Bot</b>\n"
            f"\n"
            f"<b>-- Session --</b>\n"
            f"Trades : <code>{s.get('total_trades', 0)}</code>\n"
            f"Win %  : <code>{s.get('win_rate', 0):.1%}</code>\n"
            f"P&L    : <code>{s.get('total_pnl', 0):+,.2f} USDT</code>\n"
            f"Best   : <code>{s.get('best_trade', 0):+,.2f} USDT</code>\n"
            f"Worst  : <code>{s.get('worst_trade', 0):+,.2f} USDT</code>\n"
            f"\n"
            f"<b>-- All Time --</b>\n"
            f"Trades : <code>{c.get('total_trades', 0)}</code>\n"
            f"Win %  : <code>{c.get('win_rate', 0):.1%}</code>\n"
            f"P&L    : <code>{c.get('total_pnl', 0):+,.2f} USDT</code>\n"
            f"Profit Factor: <code>{pf_str}</code>\n"
            f"Avg Win  : <code>{c.get('avg_win', 0):+,.2f} USDT</code>\n"
            f"Avg Loss : <code>{c.get('avg_loss', 0):+,.2f} USDT</code>\n"
            f"Streak   : <code>{streak} (current)</code>\n"
            f"{risk_block}"
            f"\n"
            f"<i>Updated: {now}</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # Kill-switch alert
    # =================================================================
    async def send_kill_switch_alert(self) -> bool:
        """Send an urgent warning when the daily kill switch activates."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"<b>[WARNING] KILL SWITCH ACTIVATED</b>\n"
            f"\n"
            f"Daily drawdown limit ({cfg.DAILY_DRAWDOWN_LIMIT:.1%}) reached.\n"
            f"All trading halted until next UTC midnight.\n"
            f"\n"
            f"Time: <code>{now}</code>"
        )
        return await self.send_message(text)

    # =================================================================
    # Daily summary (enhanced)
    # =================================================================
    async def send_daily_summary(
        self,
        pnl: float,
        trades: int,
        win_rate: float,
        start_balance: float | None = None,
        end_balance: float | None = None,
        cumulative_stats: dict | None = None,
        *,
        circuit_breaker_trips: int = 0,
        trailing_stop_exits: int = 0,
        regime_changes: int = 0,
        blocked_by_limit: int = 0,
    ) -> bool:
        """Send end-of-day summary with premium metrics."""
        pnl_icon = "PROFIT" if pnl >= 0 else "LOSS"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        balance_line = ""
        if start_balance is not None and end_balance is not None:
            balance_line = (
                f"Balance: <code>{start_balance:,.2f}</code> -> "
                f"<code>{end_balance:,.2f}</code> USDT\n"
            )

        cumulative_section = ""
        if cumulative_stats:
            c = cumulative_stats
            pf = c.get("profit_factor", 0)
            pf_str = f"{pf:.2f}" if pf != float("inf") else "INF"
            cumulative_section = (
                f"\n"
                f"<b>-- All Time --</b>\n"
                f"Total Trades : <code>{c.get('total_trades', 0)}</code>\n"
                f"Win %  : <code>{c.get('win_rate', 0):.1%}</code>\n"
                f"Total P&L : <code>{c.get('total_pnl', 0):+,.2f} USDT</code>\n"
                f"Profit Factor: <code>{pf_str}</code>\n"
                f"Streak : <code>{c.get('current_streak', '0')}</code>\n"
            )

        # Premium daily insights
        premium_lines = []
        if trailing_stop_exits > 0:
            premium_lines.append(f"Trail SL Exits  : <code>{trailing_stop_exits}</code>")
        if circuit_breaker_trips > 0:
            premium_lines.append(f"Circuit Breaks  : <code>{circuit_breaker_trips}</code>")
        if regime_changes > 0:
            premium_lines.append(f"Regime Changes  : <code>{regime_changes}</code>")
        if blocked_by_limit > 0:
            premium_lines.append(f"Trades Blocked  : <code>{blocked_by_limit}</code>")

        premium_section = ""
        if premium_lines:
            premium_section = (
                f"\n"
                f"<b>-- Premium Insights --</b>\n"
                + "\n".join(premium_lines)
                + "\n"
            )

        text = (
            f"<b>[{pnl_icon}] DAILY SUMMARY | {date_str}</b>\n"
            f"\n"
            f"P&L    : <code>{pnl:+,.2f} USDT</code>\n"
            f"Trades : <code>{trades}</code>\n"
            f"Win %  : <code>{win_rate:.1%}</code>\n"
            f"{balance_line}"
            f"{cumulative_section}"
            f"{premium_section}"
            f"\n"
            f"<i>#daily #report</i>"
        )
        return await self.send_message(text)

    # =================================================================
    # Error alert
    # =================================================================
    async def send_error_alert(self, error: str | Exception) -> bool:
        """Send an error notification to the operator."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        text = (
            f"<b>[ERROR] Bot Error</b>\n"
            f"\n"
            f"<code>{str(error)[:500]}</code>\n"
            f"\n"
            f"Time: <code>{now}</code>"
        )
        return await self.send_message(text)

    # =================================================================
    # Startup (enhanced with premium config)
    # =================================================================
    async def send_startup_message(self) -> bool:
        """Announce bot start with premium feature status."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = "DEMO (Paper)" if cfg.BINANCE_DEMO_TRADING else "LIVE"

        # Premium features status
        premium_features = []
        if hasattr(cfg, "VOL_SPIKE_MULT"):
            premium_features.append(f"Volume Filter  : <code>{cfg.VOL_SPIKE_MULT}x spike</code>")
        if hasattr(cfg, "BB_PERIOD"):
            premium_features.append(f"BB Squeeze     : <code>period={cfg.BB_PERIOD}, std={cfg.BB_STD}</code>")
        if hasattr(cfg, "ADX_PERIOD"):
            premium_features.append(
                f"ADX Regime     : <code>trend>{cfg.ADX_TREND_THRESHOLD}, range<{cfg.ADX_RANGE_THRESHOLD}</code>"
            )
        if hasattr(cfg, "KELLY_ENABLED") and cfg.KELLY_ENABLED:
            premium_features.append(f"Kelly Sizing   : <code>ON (fraction={cfg.KELLY_FRACTION})</code>")
        if hasattr(cfg, "ATR_TRAIL_ENABLED") and cfg.ATR_TRAIL_ENABLED:
            premium_features.append(
                f"ATR Trail SL   : <code>{cfg.ATR_TRAIL_MULT}x ATR, activate>{cfg.ATR_TRAIL_ACTIVATE_PCT:.1%}</code>"
            )
        if hasattr(cfg, "MAX_CONCURRENT_TRADES"):
            premium_features.append(f"Max Trades     : <code>{cfg.MAX_CONCURRENT_TRADES} concurrent</code>")
        if hasattr(cfg, "DAILY_PNL_CIRCUIT_PCT"):
            premium_features.append(f"Circuit Breaker: <code>{cfg.DAILY_PNL_CIRCUIT_PCT:.1%} daily loss</code>")

        premium_block = ""
        if premium_features:
            premium_block = (
                f"\n"
                f"<b>-- Premium Features --</b>\n"
                + "\n".join(premium_features)
                + "\n"
            )

        text = (
            f"<b>[START] Alpha-Scalp Bot PREMIUM</b>\n"
            f"\n"
            f"Exchange : <code>Binance Futures</code>\n"
            f"Mode     : <code>{mode}</code>\n"
            f"Symbol   : <code>{cfg.SYMBOL}</code>\n"
            f"TF       : <code>{cfg.TIMEFRAME}</code>\n"
            f"Leverage : <code>{cfg.LEVERAGE}x</code>\n"
            f"Strategy : EMA {cfg.EMA_FAST}/{cfg.EMA_SLOW} + RSI {cfg.RSI_PERIOD} + NW Envelope\n"
        )

        swing_line = ""
        if cfg.SWING_ENABLED:
            swing_line = (
                f"\n"
                f"<b>Swing Mode:</b>\n"
                f"Symbols  : <code>{', '.join(cfg.SWING_SYMBOLS)}</code>\n"
                f"TF       : <code>{cfg.SWING_TIMEFRAME}</code>\n"
                f"Leverage : <code>{cfg.SWING_LEVERAGE}x</code>\n"
                f"SL/TP    : <code>{cfg.SWING_STOP_LOSS_PCT:.1%} / {cfg.SWING_TAKE_PROFIT_PCT:.1%}</code>\n"
                f"Strategy : EMA {cfg.SWING_EMA_FAST}/{cfg.SWING_EMA_SLOW} + RSI + S/R\n"
            )

        text += (
            f"{swing_line}"
            f"{premium_block}"
            f"\n"
            f"Started  : <code>{now}</code>"
        )
        return await self.send_message(text)

    # =================================================================
    # Telegram getUpdates (for /stats command polling)
    # =================================================================
    async def get_updates(self, offset: int = 0, timeout: int = 1) -> tuple[list[dict], int]:
        """Poll Telegram getUpdates for new messages.

        Returns (list_of_updates, new_offset).
        """
        if not self.enabled:
            return [], offset

        url = f"{_TG_API}/bot{self.bot_token}/getUpdates"
        params = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }

        try:
            async with httpx.AsyncClient(timeout=timeout + 5) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            updates = data.get("result", [])
            new_offset = offset
            if updates:
                new_offset = updates[-1]["update_id"] + 1
            return updates, new_offset
        except Exception as exc:
            logger.debug("getUpdates failed: {}", exc)
            return [], offset

    # =================================================================
    # Shutdown
    # =================================================================
    async def send_shutdown_message(self, reason: str = "Manual stop") -> bool:
        """Announce that the bot is shutting down."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = (
            f"<b>[STOP] Alpha-Scalp Bot Offline</b>\n"
            f"\n"
            f"Reason : <code>{reason}</code>\n"
            f"Time   : <code>{now}</code>"
        )
        return await self.send_message(text)

    # =================================================================
    # SWING: Trade Entry Alert
    # =================================================================
    async def send_swing_trade_alert(
        self,
        side: str,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
        size: float,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> bool:
        """Send swing trade entry alert."""
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        icon = "LONG" if side.lower() in ("buy", "long") else "SHORT"

        risk_amt = abs(entry - sl)
        reward_amt = abs(tp - entry)
        rr = reward_amt / risk_amt if risk_amt > 0 else 0

        detail_lines = []
        if confidence is not None:
            conf_label = "STRONG" if confidence >= 0.75 else "MODERATE" if confidence >= 0.5 else "WEAK"
            detail_lines.append(f"Signal   : <code>{confidence:.0%} confidence</code> [{conf_label}]")
        if reason:
            detail_lines.append(f"Reason   : <code>{reason}</code>")

        detail_block = ""
        if detail_lines:
            detail_block = (
                f"\n"
                f"<b>-- Signal Intel --</b>\n"
                + "\n".join(detail_lines)
                + "\n"
            )

        text = (
            f"<b>[{icon}] SWING Entry | {symbol}</b>\n"
            f"\n"
            f"Entry  : <code>{entry:,.4f}</code>\n"
            f"SL     : <code>{sl:,.4f}</code>\n"
            f"TP     : <code>{tp:,.4f}</code>\n"
            f"Size   : <code>{size:.4f}</code>\n"
            f"R:R    : <code>1:{rr:.1f}</code>\n"
            f"{detail_block}"
            f"\n"
            f"<i>{now}</i>"
        )
        return await self.send_message(text)
