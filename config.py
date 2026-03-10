"""Alpha-Scalp Bot Configuration Module.

Loads all configuration from environment variables (.env file)
with sensible defaults for paper trading on Binance Futures testnet.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Load .env from project root
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _env(key: str, default: str | None = None, cast: type = str):
    """Read an env var, cast it, and return *default* when missing."""
    raw = os.getenv(key, default)
    if raw is None:
        raise EnvironmentError(f"Required env variable '{key}' is not set.")
    if cast is bool:
        return raw.lower() in ("1", "true", "yes")
    return cast(raw)


# ===== Exchange Credentials =================================================
BINANCE_API_KEY: str = _env("BINANCE_API_KEY", "")
BINANCE_SECRET: str = _env("BINANCE_SECRET", "")
BINANCE_TESTNET: bool = _env("BINANCE_TESTNET", "true", cast=bool)

# ===== Telegram Alerts ======================================================
TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _env("TELEGRAM_CHAT_ID", "")

# ===== Trading Pair & Timeframe =============================================
SYMBOL: str = _env("SYMBOL", "BTC/USDT")  # Binance Futures (CCXT unified)
TIMEFRAME: str = _env("TIMEFRAME", "1m")          # 1-minute candles for scalping
LOOKBACK_CANDLES: int = _env("LOOKBACK_CANDLES", "200", cast=int)

# ===== Risk Management ======================================================
RISK_PER_TRADE: float = _env("RISK_PER_TRADE", "0.01", cast=float)        # 1 % equity
DAILY_DRAWDOWN_LIMIT: float = _env("DAILY_DRAWDOWN_LIMIT", "0.03", cast=float)  # 3 %
STOP_LOSS_PCT: float = _env("STOP_LOSS_PCT", "0.005", cast=float)          # 0.5 %
TAKE_PROFIT_PCT: float = _env("TAKE_PROFIT_PCT", "0.010", cast=float)      # 1.0 % (2:1 R/R)
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

# ===== Execution ============================================================
LOOP_INTERVAL: int = _env("LOOP_INTERVAL", "5", cast=int)       # seconds
ORDER_TYPE: str = _env("ORDER_TYPE", "market")
SLIPPAGE_TOLERANCE: float = _env("SLIPPAGE_TOLERANCE", "0.001", cast=float)

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
# Startup banner (logged once on import)
# ---------------------------------------------------------------------------
logger.info("=" * 60)
logger.info("Alpha-Scalp Bot | Binance Futures | config loaded")
logger.info(f"Symbol       : {SYMBOL}")
logger.info(f"Timeframe    : {TIMEFRAME}")
logger.info(f"Testnet      : {BINANCE_TESTNET}")
logger.info(f"Leverage     : {LEVERAGE}x")
logger.info(f"Risk / Trade : {RISK_PER_TRADE:.1%}")
logger.info(f"Daily DD Cap : {DAILY_DRAWDOWN_LIMIT:.1%}")
logger.info(f"SL / TP      : {STOP_LOSS_PCT:.2%} / {TAKE_PROFIT_PCT:.2%}")
logger.info(f"NW Envelope  : bw={NW_BANDWIDTH}, mult={NW_MULT}, lb={NW_LOOKBACK}")
logger.info("=" * 60)
