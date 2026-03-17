# Research

## Paper Title

**Grand Prix Alpha-Scalp: A Self-Adapting Multi-Strategy System for Cryptocurrency Futures Scalping Using Thompson Sampling Tournament Selection, Probabilistic Regime Detection, and Autonomous Loss Learning**

---

## Abstract

We present Grand Prix Alpha-Scalp, a production trading system for BTC/USDT perpetual futures that frames strategy selection as a Bayesian multi-armed bandit problem. At each 3-minute candle, a Thompson Sampling tournament draws from per-strategy Beta distributions maintained by a parallel shadow simulation layer, selecting the highest-expected-edge strategy without human intervention. Market regime is classified into five states (TRENDING_UP, TRENDING_DOWN, RANGING, VOLATILE, TRANSITION) using a Gaussian Hidden Markov Model trained on six months of historical data, with ADX-based fallback during model unavailability. Adverse selection risk is quantified in real time via Kyle's Lambda estimated from the live trade stream. A weekly DeepSeek LLM audit identifies recurring loss patterns, generates candidate block conditions, and routes them through a shadow-testing pipeline with statistical approval gates before any rule affects live execution. The system enforces strict component isolation across three simultaneously tracked symbols, with a shared portfolio correlation guard preventing concurrent directional exposure. All components are fully unit-tested across 242 test cases.

---

## Target Venue

- **Primary:** arXiv cs.LG (Machine Learning)
- **Cross-list:** arXiv q-fin.TR (Trading and Market Microstructure)

---

## Status

**In preparation.** System is currently in demo trading phase (Step 13 of 13). Paper will be submitted following completion of the two-week paper trading validation period.

---

## Key Contributions

1. Thompson Sampling tournament framing for intraday strategy selection with Beta distribution warm-start via passive shadow tracking
2. Five-state HMM regime classifier with graceful ADX fallback and Sunday retraining schedule
3. LLM-in-the-loop loss auditing pipeline with Jaccard similarity deduplication and statistical shadow-test approval gates
4. Strict per-symbol component isolation architecture enabling safe multi-asset operation from a shared codebase
5. Kyle's Lambda as a pre-trade adverse selection gate integrated into the risk engine filter cascade
