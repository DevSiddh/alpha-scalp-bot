# Grand Prix Alpha-Scalp

A self-adapting, multi-strategy BTC/USDT perpetual futures scalper using Thompson Sampling tournament selection, probabilistic HMM regime detection, and autonomous loss learning.

---

**Tests: 242 passing** | **Steps: 13/13 complete** | **Status: Demo Trading** | **Python 3.11**

---

## Architecture

```
MARKET DATA
    │
    ▼
┌─────────────────────────────────────────┐
│ LAYER 1 — DATA INGESTION                │
│                                         │
│  BinanceWSManager                       │
│    │ routes by symbol                   │
│    ▼                                    │
│  SymbolContext (one per symbol)         │
│    ├── FeatureCache (OHLCV + indicators)│
│    └── OrderFlowCache (trade stream)    │
└─────────────┬───────────────────────────┘
              │ FeatureSet
              ▼
┌─────────────────────────────────────────┐
│ LAYER 2 — ALPHA GENERATION              │
│                                         │
│  SignalRegistry (11-signal metadata)    │
│  AlphaEngine (stateless)               │
│  SubStrategyManager                     │
│    applies microstructure + swing gates │
└─────────────┬───────────────────────────┘
              │ SubStrategySignals
              ▼
┌─────────────────────────────────────────┐
│ LAYER 3 — TOURNAMENT                    │
│                                         │
│  ShadowTracker → Beta(α,β) per strategy │
│  TournamentEngine → Thompson Sampling   │
│  StrategyRouter → promote/bench         │
└─────────────┬───────────────────────────┘
              │ TournamentResult
              ▼
┌─────────────────────────────────────────┐
│ LAYER 4 — RISK + EXECUTION              │
│                                         │
│  RiskEngine (6-gate cascade)            │
│  PortfolioCorrelationGuard              │
│  OrderExecutor (limit/market by regime) │
│  ExitEngine (4-state machine per trade) │
└─────────────┬───────────────────────────┘
              │ TradeResult
              ▼
┌─────────────────────────────────────────┐
│ LAYER 5 — INTELLIGENCE                  │
│                                         │
│  TradeTrackerV2 (trades + shadow JSONL) │
│  DeepSeekPitBoss (Sunday audit, LLM)    │
│  HypothesisTracker (shadow test rules)  │
│  TelegramAlerts                         │
└─────────────────────────────────────────┘
```

---

## Six Strategies

| # | Name | Regime | Mechanism |
|---|------|--------|-----------|
| 1 | **Breakout** | TRENDING_UP / TRENDING_DOWN | BB squeeze + trade aggression momentum |
| 2 | **VWAP Mean Reversion** | RANGING / NEUTRAL | RSI zone + VWAP cross mean reversion |
| 3 | **Liquidity Sweep Reversal** | All except VOLATILE | Stop hunt detection via OB imbalance |
| 4 | **Trend Pullback** | TRENDING_UP / TRENDING_DOWN | MTF bias + higher timeframe pullback entry |
| 5 | **Order Flow Momentum** | All regimes | Microstructure-driven (aggression + imbalance) |
| 6 | **Cash Mode** | Any | Deliberate flat position, tracked as strategy 6 |

---

## Key Algorithms

- **Thompson Sampling** — Bayesian multi-armed bandit tournament selects the highest-edge strategy each candle using Beta(α,β) distributions maintained by ShadowTracker
- **GaussianHMM** — 5-state probabilistic regime classifier (TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, TRANSITION); fallback to ADX-based detection
- **Kyle's Lambda** — Adverse selection guard estimates price impact from order flow before allowing position entry
- **Exponential decay reward shaping** — Recent shadow trades weighted heavier in Beta updates; older outcomes decay
- **Jaccard similarity deduplication** — FIX-7: new loss-pattern hypotheses with >70% token overlap to existing ones are rejected before shadow testing begins
- **Kalman Filter** — Phase 2 adaptive price smoother (Q=0.001, R=0.01); replaces EMA+NW after 200 live trades

---

## Risk Controls

- **6-gate pre-trade filter cascade** — kill switch → three-strike → equity floor → spread guard → Kyle's lambda → max positions; all must pass before any order
- **Three-Strike 90min cooldown** — 3 consecutive losses triggers automatic 90-minute trading pause
- **Equity floor circuit breaker** — balance drops to 80% of starting equity → full bot shutdown
- **Dynamic leverage** — ceiling set by regime (TRENDING 5×, RANGING 3×, VOLATILE 2×), scaled by Thompson confidence score and drawdown state
- **Portfolio correlation guard** — blocks new positions when Pearson ρ > 0.75 on rolling 50-candle returns across open symbols
- **Kyle's Lambda position sizing** — adverse selection coefficient from trade stream adjusts allowable position size in real time

---

## Project Structure

