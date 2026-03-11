# Alpha-Scalp Bot

**Production-grade crypto scalping bot for Binance USD-M Futures**

Event-driven Python bot with a modular alpha engine pipeline, real-time WebSocket market data, adaptive signal scoring, and strict risk management. Supports both polling and WebSocket execution modes.

---

## Architecture Overview

The bot uses a layered signal pipeline:

```
Market Data (REST / WebSocket)
        |
  FeatureCache.compute(df)  -->  FeatureSet (indicators, regime, NW envelope)
        |
  AlphaEngine.generate_votes(fs)  -->  AlphaVotes (per-signal directional votes)
        |
  SignalScoring.score(votes, fs)  -->  ScoringResult (weighted composite score)
        |
  RiskEngine  -->  Position sizing (quarter-Kelly after warm-up)
        |
  OrderExecutor  -->  Binance Futures bracket orders (entry + SL + TP)
```

---

## Key Features

### Signal Pipeline
- **FeatureCache** -- Computes and caches all technical indicators (EMA, RSI, MACD, Bollinger, ATR, OBV, VWAP, Nadaraya-Watson envelope, volume profile, regime detection)
- **AlphaEngine** -- Multi-signal voting system. Each alpha signal (trend, momentum, mean-reversion, volume, volatility) casts independent directional votes
- **SignalScoring** -- Weighted composite scorer with JSON-configurable weights. Combines alpha votes into a single normalized score with confidence
- **WeightOptimizer** -- Auto-tunes signal weights after 30+ trades based on historical per-signal P&L attribution

### Execution Modes
- **Polling mode** (`run_bot_polling`) -- Classic REST-based candle polling on a configurable interval
- **WebSocket mode** (`run_bot_ws`) -- Event-driven via Binance WebSocket streams:
  - Real-time kline/candlestick streams with candle completion callbacks
  - Order book depth snapshots for spread/imbalance signals
  - Price jump detection (configurable BPS threshold)
  - Automatic reconnection with exponential backoff

### WebSocket Infrastructure
- **BinanceWSManager** -- Manages multiple WebSocket streams (kline, depth, trade) with heartbeat monitoring, auto-reconnect, and graceful shutdown
- **MarketState** -- Maintains live order book, tracks bid/ask spread, book imbalance, mid-price, and detects price jumps. Validates book staleness
- **StateChangeDispatcher** -- Routes market state changes to the alpha pipeline via typed callbacks (`on_candle_complete`, `on_book_update`, `on_price_jump`, `on_book_invalidated`)

### Risk Management
- **Quarter-Kelly position sizing** -- After 10-trade warm-up, uses Kelly criterion (capped at 25% Kelly, bounded 0.5%-5% equity)
- **3% daily kill switch** -- Halts all trading if daily drawdown reaches 3%
- **Bracket orders** -- Every entry places separate STOP_MARKET + TAKE_PROFIT_MARKET orders
- **Isolated margin** -- Per-symbol margin isolation
- **Graceful shutdown** -- SIGINT/SIGTERM cancels pending orders, closes positions, notifies Telegram

### Trade Tracking
- **TradeTrackerV2** -- Enhanced trade journal with per-signal attribution, win/loss streaks, Sharpe ratio, max drawdown, and regime-tagged analytics
- **TradeTracker** -- Legacy tracker (still available for simpler setups)

### Strategies
- **ScalpStrategy** -- Primary strategy using the alpha engine pipeline (EMA, RSI, NW envelope, MACD, Bollinger, volume, volatility signals)
- **SwingStrategy** -- Longer-timeframe swing trading with multi-timeframe confirmation

### Alerts & Logging
- **Telegram alerts** -- Real-time trade entries/exits, kill-switch warnings, daily P&L summaries, and session stats
- **Loguru logging** -- Structured logging with rotation, compression, and 7-day retention

---

## Project Structure

```
alpha-scalp-bot/
|-- main.py               # Entry point: polling + WebSocket modes, pipeline orchestration
|-- config.py             # All configuration from .env with defaults
|-- feature_cache.py      # Technical indicator computation & caching (FeatureSet)
|-- alpha_engine.py       # Multi-signal voting engine (AlphaVotes)
|-- signal_scoring.py     # Weighted composite scoring (ScoringResult)
|-- weight_optimizer.py   # Auto-tunes signal weights from trade history
|-- strategy.py           # ScalpStrategy (signal generation + Kelly sizing)
|-- swing_strategy.py     # SwingStrategy (multi-TF swing trades)
|-- risk_engine.py        # Kill switch, position sizing, SL/TP management
|-- order_executor.py     # CCXT order management for Binance Futures
|-- trade_tracker_v2.py   # Enhanced trade journal with per-signal attribution
|-- trade_tracker.py      # Legacy trade tracker
|-- ws_manager.py         # Binance WebSocket stream manager
|-- market_state.py       # Live order book & price jump detection
|-- state_dispatcher.py   # Routes WS events to pipeline callbacks
|-- telegram_alerts.py    # Async Telegram notifications
|-- backtest.py           # Backtesting using the full alpha engine pipeline
|-- requirements.txt      # Python dependencies
|-- .env.example          # Template for environment variables
```

