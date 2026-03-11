# Alpha-Scalp Bot

### Real-Time Multi-Factor Signal Processing System with Adaptive Risk Management

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](Dockerfile)
[![Binance Futures](https://img.shields.io/badge/exchange-Binance%20Futures-F0B90B.svg)](https://www.binance.com/en/futures)

A production-grade, event-driven algorithmic trading system for cryptocurrency futures markets. The system implements a **multi-factor alpha engine** with 8 engineered signal features, **adaptive risk management** using Kelly Criterion position sizing, and **real-time market microstructure analysis** via WebSocket streams.

Built as a modular pipeline architecture that separates data ingestion, feature engineering, signal generation, risk management, and order execution into independent, testable components.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [Signal Pipeline](#signal-pipeline)
- [Alpha Engine: 8 Signal Factors](#alpha-engine-8-signal-factors)
- [Risk Management Framework](#risk-management-framework)
- [Scoring & Decision Engine](#scoring--decision-engine)
- [Market Regime Detection](#market-regime-detection)
- [Execution Modes](#execution-modes)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Installation & Deployment](#installation--deployment)
- [Configuration](#configuration)
- [Backtesting](#backtesting)
- [Monitoring & Alerts](#monitoring--alerts)
- [Methodology & Design Decisions](#methodology--design-decisions)
- [Limitations & Future Work](#limitations--future-work)
- [License](#license)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        ALPHA-SCALP BOT                                  │
│                   Event-Driven Trading System                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   DATA LAYER                                                            │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                 │
│   │  REST API    │  │  WebSocket   │  │  Order Book  │                 │
│   │  (Polling)   │  │  (Streaming) │  │  (Depth 20)  │                 │
│   └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                 │
│          │                 │                 │                          │
│          └─────────────────┼─────────────────┘                          │
│                            │                                            │
│   FEATURE ENGINEERING      ▼                                            │
│   ┌─────────────────────────────────────────────┐                       │
│   │              FeatureCache                    │                      │
│   │  EMA(9,21) · RSI(14) · MACD · Bollinger     │                      │
│   │  ATR(14) · OBV · VWAP · Nadaraya-Watson     │                      │
│   │  CVD · Volume Ratio · ADX Regime             │                      │
│   └──────────────────────┬──────────────────────┘                       │
│                          │                                              │
│   ALPHA GENERATION       ▼                                              │
│   ┌─────────────────────────────────────────────┐                       │
│   │              AlphaEngine                     │                      │
│   │  8 Independent Signal Voters (-2 to +2)      │                      │
│   │  EMA Cross · EMA Trend · RSI · NW Envelope   │                      │
│   │  Volume · BB Squeeze · ADX Regime · CVD       │                     │
│   └──────────────────────┬──────────────────────┘                       │
│                          │                                              │
│   SCORING & DECISION     ▼                                              │
│   ┌─────────────────────────────────────────────┐                       │
│   │            SignalScoring                     │                      │
│   │  Weighted Aggregation · Regime-Aware Weights │                      │
│   │  Threshold: |score| >= 3.0 → BUY/SELL       │                      │
│   │  Confidence = min(|score| / 6, 1.0)          │                      │
│   └──────────────────────┬──────────────────────┘                       │
│                          │                                              │
│   RISK MANAGEMENT        ▼                                              │
│   ┌─────────────────────────────────────────────┐                       │
│   │              RiskEngine                      │                      │
│   │  Kelly Criterion Sizing · 3% Kill Switch     │                      │
│   │  ATR Trailing Stops · Regime SL/TP Adjust    │                      │
│   │  Circuit Breaker · Concurrent Trade Limiter  │                      │
│   └──────────────────────┬──────────────────────┘                       │
│                          │                                              │
│   EXECUTION              ▼                                              │
│   ┌─────────────────────────────────────────────┐                       │
│   │            OrderExecutor                     │                      │
│   │  Bracket Orders (SL + TP) · Isolated Margin  │                      │
│   │  Spread Guard · Slippage Tolerance           │                      │
│   └──────────────────────┬──────────────────────┘                       │
│                          │                                              │
│   MONITORING             ▼                                              │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                 │
│   │  Telegram    │  │  Trade       │  │  Weight      │                 │
│   │  Alerts      │  │  Tracker V2  │  │  Optimizer   │                 │
│   └──────────────┘  └──────────────┘  └──────────────┘                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Signal Pipeline

The system processes market data through a **5-stage pipeline**, where each stage is an independent module with clearly defined inputs and outputs:

```
Market Data → FeatureCache → AlphaEngine → SignalScoring → RiskEngine → OrderExecutor
```

| Stage | Module | Input | Output |
|-------|--------|-------|--------|
| 1. Feature Engineering | `feature_cache.py` | Raw OHLCV + Order Book | `FeatureSet` (30+ computed indicators) |
| 2. Alpha Generation | `alpha_engine.py` | `FeatureSet` | `AlphaVotes` (8 directional votes, -2 to +2) |
| 3. Signal Scoring | `signal_scoring.py` | `AlphaVotes` + regime | `ScoringResult` (action, score, confidence) |
| 4. Risk Gate | `risk_engine.py` | Score + portfolio state | Position size + SL/TP levels |
| 5. Execution | `order_executor.py` | Trade parameters | Bracket order on exchange |

---

## Alpha Engine: 8 Signal Factors

Each factor independently analyzes a different market dimension and casts a **directional vote** from -2 (strong short) to +2 (strong long):

| # | Factor | Type | Logic | Vote Range |
|---|--------|------|-------|------------|
| 1 | **EMA Crossover** | Momentum | EMA(9) crosses EMA(21) | +/-2 on cross |
| 2 | **EMA Trend** | Trend | Price position relative to EMA alignment | +/-1 |
| 3 | **RSI** | Mean Reversion | 5-zone classification (< 25, 25-35, 35-65, 65-75, > 75) | +/-2 at extremes |
| 4 | **Nadaraya-Watson Envelope** | Mean Reversion | Non-parametric kernel regression bands | +/-2 on band cross |
| 5 | **Volume** | Confirmation | Volume spike detection (1.5x / 2x average) | +/-2 with direction |
| 6 | **Bollinger Squeeze** | Volatility | Bandwidth contraction → anticipated breakout | +/-1 |
| 7 | **ADX Regime** | Context | Trend strength filter (trend-follow vs mean-revert) | +/-1 |
| 8 | **CVD** | Order Flow | Cumulative Volume Delta slope + divergence detection | +/-2 clamped |

### Scoring Formula

```
weighted_score = Σ (vote_i × weight_i)    for i = 1..8

Decision:
  score >= +3.0  →  BUY
  score <= -3.0  →  SELL
  otherwise      →  HOLD

Confidence (for position sizing):
  confidence = min(|score| / 6.0, 1.0)
```

### Default Signal Weights

| Signal | Weight | Rationale |
|--------|--------|-----------|
| EMA Crossover | 1.5 | Fresh crossovers are high-conviction directional signals |
| RSI | 1.2 | RSI extremes have strong mean-reversion properties |
| NW Envelope | 1.3 | Non-parametric regression captures non-linear price dynamics |
| CVD | 1.1 | Order flow reveals hidden buying/selling pressure |
| Volume | 1.0 | Standard confirmation weight |
| EMA Trend | 0.8 | Supporting signal, not primary |
| ADX Regime | 0.8 | Contextual filter, not directional |
| BB Squeeze | 0.7 | Anticipatory signal, less definitive |

Weights are stored in `weights.json` and support **per-regime overrides**. The `WeightOptimizer` module auto-tunes weights after accumulating sufficient trade history (30+ trades) using performance attribution analysis.

---

## Risk Management Framework

The `RiskEngine` implements a multi-layered risk management system inspired by institutional trading practices:

### Position Sizing: Kelly Criterion

```
Kelly Fraction f* = (p × b - q) / b

where:
  p = historical win rate
  b = average win / average loss ratio
  q = 1 - p

Applied as Quarter-Kelly (f*/4) to account for:
  - Parameter estimation uncertainty
  - Non-stationary market conditions
  - Fat-tailed return distributions
```

Falls back to **fixed fractional sizing** (1% risk per trade) during the warm-up period before sufficient trade history is available.

### Risk Controls

| Control | Mechanism | Threshold |
|---------|-----------|----------|
| **Daily Kill Switch** | Halts all trading if equity drawdown exceeds limit | 3% daily drawdown |
| **Circuit Breaker** | Pauses on realized loss accumulation | 3% realized daily loss |
| **ATR Trailing Stop** | Ratchets stop in favorable direction after +0.4% move | 1.0x ATR trail distance |
| **Regime-Aware SL** | Widens stop-loss in trending/volatile markets | +20% SL in trends |
| **Regime-Aware TP** | Extends take-profit to let trends run | +30% TP in trends |
| **Spread Guard** | Rejects trades when bid-ask spread is too wide | 0.05% max spread |
| **Concurrent Limiter** | Caps total open positions across strategies | 3 max concurrent |
| **Pre-Trade Gate** | Runs ALL checks before any order | `can_open_trade()` |

### Stop-Loss / Take-Profit

Supports both **percentage-based** and **ATR-based** SL/TP calculation with automatic taker fee buffering:

```
Percentage Mode:  SL = entry × (1 ± SL% ± fee_buffer)
ATR Mode:         SL = entry ± (ATR × multiplier × regime_factor)
```

---

## Market Regime Detection

The system classifies market conditions into three regimes using **ADX (Average Directional Index)** and adapts its behavior accordingly:

| Regime | ADX Range | Strategy Adaptation |
|--------|-----------|--------------------|
| **TRENDING** | 25 - 40 | Follow momentum signals, standard SL/TP |
| **VOLATILE** | > 40 | Ride strong trends, widen SL (+20%) and TP (+30%) |
| **RANGING** | < 25 | Favor mean-reversion signals, tighter SL/TP |

Regime detection feeds into:
- Signal weight selection (per-regime weight profiles)
- SL/TP distance calculation
- ADX regime vote in the alpha engine

---

## Execution Modes

### REST Polling Mode (Phase 2)
```
Loop every 5 seconds:
  1. Fetch latest candles via REST API
  2. Compute features → Generate votes → Score signals
  3. Execute trades if score exceeds threshold
```

### WebSocket Streaming Mode (Phase 3)
```
Event-driven callbacks:
  on_candle_complete  → Full pipeline re-evaluation
  on_book_update      → Spread guard + order book analysis
  on_price_jump       → Rapid signal check (> 15 bps move)
  on_book_invalidated → Pause trading until book recovers
```

WebSocket mode provides **sub-second signal latency** compared to 5-second polling intervals, critical for scalping strategies where entry timing directly impacts profitability.

---

## Tech Stack

| Category | Technology | Purpose |
|----------|-----------|----------|
| **Language** | Python 3.11+ | Async-first with type hints |
| **Exchange** | CCXT 4.x | Unified exchange abstraction |
| **Data** | pandas + pandas-ta | OHLCV manipulation + 130+ technical indicators |
| **Math** | NumPy + SciPy | Nadaraya-Watson kernel regression, statistical functions |
| **Streaming** | websockets | Real-time market data ingestion |
| **HTTP** | httpx | Async HTTP for Telegram alerts |
| **Logging** | Loguru | Structured logging with rotation |
| **Config** | python-dotenv | Environment-based configuration |
| **Deployment** | Docker + docker-compose | Containerized production deployment |
| **Monitoring** | Telegram Bot API | Real-time trade alerts + heartbeat monitoring |

---

## Project Structure

```
alpha-scalp-bot/
│
├── main.py                 # Entry point: polling + WebSocket modes, orchestration
├── config.py               # 50+ configurable parameters from environment
│
├── feature_cache.py        # Feature engineering: 30+ indicators with caching
├── alpha_engine.py         # 8 independent alpha signal voters
├── signal_scoring.py       # Weighted aggregation + threshold decision engine
│
├── risk_engine.py          # Kelly sizing, kill switch, trailing stops, regime SL/TP
├── order_executor.py       # Bracket order execution with spread guard
│
├── strategy.py             # Core scalp strategy logic
├── swing_strategy.py       # Multi-asset swing trading strategy
│
├── market_state.py         # Live order book state + price jump detection
├── ws_manager.py           # Binance WebSocket connection manager
├── state_dispatcher.py     # Event routing for WebSocket callbacks
│
├── trade_tracker.py        # Trade journal V1
├── trade_tracker_v2.py     # Enhanced journal: per-signal P&L attribution, Sharpe
├── weight_optimizer.py     # Auto-tune signal weights from trade performance
│
├── telegram_alerts.py      # Telegram notifications: trades, heartbeat, stats
├── backtest.py             # Historical backtesting framework
│
├── Dockerfile              # Multi-stage production build
├── docker-compose.yml      # Container orchestration with auto-restart
├── .dockerignore           # Build context optimization
├── requirements.txt        # Pinned Python dependencies
├── .env.example            # Template for environment variables
└── .gitignore              # Security: excludes .env, state files, secrets
```

---

## Installation & Deployment

### Prerequisites

- Python 3.11+ (or Docker)
- Binance Futures account (Testnet supported for paper trading)
- Telegram Bot Token (for monitoring alerts)

### Local Development

```bash
# Clone the repository
git clone https://github.com/DevSiddh/alpha-scalp-bot.git
cd alpha-scalp-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Run the bot
python main.py
```

### Docker Deployment (Production)

```bash
# Clone and configure
git clone https://github.com/DevSiddh/alpha-scalp-bot.git
cd alpha-scalp-bot
cp .env.example .env
nano .env  # Add your API keys

# Build and run
docker-compose up -d --build

# Monitor logs
docker-compose logs -f

# Update to latest version
git pull && docker-compose up -d --build
```

The Docker setup includes:
- **Auto-restart** on crash (`restart: unless-stopped`)
- **Log rotation** to prevent disk overflow
- **Health checks** via Telegram heartbeat
- **Isolated environment** with pinned dependencies

---

## Configuration

All parameters are configured via environment variables (`.env` file). Key parameters:

### Trading Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SYMBOL` | BTC/USDT | Trading pair |
| `TIMEFRAME` | 1m | Candle interval |
| `LEVERAGE` | 5x | Isolated margin leverage |
| `RISK_PER_TRADE` | 1% | Fixed fractional risk (pre-Kelly) |
| `STOP_LOSS_PCT` | 0.5% | Default stop-loss distance |
| `TAKE_PROFIT_PCT` | 1.0% | Default take-profit distance |
| `MAX_OPEN_POSITIONS` | 1 | Maximum concurrent scalp positions |

### Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `DAILY_DRAWDOWN_LIMIT` | 3% | Kill switch threshold |
| `DAILY_LOSS_LIMIT` | 3% | Circuit breaker threshold |
| `MAX_CONCURRENT_TRADES` | 3 | Total position limit (scalp + swing) |
| `MAX_SPREAD_PCT` | 0.05% | Spread guard rejection threshold |

### Timeframe Auto-Presets

The system automatically adjusts indicator periods and SL/TP distances based on the selected timeframe:

| Timeframe | EMA Fast/Slow | SL | TP | ATR SL Mult |
|-----------|--------------|-----|-----|-------------|
| 1m | 9 / 21 | 0.5% | 1.0% | 1.5x |
| 3m | 12 / 34 | 0.8% | 1.8% | 1.5x |
| 5m | 12 / 34 | 1.2% | 2.5% | 1.8x |

---

## Backtesting

```bash
python backtest.py
```

The backtesting module replays historical candle data through the same pipeline used in live trading, ensuring consistency between backtest results and live performance. Outputs include:

- Total return and drawdown curves
- Win rate, profit factor, and Sharpe ratio
- Per-signal P&L attribution
- Regime-tagged trade analysis

---

## Monitoring & Alerts

The Telegram integration provides real-time visibility into bot operations:

### Trade Alerts
```
[ENTRY] BUY BTC/USDT @ $97,450.20
Score: +4.30 | Confidence: 71.7%
Signals: ema_cross(+2) rsi(+1) nw_envelope(+2)
SL: $96,963.08 | TP: $98,424.70
Size: 0.001 BTC ($97.45)
```

### Heartbeat Monitoring
```
[PULSE] Alpha-Scalp Bot Alive
Uptime    : 4h 32m
Book      : OK
Spread    : 0.8 bps
Last Sig  : SELL (-3.40)
Regime    : TRENDING
Trades    : 3 (session) / 12 (all-time)
PnL       : $+2.45 (session) / $+15.80 (all-time)
```

---

## Methodology & Design Decisions

### Why Multi-Factor Over Single-Indicator?

Single indicators generate excessive false signals in noisy 1-minute data. By requiring **consensus across 8 independent factors** with a score threshold, the system achieves higher precision at the cost of lower trade frequency — a favorable tradeoff for scalping where transaction costs are significant.

### Why Kelly Criterion Over Fixed Sizing?

Fixed fractional sizing treats all trades equally regardless of edge quality. Kelly Criterion allocates **more capital to higher-confidence signals** (score 6 = 100% Kelly fraction vs score 3 = 50%), maximizing long-term geometric growth rate while Quarter-Kelly application provides a safety margin against estimation errors.

### Why Regime Detection?

A momentum strategy that works in trending markets will lose money in ranging markets (and vice versa). By detecting the current regime via ADX, the system **adapts both its signal weights and risk parameters** in real-time, reducing regime-mismatch losses.

### Why Nadaraya-Watson Over Traditional Bands?

Traditional Bollinger Bands assume a Gaussian price distribution. The **Nadaraya-Watson kernel regression** is a non-parametric estimator that adapts to the actual price distribution, providing more accurate support/resistance levels in non-normal market conditions.

---

## Limitations & Future Work

### Current Limitations

- **Limited live validation** — System is in paper trading phase; real-money performance may differ due to slippage and partial fills
- **Single exchange** — Currently supports Binance Futures only
- **No ML model integration** — Alpha factors use statistical rules, not learned models
- **Backtest limitations** — Does not simulate order book impact or queue position

### Planned Improvements

- [ ] Walk-forward optimization with out-of-sample validation
- [ ] Multi-exchange support (Bybit, OKX)
- [ ] Reinforcement learning for dynamic weight adjustment
- [ ] Orderbook imbalance features from Level 2 data
- [ ] Portfolio-level risk management across correlated assets
- [ ] Web dashboard for real-time P&L visualization

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

**Disclaimer:** This software is for educational and research purposes. Cryptocurrency trading involves substantial risk of loss. Past performance does not guarantee future results. Always trade responsibly and never risk more than you can afford to lose.