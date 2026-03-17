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
TIMEFRAME: str = "3m"  # P0-2: Hardcoded to 3m
LOOKBACK_CANDLES: int = _env("LOOKBACK_CANDLES", "200", cast=int)

# ===== Multi-Symbol Passive Shadow (Step 13) ================================
PASSIVE_SHADOW_SYMBOLS: list[str] = [
    s.strip() for s in _env("PASSIVE_SHADOW_SYMBOLS", "ETH/USDT,SOL/USDT").split(",")
    if s.strip()
]
PAPER_TRADING_MODE: bool = _env("PAPER_TRADING_MODE", "false", cast=bool)
PAPER_SLIPPAGE_PCT: float = _env("PAPER_SLIPPAGE_PCT", "0.0005", cast=float)

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
# Kelly Criterion warm-up guards
KELLY_MIN_TRADES: int = _env("KELLY_MIN_TRADES", "300", cast=int)       # ignore Kelly below this
KELLY_MAX_FRACTION: float = _env("KELLY_MAX_FRACTION", "0.03", cast=float)  # hard cap 3%
KELLY_RAMP_TRADES: int = _env("KELLY_RAMP_TRADES", "60", cast=int)      # blend fixed->Kelly over 30-60 trades
DAILY_DRAWDOWN_LIMIT: float = _env("DAILY_DRAWDOWN_LIMIT", "0.03", cast=float)
STOP_LOSS_PCT: float = _env("STOP_LOSS_PCT", "0.005", cast=float)
TAKE_PROFIT_PCT: float = _env("TAKE_PROFIT_PCT", "0.010", cast=float)
MAX_OPEN_POSITIONS: int = _env("MAX_OPEN_POSITIONS", "1", cast=int)
LEVERAGE: int = 2  # P0-2: Changed from 5

# P0-2: New configuration parameters
SIGNAL_THRESHOLD: float = _env("SIGNAL_THRESHOLD", "0.72", cast=float)
SCALP_MAX_HOLD_SECONDS: int = _env("SCALP_MAX_HOLD_SECONDS", "2700", cast=int)
MAX_CONCURRENT_TRADES: int = _env("MAX_CONCURRENT_TRADES", "1", cast=int)
MAX_DAILY_LOSS_PCT: float = _env("MAX_DAILY_LOSS_PCT", "0.03", cast=float)
KELLY_FRACTION_CAP: float = _env("KELLY_FRACTION_CAP", "0.10", cast=float)
MIN_POSITION_SIZE_USDT: float = _env("MIN_POSITION_SIZE_USDT", "6", cast=float)
MAX_POSITION_SIZE_USDT: float = _env("MAX_POSITION_SIZE_USDT", "15", cast=float)
ATR_SL_MULTIPLIER: float = _env("ATR_SL_MULTIPLIER", "1.5", cast=float)
ATR_TP_MULTIPLIER: float = _env("ATR_TP_MULTIPLIER", "3.0", cast=float)
ATR_PERIOD: int = _env("ATR_PERIOD", "14", cast=int)
MIN_REWARD_RISK_RATIO: float = _env("MIN_REWARD_RISK_RATIO", "1.8", cast=float)
ATR_RATIO_MAX: float = _env("ATR_RATIO_MAX", "2.5", cast=float)
ATR_RATIO_MIN: float = _env("ATR_RATIO_MIN", "0.5", cast=float)
IS_SESSION_FILTER_ENABLED: bool = _env("IS_SESSION_FILTER_ENABLED", "true", cast=bool)

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
# Trail delta: once trail activates at +0.4%, lock stop at +0.2% (covers fees)
SCALP_TRAIL_DELTA_PCT: float = _env("SCALP_TRAIL_DELTA_PCT", "0.002", cast=float)

# ===== PREMIUM: Scalp Time Stop (max hold duration) =========================
# Force-close scalp positions after N seconds if TP/SL not hit.
# Edge from microstructure signals decays fast -- 180s = 3 candles on 1m.
SCALP_MAX_HOLD_SECONDS: int = _env("SCALP_MAX_HOLD_SECONDS", "2700", cast=int)

# ===== PREMIUM: Concurrent Trade Limiter ====================================
MAX_CONCURRENT_TRADES: int = _env("MAX_CONCURRENT_TRADES", "1", cast=int)

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

# --- Phase 2: LLM Optimization Settings ---
# Use OpenRouter, OpenAI, or your Nebula endpoint
LLM_API_URL = os.getenv("LLM_API_URL", "https://openrouter.ai/api/v1/chat/completions")
LLM_API_KEY = os.getenv("LLM_API_KEY", "your_api_key_here")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")

# Safety constraints for the LLM output
MIN_WEIGHT = 0.1
MAX_WEIGHT = 3.0

# ===== WebSocket Mode =======================================================
# Set to True to use event-driven WebSocket architecture instead of polling.
# When False, the bot falls back to the original REST polling loop.
USE_WEBSOCKET: bool = _env("USE_WEBSOCKET", "true", cast=bool)
WS_BOOK_DEPTH: int = _env("WS_BOOK_DEPTH", "20", cast=int)           # order book levels to track
WS_PRICE_JUMP_BPS: float = _env("WS_PRICE_JUMP_BPS", "15.0", cast=float)  # basis points for price jump alert
WS_CANDLE_HISTORY: int = _env("WS_CANDLE_HISTORY", "500", cast=int)   # candles to keep in memory

# ===== Execution ============================================================
LOOP_INTERVAL: int = _env("LOOP_INTERVAL", "5", cast=int)
ORDER_TYPE: str = _env("ORDER_TYPE", "market")
SLIPPAGE_TOLERANCE: float = _env("SLIPPAGE_TOLERANCE", "0.001", cast=float)