---

## Risk Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| Risk per Trade | 1% | Max equity at risk (warm-up period) |
| Position Sizing | Quarter-Kelly | After 10 trades, bounded 0.5%-5% |
| Stop Loss | 0.5% | Distance from entry |
| Take Profit | 1.0% | 2:1 reward/risk ratio |
| Daily Drawdown | 3% | Kill switch threshold |
| Max Positions | 1 | One position at a time |
| Leverage | 5x | Default leverage |
| Margin Mode | Isolated | Per-symbol isolation |

---

## Setup

### Prerequisites
- Python 3.11+
- Binance Futures account (testnet or live)
- Telegram bot token (optional, for alerts)

### 1. Clone & Install

```bash
git clone https://github.com/DevSiddh/alpha-scalp-bot.git
cd alpha-scalp-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Binance
BINANCE_API_KEY=your_key
BINANCE_SECRET=your_secret
BINANCE_TESTNET=true

# Telegram (optional)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Execution mode
USE_WEBSOCKET=false          # true = WS event-driven, false = REST polling
WS_BOOK_DEPTH=10             # Order book depth levels
WS_PRICE_JUMP_BPS=15         # Price jump detection threshold (basis points)
WS_CANDLE_HISTORY=200        # Historical candles to load on WS startup
```

### 3. Run

```bash
# Polling mode (default)
python main.py

# WebSocket mode
USE_WEBSOCKET=true python main.py
```

---

## Configuration Reference

```env
# Trading
SYMBOL=BTC/USDT
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

# WebSocket
USE_WEBSOCKET=false
WS_BOOK_DEPTH=10
WS_PRICE_JUMP_BPS=15
WS_CANDLE_HISTORY=200

# Weight Optimization
WEIGHT_OPT_MIN_TRADES=30    # Minimum trades before auto-optimization

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

## Backtesting

```bash
python backtest.py
```

The backtester uses the full alpha engine pipeline (FeatureCache -> AlphaEngine -> SignalScoring) and reports:
- Per-trade entries with composite score, regime, and contributing signals
- Win rate, total P&L, average return
- Signal attribution breakdown

---

## How It Works

### Polling Mode
1. Fetches candles via REST every N seconds
2. Runs the full pipeline: features -> alpha votes -> scoring
3. If score exceeds threshold with sufficient confidence, executes trade
4. Places bracket orders (SL + TP) and tracks via TradeTrackerV2
5. After 30+ trades, runs weight optimizer to tune signal weights

### WebSocket Mode
1. Loads historical candles via REST on startup
2. Opens WebSocket streams for kline, depth, and trade data
3. On candle completion: runs the full alpha pipeline
4. On book updates: recalculates spread/imbalance for the next signal
5. On price jumps: triggers immediate pipeline evaluation
6. Auto-reconnects on disconnection with exponential backoff

---

## Paper Trading vs Live

| Setting | Mode |
|---------|------|
| `BINANCE_TESTNET=true` | Paper trading (testnet) |
| `BINANCE_TESTNET=false` | **Live trading -- REAL FUNDS AT RISK** |

**Always test on testnet first.**

---

## Disclaimer

**This software is for educational and research purposes only.**

- Cryptocurrency trading involves substantial risk of loss
- Past performance does not guarantee future results
- You are solely responsible for your trading decisions
- Always start with paper trading and small positions
- Never risk more than you can afford to lose

**USE AT YOUR OWN RISK.**

---

## Tech Stack

- **Python 3.11+** -- Async-first architecture
- **CCXT 4.x** -- Binance Futures (USD-M)
- **pandas / pandas-ta** -- DataFrames + technical indicators
- **NumPy / SciPy** -- Nadaraya-Watson kernel, statistical functions
- **websockets** -- Binance WebSocket streams
- **Loguru** -- Structured logging
- **httpx** -- Async HTTP (Telegram)
- **python-dotenv** -- Env configuration

---

## License

MIT
