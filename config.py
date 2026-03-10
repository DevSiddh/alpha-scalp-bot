"""Alpha-Scalp Bot Configuration Module.

Loads all configuration from environment variables (.env file)
with sensible defaults for paper trading on Binance Futures testnet.
"""

from __future__ import annotations

import os
from pathlib import Path

# Stub dotenv + loguru if not installed
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(**kw): pass

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger("config")

# ---------------------------------------------------------------------------
# Load .env from project root
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _env(key: str, default: str | None = None, cast: type = str):
    raw = os.getenv(key, default)
    if raw is None:
        raise EnvironmentError(f"Required env variable '{key}' is not set.")
    if cast is bool:
        return raw.lower() in ("1", "true", "yes")
    return cast(raw)


# ===== Exchange Credentials =================================================
BINANCE_API_KEY: str = _env("BINANCE_API_KEY", "")
BINANCE_SECRET: str = _env("BINANCE_SECRET", "")
BINANCE_DEMO_TRADING: bool = _env("BINANCE_DEMO_TRADING", "true", cast=bool)

# ===== Telegram Alerts ======================================================
TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _env("TELEGRAM_CHAT_ID", "")

# ===== Trading Pair & Timeframe =============================================
SYMBOL: str = _env("SYMBOL", "BTC/USDT")
TIMEFRAME: str = _env("TIMEFRAME", "1m")
LOOKBACK_CANDLES: int = _env("LOOKBACK_CANDLES", "200", cast=int)

# ===== Swing Trading ========================================================
SWING_ENABLED: bool = _env("SWING_ENABLED", "true", cast=bool)
SWING_SYMBOLS: list[str] = _env("SWING_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT").split(",")
SWING_TIMEFRAME: str = _env("SWING_TIMEFRAME", "4h")
SWING_LOOKBACK_CANDLES: int = _env("SWING_LOOKBACK_CANDLES", "300", cast=int)
SWING_LEVERAGE: int = _env("SWING_LEVERAGE", "3", cast=int)
SWING_RISK_PER_TRADE: float = _env("SWING_RISK_PER_TRADE", "0.01", cast=float)
SWING_STOP_LOSS_PCT: float = _env("SWING_STOP_LOSS_PCT", "0.03", cast=float)
SWING_TAKE_PROFIT_PCT: float = _env("SWING_TAKE_PROFIT_PCT", "0.08", cast=float)
SWING_MAX_OPEN_POSITIONS: int = _env("SWING_MAX_OPEN_POSITIONS", "3", cast=int)
SWING_CHECK_INTERVAL: int = _env("SWING_CHECK_INTERVAL", "900", cast=int)  # 15 min check
# EMA periods for swing (golden/death cross)
SWING_EMA_FAST: int = _env("SWING_EMA_FAST", "50", cast=int)
SWING_EMA_SLOW: int = _env("SWING_EMA_SLOW", "200", cast=int)
SWING_RSI_PERIOD: int = _env("SWING_RSI_PERIOD", "14", cast=int)
# RSI zones for swing entries (replaces single oversold/overbought thresholds)
SWING_RSI_LONG_LOW: int = _env("SWING_RSI_LONG_LOW", "40", cast=int)
SWING_RSI_LONG_HIGH: int = _env("SWING_RSI_LONG_HIGH", "50", cast=int)
SWING_RSI_SHORT_LOW: int = _env("SWING_RSI_SHORT_LOW", "50", cast=int)
SWING_RSI_SHORT_HIGH: int = _env("SWING_RSI_SHORT_HIGH", "60", cast=int)
# ATR-based SL option (1.5-2x 4h ATR)
SWING_SL_USE_ATR: bool = _env("SWING_SL_USE_ATR", "true", cast=bool)
SWING_SL_ATR_MULTIPLIER: float = _env("SWING_SL_ATR_MULTIPLIER", "1.75", cast=float)
SWING_SL_ATR_PERIOD: int = _env("SWING_SL_ATR_PERIOD", "14", cast=int)
# Trailing TP: after +4%, trail by 3% or EMA 20
SWING_TRAIL_ACTIVATE_PCT: float = _env("SWING_TRAIL_ACTIVATE_PCT", "0.04", cast=float)
SWING_TRAIL_OFFSET_PCT: float = _env("SWING_TRAIL_OFFSET_PCT", "0.03", cast=float)
SWING_TRAIL_USE_EMA20: bool = _env("SWING_TRAIL_USE_EMA20", "true", cast=bool)
# Total exposure cap across all swing pairs
SWING_MAX_TOTAL_EXPOSURE_PCT: float = _env("SWING_MAX_TOTAL_EXPOSURE_PCT", "0.025", cast=float)

