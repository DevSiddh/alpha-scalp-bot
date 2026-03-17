# Grand Prix Alpha-Scalp — Future Roadmap

All 13 build steps are complete. This file tracks what comes next,
gated by live trade milestones. Check this at the start of every session.

---

## Current Status

| Item | State |
|------|-------|
| Build steps | 13 / 13 complete |
| Tests | 242 passing |
| Mode | Demo trading (paper) |
| Symbols active | BTC/USDT full pipeline |
| Symbols shadow | ETH/USDT, SOL/USDT |

---

## Phase 2 — After 50 Live Trades

- [ ] Verify Three-Strike cooldown triggers correctly in live conditions
- [ ] Verify equity floor check reads real balance (not paper balance)
- [ ] First DeepSeek PitBoss Sunday audit — check `loss_audit_log.jsonl` output
- [ ] Review `shadow_trades.jsonl` — confirm ETH/SOL Beta distributions are building
- [ ] Confirm `bot_state.json` is writing atomically on every state change
- [ ] Test restart reconciliation: kill bot mid-trade, restart, verify position restored
- [ ] Check Telegram alert latency on entry/exit events

---

## Phase 2 — After 200 Live Trades

- [ ] **Activate Kalman signal** (`kalman_signal`, Q=0.001, R=0.01)
  - Add to `signal_registry.py` Phase 2 block
  - Replaces EMA cross + NW signal
- [ ] **Activate FVG signal** (`fvg_signal`, institutional family)
  - Add to `signal_registry.py` Phase 2 block
- [ ] Review Kelly readiness — needs 300 trades minimum before enabling
- [ ] Evaluate ETH/USDT Beta distributions — ready for live activation?
- [ ] First HypothesisTracker approval cycle review
  - Are any block conditions approved yet?
  - Check `block_conditions_registry.json`

---

## Phase 2 — After 300 Live Trades

- [ ] **Enable Kelly sizing** (`KELLY_ENABLED=true`, `KELLY_MIN_TRADES=300`)
  - Ramp: 300 trades = starts, 500 trades = full fraction
  - Cap stays at 10% full Kelly
- [ ] **Activate ETH/USDT live** (if Beta distributions are warm)
  - Change `PASSIVE_SHADOW_SYMBOLS` to `SOL/USDT` only
  - Confirm `route_agent_activation()` is working correctly

---

## Phase 3 — After 500 Live Trades

- [ ] **Activate equal_highs_lows signal** (institutional family)
- [ ] **Activate llm_pattern signal** (contextual family, DeepSeek)
- [ ] **Activate SOL/USDT live** (if ETH is stable and Beta is warm)
- [ ] Full Kelly ramp complete — review position sizing in production
- [ ] Walk-forward validation: split live trades into in-sample / out-of-sample
- [ ] Review DeepSeek PitBoss audit quality after 3+ Sunday cycles

---

## Infrastructure — Any Time

- [ ] **VPS deployment** — move from local Windows to Linux VPS
  - Set up systemd service for auto-restart
  - Confirm `PASSIVE_SHADOW_SYMBOLS` runs 24/7
- [ ] **Telegram backup** — verify 5-minute auto-backup of key files
- [ ] **`shadow_trades.jsonl` rotation** — confirm PitBoss archive logic works
- [ ] **`.env.example`** — keep in sync with every new config key added
- [ ] **Log rotation** — confirm Loguru rotation doesn't fill disk on VPS
- [ ] **HMM retrain** — confirm Sunday retrain triggers after 500+ new candles

---

## Research & Documentation

- [ ] Complete 2-week paper trading validation period
- [ ] Document live system behavior (latency, fill rates, regime distribution)
- [ ] Write methodology section of arxiv paper (cs.LG / q-fin.TR)
- [ ] Submit to arxiv — **target: after 500 live trades**
- [ ] Add `ARCHITECTURE/` diagrams for HypothesisTracker lifecycle
- [ ] Add sequence diagram for multi-symbol candle routing

---

## Known Tech Debt (Low Priority)

- [ ] `main.py` does not yet call `PassiveShadowManager.start()` — wire in
- [ ] `main.py` does not yet call `registry.route_agent_activation()` per candle — wire in
- [ ] `PortfolioCorrelationGuard` not yet wired into `RiskEngine.can_open_trade()`
- [ ] `ExitEngine` instances not yet created on trade entry in `main.py`
- [ ] Paper slippage model (`PAPER_SLIPPAGE_PCT`) not yet applied in `OrderExecutor`

---

## Completed (Do Not Touch)

| Step | Component | Tests |
|------|-----------|-------|
| Step 1 | WeightOptimizer frozen, WEIGHTS_LOCKED | — |
| Step 2 | RiskEngine — Three-Strike, equity floor, Kelly gate | ✅ |
| Step 3 | Spike filter, spread guard, ATR validation | ✅ |
| Step 4 | SignalRegistry — 11 signals declarative | ✅ |
| Step 5 | SubStrategyManager — 5 strategies + gates | ✅ |
| Step 6 | ShadowTracker — Beta distributions, ghost trades | ✅ |
| Step 7 | TournamentEngine — Thompson Sampling | ✅ |
| Step 8 | StrategyRouter — promote/bench lifecycle | ✅ |
| Step 9 | ExitEngine — 4-state machine | ✅ |
| Step 10 | DeepSeekPitBoss + HypothesisTracker + BlockConditions | ✅ |
| Step 11 | SymbolContext + SymbolContextRegistry | ✅ |
| Step 12 | PortfolioCorrelationGuard | ✅ |
| Step 13 | PassiveShadowManager — ETH/SOL shadow | ✅ |
| FIX-7 | Jaccard similarity deduplication in HypothesisTracker | ✅ |
| Infra | pandas_ta shim, .venv, requirements.txt | ✅ |
| Docs | README, RESEARCH.md, ROADMAP.md | ✅ |

---

_Update this file at the start of each session. Move items to Completed when done._
