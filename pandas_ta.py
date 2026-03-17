"""pandas_ta compatibility shim — wraps the `ta` library.

pandas-ta 0.3.x is no longer available on PyPI for Python 3.11.
This shim exposes the same function signatures that feature_cache.py
uses, backed by the `ta` library (pip install ta).

Functions implemented:
    ema(close, length)               → pd.Series
    rsi(close, length)               → pd.Series
    atr(high, low, close, length)    → pd.Series
    sma(close, length)               → pd.Series
    bbands(close, length, std)       → pd.DataFrame  (cols: BBL, BBM, BBU, BBB, BBP)
    adx(high, low, close, length)    → pd.DataFrame  (cols: ADX, DMP, DMN)
"""

from __future__ import annotations

import pandas as pd

try:
    import ta as _ta
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False


def _require_ta() -> None:
    if not _TA_AVAILABLE:
        raise ImportError(
            "pandas_ta shim requires the 'ta' library. "
            "Install it with: pip install ta"
        )


def ema(close: pd.Series, length: int = 14, **_) -> pd.Series | None:
    """Exponential Moving Average."""
    _require_ta()
    return _ta.trend.ema_indicator(close.astype(float), window=length)


def rsi(close: pd.Series, length: int = 14, **_) -> pd.Series | None:
    """Relative Strength Index."""
    _require_ta()
    return _ta.momentum.rsi(close.astype(float), window=length)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        length: int = 14, **_) -> pd.Series | None:
    """Average True Range."""
    _require_ta()
    return _ta.volatility.average_true_range(
        high.astype(float), low.astype(float), close.astype(float),
        window=length,
    )


def sma(close: pd.Series, length: int = 14, **_) -> pd.Series | None:
    """Simple Moving Average."""
    _require_ta()
    return _ta.trend.sma_indicator(close.astype(float), window=length)


def bbands(close: pd.Series, length: int = 20, std: float = 2.0,
           **_) -> pd.DataFrame | None:
    """Bollinger Bands.

    Returns a DataFrame with columns matching pandas-ta convention:
        BBL_<length>_<std>   (lower band)
        BBM_<length>_<std>   (middle band / SMA)
        BBU_<length>_<std>   (upper band)
        BBB_<length>_<std>   (bandwidth)
        BBP_<length>_<std>   (percent-b)
    """
    _require_ta()
    c = close.astype(float)
    bb = _ta.volatility.BollingerBands(c, window=length, window_dev=std)

    lower  = bb.bollinger_lband()
    middle = bb.bollinger_mavg()
    upper  = bb.bollinger_hband()

    # bandwidth and percent-b (match pandas-ta column semantics)
    bandwidth = (upper - lower) / middle.replace(0, float("nan"))
    pct_b = (c - lower) / (upper - lower).replace(0, float("nan"))

    suffix = f"_{length}_{std}"
    return pd.DataFrame({
        f"BBL{suffix}": lower,
        f"BBM{suffix}": middle,
        f"BBU{suffix}": upper,
        f"BBB{suffix}": bandwidth,
        f"BBP{suffix}": pct_b,
    }, index=close.index)


def adx(high: pd.Series, low: pd.Series, close: pd.Series,
        length: int = 14, **_) -> pd.DataFrame | None:
    """Average Directional Index.

    Returns a DataFrame with columns matching pandas-ta convention:
        ADX_<length>    (ADX line)
        DMP_<length>    (+DI line)
        DMN_<length>    (-DI line)
    """
    _require_ta()
    h = high.astype(float)
    l = low.astype(float)
    c = close.astype(float)
    ind = _ta.trend.ADXIndicator(h, l, c, window=length)

    suffix = f"_{length}"
    return pd.DataFrame({
        f"ADX{suffix}": ind.adx(),
        f"DMP{suffix}": ind.adx_pos(),
        f"DMN{suffix}": ind.adx_neg(),
    }, index=close.index)