# ===== Risk Management ======================================================
RISK_PER_TRADE: float = _env("RISK_PER_TRADE", "0.01", cast=float)
DAILY_DRAWDOWN_LIMIT: float = _env("DAILY_DRAWDOWN_LIMIT", "0.03", cast=float)
STOP_LOSS_PCT: float = _env("STOP_LOSS_PCT", "0.005", cast=float)
TAKE_PROFIT_PCT: float = _env("TAKE_PROFIT_PCT", "0.010", cast=float)
MAX_OPEN_POSITIONS: int = _env("MAX_OPEN_POSITIONS", "1", cast=int)
LEVERAGE: int = _env("LEVERAGE", "5", cast=int)

# ===== Strategy – EMA / RSI =================================================
EMA_FAST: int = _env("EMA_FAST", "9", cast=int)
EMA_SLOW: int = _env("EMA_SLOW", "21", cast=int)
RSI_PERIOD: int = _env("RSI_PERIOD", "14", cast=int)
RSI_OVERSOLD: int = _env("RSI_OVERSOLD", "30", cast=int)
RSI_OVERBOUGHT: int = _env("RSI_OVERBOUGHT", "70", cast=int)

# ===== Strategy – Nadaraya-Watson Envelope ==================================
NW_BANDWIDTH: float = _env("NW_BANDWIDTH", "8.0", cast=float)
NW_MULT: float = _env("NW_MULT", "2.0", cast=float)
NW_LOOKBACK: int = _env("NW_LOOKBACK", "50", cast=int)

# ===== PREMIUM: Volume Spike Filter =========================================
VOL_SMA_PERIOD: int = _env("VOL_SMA_PERIOD", "20", cast=int)
VOL_SPIKE_MULT: float = _env("VOL_SPIKE_MULT", "1.5", cast=float)

# ===== PREMIUM: Bollinger Band Squeeze ======================================
BB_PERIOD: int = _env("BB_PERIOD", "20", cast=int)
BB_STD: float = _env("BB_STD", "2.0", cast=float)
BB_SQUEEZE_THRESHOLD: float = _env("BB_SQUEEZE_THRESHOLD", "0.02", cast=float)

# ===== PREMIUM: ADX Regime Detection ========================================
ADX_PERIOD: int = _env("ADX_PERIOD", "14", cast=int)
ADX_TREND_THRESHOLD: float = _env("ADX_TREND_THRESHOLD", "25.0", cast=float)
ADX_STRONG_TREND: float = _env("ADX_STRONG_TREND", "40.0", cast=float)
ADX_RANGE_THRESHOLD: float = _env("ADX_RANGE_THRESHOLD", "20.0", cast=float)

# ===== PREMIUM: Scalp ATR Trailing Stop =====================================
SCALP_TRAIL_ACTIVATE_PCT: float = _env("SCALP_TRAIL_ACTIVATE_PCT", "0.004", cast=float)
SCALP_TRAIL_ATR_MULT: float = _env("SCALP_TRAIL_ATR_MULT", "1.0", cast=float)

# ===== PREMIUM: Concurrent Trade Limiter ====================================
MAX_CONCURRENT_TRADES: int = _env("MAX_CONCURRENT_TRADES", "3", cast=int)

# ===== PREMIUM: Daily P&L Circuit Breaker ===================================
DAILY_LOSS_LIMIT: float = _env("DAILY_LOSS_LIMIT", "0.03", cast=float)