# --- Spread Guard (pre-execution safety check) ---
# Abort trade if live bid-ask spread exceeds this % of mid price
SPREAD_GUARD_ENABLED: bool = _env("SPREAD_GUARD_ENABLED", "true", cast=bool)
MAX_SPREAD_PCT: float = _env("MAX_SPREAD_PCT", "0.0005", cast=float)  # 0.05%
SPREAD_GUARD_BOOK_DEPTH: int = _env("SPREAD_GUARD_BOOK_DEPTH", "5", cast=int)  # levels to fetch

# ===== CVD (Cumulative Volume Delta) ========================================
# Tracks aggressive buyer vs seller pressure from trade-level data
CVD_ENABLED: bool = _env("CVD_ENABLED", "true", cast=bool)
CVD_LOOKBACK: int = _env("CVD_LOOKBACK", "20", cast=int)  # bars for CVD slope
CVD_STRONG_THRESHOLD: float = _env("CVD_STRONG_THRESHOLD", "0.6", cast=float)  # normalised, strong signal
CVD_MILD_THRESHOLD: float = _env("CVD_MILD_THRESHOLD", "0.3", cast=float)     # normalised, mild signal

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
logger.info(f"Spread Guard : {'ON' if SPREAD_GUARD_ENABLED else 'OFF'} (max={MAX_SPREAD_PCT:.4%}, depth={SPREAD_GUARD_BOOK_DEPTH})")
logger.info(f"CVD Signal   : {'ON' if CVD_ENABLED else 'OFF'} (lookback={CVD_LOOKBACK}, strong={CVD_STRONG_THRESHOLD}, mild={CVD_MILD_THRESHOLD})")
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

# ===== Per-Token Risk Profiles =================================================
TOKEN_PROFILES: dict[str, dict] = {
    "BTC/USDT": {"sl_pct": 0.007, "tp_pct": 0.015, "leverage": 4},
    "ETH/USDT": {"sl_pct": 0.009, "tp_pct": 0.018, "leverage": 3},
    "SOL/USDT": {"sl_pct": 0.012, "tp_pct": 0.025, "leverage": 2},
}
_TOKEN_PROFILE = TOKEN_PROFILES.get(SYMBOL, {"sl_pct": STOP_LOSS_PCT, "tp_pct": TAKE_PROFIT_PCT, "leverage": LEVERAGE})
TOKEN_SL_PCT: float = _TOKEN_PROFILE["sl_pct"]
TOKEN_TP_PCT: float = _TOKEN_PROFILE["tp_pct"]
TOKEN_LEVERAGE: int = _TOKEN_PROFILE["leverage"]

# ===== Grand Prix Step 3: Quick Wins ==========================================
# Spike filter: skip candle if (high - low) > N × ATR
SPIKE_ATR_MULT: float = _env("SPIKE_ATR_MULT", "3.0", cast=float)

# ===== Grand Prix Step 2: Risk Engine Extensions ==============================
# Three-Strike: 3 consecutive losses → N-second cooldown
THREE_STRIKE_LOSSES: int = _env("THREE_STRIKE_LOSSES", "3", cast=int)
THREE_STRIKE_COOLDOWN_SECONDS: float = _env("THREE_STRIKE_COOLDOWN_SECONDS", "5400", cast=float)  # 90 min

# Equity Floor: balance drops to X% of session start → permanent shutdown
EQUITY_FLOOR_PCT: float = _env("EQUITY_FLOOR_PCT", "0.80", cast=float)  # 80%

# Active Cash Mode: below this equity ratio → half position size
ACTIVE_CASH_THRESHOLD_PCT: float = _env("ACTIVE_CASH_THRESHOLD_PCT", "0.90", cast=float)  # 90%

# Minimum SL distance as fraction of entry price
MIN_SL_FLOOR_PCT: float = _env("MIN_SL_FLOOR_PCT", "0.0015", cast=float)  # 0.15%

# ATR validation: minimum ATR as fraction of entry price
ATR_MIN_PCT: float = _env("ATR_MIN_PCT", "0.0005", cast=float)  # 0.05%

# Regime-aware minimum R:R ratios
REGIME_MIN_RR: dict = {
    "RANGING": 1.5,
    "NEUTRAL": 1.5,
    "TRENDING": 2.0,
    "TRENDING_UP": 2.0,
    "TRENDING_DOWN": 2.0,
    "VOLATILE": 1.8,
}

# ===== Regime-Based Signal Disabling ==========================================
DISABLED_SIGNALS_BY_REGIME: dict[str, list[str]] = {
    "TRENDING_DOWN": ["bb_bounce"],
    "VOLATILE": ["ema_cross"],
}

# ===== Funding Rate Signal ====================================================
FUNDING_RATE_CACHE_SECONDS: int = _env("FUNDING_RATE_CACHE_SECONDS", "28800", cast=int)  # 8 hours
FUNDING_RATE_THRESHOLD: float = _env("FUNDING_RATE_THRESHOLD", "0.0005", cast=float)

# New variables Added
MAX_SPREAD_BPS = int(os.getenv("MAX_SPREAD_BPS", "20"))
DATA_FRESHNESS_SECONDS = int(os.getenv("DATA_FRESHNESS_SECONDS", "5"))
MIN_POSITION_SIZE_USDT = float(os.getenv("MIN_POSITION_SIZE_USDT", "6"))
MAX_POSITION_SIZE_USDT = float(os.getenv("MAX_POSITION_SIZE_USDT", "15"))
CONSENSUS_THRESHOLD = float(os.getenv("CONSENSUS_THRESHOLD", "0.65"))
