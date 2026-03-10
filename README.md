# Alpha-Scalp Bybit Bot

**Automated cryptocurrency scalping bot for Bybit Futures (Linear Perpetuals)**

A production-grade Python bot that executes high-frequency scalp trades on Bybit using a triple-confirmation strategy: EMA crossover + RSI momentum + Nadaraya-Watson kernel envelope. Built with strict risk management including a 3% daily kill switch.

---

## Features

- **Triple-Confirmation Strategy** -- Trades only fire when EMA crossover, RSI momentum, AND Nadaraya-Watson envelope all align
- **Nadaraya-Watson Kernel Regression** -- Gaussian kernel-based dynamic support/resistance envelope (not a simple moving average)
- **Strict Risk Management** -- 1% equity risk per trade, 0.5% stop-loss, 1.0% take-profit (2:1 R/R)
- **3% Daily Kill Switch** -- Automatically halts all trading if daily drawdown hits 3%
- **Bybit Futures via CCXT** -- Works with linear perpetual contracts (USDT-margined)
- **Paper Trading Mode** -- Testnet enabled by default for safe strategy validation
- **Telegram Alerts** -- Real-time trade notifications, kill-switch warnings, and daily P&L summaries
- **Structured Logging** -- Full audit trail via Loguru with rotation and compression
- **Graceful Shutdown** -- Clean exit on SIGINT/SIGTERM with position cleanup

---

## Strategy Explained

### 1. EMA Crossover (Trend)
- **EMA 9** (fast) vs **EMA 21** (slow)
- Bullish cross (fast > slow) = potential long entry
- Bearish cross (fast < slow) = potential short entry

### 2. RSI Filter (Momentum)
- **RSI 14** confirms momentum isn't exhausted
- Long entries require RSI < 35 (oversold zone)
- Short entries require RSI > 65 (overbought zone)

### 3. Nadaraya-Watson Envelope (Mean Reversion)
- Gaussian kernel regression creates a dynamic "fair value" line
- Upper/lower bands act as dynamic overbought/oversold levels
- Long: price near or below the lower band
- Short: price near or above the upper band

### Signal Logic
```
BUY  = EMA9 crosses above EMA21 AND RSI < 35 AND price <= NW lower band
SELL = EMA9 crosses below EMA21 AND RSI > 65 AND price >= NW upper band
```

All three conditions must be true simultaneously. This dramatically reduces false signals.

---

## Risk Management

| Parameter | Value | Description |
|-----------|-------|-------------|
| Risk per Trade | 1% | Maximum equity at risk per position |
| Stop Loss | 0.5% | Distance from entry price |
| Take Profit | 1.0% | Distance from entry (2:1 reward/risk) |
| Daily Drawdown | 3% | Kill switch threshold |
| Max Positions | 1 | Only one open position at a time |
| Leverage | 5x | Default leverage on Bybit |

The kill switch compares current equity to the balance at UTC midnight. Once triggered, all trading halts until the next day.

---

## Project Structure

```
alpha-scalp-bot/
|-- main.py              # Entry point & async trading loop
|-- config.py            # Configuration from .env with defaults
|-- strategy.py          # Signal generation (EMA + RSI + NW)
|-- risk_engine.py       # Kill switch, position sizing, SL/TP
|-- order_executor.py    # CCXT order management for Bybit
|-- telegram_alerts.py   # Async Telegram notifications
|-- requirements.txt     # Python dependencies
|-- .env.example         # Template for environment variables
|-- logs/                # Auto-created log directory
```

---

## Setup

### Prerequisites
- Python 3.11+
- Bybit account (testnet or live)
- Telegram bot token (optional, for alerts)

### 1. Clone & Install

```bash
git clone <your-repo-url> alpha-scalp-bot
cd alpha-scalp-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
BYBIT_API_KEY=your_api_key_here
BYBIT_API_SECRET=your_api_secret_here
BYBIT_TESTNET=true
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 3. Get Bybit Testnet Keys

1. Go to [testnet.bybit.com](https://testnet.bybit.com)
2. Create an account and generate API keys
3. Enable **Futures Trading** permission
4. Add keys to your `.env` file

### 4. Run the Bot

```bash
python main.py
```

The bot will:
- Connect to Bybit testnet
- Send a startup message to Telegram
- Begin scanning for trade signals every 5 seconds
- Execute trades when triple-confirmation fires
- Automatically manage SL/TP and daily risk

---

## Paper Trading vs Live Trading

| Setting | Mode |
|---------|------|
| `BYBIT_TESTNET=true` | **Paper trading** (testnet) -- no real money |
| `BYBIT_TESTNET=false` | **Live trading** -- REAL FUNDS AT RISK |

**Always test extensively on testnet before going live.**

To switch to live:
1. Create API keys on [bybit.com](https://bybit.com) (not testnet)
2. Update `.env` with live keys
3. Set `BYBIT_TESTNET=false`
4. Fund your Bybit USDT Perpetuals account

---

## Configuration Reference

All parameters can be overridden via `.env`:

```env
# Trading
SYMBOL=BTC/USDT:USDT
TIMEFRAME=1m
LOOKBACK_CANDLES=200
LEVERAGE=5

# Risk
RISK_PER_TRADE=0.01
DAILY_DRAWDOWN_LIMIT=0.03
STOP_LOSS_PCT=0.005
TAKE_PROFIT_PCT=0.010
MAX_OPEN_POSITIONS=1

# Strategy
EMA_FAST=9
EMA_SLOW=21
RSI_PERIOD=14
RSI_OVERSOLD=30
RSI_OVERBOUGHT=70
NW_BANDWIDTH=8.0
NW_MULT=2.0
NW_LOOKBACK=50

# Execution
LOOP_INTERVAL=5
ORDER_TYPE=market
SLIPPAGE_TOLERANCE=0.001

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/alpha_scalp.log
LOG_ROTATION=10 MB
```

---

## Logs

Logs are written to `logs/alpha_scalp.log` with:
- Automatic rotation at 10 MB
- 7-day retention
- Gzip compression of old logs
- Full stack traces on errors

---

## Stopping the Bot

Press `Ctrl+C` or send `SIGTERM`. The bot will:
1. Stop accepting new signals
2. Close any open positions
3. Cancel pending orders
4. Send a shutdown notification to Telegram
5. Exit cleanly

---

## Disclaimer

**This software is for educational and research purposes only.**

- Cryptocurrency trading involves substantial risk of loss
- Past performance does not guarantee future results
- This bot does NOT guarantee profits
- You are solely responsible for your trading decisions
- Always start with paper trading (testnet) and small position sizes
- Never risk more than you can afford to lose
- The developers assume no liability for financial losses

**USE AT YOUR OWN RISK.**

---

## Tech Stack

- **Python 3.11+** -- Async-first architecture
- **CCXT 4.x** -- Unified exchange API
- **pandas-ta** -- Technical indicators (EMA, RSI)
- **NumPy** -- Nadaraya-Watson kernel regression
- **Loguru** -- Structured logging
- **httpx** -- Async HTTP for Telegram
- **python-dotenv** -- Environment configuration

---

## License

MIT