# ===== Scalp ATR-based SL/TP ================================================
SCALP_SL_USE_ATR: bool = _env("SCALP_SL_USE_ATR", "false", cast=bool)
SCALP_SL_ATR_PERIOD: int = _env("SCALP_SL_ATR_PERIOD", "14", cast=int)
SCALP_SL_ATR_MULTIPLIER: float = _env("SCALP_SL_ATR_MULTIPLIER", "1.5", cast=float)
SCALP_TP_ATR_MULTIPLIER: float = _env("SCALP_TP_ATR_MULTIPLIER", "2.5", cast=float)

# ===== Timeframe Presets (auto-applied when TIMEFRAME != '1m') ==============
# These override EMA_FAST, EMA_SLOW, RSI_PERIOD, NW_LOOKBACK, STOP_LOSS_PCT,
# TAKE_PROFIT_PCT based on TIMEFRAME. Set SCALP_USE_TF_PRESETS=false to disable.
SCALP_USE_TF_PRESETS: bool = _env("SCALP_USE_TF_PRESETS", "true", cast=bool)

_TF_PRESETS: dict[str, dict] = {
    "1m": {
        "ema_fast": 9, "ema_slow": 21, "rsi_period": 14,
        "nw_lookback": 50, "sl_pct": 0.005, "tp_pct": 0.010,
        "atr_sl_mult": 1.5, "atr_tp_mult": 2.5,
    },
    "3m": {
        "ema_fast": 12, "ema_slow": 34, "rsi_period": 14,
        "nw_lookback": 150, "sl_pct": 0.008, "tp_pct": 0.018,
        "atr_sl_mult": 1.5, "atr_tp_mult": 2.5,
    },
    "5m": {
        "ema_fast": 12, "ema_slow": 34, "rsi_period": 18,
        "nw_lookback": 200, "sl_pct": 0.012, "tp_pct": 0.025,
        "atr_sl_mult": 1.8, "atr_tp_mult": 3.0,
    },
}

if SCALP_USE_TF_PRESETS and TIMEFRAME in _TF_PRESETS and TIMEFRAME != "1m":
    _preset = _TF_PRESETS[TIMEFRAME]
    EMA_FAST = _preset["ema_fast"]
    EMA_SLOW = _preset["ema_slow"]
    RSI_PERIOD = _preset["rsi_period"]
    NW_LOOKBACK = _preset["nw_lookback"]
    STOP_LOSS_PCT = _preset["sl_pct"]
    TAKE_PROFIT_PCT = _preset["tp_pct"]
    SCALP_SL_ATR_MULTIPLIER = _preset["atr_sl_mult"]
    SCALP_TP_ATR_MULTIPLIER = _preset["atr_tp_mult"]
    logger.info(f"TF Preset applied for {TIMEFRAME}: EMA {EMA_FAST}/{EMA_SLOW}, RSI {RSI_PERIOD}, NW lb={NW_LOOKBACK}, SL/TP {STOP_LOSS_PCT:.2%}/{TAKE_PROFIT_PCT:.2%}")

# ===== Execution ============================================================
LOOP_INTERVAL: int = _env("LOOP_INTERVAL", "5", cast=int)
ORDER_TYPE: str = _env("ORDER_TYPE", "market")
SLIPPAGE_TOLERANCE: float = _env("SLIPPAGE_TOLERANCE", "0.001", cast=float)

# ===== Trade Tracking =======================================================
TRADE_HISTORY_FILE: str = _env("TRADE_HISTORY_FILE", "logs/trade_history.json")
STATS_COMMAND_ENABLED: bool = _env("STATS_COMMAND_ENABLED", "true", cast=bool)
POSITION_MONITOR_INTERVAL: int = _env("POSITION_MONITOR_INTERVAL", "30", cast=int)  # seconds

# ===== Logging ==============================================================
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")
LOG_FILE: str = _env("LOG_FILE", "logs/alpha_scalp.log")
LOG_ROTATION: str = _env("LOG_ROTATION", "10 MB")

