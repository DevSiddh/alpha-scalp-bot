# conftest.py — test collection setup
import sys
import os
import types
import warnings

# Add repo root to sys.path — must be first so all bot modules are importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# pandas_ta stub — injected when the real package is not installed.
# The affected tests only use FeatureSet/SwingStrategy as dataclasses;
# they never call the compute functions, so no-op stubs are sufficient.
# ---------------------------------------------------------------------------
try:
    import pandas_ta  # noqa: F401
except ModuleNotFoundError:
    import pandas as _pd

    def _series_stub(*args, **kwargs):
        """Return an empty Series so attribute access on the result is safe."""
        return _pd.Series(dtype=float)

    def _bbands_stub(series, length=20, std=2.0, **kwargs):
        import numpy as _np
        n = len(series)
        return _pd.DataFrame({
            f"BBL_{length}_{float(std)}": _np.nan,
            f"BBM_{length}_{float(std)}": _np.nan,
            f"BBU_{length}_{float(std)}": _np.nan,
            f"BBW_{length}_{float(std)}": _np.nan,
        }, index=series.index if hasattr(series, "index") else range(n))

    def _adx_stub(high, low, close, length=14, **kwargs):
        import numpy as _np
        return _pd.DataFrame({
            f"ADX_{length}": _np.nan,
            f"DMP_{length}": _np.nan,
            f"DMN_{length}": _np.nan,
        }, index=close.index if hasattr(close, "index") else range(len(close)))

    _stub = types.ModuleType("pandas_ta")
    _stub.ema = _series_stub
    _stub.rsi = _series_stub
    _stub.atr = _series_stub
    _stub.sma = _series_stub
    _stub.bbands = _bbands_stub
    _stub.adx = _adx_stub
    sys.modules["pandas_ta"] = _stub

# ---------------------------------------------------------------------------
# Suppress pandas-ta deprecation noise on Python 3.12+
# ---------------------------------------------------------------------------
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pandas_ta")
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas_ta")
warnings.filterwarnings("ignore", message=".*DataFrame.swapaxes.*")
warnings.filterwarnings("ignore", message=".*np.bool.*")
warnings.filterwarnings("ignore", message=".*np.int.*")
warnings.filterwarnings("ignore", message=".*np.float.*")
warnings.filterwarnings("ignore", message=".*mode.copy_on_write.*")