```
alpha-scalp-bot/
│
├── main.py                      # Entry point: WebSocket + polling orchestration
├── config.py                    # All env-var configuration
│
│  ── Layer 1: Data Ingestion ──
├── ws_manager.py                # Binance WebSocket connection manager
├── market_state.py              # Live order book + price jump detection
├── state_dispatcher.py          # Event routing for WebSocket callbacks
├── feature_cache.py             # 30+ indicators (EMA, RSI, ATR, BB, VWAP, ADX)
├── symbol_context.py            # Per-symbol isolated state + SymbolContextRegistry
├── passive_shadow.py            # ETH/SOL passive shadow manager (Step 13)
│
│  ── Layer 2: Alpha Generation ──
├── signal_registry.py           # Declarative source of truth for 11 signals
├── alpha_engine.py              # AlphaVotes generation (stateless)
├── signal_scoring.py            # Weighted aggregation + spike filter
├── sub_strategy_manager.py      # 5 sub-strategies + hard gates
│
│  ── Layer 3: Tournament ──
├── shadow_tracker.py            # Ghost trades + Beta(α,β) per strategy
├── tournament_engine.py         # Thompson Sampling + HMM scheduler
├── strategy_router.py           # Promote/bench lifecycle management
│
│  ── Layer 4: Risk + Execution ──
├── risk_engine.py               # 6-gate pre-trade filter + dynamic leverage
├── portfolio_correlation_guard.py # Cross-symbol Pearson ρ gate
├── order_executor.py            # Limit/market orders by regime
├── exit_engine.py               # 4-state exit machine per open position
│
│  ── Layer 5: Intelligence ──
├── trade_tracker_v2.py          # Live + shadow trade journal (JSONL)
├── deepseek_pit_boss.py         # Weekly LLM loss audit (audit-only)
├── hypothesis_tracker.py        # Shadow-test and approve/reject loss rules
├── block_conditions.py          # Runtime block condition registry reader
├── telegram_alerts.py           # All system alerts
│
│  ── Supporting ──
├── strategy.py                  # Core scalp strategy
├── swing_strategy.py            # Multi-symbol swing strategy
├── pandas_ta.py                 # pandas-ta compatibility shim (wraps `ta`)
├── backtest.py                  # Historical replay framework
├── weight_optimizer.py          # Frozen (WEIGHTS_LOCKED=true)
├── requirements.txt
├── Dockerfile                   # Python 3.11-slim + pip deps
├── docker-compose.yml           # Production container config
├── scripts/
│   ├── setup_vps.sh             # One-command VPS bootstrap
│   └── auto_update.sh           # git pull + smart restart (cron every 5 min)
└── tests/                       # 242 tests across all 13 components
```

---

## Deployment — VPS (Docker, recommended)

### One-command setup on a fresh Ubuntu VPS

```bash
# 1. Copy setup script to the VPS and run it
curl -O https://raw.githubusercontent.com/DevSiddh/alpha-scalp-bot/main/scripts/setup_vps.sh
chmod +x setup_vps.sh && sudo ./setup_vps.sh
```

The script:
- Installs Docker
- Clones this repo to `/opt/alpha-scalp`
- Creates `.env` from template and waits for you to fill in API keys
- Builds the image and starts the bot
- Installs a cron job that auto-updates every 5 minutes

### Auto-update flow

```
You push to main (from local machine)
        ↓
Cron on VPS runs auto_update.sh every 5 min
        ↓
git fetch → new commit detected?
        ↓ yes
Check bot_state.json for open positions
        ↓ no positions open
git pull → docker compose up -d
        ↓
Bot running on new code within 5 minutes
```

> **Safety**: the update script never restarts the container while a trade is open.
> If a position is open it defers and retries at the next 5-minute interval.

### Common VPS commands

```bash
docker logs -f alpha-scalp-bot          # live log stream
docker ps                               # status + health
docker inspect --format='{{.State.Health.Status}}' alpha-scalp-bot

cat /var/log/alpha-scalp-update.log     # auto-update history

docker compose down                     # stop bot
docker compose up -d                    # start bot
/opt/alpha-scalp/scripts/auto_update.sh  # force update now
```

### Switching from Demo to Live

```bash
# Edit .env on the VPS
nano /opt/alpha-scalp/.env

# Change:
BINANCE_DEMO_TRADING=false   # use real Binance Futures
INITIAL_BALANCE=<your USDT>  # actual account balance

# Restart
docker compose up -d
```

---

## Local Setup (development / testing)

```bash
git clone https://github.com/DevSiddh/alpha-scalp-bot.git
cd alpha-scalp-bot

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

pip install -r requirements.txt

cp .env.example .env
# Edit .env: add BINANCE_API_KEY, BINANCE_SECRET, TELEGRAM_BOT_TOKEN

python main.py
```

---

## Testing

```bash
pytest tests/ -v
```

242 tests across all 13 components. Tests use a no-op stub for exchange calls and run fully offline.

---

## Research

This project is being prepared for arxiv submission under **cs.LG / q-fin.TR**.
See `ARCHITECTURE/ARCHITECTURE.md` for full system design and component interaction diagrams.

---

## License

MIT