# ---------------------------------------------------------------------------
# Configure Loguru
# ---------------------------------------------------------------------------
_log_path = Path(LOG_FILE)
_log_path.parent.mkdir(parents=True, exist_ok=True)

logger.add(
    str(_log_path),
    level=LOG_LEVEL,
    rotation=LOG_ROTATION,
    retention="7 days",
    compression="gz",
    backtrace=True,
    diagnose=True,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
)

# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------
logger.info("=" * 60)
logger.info("Alpha-Scalp Bot PREMIUM | Binance Futures | config loaded")
logger.info(f"Symbol       : {SYMBOL}")
logger.info(f"Timeframe    : {TIMEFRAME}")
logger.info(f"Demo Trading : {BINANCE_DEMO_TRADING}")
logger.info(f"Leverage     : {LEVERAGE}x")
logger.info(f"Risk / Trade : {RISK_PER_TRADE:.1%}")
logger.info(f"Daily DD Cap : {DAILY_DRAWDOWN_LIMIT:.1%}")
logger.info(f"SL / TP      : {STOP_LOSS_PCT:.2%} / {TAKE_PROFIT_PCT:.2%}")
logger.info(f"NW Envelope  : bw={NW_BANDWIDTH}, mult={NW_MULT}, lb={NW_LOOKBACK}")
logger.info(f"ATR SL (scalp): {'ON' if SCALP_SL_USE_ATR else 'OFF'} (period={SCALP_SL_ATR_PERIOD}, SL mult={SCALP_SL_ATR_MULTIPLIER}, TP mult={SCALP_TP_ATR_MULTIPLIER})")
logger.info(f"TF Presets   : {'ON' if SCALP_USE_TF_PRESETS else 'OFF'}")
logger.info(f"Vol Filter   : SMA={VOL_SMA_PERIOD}, spike={VOL_SPIKE_MULT}x")
logger.info(f"BB Squeeze   : period={BB_PERIOD}, std={BB_STD}, threshold={BB_SQUEEZE_THRESHOLD:.2%}")
logger.info(f"ADX Regime   : period={ADX_PERIOD}, trend>{ADX_TREND_THRESHOLD}, strong>{ADX_STRONG_TREND}")
logger.info(f"Scalp Trail  : activate={SCALP_TRAIL_ACTIVATE_PCT:.2%}, ATR mult={SCALP_TRAIL_ATR_MULT}")
logger.info(f"Concurrent   : max {MAX_CONCURRENT_TRADES} trades")
logger.info(f"Circuit Break: {DAILY_LOSS_LIMIT:.1%} daily loss limit")
if SWING_ENABLED:
    logger.info(f"Swing Mode   : ENABLED")
    logger.info(f"Swing Symbols: {SWING_SYMBOLS}")
    logger.info(f"Swing TF     : {SWING_TIMEFRAME}")
    logger.info(f"Swing Lev    : {SWING_LEVERAGE}x")
    logger.info(f"Swing Risk   : {SWING_RISK_PER_TRADE:.1%} per trade")
    logger.info(f"Swing SL/TP  : {SWING_STOP_LOSS_PCT:.2%} / {SWING_TAKE_PROFIT_PCT:.2%}")
    logger.info(f"Swing RSI    : Long {SWING_RSI_LONG_LOW}-{SWING_RSI_LONG_HIGH} | Short {SWING_RSI_SHORT_LOW}-{SWING_RSI_SHORT_HIGH}")
    logger.info(f"Swing ATR SL : {'ON' if SWING_SL_USE_ATR else 'OFF'} (mult={SWING_SL_ATR_MULTIPLIER}, period={SWING_SL_ATR_PERIOD})")
    logger.info(f"Swing Trail  : activate={SWING_TRAIL_ACTIVATE_PCT:.1%}, offset={SWING_TRAIL_OFFSET_PCT:.1%}, EMA20={'ON' if SWING_TRAIL_USE_EMA20 else 'OFF'}")
    logger.info(f"Swing Exposure: max {SWING_MAX_TOTAL_EXPOSURE_PCT:.1%} total")
logger.info("=" * 60)
