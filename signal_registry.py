"""Signal Registry — single source of truth for all signal metadata.

GP-S4: Every signal name, default weight, category, phase1 eligibility,
and built-in regime disabling lives here.  AlphaEngine and SignalScoring
both read from this registry instead of maintaining separate lists.

CLAUDE.md rule — Phase 1 signals only until 200 live trades:
    bb_squeeze, vwap_cross, liquidity_sweep, trade_aggression,
    ob_imbalance, mtf_bias, funding_bias
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PHASE1_SIGNALS: frozenset[str] = frozenset({
    "bb_squeeze",
    "vwap_cross",
    "liquidity_sweep",
    "trade_aggression",
    "ob_imbalance",
    "mtf_bias",
    "funding_bias",
})

LIVE_TRADE_PHASE2_THRESHOLD: int = 200  # unlock Phase-2 signals after this many live trades


# ---------------------------------------------------------------------------
# Signal metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalMeta:
    """Immutable descriptor for one signal."""
    name: str
    default_weight: float
    category: str          # momentum | mean_reversion | volume | order_flow | bias
    description: str
    phase1: bool           # True = active during Phase 1 (< LIVE_TRADE_PHASE2_THRESHOLD)
    disabled_regimes: tuple[str, ...] = ()  # regimes where this signal is always zero-weighted


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SignalRegistry:
    """Single source of truth for all 17 signal definitions."""

    _SIGNALS: dict[str, SignalMeta] = {
        # ── Momentum ────────────────────────────────────────────────────
        "ema_cross": SignalMeta(
            name="ema_cross", default_weight=1.4,
            category="momentum", phase1=False,
            description="EMA fast/slow crossover (±0.1% deadband)",
        ),
        "macd_cross": SignalMeta(
            name="macd_cross", default_weight=1.2,
            category="momentum", phase1=False,
            description="MACD line vs signal line crossover",
        ),
        "adx_filter": SignalMeta(
            name="adx_filter", default_weight=1.0,
            category="momentum", phase1=False,
            description="ADX regime strength filter",
        ),
        # ── Mean Reversion ───────────────────────────────────────────────
        "rsi_zone": SignalMeta(
            name="rsi_zone", default_weight=1.2,
            category="mean_reversion", phase1=False,
            description="RSI oversold (<35) / overbought (>65) zones",
        ),
        "bb_bounce": SignalMeta(
            name="bb_bounce", default_weight=1.0,
            category="mean_reversion", phase1=False,
            description="Price bouncing at Bollinger Band edges",
            disabled_regimes=("TRENDING_DOWN",),
        ),
        "nw_signal": SignalMeta(
            name="nw_signal", default_weight=1.2,
            category="mean_reversion", phase1=False,
            description="Nadaraya-Watson envelope cross",
        ),
        # ── Volume ───────────────────────────────────────────────────────
        "obv_trend": SignalMeta(
            name="obv_trend", default_weight=0.9,
            category="volume", phase1=False,
            description="On-balance volume trend direction",
        ),
        "volume_spike": SignalMeta(
            name="volume_spike", default_weight=1.0,
            category="volume", phase1=False,
            description="Volume >= VOL_SPIKE_MULT × rolling average",
        ),
        "bb_squeeze": SignalMeta(
            name="bb_squeeze", default_weight=1.3,
            category="volume", phase1=True,
            description="Bollinger Band compression breakout",
        ),
        # ── Order Flow ───────────────────────────────────────────────────
        "vwap_cross": SignalMeta(
            name="vwap_cross", default_weight=1.1,
            category="order_flow", phase1=True,
            description="Price crosses VWAP (intra-session level)",
        ),
        "ob_imbalance": SignalMeta(
            name="ob_imbalance", default_weight=1.1,
            category="order_flow", phase1=True,
            description="Order book bid/ask depth ratio",
        ),
        "trade_aggression": SignalMeta(
            name="trade_aggression", default_weight=1.3,
            category="order_flow", phase1=True,
            description="Taker buy/sell ratio from recent trades",
        ),
        "liquidity_wall": SignalMeta(
            name="liquidity_wall", default_weight=0.8,
            category="order_flow", phase1=False,
            description="Large bid/ask wall within 0.3% of price",
        ),
        "liquidity_sweep": SignalMeta(
            name="liquidity_sweep", default_weight=1.7,
            category="order_flow", phase1=True,
            description="Stop-hunt sweep detection (2-candle cooldown)",
        ),
        # ── Bias ─────────────────────────────────────────────────────────
        "swing_bias": SignalMeta(
            name="swing_bias", default_weight=1.6,
            category="bias", phase1=False,
            description="4h EMA50/200 cross + RSI — injected from SwingStrategy",
        ),
        "funding_bias": SignalMeta(
            name="funding_bias", default_weight=0.8,
            category="bias", phase1=True,
            description="Negative funding → BUY, positive → SELL (8h cache)",
        ),
        "mtf_bias": SignalMeta(
            name="mtf_bias", default_weight=1.5,
            category="bias", phase1=True,
            description="15m EMA8/20 + RSI14 multi-timeframe confirmation",
        ),
    }

    # ── Public read API ─────────────────────────────────────────────────────

    @classmethod
    def get(cls, name: str) -> SignalMeta:
        """Return SignalMeta for *name*. Raises KeyError if unknown."""
        return cls._SIGNALS[name]

    @classmethod
    def all_names(cls) -> list[str]:
        """All 17 signal names in canonical order."""
        return list(cls._SIGNALS.keys())

    @classmethod
    def default_weights(cls) -> dict[str, float]:
        """Default weight for every signal — authoritative source for SignalScoring."""
        return {name: meta.default_weight for name, meta in cls._SIGNALS.items()}

    @classmethod
    def phase1_names(cls) -> list[str]:
        """Signals active during Phase 1 (< LIVE_TRADE_PHASE2_THRESHOLD live trades)."""
        return [n for n, m in cls._SIGNALS.items() if m.phase1]

    @classmethod
    def phase2_names(cls) -> list[str]:
        """Signals only unlocked at >= LIVE_TRADE_PHASE2_THRESHOLD live trades."""
        return [n for n, m in cls._SIGNALS.items() if not m.phase1]

    @classmethod
    def by_category(cls, category: str) -> list[str]:
        """All signal names belonging to *category*."""
        return [n for n, m in cls._SIGNALS.items() if m.category == category]

    # ── Gate logic ──────────────────────────────────────────────────────────

    @classmethod
    def is_enabled(
        cls,
        name: str,
        regime: str,
        live_trade_count: int,
        disabled_by_regime: dict[str, list[str]] | None = None,
    ) -> bool:
        """Return False if this signal should be zero-weighted for this candle.

        Checks (in order):
        1. Unknown signal → False
        2. Phase gate — Phase 2 signals blocked until LIVE_TRADE_PHASE2_THRESHOLD
        3. Built-in disabled_regimes on SignalMeta
        4. External disabled_by_regime config (DISABLED_SIGNALS_BY_REGIME in config.py)
        """
        meta = cls._SIGNALS.get(name)
        if meta is None:
            return False

        # Phase gate
        if not meta.phase1 and live_trade_count < LIVE_TRADE_PHASE2_THRESHOLD:
            return False

        # Built-in regime disable
        if regime in meta.disabled_regimes:
            return False

        # External regime disable
        if disabled_by_regime and name in disabled_by_regime.get(regime, []):
            return False

        return True
